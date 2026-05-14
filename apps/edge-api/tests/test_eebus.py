from __future__ import annotations

import asyncio
import errno
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Device, HemsLoadControlDelivery, HemsPolicy, ProtocolEndpoint, Site, utcnow
from app.db.session import get_engine, get_session_factory
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


def _add_controllable_device(device_id: str, name: str, dispatch_profile: str = "shelly_http_relay") -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        site = session.scalar(select(Site).limit(1))
        assert site is not None
        now = utcnow()
        session.add(
            Device(
                id=device_id,
                site_id=site.id,
                name=name,
                manufacturer="Test",
                model="Load",
                firmware="unknown",
                device_type="controllable_load",
                primary_status="visible_only",
                status_tags=["visible_only"],
                confidence=0.9,
                recovery_zone="human_gated",
                protocols=["http"],
                capabilities={
                    "visible": True,
                    "monitorable": True,
                    "controllable": True,
                    "optimizable": True,
                },
                telemetry={"power_w": 1000},
                last_seen_at=now,
            )
        )
        session.add(
            ProtocolEndpoint(
                id=f"endpoint-{device_id}-http",
                site_id=site.id,
                owner_ref=f"device:{device_id}",
                protocol="http_local",
                host="192.0.2.10",
                port=80,
                service_name="test-http",
                status="connected",
                properties={"dispatch_profile": dispatch_profile},
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def _add_eebus_wallbox_participant(device_id: str, peer_ski: str) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        site = session.scalar(select(Site).limit(1))
        assert site is not None
        session.add(
            Device(
                id=device_id,
                site_id=site.id,
                name="Mennekes Wallbox",
                manufacturer="MENNEKES",
                model="CC612_2S0R_CC",
                firmware="unknown",
                device_type="wallbox",
                primary_status="connected",
                status_tags=["connected", "eebus_ship_ready"],
                confidence=0.9,
                recovery_zone="human_gated",
                protocols=["eebus_ship"],
                capabilities={
                    "visible": True,
                    "monitorable": False,
                    "controllable": False,
                    "optimizable": False,
                },
                telemetry={"eebus_ship_advertised": True, "ship_port": 4711},
                last_seen_at=utcnow(),
            )
        )
        session.add(
            ProtocolEndpoint(
                id=f"endpoint-{device_id}-eebus",
                site_id=site.id,
                owner_ref=f"device:{device_id}",
                protocol="eebus_ship",
                host="192.0.2.80",
                port=4711,
                service_name="wallbox._ship._tcp.local",
                status="connected",
                properties={
                    "peer_certificate_ski": peer_ski,
                    "ski": peer_ski,
                    "path": "/ship/",
                },
            )
        )
        session.commit()


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
    assert payload["changed_policy_fields"] == {}
    assert payload["changed_effective_limits"] == {"grid_import_limit_kw": 4.2}
    assert payload["active_constraints"][0]["limit_watts"] == 4200
    assert payload["constraint_distribution"]["participant_count"] == 0
    assert payload["plan"]["triggered_by"] == "eebus_lpc"

    session_factory = get_session_factory()
    with session_factory() as session:
        policy = session.scalar(select(HemsPolicy).limit(1))
        assert policy is not None
        assert policy.grid_import_limit_kw == 12.0


def test_eebus_lpc_distribution_uses_configured_load_control_participants(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "eebus-lpc-participants.db")

    with TestClient(app) as client:
        client.get("/api/v1/overview")
        _add_controllable_device("dev-load-a", "Load A")
        _add_controllable_device("dev-load-b", "Load B")
        for device_id, share in [("dev-load-a", 70), ("dev-load-b", 30)]:
            response = client.post(
                "/api/v1/actions/load_control.configure_device",
                json={
                    "input": {
                        "device_id": device_id,
                        "participates_lpc": True,
                        "lpc_share_pct": share,
                    }
                },
            )
            assert response.status_code == 200

        response = client.post(
            "/api/v1/eebus/load-power-limits/distribute",
            json={
                "use_case": "lpc",
                "limit_watts": 10000,
                "source": "test",
                "peer_ski": "0123456789abcdef0123456789abcdef01234567",
            },
        )

    assert response.status_code == 200
    distribution = response.json()["constraint_distribution"]
    assert distribution["participant_count"] == 2
    assert distribution["enforceable"] is True
    participants = {row["device_id"]: row for row in distribution["participants"]}
    assert participants["dev-load-a"]["allocated_limit_watts"] == 7000
    assert participants["dev-load-b"]["allocated_limit_watts"] == 3000

    with TestClient(app) as client:
        overview_response = client.get("/api/v1/overview")

    assert overview_response.status_code == 200
    load_control = overview_response.json()["load_control"]
    assert len(load_control["active_constraints"]) == 1
    constraint = load_control["active_constraints"][0]
    assert constraint["use_case"] == "limitationOfPowerConsumption"
    assert constraint["limit_watts"] == 10000
    overview_participants = {row["device_id"]: row for row in constraint["participants"]}
    assert overview_participants["dev-load-a"]["allocated_limit_watts"] == 7000
    assert overview_participants["dev-load-b"]["allocated_limit_watts"] == 3000


def test_eebus_lpc_distribution_creates_delivery_state_for_eebus_participant(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "eebus-lpc-delivery.db")
    wallbox_ski = "8f163fec6d78457f6e7b6dbf7b1608cdfde88388"

    with TestClient(app) as client:
        client.get("/api/v1/overview")
        _add_eebus_wallbox_participant("dev-wallbox", wallbox_ski)
        response = client.post(
            "/api/v1/actions/load_control.configure_device",
            json={
                "input": {
                    "device_id": "dev-wallbox",
                    "participates_lpc": True,
                    "lpc_share_pct": 100,
                }
            },
        )
        assert response.status_code == 200

        response = client.post(
            "/api/v1/eebus/load-power-limits/distribute",
            json={
                "use_case": "lpc",
                "limit_watts": 6000,
                "duration_seconds": 600,
                "source": "test",
                "peer_ski": "f819e215a4f292d803325276767d9e27f67fe108",
            },
        )

    assert response.status_code == 200
    participant = response.json()["constraint_distribution"]["participants"][0]
    assert participant["device_id"] == "dev-wallbox"
    assert participant["control_path"] == "eebus_spine"
    assert participant["target_peer_ski"] == wallbox_ski
    assert participant["delivery_status"] == "pending"
    assert participant["delivery_id"]

    session_factory = get_session_factory()
    with session_factory() as session:
        deliveries = session.scalars(select(HemsLoadControlDelivery)).all()
        assert len(deliveries) == 1
        assert deliveries[0].status == "pending"
        assert deliveries[0].target_peer_ski == wallbox_ski

    with TestClient(app) as client:
        overview_response = client.get("/api/v1/overview")
    overview_participant = overview_response.json()["load_control"]["active_constraints"][0]["participants"][0]
    assert overview_participant["delivery_status"] == "pending"
    assert overview_participant["control_path"] == "eebus_spine"


def test_eebus_transient_load_control_delivery_failure_stays_pending(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "eebus-lpc-transient-delivery.db")
    wallbox_ski = "8f163fec6d78457f6e7b6dbf7b1608cdfde88388"

    with TestClient(app) as client:
        client.get("/api/v1/overview")
        _add_eebus_wallbox_participant("dev-wallbox", wallbox_ski)
        response = client.post(
            "/api/v1/actions/load_control.configure_device",
            json={"input": {"device_id": "dev-wallbox", "participates_lpc": True, "lpc_share_pct": 100}},
        )
        assert response.status_code == 200
        response = client.post(
            "/api/v1/eebus/load-power-limits/distribute",
            json={
                "use_case": "lpc",
                "limit_watts": 6000,
                "duration_seconds": 600,
                "source": "test",
                "peer_ski": "f819e215a4f292d803325276767d9e27f67fe108",
            },
        )
        assert response.status_code == 200
        distribution = response.json()["constraint_distribution"]

    manager = EebusRuntimeManager()
    manager._session_factory = get_session_factory()

    async def fake_send(**kwargs):
        raise RuntimeError(f"peer {kwargs['peer_ski']} has not completed LoadControl discovery yet")

    monkeypatch.setattr(manager, "_send_load_power_limit_to_peer", fake_send)
    asyncio.run(manager._forward_load_power_limit_async(distribution))

    session_factory = get_session_factory()
    with session_factory() as session:
        delivery = session.scalar(select(HemsLoadControlDelivery).where(HemsLoadControlDelivery.target_device_id == "dev-wallbox"))
        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.detail == "Waiting for EEBUS LoadControl path."
        assert delivery.last_error == ""

    with TestClient(app) as client:
        overview_response = client.get("/api/v1/overview")
    overview_participant = overview_response.json()["load_control"]["active_constraints"][0]["participants"][0]
    assert overview_participant["delivery_status"] == "pending"
    assert overview_participant["delivery_detail"] == "Waiting for EEBUS LoadControl path."


def test_eebus_lpp_distribution_acknowledges_native_dispatch_participant(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "eebus-native-dispatch.db")

    def fake_replan(session, triggered_by):
        now = utcnow()
        return SimpleNamespace(
            id="plan-native",
            status="completed",
            execution_mode="guarded_auto",
            triggered_by=triggered_by,
            solver_name="fake",
            objective_value=None,
            summary="Native dispatch completed.",
            horizon_start=now,
            horizon_end=now,
            created_at=now,
            finished_at=now,
            dispatch_events=[
                SimpleNamespace(
                    device_id="dev-pv-native",
                    status="applied",
                    summary="Applied Fronius SunSpec active-power limit.",
                    details={"adapter": "sunspec_immediate_wmax_pct"},
                )
            ],
        )

    monkeypatch.setattr("app.hems.service.run_hems_replan", fake_replan)

    with TestClient(app) as client:
        client.get("/api/v1/overview")
        _add_controllable_device("dev-pv-native", "PV Native", dispatch_profile="sunspec_immediate_wmax_pct")
        response = client.post(
            "/api/v1/actions/load_control.configure_device",
            json={
                "input": {
                    "device_id": "dev-pv-native",
                    "participates_lpp": True,
                    "lpp_share_pct": 100,
                }
            },
        )
        assert response.status_code == 200

        response = client.post(
            "/api/v1/eebus/load-power-limits/distribute",
            json={
                "use_case": "lpp",
                "limit_watts": 2000,
                "duration_seconds": 600,
                "source": "test",
                "peer_ski": "f819e215a4f292d803325276767d9e27f67fe108",
            },
        )

    assert response.status_code == 200
    participant = response.json()["constraint_distribution"]["participants"][0]
    assert participant["device_id"] == "dev-pv-native"
    assert participant["control_path"] == "sunspec_immediate_wmax_pct"
    assert participant["delivery_status"] == "acknowledged"
    assert participant["delivery_detail"] == "Applied Fronius SunSpec active-power limit."

    session_factory = get_session_factory()
    with session_factory() as session:
        delivery = session.scalar(select(HemsLoadControlDelivery).where(HemsLoadControlDelivery.target_device_id == "dev-pv-native"))
        assert delivery is not None
        assert delivery.status == "acknowledged"


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


def test_eebus_runtime_builds_outbound_load_power_write_for_connected_client():
    sent_datagrams: list[object] = []

    class FakeClient:
        _last_remote_discovery = {
            "featureInformation": [
                {
                    "description": {
                        "featureAddress": {"device": "remote-device", "entity": [1], "feature": 2},
                        "featureType": "LoadControl",
                        "role": "server",
                    }
                }
            ]
        }
        _remote_device_address = "remote-device"

        def __init__(self):
            self.counter = 10

        def local_device_address(self):
            return "local-hems"

        def _next_msg_counter(self):
            self.counter += 1
            return self.counter

        def _outbound_read_ack_request(self):
            return True

        async def send_datagram(self, datagram):
            sent_datagrams.append(datagram)

    manager = EebusRuntimeManager()

    metadata = asyncio.run(
        manager._send_load_power_limit_with_client(
            FakeClient(),
            peer_ski="8f163fec6d78457f6e7b6dbf7b1608cdfde88388",
            watts=6000,
            duration_seconds=600,
            limit_id=0,
            is_active=True,
        )
    )

    assert metadata["msg_counter"] == 11
    assert metadata["readback_msg_counter"] == 12
    assert len(sent_datagrams) == 2
    from eebus_sdk.spine import extract_commands, extract_header

    write_header = extract_header(sent_datagrams[0])
    write_commands = extract_commands(sent_datagrams[0])
    assert write_header["cmdClassifier"] == "write"
    assert write_header["addressDestination"] == {"device": "remote-device", "entity": [1], "feature": 2}
    assert write_commands[0]["loadControlLimitListData"]["loadControlLimitData"][0]["value"]["number"] == 6000


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


def test_eebus_runtime_selects_next_available_local_ship_port(monkeypatch):
    started_ports: list[int] = []
    advertised_ports: list[int] = []

    class FakeShipServer:
        def __init__(self, config, trace_logger=None):
            self.config = config
            self.trace_logger = trace_logger
            self.server = None

        async def start(self):
            started_ports.append(self.config.port)
            if self.config.port == 4712:
                raise OSError(errno.EADDRINUSE, "address already in use")
            socket = SimpleNamespace(getsockname=lambda: ("0.0.0.0", self.config.port))
            self.server = SimpleNamespace(sockets=[socket])

        async def stop(self):
            return None

        async def events(self):
            while True:
                await asyncio.sleep(3600)
                yield SimpleNamespace(kind="idle", payload={})

    class FakeShipServiceAdvertiser:
        def __init__(self, advertisement):
            self.advertisement = advertisement

        async def start(self):
            advertised_ports.append(self.advertisement.port)

        async def stop(self):
            return None

    monkeypatch.setattr(
        "app.services.eebus_runtime.materialize_eebus_identity",
        lambda identity, directory: SimpleNamespace(
            ski=identity.ski,
            ship_id="i:32266_u:HELIOS-HOME-HEMS_r:HEMS",
            device_id="HELIOS-HOME-HEMS",
        ),
    )
    monkeypatch.setattr("eebus_sdk.ShipServer", FakeShipServer)
    monkeypatch.setattr("eebus_sdk.advertisement.ShipServiceAdvertiser", FakeShipServiceAdvertiser)

    manager = EebusRuntimeManager()
    try:
        snapshot = manager.start_or_update(
            session_factory=lambda: None,
            settings=SimpleNamespace(
                eebus_interface_ip="192.0.2.10",
                eebus_ship_bind_host="0.0.0.0",
                eebus_ship_port=0,
                eebus_ship_port_range="4712-4714",
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
            connection_direction="inbound_from_peer",
        )

        assert snapshot.status == "listening"
        assert snapshot.port == 4713
        assert started_ports[:2] == [4712, 4713]
        assert advertised_ports == [4713]
        assert snapshot.connection_states["endpoint:peer-eebus"]["inbound_from_peer"]["port"] == 4713
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
        assert second.connection_states["endpoint:peer-eebus"]["outbound_to_peer"]["status"] == "ready"
    finally:
        manager.stop()
