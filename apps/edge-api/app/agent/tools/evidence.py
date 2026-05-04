from __future__ import annotations

from uuid import uuid4

from app.agent.tools.schemas import (
    AgentToolContext,
    AgentUiEvent,
    EvidenceRecordUserAssertionInput,
    ToolExecutionResult,
)
from app.db.models import HomeGraphEvidence, utcnow
from app.home_graph.service import resolve_entity


class EvidenceRecordUserAssertionTool:
    name = "evidence.record_user_assertion"
    purpose = "Record a user-provided fact as structured Home Graph evidence."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "debug")
    input_model = EvidenceRecordUserAssertionInput
    mutates_state = True
    reads = ["home_graph"]
    writes = ["home_graph_evidence"]
    side_effects = ["records user assertion as tentative Home Graph evidence"]
    emitted_ui_events = ["evidence.recorded"]

    def execute(self, context: AgentToolContext, payload: EvidenceRecordUserAssertionInput) -> ToolExecutionResult:
        entity = resolve_entity(context.session, payload.subject_ref)
        if entity is None:
            raise ValueError(f"Unknown Home Graph entity: {payload.subject_ref}")
        evidence = HomeGraphEvidence(
            id=f"evidence-{uuid4().hex[:12]}",
            site_id=context.site.id,
            subject_ref=entity.id,
            evidence_type=f"user_{payload.assertion_type}",
            source="user",
            summary=payload.value,
            payload={
                "assertion_type": payload.assertion_type,
                "value": payload.value,
                "source_turn_ref": payload.source_turn_ref or context.turn.id,
            },
            confidence=0.9,
            trust="user_asserted",
            created_at=utcnow(),
        )
        context.session.add(evidence)
        context.session.commit()
        return ToolExecutionResult(
            output={
                "evidence_ref": evidence.id,
                "subject_ref": evidence.subject_ref,
                "assertion_type": payload.assertion_type,
                "value": payload.value,
                "status": "recorded",
                "trust": evidence.trust,
            },
            ui_events=[
                AgentUiEvent(
                    event_type="evidence.recorded",
                    payload={"evidence_ref": evidence.id, "subject_ref": evidence.subject_ref},
                )
            ],
        )
