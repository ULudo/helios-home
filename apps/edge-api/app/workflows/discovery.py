from __future__ import annotations

from sqlalchemy.orm import Session

from app.home_graph.service import sync_inventory_to_home_graph
from app.services.discovery import run_discovery


def inspect_home_network(session: Session) -> dict:
    discovery = run_discovery(session)
    entity_refs = sync_inventory_to_home_graph(session)
    return {
        "run": discovery.model_dump(mode="json"),
        "entity_refs": entity_refs,
        "candidate_count": discovery.candidate_count,
        "integrated_devices": discovery.integrated_devices,
        "new_device_ids": discovery.new_device_ids,
    }

