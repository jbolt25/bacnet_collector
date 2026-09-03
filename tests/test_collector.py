from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bacnet_console.collector import Collector
from bacnet_console.config import load_config
from bacnet_console.db import Store


class FakeTrendClient:
    def __init__(self) -> None:
        self.reads: list[tuple[str, str, str]] = []

    async def read(self, address: str, object_id: str, property_id: str):
        self.reads.append((address, object_id, property_id))
        return 70.25


class BatchTrendClient(FakeTrendClient):
    def __init__(self):
        super().__init__()
        self.batches = []

    async def read_multiple(self, address, points):
        self.batches.append((address, points))
        return {key: 70.25 for key in points}


@pytest.mark.asyncio
async def test_collection_never_discovers_and_waits_for_address(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    client = FakeTrendClient()
    collector = Collector(config, store, client)
    await collector.poll_once()
    assert client.reads == []
    assert "no address" in store.status()["approved_devices"][0]["last_error"]

    scan_id = store.start_scan("test")
    store.scan_device(scan_id, 1001, "192.168.50.41", "AHU-1", None)
    store.finish_scan(scan_id, 1, 0)
    await collector.poll_once()
    assert client.reads == [("192.168.50.41", "analog-input,1", "present-value")]
    point = store.status()["approved_devices"][0]["points"][0]
    assert point["last_value_number"] == 70.25
    store.close()


@pytest.mark.asyncio
async def test_empty_allowlist_cycle_is_healthy(tmp_path: Path, config_file: Path) -> None:
    text = config_file.read_text(encoding="utf-8")
    start = text.index("\ndevices:\n")
    text = text[: start + 1] + "devices: []\n"
    config_file.write_text(text, encoding="utf-8")
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    await Collector(config, store, FakeTrendClient()).poll_once()
    assert store.status()["collector"]["cycles_completed"] == 1
    store.close()


@pytest.mark.asyncio
async def test_collection_uses_bounded_read_multiple_batches(config_file: Path) -> None:
    config = load_config(config_file)
    config = replace(config, devices=(replace(
        config.devices[0],
        points=(*config.devices[0].points,
                replace(config.devices[0].points[0], name='SAT-2', object_id='analog-input,2')),
    ),))
    store = Store(config.database_path)
    store.register_config(config.devices)
    scan_id = store.start_scan('test')
    store.scan_device(scan_id, 1001, '192.168.50.41', 'AHU-1', None)
    store.finish_scan(scan_id, 1, 0)
    client = BatchTrendClient()
    await Collector(config, store, client).poll_once()
    assert len(client.batches) == 1
    assert len(client.batches[0][1]) == 2
    assert client.reads == []
    assert store.status()['approved_devices'][0]['points'][0]['last_value_number'] == 70.25
    store.close()
