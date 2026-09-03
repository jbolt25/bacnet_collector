from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bacnet_console.bacnet import DiscoveredDevice
from bacnet_console.config import load_config
from bacnet_console.db import Store
from bacnet_console.scanner import ScanManager


class FakeScanClient:
    def __init__(self) -> None:
        self.discovery_calls = 0
        self.target_calls = 0
        self.reads: list[tuple[str, str, str]] = []

    async def discover_all(self, response_window: float) -> tuple[DiscoveredDevice, ...]:
        self.discovery_calls += 1
        return (DiscoveredDevice(1001, "192.168.50.41"),)

    async def discover_target(self, instance: int, address: str, response_window: float):
        self.target_calls += 1
        return DiscoveredDevice(instance, address)

    async def read(self, address: str, object_id: str, property_id: str):
        self.reads.append((address, object_id, property_id))
        values = {
            ("device,1001", "object-name"): "AHU-1",
            ("device,1001", "object-list"): [("device", 1001), ("analog-input", 1)],
            ("analog-input,1", "object-name"): "Supply Air Temp",
            ("analog-input,1", "present-value"): 72.5,
            ("analog-input,1", "units"): "degrees-fahrenheit",
        }
        return values[(object_id, property_id)]

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_constructing_manager_does_not_scan(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    client = FakeScanClient()
    ScanManager(config, store, client)
    await asyncio.sleep(0)
    assert client.discovery_calls == 0
    assert store.status()["scans"] == []
    store.close()


@pytest.mark.asyncio
async def test_explicit_scan_persists_devices_points_and_history(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    client = FakeScanClient()
    manager = ScanManager(config, store, client)
    assert manager.request_scan("192.168.50.20") is True
    assert manager.request_scan("192.168.50.21") is False
    await manager.wait()
    status = store.status()
    assert client.discovery_calls == 1
    assert status["scans"][0]["status"] == "completed"
    assert status["scans"][0]["device_count"] == 1
    assert status["discovered_devices"][0]["approved"] == 1
    point = status["discovered_devices"][0]["points"][0]
    assert point["object_id"] == "analog-input,1"
    assert point["value_number"] == 72.5
    assert point["approved"] == 1
    assert store.address_for(1001) == "192.168.50.41"
    store.close()

