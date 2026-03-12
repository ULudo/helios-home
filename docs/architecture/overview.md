# Architecture Overview

## Product stance

Helios Home is currently a local discovery and device-inspection runtime with a backend-first HEMS core.

The system is built to:

- discover network-reachable energy devices locally
- reconcile overlapping evidence from multiple local sources
- materialize a clean device inventory
- map integrated assets into a canonical HEMS model
- plan and audit guarded local energy-management decisions
- keep the runtime offline-capable and low-overhead

Control and optimization remain deferred from the current public UI, but the backend core for HEMS planning now exists behind the API.

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

### 6. HEMS backend

Above discovery, Helios now builds a canonical site model for:

- `pv_inverter`
- `battery`
- `grid_meter`
- `ev_charger`
- `heat_pump`
- `uncontrolled_load`

The HEMS backend then:

- determines execution eligibility per asset
- generates a plan over a configurable horizon
- dispatches the current interval only through guarded paths
- persists plans, dispatch events and violations

Current native write support is intentionally narrow and opt-in:

- telemetry simulation for validated test assets
- Shelly local HTTP relay control
- Tasmota local HTTP power control

## Current boundaries

The current milestone intentionally excludes:

- installer-required serial integrations
- cloud-first architecture
- shared multi-user knowledge sync
- production credential management
- public control/optimization workflows
- production-grade device actuation coverage
