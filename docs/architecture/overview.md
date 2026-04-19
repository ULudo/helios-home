# Architecture Overview

## Product stance

Helios Home is currently a local, agent-first runtime for discovering devices, confirming setup decisions through conversation, and building a deterministic HEMS foundation underneath.

The system is built to:

- discover network-reachable energy devices locally
- reconcile overlapping evidence from multiple local sources
- materialize a clean device inventory
- map integrated assets into a canonical HEMS model
- plan and audit guarded local energy-management decisions
- keep the runtime offline-capable and low-overhead

The public UI is conversation-first. Technical inventory and HEMS state still exist, but they are secondary, advanced surfaces behind the same runtime.

The assistant runtime is now provider-neutral at the configuration layer:

- the user can select a model provider locally
- credentials remain local to the machine
- the deterministic tool core stays inside Helios
- model-backed phrasing sits above the deterministic setup/runtime state

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

The current frontend consumes:

- a conversation thread with streamed activity
- pending confirmation proposals
- a setup profile with confirmed systems and unresolved questions
- advanced inventory and HEMS inspection data

### 6. HEMS backend

Above discovery, Helios now builds a canonical site model for:

- `pv_inverter`
- `battery`
- `grid_meter`
- `ev_charger`
- `heat_pump`
- `controllable_load`
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
