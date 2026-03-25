from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import AuditEvent, HemsDispatchEvent, HemsPlanInterval, HemsPlanRun, HemsViolation, Site, utcnow
from app.hems.dispatcher import dispatch_current_interval
from app.hems.models import ForecastBundle, SiteModel
from app.hems.planner import solve_site_plan
from app.hems.policy import get_hems_policy, get_or_create_hems_policy, update_hems_policy
from app.hems.schemas import (
    HemsAssetRead,
    HemsCommandContractRead,
    HemsDispatchEventRead,
    HemsPlanHeaderRead,
    HemsPlanIntervalRead,
    HemsPlanRead,
    HemsPolicyRead,
    HemsPolicyUpdate,
    HemsSummaryRead,
    HemsViolationRead,
)
from app.hems.site_model import build_site_model


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


def _serialize_asset(asset) -> HemsAssetRead:
    return HemsAssetRead(
        asset_key=asset.asset_key,
        asset_type=asset.asset_type,
        label=asset.label,
        device_id=asset.device_id,
        control_capability=asset.control_capability,
        eligibility=asset.eligibility,
        telemetry=asset.telemetry,
        constraints=asset.constraints,
        command_contract=(
            HemsCommandContractRead(
                command_key=asset.command_contract.command_key,
                value_type=asset.command_contract.value_type,
                unit=asset.command_contract.unit,
                minimum=asset.command_contract.minimum,
                maximum=asset.command_contract.maximum,
                allowed_values=asset.command_contract.allowed_values,
                adapter_name=asset.command_contract.adapter_name,
                validation_state=asset.command_contract.validation_state,
                requires_native_writes=asset.command_contract.requires_native_writes,
                safety_checks=asset.command_contract.safety_checks,
            )
            if asset.command_contract is not None
            else None
        ),
        reasons=asset.reasons,
    )


def _serialize_interval(interval: HemsPlanInterval) -> HemsPlanIntervalRead:
    return HemsPlanIntervalRead(
        id=interval.id,
        asset_key=interval.asset_key,
        asset_type=interval.asset_type,
        device_id=interval.device_id,
        starts_at=interval.starts_at,
        ends_at=interval.ends_at,
        command=interval.command or {},
        predicted_state=interval.predicted_state or {},
    )


def _serialize_dispatch_event(event: HemsDispatchEvent) -> HemsDispatchEventRead:
    return HemsDispatchEventRead(
        id=event.id,
        asset_key=event.asset_key,
        asset_type=event.asset_type,
        device_id=event.device_id,
        status=event.status,
        requested_command=event.requested_command or {},
        applied_command=event.applied_command or {},
        summary=event.summary,
        planned_for=event.planned_for,
        executed_at=event.executed_at,
        details=event.details or {},
    )


def _serialize_violation(violation: HemsViolation) -> HemsViolationRead:
    return HemsViolationRead(
        id=violation.id,
        asset_key=violation.asset_key,
        severity=violation.severity,
        violation_type=violation.violation_type,
        message=violation.message,
        details=violation.details or {},
        created_at=violation.created_at,
    )


def _serialize_plan_header(plan_run: HemsPlanRun) -> HemsPlanHeaderRead:
    return HemsPlanHeaderRead(
        id=plan_run.id,
        status=plan_run.status,
        execution_mode=plan_run.execution_mode,
        triggered_by=plan_run.triggered_by,
        solver_name=plan_run.solver_name,
        objective_value=plan_run.objective_value,
        summary=plan_run.summary,
        horizon_start=plan_run.horizon_start,
        horizon_end=plan_run.horizon_end,
        created_at=plan_run.created_at,
        finished_at=plan_run.finished_at,
    )


def _serialize_plan(plan_run: HemsPlanRun, policy: HemsPolicyRead, site_model: SiteModel | None = None) -> HemsPlanRead:
    return HemsPlanRead(
        **_serialize_plan_header(plan_run).model_dump(),
        policy=policy,
        assets=[_serialize_asset(asset) for asset in (site_model.assets if site_model is not None else [])],
        input_snapshot=plan_run.input_snapshot or {},
        output_snapshot=plan_run.output_snapshot or {},
        intervals=[_serialize_interval(interval) for interval in plan_run.intervals],
        dispatch_events=[_serialize_dispatch_event(event) for event in plan_run.dispatch_events],
        violations=[_serialize_violation(violation) for violation in plan_run.violations],
    )


def _latest_plan_run(session: Session) -> HemsPlanRun | None:
    return session.scalar(
        select(HemsPlanRun)
        .options(
            selectinload(HemsPlanRun.intervals),
            selectinload(HemsPlanRun.dispatch_events),
            selectinload(HemsPlanRun.violations),
        )
        .order_by(HemsPlanRun.created_at.desc())
        .limit(1)
    )


def list_hems_assets(session: Session, forecast_override: ForecastBundle | None = None) -> list[HemsAssetRead]:
    site_model = build_site_model(session, forecast_override=forecast_override)
    return [_serialize_asset(asset) for asset in site_model.assets]


def get_hems_summary(session: Session, forecast_override: ForecastBundle | None = None) -> HemsSummaryRead:
    policy = get_hems_policy(session)
    site_model = build_site_model(session, forecast_override=forecast_override)
    latest_plan = _latest_plan_run(session)
    return HemsSummaryRead(
        policy=policy,
        asset_count=len(site_model.assets),
        dispatchable_asset_count=len([asset for asset in site_model.assets if asset.eligibility == "dispatchable"]),
        plan_only_asset_count=len([asset for asset in site_model.assets if asset.eligibility == "plan_only"]),
        blocked_asset_count=len([asset for asset in site_model.assets if asset.eligibility == "blocked"]),
        read_only_asset_count=len([asset for asset in site_model.assets if asset.eligibility == "read_only"]),
        latest_plan=_serialize_plan_header(latest_plan) if latest_plan is not None else None,
    )


def get_latest_hems_plan(session: Session) -> HemsPlanRead | None:
    latest_plan = _latest_plan_run(session)
    if latest_plan is None:
        return None
    policy = get_hems_policy(session)
    return _serialize_plan(latest_plan, policy)


def patch_hems_policy(session: Session, payload: HemsPolicyUpdate) -> HemsPolicyRead:
    return update_hems_policy(session, payload.model_dump(exclude_none=True))


def run_hems_replan(
    session: Session,
    *,
    triggered_by: str = "manual",
    forecast_override: ForecastBundle | None = None,
) -> HemsPlanRead:
    site = _get_site(session)
    policy = get_or_create_hems_policy(session)
    now = utcnow()
    site_model = build_site_model(session, now=now, forecast_override=forecast_override)
    planning_result = solve_site_plan(site_model)
    plan_run = HemsPlanRun(
        id=f"hems-plan-{uuid4().hex[:12]}",
        site_id=site.id,
        status=planning_result.status,
        execution_mode=planning_result.execution_mode,
        triggered_by=triggered_by,
        solver_name=planning_result.solver_name,
        objective_value=planning_result.objective_value,
        summary=planning_result.summary,
        input_snapshot=planning_result.input_snapshot,
        output_snapshot=planning_result.output_snapshot,
        horizon_start=planning_result.horizon_start,
        horizon_end=planning_result.horizon_end,
        created_at=now,
        started_at=now,
        finished_at=utcnow(),
    )
    session.add(plan_run)
    session.flush()

    for interval in planning_result.intervals:
        session.add(
            HemsPlanInterval(
                plan_run_id=plan_run.id,
                asset_key=interval.asset_key,
                asset_type=interval.asset_type,
                device_id=interval.device_id,
                starts_at=interval.starts_at,
                ends_at=interval.ends_at,
                command=interval.command,
                predicted_state=interval.predicted_state,
            )
        )

    dispatch_records, dispatch_violations = dispatch_current_interval(
        session,
        site_model=site_model,
        intervals=planning_result.intervals,
        execution_mode=policy.execution_mode,
        now=now,
    )
    for record in dispatch_records:
        session.add(
            HemsDispatchEvent(
                plan_run_id=plan_run.id,
                asset_key=record.interval.asset_key,
                asset_type=record.interval.asset_type,
                device_id=record.interval.device_id,
                status=record.outcome.status,
                requested_command=record.outcome.requested_command,
                applied_command=record.outcome.applied_command,
                summary=record.outcome.summary,
                planned_for=record.interval.starts_at,
                executed_at=now,
                details=record.outcome.details,
            )
        )

    for violation in [*planning_result.violations, *dispatch_violations]:
        session.add(
            HemsViolation(
                plan_run_id=plan_run.id,
                asset_key=violation.get("asset_key"),
                severity=str(violation.get("severity", "warning")),
                violation_type=str(violation.get("violation_type", "unspecified")),
                message=str(violation.get("message", "")),
                details=dict(violation.get("details", {})),
                created_at=now,
            )
        )

    session.add(
        AuditEvent(
            actor="system",
            action="run_hems_replan",
            target_type="hems_plan",
            target_id=plan_run.id,
            summary=planning_result.summary,
            details={
                "status": planning_result.status,
                "triggered_by": triggered_by,
                "dispatch_interval_count": len(dispatch_records),
            },
            created_at=now,
        )
    )
    session.commit()

    persisted_plan = _latest_plan_run(session)
    if persisted_plan is None:
        raise RuntimeError("HEMS plan run was not persisted.")
    return _serialize_plan(persisted_plan, get_hems_policy(session), site_model=site_model)
