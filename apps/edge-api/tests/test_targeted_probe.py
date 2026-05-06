from app.core.config import get_settings
from app.db.seed import seed_default_site
from app.db.session import get_engine, get_session_factory, init_database
from app.domain.schemas import DebugExplainRequest
from app.services.discovery import run_discovery
from app.services.discovery_blueprints import RawCandidate
from app.services.knowledge import create_debug_case, promote_debug_case_to_knowledge
from app.services.modbus import ModbusProbeResult, SunSpecModelBlock
from app.services.targeted_probe import run_targeted_probe
from discovery_catalog import install_empty_standard_discovery


def _build_session(tmp_path, monkeypatch, name="test.db"):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / name}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    init_database()
    session_factory = get_session_factory()
    session = session_factory()
    seed_default_site(session)
    return session


def test_targeted_probe_confirms_dry_contact_path_for_legacy_heat_pump(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_empty_standard_discovery(monkeypatch)
        run_discovery(session)
        debug_case = create_debug_case(
            session,
            DebugExplainRequest(
                manufacturer="Manufacturer X",
                model="XY",
                device_type="heat_pump",
                notes="30 years old, SG Ready terminals only, no LAN module installed",
            ),
        )

        updated_case = run_targeted_probe(session, debug_case.id)
        assert updated_case.status == "probed"
        assert updated_case.diagnosis.reason_code == "no_supported_interface"
        assert updated_case.diagnosis.feasibility == "dry_contact_possible"
        assert updated_case.probe_runs[0].summary.startswith("Targeted probing did not confirm native networking")
        assert any(check.name == "dry_contact_path" and check.outcome == "passed" for check in updated_case.probe_runs[0].checks)

        knowledge_entry = promote_debug_case_to_knowledge(session, debug_case.id)
        assert any(item.kind == "probe_check" for item in knowledge_entry.evidence)
    finally:
        session.close()


def test_targeted_probe_upgrades_reachable_host_to_network_native_but_unsupported(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_empty_standard_discovery(monkeypatch)
        run_discovery(session)
        debug_case = create_debug_case(
            session,
            DebugExplainRequest(
                manufacturer="Unknown Vendor",
                model="ABC-55",
                device_type="battery",
                notes="Reachable at 198.51.100.55, Modbus TCP expected on port 502",
            ),
        )
        monkeypatch.setattr(
            "app.services.targeted_probe._probe_host_ports",
            lambda host, ports, timeout_seconds=0.35: {
                "open_ports": [502],
                "closed_ports": [port for port in ports if port != 502],
                "errors": [],
            },
        )

        updated_case = run_targeted_probe(session, debug_case.id)
        assert updated_case.diagnosis.state == "classified_but_not_integrable"
        assert updated_case.diagnosis.reason_code == "telemetry_path_not_validated"
        assert updated_case.diagnosis.feasibility == "network_native_but_unsupported"
        assert any(check.name == "host_reachability" and check.outcome == "passed" for check in updated_case.probe_runs[0].checks)
        assert updated_case.diagnosis.raw_diagnostics["latest_probe_run_id"] == updated_case.probe_runs[0].id
    finally:
        session.close()


def test_targeted_probe_uses_http_fingerprint_to_confirm_network_native_path(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_empty_standard_discovery(monkeypatch)
        run_discovery(session)
        debug_case = create_debug_case(
            session,
            DebugExplainRequest(
                manufacturer="OpenDTU",
                model="OpenDTU-OnBattery",
                device_type="pv_inverter",
                notes="Reachable at http://198.51.100.84 and local web UI is online",
            ),
        )
        monkeypatch.setattr(
            "app.services.targeted_probe.fingerprint_http_host",
            lambda host, timeout_seconds=1.0: RawCandidate(
                candidate_id="cand-local-http-opendtu",
                device_id="dev-local-http-opendtu",
                asset_id="asset-local-http-opendtu",
                asset_name="PV Generation",
                display_name="OpenDTU-OnBattery",
                manufacturer="OpenDTU",
                model="OpenDTU-OnBattery",
                firmware="1.0.0",
                device_type="pv_inverter",
                discovery_sources=["local_network_live"],
                protocols=["http_local"],
                telemetry={"power_kw": 1.8, "daily_energy_kwh": 4.2},
                evidence={
                    "classification_confidence": 0.91,
                    "http_base_url": f"http://{host}",
                },
                recovery_zone="auto_apply",
                issue_code=None,
                capabilities_hint={
                    "visible": True,
                    "monitorable": True,
                    "controllable": False,
                    "optimizable": False,
                },
            ),
        )

        updated_case = run_targeted_probe(session, debug_case.id)
        assert updated_case.diagnosis.reason_code == "validated_interface"
        assert updated_case.diagnosis.feasibility == "network_native"
        assert any(check.name == "http_fingerprint" and check.outcome == "passed" for check in updated_case.probe_runs[0].checks)
    finally:
        session.close()


def test_targeted_probe_uses_modbus_probe_to_confirm_native_endpoint(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_empty_standard_discovery(monkeypatch)
        run_discovery(session)
        debug_case = create_debug_case(
            session,
            DebugExplainRequest(
                manufacturer="Vendor",
                model="Battery-X",
                device_type="battery",
                notes="Modbus TCP endpoint at 198.51.100.56:502",
            ),
        )
        monkeypatch.setattr(
            "app.services.targeted_probe.probe_modbus_host",
            lambda host, timeout_seconds=0.8: ModbusProbeResult(
                host=host,
                unit_id=1,
                vendor_name="Vendor",
                product_code="Battery-X",
                revision="1.2.3",
                sunspec_base_register=40000,
                sunspec_model_ids=[124, 713],
                sunspec_model_blocks=[
                    SunSpecModelBlock(model_id=124, length=26, start_register=40002),
                    SunSpecModelBlock(model_id=713, length=9, start_register=40030),
                ],
                telemetry={"soc_pct": 48, "available_capacity_kwh": 9.1},
            ),
        )

        updated_case = run_targeted_probe(session, debug_case.id)
        assert updated_case.diagnosis.reason_code == "validated_interface"
        assert updated_case.diagnosis.feasibility == "network_native"
        assert any(check.name == "modbus_protocol_probe" and check.outcome == "passed" for check in updated_case.probe_runs[0].checks)
    finally:
        session.close()
