from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, HemsPolicy, Site, utcnow
from app.hems.schemas import HemsPolicyRead


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


def _serialize_policy(policy: HemsPolicy) -> HemsPolicyRead:
    return HemsPolicyRead(
        site_id=policy.site_id,
        execution_mode=policy.execution_mode,
        battery_reserve_pct=policy.battery_reserve_pct,
        ev_default_target_soc_pct=policy.ev_default_target_soc_pct,
        ev_default_departure_time=policy.ev_default_departure_time,
        heat_comfort_min_c=policy.heat_comfort_min_c,
        heat_comfort_max_c=policy.heat_comfort_max_c,
        grid_import_limit_kw=policy.grid_import_limit_kw,
        grid_export_limit_kw=policy.grid_export_limit_kw,
        allow_price_arbitrage=policy.allow_price_arbitrage,
        allow_heat_precharge=policy.allow_heat_precharge,
        allow_ev_load_shifting=policy.allow_ev_load_shifting,
        horizon_hours=policy.horizon_hours,
        step_minutes=policy.step_minutes,
        updated_at=policy.updated_at,
    )


def get_or_create_hems_policy(session: Session) -> HemsPolicy:
    site = _get_site(session)
    policy = session.scalar(select(HemsPolicy).where(HemsPolicy.site_id == site.id).limit(1))
    if policy is None:
        policy = HemsPolicy(site_id=site.id)
        session.add(policy)
        session.commit()
        session.refresh(policy)
    return policy


def get_hems_policy(session: Session) -> HemsPolicyRead:
    return _serialize_policy(get_or_create_hems_policy(session))


def update_hems_policy(session: Session, updates: dict[str, object]) -> HemsPolicyRead:
    policy = get_or_create_hems_policy(session)
    changed_fields: dict[str, object] = {}
    for field, value in updates.items():
        if value is None:
            continue
        setattr(policy, field, value)
        changed_fields[field] = value
    session.add(policy)
    if changed_fields:
        session.add(
            AuditEvent(
                actor="user",
                action="update_hems_policy",
                target_type="hems_policy",
                target_id=str(policy.site_id),
                summary="Updated HEMS policy defaults.",
                details=changed_fields,
                created_at=utcnow(),
            )
        )
    session.commit()
    session.refresh(policy)
    return _serialize_policy(policy)
