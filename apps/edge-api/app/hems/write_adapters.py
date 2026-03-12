from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Asset, Device, DeviceCandidate
from app.domain.enums import HemsDispatchStatus
from app.hems.models import CanonicalAsset, DispatchOutcome, PlannedInterval
from app.services.modbus import (
    ModbusSourceError,
    get_sunspec_model_block,
    read_sunspec_model_values,
    write_sunspec_model_points,
)


@dataclass(slots=True)
class DispatchTarget:
    canonical_asset: CanonicalAsset
    device: Device | None
    linked_asset: Asset | None
    candidate: DeviceCandidate | None

    @property
    def evidence(self) -> dict[str, Any]:
        if self.candidate is None or not isinstance(self.candidate.evidence, dict):
            return {}
        return self.candidate.evidence


class WriteAdapter(Protocol):
    adapter_name: str

    def supports(self, target: DispatchTarget, interval: PlannedInterval) -> bool:
        ...

    def apply(self, target: DispatchTarget, interval: PlannedInterval) -> DispatchOutcome:
        ...


def build_dispatch_target(session: Session, asset: CanonicalAsset) -> DispatchTarget:
    device = session.get(Device, asset.device_id) if asset.device_id else None
    linked_asset = session.get(Asset, asset.asset_key)
    candidate = None
    if device is not None:
        candidate = session.scalar(
            select(DeviceCandidate)
            .where(DeviceCandidate.matched_device_id == device.id)
            .order_by(DeviceCandidate.updated_at.desc())
            .limit(1)
        )
    return DispatchTarget(
        canonical_asset=asset,
        device=device,
        linked_asset=linked_asset,
        candidate=candidate,
    )


def _command_value(interval: PlannedInterval) -> tuple[str, Any]:
    if not interval.command:
        raise ValueError("Dispatch interval does not contain a command.")
    return next(iter(interval.command.items()))


def _coerce_binary_switch_state(interval: PlannedInterval) -> bool | None:
    command_key, raw_value = _command_value(interval)
    if command_key in {"set_power_kw", "set_charge_kw"}:
        try:
            return float(raw_value) > 0.05
        except (TypeError, ValueError):
            return None
    if command_key == "start_stop":
        if isinstance(raw_value, bool):
            return raw_value
        lowered = str(raw_value).strip().lower()
        if lowered in {"on", "start", "true", "1"}:
            return True
        if lowered in {"off", "stop", "false", "0"}:
            return False
        return None
    if command_key == "set_mode":
        lowered = str(raw_value).strip().lower()
        if lowered in {"off", "disabled", "standby"}:
            return False
        if lowered:
            return True
    return None


def _coerce_numeric_command(interval: PlannedInterval) -> float | None:
    _, raw_value = _command_value(interval)
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def _update_applied_state(
    target: DispatchTarget,
    *,
    applied_command: dict[str, Any],
    adapter_name: str,
    on: bool | None = None,
    extra_metrics: dict[str, Any] | None = None,
) -> None:
    if target.device is None or target.linked_asset is None:
        return

    target.device.telemetry = dict(target.device.telemetry or {})
    target.linked_asset.metrics = dict(target.linked_asset.metrics or {})

    target.device.telemetry["last_dispatch_adapter"] = adapter_name
    target.linked_asset.metrics["last_dispatch_adapter"] = adapter_name
    if on is not None:
        target.device.telemetry["relay_output_on"] = on
        target.linked_asset.metrics["relay_output_on"] = on

    for command_key, value in applied_command.items():
        target.device.telemetry[f"last_{command_key}"] = value
        target.linked_asset.metrics[f"last_{command_key}"] = value
    for metric_key, value in (extra_metrics or {}).items():
        target.device.telemetry[metric_key] = value
        target.linked_asset.metrics[metric_key] = value


class TelemetrySimulationAdapter:
    adapter_name = "telemetry_simulation"

    def __init__(self, session: Session):
        self.session = session

    def supports(self, target: DispatchTarget, interval: PlannedInterval) -> bool:
        return bool(target.canonical_asset.telemetry.get("simulation_supported"))

    def apply(self, target: DispatchTarget, interval: PlannedInterval) -> DispatchOutcome:
        if target.device is None or target.linked_asset is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="Simulation could not update the target asset because the linked records are missing.",
            )

        target.device.telemetry = dict(target.device.telemetry or {})
        target.linked_asset.metrics = dict(target.linked_asset.metrics or {})

        command_key, raw_value = _command_value(interval)
        if target.canonical_asset.asset_type == "battery":
            target.device.telemetry["power_kw"] = float(raw_value)
            target.linked_asset.metrics["power_kw"] = float(raw_value)
        elif target.canonical_asset.asset_type == "ev_charger":
            target.device.telemetry["scheduled_charge_kw"] = float(raw_value)
            target.linked_asset.metrics["scheduled_charge_kw"] = float(raw_value)
        elif target.canonical_asset.asset_type in {"heat_pump", "pv_inverter"}:
            target.device.telemetry["scheduled_power_kw"] = float(raw_value)
            target.linked_asset.metrics["scheduled_power_kw"] = float(raw_value)

        self.session.add(target.device)
        self.session.add(target.linked_asset)
        return DispatchOutcome(
            status=HemsDispatchStatus.SIMULATED.value,
            requested_command=interval.command,
            applied_command={command_key: float(raw_value)},
            summary="Applied the command through the telemetry simulation adapter.",
            details={"adapter": self.adapter_name},
        )


class ShellyHttpRelayAdapter:
    adapter_name = "shelly_http_relay"

    def supports(self, target: DispatchTarget, interval: PlannedInterval) -> bool:
        evidence = target.evidence
        if evidence.get("dispatch_profile") != self.adapter_name:
            return False
        return bool(evidence.get("http_base_url"))

    def apply(self, target: DispatchTarget, interval: PlannedInterval) -> DispatchOutcome:
        desired_state = _coerce_binary_switch_state(interval)
        if desired_state is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The Shelly relay adapter only supports binary switch commands.",
                details={"adapter": self.adapter_name},
            )

        evidence = target.evidence
        base_url = str(evidence.get("http_base_url", "")).rstrip("/")
        channel = int(evidence.get("dispatch_channel", 0))
        generation = int(evidence.get("dispatch_generation", 2))
        timeout = get_settings().modbus_timeout_seconds

        try:
            if generation >= 2:
                response = httpx.post(
                    f"{base_url}/rpc/Switch.Set",
                    json={"id": channel, "on": desired_state},
                    timeout=timeout,
                    verify=False,
                )
                if response.status_code in {404, 405}:
                    response = httpx.get(
                        f"{base_url}/rpc/Switch.Set",
                        params={"id": channel, "on": str(desired_state).lower()},
                        timeout=timeout,
                        verify=False,
                    )
            else:
                response = httpx.get(
                    f"{base_url}/relay/{channel}",
                    params={"turn": "on" if desired_state else "off"},
                    timeout=timeout,
                    verify=False,
                )
        except httpx.HTTPError as exc:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary=f"Shelly write request failed: {exc}",
                details={"adapter": self.adapter_name, "base_url": base_url},
            )

        if response.status_code >= 400:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary=f"Shelly write request returned HTTP {response.status_code}.",
                details={"adapter": self.adapter_name, "base_url": base_url},
            )

        applied_command = dict(interval.command)
        _update_applied_state(target, on=desired_state, applied_command=applied_command, adapter_name=self.adapter_name)
        return DispatchOutcome(
            status=HemsDispatchStatus.APPLIED.value,
            requested_command=interval.command,
            applied_command=applied_command,
            summary="Applied the command through the Shelly local relay API.",
            details={
                "adapter": self.adapter_name,
                "base_url": base_url,
                "dispatch_generation": generation,
                "dispatch_channel": channel,
            },
        )


class TasmotaHttpPowerAdapter:
    adapter_name = "tasmota_http_power"

    def supports(self, target: DispatchTarget, interval: PlannedInterval) -> bool:
        evidence = target.evidence
        if evidence.get("dispatch_profile") != self.adapter_name:
            return False
        return bool(evidence.get("http_base_url"))

    def apply(self, target: DispatchTarget, interval: PlannedInterval) -> DispatchOutcome:
        desired_state = _coerce_binary_switch_state(interval)
        if desired_state is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The Tasmota power adapter only supports binary switch commands.",
                details={"adapter": self.adapter_name},
            )

        evidence = target.evidence
        base_url = str(evidence.get("http_base_url", "")).rstrip("/")
        timeout = get_settings().modbus_timeout_seconds
        try:
            response = httpx.get(
                f"{base_url}/cm",
                params={"cmnd": f"Power {'On' if desired_state else 'Off'}"},
                timeout=timeout,
                verify=False,
            )
        except httpx.HTTPError as exc:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary=f"Tasmota write request failed: {exc}",
                details={"adapter": self.adapter_name, "base_url": base_url},
            )

        if response.status_code >= 400:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary=f"Tasmota write request returned HTTP {response.status_code}.",
                details={"adapter": self.adapter_name, "base_url": base_url},
            )

        applied_command = dict(interval.command)
        _update_applied_state(target, on=desired_state, applied_command=applied_command, adapter_name=self.adapter_name)
        return DispatchOutcome(
            status=HemsDispatchStatus.APPLIED.value,
            requested_command=interval.command,
            applied_command=applied_command,
            summary="Applied the command through the Tasmota local power API.",
            details={
                "adapter": self.adapter_name,
                "base_url": base_url,
            },
        )


class SunSpecStorageRateAdapter:
    adapter_name = "sunspec_storage_basic_rate"

    def supports(self, target: DispatchTarget, interval: PlannedInterval) -> bool:
        return target.evidence.get("dispatch_profile") == self.adapter_name

    def apply(self, target: DispatchTarget, interval: PlannedInterval) -> DispatchOutcome:
        desired_power_kw = _coerce_numeric_command(interval)
        if desired_power_kw is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The SunSpec storage adapter requires a numeric power command.",
                details={"adapter": self.adapter_name},
            )

        evidence = target.evidence
        host = str(evidence.get("modbus_host", "")).strip()
        unit_id = int(evidence.get("modbus_unit_id", 0))
        model_block = get_sunspec_model_block(evidence.get("sunspec_model_blocks"), int(evidence.get("dispatch_model_id", 124)))
        timeout = get_settings().write_http_timeout_seconds
        if not host or unit_id < 0 or model_block is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The SunSpec storage adapter is missing validated model metadata.",
                details={"adapter": self.adapter_name},
            )

        current_values = read_sunspec_model_values(host, unit_id, model_block, timeout)
        if current_values is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The SunSpec storage adapter could not read the current control model values.",
                details={"adapter": self.adapter_name, "host": host, "unit_id": unit_id},
            )

        max_charge_kw = float(
            target.canonical_asset.constraints.get("max_charge_kw")
            or target.canonical_asset.telemetry.get("max_charge_power_kw")
            or 0.0
        )
        max_discharge_kw = float(
            target.canonical_asset.constraints.get("max_discharge_kw")
            or target.canonical_asset.telemetry.get("max_discharge_power_kw")
            or max_charge_kw
            or 0.0
        )
        if max_charge_kw <= 0.0 or max_discharge_kw <= 0.0:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The SunSpec storage adapter is missing maximum charge or discharge limits.",
                details={"adapter": self.adapter_name},
            )

        if desired_power_kw > 0.05:
            discharge_pct = min(100.0, max(0.0, desired_power_kw / max_discharge_kw * 100.0))
            charge_pct = 0.0
            storage_mode = 1 << 1
        elif desired_power_kw < -0.05:
            charge_pct = min(100.0, max(0.0, abs(desired_power_kw) / max_charge_kw * 100.0))
            discharge_pct = 0.0
            storage_mode = 1 << 0
        else:
            charge_pct = 0.0
            discharge_pct = 0.0
            storage_mode = 0

        try:
            success = write_sunspec_model_points(
                host,
                unit_id,
                model_block,
                current_values,
                {
                    "StorCtl_Mod": storage_mode,
                    "OutWRte": discharge_pct,
                    "InWRte": charge_pct,
                    "InOutWRte_WinTms": 0,
                    "InOutWRte_RvrtTms": 0,
                    "InOutWRte_RmpTms": 0,
                },
                timeout,
            )
        except ModbusSourceError as exc:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary=f"SunSpec storage write failed: {exc}",
                details={"adapter": self.adapter_name, "host": host, "unit_id": unit_id},
            )

        if not success:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="SunSpec storage write did not receive a valid Modbus acknowledgement.",
                details={"adapter": self.adapter_name, "host": host, "unit_id": unit_id},
            )

        applied_command = dict(interval.command)
        _update_applied_state(
            target,
            applied_command=applied_command,
            adapter_name=self.adapter_name,
            extra_metrics={
                "power_kw": round(desired_power_kw, 4),
                "charge_rate_pct": round(charge_pct, 3),
                "discharge_rate_pct": round(discharge_pct, 3),
            },
        )
        return DispatchOutcome(
            status=HemsDispatchStatus.APPLIED.value,
            requested_command=interval.command,
            applied_command=applied_command,
            summary="Applied the command through the SunSpec storage rate-control profile.",
            details={
                "adapter": self.adapter_name,
                "host": host,
                "unit_id": unit_id,
                "model_id": model_block.model_id,
                "charge_rate_pct": round(charge_pct, 3),
                "discharge_rate_pct": round(discharge_pct, 3),
            },
        )


class SunSpecDerWMaxAdapter:
    adapter_name = "sunspec_der_wmax_pct"

    def supports(self, target: DispatchTarget, interval: PlannedInterval) -> bool:
        return target.evidence.get("dispatch_profile") == self.adapter_name

    def apply(self, target: DispatchTarget, interval: PlannedInterval) -> DispatchOutcome:
        desired_power_kw = _coerce_numeric_command(interval)
        if desired_power_kw is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The SunSpec DER adapter requires a numeric power command.",
                details={"adapter": self.adapter_name},
            )

        evidence = target.evidence
        host = str(evidence.get("modbus_host", "")).strip()
        unit_id = int(evidence.get("modbus_unit_id", 0))
        model_block = get_sunspec_model_block(evidence.get("sunspec_model_blocks"), int(evidence.get("dispatch_model_id", 704)))
        timeout = get_settings().write_http_timeout_seconds
        if not host or unit_id < 0 or model_block is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The SunSpec DER adapter is missing validated control-model metadata.",
                details={"adapter": self.adapter_name},
            )

        power_rating_kw = float(
            target.canonical_asset.constraints.get("power_rating_kw")
            or target.canonical_asset.telemetry.get("power_rating_kw")
            or 0.0
        )
        if power_rating_kw <= 0.0:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The SunSpec DER adapter is missing the inverter power rating.",
                details={"adapter": self.adapter_name},
            )

        current_values = read_sunspec_model_values(host, unit_id, model_block, timeout)
        if current_values is None:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="The SunSpec DER adapter could not read the current control model values.",
                details={"adapter": self.adapter_name, "host": host, "unit_id": unit_id},
            )

        desired_limit_pct = min(100.0, max(0.0, desired_power_kw / power_rating_kw * 100.0))
        try:
            success = write_sunspec_model_points(
                host,
                unit_id,
                model_block,
                current_values,
                {
                    "WMaxLimPctEna": 1,
                    "WMaxLimPct": desired_limit_pct,
                },
                timeout,
            )
        except ModbusSourceError as exc:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary=f"SunSpec DER write failed: {exc}",
                details={"adapter": self.adapter_name, "host": host, "unit_id": unit_id},
            )

        if not success:
            return DispatchOutcome(
                status=HemsDispatchStatus.FAILED.value,
                requested_command=interval.command,
                applied_command={},
                summary="SunSpec DER write did not receive a valid Modbus acknowledgement.",
                details={"adapter": self.adapter_name, "host": host, "unit_id": unit_id},
            )

        applied_command = dict(interval.command)
        _update_applied_state(
            target,
            applied_command=applied_command,
            adapter_name=self.adapter_name,
            extra_metrics={
                "scheduled_power_kw": round(desired_power_kw, 4),
                "curtailment_limit_pct": round(desired_limit_pct, 3),
                "curtailment_enabled": True,
            },
        )
        return DispatchOutcome(
            status=HemsDispatchStatus.APPLIED.value,
            requested_command=interval.command,
            applied_command=applied_command,
            summary="Applied the command through the SunSpec DER maximum-active-power control profile.",
            details={
                "adapter": self.adapter_name,
                "host": host,
                "unit_id": unit_id,
                "model_id": model_block.model_id,
                "limit_pct": round(desired_limit_pct, 3),
            },
        )


def resolve_write_adapter(session: Session, target: DispatchTarget, interval: PlannedInterval) -> WriteAdapter | None:
    settings = get_settings()
    adapters: list[WriteAdapter] = [TelemetrySimulationAdapter(session)]
    if settings.native_writes_enabled:
        adapters.extend(
            [
                ShellyHttpRelayAdapter(),
                SunSpecStorageRateAdapter(),
                SunSpecDerWMaxAdapter(),
                TasmotaHttpPowerAdapter(),
            ]
        )

    for adapter in adapters:
        if adapter.supports(target, interval):
            return adapter
    return None
