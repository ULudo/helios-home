from __future__ import annotations

import re
from datetime import timezone, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import AuditEvent, DebugCase, DebugProbeRun, KnowledgeEntry, ResearchFinding, Site, utcnow
from app.domain.schemas import (
    DebugCaseRead,
    DebugDiagnosisRead,
    DebugExplainRequest,
    DebugProbeRunRead,
    KnowledgeEntryRead,
    KnowledgeImportResultRead,
    KnowledgePackRead,
    KnowledgePackWrite,
    ResearchFindingCreate,
    ResearchFindingRead,
    ProbeCheckRead,
)

TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _normalize(value: str) -> str:
    return " ".join(TOKEN_PATTERN.findall(value.lower()))


def _fingerprint_key(
    manufacturer: str,
    model: str,
    device_type: str,
    reason_code: str,
    subject_label: str = "",
) -> str:
    parts = [
        _normalize(manufacturer) or "unknown-vendor",
        _normalize(model) or _normalize(subject_label) or "unknown-model",
        _normalize(device_type) or "unknown-type",
        _normalize(reason_code) or "unknown-reason",
    ]
    return "::".join(parts)


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


def _serialize_finding(finding: ResearchFinding) -> ResearchFindingRead:
    return ResearchFindingRead(
        id=finding.id,
        source_type=finding.source_type,
        title=finding.title,
        summary=finding.summary,
        url=finding.url,
        details=finding.details or {},
        created_at=finding.created_at,
    )


def _serialize_case(debug_case: DebugCase) -> DebugCaseRead:
    return DebugCaseRead(
        id=debug_case.id,
        subject_label=debug_case.subject_label,
        manufacturer=debug_case.manufacturer,
        model=debug_case.model,
        device_type=debug_case.device_type,
        notes=debug_case.notes,
        status=debug_case.status,
        matched_device_id=debug_case.matched_device_id,
        matched_candidate_id=debug_case.matched_candidate_id,
        diagnosis=DebugDiagnosisRead.model_validate(debug_case.diagnosis_snapshot or {}),
        findings=[_serialize_finding(finding) for finding in debug_case.findings],
        probe_runs=[_serialize_probe_run(probe_run) for probe_run in debug_case.probe_runs],
        created_at=debug_case.created_at,
        updated_at=debug_case.updated_at,
    )


def _serialize_probe_run(probe_run: DebugProbeRun) -> DebugProbeRunRead:
    return DebugProbeRunRead(
        id=probe_run.id,
        probe_type=probe_run.probe_type,
        status=probe_run.status,
        summary=probe_run.summary,
        checks=[ProbeCheckRead.model_validate(item) for item in (probe_run.checks or [])],
        created_at=probe_run.created_at,
    )


def _serialize_knowledge_entry(entry: KnowledgeEntry) -> KnowledgeEntryRead:
    diagnosis = DebugDiagnosisRead(
        state="knowledge",
        reason_family=entry.reason_family,
        reason_code=entry.reason_code,
        feasibility=entry.feasibility,
        confidence=entry.confidence,
        summary=entry.summary,
        evidence=entry.evidence or [],
        next_actions=entry.next_actions or [],
        retrofit_options=entry.retrofit_options or [],
        raw_diagnostics=entry.raw_diagnostics or {},
    )
    return KnowledgeEntryRead(
        id=entry.id,
        fingerprint_key=entry.fingerprint_key,
        title=entry.title,
        manufacturer=entry.manufacturer,
        model=entry.model,
        device_type=entry.device_type,
        reason_family=entry.reason_family,
        reason_code=entry.reason_code,
        feasibility=entry.feasibility,
        confidence=entry.confidence,
        summary=entry.summary,
        next_actions=diagnosis.next_actions,
        retrofit_options=diagnosis.retrofit_options,
        evidence=diagnosis.evidence,
        raw_diagnostics=diagnosis.raw_diagnostics,
        origin=entry.origin,
        source_case_id=entry.source_case_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def list_debug_cases(session: Session) -> list[DebugCaseRead]:
    cases = session.scalars(
        select(DebugCase)
        .options(
            selectinload(DebugCase.findings),
            selectinload(DebugCase.probe_runs),
        )
        .order_by(DebugCase.updated_at.desc())
    ).all()
    return [_serialize_case(debug_case) for debug_case in cases]


def get_debug_case(session: Session, case_id: int) -> DebugCaseRead | None:
    debug_case = session.scalar(
        select(DebugCase)
        .where(DebugCase.id == case_id)
        .options(
            selectinload(DebugCase.findings),
            selectinload(DebugCase.probe_runs),
        )
    )
    if debug_case is None:
        return None
    return _serialize_case(debug_case)


def create_debug_case(session: Session, payload: DebugExplainRequest) -> DebugCaseRead:
    from app.services.explainability import explain_manual_claim

    site = _get_site(session)
    now = utcnow()
    report = explain_manual_claim(session, payload)
    debug_case = DebugCase(
        site_id=site.id,
        subject_label=report.subject_label,
        manufacturer=payload.manufacturer.strip(),
        model=payload.model.strip(),
        device_type=payload.device_type.strip(),
        notes=payload.notes.strip(),
        status="open",
        matched_device_id=report.matched_device_id,
        matched_candidate_id=report.matched_candidate_id,
        diagnosis_snapshot=report.diagnosis.model_dump(),
        created_at=now,
        updated_at=now,
    )
    session.add(debug_case)
    session.flush()
    session.add(
        AuditEvent(
            actor="user",
            action="create_debug_case",
            target_type="debug_case",
            target_id=str(debug_case.id),
            summary=f"Opened debug case for {debug_case.subject_label}.",
            details={
                "manufacturer": debug_case.manufacturer,
                "model": debug_case.model,
                "device_type": debug_case.device_type,
                "matched_device_id": debug_case.matched_device_id,
                "matched_candidate_id": debug_case.matched_candidate_id,
            },
            created_at=now,
        )
    )
    session.commit()
    session.refresh(debug_case)
    return get_debug_case(session, debug_case.id) or _serialize_case(debug_case)


def add_research_finding(session: Session, case_id: int, payload: ResearchFindingCreate) -> ResearchFindingRead:
    debug_case = session.get(DebugCase, case_id)
    if debug_case is None:
        raise KeyError(case_id)
    now = utcnow()
    finding = ResearchFinding(
        debug_case_id=debug_case.id,
        source_type=payload.source_type,
        title=payload.title,
        summary=payload.summary,
        url=payload.url,
        details=payload.details,
        created_at=now,
    )
    debug_case.updated_at = now
    session.add(finding)
    session.add(debug_case)
    session.add(
        AuditEvent(
            actor="user",
            action="add_research_finding",
            target_type="debug_case",
            target_id=str(debug_case.id),
            summary=f"Added a {payload.source_type} finding to debug case {debug_case.id}.",
            details={"title": payload.title, "url": payload.url},
            created_at=now,
        )
    )
    session.commit()
    session.refresh(finding)
    return _serialize_finding(finding)


def promote_debug_case_to_knowledge(session: Session, case_id: int) -> KnowledgeEntryRead:
    debug_case = session.scalar(
        select(DebugCase)
        .where(DebugCase.id == case_id)
        .options(
            selectinload(DebugCase.findings),
            selectinload(DebugCase.probe_runs),
        )
    )
    if debug_case is None:
        raise KeyError(case_id)

    diagnosis = DebugDiagnosisRead.model_validate(debug_case.diagnosis_snapshot or {})
    evidence = list(diagnosis.evidence)
    for finding in debug_case.findings:
        evidence.append(
            {
                "kind": "research",
                "label": finding.title,
                "value": finding.summary,
                "source": finding.source_type,
                "confidence": None,
            }
        )
    for probe_run in debug_case.probe_runs:
        evidence.append(
            {
                "kind": "probe_run",
                "label": probe_run.probe_type,
                "value": probe_run.summary,
                "source": "targeted_probe",
                "confidence": None,
            }
        )
        for check in probe_run.checks or []:
            evidence.append(
                {
                    "kind": "probe_check",
                    "label": check.get("name", "probe_check"),
                    "value": check.get("summary", ""),
                    "source": "targeted_probe",
                    "confidence": check.get("confidence"),
                }
            )
    fingerprint_key = _fingerprint_key(
        manufacturer=debug_case.manufacturer,
        model=debug_case.model,
        device_type=debug_case.device_type,
        reason_code=diagnosis.reason_code,
        subject_label=debug_case.subject_label,
    )
    site = _get_site(session)
    now = utcnow()
    entry = session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.fingerprint_key == fingerprint_key))
    if entry is None:
        entry = KnowledgeEntry(site_id=site.id, fingerprint_key=fingerprint_key, created_at=now)
        session.add(entry)
    entry.title = debug_case.subject_label
    entry.manufacturer = debug_case.manufacturer
    entry.model = debug_case.model
    entry.device_type = debug_case.device_type
    entry.reason_family = diagnosis.reason_family
    entry.reason_code = diagnosis.reason_code
    entry.feasibility = diagnosis.feasibility
    entry.confidence = diagnosis.confidence
    entry.summary = diagnosis.summary
    entry.next_actions = diagnosis.next_actions
    entry.retrofit_options = [option.model_dump() if hasattr(option, "model_dump") else option for option in diagnosis.retrofit_options]
    entry.evidence = [item.model_dump() if hasattr(item, "model_dump") else item for item in evidence]
    entry.raw_diagnostics = diagnosis.raw_diagnostics
    entry.origin = "debug_case"
    entry.source_case_id = debug_case.id
    entry.updated_at = now

    debug_case.status = "knowledge_captured"
    debug_case.updated_at = now
    session.add(debug_case)
    session.add(
        AuditEvent(
            actor="user",
            action="promote_debug_case_to_knowledge",
            target_type="knowledge_entry",
            target_id=fingerprint_key,
            summary=f"Promoted debug case {debug_case.id} into the local knowledge base.",
            details={"debug_case_id": debug_case.id, "fingerprint_key": fingerprint_key},
            created_at=now,
        )
    )
    session.commit()
    session.refresh(entry)
    return _serialize_knowledge_entry(entry)


def list_knowledge_entries(session: Session) -> list[KnowledgeEntryRead]:
    entries = session.scalars(select(KnowledgeEntry).order_by(KnowledgeEntry.updated_at.desc())).all()
    return [_serialize_knowledge_entry(entry) for entry in entries]


def export_knowledge_pack(session: Session) -> KnowledgePackRead:
    return KnowledgePackRead(
        exported_at=datetime.now(timezone.utc),
        entries=list_knowledge_entries(session),
    )


def import_knowledge_pack(session: Session, payload: KnowledgePackWrite) -> KnowledgeImportResultRead:
    site = _get_site(session)
    imported_count = 0
    updated_count = 0
    now = utcnow()
    for item in payload.entries:
        entry = session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.fingerprint_key == item.fingerprint_key))
        is_new = entry is None
        if entry is None:
            entry = KnowledgeEntry(site_id=site.id, fingerprint_key=item.fingerprint_key, created_at=now)
            session.add(entry)
        entry.title = item.title
        entry.manufacturer = item.manufacturer
        entry.model = item.model
        entry.device_type = item.device_type
        entry.reason_family = item.reason_family
        entry.reason_code = item.reason_code
        entry.feasibility = item.feasibility
        entry.confidence = item.confidence
        entry.summary = item.summary
        entry.next_actions = item.next_actions
        entry.retrofit_options = [option.model_dump() for option in item.retrofit_options]
        entry.evidence = [item_evidence.model_dump() for item_evidence in item.evidence]
        entry.raw_diagnostics = item.raw_diagnostics
        entry.origin = item.origin
        entry.updated_at = now
        if is_new:
            imported_count += 1
        else:
            updated_count += 1

    session.add(
        AuditEvent(
            actor="user",
            action="import_knowledge_pack",
            target_type="knowledge_pack",
            target_id="local",
            summary="Imported local knowledge entries.",
            details={"imported_count": imported_count, "updated_count": updated_count},
            created_at=now,
        )
    )
    session.commit()
    total_entries = len(session.scalars(select(KnowledgeEntry.id)).all())
    return KnowledgeImportResultRead(
        imported_count=imported_count,
        updated_count=updated_count,
        total_entries=total_entries,
    )
