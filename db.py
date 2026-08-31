from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  instance INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  configured INTEGER NOT NULL DEFAULT 1,
  configured_address TEXT,
  current_address TEXT,
  last_discovered_at TEXT,
  last_seen_at TEXT,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS points (
  id INTEGER PRIMARY KEY,
  device_instance INTEGER NOT NULL REFERENCES devices(instance),
  name TEXT NOT NULL,
  object_id TEXT NOT NULL,
  property_id TEXT NOT NULL,
  units TEXT,
  last_value_text TEXT,
  last_value_number REAL,
  last_success_at TEXT,
  last_attempt_at TEXT,
  last_error TEXT,
  consecutive_errors INTEGER NOT NULL DEFAULT 0,
  UNIQUE(device_instance, name)
);
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY,
  point_id INTEGER NOT NULL REFERENCES points(id),
  observed_at TEXT NOT NULL,
  value_text TEXT,
  value_number REAL
);
CREATE INDEX IF NOT EXISTS readings_point_time ON readings(point_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  scope TEXT NOT NULL,
  device_instance INTEGER,
  point_id INTEGER,
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS errors_time ON errors(occurred_at DESC);
CREATE TABLE IF NOT EXISTS collector_state (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  started_at TEXT,
  heartbeat_at TEXT,
  last_cycle_started_at TEXT,
  last_cycle_finished_at TEXT,
  cycles_completed INTEGER NOT NULL DEFAULT 0,
  fatal_error TEXT
);
INSERT OR IGNORE INTO collector_state(singleton, cycles_completed) VALUES (1, 0);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    def register_config(self, devices: tuple[Any, ...]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE devices SET configured=0")
            for device in devices:
                self._db.execute(
                    """INSERT INTO devices(instance,name,configured,configured_address,current_address)
                       VALUES(?,?,1,?,?) ON CONFLICT(instance) DO UPDATE SET
                       name=excluded.name, configured=1, configured_address=excluded.configured_address,
                       current_address=COALESCE(excluded.configured_address,devices.current_address)""",
                    (device.instance, device.name, device.address, device.address),
                )
                for point in device.points:
                    self._db.execute(
                        """INSERT INTO points(device_instance,name,object_id,property_id,units)
                           VALUES(?,?,?,?,?) ON CONFLICT(device_instance,name) DO UPDATE SET
                           object_id=excluded.object_id, property_id=excluded.property_id, units=excluded.units""",
                        (device.instance, point.name, point.object_id, point.property_id, point.units),
                    )

    def start(self) -> None:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                "UPDATE collector_state SET started_at=?,heartbeat_at=?,fatal_error=NULL WHERE singleton=1",
                (now, now),
            )

    def cycle_started(self) -> None:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                "UPDATE collector_state SET heartbeat_at=?,last_cycle_started_at=? WHERE singleton=1", (now, now)
            )

    def cycle_finished(self) -> None:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                """UPDATE collector_state SET heartbeat_at=?,last_cycle_finished_at=?,
                   cycles_completed=cycles_completed+1,fatal_error=NULL WHERE singleton=1""",
                (now, now),
            )

    def set_fatal(self, message: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE collector_state SET heartbeat_at=?,fatal_error=? WHERE singleton=1", (utc_now(), message[:1000])
            )

    def address_for(self, instance: int) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT current_address FROM devices WHERE instance=?", (instance,)).fetchone()
        return row[0] if row else None

    def discovered(self, instance: int, address: str) -> None:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                "UPDATE devices SET current_address=?,last_discovered_at=?,last_error=NULL WHERE instance=?",
                (address, now, instance),
            )

    def device_error(self, instance: int, scope: str, message: str) -> None:
        now = utc_now()
        message = message[:1000]
        with self._lock, self._db:
            self._db.execute("UPDATE devices SET last_error=? WHERE instance=?", (message, instance))
            self._db.execute(
                "INSERT INTO errors(occurred_at,scope,device_instance,message) VALUES(?,?,?,?)",
                (now, scope, instance, message),
            )

    def point_result(
        self, instance: int, point_name: str, value_number: float | None, value_text: str
    ) -> None:
        now = utc_now()
        with self._lock, self._db:
            point = self._db.execute(
                "SELECT id FROM points WHERE device_instance=? AND name=?", (instance, point_name)
            ).fetchone()
            if point is None:
                raise RuntimeError("point configuration is not registered")
            point_id = int(point[0])
            self._db.execute(
                """UPDATE points SET last_value_text=?,last_value_number=?,last_success_at=?,
                   last_attempt_at=?,last_error=NULL,consecutive_errors=0 WHERE id=?""",
                (value_text, value_number, now, now, point_id),
            )
            self._db.execute(
                "INSERT INTO readings(point_id,observed_at,value_text,value_number) VALUES(?,?,?,?)",
                (point_id, now, value_text, value_number),
            )
            self._db.execute(
                "UPDATE devices SET last_seen_at=?,last_error=NULL WHERE instance=?", (now, instance)
            )

    def point_error(self, instance: int, point_name: str, message: str) -> None:
        now = utc_now()
        message = message[:1000]
        with self._lock, self._db:
            point = self._db.execute(
                "SELECT id FROM points WHERE device_instance=? AND name=?", (instance, point_name)
            ).fetchone()
            if point is None:
                return
            point_id = int(point[0])
            self._db.execute(
                """UPDATE points SET last_attempt_at=?,last_error=?,
                   consecutive_errors=consecutive_errors+1 WHERE id=?""",
                (now, message, point_id),
            )
            self._db.execute(
                "INSERT INTO errors(occurred_at,scope,device_instance,point_id,message) VALUES(?,?,?,?,?)",
                (now, "point", instance, point_id, message),
            )

    def prune(self, retention_days: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock, self._db:
            self._db.execute("DELETE FROM readings WHERE observed_at < ?", (cutoff,))
            self._db.execute("DELETE FROM errors WHERE occurred_at < ?", (cutoff,))

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._db.execute("SELECT * FROM collector_state WHERE singleton=1").fetchone())
            devices = [
                dict(row)
                for row in self._db.execute("SELECT * FROM devices WHERE configured=1 ORDER BY name")
            ]
            for device in devices:
                device["points"] = [
                    dict(row)
                    for row in self._db.execute(
                        """SELECT name,object_id,property_id,units,last_value_text,last_value_number,
                           last_success_at,last_attempt_at,last_error,consecutive_errors
                           FROM points WHERE device_instance=? ORDER BY name""",
                        (device["instance"],),
                    )
                ]
            errors = [
                dict(row)
                for row in self._db.execute(
                    "SELECT occurred_at,scope,device_instance,message FROM errors ORDER BY id DESC LIMIT 25"
                )
            ]
        return {"collector": state, "devices": devices, "recent_errors": errors}
