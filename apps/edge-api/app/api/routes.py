from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.domain.schemas import DiscoveryRunRead, OverviewResponse, ReachableSubnetRead, SiteRead, SiteUpdate
from app.services.dashboard import build_overview, update_site
from app.services.discovery import run_discovery
from app.services.network_scope import list_reachable_subnets

router = APIRouter()


@router.get("/overview", response_model=OverviewResponse)
def read_overview(session: Session = Depends(get_session)) -> OverviewResponse:
    return build_overview(session)


@router.get("/network/reachable-subnets", response_model=list[ReachableSubnetRead])
def read_reachable_subnets() -> list[ReachableSubnetRead]:
    return [
        ReachableSubnetRead(cidr=option.cidr, interface=option.interface, label=option.label)
        for option in list_reachable_subnets()
    ]


@router.post("/discovery/runs", response_model=DiscoveryRunRead)
def create_discovery_run(session: Session = Depends(get_session)) -> DiscoveryRunRead:
    return run_discovery(session)


@router.patch("/site", response_model=SiteRead)
def patch_site(payload: SiteUpdate, session: Session = Depends(get_session)) -> SiteRead:
    updates = payload.model_dump(exclude_none=True)
    return update_site(session, updates)
