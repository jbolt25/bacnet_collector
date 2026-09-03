from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

from bacnet_console.config import load_config
from bacnet_console.db import Store


def test_scan_names_local_deletes_audit_and_csv(config_file: Path) -> None:
    config = load_config(config_file)
    store = Store(config.database_path)
    store.register_config(config.devices)
    scan_id = store.start_scan("operator")
    store.scan_device(scan_id, 1001, "192.168.50.41", "AHU", None)
    store.scan_point(scan_id, 1001, "analog-input,1", "SAT", "72", 72, "F", None)
    assert store.status()["scans"][0]["device_count"] == 1
    assert store.status()["scans"][0]["point_count"] == 1
    assert store.delete_scan(scan_id, actor="operator") is False
    store.finish_scan(scan_id, 0, 0)
    status = store.status()
    assert status["scans"][0]["name"].startswith("Scan ")
    assert store.rename_scan(scan_id, "  First floor survey  ", actor="operator") is True
    assert store.rename_scan(scan_id, "   ") is False

    rows = list(csv.reader(io.StringIO(store.points_csv())))
    assert rows[0] == [
        "scan_id", "scan_name", "device_instance", "device_name", "address",
        "point_name", "object_id", "units", "value", "read_error", "approved",
    ]
    assert rows[1][0] == str(scan_id)
    assert rows[1][2:] == ["1001", "AHU", "192.168.50.41", "SAT", "analog-input,1", "F", "72", "", "1"]

    point_id = store.status()["approved_devices"][0]["points"][0]["id"]
    assert store.delete_point(point_id, actor="operator") is True
    assert store.delete_point(point_id, actor="operator") is False
    assert store.status()["approved_devices"][0]["points"] == []
    assert store.delete_scan(scan_id, actor="operator") is True
    assert store.delete_scan(scan_id, actor="operator") is False
    actions = [event["action"] for event in store.status()["audit_events"]]
    assert "scan_renamed" in actions
    assert "point_disabled" in actions
    assert "scan_deleted" in actions
    store.close()


def test_existing_scans_receive_names_during_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE scans (
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
          requested_by TEXT NOT NULL, status TEXT NOT NULL,
          device_count INTEGER NOT NULL DEFAULT 0,
          point_count INTEGER NOT NULL DEFAULT 0, error TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO scans(id,started_at,completed_at,requested_by,status)
           VALUES(1,'2026-08-30T12:34:56+00:00','2026-08-30T12:35:00+00:00','operator','completed')"""
    )
    connection.commit()
    connection.close()

    store = Store(path)
    assert store.status()["scans"][0]["name"] == "Scan 2026-08-30 12:34:56 UTC"
    store.close()
