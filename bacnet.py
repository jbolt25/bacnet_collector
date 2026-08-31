from __future__ import annotations

import asyncio
from argparse import Namespace
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DiscoveredDevice:
    instance: int
    address: str


class ReadOnlyClient(Protocol):
    async def discover(self, instance: int) -> DiscoveredDevice | None: ...

    async def read(self, address: str, object_id: str, property_id: str) -> Any: ...

    def close(self) -> None: ...


class BacpypesReadOnlyClient:
    """Narrow BACpypes facade exposing only Who-Is and ReadProperty.

    The wider BACpypes application stays private. Collector code receives this
    facade, which intentionally has no mutation or control methods.
    """

    def __init__(self, bind: str, timeout_seconds: float) -> None:
        try:
            from bacpypes3.app import Application
        except ImportError as exc:
            raise RuntimeError("bacpypes3 is not installed; run the setup steps in README.md") from exc

        args = Namespace(
            address=bind,
            name="ReadonlyCollector",
            instance=4190000,
            network=0,
            vendoridentifier=999,
            foreign=None,
            ttl=30,
            bbmd=None,
        )
        self.__app = Application.from_args(args)
        self._timeout = timeout_seconds

    async def discover(self, instance: int) -> DiscoveredDevice | None:
        responses = await asyncio.wait_for(
            self.__app.who_is(low_limit=instance, high_limit=instance),
            timeout=self._timeout + 1,
        )
        if not responses:
            return None
        response = responses[0]
        found_instance = int(response.iAmDeviceIdentifier[1])
        if found_instance != instance:
            return None
        return DiscoveredDevice(found_instance, str(response.pduSource))

    async def read(self, address: str, object_id: str, property_id: str) -> Any:
        value = await asyncio.wait_for(
            self.__app.read_property(address, object_id, property_id),
            timeout=self._timeout,
        )
        if value.__class__.__name__ == "AnyAtomic" and hasattr(value, "get_value"):
            value = value.get_value()
        return value

    def close(self) -> None:
        self.__app.close()


def serialise_value(value: Any) -> tuple[float | None, str]:
    if isinstance(value, bool):
        return None, "true" if value else "false"
    if isinstance(value, (int, float)):
        return float(value), str(value)
    return None, str(value)
