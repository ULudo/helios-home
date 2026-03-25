from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Asset, Device, DeviceCandidate, Site
from app.db.seed import seed_demo_data
from app.db.session import get_engine, get_session_factory, init_database
from app.hems.models import ForecastBundle
from app.hems.service import get_hems_summary, list_hems_assets, run_hems_replan
from app.hems.site_model import build_site_model
from app.main import create_app
from app.services.discovery import run_discovery


def _build_session(tmp_path, monkeypatch, name="test.db"):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / name}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    init_database()
    session_factory = get_session_factory()
    session = session_factory()
    seed_demo_data(session)
    return session


def _deterministic_forecast() -> ForecastBundle:
    start = datetime(2026, 3, 10, 6, 0, tzinfo=timezone.utc)
    steps = 96
    import_prices = [0.18 if step < 24 else 0.34 for step in range(steps)]
    return ForecastBundle(
        horizon_start=start,
        step_minutes=15,
        import_price_eur_per_kwh=import_prices,
        export_price_eur_per_kwh=[0.07] * steps,
        pv_generation_kw=[0.0] * 16 + [1.2] * 20 + [3.8] * 16 + [1.6] * 12 + [0.0] * 32,
        base_load_kw=[0.9] * steps,
        ambient_temperature_c=[7.0] * 24 + [9.0] * 24 + [11.0] * 24 + [8.0] * 24,
        notes={"source": "test_fixture"},
    )


def _make_dispatchable(session):
    battery = session.get(Device, "dev-byd-battery")
    ev = session.get(Device, "dev-easee-wallbox")
    heat_pump = session.get(Device, "dev-vaillant-heatpump")
    battery_asset = session.get(Asset, "asset-battery")
    ev_asset = session.get(Asset, "asset-wallbox")
    heat_asset = session.get(Asset, "asset-heat")
    assert battery is not None and ev is not None and heat_pump is not None
    assert battery_asset is not None and ev_asset is not None and heat_asset is not None

    battery.primary_status = "optimizable"
    battery.capabilities = {
        "visible": True,
        "monitorable": True,
        "controllable": True,
        "optimizable": True,
    }
    battery.telemetry = {
        "soc_pct": 46.0,
        "available_capacity_kwh": 9.2,
        "power_kw": 0.0,
        "simulation_supported": True,
    }
    battery_asset.status = "optimizable"
    battery_asset.metrics = dict(battery.telemetry)

    ev.primary_status = "controllable"
    ev.capabilities = {
        "visible": True,
        "monitorable": True,
        "controllable": True,
        "optimizable": False,
    }
    ev.telemetry = {
        "vehicle_connected": True,
        "current_soc_pct": 58.0,
        "max_charge_kw": 7.4,
        "simulation_supported": True,
    }
    ev_asset.status = "controllable"
    ev_asset.metrics = dict(ev.telemetry)

    heat_pump.primary_status = "controllable"
    heat_pump.capabilities = {
        "visible": True,
        "monitorable": True,
        "controllable": True,
        "optimizable": False,
    }
    heat_pump.telemetry = {
        "room_temperature_c": 20.8,
        "electrical_power_kw": 2.7,
        "simulation_supported": True,
    }
    heat_asset.status = "controllable"
    heat_asset.metrics = dict(heat_pump.telemetry)
    session.add_all([battery, ev, heat_pump, battery_asset, ev_asset, heat_asset])
    session.commit()


def _add_controllable_load(
    session,
    *,
    simulation_supported: bool = False,
    dispatch_profile: str | None = None,
) -> tuple[Device, Asset, DeviceCandidate]:
    site = session.scalar(select(Site).limit(1))
    assert site is not None

    device = Device(
        id="dev-laundry-relay",
        site_id=site.id,
        name="Laundry Relay",
        manufacturer="Shelly",
        model="Plug S Gen3",
        firmware="1.0.0",
        device_type="smart_appliance",
        primary_status="controllable",
        status_tags=["discovered", "connected", "monitorable", "controllable"],
        confidence=0.93,
        recovery_zone="auto_apply",
        protocols=["http_local"],
        capabilities={
            "visible": True,
            "monitorable": True,
            "controllable": True,
            "optimizable": True,
        },
        telemetry={
            "power_w": 95.0,
            "runtime_target_hours": 1.0,
            "minimum_runtime_minutes": 30,
            **({"simulation_supported": True} if simulation_supported else {}),
        },
        problem_summary="",
        explanation="Validated smart load",
        next_step="No action required.",
    )
    asset = Asset(
        id="asset-laundry-relay",
        site_id=site.id,
        name="Laundry Smart Load",
        asset_type="smart_appliance",
        status="controllable",
        health="healthy",
        device_ids=[device.id],
        metrics=dict(device.telemetry),
    )
    candidate = DeviceCandidate(
        id="cand-laundry-relay",
        site_id=site.id,
        stable_key="dev-laundry-relay",
        display_name=device.name,
        manufacturer=device.manufacturer,
        model=device.model,
        firmware=device.firmware,
        device_type="smart_appliance",
        discovery_sources=["local_network_live"],
        protocols=["http_local"],
        evidence={
            "http_base_url": "http://192.0.2.88",
            **({"dispatch_profile": dispatch_profile} if dispatch_profile else {}),
        },
        classification_confidence=0.93,
        classification_reasoning="Test controllable load",
        state="classified",
        matched_device_id=device.id,
    )
    session.add_all([device, asset, candidate])
    session.commit()
    return device, asset, candidate


def test_site_model_maps_current_discovery_assets_into_canonical_hems_assets(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        run_discovery(session)
        site_model = build_site_model(session)
        assets_by_type = {asset.asset_type: asset for asset in site_model.assets}

        assert {"pv_inverter", "battery", "grid_meter", "ev_charger", "heat_pump"} <= set(assets_by_type)
        assert assets_by_type["pv_inverter"].eligibility == "plan_only"
        assert assets_by_type["battery"].eligibility == "plan_only"
        assert assets_by_type["ev_charger"].eligibility == "blocked"
        assert assets_by_type["heat_pump"].eligibility == "plan_only"
        assert assets_by_type["battery"].command_contract is not None
        assert assets_by_type["battery"].command_contract.command_key == "set_power_kw"
        assert assets_by_type["battery"].command_contract.validation_state == "unavailable"
        assert assets_by_type["heat_pump"].command_contract is not None
        assert assets_by_type["heat_pump"].command_contract.maximum == 2.5
    finally:
        session.close()


def test_hems_replan_persists_intervals_and_simulated_dispatch(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        run_discovery(session)
        _make_dispatchable(session)

        plan = run_hems_replan(session, forecast_override=_deterministic_forecast())
        assert plan.status in {"completed", "degraded"}
        assert plan.policy.execution_mode == "guarded_auto"
        assert any(interval.asset_type == "battery" for interval in plan.intervals)
        assert any(interval.asset_type == "ev_charger" for interval in plan.intervals)
        assert any(interval.asset_type == "heat_pump" for interval in plan.intervals)
        assert len(plan.dispatch_events) == 3
        assert all(event.status == "simulated" for event in plan.dispatch_events)

        updated_summary = get_hems_summary(session, forecast_override=_deterministic_forecast())
        assert updated_summary.dispatchable_asset_count >= 3

        assets = list_hems_assets(session, forecast_override=_deterministic_forecast())
        assert any(asset.asset_type == "battery" and asset.eligibility == "dispatchable" for asset in assets)
        assert any(
            asset.asset_type == "battery"
            and asset.command_contract is not None
            and asset.command_contract.validation_state == "simulation"
            for asset in assets
        )
    finally:
        session.close()


def test_native_write_runtime_gate_demotes_native_contracts_to_plan_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_NATIVE_WRITES_ENABLED", "false")
    get_settings.cache_clear()
    session = _build_session(tmp_path, monkeypatch, name="native-disabled.db")
    try:
        run_discovery(session)
        _make_dispatchable(session)
        battery_candidate = session.get(DeviceCandidate, "cand-byd-battery")
        assert battery_candidate is not None
        battery_candidate.evidence = {
            **dict(battery_candidate.evidence or {}),
            "dispatch_profile": "sunspec_storage_basic_rate",
        }
        session.add(battery_candidate)
        session.commit()

        site_model = build_site_model(session)
        assets_by_type = {asset.asset_type: asset for asset in site_model.assets}

        assert assets_by_type["battery"].eligibility == "plan_only"
        assert assets_by_type["battery"].command_contract is not None
        assert assets_by_type["battery"].command_contract.validation_state == "native_disabled"
        assert "Native writes are currently disabled in the runtime." in assets_by_type["battery"].reasons
        assert assets_by_type["heat_pump"].eligibility == "dispatchable"
        assert assets_by_type["heat_pump"].command_contract is not None
        assert assets_by_type["heat_pump"].command_contract.validation_state == "simulation"
    finally:
        session.close()
        get_settings.cache_clear()


def test_site_model_maps_controllable_smart_appliance_into_dispatchable_load(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_NATIVE_WRITES_ENABLED", "true")
    get_settings.cache_clear()
    session = _build_session(tmp_path, monkeypatch, name="controllable-load.db")
    try:
        _add_controllable_load(session, dispatch_profile="shelly_http_relay")

        site_model = build_site_model(session)
        controllable_load = next(asset for asset in site_model.assets if asset.asset_key == "asset-laundry-relay")

        assert controllable_load.asset_type == "controllable_load"
        assert controllable_load.control_capability == "start_stop"
        assert controllable_load.eligibility == "dispatchable"
        assert controllable_load.command_contract is not None
        assert controllable_load.command_contract.command_key == "start_stop"
        assert controllable_load.command_contract.value_type == "boolean"
        assert controllable_load.command_contract.validation_state == "native"
    finally:
        session.close()
        get_settings.cache_clear()


def test_hems_replan_dispatches_controllable_load_binary_commands(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch, name="controllable-load-plan.db")
    try:
        _add_controllable_load(session, simulation_supported=True)

        plan = run_hems_replan(session, forecast_override=_deterministic_forecast())

        load_intervals = [interval for interval in plan.intervals if interval.asset_key == "asset-laundry-relay"]
        assert load_intervals
        assert all(isinstance(interval.command["start_stop"], bool) for interval in load_intervals)
        assert any(interval.command["start_stop"] for interval in load_intervals)
        assert any(asset.asset_type == "controllable_load" and asset.eligibility == "dispatchable" for asset in plan.assets)
        assert any(
            event.asset_key == "asset-laundry-relay"
            and event.status == "simulated"
            and isinstance(event.applied_command.get("start_stop"), bool)
            for event in plan.dispatch_events
        )
    finally:
        session.close()


def test_hems_api_endpoints_return_summary_policy_and_latest_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    get_settings.cache_clear()
    get_engine.cache_clear()

    app = create_app()
    with TestClient(app) as client:
        discovery_response = client.post("/api/v1/discovery/runs")
        assert discovery_response.status_code == 200

        summary_response = client.get("/api/v1/hems/summary")
        assert summary_response.status_code == 200
        assert summary_response.json()["policy"]["execution_mode"] == "guarded_auto"

        assets_response = client.get("/api/v1/hems/assets")
        assert assets_response.status_code == 200
        assert len(assets_response.json()) >= 1

        policy_response = client.patch("/api/v1/hems/policy", json={"grid_import_limit_kw": 9.5})
        assert policy_response.status_code == 200
        assert policy_response.json()["grid_import_limit_kw"] == 9.5

        replan_response = client.post("/api/v1/hems/replan")
        assert replan_response.status_code == 200
        assert replan_response.json()["policy"]["grid_import_limit_kw"] == 9.5

        latest_plan_response = client.get("/api/v1/hems/plans/latest")
        assert latest_plan_response.status_code == 200
        assert latest_plan_response.json()["id"] == replan_response.json()["id"]
