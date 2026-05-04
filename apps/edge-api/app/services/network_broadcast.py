from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.domain.enums import RecoveryZone
from app.services.discovery_blueprints import RawCandidate
from app.services.local_network import (
    DEVICE_RULES,
    HIGH_SIGNAL_KEYWORDS,
    MANUFACTURER_TOKENS,
    matched_keywords,
)


MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
DNS_TYPE_A = 1
DNS_TYPE_PTR = 12
DNS_TYPE_TXT = 16
DNS_TYPE_AAAA = 28
DNS_TYPE_SRV = 33


class BroadcastDiscoveryError(RuntimeError):
    pass


@dataclass(slots=True)
class BroadcastAnnouncement:
    protocol: str
    host: str | None
    service_type: str
    service_name: str
    server: str
    location: str
    usn: str
    txt: list[str]


@dataclass(slots=True)
class BroadcastDiscoveryBatch:
    source_name: str
    status: str
    message: str
    candidates: list[RawCandidate]


def _slugify(value: str) -> str:
    normalized = [character.lower() if character.isalnum() else "-" for character in value]
    slug = "".join(normalized).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "network-broadcast"


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


def _combined_text(*values: str | None) -> str:
    return " ".join(value for value in values if value).lower()


def _classify_device_from_text(haystack: str) -> tuple[str, float, str]:
    for device_type, tokens, confidence in DEVICE_RULES:
        matched = matched_keywords(haystack, tokens)
        if matched:
            return (
                device_type,
                confidence,
                f"Network broadcast fingerprint matched {', '.join(matched[:3])} for the {device_type} profile.",
            )
    return (
        "unclassified_energy_device",
        0.68,
        "Network broadcast discovery found an energy-relevant service advertisement, but no strong device profile matched yet.",
    )


def _manufacturer_from_text(haystack: str, server_header: str) -> str:
    combined = f"{haystack} {server_header}".lower()
    for token, manufacturer in MANUFACTURER_TOKENS.items():
        if matched_keywords(combined, [token]):
            return manufacturer
    if server_header:
        return server_header.split("/", 1)[0].strip() or "Unknown"
    return "Unknown"


def _is_energy_relevant(haystack: str) -> bool:
    if matched_keywords(haystack, HIGH_SIGNAL_KEYWORDS):
        return True
    return bool(matched_keywords(haystack, MANUFACTURER_TOKENS))


def _extract_uuid_keys(values: list[str]) -> list[str]:
    uuid_keys: list[str] = []
    for value in values:
        for part in value.split("::"):
            normalized = part.strip().lower()
            if normalized.startswith("uuid:") and len(normalized) > 5:
                uuid_keys.append(f"service-uuid:{normalized[5:]}")
    return uuid_keys


def _extract_url_host_keys(values: list[str]) -> list[str]:
    identity_keys: list[str] = []
    for value in values:
        for token in value.split():
            url_token = token.split("=", 1)[1] if "=" in token else token
            if "://" not in url_token:
                continue
            parsed = urlparse(url_token.strip())
            if parsed.hostname:
                slug = _slugify(parsed.hostname)
                identity_keys.append(f"http-host:{slug}")
                identity_keys.append(f"network-host:{slug}")
    return identity_keys


def _service_instance_key(value: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None
    if "._" in normalized and normalized.endswith(".local"):
        normalized = normalized.split("._", 1)[0]
    return _slugify(normalized)


def _is_eebus_ship_announcement(announcements: list[BroadcastAnnouncement]) -> bool:
    for announcement in announcements:
        haystack = _combined_text(announcement.service_type, announcement.service_name, *announcement.txt)
        if "_ship._tcp.local" in haystack or "eebus" in haystack:
            return True
    return False


def _build_announcement_candidate(group_key: str, announcements: list[BroadcastAnnouncement]) -> RawCandidate | None:
    protocols = sorted({announcement.protocol for announcement in announcements})
    if _is_eebus_ship_announcement(announcements):
        protocols = sorted({*protocols, "eebus_ship"})
    host = next((announcement.host for announcement in announcements if announcement.host), None)
    service_names = [announcement.service_name for announcement in announcements if announcement.service_name]
    service_types = [announcement.service_type for announcement in announcements if announcement.service_type]
    servers = [announcement.server for announcement in announcements if announcement.server]
    locations = [announcement.location for announcement in announcements if announcement.location]
    usns = [announcement.usn for announcement in announcements if announcement.usn]
    txt_values = [item for announcement in announcements for item in announcement.txt]

    haystack = _combined_text(
        *service_names,
        *service_types,
        *servers,
        *locations,
        *usns,
        *txt_values,
    )
    if not _is_energy_relevant(haystack):
        return None

    device_type, confidence, reasoning = _classify_device_from_text(haystack)
    display_name = service_names[0] if service_names else (servers[0] if servers else f"Network service {group_key}")
    manufacturer = _manufacturer_from_text(haystack, servers[0] if servers else "")
    model = service_types[0] if service_types else "Service advertisement"
    identity_keys = [f"service-name:{_slugify(name)}" for name in service_names[:2]]
    service_instance_keys = {
        f"service-instance:{service_instance}"
        for service_instance in (
            _service_instance_key(service_name)
            for service_name in service_names[:4]
        )
        if service_instance
    }
    if host:
        identity_keys.append(f"http-host:{_slugify(host)}")
        identity_keys.append(f"network-host:{_slugify(host)}")
    identity_keys.extend(sorted(service_instance_keys))
    identity_keys.extend(_extract_uuid_keys(service_types + usns))
    identity_keys.extend(_extract_url_host_keys(locations + txt_values))

    return RawCandidate(
        candidate_id=f"cand-broadcast-{_slugify(host or group_key)}",
        device_id=f"dev-broadcast-{_slugify(host or group_key)}",
        asset_id=f"asset-broadcast-{_slugify(host or group_key)}",
        asset_name=_asset_name_for_type(device_type),
        display_name=display_name,
        manufacturer=manufacturer,
        model=model,
        firmware="unknown",
        device_type=device_type,
        discovery_sources=["network_broadcast_live"],
        protocols=protocols,
        telemetry={},
        evidence={
            "broadcast_announcements": [
                {
                    "protocol": announcement.protocol,
                    "host": announcement.host,
                    "service_name": announcement.service_name,
                    "service_type": announcement.service_type,
                    "server": announcement.server,
                    "location": announcement.location,
                    "usn": announcement.usn,
                    "txt": announcement.txt,
                }
                for announcement in announcements
            ],
            "identity_keys": sorted(set(identity_keys)),
            "classification_reasoning": reasoning,
            "classification_confidence": confidence,
        },
        recovery_zone=RecoveryZone.AUTO_APPLY.value,
        issue_code=None,
        explanation_hint=(
            "Helios identified an energy-relevant local network advertisement, but has not validated a telemetry path yet."
        ),
        next_step_hint="Use the broadcast evidence to probe a local HTTP, MQTT or protocol-specific read path.",
        capabilities_hint={
            "visible": True,
            "monitorable": False,
            "controllable": False,
            "optimizable": False,
        },
    )


def build_candidates_from_broadcast_announcements(announcements: list[BroadcastAnnouncement]) -> list[RawCandidate]:
    grouped: dict[str, list[BroadcastAnnouncement]] = {}
    for announcement in announcements:
        if announcement.host:
            key = f"host:{announcement.host}"
        else:
            key = f"service:{announcement.service_name or announcement.service_type}"
        grouped.setdefault(key, []).append(announcement)

    candidates = [
        candidate
        for candidate in (
            _build_announcement_candidate(group_key, group_announcements)
            for group_key, group_announcements in grouped.items()
        )
        if candidate is not None
    ]
    return sorted(candidates, key=lambda candidate: candidate.display_name.lower())


def parse_ssdp_response(payload: bytes, host: str) -> BroadcastAnnouncement | None:
    try:
        text = payload.decode("utf-8", errors="ignore")
    except UnicodeDecodeError:
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    return BroadcastAnnouncement(
        protocol="ssdp",
        host=host,
        service_type=headers.get("st", ""),
        service_name=headers.get("server", "") or headers.get("st", ""),
        server=headers.get("server", ""),
        location=headers.get("location", ""),
        usn=headers.get("usn", ""),
        txt=[],
    )


def _encode_dns_name(name: str) -> bytes:
    labels = name.strip(".").split(".")
    return b"".join(bytes([len(label)]) + label.encode("utf-8") for label in labels if label) + b"\x00"


def _decode_dns_name(message: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    if depth > 10:
        raise BroadcastDiscoveryError("mDNS name compression exceeded safe recursion depth.")

    labels: list[str] = []
    current_offset = offset
    jumped = False
    final_offset = offset

    while current_offset < len(message):
        length = message[current_offset]
        if length == 0:
            current_offset += 1
            if not jumped:
                final_offset = current_offset
            break
        if length & 0xC0 == 0xC0:
            pointer = ((length & 0x3F) << 8) | message[current_offset + 1]
            pointed_name, _ = _decode_dns_name(message, pointer, depth + 1)
            labels.append(pointed_name)
            current_offset += 2
            if not jumped:
                final_offset = current_offset
            jumped = True
            break
        current_offset += 1
        label = message[current_offset:current_offset + length].decode("utf-8", errors="ignore")
        labels.append(label)
        current_offset += length
        if not jumped:
            final_offset = current_offset

    return ".".join(part for part in labels if part), final_offset


def _parse_mdns_records(payload: bytes, source_host: str) -> list[dict[str, Any]]:
    if len(payload) < 12:
        return []
    _, _, question_count, answer_count, authority_count, additional_count = struct.unpack("!HHHHHH", payload[:12])
    offset = 12

    for _ in range(question_count):
        _, offset = _decode_dns_name(payload, offset)
        offset += 4

    records: list[dict[str, Any]] = []
    total_records = answer_count + authority_count + additional_count
    for _ in range(total_records):
        try:
            name, offset = _decode_dns_name(payload, offset)
        except BroadcastDiscoveryError:
            break
        if offset + 10 > len(payload):
            break
        record_type, record_class, _, data_length = struct.unpack("!HHIH", payload[offset:offset + 10])
        offset += 10
        data_offset = offset
        offset += data_length
        if data_offset + data_length > len(payload):
            break

        record: dict[str, Any] = {
            "name": name,
            "type": record_type,
            "class": record_class,
            "source_host": source_host,
        }
        if record_type == DNS_TYPE_PTR:
            target, _ = _decode_dns_name(payload, data_offset)
            record["target"] = target
        elif record_type == DNS_TYPE_SRV and data_length >= 6:
            _, _, port = struct.unpack("!HHH", payload[data_offset:data_offset + 6])
            target, _ = _decode_dns_name(payload, data_offset + 6)
            record["port"] = port
            record["target"] = target
        elif record_type == DNS_TYPE_TXT:
            txt_values: list[str] = []
            txt_offset = data_offset
            while txt_offset < data_offset + data_length:
                entry_length = payload[txt_offset]
                txt_offset += 1
                entry = payload[txt_offset:txt_offset + entry_length].decode("utf-8", errors="ignore")
                txt_values.append(entry)
                txt_offset += entry_length
            record["txt"] = txt_values
        elif record_type == DNS_TYPE_A and data_length == 4:
            record["address"] = socket.inet_ntoa(payload[data_offset:data_offset + 4])
        elif record_type == DNS_TYPE_AAAA and data_length == 16:
            record["address"] = socket.inet_ntop(socket.AF_INET6, payload[data_offset:data_offset + 16])
        records.append(record)
    return records


def _query_mdns(sock: socket.socket, query_name: str, timeout_seconds: float) -> list[dict[str, Any]]:
    query = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + _encode_dns_name(query_name) + struct.pack("!HH", DNS_TYPE_PTR, 0x8001)
    sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
    deadline = time.monotonic() + timeout_seconds
    records: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        sock.settimeout(max(deadline - time.monotonic(), 0.05))
        try:
            payload, address = sock.recvfrom(65535)
        except socket.timeout:
            break
        records.extend(_parse_mdns_records(payload, address[0]))
    return records


def _discover_mdns_announcements(timeout_seconds: float, max_service_types: int) -> list[BroadcastAnnouncement]:
    announcements: list[BroadcastAnnouncement] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))

        service_records = _query_mdns(sock, "_services._dns-sd._udp.local", timeout_seconds)
        service_types = sorted(
            {
                record["target"]
                for record in service_records
                if record.get("type") == DNS_TYPE_PTR and record.get("target")
            }
        )[:max_service_types]

        for service_type in service_types:
            instance_records = _query_mdns(sock, service_type, timeout_seconds / 2)
            address_by_target = {
                record["name"]: record["address"]
                for record in instance_records
                if record.get("type") in {DNS_TYPE_A, DNS_TYPE_AAAA} and record.get("address")
            }
            srv_by_name = {
                record["name"]: record
                for record in instance_records
                if record.get("type") == DNS_TYPE_SRV
            }
            txt_by_name = {
                record["name"]: record.get("txt", [])
                for record in instance_records
                if record.get("type") == DNS_TYPE_TXT
            }

            for record in instance_records:
                if record.get("type") != DNS_TYPE_PTR or record.get("name") != service_type:
                    continue
                service_name = str(record.get("target", ""))
                srv_record = srv_by_name.get(service_name, {})
                target_host = str(srv_record.get("target", ""))
                host = address_by_target.get(target_host) or record.get("source_host")
                announcements.append(
                    BroadcastAnnouncement(
                        protocol="mdns",
                        host=str(host) if host else None,
                        service_type=service_type,
                        service_name=service_name,
                        server=target_host,
                        location="",
                        usn="",
                        txt=list(txt_by_name.get(service_name, [])),
                    )
                )
    return announcements


def _discover_ssdp_announcements(timeout_seconds: float) -> list[BroadcastAnnouncement]:
    payload = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST: {SSDP_GROUP}:{SSDP_PORT}",
            'MAN: "ssdp:discover"',
            "MX: 1",
            "ST: ssdp:all",
            "",
            "",
        ]
    ).encode("utf-8")

    announcements: list[BroadcastAnnouncement] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout_seconds)
        sock.sendto(payload, (SSDP_GROUP, SSDP_PORT))

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                response, address = sock.recvfrom(65535)
            except socket.timeout:
                break
            announcement = parse_ssdp_response(response, address[0])
            if announcement is not None:
                announcements.append(announcement)
    return announcements


def discover_network_broadcast(timeout_seconds: float = 1.0, max_service_types: int = 12) -> BroadcastDiscoveryBatch:
    try:
        announcements = _discover_ssdp_announcements(timeout_seconds) + _discover_mdns_announcements(
            timeout_seconds,
            max_service_types,
        )
    except OSError as exc:
        return BroadcastDiscoveryBatch(
            source_name="network_broadcast_live",
            status="failed",
            message=f"Network broadcast discovery failed: {exc}",
            candidates=[],
        )
    except BroadcastDiscoveryError as exc:
        return BroadcastDiscoveryBatch(
            source_name="network_broadcast_live",
            status="failed",
            message=str(exc),
            candidates=[],
        )

    candidates = build_candidates_from_broadcast_announcements(announcements)
    if candidates:
        return BroadcastDiscoveryBatch(
            source_name="network_broadcast_live",
            status="completed",
            message=f"Imported {len(candidates)} candidates from local mDNS and SSDP advertisements.",
            candidates=candidates,
        )
    return BroadcastDiscoveryBatch(
        source_name="network_broadcast_live",
        status="completed",
        message="Network broadcast discovery completed, but no energy-relevant advertisements were identified.",
        candidates=[],
    )
