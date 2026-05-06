from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import HemsAssetType, HemsEligibility
from app.hems.adapter_registry import SIMULATION_ADAPTER_NAME, is_supported_native_dispatch_profile
from app.hems.models import CommandContract

SUPPORTED_DISPATCH_ASSET_TYPES = {
    HemsAssetType.BATTERY.value,
    HemsAssetType.CONTROLLABLE_LOAD.value,
    HemsAssetType.EV_CHARGER.value,
    HemsAssetType.HEAT_PUMP.value,
    HemsAssetType.PV_INVERTER.value,
}

RUNTIME_ALLOWED_DEVICE_STATUSES = {"connected", "controllable", "optimizable"}
BLOCKING_DEVICE_STATUSES = {"authentication_required", "manufacturer_access_required", "not_integratable"}


@dataclass(slots=True)
class DispatchabilityAssessment:
    eligibility: str
    reasons: list[str]
    command_contract: CommandContract | None


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


def _validation_state(
    telemetry: dict[str, Any],
    evidence: dict[str, Any],
    *,
    native_writes_enabled: bool,
) -> tuple[str, str | None, list[str]]:
    reasons: list[str] = []
    dispatch_profile = str(evidence.get("dispatch_profile", "")).strip()
    if dispatch_profile:
        if not is_supported_native_dispatch_profile(dispatch_profile):
            reasons.append(f"Dispatch profile `{dispatch_profile}` is not registered in the runtime adapter registry.")
            return "unavailable", None, reasons
        if not native_writes_enabled:
            reasons.append("Native writes are currently disabled in the runtime.")
            return "native_disabled", dispatch_profile, reasons
        return "native", dispatch_profile, reasons

    if bool(telemetry.get("simulation_supported")):
        return "simulation", SIMULATION_ADAPTER_NAME, reasons

    reasons.append("No validated dispatch profile is available for this asset yet.")
    return "unavailable", None, reasons


def _battery_contract(
    telemetry: dict[str, Any],
    constraints: dict[str, Any],
    *,
    validation_state: str,
    adapter_name: str | None,
) -> tuple[CommandContract | None, list[str]]:
    reasons: list[str] = []
    max_charge_kw = _numeric_value(constraints, "max_charge_kw") or _numeric_value(telemetry, "max_charge_power_kw")
    max_discharge_kw = _numeric_value(constraints, "max_discharge_kw") or _numeric_value(
        telemetry, "max_discharge_power_kw", "max_charge_power_kw"
    )
    if max_charge_kw is None or max_charge_kw <= 0.0:
        reasons.append("Battery charge bounds are missing.")
    if max_discharge_kw is None or max_discharge_kw <= 0.0:
        reasons.append("Battery discharge bounds are missing.")
    if reasons:
        return None, reasons
    return (
        CommandContract(
            command_key="set_power_kw",
            value_type="number",
            unit="kW",
            minimum=round(-max_charge_kw, 4),
            maximum=round(max_discharge_kw, 4),
            adapter_name=adapter_name,
            validation_state=validation_state,
            requires_native_writes=validation_state in {"native", "native_disabled"},
            safety_checks=["fresh_telemetry", "battery_soc_guard", "power_limit_clamp"],
        ),
        [],
    )


def _ev_contract(
    telemetry: dict[str, Any],
    constraints: dict[str, Any],
    *,
    validation_state: str,
    adapter_name: str | None,
) -> tuple[CommandContract | None, list[str]]:
    reasons: list[str] = []
    max_charge_kw = _numeric_value(constraints, "max_charge_kw") or _numeric_value(telemetry, "max_charge_kw")
    if max_charge_kw is None or max_charge_kw <= 0.0:
        reasons.append("EV charging power bounds are missing.")
    if not constraints.get("departure_at"):
        reasons.append("The EV departure deadline is missing.")
    if reasons:
        return None, reasons
    return (
        CommandContract(
            command_key="set_charge_kw",
            value_type="number",
            unit="kW",
            minimum=0.0,
            maximum=round(max_charge_kw, 4),
            adapter_name=adapter_name,
            validation_state=validation_state,
            requires_native_writes=validation_state in {"native", "native_disabled"},
            safety_checks=["fresh_telemetry", "charge_power_limit", "departure_deadline"],
        ),
        [],
    )


def _heat_pump_contract(
    telemetry: dict[str, Any],
    constraints: dict[str, Any],
    *,
    validation_state: str,
    adapter_name: str | None,
) -> tuple[CommandContract | None, list[str]]:
    reasons: list[str] = []
    current_temperature = _numeric_value(constraints, "current_temperature_c")
    max_power_kw = _numeric_value(constraints, "max_power_kw") or _numeric_value(
        telemetry, "electrical_power_kw", "power_kw", "thermal_output_kw"
    )
    if current_temperature is None:
        reasons.append("No temperature proxy is available for heat-pump comfort control.")
    if max_power_kw is None or max_power_kw <= 0.0:
        reasons.append("Heat-pump power bounds are missing.")
    if reasons:
        return None, reasons
    return (
        CommandContract(
            command_key="set_power_kw",
            value_type="number",
            unit="kW",
            minimum=0.0,
            maximum=round(max_power_kw, 4),
            adapter_name=adapter_name,
            validation_state=validation_state,
            requires_native_writes=validation_state in {"native", "native_disabled"},
            safety_checks=["fresh_telemetry", "comfort_band", "temperature_proxy"],
        ),
        [],
    )


def _pv_contract(
    telemetry: dict[str, Any],
    constraints: dict[str, Any],
    *,
    validation_state: str,
    adapter_name: str | None,
) -> tuple[CommandContract | None, list[str]]:
    reasons: list[str] = []
    curtailment_supported = bool(constraints.get("curtailment_supported") or telemetry.get("curtailment_supported"))
    power_rating_kw = _numeric_value(constraints, "power_rating_kw") or _numeric_value(telemetry, "power_rating_kw")
    if not curtailment_supported:
        reasons.append("PV curtailment support has not been validated.")
    if power_rating_kw is None or power_rating_kw <= 0.0:
        reasons.append("The inverter power rating is missing.")
    if reasons:
        return None, reasons
    return (
        CommandContract(
            command_key="set_power_kw",
            value_type="number",
            unit="kW",
            minimum=0.0,
            maximum=round(power_rating_kw, 4),
            adapter_name=adapter_name,
            validation_state=validation_state,
            requires_native_writes=validation_state in {"native", "native_disabled"},
            safety_checks=["fresh_telemetry", "curtailment_limit", "inverter_rating"],
        ),
        [],
    )


def _controllable_load_contract(
    telemetry: dict[str, Any],
    constraints: dict[str, Any],
    *,
    validation_state: str,
    adapter_name: str | None,
) -> tuple[CommandContract | None, list[str]]:
    reasons: list[str] = []
    nominal_power_kw = _numeric_value(constraints, "nominal_power_kw") or _numeric_value(telemetry, "power_kw")
    if nominal_power_kw is None:
        power_w = _numeric_value(telemetry, "power_w")
        nominal_power_kw = power_w / 1000.0 if power_w is not None else None
    if nominal_power_kw is None or nominal_power_kw <= 0.0:
        reasons.append("The controllable load is missing a nominal power estimate.")
    if reasons:
        return None, reasons
    return (
        CommandContract(
            command_key="start_stop",
            value_type="boolean",
            unit=None,
            allowed_values=["off", "on"],
            adapter_name=adapter_name,
            validation_state=validation_state,
            requires_native_writes=validation_state in {"native", "native_disabled"},
            safety_checks=["fresh_telemetry", "binary_actuation", "load_power_proxy"],
        ),
        [],
    )


def build_command_contract(
    *,
    asset_type: str,
    control_capability: str,
    telemetry: dict[str, Any],
    constraints: dict[str, Any],
    evidence: dict[str, Any],
    native_writes_enabled: bool,
) -> tuple[CommandContract | None, list[str]]:
    _ = control_capability
    validation_state, adapter_name, validation_reasons = _validation_state(
        telemetry,
        evidence,
        native_writes_enabled=native_writes_enabled,
    )

    if asset_type == HemsAssetType.BATTERY.value:
        contract, reasons = _battery_contract(
            telemetry,
            constraints,
            validation_state=validation_state,
            adapter_name=adapter_name,
        )
    elif asset_type == HemsAssetType.EV_CHARGER.value:
        contract, reasons = _ev_contract(
            telemetry,
            constraints,
            validation_state=validation_state,
            adapter_name=adapter_name,
        )
    elif asset_type == HemsAssetType.HEAT_PUMP.value:
        contract, reasons = _heat_pump_contract(
            telemetry,
            constraints,
            validation_state=validation_state,
            adapter_name=adapter_name,
        )
    elif asset_type == HemsAssetType.PV_INVERTER.value:
        contract, reasons = _pv_contract(
            telemetry,
            constraints,
            validation_state=validation_state,
            adapter_name=adapter_name,
        )
    elif asset_type == HemsAssetType.CONTROLLABLE_LOAD.value:
        contract, reasons = _controllable_load_contract(
            telemetry,
            constraints,
            validation_state=validation_state,
            adapter_name=adapter_name,
        )
    else:
        return None, ["This asset type is outside the current HEMS dispatch scope."]

    return contract, [*validation_reasons, *reasons]


def assess_dispatchability(
    *,
    device,
    asset_type: str,
    control_capability: str,
    telemetry: dict[str, Any],
    constraints: dict[str, Any],
    evidence: dict[str, Any],
    native_writes_enabled: bool,
) -> DispatchabilityAssessment:
    if device is None:
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.BLOCKED.value,
            reasons=["No device is linked to this asset."],
            command_contract=None,
        )

    reasons: list[str] = []
    capabilities = device.capabilities or {}
    if not bool(capabilities.get("visible")):
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.BLOCKED.value,
            reasons=["The asset is not visible in the local runtime."],
            command_contract=None,
        )
    if not bool(capabilities.get("monitorable")):
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.BLOCKED.value,
            reasons=["No validated telemetry path is available for this asset."],
            command_contract=None,
        )
    if device.primary_status in BLOCKING_DEVICE_STATUSES:
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.BLOCKED.value,
            reasons=[f"Device status blocks dispatchability: {device.primary_status}."],
            command_contract=None,
        )
    if asset_type not in SUPPORTED_DISPATCH_ASSET_TYPES:
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.READ_ONLY.value,
            reasons=["This asset type is outside the current HEMS dispatch scope."],
            command_contract=None,
        )

    command_contract, contract_reasons = build_command_contract(
        asset_type=asset_type,
        control_capability=control_capability,
        telemetry=telemetry,
        constraints=constraints,
        evidence=evidence,
        native_writes_enabled=native_writes_enabled,
    )
    reasons.extend(contract_reasons)

    if not bool(capabilities.get("controllable")):
        reasons.append("The asset has no validated write path yet.")
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.PLAN_ONLY.value,
            reasons=reasons,
            command_contract=command_contract,
        )

    if command_contract is None:
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.PLAN_ONLY.value,
            reasons=reasons or ["The asset does not satisfy the current HEMS command contract requirements."],
            command_contract=None,
        )

    if command_contract.validation_state == "native_disabled":
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.PLAN_ONLY.value,
            reasons=reasons,
            command_contract=command_contract,
        )

    if command_contract.validation_state == "unavailable":
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.PLAN_ONLY.value,
            reasons=reasons,
            command_contract=command_contract,
        )

    if device.primary_status not in RUNTIME_ALLOWED_DEVICE_STATUSES:
        reasons.append("The device status is not validated strongly enough for guarded auto execution.")
        return DispatchabilityAssessment(
            eligibility=HemsEligibility.PLAN_ONLY.value,
            reasons=reasons,
            command_contract=command_contract,
        )

    return DispatchabilityAssessment(
        eligibility=HemsEligibility.DISPATCHABLE.value,
        reasons=reasons,
        command_contract=command_contract,
    )
