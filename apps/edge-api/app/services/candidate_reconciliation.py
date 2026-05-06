from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.domain.enums import RecoveryZone
from app.services.discovery_blueprints import RawCandidate


SOURCE_PRIORITY = {
    "local_network_live": 0,
    "modbus_live": 1,
    "eebus_ship_live": 2,
    "network_broadcast_live": 3,
    "mqtt_live": 4,
}
GENERIC_LABELS = {
    "",
    "local energy device",
    "local http device",
    "mqtt energy device",
    "shelly web admin",
    "tasmota",
    "unknown",
}
GENERIC_MODELS = {
    "",
    "local http device",
    "mqtt energy device",
    "unknown",
}
STRICT_RECOVERY_ORDER = {
    RecoveryZone.AUTO_APPLY.value: 0,
    RecoveryZone.GUARDED_APPLY.value: 1,
    RecoveryZone.HUMAN_GATED.value: 2,
}


def _normalize_text(value: str) -> str:
    normalized = [
        character.lower() if character.isalnum() else "-"
        for character in value.strip()
    ]
    text = "".join(normalized).strip("-")
    while "--" in text:
        text = text.replace("--", "-")
    return text


def _normalize_mac(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _evidence_list(evidence: dict[str, Any], key: str) -> list[Any]:
    value = evidence.get(key)
    if isinstance(value, list):
        return list(value)
    if value is None:
        return []
    return [value]


def _candidate_identity_keys(candidate: RawCandidate) -> set[str]:
    evidence = candidate.evidence or {}
    keys: set[str] = set()

    for raw_key in _evidence_list(evidence, "identity_keys"):
        if isinstance(raw_key, str) and raw_key.strip():
            keys.add(raw_key.strip().lower())

    for raw_mac in _evidence_list(evidence, "network_macs"):
        normalized = _normalize_mac(str(raw_mac))
        if normalized:
            keys.add(f"mac:{normalized}")

    mqtt_slug = str(evidence.get("mqtt_device_slug", "")).strip()
    if mqtt_slug:
        keys.add(f"mqtt-slug:{_normalize_text(mqtt_slug)}")

    return keys


def _is_generic_label(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in GENERIC_LABELS or lowered.startswith("local energy device ")


def _is_generic_model(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in GENERIC_MODELS


def _source_rank(candidate: RawCandidate) -> tuple[int, int, int]:
    source_priority = min(
        (SOURCE_PRIORITY.get(source_name, 99) for source_name in (candidate.discovery_sources or [])),
        default=99,
    )
    monitorable_rank = 0 if candidate.capabilities_hint.get("monitorable") else 1
    telemetry_rank = -len(candidate.telemetry or {})
    return (source_priority, monitorable_rank, telemetry_rank)


def _candidate_sort_key(candidate: RawCandidate) -> tuple[int, int, int]:
    return _source_rank(candidate)


def _pick_display_name(candidates: list[RawCandidate], fallback: str) -> str:
    for candidate in sorted(candidates, key=_candidate_sort_key):
        if not _is_generic_label(candidate.display_name):
            return candidate.display_name
    return fallback


def _pick_model(candidates: list[RawCandidate], fallback: str) -> str:
    for candidate in sorted(candidates, key=_candidate_sort_key):
        if not _is_generic_model(candidate.model):
            return candidate.model
    return fallback


def _pick_firmware(candidates: list[RawCandidate], fallback: str) -> str:
    for candidate in sorted(candidates, key=_candidate_sort_key):
        firmware = candidate.firmware.strip()
        if firmware and firmware.lower() != "unknown":
            return candidate.firmware
    return fallback


def _pick_issue_code(candidates: list[RawCandidate], any_monitorable: bool) -> str | None:
    if any_monitorable:
        return None
    for candidate in sorted(candidates, key=_candidate_sort_key):
        if candidate.issue_code:
            return candidate.issue_code
    return None


def _pick_recovery_zone(candidates: list[RawCandidate], base_zone: str) -> str:
    strictest_candidate = max(
        candidates,
        key=lambda candidate: STRICT_RECOVERY_ORDER.get(candidate.recovery_zone, 0),
    )
    return strictest_candidate.recovery_zone or base_zone


def _merge_telemetry(candidates: list[RawCandidate]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    for candidate in sorted(candidates, key=_source_rank, reverse=True):
        telemetry.update(candidate.telemetry or {})
    return telemetry


def _merge_evidence(candidates: list[RawCandidate]) -> dict[str, Any]:
    primary = sorted(candidates, key=_candidate_sort_key)[0]
    source_evidence: dict[str, Any] = {}
    network_macs: set[str] = set()
    hostnames: set[str] = set()
    identity_keys: set[str] = set()

    for candidate in candidates:
        candidate_source = ",".join(candidate.discovery_sources or ["unknown"])
        source_evidence[candidate_source] = candidate.evidence
        identity_keys.update(_candidate_identity_keys(candidate))
        for value in _evidence_list(candidate.evidence or {}, "network_macs"):
            if str(value).strip():
                network_macs.add(str(value).strip())
        for value in _evidence_list(candidate.evidence or {}, "hostnames"):
            if str(value).strip():
                hostnames.add(str(value).strip())

    merged = dict(primary.evidence or {})
    merged["reconciled"] = len(candidates) > 1
    merged["source_evidence"] = source_evidence
    if identity_keys:
        merged["identity_keys"] = sorted(identity_keys)
    if network_macs:
        merged["network_macs"] = sorted(network_macs)
    if hostnames:
        merged["hostnames"] = sorted(hostnames)
    return merged


def _merge_cluster(candidates: list[RawCandidate]) -> RawCandidate:
    primary = sorted(candidates, key=_candidate_sort_key)[0]
    if len(candidates) == 1:
        return primary

    any_monitorable = any(candidate.capabilities_hint.get("monitorable") for candidate in candidates)
    any_controllable = any(candidate.capabilities_hint.get("controllable") for candidate in candidates)
    any_optimizable = any(candidate.capabilities_hint.get("optimizable") for candidate in candidates)

    merged_capabilities = {
        "visible": any(candidate.capabilities_hint.get("visible") for candidate in candidates),
        "monitorable": any_monitorable,
        "controllable": any_controllable,
        "optimizable": any_optimizable,
    }
    merged_sources = sorted({source for candidate in candidates for source in (candidate.discovery_sources or [])})
    merged_protocols = sorted({protocol for candidate in candidates for protocol in (candidate.protocols or [])})
    return replace(
        primary,
        display_name=_pick_display_name(candidates, primary.display_name),
        model=_pick_model(candidates, primary.model),
        firmware=_pick_firmware(candidates, primary.firmware),
        discovery_sources=merged_sources,
        protocols=merged_protocols,
        telemetry=_merge_telemetry(candidates),
        evidence=_merge_evidence(candidates),
        recovery_zone=_pick_recovery_zone(candidates, primary.recovery_zone),
        issue_code=_pick_issue_code(candidates, any_monitorable),
        capabilities_hint=merged_capabilities,
    )


def reconcile_candidates(candidates: list[RawCandidate]) -> list[RawCandidate]:
    if not candidates:
        return []

    clusters: list[list[RawCandidate]] = []
    cluster_keys: list[set[str]] = []

    for candidate in candidates:
        candidate_keys = _candidate_identity_keys(candidate)
        matching_indices = [
            index
            for index, existing_keys in enumerate(cluster_keys)
            if candidate_keys and existing_keys.intersection(candidate_keys)
        ]

        if not matching_indices:
            clusters.append([candidate])
            cluster_keys.append(set(candidate_keys))
            continue

        target_index = matching_indices[0]
        clusters[target_index].append(candidate)
        cluster_keys[target_index].update(candidate_keys)

        for duplicate_index in reversed(matching_indices[1:]):
            clusters[target_index].extend(clusters.pop(duplicate_index))
            cluster_keys[target_index].update(cluster_keys.pop(duplicate_index))

    reconciled = [_merge_cluster(cluster) for cluster in clusters]
    return sorted(reconciled, key=lambda candidate: candidate.display_name.lower())
