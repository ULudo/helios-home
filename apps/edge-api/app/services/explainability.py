from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Device, DeviceCandidate, DiscoveryRun, KnowledgeEntry, Site
from app.domain.enums import (
    ExplainabilityReasonCode,
    ExplainabilityReasonFamily,
    ExplainabilityState,
    IntegrationFeasibility,
    IntegrationStatus,
)
from app.domain.schemas import (
    DebugDiagnosisRead,
    DebugEvidenceRead,
    DebugExplainRequest,
    DebugReportRead,
    RetrofitOptionRead,
)

TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
LEGACY_HINT_PATTERN = re.compile(r"\b(?:[12]?\d|30|40)\s*(?:year|years|yr|yrs|jahre|jahr)\b", re.IGNORECASE)


def _normalize(value: str) -> str:
    return " ".join(TOKEN_PATTERN.findall(value.lower()))


def _tokenize(*values: str) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(TOKEN_PATTERN.findall(value.lower()))
    return tokens


def _claim_label(payload: DebugExplainRequest) -> str:
    parts = [payload.manufacturer.strip(), payload.model.strip(), payload.device_type.replace("_", " ").strip()]
    label = " ".join(part for part in parts if part)
    return label or "Unspecified device claim"


def _dedupe_text(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(normalized)
    return ordered


def _evidence(
    kind: str,
    label: str,
    value: Any,
    source: str,
    confidence: float | None = None,
) -> DebugEvidenceRead:
    if isinstance(value, list):
        rendered = ", ".join(str(item) for item in value)
    else:
        rendered = str(value)
    return DebugEvidenceRead(
        kind=kind,
        label=label,
        value=rendered,
        source=source,
        confidence=confidence,
    )


def _load_device(session: Session, device_id: str) -> Device | None:
    return session.scalar(
        select(Device)
        .where(Device.id == device_id)
        .options(
            selectinload(Device.connector_attempts),
            selectinload(Device.incidents),
        )
    )


def _load_candidate_for_device(session: Session, device_id: str) -> DeviceCandidate | None:
    return session.scalar(
        select(DeviceCandidate)
        .where(DeviceCandidate.matched_device_id == device_id)
        .order_by(DeviceCandidate.last_seen_at.desc())
    )


def _load_candidate(session: Session, candidate_id: str) -> DeviceCandidate | None:
    return session.get(DeviceCandidate, candidate_id)


def _list_recent_discovery_runs(session: Session) -> list[DiscoveryRun]:
    return session.scalars(select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc()).limit(3)).all()


def _get_site(session: Session) -> Site | None:
    return session.scalar(select(Site).limit(1))


def _summary_from_status(device: Device) -> str:
    if device.primary_status in {
        IntegrationStatus.MONITORABLE.value,
        IntegrationStatus.CONTROLLABLE.value,
        IntegrationStatus.OPTIMIZABLE.value,
    }:
        return "The device exposes a validated, network-reachable telemetry path."
    if device.primary_status == IntegrationStatus.VISIBLE_ONLY.value:
        return "The device is visible, but Helios has not validated a stable telemetry adapter yet."
    return "Helios materialized the device, but the integration path still needs explanation data."


def _retrofit_options_for_device_type(device_type: str, notes: str = "") -> list[RetrofitOptionRead]:
    lowered = notes.lower()
    options: list[RetrofitOptionRead] = []

    if device_type == "heat_pump":
        options.append(
            RetrofitOptionRead(
                kind="vendor_gateway",
                label="Vendor network module",
                description="Check whether the heat pump offers an official LAN, WLAN, or gateway add-on.",
                effort="vendor_specific",
                requires_vendor_gateway=True,
            )
        )
        options.append(
            RetrofitOptionRead(
                kind="dry_contact",
                label="SG Ready relay",
                description="Use a dry-contact relay on the SG Ready input for basic surplus and demand-response signaling.",
                effort="electrician_likely",
                requires_electrician=True,
            )
        )
        options.append(
            RetrofitOptionRead(
                kind="meter_only",
                label="External energy metering",
                description="Measure the heat pump externally when native telemetry is not available.",
                effort="diy",
            )
        )
        return options

    if device_type in {"smart_appliance", "unclassified_energy_device"}:
        options.append(
            RetrofitOptionRead(
                kind="meter_only",
                label="External smart meter",
                description="Capture energy behavior externally when the device itself exposes no usable network API.",
                effort="diy",
            )
        )
        return options

    if device_type in {"wallbox", "pv_inverter", "battery", "grid_meter"} or "gateway" in lowered:
        options.append(
            RetrofitOptionRead(
                kind="vendor_gateway",
                label="Vendor bridge or gateway",
                description="Check whether the manufacturer offers a supported network bridge for local integration.",
                effort="vendor_specific",
                requires_vendor_gateway=True,
            )
        )
    return options


def _device_diagnosis(device: Device, candidate: DeviceCandidate | None) -> DebugDiagnosisRead:
    protocols = device.protocols or []
    evidence: list[DebugEvidenceRead] = []
    if candidate is not None:
        evidence.append(
            _evidence(
                kind="classification",
                label="Candidate reasoning",
                value=candidate.classification_reasoning,
                source="discovery",
                confidence=candidate.classification_confidence,
            )
        )
        evidence.append(
            _evidence(
                kind="source",
                label="Discovery sources",
                value=candidate.discovery_sources or ["unknown"],
                source="discovery",
            )
        )
    if protocols:
        evidence.append(_evidence(kind="protocol", label="Protocols", value=protocols, source="materialization"))
    if device.telemetry:
        evidence.append(
            _evidence(
                kind="telemetry",
                label="Telemetry keys",
                value=sorted(device.telemetry.keys()),
                source="device",
            )
        )
    for attempt in device.connector_attempts[:4]:
        evidence.append(
            _evidence(
                kind="connector",
                label=attempt.connector_name,
                value=f"{attempt.outcome}: {attempt.detail}",
                source=attempt.protocol,
            )
        )
    for incident in device.incidents[:2]:
        evidence.append(
            _evidence(
                kind="incident",
                label=incident.title,
                value=incident.summary,
                source="incident",
                confidence=incident.confidence,
            )
        )

    if candidate is not None and (
        candidate.device_type == "unclassified_energy_device" or candidate.classification_confidence < 0.65
    ):
        return DebugDiagnosisRead(
            state=ExplainabilityState.SEEN_BUT_NOT_CLASSIFIED.value,
            reason_family=ExplainabilityReasonFamily.CLASSIFICATION.value,
            reason_code=ExplainabilityReasonCode.CLASSIFICATION_CONFIDENCE_LOW.value,
            feasibility=IntegrationFeasibility.UNKNOWN.value,
            confidence=max(candidate.classification_confidence, 0.35),
            summary="Helios saw energy-relevant evidence, but the classification confidence is still too low for a stable device profile.",
            evidence=evidence,
            retrofit_options=[],
            raw_diagnostics={
                "primary_status": device.primary_status,
                "classification_confidence": candidate.classification_confidence,
            },
        )

    if device.primary_status in {
        IntegrationStatus.MONITORABLE.value,
        IntegrationStatus.CONTROLLABLE.value,
        IntegrationStatus.OPTIMIZABLE.value,
    }:
        return DebugDiagnosisRead(
            state=ExplainabilityState.INTEGRATED.value,
            reason_family=ExplainabilityReasonFamily.OPERATIONAL.value,
            reason_code=ExplainabilityReasonCode.VALIDATED_INTERFACE.value,
            feasibility=IntegrationFeasibility.NETWORK_NATIVE.value,
            confidence=max(device.confidence, 0.75),
            summary=_summary_from_status(device),
            evidence=evidence,
            retrofit_options=[],
            raw_diagnostics={
                "primary_status": device.primary_status,
                "status_tags": device.status_tags or [],
            },
        )

    if device.primary_status == IntegrationStatus.AUTHENTICATION_REQUIRED.value:
        return DebugDiagnosisRead(
            state=ExplainabilityState.CLASSIFIED_BUT_NOT_INTEGRABLE.value,
            reason_family=ExplainabilityReasonFamily.AUTH.value,
            reason_code=ExplainabilityReasonCode.AUTH_REQUIRED.value,
            feasibility=IntegrationFeasibility.NETWORK_NATIVE_BUT_AUTH_BLOCKED.value,
            confidence=max(device.confidence, 0.7),
            summary=_summary_from_status(device),
            evidence=evidence,
            retrofit_options=_retrofit_options_for_device_type(device.device_type, "auth blocked"),
            raw_diagnostics={
                "primary_status": device.primary_status,
                "status_tags": device.status_tags or [],
                "protocols": protocols,
            },
        )

    if device.primary_status in {
        IntegrationStatus.PROTOCOL_INCOMPLETE.value,
        IntegrationStatus.RECOVERY_RUNNING.value,
        IntegrationStatus.PARTIALLY_INTEGRABLE.value,
    }:
        return DebugDiagnosisRead(
            state=ExplainabilityState.CLASSIFIED_BUT_NOT_INTEGRABLE.value,
            reason_family=ExplainabilityReasonFamily.PROTOCOL.value,
            reason_code=ExplainabilityReasonCode.PROTOCOL_INCOMPLETE.value,
            feasibility=IntegrationFeasibility.NETWORK_NATIVE_BUT_UNSUPPORTED.value,
            confidence=max(device.confidence, 0.68),
            summary=_summary_from_status(device),
            evidence=evidence,
            retrofit_options=_retrofit_options_for_device_type(device.device_type),
            raw_diagnostics={
                "primary_status": device.primary_status,
                "status_tags": device.status_tags or [],
                "protocols": protocols,
            },
        )

    if device.primary_status == IntegrationStatus.NOT_INTEGRATABLE.value:
        return DebugDiagnosisRead(
            state=ExplainabilityState.CLASSIFIED_BUT_NOT_INTEGRABLE.value,
            reason_family=ExplainabilityReasonFamily.INTERFACE.value,
            reason_code=ExplainabilityReasonCode.NO_SUPPORTED_INTERFACE.value,
            feasibility=IntegrationFeasibility.NOT_REASONABLY_INTEGRABLE.value,
            confidence=max(device.confidence, 0.65),
            summary=_summary_from_status(device),
            evidence=evidence,
            retrofit_options=_retrofit_options_for_device_type(device.device_type),
            raw_diagnostics={
                "primary_status": device.primary_status,
                "status_tags": device.status_tags or [],
                "protocols": protocols,
            },
        )

    return DebugDiagnosisRead(
        state=ExplainabilityState.CLASSIFIED_BUT_NOT_INTEGRABLE.value,
        reason_family=ExplainabilityReasonFamily.INTERFACE.value,
        reason_code=ExplainabilityReasonCode.TELEMETRY_PATH_NOT_VALIDATED.value,
        feasibility=IntegrationFeasibility.NETWORK_NATIVE_BUT_UNSUPPORTED.value if protocols else IntegrationFeasibility.UNKNOWN.value,
        confidence=max(device.confidence, 0.6),
        summary=_summary_from_status(device),
        evidence=evidence,
        retrofit_options=_retrofit_options_for_device_type(device.device_type),
        raw_diagnostics={
            "primary_status": device.primary_status,
            "status_tags": device.status_tags or [],
            "protocols": protocols,
        },
    )


def get_device_debug_report(session: Session, device_id: str) -> DebugReportRead | None:
    device = _load_device(session, device_id)
    if device is None:
        return None
    candidate = _load_candidate_for_device(session, device.id)
    diagnosis = _device_diagnosis(device, candidate)
    return DebugReportRead(
        subject_type="device",
        subject_id=device.id,
        subject_label=device.name,
        matched_device_id=device.id,
        matched_candidate_id=candidate.id if candidate is not None else None,
        diagnosis=diagnosis,
    )


def get_candidate_debug_report(session: Session, candidate_id: str) -> DebugReportRead | None:
    candidate = _load_candidate(session, candidate_id)
    if candidate is None:
        return None

    device = _load_device(session, candidate.matched_device_id) if candidate.matched_device_id else None
    if device is not None:
        diagnosis = _device_diagnosis(device, candidate)
        evidence = list(diagnosis.evidence)
        evidence.append(
            _evidence(
                kind="candidate",
                label="Stable key",
                value=candidate.stable_key,
                source="candidate",
                confidence=candidate.classification_confidence,
            )
        )
        diagnosis = diagnosis.model_copy(update={"evidence": evidence})
        return DebugReportRead(
            subject_type="device_candidate",
            subject_id=candidate.id,
            subject_label=candidate.display_name,
            matched_device_id=device.id,
            matched_candidate_id=candidate.id,
            diagnosis=diagnosis,
        )

    evidence = [
        _evidence(
            kind="classification",
            label="Candidate reasoning",
            value=candidate.classification_reasoning,
            source="candidate",
            confidence=candidate.classification_confidence,
        ),
        _evidence(
            kind="source",
            label="Discovery sources",
            value=candidate.discovery_sources or ["unknown"],
            source="candidate",
        ),
    ]
    if candidate.protocols:
        evidence.append(_evidence(kind="protocol", label="Protocols", value=candidate.protocols, source="candidate"))

    if candidate.device_type == "unclassified_energy_device" or candidate.classification_confidence < 0.65:
        diagnosis = DebugDiagnosisRead(
            state=ExplainabilityState.SEEN_BUT_NOT_CLASSIFIED.value,
            reason_family=ExplainabilityReasonFamily.CLASSIFICATION.value,
            reason_code=ExplainabilityReasonCode.CLASSIFICATION_CONFIDENCE_LOW.value,
            feasibility=IntegrationFeasibility.UNKNOWN.value,
            confidence=max(candidate.classification_confidence, 0.35),
            summary="Helios captured a candidate, but the evidence is still too weak for a stable device classification.",
            evidence=evidence,
            retrofit_options=[],
            raw_diagnostics={"candidate_state": candidate.state},
        )
    else:
        diagnosis = DebugDiagnosisRead(
            state=ExplainabilityState.CLASSIFIED_BUT_NOT_INTEGRABLE.value,
            reason_family=ExplainabilityReasonFamily.INTERFACE.value,
            reason_code=ExplainabilityReasonCode.TELEMETRY_PATH_NOT_VALIDATED.value,
            feasibility=IntegrationFeasibility.NETWORK_NATIVE_BUT_UNSUPPORTED.value if candidate.protocols else IntegrationFeasibility.UNKNOWN.value,
            confidence=max(candidate.classification_confidence, 0.55),
            summary="Helios classified the candidate, but there is no materialized device integration yet.",
            evidence=evidence,
            retrofit_options=_retrofit_options_for_device_type(candidate.device_type),
            raw_diagnostics={"candidate_state": candidate.state},
        )

    return DebugReportRead(
        subject_type="device_candidate",
        subject_id=candidate.id,
        subject_label=candidate.display_name,
        matched_device_id=candidate.matched_device_id or None,
        matched_candidate_id=candidate.id,
        diagnosis=diagnosis,
    )


def _claim_match_score(
    claim: DebugExplainRequest,
    manufacturer: str,
    model: str,
    label: str,
    device_type: str,
) -> float:
    score = 0.0
    claim_manufacturer = _normalize(claim.manufacturer)
    claim_model = _normalize(claim.model)
    claim_type = claim.device_type.strip().lower()
    normalized_manufacturer = _normalize(manufacturer)
    normalized_model = _normalize(model)
    normalized_label = _normalize(label)

    if claim_manufacturer and claim_manufacturer == normalized_manufacturer:
        score += 0.35
    elif claim_manufacturer and claim_manufacturer in normalized_label:
        score += 0.18

    if claim_model and claim_model == normalized_model:
        score += 0.45
    elif claim_model and claim_model in normalized_label:
        score += 0.25

    if claim_type and claim_type == device_type.lower():
        score += 0.2

    claim_tokens = _tokenize(claim.manufacturer, claim.model, claim.device_type)
    subject_tokens = _tokenize(manufacturer, model, label, device_type)
    overlap = len(claim_tokens & subject_tokens)
    if overlap:
        score += min(overlap * 0.04, 0.2)

    return min(score, 1.0)


def _manual_claim_context_evidence(session: Session, payload: DebugExplainRequest) -> tuple[list[DebugEvidenceRead], dict[str, Any]]:
    site = _get_site(session)
    discovery_runs = _list_recent_discovery_runs(session)
    evidence = [
        _evidence(kind="claim", label="Claimed manufacturer", value=payload.manufacturer or "unknown", source="user"),
        _evidence(kind="claim", label="Claimed model", value=payload.model or "unknown", source="user"),
        _evidence(kind="claim", label="Claimed device type", value=payload.device_type or "unknown", source="user"),
    ]
    if payload.notes:
        evidence.append(_evidence(kind="claim", label="Claim notes", value=payload.notes, source="user"))
    if site is not None:
        evidence.append(_evidence(kind="site", label="Configured subnet", value=site.local_subnet, source="site"))
        if site.mqtt_broker_url:
            evidence.append(_evidence(kind="site", label="Configured MQTT broker", value=site.mqtt_broker_url, source="site"))
    if discovery_runs:
        latest_run = discovery_runs[0]
        evidence.append(
            _evidence(
                kind="discovery",
                label="Latest discovery sources",
                value=latest_run.source_names or ["unknown"],
                source="discovery",
            )
        )
    return evidence, {
        "latest_discovery_sources": discovery_runs[0].source_names if discovery_runs else [],
        "latest_discovery_status": discovery_runs[0].status if discovery_runs else None,
        "claim": payload.model_dump(),
    }


def _find_best_knowledge_entry(session: Session, payload: DebugExplainRequest) -> tuple[KnowledgeEntry | None, float]:
    entries = session.scalars(select(KnowledgeEntry).order_by(KnowledgeEntry.updated_at.desc())).all()
    best_entry: KnowledgeEntry | None = None
    best_score = 0.0
    for entry in entries:
        score = _claim_match_score(payload, entry.manufacturer, entry.model, entry.title, entry.device_type)
        if score > best_score:
            best_entry = entry
            best_score = score
    return best_entry, best_score


def _debug_report_from_knowledge(
    session: Session,
    payload: DebugExplainRequest,
    entry: KnowledgeEntry,
    score: float,
) -> DebugReportRead:
    evidence, raw_diagnostics = _manual_claim_context_evidence(session, payload)
    knowledge_evidence = [DebugEvidenceRead.model_validate(item) for item in (entry.evidence or [])]
    diagnosis = DebugDiagnosisRead(
        state=ExplainabilityState.NOT_FOUND.value,
        reason_family=entry.reason_family,
        reason_code=entry.reason_code,
        feasibility=entry.feasibility,
        confidence=max(entry.confidence, min(score, 0.95)),
        summary=entry.summary,
        evidence=[
            _evidence(
                kind="knowledge",
                label="Matched knowledge entry",
                value=entry.fingerprint_key,
                source=entry.origin,
                confidence=score,
            ),
            *evidence,
            *knowledge_evidence,
        ],
        retrofit_options=[RetrofitOptionRead.model_validate(item) for item in (entry.retrofit_options or [])],
        raw_diagnostics={
            **raw_diagnostics,
            "knowledge_entry_id": entry.id,
            "knowledge_match_score": round(score, 3),
            "knowledge_origin": entry.origin,
        },
    )
    return DebugReportRead(
        subject_type="manual_claim",
        subject_id=None,
        subject_label=_claim_label(payload),
        matched_device_id=None,
        matched_candidate_id=None,
        diagnosis=diagnosis,
    )


def _build_not_found_claim_diagnosis(session: Session, payload: DebugExplainRequest) -> DebugDiagnosisRead:
    label = _claim_label(payload).lower()
    notes = payload.notes.lower()
    combined = " ".join(part for part in [label, notes] if part)

    network_hints = any(
        token in combined
        for token in ("lan", "wlan", "wifi", "ethernet", "mqtt", "modbus", "http", "api", "tcp", "web")
    )
    vendor_hints = any(token in combined for token in ("gateway", "cloud", "oauth", "app", "vendor portal"))
    legacy_hints = bool(LEGACY_HINT_PATTERN.search(combined)) or any(token in combined for token in ("legacy", "old"))
    sg_ready_hint = "sg ready" in combined or "sg-ready" in combined

    evidence, raw_diagnostics = _manual_claim_context_evidence(session, payload)
    device_type = payload.device_type.strip().lower()

    if device_type == "heat_pump" and (legacy_hints or sg_ready_hint or not vendor_hints):
        return DebugDiagnosisRead(
            state=ExplainabilityState.NOT_FOUND.value,
            reason_family=ExplainabilityReasonFamily.INTERFACE.value,
            reason_code=ExplainabilityReasonCode.NO_SUPPORTED_INTERFACE.value,
            feasibility=IntegrationFeasibility.DRY_CONTACT_POSSIBLE.value,
            confidence=0.8 if sg_ready_hint or legacy_hints else 0.68,
            summary="No network-reachable interface was matched for the claimed heat pump. The device likely lacks a supported local LAN, WLAN, or API path.",
            evidence=evidence,
            retrofit_options=_retrofit_options_for_device_type("heat_pump", notes),
            raw_diagnostics=raw_diagnostics,
        )

    if network_hints:
        return DebugDiagnosisRead(
            state=ExplainabilityState.NOT_FOUND.value,
            reason_family=ExplainabilityReasonFamily.NETWORK.value,
            reason_code=ExplainabilityReasonCode.NETWORK_UNREACHABLE.value,
            feasibility=IntegrationFeasibility.NETWORK_NATIVE.value,
            confidence=0.72,
            summary="The claim suggests a network-reachable interface, but the latest discovery runs did not match it to any device candidate.",
            evidence=evidence,
            retrofit_options=[],
            raw_diagnostics=raw_diagnostics,
        )

    if vendor_hints:
        return DebugDiagnosisRead(
            state=ExplainabilityState.NOT_FOUND.value,
            reason_family=ExplainabilityReasonFamily.VENDOR.value,
            reason_code=ExplainabilityReasonCode.GATEWAY_REQUIRED.value,
            feasibility=IntegrationFeasibility.GATEWAY_POSSIBLE.value,
            confidence=0.67,
            summary="The claim points to a vendor-managed integration path. Helios did not find a direct local network interface for the device.",
            evidence=evidence,
            retrofit_options=_retrofit_options_for_device_type(device_type or "unclassified_energy_device", notes),
            raw_diagnostics=raw_diagnostics,
        )

    feasibility = (
        IntegrationFeasibility.METER_ONLY_POSSIBLE.value
        if device_type in {"smart_appliance", "heat_pump", "wallbox"}
        else IntegrationFeasibility.UNKNOWN.value
    )
    return DebugDiagnosisRead(
        state=ExplainabilityState.NOT_FOUND.value,
        reason_family=ExplainabilityReasonFamily.UNKNOWN.value,
        reason_code=ExplainabilityReasonCode.NO_MATCH_IN_DISCOVERY.value,
        feasibility=feasibility,
        confidence=0.58,
        summary="Helios did not match the claimed device in the latest discovery runs and does not yet have enough evidence to identify the limiting factor.",
        evidence=evidence,
        retrofit_options=_retrofit_options_for_device_type(device_type or "unclassified_energy_device", notes),
        raw_diagnostics=raw_diagnostics,
    )


def explain_manual_claim(session: Session, payload: DebugExplainRequest) -> DebugReportRead:
    devices = session.scalars(select(Device).order_by(Device.name)).all()
    best_device: Device | None = None
    best_score = 0.0
    for device in devices:
        score = _claim_match_score(payload, device.manufacturer, device.model, device.name, device.device_type)
        if score > best_score:
            best_device = device
            best_score = score

    if best_device is not None and best_score >= 0.55:
        existing_report = get_device_debug_report(session, best_device.id)
        if existing_report is None:
            raise RuntimeError("Matched device could not be reloaded for explainability.")
        diagnosis = existing_report.diagnosis.model_copy(
            update={
                "raw_diagnostics": {
                    **existing_report.diagnosis.raw_diagnostics,
                    "claim_match_score": round(best_score, 3),
                    "claim": payload.model_dump(),
                }
            }
        )
        return DebugReportRead(
            subject_type="manual_claim",
            subject_id=None,
            subject_label=_claim_label(payload),
            matched_device_id=best_device.id,
            matched_candidate_id=existing_report.matched_candidate_id,
            diagnosis=diagnosis,
        )

    knowledge_entry, knowledge_score = _find_best_knowledge_entry(session, payload)
    if knowledge_entry is not None and knowledge_score >= 0.58:
        return _debug_report_from_knowledge(session, payload, knowledge_entry, knowledge_score)

    diagnosis = _build_not_found_claim_diagnosis(session, payload)
    return DebugReportRead(
        subject_type="manual_claim",
        subject_id=None,
        subject_label=_claim_label(payload),
        matched_device_id=None,
        matched_candidate_id=None,
        diagnosis=diagnosis,
    )
