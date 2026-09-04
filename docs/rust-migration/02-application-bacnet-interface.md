# Application BACnet Interface

## Current Application Operations

The application currently relies on a limited subset of BACnet functionality, primarily centered around discovering devices and reading properties.

### BACnet Operations Summary

| Application operation | Calling code | Inputs | Expected result | Error behavior | BACnet operation underneath |
|---|---|---|---|---|---|
| Discover all devices | `ScanManager._run` -> `ScanClient.discover_all` | `response_window: float` | `tuple[DiscoveredDevice, ...]` | Handled generally via `asyncio.timeout` and `READ_ERRORS` bubble up | Who-Is (Broadcast) |
| Discover target device | `ScanManager._run` -> `ScanClient.discover_target` | `instance: int`, `address: str`, `response_window: float` | `DiscoveredDevice \| None` | Handled generally via `asyncio.timeout` and `READ_ERRORS` bubble up | Who-Is (Unicast to specific address/instance) |
| Read single property | `ReadScheduler.one` -> `TrendClient.read` / `ScanClient.read` | `address: str`, `object_id: str`, `property_id: str` | `Any` (Primitive value or ErrorType/AnyAtomic) | Returns `ReadResult` containing `error` string or `Exception` / `ErrorRejectAbortNack` bubbles up and is caught. | ReadProperty |
| Read multiple properties | `ReadScheduler.many` -> `TrendClient.read_multiple` / `ScanClient.read_multiple` | `address: str`, `points: list[ReadKey]` | `dict[ReadKey, ReadResult]` | Modifies batch size down on failure, falls back to individual reads on timeout/error. | ReadPropertyMultiple |

### Not Currently Used

The following features are **not currently used** and do not need backend implementations at this time:

- WriteProperty / Writing at a BACnet priority
- Relinquishing a value
- COV (Subscribe to COV / Process COV notifications)
- BBMD / foreign-device registration
- Segmentation configuration or negotiation (handled opaquely if at all)
- Proprietary objects/properties beyond standard name/value/units polling
- Caching at the BACnet layer (application manages database state)

## BACpypes Type Coupling

The current implementation has BACpypes types leaking across the boundary into the application logic, particularly within `src/bacnet_console/bacnet.py`, `reads.py`, and `scanner.py`.

*   **`ErrorRejectAbortNack` and `Exception`:** In `reads.py` and `bacnet.py`, the `READ_ERRORS` tuple exposes `ErrorRejectAbortNack` to the application, which catches it to manage pacing and fallback logic. `read_result` explicitly checks for `ErrorRejectAbortNack` and stringifies it.
*   **`ErrorType`:** The `read_result` function in `bacnet.py` receives BACpypes `ErrorType` objects when a property read fails gracefully within a successful response, converting `value.errorClass` and `value.errorCode` to a string.
*   **`AnyAtomic`:** The `read_result` and `_read` logic in `bacnet.py` explicitly unwraps BACpypes `AnyAtomic` wrapper types using `value.get_value()`.
*   **`ObjectIdentifier` and `PropertyIdentifier`:** In `scanner.py` and `bacnet.py`, BACpypes primitive types are used to format identifiers and correlate ReadPropertyMultiple responses back to requests. `scanner.py` falls back to string parsing if the `bacpypes3` import fails.
*   **`Address`:** The application instantiates BACpypes `Address` objects (e.g. for targeted `Who-Is`) based on application-level string definitions.

These leaks tightly couple the application's core read-scheduling and scanning logic to BACpypes-specific data structures.

## Proposed Compatibility Contract

The backend interface should completely abstract away underlying library types (BACpypes or Rust) and expose domain-level objects and errors.

### Domain Types

```python
from dataclasses import dataclass
from typing import Any, Protocol

class BacnetError(Exception):
    """Base exception for all BACnet backend failures."""
    pass

class BacnetTimeoutError(BacnetError):
    """Raised when a request exceeds its defined timeout."""
    pass

class BacnetRequestError(BacnetError):
    """Raised for network or device-level rejections (e.g., Abort, Reject)."""
    pass

@dataclass(frozen=True)
class DiscoveredDevice:
    instance: int
    address: str

@dataclass(frozen=True)
class ReadResult:
    # Contains a Python primitive (float, int, str, bool) if successful
    value: Any = None
    # Contains a domain-specific error string (e.g., "property-error: unknown-property")
    error: str | None = None

ReadKey = tuple[str, str] # (object_id, property_id)
```

### Backend Protocol

```python
class BacnetBackend(Protocol):
    async def discover_all(self, response_window: float) -> tuple[DiscoveredDevice, ...]:
        """Broadcasts Who-Is and returns unique devices seen within the window."""
        ...

    async def discover_target(self, instance: int, address: str, response_window: float) -> DiscoveredDevice | None:
        """Sends targeted Who-Is to verify a specific device at an address."""
        ...

    async def read(self, address: str, object_id: str, property_id: str) -> Any:
        """
        Reads a single property.
        Returns a primitive Python type.
        Raises BacnetError (or subclasses) on failure.
        """
        ...

    async def read_multiple(self, address: str, points: list[ReadKey]) -> dict[ReadKey, ReadResult]:
        """
        Reads multiple properties.
        Returns a mapping of requested keys to their results (value or property-level error).
        Raises BacnetError for request-level failures (e.g., reject, timeout).
        """
        ...

    def close(self) -> None:
        """Cleans up sockets and background tasks."""
        ...
```

## Important Findings

### Async and Timing Assumptions

*   **`asyncio.timeout` and Timeouts**: The application enforces timeouts tightly via `asyncio.timeout` at the application level (`reads.py` -> `_request` uses `config.request_timeout_seconds`, `scanner.py` -> `_run` uses `config.scan_max_duration_seconds`). Timeouts are also passed explicitly to the BACnet backend methods (`timeout=response_window`, `timeout=response_window + 1` inside `bacnet.py`).
*   **Request Serialization (`asyncio.Lock`)**: The `_BacpypesOwner` class maintains an `asyncio.Lock()` (`self._request_lock`) around *all* underlying BACpypes network calls (`who_is`, `read_property`, `read_property_multiple`). This means the application inherently limits BACpypes to exactly **one outstanding BACnet transaction at a time** globally.
*   **Pacing and Delays**: `ReadScheduler` in `reads.py` implements a global request gate (`self._gate`). It introduces artificial pacing via `asyncio.sleep(delay)` based on `config.inter_request_delay_seconds` (default 250ms). This pacing applies to all reads across all devices.
*   **Batch Sizing**: `ReadScheduler` dynamically resizes RPM batches based on response latency. If a request takes more than 75% of the timeout, the batch size is halved and the delay is increased.
*   **Retries and Fallbacks**: Read batches that fail (e.g., throwing a `READ_ERROR` like Abort) trigger a fallback behavior: the batch is broken into individual `ReadProperty` calls, and the device goes into a bounded exponential cooldown before `ReadPropertyMultiple` is attempted again.

### Threading and Global State

*   **Global State**: `ReadScheduler` keeps global pacing state (`self.devices`), tracking successes, failures, cooldowns, and batch sizes in memory per address.
*   **Threads**: The application operates entirely within a single `asyncio` event loop. There are no background threads doing BACnet polling; it's all cooperatively scheduled via `asyncio.Task` (`operator-bacnet-scan`) and the `Collector.run` polling loop.
*   **Callbacks**: The current design uses `async/await` rather than a callback-driven architecture, except for a few instances interfacing with the UI/system lifecycle (e.g. `dashboard` using `request_scan` callable). The backend should expose `async` methods.

### Implications for Rust Migration

*   **Concurrency limits**: The strict `asyncio.Lock()` serialization in the current Python BACpypes wrapper might exist to work around BACpypes' internal limitations, or it might be protecting fragile destination devices. If the Rust backend supports true concurrent I/O, the application *could* dispatch multiple requests simultaneously. However, doing so requires careful validation to ensure older BACnet IP devices aren't overwhelmed, as the current implementation explicitly protects them via a global lock and 250ms minimum inter-request delay.
*   **Timeout management**: The new backend needs to handle its own I/O timeouts gracefully, but the application will continue to wrap calls in `asyncio.timeout` for safety. The Rust backend must ensure it doesn't leak memory or hang indefinitely if the Python caller cancels the awaitable.
