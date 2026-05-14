from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import (
    Blocker,
    HemsLoadControlDelivery,
    HemsLoadControlLimit,
    ProtocolDiagnosticRun,
    ProtocolEndpoint,
    utcnow,
)
from app.hems.load_control import is_transient_load_control_delivery_error, update_load_control_delivery_status
from app.hems.schemas import EebusLoadPowerLimitCreate
from app.services.eebus import distribute_load_power_limit
from app.services.eebus_identity import materialize_eebus_identity


@dataclass(slots=True)
class EebusPeerTrustMaterial:
    host: str
    port: int
    server_name: str
    advertised_ski: str
    certificate_pem: str
    certificate_ski: str
    txt_ski_matches_certificate_ski: bool | None
    client_cert_requested: bool
    openssl_exit_code: int
    path: str = "/ship/"
    error: str = ""


@dataclass(slots=True)
class EebusRuntimePeer:
    endpoint_ref: str
    entity_ref: str
    advertised_ski: str
    certificate_ski: str
    certificate_path: str
    host: str
    port: int
    path: str = "/ship/"
    server_name: str = ""


@dataclass(slots=True)
class EebusRuntimeSnapshot:
    status: str = "not_started"
    local_ski: str = ""
    local_ship_id: str = ""
    bind_host: str = ""
    port: int | None = None
    path: str = "/ship/"
    interface_ip: str = ""
    trusted_peer_skis: list[str] = field(default_factory=list)
    ready_peer_skis: list[str] = field(default_factory=list)
    endpoint_refs: list[str] = field(default_factory=list)
    diagnostic_run_refs: list[str] = field(default_factory=list)
    active_connection_directions: list[str] = field(default_factory=list)
    connection_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_event: dict[str, Any] = field(default_factory=dict)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    received_load_power_limit_count: int = 0
    last_load_power_limit: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "local_ski": self.local_ski,
            "local_ship_id": self.local_ship_id,
            "bind_host": self.bind_host,
            "port": self.port,
            "path": self.path,
            "interface_ip": self.interface_ip,
            "trusted_peer_skis": self.trusted_peer_skis,
            "ready_peer_skis": self.ready_peer_skis,
            "endpoint_refs": self.endpoint_refs,
            "diagnostic_run_refs": self.diagnostic_run_refs,
            "active_connection_directions": self.active_connection_directions,
            "connection_states": self.connection_states,
            "last_event": self.last_event,
            "recent_events": self.recent_events,
            "received_load_power_limit_count": self.received_load_power_limit_count,
            "last_load_power_limit": self.last_load_power_limit,
            "error": self.error,
        }


class EebusRuntimeTraceLogger:
    def __init__(
        self,
        manager: "EebusRuntimeManager",
        *,
        session_factory: sessionmaker[Session],
        direction: str = "inbound_from_peer",
        endpoint_ref: str = "",
        peer_ski: str = "",
    ) -> None:
        self._manager = manager
        self._session_factory = session_factory
        self._direction = direction
        self._endpoint_ref = endpoint_ref
        self._peer_ski = peer_ski

    def log(self, event: str, **data: Any) -> None:
        sanitized = _sanitize_trace_data(data)
        recorded_event = event if self._direction == "inbound_from_peer" else f"outbound_{event}"
        self._manager.record_event(
            recorded_event,
            {
                **sanitized,
                "connection_direction": self._direction,
                "endpoint_ref": self._endpoint_ref,
            },
        )
        if event in {"server_rx_data", "rx_data"}:
            self._process_incoming_spine_payload(sanitized.get("payload"))
        elif event == "server_connection_closed":
            self._manager.mark_closed(
                str(sanitized.get("error") or "connection closed"),
                endpoint_ref=self._endpoint_ref,
                direction=self._direction,
            )

    def _process_incoming_spine_payload(self, payload: Any) -> None:
        for command in _extract_load_power_limit_commands(payload):
            peer_ski = self._peer_ski or self._manager.current_peer_ski()
            command["peer_ski"] = peer_ski
            try:
                with self._session_factory() as session:
                    result = distribute_load_power_limit(
                        session,
                        EebusLoadPowerLimitCreate(
                            use_case=command["use_case"],
                            limit_id=command["limit_id"],
                            limit_watts=command["limit_watts"],
                            duration_seconds=command["duration_seconds"],
                            is_active=command["is_active"],
                            source="eebus_ship_runtime",
                            peer_ski=peer_ski or None,
                            raw=command["raw"],
                        ),
                    )
                self._manager.record_load_power_limit(
                    {
                        **command,
                        "applied_grid_import_limit_kw": result.applied_grid_import_limit_kw,
                        "applied_grid_export_limit_kw": result.applied_grid_export_limit_kw,
                        "changed_policy_fields": result.changed_policy_fields,
                    }
                )
                self._manager.forward_load_power_limit(result.constraint_distribution)
            except Exception as exc:
                self._manager.record_event(
                    "load_power_limit_distribution_failed",
                    {
                        "error": str(exc),
                        "limit_id": command.get("limit_id"),
                        "peer_ski": peer_ski,
                    },
                    level="error",
                )


class EebusRuntimeManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._advertiser: Any = None
        self._stop_event: asyncio.Event | None = None
        self._runtime_dir: str | None = None
        self._peers: dict[str, EebusRuntimePeer] = {}
        self._outbound_clients: dict[str, Any] = {}
        self._outbound_clients_by_ski: dict[str, Any] = {}
        self._pending_delivery_writes: dict[tuple[str, int], str] = {}
        self._pending_delivery_readbacks: dict[tuple[str, int], str] = {}
        self._session_factory: sessionmaker[Session] | None = None
        self._settings: Settings | None = None
        self._material: Any = None
        self._diagnostic_run_refs: set[str] = set()
        self._ready_peer_skis: set[str] = set()
        self._current_peer_ski = ""
        self._snapshot = EebusRuntimeSnapshot()

    def snapshot(self) -> EebusRuntimeSnapshot:
        with self._lock:
            return EebusRuntimeSnapshot(
                status=self._snapshot.status,
                local_ski=self._snapshot.local_ski,
                local_ship_id=self._snapshot.local_ship_id,
                bind_host=self._snapshot.bind_host,
                port=self._snapshot.port,
                path=self._snapshot.path,
                interface_ip=self._snapshot.interface_ip,
                trusted_peer_skis=list(self._snapshot.trusted_peer_skis),
                ready_peer_skis=list(self._snapshot.ready_peer_skis),
                endpoint_refs=list(self._snapshot.endpoint_refs),
                diagnostic_run_refs=list(self._snapshot.diagnostic_run_refs),
                active_connection_directions=list(self._snapshot.active_connection_directions),
                connection_states={
                    endpoint_ref: {
                        direction: dict(state)
                        for direction, state in direction_states.items()
                    }
                    for endpoint_ref, direction_states in self._snapshot.connection_states.items()
                },
                last_event=dict(self._snapshot.last_event),
                recent_events=[dict(event) for event in self._snapshot.recent_events],
                received_load_power_limit_count=self._snapshot.received_load_power_limit_count,
                last_load_power_limit=dict(self._snapshot.last_load_power_limit),
                error=self._snapshot.error,
            )

    def current_peer_ski(self) -> str:
        with self._lock:
            return self._current_peer_ski

    def start_or_update(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        local_identity,
        peer: EebusPeerTrustMaterial,
        entity_ref: str,
        endpoint_ref: str,
        diagnostic_run_ref: str,
        connection_direction: str = "auto",
    ) -> EebusRuntimeSnapshot:
        runtime_dir = self._runtime_dir or tempfile.mkdtemp(prefix="helios-eebus-runtime-")
        self._runtime_dir = runtime_dir
        peer_cert_path = Path(runtime_dir) / f"peer-{_safe_filename(endpoint_ref)}.crt.pem"
        peer_cert_path.write_text(peer.certificate_pem, encoding="ascii")
        peer_config = EebusRuntimePeer(
            endpoint_ref=endpoint_ref,
            entity_ref=entity_ref,
            advertised_ski=peer.advertised_ski,
            certificate_ski=peer.certificate_ski,
            certificate_path=str(peer_cert_path),
            host=peer.host,
            port=peer.port,
            path=peer.path or "/ship/",
            server_name=peer.server_name,
        )
        requested_directions = _requested_connection_directions(peer_config, connection_direction)
        material = materialize_eebus_identity(local_identity, directory=Path(runtime_dir) / "identity")
        with self._lock:
            can_reuse = self._can_reuse_runtime_locked(
                material_ski=material.ski,
                endpoint_ref=endpoint_ref,
                peer=peer_config,
                requested_directions=requested_directions,
            )
            self._peers[endpoint_ref] = peer_config
            if diagnostic_run_ref:
                self._diagnostic_run_refs.add(diagnostic_run_ref)
            self._session_factory = session_factory
            self._settings = settings
            self._material = material
            self._snapshot.trusted_peer_skis = sorted(
                {row.certificate_ski for row in self._peers.values() if row.certificate_ski}
            )
            self._snapshot.endpoint_refs = sorted(self._peers)
            self._snapshot.diagnostic_run_refs = sorted(self._diagnostic_run_refs)
            for direction in requested_directions:
                if can_reuse:
                    self._repair_reused_ready_state_locked(endpoint_ref, direction, peer_config)
                    continue
                self._set_connection_state_locked(
                    endpoint_ref,
                    direction,
                    {
                        "status": "starting",
                        "entity_ref": entity_ref,
                        "endpoint_ref": endpoint_ref,
                        "host": peer.host,
                        "port": peer.port,
                        "path": peer_config.path,
                        "server_name": peer_config.server_name or peer.host,
                        "peer_ski": peer.certificate_ski,
                    },
                )
            if can_reuse:
                return self.snapshot()
            self._snapshot.status = "starting"
            self._snapshot.error = ""
            runtime_directions = self._runtime_connection_directions_locked(requested_directions)
        self._restart(
            session_factory=session_factory,
            settings=settings,
            material=material,
            connection_directions=runtime_directions,
        )
        deadline = time.time() + 2.0
        while time.time() < deadline:
            snapshot = self.snapshot()
            if snapshot.status not in {"starting", "not_started"}:
                return snapshot
            time.sleep(0.05)
        return self.snapshot()

    def record_event(self, event: str, payload: dict[str, Any], *, level: str = "info") -> None:
        entry = {"level": level, "event": event, **payload, "ts": time.time()}
        with self._lock:
            if event in {"server_tls_connected", "outbound_tls_connected"}:
                self._current_peer_ski = str(payload.get("peer_ski") or "")
            self._snapshot.last_event = entry
            self._snapshot.recent_events = [*self._snapshot.recent_events[-19:], entry]
            diagnostic_refs = list(self._diagnostic_run_refs)
            self._append_diagnostic_entries(diagnostic_refs, [entry])

    def mark_listening(
        self,
        *,
        local_ski: str,
        local_ship_id: str,
        bind_host: str,
        port: int,
        path: str,
        interface_ip: str,
    ) -> None:
        with self._lock:
            self._snapshot.status = "listening"
            self._snapshot.local_ski = local_ski
            self._snapshot.local_ship_id = local_ship_id
            self._snapshot.bind_host = bind_host
            self._snapshot.port = port
            self._snapshot.path = path
            self._snapshot.interface_ip = interface_ip
            self._snapshot.trusted_peer_skis = sorted({peer.certificate_ski for peer in self._peers.values() if peer.certificate_ski})
            self._snapshot.endpoint_refs = sorted(self._peers)
            self._snapshot.diagnostic_run_refs = sorted(self._diagnostic_run_refs)
            self._snapshot.error = ""
            for endpoint_ref in self._peers:
                self._set_connection_state_locked(
                    endpoint_ref,
                    "inbound_from_peer",
                    {
                        "status": "listening",
                        "bind_host": bind_host,
                        "port": port,
                        "path": path,
                        "interface_ip": interface_ip,
                    },
                )

    def mark_ready(self, payload: dict[str, Any]) -> None:
        peer_ski = _normalize_ski(str(payload.get("peer_ski") or ""))
        endpoint_ref = str(payload.get("endpoint_ref") or "")
        with self._lock:
            if not endpoint_ref and peer_ski:
                endpoint_ref = next(
                    (
                        peer.endpoint_ref
                        for peer in self._peers.values()
                        if _normalize_ski(peer.certificate_ski) == peer_ski
                    ),
                    "",
                )
            if peer_ski:
                self._ready_peer_skis.add(peer_ski)
                self._current_peer_ski = peer_ski
            self._snapshot.status = "ship_ready"
            self._snapshot.ready_peer_skis = sorted(self._ready_peer_skis)
            self._snapshot.error = ""
            if endpoint_ref:
                self._set_connection_state_locked(
                    endpoint_ref,
                    str(payload.get("connection_direction") or "inbound_from_peer"),
                    {
                        "status": "ready",
                        "peer_ski": peer_ski,
                        "error": None,
                    },
                )
        self.record_event("ship_ready", payload)
        self._schedule_pending_load_control_retry(peer_ski)

    def mark_outbound_connecting(self, peer: EebusRuntimePeer) -> None:
        with self._lock:
            self._snapshot.status = "connecting"
            self._snapshot.error = ""
            self._set_connection_state_locked(
                peer.endpoint_ref,
                "outbound_to_peer",
                {
                    "status": "connecting",
                    "entity_ref": peer.entity_ref,
                    "endpoint_ref": peer.endpoint_ref,
                    "host": peer.host,
                    "port": peer.port,
                    "path": peer.path,
                    "server_name": peer.server_name or peer.host,
                    "peer_ski": peer.certificate_ski,
                },
            )
        self.record_event(
            "outbound_connecting",
            {
                "endpoint_ref": peer.endpoint_ref,
                "entity_ref": peer.entity_ref,
                "host": peer.host,
                "port": peer.port,
                "path": peer.path,
                "server_name": peer.server_name or peer.host,
                "peer_ski": peer.certificate_ski,
                "connection_direction": "outbound_to_peer",
            },
        )

    def mark_outbound_ready(self, peer: EebusRuntimePeer, *, remote_ship_id: str = "") -> None:
        peer_ski = _normalize_ski(peer.certificate_ski)
        with self._lock:
            if peer_ski:
                self._ready_peer_skis.add(peer_ski)
                self._current_peer_ski = peer_ski
            self._snapshot.status = "ship_ready"
            self._snapshot.ready_peer_skis = sorted(self._ready_peer_skis)
            self._snapshot.error = ""
            self._set_connection_state_locked(
                peer.endpoint_ref,
                "outbound_to_peer",
                {
                    "status": "ready",
                    "entity_ref": peer.entity_ref,
                    "endpoint_ref": peer.endpoint_ref,
                    "host": peer.host,
                    "port": peer.port,
                    "path": peer.path,
                    "server_name": peer.server_name or peer.host,
                    "peer_ski": peer_ski,
                    "remote_ship_id": remote_ship_id,
                    "error": None,
                },
            )
        self.record_event(
            "outbound_ship_ready",
            {
                "endpoint_ref": peer.endpoint_ref,
                "entity_ref": peer.entity_ref,
                "host": peer.host,
                "port": peer.port,
                "path": peer.path,
                "server_name": peer.server_name or peer.host,
                "peer_ski": peer_ski,
                "remote_ship_id": remote_ship_id,
                "connection_direction": "outbound_to_peer",
            },
        )
        self._schedule_pending_load_control_retry(peer_ski)

    def mark_outbound_failed(self, peer: EebusRuntimePeer, error: str) -> None:
        with self._lock:
            self._snapshot.status = "failed"
            self._snapshot.error = error
            self._set_connection_state_locked(
                peer.endpoint_ref,
                "outbound_to_peer",
                {
                    "status": "failed",
                    "entity_ref": peer.entity_ref,
                    "endpoint_ref": peer.endpoint_ref,
                    "host": peer.host,
                    "port": peer.port,
                    "path": peer.path,
                    "server_name": peer.server_name or peer.host,
                    "peer_ski": peer.certificate_ski,
                    "error": error,
                },
            )
        self.record_event(
            "outbound_connection_failed",
            {
                "endpoint_ref": peer.endpoint_ref,
                "entity_ref": peer.entity_ref,
                "host": peer.host,
                "port": peer.port,
                "path": peer.path,
                "server_name": peer.server_name or peer.host,
                "peer_ski": peer.certificate_ski,
                "connection_direction": "outbound_to_peer",
                "error": error,
            },
            level="error",
        )

    def mark_failed(self, error: str) -> None:
        with self._lock:
            self._snapshot.status = "failed"
            self._snapshot.error = error
        self.record_event("runtime_failed", {"error": error}, level="error")

    def mark_closed(self, error: str, *, endpoint_ref: str = "", direction: str = "") -> None:
        with self._lock:
            if endpoint_ref and direction:
                self._set_connection_state_locked(endpoint_ref, direction, {"status": "closed", "error": error})
            self._recompute_ready_peer_skis_locked()
            if self._ready_peer_skis:
                self._snapshot.status = "ship_ready"
                self._snapshot.error = ""
            elif self._snapshot.status != "failed":
                self._snapshot.status = "closed"
                self._snapshot.error = error

    def record_load_power_limit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot.received_load_power_limit_count += 1
            self._snapshot.last_load_power_limit = payload
        self.record_event("load_power_limit_received", payload)

    def forward_load_power_limit(self, distribution: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            self.record_event(
                "load_power_forwarding_unavailable",
                {"error": "EEBUS runtime loop is not running.", "constraint_distribution": distribution},
                level="error",
            )
            self._fail_pending_distribution_deliveries(distribution, "EEBUS runtime loop is not running.")
            return
        future = asyncio.run_coroutine_threadsafe(self._forward_load_power_limit_async(distribution), loop)
        future.add_done_callback(self._handle_forwarding_future)

    def _schedule_pending_load_control_retry(self, peer_ski: str) -> None:
        normalized_peer_ski = _normalize_ski(peer_ski)
        if not normalized_peer_ski:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        def schedule() -> None:
            asyncio.create_task(self._retry_pending_load_control_deliveries(normalized_peer_ski))

        loop.call_soon_threadsafe(schedule)

    def disconnect_endpoint(self, endpoint_ref: str) -> None:
        with self._lock:
            removed = self._peers.pop(endpoint_ref, None)
            self._snapshot.connection_states.pop(endpoint_ref, None)
            self._outbound_clients.pop(endpoint_ref, None)
            if removed is not None:
                normalized_ski = _normalize_ski(removed.certificate_ski)
                if normalized_ski:
                    self._outbound_clients_by_ski.pop(normalized_ski, None)
            self._recompute_ready_peer_skis_locked()
            remaining = bool(self._peers)
            session_factory = self._session_factory
            settings = self._settings
            material = self._material
            directions = self._runtime_connection_directions_locked([])
        if not remaining:
            self.stop(clear_runtime=True)
            return
        if session_factory is None or settings is None or material is None:
            self.stop(clear_runtime=False)
            return
        self._restart(
            session_factory=session_factory,
            settings=settings,
            material=material,
            connection_directions=directions,
        )

    def _restart(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        material,
        connection_directions: list[str],
    ) -> None:
        self.stop(clear_runtime=False)
        with self._lock:
            peers = list(self._peers.values())
        thread = threading.Thread(
            target=self._thread_main,
            kwargs={
                "session_factory": session_factory,
                "settings": settings,
                "material": material,
                "peers": peers,
                "connection_directions": connection_directions,
            },
            name="helios-eebus-ship-runtime",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, *, clear_runtime: bool = True) -> None:
        loop = self._loop
        server = self._server
        advertiser = self._advertiser
        if loop is not None and loop.is_running():
            async def _stop() -> None:
                if self._stop_event is not None:
                    self._stop_event.set()

            future = asyncio.run_coroutine_threadsafe(_stop(), loop)
            with contextlib.suppress(Exception):
                future.result(timeout=1.0)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._loop = None
        self._server = None
        self._advertiser = None
        self._stop_event = None
        self._outbound_clients.clear()
        self._outbound_clients_by_ski.clear()
        if clear_runtime:
            with self._lock:
                self._peers.clear()
                self._diagnostic_run_refs.clear()
                self._ready_peer_skis.clear()
                self._current_peer_ski = ""
                self._pending_delivery_writes.clear()
                self._pending_delivery_readbacks.clear()
                self._session_factory = None
                self._settings = None
                self._material = None
                self._snapshot = EebusRuntimeSnapshot()

    def _can_reuse_runtime_locked(
        self,
        *,
        material_ski: str,
        endpoint_ref: str,
        peer: EebusRuntimePeer,
        requested_directions: list[str],
    ) -> bool:
        if self._thread is None or not self._thread.is_alive():
            return False
        if self._snapshot.status in {"failed", "closed"}:
            return False
        if self._snapshot.local_ski and self._snapshot.local_ski != material_ski:
            return False
        existing = self._peers.get(endpoint_ref)
        if existing is not None and _peer_signature(existing) != _peer_signature(peer):
            return False
        endpoint_states = self._snapshot.connection_states.get(endpoint_ref, {})
        reusable_statuses = {"starting", "listening", "connecting", "ready"}
        return all(endpoint_states.get(direction, {}).get("status") in reusable_statuses for direction in requested_directions)

    def _repair_reused_ready_state_locked(
        self,
        endpoint_ref: str,
        direction: str,
        peer: EebusRuntimePeer,
    ) -> None:
        endpoint_states = self._snapshot.connection_states.get(endpoint_ref, {})
        existing = endpoint_states.get(direction, {})
        if existing.get("status") == "ready":
            return
        peer_ski = _normalize_ski(peer.certificate_ski)
        ready_peer_skis = {_normalize_ski(row) for row in self._snapshot.ready_peer_skis}
        if peer_ski and peer_ski in ready_peer_skis:
            self._set_connection_state_locked(
                endpoint_ref,
                direction,
                {
                    "status": "ready",
                    "entity_ref": peer.entity_ref,
                    "endpoint_ref": endpoint_ref,
                    "host": peer.host,
                    "port": peer.port,
                    "path": peer.path,
                    "server_name": peer.server_name or peer.host,
                    "peer_ski": peer_ski,
                    "recovered_from_ready_peer_ski": True,
                    "error": None,
                },
            )

    def _set_connection_state_locked(self, endpoint_ref: str, direction: str, updates: dict[str, Any]) -> None:
        if not endpoint_ref or not direction:
            return
        endpoint_state = {
            key: dict(value)
            for key, value in self._snapshot.connection_states.get(endpoint_ref, {}).items()
            if isinstance(value, dict)
        }
        direction_state = dict(endpoint_state.get(direction, {}))
        for key, value in updates.items():
            if value is None:
                direction_state.pop(key, None)
            else:
                direction_state[key] = value
        endpoint_state[direction] = direction_state
        self._snapshot.connection_states[endpoint_ref] = endpoint_state
        active: set[str] = set()
        for states in self._snapshot.connection_states.values():
            for state_direction, state in states.items():
                if state.get("status") in {"starting", "listening", "connecting", "ready"}:
                    active.add(state_direction)
        self._snapshot.active_connection_directions = sorted(active)

    def _runtime_connection_directions_locked(self, requested_directions: list[str]) -> list[str]:
        directions = set(requested_directions)
        directions.update(self._snapshot.active_connection_directions)
        return sorted(directions)

    def _recompute_ready_peer_skis_locked(self) -> None:
        ready: set[str] = set()
        for endpoint_ref, states in self._snapshot.connection_states.items():
            if endpoint_ref not in self._peers:
                continue
            for state in states.values():
                if not isinstance(state, dict) or state.get("status") != "ready":
                    continue
                peer_ski = _normalize_ski(str(state.get("peer_ski") or ""))
                if peer_ski:
                    ready.add(peer_ski)
        self._ready_peer_skis = ready
        self._snapshot.ready_peer_skis = sorted(ready)
        if self._current_peer_ski not in ready:
            self._current_peer_ski = next(iter(sorted(ready)), "")

    def _thread_main(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        material,
        peers: list[EebusRuntimePeer],
        connection_directions: list[str],
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(
                self._run_server(
                    session_factory=session_factory,
                    settings=settings,
                    material=material,
                    peers=peers,
                    connection_directions=connection_directions,
                )
            )
        finally:
            with contextlib.suppress(Exception):
                loop.close()

    async def _run_server(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        material,
        peers: list[EebusRuntimePeer],
        connection_directions: list[str],
    ) -> None:
        try:
            from eebus_sdk import HemsClient, ShipServer, ShipServerConfig
            from eebus_sdk.advertisement import ShipServiceAdvertisement, ShipServiceAdvertiser
            from eebus_sdk.discovery import ShipService, detect_interface_ip
            from eebus_sdk.trust import TrustStore
        except ModuleNotFoundError as exc:
            self.mark_failed(str(exc))
            return

        server = None
        advertiser = None
        client_tasks: list[asyncio.Task] = []
        try:
            interface_ip = settings.eebus_interface_ip or detect_interface_ip()
            stop_event = asyncio.Event()
            self._stop_event = stop_event
            if "inbound_from_peer" in connection_directions:
                trace_logger = EebusRuntimeTraceLogger(
                    self,
                    session_factory=session_factory,
                    direction="inbound_from_peer",
                )
                selected_port = None
                start_errors: list[str] = []
                for candidate_port in _ship_port_candidates(settings):
                    server = ShipServer(
                        ShipServerConfig(
                            identity=material,
                            ship_id=material.ship_id,
                            bind_host=settings.eebus_ship_bind_host,
                            port=candidate_port,
                            path=settings.eebus_ship_path,
                            device_id=material.device_id,
                            peer_trust_anchors=tuple(peer.certificate_path for peer in peers),
                            trusted_client_skis=tuple(peer.certificate_ski for peer in peers if peer.certificate_ski),
                            ship_handshake_mode="compatibility",
                            spine_profile="default",
                        ),
                        trace_logger=trace_logger,
                    )
                    try:
                        await server.start()
                    except OSError as exc:
                        server = None
                        detail = f"{settings.eebus_ship_bind_host}:{candidate_port} unavailable: {exc}"
                        start_errors.append(detail)
                        self.record_event(
                            "ship_port_unavailable",
                            {
                                "bind_host": settings.eebus_ship_bind_host,
                                "port": candidate_port,
                                "error": str(exc),
                            },
                            level="warning",
                        )
                        continue
                    selected_port = _bound_ship_port(server, candidate_port)
                    if selected_port != candidate_port:
                        self.record_event(
                            "ship_port_selected",
                            {
                                "bind_host": settings.eebus_ship_bind_host,
                                "requested_port": candidate_port,
                                "selected_port": selected_port,
                            },
                        )
                    break
                if server is None or selected_port is None:
                    raise OSError("; ".join(start_errors) or "No local EEBus SHIP port could be opened.")
                advertiser = ShipServiceAdvertiser(
                    ShipServiceAdvertisement(
                        interface_ip=interface_ip,
                        port=selected_port,
                        ski=material.ski,
                        ship_id=material.ship_id,
                        device_id=settings.eebus_ship_device_id or material.device_id,
                        instance_name=settings.eebus_ship_device_id or material.device_id,
                        path=settings.eebus_ship_path,
                        brand="Helios Home",
                        model="Helios Home HEMS",
                        device_type="DeviceTypeTypeEnergyManagementSystem",
                        category="DeviceCategoryTypeEnergyManagementSystem",
                        register=True,
                    )
                )
                self._server = server
                self._advertiser = advertiser
                await advertiser.start()
                self.mark_listening(
                    local_ski=material.ski,
                    local_ship_id=material.ship_id,
                    bind_host=settings.eebus_ship_bind_host,
                    port=selected_port,
                    path=settings.eebus_ship_path,
                    interface_ip=interface_ip,
                )
                client_tasks.append(asyncio.create_task(self._consume_server_events(server)))
            else:
                with self._lock:
                    self._snapshot.local_ski = material.ski
                    self._snapshot.local_ship_id = material.ship_id
                    self._snapshot.interface_ip = interface_ip
                    self._snapshot.trusted_peer_skis = sorted(
                        {peer.certificate_ski for peer in peers if peer.certificate_ski}
                    )
                    self._snapshot.endpoint_refs = sorted(peer.endpoint_ref for peer in peers)
                    self._snapshot.diagnostic_run_refs = sorted(self._diagnostic_run_refs)

            if "outbound_to_peer" in connection_directions:
                for peer in peers:
                    client_tasks.append(
                        asyncio.create_task(
                            self._run_outbound_client(
                                HemsClient=HemsClient,
                                ShipService=ShipService,
                                TrustStore=TrustStore,
                                session_factory=session_factory,
                                material=material,
                                peer=peer,
                                interface_ip=interface_ip,
                                timeout=float(settings.eebus_timeout_seconds),
                            )
                        )
                    )
            await stop_event.wait()
            for task in client_tasks:
                task.cancel()
            for task in client_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        except Exception as exc:
            self.mark_failed(str(exc))
        finally:
            with contextlib.suppress(Exception):
                if advertiser is not None:
                    await advertiser.stop()
            with contextlib.suppress(Exception):
                if server is not None:
                    await server.stop()

    async def _run_outbound_client(
        self,
        *,
        HemsClient: Any,
        ShipService: Any,
        TrustStore: Any,
        session_factory: sessionmaker[Session],
        material: Any,
        peer: EebusRuntimePeer,
        interface_ip: str,
        timeout: float,
    ) -> None:
        self.mark_outbound_connecting(peer)
        client = None
        trace_logger = EebusRuntimeTraceLogger(
            self,
            session_factory=session_factory,
            direction="outbound_to_peer",
            endpoint_ref=peer.endpoint_ref,
            peer_ski=peer.certificate_ski,
        )
        try:
            service = _ship_service_for_peer(peer, ShipService)
            trust = TrustStore.from_server_ski(peer.certificate_ski or peer.advertised_ski, verify_tls=False)
            client = await HemsClient.connect(
                service,
                material,
                trust,
                interface_ip=interface_ip,
                trace_logger=trace_logger,
                pairing_wait_seconds=60,
                timeout=timeout,
                profile="cls-adapter",
            )
            remote_ship_id = str(getattr(client.session, "remote_ship_id", "") or "")
            with contextlib.suppress(Exception):
                await client.bootstrap_spine(timeout=1.5)
            with self._lock:
                self._outbound_clients[peer.endpoint_ref] = client
                normalized_ski = _normalize_ski(peer.certificate_ski)
                if normalized_ski:
                    self._outbound_clients_by_ski[normalized_ski] = client
            self.mark_outbound_ready(peer, remote_ship_id=remote_ship_id)
            async for event in client.session_events():
                if event.kind == "datagram":
                    datagram_payload = _spine_datagram_as_ship_payload(event.payload)
                    self._process_outbound_delivery_result(peer, event.payload)
                    trace_logger._process_incoming_spine_payload(datagram_payload)
                    await client.handle_incoming_datagram(event.payload)
                    self._schedule_pending_load_control_retry(peer.certificate_ski)
                    self.record_event(
                        "outbound_datagram_handled",
                        {
                            "endpoint_ref": peer.endpoint_ref,
                            "entity_ref": peer.entity_ref,
                            "connection_direction": "outbound_to_peer",
                        },
                    )
                elif event.kind == "end":
                    self.mark_closed("peer ended SHIP session", endpoint_ref=peer.endpoint_ref, direction="outbound_to_peer")
                    break
                else:
                    self.record_event(
                        f"outbound_{event.kind}",
                        {
                            "endpoint_ref": peer.endpoint_ref,
                            "entity_ref": peer.entity_ref,
                            "connection_direction": "outbound_to_peer",
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.mark_outbound_failed(peer, str(exc))
        finally:
            with self._lock:
                if self._outbound_clients.get(peer.endpoint_ref) is client:
                    self._outbound_clients.pop(peer.endpoint_ref, None)
                normalized_ski = _normalize_ski(peer.certificate_ski)
                if normalized_ski and self._outbound_clients_by_ski.get(normalized_ski) is client:
                    self._outbound_clients_by_ski.pop(normalized_ski, None)
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()

    async def _consume_server_events(self, server: Any) -> None:
        async for event in server.events():
            if event.kind == "ready":
                self.mark_ready(dict(event.payload or {}))
            elif event.kind in {"load_power_write_sent", "load_power_write_result", "load_power_readback"}:
                payload = dict(event.payload or {})
                self._process_server_delivery_event(event.kind, payload)
                self.record_event(str(event.kind), payload)
            else:
                self.record_event(str(event.kind), dict(event.payload or {}))

    async def _forward_load_power_limit_async(self, distribution: dict[str, Any]) -> None:
        participants = [
            participant
            for participant in distribution.get("participants", [])
            if isinstance(participant, dict)
            and str(participant.get("delivery_status") or participant.get("status") or "") in {"pending", "delivery_ready"}
        ]
        for participant in participants:
            delivery_id = str(participant.get("delivery_id") or "")
            peer_ski = _normalize_ski(str(participant.get("target_peer_ski") or ""))
            endpoint_ref = str(participant.get("target_endpoint_ref") or "")
            allocated_limit_watts = int(participant.get("allocated_limit_watts") or 0)
            if not delivery_id or not peer_ski or allocated_limit_watts <= 0:
                continue
            try:
                await self._send_load_power_limit_to_peer(
                    delivery_id=delivery_id,
                    endpoint_ref=endpoint_ref,
                    peer_ski=peer_ski,
                    watts=allocated_limit_watts,
                    duration_seconds=_optional_int(distribution.get("duration_seconds")),
                    limit_id=int(distribution.get("limit_id") or 0),
                    is_active=bool(distribution.get("is_active", True)),
                )
            except Exception as exc:
                error = str(exc)
                transient = is_transient_load_control_delivery_error(error)
                self._update_delivery(
                    delivery_id,
                    status="pending" if transient else "failed",
                    detail="Waiting for EEBUS LoadControl path." if transient else "EEBUS delivery failed.",
                    error="" if transient else error,
                    raw_update={"participant": participant, "error": error, "transient": transient},
                )
                self.record_event(
                    "load_power_forwarding_pending" if transient else "load_power_forwarding_failed",
                    {
                        "delivery_id": delivery_id,
                        "peer_ski": peer_ski,
                        "endpoint_ref": endpoint_ref,
                        "error": error,
                    },
                    level="warning" if transient else "error",
                )

    async def _retry_pending_load_control_deliveries(self, peer_ski: str) -> None:
        session_factory = self._session_factory
        if session_factory is None:
            return
        normalized_peer_ski = _normalize_ski(peer_ski)
        if not normalized_peer_ski:
            return
        now = utcnow()
        pending: list[dict[str, Any]] = []
        with session_factory() as session:
            deliveries = session.scalars(
                select(HemsLoadControlDelivery).where(
                    HemsLoadControlDelivery.target_peer_ski == normalized_peer_ski,
                    HemsLoadControlDelivery.status.in_(["pending", "failed"]),
                    HemsLoadControlDelivery.is_active.is_(True),
                )
            ).all()
            for delivery in deliveries:
                if delivery.status == "failed" and not is_transient_load_control_delivery_error(delivery.last_error):
                    continue
                constraint = session.get(HemsLoadControlLimit, delivery.constraint_id)
                if constraint is None or not constraint.is_active:
                    continue
                comparable_now = now
                if constraint.expires_at is not None and constraint.expires_at.tzinfo is None:
                    comparable_now = comparable_now.replace(tzinfo=None)
                if constraint.expires_at is not None and constraint.expires_at <= comparable_now:
                    continue
                if delivery.allocated_limit_watts <= 0:
                    continue
                pending.append(
                    {
                        "delivery_id": delivery.id,
                        "endpoint_ref": delivery.target_endpoint_ref,
                        "peer_ski": delivery.target_peer_ski,
                        "watts": delivery.allocated_limit_watts,
                        "duration_seconds": delivery.duration_seconds,
                        "limit_id": delivery.limit_id,
                        "is_active": delivery.is_active,
                    }
                )
        for delivery in pending:
            try:
                await self._send_load_power_limit_to_peer(**delivery)
            except Exception as exc:
                error = str(exc)
                transient = is_transient_load_control_delivery_error(error)
                self._update_delivery(
                    str(delivery["delivery_id"]),
                    status="pending" if transient else "failed",
                    detail="Waiting for EEBUS LoadControl path." if transient else "EEBUS delivery failed.",
                    error="" if transient else error,
                    raw_update={"retry": True, "error": error, "transient": transient},
                )
                self.record_event(
                    "load_power_retry_pending" if transient else "load_power_retry_failed",
                    {
                        "delivery_id": delivery["delivery_id"],
                        "peer_ski": delivery["peer_ski"],
                        "endpoint_ref": delivery["endpoint_ref"],
                        "error": error,
                    },
                    level="warning" if transient else "error",
                )

    async def _send_load_power_limit_to_peer(
        self,
        *,
        delivery_id: str,
        endpoint_ref: str,
        peer_ski: str,
        watts: int,
        duration_seconds: int | None,
        limit_id: int,
        is_active: bool,
    ) -> None:
        server = self._server
        if server is not None:
            try:
                metadata = await server.send_load_power_limit_to_peer(
                    peer_ski,
                    watts=watts,
                    duration_seconds=duration_seconds,
                    limit_id=limit_id,
                    is_active=is_active,
                )
            except Exception as server_exc:
                self.record_event(
                    "load_power_server_send_unavailable",
                    {
                        "delivery_id": delivery_id,
                        "peer_ski": peer_ski,
                        "endpoint_ref": endpoint_ref,
                        "error": str(server_exc),
                    },
                    level="warning",
                )
            else:
                self._register_pending_delivery(peer_ski, delivery_id, metadata)
                self._update_delivery(
                    delivery_id,
                    status="sent",
                    detail="EEBUS load-power write was sent over an inbound SHIP session.",
                    raw_update=metadata,
                )
                return
        client = self._outbound_client_for(endpoint_ref, peer_ski)
        if client is None:
            raise RuntimeError(f"peer {peer_ski} is not currently connected")
        metadata = await self._send_load_power_limit_with_client(
            client,
            peer_ski=peer_ski,
            watts=watts,
            duration_seconds=duration_seconds,
            limit_id=limit_id,
            is_active=is_active,
        )
        self._register_pending_delivery(peer_ski, delivery_id, metadata)
        self._update_delivery(
            delivery_id,
            status="sent",
            detail="EEBUS load-power write was sent over an outbound SHIP session.",
            raw_update=metadata,
        )

    async def _send_load_power_limit_with_client(
        self,
        client: Any,
        *,
        peer_ski: str,
        watts: int,
        duration_seconds: int | None,
        limit_id: int,
        is_active: bool,
    ) -> dict[str, Any]:
        from eebus_sdk._load_power import build_limit_payload
        from eebus_sdk._spine_helpers import feature_addresses
        from eebus_sdk.spine import build_datagram, build_read_datagram, extract_header

        discovery = getattr(client, "_last_remote_discovery", None)
        if not isinstance(discovery, dict):
            raise RuntimeError(f"peer {peer_ski} has not completed LoadControl discovery yet")
        destinations = feature_addresses(
            discovery,
            feature_type="LoadControl",
            role="server",
            default_device=getattr(client, "_remote_device_address", None),
        )
        if not destinations:
            raise RuntimeError(f"peer {peer_ski} does not expose a LoadControl server feature")
        destination = destinations[0]
        source = {"device": client.local_device_address(), "entity": [1], "feature": 6}
        write_datagram = build_datagram(
            source=source,
            destination=destination,
            cmd_classifier="write",
            msg_counter=client._next_msg_counter(),
            commands=[
                build_limit_payload(
                    watts=watts,
                    duration_seconds=duration_seconds,
                    limit_id=limit_id,
                    is_active=is_active,
                )
            ],
            ack_request=True,
        )
        readback_datagram = build_read_datagram(
            source=source,
            destination=destination,
            msg_counter=client._next_msg_counter(),
            function_name="loadControlLimitListData",
            ack_request=client._outbound_read_ack_request(),
        )
        await client.send_datagram(write_datagram)
        await client.send_datagram(readback_datagram)
        return {
            "peer_ski": peer_ski,
            "limit_id": limit_id,
            "watts": watts,
            "duration_seconds": duration_seconds,
            "is_active": is_active,
            "msg_counter": extract_header(write_datagram).get("msgCounter"),
            "readback_msg_counter": extract_header(readback_datagram).get("msgCounter"),
        }

    def _outbound_client_for(self, endpoint_ref: str, peer_ski: str) -> Any:
        with self._lock:
            if endpoint_ref and endpoint_ref in self._outbound_clients:
                return self._outbound_clients[endpoint_ref]
            return self._outbound_clients_by_ski.get(_normalize_ski(peer_ski))

    def _register_pending_delivery(self, peer_ski: str, delivery_id: str, metadata: dict[str, Any]) -> None:
        normalized_peer_ski = _normalize_ski(peer_ski)
        with self._lock:
            msg_counter = metadata.get("msg_counter")
            readback_msg_counter = metadata.get("readback_msg_counter")
            if isinstance(msg_counter, int):
                self._pending_delivery_writes[(normalized_peer_ski, msg_counter)] = delivery_id
            if isinstance(readback_msg_counter, int):
                self._pending_delivery_readbacks[(normalized_peer_ski, readback_msg_counter)] = delivery_id

    def _process_server_delivery_event(self, kind: str, payload: dict[str, Any]) -> None:
        peer_ski = _normalize_ski(str(payload.get("peer_ski") or ""))
        if kind == "load_power_write_sent":
            delivery_id = self._delivery_id_for_counter(peer_ski, payload.get("msg_counter"), write=True, pop=False)
            if delivery_id:
                self._update_delivery(delivery_id, status="sent", detail="EEBUS load-power write was sent.", raw_update=payload)
            return
        if kind == "load_power_write_result":
            delivery_id = self._delivery_id_for_counter(peer_ski, payload.get("msg_counter_reference"), write=True, pop=True)
            if not delivery_id:
                return
            error_number = payload.get("error_number")
            if error_number == 0:
                self._update_delivery(
                    delivery_id,
                    status="acknowledged",
                    detail="EEBUS peer acknowledged the load-power write.",
                    raw_update=payload,
                )
            else:
                self._update_delivery(
                    delivery_id,
                    status="rejected",
                    detail="EEBUS peer rejected the load-power write.",
                    error=str(payload.get("description") or error_number or "rejected"),
                    raw_update=payload,
                )
            return
        if kind == "load_power_readback":
            delivery_id = self._delivery_id_for_counter(peer_ski, payload.get("msg_counter_reference"), write=False, pop=True)
            if delivery_id:
                self._update_delivery(
                    delivery_id,
                    status="readback_confirmed",
                    detail="EEBUS peer readback confirmed the load-power state.",
                    raw_update=payload,
                )

    def _process_outbound_delivery_result(self, peer: EebusRuntimePeer, datagram: Any) -> None:
        from eebus_sdk._load_power import extract_limit_state
        from eebus_sdk.spine import extract_commands, extract_header

        peer_ski = _normalize_ski(peer.certificate_ski)
        header = extract_header(datagram)
        msg_counter_reference = header.get("msgCounterReference")
        if not isinstance(msg_counter_reference, int):
            return
        commands = extract_commands(datagram)
        if header.get("cmdClassifier") == "result":
            delivery_id = self._delivery_id_for_counter(peer_ski, msg_counter_reference, write=True, pop=True)
            if not delivery_id:
                return
            result = next((command.get("resultData") for command in commands if isinstance(command.get("resultData"), dict)), None)
            error_number = result.get("errorNumber") if isinstance(result, dict) else None
            if error_number == 0:
                self._update_delivery(
                    delivery_id,
                    status="acknowledged",
                    detail="EEBUS peer acknowledged the load-power write.",
                    raw_update={"header": header, "result": result},
                )
            else:
                self._update_delivery(
                    delivery_id,
                    status="rejected",
                    detail="EEBUS peer rejected the load-power write.",
                    error=str((result or {}).get("description") or error_number or "rejected"),
                    raw_update={"header": header, "result": result},
                )
            return
        if header.get("cmdClassifier") == "reply":
            delivery_id = self._delivery_id_for_counter(peer_ski, msg_counter_reference, write=False, pop=True)
            if not delivery_id:
                return
            load_control_state = next(
                (
                    command.get("loadControlLimitListData")
                    for command in commands
                    if isinstance(command.get("loadControlLimitListData"), dict)
                ),
                None,
            )
            state = extract_limit_state(load_control_state)
            if state is not None:
                self._update_delivery(
                    delivery_id,
                    status="readback_confirmed",
                    detail="EEBUS peer readback confirmed the load-power state.",
                    raw_update={"header": header, "state": state},
                )

    def _delivery_id_for_counter(self, peer_ski: str, counter: Any, *, write: bool, pop: bool) -> str:
        if not isinstance(counter, int):
            return ""
        key = (_normalize_ski(peer_ski), counter)
        with self._lock:
            mapping = self._pending_delivery_writes if write else self._pending_delivery_readbacks
            if pop:
                return mapping.pop(key, "")
            return mapping.get(key, "")

    def _update_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        detail: str = "",
        error: str = "",
        raw_update: dict[str, Any] | None = None,
    ) -> None:
        session_factory = self._session_factory
        if session_factory is None:
            return
        with session_factory() as session:
            update_load_control_delivery_status(
                session,
                delivery_id,
                status=status,
                detail=detail,
                error=error,
                raw_update=raw_update,
            )
            session.commit()

    def _fail_pending_distribution_deliveries(self, distribution: dict[str, Any], error: str) -> None:
        for participant in distribution.get("participants", []):
            if not isinstance(participant, dict):
                continue
            delivery_id = str(participant.get("delivery_id") or "")
            if delivery_id:
                self._update_delivery(delivery_id, status="failed", detail="EEBUS delivery failed.", error=error)

    def _handle_forwarding_future(self, future: asyncio.Future) -> None:
        with contextlib.suppress(Exception):
            future.result()

    def _append_diagnostic_entries(self, diagnostic_refs: list[str], entries: list[dict[str, Any]]) -> None:
        if not diagnostic_refs:
            return
        from app.db.session import get_session_factory

        session_factory = get_session_factory()
        with session_factory() as session:
            runs = session.scalars(select(ProtocolDiagnosticRun).where(ProtocolDiagnosticRun.id.in_(diagnostic_refs))).all()
            for run in runs:
                current = list(run.log_entries or [])
                run.log_entries = [*current, *entries][-80:]
                run.result = {
                    **(run.result or {}),
                    "runtime": self.snapshot().as_dict(),
                }
                run.status = self.snapshot().status
                session.add(run)
            session.commit()


def probe_eebus_peer_certificate(
    *,
    host: str,
    port: int,
    server_name: str = "",
    advertised_ski: str = "",
    timeout_seconds: float = 6.0,
) -> EebusPeerTrustMaterial:
    if not host or not port:
        raise ValueError("EEBus peer certificate probe requires host and port.")
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is required to inspect the EEBus peer certificate.")
    cmd = [
        "openssl",
        "s_client",
        "-connect",
        f"{host}:{port}",
        "-servername",
        server_name or host,
        "-showcerts",
    ]
    result = subprocess.run(
        cmd,
        input=b"",
        capture_output=True,
        timeout=max(3.0, timeout_seconds),
        check=False,
    )
    combined = (result.stdout + result.stderr).decode("utf-8", "replace")
    pem = _extract_first_pem(combined)
    if pem is None:
        raise RuntimeError("The EEBus peer did not return a certificate during TLS probing.")
    certificate_ski = _certificate_ski_from_pem(pem)
    normalized_advertised_ski = _normalize_ski(advertised_ski)
    return EebusPeerTrustMaterial(
        host=host,
        port=port,
        server_name=server_name or host,
        advertised_ski=normalized_advertised_ski,
        certificate_pem=pem,
        certificate_ski=certificate_ski,
        txt_ski_matches_certificate_ski=(
            certificate_ski == normalized_advertised_ski if normalized_advertised_ski else None
        ),
        client_cert_requested="Client Certificate Types:" in combined,
        openssl_exit_code=result.returncode,
        path="/ship/",
        error="",
    )


def update_endpoint_peer_trust_material(session: Session, endpoint: ProtocolEndpoint, peer: EebusPeerTrustMaterial) -> None:
    properties = dict(endpoint.properties or {})
    tls_probe = dict(properties.get("tls_probe") or {})
    tls_probe.update(
        {
            "available": True,
            "client_cert_requested": peer.client_cert_requested,
            "openssl_exit_code": peer.openssl_exit_code,
            "cert_ski": peer.certificate_ski,
            "txt_ski_matches_cert_ski": peer.txt_ski_matches_certificate_ski,
        }
    )
    properties.update(
        {
            "peer_certificate_pem": peer.certificate_pem,
            "peer_certificate_ski": peer.certificate_ski,
            "txt_ski_matches_certificate_ski": peer.txt_ski_matches_certificate_ski,
            "tls_probe": tls_probe,
        }
    )
    endpoint.properties = properties
    endpoint.updated_at = utcnow()
    session.add(endpoint)


def runtime_snapshot_for_endpoint(endpoint_ref: str) -> dict[str, Any]:
    snapshot = get_eebus_runtime_manager().snapshot().as_dict()
    snapshot["endpoint_ref"] = endpoint_ref
    snapshot["endpoint_in_runtime"] = endpoint_ref in snapshot["endpoint_refs"]
    snapshot["endpoint_connection_states"] = dict(snapshot.get("connection_states", {}).get(endpoint_ref, {}))
    return snapshot


def resolve_eebus_trust_blockers(session: Session, *, subject_ref: str, task_id: str | None = None) -> None:
    statement = select(Blocker).where(
        Blocker.subject_ref == subject_ref,
        Blocker.blocker_type.in_(
            [
                "ship_trust_commissioning_not_validated",
                "local_eebus_identity_missing",
            ]
        ),
        Blocker.status == "open",
    )
    if task_id:
        statement = statement.where(Blocker.task_id == task_id)
    for blocker in session.scalars(statement).all():
        blocker.status = "resolved"
        blocker.resolved_at = utcnow()
        blocker.details = {**(blocker.details or {}), "resolved_by": "eebus_runtime_ship_ready"}
        session.add(blocker)


def _extract_load_power_limit_commands(payload: Any) -> list[dict[str, Any]]:
    try:
        from eebus_sdk._load_power import extract_limit_states
    except ModuleNotFoundError:
        return []
    datagram = (
        payload.get("data", {})
        .get("payload", {})
        .get("datagram", {})
        if isinstance(payload, dict)
        else {}
    )
    header = datagram.get("header", {}) if isinstance(datagram, dict) else {}
    if header.get("cmdClassifier") != "write":
        return []
    commands = datagram.get("payload", {}).get("cmd", []) if isinstance(datagram, dict) else []
    extracted: list[dict[str, Any]] = []
    for command in commands:
        if not isinstance(command, dict) or "loadControlLimitListData" not in command:
            continue
        states = extract_limit_states(command.get("loadControlLimitListData"))
        for state in states:
            limit_id = state.get("limit_id")
            watts = state.get("watts")
            if limit_id not in {0, 1} or not isinstance(watts, int):
                continue
            extracted.append(
                {
                    "use_case": "limitationOfPowerConsumption" if limit_id == 0 else "limitationOfPowerProduction",
                    "limit_id": limit_id,
                    "limit_watts": watts,
                    "duration_seconds": _duration_to_seconds(state.get("duration")),
                    "is_active": bool(state.get("is_active", True)),
                    "raw": {
                        "state": state,
                        "header": header,
                        "command": command,
                    },
                }
            )
    return extracted


def _duration_to_seconds(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _requested_connection_directions(peer: EebusRuntimePeer, connection_direction: str) -> list[str]:
    if connection_direction == "inbound_from_peer":
        return ["inbound_from_peer"]
    if connection_direction == "outbound_to_peer":
        return ["outbound_to_peer"]
    directions = ["inbound_from_peer"]
    if peer.host and peer.port:
        directions.append("outbound_to_peer")
    return directions


def _ship_port_candidates(settings: Settings) -> list[int]:
    configured_port = int(getattr(settings, "eebus_ship_port", 0) or 0)
    candidates: list[int] = []
    if configured_port > 0:
        candidates.append(configured_port)
    candidates.extend(_parse_ship_port_range(str(getattr(settings, "eebus_ship_port_range", "") or "")))
    if configured_port <= 0:
        candidates.append(0)
    if not candidates:
        candidates.append(0)
    unique: list[int] = []
    for port in candidates:
        if 0 <= port <= 65535 and port not in unique:
            unique.append(port)
    return unique or [0]


def _parse_ship_port_range(value: str) -> list[int]:
    ports: list[int] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            raw_start, raw_end = part.split("-", 1)
            try:
                start = int(raw_start.strip())
                end = int(raw_end.strip())
            except ValueError:
                continue
            if end < start:
                start, end = end, start
            for port in range(start, min(end, start + 99) + 1):
                ports.append(port)
            continue
        try:
            ports.append(int(part))
        except ValueError:
            continue
    return ports


def _bound_ship_port(server: Any, fallback_port: int) -> int:
    asyncio_server = getattr(server, "server", None)
    for socket in getattr(asyncio_server, "sockets", []) or []:
        with contextlib.suppress(Exception):
            sockname = socket.getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2 and isinstance(sockname[1], int):
                return sockname[1]
    return fallback_port


def _peer_signature(peer: EebusRuntimePeer) -> tuple[str, str, str, int, str, str]:
    return (
        peer.endpoint_ref,
        _normalize_ski(peer.certificate_ski),
        peer.host,
        int(peer.port or 0),
        peer.path or "/ship/",
        peer.server_name or peer.host,
    )


def _ship_service_for_peer(peer: EebusRuntimePeer, ShipService: Any) -> Any:
    addresses = {"ipv4": [], "ipv6": []}
    if _looks_like_ipv4(peer.host):
        addresses["ipv4"].append(peer.host)
    elif _looks_like_ipv6(peer.host):
        addresses["ipv6"].append(peer.host)
    return ShipService(
        service_name=peer.server_name or peer.host,
        target=peer.server_name or peer.host,
        port=peer.port,
        path=peer.path or "/ship/",
        ski=peer.certificate_ski or peer.advertised_ski,
        addresses=addresses,
        txt={"source": "helios_home_runtime"},
    )


def _spine_datagram_as_ship_payload(datagram: Any) -> dict[str, Any]:
    if hasattr(datagram, "as_ship_payload"):
        return datagram.as_ship_payload()
    if isinstance(datagram, dict):
        if "data" in datagram:
            return datagram
        return {"data": {"header": {"protocolId": "ee1.0"}, "payload": {"datagram": datagram}}}
    return {}


def _looks_like_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def _looks_like_ipv6(value: str) -> bool:
    return ":" in value and value.count(":") >= 2


def _extract_first_pem(text: str) -> str | None:
    match = re.search(
        r"(-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----)",
        text,
        flags=re.DOTALL,
    )
    return match.group(1) if match else None


def _certificate_ski_from_pem(pem: str) -> str:
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID

    certificate = x509.load_pem_x509_certificate(pem.encode("ascii"))
    extension = certificate.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_KEY_IDENTIFIER)
    return extension.value.digest.hex()


def _normalize_ski(value: str) -> str:
    return value.replace(":", "").replace(" ", "").strip().lower()


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-") or "endpoint"


def _sanitize_trace_data(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, child in value.items():
            if key in {"private_key", "private_key_pem", "key_path"}:
                sanitized[key] = "<redacted>"
            else:
                sanitized[key] = _sanitize_trace_data(child)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_trace_data(item) for item in value]
    return value


_runtime_manager = EebusRuntimeManager()


def get_eebus_runtime_manager() -> EebusRuntimeManager:
    return _runtime_manager
