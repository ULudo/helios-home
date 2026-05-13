from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import Device, ProtocolEndpoint, utcnow
from app.services.local_network import (
    _telemetry_from_shelly_rpc,
    _telemetry_from_shelly_status,
    _telemetry_from_tasmota,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HttpTelemetryProbeResult:
    status: str
    telemetry: dict[str, Any]
    source: str
    message: str


def _endpoint_base_url(endpoint: ProtocolEndpoint) -> str:
    properties = endpoint.properties or {}
    base_url = str(properties.get("base_url") or "").strip().rstrip("/")
    if base_url:
        return base_url
    if not endpoint.host:
        return ""
    scheme = "https" if endpoint.port == 443 else "http"
    if endpoint.port and endpoint.port not in {80, 443}:
        return f"{scheme}://{endpoint.host}:{endpoint.port}"
    return f"{scheme}://{endpoint.host}"


def _profile_order(device: Device | None, endpoint: ProtocolEndpoint) -> list[str]:
    properties = endpoint.properties or {}
    haystack = " ".join(
        [
            endpoint.service_name or "",
            str(properties.get("fingerprint_profile") or ""),
            str(properties.get("profile") or ""),
            str(device.manufacturer if device is not None else ""),
            str(device.model if device is not None else ""),
        ]
    ).lower()
    profiles: list[str] = []
    if "shelly" in haystack:
        profiles.append("shelly")
    if "tasmota" in haystack:
        profiles.append("tasmota")
    for profile in ("shelly", "tasmota"):
        if profile not in profiles:
            profiles.append(profile)
    return profiles


def _fetch_json(client: httpx.Client, base_url: str, path: str, timeout_seconds: float) -> dict[str, Any] | None:
    try:
        response = client.get(
            f"{base_url}{path}",
            timeout=timeout_seconds,
            headers={"Accept": "application/json, */*;q=0.5"},
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def probe_http_endpoint_telemetry(
    *,
    base_url: str,
    profiles: list[str],
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> HttpTelemetryProbeResult:
    if not base_url:
        return HttpTelemetryProbeResult(
            status="unreachable",
            telemetry={},
            source="http_local",
            message="HTTP endpoint has no host or base URL.",
        )

    owns_client = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True, verify=False)

    try:
        saw_response = False
        for profile in profiles:
            telemetry: dict[str, Any] = {}
            if profile == "shelly":
                status_payload = _fetch_json(client, base_url, "/status", timeout_seconds)
                if status_payload is not None:
                    saw_response = True
                    telemetry.update(_telemetry_from_shelly_status(status_payload))
                rpc_payload = _fetch_json(client, base_url, "/rpc/Shelly.GetStatus", timeout_seconds)
                if rpc_payload is not None:
                    saw_response = True
                    telemetry.update(_telemetry_from_shelly_rpc(rpc_payload))
                if telemetry:
                    return HttpTelemetryProbeResult(
                        status="updated",
                        telemetry=telemetry,
                        source="shelly_http",
                        message="Shelly local HTTP telemetry sample received.",
                    )
            elif profile == "tasmota":
                status_payload = _fetch_json(client, base_url, "/cm?cmnd=Status%200", timeout_seconds)
                if status_payload is not None:
                    saw_response = True
                    telemetry.update(_telemetry_from_tasmota(status_payload))
                if telemetry:
                    return HttpTelemetryProbeResult(
                        status="updated",
                        telemetry=telemetry,
                        source="tasmota_http",
                        message="Tasmota local HTTP telemetry sample received.",
                    )

        if saw_response:
            return HttpTelemetryProbeResult(
                status="empty",
                telemetry={},
                source="http_local",
                message="HTTP endpoint responded, but no supported telemetry payload was decoded.",
            )
        return HttpTelemetryProbeResult(
            status="unreachable",
            telemetry={},
            source="http_local",
            message="HTTP endpoint did not respond to telemetry probes.",
        )
    finally:
        if owns_client:
            client.close()


def refresh_http_device_telemetry(
    session: Session,
    device: Device | None,
    endpoint: ProtocolEndpoint,
    *,
    now: datetime | None = None,
    timeout_seconds: float | None = None,
) -> HttpTelemetryProbeResult:
    now = now or utcnow()
    settings = get_settings()
    timeout = timeout_seconds if timeout_seconds is not None else settings.http_telemetry_timeout_seconds
    result = probe_http_endpoint_telemetry(
        base_url=_endpoint_base_url(endpoint),
        profiles=_profile_order(device, endpoint),
        timeout_seconds=timeout,
    )

    properties = dict(endpoint.properties or {})
    properties["last_telemetry_probe_at"] = now.isoformat()
    properties["last_telemetry_status"] = result.status
    properties["last_telemetry_source"] = result.source
    properties["last_telemetry_message"] = result.message
    if result.telemetry:
        properties["last_telemetry_keys"] = sorted(result.telemetry.keys())
    endpoint.properties = properties
    endpoint.updated_at = now
    session.add(endpoint)

    if device is not None:
        if result.status == "updated":
            device.telemetry = result.telemetry
            device.telemetry_status = "live"
            device.telemetry_updated_at = now
            device.last_seen_at = now
            tags = list(device.status_tags or [])
            for tag in ("connected", "http_ready"):
                if tag not in tags:
                    tags.append(tag)
            device.status_tags = tags
        elif result.status in {"unreachable", "empty"}:
            device.telemetry_status = "stale" if device.telemetry else "error"
        session.add(device)
    return result


def refresh_connected_http_telemetry(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        endpoints = session.scalars(
            select(ProtocolEndpoint).where(
                ProtocolEndpoint.protocol == "http_local",
                ProtocolEndpoint.status == "connected",
            )
        ).all()
        changed = False
        for endpoint in endpoints:
            device = _device_for_endpoint(session, endpoint)
            refresh_http_device_telemetry(session, device, endpoint)
            changed = True
        if changed:
            session.commit()


def _device_for_endpoint(session: Session, endpoint: ProtocolEndpoint) -> Device | None:
    if not endpoint.owner_ref.startswith("device:"):
        return None
    return session.get(Device, endpoint.owner_ref.removeprefix("device:"))


class HttpTelemetryRuntimeManager:
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
                await asyncio.to_thread(refresh_connected_http_telemetry, session_factory)
            except Exception:
                logger.exception("HTTP telemetry polling failed.")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue


_HTTP_TELEMETRY_RUNTIME_MANAGER = HttpTelemetryRuntimeManager()


def get_http_telemetry_runtime_manager() -> HttpTelemetryRuntimeManager:
    return _HTTP_TELEMETRY_RUNTIME_MANAGER
