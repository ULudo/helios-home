from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from app.agent.tools.protocol import _endpoint_as_tool_payload
from app.agent.tools.schemas import (
    AgentToolContext,
    CommissioningGetLogInput,
    CommissioningStartOrContinueInput,
    ToolExecutionResult,
)
from app.db.models import AgentTask, Blocker, ProtocolDiagnosticRun, ProtocolEndpoint, utcnow
from app.home_graph.service import connection_facets_for_entity, resolve_entity, sync_inventory_to_home_graph
from app.services.eebus_identity import eebus_identity_public_payload, get_or_create_eebus_local_identity
from app.work_store.service import add_blocker, add_task_step, create_task
from app.workflows.role_binding import allowed_integration_paths_for_protocol


class CommissioningStartOrContinueTool:
    name = "commissioning.start_or_continue"
    purpose = (
        "Start or continue an assisted commissioning workflow for an already discovered entity. "
        "This records preparation, diagnostics, logs, and blockers; it does not approve decisions, "
        "does not change the peer device, and does not claim telemetry/control validation."
    )
    risk_level = "medium"
    confirmation_policy = "none"
    contexts = ("setup", "commissioning", "debug")
    input_model = CommissioningStartOrContinueInput
    mutates_state = True
    reads = ["home_graph", "protocol_endpoints", "eebus_local_identity", "work_store"]
    writes = ["eebus_local_identity", "work_store", "protocol_diagnostic_run"]
    side_effects = ["may create a local EEBus identity and records commissioning task progress"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: CommissioningStartOrContinueInput) -> ToolExecutionResult:
        sync_inventory_to_home_graph(context.session, context.site.id)
        entity = resolve_entity(context.session, payload.entity_ref)
        if entity is None:
            raise ValueError(f"Unknown Home Graph entity: {payload.entity_ref}")
        endpoint = _select_endpoint(context, entity.id, payload.endpoint_ref, payload.integration_path)
        integration_path = payload.integration_path or _default_integration_path(endpoint)
        if integration_path and integration_path not in allowed_integration_paths_for_protocol(endpoint.protocol):
            raise ValueError(f"Integration path {integration_path} is not compatible with endpoint protocol {endpoint.protocol}.")

        task = _get_or_create_commissioning_task(
            context,
            entity_ref=entity.id,
            endpoint_ref=endpoint.id,
            integration_path=integration_path,
            role=payload.role or "",
        )
        log_entries: list[dict] = [
            {
                "level": "info",
                "event": "commissioning_workflow_started",
                "entity_ref": entity.id,
                "endpoint_ref": endpoint.id,
                "integration_path": integration_path,
            }
        ]
        output: dict = {
            "entity_ref": entity.id,
            "endpoint": _endpoint_as_tool_payload(endpoint),
            "integration_path": integration_path,
            "task_ref": task.id,
            "phase": "preparation",
            "effects_not_included": [
                "no_user_decision_approval",
                "no_peer_configuration_change",
                "no_ship_trust_established",
                "no_spine_feature_validation",
                "no_telemetry_validation",
                "no_control_validation",
            ],
        }

        if integration_path == "eebus_spine":
            identity = get_or_create_eebus_local_identity(
                context.session,
                site_id=context.site.id,
                common_name="Helios Home HEMS",
            )
            identity_payload = eebus_identity_public_payload(identity)
            endpoint_properties = endpoint.properties or {}
            log_entries.extend(
                [
                    {
                        "level": "info",
                        "event": "local_eebus_identity_ready",
                        "local_ski": identity.ski,
                    },
                    {
                        "level": "warning",
                        "event": "manual_peer_trust_required",
                        "remote_ski": endpoint_properties.get("ski", ""),
                        "remote_register": endpoint_properties.get("register"),
                        "local_ski": identity.ski,
                    },
                ]
            )
            _add_unique_blocker(
                context,
                task=task,
                subject_ref=entity.id,
                blocker_type="eebus_peer_trust_required",
                summary="eebus_peer_trust_required",
                details={
                    "endpoint_ref": endpoint.id,
                    "remote_ski": endpoint_properties.get("ski", ""),
                    "remote_register": endpoint_properties.get("register"),
                    "local_ski": identity.ski,
                    "required_external_action": "Authorize the Helios local SKI in the peer EEBus/SHIP trust configuration, then retry commissioning.",
                },
            )
            output.update(
                {
                    "phase": "waiting_for_peer_trust",
                    "status": "blocked_waiting_for_user_action",
                    "local_identity": identity_payload,
                    "required_external_action": {
                        "action": "authorize_local_ski_on_peer",
                        "local_ski": identity.ski,
                        "remote_ski": endpoint_properties.get("ski", ""),
                        "remote_register": endpoint_properties.get("register"),
                    },
                }
            )
        else:
            output.update(
                {
                    "phase": "adapter_not_implemented",
                    "status": "blocked_missing_protocol_commissioning_adapter",
                }
            )
            log_entries.append(
                {
                    "level": "warning",
                    "event": "commissioning_adapter_missing",
                    "integration_path": integration_path,
                }
            )

        task.status = "blocked"
        task.updated_at = utcnow()
        task.context = {
            **(task.context or {}),
            "current_phase": output["phase"],
            "endpoint_ref": endpoint.id,
            "integration_path": integration_path,
        }
        context.session.add(task)
        add_task_step(
            context.session,
            task_id=task.id,
            step_key="commissioning_prepare",
            title="commissioning_prepare",
            status="completed",
            summary=output["phase"],
            result={
                "endpoint_ref": endpoint.id,
                "integration_path": integration_path,
                "status": output.get("status", ""),
            },
        )
        diagnostic = ProtocolDiagnosticRun(
            id=f"protocol-diagnostic-{uuid4().hex[:12]}",
            site_id=context.site.id,
            thread_id=context.thread.id,
            turn_id=context.turn.id,
            entity_ref=entity.id,
            endpoint_ref=endpoint.id,
            protocol=endpoint.protocol,
            integration_path=integration_path,
            status=str(output.get("status") or output["phase"]),
            log_entries=log_entries,
            result={
                "phase": output["phase"],
                "status": output.get("status", ""),
                "blocker_codes": ["eebus_peer_trust_required"] if integration_path == "eebus_spine" else ["commissioning_adapter_missing"],
            },
            created_at=utcnow(),
        )
        context.session.add(diagnostic)
        context.session.commit()
        output["diagnostic_run_ref"] = diagnostic.id
        output["log_entries"] = log_entries
        output["connection_facets"] = connection_facets_for_entity(context.session, entity_ref=entity.id)
        return ToolExecutionResult(output=output)


class CommissioningGetLogTool:
    name = "commissioning.get_log"
    purpose = "Read compact commissioning and protocol diagnostic logs for one entity or explicit diagnostic runs."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "debug")
    input_model = CommissioningGetLogInput
    mutates_state = False
    reads = ["protocol_diagnostic_run", "work_store"]
    writes: list[str] = []
    side_effects: list[str] = []
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: CommissioningGetLogInput) -> ToolExecutionResult:
        statement = select(ProtocolDiagnosticRun).where(ProtocolDiagnosticRun.site_id == context.site.id)
        if payload.diagnostic_run_refs:
            statement = statement.where(ProtocolDiagnosticRun.id.in_(payload.diagnostic_run_refs))
        elif payload.entity_ref:
            entity = resolve_entity(context.session, payload.entity_ref)
            if entity is None:
                raise ValueError(f"Unknown Home Graph entity: {payload.entity_ref}")
            statement = statement.where(ProtocolDiagnosticRun.entity_ref == entity.id)
        runs = context.session.scalars(
            statement.order_by(ProtocolDiagnosticRun.created_at.desc()).limit(payload.limit)
        ).all()
        return ToolExecutionResult(
            output={
                "diagnostic_runs": [
                    {
                        "diagnostic_run_ref": run.id,
                        "entity_ref": run.entity_ref,
                        "endpoint_ref": run.endpoint_ref,
                        "protocol": run.protocol,
                        "integration_path": run.integration_path,
                        "status": run.status,
                        "result": run.result or {},
                        "log_entries": run.log_entries or [],
                        "created_at": run.created_at,
                    }
                    for run in runs
                ]
            }
        )


def _select_endpoint(
    context: AgentToolContext,
    entity_ref: str,
    endpoint_ref: str,
    integration_path: str,
) -> ProtocolEndpoint:
    if endpoint_ref:
        endpoint = context.session.get(ProtocolEndpoint, endpoint_ref)
        if endpoint is None:
            raise ValueError(f"Unknown protocol endpoint: {endpoint_ref}")
        if endpoint.owner_ref != entity_ref:
            raise ValueError(f"Endpoint {endpoint_ref} does not belong to {entity_ref}.")
        return endpoint
    endpoints = context.session.scalars(
        select(ProtocolEndpoint)
        .where(ProtocolEndpoint.owner_ref == entity_ref)
        .order_by(ProtocolEndpoint.protocol, ProtocolEndpoint.service_name)
    ).all()
    if integration_path:
        endpoints = [
            endpoint
            for endpoint in endpoints
            if integration_path in allowed_integration_paths_for_protocol(endpoint.protocol)
        ]
    if not endpoints:
        raise ValueError(f"No protocol endpoint is available for {entity_ref}.")
    return endpoints[0]


def _default_integration_path(endpoint: ProtocolEndpoint) -> str:
    allowed = allowed_integration_paths_for_protocol(endpoint.protocol)
    return allowed[0] if len(allowed) == 1 else ""


def _get_or_create_commissioning_task(
    context: AgentToolContext,
    *,
    entity_ref: str,
    endpoint_ref: str,
    integration_path: str,
    role: str,
) -> AgentTask:
    tasks = context.session.scalars(
        select(AgentTask)
        .where(
            AgentTask.site_id == context.site.id,
            AgentTask.task_type == "commission_role_candidate",
            AgentTask.status.in_(["open", "running", "blocked"]),
        )
        .order_by(AgentTask.updated_at.desc())
    ).all()
    for task in tasks:
        refs = set(task.target_refs or [])
        if entity_ref in refs or endpoint_ref in refs:
            return task
    task = create_task(
        context.session,
        site_id=context.site.id,
        thread_id=context.thread.id,
        turn_id=context.turn.id,
        task_type="commission_role_candidate",
        title="commission_role_candidate",
        goal="commission_role_candidate",
        target_refs=[ref for ref in [entity_ref, endpoint_ref, f"role:{role}" if role else ""] if ref],
        context={
            "endpoint_ref": endpoint_ref,
            "integration_path": integration_path,
            "current_phase": "commissioning_prepare",
        },
    )
    return task


def _add_unique_blocker(
    context: AgentToolContext,
    *,
    task: AgentTask,
    subject_ref: str,
    blocker_type: str,
    summary: str,
    details: dict,
) -> Blocker:
    existing = context.session.scalars(
        select(Blocker)
        .where(
            Blocker.status == "open",
            Blocker.blocker_type == blocker_type,
            Blocker.subject_ref == subject_ref,
        )
        .order_by(Blocker.created_at.desc())
        .limit(1)
    ).first()
    if existing is not None:
        existing.task_id = task.id
        existing.details = details
        context.session.add(existing)
        return existing
    return add_blocker(
        context.session,
        task_id=task.id,
        subject_ref=subject_ref,
        blocker_type=blocker_type,
        summary=summary,
        details=details,
    )
