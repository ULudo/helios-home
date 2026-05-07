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
from app.agent.provider import get_model_provider
from app.agent.runtime import AgentRuntime
from app.agent.schemas import (
    ActionProposalDecisionRead,
    ActionProposalRead,
    AgentBlockerRead,
    AgentMessageCreate,
    AgentProviderConfigRead,
    AgentProviderConfigUpdate,
    AgentProviderOptionRead,
    AgentMessageRead,
    AgentTaskRead,
    AgentThreadRead,
    AgentTurnAcceptedRead,
    AgentTurnEventRead,
    SetupItemRead,
    SetupSystemBindingRead,
    SiteSetupProfileRead,
    UserDecisionCreate,
)
from app.agent.tools.registry import create_default_tool_registry
from app.core.config import get_settings
from app.db.models import (
    AgentTask,
    Asset,
    Blocker,
    ConversationEvent,
    ConversationMessage,
    ConversationThread,
    ConversationTurn,
    DebugCase,
    Device,
    Proposal,
    Site,
    SiteSetupProfile,
    UserDecisionRequest,
    utcnow,
)
from app.db.session import get_session_factory
from app.domain.schemas import DebugCaseRead
from app.hems.bindings import list_system_bindings
from app.home_graph.service import canonical_inventory_summary, sync_inventory_to_home_graph
from app.services.dashboard import build_overview
from app.services.knowledge import get_debug_case
from app.services.network_scope import list_reachable_subnets
from app.work_store.service import (
    accepted_role_candidates,
    list_pending_proposals,
    record_user_decision,
)


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
            summary="setup_profile_initialized",
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
        profile.summary = "setup_profile_empty"
        return
    profile.summary = "setup_profile_has_progress"


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


def _serialize_proposal(proposal: Proposal, decision_request: UserDecisionRequest | None = None) -> ActionProposalRead:
    return ActionProposalRead(
        id=proposal.id,
        action_type=proposal.proposal_type,
        summary=proposal.summary,
        payload=proposal.payload or {},
        status=proposal.status,
        title=proposal.title,
        risk_level=proposal.risk_level,
        target_refs=proposal.target_refs or [],
        decision_request_id=decision_request.id if decision_request is not None else None,
        decision_question=decision_request.question or None if decision_request is not None else None,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
        resolved_at=proposal.resolved_at,
    )


def _latest_debug_case(session: Session) -> DebugCaseRead | None:
    debug_case = session.scalar(select(DebugCase).order_by(DebugCase.updated_at.desc()).limit(1))
    if debug_case is None:
        return None
    return get_debug_case(session, debug_case.id)


def _serialize_blocker(blocker: Blocker) -> AgentBlockerRead:
    return AgentBlockerRead(
        id=blocker.id,
        task_id=blocker.task_id,
        subject_ref=blocker.subject_ref,
        blocker_type=blocker.blocker_type,
        summary=blocker.summary or blocker.blocker_type,
        status=blocker.status,
        details=blocker.details or {},
        created_at=blocker.created_at,
        resolved_at=blocker.resolved_at,
    )


def _serialize_active_tasks(session: Session, site_id: int) -> list[AgentTaskRead]:
    tasks = session.scalars(
        select(AgentTask)
        .where(
            AgentTask.site_id == site_id,
            AgentTask.status.in_(["open", "running", "blocked"]),
        )
        .order_by(AgentTask.updated_at.desc())
        .limit(12)
    ).all()
    if not tasks:
        return []
    blockers = session.scalars(
        select(Blocker)
        .where(
            Blocker.status == "open",
        )
        .order_by(Blocker.created_at.desc())
        .limit(40)
    ).all()
    blockers_by_task: dict[str, list[Blocker]] = {}
    for blocker in blockers:
        if blocker.task_id:
            blockers_by_task.setdefault(blocker.task_id, []).append(blocker)
    return [
        AgentTaskRead(
            id=task.id,
            task_type=task.task_type,
            title=task.title,
            goal=task.goal,
            status=task.status,
            target_refs=task.target_refs or [],
            context=task.context or {},
            blockers=[_serialize_blocker(blocker) for blocker in blockers_by_task.get(task.id, [])],
            created_at=task.created_at,
            updated_at=task.updated_at,
            completed_at=task.completed_at,
        )
        for task in tasks
    ]


def _serialize_open_blockers(session: Session, site_id: int) -> list[AgentBlockerRead]:
    blockers = session.scalars(
        select(Blocker)
        .join(AgentTask, AgentTask.id == Blocker.task_id, isouter=True)
        .where(
            Blocker.status == "open",
            ((Blocker.task_id.is_(None)) | (AgentTask.site_id == site_id)),
        )
        .order_by(Blocker.created_at.desc())
        .limit(20)
    ).all()
    return [_serialize_blocker(blocker) for blocker in blockers]


def _serialize_thread(session: Session, thread: ConversationThread, profile: SiteSetupProfile) -> AgentThreadRead:
    session.refresh(thread)
    return AgentThreadRead(
        id=thread.id,
        title=thread.title,
        status=thread.status,
        messages=[_serialize_message(message) for message in thread.messages],
        pending_proposals=[
            _serialize_proposal(proposal, decision_request)
            for proposal, decision_request in list_pending_proposals(session, thread.id)
        ],
        active_tasks=_serialize_active_tasks(session, thread.site_id),
        open_blockers=_serialize_open_blockers(session, thread.site_id),
        setup_profile=_serialize_setup_profile(profile),
        latest_debug_case=_latest_debug_case(session),
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def get_agent_thread(session: Session) -> AgentThreadRead:
    profile = _get_or_create_setup_profile(session)
    thread = _get_or_create_primary_thread(session)
    session.refresh(thread)
    return _serialize_thread(session, thread, profile)


def _context_snapshot(
    session: Session,
    thread: ConversationThread,
    profile: SiteSetupProfile,
    *,
    input_context: dict | None = None,
    available_tools: list[dict] | None = None,
) -> dict:
    overview = build_overview(session)
    site = _get_site(session)
    sync_inventory_to_home_graph(session, site.id)
    assets = session.scalars(select(Asset).order_by(Asset.updated_at.desc())).all()
    asset_id_by_device_id: dict[str, str] = {}
    for asset in assets:
        for device_id in asset.device_ids or []:
            asset_id_by_device_id.setdefault(device_id, asset.id)
    bindings = list_system_bindings(session, confirmed_only=True)
    inventory_summary = canonical_inventory_summary(session, site.id)
    role_candidates = accepted_role_candidates(session, site_id=site.id)
    active_tasks = session.scalars(
        select(AgentTask)
        .where(
            AgentTask.site_id == site.id,
            AgentTask.status.in_(["open", "running", "blocked"]),
        )
        .order_by(AgentTask.updated_at.desc())
        .limit(10)
    ).all()
    open_blockers = session.scalars(
        select(Blocker)
        .where(Blocker.status == "open")
        .order_by(Blocker.created_at.desc())
        .limit(10)
    ).all()
    recent_candidate_sets = _recent_candidate_sets(session, thread.id)
    return {
        "site_id": site.id,
        "input_context": input_context or {},
        "current_subnets": [entry.strip() for entry in site.local_subnet.split(",") if entry.strip()],
        "reachable_subnets": [
            {"cidr": option.cidr, "interface": option.interface, "label": option.label}
            for option in list_reachable_subnets()
        ],
        "devices": [
            {
                "ref": f"device:{device.id}",
                "id": device.id,
                "asset_id": asset_id_by_device_id.get(device.id),
                "name": device.name,
                "device_type": device.device_type,
                "manufacturer": device.manufacturer,
                "model": device.model,
                "status": device.primary_status,
                "protocols": list(device.protocols),
                "capabilities": device.capabilities.model_dump(),
                "telemetry_keys": sorted(device.telemetry.keys()),
            }
            for device in overview.devices
        ],
        "setup_profile": _serialize_setup_profile(profile).model_dump(),
        "hems_bindings": [
            {
                "id": binding.id,
                "system_type": binding.system_type,
                "label": binding.label,
                "device_id": binding.device_id,
                "asset_id": binding.asset_id,
                "status": binding.status,
                "connection_status": binding.connection_status,
                "telemetry_status": binding.telemetry_status,
                "control_status": binding.control_status,
            }
            for binding in bindings
        ],
        "recent_messages": [
            {
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
            }
            for message in thread.messages[-6:]
        ],
        "home_inventory": inventory_summary,
        "home_graph_summary": {
            "canonical_device_count": inventory_summary["canonical_device_count"],
            "canonical_device_counts_by_type": inventory_summary["canonical_device_counts_by_type"],
            "raw_artifact_counts": inventory_summary["raw_artifact_counts"],
            "details_available_via": ["home_graph.query", "home_graph.get_entity_details"],
            "normal_query_scope": "canonical_devices",
        },
        "recent_candidate_sets": recent_candidate_sets,
        "accepted_role_candidates": role_candidates,
        "active_tasks": [
            {
                "task_ref": task.id,
                "task_type": task.task_type,
                "title": task.title,
                "goal": task.goal,
                "status": task.status,
                "target_refs": task.target_refs or [],
                "context": task.context or {},
                "updated_at": task.updated_at,
            }
            for task in active_tasks
        ],
        "open_blockers": [
            {
                "blocker_ref": blocker.id,
                "task_ref": blocker.task_id,
                "subject_ref": blocker.subject_ref,
                "blocker_type": blocker.blocker_type,
                "summary": blocker.summary,
                "details": blocker.details or {},
                "created_at": blocker.created_at,
            }
            for blocker in open_blockers
        ],
        "pending_proposals": [
            _serialize_proposal(proposal, decision_request).model_dump(mode="json")
            for proposal, decision_request in list_pending_proposals(session, thread.id)
        ],
    }


def _recent_candidate_sets(session: Session, thread_id: str) -> list[dict]:
    events = session.scalars(
        select(ConversationEvent)
        .join(ConversationTurn, ConversationTurn.id == ConversationEvent.turn_id)
        .where(
            ConversationTurn.thread_id == thread_id,
            ConversationEvent.event_type == "tool_finished",
        )
        .order_by(ConversationEvent.created_at.desc())
        .limit(20)
    ).all()
    sets: list[dict] = []
    for event in events:
        payload = event.payload or {}
        if payload.get("tool_name") != "home_graph.query":
            continue
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        matches = result.get("matching_entities") if isinstance(result.get("matching_entities"), list) else result.get("entities")
        matches = matches if isinstance(matches, list) else []
        if not matches:
            continue
        sets.append(
            {
                "turn_id": event.turn_id,
                "role": result.get("role_hypothesis"),
                "role_label": result.get("role_label") or result.get("role_hypothesis"),
                "candidate_refs": [entry.get("ref") for entry in matches if isinstance(entry, dict) and entry.get("ref")],
                "candidate_labels": [entry.get("display_name") for entry in matches if isinstance(entry, dict) and entry.get("display_name")],
                "created_at": event.created_at,
            }
        )
        if len(sets) >= 3:
            break
    return sets


def create_agent_message(session: Session, payload: AgentMessageCreate) -> AgentTurnAcceptedRead:
    profile = _get_or_create_setup_profile(session)
    thread = _get_or_create_primary_thread(session)
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
    if payload.context:
        session.add(
            ConversationEvent(
                turn_id=turn.id,
                event_index=0,
                event_type="user_context",
                payload=jsonable_encoder(payload.context),
                created_at=now,
            )
        )
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


def _decision_response_for_proposal(
    session: Session,
    proposal: Proposal,
    decision_request: UserDecisionRequest | None,
) -> ActionProposalDecisionRead:
    if decision_request is None:
        decision_request = session.scalar(
            select(UserDecisionRequest)
            .where(UserDecisionRequest.proposal_id == proposal.id)
            .order_by(UserDecisionRequest.created_at.desc())
            .limit(1)
        )
    thread = session.get(ConversationThread, proposal.thread_id) if proposal.thread_id else None
    if thread is None:
        raise RuntimeError("Conversation thread is missing.")
    return ActionProposalDecisionRead(
        proposal=_serialize_proposal(proposal, decision_request),
        thread=_serialize_thread(session, thread, _get_or_create_setup_profile(session)),
    )


def respond_to_user_decision_request(
    session: Session,
    decision_request_id: str,
    payload: UserDecisionCreate,
) -> ActionProposalDecisionRead:
    proposal, decision_request, _decision = record_user_decision(
        session,
        request_id=decision_request_id,
        decision=payload.decision,
        comment=payload.comment,
    )
    return _decision_response_for_proposal(session, proposal, decision_request)


def get_setup_profile(session: Session) -> SiteSetupProfileRead:
    return _serialize_setup_profile(_get_or_create_setup_profile(session))


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


def _turn_input_context(turn: ConversationTurn) -> dict:
    for event in turn.events:
        if event.event_type == "user_context":
            return event.payload or {}
    return {}


def stream_turn_events(turn_id: str) -> Generator[str, None, None]:
    session_factory = get_session_factory()
    settings = get_settings()
    runtime = resolve_provider_status()
    registry = create_default_tool_registry()

    def generator() -> Generator[str, None, None]:
        with session_factory() as session:
            turn = session.scalar(
                select(ConversationTurn)
                .where(ConversationTurn.id == turn_id)
                .options(
                    selectinload(ConversationTurn.events),
                    selectinload(ConversationTurn.thread),
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

            input_context = _turn_input_context(turn)
            mode = str(input_context.get("agent_mode") or "setup")
            runtime_events_yielded = False

            try:
                try:
                    provider = get_model_provider(runtime)
                except Exception as exc:
                    _persist_event(
                        session,
                        turn,
                        "provider_error",
                        {
                            "provider": runtime.effective_provider,
                            "message": str(exc),
                        },
                    )
                    raise

                def build_runtime_context(
                    context_session: Session,
                    context_thread: ConversationThread,
                    context_input: dict,
                    available_tools: list[dict],
                ) -> dict:
                    return _context_snapshot(
                        context_session,
                        context_thread,
                        _get_or_create_setup_profile(context_session),
                        input_context=context_input,
                        available_tools=available_tools,
                    )

                agent_runtime = AgentRuntime(
                    session=session,
                    site=_get_site(session),
                    thread=thread,
                    turn=turn,
                    user_message=user_message,
                    provider=provider,
                    registry=registry,
                    mode=mode,
                    input_context=input_context,
                    max_tool_iterations=6,
                    build_context=build_runtime_context,
                    write_event=_persist_event,
                    serialize_proposal=_serialize_proposal,
                )
                runtime_result = agent_runtime.run()
                for event in runtime_result.events:
                    yield _encode_sse(event)
                runtime_events_yielded = True

                for chunk in _stream_text(runtime_result.final_answer):
                    delta_event = _persist_event(session, turn, "assistant_delta", {"delta": chunk})
                    yield _encode_sse(delta_event)
                    if settings.agent_stream_delay_ms > 0:
                        time.sleep(settings.agent_stream_delay_ms / 1000.0)

                assistant_message = session.get(ConversationMessage, assistant_message_id)
                turn = session.get(ConversationTurn, turn.id)
                assert assistant_message is not None and turn is not None
                assistant_message.content = runtime_result.final_answer
                assistant_message.status = "completed"
                assistant_message.updated_at = utcnow()
                turn.status = "completed"
                turn.summary = runtime_result.final_answer
                turn.finished_at = utcnow()
                session.add_all([assistant_message, turn, thread])
                session.commit()

                completed_event = _persist_event(
                    session,
                    turn,
                    "assistant_message_completed",
                    {
                        "message": _serialize_message(assistant_message).model_dump(mode="json"),
                        "ui_events": [],
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
                if not runtime_events_yielded and turn is not None:
                    session.refresh(turn)
                    for stored_event in turn.events:
                        yield _encode_sse(
                            AgentTurnEventRead(
                                turn_id=turn.id,
                                event_type=stored_event.event_type,
                                payload=stored_event.payload or {},
                                created_at=stored_event.created_at,
                            )
                        )
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
