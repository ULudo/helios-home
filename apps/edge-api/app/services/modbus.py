from __future__ import annotations

import asyncio
import math
import socket
import struct
from dataclasses import dataclass
from typing import Any

from app.domain.enums import RecoveryZone
from app.services.discovery_blueprints import RawCandidate
from app.services.local_network import DEVICE_RULES, MANUFACTURER_TOKENS


MODBUS_PORT = 502
MODBUS_UNIT_IDS = (1, 247, 0)
MODBUS_MAX_READ_COUNT = 120
SUNSPEC_BASE_ADDRESSES = (40000, 39999)
SUNSPEC_INVERTER_MODELS = {101, 102, 103}
SUNSPEC_METER_MODELS = {201, 202, 203, 204, 211, 212, 213}
SUNSPEC_STORAGE_MODELS = {124, 713}
SUNSPEC_GENERIC_DER_MODELS = {701}
SUNSPEC_TERMINATOR_IDS = {0x0000, 0xFFFF}


class ModbusSourceError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class SunSpecPoint:
    name: str
    point_type: str
    size: int = 1
    scale_factor: str | None = None


@dataclass(slots=True, frozen=True)
class SunSpecModelBlock:
    model_id: int
    length: int
    start_register: int


@dataclass(slots=True)
class ModbusProbeResult:
    host: str
    unit_id: int
    vendor_name: str
    product_code: str
    revision: str
    sunspec_base_register: int | None
    sunspec_model_ids: list[int]
    sunspec_model_blocks: list[SunSpecModelBlock] | None = None
    telemetry: dict[str, Any] | None = None


@dataclass(slots=True)
class ModbusDiscoveryBatch:
    source_name: str
    status: str
    message: str
    candidates: list[RawCandidate]


def _point(name: str, point_type: str, size: int = 1, scale_factor: str | None = None) -> SunSpecPoint:
    return SunSpecPoint(name=name, point_type=point_type, size=size, scale_factor=scale_factor)


INVERTER_POINTS = [
    _point("ID", "uint16"),
    _point("L", "uint16"),
    _point("A", "uint16", scale_factor="A_SF"),
    _point("AphA", "uint16", scale_factor="A_SF"),
    _point("AphB", "uint16", scale_factor="A_SF"),
    _point("AphC", "uint16", scale_factor="A_SF"),
    _point("A_SF", "sunssf"),
    _point("PPVphAB", "uint16", scale_factor="V_SF"),
    _point("PPVphBC", "uint16", scale_factor="V_SF"),
    _point("PPVphCA", "uint16", scale_factor="V_SF"),
    _point("PhVphA", "uint16", scale_factor="V_SF"),
    _point("PhVphB", "uint16", scale_factor="V_SF"),
    _point("PhVphC", "uint16", scale_factor="V_SF"),
    _point("V_SF", "sunssf"),
    _point("W", "int16", scale_factor="W_SF"),
    _point("W_SF", "sunssf"),
    _point("Hz", "uint16", scale_factor="Hz_SF"),
    _point("Hz_SF", "sunssf"),
    _point("VA", "int16", scale_factor="VA_SF"),
    _point("VA_SF", "sunssf"),
    _point("VAr", "int16", scale_factor="VAr_SF"),
    _point("VAr_SF", "sunssf"),
    _point("PF", "int16", scale_factor="PF_SF"),
    _point("PF_SF", "sunssf"),
    _point("WH", "acc32", size=2, scale_factor="WH_SF"),
    _point("WH_SF", "sunssf"),
    _point("DCA", "uint16", scale_factor="DCA_SF"),
    _point("DCA_SF", "sunssf"),
    _point("DCV", "uint16", scale_factor="DCV_SF"),
    _point("DCV_SF", "sunssf"),
    _point("DCW", "int16", scale_factor="DCW_SF"),
    _point("DCW_SF", "sunssf"),
    _point("TmpCab", "int16", scale_factor="Tmp_SF"),
    _point("TmpSnk", "int16", scale_factor="Tmp_SF"),
    _point("TmpTrns", "int16", scale_factor="Tmp_SF"),
    _point("TmpOt", "int16", scale_factor="Tmp_SF"),
    _point("Tmp_SF", "sunssf"),
    _point("St", "enum16"),
]

METER_SCALED_POINTS = [
    _point("ID", "uint16"),
    _point("L", "uint16"),
    _point("A", "int16", scale_factor="A_SF"),
    _point("AphA", "int16", scale_factor="A_SF"),
    _point("AphB", "int16", scale_factor="A_SF"),
    _point("AphC", "int16", scale_factor="A_SF"),
    _point("A_SF", "sunssf"),
    _point("PhV", "int16", scale_factor="V_SF"),
    _point("PhVphA", "int16", scale_factor="V_SF"),
    _point("PhVphB", "int16", scale_factor="V_SF"),
    _point("PhVphC", "int16", scale_factor="V_SF"),
    _point("PPV", "int16", scale_factor="V_SF"),
    _point("PhVphAB", "int16", scale_factor="V_SF"),
    _point("PhVphBC", "int16", scale_factor="V_SF"),
    _point("PhVphCA", "int16", scale_factor="V_SF"),
    _point("V_SF", "sunssf"),
    _point("Hz", "int16", scale_factor="Hz_SF"),
    _point("Hz_SF", "sunssf"),
    _point("W", "int16", scale_factor="W_SF"),
    _point("WphA", "int16", scale_factor="W_SF"),
    _point("WphB", "int16", scale_factor="W_SF"),
    _point("WphC", "int16", scale_factor="W_SF"),
    _point("W_SF", "sunssf"),
    _point("VA", "int16", scale_factor="VA_SF"),
    _point("VAphA", "int16", scale_factor="VA_SF"),
    _point("VAphB", "int16", scale_factor="VA_SF"),
    _point("VAphC", "int16", scale_factor="VA_SF"),
    _point("VA_SF", "sunssf"),
    _point("VAR", "int16", scale_factor="VAR_SF"),
    _point("VARphA", "int16", scale_factor="VAR_SF"),
    _point("VARphB", "int16", scale_factor="VAR_SF"),
    _point("VARphC", "int16", scale_factor="VAR_SF"),
    _point("VAR_SF", "sunssf"),
    _point("PF", "int16", scale_factor="PF_SF"),
    _point("PFphA", "int16", scale_factor="PF_SF"),
    _point("PFphB", "int16", scale_factor="PF_SF"),
    _point("PFphC", "int16", scale_factor="PF_SF"),
    _point("PF_SF", "sunssf"),
    _point("TotWhExp", "acc32", size=2, scale_factor="TotWh_SF"),
    _point("TotWhExpPhA", "acc32", size=2, scale_factor="TotWh_SF"),
    _point("TotWhExpPhB", "acc32", size=2, scale_factor="TotWh_SF"),
    _point("TotWhExpPhC", "acc32", size=2, scale_factor="TotWh_SF"),
    _point("TotWhImp", "acc32", size=2, scale_factor="TotWh_SF"),
    _point("TotWhImpPhA", "acc32", size=2, scale_factor="TotWh_SF"),
    _point("TotWhImpPhB", "acc32", size=2, scale_factor="TotWh_SF"),
    _point("TotWhImpPhC", "acc32", size=2, scale_factor="TotWh_SF"),
    _point("TotWh_SF", "sunssf"),
]

METER_FLOAT_POINTS = [
    _point("ID", "uint16"),
    _point("L", "uint16"),
    _point("A", "float32", size=2),
    _point("AphA", "float32", size=2),
    _point("AphB", "float32", size=2),
    _point("AphC", "float32", size=2),
    _point("PhV", "float32", size=2),
    _point("PhVphA", "float32", size=2),
    _point("PhVphB", "float32", size=2),
    _point("PhVphC", "float32", size=2),
    _point("PPV", "float32", size=2),
    _point("PPVphAB", "float32", size=2),
    _point("PPVphBC", "float32", size=2),
    _point("PPVphCA", "float32", size=2),
    _point("Hz", "float32", size=2),
    _point("W", "float32", size=2),
    _point("WphA", "float32", size=2),
    _point("WphB", "float32", size=2),
    _point("WphC", "float32", size=2),
    _point("VA", "float32", size=2),
    _point("VAphA", "float32", size=2),
    _point("VAphB", "float32", size=2),
    _point("VAphC", "float32", size=2),
    _point("VAR", "float32", size=2),
    _point("VARphA", "float32", size=2),
    _point("VARphB", "float32", size=2),
    _point("VARphC", "float32", size=2),
    _point("PF", "float32", size=2),
    _point("PFphA", "float32", size=2),
    _point("PFphB", "float32", size=2),
    _point("PFphC", "float32", size=2),
    _point("TotWhExp", "float32", size=2),
    _point("TotWhExpPhA", "float32", size=2),
    _point("TotWhExpPhB", "float32", size=2),
    _point("TotWhExpPhC", "float32", size=2),
    _point("TotWhImp", "float32", size=2),
    _point("TotWhImpPhA", "float32", size=2),
    _point("TotWhImpPhB", "float32", size=2),
    _point("TotWhImpPhC", "float32", size=2),
]

STORAGE_BASIC_POINTS = [
    _point("ID", "uint16"),
    _point("L", "uint16"),
    _point("WChaMax", "uint16", scale_factor="WChaMax_SF"),
    _point("WChaGra", "uint16", scale_factor="WChaDisChaGra_SF"),
    _point("WDisChaGra", "uint16", scale_factor="WChaDisChaGra_SF"),
    _point("StorCtl_Mod", "bitfield16"),
    _point("VAChaMax", "uint16", scale_factor="VAChaMax_SF"),
    _point("MinRsvPct", "uint16", scale_factor="MinRsvPct_SF"),
    _point("ChaState", "uint16", scale_factor="ChaState_SF"),
    _point("StorAval", "uint16", scale_factor="StorAval_SF"),
    _point("InBatV", "uint16", scale_factor="InBatV_SF"),
    _point("ChaSt", "enum16"),
    _point("OutWRte", "int16", scale_factor="InOutWRte_SF"),
    _point("InWRte", "int16", scale_factor="InOutWRte_SF"),
    _point("InOutWRte_WinTms", "uint16"),
    _point("InOutWRte_RvrtTms", "uint16"),
    _point("InOutWRte_RmpTms", "uint16"),
    _point("ChaGriSet", "enum16"),
    _point("WChaMax_SF", "sunssf"),
    _point("WChaDisChaGra_SF", "sunssf"),
    _point("VAChaMax_SF", "sunssf"),
    _point("MinRsvPct_SF", "sunssf"),
    _point("ChaState_SF", "sunssf"),
    _point("StorAval_SF", "sunssf"),
    _point("InBatV_SF", "sunssf"),
    _point("InOutWRte_SF", "sunssf"),
]

DER_MEASURE_POINTS = [
    _point("ID", "uint16"),
    _point("L", "uint16"),
    _point("ACType", "enum16"),
    _point("St", "enum16"),
    _point("InvSt", "enum16"),
    _point("ConnSt", "enum16"),
    _point("Alrm", "bitfield32", size=2),
    _point("DERMode", "bitfield32", size=2),
    _point("W", "int16", scale_factor="W_SF"),
    _point("VA", "int16", scale_factor="VA_SF"),
    _point("Var", "int16", scale_factor="Var_SF"),
    _point("PF", "int16", scale_factor="PF_SF"),
    _point("A", "int16", scale_factor="A_SF"),
    _point("LLV", "uint16", scale_factor="V_SF"),
    _point("LNV", "uint16", scale_factor="V_SF"),
    _point("Hz", "uint32", size=2, scale_factor="Hz_SF"),
    _point("TotWhInj", "uint64", size=4, scale_factor="TotWh_SF"),
    _point("TotWhAbs", "uint64", size=4, scale_factor="TotWh_SF"),
    _point("TotVarhInj", "uint64", size=4, scale_factor="TotVarh_SF"),
    _point("TotVarhAbs", "uint64", size=4, scale_factor="TotVarh_SF"),
    _point("TmpAmb", "int16", scale_factor="Tmp_SF"),
    _point("TmpCab", "int16", scale_factor="Tmp_SF"),
    _point("TmpSnk", "int16", scale_factor="Tmp_SF"),
    _point("TmpTrns", "int16", scale_factor="Tmp_SF"),
    _point("TmpSw", "int16", scale_factor="Tmp_SF"),
    _point("TmpOt", "int16", scale_factor="Tmp_SF"),
    _point("WL1", "int16", scale_factor="W_SF"),
    _point("VAL1", "int16", scale_factor="VA_SF"),
    _point("VarL1", "int16", scale_factor="Var_SF"),
    _point("PFL1", "int16", scale_factor="PF_SF"),
    _point("AL1", "int16", scale_factor="A_SF"),
    _point("VL1L2", "uint16", scale_factor="V_SF"),
    _point("VL1", "uint16", scale_factor="V_SF"),
    _point("TotWhInjL1", "uint64", size=4, scale_factor="TotWh_SF"),
    _point("TotWhAbsL1", "uint64", size=4, scale_factor="TotWh_SF"),
    _point("TotVarhInjL1", "uint64", size=4, scale_factor="TotVarh_SF"),
    _point("TotVarhAbsL1", "uint64", size=4, scale_factor="TotVarh_SF"),
    _point("WL2", "int16", scale_factor="W_SF"),
    _point("VAL2", "int16", scale_factor="VA_SF"),
    _point("VarL2", "int16", scale_factor="Var_SF"),
    _point("PFL2", "int16", scale_factor="PF_SF"),
    _point("AL2", "int16", scale_factor="A_SF"),
    _point("VL2L3", "uint16", scale_factor="V_SF"),
    _point("VL2", "uint16", scale_factor="V_SF"),
    _point("TotWhInjL2", "uint64", size=4, scale_factor="TotWh_SF"),
    _point("TotWhAbsL2", "uint64", size=4, scale_factor="TotWh_SF"),
    _point("TotVarhInjL2", "uint64", size=4, scale_factor="TotVarh_SF"),
    _point("TotVarhAbsL2", "uint64", size=4, scale_factor="TotVarh_SF"),
    _point("WL3", "int16", scale_factor="W_SF"),
    _point("VAL3", "int16", scale_factor="VA_SF"),
    _point("VarL3", "int16", scale_factor="Var_SF"),
    _point("PFL3", "int16", scale_factor="PF_SF"),
    _point("AL3", "int16", scale_factor="A_SF"),
    _point("VL3L1", "uint16", scale_factor="V_SF"),
    _point("VL3", "uint16", scale_factor="V_SF"),
    _point("TotWhInjL3", "uint64", size=4, scale_factor="TotWh_SF"),
    _point("TotWhAbsL3", "uint64", size=4, scale_factor="TotWh_SF"),
    _point("TotVarhInjL3", "uint64", size=4, scale_factor="TotVarh_SF"),
    _point("TotVarhAbsL3", "uint64", size=4, scale_factor="TotVarh_SF"),
    _point("ThrotPct", "uint16"),
    _point("ThrotSrc", "bitfield32", size=2),
    _point("A_SF", "sunssf"),
    _point("V_SF", "sunssf"),
    _point("Hz_SF", "sunssf"),
    _point("W_SF", "sunssf"),
    _point("PF_SF", "sunssf"),
    _point("VA_SF", "sunssf"),
    _point("Var_SF", "sunssf"),
    _point("TotWh_SF", "sunssf"),
    _point("TotVarh_SF", "sunssf"),
    _point("Tmp_SF", "sunssf"),
]

STORAGE_CAPACITY_POINTS = [
    _point("ID", "uint16"),
    _point("L", "uint16"),
    _point("WHRtg", "uint16", scale_factor="WH_SF"),
    _point("WHAvail", "uint16", scale_factor="WH_SF"),
    _point("SoC", "uint16", scale_factor="Pct_SF"),
    _point("SoH", "uint16", scale_factor="Pct_SF"),
    _point("Sta", "enum16"),
    _point("WH_SF", "sunssf"),
    _point("Pct_SF", "sunssf"),
]

MODEL_POINT_DEFINITIONS: dict[int, list[SunSpecPoint]] = {
    101: INVERTER_POINTS,
    102: INVERTER_POINTS,
    103: INVERTER_POINTS,
    124: STORAGE_BASIC_POINTS,
    201: METER_SCALED_POINTS,
    202: METER_SCALED_POINTS,
    203: METER_SCALED_POINTS,
    204: METER_SCALED_POINTS,
    211: METER_FLOAT_POINTS,
    212: METER_FLOAT_POINTS,
    213: METER_FLOAT_POINTS,
    701: DER_MEASURE_POINTS,
    713: STORAGE_CAPACITY_POINTS,
}


def _slugify(value: str) -> str:
    normalized = [character.lower() if character.isalnum() else "-" for character in value]
    slug = "".join(normalized).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "modbus-device"


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


def _normalize_metric(value: float | int | None) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    if not math.isfinite(normalized):
        return None
    return round(normalized, 4)


def _kwh_from_wh(value: float | int | None) -> float | None:
    normalized = _normalize_metric(value)
    if normalized is None:
        return None
    return round(normalized / 1000.0, 4)


def _average(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def _set_metric(telemetry: dict[str, Any], key: str, value: float | int | None) -> None:
    normalized = _normalize_metric(value)
    if normalized is not None:
        telemetry[key] = normalized


def _set_phase_metric(telemetry: dict[str, Any], prefix: str, values: list[float | None]) -> None:
    for index, value in enumerate(values):
        _set_metric(telemetry, f"phase_{index}_{prefix}", value)


async def _scan_modbus_hosts_async(subnet: str, timeout_seconds: float, concurrency: int, max_hosts: int) -> list[str]:
    try:
        import ipaddress

        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError as exc:
        raise ModbusSourceError("Local subnet is not a valid CIDR range for Modbus probing.") from exc

    hosts = [str(host) for host in network.hosts()]
    if len(hosts) > max_hosts:
        raise ModbusSourceError(
            f"Local subnet contains {len(hosts)} hosts, which exceeds the configured Modbus scan limit of {max_hosts}."
        )

    semaphore = asyncio.Semaphore(max(1, concurrency))
    open_hosts: list[str] = []

    async def probe_host(host: str) -> None:
        async with semaphore:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, MODBUS_PORT), timeout_seconds)
            except (asyncio.TimeoutError, OSError):
                return
            writer.close()
            await writer.wait_closed()
            open_hosts.append(host)

    await asyncio.gather(*(probe_host(host) for host in hosts))
    return sorted(open_hosts)


def _recv_exact(sock: socket.socket, byte_count: int) -> bytes:
    payload = bytearray()
    while len(payload) < byte_count:
        chunk = sock.recv(byte_count - len(payload))
        if not chunk:
            raise ModbusSourceError("Modbus connection closed before the response was complete.")
        payload.extend(chunk)
    return bytes(payload)


def _send_modbus_request(host: str, unit_id: int, pdu: bytes, timeout_seconds: float, transaction_id: int) -> bytes:
    with socket.create_connection((host, MODBUS_PORT), timeout=timeout_seconds) as sock:
        sock.settimeout(timeout_seconds)
        mbap = struct.pack("!HHHB", transaction_id, 0, len(pdu) + 1, unit_id)
        sock.sendall(mbap + pdu)
        response_header = _recv_exact(sock, 7)
        response_transaction_id, protocol_id, length, _ = struct.unpack("!HHHB", response_header)
        if response_transaction_id != transaction_id or protocol_id != 0:
            raise ModbusSourceError("Modbus response header did not match the request.")
        response_pdu = _recv_exact(sock, length - 1)
        if response_pdu and response_pdu[0] & 0x80:
            exception_code = response_pdu[1] if len(response_pdu) > 1 else "unknown"
            raise ModbusSourceError(f"Modbus device returned exception code {exception_code}.")
        return response_pdu


def read_device_identification(host: str, unit_id: int, timeout_seconds: float) -> dict[str, str] | None:
    try:
        response = _send_modbus_request(
            host,
            unit_id,
            bytes([0x2B, 0x0E, 0x01, 0x00]),
            timeout_seconds,
            transaction_id=1,
        )
    except (OSError, ModbusSourceError):
        return None

    if len(response) < 7 or response[0:2] != bytes([0x2B, 0x0E]):
        return None

    object_count = response[6]
    offset = 7
    values: dict[int, str] = {}
    for _ in range(object_count):
        if offset + 2 > len(response):
            break
        object_id = response[offset]
        length = response[offset + 1]
        offset += 2
        if offset + length > len(response):
            break
        values[object_id] = response[offset:offset + length].decode("utf-8", errors="ignore").strip()
        offset += length

    return {
        "vendor_name": values.get(0, ""),
        "product_code": values.get(1, ""),
        "revision": values.get(2, ""),
    }


def read_holding_registers(host: str, unit_id: int, address: int, count: int, timeout_seconds: float) -> list[int] | None:
    try:
        response = _send_modbus_request(
            host,
            unit_id,
            struct.pack("!BHH", 0x03, address, count),
            timeout_seconds,
            transaction_id=2 + (address % 1000),
        )
    except (OSError, ModbusSourceError):
        return None

    if len(response) < 2 or response[0] != 0x03:
        return None
    byte_count = response[1]
    register_payload = response[2:2 + byte_count]
    if len(register_payload) % 2 != 0:
        return None
    return list(struct.unpack(f"!{len(register_payload) // 2}H", register_payload))


def read_register_window(host: str, unit_id: int, address: int, count: int, timeout_seconds: float) -> list[int] | None:
    registers: list[int] = []
    remaining = count
    cursor = address
    while remaining > 0:
        chunk_size = min(remaining, MODBUS_MAX_READ_COUNT)
        chunk = read_holding_registers(host, unit_id, cursor, chunk_size, timeout_seconds)
        if chunk is None or len(chunk) != chunk_size:
            return None
        registers.extend(chunk)
        cursor += chunk_size
        remaining -= chunk_size
    return registers


def _decode_signed_16(register: int) -> int | None:
    if register == 0x8000:
        return None
    return struct.unpack("!h", struct.pack("!H", register))[0]


def _decode_unsigned_16(register: int) -> int | None:
    if register == 0xFFFF:
        return None
    return register


def _decode_signed_32(registers: list[int]) -> int | None:
    value = (registers[0] << 16) | registers[1]
    if value == 0x80000000:
        return None
    return struct.unpack("!i", struct.pack("!I", value))[0]


def _decode_unsigned_32(registers: list[int]) -> int | None:
    value = (registers[0] << 16) | registers[1]
    if value == 0xFFFFFFFF:
        return None
    return value


def _decode_unsigned_64(registers: list[int]) -> int | None:
    value = 0
    for register in registers:
        value = (value << 16) | register
    if value == 0xFFFFFFFFFFFFFFFF:
        return None
    return value


def _decode_float_32(registers: list[int]) -> float | None:
    raw = struct.pack("!HH", registers[0], registers[1])
    value = struct.unpack("!f", raw)[0]
    if not math.isfinite(value):
        return None
    return float(value)


def _decode_sunssf(register: int) -> int | None:
    return _decode_signed_16(register)


def _apply_scale_factor(value: float | int | None, scale_factor: int | None) -> float | None:
    if value is None:
        return None
    scaled = float(value)
    if scale_factor is not None:
        scaled *= 10 ** scale_factor
    if not math.isfinite(scaled):
        return None
    return scaled


def _decode_point_value(point: SunSpecPoint, registers: list[int]) -> Any:
    if point.point_type == "int16":
        return _decode_signed_16(registers[0])
    if point.point_type in {"uint16", "enum16", "bitfield16"}:
        return _decode_unsigned_16(registers[0])
    if point.point_type in {"bitfield32", "uint32", "acc32"}:
        return _decode_unsigned_32(registers)
    if point.point_type == "int32":
        return _decode_signed_32(registers)
    if point.point_type == "uint64":
        return _decode_unsigned_64(registers)
    if point.point_type == "float32":
        return _decode_float_32(registers)
    if point.point_type == "sunssf":
        return _decode_sunssf(registers[0])
    return None


def parse_sunspec_model_blocks(registers: list[int], start_register: int = 0) -> list[SunSpecModelBlock]:
    model_blocks: list[SunSpecModelBlock] = []
    offset = 0
    while offset + 1 < len(registers):
        model_id = registers[offset]
        length = registers[offset + 1]
        if model_id in SUNSPEC_TERMINATOR_IDS:
            break
        if length <= 0:
            break
        model_blocks.append(
            SunSpecModelBlock(
                model_id=model_id,
                length=length,
                start_register=start_register + offset,
            )
        )
        offset += 2 + length
    return model_blocks


def parse_sunspec_model_ids(registers: list[int]) -> list[int]:
    return [block.model_id for block in parse_sunspec_model_blocks(registers)]


def _parse_sunspec_register_block(registers: list[int], point_definitions: list[SunSpecPoint]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    offset = 0
    for point in point_definitions:
        end = offset + point.size
        if end > len(registers):
            break
        parsed[point.name] = _decode_point_value(point, registers[offset:end])
        offset = end
    return parsed


def _metric_with_sf(values: dict[str, Any], value_key: str, scale_factor_key: str | None) -> float | None:
    raw_value = values.get(value_key)
    if raw_value is None:
        return None
    if scale_factor_key is None:
        return _normalize_metric(raw_value)
    return _normalize_metric(_apply_scale_factor(raw_value, values.get(scale_factor_key)))


def _extract_inverter_telemetry(values: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    power_w = _metric_with_sf(values, "W", "W_SF")
    energy_wh = _metric_with_sf(values, "WH", "WH_SF")
    phase_voltages = [
        _metric_with_sf(values, "PhVphA", "V_SF"),
        _metric_with_sf(values, "PhVphB", "V_SF"),
        _metric_with_sf(values, "PhVphC", "V_SF"),
    ]
    _set_metric(telemetry, "power_kw", None if power_w is None else power_w / 1000.0)
    _set_metric(telemetry, "energy_total_kwh", _kwh_from_wh(energy_wh))
    _set_metric(telemetry, "voltage_v", _average(phase_voltages))
    _set_metric(telemetry, "current_a", _metric_with_sf(values, "A", "A_SF"))
    _set_metric(telemetry, "line_frequency_hz", _metric_with_sf(values, "Hz", "Hz_SF"))
    _set_metric(telemetry, "dc_voltage_v", _metric_with_sf(values, "DCV", "DCV_SF"))
    _set_metric(telemetry, "dc_current_a", _metric_with_sf(values, "DCA", "DCA_SF"))
    dc_power_w = _metric_with_sf(values, "DCW", "DCW_SF")
    _set_metric(telemetry, "dc_power_kw", None if dc_power_w is None else dc_power_w / 1000.0)
    temperature_candidates = [
        _metric_with_sf(values, "TmpCab", "Tmp_SF"),
        _metric_with_sf(values, "TmpSnk", "Tmp_SF"),
        _metric_with_sf(values, "TmpTrns", "Tmp_SF"),
        _metric_with_sf(values, "TmpOt", "Tmp_SF"),
    ]
    _set_metric(telemetry, "temperature_c", _average(temperature_candidates))
    _set_phase_metric(telemetry, "voltage_v", phase_voltages)
    return telemetry


def _extract_meter_scaled_telemetry(values: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    grid_power_w = _metric_with_sf(values, "W", "W_SF")
    _set_metric(telemetry, "grid_power_kw", None if grid_power_w is None else grid_power_w / 1000.0)
    _set_metric(telemetry, "voltage_v", _metric_with_sf(values, "PhV", "V_SF"))
    _set_metric(telemetry, "line_voltage_v", _metric_with_sf(values, "PPV", "V_SF"))
    _set_metric(telemetry, "current_a", _metric_with_sf(values, "A", "A_SF"))
    _set_metric(telemetry, "line_frequency_hz", _metric_with_sf(values, "Hz", "Hz_SF"))
    _set_metric(telemetry, "grid_import_total_kwh", _kwh_from_wh(_metric_with_sf(values, "TotWhImp", "TotWh_SF")))
    _set_metric(telemetry, "grid_export_total_kwh", _kwh_from_wh(_metric_with_sf(values, "TotWhExp", "TotWh_SF")))
    _set_phase_metric(
        telemetry,
        "power_w",
        [
            _metric_with_sf(values, "WphA", "W_SF"),
            _metric_with_sf(values, "WphB", "W_SF"),
            _metric_with_sf(values, "WphC", "W_SF"),
        ],
    )
    _set_phase_metric(
        telemetry,
        "voltage_v",
        [
            _metric_with_sf(values, "PhVphA", "V_SF"),
            _metric_with_sf(values, "PhVphB", "V_SF"),
            _metric_with_sf(values, "PhVphC", "V_SF"),
        ],
    )
    _set_phase_metric(
        telemetry,
        "current_a",
        [
            _metric_with_sf(values, "AphA", "A_SF"),
            _metric_with_sf(values, "AphB", "A_SF"),
            _metric_with_sf(values, "AphC", "A_SF"),
        ],
    )
    return telemetry


def _extract_meter_float_telemetry(values: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    grid_power_w = _metric_with_sf(values, "W", None)
    _set_metric(telemetry, "grid_power_kw", None if grid_power_w is None else grid_power_w / 1000.0)
    _set_metric(telemetry, "voltage_v", _metric_with_sf(values, "PhV", None))
    _set_metric(telemetry, "line_voltage_v", _metric_with_sf(values, "PPV", None))
    _set_metric(telemetry, "current_a", _metric_with_sf(values, "A", None))
    _set_metric(telemetry, "line_frequency_hz", _metric_with_sf(values, "Hz", None))
    _set_metric(telemetry, "grid_import_total_kwh", _kwh_from_wh(_metric_with_sf(values, "TotWhImp", None)))
    _set_metric(telemetry, "grid_export_total_kwh", _kwh_from_wh(_metric_with_sf(values, "TotWhExp", None)))
    _set_phase_metric(
        telemetry,
        "power_w",
        [
            _metric_with_sf(values, "WphA", None),
            _metric_with_sf(values, "WphB", None),
            _metric_with_sf(values, "WphC", None),
        ],
    )
    _set_phase_metric(
        telemetry,
        "voltage_v",
        [
            _metric_with_sf(values, "PhVphA", None),
            _metric_with_sf(values, "PhVphB", None),
            _metric_with_sf(values, "PhVphC", None),
        ],
    )
    _set_phase_metric(
        telemetry,
        "current_a",
        [
            _metric_with_sf(values, "AphA", None),
            _metric_with_sf(values, "AphB", None),
            _metric_with_sf(values, "AphC", None),
        ],
    )
    return telemetry


def _extract_storage_basic_telemetry(values: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    _set_metric(telemetry, "soc_pct", _metric_with_sf(values, "ChaState", "ChaState_SF"))
    _set_metric(telemetry, "available_charge_ah", _metric_with_sf(values, "StorAval", "StorAval_SF"))
    _set_metric(telemetry, "battery_voltage_v", _metric_with_sf(values, "InBatV", "InBatV_SF"))
    _set_metric(telemetry, "reserve_pct", _metric_with_sf(values, "MinRsvPct", "MinRsvPct_SF"))
    _set_metric(telemetry, "max_charge_power_kw", None if (value := _metric_with_sf(values, "WChaMax", "WChaMax_SF")) is None else value / 1000.0)
    charge_rate_pct = _metric_with_sf(values, "InWRte", "InOutWRte_SF")
    discharge_rate_pct = _metric_with_sf(values, "OutWRte", "InOutWRte_SF")
    _set_metric(telemetry, "charge_rate_pct", charge_rate_pct)
    _set_metric(telemetry, "discharge_rate_pct", discharge_rate_pct)
    if values.get("ChaSt") is not None:
        telemetry["charge_state_code"] = values["ChaSt"]
    return telemetry


def _extract_der_measure_telemetry(values: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    power_w = _metric_with_sf(values, "W", "W_SF")
    _set_metric(telemetry, "power_kw", None if power_w is None else power_w / 1000.0)
    _set_metric(telemetry, "current_a", _metric_with_sf(values, "A", "A_SF"))
    _set_metric(telemetry, "voltage_v", _metric_with_sf(values, "LNV", "V_SF"))
    _set_metric(telemetry, "line_voltage_v", _metric_with_sf(values, "LLV", "V_SF"))
    _set_metric(telemetry, "line_frequency_hz", _metric_with_sf(values, "Hz", "Hz_SF"))
    _set_metric(telemetry, "energy_export_total_kwh", _kwh_from_wh(_metric_with_sf(values, "TotWhInj", "TotWh_SF")))
    _set_metric(telemetry, "energy_import_total_kwh", _kwh_from_wh(_metric_with_sf(values, "TotWhAbs", "TotWh_SF")))
    _set_phase_metric(
        telemetry,
        "power_w",
        [
            _metric_with_sf(values, "WL1", "W_SF"),
            _metric_with_sf(values, "WL2", "W_SF"),
            _metric_with_sf(values, "WL3", "W_SF"),
        ],
    )
    _set_phase_metric(
        telemetry,
        "voltage_v",
        [
            _metric_with_sf(values, "VL1", "V_SF"),
            _metric_with_sf(values, "VL2", "V_SF"),
            _metric_with_sf(values, "VL3", "V_SF"),
        ],
    )
    temperature_candidates = [
        _metric_with_sf(values, "TmpAmb", "Tmp_SF"),
        _metric_with_sf(values, "TmpCab", "Tmp_SF"),
        _metric_with_sf(values, "TmpSnk", "Tmp_SF"),
        _metric_with_sf(values, "TmpTrns", "Tmp_SF"),
        _metric_with_sf(values, "TmpSw", "Tmp_SF"),
        _metric_with_sf(values, "TmpOt", "Tmp_SF"),
    ]
    _set_metric(telemetry, "temperature_c", _average(temperature_candidates))
    return telemetry


def _extract_storage_capacity_telemetry(values: dict[str, Any]) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    _set_metric(telemetry, "energy_rating_kwh", _kwh_from_wh(_metric_with_sf(values, "WHRtg", "WH_SF")))
    _set_metric(telemetry, "available_capacity_kwh", _kwh_from_wh(_metric_with_sf(values, "WHAvail", "WH_SF")))
    _set_metric(telemetry, "soc_pct", _metric_with_sf(values, "SoC", "Pct_SF"))
    _set_metric(telemetry, "state_of_health_pct", _metric_with_sf(values, "SoH", "Pct_SF"))
    if values.get("Sta") is not None:
        telemetry["storage_state_code"] = values["Sta"]
    return telemetry


MODEL_TELEMETRY_EXTRACTORS = {
    101: _extract_inverter_telemetry,
    102: _extract_inverter_telemetry,
    103: _extract_inverter_telemetry,
    124: _extract_storage_basic_telemetry,
    201: _extract_meter_scaled_telemetry,
    202: _extract_meter_scaled_telemetry,
    203: _extract_meter_scaled_telemetry,
    204: _extract_meter_scaled_telemetry,
    211: _extract_meter_float_telemetry,
    212: _extract_meter_float_telemetry,
    213: _extract_meter_float_telemetry,
    701: _extract_der_measure_telemetry,
    713: _extract_storage_capacity_telemetry,
}


def _read_sunspec_model_blocks(host: str, unit_id: int, base_register: int, timeout_seconds: float) -> list[SunSpecModelBlock]:
    blocks: list[SunSpecModelBlock] = []
    cursor = base_register + 2
    for _ in range(16):
        header = read_register_window(host, unit_id, cursor, 2, timeout_seconds)
        if not header or len(header) != 2:
            break
        model_id, length = header
        if model_id in SUNSPEC_TERMINATOR_IDS:
            break
        if length <= 0:
            break
        blocks.append(SunSpecModelBlock(model_id=model_id, length=length, start_register=cursor))
        cursor += 2 + length
    return blocks


def _detect_sunspec_base(host: str, unit_id: int, timeout_seconds: float) -> tuple[int | None, list[SunSpecModelBlock]]:
    for base_register in SUNSPEC_BASE_ADDRESSES:
        signature = read_register_window(host, unit_id, base_register, 2, timeout_seconds)
        if not signature or len(signature) != 2:
            continue
        signature_text = struct.pack("!HH", signature[0], signature[1]).decode("ascii", errors="ignore")
        if signature_text != "SunS":
            continue
        return base_register, _read_sunspec_model_blocks(host, unit_id, base_register, timeout_seconds)
    return None, []


def _extract_probe_telemetry(host: str, unit_id: int, model_blocks: list[SunSpecModelBlock], timeout_seconds: float) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    for model_block in model_blocks:
        point_definitions = MODEL_POINT_DEFINITIONS.get(model_block.model_id)
        extractor = MODEL_TELEMETRY_EXTRACTORS.get(model_block.model_id)
        if point_definitions is None or extractor is None:
            continue
        registers = read_register_window(host, unit_id, model_block.start_register, model_block.length + 2, timeout_seconds)
        if not registers or len(registers) < 2:
            continue
        parsed = _parse_sunspec_register_block(registers, point_definitions)
        telemetry.update(extractor(parsed))
    return telemetry


def probe_modbus_host(host: str, timeout_seconds: float) -> ModbusProbeResult | None:
    for unit_id in MODBUS_UNIT_IDS:
        identity = read_device_identification(host, unit_id, timeout_seconds) or {}
        sunspec_base_register, model_blocks = _detect_sunspec_base(host, unit_id, timeout_seconds)
        if not identity and sunspec_base_register is None:
            continue
        telemetry = (
            _extract_probe_telemetry(host, unit_id, model_blocks, timeout_seconds)
            if sunspec_base_register is not None and model_blocks
            else {}
        )
        return ModbusProbeResult(
            host=host,
            unit_id=unit_id,
            vendor_name=identity.get("vendor_name", ""),
            product_code=identity.get("product_code", ""),
            revision=identity.get("revision", ""),
            sunspec_base_register=sunspec_base_register,
            sunspec_model_ids=[model_block.model_id for model_block in model_blocks],
            sunspec_model_blocks=model_blocks,
            telemetry=telemetry,
        )
    return None


def _classify_probe_result(probe: ModbusProbeResult) -> tuple[str, float, str]:
    telemetry = probe.telemetry or {}
    if any(model_id in SUNSPEC_STORAGE_MODELS for model_id in probe.sunspec_model_ids) or "soc_pct" in telemetry:
        return ("battery", 0.9, "SunSpec storage telemetry exposed state-of-charge or storage capacity metrics.")
    if any(model_id in SUNSPEC_METER_MODELS for model_id in probe.sunspec_model_ids) or "grid_power_kw" in telemetry:
        return ("grid_meter", 0.9, "SunSpec meter telemetry exposed grid power and energy counters.")
    if any(model_id in SUNSPEC_INVERTER_MODELS for model_id in probe.sunspec_model_ids):
        return ("pv_inverter", 0.9, "SunSpec inverter telemetry exposed production-side AC and DC metrics.")

    haystack = _combined_text(
        probe.vendor_name,
        probe.product_code,
        str(probe.sunspec_model_ids),
        " ".join((probe.telemetry or {}).keys()),
    )
    for device_type, tokens, confidence in DEVICE_RULES:
        matched = [token for token in tokens if token in haystack]
        if matched:
            return (
                device_type,
                confidence,
                f"Modbus identification matched {', '.join(matched[:3])} for the {device_type} profile.",
            )

    if any(model_id in SUNSPEC_GENERIC_DER_MODELS for model_id in probe.sunspec_model_ids):
        return (
            "unclassified_energy_device",
            0.76 if telemetry else 0.72,
            "Generic SunSpec DER telemetry was found, but it did not expose a clear inverter, meter or storage profile.",
        )

    return (
        "unclassified_energy_device",
        0.74 if telemetry else 0.64,
        "Native Modbus/TCP responded, but no strong semantic device profile matched yet.",
    )


def _manufacturer_for_probe(probe: ModbusProbeResult) -> str:
    if probe.vendor_name:
        return probe.vendor_name
    haystack = _combined_text(probe.product_code, str(probe.sunspec_model_ids))
    for token, manufacturer in MANUFACTURER_TOKENS.items():
        if token in haystack:
            return manufacturer
    return "Modbus device"


def build_candidate_from_modbus_probe(probe: ModbusProbeResult) -> RawCandidate:
    device_type, confidence, reasoning = _classify_probe_result(probe)
    manufacturer = _manufacturer_for_probe(probe)
    model = probe.product_code or ("SunSpec device" if probe.sunspec_base_register is not None else "Modbus/TCP device")
    display_name = (
        f"{manufacturer} {model}".strip()
        if manufacturer and model
        else f"Modbus device {probe.host}"
    )
    telemetry = probe.telemetry or {}
    monitorable = bool(telemetry)
    evidence = {
        "identity_keys": [f"network-host:{_slugify(probe.host)}"],
        "modbus_host": probe.host,
        "modbus_port": MODBUS_PORT,
        "modbus_unit_id": probe.unit_id,
        "modbus_identity": {
            "vendor_name": probe.vendor_name,
            "product_code": probe.product_code,
            "revision": probe.revision,
        },
        "sunspec_base_register": probe.sunspec_base_register,
        "sunspec_model_ids": probe.sunspec_model_ids,
        "sunspec_model_blocks": [
            {
                "model_id": block.model_id,
                "length": block.length,
                "start_register": block.start_register,
            }
            for block in (probe.sunspec_model_blocks or [])
        ],
        "validated_metrics": sorted(telemetry.keys()),
        "classification_reasoning": reasoning,
        "classification_confidence": confidence,
    }
    explanation_hint = (
        "Helios validated a native Modbus/TCP endpoint and mapped standardized SunSpec telemetry."
        if monitorable
        else "Helios validated a native Modbus/TCP endpoint and, when available, a SunSpec directory, but no stable telemetry mapping succeeded yet."
    )
    next_step_hint = (
        "Keep the device monitorable through the native SunSpec read path and extend validated write profiles when safe."
        if monitorable
        else "Promote the validated Modbus endpoint into a device-specific telemetry profile."
    )
    return RawCandidate(
        candidate_id=f"cand-modbus-{_slugify(probe.host)}-u{probe.unit_id}",
        device_id=f"dev-modbus-{_slugify(probe.host)}-u{probe.unit_id}",
        asset_id=f"asset-modbus-{_slugify(probe.host)}-u{probe.unit_id}",
        asset_name=_asset_name_for_type(device_type),
        display_name=display_name,
        manufacturer=manufacturer,
        model=model,
        firmware=probe.revision or "unknown",
        device_type=device_type,
        discovery_sources=["modbus_live"],
        protocols=["modbus_tcp"],
        telemetry=telemetry,
        evidence=evidence,
        recovery_zone=RecoveryZone.AUTO_APPLY.value,
        issue_code=None,
        explanation_hint=explanation_hint,
        next_step_hint=next_step_hint,
        capabilities_hint={
            "visible": True,
            "monitorable": monitorable,
            "controllable": False,
            "optimizable": False,
        },
    )


def discover_modbus_site(
    *,
    subnet: str,
    timeout_seconds: float = 1.0,
    concurrency: int = 32,
    max_hosts: int = 256,
) -> ModbusDiscoveryBatch:
    try:
        open_hosts = asyncio.run(_scan_modbus_hosts_async(subnet, timeout_seconds, concurrency, max_hosts))
    except (OSError, RuntimeError, ModbusSourceError) as exc:
        return ModbusDiscoveryBatch(
            source_name="modbus_live",
            status="failed",
            message=f"Modbus discovery failed: {exc}",
            candidates=[],
        )

    candidates: list[RawCandidate] = []
    for host in open_hosts:
        probe = probe_modbus_host(host, timeout_seconds)
        if probe is not None:
            candidates.append(build_candidate_from_modbus_probe(probe))

    if candidates:
        monitorable_count = sum(1 for candidate in candidates if candidate.capabilities_hint.get("monitorable"))
        if monitorable_count:
            message = (
                f"Imported {len(candidates)} candidates from native Modbus/TCP probing; "
                f"{monitorable_count} exposed standardized SunSpec telemetry."
            )
        else:
            message = (
                f"Imported {len(candidates)} candidates from native Modbus/TCP probing, "
                "but none exposed a fully decoded telemetry profile yet."
            )
        return ModbusDiscoveryBatch(
            source_name="modbus_live",
            status="completed",
            message=message,
            candidates=sorted(candidates, key=lambda candidate: candidate.display_name.lower()),
        )

    return ModbusDiscoveryBatch(
        source_name="modbus_live",
        status="completed",
        message="Modbus discovery completed, but no native Modbus/TCP devices exposed a usable identity or SunSpec signature.",
        candidates=[],
    )
