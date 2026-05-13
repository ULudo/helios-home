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
    HemsLoadControlDelivery,
    HemsLoadControlLimit,
    HemsPolicy,
    ProtocolEndpoint,
    Site,
    utcnow,
)
from app.domain.schemas import LoadControlConstraintRead, LoadControlParticipantRead, OverviewLoadControlRead
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
    _touch_load_control_devices(session, site_id=site_id, use_case=use_case, now=now)
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


def create_load_control_deliveries(
    session: Session,
    *,
    limit: HemsLoadControlLimit,
    distribution: dict[str, Any],
) -> list[HemsLoadControlDelivery]:
    deliveries: list[HemsLoadControlDelivery] = []
    for participant in distribution.get("participants", []):
        if not isinstance(participant, dict):
            continue
        device_id = str(participant.get("device_id") or "")
        if not device_id:
            continue
        allocated_limit_watts = int(participant.get("allocated_limit_watts") or 0)
        target_endpoint_ref = str(participant.get("target_endpoint_ref") or "")
        target_peer_ski = str(participant.get("target_peer_ski") or "").strip().lower()
        if allocated_limit_watts <= 0:
            status = "skipped_zero_allocation"
            detail = "No power was allocated to this participant."
        elif target_endpoint_ref and target_peer_ski:
            status = "pending"
            detail = "Allocated and waiting for EEBUS delivery."
        else:
            status = "not_deliverable"
            detail = "Allocated locally, but no EEBUS control endpoint is configured for this participant."
        delivery = HemsLoadControlDelivery(
            id=f"load-control-delivery-{uuid4().hex[:12]}",
            site_id=limit.site_id,
            constraint_id=limit.id,
            source_peer_ski=limit.peer_ski,
            target_device_id=device_id,
            target_endpoint_ref=target_endpoint_ref,
            target_peer_ski=target_peer_ski,
            use_case=limit.use_case,
            limit_id=limit.limit_id,
            limit_watts=limit.limit_watts,
            allocated_limit_watts=allocated_limit_watts,
            duration_seconds=limit.duration_seconds,
            is_active=limit.is_active,
            status=status,
            detail=detail,
            raw={
                "participant": participant,
                "constraint": {
                    "id": limit.id,
                    "use_case": limit.use_case,
                    "limit_id": limit.limit_id,
                    "limit_watts": limit.limit_watts,
                    "duration_seconds": limit.duration_seconds,
                    "is_active": limit.is_active,
                },
            },
            requested_at=limit.received_at,
            updated_at=limit.received_at,
        )
        session.add(delivery)
        deliveries.append(delivery)
    session.flush()
    return deliveries


def update_load_control_delivery_status(
    session: Session,
    delivery_id: str,
    *,
    status: str,
    detail: str = "",
    error: str = "",
    raw_update: dict[str, Any] | None = None,
) -> HemsLoadControlDelivery | None:
    delivery = session.get(HemsLoadControlDelivery, delivery_id)
    if delivery is None:
        return None
    now = utcnow()
    delivery.status = status
    if detail:
        delivery.detail = detail
    if error:
        delivery.last_error = error
    delivery.updated_at = now
    if status in {"sent", "acknowledged", "readback_confirmed"} and delivery.sent_at is None:
        delivery.sent_at = now
    if status in {"acknowledged", "readback_confirmed"} and delivery.acknowledged_at is None:
        delivery.acknowledged_at = now
    if status == "readback_confirmed":
        delivery.readback_at = now
    raw = dict(delivery.raw or {})
    updates = list(raw.get("updates") or [])
    updates.append({"status": status, "detail": detail, "error": error, "raw": raw_update or {}, "at": now.isoformat()})
    raw["updates"] = updates[-20:]
    delivery.raw = raw
    session.add(delivery)
    session.flush()
    return delivery


def load_control_deliveries_for_constraint(session: Session, constraint_id: str) -> list[HemsLoadControlDelivery]:
    return session.scalars(
        select(HemsLoadControlDelivery)
        .where(HemsLoadControlDelivery.constraint_id == constraint_id)
        .order_by(HemsLoadControlDelivery.requested_at.asc())
    ).all()


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
    seen_keys: set[tuple[str, int, str]] = set()
    for row in rows:
        expires_at = row.expires_at
        compare_time = current_time
        if expires_at is not None and expires_at.tzinfo is None and compare_time.tzinfo is not None:
            compare_time = compare_time.replace(tzinfo=None)
        if expires_at is not None and expires_at <= compare_time:
            row.is_active = False
            session.add(row)
            continue
        key = (row.use_case, row.limit_id, row.peer_ski)
        if key in seen_keys:
            row.is_active = False
            session.add(row)
            continue
        seen_keys.add(key)
        active.append(row)
    return active


def _touch_load_control_devices(session: Session, *, site_id: int, use_case: str, now) -> None:
    receiver_field = "receives_lpc" if use_case == LPC_USE_CASE else "receives_lpp"
    participant_field = "participates_lpc" if use_case == LPC_USE_CASE else "participates_lpp"
    configs = session.scalars(
        select(HemsLoadControlDeviceConfig).where(
            HemsLoadControlDeviceConfig.site_id == site_id,
            (
                getattr(HemsLoadControlDeviceConfig, receiver_field).is_(True)
                | getattr(HemsLoadControlDeviceConfig, participant_field).is_(True)
            ),
        )
    ).all()
    for config in configs:
        device = session.get(Device, config.device_id)
        if device is None:
            continue
        device.last_seen_at = now
        session.add(device)


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
        native_control_available = bool(capabilities.get("controllable")) if isinstance(capabilities, dict) else False
        eebus_endpoint = _load_control_eebus_endpoint(session, config.device_id)
        target_peer_ski = _endpoint_peer_ski(eebus_endpoint)
        eebus_control_available = eebus_endpoint is not None and bool(target_peer_ski)
        control_available = native_control_available or eebus_control_available
        control_path = "eebus_spine" if eebus_control_available else "native" if native_control_available else ""
        rows.append(
            {
                "device_id": config.device_id,
                "device_name": device.name if device is not None else config.device_id,
                "share_pct": share,
                "normalized_share": round(normalized, 6),
                "allocated_limit_watts": int(round(limit_watts * normalized)) if normalized else 0,
                "control_available": control_available,
                "control_path": control_path,
                "target_endpoint_ref": eebus_endpoint.id if eebus_endpoint is not None else "",
                "target_peer_ski": target_peer_ski,
                "status": "delivery_ready" if eebus_control_available else "dispatchable" if native_control_available else "configured_no_control_path",
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


def _load_control_eebus_endpoint(session: Session, device_id: str) -> ProtocolEndpoint | None:
    return session.scalar(
        select(ProtocolEndpoint)
        .where(
            ProtocolEndpoint.owner_ref == f"device:{device_id}",
            ProtocolEndpoint.protocol == "eebus_ship",
            ProtocolEndpoint.status != "disconnected",
        )
        .order_by(ProtocolEndpoint.updated_at.desc())
        .limit(1)
    )


def _endpoint_peer_ski(endpoint: ProtocolEndpoint | None) -> str:
    if endpoint is None:
        return ""
    properties = endpoint.properties if isinstance(endpoint.properties, dict) else {}
    return str(properties.get("peer_certificate_ski") or properties.get("ski") or "").strip().lower()


def build_load_control_overview(session: Session, *, site_id: int) -> OverviewLoadControlRead:
    constraints: list[LoadControlConstraintRead] = []
    for limit in active_load_control_limits(session, site_id=site_id):
        receiver_field = "receives_lpc" if limit.use_case == LPC_USE_CASE else "receives_lpp"
        receiver_device_ids = [
            config.device_id
            for config in session.scalars(
                select(HemsLoadControlDeviceConfig).where(
                    HemsLoadControlDeviceConfig.site_id == site_id,
                    getattr(HemsLoadControlDeviceConfig, receiver_field).is_(True),
                )
            ).all()
        ]
        distribution = build_constraint_distribution(
            session,
            site_id=site_id,
            use_case=limit.use_case,
            limit_watts=limit.limit_watts,
        )
        deliveries = {
            delivery.target_device_id: delivery
            for delivery in load_control_deliveries_for_constraint(session, limit.id)
        }
        constraints.append(
            LoadControlConstraintRead(
                id=limit.id,
                use_case=limit.use_case,
                direction=limit.direction,
                source=limit.source,
                peer_ski=limit.peer_ski,
                limit_watts=limit.limit_watts,
                duration_seconds=limit.duration_seconds,
                received_at=limit.received_at,
                expires_at=limit.expires_at,
                receiver_device_ids=receiver_device_ids,
                participants=[
                    LoadControlParticipantRead(
                        device_id=str(row.get("device_id") or ""),
                        device_name=str(row.get("device_name") or ""),
                        share_pct=float(row.get("share_pct") or 0.0),
                        normalized_share=float(row.get("normalized_share") or 0.0),
                        allocated_limit_watts=int(row.get("allocated_limit_watts") or 0),
                        control_available=bool(row.get("control_available")),
                        status=str(row.get("status") or ""),
                        control_path=str(row.get("control_path") or ""),
                        target_endpoint_ref=str(row.get("target_endpoint_ref") or ""),
                        target_peer_ski=str(row.get("target_peer_ski") or ""),
                        delivery_id=deliveries.get(str(row.get("device_id") or "")).id
                        if deliveries.get(str(row.get("device_id") or ""))
                        else "",
                        delivery_status=deliveries.get(str(row.get("device_id") or "")).status
                        if deliveries.get(str(row.get("device_id") or ""))
                        else "",
                        delivery_detail=deliveries.get(str(row.get("device_id") or "")).detail
                        if deliveries.get(str(row.get("device_id") or ""))
                        else "",
                        delivery_updated_at=deliveries.get(str(row.get("device_id") or "")).updated_at
                        if deliveries.get(str(row.get("device_id") or ""))
                        else None,
                    )
                    for row in distribution.get("participants", [])
                    if isinstance(row, dict)
                ],
            )
        )
    return OverviewLoadControlRead(active_constraints=constraints)
