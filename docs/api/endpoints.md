# API Endpoints

Base URL: `/api/v1`

The current stable frontend-facing API is centered on the Helios assistant workspace.

The backend also exposes HEMS inspection and planning endpoints for advanced use and runtime validation.

## Health

### `GET /health`

Returns a minimal liveness payload:

- service status
- runtime mode
- database readiness flag

## Overview

### `GET /overview`

Returns the device inventory and current site scope used by the advanced runtime view:

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

## Agent workspace

### `GET /agent/thread`

Returns the current conversation thread, including:

- persisted user and assistant messages
- pending confirmation proposals
- current setup profile
- latest debug case summary, if present

### `GET /agent/provider-config`

Returns the current assistant model-provider configuration state:

- selected provider
- effective provider used at runtime
- readiness status and message
- configured provider options

The response never returns stored credentials.

### `PATCH /agent/provider-config`

Updates the local assistant model-provider configuration.

Example body:

```json
{
  "provider_id": "openai",
  "model": "your-model-id",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-..."
}
```

### `GET /agent/setup-profile`

Returns the current setup memory:

- confirmed systems
- unresolved setup questions
- saved user notes

### `POST /agent/messages`

Creates a user message and starts a new assistant turn.

Request body:

```json
{
  "content": "I want to integrate my heat pump."
}
```

### `GET /agent/turns/{turn_id}/events`

Streams the turn as Server-Sent Events, including:

- assistant text deltas
- tool start/finish events
- proposal creation events
- final assistant message

### `POST /agent/proposals/{proposal_id}/confirm`

Confirms a pending action proposal and applies the associated setup change.

### `POST /agent/proposals/{proposal_id}/reject`

Rejects a pending action proposal and leaves the current setup unchanged.

## HEMS

### `GET /hems/summary`

Returns:

- current HEMS policy
- current canonical asset counts
- latest persisted plan header, if present

### `GET /hems/assets`

Returns the canonical HEMS asset view used by the planner, including:

- canonical asset type
- confirmed binding and connection state, when present
- control capability
- execution eligibility
- extracted constraints
- latest telemetry snapshot

### `GET /hems/bindings`

Returns confirmed HEMS system bindings created through the agent setup flow, including:

- canonical HEMS role
- user-facing system label
- linked device and asset ids
- binding, connection, telemetry, and control status
- resolver evidence used when the binding was confirmed

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

## EEBus

### `GET /eebus/ship-services`

Runs an EEBus SHIP DNS-SD discovery pass through the standard `eebus-sdk` integration and returns discovered `_ship._tcp.local` services.

### `POST /eebus/load-power-limits/distribute`

Accepts an EEBus LoadControl LPC/LPP limit, maps it into the HEMS grid policy, and replans.

Example LPC body:

```json
{
  "use_case": "lpc",
  "limit_watts": 4200,
  "duration_seconds": 7200,
  "source": "eebus",
  "peer_ski": "0123456789abcdef0123456789abcdef01234567"
}
```

Mapping:

- `lpc` / `limitationOfPowerConsumption` / `limit_id: 0` updates `grid_import_limit_kw`
- `lpp` / `limitationOfPowerProduction` / `limit_id: 1` updates `grid_export_limit_kw`
