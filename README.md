# Helios Home

Helios Home is a local-first Home Energy Management System built around an agent-first setup experience. The current frontend milestone is focused on helping a user discover network-reachable devices, confirm what they belong to in the home, and build the setup interactively through conversation.

The current public milestone is intentionally narrow:

- conversation-first setup workspace
- streamed activity during discovery and setup turns
- confirmation-gated setup actions
- advanced drawer for device inventory and HEMS inspection
- local-only runtime with no cloud dependency

The runtime now also contains a first HEMS core with:

- canonical asset mapping
- guarded planning eligibility
- day-ahead / rolling-horizon optimization
- guarded dispatch, including generic controllable loads
- audit persistence

The stable public UI is now centered on the Helios assistant workspace.

## Agent model provider

Helios now supports a local, provider-neutral model configuration for the assistant runtime.

Current built-in options:

- development stub
- OpenAI
- Anthropic
- OpenRouter
- Ollama
- custom OpenAI-compatible endpoint

Provider credentials are stored only on the local machine in the agent config path and are never returned by the API after saving. The repository does not contain any provider keys or checked-in model configuration.

## EEBus / SHIP support

Helios discovers EEBus SHIP peers as a standard local protocol and translates EEBus LoadControl limits into HEMS planning constraints.

Current EEBus scope:

- `_ship._tcp.local` discovery through the bundled `eebus-sdk` integration
- visible inventory records for discovered SHIP peers
- LPC, `limitationOfPowerConsumption`, mapped to `grid_import_limit_kw`
- LPP, `limitationOfPowerProduction`, mapped to `grid_export_limit_kw`
- HEMS replanning after active LPC/LPP limits are accepted

The integration uses the Python SDK from `https://github.com/ULudo/eebus-sdk`. EEBus SHIP discovery runs with normal local/live device discovery; `HELIOS_EEBUS_INTERFACE_IP` may be set only when the host has multiple interfaces and automatic interface selection is not sufficient. Trust commissioning, certificate identity management and production-grade EEBus daemon lifecycle remain explicit operational steps.

## Repository layout

```text
apps/
  edge-api/      FastAPI backend and tests
  web-ui/        React + TypeScript frontend
docs/
  architecture/  Current architecture notes
  api/           Stable API surface
infra/
  compose/       Docker Compose setup
  docker/        Container images
scripts/
  run/test/build helpers for local development
```

## Local development

### Backend

Install backend dependencies once:

```bash
./scripts/setup-backend.sh
```

```bash
export HELIOS_LOCAL_SCAN_ENABLED=true
export HELIOS_BROADCAST_DISCOVERY_ENABLED=true
export HELIOS_MODBUS_LIVE_ENABLED=true
./scripts/run-backend.sh
```

The backend listens on `http://127.0.0.1:8000`.

### Frontend

```bash
./scripts/run-frontend.sh
```

The frontend listens on `http://127.0.0.1:5173`.

### Tests

```bash
./scripts/test-backend.sh
./scripts/build-frontend.sh
```

## Current product scope

Implemented now:

- local subnet selection based on reachable host routes
- local HTTP discovery
- mDNS / SSDP discovery
- native Modbus / SunSpec probing
- EEBus / SHIP discovery, with LPC/LPP load-control distribution
- candidate reconciliation across native live sources
- device inventory materialization into the local SQLite store
- agent conversation thread with streaming activity and confirmation proposals
- setup profile persistence for confirmed systems and unresolved questions
- advanced drawer with device inventory and HEMS runtime inspection
- backend HEMS canonical asset model
- backend HEMS policy, planner, dispatch and audit records

Not in current public UI scope:

- end-user debugging workflows
- cloud sync
- shared knowledge base
- production secret management

## HEMS backend

The first HEMS backend milestone is now available through the API. It is intentionally backend-first and currently targets:

- `PV`
- `battery`
- `controllable load`
- `EV charger`
- `heat pump`

The execution model is hybrid:

- planning over a configurable horizon
- guarded dispatch of the current interval
- fallback to plan-only behavior for assets without validated write paths

Default solver stack:

- `CVXPY`
- `HiGHS`

Guarded native actuation is available behind an explicit opt-in:

- `HELIOS_NATIVE_WRITES_ENABLED=true`

Current write adapters:

- telemetry simulation
- Shelly local HTTP relay control
- Tasmota local HTTP power control

Relevant endpoints:

- `GET /api/v1/hems/summary`
- `GET /api/v1/hems/assets`
- `GET /api/v1/hems/plans/latest`
- `PATCH /api/v1/hems/policy`
- `POST /api/v1/hems/replan`

## Public-repo hygiene

This repository is prepared for local development first and public publication later:

- no committed local subnet defaults
- no committed `.env` or secret files
- no committed local databases, logs or build artifacts
- generated TypeScript and Python artifacts are ignored

If you want a fresh local state before testing:

```bash
rm -f helios_home.db helios_home.db-shm helios_home.db-wal
```
