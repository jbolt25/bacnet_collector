# Rust BACnet Migration Options

## Candidate Evaluation

| Candidate | Features relevant to this project | Missing features | Activity | License | Python integration difficulty | Risk |
|---|---|---|---|---|---|---|
| **rusty-bacnet** | BACnet/IP, Who-Is / I-Am, ReadProperty, ReadPropertyMultiple, async request handling, malformed packet handling, error/reject/abort handling, datatypes | None required by project; actively building out full 135-2020 conformance. | Very High (actively maintained, 20+ releases, 5500+ tests) | MIT | Very Low (Already provides native PyO3 + pyo3-async-runtimes bindings for asyncio) | Low |
| **rust-bac** (rbhans) | BACnet/IP, Who-Is, ReadProperty, ReadPropertyMultiple, async via Tokio, no_std core | No built-in Python integration, relies heavily on CLI tools for high-level usage | Moderate (Some commits in 2026, but low open issue activity) | MIT / Apache-2.0 | High (Would require writing PyO3/maturin bindings from scratch) | Medium |
| **bacnet-rs** (bachp / bacnet-stack) | BACnet/IP, Who-Is, ReadProperty, ReadPropertyMultiple | Segmentation, Error/Abort handling gaps, lack of complex datatype wrappers | High (active development, but API is unstable) | MIT / Apache-2.0 | High (Requires building complete PyO3 wrappers) | High |

*(Note: "Missing features" primarily highlights features lacking that are required by the project. All three libraries lack some advanced/legacy features not required by our read-only dashboard, such as full MS/TP or complex routed network setups, but rusty-bacnet is the most feature-complete).*

## Determine the best strategy

1. **Use an existing Rust BACnet library directly:** This is the highest-ranked approach. The existence of `rusty-bacnet`, which already has high test coverage, native PyO3 asyncio bindings, and targets the exact BACnet/IP read features our project requires, makes it the optimal choice. It minimizes maintenance overhead and reduces the risk of protocol errors.
2. **Use an existing protocol core but build our own transaction/application layer:** This is the second-best approach, viable if we used `rust-bac`'s `rustbac-core`. However, since our project relies heavily on concurrent `asyncio` reads, reinventing the transaction state machine and timeout management is unnecessary given that `rusty-bacnet` already handles this safely in Tokio.
3. **Fork and extend an existing library:** Ranked third. Forking is a high-maintenance burden and should only be done if the upstream project is abandoned or refuses critical patches.
4. **Implement the project's required BACnet subset ourselves:** Ranked lowest. Implementing BACnet from scratch is a massive undertaking with a high risk of parsing vulnerabilities, malformed packet crashes, and interoperability failures. It should be strictly avoided.

## Immature or dangerous areas

Regardless of the library chosen, several areas of BACnet implementation in Rust require careful validation:

- **Malformed packet handling:** BACnet devices are notorious for sending corrupted or proprietary APDUs. The Rust decoder must guarantee no panics (memory safety) when parsing invalid bytes.
- **Concurrent transactions:** Managing multiple in-flight ReadPropertyMultiple requests (Invoke-ID management) without state corruption or dropped packets under heavy load.
- **Segmentation:** Although not currently heavily used by the project, devices sending large ReadPropertyMultiple responses may require segmentation. Lack of segmentation support can cause silent data loss.
- **Proprietary BACnet data:** The library must be able to gracefully skip or wrap proprietary object/property IDs without failing the entire response.
- **COV / Routed Networks:** Not currently in use by the dashboard, but if future requirements add them, untested library implementations may struggle with BBMD or foreign device registration edge cases.

## Recommended Rust foundation

**`rusty-bacnet`** is the strongly recommended foundation for this migration.

**Why:** It perfectly aligns with the project's requirements for reliability and maintainability. It natively solves the hardest part of the migration—Python `asyncio` to Rust `tokio` integration—via its existing PyO3 bindings. It supports the essential BACnet/IP read services, has an extensive test suite (5,500+ tests), is actively maintained, and uses a permissive MIT license. Its design explicitly addresses safe concurrent request handling and error management (Reject/Abort types).

## Unknowns requiring prototype validation

Before committing fully to `rusty-bacnet` (or any alternative), we must validate the following through focused experiments:

1. **Python `asyncio` GIL Contention:** Verify that `rusty-bacnet`'s Tokio runtime does not block the Python GIL during heavy parallel `ReadPropertyMultiple` requests across many devices.
2. **BACpypes Error Parity:** Ensure that Rust's error types (Timeout, Reject, Abort) map cleanly to the expected `ReadResult` failure states currently handled in `bacnet.py`.
3. **Complex Datatype Decoding:** Validate that the Rust library correctly decodes edge-case datatypes (e.g., deeply nested arrays or proprietary properties) into native Python types without crashing.
4. **Memory Leaks in PyO3:** Run a continuous 24-hour polling loop to monitor for memory leaks when creating and dropping thousands of Rust-backed Python object wrappers.
5. **Cross-Platform Compilation:** Verify that Matruin can easily build wheels for both Linux (target server) and Windows/macOS (developer environments) without complex C-toolchain dependencies.
