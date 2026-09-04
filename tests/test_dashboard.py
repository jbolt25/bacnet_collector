from __future__ import annotations

from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

from bacnet_console.config import load_config
from bacnet_console.dashboard import Dashboard
from bacnet_console.db import Store


def _request(port: int, method: str, path: str, body: str | None = None):
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, dict(response.getheaders()), payload)
    connection.close()
    return result


def test_dashboard_requires_token_and_only_rescan_is_action(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    requests: list[str] = []
    cancelled: list[bool] = []
    dashboard = Dashboard(
        store, "127.0.0.1", 0, config.stale_after_seconds,
        lambda by: not requests.append(by), lambda: not cancelled.append(True),
    )
    dashboard.start()
    try:
        status, _, page = _request(dashboard.bound_port, "GET", "/")
        assert status == 200
        assert b"Run operator scan now" in page
        assert requests == []

        status, _, _ = _request(dashboard.bound_port, "POST", "/api/rescan", "csrf_token=wrong")
        assert status == 403
        assert requests == []

        body = urlencode({"csrf_token": dashboard.csrf_token})
        status, headers, _ = _request(dashboard.bound_port, "POST", "/api/rescan", body)
        assert status == 303
        assert headers["Location"] == "/"
        assert requests == ["127.0.0.1"]

        body = urlencode({"csrf_token": dashboard.csrf_token})
        status, headers, _ = _request(dashboard.bound_port, "POST", "/api/rescan/cancel", body)
        assert status == 303
        assert headers["Location"] == "/"
        assert cancelled == [True]

        status, headers, _ = _request(dashboard.bound_port, "DELETE", "/api/status")
        assert status == 405
        assert headers["Allow"] == "GET"
    finally:
        dashboard.close()
        store.close()


def test_dashboard_escapes_discovered_values(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)

    # 1. Device name XSS
    scan = store.start_scan("<img src=x onerror=alert('scan_name')>")
    store.scan_device(scan, 1001, "192.168.50.41", "<script>alert(1)</script>", None)

    # 2. Point name/value XSS
    store.scan_point(scan, 1001, "analog-input,1", "\"'><script>alert('point')</script>", "<iframe src=x>", 0, "F", None)

    # 3. Error string XSS
    store.scan_device(scan, 1002, "192.168.50.42", "Device 2", "<b onmouseover=alert(1)>error</b>")

    store.finish_scan(scan, 2, 1)

    dashboard = Dashboard(store, "127.0.0.1", 0, config.stale_after_seconds, lambda _: True)
    dashboard.start()
    try:
        _, _, page = _request(dashboard.bound_port, "GET", "/")

        # Check raw payloads aren't present
        assert b"<script>alert(1)</script>" not in page
        assert b"<img src=x onerror=alert('scan_name')>" not in page
        assert b"\"'><script>alert('point')</script>" not in page
        assert b"<iframe src=x>" not in page
        assert b"<b onmouseover=alert(1)>error</b>" not in page

        # Check escaped variations are present
        assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in page
        assert b"&lt;img src=x onerror=alert(&#39;scan_name&#39;)&gt;" in page
        assert b"&#34;&#39;&gt;&lt;script&gt;alert(&#39;point&#39;)&lt;/script&gt;" in page
        assert b"&lt;iframe src=x&gt;" in page
        assert b"&lt;b onmouseover=alert(1)&gt;error&lt;/b&gt;" in page

    finally:
        dashboard.close()
        store.close()


def test_saved_scan_page_and_scan_point_delete_are_local_token_protected(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    scan_id = store.start_scan("operator")
    store.scan_device(scan_id, 1001, "192.168.50.41", "AHU", None)
    store.scan_point(scan_id, 1001, "analog-input,1", "SAT", "72", 72, "F", None)
    store.finish_scan(scan_id, 1, 1)
    point_id = store.status()["saved_scans"][0]["devices"][0]["points"][0]["id"]
    dashboard = Dashboard(store, "127.0.0.1", 0, config.stale_after_seconds, lambda _: True)
    dashboard.start()
    try:
        _, _, page = _request(dashboard.bound_port, "GET", "/")
        assert b"Saved scan results" in page
        assert b"SAT" in page
        assert b'id="scan-form"' in page
        assert b'id="scan-live"' in page
        assert b'src="/static/dashboard.js"' in page
        assert b"http-equiv='refresh'" not in page
        body = urlencode({"csrf_token": dashboard.csrf_token, "point_id": point_id})
        status, headers, _ = _request(dashboard.bound_port, "POST", "/api/scan-point/delete", body)
        assert status == 303
        assert headers["Location"] == "/"
        assert store.status()["saved_scans"][0]["devices"][0]["points"] == []
        assert store.status()["audit_events"][0]["action"] == "scan_point_deleted"
    finally:
        dashboard.close()
        store.close()


def test_csv_download_and_scan_rename_are_local_token_protected_actions(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    scan_id = store.start_scan("test")
    store.scan_device(scan_id, 1001, "192.168.50.41", "AHU", None)
    store.scan_point(scan_id, 1001, "analog-input,1", "SAT", "72", 72, "F", None)
    store.finish_scan(scan_id, 1, 1)
    dashboard = Dashboard(store, "127.0.0.1", 0, config.stale_after_seconds, lambda _: True)
    dashboard.start()
    try:
        status, headers, payload = _request(
            dashboard.bound_port, "POST", "/api/points.csv",
            urlencode({"csrf_token": dashboard.csrf_token}),
        )
        assert status == 200
        assert headers["Content-Disposition"] == "attachment; filename=bacnet-points.csv"
        assert b"scan_id,scan_name,device_instance,device_name,address" in payload
        assert b"1001,AHU,192.168.50.41,SAT" in payload
        assert store.status()["audit_events"][0]["action"] == "points_exported"

        bad = urlencode({"csrf_token": dashboard.csrf_token, "scan_id": "not-a-number", "name": "Morning"})
        status, _, _ = _request(dashboard.bound_port, "POST", "/api/scan/rename", bad)
        assert status == 400

        body = urlencode({"csrf_token": dashboard.csrf_token, "scan_id": scan_id, "name": "Morning survey"})
        status, headers, _ = _request(dashboard.bound_port, "POST", "/api/scan/rename", body)
        assert status == 303
        assert headers["Location"] == "/"
        current = store.status()
        assert current["scans"][0]["name"] == "Morning survey"
        assert current["audit_events"][0]["action"] == "scan_renamed"
        assert current["audit_events"][0]["actor"] == "127.0.0.1"

        _, _, page = _request(dashboard.bound_port, "GET", "/")
        assert b"READ-ONLY MODE" in page
        assert b"Morning survey" in page
        assert b"action='/api/scan/rename'" in page
    finally:
        dashboard.close()
        store.close()

from bacnet_console.config import DeviceConfig, PointConfig

def test_dashboard_escapes_approved_values(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)

    device = DeviceConfig(
        instance=2001,
        name="<script>alert('device')</script>",
        address="192.168.1.1",
        points=(
            PointConfig(
                object_id="analog-input,1",
                name="<img src=x onerror=alert('point')>"
            ),
        )
    )

    store.register_config((device,))

    # Store some fake point data
    store.point_result(2001, "<img src=x onerror=alert('point')>", 72.0, "72.0 <script>")

    dashboard = Dashboard(store, "127.0.0.1", 0, config.stale_after_seconds, lambda _: True)
    dashboard.start()
    try:
        _, _, page = _request(dashboard.bound_port, "GET", "/")

        # Check raw payloads aren't present
        assert b"<script>alert('device')</script>" not in page
        assert b"<img src=x onerror=alert('point')>" not in page
        assert b"72.0 <script>" not in page

        # Check escaped variations are present
        assert b"&lt;script&gt;alert(&#39;device&#39;)&lt;/script&gt;" in page
        assert b"&lt;img src=x onerror=alert(&#39;point&#39;)&gt;" in page
        assert b"72.0 &lt;script&gt;" in page

    finally:
        dashboard.close()
        store.close()
