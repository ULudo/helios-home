from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Asset, Device, DeviceCandidate, Site
from app.domain.enums import HemsAssetType, HemsControlCapability
from app.hems.dispatchability import assess_dispatchability
from app.hems.forecast import build_default_forecast
from app.hems.models import CanonicalAsset, ForecastBundle, SiteModel
from app.hems.policy import get_or_create_hems_policy


def _numeric_value(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw_value = payload.get(key)
        if isinstance(raw_value, (int, float)):
            return float(raw_value)
        if isinstance(raw_value, str):
            try:
                return float(raw_value)
            except ValueError:
                continue
    return None


def _bool_value(payload: dict[str, Any], *keys: str) -> bool | None:
    for key in keys:
        raw_value = payload.get(key)
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, str):
            lowered = raw_value.strip().lower()
            if lowered in {"true", "on", "yes", "1"}:
                return True
            if lowered in {"false", "off", "no", "0"}:
                return False
    return None


def _map_asset_type(asset_type: str, *, device: Device | None, evidence: dict[str, Any]) -> str:
    if asset_type == "wallbox":
        return HemsAssetType.EV_CHARGER.value
    if asset_type == "smart_appliance":
        capabilities = dict(device.capabilities or {}) if device is not None else {}
        telemetry = dict(device.telemetry or {}) if device is not None else {}
        if bool(capabilities.get("controllable")) or bool(evidence.get("dispatch_profile")) or bool(telemetry.get("simulation_supported")):
            return HemsAssetType.CONTROLLABLE_LOAD.value
        return HemsAssetType.UNCONTROLLED_LOAD.value
    if asset_type in {item.value for item in HemsAssetType}:
        return asset_type
    return HemsAssetType.UNCONTROLLED_LOAD.value


def _control_capability(asset_type: str, telemetry: dict[str, Any], controllable: bool) -> str:
    if not controllable:
        return HemsControlCapability.MONITOR_ONLY.value
    if asset_type in {HemsAssetType.BATTERY.value, HemsAssetType.PV_INVERTER.value}:
        return HemsControlCapability.SET_POWER.value
    if asset_type == HemsAssetType.EV_CHARGER.value:
        if _numeric_value(telemetry, "current_a", "max_current_a") is not None:
            return HemsControlCapability.SET_CURRENT.value
        return HemsControlCapability.SET_POWER.value
    if asset_type == HemsAssetType.HEAT_PUMP.value:
        if any(key in telemetry for key in ("sg_ready_mode", "operating_mode", "mode")):
            return HemsControlCapability.SET_MODE.value
        return HemsControlCapability.START_STOP.value
    if asset_type == HemsAssetType.CONTROLLABLE_LOAD.value:
        return HemsControlCapability.START_STOP.value
    return HemsControlCapability.MONITOR_ONLY.value


def _next_departure_datetime(now: datetime, departure_time: str) -> datetime:
    hour, minute = (int(part) for part in departure_time.split(":", 1))
    candidate = now.astimezone(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now.astimezone(timezone.utc):
        candidate += timedelta(days=1)
    return candidate


def _battery_constraints(telemetry: dict[str, Any], reserve_pct: float) -> dict[str, Any]:
    soc_pct = _numeric_value(telemetry, "soc_pct") or 50.0
    available_capacity_kwh = _numeric_value(telemetry, "available_capacity_kwh", "usable_capacity_kwh")
    if available_capacity_kwh is not None and soc_pct > 0.0:
        capacity_kwh = max(available_capacity_kwh / max(soc_pct / 100.0, 0.05), available_capacity_kwh)
    else:
        capacity_kwh = 10.0
    max_charge_power_kw = _numeric_value(telemetry, "max_charge_power_kw")
    max_discharge_power_kw = _numeric_value(telemetry, "max_discharge_power_kw")
    observed_power = abs(_numeric_value(telemetry, "power_kw") or 0.0)
    return {
        "soc_pct": soc_pct,
        "capacity_kwh": round(capacity_kwh, 3),
        "reserve_floor_pct": reserve_pct,
        "max_charge_kw": round(max(max_charge_power_kw or 0.0, observed_power, 3.0), 3),
        "max_discharge_kw": round(max(max_discharge_power_kw or max_charge_power_kw or 0.0, observed_power, 3.0), 3),
        "roundtrip_efficiency": 0.92,
    }


def _ev_constraints(telemetry: dict[str, Any], target_soc_pct: float, departure_time: str, now: datetime) -> dict[str, Any]:
    current_soc_pct = _numeric_value(telemetry, "current_soc_pct", "soc_pct")
    max_charge_kw = _numeric_value(telemetry, "max_charge_kw")
    if max_charge_kw is None:
        max_current_a = _numeric_value(telemetry, "max_current_a", "current_a")
        max_charge_kw = round((max_current_a * 230.0 / 1000.0), 3) if max_current_a is not None else 11.0
    return {
        "connected": _bool_value(telemetry, "vehicle_connected", "connected", "plugged_in") is not False,
        "current_soc_pct": current_soc_pct,
        "battery_capacity_kwh": _numeric_value(telemetry, "vehicle_battery_capacity_kwh") or 60.0,
        "target_soc_pct": target_soc_pct,
        "departure_at": _next_departure_datetime(now, departure_time).isoformat(),
        "max_charge_kw": max_charge_kw,
        "charge_efficiency": 0.95,
    }


def _heat_pump_constraints(telemetry: dict[str, Any], comfort_min_c: float, comfort_max_c: float) -> dict[str, Any]:
    current_temperature = _numeric_value(
        telemetry,
        "room_temperature_c",
        "buffer_temperature_c",
        "flow_temperature_c",
        "temperature_c",
    )
    max_power_kw = max(_numeric_value(telemetry, "electrical_power_kw", "power_kw", "thermal_output_kw") or 0.0, 2.5)
    return {
        "current_temperature_c": current_temperature,
        "comfort_min_c": comfort_min_c,
        "comfort_max_c": comfort_max_c,
        "max_power_kw": max_power_kw,
        "thermal_gain_c_per_kwh": 0.45,
        "thermal_loss_per_hour": 0.08,
    }


def _pv_constraints(telemetry: dict[str, Any]) -> dict[str, Any]:
    current_power_kw = _numeric_value(telemetry, "power_kw")
    if current_power_kw is None:
        power_w = _numeric_value(telemetry, "power_w")
        current_power_kw = power_w / 1000.0 if power_w is not None else 0.0
    return {
        "current_power_kw": current_power_kw,
        "power_rating_kw": _numeric_value(telemetry, "power_rating_kw"),
        "curtailment_supported": _bool_value(telemetry, "curtailment_supported") is True,
    }


def _controllable_load_constraints(telemetry: dict[str, Any], step_minutes: int) -> dict[str, Any]:
    power_kw = _numeric_value(telemetry, "power_kw")
    if power_kw is None:
        power_w = _numeric_value(telemetry, "power_w")
        power_kw = power_w / 1000.0 if power_w is not None else None
    runtime_target_steps = int(_numeric_value(telemetry, "runtime_target_steps") or 0)
    runtime_target_hours = _numeric_value(telemetry, "runtime_target_hours", "preferred_runtime_hours")
    if runtime_target_steps <= 0 and runtime_target_hours is not None and runtime_target_hours > 0:
        runtime_target_steps = max(1, int(round(runtime_target_hours * 60.0 / step_minutes)))
    minimum_on_minutes = _numeric_value(telemetry, "minimum_on_minutes", "minimum_runtime_minutes")
    return {
        "nominal_power_kw": round(max(power_kw or 0.25, 0.25), 4),
        "runtime_target_steps": runtime_target_steps,
        "minimum_on_minutes": int(max(minimum_on_minutes or step_minutes, step_minutes)),
    }


def _asset_constraints(asset_type: str, telemetry: dict[str, Any], policy, now: datetime) -> dict[str, Any]:
    if asset_type == HemsAssetType.BATTERY.value:
        return _battery_constraints(telemetry, policy.battery_reserve_pct)
    if asset_type == HemsAssetType.EV_CHARGER.value:
        return _ev_constraints(telemetry, policy.ev_default_target_soc_pct, policy.ev_default_departure_time, now)
    if asset_type == HemsAssetType.HEAT_PUMP.value:
        return _heat_pump_constraints(telemetry, policy.heat_comfort_min_c, policy.heat_comfort_max_c)
    if asset_type == HemsAssetType.PV_INVERTER.value:
        return _pv_constraints(telemetry)
    if asset_type == HemsAssetType.CONTROLLABLE_LOAD.value:
        return _controllable_load_constraints(telemetry, policy.step_minutes)
    return {}


def _canonical_asset(
    asset: Asset,
    device: Device | None,
    evidence: dict[str, Any],
    policy,
    now: datetime,
    *,
    native_writes_enabled: bool,
) -> CanonicalAsset:
    asset_type = _map_asset_type(asset.asset_type, device=device, evidence=evidence)
    telemetry = dict(device.telemetry if device is not None else asset.metrics or {})
    constraints = _asset_constraints(asset_type, telemetry, policy, now)
    control_capability = _control_capability(asset_type, telemetry, bool(device and device.capabilities.get("controllable")))
    dispatchability = assess_dispatchability(
        device=device,
        asset_type=asset_type,
        control_capability=control_capability,
        telemetry=telemetry,
        constraints=constraints,
        evidence=evidence,
        native_writes_enabled=native_writes_enabled,
    )
    return CanonicalAsset(
        asset_key=asset.id,
        asset_type=asset_type,
        label=device.name if device is not None else asset.name,
        device_id=device.id if device is not None else None,
        control_capability=control_capability,
        eligibility=dispatchability.eligibility,
        telemetry=telemetry,
        constraints=constraints,
        command_contract=dispatchability.command_contract,
        reasons=dispatchability.reasons,
    )


def build_site_model(
    session: Session,
    now: datetime | None = None,
    forecast_override: ForecastBundle | None = None,
) -> SiteModel:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    policy = get_or_create_hems_policy(session)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    native_writes_enabled = get_settings().native_writes_enabled

    assets = session.scalars(select(Asset).order_by(Asset.asset_type, Asset.name)).all()
    devices = session.scalars(select(Device).order_by(Device.name)).all()
    devices_by_id = {device.id: device for device in devices}
    candidate_evidence_by_device_id = {
        candidate.matched_device_id: dict(candidate.evidence or {})
        for candidate in session.scalars(select(DeviceCandidate).order_by(DeviceCandidate.updated_at.desc())).all()
        if candidate.matched_device_id
    }

    canonical_assets: list[CanonicalAsset] = []
    for asset in assets:
        device = None
        for device_id in asset.device_ids or []:
            device = devices_by_id.get(device_id)
            if device is not None:
                break
        evidence = dict(candidate_evidence_by_device_id.get(device.id, {}) if device is not None else {})
        canonical_assets.append(
            _canonical_asset(
                asset,
                device,
                evidence,
                policy,
                current_time,
                native_writes_enabled=native_writes_enabled,
            )
        )

    forecast = forecast_override or build_default_forecast(
        canonical_assets,
        current_time,
        horizon_hours=policy.horizon_hours,
        step_minutes=policy.step_minutes,
    )
    return SiteModel(
        built_at=current_time,
        policy={
            "execution_mode": policy.execution_mode,
            "battery_reserve_pct": policy.battery_reserve_pct,
            "ev_default_target_soc_pct": policy.ev_default_target_soc_pct,
            "ev_default_departure_time": policy.ev_default_departure_time,
            "heat_comfort_min_c": policy.heat_comfort_min_c,
            "heat_comfort_max_c": policy.heat_comfort_max_c,
            "grid_import_limit_kw": policy.grid_import_limit_kw,
            "grid_export_limit_kw": policy.grid_export_limit_kw,
            "allow_price_arbitrage": policy.allow_price_arbitrage,
            "allow_heat_precharge": policy.allow_heat_precharge,
            "allow_ev_load_shifting": policy.allow_ev_load_shifting,
            "horizon_hours": policy.horizon_hours,
            "step_minutes": policy.step_minutes,
        },
        assets=canonical_assets,
        grid_constraints={
            "import_limit_kw": policy.grid_import_limit_kw,
            "export_limit_kw": policy.grid_export_limit_kw,
        },
        forecast=forecast,
    )
