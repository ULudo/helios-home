from __future__ import annotations

from hashlib import sha1
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, Device, HemsLoadControlDeviceConfig, ProtocolEndpoint, Site, utcnow
from app.domain.enums import HemsAssetType


CONFIGURED_FIELDS = (
    "receives_lpc",
    "receives_lpp",
    "participates_lpc",
    "participates_lpp",
)


def _asset_id_for_device(device_id: str) -> str:
    digest = sha1(device_id.encode("utf-8")).hexdigest()[:16]
    return f"asset-hems-{digest}"


def _existing_asset_for_device(session: Session, device_id: str) -> Asset | None:
    for asset in session.scalars(select(Asset).order_by(Asset.updated_at.desc())).all():
        if device_id in (asset.device_ids or []):
            return asset
    return None


def _configured(config: HemsLoadControlDeviceConfig) -> bool:
    return any(bool(getattr(config, field)) for field in CONFIGURED_FIELDS)


def _connected_endpoints(session: Session, device_id: str) -> list[ProtocolEndpoint]:
    return list(
        session.scalars(
            select(ProtocolEndpoint)
            .where(
                ProtocolEndpoint.owner_ref == f"device:{device_id}",
                ProtocolEndpoint.status == "connected",
            )
            .order_by(ProtocolEndpoint.updated_at.desc())
        ).all()
    )


def _has_dispatch_profile(endpoints: list[ProtocolEndpoint]) -> bool:
    for endpoint in endpoints:
        properties = endpoint.properties if isinstance(endpoint.properties, dict) else {}
        if str(properties.get("dispatch_profile") or "").strip():
            return True
    return False


def _infer_asset_type(
    device: Device,
    *,
    config: HemsLoadControlDeviceConfig,
    endpoints: list[ProtocolEndpoint],
) -> str | None:
    raw_type = (device.device_type or "").strip()
    telemetry = device.telemetry if isinstance(device.telemetry, dict) else {}
    capabilities = device.capabilities if isinstance(device.capabilities, dict) else {}
    hems_types = {item.value for item in HemsAssetType}
    if raw_type in hems_types:
        return raw_type
    if raw_type == "wallbox":
        return HemsAssetType.EV_CHARGER.value
    if raw_type == "smart_appliance":
        if bool(capabilities.get("controllable")) or _has_dispatch_profile(endpoints) or config.participates_lpc or config.participates_lpp:
            return HemsAssetType.CONTROLLABLE_LOAD.value
        return None
    if bool(telemetry.get("curtailment_supported")) or any(
        str((endpoint.properties or {}).get("dispatch_profile") or "").startswith("sunspec_")
        for endpoint in endpoints
    ):
        return HemsAssetType.PV_INVERTER.value
    return None


def _asset_health(primary_status: str) -> str:
    if primary_status in {"connected", "monitorable", "controllable", "optimizable"}:
        return "healthy"
    if primary_status in {"authentication_required", "manufacturer_access_required", "not_integratable"}:
        return "blocked"
    return "attention"


def materialize_configured_hems_assets(session: Session, *, site_id: int | None = None) -> list[Asset]:
    site = session.get(Site, site_id) if site_id is not None else session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")

    materialized: list[Asset] = []
    configs = session.scalars(
        select(HemsLoadControlDeviceConfig).where(HemsLoadControlDeviceConfig.site_id == site.id)
    ).all()
    now = utcnow()
    for config in configs:
        if not _configured(config):
            continue
        device = session.get(Device, config.device_id)
        if device is None or device.site_id != site.id:
            continue
        endpoints = _connected_endpoints(session, device.id)
        asset_type = _infer_asset_type(device, config=config, endpoints=endpoints)
        if asset_type is None:
            continue

        asset = _existing_asset_for_device(session, device.id)
        created = False
        if asset is None:
            asset = Asset(
                id=_asset_id_for_device(device.id),
                site_id=site.id,
                created_at=now,
            )
            created = True

        asset.name = device.name
        asset.asset_type = asset_type
        asset.status = device.primary_status
        asset.health = _asset_health(device.primary_status)
        asset.device_ids = [device.id]
        asset.metrics = dict(device.telemetry or {})
        asset.updated_at = now
        session.add(asset)
        materialized.append(asset)

        if created:
            session.flush()

    return materialized
