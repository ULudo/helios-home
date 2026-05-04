from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.semantics import HEMS_SYSTEM_TYPES
from app.db.models import (
    Device,
    DeviceAssessment,
    DeviceCandidate,
    HomeGraphEntity,
    HomeGraphEvidence,
    ProtocolEndpoint,
    Site,
    utcnow,
)


def entity_ref(entity_type: str, source_id: str) -> str:
    return f"{entity_type}:{source_id}"


def split_entity_ref(ref: str) -> tuple[str, str]:
    if ":" not in ref:
        return "", ref
    entity_type, source_id = ref.split(":", 1)
    return entity_type, source_id


def _stable_ref(prefix: str, value: str) -> str:
    return f"{prefix}-{uuid5(NAMESPACE_URL, value).hex[:16]}"


def _upsert_entity(
    session: Session,
    *,
    site_id: int,
    ref: str,
    entity_type: str,
    source_type: str,
    source_id: str,
    display_name: str,
    semantic_type: str,
    status: str,
    properties: dict,
) -> HomeGraphEntity:
    entity = session.get(HomeGraphEntity, ref)
    if entity is None:
        entity = HomeGraphEntity(id=ref, site_id=site_id, created_at=utcnow())
        session.add(entity)
    entity.entity_type = entity_type
    entity.source_type = source_type
    entity.source_id = source_id
    entity.display_name = display_name
    entity.semantic_type = semantic_type
    entity.status = status
    entity.properties = properties
    entity.updated_at = utcnow()
    return entity


def _upsert_endpoint(
    session: Session,
    *,
    site_id: int,
    owner_ref: str,
    protocol: str,
    service_name: str = "",
    address: str = "",
    port: int | None = None,
    properties: dict | None = None,
) -> ProtocolEndpoint:
    endpoint_id = _stable_ref("endpoint", f"{owner_ref}:{protocol}:{service_name}:{address}:{port or ''}")
    endpoint = session.get(ProtocolEndpoint, endpoint_id)
    if endpoint is None:
        endpoint = ProtocolEndpoint(id=endpoint_id, site_id=site_id, created_at=utcnow())
        session.add(endpoint)
    endpoint.owner_ref = owner_ref
    endpoint.protocol = protocol
    endpoint.service_name = service_name
    endpoint.address = address
    endpoint.port = port
    endpoint.status = "observed"
    endpoint.properties = properties or {}
    endpoint.updated_at = utcnow()
    return endpoint


def _record_evidence(
    session: Session,
    *,
    site_id: int,
    subject_ref: str,
    evidence_type: str,
    source: str,
    summary: str,
    payload: dict,
    confidence: float,
    trust: str = "observed",
) -> HomeGraphEvidence:
    evidence = HomeGraphEvidence(
        id=f"evidence-{uuid4().hex[:12]}",
        site_id=site_id,
        subject_ref=subject_ref,
        evidence_type=evidence_type,
        source=source,
        summary=summary,
        payload=payload,
        confidence=confidence,
        trust=trust,
        created_at=utcnow(),
    )
    session.add(evidence)
    return evidence


def sync_inventory_to_home_graph(session: Session, site_id: int | None = None) -> list[str]:
    site = session.scalar(select(Site).limit(1)) if site_id is None else session.get(Site, site_id)
    if site is None:
        raise RuntimeError("Site has not been seeded.")

    refs: list[str] = []
    candidates = session.scalars(select(DeviceCandidate).where(DeviceCandidate.site_id == site.id)).all()
    for candidate in candidates:
        ref = entity_ref("candidate", candidate.id)
        refs.append(ref)
        _upsert_entity(
            session,
            site_id=site.id,
            ref=ref,
            entity_type="candidate",
            source_type="device_candidate",
            source_id=candidate.id,
            display_name=candidate.display_name,
            semantic_type=candidate.device_type,
            status=candidate.state,
            properties={
                "manufacturer": candidate.manufacturer,
                "model": candidate.model,
                "firmware": candidate.firmware,
                "protocols": candidate.protocols or [],
                "discovery_sources": candidate.discovery_sources or [],
                "classification_confidence": candidate.classification_confidence,
                "classification_reasoning": candidate.classification_reasoning,
                "matched_device_id": candidate.matched_device_id,
                "evidence": candidate.evidence or {},
            },
        )
        for protocol in candidate.protocols or []:
            _upsert_endpoint(
                session,
                site_id=site.id,
                owner_ref=ref,
                protocol=str(protocol),
                service_name=str((candidate.evidence or {}).get("service_name", "")),
                address=str((candidate.evidence or {}).get("host", "")),
                properties={"source": "candidate"},
            )

    devices = session.scalars(select(Device).where(Device.site_id == site.id)).all()
    for device in devices:
        ref = entity_ref("device", device.id)
        refs.append(ref)
        _upsert_entity(
            session,
            site_id=site.id,
            ref=ref,
            entity_type="device",
            source_type="device",
            source_id=device.id,
            display_name=device.name,
            semantic_type=device.device_type,
            status=device.primary_status,
            properties={
                "manufacturer": device.manufacturer,
                "model": device.model,
                "firmware": device.firmware,
                "protocols": device.protocols or [],
                "capabilities": device.capabilities or {},
                "telemetry": device.telemetry or {},
                "explanation": device.explanation,
                "next_step": device.next_step,
                "confidence": device.confidence,
            },
        )
        for protocol in device.protocols or []:
            _upsert_endpoint(
                session,
                site_id=site.id,
                owner_ref=ref,
                protocol=str(protocol),
                service_name=str((device.telemetry or {}).get("service_name", "")),
                port=_extract_port(device.telemetry or {}),
                properties={"source": "device"},
            )

    session.commit()
    return refs


def _extract_port(payload: dict) -> int | None:
    for key in ("port", "ship_port", "http_port"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def query_entities(
    session: Session,
    *,
    text: str = "",
    entity_refs: list[str] | None = None,
    entity_types: list[str] | None = None,
    include_evidence: bool = False,
    include_relationships: bool = True,
) -> dict:
    statement = select(HomeGraphEntity).order_by(HomeGraphEntity.updated_at.desc())
    if entity_refs:
        statement = statement.where(HomeGraphEntity.id.in_(entity_refs))
    if entity_types:
        statement = statement.where(HomeGraphEntity.entity_type.in_(entity_types))
    entities = session.scalars(statement).all()
    if text:
        lowered = text.lower()
        entities = [
            entity
            for entity in entities
            if lowered in entity.display_name.lower()
            or lowered in entity.semantic_type.lower()
            or lowered in str(entity.properties or {}).lower()
        ]
    evidence: list[dict] = []
    if include_evidence and entities:
        refs = [entity.id for entity in entities]
        evidence_rows = session.scalars(
            select(HomeGraphEvidence)
            .where(HomeGraphEvidence.subject_ref.in_(refs))
            .order_by(HomeGraphEvidence.created_at.desc())
        ).all()
        evidence = [_evidence_as_dict(row) for row in evidence_rows]
    return {
        "entities": [_entity_as_dict(entity) for entity in entities],
        "evidence": evidence,
        "relationships": _relationships_for_entities(session, entities) if include_relationships else [],
    }


def resolve_entity(session: Session, ref: str) -> HomeGraphEntity | None:
    entity = session.get(HomeGraphEntity, ref)
    if entity is not None:
        return entity
    entity_type, source_id = split_entity_ref(ref)
    if entity_type == "device":
        device = session.get(Device, source_id)
        if device is not None:
            sync_inventory_to_home_graph(session, device.site_id)
            return session.get(HomeGraphEntity, ref)
    if entity_type == "candidate":
        candidate = session.get(DeviceCandidate, source_id)
        if candidate is not None:
            sync_inventory_to_home_graph(session, candidate.site_id)
            return session.get(HomeGraphEntity, ref)
    return None


@dataclass(slots=True)
class AssessmentResult:
    assessment: DeviceAssessment
    evidence: list[HomeGraphEvidence]


def assess_device(session: Session, *, entity_reference: str, question: str = "") -> AssessmentResult:
    entity = resolve_entity(session, entity_reference)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_reference}")

    properties = entity.properties or {}
    evidence_rows = [
        _record_evidence(
            session,
            site_id=entity.site_id,
            subject_ref=entity.id,
            evidence_type="identity",
            source=entity.source_type or "home_graph",
            summary="identity_observed",
            payload={
                "display_name": entity.display_name,
                "manufacturer": properties.get("manufacturer", ""),
                "model": properties.get("model", ""),
                "semantic_type": entity.semantic_type,
            },
            confidence=float(properties.get("confidence") or properties.get("classification_confidence") or 0.5),
        )
    ]
    protocols = [str(protocol) for protocol in properties.get("protocols", [])]
    if protocols:
        evidence_rows.append(
            _record_evidence(
                session,
                site_id=entity.site_id,
                subject_ref=entity.id,
                evidence_type="protocols",
                source="home_graph",
                summary="protocols_observed",
                payload={"protocols": protocols},
                confidence=0.75,
            )
        )

    possible_roles = _possible_roles_for_entity(entity)

    assessment = DeviceAssessment(
        id=f"assessment-{uuid4().hex[:12]}",
        site_id=entity.site_id,
        subject_ref=entity.id,
        summary="device_assessment",
        possible_roles=possible_roles,
        evidence_refs=[row.id for row in evidence_rows],
        confidence=float(possible_roles[0]["confidence"]) if possible_roles else 0.35,
        status="tentative",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(assessment)
    session.commit()
    return AssessmentResult(assessment=assessment, evidence=evidence_rows)


def _possible_roles_for_entity(entity: HomeGraphEntity) -> list[dict]:
    semantic_type = entity.semantic_type
    role_map = {
        "grid_meter": "grid_meter",
        "smart_meter_gateway": "grid_meter",
        "wallbox": "ev_charger",
        "ev_charger": "ev_charger",
        "battery": "battery",
        "pv_inverter": "pv_inverter",
        "heat_pump": "heat_pump",
        "smart_appliance": "controllable_load",
        "controllable_load": "controllable_load",
    }
    role = role_map.get(semantic_type)
    if role is None and semantic_type in HEMS_SYSTEM_TYPES:
        role = semantic_type
    if role is None:
        return []
    confidence = float((entity.properties or {}).get("confidence") or (entity.properties or {}).get("classification_confidence") or 0.72)
    return [
        {
            "role": role,
            "confidence": min(max(confidence, 0.0), 1.0),
            "reason_codes": ["semantic_type_match"],
            "source_semantic_type": semantic_type,
        }
    ]


def _entity_as_dict(entity: HomeGraphEntity) -> dict:
    return {
        "ref": entity.id,
        "entity_type": entity.entity_type,
        "source_type": entity.source_type,
        "source_id": entity.source_id,
        "display_name": entity.display_name,
        "semantic_type": entity.semantic_type,
        "status": entity.status,
        "properties": entity.properties or {},
        "updated_at": entity.updated_at,
    }


def _evidence_as_dict(evidence: HomeGraphEvidence) -> dict:
    return {
        "ref": evidence.id,
        "subject_ref": evidence.subject_ref,
        "evidence_type": evidence.evidence_type,
        "source": evidence.source,
        "summary": evidence.summary,
        "payload": evidence.payload or {},
        "confidence": evidence.confidence,
        "trust": evidence.trust,
        "created_at": evidence.created_at,
    }


def _relationships_for_entities(session: Session, entities: list[HomeGraphEntity]) -> list[dict]:
    relationships: list[dict] = []
    for entity in entities:
        matched_device_id = (entity.properties or {}).get("matched_device_id")
        if entity.entity_type == "candidate" and matched_device_id:
            relationships.append(
                {
                    "from_ref": entity.id,
                    "to_ref": entity_ref("device", str(matched_device_id)),
                    "relationship": "materialized_as",
                }
            )
    endpoints = session.scalars(
        select(ProtocolEndpoint).where(ProtocolEndpoint.owner_ref.in_([entity.id for entity in entities]))
    ).all()
    for endpoint in endpoints:
        relationships.append(
            {
                "from_ref": endpoint.owner_ref,
                "to_ref": endpoint.id,
                "relationship": "has_protocol_endpoint",
                "properties": {
                    "protocol": endpoint.protocol,
                    "address": endpoint.address,
                    "port": endpoint.port,
                    "service_name": endpoint.service_name,
                },
            }
        )
    return relationships
