from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app.agent.tools.schemas import (
    AgentToolContext,
    HomeGraphResolveEntityReferenceInput,
    ToolExecutionResult,
    focus_entities_event,
    view_open_event,
)
from app.db.models import ConversationEvent, ConversationTurn, HomeGraphEntity
from app.home_graph.service import sync_inventory_to_home_graph


class HomeGraphResolveEntityReferenceTool:
    name = "home_graph.resolve_entity_reference"
    purpose = "Resolve a follow-up user reference such as 'the Mennekes one' to a specific Home Graph entity."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = HomeGraphResolveEntityReferenceInput
    mutates_state = True
    reads = ["home_graph", "conversation_events"]
    writes = ["home_graph"]
    side_effects = ["materializes current inventory into Home Graph before resolving"]
    emitted_ui_events = ["view.open", "entity.focus"]

    def execute(
        self,
        context: AgentToolContext,
        payload: HomeGraphResolveEntityReferenceInput,
    ) -> ToolExecutionResult:
        sync_inventory_to_home_graph(context.session, context.site.id)
        candidates = _candidate_entities(context, payload)
        scored = sorted(
            (
                (_score_entity(payload.text, entity, payload.role), entity)
                for entity in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        matches = [(score, entity) for score, entity in scored if score > 0]
        top_score = matches[0][0] if matches else 0.0
        close_matches = [(score, entity) for score, entity in matches if top_score - score <= 0.18][: payload.max_results]
        resolved = close_matches[0][1] if top_score >= 0.35 and len(close_matches) == 1 else None

        ui_events = []
        if resolved is not None:
            ui_events = [
                view_open_event("overview", "focus"),
                focus_entities_event([resolved.id]),
                focus_entities_event([resolved.id], mode="highlight"),
            ]
        elif close_matches:
            refs = [entity.id for _, entity in close_matches if entity.entity_type == "device"]
            if refs:
                ui_events = [
                    view_open_event("overview", "focus"),
                    focus_entities_event(refs, mode="highlight"),
                ]

        return ToolExecutionResult(
            output={
                "found": resolved is not None,
                "ambiguous": resolved is None and bool(close_matches),
                "query": payload.text,
                "role": payload.role,
                "resolved_entity": _entity_payload(resolved) if resolved is not None else None,
                "matches": [_entity_payload(entity, score=score) for score, entity in close_matches],
            },
            ui_events=ui_events,
        )


def _candidate_entities(
    context: AgentToolContext,
    payload: HomeGraphResolveEntityReferenceInput,
) -> list[HomeGraphEntity]:
    refs = list(payload.candidate_refs)
    if not refs:
        refs = _recent_candidate_refs(context, payload.role)
    if refs:
        entities = [context.session.get(HomeGraphEntity, ref) for ref in refs]
        deduped = _dedupe_entities([entity for entity in entities if entity is not None])
        if deduped:
            return deduped

    statement = (
        select(HomeGraphEntity)
        .where(
            HomeGraphEntity.site_id == context.site.id,
            HomeGraphEntity.entity_type.in_(["device", "candidate"]),
        )
        .order_by(HomeGraphEntity.updated_at.desc())
        .limit(80)
    )
    rows = context.session.scalars(statement).all()
    return _dedupe_entities([row for row in rows if payload.role is None or _semantic_type_matches_role(row.semantic_type, payload.role)])


def _recent_candidate_refs(context: AgentToolContext, role: str | None) -> list[str]:
    events = context.session.scalars(
        select(ConversationEvent)
        .join(ConversationTurn, ConversationTurn.id == ConversationEvent.turn_id)
        .where(
            ConversationTurn.thread_id == context.thread.id,
            ConversationEvent.event_type == "tool_finished",
        )
        .order_by(ConversationEvent.created_at.desc())
        .limit(20)
    ).all()
    refs: list[str] = []
    for event in events:
        payload = event.payload or {}
        if payload.get("tool_name") != "home_graph.query":
            continue
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if role and result.get("role_hypothesis") != role:
            continue
        for entity in result.get("matching_entities") or result.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            ref = str(entity.get("ref") or "").strip()
            if ref and ref not in refs:
                refs.append(ref)
        if refs:
            return refs
    return refs


def _dedupe_entities(entities: list[HomeGraphEntity]) -> list[HomeGraphEntity]:
    deduped: dict[str, HomeGraphEntity] = {}
    for entity in entities:
        key = _physical_key(entity)
        previous = deduped.get(key)
        if previous is None or _entity_rank(entity) > _entity_rank(previous):
            deduped[key] = entity
    return list(deduped.values())


def _entity_rank(entity: HomeGraphEntity) -> int:
    if entity.entity_type == "device":
        return 2
    return 1


def _physical_key(entity: HomeGraphEntity) -> str:
    properties = entity.properties or {}
    if entity.entity_type == "device":
        return f"device:{entity.source_id}"
    matched_device_id = str(properties.get("matched_device_id") or "").strip()
    if matched_device_id:
        return f"device:{matched_device_id}"
    if entity.source_id.startswith("cand-"):
        return f"device:{entity.source_id.replace('cand-', 'dev-', 1)}"
    manufacturer = str(properties.get("manufacturer") or "")
    model = str(properties.get("model") or "")
    protocols = ",".join(str(protocol) for protocol in properties.get("protocols", []))
    return f"{entity.display_name}|{manufacturer}|{model}|{protocols}"


def _score_entity(text: str, entity: HomeGraphEntity, role: str | None) -> float:
    normalized = _normalize(text)
    properties = entity.properties or {}
    haystacks = [
        entity.id,
        entity.display_name,
        str(properties.get("manufacturer") or ""),
        str(properties.get("model") or ""),
        " ".join(str(protocol) for protocol in properties.get("protocols", [])),
    ]
    score = 0.0
    if role and _semantic_type_matches_role(entity.semantic_type, role):
        score += 0.05
    for value in haystacks:
        score += _token_score(normalized, value)
    return min(score, 1.0)


def _token_score(text: str, value: str) -> float:
    score = 0.0
    normalized_value = _normalize(value)
    if not normalized_value:
        return score
    if normalized_value in text:
        score += 0.55
    for token in re.split(r"[^a-z0-9äöüß]+", normalized_value):
        if len(token) < 3:
            continue
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text):
            score += 0.32
    return min(score, 0.8)


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _semantic_type_matches_role(semantic_type: str, role: str) -> bool:
    role_map = {
        "grid_meter": {"grid_meter", "smart_meter_gateway"},
        "ev_charger": {"ev_charger", "wallbox"},
        "pv_inverter": {"pv_inverter"},
        "battery": {"battery"},
        "heat_pump": {"heat_pump"},
        "controllable_load": {"controllable_load"},
    }
    return semantic_type in role_map.get(role, {role})


def _entity_payload(entity: HomeGraphEntity | None, *, score: float | None = None) -> dict[str, Any] | None:
    if entity is None:
        return None
    properties = entity.properties or {}
    payload: dict[str, Any] = {
        "ref": entity.id,
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
        "semantic_type": entity.semantic_type,
        "status": entity.status,
        "manufacturer": str(properties.get("manufacturer") or ""),
        "model": str(properties.get("model") or ""),
        "protocols": list(properties.get("protocols") or []),
    }
    if score is not None:
        payload["score"] = score
    return payload
