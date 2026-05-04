from __future__ import annotations

from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentTask,
    AuditEvent,
    Blocker,
    HomeGraphEntity,
    HomeGraphEvidence,
    Proposal,
    TaskStep,
    UserDecision,
    UserDecisionRequest,
    utcnow,
)


def new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def stable_ref(prefix: str, value: str) -> str:
    return f"{prefix}-{uuid5(NAMESPACE_URL, value).hex[:16]}"


def create_task(
    session: Session,
    *,
    site_id: int,
    task_type: str,
    title: str,
    goal: str,
    thread_id: str | None = None,
    turn_id: str | None = None,
    target_refs: list[str] | None = None,
    context: dict | None = None,
) -> AgentTask:
    task = AgentTask(
        id=new_ref("task"),
        site_id=site_id,
        thread_id=thread_id,
        turn_id=turn_id,
        task_type=task_type,
        title=title,
        goal=goal,
        status="running",
        target_refs=target_refs or [],
        context=context or {},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(task)
    return task


def add_task_step(
    session: Session,
    *,
    task_id: str,
    step_key: str,
    title: str,
    status: str = "completed",
    summary: str = "",
    result: dict | None = None,
) -> TaskStep:
    step = TaskStep(
        id=new_ref("step"),
        task_id=task_id,
        step_key=step_key,
        title=title,
        status=status,
        summary=summary,
        result=result or {},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(step)
    return step


def complete_task(session: Session, task: AgentTask, *, summary: str = "") -> AgentTask:
    task.status = "completed"
    task.completed_at = utcnow()
    task.updated_at = utcnow()
    context = dict(task.context or {})
    if summary:
        context["completion_summary"] = summary
    task.context = context
    session.add(task)
    return task


def add_blocker(
    session: Session,
    *,
    blocker_type: str,
    summary: str,
    task_id: str | None = None,
    subject_ref: str = "",
    details: dict | None = None,
) -> Blocker:
    blocker = Blocker(
        id=new_ref("blocker"),
        task_id=task_id,
        subject_ref=subject_ref,
        blocker_type=blocker_type,
        summary=summary,
        status="open",
        details=details or {},
        created_at=utcnow(),
    )
    session.add(blocker)
    return blocker


def create_proposal_with_decision_request(
    session: Session,
    *,
    site_id: int,
    proposal_type: str,
    title: str,
    summary: str,
    payload: dict,
    question: str,
    thread_id: str | None = None,
    turn_id: str | None = None,
    task_id: str | None = None,
    target_refs: list[str] | None = None,
    risk_level: str = "medium",
) -> tuple[Proposal, UserDecisionRequest]:
    proposal = Proposal(
        id=new_ref("proposal"),
        site_id=site_id,
        thread_id=thread_id,
        turn_id=turn_id,
        task_id=task_id,
        proposal_type=proposal_type,
        title=title,
        summary=summary,
        payload=payload,
        target_refs=target_refs or [],
        risk_level=risk_level,
        status="awaiting_user_decision",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(proposal)
    session.flush()

    decision_request = UserDecisionRequest(
        id=new_ref("decision"),
        site_id=site_id,
        thread_id=thread_id,
        turn_id=turn_id,
        proposal_id=proposal.id,
        question=question,
        options=["approve", "reject"],
        risk_level=risk_level,
        status="pending",
        created_at=utcnow(),
    )
    session.add(decision_request)
    session.add(
        AuditEvent(
            actor="agent",
            action="create_user_decision_request",
            target_type="proposal",
            target_id=proposal.id,
            summary=summary,
            details={
                "decision_request_id": decision_request.id,
                "proposal_type": proposal_type,
                "risk_level": risk_level,
                "target_refs": target_refs or [],
            },
            created_at=utcnow(),
        )
    )
    return proposal, decision_request


def _role_candidate_ref(site_id: int, entity_ref: str, role: str) -> str:
    return stable_ref("role-candidate", f"{site_id}:{entity_ref}:{role}")


def _find_open_commissioning_task(session: Session, *, site_id: int, role_candidate_ref: str) -> AgentTask | None:
    tasks = session.scalars(
        select(AgentTask)
        .where(
            AgentTask.site_id == site_id,
            AgentTask.task_type == "commission_role_candidate",
            AgentTask.status.in_(["open", "running", "blocked"]),
        )
        .order_by(AgentTask.created_at.desc())
    ).all()
    for task in tasks:
        if role_candidate_ref in (task.target_refs or []):
            return task
    return None


def materialize_approved_role_candidate(
    session: Session,
    *,
    proposal: Proposal,
    decision_request: UserDecisionRequest,
) -> dict | None:
    if proposal.proposal_type != "role_binding":
        return None
    payload = proposal.payload or {}
    entity_ref = str(payload.get("entity_ref") or "").strip()
    role = str(payload.get("role") or "").strip()
    label = str(payload.get("label") or entity_ref).strip()
    if not entity_ref or not role:
        return None

    now = utcnow()
    candidate_ref = _role_candidate_ref(proposal.site_id, entity_ref, role)
    existing = session.get(HomeGraphEntity, candidate_ref)
    if existing is None:
        existing = HomeGraphEntity(
            id=candidate_ref,
            site_id=proposal.site_id,
            entity_type="role_candidate",
            created_at=now,
        )
        session.add(existing)
    existing.source_type = "proposal"
    existing.source_id = proposal.id
    existing.display_name = label
    existing.semantic_type = role
    existing.status = "accepted"
    existing.properties = {
        "entity_ref": entity_ref,
        "role": role,
        "label": label,
        "proposal_id": proposal.id,
        "decision_request_id": decision_request.id,
        "accepted_at": now.isoformat(),
        "readiness_status": "awaiting_commissioning",
        "next_step": "commissioning_workflow_required",
    }
    existing.updated_at = now

    session.add(
        HomeGraphEvidence(
            id=new_ref("evidence"),
            site_id=proposal.site_id,
            subject_ref=entity_ref,
            evidence_type="user_approved_role_candidate",
            source="user_decision",
            summary="user_approved_role_candidate",
            payload={
                "role_candidate_ref": candidate_ref,
                "proposal_id": proposal.id,
                "decision_request_id": decision_request.id,
                "role": role,
                "label": label,
            },
            confidence=1.0,
            trust="user_approved",
            created_at=now,
        )
    )

    task = _find_open_commissioning_task(session, site_id=proposal.site_id, role_candidate_ref=candidate_ref)
    if task is None:
        task = AgentTask(
            id=new_ref("task"),
            site_id=proposal.site_id,
            thread_id=proposal.thread_id,
            turn_id=proposal.turn_id,
            task_type="commission_role_candidate",
            title="commission_role_candidate",
            goal="commission_role_candidate",
            status="open",
            target_refs=[entity_ref, candidate_ref, f"role:{role}"],
            context={
                "role_candidate_ref": candidate_ref,
                "proposal_id": proposal.id,
                "decision_request_id": decision_request.id,
                "current_phase": "awaiting_commissioning_workflow",
            },
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        add_task_step(
            session,
            task_id=task.id,
            step_key="role_candidate_accepted",
            title="role_candidate_accepted",
            status="completed",
            summary="role_candidate_accepted",
            result={
                "role_candidate_ref": candidate_ref,
                "proposal_id": proposal.id,
            },
        )
        add_blocker(
            session,
            task_id=task.id,
            subject_ref=candidate_ref,
            blocker_type="commissioning_workflow_not_started",
            summary="commissioning_workflow_not_started",
            details={
                "next_capability": "eebus_trust_commissioning_readiness_validation",
                "role": role,
                "entity_ref": entity_ref,
            },
        )
    else:
        task.title = "commission_role_candidate"
        task.goal = "commission_role_candidate"
        task.updated_at = now
        context = dict(task.context or {})
        context.update(
            {
                "role_candidate_ref": candidate_ref,
                "proposal_id": proposal.id,
                "decision_request_id": decision_request.id,
                "current_phase": "awaiting_commissioning_workflow",
            }
        )
        task.context = context
        session.add(task)

    session.add(
        AuditEvent(
            actor="system",
            action="materialize_approved_role_candidate",
            target_type="role_candidate",
            target_id=candidate_ref,
            summary="role_candidate_materialized",
            details={
                "proposal_id": proposal.id,
                "decision_request_id": decision_request.id,
                "task_id": task.id,
                "entity_ref": entity_ref,
                "role": role,
            },
            created_at=now,
        )
    )
    return {
        "role_candidate_ref": candidate_ref,
        "task_ref": task.id,
        "entity_ref": entity_ref,
        "role": role,
        "label": label,
        "status": "accepted",
        "next_step": existing.properties["next_step"],
    }


def list_pending_proposals(session: Session, thread_id: str) -> list[tuple[Proposal, UserDecisionRequest | None]]:
    proposals = session.scalars(
        select(Proposal)
        .where(
            Proposal.thread_id == thread_id,
            Proposal.status == "awaiting_user_decision",
        )
        .order_by(Proposal.created_at.desc())
    ).all()
    rows: list[tuple[Proposal, UserDecisionRequest | None]] = []
    for proposal in proposals:
        request = session.scalar(
            select(UserDecisionRequest)
            .where(
                UserDecisionRequest.proposal_id == proposal.id,
                UserDecisionRequest.status == "pending",
            )
            .order_by(UserDecisionRequest.created_at.desc())
            .limit(1)
        )
        rows.append((proposal, request))
    return rows


def record_user_decision(
    session: Session,
    *,
    request_id: str,
    decision: str,
    actor: str = "user",
    comment: str = "",
) -> tuple[Proposal, UserDecisionRequest, UserDecision]:
    decision_request = session.get(UserDecisionRequest, request_id)
    if decision_request is None:
        raise KeyError(request_id)
    proposal = session.get(Proposal, decision_request.proposal_id)
    if proposal is None:
        raise RuntimeError("Decision request points to a missing proposal.")
    if decision_request.status != "pending":
        raise ValueError("Decision request is no longer pending.")
    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"approve", "reject"}:
        raise ValueError("Decision must be approve or reject.")

    now = utcnow()
    row = UserDecision(
        id=new_ref("user-decision"),
        request_id=decision_request.id,
        proposal_id=proposal.id,
        decision=normalized_decision,
        actor=actor,
        comment=comment,
        details={},
        created_at=now,
    )
    decision_request.status = "approved" if normalized_decision == "approve" else "rejected"
    decision_request.decided_at = now
    proposal.status = "user_approved" if normalized_decision == "approve" else "rejected"
    proposal.resolved_at = now
    proposal.updated_at = now

    session.add_all([row, decision_request, proposal])
    session.add(
        AuditEvent(
            actor=actor,
            action="record_user_decision",
            target_type="proposal",
            target_id=proposal.id,
            summary="user_decision_recorded",
            details={
                "decision_request_id": decision_request.id,
                "decision": normalized_decision,
                "proposal_type": proposal.proposal_type,
            },
            created_at=now,
        )
    )
    materialized = None
    if normalized_decision == "approve":
        materialized = materialize_approved_role_candidate(
            session,
            proposal=proposal,
            decision_request=decision_request,
        )
        row.details = {"materialized": materialized} if materialized else {}
        session.add(row)
    session.commit()
    return proposal, decision_request, row


def accepted_role_candidates(session: Session, *, site_id: int) -> list[dict]:
    rows = session.scalars(
        select(HomeGraphEntity)
        .where(
            HomeGraphEntity.site_id == site_id,
            HomeGraphEntity.entity_type == "role_candidate",
            HomeGraphEntity.status == "accepted",
        )
        .order_by(HomeGraphEntity.updated_at.desc())
    ).all()
    return [
        {
            "role_candidate_ref": row.id,
            "entity_ref": (row.properties or {}).get("entity_ref", ""),
            "role": row.semantic_type,
            "label": row.display_name,
            "status": row.status,
            "readiness_status": (row.properties or {}).get("readiness_status", "unknown"),
            "next_step": (row.properties or {}).get("next_step", ""),
            "proposal_id": (row.properties or {}).get("proposal_id", ""),
        }
        for row in rows
    ]


def latest_accepted_role_candidate_for_entity(
    session: Session,
    *,
    site_id: int,
    entity_ref: str,
    role: str,
) -> dict | None:
    ref = _role_candidate_ref(site_id, entity_ref, role)
    row = session.get(HomeGraphEntity, ref)
    if row is None or row.status != "accepted":
        return None
    return {
        "role_candidate_ref": row.id,
        "entity_ref": (row.properties or {}).get("entity_ref", ""),
        "role": row.semantic_type,
        "label": row.display_name,
        "status": row.status,
        "readiness_status": (row.properties or {}).get("readiness_status", "unknown"),
        "next_step": (row.properties or {}).get("next_step", ""),
        "proposal_id": (row.properties or {}).get("proposal_id", ""),
    }
