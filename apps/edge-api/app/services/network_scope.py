from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network


@dataclass(frozen=True, slots=True)
class ReachableSubnetOption:
    cidr: str
    interface: str
    label: str


def parse_configured_subnets(raw_value: str) -> list[str]:
    separators = [",", "\n", ";"]
    normalized = raw_value
    for separator in separators[1:]:
        normalized = normalized.replace(separator, separators[0])

    seen: set[str] = set()
    subnets: list[str] = []
    for chunk in normalized.split(separators[0]):
        subnet = chunk.strip()
        if not subnet or subnet in seen:
            continue
        seen.add(subnet)
        subnets.append(subnet)
    return subnets


def _ipv4_from_linux_hex(value: str) -> IPv4Address:
    raw_bytes = bytes.fromhex(value)
    return IPv4Address(int.from_bytes(raw_bytes, "little"))


def parse_ipv4_route_subnets(route_text: str) -> list[ReachableSubnetOption]:
    options: list[ReachableSubnetOption] = []
    seen: set[str] = set()
    lines = [line.strip() for line in route_text.splitlines() if line.strip()]
    for line in lines[1:]:
        columns = line.split()
        if len(columns) < 8:
            continue

        interface, destination_hex, _gateway, _flags, _refcnt, _use, _metric, mask_hex = columns[:8]
        if interface == "lo" or destination_hex == "00000000" or mask_hex == "00000000":
            continue

        try:
            destination = _ipv4_from_linux_hex(destination_hex)
            netmask = _ipv4_from_linux_hex(mask_hex)
            network = IPv4Network(f"{destination}/{netmask}", strict=False)
        except ValueError:
            continue

        cidr = str(network)
        if cidr in seen:
            continue
        seen.add(cidr)
        options.append(
            ReachableSubnetOption(
                cidr=cidr,
                interface=interface,
                label=f"{cidr} ({interface})",
            )
        )

    return sorted(options, key=lambda option: (option.interface, option.cidr))


def list_reachable_subnets() -> list[ReachableSubnetOption]:
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as route_file:
            route_text = route_file.read()
    except OSError:
        return []
    return parse_ipv4_route_subnets(route_text)
