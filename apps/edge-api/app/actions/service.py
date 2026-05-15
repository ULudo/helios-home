from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actions.schemas import (
    ActionExecutionRead,
    ConnectionActionRef,
    ConnectionEndpointOptionRead,
    ConnectionOptionsRead,
    ConnectionStateRead,
)
from app.db.models import AgentTask, AuditEvent, Device, ProtocolDiagnosticRun, ProtocolEndpoint, Site, utcnow
from app.hems.load_control import update_load_control_config
from app.hems.materialization import materialize_configured_hems_assets
from app.hems.schemas import HemsLoadControlDeviceConfigUpdate
from app.home_graph.service import connection_facets_for_entity, resolve_entity, sync_inventory_to_home_graph
from app.services.dashboard import remove_device_from_inventory
from app.services.eebus_identity import read_eebus_local_identity
from app.services.eebus_runtime import get_eebus_runtime_manager, runtime_snapshot_for_endpoint
from app.services.http_telemetry import HttpTelemetryProbeResult, refresh_http_device_telemetry
from app.services.modbus_telemetry import (
    ModbusTelemetryProbeResult,
    ensure_modbus_endpoint_for_device,
    record_modbus_probe_evidence,
    refresh_modbus_device_telemetry,
)
from app.workflows.eebus_connection import EebusConnectionContext, establish_eebus_connection
from app.workflows.role_binding import allowed_integration_paths_for_protocol


ActionActor = Literal["user", "agent", "system"]


@dataclass(slots=True)
class ActionContext:
    session: Session
    site: Site
    actor: ActionActor
    thread_id: str | None = None
    turn_id: str | None = None


def execute_action(context: ActionContext, action_name: str, payload: dict[str, Any]) -> ActionExecutionRead:
    context.session.add(
        AuditEvent(
            actor=context.actor,
            action="start_action",
            target_type="action",
            target_id=action_name,
            summary=f"Started {action_name}.",
            details={"input": jsonable_encoder(payload)},
            created_at=utcnow(),
        )
    )
    context.session.commit()
    try:
        output, ui_events = _execute_action(context, action_name, payload)
    except Exception as exc:
        context.session.rollback()
        context.session.add(
            AuditEvent(
                actor=context.actor,
                action="fail_action",
                target_type="action",
                target_id=action_name,
                summary=f"{action_name} failed.",
                details={"error": str(exc), "error_type": exc.__class__.__name__},
                created_at=utcnow(),
            )
        )
        context.session.commit()
        raise
    context.session.add(
        AuditEvent(
            actor=context.actor,
            action="complete_action",
            target_type="action",
            target_id=action_name,
            summary=f"Completed {action_name}.",
            details={"output": jsonable_encoder(output), "ui_events": jsonable_encoder(ui_events)},
            created_at=utcnow(),
        )
    )
    context.session.commit()
    return ActionExecutionRead(
        action_name=action_name,
        actor=context.actor,
        status=str(output.get("status") or "completed"),
        output=output,
        ui_events=ui_events,
    )


def _execute_action(context: ActionContext, action_name: str, payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if action_name == "connection.establish":
        integration_path = str(payload.get("integration_path") or "")
        if integration_path == "http_local":
            output = _establish_http_local_connection(
                context,
                entity_ref=str(payload.get("entity_ref") or ""),
                endpoint_ref=str(payload.get("endpoint_ref") or ""),
            )
        elif integration_path in {"modbus_tcp", "sunspec_modbus"}:
            output = _establish_modbus_connection(
                context,
                entity_ref=str(payload.get("entity_ref") or ""),
                endpoint_ref=str(payload.get("endpoint_ref") or ""),
                integration_path=integration_path,
            )
        elif integration_path == "eebus_spine":
            output = establish_eebus_connection(
                EebusConnectionContext(
                    session=context.session,
                    site=context.site,
                    thread_id=context.thread_id,
                    turn_id=context.turn_id,
                ),
                entity_ref=str(payload.get("entity_ref") or ""),
                endpoint_ref=str(payload.get("endpoint_ref") or ""),
                integration_path=integration_path,
                role=str(payload.get("role") or ""),
            )
        else:
            raise ValueError(f"Unsupported connection integration path: {integration_path}")
        ui_events = [
            {
                "event_type": "connection.overlay.open",
                "payload": {
                    "entity_ref": output.get("entity_ref", payload.get("entity_ref", "")),
                    "endpoint_ref": (output.get("endpoint") or {}).get("endpoint_ref", payload.get("endpoint_ref", "")),
                    "integration_path": output.get("integration_path", payload.get("integration_path", "")),
                    "mode": output.get("phase", ""),
                },
            }
        ]
        return output, ui_events
    if action_name == "connection.disconnect":
        output = _disconnect_connection(
            context,
            entity_ref=str(payload.get("entity_ref") or ""),
            endpoint_ref=str(payload.get("endpoint_ref") or ""),
            integration_path=str(payload.get("integration_path") or ""),
        )
        return output, []
    if action_name == "connection.get_state":
        state = get_connection_state(
            context.session,
            context.site,
            entity_ref=str(payload.get("entity_ref") or ""),
            endpoint_ref=str(payload.get("endpoint_ref") or ""),
            integration_path=str(payload.get("integration_path") or ""),
        )
        return state.model_dump(mode="json"), []
    if action_name == "connection.get_options":
        options = get_connection_options(context.session, context.site, str(payload.get("device_id") or ""))
        return options.model_dump(mode="json"), []
    if action_name == "inventory.remove_device":
        device_id = str(payload.get("device_id") or "")
        if not device_id:
            raise ValueError("device_id is required.")
        removed = remove_device_from_inventory(context.session, device_id, actor=context.actor)
        if removed is None:
            raise ValueError(f"Unknown device: {device_id}")
        return (
            {
                "status": "removed",
                "device_id": removed.id,
                "device_name": removed.name,
                "rediscovery_required": True,
            },
            [],
        )
    if action_name == "load_control.configure_device":
        config = update_load_control_config(
            context.session,
            HemsLoadControlDeviceConfigUpdate.model_validate(payload),
            actor=context.actor,
        )
        return (
            {
                "status": "configured",
                "device_id": config.device_id,
                "load_control": config.model_dump(mode="json"),
            },
            [{"event_type": "device.details.open", "payload": {"entity_ref": f"device:{config.device_id}"}}],
        )
    if action_name == "ui.open_device_details":
        entity_ref = str(payload.get("entity_ref") or "")
        if not entity_ref:
            raise ValueError("entity_ref is required.")
        return (
            {"status": "opened", "entity_ref": entity_ref},
            [{"event_type": "device.details.open", "payload": {"entity_ref": entity_ref}}],
        )
    if action_name == "ui.open_connection_overlay":
        entity_ref = str(payload.get("entity_ref") or "")
        endpoint_ref = str(payload.get("endpoint_ref") or "")
        integration_path = str(payload.get("integration_path") or "")
        if not entity_ref or not endpoint_ref or not integration_path:
            raise ValueError("entity_ref, endpoint_ref, and integration_path are required.")
        return (
            {
                "status": "opened",
                "entity_ref": entity_ref,
                "endpoint_ref": endpoint_ref,
                "integration_path": integration_path,
            },
            [
                {
                    "event_type": "connection.overlay.open",
                    "payload": {
                        "entity_ref": entity_ref,
                        "endpoint_ref": endpoint_ref,
                        "integration_path": integration_path,
                        "mode": "focus",
                    },
                }
            ],
        )
    raise ValueError(f"Unknown action: {action_name}")


def _establish_http_local_connection(context: ActionContext, *, entity_ref: str, endpoint_ref: str) -> dict[str, Any]:
    entity = resolve_entity(context.session, entity_ref)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_ref}")
    endpoint = _select_endpoint(context.session, entity.id, endpoint_ref, "http_local")
    if endpoint.protocol != "http_local":
        raise ValueError("connection.establish with http_local requires a local HTTP endpoint.")
    device = _device_for_entity_ref(context.session, entity.id)
    now = utcnow()
    telemetry_probe = refresh_http_device_telemetry(context.session, device, endpoint, now=now)
    diagnostic_result = _http_connection_result(device, endpoint, attempt_connect=True, telemetry_probe=telemetry_probe)
    status = str(diagnostic_result["status"])
    phase = str(diagnostic_result["phase"])
    if status == "connected_http_ready" and device is not None:
        tags = list(device.status_tags or [])
        for tag in ("connected", "http_ready"):
            if tag not in tags:
                tags.append(tag)
        device.status_tags = tags
        device.primary_status = "connected" if device.primary_status in {"", "discovered", "visible_only"} else device.primary_status
        context.session.add(device)
        endpoint.status = "connected"
        endpoint.updated_at = now
        context.session.add(endpoint)
        context.session.flush()
        materialize_configured_hems_assets(context.session, site_id=context.site.id)
    else:
        if device is not None:
            device.status_tags = [tag for tag in (device.status_tags or []) if tag not in {"connected", "http_ready"}]
            if device.primary_status == "connected":
                device.primary_status = "visible_only"
            context.session.add(device)
        endpoint.status = "observed"
    endpoint.updated_at = now
    context.session.add(endpoint)
    diagnostic = ProtocolDiagnosticRun(
        id=f"protocol-diagnostic-{uuid4().hex[:12]}",
        site_id=context.site.id,
        thread_id=context.thread_id,
        turn_id=context.turn_id,
        entity_ref=entity.id,
        endpoint_ref=endpoint.id,
        protocol=endpoint.protocol,
        integration_path="http_local",
        status=status,
        log_entries=[
            {
                "level": "info" if status == "connected_http_ready" else "warning",
                "event": "http_local_connection_establish",
                "host": endpoint.host,
                "port": endpoint.port,
                "status": status,
                "phase": phase,
                "telemetry_probe": telemetry_probe.status,
            }
        ],
        result=diagnostic_result,
        created_at=now,
    )
    context.session.add(diagnostic)
    context.session.commit()
    return {
        "status": status,
        "phase": phase,
        "entity_ref": entity.id,
        "endpoint": _endpoint_summary(endpoint),
        "integration_path": "http_local",
        "diagnostic_run_ref": diagnostic.id,
        "required_user_action": diagnostic_result.get("required_user_action", {}),
        "message": diagnostic_result.get("message", ""),
    }


def _establish_modbus_connection(
    context: ActionContext,
    *,
    entity_ref: str,
    endpoint_ref: str,
    integration_path: str,
) -> dict[str, Any]:
    entity = resolve_entity(context.session, entity_ref)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_ref}")
    endpoint = _select_endpoint(context.session, entity.id, endpoint_ref, integration_path)
    if endpoint.protocol != "modbus_tcp":
        raise ValueError("connection.establish with Modbus requires a Modbus/TCP endpoint.")
    device = _device_for_entity_ref(context.session, entity.id)
    now = utcnow()
    telemetry_probe = refresh_modbus_device_telemetry(context.session, device, endpoint, now=now)
    if device is not None and telemetry_probe.probe is not None:
        record_modbus_probe_evidence(context.session, context.site, device, telemetry_probe.probe, now)
    diagnostic_result = _modbus_connection_result(device, endpoint, attempt_connect=True, telemetry_probe=telemetry_probe)
    status = str(diagnostic_result["status"])
    phase = str(diagnostic_result["phase"])
    if status == "connected_sunspec_ready" and device is not None:
        tags = list(device.status_tags or [])
        for tag in ("connected", "modbus_ready"):
            if tag not in tags:
                tags.append(tag)
        device.status_tags = tags
        if telemetry_probe.telemetry:
            device.telemetry = telemetry_probe.telemetry
            device.telemetry_status = "live"
            device.telemetry_updated_at = now
            device.last_seen_at = now
        device.primary_status = "connected" if device.primary_status in {"", "discovered", "visible_only"} else device.primary_status
        context.session.add(device)
        endpoint.status = "connected"
        endpoint.updated_at = now
        context.session.add(endpoint)
        context.session.flush()
        materialize_configured_hems_assets(context.session, site_id=context.site.id)
    else:
        if device is not None:
            device.status_tags = [tag for tag in (device.status_tags or []) if tag not in {"connected", "modbus_ready"}]
            if device.primary_status == "connected":
                device.primary_status = "visible_only"
            context.session.add(device)
        endpoint.status = "observed"
    endpoint.updated_at = now
    context.session.add(endpoint)
    diagnostic = ProtocolDiagnosticRun(
        id=f"protocol-diagnostic-{uuid4().hex[:12]}",
        site_id=context.site.id,
        thread_id=context.thread_id,
        turn_id=context.turn_id,
        entity_ref=entity.id,
        endpoint_ref=endpoint.id,
        protocol=endpoint.protocol,
        integration_path=integration_path,
        status=status,
        log_entries=[
            {
                "level": "info" if status == "connected_sunspec_ready" else "warning",
                "event": "modbus_connection_establish",
                "host": endpoint.host,
                "port": endpoint.port,
                "status": status,
                "phase": phase,
                "telemetry_probe": telemetry_probe.status,
                "sunspec_model_ids": (endpoint.properties or {}).get("sunspec_model_ids", []),
            }
        ],
        result=diagnostic_result,
        created_at=now,
    )
    context.session.add(diagnostic)
    context.session.commit()
    return {
        "status": status,
        "phase": phase,
        "entity_ref": entity.id,
        "endpoint": _endpoint_summary(endpoint),
        "integration_path": integration_path,
        "diagnostic_run_ref": diagnostic.id,
        "required_user_action": diagnostic_result.get("required_user_action", {}),
        "message": diagnostic_result.get("message", ""),
        "telemetry_keys": sorted((telemetry_probe.telemetry or {}).keys()),
        "sunspec_model_ids": (endpoint.properties or {}).get("sunspec_model_ids", []),
    }


def _disconnect_connection(context: ActionContext, *, entity_ref: str, endpoint_ref: str, integration_path: str) -> dict[str, Any]:
    entity = resolve_entity(context.session, entity_ref)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_ref}")
    endpoint = _select_endpoint(context.session, entity.id, endpoint_ref, integration_path)
    now = utcnow()
    if endpoint.protocol == "eebus_ship":
        get_eebus_runtime_manager().disconnect_endpoint(endpoint.id)
    endpoint.status = "disconnected"
    endpoint.updated_at = now
    context.session.add(endpoint)
    device = _device_for_entity_ref(context.session, entity.id)
    if device is not None:
        device.status_tags = [
            tag
            for tag in (device.status_tags or [])
            if tag not in {"connected", "eebus_ship_ready", "http_ready", "modbus_ready"}
        ]
        if device.primary_status == "connected":
            device.primary_status = "visible_only"
        if endpoint.protocol == "http_local":
            device.telemetry_status = "sampled" if device.telemetry else "unknown"
        elif endpoint.protocol == "modbus_tcp":
            device.telemetry_status = "sampled" if device.telemetry else "unknown"
        context.session.add(device)
    diagnostic = ProtocolDiagnosticRun(
        id=f"protocol-diagnostic-{uuid4().hex[:12]}",
        site_id=context.site.id,
        thread_id=context.thread_id,
        turn_id=context.turn_id,
        entity_ref=entity.id,
        endpoint_ref=endpoint.id,
        protocol=endpoint.protocol,
        integration_path=integration_path or _default_integration_path(endpoint),
        status="disconnected",
        log_entries=[{"level": "info", "event": "connection_disconnect", "protocol": endpoint.protocol}],
        result={"phase": "disconnected", "status": "disconnected"},
        created_at=now,
    )
    context.session.add(diagnostic)
    context.session.commit()
    return {
        "status": "disconnected",
        "phase": "disconnected",
        "entity_ref": entity.id,
        "endpoint": _endpoint_summary(endpoint),
        "integration_path": integration_path or _default_integration_path(endpoint),
    }


def _device_for_entity_ref(session: Session, entity_ref: str) -> Device | None:
    if not entity_ref.startswith("device:"):
        return None
    return session.get(Device, entity_ref.removeprefix("device:"))


def _endpoint_summary(endpoint: ProtocolEndpoint) -> dict[str, Any]:
    return {
        "endpoint_ref": endpoint.id,
        "protocol": endpoint.protocol,
        "host": endpoint.host,
        "port": endpoint.port,
        "service_name": endpoint.service_name,
        "status": endpoint.status,
    }


def _http_connection_result(
    device: Device | None,
    endpoint: ProtocolEndpoint,
    *,
    attempt_connect: bool = False,
    telemetry_probe: HttpTelemetryProbeResult | None = None,
) -> dict[str, Any]:
    properties = endpoint.properties or {}
    capabilities = device.capabilities if device is not None and isinstance(device.capabilities, dict) else {}
    telemetry_ready = bool(capabilities.get("monitorable") or capabilities.get("controllable") or capabilities.get("optimizable"))
    fingerprint = str(properties.get("fingerprint_profile") or properties.get("profile") or "")
    manufacturer = str(device.manufacturer if device is not None else "").lower()
    if endpoint.status == "disconnected":
        return {
            "phase": "disconnected",
            "status": "disconnected",
            "message": "Local HTTP endpoint is disconnected from the HEMS inventory state.",
        }
    if attempt_connect and telemetry_probe is not None and telemetry_probe.status != "updated":
        status = "http_unreachable" if telemetry_probe.status == "unreachable" else "http_no_live_telemetry"
        return {
            "phase": "http_probe_failed",
            "status": status,
            "message": telemetry_probe.message,
            "required_user_action": {
                "action": "check_local_http_device",
                "reason": telemetry_probe.status,
            },
        }
    if telemetry_ready and (attempt_connect or endpoint.status == "connected"):
        return {
            "phase": "http_ready",
            "status": "connected_http_ready",
            "message": "Local HTTP telemetry path is live.",
        }
    if telemetry_ready:
        return {
            "phase": "http_ready_to_connect",
            "status": "ready_http_adapter",
            "message": "Local HTTP adapter is available and can be connected to the HEMS.",
        }
    if fingerprint == "generic_http_energy" or endpoint.service_name == "generic_http_energy":
        message = "Local HTTP service is visible, but no device-specific telemetry/control adapter is validated yet."
        if "fronius" in manufacturer:
            message = (
                "Fronius was observed through a local HTTP endpoint, but Helios has not validated a "
                "Fronius HTTP/Solar API or SunSpec Modbus adapter for this endpoint yet."
            )
        return {
            "phase": "http_visible",
            "status": "visible_no_device_adapter",
            "message": message,
            "required_user_action": {
                "action": "device_specific_http_or_modbus_adapter_required",
                "reason": "generic_http_identity_only",
            },
        }
    return {
        "phase": "http_visible",
        "status": "visible_http_endpoint",
        "message": "Local HTTP endpoint is visible.",
    }


def _http_connection_steps(endpoint: ProtocolEndpoint, state: dict[str, Any], *, connected: bool) -> list[dict[str, Any]]:
    adapter_ready = str(state.get("status") or "") in {"connected_http_ready", "ready_http_adapter"}
    return [
        {
            "key": "endpoint_observed",
            "label": "Endpoint observed",
            "status": "completed",
            "detail": f"http {endpoint.host}:{endpoint.port or 80}",
        },
        {
            "key": "device_adapter",
            "label": "Device adapter",
            "status": "completed" if adapter_ready else "blocked",
            "detail": state.get("message", ""),
        },
        {
            "key": "telemetry_control_path",
            "label": "Telemetry / control path",
            "status": "completed" if adapter_ready else "pending",
            "detail": "Validated local HTTP path." if adapter_ready else "Needs a validated device-specific adapter.",
        },
    ]


def _modbus_connection_result(
    device: Device | None,
    endpoint: ProtocolEndpoint,
    *,
    attempt_connect: bool = False,
    telemetry_probe: ModbusTelemetryProbeResult | None = None,
) -> dict[str, Any]:
    properties = endpoint.properties or {}
    model_ids = properties.get("sunspec_model_ids") if isinstance(properties.get("sunspec_model_ids"), list) else []
    telemetry_ready = bool(model_ids) or bool((device.telemetry if device is not None else {}) or {})
    last_probe_status = str(properties.get("last_telemetry_status") or "")
    if endpoint.status == "disconnected":
        return {
            "phase": "disconnected",
            "status": "disconnected",
            "message": "Modbus/TCP endpoint is disconnected from the HEMS inventory state.",
        }
    if attempt_connect and telemetry_probe is not None and telemetry_probe.status != "updated":
        status = "modbus_unreachable" if telemetry_probe.status == "unreachable" else "modbus_no_live_telemetry"
        return {
            "phase": "modbus_probe_failed",
            "status": status,
            "message": telemetry_probe.message,
            "required_user_action": {
                "action": "check_modbus_tcp_device",
                "reason": telemetry_probe.status,
            },
        }
    if endpoint.status == "connected" and last_probe_status in {"unreachable", "empty"}:
        return {
            "phase": "modbus_poll_failed",
            "status": "modbus_telemetry_stale",
            "message": str(properties.get("last_telemetry_message") or "Modbus/TCP telemetry is stale."),
        }
    if telemetry_ready and (attempt_connect or endpoint.status == "connected"):
        return {
            "phase": "sunspec_ready",
            "status": "connected_sunspec_ready",
            "message": "SunSpec Modbus telemetry path is live.",
        }
    if telemetry_ready:
        return {
            "phase": "sunspec_ready_to_connect",
            "status": "ready_sunspec_adapter",
            "message": "SunSpec Modbus endpoint is available and can be connected to the HEMS.",
        }
    if endpoint.host:
        return {
            "phase": "modbus_visible",
            "status": "visible_modbus_endpoint",
            "message": "Modbus/TCP endpoint is visible, but SunSpec telemetry has not been validated yet.",
        }
    return {
        "phase": "modbus_not_materialized",
        "status": "missing_modbus_endpoint",
        "message": "No Modbus/TCP endpoint host is available.",
        "required_user_action": {
            "action": "run_discovery_or_inspect_known_endpoint",
            "reason": "missing_modbus_endpoint_host",
        },
    }


def _modbus_connection_steps(endpoint: ProtocolEndpoint, state: dict[str, Any], *, connected: bool) -> list[dict[str, Any]]:
    properties = endpoint.properties or {}
    model_ids = properties.get("sunspec_model_ids") if isinstance(properties.get("sunspec_model_ids"), list) else []
    telemetry_keys = properties.get("last_telemetry_keys") if isinstance(properties.get("last_telemetry_keys"), list) else []
    dispatch_profile = str(properties.get("dispatch_profile") or "")
    adapter_ready = str(state.get("status") or "") in {"connected_sunspec_ready", "ready_sunspec_adapter"}
    return [
        {
            "key": "endpoint_observed",
            "label": "Endpoint observed",
            "status": "completed" if endpoint.host else "pending",
            "detail": f"modbus_tcp {endpoint.host}:{endpoint.port or 502}" if endpoint.host else "No Modbus/TCP host.",
        },
        {
            "key": "sunspec_signature",
            "label": "SunSpec signature",
            "status": "completed" if model_ids else "pending",
            "detail": f"Model ids: {', '.join(str(model_id) for model_id in model_ids[:8])}" if model_ids else "Not validated yet.",
        },
        {
            "key": "telemetry_path",
            "label": "Telemetry path",
            "status": "completed" if adapter_ready and telemetry_keys else "pending",
            "detail": (
                f"Validated metrics: {', '.join(str(key) for key in telemetry_keys[:6])}"
                if telemetry_keys
                else "Needs a live SunSpec telemetry sample."
            ),
        },
        {
            "key": "control_profile",
            "label": "Control profile",
            "status": "completed" if dispatch_profile else "pending",
            "detail": dispatch_profile or "No writable control profile has been validated.",
        },
    ]


def _connection_endpoint_sort_key(endpoint: ProtocolEndpoint) -> tuple[int, int, int, str, str]:
    properties = endpoint.properties if isinstance(endpoint.properties, dict) else {}
    has_control_profile = bool(str(properties.get("dispatch_profile") or "").strip())
    connection_priority = 0 if endpoint.protocol == "eebus_ship" else 1 if has_control_profile else 2
    status_priority = {"connected": 0, "observed": 1, "disconnected": 2}.get(endpoint.status, 3)
    protocol_priority = {"eebus_ship": 0, "modbus_tcp": 1, "http_local": 2}.get(endpoint.protocol, 9)
    return (
        connection_priority,
        status_priority,
        protocol_priority,
        endpoint.service_name or "",
        endpoint.host or "",
    )


def get_connection_options(session: Session, site: Site, device_id: str) -> ConnectionOptionsRead:
    if not device_id:
        raise ValueError("device_id is required.")
    sync_inventory_to_home_graph(session, site.id)
    device = session.get(Device, device_id)
    if device is None:
        raise ValueError(f"Unknown device: {device_id}")
    ensure_modbus_endpoint_for_device(session, site, device)
    sync_inventory_to_home_graph(session, site.id)
    entity_ref = f"device:{device.id}"
    endpoints = session.scalars(
        select(ProtocolEndpoint)
        .where(ProtocolEndpoint.site_id == site.id, ProtocolEndpoint.owner_ref == entity_ref)
        .order_by(ProtocolEndpoint.protocol, ProtocolEndpoint.service_name, ProtocolEndpoint.host)
    ).all()
    endpoints = [
        endpoint
        for endpoint in endpoints
        if endpoint.protocol not in {"mdns", "ssdp"} and allowed_integration_paths_for_protocol(endpoint.protocol)
    ]
    endpoints = sorted(endpoints, key=_connection_endpoint_sort_key)
    return ConnectionOptionsRead(
        entity_ref=entity_ref,
        device_id=device.id,
        display_name=device.name,
        endpoints=[_connection_endpoint_option(session, site, endpoint) for endpoint in endpoints],
    )


def get_connection_state(
    session: Session,
    site: Site,
    *,
    entity_ref: str,
    endpoint_ref: str = "",
    integration_path: str = "",
) -> ConnectionStateRead:
    if not entity_ref:
        raise ValueError("entity_ref is required.")
    sync_inventory_to_home_graph(session, site.id)
    entity = resolve_entity(session, entity_ref)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_ref}")
    endpoint = _select_endpoint(session, entity.id, endpoint_ref, integration_path)
    resolved_path = integration_path or _default_integration_path(endpoint)
    facets = connection_facets_for_entity(session, entity_ref=entity.id, endpoints=[endpoint])
    local_identity = read_eebus_local_identity(session, site_id=site.id)
    runtime = runtime_snapshot_for_endpoint(endpoint.id) if endpoint.protocol == "eebus_ship" else {}
    diagnostic = _latest_diagnostic(session, site.id, entity.id, endpoint.id, resolved_path)
    task = _latest_task(session, site.id, entity.id, endpoint.id)
    if endpoint.protocol == "http_local":
        device = _device_for_entity_ref(session, entity.id)
        diagnostic_result = diagnostic.result if diagnostic is not None and isinstance(diagnostic.result, dict) else {}
        state_payload = _http_connection_result(device, endpoint)
        if endpoint.status == "disconnected":
            phase = "disconnected"
            status = "disconnected"
        elif endpoint.status == "connected":
            phase = str(diagnostic_result.get("phase") or state_payload["phase"])
            status = str(diagnostic_result.get("status") or state_payload["status"])
        else:
            phase = str(state_payload["phase"])
            status = str(state_payload["status"])
        connected = endpoint.status == "connected" and status == "connected_http_ready"
        can_connect = not connected
        action_input = {"entity_ref": entity.id, "endpoint_ref": endpoint.id, "integration_path": "http_local"}
        return ConnectionStateRead(
            entity_ref=entity.id,
            endpoint_ref=endpoint.id,
            protocol=endpoint.protocol,
            host=endpoint.host,
            port=endpoint.port,
            service_name=endpoint.service_name,
            integration_path="http_local",
            phase=phase,
            status=status,
            can_connect=can_connect,
            steps=_http_connection_steps(endpoint, state_payload, connected=connected),
            required_user_action=state_payload.get("required_user_action", {}),
            connection_facets=facets,
            diagnostic_run_ref=diagnostic.id if diagnostic is not None and endpoint.status == "connected" else "",
            task_ref="",
            last_error=str(state_payload.get("last_error") or ""),
            updated_at=diagnostic.created_at if diagnostic is not None and endpoint.status == "connected" else endpoint.updated_at,
            connect_action=ConnectionActionRef(name="connection.establish", input=action_input) if can_connect else None,
            disconnect_action=ConnectionActionRef(name="connection.disconnect", input=action_input) if connected else None,
        )
    if endpoint.protocol == "modbus_tcp":
        device = _device_for_entity_ref(session, entity.id)
        diagnostic_result = diagnostic.result if diagnostic is not None and isinstance(diagnostic.result, dict) else {}
        state_payload = _modbus_connection_result(device, endpoint)
        if endpoint.status == "disconnected":
            phase = "disconnected"
            status = "disconnected"
        elif endpoint.status == "connected":
            phase = str(diagnostic_result.get("phase") or state_payload["phase"])
            status = str(diagnostic_result.get("status") or state_payload["status"])
        else:
            phase = str(state_payload["phase"])
            status = str(state_payload["status"])
        connected = endpoint.status == "connected" and status == "connected_sunspec_ready"
        can_connect = not connected
        action_input = {"entity_ref": entity.id, "endpoint_ref": endpoint.id, "integration_path": resolved_path or "sunspec_modbus"}
        return ConnectionStateRead(
            entity_ref=entity.id,
            endpoint_ref=endpoint.id,
            protocol=endpoint.protocol,
            host=endpoint.host,
            port=endpoint.port,
            service_name=endpoint.service_name,
            integration_path=resolved_path or "sunspec_modbus",
            phase=phase,
            status=status,
            can_connect=can_connect,
            steps=_modbus_connection_steps(endpoint, state_payload, connected=connected),
            required_user_action=state_payload.get("required_user_action", {}),
            connection_facets=facets,
            diagnostic_run_ref=diagnostic.id if diagnostic is not None and endpoint.status == "connected" else "",
            task_ref="",
            last_error=str(state_payload.get("last_error") or ""),
            updated_at=diagnostic.created_at if diagnostic is not None and endpoint.status == "connected" else endpoint.updated_at,
            connect_action=ConnectionActionRef(name="connection.establish", input=action_input) if can_connect else None,
            disconnect_action=ConnectionActionRef(name="connection.disconnect", input=action_input) if connected else None,
        )
    diagnostic_result = diagnostic.result if diagnostic is not None and isinstance(diagnostic.result, dict) else {}
    diagnostic_runtime = diagnostic_result.get("runtime") if isinstance(diagnostic_result.get("runtime"), dict) else {}
    runtime = _runtime_for_display(endpoint.id, runtime, diagnostic_runtime)
    ready = _runtime_is_ship_ready(runtime)
    last_error = _runtime_error(runtime)
    runtime_mentions_endpoint = runtime.get("endpoint_in_runtime") is True
    task_phase = str((task.context or {}).get("current_phase") or "") if task is not None else ""
    phase = str(diagnostic_result.get("phase") or task_phase)
    if ready:
        phase = "ship_ready"
    elif phase in {"waiting_for_user_trust", "waiting_for_ship_session"} and not runtime_mentions_endpoint and not last_error:
        phase = _phase_from_runtime(endpoint, runtime)
    elif not phase:
        phase = _phase_from_runtime(endpoint, runtime)
    if phase == "ship_ready" and not _runtime_is_ship_ready(runtime):
        phase = _phase_from_runtime(endpoint, runtime)
    status = str(diagnostic_result.get("status") or facets.get("overall_connection_state") or "unknown")
    if ready:
        status = "connected_ship_ready"
    elif status in {"waiting_for_user_trust", "ship_session_pending"} and not runtime_mentions_endpoint and not last_error:
        status = _phase_from_runtime(endpoint, runtime)
    elif status == "connected_ship_ready" and not _runtime_is_ship_ready(runtime):
        status = str(facets.get("overall_connection_state") or _phase_from_runtime(endpoint, runtime))
    required_user_action = {} if ready else dict(diagnostic_result.get("required_user_action") or {})
    if _is_stale_trust_action(required_user_action, runtime, last_error):
        required_user_action = {}
    if required_user_action and _runtime_failed_without_peer_rejection(runtime, last_error):
        required_user_action = _required_user_action_for_state(endpoint, local_identity, runtime, last_error)
    if not required_user_action:
        required_user_action = _required_user_action_for_state(endpoint, local_identity, runtime, last_error)
    expected_trust_wait = _is_expected_trust_wait(phase, required_user_action, last_error)
    display_last_error = "" if ready or expected_trust_wait else last_error
    can_connect = resolved_path == "eebus_spine" and endpoint.protocol == "eebus_ship" and not ready
    action_input = {"entity_ref": entity.id, "endpoint_ref": endpoint.id, "integration_path": resolved_path}
    return ConnectionStateRead(
        entity_ref=entity.id,
        endpoint_ref=endpoint.id,
        protocol=endpoint.protocol,
        host=endpoint.host,
        port=endpoint.port,
        service_name=endpoint.service_name,
        integration_path=resolved_path,
        phase=phase,
        status=status,
        can_connect=can_connect,
        steps=_connection_steps(endpoint, local_identity, runtime, phase=phase, expected_trust_wait=expected_trust_wait),
        required_user_action=required_user_action,
        connection_facets=facets,
        diagnostic_run_ref=diagnostic.id if diagnostic is not None else "",
        task_ref=task.id if task is not None else "",
        local_ski=local_identity.ski if local_identity is not None else "",
        peer_ski=str((endpoint.properties or {}).get("peer_certificate_ski") or (endpoint.properties or {}).get("ski") or ""),
        last_error=display_last_error,
        updated_at=diagnostic.created_at if diagnostic is not None else endpoint.updated_at,
        connect_action=ConnectionActionRef(name="connection.establish", input=action_input) if can_connect else None,
        disconnect_action=ConnectionActionRef(name="connection.disconnect", input=action_input) if ready else None,
    )


def _connection_endpoint_option(session: Session, site: Site, endpoint: ProtocolEndpoint) -> ConnectionEndpointOptionRead:
    properties = endpoint.properties or {}
    allowed_paths = allowed_integration_paths_for_protocol(endpoint.protocol)
    state = get_connection_state(
        session,
        site,
        entity_ref=endpoint.owner_ref,
        endpoint_ref=endpoint.id,
        integration_path=allowed_paths[0] if len(allowed_paths) == 1 else "",
    )
    connectable = state.can_connect or state.disconnect_action is not None
    return ConnectionEndpointOptionRead(
        endpoint_ref=endpoint.id,
        owner_ref=endpoint.owner_ref,
        protocol=endpoint.protocol,
        host=endpoint.host,
        port=endpoint.port,
        service_name=endpoint.service_name,
        status=endpoint.status,
        source=str(properties.get("source") or ""),
        last_seen_at=str(properties.get("last_seen_at") or ""),
        confidence=float(properties.get("confidence") or 0.0),
        allowed_integration_paths=allowed_paths,
        connectable=connectable,
        state=state.model_dump(mode="json"),
        connect_action=state.connect_action,
        disconnect_action=state.disconnect_action,
    )


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
        .order_by(ProtocolEndpoint.protocol, ProtocolEndpoint.service_name, ProtocolEndpoint.host)
    ).all()
    if integration_path:
        endpoints = [endpoint for endpoint in endpoints if integration_path in allowed_integration_paths_for_protocol(endpoint.protocol)]
    if not endpoints:
        raise ValueError(f"No protocol endpoint is available for {entity_ref}.")
    return endpoints[0]


def _default_integration_path(endpoint: ProtocolEndpoint) -> str:
    if endpoint.protocol == "modbus_tcp":
        properties = endpoint.properties or {}
        model_ids = properties.get("sunspec_model_ids") if isinstance(properties.get("sunspec_model_ids"), list) else []
        return "sunspec_modbus" if model_ids else "modbus_tcp"
    allowed_paths = allowed_integration_paths_for_protocol(endpoint.protocol)
    return allowed_paths[0] if len(allowed_paths) == 1 else ""


def _latest_diagnostic(
    session: Session,
    site_id: int,
    entity_ref: str,
    endpoint_ref: str,
    integration_path: str,
) -> ProtocolDiagnosticRun | None:
    statement = (
        select(ProtocolDiagnosticRun)
        .where(
            ProtocolDiagnosticRun.site_id == site_id,
            ProtocolDiagnosticRun.entity_ref == entity_ref,
            ProtocolDiagnosticRun.endpoint_ref == endpoint_ref,
        )
        .order_by(ProtocolDiagnosticRun.created_at.desc())
        .limit(1)
    )
    if integration_path:
        statement = statement.where(ProtocolDiagnosticRun.integration_path == integration_path)
    return session.scalar(statement)


def _latest_task(session: Session, site_id: int, entity_ref: str, endpoint_ref: str) -> AgentTask | None:
    tasks = session.scalars(
        select(AgentTask)
        .where(
            AgentTask.site_id == site_id,
            AgentTask.task_type == "commission_role_candidate",
            AgentTask.status.in_(["open", "running", "blocked"]),
        )
        .order_by(AgentTask.updated_at.desc())
        .limit(20)
    ).all()
    for task in tasks:
        refs = set(task.target_refs or [])
        if entity_ref in refs or endpoint_ref in refs:
            return task
    return None


def _phase_from_runtime(endpoint: ProtocolEndpoint, runtime: dict[str, Any]) -> str:
    if endpoint.protocol != "eebus_ship":
        return "endpoint_visible"
    if _runtime_is_ship_ready(runtime):
        return "ship_ready"
    if runtime.get("status") == "failed":
        return "ship_failed"
    if runtime.get("endpoint_in_runtime"):
        return "waiting_for_ship_session"
    return "not_started"


def _connection_steps(
    endpoint: ProtocolEndpoint,
    local_identity: Any,
    runtime: dict[str, Any],
    *,
    phase: str = "",
    expected_trust_wait: bool = False,
) -> list[dict[str, Any]]:
    properties = endpoint.properties or {}
    runtime_ready = _runtime_is_ship_ready(runtime)
    runtime_error = _runtime_error(runtime)
    peer_rejected = _is_peer_rejection_error(runtime_error)
    raw_runtime_failed = runtime.get("status") == "failed" or any(
        isinstance(state, dict) and state.get("status") == "failed"
        for state in (runtime.get("endpoint_connection_states") or {}).values()
    )
    ship_failed = raw_runtime_failed and not expected_trust_wait and not peer_rejected
    endpoint_peer_ski = str(properties.get("peer_certificate_ski") or properties.get("ski") or "").lower()
    last_limit = runtime.get("last_load_power_limit") if isinstance(runtime.get("last_load_power_limit"), dict) else {}
    received_limit = bool(endpoint_peer_ski and str(last_limit.get("peer_ski") or "").lower() == endpoint_peer_ski)
    peer_trust_detail = "Peer trust is established." if runtime_ready else "Local SKI may need to be trusted on the peer."
    ship_session_detail = str(runtime.get("status") or "not_started")
    if not runtime.get("endpoint_in_runtime"):
        peer_trust_detail = "Trust is checked when Connect starts the SHIP session."
        ship_session_detail = "not_started"
    if expected_trust_wait:
        peer_trust_detail = "Trust the local SKI on the peer, then continue the connection."
        ship_session_detail = "Waiting for peer trust."
    elif ship_failed:
        peer_trust_detail = "No peer trust rejection was reported in the latest attempt."
        ship_session_detail = runtime_error or "SHIP runtime failed."
    return [
        {
            "key": "endpoint_observed",
            "label": "Endpoint observed",
            "status": "completed",
            "detail": f"{endpoint.protocol} {endpoint.host}:{endpoint.port or ''}".rstrip(":"),
        },
        {
            "key": "local_identity",
            "label": "Local EEBUS identity",
            "status": "completed" if local_identity is not None else "pending",
            "detail": local_identity.ski if local_identity is not None else "Created when Connect starts.",
        },
        {
            "key": "peer_certificate",
            "label": "Peer certificate",
            "status": "completed" if properties.get("peer_certificate_pem") else "pending",
            "detail": str(properties.get("peer_certificate_ski") or properties.get("ski") or ""),
        },
        {
            "key": "peer_trust",
            "label": "Peer trust",
            "status": "completed" if runtime_ready else "action_required" if expected_trust_wait else "pending",
            "detail": peer_trust_detail,
        },
        {
            "key": "ship_session",
            "label": "SHIP session",
            "status": "completed" if runtime_ready else "failed" if ship_failed else "pending",
            "detail": ship_session_detail,
        },
        {
            "key": "spine_lpc_lpp",
            "label": "SPINE / LPC-LPP",
            "status": "completed" if received_limit else "pending",
            "detail": "Validated after a live SHIP/SPINE exchange.",
        },
    ]


def _runtime_error(runtime: dict[str, Any]) -> str:
    errors: list[str] = []
    if runtime.get("error"):
        errors.append(str(runtime.get("error")))
    for state in (runtime.get("endpoint_connection_states") or {}).values():
        if isinstance(state, dict) and state.get("error"):
            errors.append(str(state.get("error")))
    for event in runtime.get("recent_events") or []:
        if isinstance(event, dict) and event.get("error"):
            errors.append(str(event.get("error")))
        if isinstance(event, dict) and event.get("reason"):
            errors.append(str(event.get("reason")))
    return " | ".join(dict.fromkeys(errors))


def _is_expected_trust_wait(phase: str, required_user_action: dict[str, Any], last_error: str) -> bool:
    action = str(required_user_action.get("action") or "")
    if last_error and not _is_peer_rejection_error(last_error):
        return False
    return phase == "waiting_for_user_trust" or _is_peer_rejection_error(last_error) or (
        action.startswith("authorize_local_ski") and phase in {"waiting_for_user_trust", "waiting_for_ship_session"}
    )


def _is_stale_trust_action(required_user_action: dict[str, Any], runtime: dict[str, Any], last_error: str) -> bool:
    action = str(required_user_action.get("action") or "")
    if not action.startswith("authorize_local_ski"):
        return False
    if _is_peer_rejection_error(last_error):
        return False
    return not _runtime_is_ship_ready(runtime)


def _required_user_action_for_state(
    endpoint: ProtocolEndpoint,
    local_identity: Any,
    runtime: dict[str, Any],
    last_error: str,
) -> dict[str, Any]:
    if endpoint.protocol != "eebus_ship":
        return {}
    if _runtime_is_ship_ready(runtime):
        return {}
    if local_identity is None:
        return {"action": "press_connect_to_create_local_identity"}
    if _is_peer_rejection_error(last_error):
        return {
            "action": "authorize_local_ski_on_peer_then_continue",
            "local_ski": local_identity.ski,
            "retry_action": "connection.establish",
        }
    if _runtime_failed_without_peer_rejection(runtime, last_error):
        return {
            "action": "resolve_ship_runtime_error_then_continue",
            "reason": "ship_runtime_failed",
            "local_ski": local_identity.ski,
            "retry_action": "connection.establish",
            "last_error": last_error,
        }
    return {"action": "press_connect_to_continue", "local_ski": local_identity.ski}


def _runtime_for_display(
    endpoint_ref: str,
    live_runtime: dict[str, Any],
    diagnostic_runtime: dict[str, Any],
) -> dict[str, Any]:
    if not diagnostic_runtime:
        return live_runtime
    if diagnostic_runtime.get("status") == "ship_ready" and not _runtime_is_ship_ready(live_runtime):
        return live_runtime
    live_status = str(live_runtime.get("status") or "")
    live_mentions_endpoint = bool(live_runtime.get("endpoint_in_runtime"))
    if live_mentions_endpoint and live_status not in {"", "not_started"}:
        return live_runtime
    merged = dict(diagnostic_runtime)
    merged["endpoint_in_runtime"] = endpoint_ref in (merged.get("endpoint_refs") or [])
    merged["endpoint_connection_states"] = dict((merged.get("connection_states") or {}).get(endpoint_ref, {}))
    return merged


def _runtime_is_ship_ready(runtime: dict[str, Any]) -> bool:
    if runtime.get("endpoint_in_runtime") is not True:
        return False
    states = runtime.get("endpoint_connection_states") or {}
    if not isinstance(states, dict):
        return False
    if any(isinstance(state, dict) and state.get("status") == "ready" for state in states.values()):
        return True
    if runtime.get("status") != "ship_ready":
        return False
    ready_peer_skis = {str(row or "").lower() for row in runtime.get("ready_peer_skis") or []}
    endpoint_peer_skis = {
        str(state.get("peer_ski") or "").lower()
        for state in states.values()
        if isinstance(state, dict) and state.get("peer_ski")
    }
    return bool(ready_peer_skis & endpoint_peer_skis)


def _runtime_failed_without_peer_rejection(runtime: dict[str, Any], last_error: str) -> bool:
    failed = runtime.get("status") == "failed" or any(
        isinstance(state, dict) and state.get("status") == "failed"
        for state in (runtime.get("endpoint_connection_states") or {}).values()
    )
    return failed and not _is_peer_rejection_error(last_error)


def _is_peer_rejection_error(error: str) -> bool:
    normalized = error.lower()
    return "rejected by application" in normalized or "node rejected" in normalized
