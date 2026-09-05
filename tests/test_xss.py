from pathlib import Path
from bacnet_console.db import Store
from bacnet_console.config import load_config
from bacnet_console.dashboard import Dashboard

def _request(port: int, method: str, path: str, body: str | None = None, headers: dict = None):
    from http.client import HTTPConnection
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    req_headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
    if headers:
        req_headers.update(headers)
    connection.request(method, path, body=body, headers=req_headers)
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, dict(response.getheaders()), payload)
    connection.close()
    return result

def test_xss_regression_saved_scan_and_status(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    scan_id = store.start_scan("<script>alert('scan_name')</script>")
    store.scan_device(scan_id, 1001, "192.168.50.41", "<script>alert('dev')</script>", "<script>alert('err')</script>")
    store.scan_point(scan_id, 1001, "<script>alert('obj')</script>", "<script>alert('name')</script>", "<script>alert('val')</script>", None, "<script>alert('unit')</script>", "<script>alert('err2')</script>")
    store.finish_scan(scan_id, 1, 1)

    dashboard = Dashboard(store, "127.0.0.1", 0, config.stale_after_seconds, lambda _: True)
    dashboard.start()
    try:
        _, _, page = _request(dashboard.bound_port, "GET", "/")
        # check that there's no script tags from injection
        assert b"<script>alert(" not in page

        status, headers, status_page = _request(dashboard.bound_port, "GET", "/api/scan/status")
        assert status == 200
        assert headers.get('Content-Type') == 'application/json'
    finally:
        dashboard.close()
        store.close()
