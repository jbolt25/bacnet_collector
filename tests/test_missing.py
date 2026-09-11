import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bacnet_console.db import Store
from bacnet_console.dashboard import Dashboard
from bacnet_console.config import load_config
from bacnet_console.cli import main

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

def test_invalid_csv_access(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    dashboard = Dashboard(store, "127.0.0.1", 0, config.stale_after_seconds, lambda _: True)
    dashboard.start()
    try:
        # GET should be 405 Method Not Allowed and return specific body
        status, headers, payload = _request(dashboard.bound_port, "GET", "/api/points.csv")
        assert status == 405
        assert payload == b"use POST with the dashboard token\n"
        assert headers.get("Allow") == "POST"

        # POST with wrong token should be 403 Forbidden
        status, _, payload = _request(dashboard.bound_port, "POST", "/api/points.csv", "csrf_token=wrong")
        assert status == 403
        assert payload == b"invalid action token\n"
    finally:
        dashboard.close()
        store.close()

def test_healthz_edge_cases(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    dashboard = Dashboard(store, "127.0.0.1", 0, config.stale_after_seconds, lambda _: True)
    dashboard.start()
    try:
        status, headers, payload = _request(dashboard.bound_port, "GET", "/healthz")
        assert status == 200
        assert payload == b"ok\n"
        assert "text/plain" in headers.get("Content-Type", "")

        # POST to healthz should be 405
        status, _, _ = _request(dashboard.bound_port, "POST", "/healthz")
        assert status == 405
    finally:
        dashboard.close()
        store.close()

def test_store_prune(tmp_path: Path) -> None:
    store = Store(tmp_path / 'prune.sqlite3')
    try:
        store.register_config([])
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=10)).isoformat(timespec="seconds")
        recent_time = (now - timedelta(days=1)).isoformat(timespec="seconds")

        with store._lock, store._db:
            store._db.execute("INSERT INTO devices (instance, name) VALUES (1, 'D1')")
            store._db.execute("INSERT INTO points (id, device_instance, name, object_id, property_id) VALUES (1, 1, 'P1', 'analog-input,1', 'present-value')")
            store._db.execute("INSERT INTO readings (point_id, observed_at, value_text) VALUES (1, ?, '10')", (old_time,))
            store._db.execute("INSERT INTO readings (point_id, observed_at, value_text) VALUES (1, ?, '20')", (recent_time,))

            store._db.execute("INSERT INTO errors (occurred_at, scope, message) VALUES (?, 'system', 'err1')", (old_time,))
            store._db.execute("INSERT INTO errors (occurred_at, scope, message) VALUES (?, 'system', 'err2')", (recent_time,))

        # Prune older than 5 days
        store.prune(5)

        with store._lock, store._db:
            readings = store._db.execute("SELECT value_text FROM readings").fetchall()
            assert len(readings) == 1
            assert tuple(readings[0])[0] == '20'

            errors = store._db.execute("SELECT message FROM errors").fetchall()
            assert len(errors) == 1
            assert tuple(errors[0])[0] == 'err2'

    finally:
        store.close()

def test_cli_check_config(config_file: Path, capsys, monkeypatch) -> None:
    # Valid config
    monkeypatch.setattr("sys.argv", ["bacnet-console", "--config", str(config_file), "--check-config"])
    try:
        main()
    except SystemExit as e:
        assert e.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "configuration is valid"

    # Invalid config
    invalid_conf = config_file.parent / "invalid.yaml"
    invalid_conf.write_text("invalid_yaml: [")
    monkeypatch.setattr("sys.argv", ["bacnet-console", "--config", str(invalid_conf), "--check-config"])

    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code != 0
    captured = capsys.readouterr()
    assert "error: " in captured.err.lower()

def test_missing_content_length_post(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    dashboard = Dashboard(store, "127.0.0.1", 0, config.stale_after_seconds, lambda _: True)
    dashboard.start()
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", dashboard.bound_port))
        # Send raw POST request without Content-Length
        s.sendall(b"POST /api/rescan HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/x-www-form-urlencoded\r\n\r\ncsrf_token=wrong")
        response_data = s.recv(1024)
        s.close()

        assert b"400" in response_data
    finally:
        dashboard.close()
        store.close()
