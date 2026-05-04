from __future__ import annotations

from sqlalchemy.orm import Session

from app.home_graph.service import assess_device


def assess_home_graph_entity(session: Session, *, entity_ref: str, question: str = "") -> dict:
    result = assess_device(session, entity_reference=entity_ref, question=question)
    return {
        "assessment_ref": result.assessment.id,
        "subject_ref": result.assessment.subject_ref,
        "assessment_type": result.assessment.summary,
        "possible_roles": result.assessment.possible_roles or [],
        "evidence_refs": result.assessment.evidence_refs or [],
        "confidence": result.assessment.confidence,
        "status": result.assessment.status,
        "evidence": [
            {
                "ref": evidence.id,
                "type": evidence.evidence_type,
                "source": evidence.source,
                "payload": evidence.payload or {},
                "confidence": evidence.confidence,
                "trust": evidence.trust,
            }
            for evidence in result.evidence
        ],
    }
