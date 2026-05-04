from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Asset,
    AuditEvent,
    ConnectorAttempt,
    Device,
    DeviceCandidate,
    DiscoveryRun,
    Incident,
    Recommendation,
    Site,
    utcnow,
)
from app.domain.enums import DiscoveryRunStatus
from app.domain.schemas import DeviceCandidateRead, DiscoveryRunRead, DiscoverySourceResultRead
from app.services.candidate_reconciliation import reconcile_candidates
from app.services.network_broadcast import discover_network_broadcast
from app.services.discovery_blueprints import (
    RawCandidate,
    assess_connectors,
    build_fixture_candidates,
    classify_candidate,
    diagnose_candidate,
)
from app.services.eebus import EEBUS_SOURCE_NAME, discover_eebus_site
from app.services.local_network import discover_local_network_site
from app.services.modbus import discover_modbus_site
from app.services.mqtt import discover_mqtt_site
from app.services.network_scope import list_reachable_subnets, parse_configured_subnets


@dataclass(slots=True)
class SourceDiscoveryBatch:
    source_name: str
    status: str
    message: str
    candidates: list[RawCandidate]


def _serialize_candidate(candidate: DeviceCandidate) -> DeviceCandidateRead:
    return DeviceCandidateRead(
        id=candidate.id,
        stable_key=candidate.stable_key,
        display_name=candidate.display_name,
        manufacturer=candidate.manufacturer,
        model=candidate.model,
        firmware=candidate.firmware,
        device_type=candidate.device_type,
        discovery_sources=candidate.discovery_sources or [],
        protocols=candidate.protocols or [],
        evidence=candidate.evidence or {},
        classification_confidence=candidate.classification_confidence,
        classification_reasoning=candidate.classification_reasoning,
        state=candidate.state,
        matched_device_id=candidate.matched_device_id,
        last_seen_at=candidate.last_seen_at,
    )


def _serialize_source_result(payload: dict[str, object]) -> DiscoverySourceResultRead:
    return DiscoverySourceResultRead(
        source_name=str(payload.get("source_name", "unknown")),
        status=str(payload.get("status", "unknown")),
        message=str(payload.get("message", "")),
        candidate_count=int(payload.get("candidate_count", 0)),
    )


def list_device_candidates(session: Session) -> list[DeviceCandidateRead]:
    candidates = session.scalars(select(DeviceCandidate).order_by(DeviceCandidate.display_name)).all()
    return [_serialize_candidate(candidate) for candidate in candidates]


def list_discovery_runs(session: Session) -> list[DiscoveryRunRead]:
    runs = session.scalars(select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc())).all()
    return [
        DiscoveryRunRead(
            id=run.id,
            status=run.status,
            source_names=run.source_names or [],
            source_results=[
                _serialize_source_result(payload)
                for payload in (run.notes or {}).get("source_results", [])
                if isinstance(payload, dict)
            ],
            executed_at=run.finished_at or run.started_at,
            message=run.summary,
            new_device_ids=(run.notes or {}).get("new_device_ids", []),
            refreshed_devices=run.integrated_device_count,
            candidate_count=run.candidate_count,
            integrated_devices=run.integrated_device_count,
        )
        for run in runs
    ]


def _batch_as_note(batch: SourceDiscoveryBatch) -> dict[str, object]:
    return {
        "source_name": batch.source_name,
        "status": batch.status,
        "message": batch.message,
        "candidate_count": len(batch.candidates),
    }


def _batch_as_read(batch: SourceDiscoveryBatch) -> DiscoverySourceResultRead:
    return DiscoverySourceResultRead(
        source_name=batch.source_name,
        status=batch.status,
        message=batch.message,
        candidate_count=len(batch.candidates),
    )


MANAGED_SOURCE_NAMES = {
    "fixture_registry",
    "local_network_live",
    "modbus_live",
    "mqtt_live",
    "network_broadcast_live",
    EEBUS_SOURCE_NAME,
}


def _build_fixture_batch(site: Site) -> SourceDiscoveryBatch:
    candidates = [
        replace(
            candidate,
            discovery_sources=sorted(
                {
                    "fixture_registry",
                    *(candidate.discovery_sources or []),
                }
            ),
        )
        for candidate in build_fixture_candidates(site)
    ]
    message = "Fixture-backed discovery populated the current demo integration graph."
    return SourceDiscoveryBatch(
        source_name="fixture_registry",
        status="completed",
        message=message,
        candidates=candidates,
    )


def _combine_batches(
    source_name: str,
    batches: list[tuple[str, SourceDiscoveryBatch]],
    success_message: str,
    empty_message: str,
) -> SourceDiscoveryBatch:
    combined_candidates: list[RawCandidate] = []
    completed_scopes: list[str] = []
    failed_messages: list[str] = []

    for scope, batch in batches:
        combined_candidates.extend(batch.candidates)
        if batch.status == "completed":
            completed_scopes.append(scope)
        else:
            failed_messages.append(f"{scope}: {batch.message}")

    if completed_scopes:
        if combined_candidates:
            message = success_message.format(
                candidate_count=len(combined_candidates),
                scope_count=len(completed_scopes),
            )
        else:
            message = empty_message.format(scope_count=len(completed_scopes))
        if failed_messages:
            message = f"{message} Partial failures: {'; '.join(failed_messages)}"
        return SourceDiscoveryBatch(
            source_name=source_name,
            status="completed",
            message=message,
            candidates=combined_candidates,
        )

    return SourceDiscoveryBatch(
        source_name=source_name,
        status="failed",
        message="; ".join(failed_messages) if failed_messages else f"{source_name} failed before scanning any scope.",
        candidates=[],
    )


def _discover_batches(site: Site) -> tuple[list[SourceDiscoveryBatch], list[SourceDiscoveryBatch]]:
    settings = get_settings()
    attempted_batches: list[SourceDiscoveryBatch] = []
    configured_subnets = parse_configured_subnets(site.local_subnet)
    reachable_subnets = [option.cidr for option in list_reachable_subnets()]
    scan_subnets = configured_subnets or reachable_subnets
    scope_kind = "configured" if configured_subnets else "reachable"
    live_discovery_requested = (
        settings.local_scan_enabled
        or settings.broadcast_discovery_enabled
        or settings.modbus_live_enabled
        or (bool(site.mqtt_broker_url) and settings.mqtt_live_enabled)
        or not settings.demo_mode
    )

    if scan_subnets and settings.local_scan_enabled:
        batches = [
            (
                subnet,
                discover_local_network_site(
                    subnet=subnet,
                    timeout_seconds=settings.local_scan_timeout_seconds,
                    concurrency=settings.local_scan_concurrency,
                    max_hosts=settings.local_scan_max_hosts,
                ),
            )
            for subnet in scan_subnets
        ]
        batch = _combine_batches(
            source_name="local_network_live",
            batches=batches,
            success_message=f"Imported {{candidate_count}} energy-relevant local HTTP device candidates from {{scope_count}} {scope_kind} subnet scan(s).",
            empty_message=f"Local network discovery completed across {{scope_count}} {scope_kind} subnet scan(s), but no energy-relevant HTTP interfaces were identified.",
        )
        attempted_batches.append(batch)

    if settings.broadcast_discovery_enabled:
        batch = discover_network_broadcast(
            timeout_seconds=settings.broadcast_timeout_seconds,
            max_service_types=settings.broadcast_max_service_types,
        )
        broadcast_batch = SourceDiscoveryBatch(
            source_name=batch.source_name,
            status=batch.status,
            message=batch.message,
            candidates=batch.candidates,
        )
        attempted_batches.append(broadcast_batch)

    if scan_subnets and settings.modbus_live_enabled:
        batches = [
            (
                subnet,
                discover_modbus_site(
                    subnet=subnet,
                    timeout_seconds=settings.modbus_timeout_seconds,
                    concurrency=settings.modbus_concurrency,
                    max_hosts=settings.modbus_max_hosts,
                ),
            )
            for subnet in scan_subnets
        ]
        batch = _combine_batches(
            source_name="modbus_live",
            batches=batches,
            success_message=f"Imported {{candidate_count}} candidates from native Modbus/TCP probing across {{scope_count}} {scope_kind} subnet scan(s).",
            empty_message=f"Modbus discovery completed across {{scope_count}} {scope_kind} subnet scan(s), but no native Modbus/TCP devices exposed a usable identity or SunSpec signature.",
        )
        attempted_batches.append(batch)

    if site.mqtt_broker_url and settings.mqtt_live_enabled:
        batch = discover_mqtt_site(
            broker_url=site.mqtt_broker_url,
            connect_timeout_seconds=settings.mqtt_timeout_seconds,
            probe_window_seconds=settings.mqtt_probe_window_seconds,
        )
        mqtt_batch = SourceDiscoveryBatch(
            source_name=batch.source_name,
            status=batch.status,
            message=batch.message,
            candidates=batch.candidates,
        )
        attempted_batches.append(mqtt_batch)

    if live_discovery_requested:
        batch = discover_eebus_site(
            interface_ip=settings.eebus_interface_ip or None,
            timeout_seconds=settings.eebus_timeout_seconds,
            tls_check=settings.eebus_tls_check_enabled,
        )
        eebus_batch = SourceDiscoveryBatch(
            source_name=batch.source_name,
            status=batch.status,
            message=batch.message,
            candidates=batch.candidates,
        )
        attempted_batches.append(eebus_batch)

    if attempted_batches:
        selected_batches = [batch for batch in attempted_batches if batch.candidates]
        return attempted_batches, selected_batches

    if settings.demo_mode:
        fixture_batch = _build_fixture_batch(site)
        return [fixture_batch], [fixture_batch]

    failure_batch = SourceDiscoveryBatch(
        source_name="discovery",
        status="failed",
        message=(
            "No live discovery source is configured and demo mode is disabled. Configure local subnet scanning, "
            "network broadcast discovery, native Modbus discovery, MQTT live discovery, or EEBus SHIP discovery."
        ),
        candidates=[],
    )
    return [failure_batch], []


def _upsert_candidate(session: Session, site: Site, raw_candidate: RawCandidate, classification, now) -> None:
    candidate = session.get(DeviceCandidate, raw_candidate.candidate_id)
    if candidate is None:
        candidate = DeviceCandidate(id=raw_candidate.candidate_id, site_id=site.id)
        session.add(candidate)
    candidate.stable_key = raw_candidate.device_id
    candidate.display_name = raw_candidate.display_name
    candidate.manufacturer = raw_candidate.manufacturer
    candidate.model = raw_candidate.model
    candidate.firmware = raw_candidate.firmware
    candidate.device_type = classification.device_type
    candidate.discovery_sources = raw_candidate.discovery_sources
    candidate.protocols = raw_candidate.protocols
    candidate.evidence = raw_candidate.evidence
    candidate.classification_confidence = classification.confidence
    candidate.classification_reasoning = classification.reasoning
    candidate.state = "classified"
    candidate.matched_device_id = raw_candidate.device_id
    candidate.last_seen_at = now


def _clear_related_records(session: Session, device_id: str) -> None:
    session.query(ConnectorAttempt).filter(ConnectorAttempt.device_id == device_id).delete(synchronize_session=False)
    session.query(Incident).filter(Incident.device_id == device_id).delete(synchronize_session=False)
    session.query(Recommendation).filter(Recommendation.device_id == device_id).delete(synchronize_session=False)


def _upsert_device(session: Session, site: Site, raw_candidate: RawCandidate, diagnosis, classification, now) -> None:
    device = session.get(Device, raw_candidate.device_id)
    if device is None:
        device = Device(id=raw_candidate.device_id, site_id=site.id)
        session.add(device)

    device.name = raw_candidate.display_name
    device.manufacturer = raw_candidate.manufacturer
    device.model = raw_candidate.model
    device.firmware = raw_candidate.firmware
    device.device_type = classification.device_type
    device.primary_status = diagnosis.primary_status
    device.status_tags = diagnosis.status_tags
    device.confidence = classification.confidence
    device.recovery_zone = raw_candidate.recovery_zone
    device.protocols = raw_candidate.protocols
    device.capabilities = diagnosis.capabilities
    device.telemetry = raw_candidate.telemetry
    device.problem_summary = diagnosis.problem_summary
    device.explanation = diagnosis.explanation
    device.next_step = diagnosis.next_step
    device.last_seen_at = now

    _clear_related_records(session, device.id)

    for assessment in assess_connectors(raw_candidate):
        session.add(
            ConnectorAttempt(
                device_id=device.id,
                connector_name=assessment.connector_name,
                protocol=assessment.protocol,
                outcome=assessment.outcome,
                detail=assessment.detail,
                attempted_at=now,
            )
        )

    if diagnosis.incident_title and diagnosis.incident_summary and diagnosis.incident_severity:
        session.add(
            Incident(
                site_id=site.id,
                device_id=device.id,
                severity=diagnosis.incident_severity,
                title=diagnosis.incident_title,
                summary=diagnosis.incident_summary,
                status="open",
                confidence=classification.confidence,
                created_at=now,
                updated_at=now,
            )
        )

    for recommendation in diagnosis.recommendations:
        session.add(
            Recommendation(
                site_id=site.id,
                device_id=device.id,
                title=recommendation["title"],
                description=recommendation["description"],
                priority=recommendation["priority"],
                action_type=recommendation["action_type"],
                zone=recommendation["zone"],
                auto_applicable=recommendation["auto_applicable"],
                created_at=now,
            )
        )

    asset = session.get(Asset, raw_candidate.asset_id)
    if asset is None:
        asset = Asset(id=raw_candidate.asset_id, site_id=site.id)
        session.add(asset)
    asset.name = raw_candidate.asset_name
    asset.asset_type = classification.device_type
    asset.status = diagnosis.primary_status
    asset.health = "healthy" if diagnosis.primary_status in {"monitorable", "controllable", "optimizable"} else "attention"
    if diagnosis.primary_status == "recovery_running":
        asset.health = "degraded"
    asset.device_ids = [device.id]
    asset.metrics = raw_candidate.telemetry


def _remove_materialization_for_sources(
    session: Session,
    source_names: set[str],
    keep_candidate_ids: set[str],
    keep_device_ids: set[str],
    keep_asset_ids: set[str],
) -> None:
    if not source_names:
        return

    candidates = session.scalars(select(DeviceCandidate).order_by(DeviceCandidate.created_at)).all()
    assets = session.scalars(select(Asset).order_by(Asset.created_at)).all()
    stale_device_ids: set[str] = set()

    for candidate in candidates:
        candidate_sources = set(candidate.discovery_sources or [])
        if not (candidate_sources & source_names):
            continue

        if candidate.id in keep_candidate_ids:
            continue

        device_id = candidate.matched_device_id
        if device_id and device_id not in keep_device_ids:
            stale_device_ids.add(device_id)
        session.delete(candidate)

    for asset in assets:
        asset_device_ids = list(asset.device_ids or [])
        remaining_device_ids = [device_id for device_id in asset_device_ids if device_id in keep_device_ids]
        if remaining_device_ids != asset_device_ids:
            if remaining_device_ids:
                asset.device_ids = remaining_device_ids
            elif asset.id not in keep_asset_ids:
                session.delete(asset)
        elif asset.id not in keep_asset_ids and not remaining_device_ids:
            session.delete(asset)

    for device_id in stale_device_ids:
        if device_id in keep_device_ids:
            continue
        device = session.get(Device, device_id)
        if device is not None:
            session.delete(device)


def _build_discovery_status(batches: list[SourceDiscoveryBatch], candidate_count: int) -> str:
    if any(batch.status == "failed" for batch in batches) and candidate_count == 0:
        return DiscoveryRunStatus.FAILED.value
    return DiscoveryRunStatus.COMPLETED.value


def _build_discovery_message(batches: list[SourceDiscoveryBatch], candidate_count: int) -> str:
    if len(batches) == 1:
        return batches[0].message
    if candidate_count == 0:
        return "Discovery completed without materializing any device candidates."
    return f"Discovery reconciled {candidate_count} candidates across {len(batches)} sources."


def run_discovery(session: Session) -> DiscoveryRunRead:
    now = utcnow()
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")

    attempted_batches, selected_batches = _discover_batches(site)
    source_names = [batch.source_name for batch in selected_batches] or [batch.source_name for batch in attempted_batches]

    discovery_run = DiscoveryRun(
        id=f"discover-{uuid4().hex[:10]}",
        site_id=site.id,
        status=DiscoveryRunStatus.RUNNING.value,
        source_names=source_names,
        candidate_count=0,
        integrated_device_count=0,
        summary="Starting discovery orchestration.",
        notes={},
        started_at=now,
        finished_at=None,
    )
    session.add(discovery_run)

    existing_device_ids = {device_id for device_id in session.scalars(select(Device.id)).all()}
    raw_candidates = [candidate for batch in selected_batches for candidate in batch.candidates]
    all_candidates = reconcile_candidates(raw_candidates)
    keep_candidate_ids = {candidate.candidate_id for candidate in all_candidates}
    keep_device_ids = {candidate.device_id for candidate in all_candidates}
    keep_asset_ids = {candidate.asset_id for candidate in all_candidates}

    _remove_materialization_for_sources(
        session,
        MANAGED_SOURCE_NAMES,
        keep_candidate_ids=keep_candidate_ids,
        keep_device_ids=keep_device_ids,
        keep_asset_ids=keep_asset_ids,
    )

    for raw_candidate in all_candidates:
        classification = classify_candidate(raw_candidate)
        diagnosis = diagnose_candidate(raw_candidate, assess_connectors(raw_candidate))
        _upsert_candidate(session, site, raw_candidate, classification, now)
        _upsert_device(session, site, raw_candidate, diagnosis, classification, now)

    new_device_ids = [
        candidate.device_id
        for candidate in all_candidates
        if candidate.device_id not in existing_device_ids
    ]
    discovery_status = _build_discovery_status(attempted_batches, len(all_candidates))
    discovery_message = _build_discovery_message(selected_batches or attempted_batches, len(all_candidates))

    site.discovery_last_run = now
    discovery_run.status = discovery_status
    discovery_run.candidate_count = len(all_candidates)
    discovery_run.integrated_device_count = len(all_candidates)
    discovery_run.summary = discovery_message
    discovery_run.notes = {
        "sources": source_names,
        "new_device_ids": new_device_ids,
        "source_results": [_batch_as_note(batch) for batch in attempted_batches],
    }
    discovery_run.finished_at = now

    session.add(
        AuditEvent(
            actor="discovery",
            action="run_discovery_pipeline",
            target_type="site",
            target_id=str(site.id),
            summary=discovery_message,
            details={
                "candidate_count": len(all_candidates),
                "new_device_ids": new_device_ids,
                "source_names": source_names,
                "source_results": [_batch_as_note(batch) for batch in attempted_batches],
            },
            created_at=now,
        )
    )
    session.commit()

    return DiscoveryRunRead(
        id=discovery_run.id,
        status=discovery_run.status,
        source_names=source_names,
        source_results=[_batch_as_read(batch) for batch in attempted_batches],
        executed_at=now,
        message=discovery_message,
        new_device_ids=new_device_ids,
        refreshed_devices=len(all_candidates),
        candidate_count=len(all_candidates),
        integrated_devices=len(all_candidates),
    )
