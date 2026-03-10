from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Site, utcnow


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
