from __future__ import annotations

import asyncio
import math
from argparse import Namespace
from dataclasses import dataclass
from typing import Any, Protocol

from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.basetypes import ErrorType
from bacpypes3.primitivedata import ObjectIdentifier, PropertyIdentifier


READ_ERRORS = (Exception, ErrorRejectAbortNack)
ReadKey = tuple[str, str]


@dataclass(frozen=True)
class ReadResult:
    """A property failure is data about the read, never a successful value."""

    value: Any = None
    error: str | None = None


def read_result(value: Any) -> ReadResult:
    if isinstance(value, ReadResult):
        return value
    if isinstance(value, ErrorType):
        return ReadResult(error=f"{value.errorClass}: {value.errorCode}"[:1000])
    if isinstance(value, ErrorRejectAbortNack):
        return ReadResult(error=f"{type(value).__name__}: {value}"[:1000])
    if value is None:
        return ReadResult(error="property response could not be decoded")
    if value.__class__.__name__ == "AnyAtomic" and hasattr(value, "get_value"):
        value = value.get_value()
    return ReadResult(value=value)


@dataclass(frozen=True)
class DiscoveredDevice:
    instance: int
    address: str


class TrendClient(Protocol):
    async def read(self, address: str, object_id: str, property_id: str) -> Any: ...
    async def read_multiple(self, address: str, points: list[ReadKey]) -> dict[ReadKey, ReadResult]: ...


class ScanClient(Protocol):
    async def discover_all(self, response_window: float) -> tuple[DiscoveredDevice, ...]: ...
    async def discover_target(
        self, instance: int, address: str, response_window: float
    ) -> DiscoveredDevice | None: ...
    async def read(self, address: str, object_id: str, property_id: str) -> Any: ...
    async def read_multiple(self, address: str, points: list[ReadKey]) -> dict[ReadKey, ReadResult]: ...
    def close(self) -> None: ...


class _BacpypesOwner:
    def __init__(self, bind: str, timeout_seconds: float) -> None:
        try:
            from bacpypes3.app import Application
            from bacpypes3.pdu import Address
        except ImportError as exc:
            raise RuntimeError("bacpypes3 is not installed; follow README.md") from exc
        args = Namespace(
            address=bind,
            name="ReadonlyConsole",
            instance=4190000,
            network=0,
            vendoridentifier=999,
            foreign=None,
            ttl=30,
            bbmd=None,
        )
        self.__app = Application.from_args(args)
        self._address_type = Address
        self._timeout = timeout_seconds
        self._request_lock = asyncio.Lock()

    @property
    def _app(self) -> Any:
        return self.__app

    async def _read(self, address: str, object_id: str, property_id: str) -> Any:
        async with self._request_lock:
            value = await asyncio.wait_for(
                self.__app.read_property(address, object_id, property_id), timeout=self._timeout
            )
        if value.__class__.__name__ == "AnyAtomic" and hasattr(value, "get_value"):
            value = value.get_value()
        return value

    async def _read_multiple(self, address: str, points: list[ReadKey]) -> dict[ReadKey, ReadResult]:
        # BACpypes 0.0.106 consumes an alternating flat list, despite its type hint.
        # Group properties of the same object to reduce request overhead.
        grouped: dict[str, list[str]] = {}
        requested = {}
        for object_id, property_id in points:
            grouped.setdefault(object_id, []).append(property_id)
            requested[(ObjectIdentifier(object_id), PropertyIdentifier(property_id))] = (object_id, property_id)
        parameters: list[Any] = []
        for object_id, properties in grouped.items():
            parameters.extend([object_id, properties])
        async with self._request_lock:
            response = await asyncio.wait_for(
                self.__app.read_property_multiple(address, parameters), timeout=self._timeout
            )
        if isinstance(response, ErrorRejectAbortNack):
            raise response
        if response is None:
            raise ValueError("invalid ReadPropertyMultiple response")
        values: dict[ReadKey, ReadResult] = {}
        for object_id, property_id, _array_index, value in response:
            # Match canonical protocol identifiers back to the caller's spelling.
            key = requested.get((ObjectIdentifier(object_id), PropertyIdentifier(property_id)))
            if key is not None and _array_index is None:
                values[key] = read_result(value)
        return values

    def close(self) -> None:
        self.__app.close()


class BacpypesScanClient(_BacpypesOwner):
    """Operator-scan facade: Who-Is, ReadProperty and ReadPropertyMultiple only."""

    @staticmethod
    def _responses(values: list[Any], limit: int | None = None) -> tuple[DiscoveredDevice, ...]:
        unique: dict[int, DiscoveredDevice] = {}
        for response in values:
            instance = int(response.iAmDeviceIdentifier[1])
            unique[instance] = DiscoveredDevice(instance, str(response.pduSource))
            if limit and len(unique) >= limit:
                break
        return tuple(unique.values())

    async def discover_all(self, response_window: float) -> tuple[DiscoveredDevice, ...]:
        async with self._request_lock:
            responses = await asyncio.wait_for(
                self._app.who_is(timeout=response_window), timeout=response_window + 1
            )
        return self._responses(responses)

    async def discover_target(
        self, instance: int, address: str, response_window: float
    ) -> DiscoveredDevice | None:
        async with self._request_lock:
            responses = await asyncio.wait_for(
                self._app.who_is(
                    low_limit=instance, high_limit=instance,
                    address=self._address_type(address), timeout=response_window
                ),
                timeout=response_window + 1,
            )
        devices = self._responses(responses)
        return next((device for device in devices if device.instance == instance), None)

    async def read(self, address: str, object_id: str, property_id: str) -> Any:
        return await self._read(address, object_id, property_id)

    async def read_multiple(self, address: str, points: list[ReadKey]) -> dict[ReadKey, ReadResult]:
        return await self._read_multiple(address, points)


class TrendOnlyView:
    """Capability-limited view used by normal collection; discovery is absent."""

    def __init__(self, client: ScanClient) -> None:
        self.__client = client

    async def read(self, address: str, object_id: str, property_id: str) -> Any:
        return await self.__client.read(address, object_id, property_id)

    async def read_multiple(self, address: str, points: list[ReadKey]) -> dict[ReadKey, ReadResult]:
        return await self.__client.read_multiple(address, points)


def serialise_value(value: Any) -> tuple[float | None, str]:
    if isinstance(value, bool):
        return None, "true" if value else "false"
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None, str(value)
    return None, str(value)
