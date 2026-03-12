# API Endpoints

Base URL: `/api/v1`

The current stable frontend-facing API is intentionally small and aligned with the Devices page.

The backend also exposes a first HEMS control API for local development and backend validation.

## Health

### `GET /health`

Returns a minimal liveness payload:

- service status
- runtime mode
- database readiness flag

## Overview

### `GET /overview`

Returns the full payload used by the Devices page:

- current site scope
- integrated devices

## Network scope

### `GET /network/reachable-subnets`

Returns the IPv4 subnets currently reachable from the local host. These values are used to populate the subnet selection in the frontend.

## Site configuration

### `PATCH /site`

Updates the discovery scope.

Request body:

```json
{
  "local_subnet": "198.51.100.0/24, 203.0.113.0/24"
}
```

## Discovery

### `POST /discovery/runs`

Runs a discovery cycle against the currently configured subnet scope and returns:

- execution timestamp
- source-level statuses
- new device ids
- candidate count
- integrated device count

## HEMS

### `GET /hems/summary`

Returns:

- current HEMS policy
- current canonical asset counts
- latest persisted plan header, if present

### `GET /hems/assets`

Returns the canonical HEMS asset view used by the planner, including:

- canonical asset type
- control capability
- execution eligibility
- extracted constraints
- latest telemetry snapshot

### `GET /hems/plans/latest`

Returns the latest persisted HEMS plan, including:

- plan header
- policy snapshot
- interval schedule
- dispatch events
- persisted violations

Returns `404` if no plan has been created yet.

### `PATCH /hems/policy`

Updates the site HEMS policy.

Example body:

```json
{
  "grid_import_limit_kw": 9.5,
  "battery_reserve_pct": 25.0
}
```

### `POST /hems/replan`

Builds the current canonical site model, solves a new plan and runs guarded dispatch for the current interval.
