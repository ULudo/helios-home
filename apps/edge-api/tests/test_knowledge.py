from app.core.config import get_settings
from app.db.seed import seed_demo_data
from app.db.session import get_engine, get_session_factory, init_database
from app.domain.schemas import DebugExplainRequest, KnowledgePackWrite, ResearchFindingCreate
from app.services.discovery import run_discovery
from app.services.explainability import explain_manual_claim
from app.services.knowledge import (
    add_research_finding,
    create_debug_case,
    export_knowledge_pack,
    import_knowledge_pack,
    list_debug_cases,
    list_knowledge_entries,
    promote_debug_case_to_knowledge,
)


def _build_session(tmp_path, monkeypatch, name="test.db"):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / name}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    init_database()
    session_factory = get_session_factory()
    session = session_factory()
    seed_demo_data(session)
    return session


def _legacy_heat_pump_claim() -> DebugExplainRequest:
    return DebugExplainRequest(
        manufacturer="Manufacturer X",
        model="XY",
        device_type="heat_pump",
        notes="30 years old, SG Ready terminals only, no LAN module installed",
    )


def test_debug_case_can_be_promoted_into_local_knowledge(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        run_discovery(session)
        debug_case = create_debug_case(session, _legacy_heat_pump_claim())
        assert debug_case.status == "open"
        assert debug_case.diagnosis.reason_code == "no_supported_interface"

        finding = add_research_finding(
            session,
            debug_case.id,
            ResearchFindingCreate(
                source_type="vendor_doc",
                title="SG Ready retrofit path",
                summary="The controller only exposes SG Ready contacts and needs a relay for basic demand-response integration.",
                url="https://example.invalid/sg-ready-note",
            ),
        )
        assert finding.source_type == "vendor_doc"

        entry = promote_debug_case_to_knowledge(session, debug_case.id)
        assert entry.source_case_id == debug_case.id
        assert entry.reason_code == "no_supported_interface"

        stored_cases = list_debug_cases(session)
        assert stored_cases[0].status == "knowledge_captured"
        stored_entries = list_knowledge_entries(session)
        assert len(stored_entries) == 1
        assert stored_entries[0].fingerprint_key == entry.fingerprint_key

        report = explain_manual_claim(session, _legacy_heat_pump_claim())
        assert report.diagnosis.raw_diagnostics["knowledge_entry_id"] == entry.id
        assert report.diagnosis.reason_code == "no_supported_interface"
        assert any(option.kind == "dry_contact" for option in report.diagnosis.retrofit_options)
    finally:
        session.close()


def test_knowledge_pack_can_be_exported_and_imported(tmp_path, monkeypatch):
    source_session = _build_session(tmp_path, monkeypatch, name="source.db")
    try:
        run_discovery(source_session)
        debug_case = create_debug_case(source_session, _legacy_heat_pump_claim())
        promote_debug_case_to_knowledge(source_session, debug_case.id)
        exported_pack = export_knowledge_pack(source_session)
        assert len(exported_pack.entries) == 1
    finally:
        source_session.close()

    target_session = _build_session(tmp_path, monkeypatch, name="target.db")
    try:
        result = import_knowledge_pack(
            target_session,
            KnowledgePackWrite.model_validate(exported_pack.model_dump()),
        )
        assert result.imported_count == 1
        assert result.updated_count == 0
        assert result.total_entries == 1

        report = explain_manual_claim(target_session, _legacy_heat_pump_claim())
        assert report.matched_device_id is None
        assert report.diagnosis.raw_diagnostics["knowledge_entry_id"] >= 1
        assert report.diagnosis.feasibility == "dry_contact_possible"
    finally:
        target_session.close()
