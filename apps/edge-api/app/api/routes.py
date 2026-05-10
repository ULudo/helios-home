from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actions.schemas import ActionExecuteRequest, ActionExecutionRead, ConnectionOptionsRead, ConnectionStateRead
from app.actions.service import ActionContext, execute_action, get_connection_options, get_connection_state
from app.agent.schemas import (
    ActionProposalDecisionRead,
    AgentMessageCreate,
    AgentProviderConfigRead,
    AgentProviderConfigUpdate,
    AgentThreadRead,
    AgentTurnAcceptedRead,
    SiteSetupProfileRead,
    UserDecisionCreate,
)
from app.agent.service import (
    create_agent_message,
    get_agent_provider_config,
    get_agent_thread,
    get_setup_profile,
    respond_to_user_decision_request,
    stream_turn_events,
    update_agent_provider_config,
)
from app.core.config import get_settings
from app.db.models import Site
from app.db.session import get_session
from app.domain.schemas import DeviceRead, DiscoveryRunRead, OverviewResponse, ReachableSubnetRead, SiteRead, SiteUpdate
from app.hems.schemas import (
    EebusLoadPowerLimitCreate,
    EebusLoadPowerLimitDistributionRead,
    EebusShipServiceRead,
    HemsAssetRead,
    HemsPlanRead,
    HemsPolicyRead,
    HemsPolicyUpdate,
    HemsSummaryRead,
    HemsSystemBindingRead,
)
from app.hems.service import (
    get_hems_summary,
    get_latest_hems_plan,
    list_hems_assets,
    list_hems_system_bindings,
    patch_hems_policy,
    run_hems_replan,
)
from app.services.dashboard import build_overview, remove_device_from_inventory, update_site
from app.services.discovery import run_discovery
from app.services.eebus import distribute_load_power_limit, list_eebus_ship_services
from app.services.network_scope import list_reachable_subnets

router = APIRouter()


def _get_site(session: Session) -> Site:
    site = session.scalar(select(Site).limit(1))
    if site is None:
        raise RuntimeError("Site has not been seeded.")
    return site


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


@router.get("/devices/{device_id}/connection-options", response_model=ConnectionOptionsRead)
def read_device_connection_options(device_id: str, session: Session = Depends(get_session)) -> ConnectionOptionsRead:
    try:
        return get_connection_options(session, _get_site(session), device_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/devices/{device_id}", response_model=DeviceRead)
def remove_device(device_id: str, session: Session = Depends(get_session)) -> DeviceRead:
    removed = remove_device_from_inventory(session, device_id, actor="user")
    if removed is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    return removed


@router.get("/connections/state", response_model=ConnectionStateRead)
def read_connection_state(
    entity_ref: str,
    endpoint_ref: str = "",
    integration_path: str = "",
    session: Session = Depends(get_session),
) -> ConnectionStateRead:
    try:
        return get_connection_state(
            session,
            _get_site(session),
            entity_ref=entity_ref,
            endpoint_ref=endpoint_ref,
            integration_path=integration_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/actions/{action_name}", response_model=ActionExecutionRead)
def execute_dashboard_action(
    action_name: str,
    payload: ActionExecuteRequest,
    session: Session = Depends(get_session),
) -> ActionExecutionRead:
    try:
        return execute_action(
            ActionContext(session=session, site=_get_site(session), actor="user"),
            action_name,
            payload.input,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@router.post("/agent/decision-requests/{decision_request_id}/responses", response_model=ActionProposalDecisionRead)
def respond_to_agent_decision_request(
    decision_request_id: str,
    payload: UserDecisionCreate,
    session: Session = Depends(get_session),
) -> ActionProposalDecisionRead:
    try:
        return respond_to_user_decision_request(session, decision_request_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Decision request not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/hems/summary", response_model=HemsSummaryRead)
def read_hems_summary(session: Session = Depends(get_session)) -> HemsSummaryRead:
    return get_hems_summary(session)


@router.get("/hems/assets", response_model=list[HemsAssetRead])
def read_hems_assets(session: Session = Depends(get_session)) -> list[HemsAssetRead]:
    return list_hems_assets(session)


@router.get("/hems/bindings", response_model=list[HemsSystemBindingRead])
def read_hems_bindings(session: Session = Depends(get_session)) -> list[HemsSystemBindingRead]:
    return list_hems_system_bindings(session)


@router.get("/hems/plans/latest", response_model=HemsPlanRead | None)
def read_latest_hems_plan(session: Session = Depends(get_session)) -> HemsPlanRead | None:
    return get_latest_hems_plan(session)


@router.patch("/hems/policy", response_model=HemsPolicyRead)
def update_hems_policy_route(payload: HemsPolicyUpdate, session: Session = Depends(get_session)) -> HemsPolicyRead:
    return patch_hems_policy(session, payload)


@router.post("/hems/replan", response_model=HemsPlanRead)
def create_hems_replan(session: Session = Depends(get_session)) -> HemsPlanRead:
    return run_hems_replan(session)


@router.get("/eebus/ship-services", response_model=list[EebusShipServiceRead])
def read_eebus_ship_services() -> list[EebusShipServiceRead]:
    settings = get_settings()
    try:
        services = list_eebus_ship_services(
            interface_ip=settings.eebus_interface_ip or None,
            timeout_seconds=settings.eebus_timeout_seconds,
            tls_check=settings.eebus_tls_check_enabled,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return [EebusShipServiceRead(**service) for service in services]


@router.post("/eebus/load-power-limits/distribute", response_model=EebusLoadPowerLimitDistributionRead)
def distribute_eebus_load_power_limit_route(
    payload: EebusLoadPowerLimitCreate,
    session: Session = Depends(get_session),
) -> EebusLoadPowerLimitDistributionRead:
    try:
        return distribute_load_power_limit(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
