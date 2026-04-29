from __future__ import annotations

import json
import time
from collections.abc import Generator
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.agent.configuration import (
    provider_is_ready,
    list_provider_specs,
    load_agent_provider_config,
    resolve_provider_status,
    upsert_agent_provider_config,
)
from app.agent.provider import AgentProvider, ProposalRequest, ToolRequest, get_agent_provider
from app.agent.schemas import (
    ActionProposalDecisionRead,
    ActionProposalRead,
    AgentMessageCreate,
    AgentProviderConfigRead,
    AgentProviderConfigUpdate,
    AgentProviderOptionRead,
    AgentMessageRead,
    AgentThreadRead,
    AgentTurnAcceptedRead,
    AgentTurnEventRead,
    SetupItemRead,
    SetupSystemBindingRead,
    SiteSetupProfileRead,
)
from app.core.config import get_settings
from app.db.models import (
    ActionProposal,
    AuditEvent,
    ConversationEvent,
    ConversationMessage,
    ConversationThread,
    ConversationTurn,
    DebugCase,
    Site,
    SiteSetupProfile,
    utcnow,
)
from app.db.session import get_session_factory
from app.domain.schemas import DebugCaseRead, DebugExplainRequest
from app.services.dashboard import build_overview, update_site
from app.services.discovery import run_discovery
from app.services.knowledge import create_debug_case, get_debug_case
from app.services.network_scope import list_reachable_subnets
from app.services.targeted_probe import run_targeted_probe


def _serialize_agent_provider_config() -> AgentProviderConfigRead:
    config = load_agent_provider_config()
    runtime = resolve_provider_status(config)
    option_reads: list[AgentProviderOptionRead] = []
    for spec in list_provider_specs():
        state = config.providers.get(spec.provider_id)
        option_reads.append(
            AgentProviderOptionRead(
                provider_id=spec.provider_id,
                label=spec.label,
                description=spec.description,
                auth_kind=spec.auth_kind,
                base_url_default=spec.base_url_default,
                model_placeholder=spec.model_placeholder,
                supports_base_url=spec.supports_base_url,
                supports_model=spec.supports_model,
                selected=runtime.selected_provider == spec.provider_id,
                model=state.model if state is not None else "",
                base_url=state.base_url if state is not None else spec.base_url_default,
                api_key_configured=bool(state and state.api_key),
                ready=provider_is_ready(spec.provider_id, config),
            )
        )
    return AgentProviderConfigRead(
        selected_provider=runtime.selected_provider,
        effective_provider=runtime.effective_provider,
        ready=runtime.ready,
        message=runtime.message,
        provider_options=option_reads,
    )


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


def _get_or_create_setup_profile(session: Session) -> SiteSetupProfile:
    site = _get_site(session)
    profile = session.scalar(select(SiteSetupProfile).where(SiteSetupProfile.site_id == site.id).limit(1))
    if profile is None:
        profile = SiteSetupProfile(
            site_id=site.id,
            summary="Helios is ready to help discover devices and confirm how they belong to the home.",
            confirmed_systems=[],
            unresolved_items=[],
            user_notes=[],
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
    return profile


def _refresh_setup_profile_summary(profile: SiteSetupProfile) -> None:
    confirmed = len(profile.confirmed_systems or [])
    unresolved = len(profile.unresolved_items or [])
    if confirmed == 0 and unresolved == 0:
        profile.summary = "No home systems have been confirmed yet."
        return
    parts: list[str] = []
    if confirmed:
        parts.append(f"{confirmed} confirmed system(s)")
    if unresolved:
        parts.append(f"{unresolved} open setup question(s)")
    profile.summary = "Setup progress: " + ", ".join(parts) + "."


def _serialize_setup_profile(profile: SiteSetupProfile) -> SiteSetupProfileRead:
    return SiteSetupProfileRead(
        summary=profile.summary,
        confirmed_systems=[SetupSystemBindingRead.model_validate(item) for item in (profile.confirmed_systems or [])],
        unresolved_items=[SetupItemRead.model_validate(item) for item in (profile.unresolved_items or [])],
        user_notes=list(profile.user_notes or []),
        updated_at=profile.updated_at,
    )


def _thread_query():
    return (
        select(ConversationThread)
        .options(
            selectinload(ConversationThread.messages),
            selectinload(ConversationThread.proposals),
        )
        .order_by(ConversationThread.created_at.asc())
    )


def _get_or_create_primary_thread(session: Session) -> ConversationThread:
    site = _get_site(session)
    thread = session.scalar(_thread_query().where(ConversationThread.site_id == site.id).limit(1))
    if thread is None:
        thread = ConversationThread(
            id=f"thread-{uuid4().hex[:12]}",
            site_id=site.id,
            title="Helios setup assistant",
            status="active",
        )
        session.add(thread)
        session.commit()
        session.refresh(thread)
    return thread


def get_agent_provider_config() -> AgentProviderConfigRead:
    return _serialize_agent_provider_config()


def update_agent_provider_config(payload: AgentProviderConfigUpdate) -> AgentProviderConfigRead:
    upsert_agent_provider_config(
        provider_id=payload.provider_id,
        model=payload.model,
        base_url=payload.base_url,
        api_key=payload.api_key,
        clear_api_key=payload.clear_api_key,
        select_provider=True,
    )
    return _serialize_agent_provider_config()


def _serialize_message(message: ConversationMessage) -> AgentMessageRead:
    return AgentMessageRead(
        id=message.id,
        role=message.role,
        content=message.content,
        status=message.status,
        created_at=message.created_at,
        turn_id=message.turn_id,
    )


def _serialize_proposal(proposal: ActionProposal) -> ActionProposalRead:
    return ActionProposalRead(
        id=proposal.id,
        action_type=proposal.action_type,
        summary=proposal.summary,
        payload=proposal.payload or {},
        status=proposal.status,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
        resolved_at=proposal.resolved_at,
    )


def _latest_debug_case(session: Session) -> DebugCaseRead | None:
    debug_case = session.scalar(select(DebugCase).order_by(DebugCase.updated_at.desc()).limit(1))
    if debug_case is None:
        return None
    return get_debug_case(session, debug_case.id)


def _serialize_thread(session: Session, thread: ConversationThread, profile: SiteSetupProfile) -> AgentThreadRead:
    session.refresh(thread)
    return AgentThreadRead(
        id=thread.id,
        title=thread.title,
        status=thread.status,
        messages=[_serialize_message(message) for message in thread.messages],
        pending_proposals=[
            _serialize_proposal(proposal)
            for proposal in thread.proposals
            if proposal.status == "pending"
        ],
        setup_profile=_serialize_setup_profile(profile),
        latest_debug_case=_latest_debug_case(session),
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _welcome_message_text(session: Session) -> str:
    overview = build_overview(session)
    if not overview.devices:
        return (
            "I am Helios. I can scan your home network, explain what I find, and help you confirm how devices belong to "
            "systems like the heat pump, battery, PV, or EV charger."
        )
    names = ", ".join(device.name for device in overview.devices[:4])
    suffix = "" if len(overview.devices) <= 4 else f", and {len(overview.devices) - 4} more"
    return (
        f"I am Helios. I currently see {len(overview.devices)} detected device(s), including {names}{suffix}. "
        "Tell me what you want to set up and I will guide you through it."
    )


def _ensure_welcome_message(session: Session, thread: ConversationThread) -> None:
    if thread.messages:
        return
    message = ConversationMessage(
        id=f"msg-{uuid4().hex[:12]}",
        thread_id=thread.id,
        role="assistant",
        content=_welcome_message_text(session),
        status="completed",
    )
    session.add(message)
    session.commit()
    session.refresh(thread)


def get_agent_thread(session: Session) -> AgentThreadRead:
    profile = _get_or_create_setup_profile(session)
    thread = _get_or_create_primary_thread(session)
    _ensure_welcome_message(session, thread)
    return _serialize_thread(session, thread, profile)


def _context_snapshot(session: Session, thread: ConversationThread, profile: SiteSetupProfile) -> dict:
    overview = build_overview(session)
    site = _get_site(session)
    return {
        "site_id": site.id,
        "current_subnets": [entry.strip() for entry in site.local_subnet.split(",") if entry.strip()],
        "reachable_subnets": [
            {"cidr": option.cidr, "interface": option.interface, "label": option.label}
            for option in list_reachable_subnets()
        ],
        "devices": [
            {
                "id": device.id,
                "name": device.name,
                "device_type": device.device_type,
                "manufacturer": device.manufacturer,
                "model": device.model,
                "status": device.primary_status,
                "protocols": list(device.protocols),
                "capabilities": device.capabilities.model_dump(),
                "telemetry_keys": sorted(device.telemetry.keys()),
                "telemetry_preview": {
                    key: value
                    for key, value in list(device.telemetry.items())[:6]
                },
                "next_step": device.next_step,
            }
            for device in overview.devices
        ],
        "setup_profile": _serialize_setup_profile(profile).model_dump(),
        "recent_messages": [
            {
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
            }
            for message in thread.messages[-6:]
        ],
        "pending_proposals": [
            _serialize_proposal(proposal).model_dump()
            for proposal in thread.proposals
            if proposal.status == "pending"
        ],
    }


def create_agent_message(session: Session, payload: AgentMessageCreate) -> AgentTurnAcceptedRead:
    profile = _get_or_create_setup_profile(session)
    thread = _get_or_create_primary_thread(session)
    _ensure_welcome_message(session, thread)
    now = utcnow()
    user_message = ConversationMessage(
        id=f"msg-{uuid4().hex[:12]}",
        thread_id=thread.id,
        role="user",
        content=payload.content.strip(),
        status="completed",
        created_at=now,
        updated_at=now,
    )
    runtime = resolve_provider_status()
    turn = ConversationTurn(
        id=f"turn-{uuid4().hex[:12]}",
        thread_id=thread.id,
        user_message_id=user_message.id,
        provider_name=runtime.effective_provider,
        status="pending",
        created_at=now,
    )
    session.add_all([user_message, turn])
    session.commit()
    session.refresh(thread)
    return AgentTurnAcceptedRead(
        thread_id=thread.id,
        turn_id=turn.id,
        user_message=_serialize_message(user_message),
    )


def _next_event_index(turn: ConversationTurn) -> int:
    if not turn.events:
        return 0
    return max(event.event_index for event in turn.events) + 1


def _persist_event(session: Session, turn: ConversationTurn, event_type: str, payload: dict) -> AgentTurnEventRead:
    event = ConversationEvent(
        turn_id=turn.id,
        event_index=_next_event_index(turn),
        event_type=event_type,
        payload=jsonable_encoder(payload),
        created_at=utcnow(),
    )
    session.add(event)
    session.commit()
    session.refresh(turn)
    return AgentTurnEventRead(
        turn_id=turn.id,
        event_type=event.event_type,
        payload=event.payload or {},
        created_at=event.created_at,
    )


def _ensure_unresolved_item(profile: SiteSetupProfile, *, kind: str, label: str, details: str = "") -> None:
    unresolved_items = list(profile.unresolved_items or [])
    if any(item.get("kind") == kind and item.get("label") == label for item in unresolved_items):
        return
    unresolved_items.append(
        {
            "kind": kind,
            "label": label,
            "details": details,
            "status": "open",
        }
    )
    profile.unresolved_items = unresolved_items
    _refresh_setup_profile_summary(profile)


def _remove_unresolved_items(profile: SiteSetupProfile, *, kind: str, label: str) -> None:
    profile.unresolved_items = [
        item
        for item in (profile.unresolved_items or [])
        if not (item.get("kind") == kind and item.get("label") == label)
    ]
    _refresh_setup_profile_summary(profile)


def _upsert_confirmed_system(profile: SiteSetupProfile, payload: dict) -> None:
    confirmed = list(profile.confirmed_systems or [])
    replaced = False
    for index, item in enumerate(confirmed):
        if item.get("system_type") == payload.get("system_type"):
            confirmed[index] = payload
            replaced = True
            break
    if not replaced:
        confirmed.append(payload)
    profile.confirmed_systems = confirmed
    _remove_unresolved_items(profile, kind="system_binding", label=payload.get("system_type", ""))
    _refresh_setup_profile_summary(profile)


def _create_proposal(
    session: Session,
    thread: ConversationThread,
    turn: ConversationTurn,
    request: ProposalRequest,
) -> ActionProposal:
    proposal = ActionProposal(
        id=f"proposal-{uuid4().hex[:12]}",
        thread_id=thread.id,
        turn_id=turn.id,
        action_type=request.action_type,
        summary=request.summary,
        payload=request.payload,
        status="pending",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(proposal)
    session.commit()
    session.refresh(thread)
    return proposal


def _apply_proposal(session: Session, proposal: ActionProposal) -> str:
    site = _get_site(session)
    profile = _get_or_create_setup_profile(session)
    now = utcnow()

    if proposal.action_type == "update_site_scope":
        local_subnet = str(proposal.payload.get("local_subnet", "")).strip()
        update_site(session, {"local_subnet": local_subnet})
        proposal.status = "confirmed"
        proposal.resolved_at = now
        session.add(
            AuditEvent(
                actor="agent_user",
                action="confirm_site_scope_update",
                target_type="site",
                target_id=str(site.id),
                summary=f"Confirmed network discovery scope: {local_subnet}.",
                details={"local_subnet": local_subnet},
                created_at=now,
            )
        )
        session.add(proposal)
        session.commit()
        return f"Discovery will now use: {local_subnet}."

    if proposal.action_type == "confirm_system_binding":
        binding = {
            "system_type": str(proposal.payload.get("system_type", "")).strip(),
            "label": str(proposal.payload.get("label", "")).strip(),
            "device_id": proposal.payload.get("device_id"),
            "device_name": proposal.payload.get("device_name"),
            "status": "confirmed",
        }
        _upsert_confirmed_system(profile, binding)
        proposal.status = "confirmed"
        proposal.resolved_at = now
        session.add(profile)
        session.add(proposal)
        session.add(
            AuditEvent(
                actor="agent_user",
                action="confirm_system_binding",
                target_type="setup_profile",
                target_id=str(profile.id),
                summary=f"Confirmed {binding['label']} as {binding['system_type']}.",
                details=binding,
                created_at=now,
            )
        )
        session.commit()
        return f"{binding['label']} is now recorded as the home's {binding['system_type'].replace('_', ' ')}."

    proposal.status = "failed"
    proposal.resolved_at = now
    session.add(proposal)
    session.commit()
    return "The requested proposal type is not implemented."


def _confirm_proposal(session: Session, proposal_id: str) -> ActionProposalDecisionRead:
    proposal = session.get(ActionProposal, proposal_id)
    if proposal is None:
        raise KeyError(proposal_id)
    summary = _apply_proposal(session, proposal)
    thread = session.get(ConversationThread, proposal.thread_id)
    if thread is None:
        raise RuntimeError("Conversation thread is missing.")
    profile = _get_or_create_setup_profile(session)
    proposal = session.get(ActionProposal, proposal_id)
    assert proposal is not None
    proposal.summary = summary if proposal.action_type == "update_site_scope" else proposal.summary
    session.add(proposal)
    session.commit()
    return ActionProposalDecisionRead(
        proposal=_serialize_proposal(proposal),
        thread=_serialize_thread(session, thread, profile),
    )


def confirm_action_proposal(session: Session, proposal_id: str) -> ActionProposalDecisionRead:
    return _confirm_proposal(session, proposal_id)


def reject_action_proposal(session: Session, proposal_id: str) -> ActionProposalDecisionRead:
    proposal = session.get(ActionProposal, proposal_id)
    if proposal is None:
        raise KeyError(proposal_id)
    proposal.status = "rejected"
    proposal.resolved_at = utcnow()
    session.add(proposal)
    session.add(
        AuditEvent(
            actor="agent_user",
            action="reject_action_proposal",
            target_type="action_proposal",
            target_id=proposal.id,
            summary=f"Rejected proposal: {proposal.summary}",
            details={"action_type": proposal.action_type},
            created_at=utcnow(),
        )
    )
    session.commit()
    thread = session.get(ConversationThread, proposal.thread_id)
    if thread is None:
        raise RuntimeError("Conversation thread is missing.")
    return ActionProposalDecisionRead(
        proposal=_serialize_proposal(proposal),
        thread=_serialize_thread(session, thread, _get_or_create_setup_profile(session)),
    )


def get_setup_profile(session: Session) -> SiteSetupProfileRead:
    return _serialize_setup_profile(_get_or_create_setup_profile(session))


def _tool_refresh_discovery(session: Session) -> dict:
    result = run_discovery(session)
    return result.model_dump()


def _tool_open_debug_case(session: Session, request: ToolRequest) -> dict:
    report = create_debug_case(
        session,
        DebugExplainRequest(
            manufacturer="",
            model="",
            device_type=str(request.arguments.get("device_type", "")).strip(),
            notes=str(request.arguments.get("notes", "")).strip(),
        ),
    )
    profile = _get_or_create_setup_profile(session)
    if request.arguments.get("device_type"):
        _ensure_unresolved_item(
            profile,
            kind="system_binding",
            label=str(request.arguments["device_type"]),
            details="Helios needs a clearer device match before this system can be confirmed.",
        )
        session.add(profile)
        session.commit()
    return report.model_dump()


def _tool_run_latest_debug_probe(session: Session) -> dict:
    debug_case = session.scalar(select(DebugCase).order_by(DebugCase.updated_at.desc()).limit(1))
    if debug_case is None:
        return {}
    return run_targeted_probe(session, debug_case.id).model_dump()


def _tool_confirm_latest_proposal(session: Session, thread: ConversationThread) -> dict:
    proposal = session.scalar(
        select(ActionProposal)
        .where(ActionProposal.thread_id == thread.id, ActionProposal.status == "pending")
        .order_by(ActionProposal.created_at.desc())
        .limit(1)
    )
    if proposal is None:
        return {"summary": "There is no pending proposal to confirm."}
    decision = _confirm_proposal(session, proposal.id)
    return {
        "proposal_id": proposal.id,
        "summary": decision.proposal.summary,
    }


def _tool_reject_latest_proposal(session: Session, thread: ConversationThread) -> dict:
    proposal = session.scalar(
        select(ActionProposal)
        .where(ActionProposal.thread_id == thread.id, ActionProposal.status == "pending")
        .order_by(ActionProposal.created_at.desc())
        .limit(1)
    )
    if proposal is None:
        return {"summary": "There is no pending proposal to reject."}
    decision = reject_action_proposal(session, proposal.id)
    return {
        "proposal_id": proposal.id,
        "summary": decision.proposal.summary,
    }


def _execute_tool(session: Session, thread: ConversationThread, request: ToolRequest) -> dict:
    if request.name == "refresh_discovery":
        return _tool_refresh_discovery(session)
    if request.name == "open_debug_case":
        return _tool_open_debug_case(session, request)
    if request.name == "run_latest_debug_probe":
        return _tool_run_latest_debug_probe(session)
    if request.name == "confirm_latest_proposal":
        return _tool_confirm_latest_proposal(session, thread)
    if request.name == "reject_latest_proposal":
        return _tool_reject_latest_proposal(session, thread)
    return {"summary": f"Unknown tool `{request.name}`."}


def _stream_text(text: str) -> list[str]:
    words = text.split(" ")
    if len(words) <= 1:
        return [text]
    chunks: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
            continue
        next_chunk = f"{current} {word}"
        if len(next_chunk) > 48:
            chunks.append(f"{current} ")
            current = word
        else:
            current = next_chunk
    if current:
        chunks.append(current)
    return chunks


def _encode_sse(event: AgentTurnEventRead) -> str:
    payload = event.model_dump(mode="json")
    return f"data: {json.dumps(payload)}\n\n"


def stream_turn_events(turn_id: str) -> Generator[str, None, None]:
    session_factory = get_session_factory()
    settings = get_settings()
    runtime = resolve_provider_status()
    provider: AgentProvider = get_agent_provider(runtime)

    def generator() -> Generator[str, None, None]:
        with session_factory() as session:
            turn = session.scalar(
                select(ConversationTurn)
                .where(ConversationTurn.id == turn_id)
                .options(
                    selectinload(ConversationTurn.events),
                    selectinload(ConversationTurn.thread).selectinload(ConversationThread.proposals),
                )
            )
            if turn is None:
                error_event = AgentTurnEventRead(
                    turn_id=turn_id,
                    event_type="error",
                    payload={"message": "Conversation turn not found."},
                    created_at=utcnow(),
                )
                yield _encode_sse(error_event)
                return

            if turn.status in {"completed", "failed"}:
                for event in turn.events:
                    yield _encode_sse(
                        AgentTurnEventRead(
                            turn_id=turn.id,
                            event_type=event.event_type,
                            payload=event.payload or {},
                            created_at=event.created_at,
                        )
                    )
                yield _encode_sse(
                    AgentTurnEventRead(
                        turn_id=turn.id,
                        event_type="stream_end",
                        payload={},
                        created_at=utcnow(),
                    )
                )
                return

            thread = turn.thread
            if thread is None:
                raise RuntimeError("Conversation thread is missing.")
            profile = _get_or_create_setup_profile(session)
            user_message = session.get(ConversationMessage, turn.user_message_id)
            if user_message is None:
                raise RuntimeError("User message is missing.")

            assistant_message = ConversationMessage(
                id=f"msg-{uuid4().hex[:12]}",
                thread_id=thread.id,
                role="assistant",
                content="",
                status="streaming",
                turn_id=turn.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            assistant_message_id = assistant_message.id
            turn.assistant_message_id = assistant_message.id
            turn.status = "running"
            turn.started_at = utcnow()
            session.add_all([assistant_message, turn])
            session.commit()
            session.refresh(turn)
            session.refresh(thread)

            context = _context_snapshot(session, thread, profile)
            decision = provider.decide_turn(context, user_message.content)
            tool_results: dict[str, dict] = {}
            created_proposals: list[dict] = []

            try:
                for request in decision.tool_calls:
                    started = _persist_event(
                        session,
                        turn,
                        "tool_started",
                        {"tool_name": request.name, "arguments": request.arguments},
                    )
                    yield _encode_sse(started)

                    result = _execute_tool(session, thread, request)
                    tool_results[request.name] = result
                    session.refresh(thread)
                    session.refresh(profile)
                    finished = _persist_event(
                        session,
                        turn,
                        "tool_finished",
                        {"tool_name": request.name, "result": result},
                    )
                    yield _encode_sse(finished)

                for proposal_request in decision.proposal_requests:
                    proposal = _create_proposal(session, thread, turn, proposal_request)
                    if proposal.action_type == "confirm_system_binding":
                        profile = _get_or_create_setup_profile(session)
                        _ensure_unresolved_item(
                            profile,
                            kind="system_binding",
                            label=str(proposal.payload.get("system_type", "")),
                            details="Helios is waiting for you to confirm the detected device binding.",
                        )
                        session.add(profile)
                        session.commit()
                    proposal_payload = _serialize_proposal(proposal).model_dump(mode="json")
                    created_proposals.append(proposal_payload)
                    event = _persist_event(session, turn, "proposal_created", proposal_payload)
                    yield _encode_sse(event)

                session.refresh(thread)
                profile = _get_or_create_setup_profile(session)
                final_context = _context_snapshot(session, thread, profile)
                final_output = provider.compose_turn(final_context, user_message.content, tool_results, created_proposals)
                if final_output.ui_actions:
                    ui_actions_payload = {
                        "actions": [
                            {
                                "type": action.type,
                                "payload": action.payload,
                            }
                            for action in final_output.ui_actions
                        ]
                    }
                    ui_event = _persist_event(session, turn, "ui_actions", ui_actions_payload)
                    yield _encode_sse(ui_event)
                for chunk in _stream_text(final_output.message):
                    delta_event = _persist_event(session, turn, "assistant_delta", {"delta": chunk})
                    yield _encode_sse(delta_event)
                    if settings.agent_stream_delay_ms > 0:
                        time.sleep(settings.agent_stream_delay_ms / 1000.0)

                assistant_message = session.get(ConversationMessage, assistant_message_id)
                turn = session.get(ConversationTurn, turn.id)
                assert assistant_message is not None and turn is not None
                assistant_message.content = final_output.message
                assistant_message.status = "completed"
                assistant_message.updated_at = utcnow()
                turn.status = "completed"
                turn.summary = final_output.message
                turn.finished_at = utcnow()
                session.add_all([assistant_message, turn, thread])
                session.commit()

                completed_event = _persist_event(
                    session,
                    turn,
                    "assistant_message_completed",
                    {
                        "message": _serialize_message(assistant_message).model_dump(mode="json"),
                        "ui_actions": [
                            {
                                "type": action.type,
                                "payload": action.payload,
                            }
                            for action in final_output.ui_actions
                        ],
                    },
                )
                yield _encode_sse(completed_event)
            except Exception as exc:  # pragma: no cover - failure path
                assistant_message = session.get(ConversationMessage, assistant_message_id)
                turn = session.get(ConversationTurn, turn.id)
                if assistant_message is not None:
                    assistant_message.status = "failed"
                    session.add(assistant_message)
                if turn is not None:
                    turn.status = "failed"
                    turn.summary = str(exc)
                    turn.finished_at = utcnow()
                    session.add(turn)
                session.commit()
                error_event = _persist_event(
                    session,
                    turn,
                    "error",
                    {"message": str(exc)},
                )
                yield _encode_sse(error_event)

            yield _encode_sse(
                AgentTurnEventRead(
                    turn_id=turn_id,
                    event_type="stream_end",
                    payload={},
                    created_at=utcnow(),
                )
            )

    return generator()
