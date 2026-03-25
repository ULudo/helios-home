from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

try:
    import cvxpy as cp
    import numpy as np
except ImportError:  # pragma: no cover - guarded by dependency installation and runtime checks
    cp = None
    np = None

from app.domain.enums import HemsEligibility, HemsExecutionMode, HemsPlanStatus, HemsViolationSeverity
from app.hems.models import CanonicalAsset, PlannedInterval, PlanningResult, SiteModel


def _require_solver() -> tuple[object, object]:
    if cp is None or np is None:
        raise RuntimeError("The HEMS planner requires cvxpy and numpy to be installed.")
    return cp, np


def _asset_command_key(asset: CanonicalAsset) -> str:
    if asset.command_contract is not None and asset.command_contract.command_key:
        return asset.command_contract.command_key
    if asset.asset_type == "battery":
        return "set_power_kw"
    if asset.asset_type == "ev_charger":
        return "set_charge_kw"
    if asset.asset_type == "heat_pump":
        return "set_power_kw"
    if asset.asset_type == "pv_inverter":
        return "set_power_kw"
    return "setpoint"


def _float_series(values) -> list[float]:
    return [float(value) for value in values]


def _sum_expressions(expressions):
    if not expressions:
        return 0.0
    combined = expressions[0]
    for expression in expressions[1:]:
        combined = combined + expression
    return combined


def solve_site_plan(site_model: SiteModel) -> PlanningResult:
    cvxpy, numpy = _require_solver()
    forecast = site_model.forecast
    assets = site_model.assets
    steps = len(forecast.import_price_eur_per_kwh)
    dt_hours = forecast.step_minutes / 60.0

    dispatchable_assets = [asset for asset in assets if asset.eligibility == HemsEligibility.DISPATCHABLE.value]
    if not dispatchable_assets:
        return PlanningResult(
            status=HemsPlanStatus.DEGRADED.value,
            execution_mode=site_model.policy["execution_mode"],
            solver_name="cvxpy-highs",
            summary="No dispatchable assets are available yet. The HEMS core kept the site in plan-only mode.",
            objective_value=None,
            horizon_start=forecast.horizon_start,
            horizon_end=forecast.horizon_start + timedelta(minutes=steps * forecast.step_minutes),
            input_snapshot={
                "policy": site_model.policy,
                "forecast": forecast.notes,
                "asset_keys": [asset.asset_key for asset in assets],
            },
            output_snapshot={},
            intervals=[],
            violations=[
                {
                    "asset_key": None,
                    "severity": HemsViolationSeverity.WARNING.value,
                    "violation_type": "no_dispatchable_assets",
                    "message": "No asset currently satisfies guarded auto execution.",
                    "details": {
                        "plan_only_assets": [asset.asset_key for asset in assets if asset.eligibility == HemsEligibility.PLAN_ONLY.value],
                        "blocked_assets": [asset.asset_key for asset in assets if asset.eligibility == HemsEligibility.BLOCKED.value],
                    },
                }
            ],
        )

    import_prices = numpy.array(forecast.import_price_eur_per_kwh, dtype=float)
    export_prices = numpy.array(forecast.export_price_eur_per_kwh, dtype=float)
    pv_generation = numpy.array(forecast.pv_generation_kw, dtype=float)
    base_load = numpy.array(forecast.base_load_kw, dtype=float)
    ambient_temperature = numpy.array(forecast.ambient_temperature_c, dtype=float)

    constraints = []
    objective_terms = []
    violations: list[dict] = []
    intervals: list[PlannedInterval] = []

    grid_import = cvxpy.Variable(steps, nonneg=True, name="grid_import_kw")
    grid_export = cvxpy.Variable(steps, nonneg=True, name="grid_export_kw")
    constraints += [
        grid_import <= float(site_model.grid_constraints["import_limit_kw"]),
        grid_export <= float(site_model.grid_constraints["export_limit_kw"]),
    ]
    dispatchable_pv_assets = [asset for asset in dispatchable_assets if asset.asset_type == "pv_inverter"]
    net_supply_terms = [grid_import - grid_export]
    if not dispatchable_pv_assets:
        net_supply_terms.append(pv_generation)
    net_demand_terms = [base_load]

    battery_outputs: dict[str, dict[str, object]] = {}
    ev_outputs: dict[str, dict[str, object]] = {}
    heat_outputs: dict[str, dict[str, object]] = {}
    load_outputs: dict[str, dict[str, object]] = {}
    pv_outputs: dict[str, dict[str, object]] = {}

    for asset in dispatchable_assets:
        if asset.asset_type == "battery":
            constraint_set = asset.constraints
            charge_kw = cvxpy.Variable(steps, nonneg=True, name=f"{asset.asset_key}_charge_kw")
            discharge_kw = cvxpy.Variable(steps, nonneg=True, name=f"{asset.asset_key}_discharge_kw")
            soc_pct = cvxpy.Variable(steps + 1, name=f"{asset.asset_key}_soc_pct")
            capacity_kwh = float(constraint_set.get("capacity_kwh", 10.0))
            reserve_floor_pct = float(constraint_set.get("reserve_floor_pct", 20.0))
            roundtrip_efficiency = float(constraint_set.get("roundtrip_efficiency", 0.92))
            sqrt_efficiency = round(roundtrip_efficiency ** 0.5, 6)
            constraints += [
                charge_kw <= float(constraint_set.get("max_charge_kw", 3.0)),
                discharge_kw <= float(constraint_set.get("max_discharge_kw", 3.0)),
                soc_pct[0] == float(constraint_set.get("soc_pct", 50.0)),
                soc_pct >= reserve_floor_pct,
                soc_pct <= 100.0,
            ]
            for step in range(steps):
                constraints.append(
                    soc_pct[step + 1]
                    == soc_pct[step]
                    + ((charge_kw[step] * sqrt_efficiency) - (discharge_kw[step] / sqrt_efficiency)) * dt_hours / capacity_kwh * 100.0
                )
            objective_terms.append(0.01 * cvxpy.sum(charge_kw + discharge_kw) * dt_hours)
            net_supply_terms.append(discharge_kw - charge_kw)
            battery_outputs[asset.asset_key] = {
                "charge_kw": charge_kw,
                "discharge_kw": discharge_kw,
                "soc_pct": soc_pct,
            }

        elif asset.asset_type == "ev_charger":
            constraint_set = asset.constraints
            charge_kw = cvxpy.Variable(steps, nonneg=True, name=f"{asset.asset_key}_charge_kw")
            constraints.append(charge_kw <= float(constraint_set.get("max_charge_kw", 11.0)))
            connected = bool(constraint_set.get("connected", True))
            if not connected:
                constraints.append(charge_kw == 0.0)
            current_soc_pct = constraint_set.get("current_soc_pct")
            target_soc_pct = float(constraint_set.get("target_soc_pct", site_model.policy["ev_default_target_soc_pct"]))
            battery_capacity_kwh = float(constraint_set.get("battery_capacity_kwh", 60.0))
            target_energy_kwh = 0.0
            if isinstance(current_soc_pct, (int, float)):
                target_energy_kwh = max((target_soc_pct - float(current_soc_pct)) / 100.0 * battery_capacity_kwh, 0.0)
            departure_at = constraint_set.get("departure_at")
            deadline_index = steps
            if isinstance(departure_at, str):
                departure_dt = datetime.fromisoformat(departure_at)
                deadline_index = min(steps, max(1, ceil((departure_dt - forecast.horizon_start).total_seconds() / 60.0 / forecast.step_minutes)))
            deficit_kwh = cvxpy.Variable(nonneg=True, name=f"{asset.asset_key}_deficit_kwh")
            constraints.append(cvxpy.sum(charge_kw[:deadline_index]) * dt_hours * float(constraint_set.get("charge_efficiency", 0.95)) + deficit_kwh >= target_energy_kwh)
            objective_terms.append(250.0 * deficit_kwh)
            objective_terms.append(0.005 * cvxpy.sum(charge_kw) * dt_hours)
            net_demand_terms.append(charge_kw)
            ev_outputs[asset.asset_key] = {
                "charge_kw": charge_kw,
                "deficit_kwh": deficit_kwh,
                "deadline_index": deadline_index,
            }

        elif asset.asset_type == "heat_pump":
            constraint_set = asset.constraints
            heat_kw = cvxpy.Variable(steps, nonneg=True, name=f"{asset.asset_key}_heat_kw")
            temperature_c = cvxpy.Variable(steps + 1, name=f"{asset.asset_key}_temperature_c")
            lower_slack = cvxpy.Variable(steps, nonneg=True, name=f"{asset.asset_key}_temp_low_slack")
            upper_slack = cvxpy.Variable(steps, nonneg=True, name=f"{asset.asset_key}_temp_high_slack")
            constraints += [
                heat_kw <= float(constraint_set.get("max_power_kw", 2.5)),
                temperature_c[0] == float(constraint_set.get("current_temperature_c", site_model.policy["heat_comfort_min_c"])),
            ]
            thermal_gain = float(constraint_set.get("thermal_gain_c_per_kwh", 0.45))
            thermal_loss = float(constraint_set.get("thermal_loss_per_hour", 0.08))
            comfort_min = float(constraint_set.get("comfort_min_c", site_model.policy["heat_comfort_min_c"]))
            comfort_max = float(constraint_set.get("comfort_max_c", site_model.policy["heat_comfort_max_c"]))
            for step in range(steps):
                constraints.append(
                    temperature_c[step + 1]
                    == temperature_c[step]
                    + heat_kw[step] * thermal_gain * dt_hours
                    - (temperature_c[step] - ambient_temperature[step]) * thermal_loss * dt_hours
                )
                constraints.append(temperature_c[step + 1] + lower_slack[step] >= comfort_min)
                constraints.append(temperature_c[step + 1] - upper_slack[step] <= comfort_max)
            objective_terms.append(75.0 * cvxpy.sum(lower_slack + upper_slack))
            objective_terms.append(0.01 * cvxpy.sum(heat_kw) * dt_hours)
            net_demand_terms.append(heat_kw)
            heat_outputs[asset.asset_key] = {
                "heat_kw": heat_kw,
                "temperature_c": temperature_c,
                "lower_slack": lower_slack,
                "upper_slack": upper_slack,
            }

        elif asset.asset_type == "controllable_load":
            constraint_set = asset.constraints
            nominal_power_kw = float(constraint_set.get("nominal_power_kw", 0.25))
            runtime_target_steps = max(0, int(constraint_set.get("runtime_target_steps", 0)))
            minimum_on_steps = max(1, ceil(float(constraint_set.get("minimum_on_minutes", forecast.step_minutes)) / forecast.step_minutes))

            on_state = cvxpy.Variable(steps, boolean=True, name=f"{asset.asset_key}_on_state")
            start_state = cvxpy.Variable(steps, boolean=True, name=f"{asset.asset_key}_start_state")
            power_kw = nominal_power_kw * on_state
            constraints += [
                start_state[0] >= on_state[0],
                start_state[0] <= on_state[0],
            ]
            for step in range(1, steps):
                constraints.append(start_state[step] >= on_state[step] - on_state[step - 1])
                constraints.append(start_state[step] <= 1 - on_state[step - 1])
                constraints.append(start_state[step] <= on_state[step])

            for step in range(steps):
                if step + minimum_on_steps > steps:
                    constraints.append(start_state[step] == 0)
                    continue
                for hold_step in range(step, min(steps, step + minimum_on_steps)):
                    constraints.append(on_state[hold_step] >= start_state[step])

            runtime_shortfall = None
            if runtime_target_steps > 0:
                runtime_shortfall = cvxpy.Variable(nonneg=True, name=f"{asset.asset_key}_runtime_shortfall")
                constraints.append(cvxpy.sum(on_state) + runtime_shortfall >= runtime_target_steps)
                objective_terms.append(20.0 * runtime_shortfall)
            objective_terms.append(0.02 * cvxpy.sum(start_state))
            net_demand_terms.append(power_kw)
            load_outputs[asset.asset_key] = {
                "on_state": on_state,
                "power_kw": power_kw,
                "start_state": start_state,
                "runtime_shortfall": runtime_shortfall,
                "runtime_target_steps": runtime_target_steps,
            }

        elif asset.asset_type == "pv_inverter":
            share_profile = pv_generation / max(len(dispatchable_pv_assets), 1)
            power_limit_kw = float(asset.constraints.get("power_rating_kw") or max(float(numpy.max(share_profile)) if len(share_profile) else 0.0, 0.0))
            pv_output_kw = cvxpy.Variable(steps, nonneg=True, name=f"{asset.asset_key}_pv_output_kw")
            constraints.append(pv_output_kw <= share_profile)
            if power_limit_kw > 0.0:
                constraints.append(pv_output_kw <= power_limit_kw)
            objective_terms.append(0.02 * cvxpy.sum(share_profile - pv_output_kw) * dt_hours)
            net_supply_terms.append(pv_output_kw)
            pv_outputs[asset.asset_key] = {
                "pv_output_kw": pv_output_kw,
                "available_profile_kw": share_profile,
            }

    constraints.append(_sum_expressions(net_supply_terms) == _sum_expressions(net_demand_terms))
    objective = cvxpy.Minimize(
        cvxpy.sum(cvxpy.multiply(import_prices, grid_import) * dt_hours)
        - cvxpy.sum(cvxpy.multiply(export_prices, grid_export) * dt_hours)
        + _sum_expressions(objective_terms)
    )
    problem = cvxpy.Problem(objective, constraints)

    try:
        problem.solve(solver=cvxpy.HIGHS, verbose=False)
    except Exception as exc:  # pragma: no cover - solver-specific failure path
        return PlanningResult(
            status=HemsPlanStatus.FAILED.value,
            execution_mode=site_model.policy["execution_mode"],
            solver_name="cvxpy-highs",
            summary="The HEMS planner failed before producing a schedule.",
            objective_value=None,
            horizon_start=forecast.horizon_start,
            horizon_end=forecast.horizon_start + timedelta(minutes=steps * forecast.step_minutes),
            input_snapshot={"policy": site_model.policy, "forecast": forecast.notes},
            output_snapshot={},
            intervals=[],
            violations=[
                {
                    "asset_key": None,
                    "severity": HemsViolationSeverity.HIGH.value,
                    "violation_type": "solver_failure",
                    "message": str(exc),
                    "details": {},
                }
            ],
        )

    status = problem.status
    if status not in {cvxpy.OPTIMAL, cvxpy.OPTIMAL_INACCURATE}:
        return PlanningResult(
            status=HemsPlanStatus.DEGRADED.value,
            execution_mode=site_model.policy["execution_mode"],
            solver_name="cvxpy-highs",
            summary=f"The HEMS planner returned {status} and did not produce a trusted dispatch plan.",
            objective_value=float(problem.value) if problem.value is not None else None,
            horizon_start=forecast.horizon_start,
            horizon_end=forecast.horizon_start + timedelta(minutes=steps * forecast.step_minutes),
            input_snapshot={"policy": site_model.policy, "forecast": forecast.notes},
            output_snapshot={"solver_status": status},
            intervals=[],
            violations=[
                {
                    "asset_key": None,
                    "severity": HemsViolationSeverity.WARNING.value,
                    "violation_type": "solver_untrusted_status",
                    "message": f"Planner returned {status}.",
                    "details": {},
                }
            ],
        )

    for asset in dispatchable_assets:
        if asset.asset_key in battery_outputs:
            output = battery_outputs[asset.asset_key]
            charge_series = _float_series(output["charge_kw"].value)
            discharge_series = _float_series(output["discharge_kw"].value)
            soc_series = _float_series(output["soc_pct"].value)
            for step in range(steps):
                starts_at = forecast.horizon_start + timedelta(minutes=step * forecast.step_minutes)
                ends_at = starts_at + timedelta(minutes=forecast.step_minutes)
                set_power_kw = round(discharge_series[step] - charge_series[step], 4)
                intervals.append(
                    PlannedInterval(
                        asset_key=asset.asset_key,
                        asset_type=asset.asset_type,
                        device_id=asset.device_id,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        command={_asset_command_key(asset): set_power_kw},
                        predicted_state={"soc_pct": round(soc_series[step + 1], 3)},
                    )
                )

        elif asset.asset_key in ev_outputs:
            output = ev_outputs[asset.asset_key]
            charge_series = _float_series(output["charge_kw"].value)
            deficit_kwh = float(output["deficit_kwh"].value)
            if deficit_kwh > 0.05:
                violations.append(
                    {
                        "asset_key": asset.asset_key,
                        "severity": HemsViolationSeverity.WARNING.value,
                        "violation_type": "ev_target_unmet",
                        "message": "The EV target could not be fully met within the configured deadline.",
                        "details": {"deficit_kwh": round(deficit_kwh, 3)},
                    }
                )
            delivered_kwh = 0.0
            for step in range(steps):
                starts_at = forecast.horizon_start + timedelta(minutes=step * forecast.step_minutes)
                ends_at = starts_at + timedelta(minutes=forecast.step_minutes)
                delivered_kwh += charge_series[step] * dt_hours * float(asset.constraints.get("charge_efficiency", 0.95))
                intervals.append(
                    PlannedInterval(
                        asset_key=asset.asset_key,
                        asset_type=asset.asset_type,
                        device_id=asset.device_id,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        command={_asset_command_key(asset): round(charge_series[step], 4)},
                        predicted_state={"delivered_energy_kwh": round(delivered_kwh, 3)},
                    )
                )

        elif asset.asset_key in heat_outputs:
            output = heat_outputs[asset.asset_key]
            heat_series = _float_series(output["heat_kw"].value)
            temperature_series = _float_series(output["temperature_c"].value)
            lower_slack = _float_series(output["lower_slack"].value)
            upper_slack = _float_series(output["upper_slack"].value)
            if any(value > 0.05 for value in lower_slack + upper_slack):
                violations.append(
                    {
                        "asset_key": asset.asset_key,
                        "severity": HemsViolationSeverity.WARNING.value,
                        "violation_type": "comfort_band_soft_violation",
                        "message": "The heat-pump comfort envelope required slack in at least one interval.",
                        "details": {
                            "lower_slack_max_c": round(max(lower_slack), 3),
                            "upper_slack_max_c": round(max(upper_slack), 3),
                        },
                    }
                )
            for step in range(steps):
                starts_at = forecast.horizon_start + timedelta(minutes=step * forecast.step_minutes)
                ends_at = starts_at + timedelta(minutes=forecast.step_minutes)
                intervals.append(
                    PlannedInterval(
                        asset_key=asset.asset_key,
                        asset_type=asset.asset_type,
                        device_id=asset.device_id,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        command={_asset_command_key(asset): round(heat_series[step], 4)},
                        predicted_state={"temperature_c": round(temperature_series[step + 1], 3)},
                    )
                )

        elif asset.asset_key in load_outputs:
            output = load_outputs[asset.asset_key]
            on_series = _float_series(output["on_state"].value)
            power_series = _float_series(output["power_kw"].value)
            runtime_shortfall = output["runtime_shortfall"]
            if runtime_shortfall is not None and float(runtime_shortfall.value) > 0.05:
                violations.append(
                    {
                        "asset_key": asset.asset_key,
                        "severity": HemsViolationSeverity.WARNING.value,
                        "violation_type": "runtime_target_soft_violation",
                        "message": "The controllable load could not satisfy its requested runtime target within the planning horizon.",
                        "details": {
                            "runtime_shortfall_steps": round(float(runtime_shortfall.value), 3),
                            "runtime_target_steps": int(output["runtime_target_steps"]),
                        },
                    }
                )
            for step in range(steps):
                starts_at = forecast.horizon_start + timedelta(minutes=step * forecast.step_minutes)
                ends_at = starts_at + timedelta(minutes=forecast.step_minutes)
                is_on = on_series[step] >= 0.5
                intervals.append(
                    PlannedInterval(
                        asset_key=asset.asset_key,
                        asset_type=asset.asset_type,
                        device_id=asset.device_id,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        command={_asset_command_key(asset): is_on},
                        predicted_state={
                            "scheduled_power_kw": round(power_series[step], 4),
                            "relay_output_on": is_on,
                        },
                    )
                )

        elif asset.asset_key in pv_outputs:
            output = pv_outputs[asset.asset_key]
            pv_series = _float_series(output["pv_output_kw"].value)
            available_series = _float_series(output["available_profile_kw"])
            for step in range(steps):
                starts_at = forecast.horizon_start + timedelta(minutes=step * forecast.step_minutes)
                ends_at = starts_at + timedelta(minutes=forecast.step_minutes)
                intervals.append(
                    PlannedInterval(
                        asset_key=asset.asset_key,
                        asset_type=asset.asset_type,
                        device_id=asset.device_id,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        command={_asset_command_key(asset): round(pv_series[step], 4)},
                        predicted_state={
                            "available_power_kw": round(available_series[step], 4),
                            "curtailed_power_kw": round(max(available_series[step] - pv_series[step], 0.0), 4),
                        },
                    )
                )

    output_snapshot = {
        "solver_status": status,
        "grid_import_kw": _float_series(grid_import.value),
        "grid_export_kw": _float_series(grid_export.value),
        "pv_generation_kw": forecast.pv_generation_kw,
        "base_load_kw": forecast.base_load_kw,
        "dispatchable_asset_keys": [asset.asset_key for asset in dispatchable_assets],
    }
    input_snapshot = {
        "policy": site_model.policy,
        "forecast": {
            "import_price_eur_per_kwh": forecast.import_price_eur_per_kwh,
            "export_price_eur_per_kwh": forecast.export_price_eur_per_kwh,
            "pv_generation_kw": forecast.pv_generation_kw,
            "base_load_kw": forecast.base_load_kw,
            "ambient_temperature_c": forecast.ambient_temperature_c,
            "notes": forecast.notes,
        },
        "assets": [
            {
                "asset_key": asset.asset_key,
                "asset_type": asset.asset_type,
                "eligibility": asset.eligibility,
                "constraints": asset.constraints,
                "telemetry": asset.telemetry,
            }
            for asset in assets
        ],
    }
    return PlanningResult(
        status=HemsPlanStatus.COMPLETED.value if not violations else HemsPlanStatus.DEGRADED.value,
        execution_mode=site_model.policy.get("execution_mode", HemsExecutionMode.GUARDED_AUTO.value),
        solver_name="cvxpy-highs",
        summary=f"Planned {len(dispatchable_assets)} dispatchable asset(s) across {steps} interval(s).",
        objective_value=float(problem.value) if problem.value is not None else None,
        horizon_start=forecast.horizon_start,
        horizon_end=forecast.horizon_start + timedelta(minutes=steps * forecast.step_minutes),
        input_snapshot=input_snapshot,
        output_snapshot=output_snapshot,
        intervals=intervals,
        violations=violations,
    )
