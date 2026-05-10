from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AgentTask, Blocker, ProtocolDiagnosticRun, ProtocolEndpoint, Site, utcnow
from app.db.session import get_session_factory
from app.home_graph.service import connection_facets_for_entity, resolve_entity, sync_inventory_to_home_graph
from app.services.eebus_identity import eebus_identity_public_payload, get_or_create_eebus_local_identity
from app.services.eebus_runtime import (
    EebusPeerTrustMaterial,
    get_eebus_runtime_manager,
    probe_eebus_peer_certificate,
    resolve_eebus_trust_blockers,
    update_endpoint_peer_trust_material,
)
from app.work_store.service import add_blocker, add_task_step, create_task
from app.workflows.role_binding import allowed_integration_paths_for_protocol


@dataclass(slots=True)
class EebusConnectionContext:
    session: Session
    site: Site
    thread_id: str | None
    turn_id: str | None


def establish_eebus_connection(
    context: EebusConnectionContext,
    *,
    entity_ref: str,
    endpoint_ref: str = "",
    integration_path: str = "",
    role: str = "",
) -> dict:
    sync_inventory_to_home_graph(context.session, context.site.id)
    entity = resolve_entity(context.session, entity_ref)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_ref}")
    endpoint = _select_endpoint(context.session, entity.id, endpoint_ref, integration_path)
    resolved_path = integration_path or _default_integration_path(endpoint)
    if resolved_path != "eebus_spine":
        raise ValueError("connection.establish currently supports EEBus/SPINE endpoints only.")
    if resolved_path not in allowed_integration_paths_for_protocol(endpoint.protocol):
        raise ValueError(f"Integration path {resolved_path} is not compatible with endpoint protocol {endpoint.protocol}.")

    diagnostic_id = f"protocol-diagnostic-{uuid4().hex[:12]}"
    task = _get_or_create_commissioning_task(
        context,
        entity_ref=entity.id,
        endpoint_ref=endpoint.id,
        integration_path=resolved_path,
        role=role,
    )
    previous_task_context = dict(task.context or {})
    previous_phase = str(previous_task_context.get("current_phase") or "")
    previous_attempt_count = int(previous_task_context.get("connection_attempt_count") or 0)
    _resolve_stale_commissioning_blockers(context.session, task=task, entity_ref=entity.id)
    endpoint_properties = endpoint.properties or {}
    log_entries: list[dict] = [
        {
            "level": "info",
            "event": "connection_establish_started",
            "entity_ref": entity.id,
            "endpoint_ref": endpoint.id,
            "integration_path": resolved_path,
        }
    ]
    blocker_codes: list[str] = []
    output: dict = {
        "entity_ref": entity.id,
        "endpoint": _endpoint_payload(endpoint),
        "integration_path": resolved_path,
        "task_ref": task.id,
        "phase": "preparation",
        "status": "preparing",
        "effects_not_included": [
            "no_user_decision_approval",
            "no_peer_configuration_change",
            "no_spine_feature_validation",
            "no_telemetry_validation",
            "no_control_validation",
        ],
    }

    identity = get_or_create_eebus_local_identity(
        context.session,
        site_id=context.site.id,
        common_name="Helios Home HEMS",
    )
    identity_payload = eebus_identity_public_payload(identity)
    log_entries.append({"level": "info", "event": "local_eebus_identity_ready", "local_ski": identity.ski})

    try:
        peer = _peer_trust_material(endpoint)
        update_endpoint_peer_trust_material(context.session, endpoint, peer)
        context.session.flush()
        log_entries.append(
            {
                "level": "info",
                "event": "peer_certificate_materialized",
                "remote_ski": endpoint_properties.get("ski", ""),
                "remote_certificate_ski": peer.certificate_ski,
                "txt_ski_matches_certificate_ski": peer.txt_ski_matches_certificate_ski,
            }
        )
        _ensure_diagnostic_run_visible(
            context,
            diagnostic_id=diagnostic_id,
            entity_ref=entity.id,
            endpoint=endpoint,
            integration_path=resolved_path,
            log_entries=log_entries,
        )
    except Exception as exc:
        blocker_codes.append("eebus_peer_certificate_unavailable")
        log_entries.append(
            {
                "level": "error",
                "event": "peer_certificate_probe_failed",
                "host": endpoint.host,
                "port": endpoint.port,
                "error": str(exc),
            }
        )
        _add_unique_blocker(
            context.session,
            task=task,
            subject_ref=entity.id,
            blocker_type="eebus_peer_certificate_unavailable",
            summary="eebus_peer_certificate_unavailable",
            details={
                "endpoint_ref": endpoint.id,
                "host": endpoint.host,
                "port": endpoint.port,
                "remote_ski": endpoint_properties.get("ski", ""),
                "local_ski": identity.ski,
                "error": str(exc),
            },
        )
        output.update(
            {
                "phase": "waiting_for_peer_certificate",
                "status": "blocked_peer_certificate_unavailable",
                "local_identity": identity_payload,
                "peer_certificate": {"status": "unavailable", "error": str(exc)},
                "required_user_action": {
                    "action": "authorize_local_ski_on_peer_if_required",
                    "local_ski": identity.ski,
                },
            }
        )
    else:
        runtime = get_eebus_runtime_manager().start_or_update(
            session_factory=get_session_factory(),
            settings=get_settings(),
            local_identity=identity,
            peer=peer,
            entity_ref=entity.id,
            endpoint_ref=endpoint.id,
            diagnostic_run_ref=diagnostic_id,
            connection_direction="auto",
        )
        runtime_payload = runtime.as_dict()
        is_ready = runtime.status == "ship_ready" and peer.certificate_ski in set(runtime.ready_peer_skis)
        attempt_count = previous_attempt_count + 1
        if is_ready:
            resolve_eebus_trust_blockers(context.session, subject_ref=entity.id, task_id=task.id)
            _resolve_connection_blockers(context.session, task=task, entity_ref=entity.id)
            phase = "ship_ready"
            status = "connected_ship_ready"
            effects_not_included = [
                "no_user_decision_approval",
                "no_peer_configuration_change",
                "no_telemetry_validation",
                "no_control_validation",
            ]
        else:
            runtime_error = _runtime_error(runtime_payload)
            peer_rejected = _runtime_error_indicates_peer_rejection(runtime_error)
            if runtime.status == "failed" and peer_rejected and previous_phase not in {"waiting_for_user_trust"}:
                phase = "waiting_for_user_trust"
                status = "waiting_for_user_trust"
                blocker_codes.append("eebus_peer_trust_required")
                _add_unique_blocker(
                    context.session,
                    task=task,
                    subject_ref=entity.id,
                    blocker_type="eebus_peer_trust_required",
                    summary="eebus_peer_trust_required",
                    details={
                        "endpoint_ref": endpoint.id,
                        "remote_ski": endpoint_properties.get("ski", ""),
                        "remote_certificate_ski": peer.certificate_ski,
                        "local_ski": identity.ski,
                        "last_error": runtime_error,
                        "retry_tool": "connection.establish",
                    },
                )
            elif runtime.status == "failed":
                phase = "ship_failed"
                status = "peer_rejected_after_retry" if peer_rejected else "failed_ship_runtime"
                blocker_codes.append("eebus_ship_runtime_failed")
                _add_unique_blocker(
                    context.session,
                    task=task,
                    subject_ref=entity.id,
                    blocker_type="eebus_ship_runtime_failed",
                    summary="eebus_ship_runtime_failed",
                    details={
                        "endpoint_ref": endpoint.id,
                        "remote_ski": endpoint_properties.get("ski", ""),
                        "remote_certificate_ski": peer.certificate_ski,
                        "local_ski": identity.ski,
                        "last_error": runtime_error,
                        "attempt_count": attempt_count,
                        "retry_tool": "connection.establish",
                    },
                )
            else:
                phase = "waiting_for_ship_session"
                status = _connection_status_from_runtime(runtime_payload)
                blocker_codes.append("eebus_peer_connection_pending")
                _add_unique_blocker(
                    context.session,
                    task=task,
                    subject_ref=entity.id,
                    blocker_type="eebus_peer_connection_pending",
                    summary="eebus_peer_connection_pending",
                    details={
                        "endpoint_ref": endpoint.id,
                        "remote_ski": endpoint_properties.get("ski", ""),
                        "remote_certificate_ski": peer.certificate_ski,
                        "local_ski": identity.ski,
                        "runtime": runtime_payload,
                        "retry_tool": "connection.establish",
                    },
                )
            effects_not_included = output["effects_not_included"]
        log_entries.append(
            {
                "level": "error" if runtime.status == "failed" else "info",
                "event": "eebus_connection_workflow_result",
                "runtime_status": runtime.status,
                "local_ski": identity.ski,
                "remote_certificate_ski": peer.certificate_ski,
                "runtime_connection_states": runtime_payload.get("connection_states", {}),
                "previous_phase": previous_phase,
                "connection_attempt_count": attempt_count,
                "outbound_target": {
                    "host": peer.host,
                    "port": peer.port,
                    "path": peer.path,
                    "server_name": peer.server_name or peer.host,
                },
                "listener": {
                    "bind_host": runtime.bind_host,
                    "port": runtime.port,
                    "path": runtime.path,
                    "interface_ip": runtime.interface_ip,
                },
            }
        )
        output.update(
            {
                "phase": phase,
                "status": status,
                "effects_not_included": effects_not_included,
                "local_identity": identity_payload,
                "peer_certificate": {
                    "status": "materialized",
                    "remote_ski": endpoint_properties.get("ski", ""),
                    "remote_certificate_ski": peer.certificate_ski,
                    "txt_ski_matches_certificate_ski": peer.txt_ski_matches_certificate_ski,
                },
                "ship_runtime": runtime_payload,
                "required_user_action": _required_user_action(identity.ski, peer, runtime_payload, is_ready),
                "connection_lifecycle": {
                    "previous_phase": previous_phase,
                    "current_phase": phase,
                    "connection_attempt_count": attempt_count,
                    "state_is_tool_verified": True,
                    "user_reported_trust_is_not_treated_as_ship_ready": True,
                    "verified_states": {
                        "local_identity": "ready",
                        "peer_certificate": "materialized",
                        "ship_session": "ready" if is_ready else "not_ready",
                        "spine_feature_exchange": "not_validated",
                        "lpc_lpp_receive": "not_validated",
                    },
                },
                "connection_attempt": {
                    "outbound_target": {
                        "host": peer.host,
                        "port": peer.port,
                        "path": peer.path,
                        "server_name": peer.server_name or peer.host,
                    },
                    "local_listener": {
                        "interface_ip": runtime.interface_ip,
                        "port": runtime.port,
                        "path": runtime.path,
                    },
                    "directions_attempted": runtime_payload.get("active_connection_directions", []),
                },
            }
        )

    task.status = "blocked" if blocker_codes else "in_progress"
    task.updated_at = utcnow()
    task.context = {
        **(task.context or {}),
        "current_phase": output["phase"],
        "endpoint_ref": endpoint.id,
        "integration_path": resolved_path,
        "connection_attempt_count": (previous_attempt_count + 1) if output.get("ship_runtime") else previous_attempt_count,
        "local_ski": output.get("local_identity", {}).get("ski", ""),
        "remote_ski": (output.get("peer_certificate") or {}).get("remote_certificate_ski", ""),
        "last_connection_status": output.get("status", ""),
    }
    context.session.add(task)
    add_task_step(
        context.session,
        task_id=task.id,
        step_key="eebus_connection_establish",
        title="eebus_connection_establish",
        status="completed",
        summary=output["phase"],
        result={
            "endpoint_ref": endpoint.id,
            "integration_path": resolved_path,
            "status": output.get("status", ""),
        },
    )
    diagnostic = _upsert_diagnostic_run(
        context,
        diagnostic_id=diagnostic_id,
        entity_ref=entity.id,
        endpoint=endpoint,
        integration_path=resolved_path,
        status=str(output.get("status") or output["phase"]),
        log_entries=log_entries,
        result={
            "phase": output["phase"],
            "status": output.get("status", ""),
            "blocker_codes": blocker_codes,
            "runtime": output.get("ship_runtime", {}),
            "required_user_action": output.get("required_user_action", {}),
        },
    )
    context.session.add(diagnostic)
    context.session.commit()
    output["diagnostic_run_ref"] = diagnostic.id
    output["log_entries"] = log_entries
    output["connection_facets"] = connection_facets_for_entity(context.session, entity_ref=entity.id)
    return output


def _peer_trust_material(endpoint: ProtocolEndpoint) -> EebusPeerTrustMaterial:
    properties = endpoint.properties or {}
    certificate_pem = str(properties.get("peer_certificate_pem") or "")
    certificate_ski = str(properties.get("peer_certificate_ski") or "")
    advertised_ski = str(properties.get("ski") or "")
    if certificate_pem and certificate_ski:
        return EebusPeerTrustMaterial(
            host=endpoint.host,
            port=int(endpoint.port or 0),
            server_name=str(properties.get("target") or endpoint.host),
            advertised_ski=advertised_ski,
            certificate_pem=certificate_pem,
            certificate_ski=certificate_ski,
            txt_ski_matches_certificate_ski=properties.get("txt_ski_matches_certificate_ski"),
            client_cert_requested=bool((properties.get("tls_probe") or {}).get("client_cert_requested")),
            openssl_exit_code=int((properties.get("tls_probe") or {}).get("openssl_exit_code") or 0),
            path=str(properties.get("path") or "/ship/"),
        )
    peer = probe_eebus_peer_certificate(
        host=endpoint.host,
        port=int(endpoint.port or 0),
        server_name=str(properties.get("target") or endpoint.host),
        advertised_ski=advertised_ski,
        timeout_seconds=max(3.0, get_settings().eebus_timeout_seconds + 5.0),
    )
    peer.path = str(properties.get("path") or "/ship/")
    return peer


def _connection_status_from_runtime(runtime_payload: dict) -> str:
    if runtime_payload.get("status") == "failed":
        return "failed_ship_runtime"
    states = [
        state
        for endpoint_state in (runtime_payload.get("connection_states") or {}).values()
        for state in endpoint_state.values()
        if isinstance(state, dict)
    ]
    if any(state.get("status") == "failed" for state in states):
        return "needs_peer_trust_or_retry"
    if any(state.get("status") == "connecting" for state in states):
        return "connecting_ship_session"
    return "ship_session_pending"


def _runtime_error(runtime_payload: dict) -> str:
    errors: list[str] = []
    if runtime_payload.get("error"):
        errors.append(str(runtime_payload.get("error")))
    for endpoint_state in (runtime_payload.get("connection_states") or {}).values():
        if not isinstance(endpoint_state, dict):
            continue
        for state in endpoint_state.values():
            if isinstance(state, dict) and state.get("error"):
                errors.append(str(state.get("error")))
    for event in runtime_payload.get("recent_events") or []:
        if isinstance(event, dict) and event.get("error"):
            errors.append(str(event.get("error")))
        if isinstance(event, dict) and event.get("reason"):
            errors.append(str(event.get("reason")))
    return " | ".join(dict.fromkeys(errors))


def _runtime_error_indicates_peer_rejection(error: str) -> bool:
    normalized = error.lower()
    return "rejected by application" in normalized or "node rejected" in normalized


def _required_user_action(
    local_ski: str,
    peer: EebusPeerTrustMaterial,
    runtime_payload: dict,
    is_ready: bool,
) -> dict:
    if is_ready:
        return {}
    runtime_error = _runtime_error(runtime_payload)
    states = [
        state
        for endpoint_state in (runtime_payload.get("connection_states") or {}).values()
        for state in endpoint_state.values()
        if isinstance(state, dict)
    ]
    errors = " ".join(str(state.get("error", "")) for state in states if state.get("error"))
    if _runtime_error_indicates_peer_rejection(runtime_error):
        reason = "peer_rejected_application_until_local_ski_is_authorized_or_connection_is_retried"
    elif "trust" in errors.lower() or "pair" in errors.lower() or "certificate" in errors.lower():
        reason = "peer_trust_required_or_rejected"
    else:
        reason = "connection_not_ready"
    return {
        "action": "authorize_local_ski_on_peer_then_retry_connection_establish",
        "reason": reason,
        "local_ski": local_ski,
        "peer_ski": peer.certificate_ski,
        "retry_tool": "connection.establish",
        "ship_ready": False,
    }


def _endpoint_payload(endpoint: ProtocolEndpoint) -> dict:
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


def _ensure_diagnostic_run_visible(
    context: EebusConnectionContext,
    *,
    diagnostic_id: str,
    entity_ref: str,
    endpoint: ProtocolEndpoint,
    integration_path: str,
    log_entries: list[dict],
) -> None:
    if context.session.get(ProtocolDiagnosticRun, diagnostic_id) is not None:
        return
    context.session.add(
        ProtocolDiagnosticRun(
            id=diagnostic_id,
            site_id=context.site.id,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
            entity_ref=entity_ref,
            endpoint_ref=endpoint.id,
            protocol=endpoint.protocol,
            integration_path=integration_path,
            status="starting",
            log_entries=list(log_entries),
            result={"phase": "starting", "status": "starting"},
            created_at=utcnow(),
        )
    )
    context.session.commit()


def _upsert_diagnostic_run(
    context: EebusConnectionContext,
    *,
    diagnostic_id: str,
    entity_ref: str,
    endpoint: ProtocolEndpoint,
    integration_path: str,
    status: str,
    log_entries: list[dict],
    result: dict,
) -> ProtocolDiagnosticRun:
    diagnostic = context.session.get(ProtocolDiagnosticRun, diagnostic_id)
    if diagnostic is None:
        diagnostic = ProtocolDiagnosticRun(
            id=diagnostic_id,
            site_id=context.site.id,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
            entity_ref=entity_ref,
            endpoint_ref=endpoint.id,
            protocol=endpoint.protocol,
            integration_path=integration_path,
            created_at=utcnow(),
        )
    runtime_entries = [
        entry
        for entry in (diagnostic.log_entries or [])
        if entry.get("event") not in {item.get("event") for item in log_entries}
    ]
    diagnostic.status = status
    diagnostic.log_entries = [*log_entries, *runtime_entries][-80:]
    diagnostic.result = {**(diagnostic.result or {}), **result}
    return diagnostic


def _select_endpoint(session: Session, entity_ref: str, endpoint_ref: str, integration_path: str) -> ProtocolEndpoint:
    if endpoint_ref:
        endpoint = session.get(ProtocolEndpoint, endpoint_ref)
        if endpoint is None:
            raise ValueError(f"Unknown protocol endpoint: {endpoint_ref}")
        if endpoint.owner_ref != entity_ref:
            raise ValueError(f"Endpoint {endpoint_ref} does not belong to {entity_ref}.")
        return endpoint
    endpoints = session.scalars(
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
    context: EebusConnectionContext,
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
    return create_task(
        context.session,
        site_id=context.site.id,
        thread_id=context.thread_id,
        turn_id=context.turn_id,
        task_type="commission_role_candidate",
        title="commission_role_candidate",
        goal="commission_role_candidate",
        target_refs=[ref for ref in [entity_ref, endpoint_ref, f"role:{role}" if role else ""] if ref],
        context={
            "endpoint_ref": endpoint_ref,
            "integration_path": integration_path,
            "current_phase": "connection_establish",
        },
    )


def _add_unique_blocker(
    session: Session,
    *,
    task: AgentTask,
    subject_ref: str,
    blocker_type: str,
    summary: str,
    details: dict,
) -> Blocker:
    existing = session.scalars(
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
        session.add(existing)
        return existing
    return add_blocker(
        session,
        task_id=task.id,
        subject_ref=subject_ref,
        blocker_type=blocker_type,
        summary=summary,
        details=details,
    )


def _resolve_stale_commissioning_blockers(session: Session, *, task: AgentTask, entity_ref: str) -> None:
    related_refs = set(task.target_refs or [])
    related_refs.add(entity_ref)
    blockers = session.scalars(
        select(Blocker)
        .where(
            Blocker.status == "open",
            Blocker.blocker_type == "commissioning_workflow_not_started",
        )
        .order_by(Blocker.created_at.desc())
    ).all()
    for blocker in blockers:
        if blocker.task_id != task.id and blocker.subject_ref not in related_refs:
            continue
        details = dict(blocker.details or {})
        details["resolved_by"] = "connection.establish"
        blocker.details = details
        blocker.status = "resolved"
        blocker.resolved_at = utcnow()
        session.add(blocker)


def _resolve_connection_blockers(session: Session, *, task: AgentTask, entity_ref: str) -> None:
    blocker_types = {
        "eebus_peer_connection_pending",
        "eebus_peer_trust_required",
        "eebus_ship_runtime_failed",
        "ship_trust_commissioning_not_validated",
    }
    blockers = session.scalars(
        select(Blocker)
        .where(
            Blocker.status == "open",
            Blocker.blocker_type.in_(blocker_types),
            Blocker.subject_ref == entity_ref,
        )
        .order_by(Blocker.created_at.desc())
    ).all()
    for blocker in blockers:
        if blocker.task_id not in {None, task.id}:
            continue
        blocker.details = {**(blocker.details or {}), "resolved_by": "connection.establish.ship_ready"}
        blocker.status = "resolved"
        blocker.resolved_at = utcnow()
        session.add(blocker)
