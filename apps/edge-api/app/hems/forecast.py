from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import sin, pi

from app.hems.models import CanonicalAsset, ForecastBundle


def align_horizon_start(now: datetime, step_minutes: int) -> datetime:
    minute_bucket = (now.minute // step_minutes) * step_minutes
    return now.replace(minute=minute_bucket, second=0, microsecond=0)


def _daylight_profile(hour: float) -> float:
    if hour < 6.0 or hour > 20.0:
        return 0.0
    normalized = (hour - 6.0) / 14.0
    return max(0.0, sin(pi * normalized))


def _heuristic_import_price(hour: float) -> float:
    if hour < 5.0:
        return 0.22
    if hour < 16.0:
        return 0.30
    if hour < 21.0:
        return 0.36
    return 0.28


def _heuristic_ambient_temperature(hour: float) -> float:
    return 8.0 + 4.0 * sin((hour - 6.0) / 24.0 * 2.0 * pi)


def _current_pv_power_kw(assets: list[CanonicalAsset]) -> float:
    total = 0.0
    for asset in assets:
        if asset.asset_type != "pv_inverter":
            continue
        for key in ("power_kw", "power_w"):
            raw_value = asset.telemetry.get(key)
            if isinstance(raw_value, (int, float)):
                total += float(raw_value) if key.endswith("_kw") else float(raw_value) / 1000.0
                break
    return total


def _current_grid_import_kw(assets: list[CanonicalAsset]) -> float:
    for asset in assets:
        if asset.asset_type != "grid_meter":
            continue
        raw_value = asset.telemetry.get("grid_power_kw")
        if isinstance(raw_value, (int, float)):
            return max(float(raw_value), 0.0)
        raw_value = asset.telemetry.get("power_kw")
        if isinstance(raw_value, (int, float)):
            return max(float(raw_value), 0.0)
    return 0.8


def build_default_forecast(
    assets: list[CanonicalAsset],
    now: datetime,
    horizon_hours: int,
    step_minutes: int,
) -> ForecastBundle:
    horizon_start = align_horizon_start(now.astimezone(timezone.utc), step_minutes)
    steps = max(1, int(horizon_hours * 60 / step_minutes))
    step_hours = step_minutes / 60.0
    current_pv_kw = _current_pv_power_kw(assets)
    pv_peak_guess_kw = max(current_pv_kw, 3.5) if any(asset.asset_type == "pv_inverter" for asset in assets) else 0.0
    grid_import_kw = _current_grid_import_kw(assets)

    import_prices: list[float] = []
    export_prices: list[float] = []
    pv_generation: list[float] = []
    base_load: list[float] = []
    ambient_temperature: list[float] = []

    for step in range(steps):
        current_time = horizon_start + timedelta(minutes=step * step_minutes)
        hour = current_time.hour + current_time.minute / 60.0
        import_prices.append(_heuristic_import_price(hour))
        export_prices.append(0.08)
        pv_generation.append(pv_peak_guess_kw * _daylight_profile(hour))
        base_load.append(max(grid_import_kw, 0.4) + (0.15 if 18.0 <= hour <= 22.0 else 0.0))
        ambient_temperature.append(_heuristic_ambient_temperature(hour))

    return ForecastBundle(
        horizon_start=horizon_start,
        step_minutes=step_minutes,
        import_price_eur_per_kwh=import_prices,
        export_price_eur_per_kwh=export_prices,
        pv_generation_kw=pv_generation,
        base_load_kw=base_load,
        ambient_temperature_c=ambient_temperature,
        notes={
            "source": "heuristic_local_defaults",
            "step_hours": step_hours,
            "pv_peak_guess_kw": pv_peak_guess_kw,
        },
    )
