from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import Device, DeviceCandidate, ProtocolEndpoint, Site, utcnow
from app.services.modbus import MODBUS_PORT, ModbusProbeResult, dispatch_profile_for_modbus_probe, probe_modbus_host


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ModbusTelemetryProbeResult:
    status: str
    telemetry: dict[str, Any]
    source: str
    message: str
    probe: ModbusProbeResult | None = None
    dispatch_profile: str = ""


def _stable_endpoint_id(owner_ref: str, protocol: str, service_name: str, host: str, port: int | None) -> str:
    key = f"{owner_ref}:{protocol}:{service_name}:{host}:{port or ''}"
    return f"endpoint-{uuid5(NAMESPACE_URL, key).hex[:16]}"


def _probe_model_blocks(probe: ModbusProbeResult) -> list[dict[str, Any]]:
    return [
        {
            "model_id": block.model_id,
            "length": block.length,
            "start_register": block.start_register,
        }
        for block in (probe.sunspec_model_blocks or [])
    ]


def _dispatch_profile_for_probe(probe: ModbusProbeResult) -> str:
    dispatch_profile, _ = dispatch_profile_for_modbus_probe(probe)
    return dispatch_profile


def _probe_evidence(probe: ModbusProbeResult) -> dict[str, Any]:
    telemetry = probe.telemetry or {}
    evidence: dict[str, Any] = {
        "identity_keys": [f"network-host:{probe.host.replace('.', '-')}"],
        "modbus_host": probe.host,
        "modbus_port": MODBUS_PORT,
        "modbus_unit_id": probe.unit_id,
        "modbus_identity": {
            "vendor_name": probe.vendor_name,
            "product_code": probe.product_code,
            "revision": probe.revision,
        },
        "sunspec_base_register": probe.sunspec_base_register,
        "sunspec_model_ids": probe.sunspec_model_ids,
        "sunspec_model_blocks": _probe_model_blocks(probe),
        "validated_metrics": sorted(telemetry.keys()),
    }
    dispatch_profile = _dispatch_profile_for_probe(probe)
    if dispatch_profile:
        _, dispatch_model_id = dispatch_profile_for_modbus_probe(probe)
        evidence["dispatch_profile"] = dispatch_profile
        evidence["dispatch_model_id"] = dispatch_model_id
        evidence["dispatch_capabilities"] = ["set_power_kw"]
    return evidence


def _update_endpoint_from_probe(endpoint: ProtocolEndpoint, probe: ModbusProbeResult, now: datetime, result: ModbusTelemetryProbeResult) -> None:
    properties = dict(endpoint.properties or {})
    properties.update(_probe_evidence(probe))
    properties["source"] = properties.get("source") or "modbus_live"
    properties["host"] = probe.host
    properties["last_seen_at"] = now.isoformat()
    properties["last_telemetry_probe_at"] = now.isoformat()
    properties["last_telemetry_status"] = result.status
    properties["last_telemetry_source"] = result.source
    properties["last_telemetry_message"] = result.message
    if result.telemetry:
        properties["last_telemetry_keys"] = sorted(result.telemetry.keys())
    endpoint.properties = properties
    endpoint.host = probe.host
    endpoint.port = MODBUS_PORT
    endpoint.updated_at = now


def _mark_probe_failure(endpoint: ProtocolEndpoint, now: datetime, result: ModbusTelemetryProbeResult) -> None:
    properties = dict(endpoint.properties or {})
    properties["last_telemetry_probe_at"] = now.isoformat()
    properties["last_telemetry_status"] = result.status
    properties["last_telemetry_source"] = result.source
    properties["last_telemetry_message"] = result.message
    endpoint.properties = properties
    endpoint.updated_at = now


def _update_device_from_probe(device: Device, probe: ModbusProbeResult, now: datetime, *, connected: bool) -> None:
    telemetry = probe.telemetry or {}
    capabilities = dict(device.capabilities or {})
    capabilities["visible"] = True
    if telemetry:
        capabilities["monitorable"] = True
    if _dispatch_profile_for_probe(probe):
        capabilities["controllable"] = True
        capabilities["optimizable"] = True
    device.capabilities = capabilities
    if "modbus_tcp" not in (device.protocols or []):
        device.protocols = sorted([*(device.protocols or []), "modbus_tcp"])
    if telemetry:
        device.telemetry = telemetry
        device.telemetry_status = "live" if connected else "sampled"
        device.telemetry_updated_at = now
    device.last_seen_at = now
    if connected:
        tags = list(device.status_tags or [])
        for tag in ("connected", "modbus_ready"):
            if tag not in tags:
                tags.append(tag)
        device.status_tags = tags
        if device.primary_status in {"", "discovered", "visible_only", "endpoint_visible", "partially_ready"}:
            device.primary_status = "connected"


def _device_for_endpoint(session: Session, endpoint: ProtocolEndpoint) -> Device | None:
    if not endpoint.owner_ref.startswith("device:"):
        return None
    return session.get(Device, endpoint.owner_ref.removeprefix("device:"))


def refresh_modbus_device_telemetry(
    session: Session,
    device: Device | None,
    endpoint: ProtocolEndpoint,
    *,
    now: datetime | None = None,
    timeout_seconds: float | None = None,
) -> ModbusTelemetryProbeResult:
    now = now or utcnow()
    timeout = timeout_seconds if timeout_seconds is not None else get_settings().modbus_timeout_seconds
    if not endpoint.host:
        result = ModbusTelemetryProbeResult(
            status="unreachable",
            telemetry={},
            source="sunspec_modbus",
            message="Modbus endpoint has no host.",
        )
        _mark_probe_failure(endpoint, now, result)
        session.add(endpoint)
        return result

    probe = probe_modbus_host(endpoint.host, timeout)
    if probe is None:
        result = ModbusTelemetryProbeResult(
            status="unreachable",
            telemetry={},
            source="sunspec_modbus",
            message="Modbus/TCP endpoint did not expose a usable SunSpec identity.",
        )
        _mark_probe_failure(endpoint, now, result)
        if device is not None:
            device.telemetry_status = "stale" if device.telemetry else "error"
            session.add(device)
        session.add(endpoint)
        return result

    telemetry = probe.telemetry or {}
    if telemetry:
        result = ModbusTelemetryProbeResult(
            status="updated",
            telemetry=telemetry,
            source="sunspec_modbus",
            message="SunSpec Modbus telemetry sample received.",
            probe=probe,
            dispatch_profile=_dispatch_profile_for_probe(probe),
        )
    else:
        result = ModbusTelemetryProbeResult(
            status="empty",
            telemetry={},
            source="sunspec_modbus",
            message="Modbus/TCP responded, but no supported SunSpec telemetry payload was decoded.",
            probe=probe,
            dispatch_profile=_dispatch_profile_for_probe(probe),
        )

    _update_endpoint_from_probe(endpoint, probe, now, result)
    session.add(endpoint)
    if device is not None and result.status == "updated":
        _update_device_from_probe(device, probe, now, connected=endpoint.status == "connected")
        session.add(device)
    elif device is not None and result.status == "empty":
        device.telemetry_status = "stale" if device.telemetry else "error"
        session.add(device)
    return result


def _append_unique(values: list[str], value: str) -> list[str]:
    return values if value in values else [*values, value]


def _candidate_hosts_for_device(session: Session, site: Site, device: Device) -> list[str]:
    hosts: list[str] = []
    device_ref = f"device:{device.id}"
    endpoints = session.scalars(
        select(ProtocolEndpoint).where(
            ProtocolEndpoint.site_id == site.id,
            ProtocolEndpoint.owner_ref == device_ref,
        )
    ).all()
    for endpoint in endpoints:
        if endpoint.host:
            hosts.append(endpoint.host)
        properties = endpoint.properties or {}
        if isinstance(properties.get("host"), str) and properties["host"]:
            hosts.append(str(properties["host"]))

    candidates = session.scalars(
        select(DeviceCandidate).where(
            DeviceCandidate.site_id == site.id,
            DeviceCandidate.matched_device_id == device.id,
        )
    ).all()
    for candidate in candidates:
        evidence = candidate.evidence or {}
        for key in ("modbus_host", "http_host", "host"):
            value = evidence.get(key)
            if isinstance(value, str) and value.strip():
                hosts.append(value.strip())
        ship = evidence.get("ship_service") if isinstance(evidence.get("ship_service"), dict) else {}
        addresses = ship.get("addresses") if isinstance(ship.get("addresses"), dict) else {}
        ipv4 = addresses.get("ipv4") if isinstance(addresses.get("ipv4"), list) else []
        hosts.extend(str(value) for value in ipv4 if str(value).strip())

    unique_hosts: list[str] = []
    for host in hosts:
        if host and host not in unique_hosts:
            unique_hosts.append(host)
    return unique_hosts


def _upsert_candidate_modbus_evidence(session: Session, site: Site, device: Device, probe: ModbusProbeResult, now: datetime) -> None:
    candidate = session.scalar(
        select(DeviceCandidate)
        .where(DeviceCandidate.site_id == site.id, DeviceCandidate.matched_device_id == device.id)
        .order_by(DeviceCandidate.last_seen_at.desc())
        .limit(1)
    )
    if candidate is None:
        return
    evidence = dict(candidate.evidence or {})
    evidence.update(_probe_evidence(probe))
    candidate.evidence = evidence
    candidate.protocols = _append_unique(list(candidate.protocols or []), "modbus_tcp")
    candidate.discovery_sources = _append_unique(list(candidate.discovery_sources or []), "modbus_live")
    candidate.last_seen_at = now
    session.add(candidate)


def record_modbus_probe_evidence(session: Session, site: Site, device: Device, probe: ModbusProbeResult, now: datetime) -> None:
    _upsert_candidate_modbus_evidence(session, site, device, probe, now)


def ensure_modbus_endpoint_for_device(
    session: Session,
    site: Site,
    device: Device,
    *,
    timeout_seconds: float | None = None,
) -> ProtocolEndpoint | None:
    device_ref = f"device:{device.id}"
    existing = session.scalars(
        select(ProtocolEndpoint).where(
            ProtocolEndpoint.site_id == site.id,
            ProtocolEndpoint.owner_ref == device_ref,
            ProtocolEndpoint.protocol == "modbus_tcp",
        )
    ).all()
    if any(endpoint.host for endpoint in existing):
        endpoint = next(endpoint for endpoint in existing if endpoint.host)
        timeout = timeout_seconds if timeout_seconds is not None else get_settings().modbus_timeout_seconds
        now = utcnow()
        result = refresh_modbus_device_telemetry(session, device, endpoint, now=now, timeout_seconds=timeout)
        if result.probe is not None:
            record_modbus_probe_evidence(session, site, device, result.probe, now)
        session.flush()
        return endpoint

    timeout = timeout_seconds if timeout_seconds is not None else get_settings().modbus_timeout_seconds
    now = utcnow()
    for host in _candidate_hosts_for_device(session, site, device):
        probe = probe_modbus_host(host, timeout)
        if probe is None:
            continue
        _upsert_candidate_modbus_evidence(session, site, device, probe, now)
        _update_device_from_probe(device, probe, now, connected=False)
        session.add(device)
        endpoint = ProtocolEndpoint(
            id=_stable_endpoint_id(device_ref, "modbus_tcp", "sunspec_modbus", probe.host, MODBUS_PORT),
            site_id=site.id,
            owner_ref=device_ref,
            protocol="modbus_tcp",
            host=probe.host,
            port=MODBUS_PORT,
            service_name="sunspec_modbus",
            status="observed",
            created_at=now,
            updated_at=now,
        )
        _update_endpoint_from_probe(
            endpoint,
            probe,
            now,
            ModbusTelemetryProbeResult(
                status="updated" if probe.telemetry else "empty",
                telemetry=probe.telemetry or {},
                source="sunspec_modbus",
                message="SunSpec Modbus endpoint materialized from a known local host.",
                probe=probe,
                dispatch_profile=_dispatch_profile_for_probe(probe),
            ),
        )
        session.merge(endpoint)
        session.flush()
        return session.get(ProtocolEndpoint, endpoint.id)
    return None


def refresh_connected_modbus_telemetry(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        endpoints = session.scalars(
            select(ProtocolEndpoint).where(
                ProtocolEndpoint.protocol == "modbus_tcp",
                ProtocolEndpoint.status == "connected",
            )
        ).all()
        changed = False
        for endpoint in endpoints:
            device = _device_for_endpoint(session, endpoint)
            result = refresh_modbus_device_telemetry(session, device, endpoint)
            if device is not None and result.probe is not None:
                site = session.get(Site, endpoint.site_id)
                if site is not None:
                    record_modbus_probe_evidence(session, site, device, result.probe, utcnow())
            changed = True
        if changed:
            session.commit()


class ModbusTelemetryRuntimeManager:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    def start(self, session_factory: sessionmaker[Session]) -> None:
        settings = get_settings()
        if settings.http_telemetry_poll_interval_seconds <= 0:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(session_factory, settings.http_telemetry_poll_interval_seconds))

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._stop_event = None

    async def _run(self, session_factory: sessionmaker[Session], interval_seconds: float) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.to_thread(refresh_connected_modbus_telemetry, session_factory)
            except Exception:
                logger.exception("Modbus telemetry polling failed.")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue


_MODBUS_TELEMETRY_RUNTIME_MANAGER = ModbusTelemetryRuntimeManager()


def get_modbus_telemetry_runtime_manager() -> ModbusTelemetryRuntimeManager:
    return _MODBUS_TELEMETRY_RUNTIME_MANAGER
