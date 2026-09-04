# BACnet Testing Strategy for Rust Migration

This document outlines the testing strategy for verifying the planned Rust BACnet backend against the existing `bacpypes3` Python implementation. This strategy assumes the Rust backend will be integrated using an application-level facade (e.g., PyO3 or a separate service boundary).

The primary goal is ensuring the observable, application-facing contract behaves correctly. The application is a **read-only data collector**, meaning `WriteProperty` is strictly forbidden in production.

## Current Test Coverage & Gaps
A review of the repository reveals the following existing test coverage:
- **Unit and Integration Mocks:** Tests (e.g. `tests/test_batching.py`, `tests/test_gap_fixes.py`) heavily utilize Python-level mock clients (e.g. `FakeScanClient`, `ParserApp`, `BatchClient`). These verify the logic of the collector and application (like batch limits and fallbacks).
- **Safety Checking:** Tests (e.g. `tests/test_safety.py`) use AST parsing and explicit API checks to guarantee no mutating actions (`write_property`, `subscribe`) are made by the application.
- **Database & Dashboard Coverage:** Good coverage of application-layer state (`tests/test_db.py`, `tests/test_dashboard.py`).

**Identified Gaps:**
- **Lack of Raw Packet Fixtures (PCAPs):** Existing tests simulate `ReadPropertyMultiple` responses as high-level Python exceptions or valid objects. There are no actual BACnet datagrams used for testing how the network layer recovers from edge cases (segmentation faults, invalid tags).
- **Rust-to-Python Integration Testing:** Currently, there's no infrastructure to stand up the mock `TrendClient` alongside the Rust implementation and ensure both emit identical `ReadResult` payloads for the same stimulus.

## Behavioral Inventory

The following BACnet behaviors have been observed in the repository and categorized by priority. Tests should focus purely on what this repository actually does.

### P0 — Must exist before Rust replaces BACpypes
- **Discovery results (`Who-Is`):** Emitting local broadcasts and returning device instance/address pairs within a timeout window.
- **Targeted Discovery (`Who-Is` with limits):** Finding specific device instances when an address is unknown.
- **Address Parsing:** Translating application string IP/MAC addresses to BACnet addressing structures.
- **`ReadProperty` return values:** Fetching primitive values (e.g., present-value, object-name, units).
- **`ReadPropertyMultiple` (RPM) results:** Batching multiple object/property requests in one frame, mapping response arrays back to application variables, and handling per-property errors without dropping the whole batch.
- **Object-list fallback (`ReadProperty` by index):** Fetching `object-list` length via index `0`, followed by indexing each item if a full object-list read fails or segments.
- **Exceptions/Errors (BACnet Errors, Reject, Abort):** Translating BACnet network errors into string/object application responses (e.g., "property unknown-property") rather than panicking or crashing.
- **Timeout duration & Retries:** Strict adherence to `request_timeout_seconds` and network response windows.
- **Pacing & Rate Limiting:** Enforcing `inter_request_delay_seconds` and consecutive error backoffs.

### Secondary — Present but handled transparently
- **Malformed responses:** Recognizing unusable datagrams without killing the collector loop (handling them as read errors).
- **Segmentation:** Handled by the network stack; if a device requires segmentation for a large `object-list`, the fallback read-by-index is triggered instead.

### Not Used — Do not test or implement
- **WriteProperty / Write priority / Relinquish:** The application does not mutate state.
- **COV (Change of Value) Subscriptions.**
- **Concurrent requests to the same device:** Requests are serialized per device via a scheduler.
- **Routed devices / BBMD:** The current implementation hardcodes `foreign=None` and `bbmd=None`.

## Design: BACpypes-vs-Rust Differential Testing

Testing both implementations with identical logical operations should be performed via an application-level comparison layer. The design should remain backend-agnostic.

**Differential Testing Granularity Hierarchy:**
1. **Application-level Semantic Equivalence (Primary):** Asserting that the Python-facing results (dictionaries, `ReadResult` objects) returned by the Rust backend are identical to those returned by the `BACpypes` backend.
2. **Decoded BACnet Value Equivalence:** Asserting that complex types are unrolled and formatted appropriately (e.g., boolean normalization, numeric extraction, string encodings).
3. **Captured/Golden Packet Fixtures:** Comparing application outputs against deterministic PCAP fixtures of known problematic device behavior.
4. **Exact Packet Equivalence (Minimal):** Only compare exact byte encodings for deterministic outbound requests (e.g., standard `Who-Is` broadcasts). Where BACnet permits multiple valid encodings for RPM requests, semantic equivalence is preferred.

**Normalization Rules:**
When comparing BACpypes objects with Rust/Python values, normalization must handle:
- Extraneous Python `AnyAtomic` wrapper unpacking.
- Numeric limits: filtering out non-finite floats (`NaN`/`Infinity`).
- Boolean serialization (`"true"`/`"false"`).

## Malformed Network Input Test Matrix

The Rust implementation must strictly return structured errors back to Python rather than panicking. Testing must include:

- **Truncated BVLC**
- **Truncated NPDU**
- **Malformed APDU**
- **Invalid Tag / Invalid Length**
- **Missing Required Data**
- **Unexpected APDU Type:** (e.g., an Unconfirmed Request when an ACK is expected)
- **Wrong Invoke ID:** (mismatched response mapping)
- **Malformed `ReadProperty` response**
- **Malformed `RPM` response:** (e.g., missing property array elements, unexpected nesting)
- **Explicit Protocol Errors:**
    - BACnet Error (`errorClass`, `errorCode`)
    - Reject (e.g., Unrecognized Service)
    - Abort (e.g., Segmented Message Not Supported)
- **Timeout:** Complete lack of response.
- **Duplicate Response:** Network echoing or retry overlaps.

*Test Environment Note:* These malformed scenarios can be generated in software without physical BACnet hardware by mocking the UDP transport layer and feeding raw binary payloads to the Rust and BACpypes parsing implementations. Reusable fixtures (binary files or hex strings) should be used.

**Failure Reproduction & Fixture Capture:**
To capture problematic device responses as reusable fixtures:
1. Run `tcpdump -i <interface> udp port 47808 -w capture.pcap` during a collector cycle that encounters a read error (the error will be logged in the database).
2. Use Wireshark to filter by the device's IP and isolate the specific `ReadProperty` or `RPM` ACK/Error packet.
3. Extract the UDP payload as a hex string or raw binary blob.
4. Add it to a new `tests/fixtures/` directory alongside the expected application-level `ReadResult` output.
5. In integration tests, write a loop that passes this binary blob to both `BACpypes` and `Rust` parsers, asserting identical semantic outputs (or safe error handling).

## Live-network Testing & Comparison Mode

While most protocol logic and malformed-input handling can be tested using software mocks, timing tolerances, discovery pacing, and real-world network segmentation require a live BACnet network.

**Backend Selection Mechanism:**
Implement a safe mechanism to choose the backend via environment variable:
```bash
BACNET_BACKEND=bacpypes
# or
BACNET_BACKEND=rust
```

**Comparison Mode (`BACNET_BACKEND=compare`):**
A runtime software layer that issues read operations sequentially to both backends and compares the results in memory.
- **Sequential Execution:** Recommended over concurrent to avoid confusing low-resource BACnet devices with duplicate simultaneous requests.
- **Diagnostic Output:** Mismatches should log both representations (BACpypes value vs Rust value) to the database error table for analysis.
- **SAFETY RULE:** **Comparison mode must NEVER duplicate `WriteProperty` or other mutating operations.** If writes are introduced in the future, they must bypass comparison mode and execute on only one backend.

## Deliverable & Prioritization

The testing rollout plan is divided as follows:

### P0 — Must exist before Rust replaces BACpypes
- Mock/Fixture tests for semantic equivalence of `ReadProperty` and `ReadPropertyMultiple`.
- Comprehensive malformed packet testing to guarantee the Rust backend never panics on bad data.
- Unit tests verifying timeouts and pacing boundaries.
- Live-network environment variable selection mechanism.
- P0 Pass Gate: 100% test pass on standard reads and zero panics on the malformed matrix.

### P1 — Important
- Application-level comparison mode (`BACNET_BACKEND=compare`) implemented and deployed on a live test network.
- Discovery (`Who-Is`) equivalence testing, including fallback address matching.
- Exact packet equivalence on outbound `Who-Is` and `ReadProperty` requests where deterministic.
- P1 Pass Gate: Comparison mode runs on a live test network for 24 hours with zero semantic mismatches.

### P2 — Desirable robustness/performance testing
- Memory profiling of the Rust backend during high-concurrency scheduling.
- Long-running stability testing under artificial network packet loss.
- Golden PCAP fixtures representing complex multi-vendor segmentation behaviors for offline regression testing.
