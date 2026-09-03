"""Shared, paced read scheduling for scans and configured trend collection."""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .bacnet import READ_ERRORS, ReadKey, ReadResult, TrendClient, read_result
from .config import ConsoleConfig


@dataclass
class DevicePacing:
    size: int = 2
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0
    gap: float = 0.0


class ReadScheduler:
    def __init__(self, client: TrendClient, config: ConsoleConfig) -> None:
        self.client = client
        self.config = config
        self.devices: dict[str, DevicePacing] = {}
        self._gate = asyncio.Lock()
        self._next_request = 0.0
        # In-memory totals: no extra BACnet requests or per-read database writes.
        self.requests = 0
        self.request_errors = 0
        self.property_errors = 0
        self.request_seconds = 0.0

    def _state(self, address: str) -> DevicePacing:
        return self.devices.setdefault(address, DevicePacing(
            size=min(2, self.config.rpm_batch_size),
            gap=self.config.inter_request_delay_seconds,
        ))

    async def _request(self, address: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        state = self._state(address)
        # Both scan and collector share this gate in production. Every actual
        # read, including failures and fallback, observes the configured gap.
        async with self._gate:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            started = time.monotonic()
            self.requests += 1
            try:
                async with asyncio.timeout(self.config.request_timeout_seconds):
                    result = await operation()
            except READ_ERRORS:
                self.request_errors += 1
                state.successes = 0
                state.gap = max(self.config.inter_request_delay_seconds, min(5.0, max(.05, state.gap * 2)))
                raise
            else:
                # Slow responses cause smaller future batches and longer gaps.
                if time.monotonic() - started > self.config.request_timeout_seconds * .75:
                    state.size = max(1, state.size // 2)
                    state.successes = 0
                    state.gap = max(self.config.inter_request_delay_seconds, min(5.0, max(.05, state.gap * 2)))
                else:
                    state.gap = max(self.config.inter_request_delay_seconds, state.gap * .9)
                return result
            finally:
                self.request_seconds += time.monotonic() - started
                self._next_request = time.monotonic() + state.gap

    async def one(self, address: str, key: ReadKey) -> ReadResult:
        try:
            result = read_result(await self._request(address, lambda: self.client.read(address, *key)))
        except READ_ERRORS as exc:
            result = ReadResult(error=f"{type(exc).__name__}: {exc}"[:1000])
        if result.error:
            self.property_errors += 1
        return result

    async def many(self, address: str, keys: list[ReadKey]) -> AsyncIterator[tuple[ReadKey, ReadResult]]:
        keys = list(dict.fromkeys(keys))
        state = self._state(address)
        position = 0
        batch_reader = getattr(self.client, "read_multiple", None)
        while position < len(keys):
            can_batch = (callable(batch_reader) and self.config.rpm_batch_size > 1
                         and time.monotonic() >= state.cooldown_until)
            size = max(2, state.size) if can_batch else 1
            chunk = keys[position:position + min(size, self.config.rpm_batch_size)]
            position += len(chunk)
            if len(chunk) == 1:
                yield chunk[0], await self.one(address, chunk[0])
                continue
            try:
                values = await self._request(address, lambda: batch_reader(address, chunk))
                if not isinstance(values, dict):
                    raise ValueError("invalid batch response mapping")
            except READ_ERRORS:
                state.failures += 1
                state.size = max(1, state.size // 2)
                state.successes = 0
                # Remember rejection for this address; don't retry RPM on every
                # batch. Re-probe after bounded exponential cooldown.
                state.cooldown_until = time.monotonic() + min(300, 30 * 2 ** min(state.failures - 1, 4))
                for key in chunk:
                    yield key, await self.one(address, key)
                continue
            missing = False
            for key in chunk:
                if key not in values:
                    missing = True
                    yield key, await self.one(address, key)
                else:
                    result = read_result(values[key])
                    if result.error:
                        self.property_errors += 1
                    yield key, result
            if missing:
                state.successes = 0
                state.size = max(1, state.size // 2)
            else:
                state.failures = 0
                state.successes += 1
                # Gradual growth, capped by configuration. Property-level errors
                # remain per-point errors and don't trigger whole-batch retries.
                if state.successes >= 2:
                    state.size = min(self.config.rpm_batch_size, max(2, state.size * 2))
                    state.successes = 0
