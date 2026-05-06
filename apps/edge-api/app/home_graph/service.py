from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
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
    host: str = "",
    port: int | None = None,
    source: str = "",
    last_seen_at: datetime | None = None,
    confidence: float | None = None,
    properties: dict | None = None,
) -> ProtocolEndpoint:
    endpoint_id = _stable_ref("endpoint", f"{owner_ref}:{protocol}:{service_name}:{host}:{port or ''}")
    endpoint = session.get(ProtocolEndpoint, endpoint_id)
    if endpoint is None:
        endpoint = ProtocolEndpoint(id=endpoint_id, site_id=site_id, created_at=utcnow())
        session.add(endpoint)
    endpoint.owner_ref = owner_ref
    endpoint.protocol = protocol
    endpoint.service_name = service_name
    endpoint.host = host
    endpoint.port = port
    endpoint.status = "observed"
    clean_properties = dict(properties or {})
    if source:
        clean_properties["source"] = source
    if last_seen_at is not None:
        clean_properties["last_seen_at"] = last_seen_at.isoformat()
    if confidence is not None:
        clean_properties["confidence"] = confidence
    if host:
        clean_properties["host"] = host
    endpoint.properties = clean_properties
    endpoint.updated_at = utcnow()
    return endpoint


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _http_host_from_payload(evidence: dict, telemetry: dict) -> str:
    host = _first_string(evidence.get("http_host"), evidence.get("host"), telemetry.get("http_host"))
    if host:
        return host
    base_url = _first_string(evidence.get("http_base_url"), telemetry.get("http_base_url"))
    if base_url:
        return urlparse(base_url).hostname or ""
    return ""


def _http_port_from_payload(evidence: dict, telemetry: dict) -> int | None:
    explicit = _first_int(evidence.get("http_port"), telemetry.get("http_port"))
    if explicit is not None:
        return explicit
    base_url = _first_string(evidence.get("http_base_url"), telemetry.get("http_base_url"))
    if not base_url:
        return None
    parsed = urlparse(base_url)
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def _ship_payload(evidence: dict) -> dict:
    payload = evidence.get("ship_service")
    return payload if isinstance(payload, dict) else {}


def _ship_host(payload: dict, evidence: dict) -> str:
    addresses = payload.get("addresses") if isinstance(payload.get("addresses"), dict) else {}
    ipv4_addresses = [str(value) for value in addresses.get("ipv4", [])] if isinstance(addresses, dict) else []
    return _first_string(*(ipv4_addresses[:1]), payload.get("target"), evidence.get("host"))


def _mqtt_properties(evidence: dict) -> dict:
    properties: dict[str, Any] = {}
    topics = evidence.get("mqtt_topics")
    if isinstance(topics, list):
        properties["topics"] = [str(topic) for topic in topics[:12]]
    slug = evidence.get("mqtt_device_slug")
    if slug:
        properties["device_slug"] = str(slug)
    return properties


def _broadcast_endpoint_specs(protocol: str, evidence: dict) -> list[dict]:
    announcements = evidence.get("broadcast_announcements")
    if not isinstance(announcements, list):
        return []
    specs: list[dict] = []
    for announcement in announcements:
        if not isinstance(announcement, dict) or announcement.get("protocol") != protocol:
            continue
        specs.append(
            {
                "protocol": protocol,
                "host": _first_string(announcement.get("host")),
                "port": None,
                "service_name": _first_string(announcement.get("service_name")),
                "properties": {
                    "service_type": _first_string(announcement.get("service_type")),
                    "server": _first_string(announcement.get("server")),
                    "location": _first_string(announcement.get("location")),
                    "txt": announcement.get("txt") if isinstance(announcement.get("txt"), list) else [],
                },
            }
        )
    return specs


def _source_for_protocol(protocol: str, discovery_sources: list[str]) -> str:
    preferred_sources = {
        "http_local": "local_network_live",
        "modbus_tcp": "modbus_live",
        "mqtt": "mqtt_live",
        "eebus_ship": "eebus_ship_live",
        "mdns": "network_broadcast_live",
        "ssdp": "network_broadcast_live",
        "vendor_cloud": "local_network_live",
    }
    preferred = preferred_sources.get(protocol)
    if preferred and preferred in discovery_sources:
        return preferred
    return discovery_sources[0] if discovery_sources else "inventory"


def _endpoint_specs(
    *,
    protocols: list[str],
    evidence: dict,
    telemetry: dict,
    discovery_sources: list[str],
    last_seen_at: datetime,
    confidence: float,
) -> list[dict]:
    specs: list[dict] = []
    for protocol in protocols:
        protocol = str(protocol)
        if protocol == "http_local":
            specs.append(
                {
                    "protocol": protocol,
                    "host": _http_host_from_payload(evidence, telemetry),
                    "port": _http_port_from_payload(evidence, telemetry),
                    "service_name": _first_string(evidence.get("service_name"), evidence.get("fingerprint_profile"), "local_http"),
                    "properties": {
                        "base_url": _first_string(evidence.get("http_base_url")),
                        "paths": evidence.get("http_paths") if isinstance(evidence.get("http_paths"), list) else [],
                        "server": _first_string(evidence.get("http_server")),
                    },
                }
            )
        elif protocol == "modbus_tcp":
            specs.append(
                {
                    "protocol": protocol,
                    "host": _first_string(evidence.get("modbus_host"), evidence.get("host")),
                    "port": _first_int(evidence.get("modbus_port")) or 502,
                    "service_name": "modbus_tcp",
                    "properties": {
                        "unit_id": evidence.get("modbus_unit_id"),
                        "sunspec_model_ids": evidence.get("sunspec_model_ids") if isinstance(evidence.get("sunspec_model_ids"), list) else [],
                        "dispatch_profile": _first_string(evidence.get("dispatch_profile")),
                    },
                }
            )
        elif protocol == "eebus_ship":
            ship = _ship_payload(evidence)
            specs.append(
                {
                    "protocol": protocol,
                    "host": _ship_host(ship, evidence),
                    "port": _first_int(ship.get("port"), telemetry.get("ship_port")),
                    "service_name": _first_string(ship.get("service_name"), "eebus_ship"),
                    "properties": {
                        "path": _first_string(ship.get("path")),
                        "ship_id": _first_string(ship.get("ship_id")),
                        "ski": _first_string(ship.get("ski")),
                        "supported_use_cases": evidence.get("supported_use_cases") if isinstance(evidence.get("supported_use_cases"), list) else [],
                        "tls_probe": ship.get("tls_probe"),
                    },
                }
            )
        elif protocol == "mqtt":
            specs.append(
                {
                    "protocol": protocol,
                    "host": _first_string(evidence.get("mqtt_host")),
                    "port": _first_int(evidence.get("mqtt_port")),
                    "service_name": _first_string(evidence.get("mqtt_device_slug"), "mqtt"),
                    "properties": _mqtt_properties(evidence),
                }
            )
        elif protocol in {"mdns", "ssdp"}:
            broadcast_specs = _broadcast_endpoint_specs(protocol, evidence)
            specs.extend(broadcast_specs or [{"protocol": protocol, "host": _first_string(evidence.get("host")), "port": None, "service_name": protocol, "properties": {}}])
        else:
            specs.append({"protocol": protocol, "host": _first_string(evidence.get("host")), "port": None, "service_name": protocol, "properties": {}})

    identity_keys = evidence.get("identity_keys") if isinstance(evidence.get("identity_keys"), list) else []
    for spec in specs:
        properties = dict(spec.get("properties") or {})
        if identity_keys:
            properties["identity_keys"] = [str(key) for key in identity_keys]
        spec["properties"] = properties
        spec["source"] = _source_for_protocol(str(spec.get("protocol") or ""), discovery_sources)
        spec["last_seen_at"] = last_seen_at
        spec["confidence"] = confidence
    return specs


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
    candidate_by_device_id = {candidate.matched_device_id: candidate for candidate in candidates if candidate.matched_device_id}
    session.query(ProtocolEndpoint).filter(ProtocolEndpoint.site_id == site.id).delete(synchronize_session=False)
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
        for spec in _endpoint_specs(
            protocols=[str(protocol) for protocol in candidate.protocols or []],
            evidence=candidate.evidence or {},
            telemetry={},
            discovery_sources=[str(source) for source in candidate.discovery_sources or []],
            last_seen_at=candidate.last_seen_at,
            confidence=candidate.classification_confidence,
        ):
            _upsert_endpoint(
                session,
                site_id=site.id,
                owner_ref=ref,
                protocol=str(spec["protocol"]),
                service_name=str(spec.get("service_name") or ""),
                host=str(spec.get("host") or ""),
                port=spec.get("port") if isinstance(spec.get("port"), int) else None,
                source=str(spec.get("source") or "candidate"),
                last_seen_at=spec.get("last_seen_at") if isinstance(spec.get("last_seen_at"), datetime) else None,
                confidence=float(spec.get("confidence") or 0.0),
                properties=spec.get("properties") if isinstance(spec.get("properties"), dict) else {},
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
                "confidence": device.confidence,
            },
        )
        candidate = candidate_by_device_id.get(device.id)
        candidate_evidence = candidate.evidence if candidate is not None else {}
        candidate_sources = candidate.discovery_sources if candidate is not None else []
        confidence = candidate.classification_confidence if candidate is not None else device.confidence
        for spec in _endpoint_specs(
            protocols=[str(protocol) for protocol in device.protocols or []],
            evidence=candidate_evidence or {},
            telemetry=device.telemetry or {},
            discovery_sources=[str(source) for source in candidate_sources or []],
            last_seen_at=device.last_seen_at,
            confidence=float(confidence or 0.0),
        ):
            _upsert_endpoint(
                session,
                site_id=site.id,
                owner_ref=ref,
                protocol=str(spec["protocol"]),
                service_name=str(spec.get("service_name") or ""),
                host=str(spec.get("host") or ""),
                port=spec.get("port") if isinstance(spec.get("port"), int) else None,
                source=str(spec.get("source") or "device"),
                last_seen_at=spec.get("last_seen_at") if isinstance(spec.get("last_seen_at"), datetime) else None,
                confidence=float(spec.get("confidence") or 0.0),
                properties=spec.get("properties") if isinstance(spec.get("properties"), dict) else {},
            )

    session.commit()
    return refs


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


def get_entity_details(
    session: Session,
    *,
    entity_ref: str,
    include_evidence: bool = True,
) -> dict:
    entity = resolve_entity(session, entity_ref)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_ref}")

    endpoint_rows = session.scalars(
        select(ProtocolEndpoint)
        .where(ProtocolEndpoint.owner_ref == entity.id)
        .order_by(ProtocolEndpoint.protocol, ProtocolEndpoint.service_name, ProtocolEndpoint.host)
    ).all()
    evidence_rows: list[HomeGraphEvidence] = []
    if include_evidence:
        evidence_rows = session.scalars(
            select(HomeGraphEvidence)
            .where(HomeGraphEvidence.subject_ref == entity.id)
            .order_by(HomeGraphEvidence.created_at.desc())
            .limit(20)
        ).all()
    return {
        "entity": _entity_as_dict(entity),
        "protocol_endpoints": [_endpoint_as_dict(endpoint) for endpoint in endpoint_rows],
        "evidence": [_evidence_as_dict(row) for row in evidence_rows],
        "relationships": _relationships_for_entities(session, [entity]),
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


def _endpoint_as_dict(endpoint: ProtocolEndpoint) -> dict:
    properties = endpoint.properties or {}
    return {
        "endpoint_ref": endpoint.id,
        "owner_ref": endpoint.owner_ref,
        "protocol": endpoint.protocol,
        "host": endpoint.host,
        "port": endpoint.port,
        "service_name": endpoint.service_name,
        "status": endpoint.status,
        "source": properties.get("source", ""),
        "last_seen_at": properties.get("last_seen_at", ""),
        "confidence": properties.get("confidence", 0.0),
        "properties": properties,
        "updated_at": endpoint.updated_at,
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
                    "host": endpoint.host,
                    "port": endpoint.port,
                    "service_name": endpoint.service_name,
                    "source": (endpoint.properties or {}).get("source", ""),
                    "last_seen_at": (endpoint.properties or {}).get("last_seen_at", ""),
                    "confidence": (endpoint.properties or {}).get("confidence", 0.0),
                },
            }
        )
    return relationships
