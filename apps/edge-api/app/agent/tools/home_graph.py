from __future__ import annotations

from app.agent.tools.schemas import (
    AgentToolContext,
    HomeGraphGetEntityDetailsInput,
    HomeGraphQueryInput,
    ToolExecutionResult,
)
from app.home_graph.service import get_entity_details, query_entities, sync_inventory_to_home_graph


class HomeGraphQueryTool:
    name = "home_graph.query"
    purpose = "Inspect already-known Home Graph entities, evidence, relationships, and explicit role hypotheses. This does not scan the network or discover new devices."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = HomeGraphQueryInput
    mutates_state = True
    reads = ["home_graph", "device_inventory"]
    writes = ["home_graph"]
    side_effects = ["materializes current inventory into Home Graph before querying"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: HomeGraphQueryInput) -> ToolExecutionResult:
        sync_inventory_to_home_graph(context.session, context.site.id)
        entity_types = payload.entity_types
        if payload.role_hypothesis and not entity_types:
            entity_types = ["device"] if payload.scope == "canonical_devices" else ["device", "candidate", "role_candidate"]
        result = query_entities(
            context.session,
            text=payload.text,
            entity_refs=payload.entity_refs,
            entity_types=entity_types,
            scope=payload.scope,
            include_evidence=payload.include_evidence,
            include_relationships=payload.include_relationships,
            text_match_mode="rank" if payload.role_hypothesis else "filter",
        )
        if payload.role_hypothesis:
            role_types = _semantic_types_for_role(payload.role_hypothesis)
            result["entities"] = [
                entity
                for entity in result.get("entities", [])
                if entity.get("semantic_type") in role_types
                or (
                    entity.get("entity_type") == "role_candidate"
                    and (entity.get("properties") or {}).get("role") == payload.role_hypothesis
                )
            ]
            result["role_hypothesis"] = payload.role_hypothesis
            result["matching_entities"] = result["entities"]
        return ToolExecutionResult(output=result)


class HomeGraphGetEntityDetailsTool:
    name = "home_graph.get_entity_details"
    purpose = "Inspect one Home Graph entity with its factual properties, observed protocol endpoints, evidence, and relationships."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = HomeGraphGetEntityDetailsInput
    mutates_state = True
    reads = ["home_graph", "protocol_endpoints", "home_graph_evidence"]
    writes = ["home_graph", "protocol_endpoints"]
    side_effects = ["materializes current inventory into Home Graph before reading details"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: HomeGraphGetEntityDetailsInput) -> ToolExecutionResult:
        sync_inventory_to_home_graph(context.session, context.site.id)
        return ToolExecutionResult(
            output=get_entity_details(
                context.session,
                entity_ref=payload.entity_ref,
                include_evidence=payload.include_evidence,
            )
        )


def _semantic_types_for_role(role: str) -> set[str]:
    role_map = {
        "grid_meter": {"grid_meter", "smart_meter_gateway"},
        "ev_charger": {"ev_charger", "wallbox"},
        "pv_inverter": {"pv_inverter"},
        "battery": {"battery"},
        "heat_pump": {"heat_pump"},
        "controllable_load": {"controllable_load"},
    }
    return role_map.get(role, {role})
