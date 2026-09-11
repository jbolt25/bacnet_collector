# Adaptive BACnet Scan Engine Architecture

## Overview
This document scopes out the architecture to safely speed up BACnet scanning across parallel networks and constrained MS/TP paths, prioritizing reliability and minimizing global Python locks. The adaptive engine introduces concurrency controls that segment requests based on path topology rather than using a single global concurrency limit.

## Goals
- Provide dynamic, automatic read batching and request sizing per device.
- Handle shared communication paths reliably by rate-limiting constrained routes.
- Prevent "retry storms" when dealing with temporarily or permanently unsupported services.

## Architecture

### Concurrency Isolation
The adaptive scanning operates in multiple rate-limiting loops:
1. **Global Concurrency (Slowest loop)**: Sets a maximum ceiling for global BACnet requests in flight to prevent memory exhaustion and buffer overflows on the Raspberry Pi host.
2. **Path Concurrency (Slower loop)**: Group devices on a shared constrained path (like MS/TP segments behind a common router) and apply limits across those devices to avoid router congestion.
3. **Per-Device Tuning (Fast loop)**: Dynamically adjusts read throughput and properties per ReadPropertyMultiple (RPM) request based on device response latency and explicit failures.

### Batch Optimization and Fallback Strategy
RPM batch sizes dynamically increase (up to `network.read_multiple_batch_size`) upon successful consecutive reads and immediately decrease if responses are rejected due to size limits (`bufferOverflow`, `apduTooLong`).

Failures are handled explicitly:
- **Timeouts**: Temporarily skip processing chunks with moderate cooldowns.
- **Unsupported Services**: Trigger individual `ReadProperty` fallback with extended cooldowns to reduce redundant RPM requests.
- **Capacity Rejects**: Immediately halve the batch size with bounded retries and no general cooldowns.

## Telemetry
- Maintain complete accountability for telemetry metrics. All property reads—whether successfully batched in RPM or falling back to single reads—must track their outcomes so successful data points are never undercounted.
