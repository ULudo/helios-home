from __future__ import annotations

from app.agent.tools.schemas import (
    AgentToolContext,
    DiscoveryInspectHomeNetworkInput,
    ToolExecutionResult,
    focus_entities_event,
    view_open_event,
)
from app.home_graph.service import canonical_inventory_summary
from app.work_store.service import (
    add_blocker,
    add_task_step,
    complete_task,
    complete_task_step,
    create_task,
    fail_task,
    fail_task_step,
)
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
    emitted_ui_events = ["view.open", "entity.focus"]

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
        run_step = add_task_step(
            context.session,
            task_id=task.id,
            step_key="run_discovery",
            title="Run HEMS discovery",
            status="running",
        )
        context.session.commit()
        task_id = task.id
        run_step_id = run_step.id

        try:
            result = inspect_home_network(context.session)
        except Exception as exc:
            context.session.rollback()
            failed_task = context.session.get(type(task), task_id)
            failed_step = context.session.get(type(run_step), run_step_id)
            error_result = {
                "error_type": exc.__class__.__name__,
                "message": str(exc),
            }
            if failed_step is not None:
                fail_task_step(
                    context.session,
                    failed_step,
                    summary="discovery_failed",
                    result=error_result,
                )
            if failed_task is not None:
                fail_task(
                    context.session,
                    failed_task,
                    summary="discovery_failed",
                    error_type=exc.__class__.__name__,
                    error_message=str(exc),
                )
            add_blocker(
                context.session,
                task_id=task_id,
                blocker_type="discovery_failed",
                summary="Discovery failed before it could complete.",
                details=error_result,
            )
            context.session.commit()
            raise

        complete_task_step(
            context.session,
            run_step,
            summary="discovery_completed",
            result={"run_ref": result["run"].get("id")},
        )
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
        inventory_summary = canonical_inventory_summary(context.session, context.site.id)

        return ToolExecutionResult(
            output={
                "task_ref": task.id,
                "run_ref": result["run"].get("id"),
                "result": "devices_found" if result["candidate_count"] else result["result"],
                "canonical_device_count": inventory_summary["canonical_device_count"],
                "observed_class_counts": inventory_summary["observed_class_counts"],
                "role_hypothesis_counts": inventory_summary["role_hypothesis_counts"],
                "primary_observations": inventory_summary["primary_observations"],
                "raw_artifact_counts": inventory_summary["raw_artifact_counts"],
                "details_available_via": ["home_graph.query", "home_graph.get_entity_details"],
                "scope": result["scope"],
                "source_results": result["source_results"],
                "candidate_count": result["candidate_count"],
                "integrated_devices": result["integrated_devices"],
                "refs": {
                    "new_device_ids": result["new_device_ids"],
                    "entity_refs": entity_refs,
                },
                "status": "completed",
            },
            ui_events=[
                view_open_event("overview", "focus"),
                focus_entities_event(entity_refs, mode="highlight"),
            ],
        )
