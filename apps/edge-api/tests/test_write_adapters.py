from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import get_settings
from app.db.models import Asset, Device, DeviceCandidate
from app.db.seed import seed_demo_data
from app.db.session import get_engine, get_session_factory, init_database
from app.domain.enums import HemsExecutionMode
from app.hems.dispatcher import dispatch_current_interval
from app.hems.models import CanonicalAsset, ForecastBundle, PlannedInterval, SiteModel


def _build_session(tmp_path, monkeypatch, name="write-adapter.db"):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / name}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    init_database()
    session_factory = get_session_factory()
    session = session_factory()
    seed_demo_data(session)
    return session


def _site_model(asset: CanonicalAsset) -> SiteModel:
    now = datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc)
    return SiteModel(
        built_at=now,
        policy={"execution_mode": HemsExecutionMode.GUARDED_AUTO.value},
        assets=[asset],
        grid_constraints={"import_limit_kw": 12.0, "export_limit_kw": 12.0},
        forecast=ForecastBundle(
            horizon_start=now,
            step_minutes=15,
            import_price_eur_per_kwh=[0.2],
            export_price_eur_per_kwh=[0.08],
            pv_generation_kw=[0.0],
            base_load_kw=[1.0],
            ambient_temperature_c=[7.0],
            notes={},
        ),
    )


def _create_dispatchable_target(session, *, device_id: str, asset_id: str, evidence: dict, telemetry: dict | None = None):
    session.add(
        Device(
            id=device_id,
            site_id=1,
            name="Dispatch target",
            manufacturer="Adapter Test",
            model="Local",
            firmware="1.0",
            device_type="heat_pump",
            primary_status="controllable",
            status_tags=["controllable"],
            confidence=0.9,
            recovery_zone="guarded_apply",
            protocols=["http_local"],
            capabilities={"visible": True, "monitorable": True, "controllable": True, "optimizable": False},
            telemetry=dict(telemetry or {}),
            problem_summary="",
            explanation="",
            next_step="",
        )
    )
    session.add(
        Asset(
            id=asset_id,
            site_id=1,
            name="Thermal Control",
            asset_type="heat_pump",
            status="controllable",
            health="healthy",
            device_ids=[device_id],
            metrics=dict(telemetry or {}),
        )
    )
    session.add(
        DeviceCandidate(
            id=f"cand-{device_id}",
            site_id=1,
            stable_key=device_id,
            display_name="Dispatch target",
            manufacturer="Adapter Test",
            model="Local",
            firmware="1.0",
            device_type="heat_pump",
            discovery_sources=["local_network_live"],
            protocols=["http_local"],
            evidence=evidence,
            classification_confidence=0.9,
            classification_reasoning="test",
            state="classified",
            matched_device_id=device_id,
        )
    )
    session.commit()

    return CanonicalAsset(
        asset_key=asset_id,
        asset_type="heat_pump",
        label="Dispatch target",
        device_id=device_id,
        control_capability="start_stop",
        eligibility="dispatchable",
        telemetry=dict(telemetry or {}),
        constraints={"max_power_kw": 2.5},
    )


def test_dispatch_uses_shelly_http_write_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_NATIVE_WRITES_ENABLED", "true")
    get_settings.cache_clear()
    session = _build_session(tmp_path, monkeypatch)
    try:
        asset = _create_dispatchable_target(
            session,
            device_id="dev-shelly-relay",
            asset_id="asset-shelly-relay",
            evidence={
                "http_base_url": "http://198.51.100.41",
                "dispatch_profile": "shelly_http_relay",
                "dispatch_generation": 2,
                "dispatch_channel": 0,
            },
        )

        def fake_post(url, json=None, timeout=None, verify=None):
            assert url == "http://198.51.100.41/rpc/Switch.Set"
            assert json == {"id": 0, "on": True}
            return httpx.Response(200, json={"was_on": False}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)

        now = datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc)
        records, violations = dispatch_current_interval(
            session,
            _site_model(asset),
            [
                PlannedInterval(
                    asset_key=asset.asset_key,
                    asset_type=asset.asset_type,
                    device_id=asset.device_id,
                    starts_at=now,
                    ends_at=now + timedelta(minutes=15),
                    command={"set_power_kw": 1.4},
                    predicted_state={},
                )
            ],
            execution_mode=HemsExecutionMode.GUARDED_AUTO.value,
            now=now,
        )

        assert not violations
        assert len(records) == 1
        assert records[0].outcome.status == "applied"
        assert records[0].outcome.details["adapter"] == "shelly_http_relay"
        device = session.get(Device, "dev-shelly-relay")
        assert device is not None
        assert device.telemetry["relay_output_on"] is True
    finally:
        session.close()
        get_settings.cache_clear()


def test_dispatch_uses_tasmota_http_write_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_NATIVE_WRITES_ENABLED", "true")
    get_settings.cache_clear()
    session = _build_session(tmp_path, monkeypatch, name="tasmota.db")
    try:
        asset = _create_dispatchable_target(
            session,
            device_id="dev-tasmota-relay",
            asset_id="asset-tasmota-relay",
            evidence={
                "http_base_url": "http://198.51.100.30",
                "dispatch_profile": "tasmota_http_power",
            },
        )

        def fake_get(url, params=None, timeout=None, verify=None):
            assert url == "http://198.51.100.30/cm"
            assert params == {"cmnd": "Power Off"}
            return httpx.Response(200, json={"POWER": "OFF"}, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", fake_get)

        now = datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc)
        records, violations = dispatch_current_interval(
            session,
            _site_model(asset),
            [
                PlannedInterval(
                    asset_key=asset.asset_key,
                    asset_type=asset.asset_type,
                    device_id=asset.device_id,
                    starts_at=now,
                    ends_at=now + timedelta(minutes=15),
                    command={"set_power_kw": 0.0},
                    predicted_state={},
                )
            ],
            execution_mode=HemsExecutionMode.GUARDED_AUTO.value,
            now=now,
        )

        assert not violations
        assert len(records) == 1
        assert records[0].outcome.status == "applied"
        assert records[0].outcome.details["adapter"] == "tasmota_http_power"
        device = session.get(Device, "dev-tasmota-relay")
        assert device is not None
        assert device.telemetry["relay_output_on"] is False
    finally:
        session.close()
        get_settings.cache_clear()


def test_dispatch_blocks_native_write_profile_when_native_writes_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_NATIVE_WRITES_ENABLED", "false")
    get_settings.cache_clear()
    session = _build_session(tmp_path, monkeypatch, name="blocked.db")
    try:
        asset = _create_dispatchable_target(
            session,
            device_id="dev-native-blocked",
            asset_id="asset-native-blocked",
            evidence={
                "http_base_url": "http://198.51.100.50",
                "dispatch_profile": "shelly_http_relay",
                "dispatch_generation": 2,
                "dispatch_channel": 0,
            },
        )

        now = datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc)
        records, violations = dispatch_current_interval(
            session,
            _site_model(asset),
            [
                PlannedInterval(
                    asset_key=asset.asset_key,
                    asset_type=asset.asset_type,
                    device_id=asset.device_id,
                    starts_at=now,
                    ends_at=now + timedelta(minutes=15),
                    command={"set_power_kw": 1.0},
                    predicted_state={},
                )
            ],
            execution_mode=HemsExecutionMode.GUARDED_AUTO.value,
            now=now,
        )

        assert len(records) == 1
        assert records[0].outcome.status == "blocked"
        assert violations[0]["violation_type"] == "missing_dispatch_adapter"
    finally:
        session.close()
        get_settings.cache_clear()


def test_dispatch_uses_sunspec_storage_write_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_NATIVE_WRITES_ENABLED", "true")
    get_settings.cache_clear()
    session = _build_session(tmp_path, monkeypatch, name="storage.db")
    try:
        asset = _create_dispatchable_target(
            session,
            device_id="dev-storage-write",
            asset_id="asset-storage-write",
            evidence={
                "modbus_host": "198.51.100.22",
                "modbus_unit_id": 1,
                "dispatch_profile": "sunspec_storage_basic_rate",
                "dispatch_model_id": 124,
                "sunspec_model_blocks": [{"model_id": 124, "length": 26, "start_register": 40100}],
            },
            telemetry={"max_charge_power_kw": 4.6, "max_discharge_power_kw": 5.0},
        )
        asset.asset_type = "battery"
        asset.control_capability = "set_power"
        asset.constraints = {"max_charge_kw": 4.6, "max_discharge_kw": 5.0}

        monkeypatch.setattr(
            "app.hems.write_adapters.read_sunspec_model_values",
            lambda host, unit_id, model_block, timeout: {"InOutWRte_SF": 0},
        )

        calls = {}

        def fake_write(host, unit_id, model_block, current_values, point_values, timeout):
            calls["host"] = host
            calls["unit_id"] = unit_id
            calls["model_id"] = model_block.model_id
            calls["points"] = point_values
            return True

        monkeypatch.setattr("app.hems.write_adapters.write_sunspec_model_points", fake_write)

        now = datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc)
        records, violations = dispatch_current_interval(
            session,
            _site_model(asset),
            [
                PlannedInterval(
                    asset_key=asset.asset_key,
                    asset_type=asset.asset_type,
                    device_id=asset.device_id,
                    starts_at=now,
                    ends_at=now + timedelta(minutes=15),
                    command={"set_power_kw": -2.3},
                    predicted_state={},
                )
            ],
            execution_mode=HemsExecutionMode.GUARDED_AUTO.value,
            now=now,
        )

        assert not violations
        assert records[0].outcome.status == "applied"
        assert records[0].outcome.details["adapter"] == "sunspec_storage_basic_rate"
        assert calls["host"] == "198.51.100.22"
        assert calls["model_id"] == 124
        assert calls["points"]["StorCtl_Mod"] == 1
        assert calls["points"]["InWRte"] == 50.0
        assert calls["points"]["OutWRte"] == 0.0
    finally:
        session.close()
        get_settings.cache_clear()


def test_dispatch_uses_sunspec_inverter_write_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_NATIVE_WRITES_ENABLED", "true")
    get_settings.cache_clear()
    session = _build_session(tmp_path, monkeypatch, name="inverter.db")
    try:
        asset = _create_dispatchable_target(
            session,
            device_id="dev-inverter-write",
            asset_id="asset-inverter-write",
            evidence={
                "modbus_host": "198.51.100.12",
                "modbus_unit_id": 1,
                "dispatch_profile": "sunspec_der_wmax_pct",
                "dispatch_model_id": 704,
                "sunspec_model_blocks": [{"model_id": 704, "length": 65, "start_register": 40200}],
            },
            telemetry={"power_rating_kw": 10.0, "curtailment_supported": True},
        )
        asset.asset_type = "pv_inverter"
        asset.control_capability = "set_power"
        asset.constraints = {"power_rating_kw": 10.0}

        monkeypatch.setattr(
            "app.hems.write_adapters.read_sunspec_model_values",
            lambda host, unit_id, model_block, timeout: {"WMaxLimPct_SF": 0},
        )

        calls = {}

        def fake_write(host, unit_id, model_block, current_values, point_values, timeout):
            calls["points"] = point_values
            calls["model_id"] = model_block.model_id
            return True

        monkeypatch.setattr("app.hems.write_adapters.write_sunspec_model_points", fake_write)

        now = datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc)
        records, violations = dispatch_current_interval(
            session,
            _site_model(asset),
            [
                PlannedInterval(
                    asset_key=asset.asset_key,
                    asset_type=asset.asset_type,
                    device_id=asset.device_id,
                    starts_at=now,
                    ends_at=now + timedelta(minutes=15),
                    command={"set_power_kw": 6.0},
                    predicted_state={},
                )
            ],
            execution_mode=HemsExecutionMode.GUARDED_AUTO.value,
            now=now,
        )

        assert not violations
        assert records[0].outcome.status == "applied"
        assert records[0].outcome.details["adapter"] == "sunspec_der_wmax_pct"
        assert calls["model_id"] == 704
        assert calls["points"]["WMaxLimPctEna"] == 1
        assert calls["points"]["WMaxLimPct"] == 60.0
    finally:
        session.close()
        get_settings.cache_clear()
