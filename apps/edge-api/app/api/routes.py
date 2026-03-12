from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.domain.schemas import DiscoveryRunRead, OverviewResponse, ReachableSubnetRead, SiteRead, SiteUpdate
from app.hems.schemas import HemsAssetRead, HemsPlanRead, HemsPolicyRead, HemsPolicyUpdate, HemsSummaryRead
from app.hems.service import get_hems_summary, get_latest_hems_plan, list_hems_assets, patch_hems_policy, run_hems_replan
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


@router.get("/hems/summary", response_model=HemsSummaryRead)
def read_hems_summary(session: Session = Depends(get_session)) -> HemsSummaryRead:
    return get_hems_summary(session)


@router.get("/hems/assets", response_model=list[HemsAssetRead])
def read_hems_assets(session: Session = Depends(get_session)) -> list[HemsAssetRead]:
    return list_hems_assets(session)


@router.get("/hems/plans/latest", response_model=HemsPlanRead)
def read_latest_hems_plan(session: Session = Depends(get_session)) -> HemsPlanRead:
    plan = get_latest_hems_plan(session)
    if plan is None:
        raise HTTPException(status_code=404, detail="No HEMS plan has been generated yet.")
    return plan


@router.patch("/hems/policy", response_model=HemsPolicyRead)
def update_hems_policy_route(payload: HemsPolicyUpdate, session: Session = Depends(get_session)) -> HemsPolicyRead:
    return patch_hems_policy(session, payload)


@router.post("/hems/replan", response_model=HemsPlanRead)
def create_hems_replan(session: Session = Depends(get_session)) -> HemsPlanRead:
    return run_hems_replan(session)
