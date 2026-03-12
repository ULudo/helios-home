from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domain.enums import HemsDispatchStatus, HemsExecutionMode, HemsViolationSeverity
from app.hems.models import CanonicalAsset, DispatchOutcome, PlannedInterval, SiteModel
from app.hems.write_adapters import build_dispatch_target, resolve_write_adapter


@dataclass(slots=True)
class DispatchRecord:
    interval: PlannedInterval
    outcome: DispatchOutcome


def _clamped_command(asset: CanonicalAsset, interval: PlannedInterval) -> dict[str, float]:
    command_key, raw_value = next(iter(interval.command.items()))
    value = float(raw_value)
    if asset.asset_type == "battery":
        max_charge = float(asset.constraints.get("max_charge_kw", 0.0))
        max_discharge = float(asset.constraints.get("max_discharge_kw", 0.0))
        value = max(-max_charge, min(max_discharge, value))
    elif asset.asset_type == "ev_charger":
        value = max(0.0, min(float(asset.constraints.get("max_charge_kw", 11.0)), value))
    elif asset.asset_type == "heat_pump":
        value = max(0.0, min(float(asset.constraints.get("max_power_kw", 2.5)), value))
    elif asset.asset_type == "pv_inverter":
        value = max(0.0, value)
    return {command_key: round(value, 4)}


def _current_intervals(intervals: list[PlannedInterval], now: datetime) -> list[PlannedInterval]:
    current = [interval for interval in intervals if interval.starts_at <= now < interval.ends_at]
    if current:
        return current
    if not intervals:
        return []
    first_start = min(interval.starts_at for interval in intervals)
    return [interval for interval in intervals if interval.starts_at == first_start]


def dispatch_current_interval(
    session: Session,
    site_model: SiteModel,
    intervals: list[PlannedInterval],
    execution_mode: str,
    now: datetime | None = None,
) -> tuple[list[DispatchRecord], list[dict]]:
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    assets_by_key = {asset.asset_key: asset for asset in site_model.assets}
    records: list[DispatchRecord] = []
    violations: list[dict] = []

    for interval in _current_intervals(intervals, current_time):
        asset = assets_by_key.get(interval.asset_key)
        if asset is None:
            violations.append(
                {
                    "asset_key": interval.asset_key,
                    "severity": HemsViolationSeverity.WARNING.value,
                    "violation_type": "missing_asset_mapping",
                    "message": "The interval references an asset that is not present in the current site model.",
                    "details": {},
                }
            )
            continue

        requested_command = dict(interval.command)
        clamped_command = _clamped_command(asset, interval)
        effective_interval = PlannedInterval(
            asset_key=interval.asset_key,
            asset_type=interval.asset_type,
            device_id=interval.device_id,
            starts_at=interval.starts_at,
            ends_at=interval.ends_at,
            command=clamped_command,
            predicted_state=interval.predicted_state,
        )

        if execution_mode != HemsExecutionMode.GUARDED_AUTO.value:
            records.append(
                DispatchRecord(
                    interval=effective_interval,
                    outcome=DispatchOutcome(
                        status=HemsDispatchStatus.SKIPPED.value,
                        requested_command=requested_command,
                        applied_command={},
                        summary="The plan was generated in plan-only mode, so no dispatch was attempted.",
                    ),
                )
            )
            continue

        target = build_dispatch_target(session, asset)
        adapter = resolve_write_adapter(session, target, effective_interval)
        if adapter is None:
            records.append(
                DispatchRecord(
                    interval=effective_interval,
                    outcome=DispatchOutcome(
                        status=HemsDispatchStatus.BLOCKED.value,
                        requested_command=requested_command,
                        applied_command={},
                        summary="No validated dispatch adapter is available for this asset yet.",
                        details={"clamped_command": clamped_command},
                    ),
                )
            )
            violations.append(
                {
                    "asset_key": asset.asset_key,
                    "severity": HemsViolationSeverity.WARNING.value,
                    "violation_type": "missing_dispatch_adapter",
                    "message": "Guarded auto skipped dispatch because no adapter is available.",
                    "details": {"requested_command": requested_command, "clamped_command": clamped_command},
                }
            )
            continue

        outcome = adapter.apply(target, effective_interval)
        outcome.requested_command = requested_command
        if not outcome.applied_command:
            outcome.applied_command = clamped_command
        records.append(DispatchRecord(interval=effective_interval, outcome=outcome))

    return records, violations
