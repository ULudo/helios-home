from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class CommandContract:
    command_key: str
    value_type: str
    unit: str | None
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: list[str] = field(default_factory=list)
    adapter_name: str | None = None
    validation_state: str = "unavailable"
    requires_native_writes: bool = False
    safety_checks: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CanonicalAsset:
    asset_key: str
    asset_type: str
    label: str
    device_id: str | None
    control_capability: str
    eligibility: str
    telemetry: dict[str, Any]
    constraints: dict[str, Any]
    command_contract: CommandContract | None = None
    reasons: list[str] = field(default_factory=list)
    binding_id: str | None = None
    binding_status: str | None = None
    connection_status: str | None = None
    telemetry_status: str | None = None
    control_status: str | None = None


@dataclass(slots=True)
class ForecastBundle:
    horizon_start: datetime
    step_minutes: int
    import_price_eur_per_kwh: list[float]
    export_price_eur_per_kwh: list[float]
    pv_generation_kw: list[float]
    base_load_kw: list[float]
    ambient_temperature_c: list[float]
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SiteModel:
    built_at: datetime
    policy: dict[str, Any]
    assets: list[CanonicalAsset]
    grid_constraints: dict[str, float]
    forecast: ForecastBundle


@dataclass(slots=True)
class PlannedInterval:
    asset_key: str
    asset_type: str
    device_id: str | None
    starts_at: datetime
    ends_at: datetime
    command: dict[str, Any]
    predicted_state: dict[str, Any]


@dataclass(slots=True)
class DispatchOutcome:
    status: str
    requested_command: dict[str, Any]
    applied_command: dict[str, Any]
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlanningResult:
    status: str
    execution_mode: str
    solver_name: str
    summary: str
    objective_value: float | None
    horizon_start: datetime
    horizon_end: datetime
    input_snapshot: dict[str, Any]
    output_snapshot: dict[str, Any]
    intervals: list[PlannedInterval]
    violations: list[dict[str, Any]]
