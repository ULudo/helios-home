from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agent.schemas import (
    ActionProposalDecisionRead,
    AgentMessageCreate,
    AgentProviderConfigRead,
    AgentProviderConfigUpdate,
    AgentThreadRead,
    AgentTurnAcceptedRead,
    SiteSetupProfileRead,
)
from app.agent.service import (
    confirm_action_proposal,
    create_agent_message,
    get_agent_provider_config,
    get_agent_thread,
    get_setup_profile,
    reject_action_proposal,
    stream_turn_events,
    update_agent_provider_config,
)
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


@router.get("/agent/thread", response_model=AgentThreadRead)
def read_agent_thread(session: Session = Depends(get_session)) -> AgentThreadRead:
    return get_agent_thread(session)


@router.get("/agent/provider-config", response_model=AgentProviderConfigRead)
def read_agent_provider_config() -> AgentProviderConfigRead:
    return get_agent_provider_config()


@router.patch("/agent/provider-config", response_model=AgentProviderConfigRead)
def patch_agent_provider_config(payload: AgentProviderConfigUpdate) -> AgentProviderConfigRead:
    return update_agent_provider_config(payload)


@router.get("/agent/setup-profile", response_model=SiteSetupProfileRead)
def read_setup_profile(session: Session = Depends(get_session)) -> SiteSetupProfileRead:
    return get_setup_profile(session)


@router.post("/agent/messages", response_model=AgentTurnAcceptedRead)
def create_agent_message_route(payload: AgentMessageCreate, session: Session = Depends(get_session)) -> AgentTurnAcceptedRead:
    return create_agent_message(session, payload)


@router.get("/agent/turns/{turn_id}/events")
def read_agent_turn_events(turn_id: str):
    return StreamingResponse(
        stream_turn_events(turn_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/agent/proposals/{proposal_id}/confirm", response_model=ActionProposalDecisionRead)
def confirm_agent_proposal(proposal_id: str, session: Session = Depends(get_session)) -> ActionProposalDecisionRead:
    try:
        return confirm_action_proposal(session, proposal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Proposal not found.") from exc


@router.post("/agent/proposals/{proposal_id}/reject", response_model=ActionProposalDecisionRead)
def reject_agent_proposal(proposal_id: str, session: Session = Depends(get_session)) -> ActionProposalDecisionRead:
    try:
        return reject_action_proposal(session, proposal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Proposal not found.") from exc


@router.get("/hems/summary", response_model=HemsSummaryRead)
def read_hems_summary(session: Session = Depends(get_session)) -> HemsSummaryRead:
    return get_hems_summary(session)


@router.get("/hems/assets", response_model=list[HemsAssetRead])
def read_hems_assets(session: Session = Depends(get_session)) -> list[HemsAssetRead]:
    return list_hems_assets(session)


@router.get("/hems/plans/latest", response_model=HemsPlanRead | None)
def read_latest_hems_plan(session: Session = Depends(get_session)) -> HemsPlanRead | None:
    return get_latest_hems_plan(session)


@router.patch("/hems/policy", response_model=HemsPolicyRead)
def update_hems_policy_route(payload: HemsPolicyUpdate, session: Session = Depends(get_session)) -> HemsPolicyRead:
    return patch_hems_policy(session, payload)


@router.post("/hems/replan", response_model=HemsPlanRead)
def create_hems_replan(session: Session = Depends(get_session)) -> HemsPlanRead:
    return run_hems_replan(session)
