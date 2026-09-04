# Adaptive Scan Engine Architecture

## 1. Current bottleneck analysis

Based on an inspection of the current codebase, the following performance bottlenecks are limiting BACnet scanning throughput, ranked by expected impact:

1. **Global Request Serialization:** In `src/bacnet_console/reads.py`, `ReadScheduler` uses a single `asyncio.Lock()` (`self._gate`) for all BACnet requests (`_request` method). This completely prevents any concurrent transactions across the entire network, artificially limiting throughput even when devices are independent and networks are fast.
2. **Global Inter-Request Delay:** The same `_gate` mechanism enforces a global `inter_request_delay_seconds` (defaulting to 0.25s as per `src/bacnet_console/config.py`). This forces a mandatory sleep between every request sent by the scanner, drastically reducing packets/sec regardless of actual network conditions.
3. **Sequential Device Scanning:** `ScanManager` likely iterates over devices sequentially, unable to overlap discovery or scanning phases for multiple devices because of the shared lock.
4. **Simplistic RPM Batching Fallback:** While `ReadScheduler.many` batches ReadPropertyMultiple requests, a single `READ_ERRORS` exception drops the device's batch size significantly and applies exponential backoff to the global delay, punishing all devices.
5. **Indiscriminate Property Collection:** The scanner currently fetches properties like `object-name`, `present-value`, and `units` for all discovered objects without considering if those properties are actually relevant for the specific object type (e.g., units for a binary input).

## 2. Target architecture

The target architecture is a hierarchy of asynchronous schedulers designed to maximize **successful useful properties per second** without exceeding safe network and controller limits.

The architecture will evolve `ReadScheduler` and introduce a multi-level concurrency model:
- **Backend Adapter:** A thin Python layer over `rusty-bacnet` (or BACpypes temporarily) that exposes async BACnet operations and topology metadata.
- **Device Task Pool:** A bounded pool of per-device scan tasks.
- **Hierarchical Load Controller:**
  - *Per-Device Limiter:* Restricts outstanding requests per device (initially 1).
  - *Shared-Path Limiter:* Constrains aggregate concurrency across identified shared routes (e.g., MS/TP trunks).
  - *Global Limiter:* Defines the absolute ceiling for concurrent requests across the entire scanner.
- **Adaptive Sizing & Backoff:** Uses observed latency and success rates to dynamically size RPM requests per-device and adjust shared/global concurrency budgets.
- **Smart Property Mapping:** Uses a minimal static mapping to request only relevant properties in FAST mode.

## 3. Request minimization strategy

To minimize transaction counts and maximize properties per request:

- **RPM Packing:** Batch reads using ReadPropertyMultiple (RPM). Adapt the batch size per device based on success history and max APDU size (if known). If a batch fails, fall back to smaller batches, not individual reads immediately.
- **Property Selection:** Implement a static dictionary mapping BACnet Object Types to their relevant properties for FAST scans (e.g., `analog-input`: `[object-name, present-value, units]`, `binary-input`: `[object-name, present-value]`).
- **Object_List Behavior:**
  1. Try reading the entire `Object_List`.
  2. If it fails (e.g., due to segmentation or size limits), read `Object_List[0]` to get the length.
  3. Batch indexed reads (e.g., `Object_List[1]`, `Object_List[2]`) into RPM requests if supported.
  4. Fall back to individual indexed reads only if RPM fails.
- **Caching:** Cache rarely changing metadata (Object_List, object names, units). Use the `Database_Revision` property (if supported by the device) to invalidate this cache. If unsupported, invalidate via a user-initiated full refresh or a long TTL.
- **Property_List:** In DEEP scans, attempt to use the `Property_List` property to discover exactly what a device supports, rather than guessing.

## 4. Scheduling model

The scheduler consists of logically separate limits, structured hierarchically:

1. **Global Limit:** A hard ceiling on total outstanding confirmed BACnet transactions. Ramps up slowly, acting as the ultimate safety net against flooding the local interface.
2. **Shared-Path Limit:** Groups devices by routing metadata (e.g., `(next-hop-router, destination-network)`). Devices in the same group share a concurrency budget. If the backend cannot determine the path, routed devices fall back to a conservative `generic-routed` bucket.
3. **Per-Device Limit:** The maximum number of outstanding transactions for a single device. Hardcoded to `1` initially.
4. **Fairness:** When multiple devices are queued for a shared path, use round-robin scheduling to prevent one device from starving others on the same MS/TP trunk.

## 5. Adaptive control algorithm

The control loop adjusts limits based on measured performance (properties/sec) and health (RTT, errors).

**Per-Device (RPM Batch Size):**
- *Startup:* Start small (e.g., 2 properties) and conservative timeout.
- *Ramp:* If request succeeds and RTT < `baseline * 1.5`, increase RPM size (e.g., +2 or * 1.5).
- *Hold:* If RTT is increasing but not failing, keep size steady.
- *Reduction:* On timeout, Abort, or Reject, cut RPM size in half (min 1). Apply a short cooldown.
- *Recovery:* After consecutive successes post-failure, cautiously resume ramp.

**Shared-Path & Global Concurrency:**
- *Startup:* Conservative (e.g., path limit=2, global=5).
- *Ramp:* If path is healthy (timeouts < threshold, aggregate RTT stable), increase path budget by 1 every N seconds.
- *Hold:* If marginal throughput (properties/sec) decreases despite higher concurrency, hold.
- *Reduction:* On significant timeout spikes or BACnet network errors, multiplicatively decrease the path/global budget (e.g., budget = budget * 0.5).

*Baseline RTT:* Maintained as an Exponentially Weighted Moving Average (EWMA) of recent successful requests per device.

## 6. Device profile model

To remember device behavior across scans, maintain a profile for known controllers.

**In-Memory State (Reset on restart):**
- Recent RTT baseline (EWMA).
- Current RPM batch size.
- Cooldown/failure state.
- Ongoing scan progress.

**Persistent State (Saved to SQLite):**
- Device Instance and Address.
- Routed path identity.
- Vendor ID.
- `Database_Revision`.
- Max APDU size / Segmentation support (if discovered).
- Highest successful RPM batch size (used to seed the start size of the next scan, minus a safety margin).
- Supports full Object_List (boolean).

## 7. Fast scan versus deep scan

The architecture introduces two explicit scan modes:

**FAST Scan (Default):**
Designed for speed and minimal traffic. Uses the property mapping strategy.
Collects: Devices, Object Identifiers, Object Names, Present_Values (where relevant), Units (where relevant).

**DEEP Scan (Operator Selected):**
Designed for discovery and full metadata extraction. Considerably slower.
Collects: All FAST properties, plus attempts to read all supported properties via `Property_List` (if available), full vendor/model info, protocol revisions, and proprietary metadata.

## 8. Backend recommendation

**Recommendation:** Introduce a thin Python adapter over `rusty-bacnet`, but maintain BACpypes as a selectable fallback.

**Justification:** The current implementation uses BACpypes (which is blocking/synchronous wrapped in asyncio threads/locks in older versions or has concurrency limits). To achieve true concurrent IO for parallel scanning without risking python-level GIL bottlenecks or library-internal global state, a Rust-based backend is ideal.
However, to ensure correct BACnet semantics, we should wrap `rusty-bacnet`'s python bindings in a thin adapter that implements the existing `ScanClient` and `TrendClient` Protocols. This prevents `rusty-bacnet` specific types from leaking into `reads.py` and allows a safe rollback to BACpypes if `rusty-bacnet` lacks parity during early phases.

## 9. Cancellation/lifecycle design

Cancellation must be clean and absolute.

- **Task Ownership:** `ScanManager` owns the top-level scan task. This task spawns device-specific tasks via `asyncio.TaskGroup` (or `asyncio.gather` with strict return_exceptions).
- **Shutdown:** On Stop/SIGTERM, `ScanManager` cancels the top-level task.
- **Resource Release:** The scheduling semaphores (Global, Path, Device) must be used within `async with` blocks to guarantee release even if the waiting task is cancelled via `asyncio.CancelledError`.
- **Database Durability:** Ongoing micro-batch persistence transactions must be flushed to SQLite in a `finally` block or cancellation handler.

## 10. Observability

Expose the following metrics in-memory (and occasionally to the dashboard):
- **Throughput:** Successful properties/sec (the primary metric), total requests/sec.
- **Latency:** EWMA RTT per device; global median and p95 RTT.
- **Errors:** Timeouts, Rejects, Aborts, BACnet Errors (tracked per device and globally).
- **Efficiency:** Average properties per successful RPM, Object_List fallback count.
- **Concurrency:** Current global in-flight count, current shared-path in-flight counts.

## 11. Safety guarantees

Adaptive logic can never override these hard ceilings:
1. **Mutation Ban:** No write or control operations will be implemented in the adapter. Tests will enforce this.
2. **Absolute Global Ceiling:** A hardcoded maximum concurrency limit (e.g., 50) that the adaptive algorithm cannot exceed.
3. **Same-Device Limit:** Enforced at `1` concurrent request per device, regardless of global limits.
4. **No Retry Storms:** Failed requests (timeouts) will not be immediately retried if the batch fails; the system will gracefully fall back to smaller batches or skip the property, enforcing a minimum gap delay.

## 12. Test plan

Deterministic tests that do not require real BAS hardware:

- **Scheduler Tests:**
  - Mock backend to verify that device tasks block on semaphores correctly.
  - Same-device limit is enforced.
  - Global limit is enforced.
  - Shared-path limit is enforced.
  - Permits release after exceptions.
  - Permits release after cancellation.
- **Ramp Tests:**
  - Healthy device ramps upward.
  - Increasing latency stops ramp.
  - Timeout causes aggressive backoff.
  - Reject/Abort causes appropriate backoff.
  - Successful recovery allows cautious reprobe.
- **RPM Tests:**
  - Batch size grows.
  - Oversized RPM reduces learned limit.
  - Missing RPM results fall back safely.
  - Property-level errors don't incorrectly punish the entire network.
  - RPM rejection leads to bounded fallback.
- **Object_List Tests:**
  - Complete `Object_List` works.
  - Indexed fallback works.
  - Indexed RPM batching works.
  - Individual fallback remains available.
  - Oversized object counts respect configured limits.
- **Routing Tests:**
  - Simulate independent IP devices.
  - Simulate multiple devices sharing a routed network.
  - Simulate one problematic routed path while direct devices remain healthy.
- **Cancellation Tests:**
  - Cancel during discovery, scheduler permit wait, RPM request, Object_List enumeration, and database persistence.
  - Ensure no task leaks or deadlocks.
- **Performance Simulation:**
  - Create deterministic fake devices with configurable latency, RPM maximum, timeout behavior, Reject/Abort behavior, routed path, and property counts.
  - Use these simulations to prove the controller converges toward a useful operating point without needing real BAS hardware.

## 13. Performance-validation procedure

Staged live-network validation:
1. **Baseline:** Run existing scanner, record total time and properties/sec.
2. **Request-Count Optimization:** Deploy smart property selection and indexed RPM Object_List fallback. Verify properties/sec increases while total requests drop.
3. **Parallel Independent Devices:** Enable per-device concurrency (limit 1) with a low global ceiling. Verify local IP devices scan rapidly.
4. **Path-Aware Scheduling:** Deploy to a mixed network (IP + MS/TP). Verify MS/TP trunks do not crash and latency remains stable while IP devices scan quickly.
5. **Adaptive Ramp Enable:** Let the system find the performance knee. Verify p95 RTT does not exceed unacceptable levels.

## 14. Implementation phases

**Phase 0 - Baseline Instrumentation**
- **Goal:** Measure current scanner performance before optimization.
- **Files likely to change:** `src/bacnet_console/reads.py`, `src/bacnet_console/scanner.py`, `src/bacnet_console/dashboard.py`.
- **Files that should not change:** `src/bacnet_console/bacnet.py`, `src/bacnet_console/db.py`.
- **Prerequisites:** None.
- **Tests:** Add basic telemetry checks.
- **Rollback point:** Revert telemetry additions if performance overhead is suspected.
- **Parallel implementation:** No, must precede all other phases.

**Phase 1 - Backend-independent request-count reductions**
- **Goal:** Implement smart property selection (FAST scan mode) and better `Object_List` fallback.
- **Files likely to change:** `src/bacnet_console/reads.py`, `src/bacnet_console/scanner.py`, `src/bacnet_console/config.py`.
- **Files that should not change:** `src/bacnet_console/bacnet.py` (no backend changes yet).
- **Prerequisites:** Phase 0.
- **Tests:** Unit tests for indexed `Object_List` fallback and FAST property selection.
- **Rollback point:** Revert to default full property polling.
- **Parallel implementation:** Can be done in parallel with Phase 2 research/scaffolding.

**Phase 2 - Introduce the thin rusty-bacnet backend**
- **Goal:** Introduce `rusty-bacnet` alongside BACpypes using a thin adapter pattern behind the `ScanClient` protocol.
- **Files likely to change:** `src/bacnet_console/bacnet.py`, `src/bacnet_console/config.py` (to allow selection).
- **Files that should not change:** `src/bacnet_console/reads.py`, `src/bacnet_console/scanner.py` (keep scheduling logic unchanged).
- **Prerequisites:** Phase 1.
- **Tests:** Integration tests verifying that the new backend yields identical `ReadResult` / `DiscoveredDevice` shapes as BACpypes.
- **Rollback point:** Switch configuration back to BACpypes backend.
- **Parallel implementation:** Cannot proceed without Phase 1 completion to ensure requests are minimized first.

**Phase 3 - Per-device scheduling**
- **Goal:** Remove the global `_gate` in `ReadScheduler`, replacing it with a per-device limit of 1 and a conservative global semaphore.
- **Files likely to change:** `src/bacnet_console/reads.py`.
- **Files that should not change:** `src/bacnet_console/bacnet.py`, `src/bacnet_console/db.py`.
- **Prerequisites:** Phase 2.
- **Tests:** Scheduler concurrency tests showing independent devices run simultaneously while single devices never exceed limit 1.
- **Rollback point:** Revert to global `_gate`.
- **Parallel implementation:** No.

**Phase 4 - Shared-path scheduling**
- **Goal:** Introduce routing metadata grouping and path-level concurrency budgets based on BACnet network identifiers.
- **Files likely to change:** `src/bacnet_console/reads.py`, `src/bacnet_console/bacnet.py` (to expose routing info).
- **Files that should not change:** `src/bacnet_console/db.py`.
- **Prerequisites:** Phase 3.
- **Tests:** Routing tests simulating multiple devices on a single MS/TP trunk sharing a concurrency limit.
- **Rollback point:** Revert to pure per-device + global limits.
- **Parallel implementation:** No.

**Phase 5 - Adaptive global/path ramping**
- **Goal:** Implement the closed-loop controller (AIMD-like) to adjust path/global concurrency based on marginal throughput and relative RTT.
- **Files likely to change:** `src/bacnet_console/reads.py`.
- **Files that should not change:** `src/bacnet_console/bacnet.py`, `src/bacnet_console/db.py`.
- **Prerequisites:** Phase 4.
- **Tests:** Performance simulation tests proving convergence to a performance knee.
- **Rollback point:** Disable adaptive ramping; use conservative static ceilings.
- **Parallel implementation:** No.

**Phase 6 - Persistent learned device profiles**
- **Goal:** Cache `Database_Revision`, proven RPM sizes, and RTT baselines in SQLite to accelerate subsequent scans.
- **Files likely to change:** `src/bacnet_console/db.py`, `src/bacnet_console/reads.py`, `src/bacnet_console/scanner.py`.
- **Files that should not change:** `src/bacnet_console/bacnet.py`.
- **Prerequisites:** Phase 5.
- **Tests:** Verify SQLite persistence across scan restarts and correct cache invalidation when `Database_Revision` changes.
- **Rollback point:** Disable cache loading; rely only on in-memory adaptation.
- **Parallel implementation:** No.

**Phase 7 - Optional same-device concurrency experiment**
- **Goal:** Experimentally determine if increasing same-device concurrency above 1 improves useful properties/sec without major RTT penalty.
- **Files likely to change:** `src/bacnet_console/reads.py`.
- **Files that should not change:** `src/bacnet_console/bacnet.py`, `src/bacnet_console/db.py`, `src/bacnet_console/scanner.py`.
- **Prerequisites:** Phase 6 is highly stable.
- **Tests:** Ramp tests specific to same-device limits safely backing down if latency spikes.
- **Rollback point:** Hardcode per-device limit back to 1.
- **Parallel implementation:** No.

## 15. Recommended first implementation task

**Implement Phase 1 (Backend-Independent Request-Count Reductions):**
The safest and highest-value starting point is to modify `scanner.py` and `reads.py` to use a targeted dictionary for property selection based on Object Type (FAST scan mode), and to implement the batched indexed `Object_List` fallback mechanism. This immediately reduces unnecessary network traffic and avoids single-device bottlenecks, providing a solid foundation for concurrency improvements, all without changing the underlying BACpypes backend or risking network floods.
