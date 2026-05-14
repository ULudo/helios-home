from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditEvent, utcnow
from app.domain.enums import RecoveryZone
from app.hems.load_control import (
    active_load_control_limits,
    attach_delivery_state_to_distribution,
    build_constraint_distribution,
    create_load_control_deliveries,
    effective_grid_limits,
    record_load_control_limit,
    update_native_delivery_statuses_from_plan,
)
from app.hems.policy import get_or_create_hems_policy
from app.hems.schemas import (
    EebusLoadPowerLimitCreate,
    EebusLoadPowerLimitDistributionRead,
    HemsPlanHeaderRead,
)
from app.services.discovery_blueprints import RawCandidate


EEBUS_SOURCE_NAME = "eebus_ship_live"
EEBUS_PROTOCOL = "eebus_ship"
LPC_USE_CASE = "limitationOfPowerConsumption"
LPP_USE_CASE = "limitationOfPowerProduction"


@dataclass(slots=True)
class EebusDiscoveryBatch:
    source_name: str
    status: str
    message: str
    candidates: list[RawCandidate]


@dataclass(slots=True)
class EebusSdk:
    discover_ship_services: Any
    build_limit_payload: Any


def _load_sdk() -> EebusSdk:
    try:
        package = import_module("eebus_sdk")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "EEBus support is part of the standard Helios backend, but the eebus-sdk package is missing. "
            "Reinstall backend dependencies with ./scripts/setup-backend.sh."
        ) from exc

    try:
        load_power = import_module("eebus_sdk._load_power")
        build_limit_payload = load_power.build_limit_payload
    except (AttributeError, ModuleNotFoundError) as exc:
        raise RuntimeError("The installed eebus-sdk is missing required LoadControl helpers.") from exc

    return EebusSdk(
        discover_ship_services=package.discover_ship_services,
        build_limit_payload=build_limit_payload,
    )


def _slugify(value: str) -> str:
    normalized = [character.lower() if character.isalnum() else "-" for character in value]
    slug = "".join(normalized).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "eebus-peer"


def _service_to_dict(service: Any) -> dict[str, Any]:
    if hasattr(service, "as_dict"):
        return dict(service.as_dict())
    return {
        "service_name": getattr(service, "service_name", ""),
        "target": getattr(service, "target", None),
        "port": getattr(service, "port", None),
        "path": getattr(service, "path", "/ship/"),
        "ship_id": getattr(service, "ship_id", None),
        "ski": getattr(service, "ski", None),
        "brand": getattr(service, "brand", None),
        "model": getattr(service, "model", None),
        "device_type": getattr(service, "device_type", None),
        "register": getattr(service, "register", None),
        "addresses": getattr(service, "addresses", {"ipv4": [], "ipv6": []}),
        "txt": getattr(service, "txt", {}),
        "tls_probe": getattr(service, "tls_probe", None),
    }


def _combined_text(*values: Any) -> str:
    return " ".join(str(value) for value in values if value).lower()


def _classify_ship_service(service_payload: dict[str, Any]) -> tuple[str, float, str]:
    txt = service_payload.get("txt") if isinstance(service_payload.get("txt"), dict) else {}
    haystack = _combined_text(
        service_payload.get("service_name"),
        service_payload.get("brand"),
        service_payload.get("model"),
        service_payload.get("device_type"),
        " ".join(f"{key}={value}" for key, value in txt.items()),
    )
    rules = [
        ("wallbox", ("evse", "wallbox", "charger", "charging", "mennekes"), 0.82),
        ("pv_inverter", ("pv", "solar", "inverter", "photovoltaic"), 0.8),
        ("battery", ("battery", "storage", "ess"), 0.8),
        ("heat_pump", ("heat pump", "heatpump", "heating", "compressor"), 0.78),
        ("grid_meter", ("cls", "smgw", "gcph", "gateway", "grid", "meter", "grid connection point"), 0.78),
    ]
    for device_type, tokens, confidence in rules:
        matched = [token for token in tokens if token in haystack]
        if matched:
            return (
                device_type,
                confidence,
                f"EEBus SHIP advertisement matched {', '.join(matched[:3])} for the {device_type} profile.",
            )
    return (
        "unclassified_energy_device",
        0.72,
        "EEBus SHIP advertisement was found, but no stronger appliance profile matched yet.",
    )


def _asset_name_for_type(device_type: str) -> str:
    mapping = {
        "pv_inverter": "PV Generation",
        "battery": "Battery Buffer",
        "grid_meter": "Grid Metering",
        "wallbox": "EV Charging",
        "heat_pump": "Thermal Control",
    }
    return mapping.get(device_type, "EEBus Interface")


def build_candidate_from_ship_service(service: Any) -> RawCandidate:
    payload = _service_to_dict(service)
    service_name = str(payload.get("service_name") or "EEBus SHIP peer")
    ship_id = str(payload.get("ship_id") or "")
    ski = str(payload.get("ski") or "")
    target = str(payload.get("target") or "")
    addresses = payload.get("addresses") if isinstance(payload.get("addresses"), dict) else {}
    ipv4_addresses = [str(value) for value in addresses.get("ipv4", [])] if isinstance(addresses, dict) else []
    host = ipv4_addresses[0] if ipv4_addresses else target
    stable_identifier = ship_id or ski or service_name or host
    device_type, confidence, reasoning = _classify_ship_service(payload)

    identity_keys = {
        f"service-name:{_slugify(service_name)}",
    }
    if ship_id:
        identity_keys.add(f"eebus-ship-id:{_slugify(ship_id)}")
    if ski:
        identity_keys.add(f"eebus-ski:{_slugify(ski)}")
    if host:
        identity_keys.add(f"network-host:{_slugify(host)}")

    display_name = str(payload.get("model") or payload.get("brand") or service_name)
    port = payload.get("port")
    telemetry = {
        "eebus_ship_advertised": True,
        **({"ship_port": int(port)} if isinstance(port, int) else {}),
    }
    return RawCandidate(
        candidate_id=f"cand-eebus-{_slugify(stable_identifier)}",
        device_id=f"dev-eebus-{_slugify(stable_identifier)}",
        asset_id=f"asset-eebus-{_slugify(stable_identifier)}",
        asset_name=_asset_name_for_type(device_type),
        display_name=display_name,
        manufacturer=str(payload.get("brand") or "EEBus"),
        model=str(payload.get("model") or payload.get("device_type") or "SHIP peer"),
        firmware="unknown",
        device_type=device_type,
        discovery_sources=[EEBUS_SOURCE_NAME],
        protocols=[EEBUS_PROTOCOL],
        telemetry=telemetry,
        evidence={
            "ship_service": payload,
            "identity_keys": sorted(identity_keys),
            "classification_reasoning": reasoning,
            "classification_confidence": confidence,
            "supported_use_cases": [LPC_USE_CASE, LPP_USE_CASE],
        },
        recovery_zone=RecoveryZone.HUMAN_GATED.value,
        issue_code=None,
        capabilities_hint={
            "visible": True,
            "monitorable": False,
            "controllable": False,
            "optimizable": False,
        },
    )


def discover_eebus_site(
    *,
    interface_ip: str | None,
    timeout_seconds: float = 3.0,
    tls_check: bool = False,
) -> EebusDiscoveryBatch:
    try:
        sdk = _load_sdk()
        services = sdk.discover_ship_services(
            interface_ip or None,
            timeout=timeout_seconds,
            tls_check=tls_check,
        )
    except Exception as exc:
        return EebusDiscoveryBatch(
            source_name=EEBUS_SOURCE_NAME,
            status="failed",
            message=f"EEBus SHIP discovery failed: {exc}",
            candidates=[],
        )

    candidates = [build_candidate_from_ship_service(service) for service in services]
    if not candidates:
        return EebusDiscoveryBatch(
            source_name=EEBUS_SOURCE_NAME,
            status="completed",
            message="EEBus SHIP discovery completed, but no _ship._tcp.local services were found.",
            candidates=[],
        )

    return EebusDiscoveryBatch(
        source_name=EEBUS_SOURCE_NAME,
        status="completed",
        message=f"Imported {len(candidates)} EEBus SHIP peer candidate(s).",
        candidates=sorted(candidates, key=lambda candidate: candidate.display_name.lower()),
    )


def list_eebus_ship_services(
    *,
    interface_ip: str | None,
    timeout_seconds: float = 3.0,
    tls_check: bool = False,
) -> list[dict[str, Any]]:
    sdk = _load_sdk()
    services = sdk.discover_ship_services(
        interface_ip or None,
        timeout=timeout_seconds,
        tls_check=tls_check,
    )
    return [_service_to_dict(service) for service in services]


def _normalize_use_case(*, use_case: str | None, limit_id: int | None) -> tuple[str, int, str, str]:
    normalized = (use_case or "").strip().lower().replace("-", "_")
    if limit_id == 0 or normalized in {
        "lpc",
        "consume",
        "consumption",
        "limitation_of_power_consumption",
        LPC_USE_CASE.lower(),
    }:
        return LPC_USE_CASE, 0, "consume", "grid_import_limit_kw"
    if limit_id == 1 or normalized in {
        "lpp",
        "produce",
        "production",
        "limitation_of_power_production",
        LPP_USE_CASE.lower(),
    }:
        return LPP_USE_CASE, 1, "produce", "grid_export_limit_kw"
    raise ValueError("EEBus load-power limit must identify LPC/consumption or LPP/production.")


def build_load_power_limit_payload(command: EebusLoadPowerLimitCreate) -> dict[str, Any]:
    _, limit_id, _, _ = _normalize_use_case(use_case=command.use_case, limit_id=command.limit_id)
    limit_watts = command.limit_watts
    if limit_watts is None:
        raise ValueError("EEBus load-power limit requires limit_watts.")
    return _load_sdk().build_limit_payload(
        watts=int(limit_watts),
        duration_seconds=command.duration_seconds,
        limit_id=limit_id,
        is_active=command.is_active,
    )


def distribute_load_power_limit(
    session: Session,
    command: EebusLoadPowerLimitCreate,
) -> EebusLoadPowerLimitDistributionRead:
    use_case, limit_id, direction, _policy_field = _normalize_use_case(
        use_case=command.use_case,
        limit_id=command.limit_id,
    )
    if command.limit_watts is None:
        raise ValueError("EEBus load-power limit requires limit_watts.")
    if command.limit_watts < 0:
        raise ValueError("EEBus load-power limit must be provided as a positive watt value.")

    eebus_payload = build_load_power_limit_payload(command)
    policy = get_or_create_hems_policy(session)
    previous_effective_limits = effective_grid_limits(session, policy)
    previous_import_limit = previous_effective_limits["grid_import_limit_kw"]
    previous_export_limit = previous_effective_limits["grid_export_limit_kw"]

    limit = record_load_control_limit(
        session,
        site_id=policy.site_id,
        use_case=use_case,
        limit_id=limit_id,
        direction=direction,
        source=command.source or "eebus",
        peer_ski=command.peer_ski,
        limit_watts=command.limit_watts,
        duration_seconds=command.duration_seconds,
        is_active=command.is_active,
        raw=command.raw,
    )
    applied_effective_limits = effective_grid_limits(session, policy)
    changed_effective_limits = {
        field: value
        for field, value in applied_effective_limits.items()
        if previous_effective_limits.get(field) != value
    }
    active_constraints = [
        {
            "id": row.id,
            "use_case": row.use_case,
            "limit_id": row.limit_id,
            "direction": row.direction,
            "source": row.source,
            "peer_ski": row.peer_ski,
            "limit_watts": row.limit_watts,
            "duration_seconds": row.duration_seconds,
            "received_at": row.received_at.isoformat(),
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }
        for row in active_load_control_limits(session, site_id=policy.site_id)
    ]
    constraint_distribution = build_constraint_distribution(
        session,
        site_id=policy.site_id,
        use_case=use_case,
        limit_watts=command.limit_watts,
    )
    constraint_distribution.update(
        {
            "constraint_id": limit.id,
            "limit_id": limit_id,
            "duration_seconds": command.duration_seconds,
            "is_active": command.is_active,
            "source_peer_ski": command.peer_ski or "",
        }
    )
    deliveries = create_load_control_deliveries(session, limit=limit, distribution=constraint_distribution)
    attach_delivery_state_to_distribution(constraint_distribution, deliveries)

    plan = None
    if command.is_active or changed_effective_limits:
        from app.hems.service import run_hems_replan

        plan = run_hems_replan(session, triggered_by=f"eebus_{'lpc' if limit_id == 0 else 'lpp'}")
        update_native_delivery_statuses_from_plan(session, deliveries, plan)
        attach_delivery_state_to_distribution(constraint_distribution, deliveries)

    session.add(
        AuditEvent(
            actor=command.source or "eebus",
            action="distribute_eebus_load_power_limit",
            target_type="hems_load_control",
            target_id=str(policy.site_id),
            summary=(
                f"Recorded active EEBus {'LPC' if limit_id == 0 else 'LPP'} limit of {command.limit_watts} W."
                if command.is_active
                else f"Recorded inactive EEBus {'LPC' if limit_id == 0 else 'LPP'} limit."
            ),
            details={
                "use_case": use_case,
                "limit_id": limit_id,
                "direction": direction,
                "limit_watts": command.limit_watts,
                "duration_seconds": command.duration_seconds,
                "is_active": command.is_active,
                "peer_ski": command.peer_ski,
                "changed_effective_limits": changed_effective_limits,
                "active_constraints": active_constraints,
                "constraint_distribution": constraint_distribution,
                "raw": command.raw,
            },
            created_at=utcnow(),
        )
    )
    session.commit()

    return EebusLoadPowerLimitDistributionRead(
        use_case=use_case,
        limit_id=limit_id,
        direction=direction,
        is_active=command.is_active,
        limit_watts=command.limit_watts,
        duration_seconds=command.duration_seconds,
        previous_grid_import_limit_kw=previous_import_limit,
        previous_grid_export_limit_kw=previous_export_limit,
        applied_grid_import_limit_kw=applied_effective_limits["grid_import_limit_kw"],
        applied_grid_export_limit_kw=applied_effective_limits["grid_export_limit_kw"],
        changed_policy_fields={},
        changed_effective_limits=changed_effective_limits,
        active_constraints=active_constraints,
        constraint_distribution=constraint_distribution,
        eebus_payload=eebus_payload,
        plan=(
            HemsPlanHeaderRead(
                id=plan.id,
                status=plan.status,
                execution_mode=plan.execution_mode,
                triggered_by=plan.triggered_by,
                solver_name=plan.solver_name,
                objective_value=plan.objective_value,
                summary=plan.summary,
                horizon_start=plan.horizon_start,
                horizon_end=plan.horizon_end,
                created_at=plan.created_at,
                finished_at=plan.finished_at,
            )
            if plan is not None
            else None
        ),
        message=(
            f"Recorded EEBus {use_case} as an active {direction} constraint and replanned HEMS dispatch."
            if command.is_active
            else f"Recorded inactive EEBus {use_case}; HEMS base policy was left unchanged."
        ),
    )
