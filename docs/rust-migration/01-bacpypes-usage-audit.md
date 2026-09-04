# BACpypes Usage Audit

## Overview
This document outlines the direct and indirect usage of the `bacpypes`/`bacpypes3` libraries within this repository.

## Locations of BACpypes Usage

| Location | Current BACpypes dependency | BACnet functionality | Required? | Notes |
|---|---|---|---|---|
| `src/bacnet_console/bacnet.py:9` | `ErrorRejectAbortNack` | BACnet errors, rejects and aborts | Yes | Base class for error states. |
| `src/bacnet_console/bacnet.py:10` | `ErrorType` | BACnet errors, rejects and aborts | Yes | Error matching. |
| `src/bacnet_console/bacnet.py:11` | `ObjectIdentifier, PropertyIdentifier` | BACnet object & property identifiers | Yes | Parsing object ids. |
| `src/bacnet_console/bacnet.py:61-125` | `Application`, `Address` | Core BACnet Application, Addressing | Yes | Sets up UDP network, instance, routing defaults. |
| `src/bacnet_console/bacnet.py:90` | `Application.read_property` | `ReadProperty` | Yes | Reads single points. |
| `src/bacnet_console/bacnet.py:109` | `Application.read_property_multiple` | `ReadPropertyMultiple` | Yes | Reads batches of points. |
| `src/bacnet_console/bacnet.py:127-166` | `Application.who_is` | `Who-Is` / `I-Am` | Yes | Discovers devices on network and manually targets ones. |
| `src/bacnet_console/scanner.py:16,24` | `ObjectIdentifier` | BACnet object identifiers | Yes | Helper function `_object_id` for ensuring object-list is parsed correctly. |
| `tests/test_batching.py:6-15` | `ReadPropertyMultipleACK`, `ErrorType`, `Any`, `ObjectIdentifier`, `Real`, `ReadWritePropertyMultipleServices`, `get_vendor_info`, `AnalogInputObject` | `ReadPropertyMultiple`, Application classes | Yes | Used to create mock testing structures. |
| `tests/test_review_regressions.py:12` | `ErrorRejectAbortNack` | BACnet errors, rejects and aborts | Yes | Creates test `ProtocolFailure`. |

### Project-Specific Wrappers

| Location | Current BACpypes dependency | BACnet functionality | Required? | Notes |
|---|---|---|---|---|
| `src/bacnet_console/bacnet.py:61` | `_BacpypesOwner` | Encapsulates `Application` | Yes | Abstracts setup and provides thread-safe `_read` and `_read_multiple`. |
| `src/bacnet_console/bacnet.py:127` | `BacpypesScanClient` | Encapsulates `_BacpypesOwner` | Yes | Operator-scan facade, adding `discover_all` and `discover_target` |
| `src/bacnet_console/bacnet.py:169` | `TrendOnlyView` | Encapsulates `ScanClient` | Yes | Capability-limited view masking discovery calls. |
| `src/bacnet_console/reads.py:22` | `ReadScheduler` | Calls `TrendClient` | Yes | Paces read requests to specific addresses using backoff and batch limits. |

## Minimum BACnet functionality actually used

- **BACnet/IP & UDP Networking:** Basic un-routed or locally-routed IP connectivity using an explicitly bound local interface (via `Address`). BBMD and Foreign Device registration are explicitly set to `None` in the code, but `ttl` and `network` parameters are provided.
- **Who-Is / I-Am:** Used for dynamic network discovery (`discover_all`) and targeted device resolution (`discover_target`). Parses `iAmDeviceIdentifier` and `pduSource`.
- **ReadProperty:** Used as a fallback when `ReadPropertyMultiple` fails, or for single points. Supports `AnyAtomic` decoding.
- **ReadPropertyMultiple:** Uses batched properties to read values.
- **BACnet object identifiers & property identifiers:** Parses `object-list` items and requested properties using `ObjectIdentifier` and `PropertyIdentifier`. Includes indexed properties like `object-list[0]`.
- **BACnet errors, rejects and aborts:** Distinguishes application data from `ErrorType` and `ErrorRejectAbortNack` protocol exceptions.

*Note: There is NO evidence of WriteProperty, COV, or SubscribeCOV.*

## BACpypes dependency graph

1. **`_BacpypesOwner`** (in `bacnet.py`) is the root container for the BACpypes `Application`. It creates the BACpypes app from a `Namespace` object (simulating command-line arguments) and holds an `asyncio.Lock` to ensure single-flight requests into BACpypes. It implements primitive `_read` and `_read_multiple` methods.
2. **`BacpypesScanClient`** extends `_BacpypesOwner` to implement the `ScanClient` protocol. It exposes discovery (`discover_all`, `discover_target`) which call the `Application.who_is` method, and public read methods.
3. **`TrendOnlyView`** takes a `ScanClient` and implements the `TrendClient` protocol, masking out the discovery methods.
4. **`ReadScheduler`** (in `reads.py`) wraps the `TrendClient` (or `ScanClient`) to provide paced, batched, and failure-tolerant reads, handling backoff states internally.
5. **`Collector`** (in `collector.py`) and **`ScanManager`** (in `scanner.py`) depend on `ReadScheduler` and the clients to do the actual data fetching.
6. The overarching architecture heavily relies on Python's `asyncio` loop and `timeout` logic to bridge between BACpypes and application scheduling. BACpypes3 types leak into the wrapper returns (like `ReadResult` storing wrapped protocol strings or `ErrorRejectAbortNack` types) and testing modules.

## Most difficult dependencies to replace

1. **High:** `Application` and UDP/IP Network binding. A Rust migration must recreate the exact BACnet/IP state machine, port binding, and packet routing required to act as a BACnet device.
2. **High:** `ReadPropertyMultiple` parsing and serialization. Handling arbitrary BACnet primitive and constructed types (like `AnyAtomic` or arrays) requires a robust ASN.1/BACnet encoder/decoder in Rust.
3. **Medium:** `asyncio` event loop integration. The Rust implementation will need an equivalent async executor (like Tokio) and bridge correctly to Python if Python is retained as the front-end, or handle its own locking and timeout semantics.
4. **Medium:** Protocol-level error parsing (`ErrorRejectAbortNack`).

## Potentially removable legacy BACpypes code

- `src/bacnet_console/bacnet.py` handles `AnyAtomic` checks using duck-typing (`__class__.__name__ == "AnyAtomic" and hasattr(value, "get_value")`). This is an artifact of varying BACpypes responses and might be removable or streamlined in a strict Rust schema.
- The `tests/test_batching.py` constructs a fake BACpypes `ReadWritePropertyMultipleServices` application to test the scheduler. If BACpypes is removed, this test harness will also need replacing or deleting.
- `_BacpypesOwner` configures `vendoridentifier=999`, `foreign=None`, `bbmd=None` using a dummy `argparse.Namespace`. This boilerplate can be removed when moving to a natively configured Rust application.

## Migration surface estimate

**This repository uses a tiny BACnet subset.**

Evidence:
- The application is explicitly a "read-only collector".
- Tests explicitly verify that there are no mutation calls (e.g. `test_bacnet_source_contains_no_mutation_calls`).
- The application only uses `Who-Is` for discovery, and `ReadProperty` / `ReadPropertyMultiple` for data collection.
- There are no control loops, WriteProperty, COV subscriptions, segmentation handling, or advanced routing capabilities configured.
- The dependencies are cleanly encapsulated in `bacnet.py` behind clear interfaces (`TrendClient` and `ScanClient`).
