from __future__ import annotations

from sqlalchemy.orm import Session

from app.home_graph.service import sync_inventory_to_home_graph
from app.services.discovery import run_discovery


def inspect_home_network(session: Session) -> dict:
    discovery = run_discovery(session)
    run_payload = discovery.model_dump(mode="json")
    entity_refs = sync_inventory_to_home_graph(session)
    return {
        "run": run_payload,
        "entity_refs": entity_refs,
        "candidate_count": discovery.candidate_count,
        "integrated_devices": discovery.integrated_devices,
        "new_device_ids": discovery.new_device_ids,
        "source_results": run_payload.get("source_results", []),
        "scope": run_payload.get("scope", {}),
        "result": "candidates_found" if discovery.candidate_count else "no_candidates",
    }
