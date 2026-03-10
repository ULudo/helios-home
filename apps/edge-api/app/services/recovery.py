from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import AgentRun, Asset, AuditEvent, ConnectorAttempt, Device, Incident, Recommendation, utcnow
from app.domain.enums import AgentRunStatus, ConnectorOutcome, IntegrationStatus, RecoveryZone
from app.domain.schemas import AgentRunRead, RecoveryRunRead
from app.services.dashboard import get_device


def _update_asset_for_device(session: Session, device_id: str, status: str, health: str) -> None:
    for asset in session.query(Asset).all():
        if device_id in (asset.device_ids or []):
            asset.status = status
            asset.health = health


def _resolve_open_incidents(device: Device) -> None:
    for incident in device.incidents:
        incident.status = "resolved"


def _protocol_label(device: Device) -> str:
    if "modbus_tcp" in (device.protocols or []):
        return "Modbus"
    if device.protocols:
        return device.protocols[0].replace("_", " ").title()
    return "connector"


def run_recovery(session: Session, device_id: str) -> RecoveryRunRead:
    device = session.get(Device, device_id)
    if device is None:
        raise KeyError(device_id)

    now = utcnow()
    agent_run = AgentRun(
        id=f"run-{uuid4().hex[:10]}",
        device_id=device.id,
        status=AgentRunStatus.RUNNING.value,
        zone=device.recovery_zone,
        summary="Starting recovery flow.",
        action_plan=[],
        rollback_ready=device.recovery_zone != RecoveryZone.HUMAN_GATED.value,
        started_at=now,
        finished_at=None,
    )
    session.add(agent_run)

    message = "Recovery flow created."

    if (
        device.primary_status == IntegrationStatus.RECOVERY_RUNNING.value
        and device.recovery_zone == RecoveryZone.GUARDED_APPLY.value
    ):
        restored_status = (
            IntegrationStatus.OPTIMIZABLE.value
            if device.capabilities.get("controllable")
            else IntegrationStatus.CONTROLLABLE.value
        )
        status_tags = [
            IntegrationStatus.DISCOVERED.value,
            IntegrationStatus.CONNECTED.value,
            IntegrationStatus.MONITORABLE.value,
            IntegrationStatus.CONTROLLABLE.value,
        ]
        if restored_status == IntegrationStatus.OPTIMIZABLE.value:
            status_tags.append(IntegrationStatus.OPTIMIZABLE.value)

        device.primary_status = restored_status
        device.status_tags = status_tags
        device.capabilities = {
            **device.capabilities,
            "controllable": True,
            "optimizable": restored_status == IntegrationStatus.OPTIMIZABLE.value,
        }
        device.problem_summary = ""
        device.explanation = (
            "Guarded recovery validated an updated connector mapping and restored the safe "
            "write path without leaving the policy envelope."
        )
        device.next_step = "Observe charge and discharge telemetry for the next 24 hours."
        device.confidence = 0.93
        agent_run.status = AgentRunStatus.COMPLETED.value
        agent_run.summary = f"Applied a guarded {_protocol_label(device)} connector patch and revalidated the safe write path."
        agent_run.action_plan = [
            "Snapshot the current connector assumptions.",
            "Probe an alternative mapping in read-only mode.",
            "Promote the validated mapping after write checks passed.",
        ]
        agent_run.finished_at = now
        session.add(
            ConnectorAttempt(
                device_id=device.id,
                connector_name=f"Guarded {_protocol_label(device)} patch",
                protocol=device.protocols[0] if device.protocols else "unknown",
                outcome=ConnectorOutcome.SUCCESS.value,
                detail="A guarded connector patch restored the validated write path and passed safety validation.",
                attempted_at=now,
            )
        )
        session.add(
            Recommendation(
                site_id=device.site_id,
                device_id=device.id,
                title="Observe battery post-recovery",
                description="The write path is restored. Monitor telemetry drift and command acknowledgement for 24 hours.",
                priority="medium",
                action_type="observation",
                zone=RecoveryZone.HUMAN_GATED.value,
                auto_applicable=False,
                created_at=now,
            )
        )
        _resolve_open_incidents(device)
        _update_asset_for_device(session, device.id, restored_status, "healthy")
        message = "Guarded recovery completed and the validated write path was restored."
    elif (
        device.primary_status in {
            IntegrationStatus.PROTOCOL_INCOMPLETE.value,
            IntegrationStatus.PARTIALLY_INTEGRABLE.value,
        }
        and device.recovery_zone == RecoveryZone.GUARDED_APPLY.value
    ):
        device.primary_status = IntegrationStatus.PARTIALLY_INTEGRABLE.value
        device.status_tags = [
            IntegrationStatus.DISCOVERED.value,
            IntegrationStatus.CONNECTED.value,
            IntegrationStatus.MONITORABLE.value,
            IntegrationStatus.PARTIALLY_INTEGRABLE.value,
            IntegrationStatus.IN_ANALYSIS.value,
        ]
        device.explanation = (
            "A template-based adapter proposal was generated, but Helios keeps the device "
            "in monitor-only mode until the write path passes manual policy review."
        )
        device.next_step = "Review the generated adapter proposal before enabling control."
        agent_run.status = AgentRunStatus.PROPOSAL_READY.value
        agent_run.summary = "Prepared a monitor-first adapter scaffold and blocked activation pending review."
        agent_run.action_plan = [
            "Generate adapter scaffold from the strongest native protocol evidence.",
            "Map candidate control registers to safe ranges.",
            "Require human review before activation.",
        ]
        agent_run.finished_at = now
        session.add(
            ConnectorAttempt(
                device_id=device.id,
                connector_name="Adapter scaffold",
                protocol=device.protocols[0] if device.protocols else "unknown",
                outcome=ConnectorOutcome.PARTIAL.value,
                detail="Generated a read/write proposal but left the write path disabled by policy.",
                attempted_at=now,
            )
        )
        _update_asset_for_device(session, device.id, IntegrationStatus.PARTIALLY_INTEGRABLE.value, "attention")
        message = "Guarded recovery produced an adapter proposal, but activation remains blocked by policy."
    elif (
        device.primary_status
        in {
            IntegrationStatus.AUTHENTICATION_REQUIRED.value,
            IntegrationStatus.MANUFACTURER_ACCESS_REQUIRED.value,
        }
        or device.recovery_zone == RecoveryZone.HUMAN_GATED.value
    ):
        agent_run.status = AgentRunStatus.BLOCKED.value
        agent_run.summary = "Recovery cannot proceed autonomously because a human-gated vendor pairing step is missing."
        agent_run.action_plan = [
            "Open the vendor pairing flow.",
            "Approve OAuth or 2FA in the manufacturer app.",
            "Return to Helios and rerun discovery.",
        ]
        agent_run.rollback_ready = False
        agent_run.finished_at = now
        session.add(
            Recommendation(
                site_id=device.site_id,
                device_id=device.id,
                title="Complete the blocked pairing step",
                description="Autonomous recovery is paused until the required vendor authentication has been approved.",
                priority="high",
                action_type="user_action",
                zone=RecoveryZone.HUMAN_GATED.value,
                auto_applicable=False,
                created_at=now,
            )
        )
        message = "Recovery is blocked until the required human-gated pairing step is completed."
    else:
        agent_run.status = AgentRunStatus.COMPLETED.value
        agent_run.summary = "Recovery completed without requiring changes."
        agent_run.action_plan = ["Validated the existing connector state.", "No changes were necessary."]
        agent_run.finished_at = now
        message = "Recovery validated the current connector state."

    session.add(
        AuditEvent(
            actor="agent",
            action="run_recovery",
            target_type="device",
            target_id=device.id,
            summary=message,
            details={"zone": device.recovery_zone, "status": agent_run.status},
            created_at=now,
        )
    )
    session.commit()
    device_read = get_device(session, device.id)
    if device_read is None:
        raise RuntimeError("Device disappeared during recovery.")
    agent_run_read = AgentRunRead(
        id=agent_run.id,
        device_id=agent_run.device_id,
        status=agent_run.status,
        zone=agent_run.zone,
        summary=agent_run.summary,
        action_plan=agent_run.action_plan or [],
        rollback_ready=agent_run.rollback_ready,
        started_at=agent_run.started_at,
        finished_at=agent_run.finished_at,
    )
    return RecoveryRunRead(
        message=message,
        device=device_read,
        agent_run=agent_run_read,
    )
