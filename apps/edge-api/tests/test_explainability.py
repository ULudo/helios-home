from app.core.config import get_settings
from app.db.seed import seed_default_site
from app.db.session import get_engine, get_session_factory, init_database
from app.domain.schemas import DebugExplainRequest
from app.services.discovery import run_discovery
from app.services.explainability import explain_manual_claim, get_candidate_debug_report, get_device_debug_report
from discovery_catalog import install_catalog_discovery


def _build_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    init_database()
    session_factory = get_session_factory()
    session = session_factory()
    seed_default_site(session)
    return session


def test_device_debug_report_marks_integrated_native_device(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_catalog_discovery(session, monkeypatch)
        run_discovery(session)
        report = get_device_debug_report(session, "dev-fronius-gen24")
        assert report is not None
        assert report.subject_type == "device"
        assert report.diagnosis.state == "integrated"
        assert report.diagnosis.reason_code == "validated_interface"
        assert report.diagnosis.feasibility == "network_native"
    finally:
        session.close()


def test_device_debug_report_marks_auth_blocked_device(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_catalog_discovery(session, monkeypatch)
        run_discovery(session)
        report = get_device_debug_report(session, "dev-easee-wallbox")
        assert report is not None
        assert report.diagnosis.state == "classified_but_not_integrable"
        assert report.diagnosis.reason_family == "auth"
        assert report.diagnosis.reason_code == "auth_required"
        assert report.diagnosis.feasibility == "network_native_but_auth_blocked"
        assert any("pairing" in action.lower() or "oauth" in action.lower() for action in report.diagnosis.next_actions)
    finally:
        session.close()


def test_candidate_debug_report_keeps_candidate_context(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_catalog_discovery(session, monkeypatch)
        run_discovery(session)
        report = get_candidate_debug_report(session, "cand-vaillant-heatpump")
        assert report is not None
        assert report.subject_type == "device_candidate"
        assert report.matched_device_id == "dev-vaillant-heatpump"
        assert report.diagnosis.reason_code == "protocol_incomplete"
        assert any(item.kind == "candidate" for item in report.diagnosis.evidence)
    finally:
        session.close()


def test_manual_claim_matches_existing_device_when_model_aligns(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_catalog_discovery(session, monkeypatch)
        run_discovery(session)
        report = explain_manual_claim(
            session,
            DebugExplainRequest(
                manufacturer="Fronius",
                model="GEN24 Plus",
                device_type="pv_inverter",
                notes="roof inverter",
            ),
        )
        assert report.subject_type == "manual_claim"
        assert report.matched_device_id == "dev-fronius-gen24"
        assert report.diagnosis.state == "integrated"
        assert report.diagnosis.raw_diagnostics["claim_match_score"] >= 0.55
    finally:
        session.close()


def test_manual_claim_for_legacy_heat_pump_returns_retrofit_path(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        install_catalog_discovery(session, monkeypatch)
        run_discovery(session)
        report = explain_manual_claim(
            session,
            DebugExplainRequest(
                manufacturer="Manufacturer X",
                model="XY",
                device_type="heat_pump",
                notes="30 years old, SG Ready terminals only, no LAN module installed",
            ),
        )
        assert report.matched_device_id is None
        assert report.diagnosis.state == "not_found"
        assert report.diagnosis.reason_code == "no_supported_interface"
        assert report.diagnosis.feasibility == "dry_contact_possible"
        assert {option.kind for option in report.diagnosis.retrofit_options} >= {
            "dry_contact",
            "vendor_gateway",
            "meter_only",
        }
    finally:
        session.close()
