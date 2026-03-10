from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.domain.enums import RecoveryZone
from app.services.discovery_blueprints import RawCandidate


ENERGY_KEYWORDS = {
    "battery",
    "charger",
    "consumption",
    "energy",
    "ev",
    "export",
    "grid",
    "heat pump",
    "heatpump",
    "import",
    "inverter",
    "meter",
    "photovoltaic",
    "plug",
    "power",
    "pv",
    "relay",
    "shelly",
    "smart meter",
    "smart plug",
    "solar",
    "storage",
    "tasmota",
    "wallbox",
}

HIGH_SIGNAL_KEYWORDS = {
    "battery",
    "charger",
    "emeter",
    "evcc",
    "evse",
    "heat pump",
    "heatpump",
    "inverter",
    "opendtu",
    "photovoltaic",
    "pv",
    "shelly",
    "smart meter",
    "solar",
    "storage",
    "tasmota",
    "wallbox",
}

DEVICE_RULES = [
    ("pv_inverter", ("pv", "solar", "inverter", "photovoltaic", "fronius", "sunny", "solaredge", "kostal", "opendtu"), 0.88),
    ("battery", ("battery", "storage", "ess", "powerwall", "byd", "soc"), 0.87),
    ("grid_meter", ("grid", "meter", "emeter", "powermeter", "import", "export", "3em"), 0.86),
    ("wallbox", ("wallbox", "charger", "evcc", "evse", "charging", "easee", "zaptec", "go-e", "tesla"), 0.87),
    ("heat_pump", ("heat pump", "heatpump", "hvac", "compressor", "heating", "arotherm", "vaillant"), 0.84),
    ("smart_appliance", ("plug", "relay", "appliance", "washer", "laundry", "dishwasher", "tasmota", "shelly"), 0.8),
]

MANUFACTURER_TOKENS = {
    "byd": "BYD",
    "easee": "Easee",
    "evcc": "evcc",
    "fronius": "Fronius",
    "go-e": "go-e",
    "kostal": "Kostal",
    "opendtu": "OpenDTU",
    "shelly": "Shelly",
    "solaredge": "SolarEdge",
    "sunny": "SMA",
    "tasmota": "Tasmota",
    "vaillant": "Vaillant",
    "zaptec": "Zaptec",
}


class LocalNetworkSourceError(RuntimeError):
    pass


@dataclass(slots=True)
class HttpDocument:
    path: str
    status_code: int
    headers: dict[str, str]
    text: str
    json_body: Any | None


@dataclass(slots=True)
class HttpDeviceContext:
    host: str
    base_url: str
    root: HttpDocument
    documents: dict[str, HttpDocument]


@dataclass(slots=True)
class LocalNetworkDiscoveryBatch:
    source_name: str
    status: str
    message: str
    candidates: list[RawCandidate]


def _slugify(value: str) -> str:
    normalized = [character.lower() if character.isalnum() else "-" for character in value]
    slug = "".join(normalized).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "local-http-device"


def _extract_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _combined_text(*values: str | None) -> str:
    return " ".join(value for value in values if value).lower()


def _normalized_search_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def text_matches_keyword(haystack: str, keyword: str) -> bool:
    normalized_haystack = _normalized_search_text(haystack)
    normalized_keyword = _normalized_search_text(keyword)
    if not normalized_haystack or not normalized_keyword:
        return False
    if len(normalized_keyword) <= 3 or " " in normalized_keyword:
        return f" {normalized_keyword} " in f" {normalized_haystack} "
    return normalized_keyword in normalized_haystack


def matched_keywords(haystack: str, keywords: tuple[str, ...] | list[str] | set[str]) -> list[str]:
    return [keyword for keyword in keywords if text_matches_keyword(haystack, keyword)]


def _asset_name_for_type(device_type: str) -> str:
    mapping = {
        "pv_inverter": "PV Generation",
        "battery": "Battery Buffer",
        "grid_meter": "Grid Metering",
        "wallbox": "EV Charging",
        "heat_pump": "Thermal Control",
        "smart_appliance": "Flexible Smart Load",
    }
    return mapping.get(device_type, "Unclassified Energy Device")


def _parse_numeric(raw_value: Any) -> int | float | None:
    if isinstance(raw_value, (int, float)):
        numeric = float(raw_value)
        return int(numeric) if numeric.is_integer() else round(numeric, 3)
    if raw_value is None:
        return None
    try:
        numeric = float(str(raw_value).strip())
    except ValueError:
        return None
    return int(numeric) if numeric.is_integer() else round(numeric, 3)


def _telemetry_from_tasmota(status_payload: dict[str, Any]) -> dict[str, Any]:
    energy = (
        status_payload.get("StatusSNS", {}).get("ENERGY")
        or status_payload.get("StatusSNS", {}).get("ANALOG")
        or {}
    )
    if not isinstance(energy, dict):
        return {}

    telemetry: dict[str, Any] = {}
    mappings = {
        "Current": "current_a",
        "Power": "power_w",
        "ReactivePower": "reactive_power_var",
        "Today": "energy_today_kwh",
        "Total": "energy_total_kwh",
        "Voltage": "voltage_v",
    }
    for raw_key, metric_key in mappings.items():
        value = _parse_numeric(energy.get(raw_key))
        if value is not None:
            telemetry[metric_key] = value
    return telemetry


def _telemetry_from_shelly_status(status_payload: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}

    emeters = status_payload.get("emeters")
    if isinstance(emeters, list):
        for index, meter in enumerate(emeters):
            if not isinstance(meter, dict):
                continue
            power = _parse_numeric(meter.get("power"))
            total = _parse_numeric(meter.get("total"))
            voltage = _parse_numeric(meter.get("voltage"))
            if power is not None:
                telemetry[f"phase_{index}_power_w"] = power
            if total is not None:
                telemetry[f"phase_{index}_energy_total"] = total
            if voltage is not None:
                telemetry[f"phase_{index}_voltage_v"] = voltage

    meters = status_payload.get("meters")
    if isinstance(meters, list):
        for index, meter in enumerate(meters):
            if not isinstance(meter, dict):
                continue
            power = _parse_numeric(meter.get("power"))
            total = _parse_numeric(meter.get("total"))
            if power is not None:
                telemetry[f"relay_{index}_power_w"] = power
            if total is not None:
                telemetry[f"relay_{index}_energy_total"] = total

    return telemetry


def _telemetry_from_shelly_rpc(status_payload: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    for key, value in status_payload.items():
        if not isinstance(value, dict):
            continue
        prefix = _slugify(key).replace("-", "_")
        for raw_key, metric_suffix in (
            ("apower", "power_w"),
            ("current", "current_a"),
            ("voltage", "voltage_v"),
            ("act_power", "power_w"),
            ("a_current", "current_a"),
            ("a_voltage", "voltage_v"),
            ("temperature", "temperature_c"),
        ):
            numeric = _parse_numeric(value.get(raw_key))
            if numeric is not None:
                telemetry[f"{prefix}_{metric_suffix}"] = numeric
        aenergy = value.get("aenergy")
        if isinstance(aenergy, dict):
            total = _parse_numeric(aenergy.get("total"))
            if total is not None:
                telemetry[f"{prefix}_energy_total"] = total
    return telemetry


def _classify_device_from_text(haystack: str) -> tuple[str, float, str]:
    for device_type, tokens, confidence in DEVICE_RULES:
        matched = matched_keywords(haystack, tokens)
        if matched:
            return (
                device_type,
                confidence,
                f"Local HTTP fingerprint matched {', '.join(matched[:3])} for the {device_type} profile.",
            )
    return (
        "unclassified_energy_device",
        0.7,
        "Local HTTP fingerprint found an energy-relevant interface, but no strong device profile matched yet.",
    )


def _manufacturer_from_text(haystack: str, server_header: str) -> str:
    combined = f"{haystack} {server_header}".lower()
    for token, manufacturer in MANUFACTURER_TOKENS.items():
        if token in combined:
            return manufacturer
    if server_header:
        return server_header.split("/", 1)[0].strip() or "Unknown"
    return "Unknown"


def _display_name_from_context(context: HttpDeviceContext, fallback: str) -> str:
    title = _extract_title(context.root.text)
    if title and title.lower() not in {"index", "main menu"}:
        return title
    return fallback


def _energy_haystack(context: HttpDeviceContext, extra_values: list[str] | None = None) -> str:
    values = [
        context.root.headers.get("server"),
        _extract_title(context.root.text),
        context.root.text[:800],
        *[document.text[:600] for document in context.documents.values()],
    ]
    if extra_values:
        values.extend(extra_values)
    return _combined_text(*values)


def _is_energy_relevant(context: HttpDeviceContext, extra_values: list[str] | None = None) -> bool:
    haystack = _energy_haystack(context, extra_values)
    if matched_keywords(haystack, HIGH_SIGNAL_KEYWORDS):
        return True
    match_count = len(matched_keywords(haystack, ENERGY_KEYWORDS))
    return match_count >= 2


def _stable_identifier(context: HttpDeviceContext, *values: str | None) -> str:
    for value in values:
        if value:
            return str(value)
    return context.host


def _build_candidate(
    *,
    context: HttpDeviceContext,
    stable_identifier: str,
    display_name: str,
    manufacturer: str,
    model: str,
    firmware: str,
    device_type: str,
    telemetry: dict[str, Any],
    reasoning: str,
    confidence: float,
    explanation_hint: str,
    next_step_hint: str,
    evidence: dict[str, Any],
) -> RawCandidate:
    capabilities_hint = {
        "visible": True,
        "monitorable": bool(telemetry),
        "controllable": False,
        "optimizable": False,
    }
    stable_slug = _slugify(stable_identifier)
    return RawCandidate(
        candidate_id=f"cand-local-http-{stable_slug}",
        device_id=f"dev-local-http-{stable_slug}",
        asset_id=f"asset-local-http-{stable_slug}",
        asset_name=_asset_name_for_type(device_type),
        display_name=display_name,
        manufacturer=manufacturer,
        model=model,
        firmware=firmware,
        device_type=device_type,
        discovery_sources=["local_network_live"],
        protocols=["http_local"],
        telemetry=telemetry,
        evidence={
            **evidence,
            "http_base_url": context.base_url,
            "http_host": context.host,
            "http_paths": sorted(["/"] + list(context.documents.keys())),
            "http_server": context.root.headers.get("server", ""),
            "identity_keys": sorted(
                {
                    f"http-host:{_slugify(context.host)}",
                    f"network-host:{_slugify(context.host)}",
                    *[
                        str(value)
                        for value in (
                            evidence.get("identity_keys") if isinstance(evidence.get("identity_keys"), list) else []
                        )
                    ],
                }
            ),
            "classification_reasoning": reasoning,
            "classification_confidence": confidence,
        },
        recovery_zone=RecoveryZone.AUTO_APPLY.value,
        issue_code=None,
        explanation_hint=explanation_hint,
        next_step_hint=next_step_hint,
        capabilities_hint=capabilities_hint,
    )


def _build_tasmota_candidate(context: HttpDeviceContext) -> RawCandidate | None:
    server_header = context.root.headers.get("server", "")
    title = _extract_title(context.root.text)
    haystack = _energy_haystack(context)
    if "tasmota" not in _combined_text(server_header, title, context.root.text[:400]):
        return None

    status_document = context.documents.get("/cm?cmnd=Status%200")
    status_payload = status_document.json_body if status_document and isinstance(status_document.json_body, dict) else {}
    telemetry = _telemetry_from_tasmota(status_payload)
    tasmota_haystack = _combined_text(haystack, json.dumps(status_payload, ensure_ascii=True))
    if any(token in tasmota_haystack for token in {"grid", "meter", "emeter", "3em"}):
        device_type = "grid_meter"
        confidence = 0.86 if telemetry else 0.8
        reasoning = "Local Tasmota HTTP fingerprint matched meter-oriented telemetry and the grid_meter profile."
    else:
        device_type = "smart_appliance"
        confidence = 0.86 if telemetry else 0.8
        reasoning = "Local Tasmota HTTP fingerprint matched an appliance-oriented energy profile."
    friendly_names = status_payload.get("Status", {}).get("FriendlyName")
    friendly_name = friendly_names[0] if isinstance(friendly_names, list) and friendly_names else None
    hostname = status_payload.get("StatusNET", {}).get("Hostname")
    network_mac = status_payload.get("StatusNET", {}).get("Mac")
    firmware = status_payload.get("StatusFWR", {}).get("Version") or server_header or "unknown"
    stable_identifier = _stable_identifier(
        context,
        status_payload.get("StatusNET", {}).get("Mac"),
        hostname,
    )
    display_name = str(friendly_name or _display_name_from_context(context, hostname or f"Tasmota {context.host}"))
    identity_keys = []
    if hostname:
        identity_keys.append(f"mqtt-slug:{_slugify(str(hostname))}")
    return _build_candidate(
        context=context,
        stable_identifier=stable_identifier,
        display_name=display_name,
        manufacturer="Tasmota",
        model="Local HTTP device",
        firmware=str(firmware),
        device_type=device_type,
        telemetry=telemetry,
        reasoning=reasoning,
        confidence=max(confidence, 0.86 if telemetry else 0.8),
        explanation_hint=(
            "Helios identified a Tasmota HTTP interface on the local network and validated a read-only status path."
            if telemetry
            else "Helios identified a Tasmota HTTP interface, but no validated energy telemetry path is available yet."
        ),
        next_step_hint=(
            "Keep the device monitorable through the local HTTP status endpoint."
            if telemetry
            else "Probe the local status endpoint again or add a more specific adapter profile."
        ),
        evidence=(
            {
                "tasmota_status": status_payload,
                "network_macs": [network_mac] if network_mac else [],
                "hostnames": [value for value in [hostname, friendly_name] if value],
                "identity_keys": identity_keys,
            }
            if status_payload
            else {"fingerprint_profile": "tasmota_http", "hostnames": [value for value in [hostname] if value], "identity_keys": identity_keys}
        ),
    )


def _build_shelly_candidate(context: HttpDeviceContext) -> RawCandidate | None:
    server_header = context.root.headers.get("server", "")
    title = _extract_title(context.root.text)
    profile_haystack = _combined_text(server_header, title, context.root.text[:600], *context.documents.keys())
    device_info = None
    status_payload = None

    shelly_document = context.documents.get("/shelly")
    if shelly_document and isinstance(shelly_document.json_body, dict):
        device_info = shelly_document.json_body

    status_document = context.documents.get("/status")
    if status_document and isinstance(status_document.json_body, dict):
        status_payload = status_document.json_body

    rpc_info_document = context.documents.get("/rpc/Shelly.GetDeviceInfo")
    if rpc_info_document and isinstance(rpc_info_document.json_body, dict):
        device_info = rpc_info_document.json_body

    rpc_status_document = context.documents.get("/rpc/Shelly.GetStatus")
    if rpc_status_document and isinstance(rpc_status_document.json_body, dict):
        status_payload = rpc_status_document.json_body

    if "shelly" not in profile_haystack and not device_info and not status_payload:
        return None

    telemetry = {}
    if status_document and isinstance(status_document.json_body, dict):
        telemetry.update(_telemetry_from_shelly_status(status_document.json_body))
    if rpc_status_document and isinstance(rpc_status_document.json_body, dict):
        telemetry.update(_telemetry_from_shelly_rpc(rpc_status_document.json_body))

    haystack = _energy_haystack(
        context,
        [
            json.dumps(device_info, ensure_ascii=True) if device_info else "",
            json.dumps(status_payload, ensure_ascii=True) if status_payload else "",
        ],
    )
    device_type, confidence, reasoning = _classify_device_from_text(haystack)
    if any(key.startswith("phase_") for key in telemetry):
        device_type = "grid_meter"
        confidence = max(confidence, 0.9)
        reasoning = "Local Shelly telemetry exposed multi-phase energy channels and matched the grid_meter profile."
    elif telemetry and any(key.startswith("relay_") or "switch_0_power_w" in key for key in telemetry):
        device_type = "smart_appliance"
        confidence = max(confidence, 0.85)
        reasoning = "Local Shelly telemetry exposed relay-level power metrics and matched the smart_appliance profile."

    display_name = str(
        (device_info or {}).get("name")
        or (status_payload or {}).get("name")
        or _display_name_from_context(context, f"Shelly {context.host}")
    )
    model = str(
        (device_info or {}).get("type")
        or (device_info or {}).get("model")
        or (status_payload or {}).get("mac")
        or "Local HTTP device"
    )
    firmware = str(
        (device_info or {}).get("fw")
        or (device_info or {}).get("ver")
        or (status_payload or {}).get("fw")
        or server_header
        or "unknown"
    )
    stable_identifier = _stable_identifier(
        context,
        (device_info or {}).get("id"),
        (device_info or {}).get("mac"),
        (status_payload or {}).get("mac"),
    )
    shelly_id = (device_info or {}).get("id")
    network_mac = (device_info or {}).get("mac") or (status_payload or {}).get("mac")
    identity_keys = [f"mqtt-slug:{_slugify(str(shelly_id))}"] if shelly_id else []
    evidence: dict[str, Any] = {"fingerprint_profile": "shelly_http"}
    if device_info:
        evidence["shelly_info"] = device_info
    if status_payload:
        evidence["shelly_status"] = status_payload
    if network_mac:
        evidence["network_macs"] = [network_mac]
    if display_name:
        evidence["hostnames"] = [display_name]
    if identity_keys:
        evidence["identity_keys"] = identity_keys
    return _build_candidate(
        context=context,
        stable_identifier=stable_identifier,
        display_name=display_name,
        manufacturer="Shelly",
        model=model,
        firmware=firmware,
        device_type=device_type,
        telemetry=telemetry,
        reasoning=reasoning,
        confidence=confidence,
        explanation_hint=(
            "Helios identified a Shelly local interface and validated a read-only telemetry path."
            if telemetry
            else "Helios identified a Shelly local interface, but no validated telemetry endpoint is available yet."
        ),
        next_step_hint=(
            "Keep the device monitorable through the Shelly local API."
            if telemetry
            else "Probe the local Shelly API again or add a more specific adapter profile."
        ),
        evidence=evidence,
    )


def _build_generic_candidate(context: HttpDeviceContext) -> RawCandidate | None:
    if not _is_energy_relevant(context):
        return None

    haystack = _energy_haystack(context)
    device_type, confidence, reasoning = _classify_device_from_text(haystack)
    title = _extract_title(context.root.text)
    display_name = _display_name_from_context(context, f"Local energy device {context.host}")
    manufacturer = _manufacturer_from_text(haystack, context.root.headers.get("server", ""))
    model = title or context.root.headers.get("server", "") or "Local HTTP interface"
    return _build_candidate(
        context=context,
        stable_identifier=context.host,
        display_name=display_name,
        manufacturer=manufacturer,
        model=model,
        firmware="unknown",
        device_type=device_type,
        telemetry={},
        reasoning=reasoning,
        confidence=confidence,
        explanation_hint=(
            "Helios identified an energy-relevant local HTTP interface, but no validated telemetry path is available yet."
        ),
        next_step_hint="Probe vendor-specific local APIs or add an adapter profile for telemetry validation.",
        evidence={
            "fingerprint_profile": "generic_http_energy",
            "identity_keys": [f"http-host:{_slugify(context.host)}"],
        },
    )


def build_candidate_from_http_context(context: HttpDeviceContext) -> RawCandidate | None:
    for builder in (_build_tasmota_candidate, _build_shelly_candidate, _build_generic_candidate):
        candidate = builder(context)
        if candidate is not None:
            return candidate
    return None


def build_candidates_from_http_contexts(contexts: list[HttpDeviceContext]) -> list[RawCandidate]:
    candidates = [candidate for candidate in (build_candidate_from_http_context(context) for context in contexts) if candidate]
    return sorted(candidates, key=lambda candidate: candidate.display_name.lower())


def _candidate_probe_paths(root: HttpDocument) -> list[str]:
    server_header = root.headers.get("server", "")
    title = _extract_title(root.text)
    haystack = _combined_text(server_header, title, root.text[:800])
    paths: list[str] = []

    if "tasmota" in haystack:
        paths.append("/cm?cmnd=Status%200")

    if "shelly" in haystack or "mongoose" in haystack:
        paths.extend(
            [
                "/shelly",
                "/status",
                "/rpc/Shelly.GetDeviceInfo",
                "/rpc/Shelly.GetStatus",
            ]
        )

    return paths


async def _fetch_document(client: httpx.AsyncClient, base_url: str, path: str, timeout_seconds: float) -> HttpDocument | None:
    url = f"{base_url}{path}"
    try:
        response = await client.get(
            url,
            timeout=timeout_seconds,
            headers={"Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
        )
    except httpx.HTTPError:
        return None

    text = response.text[:6000]
    json_body = None
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            json_body = response.json()
        except json.JSONDecodeError:
            json_body = None
    return HttpDocument(
        path=path,
        status_code=response.status_code,
        headers={key.lower(): value for key, value in response.headers.items()},
        text=text,
        json_body=json_body,
    )


async def _probe_host(client: httpx.AsyncClient, host: str, timeout_seconds: float) -> HttpDeviceContext | None:
    for base_url in (f"http://{host}", f"https://{host}"):
        root = await _fetch_document(client, base_url, "/", timeout_seconds)
        if root is None:
            continue

        documents: dict[str, HttpDocument] = {}
        for path in _candidate_probe_paths(root):
            document = await _fetch_document(client, base_url, path, timeout_seconds)
            if document is not None:
                documents[path] = document

        return HttpDeviceContext(
            host=host,
            base_url=base_url,
            root=root,
            documents=documents,
        )
    return None


async def _scan_subnet_async(subnet: str, timeout_seconds: float, concurrency: int, max_hosts: int) -> list[HttpDeviceContext]:
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError as exc:
        raise LocalNetworkSourceError("Local subnet is not a valid CIDR range.") from exc

    hosts = [str(host) for host in network.hosts()]
    if not hosts:
        return []
    if len(hosts) > max_hosts:
        raise LocalNetworkSourceError(
            f"Local subnet contains {len(hosts)} hosts, which exceeds the configured scan limit of {max_hosts}."
        )

    semaphore = asyncio.Semaphore(max(1, concurrency))
    contexts: list[HttpDeviceContext] = []
    transport = httpx.AsyncHTTPTransport(retries=0, verify=False)

    async with httpx.AsyncClient(follow_redirects=True, transport=transport) as client:
        async def scan_host(host: str) -> None:
            async with semaphore:
                context = await _probe_host(client, host, timeout_seconds)
                if context is not None:
                    contexts.append(context)

        await asyncio.gather(*(scan_host(host) for host in hosts))

    return sorted(contexts, key=lambda context: context.host)


def discover_local_network_site(
    *,
    subnet: str,
    timeout_seconds: float = 1.5,
    concurrency: int = 32,
    max_hosts: int = 256,
) -> LocalNetworkDiscoveryBatch:
    try:
        contexts = asyncio.run(_scan_subnet_async(subnet, timeout_seconds, concurrency, max_hosts))
    except LocalNetworkSourceError as exc:
        return LocalNetworkDiscoveryBatch(
            source_name="local_network_live",
            status="failed",
            message=str(exc),
            candidates=[],
        )
    except RuntimeError as exc:
        return LocalNetworkDiscoveryBatch(
            source_name="local_network_live",
            status="failed",
            message=f"Local network discovery failed to start: {exc}",
            candidates=[],
        )

    candidates = build_candidates_from_http_contexts(contexts)
    if candidates:
        return LocalNetworkDiscoveryBatch(
            source_name="local_network_live",
            status="completed",
            message=f"Imported {len(candidates)} energy-relevant local HTTP device candidates from subnet scanning.",
            candidates=candidates,
        )

    return LocalNetworkDiscoveryBatch(
        source_name="local_network_live",
        status="completed",
        message="Local network discovery completed, but no energy-relevant HTTP interfaces were identified.",
        candidates=[],
    )


def probe_http_host(host: str, timeout_seconds: float = 1.0) -> HttpDeviceContext | None:
    async def run_probe() -> HttpDeviceContext | None:
        transport = httpx.AsyncHTTPTransport(retries=0, verify=False)
        async with httpx.AsyncClient(follow_redirects=True, transport=transport) as client:
            return await _probe_host(client, host, timeout_seconds)

    try:
        return asyncio.run(run_probe())
    except RuntimeError:
        return None


def fingerprint_http_host(host: str, timeout_seconds: float = 1.0) -> RawCandidate | None:
    context = probe_http_host(host, timeout_seconds)
    if context is None:
        return None
    return build_candidate_from_http_context(context)
