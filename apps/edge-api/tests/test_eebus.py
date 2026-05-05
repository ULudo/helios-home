from __future__ import annotations

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
