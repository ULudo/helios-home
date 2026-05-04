from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.models import Proposal, UserDecisionRequest
from app.home_graph.service import resolve_entity
from app.work_store.service import (
    create_proposal_with_decision_request,
    latest_accepted_role_candidate_for_entity,
)


ROLE_BINDING_EFFECTS_IF_APPROVED = ["create_role_candidate", "mark_role_candidate_user_approved"]
ROLE_BINDING_EFFECTS_NOT_INCLUDED = ["no_pairing", "no_commissioning", "no_control", "no_device_configuration"]


def _find_existing_role_proposal(
    session: Session,
    *,
    site_id: int,
    entity_ref: str,
    role: str,
) -> tuple[Proposal, UserDecisionRequest | None] | None:
    proposals = session.scalars(
        select(Proposal)
        .where(
            Proposal.site_id == site_id,
            Proposal.proposal_type == "role_binding",
            Proposal.status.in_(["awaiting_user_decision", "user_approved"]),
        )
        .order_by(Proposal.created_at.desc())
    ).all()
    for proposal in proposals:
        payload = proposal.payload or {}
        if payload.get("entity_ref") != entity_ref or payload.get("role") != role:
            continue
        decision_request = session.scalar(
            select(UserDecisionRequest)
            .where(UserDecisionRequest.proposal_id == proposal.id)
            .order_by(UserDecisionRequest.created_at.desc())
            .limit(1)
        )
        return proposal, decision_request
    return None


def prepare_role_binding_proposal(
    session: Session,
    *,
    site_id: int,
    entity_ref: str,
    role: str,
    label: str,
    rationale: str,
    thread_id: str | None = None,
    turn_id: str | None = None,
    task_id: str | None = None,
) -> dict:
    entity = resolve_entity(session, entity_ref)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_ref}")
    safe_label = label.strip() or entity.display_name
    accepted = latest_accepted_role_candidate_for_entity(
        session,
        site_id=site_id,
        entity_ref=entity.id,
        role=role,
    )
    if accepted is not None:
        return {
            "proposal_ref": accepted.get("proposal_id", ""),
            "decision_request_ref": "",
            "status": "already_user_approved",
            "proposal_type": "role_binding",
            "entity_ref": entity.id,
            "role": role,
            "label": accepted["label"],
            "decision_required": False,
            "target_refs": [entity.id, f"role:{role}", accepted["role_candidate_ref"]],
            "risk_level": "medium",
            "role_candidate": accepted,
        }

    existing = _find_existing_role_proposal(
        session,
        site_id=site_id,
        entity_ref=entity.id,
        role=role,
    )
    if existing is not None:
        proposal, decision_request = existing
        return {
            "proposal_ref": proposal.id,
            "decision_request_ref": decision_request.id if decision_request is not None else "",
            "status": proposal.status,
            "proposal_type": proposal.proposal_type,
            "entity_ref": entity.id,
            "role": role,
            "label": safe_label,
            "decision_required": decision_request is not None and decision_request.status == "pending",
            "target_refs": proposal.target_refs or [],
            "risk_level": proposal.risk_level,
            "effects_if_approved": ROLE_BINDING_EFFECTS_IF_APPROVED,
            "effects_not_included": ROLE_BINDING_EFFECTS_NOT_INCLUDED,
            "existing": True,
        }

    proposal, decision_request = create_proposal_with_decision_request(
        session,
        site_id=site_id,
        thread_id=thread_id,
        turn_id=turn_id,
        task_id=task_id,
        proposal_type="role_binding",
        title="role_binding_proposal",
        summary="role_binding_proposal",
        payload={
            "entity_ref": entity.id,
            "source_type": entity.source_type,
            "source_id": entity.source_id,
            "role": role,
            "label": safe_label,
            "rationale": rationale,
            "decision_required": True,
            "effects_if_approved": ROLE_BINDING_EFFECTS_IF_APPROVED,
            "effects_not_included": ROLE_BINDING_EFFECTS_NOT_INCLUDED,
            "status_after_decision": "role_candidate_accepted",
        },
        target_refs=[entity.id, f"role:{role}"],
        question="",
        risk_level="medium",
    )
    return {
        "proposal_ref": proposal.id,
        "decision_request_ref": decision_request.id,
        "status": proposal.status,
        "proposal_type": proposal.proposal_type,
        "entity_ref": entity.id,
        "role": role,
        "label": safe_label,
        "decision_required": True,
        "target_refs": proposal.target_refs or [],
        "risk_level": proposal.risk_level,
        "effects_if_approved": ROLE_BINDING_EFFECTS_IF_APPROVED,
        "effects_not_included": ROLE_BINDING_EFFECTS_NOT_INCLUDED,
    }
