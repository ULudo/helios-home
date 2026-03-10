# Discovery Core

## Mission

The discovery core exists to answer three questions reliably:

1. Which network-reachable energy devices are present?
2. Which local protocols or interfaces describe the same physical device?
3. What telemetry and status can Helios validate right now?

## Pipeline

### Source adapters

The active local adapters are:

- subnet HTTP probing
- mDNS / SSDP parsing
- native Modbus / SunSpec probing

Each adapter produces raw candidate evidence.

### Raw candidates

A raw candidate carries:

- candidate id
- likely device id
- manufacturer / model / firmware
- observed protocols
- telemetry sample
- identity keys and source evidence

### Classification

Candidates are classified into energy-relevant types such as:

- `pv_inverter`
- `battery`
- `grid_meter`
- `wallbox`
- `heat_pump`
- `smart_appliance`

### Reconciliation

Before persistence, Helios merges candidates that share strong identity signals such as:

- network-host identity
- HTTP host identity
- service-instance identity
- matching source-specific slugs

### Materialization

The reconciled result is written into the local store as:

- `DeviceCandidate`
- `Device`
- `Asset`
- `DiscoveryRun`

## Why this matters

Without explicit reconciliation and materialization, discovery becomes transient and hard to reason about.

With this shape, Helios can:

- preserve provenance
- avoid duplicate devices
- surface the protocol path that actually worked
- build monitoring on top of the integrated device record

## Current limits

The discovery core is intentionally limited to network-reachable devices.

It does not currently try to solve:

- serial-only devices that need extra hardware
- shared cloud knowledge
- automated adapter patch generation
- user-facing debug workflows in the frontend
