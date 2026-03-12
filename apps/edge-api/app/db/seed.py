from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, HemsPolicy, Site, utcnow


def seed_demo_data(session: Session) -> None:
    site_exists = session.scalar(select(Site.id).limit(1))
    if site_exists:
        return

    now = utcnow()
    site = Site(
        name="Helios Home",
        local_subnet="",
        mqtt_broker_url="",
        safety_state="safe",
        policy_mode="safe",
        discovery_last_run=now - timedelta(minutes=12),
    )
    session.add(site)
    session.flush()
    session.add(
        HemsPolicy(
            site_id=site.id,
            execution_mode="guarded_auto",
            battery_reserve_pct=20.0,
            ev_default_target_soc_pct=80.0,
            ev_default_departure_time="07:00",
            heat_comfort_min_c=20.0,
            heat_comfort_max_c=22.5,
            grid_import_limit_kw=12.0,
            grid_export_limit_kw=12.0,
            allow_price_arbitrage=True,
            allow_heat_precharge=True,
            allow_ev_load_shifting=True,
            horizon_hours=24,
            step_minutes=15,
        )
    )
    session.add(
        AuditEvent(
            actor="system",
            action="seed_site_configuration",
            target_type="site",
            target_id=str(site.id),
            summary="Provisioned the default local site configuration.",
            details={"mode": "default"},
            created_at=now,
        )
    )
    session.commit()
