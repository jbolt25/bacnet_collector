import pytest
from bacnet_console.scanner import ScanManager
from bacnet_console.config import load_config
from bacnet_console.db import Store
from bacnet_console.bacnet import ReadResult
from tests.test_scanner import FakeScanClient

class FastScanClient(FakeScanClient):
    def __init__(self):
        super().__init__()
        self.batches = []

    async def read(self, address, obj, prop):
        if prop == 'object-list':
            return [
                ('analog-input', 1),
                ('binary-input', 1),
                ('multi-state-value', 1),
                ('calendar', 1),
                ('device', 1001)
            ]
        elif prop == 'object-name' and obj == 'device,1001':
            return "Test Device"
        return await super().read(address, obj, prop)

    async def read_multiple(self, address, keys):
        self.batches.append(keys)
        return {key: ReadResult(value="test") for key in keys}

@pytest.mark.asyncio
async def test_fast_scan_property_selection(config_file):
    config = load_config(config_file)
    store = Store(config.database_path)
    client = FastScanClient()
    scanner = ScanManager(config, store, client)

    scanner.request_scan('test_fast')
    await scanner.wait()

    all_keys = []
    for batch in client.batches:
        all_keys.extend(batch)

    def get_props_for(obj):
        return [k[1] for k in all_keys if k[0] == obj]

    analog_props = get_props_for('analog-input,1')
    assert 'object-name' in analog_props
    assert 'present-value' in analog_props
    assert 'units' in analog_props
    assert len(analog_props) == 3

    binary_props = get_props_for('binary-input,1')
    assert 'object-name' in binary_props
    assert 'present-value' in binary_props
    assert 'units' not in binary_props
    assert len(binary_props) == 2

    msv_props = get_props_for('multi-state-value,1')
    assert 'object-name' in msv_props
    assert 'present-value' in msv_props
    assert 'units' not in msv_props
    assert len(msv_props) == 2

    cal_props = get_props_for('calendar,1')
    assert 'object-name' in cal_props
    assert 'present-value' not in cal_props
    assert 'units' not in cal_props
    assert len(cal_props) == 1
