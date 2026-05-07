from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from app.agent.tools.schemas import (
    AgentToolContext,
    ConnectionInspectReadinessInput,
    EebusIdentityGetOrCreateInput,
    ProtocolListEndpointsInput,
    ToolExecutionResult,
)
from app.db.models import ProtocolDiagnosticRun, ProtocolEndpoint, utcnow
from app.home_graph.service import connection_facets_for_entity, resolve_entity, sync_inventory_to_home_graph
from app.services.eebus_identity import (
    eebus_identity_public_payload,
    get_or_create_eebus_local_identity,
    read_eebus_local_identity,
)
from app.workflows.role_binding import allowed_integration_paths_for_protocol


def _endpoint_as_tool_payload(endpoint: ProtocolEndpoint) -> dict:
    properties = endpoint.properties or {}
    return {
        "endpoint_ref": endpoint.id,
        "owner_ref": endpoint.owner_ref,
        "protocol": endpoint.protocol,
        "host": endpoint.host,
        "port": endpoint.port,
        "service_name": endpoint.service_name,
        "status": endpoint.status,
        "source": properties.get("source", ""),
        "last_seen_at": properties.get("last_seen_at", ""),
        "confidence": properties.get("confidence", 0.0),
        "allowed_integration_paths": allowed_integration_paths_for_protocol(endpoint.protocol),
        "properties": properties,
    }


class ProtocolListEndpointsTool:
    name = "protocol.list_endpoints"
    purpose = "List observed protocol endpoints and the integration paths compatible with their protocol. This exposes facts, not recommendations."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = ProtocolListEndpointsInput
    mutates_state = True
    reads = ["home_graph", "protocol_endpoints"]
    writes = ["home_graph", "protocol_endpoints"]
    side_effects = ["materializes current inventory into Home Graph before listing endpoints"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: ProtocolListEndpointsInput) -> ToolExecutionResult:
        sync_inventory_to_home_graph(context.session, context.site.id)
        statement = select(ProtocolEndpoint).where(ProtocolEndpoint.site_id == context.site.id)
        if payload.entity_ref:
            entity = resolve_entity(context.session, payload.entity_ref)
            if entity is None:
                raise ValueError(f"Unknown Home Graph entity: {payload.entity_ref}")
            statement = statement.where(ProtocolEndpoint.owner_ref == entity.id)
        if payload.protocol:
            statement = statement.where(ProtocolEndpoint.protocol == payload.protocol)
        endpoints = context.session.scalars(
            statement.order_by(ProtocolEndpoint.owner_ref, ProtocolEndpoint.protocol, ProtocolEndpoint.service_name)
        ).all()
        return ToolExecutionResult(
            output={
                "entity_ref": payload.entity_ref,
                "protocol": payload.protocol,
                "endpoint_count": len(endpoints),
                "endpoints": [_endpoint_as_tool_payload(endpoint) for endpoint in endpoints],
            }
        )


class EebusIdentityGetOrCreateTool:
    name = "eebus.identity.get_or_create"
    purpose = "Create or read the local Helios EEBus identity and public SKI used for SHIP trust setup. It never commissions a peer."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("setup", "commissioning", "debug")
    input_model = EebusIdentityGetOrCreateInput
    mutates_state = True
    reads = ["eebus_local_identity"]
    writes = ["eebus_local_identity"]
    side_effects = ["generates and persists a local private key and self-signed certificate if none exists"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: EebusIdentityGetOrCreateInput) -> ToolExecutionResult:
        identity = get_or_create_eebus_local_identity(
            context.session,
            site_id=context.site.id,
            common_name=payload.common_name or "Helios Home HEMS",
        )
        return ToolExecutionResult(
            output={
                "status": "ready",
                "identity": eebus_identity_public_payload(identity),
                "private_key_exported": False,
                "effects": ["local_identity_available", "no_peer_trust_changed", "no_commissioning_started"],
            }
        )


class ConnectionInspectReadinessTool:
    name = "connection.inspect_readiness"
    purpose = "Inspect factual connection readiness, validation state, blockers, and available transitions for an entity/endpoint/path. It does not connect or commission."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = ConnectionInspectReadinessInput
    mutates_state = True
    reads = ["home_graph", "protocol_endpoints", "eebus_local_identity", "work_store"]
    writes = ["home_graph", "protocol_endpoints", "protocol_diagnostic_run"]
    side_effects = ["materializes current inventory into Home Graph before inspecting readiness"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: ConnectionInspectReadinessInput) -> ToolExecutionResult:
        sync_inventory_to_home_graph(context.session, context.site.id)
        endpoint = context.session.get(ProtocolEndpoint, payload.endpoint_ref) if payload.endpoint_ref else None
        entity_ref = payload.entity_ref or (endpoint.owner_ref if endpoint is not None else "")
        if not entity_ref:
            raise ValueError("entity_ref or endpoint_ref is required.")
        entity = resolve_entity(context.session, entity_ref)
        if entity is None:
            raise ValueError(f"Unknown Home Graph entity: {entity_ref}")
        if endpoint is not None and endpoint.owner_ref != entity.id:
            raise ValueError(f"Endpoint {endpoint.id} does not belong to {entity.id}.")
        endpoints = [endpoint] if endpoint is not None else context.session.scalars(
            select(ProtocolEndpoint)
            .where(ProtocolEndpoint.owner_ref == entity.id)
            .order_by(ProtocolEndpoint.protocol, ProtocolEndpoint.service_name)
        ).all()
        path = payload.integration_path or ""
        endpoint_payloads = [_endpoint_as_tool_payload(row) for row in endpoints]
        inspections = [
            _inspect_endpoint_path(context, row, integration_path=path, role=payload.role)
            for row in endpoints
        ]
        blockers = _flatten_blockers(inspections)
        readiness = "no_endpoint_observed"
        if inspections:
            readiness = "blocked" if blockers else "ready_for_next_workflow"
        log_entries = _diagnostic_log_entries(inspections, blockers)
        diagnostic_run = ProtocolDiagnosticRun(
            id=f"protocol-diagnostic-{uuid4().hex[:12]}",
            site_id=context.site.id,
            thread_id=context.thread.id,
            turn_id=context.turn.id,
            entity_ref=entity.id,
            endpoint_ref=payload.endpoint_ref,
            protocol=endpoint.protocol if endpoint is not None else "",
            integration_path=path,
            status=readiness,
            log_entries=log_entries,
            result={
                "readiness": readiness,
                "blocker_codes": [blocker.get("code") for blocker in blockers],
            },
            created_at=utcnow(),
        )
        context.session.add(diagnostic_run)
        context.session.commit()
        connection_facets = connection_facets_for_entity(context.session, entity_ref=entity.id, endpoints=endpoints)
        return ToolExecutionResult(
            output={
                "diagnostic_run_ref": diagnostic_run.id,
                "entity": {
                    "ref": entity.id,
                    "display_name": entity.display_name,
                    "semantic_type": entity.semantic_type,
                    "status": entity.status,
                },
                "role": payload.role,
                "endpoint_ref": payload.endpoint_ref,
                "integration_path": path,
                "readiness": readiness,
                "connection_facets": connection_facets,
                "endpoints": endpoint_payloads,
                "inspections": inspections,
                "blockers": blockers,
                "log_entries": log_entries,
                "available_transitions": _available_transitions(inspections),
            }
        )


def _inspect_endpoint_path(
    context: AgentToolContext,
    endpoint: ProtocolEndpoint,
    *,
    integration_path: str,
    role: str | None,
) -> dict:
    allowed_paths = allowed_integration_paths_for_protocol(endpoint.protocol)
    inspected_paths = [integration_path] if integration_path else allowed_paths
    if integration_path and integration_path not in allowed_paths:
        return {
            "endpoint_ref": endpoint.id,
            "protocol": endpoint.protocol,
            "integration_path": integration_path,
            "facts": {"endpoint_observed": True, "allowed_integration_paths": allowed_paths},
            "validation_results": [],
            "blockers": [
                {
                    "code": "integration_path_not_compatible",
                    "severity": "error",
                    "detail": f"{integration_path} is not compatible with endpoint protocol {endpoint.protocol}.",
                }
            ],
        }

    inspections = [
        _inspect_single_path(context, endpoint, integration_path=path, role=role)
        for path in inspected_paths
    ]
    if len(inspections) == 1:
        return inspections[0]
    return {
        "endpoint_ref": endpoint.id,
        "protocol": endpoint.protocol,
        "integration_path": "",
        "facts": {"endpoint_observed": True, "allowed_integration_paths": allowed_paths},
        "validation_results": [],
        "blockers": [],
        "path_inspections": inspections,
    }


def _inspect_single_path(
    context: AgentToolContext,
    endpoint: ProtocolEndpoint,
    *,
    integration_path: str,
    role: str | None,
) -> dict:
    properties = endpoint.properties or {}
    facts: dict[str, object] = {
        "endpoint_observed": True,
        "protocol": endpoint.protocol,
        "host": endpoint.host,
        "port": endpoint.port,
        "service_name": endpoint.service_name,
        "role": role,
        "allowed_integration_paths": allowed_integration_paths_for_protocol(endpoint.protocol),
    }
    blockers: list[dict] = []
    validation_results: list[dict] = []

    if integration_path == "eebus_spine":
        identity = read_eebus_local_identity(context.session, site_id=context.site.id)
        register_value = properties.get("register")
        facts.update(
            {
                "ship_path": properties.get("path", ""),
                "remote_ship_id": properties.get("ship_id", ""),
                "remote_ski": properties.get("ski", ""),
                "remote_register": register_value,
                "supported_use_cases_observed": properties.get("supported_use_cases", []),
                "local_identity_exists": identity is not None,
                "local_ski": identity.ski if identity is not None else "",
            }
        )
        if identity is None:
            blockers.append(
                {
                    "code": "local_eebus_identity_missing",
                    "severity": "blocking",
                    "detail": "No local Helios EEBus identity/SKI exists yet.",
                }
            )
        if register_value is False:
            blockers.append(
                {
                    "code": "remote_auto_registration_closed",
                    "severity": "blocking",
                    "detail": "The peer advertises register=false; manual trust configuration is likely required.",
                }
            )
        blockers.append(
            {
                "code": "ship_trust_commissioning_not_validated",
                "severity": "blocking",
                "detail": "No successful SHIP trust commissioning has been recorded.",
            }
        )
        validation_results.extend(
            [
                {"check": "ship_endpoint_observed", "status": "passed"},
                {"check": "spine_feature_discovery", "status": "not_run"},
                {"check": "lpc_lpp_capability_validation", "status": "not_run"},
            ]
        )
    else:
        validation_results.append({"check": "endpoint_observed", "status": "passed"})
        validation_results.append({"check": "telemetry_validation", "status": "not_run"})

    return {
        "endpoint_ref": endpoint.id,
        "protocol": endpoint.protocol,
        "integration_path": integration_path,
        "facts": facts,
        "validation_results": validation_results,
        "blockers": blockers,
    }


def _available_transitions(inspections: list[dict]) -> list[dict]:
    transitions: list[dict] = []
    flattened = []
    for inspection in inspections:
        flattened.extend(inspection.get("path_inspections") or [inspection])
    if any(
        blocker.get("code") == "local_eebus_identity_missing"
        for inspection in flattened
        for blocker in inspection.get("blockers", [])
    ):
        transitions.append(
            {
                "transition": "create_local_eebus_identity",
                "tool": "eebus.identity.get_or_create",
                "allowed": True,
                "risk": "low",
                "requires_user_decision": False,
            }
        )
    if flattened:
        transitions.append(
            {
                "transition": "prepare_binding_proposal",
                "tool": "role.prepare_binding_proposal",
                "allowed": True,
                "risk": "medium",
                "requires_user_decision": True,
            }
        )
    return transitions


def _flatten_blockers(inspections: list[dict]) -> list[dict]:
    blockers: list[dict] = []
    for inspection in inspections:
        blockers.extend(inspection.get("blockers", []))
        for path_inspection in inspection.get("path_inspections", []):
            blockers.extend(path_inspection.get("blockers", []))
    return blockers


def _diagnostic_log_entries(inspections: list[dict], blockers: list[dict]) -> list[dict]:
    entries: list[dict] = []
    for inspection in inspections:
        path_inspections = inspection.get("path_inspections") or [inspection]
        for path_inspection in path_inspections:
            facts = path_inspection.get("facts", {})
            entries.append(
                {
                    "level": "info",
                    "event": "endpoint_observed",
                    "endpoint_ref": path_inspection.get("endpoint_ref", ""),
                    "protocol": path_inspection.get("protocol", ""),
                    "integration_path": path_inspection.get("integration_path", ""),
                    "host": facts.get("host", ""),
                    "port": facts.get("port"),
                }
            )
            if path_inspection.get("integration_path") == "eebus_spine":
                entries.append(
                    {
                        "level": "info",
                        "event": "eebus_ship_metadata",
                        "remote_ski": facts.get("remote_ski", ""),
                        "remote_register": facts.get("remote_register"),
                        "local_ski": facts.get("local_ski", ""),
                    }
                )
    for blocker in blockers:
        entries.append(
            {
                "level": "warning" if blocker.get("severity") != "error" else "error",
                "event": "readiness_blocker",
                "code": blocker.get("code", ""),
                "detail": blocker.get("detail", ""),
            }
        )
    return entries
