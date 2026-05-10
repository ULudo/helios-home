from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import get_engine
from app.hems.schemas import EebusLoadPowerLimitCreate
from app.main import create_app
from app.services.discovery import run_discovery
from app.services.eebus import (
    EebusSdk,
    build_candidate_from_ship_service,
    build_load_power_limit_payload,
    discover_eebus_site,
)
from app.services.eebus_runtime import EebusPeerTrustMaterial, EebusRuntimeManager, _extract_load_power_limit_commands


def _fake_ship_service(**overrides):
    values = {
        "service_name": "PPC CLS._ship._tcp.local",
        "target": "ppc-cls.local",
        "port": 23292,
        "path": "/ship/",
        "ship_id": "PPC-CLS-123",
        "ski": "0123456789abcdef0123456789abcdef01234567",
        "brand": "PPC",
        "model": "CLS Gateway",
        "device_type": "SMGW gateway",
        "register": True,
        "addresses": {"ipv4": ["192.0.2.40"], "ipv6": []},
        "txt": {"type": "smgw", "brand": "PPC", "model": "CLS"},
        "tls_probe": {"available": True, "client_cert_requested": True},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _bootstrap_app(tmp_path, monkeypatch, name="eebus.db"):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / name}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    return create_app()


def test_eebus_ship_service_materializes_as_visible_candidate():
    candidate = build_candidate_from_ship_service(_fake_ship_service())

    assert candidate.protocols == ["eebus_ship"]
    assert candidate.discovery_sources == ["eebus_ship_live"]
    assert candidate.device_type == "grid_meter"
    assert candidate.capabilities_hint == {
        "visible": True,
        "monitorable": False,
        "controllable": False,
        "optimizable": False,
    }
    assert candidate.evidence["supported_use_cases"] == [
        "limitationOfPowerConsumption",
        "limitationOfPowerProduction",
    ]


def test_eebus_discovery_uses_sdk_ship_discovery(monkeypatch):
    def discover_ship_services(interface_ip, *, timeout, tls_check):
        assert interface_ip == "192.0.2.2"
        assert timeout == 1.0
        assert tls_check is True
        return [_fake_ship_service()]

    monkeypatch.setattr(
        "app.services.eebus._load_sdk",
        lambda: EebusSdk(discover_ship_services=discover_ship_services, build_limit_payload=lambda **kwargs: kwargs),
    )

    batch = discover_eebus_site(interface_ip="192.0.2.2", timeout_seconds=1.0, tls_check=True)

    assert batch.status == "completed"
    assert batch.source_name == "eebus_ship_live"
    assert len(batch.candidates) == 1
    assert batch.candidates[0].manufacturer == "PPC"


def test_eebus_ship_services_endpoint_reports_missing_sdk(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "eebus-missing-sdk.db")
    monkeypatch.setattr(
        "app.services.eebus._load_sdk",
        lambda: (_ for _ in ()).throw(RuntimeError("missing eebus sdk")),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/eebus/ship-services")

    assert response.status_code == 503
    assert "missing eebus sdk" in response.json()["detail"]


def test_load_power_payload_maps_lpc_and_lpp_limit_ids():
    lpc_payload = build_load_power_limit_payload(
        EebusLoadPowerLimitCreate(use_case="lpc", limit_watts=4200, duration_seconds=7200)
    )
    lpp_payload = build_load_power_limit_payload(
        EebusLoadPowerLimitCreate(use_case="lpp", limit_watts=10000, duration_seconds=None)
    )

    lpc_limit = lpc_payload["loadControlLimitListData"]["loadControlLimitData"][0]
    lpp_limit = lpp_payload["loadControlLimitListData"]["loadControlLimitData"][0]
    assert lpc_limit["limitId"] == 0
    assert lpc_limit["value"]["number"] == 4200
    assert lpc_limit["timePeriod"]["endTime"] in {"PT7200S", "PT2H"}
    assert lpp_limit["limitId"] == 1
    assert lpp_limit["value"]["number"] == -10000


def test_eebus_discovery_source_participates_in_discovery_pipeline(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "eebus-discovery.db")
    get_settings.cache_clear()
    monkeypatch.setattr("app.services.discovery.list_reachable_subnets", lambda: [])
    monkeypatch.setattr(
        "app.services.discovery.discover_network_broadcast",
        lambda timeout_seconds, max_service_types: SimpleNamespace(
            source_name="network_broadcast_live",
            status="completed",
            message="Network broadcast discovery completed, but no energy-relevant advertisements were identified.",
            candidates=[],
        ),
    )

    def discover_ship_services(interface_ip, *, timeout, tls_check):
        return [_fake_ship_service()]

    monkeypatch.setattr(
        "app.services.eebus._load_sdk",
        lambda: EebusSdk(discover_ship_services=discover_ship_services, build_limit_payload=lambda **kwargs: kwargs),
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/discovery/runs")
        assert response.status_code == 200
        payload = response.json()
        assert payload["source_names"] == ["eebus_ship_live"]
        assert payload["candidate_count"] == 1


def test_eebus_lpc_distribution_tightens_policy_and_replans(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "eebus-lpc.db")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/eebus/load-power-limits/distribute",
            json={
                "use_case": "lpc",
                "limit_watts": 4200,
                "duration_seconds": 7200,
                "source": "test",
                "peer_ski": "0123456789abcdef0123456789abcdef01234567",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["use_case"] == "limitationOfPowerConsumption"
    assert payload["direction"] == "consume"
    assert payload["previous_grid_import_limit_kw"] == 12.0
    assert payload["applied_grid_import_limit_kw"] == 4.2
    assert payload["applied_grid_export_limit_kw"] == 12.0
    assert payload["changed_policy_fields"] == {"grid_import_limit_kw": 4.2}
    assert payload["plan"]["triggered_by"] == "eebus_lpc"


def test_eebus_runtime_extracts_incoming_lpc_write_from_ship_trace_payload():
    payload = {
        "data": {
            "header": {"protocolId": "ee1.0"},
            "payload": {
                "datagram": {
                    "header": {
                        "specificationVersion": "1.3.0",
                        "cmdClassifier": "write",
                        "ackRequest": True,
                    },
                    "payload": {
                        "cmd": [
                            {
                                "function": "loadControlLimitListData",
                                "loadControlLimitListData": {
                                    "loadControlLimitData": [
                                        {
                                            "limitId": 0,
                                            "isLimitActive": True,
                                            "timePeriod": {"endTime": "PT10M"},
                                            "value": {"number": 6000, "scale": 0},
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                }
            },
        }
    }

    commands = _extract_load_power_limit_commands(payload)

    assert commands == [
        {
            "use_case": "limitationOfPowerConsumption",
            "limit_id": 0,
            "limit_watts": 6000,
            "duration_seconds": 600,
            "is_active": True,
            "raw": {
                "state": {
                    "raw": {
                        "limitId": 0,
                        "isLimitActive": True,
                        "timePeriod": {"endTime": "PT10M"},
                        "value": {"number": 6000, "scale": 0},
                    },
                    "limit_id": 0,
                    "direction": "consume",
                    "is_active": True,
                    "protocol_watts": 6000,
                    "watts": 6000,
                    "scale": 0,
                    "duration": "PT10M",
                },
                "header": {
                    "specificationVersion": "1.3.0",
                    "cmdClassifier": "write",
                    "ackRequest": True,
                },
                "command": {
                    "function": "loadControlLimitListData",
                    "loadControlLimitListData": {
                        "loadControlLimitData": [
                            {
                                "limitId": 0,
                                "isLimitActive": True,
                                "timePeriod": {"endTime": "PT10M"},
                                "value": {"number": 6000, "scale": 0},
                            }
                        ]
                    },
                },
            },
        }
    ]


def test_eebus_runtime_connects_outbound_to_discovered_ship_endpoint(monkeypatch, tmp_path):
    calls: list[dict] = []

    class FakeHemsClient:
        def __init__(self):
            self.session = SimpleNamespace(remote_ship_id="i:321_u:REMOTE_r:SMGW")

        @classmethod
        async def connect(cls, service, identity, trust, **kwargs):
            calls.append(
                {
                    "host": service.preferred_host(),
                    "port": service.port,
                    "path": service.path,
                    "server_name": service.server_name(),
                    "ski": service.ski,
                    "identity_ski": identity.ski,
                    "profile": kwargs.get("profile"),
                }
            )
            return cls()

        async def session_events(self):
            while True:
                await asyncio.sleep(3600)
                yield SimpleNamespace(kind="idle", payload={})

        async def handle_incoming_datagram(self, datagram):
            return []

        async def close(self):
            return None

    monkeypatch.setattr(
        "app.services.eebus_runtime.materialize_eebus_identity",
        lambda identity, directory: SimpleNamespace(
            ski=identity.ski,
            ship_id="i:32266_u:HELIOS-HOME-HEMS_r:HEMS",
            device_id="HELIOS-HOME-HEMS",
        ),
    )
    monkeypatch.setattr("eebus_sdk.HemsClient", FakeHemsClient)

    manager = EebusRuntimeManager()
    try:
        snapshot = manager.start_or_update(
            session_factory=lambda: None,
            settings=SimpleNamespace(
                eebus_interface_ip="192.0.2.10",
                eebus_ship_bind_host="0.0.0.0",
                eebus_ship_port=4714,
                eebus_ship_path="/ship/",
                eebus_ship_device_id="",
                eebus_timeout_seconds=1.0,
            ),
            local_identity=SimpleNamespace(ski="618a6ecdef40aaf6d5c36f01f971c610a13c0aed"),
            peer=EebusPeerTrustMaterial(
                host="192.0.2.40",
                port=23292,
                server_name="peer.local",
                advertised_ski="f819e215a4f292d803325276767d9e27f67fe108",
                certificate_pem="-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n",
                certificate_ski="f819e215a4f292d803325276767d9e27f67fe108",
                txt_ski_matches_certificate_ski=True,
                client_cert_requested=True,
                openssl_exit_code=0,
                path="/ship/",
            ),
            entity_ref="device:peer",
            endpoint_ref="endpoint:peer-eebus",
            diagnostic_run_ref="",
            connection_direction="outbound_to_peer",
        )

        assert snapshot.status == "ship_ready"
        assert calls == [
            {
                "host": "192.0.2.40",
                "port": 23292,
                "path": "/ship/",
                "server_name": "peer.local",
                "ski": "f819e215a4f292d803325276767d9e27f67fe108",
                "identity_ski": "618a6ecdef40aaf6d5c36f01f971c610a13c0aed",
                "profile": "cls-adapter",
            }
        ]
        state = snapshot.connection_states["endpoint:peer-eebus"]["outbound_to_peer"]
        assert state["status"] == "ready"
        assert state["host"] == "192.0.2.40"
        assert state["port"] == 23292
    finally:
        manager.stop()


def test_eebus_runtime_reuses_same_outbound_endpoint_session(monkeypatch):
    calls: list[str] = []

    class FakeHemsClient:
        def __init__(self):
            self.session = SimpleNamespace(remote_ship_id="i:321_u:REMOTE_r:SMGW")

        @classmethod
        async def connect(cls, service, identity, trust, **kwargs):
            calls.append(service.preferred_host())
            return cls()

        async def session_events(self):
            while True:
                await asyncio.sleep(3600)
                yield SimpleNamespace(kind="idle", payload={})

        async def handle_incoming_datagram(self, datagram):
            return []

        async def close(self):
            return None

    monkeypatch.setattr(
        "app.services.eebus_runtime.materialize_eebus_identity",
        lambda identity, directory: SimpleNamespace(
            ski=identity.ski,
            ship_id="i:32266_u:HELIOS-HOME-HEMS_r:HEMS",
            device_id="HELIOS-HOME-HEMS",
        ),
    )
    monkeypatch.setattr("eebus_sdk.HemsClient", FakeHemsClient)
    settings = SimpleNamespace(
        eebus_interface_ip="192.0.2.10",
        eebus_ship_bind_host="0.0.0.0",
        eebus_ship_port=4714,
        eebus_ship_path="/ship/",
        eebus_ship_device_id="",
        eebus_timeout_seconds=1.0,
    )
    identity = SimpleNamespace(ski="618a6ecdef40aaf6d5c36f01f971c610a13c0aed")
    peer = EebusPeerTrustMaterial(
        host="192.0.2.40",
        port=23292,
        server_name="peer.local",
        advertised_ski="f819e215a4f292d803325276767d9e27f67fe108",
        certificate_pem="-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n",
        certificate_ski="f819e215a4f292d803325276767d9e27f67fe108",
        txt_ski_matches_certificate_ski=True,
        client_cert_requested=True,
        openssl_exit_code=0,
        path="/ship/",
    )

    manager = EebusRuntimeManager()
    try:
        first = manager.start_or_update(
            session_factory=lambda: None,
            settings=settings,
            local_identity=identity,
            peer=peer,
            entity_ref="device:peer",
            endpoint_ref="endpoint:peer-eebus",
            diagnostic_run_ref="",
            connection_direction="outbound_to_peer",
        )
        second = manager.start_or_update(
            session_factory=lambda: None,
            settings=settings,
            local_identity=identity,
            peer=peer,
            entity_ref="device:peer",
            endpoint_ref="endpoint:peer-eebus",
            diagnostic_run_ref="",
            connection_direction="outbound_to_peer",
        )

        assert first.status == "ship_ready"
        assert second.status == "ship_ready"
        assert calls == ["192.0.2.40"]
    finally:
        manager.stop()
