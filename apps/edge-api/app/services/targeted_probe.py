from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address, ip_network
import re
import socket

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.db.models import AuditEvent, DebugCase, DebugProbeRun, DiscoveryRun, Site, utcnow
from app.domain.enums import (
    ExplainabilityReasonCode,
    ExplainabilityReasonFamily,
    ExplainabilityState,
    IntegrationFeasibility,
    ProbeCheckOutcome,
    ProbeRunStatus,
)
from app.domain.schemas import DebugCaseRead, DebugDiagnosisRead, DebugEvidenceRead, DebugExplainRequest
from app.services.explainability import explain_manual_claim
from app.services.knowledge import get_debug_case
from app.services.local_network import fingerprint_http_host
from app.services.modbus import probe_modbus_host
from app.services.network_scope import parse_configured_subnets

IP_PATTERN = re.compile(r"\b(?P<host>(?:\d{1,3}\.){3}\d{1,3})(?::(?P<port>\d{1,5}))?\b")
URL_PATTERN = re.compile(r"https?://(?P<host>[^/\s:]+)(?::(?P<port>\d{1,5}))?")
LEGACY_HINT_PATTERN = re.compile(r"\b(?:[12]?\d|30|40)\s*(?:year|years|yr|yrs|jahre|jahr)\b", re.IGNORECASE)


@dataclass(slots=True)
class ProbeCheck:
    name: str
    outcome: str
    summary: str
    details: dict[str, object]
    confidence: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "summary": self.summary,
            "details": self.details,
            "confidence": self.confidence,
        }


def _dedupe_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _claim_from_case(debug_case: DebugCase) -> DebugExplainRequest:
    return DebugExplainRequest(
        manufacturer=debug_case.manufacturer,
        model=debug_case.model,
        device_type=debug_case.device_type,
        notes=debug_case.notes,
    )


def _load_debug_case(session: Session, case_id: int) -> DebugCase | None:
    return session.scalar(
        select(DebugCase)
        .where(DebugCase.id == case_id)
        .options(
            selectinload(DebugCase.findings),
            selectinload(DebugCase.probe_runs),
        )
    )


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


def _latest_discovery_run(session: Session) -> DiscoveryRun | None:
    return session.scalar(select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc()).limit(1))


def _relevant_sources(site: Site, debug_case: DebugCase) -> list[str]:
    relevant = {"local_network_live", "network_broadcast_live"}
    if debug_case.device_type in {"battery", "pv_inverter", "grid_meter"}:
        relevant.add("modbus_live")
    lowered_notes = debug_case.notes.lower()
    if site.mqtt_broker_url or "mqtt" in lowered_notes:
        relevant.add("mqtt_live")
    return sorted(relevant)


def _extract_host_targets(debug_case: DebugCase) -> dict[str, set[int]]:
    targets: dict[str, set[int]] = {}
    default_ports: set[int] = set()
    lowered = debug_case.notes.lower()

    if any(token in lowered for token in ("http", "https", "web", "api")):
        default_ports.update({80, 443})
    if "modbus" in lowered or debug_case.device_type in {"battery", "pv_inverter", "grid_meter"}:
        default_ports.add(502)
    if "mqtt" in lowered:
        default_ports.update({1883, 8883})
    if not default_ports and any(token in lowered for token in ("lan", "wlan", "wifi", "ethernet")):
        default_ports.update({80, 443})

    for match in URL_PATTERN.finditer(debug_case.notes):
        host = match.group("host")
        if not host:
            continue
        targets.setdefault(host, set()).update(default_ports or {80, 443})
        if match.group("port"):
            targets[host].add(int(match.group("port")))

    for match in IP_PATTERN.finditer(debug_case.notes):
        host = match.group("host")
        if not host:
            continue
        targets.setdefault(host, set()).update(default_ports or {80, 443})
        if match.group("port"):
            targets[host].add(int(match.group("port")))

    return {host: ports for host, ports in targets.items() if ports}


def _host_in_subnet(host: str, subnet: str) -> bool | None:
    subnets = parse_configured_subnets(subnet)
    if not subnets:
        return None

    try:
        host_address = ip_address(host)
    except ValueError:
        return None

    matched_any = False
    for configured_subnet in subnets:
        try:
            if host_address in ip_network(configured_subnet, strict=False):
                return True
            matched_any = True
        except ValueError:
            continue
    return False if matched_any else None


def _probe_host_ports(host: str, ports: list[int], timeout_seconds: float = 0.35) -> dict[str, object]:
    open_ports: list[int] = []
    closed_ports: list[int] = []
    errors: list[str] = []
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                open_ports.append(port)
        except (ConnectionRefusedError, TimeoutError, OSError) as exc:
            closed_ports.append(port)
            errors.append(f"{port}:{type(exc).__name__}")
    return {
        "open_ports": open_ports,
        "closed_ports": closed_ports,
        "errors": errors,
    }


def _inventory_match_check(report_matched_device_id: str | None, report_matched_candidate_id: str | None) -> ProbeCheck:
    if report_matched_device_id:
        return ProbeCheck(
            name="inventory_match",
            outcome=ProbeCheckOutcome.PASSED.value,
            summary="The claim already matches a materialized Helios device.",
            details={
                "matched_device_id": report_matched_device_id,
                "matched_candidate_id": report_matched_candidate_id,
            },
            confidence=0.92,
        )
    if report_matched_candidate_id:
        return ProbeCheck(
            name="inventory_match",
            outcome=ProbeCheckOutcome.WARNING.value,
            summary="The claim matches a normalized device candidate, but not a fully materialized device yet.",
            details={"matched_candidate_id": report_matched_candidate_id},
            confidence=0.72,
        )
    return ProbeCheck(
        name="inventory_match",
        outcome=ProbeCheckOutcome.FAILED.value,
        summary="The current inventory does not contain a direct match for the claim.",
        details={},
        confidence=0.56,
    )


def _source_coverage_check(site: Site, debug_case: DebugCase, latest_run: DiscoveryRun | None) -> ProbeCheck:
    relevant = _relevant_sources(site, debug_case)
    observed = sorted(set(latest_run.source_names or [])) if latest_run is not None else []
    missing = [source for source in relevant if source not in observed]
    if latest_run is None:
        return ProbeCheck(
            name="source_coverage",
            outcome=ProbeCheckOutcome.FAILED.value,
            summary="No discovery run is available yet, so the claim has not been checked against the configured live sources.",
            details={
                "relevant_sources": relevant,
                "observed_sources": observed,
                "missing_sources": relevant,
            },
            confidence=0.81,
        )
    if not missing:
        return ProbeCheck(
            name="source_coverage",
            outcome=ProbeCheckOutcome.PASSED.value,
            summary="Recent discovery runs already covered the source families that are most relevant for this claim.",
            details={
                "relevant_sources": relevant,
                "observed_sources": observed,
                "missing_sources": [],
                "latest_discovery_status": latest_run.status,
            },
            confidence=0.76,
        )
    return ProbeCheck(
        name="source_coverage",
        outcome=ProbeCheckOutcome.WARNING.value,
        summary="Recent discovery runs did not cover all source families that could be relevant for this claim.",
        details={
            "relevant_sources": relevant,
            "observed_sources": observed,
            "missing_sources": missing,
            "latest_discovery_status": latest_run.status,
        },
        confidence=0.71,
    )


def _host_reachability_check(site: Site, debug_case: DebugCase) -> ProbeCheck:
    targets = _extract_host_targets(debug_case)
    if not targets:
        return ProbeCheck(
            name="host_reachability",
            outcome=ProbeCheckOutcome.NOT_APPLICABLE.value,
            summary="The claim does not include a concrete host or URL for targeted network probing.",
            details={},
            confidence=None,
        )

    results: dict[str, dict[str, object]] = {}
    open_hosts: list[str] = []
    for host, ports in targets.items():
        result = _probe_host_ports(host, sorted(ports))
        in_subnet = _host_in_subnet(host, site.local_subnet)
        result["in_site_subnet"] = in_subnet
        results[host] = result
        if result["open_ports"]:
            open_hosts.append(host)

    if open_hosts:
        open_port_count = sum(len(result["open_ports"]) for result in results.values())
        return ProbeCheck(
            name="host_reachability",
            outcome=ProbeCheckOutcome.PASSED.value,
            summary=f"Targeted probing reached {len(open_hosts)} host(s) on {open_port_count} expected local port(s).",
            details={
                "targets": {host: sorted(ports) for host, ports in targets.items()},
                "results": results,
            },
            confidence=0.86,
        )

    return ProbeCheck(
        name="host_reachability",
        outcome=ProbeCheckOutcome.FAILED.value,
        summary="Targeted probing could not reach any of the claimed hosts on the expected local ports.",
        details={
            "targets": {host: sorted(ports) for host, ports in targets.items()},
            "results": results,
        },
        confidence=0.82,
    )


def _http_fingerprint_check(debug_case: DebugCase) -> ProbeCheck:
    targets = _extract_host_targets(debug_case)
    if not targets:
        return ProbeCheck(
            name="http_fingerprint",
            outcome=ProbeCheckOutcome.NOT_APPLICABLE.value,
            summary="No concrete host was supplied for targeted HTTP fingerprinting.",
            details={},
        )

    settings = get_settings()
    results: list[dict[str, object]] = []
    for host, ports in targets.items():
        if not ({80, 443} & set(ports) or any(token in debug_case.notes.lower() for token in ("http", "https", "web", "api"))):
            continue
        candidate = fingerprint_http_host(host, timeout_seconds=min(settings.local_scan_timeout_seconds, 1.0))
        if candidate is not None:
            return ProbeCheck(
                name="http_fingerprint",
                outcome=ProbeCheckOutcome.PASSED.value,
                summary=(
                    f"HTTP fingerprinting matched {candidate.manufacturer} {candidate.model} as "
                    f"{candidate.device_type} and validated {len(candidate.telemetry)} telemetry metric(s)."
                ),
                details={
                    "host": host,
                    "candidate_id": candidate.candidate_id,
                    "device_type": candidate.device_type,
                    "display_name": candidate.display_name,
                    "manufacturer": candidate.manufacturer,
                    "model": candidate.model,
                    "monitorable": bool(candidate.capabilities_hint.get("monitorable")),
                    "telemetry_keys": sorted(candidate.telemetry.keys()),
                    "evidence": candidate.evidence,
                },
                confidence=float(candidate.evidence.get("classification_confidence", 0.8)),
            )
        results.append({"host": host, "ports": sorted(ports), "fingerprint_matched": False})

    if results:
        return ProbeCheck(
            name="http_fingerprint",
            outcome=ProbeCheckOutcome.WARNING.value,
            summary="HTTP probing reached claimed hosts, but no energy-relevant local HTTP fingerprint matched yet.",
            details={"results": results},
            confidence=0.62,
        )

    return ProbeCheck(
        name="http_fingerprint",
        outcome=ProbeCheckOutcome.NOT_APPLICABLE.value,
        summary="The claim does not indicate an HTTP-oriented local interface.",
        details={},
    )


def _modbus_protocol_check(debug_case: DebugCase) -> ProbeCheck:
    targets = _extract_host_targets(debug_case)
    lowered = debug_case.notes.lower()
    wants_modbus = (
        "modbus" in lowered
        or "sunspec" in lowered
        or debug_case.device_type in {"battery", "pv_inverter", "grid_meter"}
    )
    if not wants_modbus:
        return ProbeCheck(
            name="modbus_protocol_probe",
            outcome=ProbeCheckOutcome.NOT_APPLICABLE.value,
            summary="The claim does not indicate a Modbus/TCP or SunSpec-oriented path.",
            details={},
        )

    settings = get_settings()
    candidate_hosts = [host for host, ports in targets.items() if 502 in ports] or list(targets.keys())
    if not candidate_hosts and "modbus" in lowered:
        return ProbeCheck(
            name="modbus_protocol_probe",
            outcome=ProbeCheckOutcome.WARNING.value,
            summary="The claim mentions Modbus, but no concrete host was provided for a targeted Modbus/TCP probe.",
            details={},
            confidence=0.6,
        )

    results: list[dict[str, object]] = []
    for host in candidate_hosts:
        probe = probe_modbus_host(host, timeout_seconds=min(settings.modbus_timeout_seconds, 0.8))
        if probe is not None:
            return ProbeCheck(
                name="modbus_protocol_probe",
                outcome=ProbeCheckOutcome.PASSED.value,
                summary=(
                    f"Native Modbus/TCP probing matched unit {probe.unit_id} and observed "
                    f"{len(probe.sunspec_model_ids)} SunSpec model block(s)."
                ),
                details={
                    "host": host,
                    "unit_id": probe.unit_id,
                    "vendor_name": probe.vendor_name,
                    "product_code": probe.product_code,
                    "revision": probe.revision,
                    "sunspec_base_register": probe.sunspec_base_register,
                    "sunspec_model_ids": probe.sunspec_model_ids,
                    "telemetry_keys": sorted((probe.telemetry or {}).keys()),
                },
                confidence=0.88 if probe.telemetry else 0.78,
            )
        results.append({"host": host, "matched": False})

    return ProbeCheck(
        name="modbus_protocol_probe",
        outcome=ProbeCheckOutcome.WARNING.value,
        summary="Targeted Modbus/TCP probing did not confirm a usable native Modbus or SunSpec endpoint for the claim.",
        details={"results": results},
        confidence=0.67,
    )


def _dry_contact_check(debug_case: DebugCase) -> ProbeCheck:
    if debug_case.device_type != "heat_pump":
        return ProbeCheck(
            name="dry_contact_path",
            outcome=ProbeCheckOutcome.NOT_APPLICABLE.value,
            summary="Dry-contact fallback is only evaluated for heat-pump claims in this phase.",
            details={},
        )
    lowered = debug_case.notes.lower()
    if "sg ready" in lowered or "sg-ready" in lowered:
        return ProbeCheck(
            name="dry_contact_path",
            outcome=ProbeCheckOutcome.PASSED.value,
            summary="The claim explicitly indicates an SG Ready path, so a dry-contact retrofit is plausible.",
            details={"signal": "sg_ready"},
            confidence=0.91,
        )
    if LEGACY_HINT_PATTERN.search(lowered) or "no lan module" in lowered:
        return ProbeCheck(
            name="dry_contact_path",
            outcome=ProbeCheckOutcome.WARNING.value,
            summary="The claim looks like a legacy heat pump with no native networking, so dry-contact retrofits should be checked.",
            details={"signal": "legacy_heat_pump"},
            confidence=0.7,
        )
    return ProbeCheck(
        name="dry_contact_path",
        outcome=ProbeCheckOutcome.NOT_APPLICABLE.value,
        summary="No SG Ready or dry-contact hint was found in the claim.",
        details={},
    )


def _gateway_path_check(debug_case: DebugCase) -> ProbeCheck:
    lowered = debug_case.notes.lower()
    if debug_case.device_type in {"heat_pump", "wallbox"} or any(
        token in lowered for token in ("gateway", "cloud", "app", "vendor module", "lan module", "wlan module")
    ):
        return ProbeCheck(
            name="gateway_path",
            outcome=ProbeCheckOutcome.WARNING.value,
            summary="A vendor gateway or add-on network module is a plausible integration path for this claim.",
            details={"device_type": debug_case.device_type},
            confidence=0.68,
        )
    return ProbeCheck(
        name="gateway_path",
        outcome=ProbeCheckOutcome.NOT_APPLICABLE.value,
        summary="No vendor gateway path stands out from the current claim details.",
        details={},
    )


def _metering_fallback_check(debug_case: DebugCase) -> ProbeCheck:
    if debug_case.device_type in {"heat_pump", "wallbox", "smart_appliance", "unclassified_energy_device"}:
        return ProbeCheck(
            name="metering_fallback",
            outcome=ProbeCheckOutcome.PASSED.value,
            summary="External energy metering is a viable fallback when native device integration is missing.",
            details={"device_type": debug_case.device_type},
            confidence=0.73,
        )
    return ProbeCheck(
        name="metering_fallback",
        outcome=ProbeCheckOutcome.NOT_APPLICABLE.value,
        summary="External metering fallback is not prioritized for this device type right now.",
        details={"device_type": debug_case.device_type},
    )


def _check_map(checks: list[ProbeCheck]) -> dict[str, ProbeCheck]:
    return {check.name: check for check in checks}


def _probe_evidence(checks: list[ProbeCheck]) -> list[DebugEvidenceRead]:
    return [
        DebugEvidenceRead(
            kind="probe",
            label=check.name,
            value=check.summary,
            source="targeted_probe",
            confidence=check.confidence,
        )
        for check in checks
        if check.outcome != ProbeCheckOutcome.NOT_APPLICABLE.value
    ]


def _refine_diagnosis(
    baseline: DebugDiagnosisRead,
    debug_case: DebugCase,
    checks: list[ProbeCheck],
    matched_device_id: str | None,
    matched_candidate_id: str | None,
) -> DebugDiagnosisRead:
    diagnosis = baseline.model_copy(deep=True)
    check_map = _check_map(checks)

    evidence = list(diagnosis.evidence) + _probe_evidence(checks)
    host_check = check_map["host_reachability"]
    http_check = check_map["http_fingerprint"]
    modbus_check = check_map["modbus_protocol_probe"]
    dry_contact_check = check_map["dry_contact_path"]
    gateway_check = check_map["gateway_path"]
    metering_check = check_map["metering_fallback"]

    state = diagnosis.state
    reason_family = diagnosis.reason_family
    reason_code = diagnosis.reason_code
    feasibility = diagnosis.feasibility
    summary = diagnosis.summary
    confidence = diagnosis.confidence

    if matched_device_id or matched_candidate_id:
        pass
    elif http_check.outcome == ProbeCheckOutcome.PASSED.value or modbus_check.outcome == ProbeCheckOutcome.PASSED.value:
        passed_check = http_check if http_check.outcome == ProbeCheckOutcome.PASSED.value else modbus_check
        details = passed_check.details if isinstance(passed_check.details, dict) else {}
        monitorable = bool(details.get("monitorable")) or bool(details.get("telemetry_keys"))
        state = ExplainabilityState.CLASSIFIED_BUT_NOT_INTEGRABLE.value
        reason_family = ExplainabilityReasonFamily.OPERATIONAL.value if monitorable else ExplainabilityReasonFamily.INTERFACE.value
        reason_code = (
            ExplainabilityReasonCode.VALIDATED_INTERFACE.value
            if monitorable
            else ExplainabilityReasonCode.TELEMETRY_PATH_NOT_VALIDATED.value
        )
        feasibility = (
            IntegrationFeasibility.NETWORK_NATIVE.value
            if monitorable
            else IntegrationFeasibility.NETWORK_NATIVE_BUT_UNSUPPORTED.value
        )
        summary = (
            "Targeted protocol probing validated a native local integration path for the claim, but it is not materialized in the Helios inventory yet."
            if monitorable
            else "Targeted protocol probing identified a plausible native local endpoint, but Helios still lacks a validated telemetry mapping for it."
        )
        confidence = max(confidence, float(passed_check.confidence or 0.78))
    elif host_check.outcome == ProbeCheckOutcome.PASSED.value:
        state = ExplainabilityState.CLASSIFIED_BUT_NOT_INTEGRABLE.value
        reason_family = ExplainabilityReasonFamily.INTERFACE.value
        reason_code = ExplainabilityReasonCode.TELEMETRY_PATH_NOT_VALIDATED.value
        feasibility = IntegrationFeasibility.NETWORK_NATIVE_BUT_UNSUPPORTED.value
        summary = "A host linked to the claim is reachable on the local network, but Helios has not validated a supported telemetry path for it yet."
        confidence = max(confidence, 0.78)
    elif host_check.outcome == ProbeCheckOutcome.FAILED.value:
        state = ExplainabilityState.NOT_FOUND.value
        reason_family = ExplainabilityReasonFamily.NETWORK.value
        reason_code = ExplainabilityReasonCode.NETWORK_UNREACHABLE.value
        feasibility = IntegrationFeasibility.NETWORK_NATIVE.value
        summary = "Targeted probing could not reach the claimed host on the expected local ports."
        confidence = max(confidence, 0.8)
    elif dry_contact_check.outcome in {ProbeCheckOutcome.PASSED.value, ProbeCheckOutcome.WARNING.value}:
        state = ExplainabilityState.NOT_FOUND.value
        reason_family = ExplainabilityReasonFamily.INTERFACE.value
        reason_code = ExplainabilityReasonCode.NO_SUPPORTED_INTERFACE.value
        feasibility = IntegrationFeasibility.DRY_CONTACT_POSSIBLE.value
        summary = "No usable native network interface was confirmed for the claim, but a dry-contact retrofit path looks plausible."
        confidence = max(confidence, 0.82 if dry_contact_check.outcome == ProbeCheckOutcome.PASSED.value else 0.72)
    elif gateway_check.outcome == ProbeCheckOutcome.WARNING.value:
        state = ExplainabilityState.NOT_FOUND.value
        reason_family = ExplainabilityReasonFamily.VENDOR.value
        reason_code = ExplainabilityReasonCode.GATEWAY_REQUIRED.value
        feasibility = IntegrationFeasibility.GATEWAY_POSSIBLE.value
        summary = "The claim still lacks a direct local integration path, but a vendor gateway or network module looks plausible."
        confidence = max(confidence, 0.7)
    elif metering_check.outcome == ProbeCheckOutcome.PASSED.value and feasibility == IntegrationFeasibility.UNKNOWN.value:
        feasibility = IntegrationFeasibility.METER_ONLY_POSSIBLE.value
        confidence = max(confidence, 0.66)

    raw_diagnostics = {
        **diagnosis.raw_diagnostics,
        "targeted_probe_checks": [check.as_dict() for check in checks],
    }

    return diagnosis.model_copy(
        update={
            "state": state,
            "reason_family": reason_family,
            "reason_code": reason_code,
            "feasibility": feasibility,
            "summary": summary,
            "confidence": confidence,
            "evidence": evidence,
            "raw_diagnostics": raw_diagnostics,
        }
    )


def _probe_run_summary(
    checks: list[ProbeCheck],
    matched_device_id: str | None,
    matched_candidate_id: str | None,
) -> str:
    check_map = _check_map(checks)
    if matched_device_id:
        return "Targeted probing confirmed that the claim already matches an existing Helios device."
    if matched_candidate_id:
        return "Targeted probing confirmed that the claim already aligns with an existing discovery candidate."
    if check_map["host_reachability"].outcome == ProbeCheckOutcome.PASSED.value:
        return "Targeted probing found a reachable host but no validated Helios adapter for it yet."
    if check_map["host_reachability"].outcome == ProbeCheckOutcome.FAILED.value:
        return "Targeted probing could not reach the claimed host on the expected local ports."
    if check_map["dry_contact_path"].outcome in {ProbeCheckOutcome.PASSED.value, ProbeCheckOutcome.WARNING.value}:
        return "Targeted probing did not confirm native networking, but it identified a plausible dry-contact retrofit path."
    if check_map["gateway_path"].outcome == ProbeCheckOutcome.WARNING.value:
        return "Targeted probing points to a vendor gateway or module as the most plausible next path."
    return "Targeted probing refined source coverage and fallback paths for the claim."


def run_targeted_probe(session: Session, case_id: int) -> DebugCaseRead:
    debug_case = _load_debug_case(session, case_id)
    if debug_case is None:
        raise KeyError(case_id)

    claim = _claim_from_case(debug_case)
    baseline_report = explain_manual_claim(session, claim)
    site = _get_site(session)
    latest_run = _latest_discovery_run(session)

    checks = [
        _inventory_match_check(baseline_report.matched_device_id, baseline_report.matched_candidate_id),
        _source_coverage_check(site, debug_case, latest_run),
        _host_reachability_check(site, debug_case),
        _http_fingerprint_check(debug_case),
        _modbus_protocol_check(debug_case),
        _dry_contact_check(debug_case),
        _gateway_path_check(debug_case),
        _metering_fallback_check(debug_case),
    ]

    refined_diagnosis = _refine_diagnosis(
        baseline=baseline_report.diagnosis,
        debug_case=debug_case,
        checks=checks,
        matched_device_id=baseline_report.matched_device_id,
        matched_candidate_id=baseline_report.matched_candidate_id,
    )

    now = utcnow()
    probe_run = DebugProbeRun(
        debug_case_id=debug_case.id,
        probe_type="targeted_probe",
        status=ProbeRunStatus.COMPLETED.value,
        summary=_probe_run_summary(checks, baseline_report.matched_device_id, baseline_report.matched_candidate_id),
        checks=[check.as_dict() for check in checks],
        created_at=now,
    )
    session.add(probe_run)
    session.flush()

    refined_diagnosis = refined_diagnosis.model_copy(
        update={
            "raw_diagnostics": {
                **refined_diagnosis.raw_diagnostics,
                "latest_probe_run_id": probe_run.id,
            }
        }
    )

    debug_case.matched_device_id = baseline_report.matched_device_id
    debug_case.matched_candidate_id = baseline_report.matched_candidate_id
    debug_case.diagnosis_snapshot = refined_diagnosis.model_dump()
    if debug_case.status == "open":
        debug_case.status = "probed"
    debug_case.updated_at = now
    session.add(debug_case)
    session.add(
        AuditEvent(
            actor="system",
            action="run_targeted_probe",
            target_type="debug_case",
            target_id=str(debug_case.id),
            summary=probe_run.summary,
            details={"probe_run_id": probe_run.id, "check_count": len(checks)},
            created_at=now,
        )
    )
    session.commit()
    return get_debug_case(session, debug_case.id) or DebugCaseRead(
        id=debug_case.id,
        subject_label=debug_case.subject_label,
        manufacturer=debug_case.manufacturer,
        model=debug_case.model,
        device_type=debug_case.device_type,
        notes=debug_case.notes,
        status=debug_case.status,
        matched_device_id=debug_case.matched_device_id,
        matched_candidate_id=debug_case.matched_candidate_id,
        diagnosis=refined_diagnosis,
        findings=[],
        probe_runs=[],
        created_at=debug_case.created_at,
        updated_at=debug_case.updated_at,
    )
