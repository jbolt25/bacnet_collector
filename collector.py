from __future__ import annotations

import asyncio
import logging
import time

from .bacnet import ReadOnlyClient, serialise_value
from .config import CollectorConfig, DeviceConfig
from .db import Store


LOG = logging.getLogger(__name__)


class Collector:
    def __init__(self, config: CollectorConfig, store: Store, client: ReadOnlyClient) -> None:
        self.config = config
        self.store = store
        self.client = client
        self._last_discovery: dict[int, float] = {}

    async def _resolve(self, device: DeviceConfig) -> str | None:
        if device.address:
            return device.address
        existing = self.store.address_for(device.instance)
        now = time.monotonic()
        last = self._last_discovery.get(device.instance, 0)
        if existing and now - last < self.config.discovery_interval_seconds:
            return existing
        self._last_discovery[device.instance] = now
        try:
            discovered = await self.client.discover(device.instance)
        except Exception as exc:
            self.store.device_error(device.instance, "discovery", f"{type(exc).__name__}: {exc}")
            return existing
        if discovered is None:
            self.store.device_error(device.instance, "discovery", "approved device did not answer targeted Who-Is")
            return existing
        self.store.discovered(device.instance, discovered.address)
        return discovered.address

    async def poll_once(self) -> None:
        self.store.cycle_started()
        for device in self.config.devices:
            address = await self._resolve(device)
            if not address:
                continue
            for point in device.points:
                try:
                    value = await self.client.read(address, point.object_id, point.property_id)
                    number, text = serialise_value(value)
                    self.store.point_result(device.instance, point.name, number, text)
                except Exception as exc:
                    self.store.point_error(device.instance, point.name, f"{type(exc).__name__}: {exc}")
                if self.config.inter_request_delay_seconds:
                    await asyncio.sleep(self.config.inter_request_delay_seconds)
        self.store.cycle_finished()

    async def run(self, stop: asyncio.Event) -> None:
        self.store.start()
        next_poll = time.monotonic()
        prune_counter = 0
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                LOG.exception("collector cycle failed")
                self.store.set_fatal(f"{type(exc).__name__}: {exc}")
            prune_counter += 1
            if prune_counter >= 1440:
                self.store.prune(self.config.retention_days)
                prune_counter = 0
            next_poll += self.config.poll_interval_seconds
            delay = max(0, next_poll - time.monotonic())
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
