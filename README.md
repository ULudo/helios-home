# Helios Home

Helios Home is a local-first HEMS prototype focused on one job first: discover network-reachable energy devices in a building, represent them cleanly, and surface their live telemetry in a compact local UI.

The current public milestone is intentionally narrow:

- local network selection
- discovery across configured subnets
- device inventory with inline details
- session-based monitoring views
- local-only runtime with no cloud dependency

The backend already contains deeper discovery, explainability and knowledge-building services, but the stable public UI and API are currently centered on discovery and device inspection.

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
- candidate reconciliation across native live sources
- device inventory materialization into the local SQLite store
- inline device details and session-based monitoring charts

Not in current public UI scope:

- HEMS control and optimization pages
- end-user debugging workflows
- cloud sync
- shared knowledge base
- production secret management

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
