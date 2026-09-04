from __future__ import annotations

import csv
import io
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
  configured INTEGER NOT NULL DEFAULT 1,
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
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  action TEXT NOT NULL,
  actor TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS audit_events_time ON audit_events(occurred_at DESC);
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
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  requested_by TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  device_count INTEGER NOT NULL DEFAULT 0,
  point_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  name TEXT
);
CREATE TABLE IF NOT EXISTS scan_devices (
  id INTEGER PRIMARY KEY,
  scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  instance INTEGER NOT NULL,
  address TEXT NOT NULL,
  object_name TEXT,
  object_error TEXT,
  UNIQUE(scan_id, instance)
);
CREATE TABLE IF NOT EXISTS scan_points (
  id INTEGER PRIMARY KEY,
  scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  device_instance INTEGER NOT NULL,
  object_id TEXT NOT NULL,
  object_name TEXT,
  value_text TEXT,
  value_number REAL,
  units TEXT,
  read_error TEXT,
  UNIQUE(scan_id, device_instance, object_id)
);
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
        # Upgrade databases created by the original collector without discarding history.
        point_columns = {row[1] for row in self._db.execute("PRAGMA table_info(points)")}
        if "configured" not in point_columns:
            self._db.execute("ALTER TABLE points ADD COLUMN configured INTEGER NOT NULL DEFAULT 1")
        if "disabled" not in point_columns:
            self._db.execute("ALTER TABLE points ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        scan_columns = {row[1] for row in self._db.execute("PRAGMA table_info(scans)")}
        if "name" not in scan_columns:
            self._db.execute("ALTER TABLE scans ADD COLUMN name TEXT")
        self._db.execute(
            """UPDATE scans SET name='Scan ' || replace(substr(started_at,1,19),'T',' ') || ' UTC'
               WHERE name IS NULL OR trim(name)=''"""
        )
        # Repair counts written by older releases when a scan stopped mid-device.
        self._db.execute(
            """UPDATE scans SET
               device_count=(SELECT count(*) FROM scan_devices WHERE scan_id=scans.id),
               point_count=(SELECT count(*) FROM scan_points WHERE scan_id=scans.id)"""
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    def register_config(self, devices: tuple[Any, ...]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE devices SET configured=0")
            self._db.execute("UPDATE points SET configured=0")

            device_params = []
            point_params = []
            for device in devices:
                device_params.append((device.instance, device.name, device.address, device.address))
                for point in device.points:
                    point_params.append(
                        (device.instance, point.name, point.object_id, point.property_id, point.units)
                    )

            self._db.executemany(
                """INSERT INTO devices(instance,name,configured,configured_address,current_address)
                   VALUES(?,?,1,?,?) ON CONFLICT(instance) DO UPDATE SET
                   name=excluded.name,configured=1,
                   configured_address=excluded.configured_address,
                   current_address=COALESCE(excluded.configured_address,devices.current_address)""",
                device_params,
            )

            self._db.executemany(
                """INSERT INTO points(device_instance,name,object_id,property_id,units,configured)
                   VALUES(?,?,?,?,?,1) ON CONFLICT(device_instance,name) DO UPDATE SET
                   object_id=excluded.object_id,property_id=excluded.property_id,
                   units=excluded.units,configured=CASE WHEN points.disabled=0 THEN 1 ELSE 0 END""",
                point_params,
            )

    def start(self) -> None:
        now = utc_now()
        with self._lock, self._db:
            abandoned = self._db.execute(
                "SELECT id FROM scans WHERE status='running' ORDER BY id"
            ).fetchall()
            reason = "scan abandoned because the service restarted"
            for row in abandoned:
                self._db.execute(
                    "UPDATE scans SET completed_at=?,status='failed',error=? WHERE id=?",
                    (now, reason, row[0]),
                )
                self._db.execute(
                    "INSERT INTO audit_events(occurred_at,action,detail) VALUES(?,?,?)",
                    (now, "scan_recovered", f"scan {row[0]} marked failed: {reason}"),
                )
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
                "UPDATE collector_state SET heartbeat_at=?,fatal_error=? WHERE singleton=1",
                (utc_now(), message[:1000]),
            )

    def address_for(self, instance: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(configured_address,current_address) FROM devices WHERE instance=? AND configured=1",
                (instance,),
            ).fetchone()
        return row[0] if row else None

    def device_error(self, instance: int, scope: str, message: str) -> None:
        now = utc_now()
        message = message[:1000]
        with self._lock, self._db:
            self._db.execute("UPDATE devices SET last_error=? WHERE instance=?", (message, instance))
            self._db.execute(
                "INSERT INTO errors(occurred_at,scope,device_instance,message) VALUES(?,?,?,?)",
                (now, scope, instance, message),
            )
            self._db.execute("INSERT INTO audit_events(occurred_at,action,detail) VALUES(?,?,?)",
                             (now, "read_error", f"{scope} device {instance}: {message}"))

    def point_result(self, instance: int, point_name: str, number: float | None, text: str) -> None:
        now = utc_now()
        with self._lock, self._db:
            point = self._db.execute(
                "SELECT id FROM points WHERE device_instance=? AND name=? AND configured=1",
                (instance, point_name),
            ).fetchone()
            if point is None:
                # An operator can disable a point while a read is in flight.
                return
            point_id = int(point[0])
            self._db.execute(
                """UPDATE points SET last_value_text=?,last_value_number=?,last_success_at=?,
                   last_attempt_at=?,last_error=NULL,consecutive_errors=0 WHERE id=?""",
                (text, number, now, now, point_id),
            )
            self._db.execute(
                "INSERT INTO readings(point_id,observed_at,value_text,value_number) VALUES(?,?,?,?)",
                (point_id, now, text, number),
            )
            self._db.execute(
                "UPDATE devices SET last_seen_at=?,last_error=NULL WHERE instance=?", (now, instance)
            )

    def point_error(self, instance: int, point_name: str, message: str) -> None:
        now = utc_now()
        message = message[:1000]
        with self._lock, self._db:
            point = self._db.execute(
                "SELECT id FROM points WHERE device_instance=? AND name=? AND configured=1",
                (instance, point_name),
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
            self._db.execute("INSERT INTO audit_events(occurred_at,action,detail) VALUES(?,?,?)",
                             (now, "read_error", f"point {instance}/{point_name}: {message}"))

    def start_scan(self, requested_by: str) -> int:
        now = utc_now()
        default_name = "Scan " + now.replace("T", " ").replace("+00:00", "") + " UTC"
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO scans(started_at,requested_by,status,name) VALUES(?,?,?,?)",
                (now, requested_by[:100], "running", default_name),
            )
            scan_id = int(cursor.lastrowid)
            self._db.execute(
                "INSERT INTO audit_events(occurred_at,action,actor,detail) VALUES(?,?,?,?)",
                (now, "scan_started", requested_by[:100], f"scan {scan_id}"),
            )
            return scan_id

    def rename_scan(self, scan_id: int, name: str, actor: str | None = None) -> bool:
        name = name.strip()[:120]
        if not name:
            return False
        with self._lock, self._db:
            updated = self._db.execute("UPDATE scans SET name=? WHERE id=?", (name, scan_id)).rowcount == 1
            if updated:
                self._db.execute(
                    "INSERT INTO audit_events(occurred_at,action,actor,detail) VALUES(?,?,?,?)",
                    (utc_now(), "scan_renamed", actor[:100] if actor else None, f"scan {scan_id}: {name}"),
                )
            return updated

    def delete_scan(self, scan_id: int, actor: str | None = None) -> bool:
        with self._lock, self._db:
            scan = self._db.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
            if scan is None or scan[0] == "running":
                return False
            deleted = self._db.execute("DELETE FROM scans WHERE id=?", (scan_id,)).rowcount == 1
            if deleted:
                self._db.execute(
                    "INSERT INTO audit_events(occurred_at,action,actor,detail) VALUES(?,?,?,?)",
                    (utc_now(), "scan_deleted", actor[:100] if actor else None, f"scan {scan_id}"),
                )
            return deleted

    def point_enabled(self, instance: int, name: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM points WHERE device_instance=? AND name=? AND configured=1",
                (instance, name),
            ).fetchone() is not None

    def delete_point(self, point_id: int, actor: str | None = None) -> bool:
        with self._lock, self._db:
            point = self._db.execute(
                "SELECT device_instance,name FROM points WHERE id=? AND configured=1", (point_id,)
            ).fetchone()
            if point is None:
                return False
            self._db.execute("UPDATE points SET configured=0,disabled=1 WHERE id=?", (point_id,))
            self._db.execute(
                "INSERT INTO audit_events(occurred_at,action,actor,detail) VALUES(?,?,?,?)",
                (
                    utc_now(), "point_disabled", actor[:100] if actor else None,
                    f"point {point['device_instance']}/{point['name']}",
                ),
            )
            return True

    def enable_point(self, point_id: int, actor: str | None = None) -> bool:
        with self._lock, self._db:
            point = self._db.execute(
                "SELECT device_instance,name FROM points WHERE id=?", (point_id,)
            ).fetchone()
            if point is None:
                return False
            self._db.execute("UPDATE points SET configured=1,disabled=0 WHERE id=?", (point_id,))
            self._db.execute(
                "INSERT INTO audit_events(occurred_at,action,actor,detail) VALUES(?,?,?,?)",
                (
                    utc_now(), "point_enabled", actor[:100] if actor else None,
                    f"point {point['device_instance']}/{point['name']}",
                ),
            )
            return True

    def delete_scan_point(self, point_id: int, actor: str | None = None) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                """SELECT sp.scan_id,sp.device_instance,sp.object_id,s.status
                   FROM scan_points sp JOIN scans s ON s.id=sp.scan_id WHERE sp.id=?""",
                (point_id,),
            ).fetchone()
            if row is None or row[3] == "running":
                return False
            deleted = self._db.execute("DELETE FROM scan_points WHERE id=?", (point_id,)).rowcount == 1
            if deleted:
                self._db.execute(
                    "UPDATE scans SET point_count=(SELECT count(*) FROM scan_points WHERE scan_id=?) WHERE id=?",
                    (row["scan_id"], row["scan_id"]),
                )
                self._db.execute(
                    "INSERT INTO audit_events(occurred_at,action,actor,detail) VALUES(?,?,?,?)",
                    (
                        utc_now(), "scan_point_deleted", actor[:100] if actor else None,
                        f"scan {row['scan_id']} point {row['device_instance']}/{row['object_id']}",
                    ),
                )
            return deleted

    def points_csv(self, actor: str | None = None) -> str:
        if actor:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO audit_events(occurred_at,action,actor,detail) VALUES(?,?,?,?)",
                    (utc_now(), "points_exported", actor[:100], "saved scan points CSV"),
                )
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(
            ("scan_id", "scan_name", "device_instance", "device_name", "address",
             "point_name", "object_id", "units", "value", "read_error", "approved")
        )
        with self._lock:
            rows = self._db.execute(
                """SELECT s.id,s.name,sp.device_instance,
                          COALESCE(sd.object_name,'') AS device_name,sd.address,
                          COALESCE(sp.object_name,'') AS point_name,sp.object_id,sp.units,
                          COALESCE(sp.value_text,'') AS value,COALESCE(sp.read_error,'') AS read_error,
                          EXISTS(SELECT 1 FROM points p WHERE p.device_instance=sp.device_instance
                                 AND p.object_id=sp.object_id AND p.configured=1) AS approved
                   FROM scan_points sp
                   JOIN scans s ON s.id=sp.scan_id
                   JOIN scan_devices sd ON sd.scan_id=sp.scan_id AND sd.instance=sp.device_instance
                   ORDER BY s.id DESC,sp.device_instance,sp.object_id"""
            ).fetchall()
        for row in rows:
            # Spreadsheet software interprets device-supplied text as formulas.
            writer.writerow(tuple(
                "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value for value in row
            ))
        return out.getvalue()

    def scan_device(
        self, scan_id: int, instance: int, address: str, name: str | None, error: str | None
    ) -> None:
        with self._lock, self._db:
            inserted = self._db.execute(
                """INSERT INTO scan_devices(scan_id,instance,address,object_name,object_error)
                   VALUES(?,?,?,?,?)""",
                (scan_id, instance, address, name, error),
            ).rowcount
            if inserted:
                self._db.execute("UPDATE scans SET device_count=device_count+1 WHERE id=?", (scan_id,))
            self._db.execute(
                """UPDATE devices SET current_address=?
                   WHERE instance=? AND configured=1 AND configured_address IS NULL""",
                (address, instance),
            )

    def scan_point(
        self, scan_id: int, instance: int, object_id: str, name: str | None,
        value_text: str | None, value_number: float | None, units: str | None, error: str | None,
    ) -> None:
        with self._lock, self._db:
            inserted = self._db.execute(
                """INSERT INTO scan_points(scan_id,device_instance,object_id,object_name,
                   value_text,value_number,units,read_error) VALUES(?,?,?,?,?,?,?,?)""",
                (scan_id, instance, object_id, name, value_text, value_number, units, error),
            ).rowcount
            if inserted:
                self._db.execute("UPDATE scans SET point_count=point_count+1 WHERE id=?", (scan_id,))

    def finish_scan(self, scan_id: int, device_count: int, point_count: int, error: str | None = None) -> None:
        with self._lock, self._db:
            # A stop/deadline may interrupt the middle of a device. Committed rows
            # are authoritative, not the scanner's completed-device counters.
            device_count = self._db.execute(
                "SELECT count(*) FROM scan_devices WHERE scan_id=?", (scan_id,)
            ).fetchone()[0]
            point_count = self._db.execute(
                "SELECT count(*) FROM scan_points WHERE scan_id=?", (scan_id,)
            ).fetchone()[0]
            self._db.execute(
                """UPDATE scans SET completed_at=?,status=?,device_count=?,point_count=?,error=? WHERE id=?""",
                (utc_now(), "failed" if error else "completed", device_count, point_count, error, scan_id),
            )
            self._db.execute("INSERT INTO audit_events(occurred_at,action,detail) VALUES(?,?,?)",
                             (utc_now(), "scan_finished", f"scan {scan_id}: {device_count} devices, {point_count} points"
                              + (f", error: {error[:500]}" if error else "")))

    def prune(self, retention_days: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock, self._db:
            self._db.execute("DELETE FROM readings WHERE observed_at < ?", (cutoff,))
            self._db.execute("DELETE FROM errors WHERE occurred_at < ?", (cutoff,))

    def status(self, *, live_only: bool = False) -> dict[str, Any]:
        with self._lock:
            state = dict(self._db.execute("SELECT * FROM collector_state WHERE singleton=1").fetchone())
            devices = [
                dict(row) for row in self._db.execute("SELECT * FROM devices WHERE configured=1 ORDER BY name")
            ]
            for device in devices:
                device["points"] = [
                    dict(row) for row in self._db.execute(
                        """SELECT id,name,object_id,property_id,units,last_value_text,last_value_number,
                           last_success_at,last_attempt_at,last_error,consecutive_errors
                           FROM points WHERE device_instance=? AND configured=1 ORDER BY name""",
                        (device["instance"],),
                    )
                ]
            scan_query = "SELECT * FROM scans ORDER BY id DESC" + (" LIMIT 1" if live_only else "")
            scans = [dict(row) for row in self._db.execute(scan_query)]
            saved_scans: list[dict[str, Any]] = []

            def scan_devices_for(scan_id: int) -> list[dict[str, Any]]:
                scan_devices = [
                    dict(row) for row in self._db.execute(
                        """SELECT sd.*,CASE WHEN d.configured=1 THEN 1 ELSE 0 END AS approved
                           FROM scan_devices sd LEFT JOIN devices d ON d.instance=sd.instance
                           WHERE sd.scan_id=? ORDER BY sd.instance""",
                        (scan_id,),
                    )
                ]
                for scan_device in scan_devices:
                    scan_device["points"] = [
                        dict(row) for row in self._db.execute(
                            """SELECT sp.*,EXISTS(SELECT 1 FROM points p
                               WHERE p.device_instance=sp.device_instance AND p.object_id=sp.object_id
                               AND p.configured=1) AS approved FROM scan_points sp
                               WHERE sp.scan_id=? AND sp.device_instance=? ORDER BY sp.object_id""",
                            (scan_id, scan_device["instance"]),
                        )
                    ]
                return scan_devices

            for scan in scans:
                saved_scan = dict(scan)
                saved_scan["devices"] = scan_devices_for(scan["id"])
                saved_scans.append(saved_scan)
            latest = next((scan for scan in scans if scan["status"] != "running"), scans[0] if scans else None)
            scan_devices = next((scan["devices"] for scan in saved_scans if scan["id"] == latest["id"]), []) if latest else []
            errors = [dict(row) for row in self._db.execute(
                "SELECT occurred_at,scope,device_instance,message FROM errors ORDER BY id DESC LIMIT 25"
            )]
            audit = [dict(row) for row in self._db.execute(
                "SELECT occurred_at,action,actor,detail FROM audit_events ORDER BY id DESC LIMIT 50"
            )]
        return {
            "collector": state,
            "approved_devices": devices,
            "scans": scans,
            "saved_scans": saved_scans,
            "latest_scan": latest,
            "discovered_devices": scan_devices,
            "recent_errors": errors,
            "audit_events": audit,
        }
