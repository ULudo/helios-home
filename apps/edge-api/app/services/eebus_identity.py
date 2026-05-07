from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EebusLocalIdentity, Site, utcnow


def get_or_create_eebus_local_identity(
    session: Session,
    *,
    site_id: int,
    common_name: str = "Helios Home HEMS",
) -> EebusLocalIdentity:
    identity = session.scalar(select(EebusLocalIdentity).where(EebusLocalIdentity.site_id == site_id).limit(1))
    if identity is not None:
        return identity

    site = session.get(Site, site_id)
    if site is None:
        raise RuntimeError(f"Unknown site: {site_id}")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Helios Home"),
        ]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    ski = certificate.fingerprint(hashes.SHA1()).hex()

    identity = EebusLocalIdentity(
        site_id=site.id,
        common_name=common_name,
        ski=ski,
        certificate_pem=certificate_pem,
        private_key_pem=private_key_pem,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(identity)
    session.commit()
    return identity


def read_eebus_local_identity(session: Session, *, site_id: int) -> EebusLocalIdentity | None:
    return session.scalar(select(EebusLocalIdentity).where(EebusLocalIdentity.site_id == site_id).limit(1))


def eebus_identity_public_payload(identity: EebusLocalIdentity) -> dict:
    return {
        "identity_ref": f"eebus-local-identity:{identity.id}",
        "site_id": identity.site_id,
        "common_name": identity.common_name,
        "ski": identity.ski,
        "certificate_pem": identity.certificate_pem,
        "created_at": identity.created_at,
        "updated_at": identity.updated_at,
    }
