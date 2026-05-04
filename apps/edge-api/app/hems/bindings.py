from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, Device, HemsSystemBinding, Site, utcnow


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


def asset_id_for_device(session: Session, device_id: str | None) -> str | None:
    if not device_id:
        return None
    for candidate in session.scalars(select(Asset).order_by(Asset.updated_at.desc())).all():
        if device_id in (candidate.device_ids or []):
            return candidate.id
    return None


def _device_statuses(device: Device | None) -> tuple[str, str, str]:
    if device is None:
        return "missing_device", "unknown", "unknown"

    capabilities = device.capabilities or {}
    if bool(capabilities.get("monitorable")):
        telemetry_status = "validated"
    elif bool(capabilities.get("visible")):
        telemetry_status = "missing"
    else:
        telemetry_status = "unknown"

    if bool(capabilities.get("controllable")):
        control_status = "validated"
    elif bool(capabilities.get("visible")):
        control_status = "missing"
    else:
        control_status = "unknown"

    if device.primary_status in {"monitorable", "controllable", "optimizable", "connected"}:
        connection_status = "connected"
    elif bool(capabilities.get("visible")):
        connection_status = "visible"
    else:
        connection_status = "unknown"

    return connection_status, telemetry_status, control_status


def upsert_system_binding(
    session: Session,
    *,
    system_type: str,
    label: str,
    device_id: str | None,
    asset_id: str | None = None,
    status: str = "confirmed",
    source: str = "agent",
    confidence: float = 0.0,
    evidence: dict | None = None,
) -> HemsSystemBinding:
    site = _get_site(session)
    normalized_type = system_type.strip()
    normalized_label = label.strip() or normalized_type.replace("_", " ").title()
    resolved_asset_id = asset_id or asset_id_for_device(session, device_id)
    device = session.get(Device, device_id) if device_id else None
    connection_status, telemetry_status, control_status = _device_statuses(device)

    binding = session.scalar(
        select(HemsSystemBinding)
        .where(
            HemsSystemBinding.site_id == site.id,
            HemsSystemBinding.system_type == normalized_type,
        )
        .limit(1)
    )
    if binding is None:
        binding = HemsSystemBinding(
            id=f"hems-binding-{uuid4().hex[:12]}",
            site_id=site.id,
            system_type=normalized_type,
        )
        session.add(binding)

    binding.label = normalized_label
    binding.device_id = device_id
    binding.asset_id = resolved_asset_id
    binding.status = status
    binding.connection_status = connection_status
    binding.telemetry_status = telemetry_status
    binding.control_status = control_status
    binding.source = source
    binding.confidence = confidence
    binding.evidence = evidence or {}
    binding.updated_at = utcnow()
    return binding


def update_binding_label(session: Session, *, binding_id: str, label: str) -> HemsSystemBinding:
    binding = session.get(HemsSystemBinding, binding_id)
    if binding is None:
        raise KeyError(binding_id)
    binding.label = label.strip() or binding.label
    binding.updated_at = utcnow()
    session.add(binding)
    return binding


def list_system_bindings(session: Session, *, confirmed_only: bool = False) -> list[HemsSystemBinding]:
    query = select(HemsSystemBinding).order_by(HemsSystemBinding.system_type, HemsSystemBinding.label)
    if confirmed_only:
        query = query.where(HemsSystemBinding.status == "confirmed")
    return list(session.scalars(query).all())


def binding_lookup(session: Session) -> tuple[dict[str, HemsSystemBinding], dict[str, HemsSystemBinding]]:
    by_asset_id: dict[str, HemsSystemBinding] = {}
    by_device_id: dict[str, HemsSystemBinding] = {}
    for binding in list_system_bindings(session, confirmed_only=True):
        if binding.asset_id:
            by_asset_id[binding.asset_id] = binding
        if binding.device_id:
            by_device_id[binding.device_id] = binding
    return by_asset_id, by_device_id
