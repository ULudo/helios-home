from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.models import Site
from app.domain.enums import ConnectorOutcome, IntegrationStatus, RecoveryZone


@dataclass(slots=True)
class RawCandidate:
    candidate_id: str
    device_id: str
    asset_id: str
    asset_name: str
    display_name: str
    manufacturer: str
    model: str
    firmware: str
    device_type: str
    discovery_sources: list[str]
    protocols: list[str]
    telemetry: dict[str, Any]
    evidence: dict[str, Any]
    recovery_zone: str
    issue_code: str | None
    explanation_hint: str
    next_step_hint: str
    capabilities_hint: dict[str, bool]


@dataclass(slots=True)
class ConnectorAssessment:
    connector_name: str
    protocol: str
    outcome: str
    detail: str


@dataclass(slots=True)
class CandidateClassification:
    device_type: str
    confidence: float
    reasoning: str


@dataclass(slots=True)
class DiagnosisResult:
    primary_status: str
    status_tags: list[str]
    capabilities: dict[str, bool]
    problem_summary: str
    explanation: str
    next_step: str
    incident_title: str | None
    incident_summary: str | None
    incident_severity: str | None
    recommendations: list[dict[str, Any]]


def build_fixture_candidates(site: Site) -> list[RawCandidate]:
    mqtt_available = bool(site.mqtt_broker_url)

    candidates = [
        RawCandidate(
            candidate_id="cand-fronius-gen24",
            device_id="dev-fronius-gen24",
            asset_id="asset-pv",
            asset_name="PV Generation",
            display_name="Rooftop PV Inverter",
            manufacturer="Fronius",
            model="GEN24 Plus",
            firmware="1.28.4",
            device_type="pv_inverter",
            discovery_sources=["fixture_registry", "modbus_probe"],
            protocols=["modbus_tcp"],
            telemetry={"power_kw": 5.8, "daily_energy_kwh": 18.2, "voltage_v": 402},
            evidence={
                "modbus_host": "192.0.2.12",
                "modbus_register_profile": "validated",
            },
            recovery_zone=RecoveryZone.AUTO_APPLY.value,
            issue_code=None,
            explanation_hint="Validated native Modbus telemetry paths.",
            next_step_hint="No action required.",
            capabilities_hint={
                "visible": True,
                "monitorable": True,
                "controllable": True,
                "optimizable": True,
            },
        ),
        RawCandidate(
            candidate_id="cand-byd-battery",
            device_id="dev-byd-battery",
            asset_id="asset-battery",
            asset_name="Battery Buffer",
            display_name="Battery Storage",
            manufacturer="BYD",
            model="Battery-Box Premium HVS",
            firmware="3.14.1",
            device_type="battery",
            discovery_sources=["fixture_registry", "modbus_probe"],
            protocols=["modbus_tcp"],
            telemetry={"soc_pct": 48, "power_kw": -1.2, "available_capacity_kwh": 9.1},
            evidence={
                "modbus_host": "192.0.2.22",
                "register_issue": "unit_id_mismatch",
                "validated_read_paths": ["soc_pct", "power_kw"],
            },
            recovery_zone=RecoveryZone.GUARDED_APPLY.value,
            issue_code="modbus_unit_id_mismatch",
            explanation_hint="Read telemetry is available but command registers no longer line up with the expected profile.",
            next_step_hint="Run guarded recovery to validate a new Modbus unit-id mapping.",
            capabilities_hint={
                "visible": True,
                "monitorable": True,
                "controllable": True,
                "optimizable": False,
            },
        ),
        RawCandidate(
            candidate_id="cand-shelly-3em",
            device_id="dev-shelly-3em",
            asset_id="asset-grid",
            asset_name="Grid Metering",
            display_name="Grid Meter",
            manufacturer="Shelly",
            model="3EM",
            firmware="2025.2.1",
            device_type="grid_meter",
            discovery_sources=["fixture_registry", "mqtt_probe"],
            protocols=["mqtt"],
            telemetry={"grid_power_kw": -2.7, "grid_import_today_kwh": 4.2},
            evidence={
                "mqtt_topics": ["shellies/gridmeter/emeter/0/power"] if mqtt_available else [],
            },
            recovery_zone=RecoveryZone.AUTO_APPLY.value,
            issue_code=None,
            explanation_hint="Stable MQTT telemetry makes the grid meter immediately usable.",
            next_step_hint="No action required.",
            capabilities_hint={
                "visible": True,
                "monitorable": True,
                "controllable": False,
                "optimizable": True,
            },
        ),
        RawCandidate(
            candidate_id="cand-easee-wallbox",
            device_id="dev-easee-wallbox",
            asset_id="asset-wallbox",
            asset_name="EV Charging",
            display_name="EV Charger",
            manufacturer="Easee",
            model="Home",
            firmware="309B",
            device_type="wallbox",
            discovery_sources=["fixture_registry", "cloud_catalog"],
            protocols=["vendor_cloud"],
            telemetry={"vehicle_connected": True, "session_energy_kwh": 0.0},
            evidence={"cloud_pairing_required": True, "vendor_app": "Easee"},
            recovery_zone=RecoveryZone.HUMAN_GATED.value,
            issue_code="auth_required",
            explanation_hint="The available integration path is the vendor cloud and it still needs human-approved pairing.",
            next_step_hint="Complete vendor OAuth or app pairing before retrying integration.",
            capabilities_hint={
                "visible": True,
                "monitorable": False,
                "controllable": False,
                "optimizable": False,
            },
        ),
        RawCandidate(
            candidate_id="cand-vaillant-heatpump",
            device_id="dev-vaillant-heatpump",
            asset_id="asset-heat",
            asset_name="Thermal Control",
            display_name="Heat Pump",
            manufacturer="Vaillant",
            model="aroTHERM plus",
            firmware="7.2.0",
            device_type="heat_pump",
            discovery_sources=["fixture_registry", "cloud_catalog"],
            protocols=["vendor_cloud"],
            telemetry={"flow_temperature_c": 33.5, "thermal_output_kw": 2.4},
            evidence={
                "write_profile": "unverified",
            },
            recovery_zone=RecoveryZone.GUARDED_APPLY.value,
            issue_code="protocol_gap",
            explanation_hint="Telemetry is stable, but the control path has not been validated yet.",
            next_step_hint="Generate an adapter proposal or keep the device monitor-only.",
            capabilities_hint={
                "visible": True,
                "monitorable": True,
                "controllable": False,
                "optimizable": False,
            },
        ),
    ]

    if mqtt_available:
        candidates.append(
            RawCandidate(
                candidate_id="cand-tasmota-plug",
                device_id="dev-tasmota-plug",
                asset_id="asset-smart-plug",
                asset_name="Flexible Smart Load",
                display_name="Laundry Smart Plug",
                manufacturer="Tasmota",
                model="POWR2",
                firmware="13.5.0",
                device_type="smart_appliance",
                discovery_sources=["mqtt_probe"],
                protocols=["mqtt"],
                telemetry={"power_w": 112.0, "energy_today_kwh": 0.8},
                evidence={"mqtt_topics": ["tele/laundry-plug/SENSOR", "stat/laundry-plug/POWER"]},
                recovery_zone=RecoveryZone.AUTO_APPLY.value,
                issue_code=None,
                explanation_hint="The MQTT topic signature matched a known Tasmota smart plug.",
                next_step_hint="Optionally assign a more specific appliance profile.",
                capabilities_hint={
                    "visible": True,
                    "monitorable": True,
                    "controllable": True,
                    "optimizable": True,
                },
            )
        )

    return candidates


def classify_candidate(candidate: RawCandidate) -> CandidateClassification:
    if candidate.evidence.get("classification_reasoning"):
        return CandidateClassification(
            device_type=candidate.device_type,
            confidence=float(candidate.evidence.get("classification_confidence", 0.78)),
            reasoning=str(candidate.evidence["classification_reasoning"]),
        )
    reasoning = (
        f"{candidate.manufacturer} {candidate.model} exposes "
        f"{', '.join(candidate.protocols)} and matches the {candidate.device_type} profile."
    )
    confidence = 0.94 if candidate.issue_code is None else 0.81
    return CandidateClassification(
        device_type=candidate.device_type,
        confidence=confidence,
        reasoning=reasoning,
    )


def assess_connectors(candidate: RawCandidate) -> list[ConnectorAssessment]:
    assessments: list[ConnectorAssessment] = []
    for protocol in candidate.protocols:
        if protocol == "mqtt":
            mqtt_topics = candidate.evidence.get("mqtt_topics", [])
            outcome = ConnectorOutcome.SUCCESS.value if mqtt_topics else ConnectorOutcome.FAILED.value
            detail = (
                "Matched the device against known MQTT topics."
                if mqtt_topics
                else "The broker is configured, but no matching MQTT topics were found."
            )
            assessments.append(
                ConnectorAssessment(
                    connector_name="MQTT probe",
                    protocol=protocol,
                    outcome=outcome,
                    detail=detail,
                )
            )
        elif protocol == "modbus_tcp":
            if candidate.issue_code == "modbus_unit_id_mismatch":
                outcome = ConnectorOutcome.PARTIAL.value
                detail = "Read-only registers responded, but the write mapping appears to have shifted."
            elif candidate.capabilities_hint.get("monitorable"):
                outcome = ConnectorOutcome.SUCCESS.value
                detail = "Validated standardized SunSpec telemetry through the native Modbus/TCP path."
            else:
                outcome = ConnectorOutcome.INFO.value
                detail = "Validated the Modbus device signature, but telemetry still needs a richer SunSpec profile."
            assessments.append(
                ConnectorAssessment(
                    connector_name="Modbus TCP probe",
                    protocol=protocol,
                    outcome=outcome,
                    detail=detail,
                )
            )
        elif protocol == "http_local":
            outcome = (
                ConnectorOutcome.SUCCESS.value
                if candidate.capabilities_hint.get("monitorable")
                else ConnectorOutcome.INFO.value
            )
            detail = (
                "Validated a local HTTP read path for telemetry."
                if candidate.capabilities_hint.get("monitorable")
                else "Identified a local HTTP interface, but telemetry still needs a device-specific adapter."
            )
            assessments.append(
                ConnectorAssessment(
                    connector_name="Local HTTP probe",
                    protocol=protocol,
                    outcome=outcome,
                    detail=detail,
                )
            )
        elif protocol in {"mdns", "ssdp"}:
            assessments.append(
                ConnectorAssessment(
                    connector_name="Network broadcast probe",
                    protocol=protocol,
                    outcome=ConnectorOutcome.INFO.value,
                    detail="Observed a local network advertisement, but no validated telemetry path is attached to it yet.",
                )
            )
        elif protocol == "vendor_cloud":
            if candidate.issue_code == "auth_required":
                outcome = ConnectorOutcome.FAILED.value
                detail = "Vendor cloud pairing or OAuth is still missing."
            elif candidate.issue_code == "protocol_gap":
                outcome = ConnectorOutcome.PARTIAL.value
                detail = "Cloud telemetry is reachable, but the write path is not validated."
            else:
                outcome = ConnectorOutcome.SUCCESS.value
                detail = "The vendor cloud path is available."
            assessments.append(
                ConnectorAssessment(
                    connector_name="Vendor cloud connector",
                    protocol=protocol,
                    outcome=outcome,
                    detail=detail,
                )
            )
    return assessments


def diagnose_candidate(candidate: RawCandidate, assessments: list[ConnectorAssessment]) -> DiagnosisResult:
    if candidate.issue_code == "auth_required":
        return DiagnosisResult(
            primary_status=IntegrationStatus.AUTHENTICATION_REQUIRED.value,
            status_tags=[
                IntegrationStatus.DISCOVERED.value,
                IntegrationStatus.VISIBLE_ONLY.value,
                IntegrationStatus.PARTIALLY_INTEGRABLE.value,
                IntegrationStatus.AUTHENTICATION_REQUIRED.value,
            ],
            capabilities=candidate.capabilities_hint,
            problem_summary="The device was recognized, but the available connector requires human-approved vendor pairing.",
            explanation=candidate.explanation_hint,
            next_step=candidate.next_step_hint,
            incident_title="Authentication is required before integration can continue.",
            incident_summary="The device is visible, but telemetry and control remain blocked until the pairing step is completed.",
            incident_severity="high",
            recommendations=[
                {
                    "title": "Complete vendor pairing",
                    "description": candidate.next_step_hint,
                    "priority": "high",
                    "action_type": "user_action",
                    "zone": RecoveryZone.HUMAN_GATED.value,
                    "auto_applicable": False,
                }
            ],
        )

    if candidate.issue_code == "modbus_unit_id_mismatch":
        return DiagnosisResult(
            primary_status=IntegrationStatus.RECOVERY_RUNNING.value,
            status_tags=[
                IntegrationStatus.DISCOVERED.value,
                IntegrationStatus.CONNECTED.value,
                IntegrationStatus.MONITORABLE.value,
                IntegrationStatus.PARTIALLY_INTEGRABLE.value,
                IntegrationStatus.RECOVERY_RUNNING.value,
            ],
            capabilities=candidate.capabilities_hint,
            problem_summary="The Modbus read path works, but the command register group appears to have shifted after a firmware change.",
            explanation=candidate.explanation_hint,
            next_step=candidate.next_step_hint,
            incident_title="Command path degraded after register shift",
            incident_summary="The device is partially integrated and needs a guarded remap before it becomes fully usable again.",
            incident_severity="medium",
            recommendations=[
                {
                    "title": "Run guarded Modbus remap",
                    "description": candidate.next_step_hint,
                    "priority": "high",
                    "action_type": "recovery",
                    "zone": RecoveryZone.GUARDED_APPLY.value,
                    "auto_applicable": False,
                }
            ],
        )

    if candidate.issue_code == "protocol_gap":
        return DiagnosisResult(
            primary_status=IntegrationStatus.PROTOCOL_INCOMPLETE.value,
            status_tags=[
                IntegrationStatus.DISCOVERED.value,
                IntegrationStatus.CONNECTED.value,
                IntegrationStatus.MONITORABLE.value,
                IntegrationStatus.PARTIALLY_INTEGRABLE.value,
                IntegrationStatus.PROTOCOL_INCOMPLETE.value,
            ],
            capabilities=candidate.capabilities_hint,
            problem_summary="The telemetry path is available, but the control path is still missing a validated adapter profile.",
            explanation=candidate.explanation_hint,
            next_step=candidate.next_step_hint,
            incident_title="Protocol support is still incomplete",
            incident_summary="Monitor-only mode is available, but safe write support still needs to be generated and reviewed.",
            incident_severity="low",
            recommendations=[
                {
                    "title": "Generate adapter proposal",
                    "description": candidate.next_step_hint,
                    "priority": "medium",
                    "action_type": "adapter_scaffold",
                    "zone": RecoveryZone.GUARDED_APPLY.value,
                    "auto_applicable": False,
                }
            ],
        )

    if not candidate.capabilities_hint.get("monitorable"):
        return DiagnosisResult(
            primary_status=IntegrationStatus.VISIBLE_ONLY.value,
            status_tags=[
                IntegrationStatus.DISCOVERED.value,
                IntegrationStatus.VISIBLE_ONLY.value,
            ],
            capabilities=candidate.capabilities_hint,
            problem_summary="The device was identified, but no validated telemetry path is available yet.",
            explanation=candidate.explanation_hint,
            next_step=candidate.next_step_hint,
            incident_title=None,
            incident_summary=None,
            incident_severity=None,
            recommendations=[],
        )

    if candidate.capabilities_hint.get("optimizable"):
        primary_status = IntegrationStatus.OPTIMIZABLE.value
        status_tags = [
            IntegrationStatus.DISCOVERED.value,
            IntegrationStatus.CONNECTED.value,
            IntegrationStatus.MONITORABLE.value,
            IntegrationStatus.CONTROLLABLE.value,
            IntegrationStatus.OPTIMIZABLE.value,
        ]
    elif candidate.capabilities_hint.get("controllable"):
        primary_status = IntegrationStatus.CONTROLLABLE.value
        status_tags = [
            IntegrationStatus.DISCOVERED.value,
            IntegrationStatus.CONNECTED.value,
            IntegrationStatus.MONITORABLE.value,
            IntegrationStatus.CONTROLLABLE.value,
        ]
    else:
        primary_status = IntegrationStatus.MONITORABLE.value
        status_tags = [
            IntegrationStatus.DISCOVERED.value,
            IntegrationStatus.CONNECTED.value,
            IntegrationStatus.MONITORABLE.value,
        ]

    return DiagnosisResult(
        primary_status=primary_status,
        status_tags=status_tags,
        capabilities=candidate.capabilities_hint,
        problem_summary="",
        explanation=candidate.explanation_hint,
        next_step=candidate.next_step_hint,
        incident_title=None,
        incident_summary=None,
        incident_severity=None,
        recommendations=[],
    )
