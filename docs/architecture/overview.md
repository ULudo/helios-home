# Architecture Overview

## Product stance

Helios Home is currently a local discovery and device-inspection runtime.

The system is built to:

- discover network-reachable energy devices locally
- reconcile overlapping evidence from multiple local sources
- materialize a clean device inventory
- keep the runtime offline-capable and low-overhead

Control, optimization and user-facing debugging flows are intentionally deferred from the current public UI.

## Current runtime shape

### 1. Source intake

The backend can collect discovery evidence from:

- local subnet HTTP probes
- mDNS / SSDP advertisements
- native Modbus / SunSpec probes

Additional adapters and deeper debugging services exist behind this layer, but the current stable user flow is centered on network-native local discovery.

### 2. Candidate normalization

Every source emits a normalized candidate shape with:

- identity hints
- device metadata
- protocol evidence
- telemetry samples
- classification confidence

### 3. Reconciliation

When multiple sources describe the same physical device, Helios merges them before materialization so one device record can combine:

- local HTTP evidence
- broadcast evidence
- native Modbus evidence

### 4. Materialization

The reconciled view is persisted into the local database as:

- `DeviceCandidate`
- `Device`
- `Asset`
- `DiscoveryRun`
- supporting audit and diagnosis records

### 5. Read model

The current frontend consumes a compact read model:

- selected network scope
- integrated devices

Each device exposes:

- current status
- protocol tags
- capabilities
- telemetry
- explanation
- next step

## Current boundaries

The current milestone intentionally excludes:

- installer-required serial integrations
- cloud-first architecture
- shared multi-user knowledge sync
- production credential management
- public control/optimization workflows
