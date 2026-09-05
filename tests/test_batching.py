"""Exercise production wiring and the pinned BACpypes parser without sockets."""
import asyncio
from dataclasses import replace

import pytest
from bacpypes3.apdu import ReadPropertyMultipleACK
from bacpypes3.basetypes import (
    ErrorType, PropertyReference, ReadAccessResult, ReadAccessResultElement,
    ReadAccessResultElementChoice,
)
from bacpypes3.constructeddata import Any
from bacpypes3.primitivedata import ObjectIdentifier, Real
from bacpypes3.service.object import ReadWritePropertyMultipleServices
from bacpypes3.vendor import get_vendor_info
from bacpypes3.object import AnalogInputObject  # registers standard object types

from bacnet_console.bacnet import BacpypesScanClient, ReadResult, TrendOnlyView
from bacnet_console.collector import Collector
from bacnet_console.config import load_config
from bacnet_console.db import Store
from bacnet_console.reads import ReadScheduler
from bacnet_console.scanner import ScanManager
from test_scanner import FakeScanClient


class ParserApp:
    """Actual library parsing and decoding, only its network request is replaced."""
    read_property_multiple = ReadWritePropertyMultipleServices.read_property_multiple

    def __init__(self):
        self.requests = []

    async def get_vendor_info(self, **kwargs):
        return get_vendor_info(0)

    async def parse_object_identifier(self, value, **kwargs):
        return ObjectIdentifier(value)

    async def parse_property_reference(self, value, **kwargs):
        return PropertyReference(propertyIdentifier=value)

    async def request(self, request):
        self.requests.append(request)
        results = []
        for spec in request.listOfReadAccessSpecs:
            entries = []
            for prop in spec.listOfPropertyReferences:
                if str(prop.propertyIdentifier) == 'present-value':
                    value = Any(Real(72.5))
                    choice = ReadAccessResultElementChoice(propertyValue=value)
                else:
                    choice = ReadAccessResultElementChoice(propertyAccessError=ErrorType(
                        errorClass='property', errorCode='unknown-property'))
                entries.append(ReadAccessResultElement(
                    propertyIdentifier=prop.propertyIdentifier, readResult=choice))
            results.append(ReadAccessResult(objectIdentifier=spec.objectIdentifier, listOfResults=entries))
        return ReadPropertyMultipleACK(listOfReadAccessResults=results)


@pytest.mark.asyncio
async def test_real_parser_request_shape_and_per_property_errors():
    # Avoid constructor: it would open an actual BACnet transport.
    client = object.__new__(BacpypesScanClient)
    app = ParserApp()
    client._BacpypesOwner__app = app
    client._request_lock = asyncio.Lock()
    client._timeout = 1
    result = await TrendOnlyView(client).read_multiple('192.0.2.1', [
        ('analog-input,1', 'present-value'), ('analog-input,1', 'description'),
        ('analog-input,2', 'present-value'),
    ])
    assert len(app.requests) == 1
    assert len(app.requests[0].listOfReadAccessSpecs) == 2
    assert result['analog-input,1', 'present-value'].value == 72.5
    assert result['analog-input,2', 'present-value'].error is None
    assert 'unknown-property' in result['analog-input,1', 'description'].error


class BatchClient(FakeScanClient):
    def __init__(self, fail=False):
        super().__init__()
        self.batches = []
        self.fail = fail

    async def read_multiple(self, address, keys):
        self.batches.append(list(keys))
        if self.fail:
            raise TimeoutError('batch timeout')
        return {key: ReadResult(value=71) for key in keys}


def two_point_config(config_file):
    config = load_config(config_file)
    device = config.devices[0]
    return replace(config, devices=(replace(device, address='192.0.2.1', points=(
        device.points[0], replace(device.points[0], name='Second', object_id='analog-input,2'),
    )),))


@pytest.mark.asyncio
async def test_production_facade_and_collector_do_batch(config_file):
    config = two_point_config(config_file)
    store = Store(config.database_path)
    try:
        store.register_config(config.devices)
        transport = BatchClient()
        facade = TrendOnlyView(transport)
        reader = ReadScheduler(facade, config)
        await Collector(config, store, facade, reader).poll_once()
        assert len(transport.batches) == 1
        assert transport.reads == []
        assert [p['last_value_number'] for p in store.status()['approved_devices'][0]['points']] == [71, 71]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_collector_records_mixed_batch_error_once(config_file):
    class Mixed(BatchClient):
        async def read_multiple(self, address, keys):
            return {keys[0]: ReadResult(value=71), keys[1]: ErrorType(
                errorClass='property', errorCode='unknown-property')}
    config = two_point_config(config_file)
    store = Store(config.database_path)
    try:
        store.register_config(config.devices)
        await Collector(config, store, TrendOnlyView(Mixed())).poll_once()
        points = store.status()['approved_devices'][0]['points']
        good = next(p for p in points if p['name'] == 'SAT')
        bad = next(p for p in points if p['name'] == 'Second')
        assert good['last_value_number'] == 71
        assert bad['last_success_at'] is None
        assert bad['consecutive_errors'] == 1
        assert 'unknown-property' in bad['last_error']
    finally:
        store.close()


@pytest.mark.asyncio
async def test_paced_fallback_cooldown_and_error_not_duplicated(config_file, monkeypatch):
    # Fake only the scheduler's clock/sleep, so pacing is deterministic and fast.
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0.)
    async def sleep(delay):
        clock.now += delay
    monkeypatch.setattr('bacnet_console.reads.time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr('bacnet_console.reads.asyncio', SimpleNamespace(
        Lock=asyncio.Lock, timeout=asyncio.timeout, sleep=sleep))
    events = []
    class Rejecting(BatchClient):
        async def read_multiple(self, address, keys):
            events.append(('batch', clock.now))
            # Raise an unsupported error so it falls back to individual reads
            raise ValueError("unsupported service")
        async def read(self, address, obj, prop):
            events.append(('read', clock.now))
            if obj == 'analog-input,1':
                raise ValueError('specific point error')
            return 72
    config = replace(load_config(config_file), inter_request_delay_seconds=.25)
    reader = ReadScheduler(Rejecting(fail=True), config)
    keys = [(f'analog-input,{i}', 'present-value') for i in range(4)]
    results = [result async for _, result in reader.many('192.0.2.1', keys)]
    assert len(results) == 4
    assert 'specific point error' in results[1].error
    assert [kind for kind, _ in events] == ['batch', 'read', 'read', 'read', 'read']
    assert all(b[1] - a[1] >= .25 for a, b in zip(events, events[1:]))
    assert reader.property_errors == 1
    assert reader.devices['192.0.2.1'].cooldown_until > clock.now


@pytest.mark.asyncio
async def test_batches_ramp_within_limit(config_file):
    config = replace(load_config(config_file), rpm_batch_size=8)
    client = BatchClient()
    reader = ReadScheduler(client, config)
    keys = [(f'analog-input,{i}', 'present-value') for i in range(28)]
    results = [item async for item in reader.many('192.0.2.1', keys)]
    sizes = [len(chunk) for chunk in client.batches]
    assert sizes == [2, 2, 4, 4, 8, 8]
    assert len(results) == 28
    assert reader.requests == 6


@pytest.mark.asyncio
async def test_missing_batch_entry_retries_only_missing_property(config_file):
    class Incomplete(BatchClient):
        async def read_multiple(self, address, keys):
            return {keys[0]: ReadResult(value=71)}

        async def read(self, address, obj, prop):
            self.reads.append((address, obj, prop))
            return 72

    client = Incomplete()
    reader = ReadScheduler(client, load_config(config_file))
    keys = [('analog-input,1', 'present-value'), ('analog-input,2', 'present-value')]
    results = dict([item async for item in reader.many('192.0.2.1', keys)])
    assert results[keys[0]].value == 71
    assert results[keys[1]].value == 72
    assert client.reads == [('192.0.2.1', *keys[1])]
    assert reader.devices['192.0.2.1'].successes == 0


@pytest.mark.asyncio
async def test_operator_scan_batches_metadata_and_retains_property_errors(config_file):
    class ScanBatch(FakeScanClient):
        def __init__(self):
            super().__init__()
            self.batches = []
        async def read_multiple(self, address, keys):
            self.batches.append(keys)
            return {key: (ReadResult(error='unknown-property') if key[1] == 'present-value'
                          else ReadResult(value='Supply Air Temp')) for key in keys}
        async def read(self, address, obj, prop):
            if prop == 'object-list':
                return [('analog-input', 1)]
            elif prop == 'object-name' and obj == 'device,1001':
                return "Device Name"
            return await super().read(address, obj, prop)
    config = load_config(config_file)
    store = Store(config.database_path)
    try:
        client = ScanBatch()
        scanner = ScanManager(config, store, client)
        scanner.request_scan('offline test')
        await scanner.wait()
        saved = store.status()['saved_scans'][0]
        assert client.batches
        point = saved['devices'][0]['points'][0]
        assert 'unknown-property' in point['read_error']
        assert point['value_number'] is None
        assert saved['point_count'] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancel_pending_batch_keeps_previously_committed_points(config_file):
    class Hanging(FakeScanClient):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
        async def read(self, address, obj, prop):
            if prop == 'object-list':
                return [('analog-input', 1), ('analog-input', 2), ('analog-input', 3)]
            elif prop == 'object-name' and obj == 'device,1001':
                return "Device Name"
            return await super().read(address, obj, prop)
        async def read_multiple(self, address, keys):
            if any(k[0] == 'analog-input,3' and k[1] == 'object-name' for k in keys):
                self.entered.set()
                await asyncio.Event().wait()
            return {key: ReadResult(value=72) for key in keys}
    config = load_config(config_file)
    store = Store(config.database_path)
    scanner = ScanManager(config, store, Hanging())
    try:
        scanner.request_scan('test')
        await asyncio.wait_for(scanner.client.entered.wait(), 2)
        await asyncio.sleep(0.1)
        assert await scanner.cancel()
        saved = store.status()['saved_scans'][0]
        assert saved['point_count'] >= 1
        assert saved['error'] == 'scan stopped by operator'
    finally:
        await scanner.cancel()
        store.close()
