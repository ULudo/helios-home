from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.models import Proposal, ProtocolEndpoint, UserDecisionRequest
from app.home_graph.service import resolve_entity
from app.work_store.service import (
    create_proposal_with_decision_request,
    latest_accepted_role_candidate_for_entity,
)


ROLE_BINDING_EFFECTS_IF_APPROVED = ["create_role_candidate", "mark_role_candidate_user_approved"]
ROLE_BINDING_EFFECTS_NOT_INCLUDED = ["no_pairing", "no_commissioning", "no_control", "no_device_configuration"]

INTEGRATION_PATH_PROTOCOLS = {
    "eebus_spine": {"eebus_ship"},
    "modbus_tcp": {"modbus_tcp"},
    "sunspec_modbus": {"modbus_tcp"},
    "http_local": {"http_local"},
    "mqtt": {"mqtt"},
    "vendor_cloud": {"vendor_cloud"},
}


def _find_existing_role_proposal(
    session: Session,
    *,
    site_id: int,
    entity_ref: str,
    role: str,
    endpoint_ref: str = "",
    integration_path: str = "",
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
        if (payload.get("endpoint_ref") or "") != endpoint_ref:
            continue
        if (payload.get("integration_path") or "") != integration_path:
            continue
        decision_request = session.scalar(
            select(UserDecisionRequest)
            .where(UserDecisionRequest.proposal_id == proposal.id)
            .order_by(UserDecisionRequest.created_at.desc())
            .limit(1)
        )
        return proposal, decision_request
    return None


def _validate_endpoint_selection(
    session: Session,
    *,
    entity_ref: str,
    endpoint_ref: str,
    integration_path: str,
) -> ProtocolEndpoint | None:
    if not endpoint_ref and not integration_path:
        return None
    if not endpoint_ref:
        raise ValueError("endpoint_ref is required when integration_path is provided.")
    endpoint = session.get(ProtocolEndpoint, endpoint_ref)
    if endpoint is None:
        raise ValueError(f"Unknown protocol endpoint: {endpoint_ref}")
    if endpoint.owner_ref != entity_ref:
        raise ValueError(f"Endpoint {endpoint_ref} does not belong to {entity_ref}.")
    if integration_path:
        allowed_protocols = INTEGRATION_PATH_PROTOCOLS.get(integration_path)
        if allowed_protocols is None:
            raise ValueError(f"Unsupported integration path: {integration_path}")
        if endpoint.protocol not in allowed_protocols:
            raise ValueError(
                f"Integration path {integration_path} is not compatible with endpoint protocol {endpoint.protocol}."
            )
    return endpoint


def prepare_role_binding_proposal(
    session: Session,
    *,
    site_id: int,
    entity_ref: str,
    role: str,
    endpoint_ref: str = "",
    integration_path: str = "",
    label: str = "",
    rationale: str = "",
    thread_id: str | None = None,
    turn_id: str | None = None,
    task_id: str | None = None,
) -> dict:
    requested_endpoint_ref = endpoint_ref.strip()
    requested_integration_path = integration_path.strip()
    entity = resolve_entity(session, entity_ref)
    if entity is None:
        raise ValueError(f"Unknown Home Graph entity: {entity_ref}")
    endpoint = _validate_endpoint_selection(
        session,
        entity_ref=entity.id,
        endpoint_ref=requested_endpoint_ref,
        integration_path=requested_integration_path,
    )
    safe_label = label.strip() or entity.display_name
    accepted = latest_accepted_role_candidate_for_entity(
        session,
        site_id=site_id,
        entity_ref=entity.id,
        role=role,
    )
    if accepted is not None:
        if requested_endpoint_ref and accepted.get("endpoint_ref", "") != requested_endpoint_ref:
            raise ValueError("A role candidate is already accepted for this entity and role with a different endpoint.")
        if requested_integration_path and accepted.get("integration_path", "") != requested_integration_path:
            raise ValueError("A role candidate is already accepted for this entity and role with a different integration path.")
        return {
            "proposal_ref": accepted.get("proposal_id", ""),
            "decision_request_ref": "",
            "status": "already_user_approved",
            "proposal_type": "role_binding",
            "entity_ref": entity.id,
            "role": role,
            "label": accepted["label"],
            "endpoint_ref": accepted.get("endpoint_ref", ""),
            "integration_path": accepted.get("integration_path", ""),
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
        endpoint_ref=requested_endpoint_ref,
        integration_path=requested_integration_path,
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
            "endpoint_ref": proposal.payload.get("endpoint_ref", ""),
            "integration_path": proposal.payload.get("integration_path", ""),
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
            "endpoint_ref": endpoint.id if endpoint is not None else "",
            "endpoint_protocol": endpoint.protocol if endpoint is not None else "",
            "integration_path": requested_integration_path,
            "rationale": rationale,
            "decision_required": True,
            "effects_if_approved": ROLE_BINDING_EFFECTS_IF_APPROVED,
            "effects_not_included": ROLE_BINDING_EFFECTS_NOT_INCLUDED,
            "status_after_decision": "role_candidate_accepted",
        },
        target_refs=[ref for ref in [entity.id, f"role:{role}", endpoint.id if endpoint is not None else ""] if ref],
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
        "endpoint_ref": endpoint.id if endpoint is not None else "",
        "endpoint_protocol": endpoint.protocol if endpoint is not None else "",
        "integration_path": requested_integration_path,
        "decision_required": True,
        "target_refs": proposal.target_refs or [],
        "risk_level": proposal.risk_level,
        "effects_if_approved": ROLE_BINDING_EFFECTS_IF_APPROVED,
        "effects_not_included": ROLE_BINDING_EFFECTS_NOT_INCLUDED,
    }
