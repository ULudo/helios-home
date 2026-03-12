import struct

from app.services.modbus import (
    INVERTER_POINTS,
    METER_SCALED_POINTS,
    STORAGE_CAPACITY_POINTS,
    ModbusProbeResult,
    build_candidate_from_modbus_probe,
    discover_modbus_site,
    parse_sunspec_model_blocks,
    parse_sunspec_model_ids,
    _extract_inverter_telemetry,
    _extract_meter_scaled_telemetry,
    _extract_storage_capacity_telemetry,
    _parse_sunspec_register_block,
)


def _encode_signed_16(value: int) -> int:
    return struct.unpack("!H", struct.pack("!h", value))[0]


def _encode_signed_32(value: int) -> list[int]:
    packed = struct.pack("!i", value)
    return list(struct.unpack("!HH", packed))


def _encode_unsigned_32(value: int) -> list[int]:
    packed = struct.pack("!I", value)
    return list(struct.unpack("!HH", packed))


def _encode_unsigned_64(value: int) -> list[int]:
    packed = struct.pack("!Q", value)
    return list(struct.unpack("!HHHH", packed))


def _encode_float_32(value: float) -> list[int]:
    packed = struct.pack("!f", value)
    return list(struct.unpack("!HH", packed))


def _encode_sunssf(value: int) -> int:
    return _encode_signed_16(value)


def _build_register_block(point_definitions, values: dict[str, int | float]) -> list[int]:
    registers: list[int] = []
    for point in point_definitions:
        value = values.get(point.name)
        if point.point_type in {"uint16", "enum16", "bitfield16"}:
            registers.append(0 if value is None else int(value))
        elif point.point_type in {"int16", "sunssf"}:
            registers.append(_encode_signed_16(0 if value is None else int(value)))
        elif point.point_type in {"uint32", "acc32", "bitfield32"}:
            registers.extend(_encode_unsigned_32(0 if value is None else int(value)))
        elif point.point_type == "int32":
            registers.extend(_encode_signed_32(0 if value is None else int(value)))
        elif point.point_type == "uint64":
            registers.extend(_encode_unsigned_64(0 if value is None else int(value)))
        elif point.point_type == "float32":
            registers.extend(_encode_float_32(0.0 if value is None else float(value)))
        else:
            raise AssertionError(f"Unsupported test point type: {point.point_type}")
    return registers


def test_parse_sunspec_model_ids_reads_directory():
    model_ids = parse_sunspec_model_ids(
        [
            1,
            66,
            *([0] * 66),
            103,
            50,
            *([0] * 50),
            0xFFFF,
            0,
        ]
    )

    assert model_ids == [1, 103]


def test_parse_sunspec_model_blocks_preserves_start_registers():
    model_blocks = parse_sunspec_model_blocks(
        [
            103,
            50,
            *([0] * 50),
            203,
            105,
            *([0] * 105),
            0xFFFF,
            0,
        ],
        start_register=40002,
    )

    assert model_blocks[0].model_id == 103
    assert model_blocks[0].start_register == 40002
    assert model_blocks[1].model_id == 203
    assert model_blocks[1].start_register == 40054


def test_extract_inverter_telemetry_from_sunspec_block():
    registers = _build_register_block(
        INVERTER_POINTS,
        {
            "ID": 103,
            "L": 50,
            "A": 24,
            "AphA": 8,
            "AphB": 8,
            "AphC": 8,
            "A_SF": 0,
            "PhVphA": 230,
            "PhVphB": 231,
            "PhVphC": 229,
            "V_SF": 0,
            "W": 5432,
            "W_SF": 0,
            "Hz": 5000,
            "Hz_SF": -2,
            "WH": 123456,
            "WH_SF": 0,
            "DCA": 9,
            "DCA_SF": 0,
            "DCV": 620,
            "DCV_SF": 0,
            "DCW": 5600,
            "DCW_SF": 0,
            "TmpCab": 34,
            "TmpSnk": 35,
            "TmpTrns": 36,
            "TmpOt": 33,
            "Tmp_SF": 0,
            "St": 4,
        },
    )

    values = _parse_sunspec_register_block(registers, INVERTER_POINTS)
    telemetry = _extract_inverter_telemetry(values)

    assert telemetry["power_kw"] == 5.432
    assert telemetry["energy_total_kwh"] == 123.456
    assert telemetry["voltage_v"] == 230.0
    assert telemetry["line_frequency_hz"] == 50.0
    assert telemetry["dc_power_kw"] == 5.6


def test_extract_meter_telemetry_from_scaled_sunspec_block():
    registers = _build_register_block(
        METER_SCALED_POINTS,
        {
            "ID": 203,
            "L": 105,
            "A": 11,
            "AphA": 4,
            "AphB": 3,
            "AphC": 4,
            "A_SF": 0,
            "PhV": 230,
            "PhVphA": 231,
            "PhVphB": 229,
            "PhVphC": 230,
            "PPV": 400,
            "V_SF": 0,
            "Hz": 5000,
            "Hz_SF": -2,
            "W": -2400,
            "WphA": -800,
            "WphB": -790,
            "WphC": -810,
            "W_SF": 0,
            "TotWhExp": 98234,
            "TotWhImp": 123456,
            "TotWh_SF": 0,
        },
    )

    values = _parse_sunspec_register_block(registers, METER_SCALED_POINTS)
    telemetry = _extract_meter_scaled_telemetry(values)

    assert telemetry["grid_power_kw"] == -2.4
    assert telemetry["grid_import_total_kwh"] == 123.456
    assert telemetry["grid_export_total_kwh"] == 98.234
    assert telemetry["phase_0_power_w"] == -800.0
    assert telemetry["phase_2_voltage_v"] == 230.0


def test_extract_storage_capacity_telemetry_from_sunspec_block():
    registers = _build_register_block(
        STORAGE_CAPACITY_POINTS,
        {
            "ID": 713,
            "L": 7,
            "WHRtg": 15000,
            "WHAvail": 8400,
            "SoC": 56,
            "SoH": 97,
            "Sta": 1,
            "WH_SF": 0,
            "Pct_SF": 0,
        },
    )

    values = _parse_sunspec_register_block(registers, STORAGE_CAPACITY_POINTS)
    telemetry = _extract_storage_capacity_telemetry(values)

    assert telemetry["energy_rating_kwh"] == 15.0
    assert telemetry["available_capacity_kwh"] == 8.4
    assert telemetry["soc_pct"] == 56.0
    assert telemetry["state_of_health_pct"] == 97.0


def test_build_candidate_from_sunspec_inverter_probe_promotes_monitoring():
    candidate = build_candidate_from_modbus_probe(
        ModbusProbeResult(
            host="198.51.100.90",
            unit_id=1,
            vendor_name="Fronius",
            product_code="GEN24 Plus",
            revision="1.28.4",
            sunspec_base_register=40000,
            sunspec_model_ids=[101, 103, 702, 704],
            telemetry={
                "power_kw": 5.432,
                "energy_total_kwh": 123.456,
                "voltage_v": 230.0,
                "power_rating_kw": 10.0,
            },
        )
    )

    assert candidate.manufacturer == "Fronius"
    assert candidate.device_type == "pv_inverter"
    assert candidate.protocols == ["modbus_tcp"]
    assert candidate.capabilities_hint["monitorable"] is True
    assert candidate.capabilities_hint["controllable"] is True
    assert candidate.telemetry["power_kw"] == 5.432
    assert candidate.evidence["dispatch_profile"] == "sunspec_der_wmax_pct"
    assert candidate.evidence["validated_metrics"] == ["energy_total_kwh", "power_kw", "power_rating_kw", "voltage_v"]


def test_build_candidate_from_generic_der_probe_stays_unclassified_without_storage_signals():
    candidate = build_candidate_from_modbus_probe(
        ModbusProbeResult(
            host="198.51.100.91",
            unit_id=247,
            vendor_name="Unknown",
            product_code="DER Controller",
            revision="1.0.0",
            sunspec_base_register=39999,
            sunspec_model_ids=[701],
            telemetry={"power_kw": 2.1, "line_frequency_hz": 50.0},
        )
    )

    assert candidate.device_type == "unclassified_energy_device"
    assert candidate.capabilities_hint["monitorable"] is True


def test_build_candidate_from_storage_probe_marks_sunspec_storage_dispatch_profile():
    candidate = build_candidate_from_modbus_probe(
        ModbusProbeResult(
            host="198.51.100.92",
            unit_id=1,
            vendor_name="BYD",
            product_code="Battery-Box",
            revision="1.0.0",
            sunspec_base_register=40000,
            sunspec_model_ids=[124, 713],
            telemetry={
                "soc_pct": 56.0,
                "available_capacity_kwh": 8.4,
                "max_charge_power_kw": 4.6,
            },
        )
    )

    assert candidate.device_type == "battery"
    assert candidate.capabilities_hint["controllable"] is True
    assert candidate.evidence["dispatch_profile"] == "sunspec_storage_basic_rate"


def test_discover_modbus_site_reports_monitorable_candidates(monkeypatch):
    async def fake_scan(*args, **kwargs):
        return ["198.51.100.90"]

    def fake_probe(host: str, timeout_seconds: float):
        return ModbusProbeResult(
            host=host,
            unit_id=1,
            vendor_name="Fronius",
            product_code="GEN24 Plus",
            revision="1.28.4",
            sunspec_base_register=40000,
            sunspec_model_ids=[101, 103],
            telemetry={"power_kw": 5.432},
        )

    monkeypatch.setattr("app.services.modbus._scan_modbus_hosts_async", fake_scan)
    monkeypatch.setattr("app.services.modbus.probe_modbus_host", fake_probe)

    batch = discover_modbus_site(subnet="198.51.100.0/24")

    assert batch.status == "completed"
    assert batch.candidates[0].capabilities_hint["monitorable"] is True
    assert "standardized SunSpec telemetry" in batch.message
