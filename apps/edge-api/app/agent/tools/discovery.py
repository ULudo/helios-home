from __future__ import annotations

from app.agent.tools.schemas import (
    AgentToolContext,
    DiscoveryInspectHomeNetworkInput,
    ToolExecutionResult,
    focus_entities_event,
    show_task_event,
    view_open_event,
)
from app.work_store.service import add_task_step, complete_task, create_task
from app.workflows.discovery import inspect_home_network


class DiscoveryInspectHomeNetworkTool:
    name = "discovery.inspect_home_network"
    purpose = "Inspect the default local home network using standard HEMS discovery policy."
    risk_level = "medium"
    confirmation_policy = "none"
    contexts = ("setup", "commissioning", "debug")
    input_model = DiscoveryInspectHomeNetworkInput
    mutates_state = True
    reads = ["site", "network", "discovery_adapters"]
    writes = ["discovery_run", "device_candidate", "device", "home_graph", "work_store"]
    side_effects = ["runs standard HEMS discovery policy for the configured home network"]
    emitted_ui_events = ["view.open", "task.show", "entity.focus"]

    def execute(self, context: AgentToolContext, payload: DiscoveryInspectHomeNetworkInput) -> ToolExecutionResult:
        task = create_task(
            context.session,
            site_id=context.site.id,
            thread_id=context.thread.id,
            turn_id=context.turn.id,
            task_type="discover_home",
            title="Inspect home network",
            goal=payload.reason or "Discover devices and endpoints in the configured local home network.",
        )
        add_task_step(
            context.session,
            task_id=task.id,
            step_key="run_discovery",
            title="Run HEMS discovery",
            status="running",
        )
        context.session.commit()

        result = inspect_home_network(context.session)
        entity_refs = [
            ref
            for ref in result["entity_refs"]
            if ref.startswith("device:")
        ]
        add_task_step(
            context.session,
            task_id=task.id,
            step_key="materialize_home_graph",
            title="Materialize Home Graph candidates",
            status="completed",
            result={"entity_refs": entity_refs},
        )
        complete_task(
            context.session,
            task,
            summary="discovery_completed",
        )
        context.session.commit()

        return ToolExecutionResult(
            output={
                "task_ref": task.id,
                "run_ref": result["run"].get("id"),
                "candidate_count": result["candidate_count"],
                "integrated_devices": result["integrated_devices"],
                "new_device_ids": result["new_device_ids"],
                "entity_refs": entity_refs,
                "status": "completed",
            },
            ui_events=[
                view_open_event("overview", "focus"),
                show_task_event(task.id, "summary"),
                focus_entities_event(entity_refs, mode="highlight"),
            ],
        )
