import asyncio
from pathlib import Path
import pytest

from bacnet_console.config import _resolve_auto_bind, load_config
from bacnet_console.db import Store
from bacnet_console.scanner import ScanManager, _object_id
from bacnet_console.reads import ReadScheduler
from test_scanner import FakeScanClient


def test_object_id_normalization():
    assert _object_id("analogInput:1") == "analog-input,1"
    assert _object_id("analog-input,1") == "analog-input,1"
    assert _object_id(("analogInput", 2)) == "analog-input,2"


def test_point_re_enable_feature(config_file: Path):
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)

    device_instance = config.devices[0].instance
    point_name = config.devices[0].points[0].name
    point_id = store.status()["approved_devices"][0]["points"][0]["id"]

    assert store.point_enabled(device_instance, point_name) is True
    assert store.delete_point(point_id, actor="operator") is True
    assert store.point_enabled(device_instance, point_name) is False

    assert store.enable_point(point_id, actor="operator") is True
    assert store.point_enabled(device_instance, point_name) is True
    store.close()


@pytest.mark.asyncio
async def test_indexed_object_list_fallback(config_file: Path):
    class IndexedFallbackClient(FakeScanClient):
        async def read(self, address: str, object_id: str, property_id: str):
            if object_id == "device,1001" and property_id == "object-list":
                from bacnet_console.bacnet import ReadResult
                return ReadResult(error="segmented read unsupported")
            if object_id == "device,1001" and property_id == "object-list[0]":
                return 1
            if object_id == "device,1001" and property_id == "object-list[1]":
                return ("analogInput", 1)
            return await super().read(address, object_id, property_id)

    config = load_config(config_file)
    store = Store(config.database_path)
    client = IndexedFallbackClient()
    manager = ScanManager(config, store, client)

    assert manager.request_scan("fallback test") is True
    await manager.wait()

    status = store.status()
    assert status["scans"][0]["status"] == "completed"
    assert status["scans"][0]["point_count"] == 1
    point = status["saved_scans"][0]["devices"][0]["points"][0]
    assert point["object_id"] == "analog-input,1"
    store.close()
