# HEMS Core

## Mission

The HEMS core coordinates already-integrated household assets without embedding vendor logic into the planner.

It consumes:

- the local device and asset inventory
- confirmed HEMS system bindings created by the agent setup flow
- validated telemetry
- validated write-path capability
- site policy
- EEBus LPC/LPP grid-side load-control limits
- local forecast heuristics or future external feeds

It produces:

- a deterministic plan
- guarded dispatch decisions
- persisted audit records
- explicit violations when planning or dispatch is degraded

## Core design

### Canonical site model

The optimizer does not see vendor-specific device metadata directly. Discovery assets are mapped into a canonical site model with:

- `battery`
- `controllable_load`
- `ev_charger`
- `grid_meter`
- `heat_pump`
- `pv_inverter`
- `uncontrolled_load`

Each canonical asset carries:

- binding id and user-facing label when the system was confirmed
- binding, connection, telemetry, and control status
- telemetry snapshot
- extracted constraints
- control capability
- execution eligibility
- explanation for blocked or plan-only states

Discovery classification is treated as evidence, not as the final product truth. When the user or agent confirms that a physical device belongs to a HEMS role, the confirmed binding overrides the raw discovered label and role inside the canonical site model while preserving the original discovered identity.

### Eligibility gates

The current execution levels are:

- `read_only`
- `plan_only`
- `dispatchable`
- `blocked`

Guarded automatic dispatch is only allowed for assets with:

- validated telemetry
- validated write capability
- a current device status strong enough for local actuation

This keeps unsupported or weakly validated integrations visible to the planner without allowing unsafe control.

### Planning layer

The current planner is implemented with:

- `CVXPY`
- `HiGHS`

The V1 optimization horizon is policy-driven and defaults to:

- `24h` horizon
- `15 min` step size

V1 models included now:

- battery charge / discharge with SOC dynamics and reserve floor
- binary controllable loads with runtime targets and minimum-on windows
- EV charging with deadline and target-SOC fulfillment pressure
- heat-pump comfort-band shifting with a simple thermal proxy
- exogenous PV generation and base-load forecast
- site import and export limits

### Dispatch layer

The dispatcher applies only the current interval of the latest plan.

Current rules:

- clamp commands to current asset limits
- skip dispatch in plan-only mode
- block dispatch when no validated adapter is available
- allow telemetry simulation for explicitly marked test assets
- allow opt-in native write adapters for explicitly validated local device profiles

Every dispatch attempt is stored as a persisted event.

### Write-adapter layer

The write-adapter layer translates canonical HEMS commands into concrete local device calls.

Current supported adapters:

- `telemetry_simulation`
- `shelly_http_relay`
- `tasmota_http_power`

The adapter layer is:

- deterministic
- capability-gated
- opt-in for native writes
- protocol-specific but planner-agnostic

The planner never talks to vendor APIs directly. It emits canonical commands such as:

- `set_power_kw`
- `set_charge_kw`
- `start_stop`

The write adapter is responsible for turning these into:

- local HTTP requests
- device-specific command parameters
- applied-state bookkeeping
- explicit dispatch failures when the path is not validated

### EEBus LPC/LPP distribution

EEBus LoadControl limits enter the HEMS as grid-connection constraints:

- `limitationOfPowerConsumption` uses LoadControl limit id `0` and maps to the policy import limit.
- `limitationOfPowerProduction` uses LoadControl limit id `1` and maps to the policy export limit.
- Helios only tightens the current policy limit for an active command, then replans and lets the existing guarded dispatcher distribute the resulting commands to eligible assets.

This keeps EEBus standard handling at the grid boundary while preserving the planner's vendor-neutral internal model.

### Audit and persistence

The HEMS layer persists:

- `HemsPolicy`
- `HemsPlanRun`
- `HemsPlanInterval`
- `HemsDispatchEvent`
- `HemsViolation`

This makes every plan, interval and dispatch outcome inspectable after the fact.

## Current boundaries

The current HEMS core intentionally does not yet include:

- rich end-user control workflows
- production device write adapters
- advanced building physics models
- shared cloud coordination
- LLM-driven control

The LLM remains a future assistive layer for explanation, research and debugging, not a runtime control dependency.
