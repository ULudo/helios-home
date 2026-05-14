from __future__ import annotations

from app.actions.service import ActionContext, execute_action
from app.agent.tools.schemas import (
    AgentToolContext,
    LoadControlConfigureDeviceInput,
    LoadControlInspectStatusInput,
    ToolExecutionResult,
)
from app.db.models import utcnow
from app.hems.load_control import build_load_control_overview


def _remaining_seconds(expires_at) -> int | None:
    if expires_at is None:
        return None
    now = utcnow()
    if getattr(expires_at, "tzinfo", None) is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return max(0, int((expires_at - now).total_seconds()))


class LoadControlConfigureDeviceTool:
    name = "load_control.configure_device"
    purpose = (
        "Configures whether a device receives LPC/LPP constraints and whether it participates in LPC/LPP distribution."
    )
    risk_level = "medium"
    confirmation_policy = "none"
    contexts = ("setup", "commissioning", "operation", "debug")
    input_model = LoadControlConfigureDeviceInput
    mutates_state = True
    reads = ["inventory", "hems_load_control_config"]
    writes = ["hems_load_control_config", "audit_log"]
    side_effects = ["changes future HEMS load-control distribution behavior"]
    emitted_ui_events = ["device.details.open"]

    def execute(self, context: AgentToolContext, payload: LoadControlConfigureDeviceInput) -> ToolExecutionResult:
        result = execute_action(
            ActionContext(
                session=context.session,
                site=context.site,
                actor="agent",
                thread_id=context.thread.id,
                turn_id=context.turn.id,
            ),
            "load_control.configure_device",
            payload.model_dump(exclude_none=True),
        )
        return ToolExecutionResult(output=result.output, ui_events=result.ui_events)


class LoadControlInspectStatusTool:
    name = "load_control.inspect_status"
    purpose = (
        "Inspects active LPC/LPP constraints, remaining time, receiver devices, participant allocations, "
        "and delivery states. It reports facts only and does not change control behavior."
    )
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = LoadControlInspectStatusInput
    mutates_state = False
    reads = ["hems_load_control_limits", "hems_load_control_deliveries", "inventory"]
    writes: list[str] = []
    side_effects: list[str] = []
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: LoadControlInspectStatusInput) -> ToolExecutionResult:
        overview = build_load_control_overview(context.session, site_id=context.site.id)
        constraints = []
        for constraint in overview.active_constraints:
            participants = [
                {
                    "device_id": participant.device_id,
                    "device_name": participant.device_name,
                    "share_pct": participant.share_pct,
                    "allocated_limit_watts": participant.allocated_limit_watts,
                    "control_available": participant.control_available,
                    "control_path": participant.control_path,
                    "delivery_status": participant.delivery_status,
                    "delivery_detail": participant.delivery_detail,
                    "delivery_updated_at": participant.delivery_updated_at.isoformat()
                    if participant.delivery_updated_at
                    else None,
                }
                for participant in constraint.participants
                if not payload.device_id or participant.device_id == payload.device_id
            ]
            if payload.device_id and payload.device_id not in constraint.receiver_device_ids and not participants:
                continue
            row = {
                "constraint_ref": constraint.id,
                "use_case": constraint.use_case,
                "source": constraint.source,
                "peer_ski": constraint.peer_ski,
                "limit_watts": constraint.limit_watts,
                "duration_seconds": constraint.duration_seconds,
                "received_at": constraint.received_at.isoformat(),
                "expires_at": constraint.expires_at.isoformat() if constraint.expires_at else None,
                "remaining_seconds": _remaining_seconds(constraint.expires_at),
                "receiver_device_ids": list(constraint.receiver_device_ids),
            }
            if payload.include_deliveries:
                row["participants"] = participants
            constraints.append(row)
        return ToolExecutionResult(
            output={
                "active_constraint_count": len(constraints),
                "device_id": payload.device_id,
                "constraints": constraints,
                "notes": [
                    "remaining_seconds is computed at tool execution time.",
                    "delivery_status is operational state, not assistant wording.",
                ],
            }
        )
