from __future__ import annotations

import asyncio
from typing import Any

from .bacnet import DiscoveredDevice, READ_ERRORS, ReadResult, ScanClient, serialise_value
from .config import ConsoleConfig
from .db import Store
from .reads import ReadScheduler


def _object_id(value: Any) -> str:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        obj_type, instance = str(value[0]), int(value[1])
        try:
            from bacpypes3.primitivedata import ObjectIdentifier
            return str(ObjectIdentifier((obj_type, instance)))
        except (ImportError, ValueError):
            return f"{obj_type},{instance}"
    text = str(value).replace(":", ",")
    if "," not in text:
        raise ValueError(f"invalid object identifier {text!r}")
    try:
        from bacpypes3.primitivedata import ObjectIdentifier
        return str(ObjectIdentifier(text))
    except (ImportError, ValueError):
        return text


def _object_list(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise ValueError("object-list response is not iterable")
    return [_object_id(item) for item in value]


class ScanManager:
    """Runs a scan only when request_scan() is explicitly called."""

    def __init__(self, config: ConsoleConfig, store: Store, client: ScanClient,
                 reader: ReadScheduler | None = None) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.reader = reader or ReadScheduler(client, config)
        self._task: asyncio.Task[None] | None = None
        self._cancel_reason = "scan stopped by operator"

    def request_scan(self, requested_by: str) -> bool:
        if self._task is not None and not self._task.done():
            return False
        self._cancel_reason = "scan stopped by operator"
        self._task = asyncio.create_task(self._run(requested_by), name="operator-bacnet-scan")
        return True

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def cancel(self, reason: str = "scan stopped by operator") -> bool:
        if self._task is None or self._task.done():
            return False
        self._cancel_reason = reason
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def _run(self, requested_by: str) -> None:
        scan_id = self.store.start_scan(requested_by)
        device_count = 0
        point_count = 0
        try:
            async with asyncio.timeout(self.config.scan_max_duration_seconds):
                discovered = {
                    device.instance: device
                    for device in await self.client.discover_all(self.config.scan_timeout_seconds)
                }
                # Manual approved addresses get a targeted Who-Is if broadcast did not find them.
                for approved in self.config.devices:
                    if approved.instance in discovered or not approved.address:
                        continue
                    result = await self.client.discover_target(
                        approved.instance, approved.address, self.config.scan_timeout_seconds
                    )
                    if result is not None:
                        discovered[result.instance] = result
                devices = list(discovered.values())[: self.config.scan_max_devices]
                for device in devices:
                    points = await self._scan_device(scan_id, device)
                    device_count += 1
                    point_count += points
                self.store.finish_scan(scan_id, device_count, point_count)
        except TimeoutError:
            self.store.finish_scan(
                scan_id, device_count, point_count,
                f"scan exceeded maximum duration ({self.config.scan_max_duration_seconds:g} seconds)",
            )
        except asyncio.CancelledError:
            self.store.finish_scan(scan_id, device_count, point_count, self._cancel_reason)
            raise
        except READ_ERRORS as exc:
            self.store.finish_scan(
                scan_id, device_count, point_count, f"{type(exc).__name__}: {exc}"[:1000]
            )

    async def _scan_device(self, scan_id: int, device: DiscoveredDevice) -> int:
        device_id = f"device,{device.instance}"
        name = None
        object_error = None
        result = await self.reader.one(device.address, (device_id, "object-name"))
        if result.error:
            object_error = f"name {result.error}"
        else:
            name = str(result.value)
        try:
            result = await self.reader.one(device.address, (device_id, "object-list"))
            if result.error:
                # Attempt indexed fallback for large object-lists or unsegmented controllers
                count_res = await self.reader.one(device.address, (device_id, "object-list[0]"))
                if not count_res.error and isinstance(count_res.value, int) and count_res.value > 0:
                    indexed_objects = []
                    limit = min(count_res.value, self.config.scan_max_objects_per_device)
                    for idx in range(1, limit + 1):
                        item_res = await self.reader.one(device.address, (device_id, f"object-list[{idx}]"))
                        if not item_res.error and item_res.value:
                            indexed_objects.append(_object_id(item_res.value))
                    objects = indexed_objects
                else:
                    raise ValueError(result.error)
            else:
                objects = _object_list(result.value)
            if not objects:
                raise ValueError("object-list returned no objects")
        except READ_ERRORS as exc:
            objects = []
            list_error = f"object-list unavailable ({type(exc).__name__}): {exc}"[:1000]
            object_error = f"{object_error}; {list_error}" if object_error else list_error
        self.store.scan_device(scan_id, device.instance, device.address, name, object_error)

        count = 0
        objects = list(dict.fromkeys(obj for obj in objects if obj != device_id))

        # Determine relevant properties per object type for FAST scan
        obj_properties: dict[str, list[str]] = {}
        keys = []
        for obj in objects[:self.config.scan_max_objects_per_device]:
            obj_type = obj.split(",")[0]
            if obj_type in ("analog-input", "analog-output", "analog-value"):
                props = ["object-name", "present-value", "units"]
            elif obj_type in ("binary-input", "binary-output", "binary-value", "multi-state-input", "multi-state-output", "multi-state-value"):
                props = ["object-name", "present-value"]
            else:
                props = ["object-name"]

            obj_properties[obj] = props
            for prop in props:
                keys.append((obj, prop))

        pending = {}

        def _persist_point(object_id: str, results: dict[str, ReadResult]):
            named = results.get("object-name", ReadResult(error="no result returned"))
            valued = results.get("present-value", ReadResult(error="no result returned"))
            unit = results.get("units", ReadResult(error="no result returned"))

            point_name = None if named.error else str(named.value)
            value_number, value_text = (None, None) if valued.error else serialise_value(valued.value)
            units = None if unit.error else str(unit.value)

            # For FAST scan mapping, ignore errors for properties we did not request
            # e.g., missing "units" on a binary-input.
            expected = set(obj_properties.get(object_id, []))

            errors = []
            for label, res, prop_id in (("name", named, "object-name"), ("value", valued, "present-value"), ("units", unit, "units")):
                if res.error and prop_id in expected:
                    errors.append(f"{label} {res.error}")

            self.store.scan_point(
                scan_id, device.instance, object_id, point_name, value_text,
                value_number, units, "; ".join(errors) or None,
            )

        async for (object_id, property_id), result in self.reader.many(device.address, keys):
            results = pending.setdefault(object_id, {})
            results[property_id] = result

            expected_props = set(obj_properties[object_id])

            if expected_props <= set(results.keys()):
                # Persist each completed object immediately, even in individual-read
                # fallback, so cancelling later preserves the work already collected.
                _persist_point(object_id, results)
                count += 1
                del pending[object_id]

        # Defensive fallback: Persist any objects that did not get all their properties returned.
        for object_id, results in pending.items():
            _persist_point(object_id, results)
            count += 1

        return count
