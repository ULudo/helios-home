from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditEvent,
    Device,
    HemsLoadControlDeviceConfig,
    HemsLoadControlLimit,
    HemsPolicy,
    Site,
    utcnow,
)
from app.hems.schemas import HemsLoadControlDeviceConfigRead, HemsLoadControlDeviceConfigUpdate


LPC_USE_CASE = "limitationOfPowerConsumption"
LPP_USE_CASE = "limitationOfPowerProduction"


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


def _clamped_share(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(100.0, round(float(value), 3)))


def _serialize_config(config: HemsLoadControlDeviceConfig | None, device_id: str) -> HemsLoadControlDeviceConfigRead:
    if config is None:
        return HemsLoadControlDeviceConfigRead(device_id=device_id)
    return HemsLoadControlDeviceConfigRead(
        device_id=device_id,
        receives_lpc=config.receives_lpc,
        receives_lpp=config.receives_lpp,
        participates_lpc=config.participates_lpc,
        participates_lpp=config.participates_lpp,
        lpc_share_pct=config.lpc_share_pct,
        lpp_share_pct=config.lpp_share_pct,
        updated_at=config.updated_at,
    )


def get_load_control_config(session: Session, device_id: str) -> HemsLoadControlDeviceConfigRead:
    config = session.get(HemsLoadControlDeviceConfig, device_id)
    return _serialize_config(config, device_id)


def update_load_control_config(
    session: Session,
    payload: HemsLoadControlDeviceConfigUpdate,
    *,
    actor: str = "user",
) -> HemsLoadControlDeviceConfigRead:
    site = _get_site(session)
    device = session.get(Device, payload.device_id)
    if device is None or device.site_id != site.id:
        raise ValueError(f"Unknown device: {payload.device_id}")
    config = session.get(HemsLoadControlDeviceConfig, device.id)
    if config is None:
        config = HemsLoadControlDeviceConfig(device_id=device.id, site_id=site.id)
    updates = payload.model_dump(exclude_none=True)
    updates.pop("device_id", None)
    reason = str(updates.pop("reason", "") or "")
    for field, value in updates.items():
        if field in {"lpc_share_pct", "lpp_share_pct"}:
            value = _clamped_share(value)
        setattr(config, field, value)
    if config.participates_lpc and config.lpc_share_pct <= 0:
        config.lpc_share_pct = 100.0
    if config.participates_lpp and config.lpp_share_pct <= 0:
        config.lpp_share_pct = 100.0
    config.updated_at = utcnow()
    session.add(config)
    session.add(
        AuditEvent(
            actor=actor,
            action="configure_hems_load_control",
            target_type="device",
            target_id=device.id,
            summary="Updated HEMS load-control configuration.",
            details={
                "device_id": device.id,
                "device_name": device.name,
                "updates": updates,
                "reason": reason,
            },
            created_at=utcnow(),
        )
    )
    session.commit()
    session.refresh(config)
    return _serialize_config(config, device.id)


def record_load_control_limit(
    session: Session,
    *,
    site_id: int,
    use_case: str,
    limit_id: int,
    direction: str,
    source: str,
    peer_ski: str | None,
    limit_watts: int,
    duration_seconds: int | None,
    is_active: bool,
    raw: dict[str, Any],
) -> HemsLoadControlLimit:
    now = utcnow()
    normalized_peer_ski = (peer_ski or "").strip().lower()
    if not is_active:
        statement = select(HemsLoadControlLimit).where(
            HemsLoadControlLimit.site_id == site_id,
            HemsLoadControlLimit.use_case == use_case,
            HemsLoadControlLimit.limit_id == limit_id,
            HemsLoadControlLimit.peer_ski == normalized_peer_ski,
            HemsLoadControlLimit.is_active.is_(True),
        )
        for row in session.scalars(statement).all():
            row.is_active = False
            session.add(row)
    limit = HemsLoadControlLimit(
        id=f"load-control-limit-{uuid4().hex[:12]}",
        site_id=site_id,
        use_case=use_case,
        limit_id=limit_id,
        direction=direction,
        source=source,
        peer_ski=normalized_peer_ski,
        limit_watts=limit_watts,
        duration_seconds=duration_seconds,
        is_active=is_active,
        raw=raw,
        received_at=now,
        expires_at=now + timedelta(seconds=duration_seconds) if is_active and duration_seconds else None,
    )
    session.add(limit)
    session.flush()
    return limit


def active_load_control_limits(session: Session, *, site_id: int, now=None) -> list[HemsLoadControlLimit]:
    current_time = now or utcnow()
    rows = session.scalars(
        select(HemsLoadControlLimit)
        .where(
            HemsLoadControlLimit.site_id == site_id,
            HemsLoadControlLimit.is_active.is_(True),
        )
        .order_by(HemsLoadControlLimit.received_at.desc())
    ).all()
    active: list[HemsLoadControlLimit] = []
    for row in rows:
        expires_at = row.expires_at
        compare_time = current_time
        if expires_at is not None and expires_at.tzinfo is None and compare_time.tzinfo is not None:
            compare_time = compare_time.replace(tzinfo=None)
        if expires_at is not None and expires_at <= compare_time:
            row.is_active = False
            session.add(row)
            continue
        active.append(row)
    return active


def effective_grid_limits(session: Session, policy: HemsPolicy, *, now=None) -> dict[str, float]:
    limits = {
        "grid_import_limit_kw": float(policy.grid_import_limit_kw),
        "grid_export_limit_kw": float(policy.grid_export_limit_kw),
    }
    for limit in active_load_control_limits(session, site_id=policy.site_id, now=now):
        limit_kw = round(limit.limit_watts / 1000.0, 4)
        if limit.use_case == LPC_USE_CASE:
            limits["grid_import_limit_kw"] = min(limits["grid_import_limit_kw"], limit_kw)
        elif limit.use_case == LPP_USE_CASE:
            limits["grid_export_limit_kw"] = min(limits["grid_export_limit_kw"], limit_kw)
    return limits


def build_constraint_distribution(
    session: Session,
    *,
    site_id: int,
    use_case: str,
    limit_watts: int,
) -> dict[str, Any]:
    share_field = "lpc_share_pct" if use_case == LPC_USE_CASE else "lpp_share_pct"
    participate_field = "participates_lpc" if use_case == LPC_USE_CASE else "participates_lpp"
    configs = session.scalars(
        select(HemsLoadControlDeviceConfig).where(
            HemsLoadControlDeviceConfig.site_id == site_id,
            getattr(HemsLoadControlDeviceConfig, participate_field).is_(True),
        )
    ).all()
    rows: list[dict[str, Any]] = []
    total_share = sum(max(0.0, float(getattr(config, share_field) or 0.0)) for config in configs)
    for config in configs:
        device = session.get(Device, config.device_id)
        share = max(0.0, float(getattr(config, share_field) or 0.0))
        normalized = share / total_share if total_share > 0 else 0.0
        capabilities = device.capabilities if device is not None else {}
        control_available = bool(capabilities.get("controllable")) if isinstance(capabilities, dict) else False
        rows.append(
            {
                "device_id": config.device_id,
                "device_name": device.name if device is not None else config.device_id,
                "share_pct": share,
                "normalized_share": round(normalized, 6),
                "allocated_limit_watts": int(round(limit_watts * normalized)) if normalized else 0,
                "control_available": control_available,
                "status": "dispatchable" if control_available else "configured_no_control_path",
            }
        )
    return {
        "use_case": use_case,
        "limit_watts": limit_watts,
        "participant_count": len(rows),
        "total_share_pct": round(total_share, 3),
        "enforceable": any(row["control_available"] for row in rows),
        "participants": rows,
    }
