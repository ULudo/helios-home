# API Endpoints

Base URL: `/api/v1`

The current stable public API is intentionally small and aligned with the current UI.

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
