from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

import pytest
from bacpypes3.apdu import ErrorRejectAbortNack

from bacnet_console.bacnet import serialise_value
from bacnet_console.collector import Collector
from bacnet_console.config import ConfigError, load_config
from bacnet_console.dashboard import Dashboard, render_status
from bacnet_console.db import Store
from bacnet_console.scanner import ScanManager
from test_scanner import FakeScanClient


class ProtocolFailure(ErrorRejectAbortNack):
    def __str__(self):
        return 'unknown-property'


class PartialClient(FakeScanClient):
    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()

    async def read(self, address, object_id, property_id):
        if property_id == 'object-list':
            return [('analog-input', 1), ('analog-input', 2)]
        if object_id == 'analog-input,2':
            self.blocked.set()
            await asyncio.Event().wait()
        return await super().read(address, object_id, property_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('deadline', [False, True])
async def test_partial_scan_counts_survive_stop_and_timeout(config_file, deadline):
    config = load_config(config_file)
    if deadline:
        config = replace(config, scan_max_duration_seconds=.05)
    store = Store(config.database_path)
    client = PartialClient()
    manager = ScanManager(config, store, client)
    try:
        manager.request_scan('test')
        await asyncio.wait_for(client.blocked.wait(), 2)
        if deadline:
            await manager.wait()
        else:
            assert await manager.cancel()
            assert not await manager.cancel()
        saved = store.status()['saved_scans'][0]
        assert saved['status'] == 'failed'
        assert saved['device_count'] == 1
        assert saved['point_count'] == len(saved['devices'][0]['points']) == 1
        assert ('maximum duration' if deadline else 'stopped by operator') in saved['error']
    finally:
        await manager.cancel()
        store.close()


@pytest.mark.asyncio
async def test_protocol_error_is_saved_without_killing_scanner_or_collector(config_file):
    class BadClient(FakeScanClient):
        async def read(self, address, object_id, property_id):
            if property_id == 'present-value':
                raise ProtocolFailure()
            return await super().read(address, object_id, property_id)

    config = load_config(config_file)
    store = Store(config.database_path)
    try:
        store.register_config(config.devices)
        client = BadClient()
        manager = ScanManager(config, store, client)
        manager.request_scan('test')
        await manager.wait()
        assert 'unknown-property' in store.status()['saved_scans'][0]['devices'][0]['points'][0]['read_error']
        await Collector(config, store, client).poll_once()
        assert 'unknown-property' in store.status()['approved_devices'][0]['points'][0]['last_error']
    finally:
        store.close()


@pytest.mark.asyncio
async def test_disabled_point_stays_disabled_after_restart_and_is_not_read(config_file):
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    scan = store.start_scan('test')
    store.scan_device(scan, 1001, '192.168.50.41', 'AHU', None)
    store.finish_scan(scan, 1, 0)
    point = store.status()['approved_devices'][0]['points'][0]
    store.delete_point(point['id'])
    store.close()
    store = Store(config.database_path)
    try:
        store.register_config(config.devices)
        client = FakeScanClient()
        await Collector(config, store, client).poll_once()
        assert client.reads == []
        assert store.status()['approved_devices'][0]['points'] == []
    finally:
        store.close()


def test_scan_history_is_not_silently_pruned_and_live_view_is_small(tmp_path):
    store = Store(tmp_path / 'history.sqlite3')
    try:
        for _ in range(52):
            scan = store.start_scan('test')
            store.finish_scan(scan, 0, 0)
        assert len(store.status()['scans']) == 52
        assert len(store.status(live_only=True)['scans']) == 1
        assert store.status(live_only=True)['scans'][0]['id'] == scan
    finally:
        store.close()


def test_aliases_do_not_duplicate_scan_points_and_csv_is_safe(config_file):
    config = load_config(config_file)
    device = config.devices[0]
    device = replace(device, points=(*device.points, replace(device.points[0], name='Alias')))
    store = Store(config.database_path)
    try:
        store.register_config((device,))
        scan = store.start_scan('test')
        store.scan_device(scan, 1001, '192.168.50.41', '=1+1', None)
        store.scan_point(scan, 1001, 'analog-input,1', '+1+1', '72', 72, 'F', None)
        store.finish_scan(scan, 0, 0)
        points = store.status()['saved_scans'][0]['devices'][0]['points']
        assert len(points) == 1
        rows = list(csv.reader(io.StringIO(store.points_csv())))
        assert len(rows) == 2
        assert rows[1][3] == "'=1+1"
        assert rows[1][5] == "'+1+1"
        assert store.delete_scan_point(points[0]['id'])
        assert store.status()['scans'][0]['point_count'] == 0
    finally:
        store.close()


def test_all_local_actions_reject_wrong_and_unicode_csrf_tokens(config_file):
    config = load_config(config_file)
    store = Store(config.database_path)
    server = Dashboard(store, '127.0.0.1', 0, 900, lambda _: pytest.fail('unexpected scan'))
    server.start()
    try:
        for endpoint in ('rescan', 'rescan/cancel', 'points.csv', 'scan/rename',
                         'scan/delete', 'point/delete', 'scan-point/delete'):
            for token in ('wrong', '\u2603'):
                connection = HTTPConnection('127.0.0.1', server.bound_port, timeout=2)
                connection.request('POST', '/api/' + endpoint, urlencode({'csrf_token': token}))
                response = connection.getresponse()
                assert response.status == 403
                response.read()
                connection.close()
        connection = HTTPConnection('127.0.0.1', server.bound_port, timeout=2)
        connection.request('POST', '/api/rescan', headers={'Content-Length': 'invalid'})
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.close()
    finally:
        server.close()
        store.close()


@pytest.mark.parametrize('value', ['.nan', '.inf', '-.inf'])
def test_nonfinite_deadline_rejected(config_file, value):
    content = config_file.read_text()
    config_file.write_text(content.replace('scan:\n', f'scan:\n  max_duration_seconds: {value}\n'))
    with pytest.raises(ConfigError):
        load_config(config_file)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_reading_has_json_safe_numeric_value(value):
    assert serialise_value(value)[0] is None


def test_render_does_not_substitute_tokens_inside_controller_names(tmp_path):
    store = Store(tmp_path / 'template.sqlite3')
    try:
        scan = store.start_scan('test')
        store.scan_device(scan, 1, '192.0.2.1', '{{AUDIT}}', None)
        store.finish_scan(scan, 1, 0)
        page = render_status(store.status(), 900, 'token')
        assert '{{AUDIT}}' in page
    finally:
        store.close()


def test_assets_resolve_from_installed_prefix_when_cwd_is_elsewhere(tmp_path, monkeypatch):
    from bacnet_console.dashboard import _asset_root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('bacnet_console.dashboard.sys.prefix', str(tmp_path / 'venv'))
    assert _asset_root(None) == tmp_path / 'venv/share/bacnet-console'


def test_restart_repairs_old_partial_counts_without_losing_data(tmp_path):
    store = Store(tmp_path / 'recovery.sqlite3')
    scan = store.start_scan('test')
    store.scan_device(scan, 1, '192.0.2.1', 'Device', None)
    store.scan_point(scan, 1, 'analog-input,1', 'Point', '42', 42, None, None)
    with store._db:
        store._db.execute('UPDATE scans SET device_count=0,point_count=0 WHERE id=?', (scan,))
    store.close()
    store = Store(tmp_path / 'recovery.sqlite3')
    try:
        store.start()
        saved = store.status()['saved_scans'][0]
        assert saved['device_count'] == saved['point_count'] == 1
        assert len(saved['devices'][0]['points']) == 1
        assert saved['status'] == 'failed'
        assert 'restarted' in saved['error']
    finally:
        store.close()


def test_example_configuration_validates_offline(monkeypatch):
    from ipaddress import IPv4Interface
    monkeypatch.setattr('bacnet_console.config._resolve_auto_bind',
                        lambda: IPv4Interface('192.168.50.10/24'))
    config = load_config(Path('config.example.yaml'))
    assert config.devices == ()


def test_json_scan_actions_do_not_redirect_to_full_dashboard(tmp_path):
    store = Store(tmp_path / 'api.sqlite3')
    server = Dashboard(store, '127.0.0.1', 0, 900, lambda _: True, lambda: True)
    server.start()
    try:
        for endpoint, expected in (('rescan', 202), ('rescan/cancel', 200)):
            connection = HTTPConnection('127.0.0.1', server.bound_port, timeout=2)
            connection.request('POST', '/api/' + endpoint,
                               urlencode({'csrf_token': server.csrf_token}),
                               {'Accept': 'application/json'})
            response = connection.getresponse()
            assert response.status == expected
            assert response.read() == b'{"accepted":true}'
            connection.close()
    finally:
        server.close()
        store.close()
