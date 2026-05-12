from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HemsPolicyRead(BaseModel):
    site_id: int
    execution_mode: str
    battery_reserve_pct: float
    ev_default_target_soc_pct: float
    ev_default_departure_time: str
    heat_comfort_min_c: float
    heat_comfort_max_c: float
    grid_import_limit_kw: float
    grid_export_limit_kw: float
    allow_price_arbitrage: bool
    allow_heat_precharge: bool
    allow_ev_load_shifting: bool
    horizon_hours: int
    step_minutes: int
    updated_at: datetime


class HemsPolicyUpdate(BaseModel):
    execution_mode: str | None = None
    battery_reserve_pct: float | None = None
    ev_default_target_soc_pct: float | None = None
    ev_default_departure_time: str | None = None
    heat_comfort_min_c: float | None = None
    heat_comfort_max_c: float | None = None
    grid_import_limit_kw: float | None = None
    grid_export_limit_kw: float | None = None
    allow_price_arbitrage: bool | None = None
    allow_heat_precharge: bool | None = None
    allow_ev_load_shifting: bool | None = None
    horizon_hours: int | None = None
    step_minutes: int | None = None


class HemsLoadControlDeviceConfigRead(BaseModel):
    device_id: str
    receives_lpc: bool = False
    receives_lpp: bool = False
    participates_lpc: bool = False
    participates_lpp: bool = False
    lpc_share_pct: float = 0.0
    lpp_share_pct: float = 0.0
    updated_at: datetime | None = None


class HemsLoadControlDeviceConfigUpdate(BaseModel):
    device_id: str
    receives_lpc: bool | None = None
    receives_lpp: bool | None = None
    participates_lpc: bool | None = None
    participates_lpp: bool | None = None
    lpc_share_pct: float | None = Field(default=None, ge=0, le=100)
    lpp_share_pct: float | None = Field(default=None, ge=0, le=100)
    reason: str = ""


class HemsCommandContractRead(BaseModel):
    command_key: str
    value_type: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: list[str] = Field(default_factory=list)
    adapter_name: str | None = None
    validation_state: str
    requires_native_writes: bool = False
    safety_checks: list[str] = Field(default_factory=list)


class HemsAssetRead(BaseModel):
    asset_key: str
    asset_type: str
    label: str
    device_id: str | None
    binding_id: str | None = None
    binding_status: str | None = None
    connection_status: str | None = None
    telemetry_status: str | None = None
    control_status: str | None = None
    control_capability: str
    eligibility: str
    telemetry: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    command_contract: HemsCommandContractRead | None = None
    reasons: list[str] = Field(default_factory=list)


class HemsSystemBindingRead(BaseModel):
    id: str
    system_type: str
    label: str
    device_id: str | None = None
    asset_id: str | None = None
    status: str
    connection_status: str
    telemetry_status: str
    control_status: str
    source: str
    confidence: float = 0.0
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class HemsPlanHeaderRead(BaseModel):
    id: str
    status: str
    execution_mode: str
    triggered_by: str
    solver_name: str
    objective_value: float | None = None
    summary: str
    horizon_start: datetime
    horizon_end: datetime
    created_at: datetime
    finished_at: datetime | None = None


class HemsPlanIntervalRead(BaseModel):
    id: int | None = None
    asset_key: str
    asset_type: str
    device_id: str | None = None
    starts_at: datetime
    ends_at: datetime
    command: dict[str, Any] = Field(default_factory=dict)
    predicted_state: dict[str, Any] = Field(default_factory=dict)


class HemsDispatchEventRead(BaseModel):
    id: int
    asset_key: str
    asset_type: str
    device_id: str | None = None
    status: str
    requested_command: dict[str, Any] = Field(default_factory=dict)
    applied_command: dict[str, Any] = Field(default_factory=dict)
    summary: str
    planned_for: datetime
    executed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class HemsViolationRead(BaseModel):
    id: int
    asset_key: str | None = None
    severity: str
    violation_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class HemsPlanRead(HemsPlanHeaderRead):
    policy: HemsPolicyRead
    assets: list[HemsAssetRead] = Field(default_factory=list)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    output_snapshot: dict[str, Any] = Field(default_factory=dict)
    intervals: list[HemsPlanIntervalRead] = Field(default_factory=list)
    dispatch_events: list[HemsDispatchEventRead] = Field(default_factory=list)
    violations: list[HemsViolationRead] = Field(default_factory=list)


class HemsSummaryRead(BaseModel):
    policy: HemsPolicyRead
    asset_count: int
    dispatchable_asset_count: int
    plan_only_asset_count: int
    blocked_asset_count: int
    read_only_asset_count: int
    latest_plan: HemsPlanHeaderRead | None = None


class EebusShipServiceRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    service_name: str
    target: str | None = None
    port: int | None = None
    path: str = "/ship/"
    ship_id: str | None = None
    ski: str | None = None
    brand: str | None = None
    model: str | None = None
    device_type: str | None = None
    registration_requested: bool | None = Field(default=None, alias="register")
    addresses: dict[str, list[str]] = Field(default_factory=dict)
    txt: dict[str, str] = Field(default_factory=dict)
    tls_probe: dict[str, Any] | None = None


class EebusLoadPowerLimitCreate(BaseModel):
    use_case: str | None = None
    limit_id: int | None = None
    limit_watts: int
    duration_seconds: int | None = None
    is_active: bool = True
    source: str = "eebus"
    peer_ski: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class EebusLoadPowerLimitDistributionRead(BaseModel):
    use_case: str
    limit_id: int
    direction: str
    is_active: bool
    limit_watts: int
    duration_seconds: int | None = None
    previous_grid_import_limit_kw: float
    previous_grid_export_limit_kw: float
    applied_grid_import_limit_kw: float
    applied_grid_export_limit_kw: float
    changed_policy_fields: dict[str, float] = Field(default_factory=dict)
    changed_effective_limits: dict[str, float] = Field(default_factory=dict)
    active_constraints: list[dict[str, Any]] = Field(default_factory=list)
    constraint_distribution: dict[str, Any] = Field(default_factory=dict)
    eebus_payload: dict[str, Any] = Field(default_factory=dict)
    plan: HemsPlanHeaderRead | None = None
    message: str
