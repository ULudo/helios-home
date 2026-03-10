from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import AuditEvent, Device, Recommendation, Site, utcnow
from app.domain.schemas import CapabilityRead, ConnectorAttemptRead, DeviceRead, OverviewResponse, RecommendationRead, SiteRead


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


def _serialize_site(site: Site) -> SiteRead:
    return SiteRead(
        id=site.id,
        local_subnet=site.local_subnet,
        updated_at=site.updated_at,
    )


def _serialize_recommendation(recommendation: Recommendation) -> RecommendationRead:
    return RecommendationRead(
        id=recommendation.id,
        title=recommendation.title,
        description=recommendation.description,
        priority=recommendation.priority,
        action_type=recommendation.action_type,
        zone=recommendation.zone,
        auto_applicable=recommendation.auto_applicable,
        created_at=recommendation.created_at,
    )


def _serialize_device(device: Device) -> DeviceRead:
    connector_attempts = [
        ConnectorAttemptRead(
            id=attempt.id,
            connector_name=attempt.connector_name,
            protocol=attempt.protocol,
            outcome=attempt.outcome,
            detail=attempt.detail,
            attempted_at=attempt.attempted_at,
        )
        for attempt in device.connector_attempts
    ]
    recommendations = [_serialize_recommendation(recommendation) for recommendation in device.recommendations]
    capabilities = CapabilityRead(
        visible=bool(device.capabilities.get("visible")),
        monitorable=bool(device.capabilities.get("monitorable")),
        controllable=bool(device.capabilities.get("controllable")),
        optimizable=bool(device.capabilities.get("optimizable")),
    )
    return DeviceRead(
        id=device.id,
        name=device.name,
        manufacturer=device.manufacturer,
        model=device.model,
        firmware=device.firmware,
        device_type=device.device_type,
        primary_status=device.primary_status,
        status_tags=device.status_tags or [],
        confidence=device.confidence,
        recovery_zone=device.recovery_zone,
        protocols=device.protocols or [],
        capabilities=capabilities,
        telemetry=device.telemetry or {},
        problem_summary=device.problem_summary,
        explanation=device.explanation,
        next_step=device.next_step,
        last_seen_at=device.last_seen_at,
        connector_attempts=connector_attempts,
        recommendations=recommendations,
    )


def _load_devices(session: Session) -> list[Device]:
    return session.scalars(
        select(Device)
        .options(
            selectinload(Device.connector_attempts),
            selectinload(Device.recommendations),
        )
        .order_by(Device.device_type, Device.name)
    ).all()


def get_device(session: Session, device_id: str) -> DeviceRead | None:
    device = session.scalar(
        select(Device)
        .where(Device.id == device_id)
        .options(
            selectinload(Device.connector_attempts),
            selectinload(Device.recommendations),
        )
    )
    if device is None:
        return None
    return _serialize_device(device)


def update_site(session: Session, updates: dict[str, str]) -> SiteRead:
    site = _get_site(session)
    changed_fields: dict[str, str] = {}
    for field, value in updates.items():
        if value is None:
            continue
        setattr(site, field, value)
        changed_fields[field] = value
    if changed_fields:
        session.add(
            AuditEvent(
                actor="user",
                action="update_site_configuration",
                target_type="site",
                target_id=str(site.id),
                summary="Updated local site configuration.",
                details=changed_fields,
                created_at=utcnow(),
            )
        )
    session.add(site)
    session.commit()
    session.refresh(site)
    return _serialize_site(site)


def build_overview(session: Session) -> OverviewResponse:
    site = _get_site(session)
    devices = [_serialize_device(device) for device in _load_devices(session)]
    return OverviewResponse(
        site=_serialize_site(site),
        devices=devices,
    )
