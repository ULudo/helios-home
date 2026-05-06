from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Site
from app.services.discovery_blueprints import RawCandidate
from app.services.eebus import EebusDiscoveryBatch
from app.services.local_network import LocalNetworkDiscoveryBatch
from app.services.network_broadcast import BroadcastDiscoveryBatch


def install_empty_standard_discovery(monkeypatch) -> None:
    monkeypatch.setattr("app.services.discovery.list_reachable_subnets", lambda: [])
    monkeypatch.setattr(
        "app.services.discovery.discover_network_broadcast",
        lambda timeout_seconds, max_service_types: BroadcastDiscoveryBatch(
            source_name="network_broadcast_live",
            status="completed",
            message="Network broadcast discovery completed, but no energy-relevant advertisements were identified.",
            candidates=[],
        ),
    )
    monkeypatch.setattr(
        "app.services.discovery.discover_eebus_site",
        lambda interface_ip=None, timeout_seconds=3.0, tls_check=False: EebusDiscoveryBatch(
            source_name="eebus_ship_live",
            status="completed",
            message="EEBus SHIP discovery completed, but no _ship._tcp.local services were found.",
            candidates=[],
        ),
    )


def _catalog_candidates() -> list[RawCandidate]:
    return [
        RawCandidate(
            candidate_id="cand-fronius-gen24",
            device_id="dev-fronius-gen24",
            asset_id="asset-pv",
            asset_name="PV Generation",
            display_name="Rooftop PV Inverter",
            manufacturer="Fronius",
            model="GEN24 Plus",
            firmware="1.28.4",
            device_type="pv_inverter",
            discovery_sources=["local_network_live"],
            protocols=["modbus_tcp"],
            telemetry={"power_kw": 5.8, "daily_energy_kwh": 18.2, "voltage_v": 402},
            evidence={"modbus_host": "198.51.100.12", "modbus_register_profile": "validated"},
            recovery_zone="auto_apply",
            issue_code=None,
            capabilities_hint={"visible": True, "monitorable": True, "controllable": True, "optimizable": True},
        ),
        RawCandidate(
            candidate_id="cand-byd-battery",
            device_id="dev-byd-battery",
            asset_id="asset-battery",
            asset_name="Battery Buffer",
            display_name="Battery Storage",
            manufacturer="BYD",
            model="Battery-Box Premium HVS",
            firmware="3.14.1",
            device_type="battery",
            discovery_sources=["local_network_live"],
            protocols=["modbus_tcp"],
            telemetry={"soc_pct": 48, "power_kw": -1.2, "available_capacity_kwh": 9.1},
            evidence={
                "modbus_host": "198.51.100.22",
                "register_issue": "unit_id_mismatch",
                "validated_read_paths": ["soc_pct", "power_kw"],
            },
            recovery_zone="guarded_apply",
            issue_code="modbus_unit_id_mismatch",
            capabilities_hint={"visible": True, "monitorable": True, "controllable": True, "optimizable": False},
        ),
        RawCandidate(
            candidate_id="cand-shelly-3em",
            device_id="dev-shelly-3em",
            asset_id="asset-grid",
            asset_name="Grid Metering",
            display_name="Grid Meter",
            manufacturer="Shelly",
            model="3EM",
            firmware="2025.2.1",
            device_type="grid_meter",
            discovery_sources=["local_network_live"],
            protocols=["mqtt"],
            telemetry={"grid_power_kw": -2.7, "grid_import_today_kwh": 4.2},
            evidence={"mqtt_topics": ["shellies/gridmeter/emeter/0/power"]},
            recovery_zone="auto_apply",
            issue_code=None,
            capabilities_hint={"visible": True, "monitorable": True, "controllable": False, "optimizable": True},
        ),
        RawCandidate(
            candidate_id="cand-easee-wallbox",
            device_id="dev-easee-wallbox",
            asset_id="asset-wallbox",
            asset_name="EV Charging",
            display_name="EV Charger",
            manufacturer="Easee",
            model="Home",
            firmware="309B",
            device_type="wallbox",
            discovery_sources=["local_network_live"],
            protocols=["vendor_cloud"],
            telemetry={"vehicle_connected": True, "session_energy_kwh": 0.0},
            evidence={"cloud_pairing_required": True, "vendor_app": "Easee"},
            recovery_zone="human_gated",
            issue_code="auth_required",
            capabilities_hint={"visible": True, "monitorable": False, "controllable": False, "optimizable": False},
        ),
        RawCandidate(
            candidate_id="cand-vaillant-heatpump",
            device_id="dev-vaillant-heatpump",
            asset_id="asset-heat",
            asset_name="Thermal Control",
            display_name="Heat Pump",
            manufacturer="Vaillant",
            model="aroTHERM plus",
            firmware="7.2.0",
            device_type="heat_pump",
            discovery_sources=["local_network_live"],
            protocols=["vendor_cloud"],
            telemetry={"flow_temperature_c": 33.5, "thermal_output_kw": 2.4},
            evidence={"write_profile": "unverified"},
            recovery_zone="guarded_apply",
            issue_code="protocol_gap",
            capabilities_hint={"visible": True, "monitorable": True, "controllable": False, "optimizable": False},
        ),
    ]


def install_catalog_discovery(session: Session, monkeypatch) -> None:
    install_empty_standard_discovery(monkeypatch)
    site = session.get(Site, 1)
    assert site is not None
    site.local_subnet = "198.51.100.0/24"
    session.add(site)
    session.commit()
    candidates = _catalog_candidates()
    monkeypatch.setattr(
        "app.services.discovery.discover_local_network_site",
        lambda subnet, timeout_seconds, concurrency, max_hosts: LocalNetworkDiscoveryBatch(
            source_name="local_network_live",
            status="completed",
            message=f"Imported {len(candidates)} explicit test catalog candidate(s).",
            candidates=candidates,
        ),
    )
