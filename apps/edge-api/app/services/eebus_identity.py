from __future__ import annotations

import re
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtensionOID
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EebusLocalIdentity, Site, utcnow


def _load_identity_sdk():
    try:
        from eebus_sdk.identity import (
            IdentityMaterial,
            IdentityStore,
            build_qr_payload,
            default_ship_id,
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "EEBus support is part of the standard Helios backend, but the eebus-sdk package is missing. "
            "Reinstall backend dependencies with ./scripts/setup-backend.sh."
        ) from exc
    return IdentityMaterial, IdentityStore, build_qr_payload, default_ship_id


def _device_id_from_name(common_name: str) -> str:
    stem = common_name.removesuffix(".cls")
    device_id = re.sub(r"[^A-Z0-9_-]", "-", stem.upper()).strip("-")
    while "--" in device_id:
        device_id = device_id.replace("--", "-")
    return device_id or "HELIOS-HOME-HEMS"


def _cert_subject_key_identifier(certificate_pem: str) -> str:
    certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
    extension = certificate.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_KEY_IDENTIFIER)
    return extension.value.digest.hex()


def _is_sdk_compatible_identity(identity: EebusLocalIdentity) -> bool:
    try:
        certificate = x509.load_pem_x509_certificate(identity.certificate_pem.encode("ascii"))
        subject_key_identifier = certificate.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_KEY_IDENTIFIER
        ).value.digest.hex()
    except Exception:
        return False
    return (
        identity.ski.lower() == subject_key_identifier
        and isinstance(certificate.public_key(), ec.EllipticCurvePublicKey)
        and bool(identity.private_key_pem)
    )


def _create_sdk_identity_material(common_name: str):
    _, IdentityStore, _, _ = _load_identity_sdk()
    device_id = _device_id_from_name(common_name)
    with tempfile.TemporaryDirectory(prefix="helios-eebus-identity-create-") as temp_dir:
        material = IdentityStore.create(
            temp_dir,
            device_id=device_id,
            brand="Helios Home",
            model="Helios Home HEMS",
            device_type="DeviceTypeTypeEnergyManagementSystem",
            overwrite=True,
        )
        certificate_pem = Path(material.cert_path).read_text(encoding="ascii")
        private_key_pem = Path(material.key_path).read_text(encoding="ascii")
        return material, certificate_pem, private_key_pem


def get_or_create_eebus_local_identity(
    session: Session,
    *,
    site_id: int,
    common_name: str = "Helios Home HEMS",
) -> EebusLocalIdentity:
    identity = session.scalar(select(EebusLocalIdentity).where(EebusLocalIdentity.site_id == site_id).limit(1))
    if identity is not None and _is_sdk_compatible_identity(identity):
        return identity

    site = session.get(Site, site_id)
    if site is None:
        raise RuntimeError(f"Unknown site: {site_id}")

    material, certificate_pem, private_key_pem = _create_sdk_identity_material(common_name)
    now = utcnow()
    if identity is None:
        identity = EebusLocalIdentity(
            site_id=site.id,
            common_name=material.common_name,
            ski=material.ski,
            certificate_pem=certificate_pem,
            private_key_pem=private_key_pem,
            created_at=now,
            updated_at=now,
        )
        session.add(identity)
    else:
        identity.common_name = material.common_name
        identity.ski = material.ski
        identity.certificate_pem = certificate_pem
        identity.private_key_pem = private_key_pem
        identity.updated_at = now
        session.add(identity)
    session.commit()
    return identity


def read_eebus_local_identity(session: Session, *, site_id: int) -> EebusLocalIdentity | None:
    identity = session.scalar(select(EebusLocalIdentity).where(EebusLocalIdentity.site_id == site_id).limit(1))
    if identity is None:
        return None
    return identity if _is_sdk_compatible_identity(identity) else None


def materialize_eebus_identity(identity: EebusLocalIdentity, *, directory: str | Path | None = None):
    IdentityMaterial, IdentityStore, _, default_ship_id = _load_identity_sdk()
    out_dir = Path(directory) if directory is not None else Path(tempfile.mkdtemp(prefix="helios-eebus-identity-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    cert_path = out_dir / "client.crt.pem"
    key_path = out_dir / "client.key.pem"
    cert_path.write_text(identity.certificate_pem, encoding="ascii")
    key_path.write_text(identity.private_key_pem, encoding="ascii")
    device_id = _device_id_from_name(identity.common_name)
    return IdentityStore.import_existing(
        str(out_dir),
        cert_path=str(cert_path),
        key_path=str(key_path),
        ship_id=default_ship_id(device_id),
        device_id=device_id,
        common_name=identity.common_name,
        ski=identity.ski,
        brand="Helios Home",
        model="Helios Home HEMS",
        device_type="DeviceTypeTypeEnergyManagementSystem",
        copy_files=False,
        overwrite=True,
    )


def eebus_identity_public_payload(identity: EebusLocalIdentity) -> dict:
    _, _, build_qr_payload, default_ship_id = _load_identity_sdk()
    device_id = _device_id_from_name(identity.common_name)
    ship_id = default_ship_id(device_id)
    return {
        "identity_ref": f"eebus-local-identity:{identity.id}",
        "site_id": identity.site_id,
        "device_id": device_id,
        "ship_id": ship_id,
        "common_name": identity.common_name,
        "ski": identity.ski,
        "ski_source": "x509_subject_key_identifier",
        "qr_payload": build_qr_payload(
            ship_id,
            identity.ski,
            brand="Helios Home",
            model="Helios Home HEMS",
            device_type="DeviceTypeTypeEnergyManagementSystem",
        ),
        "certificate_pem": identity.certificate_pem,
        "created_at": identity.created_at,
        "updated_at": identity.updated_at,
    }
