from __future__ import annotations

import asyncio
import logging
import time

from .bacnet import TrendClient, serialise_value
from .config import ConsoleConfig
from .db import Store
from .reads import ReadScheduler


LOG = logging.getLogger(__name__)


class Collector:
    def __init__(self, config: ConsoleConfig, store: Store, client: TrendClient,
                 reader: ReadScheduler | None = None) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.reader = reader or ReadScheduler(client, config)

    async def poll_once(self) -> None:
        self.store.cycle_started()
        for device in self.config.devices:
            if not device.points:
                continue
            address = self.store.address_for(device.instance)
            if not address:
                self.store.device_error(
                    device.instance, "collector", "no address: configure one or run an operator scan"
                )
                continue
            points = [point for point in device.points
                      if self.store.point_enabled(device.instance, point.name)]
            by_key = {}
            for point in points:
                by_key.setdefault((point.object_id, point.property_id), []).append(point)
            async for key, result in self.reader.many(address, list(by_key)):
                # Aliases share one network read but keep their own stored history.
                for point in by_key[key]:
                    if result.error:
                        self.store.point_error(device.instance, point.name, result.error)
                    else:
                        number, text = serialise_value(result.value)
                        self.store.point_result(device.instance, point.name, number, text)
        self.store.cycle_finished()

    async def run(self, stop: asyncio.Event) -> None:
        self.store.start()
        next_poll = time.monotonic()
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                LOG.exception("collector cycle failed")
                self.store.set_fatal(f"{type(exc).__name__}: {exc}")
            # A slow cycle must not trigger back-to-back catch-up reads.
            next_poll = max(next_poll + self.config.poll_interval_seconds,
                            time.monotonic() + self.config.poll_interval_seconds)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0, next_poll - time.monotonic()))
            except TimeoutError:
                pass
