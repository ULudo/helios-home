from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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


class HemsAssetRead(BaseModel):
    asset_key: str
    asset_type: str
    label: str
    device_id: str | None
    control_capability: str
    eligibility: str
    telemetry: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


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
