from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    AgentTask,
    Asset,
    AuditEvent,
    Blocker,
    DebugCase,
    Device,
    DeviceAssessment,
    DeviceCandidate,
    HemsLoadControlDeviceConfig,
    HemsSystemBinding,
    HomeGraphEntity,
    HomeGraphEvidence,
    Proposal,
    ProtocolDiagnosticRun,
    ProtocolEndpoint,
    Site,
    TaskStep,
    UserDecisionRequest,
    utcnow,
)
from app.domain.schemas import (
    CapabilityRead,
    ConnectorAttemptRead,
    DeviceLoadControlRead,
    DeviceRead,
    OverviewResponse,
    SiteRead,
)
from app.hems.load_control import get_load_control_config
from app.home_graph.service import connection_facets_for_entity


_DEVICE_CONNECTION_STATES = {"connected", "ship_ready"}
_BASE_VISIBLE_STATUSES = {"", "discovered", "visible_only", "endpoint_visible", "partially_ready"}


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


def _serialize_device(session: Session, device: Device) -> DeviceRead:
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
    capabilities = CapabilityRead(
        visible=bool(device.capabilities.get("visible")),
        monitorable=bool(device.capabilities.get("monitorable")),
        controllable=bool(device.capabilities.get("controllable")),
        optimizable=bool(device.capabilities.get("optimizable")),
    )
    load_control_config = get_load_control_config(session, device.id)
    load_control = DeviceLoadControlRead(
        receives_lpc=load_control_config.receives_lpc,
        receives_lpp=load_control_config.receives_lpp,
        participates_lpc=load_control_config.participates_lpc,
        participates_lpp=load_control_config.participates_lpp,
        lpc_share_pct=load_control_config.lpc_share_pct,
        lpp_share_pct=load_control_config.lpp_share_pct,
    )
    primary_status = device.primary_status
    status_tags = list(device.status_tags or [])
    connection_facets = _connection_facets_for_device(session, device)
    connection_state = str(connection_facets.get("overall_connection_state") or "")
    if connection_state in _DEVICE_CONNECTION_STATES:
        primary_status = "connected" if primary_status in _BASE_VISIBLE_STATUSES else primary_status
        status_tags = _append_unique(status_tags, "connected")
        if connection_state == "ship_ready":
            status_tags = _append_unique(status_tags, "eebus_ship_ready")
    return DeviceRead(
        id=device.id,
        name=device.name,
        manufacturer=device.manufacturer,
        model=device.model,
        firmware=device.firmware,
        device_type=device.device_type,
        primary_status=primary_status,
        status_tags=status_tags,
        confidence=device.confidence,
        recovery_zone=device.recovery_zone,
        protocols=device.protocols or [],
        capabilities=capabilities,
        load_control=load_control,
        telemetry=device.telemetry or {},
        last_seen_at=device.last_seen_at,
        connector_attempts=connector_attempts,
    )


def _load_devices(session: Session) -> list[Device]:
    return session.scalars(
        select(Device)
        .options(
            selectinload(Device.connector_attempts),
        )
        .order_by(Device.device_type, Device.name)
    ).all()


def get_device(session: Session, device_id: str) -> DeviceRead | None:
    device = session.scalar(
        select(Device)
        .where(Device.id == device_id)
        .options(
            selectinload(Device.connector_attempts),
        )
    )
    if device is None:
        return None
    return _serialize_device(session, device)


def remove_device_from_inventory(session: Session, device_id: str, *, actor: str = "user") -> DeviceRead | None:
    device = session.scalar(
        select(Device)
        .where(Device.id == device_id)
        .options(
            selectinload(Device.connector_attempts),
        )
    )
    if device is None:
        return None

    removed_device = _serialize_device(session, device)
    site_id = device.site_id
    now = utcnow()
    device_ref = f"device:{device.id}"
    candidates = session.scalars(
        select(DeviceCandidate).where(
            DeviceCandidate.site_id == site_id,
            DeviceCandidate.matched_device_id == device.id,
        )
    ).all()
    candidate_refs = [f"candidate:{candidate.id}" for candidate in candidates]
    entity_refs = {device_ref, *candidate_refs}

    endpoints = session.scalars(
        select(ProtocolEndpoint).where(
            ProtocolEndpoint.site_id == site_id,
            ProtocolEndpoint.owner_ref.in_(entity_refs),
        )
    ).all()
    endpoint_refs = {endpoint.id for endpoint in endpoints}
    affected_refs = entity_refs | endpoint_refs

    _stop_runtime_if_needed(endpoint_refs)
    _remove_device_assets(session, site_id, device.id)
    _cancel_device_work(session, site_id, affected_refs, now)
    _remove_graph_materialization(session, site_id, device.id, affected_refs)

    for binding in session.scalars(
        select(HemsSystemBinding).where(
            HemsSystemBinding.site_id == site_id,
            HemsSystemBinding.device_id == device.id,
        )
    ):
        session.delete(binding)

    for config in session.scalars(
        select(HemsLoadControlDeviceConfig).where(
            HemsLoadControlDeviceConfig.site_id == site_id,
            HemsLoadControlDeviceConfig.device_id == device.id,
        )
    ):
        session.delete(config)

    for debug_case in session.scalars(
        select(DebugCase).where(
            DebugCase.site_id == site_id,
            DebugCase.matched_device_id == device.id,
        )
    ):
        debug_case.matched_device_id = None
        debug_case.updated_at = now
        session.add(debug_case)

    for candidate in candidates:
        session.delete(candidate)

    session.delete(device)
    session.add(
        AuditEvent(
            actor=actor,
            action="remove_device_from_inventory",
            target_type="device",
            target_id=device.id,
            summary=f"Removed {device.name} from the HEMS inventory.",
            details={
                "device_id": device.id,
                "device_name": device.name,
                "removed_candidate_refs": sorted(candidate_refs),
                "removed_endpoint_refs": sorted(endpoint_refs),
                "rediscovery_required": True,
            },
            created_at=now,
        )
    )
    session.commit()
    return removed_device


def _stop_runtime_if_needed(endpoint_refs: set[str]) -> None:
    if not endpoint_refs:
        return
    from app.services.eebus_runtime import get_eebus_runtime_manager

    runtime = get_eebus_runtime_manager()
    snapshot = runtime.snapshot()
    if endpoint_refs.intersection(snapshot.endpoint_refs):
        runtime.stop(clear_runtime=True)


def _remove_device_assets(session: Session, site_id: int, device_id: str) -> None:
    assets = session.scalars(select(Asset).where(Asset.site_id == site_id)).all()
    for asset in assets:
        if device_id not in (asset.device_ids or []):
            continue
        remaining_ids = [entry for entry in asset.device_ids if entry != device_id]
        if remaining_ids:
            asset.device_ids = remaining_ids
            asset.updated_at = utcnow()
            session.add(asset)
        else:
            session.delete(asset)


def _remove_graph_materialization(session: Session, site_id: int, device_id: str, affected_refs: set[str]) -> None:
    for endpoint in session.scalars(
        select(ProtocolEndpoint).where(
            ProtocolEndpoint.site_id == site_id,
            ProtocolEndpoint.id.in_(affected_refs),
        )
    ):
        session.delete(endpoint)

    for entity in session.scalars(
        select(HomeGraphEntity).where(
            HomeGraphEntity.site_id == site_id,
            (
                HomeGraphEntity.id.in_(affected_refs)
                | (
                    (HomeGraphEntity.source_type == "device")
                    & (HomeGraphEntity.source_id == device_id)
                )
            ),
        )
    ):
        session.delete(entity)

    for evidence in session.scalars(
        select(HomeGraphEvidence).where(
            HomeGraphEvidence.site_id == site_id,
            HomeGraphEvidence.subject_ref.in_(affected_refs),
        )
    ):
        session.delete(evidence)

    for assessment in session.scalars(
        select(DeviceAssessment).where(
            DeviceAssessment.site_id == site_id,
            DeviceAssessment.subject_ref.in_(affected_refs),
        )
    ):
        session.delete(assessment)

    for diagnostic in session.scalars(
        select(ProtocolDiagnosticRun).where(
            ProtocolDiagnosticRun.site_id == site_id,
            (
                ProtocolDiagnosticRun.entity_ref.in_(affected_refs)
                | ProtocolDiagnosticRun.endpoint_ref.in_(affected_refs)
            ),
        )
    ):
        session.delete(diagnostic)


def _cancel_device_work(session: Session, site_id: int, affected_refs: set[str], now) -> None:
    if not affected_refs:
        return
    cancelled_task_ids: set[str] = set()
    for task in session.scalars(select(AgentTask).where(AgentTask.site_id == site_id)):
        if not affected_refs.intersection(set(task.target_refs or [])):
            continue
        if task.status not in {"completed", "failed", "cancelled"}:
            task.status = "cancelled"
            task.completed_at = now
        task.context = {
            **(task.context or {}),
            "cancelled_reason": "referenced_device_removed_from_inventory",
            "removed_refs": sorted(affected_refs),
            "removed_at": now.isoformat(),
        }
        task.updated_at = now
        cancelled_task_ids.add(task.id)
        session.add(task)

    if cancelled_task_ids:
        for step in session.scalars(select(TaskStep).where(TaskStep.task_id.in_(cancelled_task_ids))):
            if step.status not in {"completed", "failed", "cancelled"}:
                step.status = "cancelled"
                step.summary = step.summary or "Cancelled because the referenced device was removed from inventory."
                step.updated_at = now
                session.add(step)

    for blocker in session.scalars(select(Blocker)):
        if blocker.status != "open":
            continue
        if blocker.subject_ref not in affected_refs and blocker.task_id not in cancelled_task_ids:
            continue
        blocker.status = "resolved"
        blocker.resolved_at = now
        blocker.details = {
            **(blocker.details or {}),
            "resolved_by": "device_removed_from_inventory",
            "removed_refs": sorted(affected_refs),
        }
        session.add(blocker)

    cancelled_proposal_ids: set[str] = set()
    for proposal in session.scalars(select(Proposal).where(Proposal.site_id == site_id)):
        targets_removed = affected_refs.intersection(set(proposal.target_refs or []))
        task_removed = proposal.task_id in cancelled_task_ids if proposal.task_id else False
        if not targets_removed and not task_removed:
            continue
        if proposal.status in {"awaiting_user_decision", "open", "pending"}:
            proposal.status = "cancelled"
            proposal.resolved_at = now
        proposal.updated_at = now
        cancelled_proposal_ids.add(proposal.id)
        session.add(proposal)

    if cancelled_proposal_ids:
        for decision_request in session.scalars(
            select(UserDecisionRequest).where(
                UserDecisionRequest.site_id == site_id,
                UserDecisionRequest.proposal_id.in_(cancelled_proposal_ids),
                UserDecisionRequest.status == "pending",
            )
        ):
            decision_request.status = "cancelled"
            decision_request.decided_at = now
            session.add(decision_request)


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
    devices = [_serialize_device(session, device) for device in _load_devices(session)]
    return OverviewResponse(
        site=_serialize_site(site),
        devices=devices,
    )


def _connection_facets_for_device(session: Session, device: Device) -> dict:
    return connection_facets_for_entity(session, entity_ref=f"device:{device.id}")


def _append_unique(values: list[str], value: str) -> list[str]:
    return values if value in values else [*values, value]
