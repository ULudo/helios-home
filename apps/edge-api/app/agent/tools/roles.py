from __future__ import annotations

from app.agent.tools.schemas import (
    AgentToolContext,
    RolePrepareBindingProposalInput,
    ToolExecutionResult,
    focus_entities_event,
    present_proposal_event,
    show_relationship_event,
    view_open_event,
)
from app.workflows.role_binding import prepare_role_binding_proposal


class RolePrepareBindingProposalTool:
    name = "role.prepare_binding_proposal"
    purpose = "Prepare a HEMS role binding proposal and a real user decision request."
    risk_level = "medium"
    confirmation_policy = "user_decision_required"
    contexts = ("setup", "commissioning")
    input_model = RolePrepareBindingProposalInput
    mutates_state = True
    reads = ["home_graph", "work_store"]
    writes = ["proposal", "user_decision_request", "audit_event"]
    side_effects = ["creates Proposal and UserDecisionRequest; never applies binding"]
    emitted_ui_events = ["view.open", "entity.focus", "entity.relationship.show", "proposal.present"]

    def execute(self, context: AgentToolContext, payload: RolePrepareBindingProposalInput) -> ToolExecutionResult:
        result = prepare_role_binding_proposal(
            context.session,
            site_id=context.site.id,
            thread_id=context.thread.id,
            turn_id=context.turn.id,
            entity_ref=payload.entity_ref,
            role=payload.role,
            label=payload.label,
            rationale=payload.rationale or context.user_message,
        )
        ui_events = [
            view_open_event("overview", "focus"),
            focus_entities_event([payload.entity_ref], "Role binding proposal target"),
            show_relationship_event(payload.entity_ref, f"role:{payload.role}", "proposed_for_role"),
        ]
        if result.get("decision_request_ref"):
            ui_events.append(present_proposal_event(result["proposal_ref"], result["decision_request_ref"]))

        created_proposal_refs = [] if result.get("existing") or result.get("status") == "already_user_approved" else [result["proposal_ref"]]
        created_decision_refs = [] if result.get("existing") or not result.get("decision_request_ref") else [result["decision_request_ref"]]
        return ToolExecutionResult(
            output=result,
            created_proposal_refs=created_proposal_refs,
            created_decision_request_refs=created_decision_refs,
            ui_events=ui_events,
        )
