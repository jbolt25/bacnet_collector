from __future__ import annotations

import html
import json
import secrets
import sys
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .db import Store


def _health(value: str | None, stale_seconds: float) -> str:
    if not value:
        return "bad"
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    except ValueError:
        return "bad"
    return "ok" if age <= stale_seconds else "warn"


def _time_compact(value: str | None) -> str:
    """Format compact UTC time."""
    raw = value or "never"
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc).strftime("%m/%d %H:%M:%SZ")
    except (TypeError, ValueError):
        return raw


def _asset_root(assets_dir: str | Path | None) -> Path:
    if assets_dir is not None:
        return Path(assets_dir)
    if (Path.cwd() / "templates/dashboard.html").is_file():
        return Path.cwd()
    return Path(sys.prefix) / "share/bacnet-console"


_env: Environment | None = None

def get_jinja_env(assets_dir: Path) -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(assets_dir / "templates"),
            autoescape=select_autoescape(['html', 'xml'])
        )
        _env.filters['health'] = _health
        _env.filters['time_compact'] = _time_compact
    return _env

def render_status(
    data: dict[str, Any], stale_seconds: float, csrf_token: str,
    script_nonce: str | None = None, assets_dir: str | Path | None = None,
) -> str:
    del script_nonce  # retained for compatibility; external scripts need no nonce
    env = get_jinja_env(_asset_root(assets_dir))
    template = env.get_template("dashboard.html")

    return template.render(
        data=data,
        state=data["collector"],
        csrf_token=csrf_token,
        stale_seconds=stale_seconds,
        scans=data.get("scans", []),
        audit_events=data.get("audit_events", []),
        saved_scans=data.get("saved_scans", []),
        approved_devices=data.get("approved_devices", [])
    )


class Dashboard:
    def __init__(
        self, store: Store, host: str, port: int, stale_seconds: float,
        request_scan: Callable[[str], bool], cancel_scan: Callable[[], bool] | None = None,
        assets_dir: str | Path | None = None,
    ) -> None:
        self.store = store
        self.csrf_token = secrets.token_urlsafe(32)
        self.assets_dir = _asset_root(assets_dir)
        self.cancel_scan = cancel_scan or (lambda: False)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/healthz":
                    self._send(200, "text/plain; charset=utf-8", b"ok\n")
                elif path == "/api/status":
                    self._send(200, "application/json", json.dumps(outer.store.status()).encode())
                elif path == "/api/scan/status":
                    self._send(200, "application/json", json.dumps(outer.store.status(live_only=True)).encode())
                elif path == "/api/points.csv":
                    self._send(405, "text/plain; charset=utf-8", b"use POST with the dashboard token\n", {"Allow": "POST"})
                elif path in ("/static/dashboard.css", "/static/dashboard.js"):
                    relative = path.removeprefix("/")
                    content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
                    try:
                        body = (outer.assets_dir / relative).resolve().read_bytes()
                    except (OSError, UnicodeError, ValueError):
                        self._send(404, "text/plain; charset=utf-8", b"not found\n")
                    else:
                        self._send(200, content_type, body)
                elif path == "/":
                    try:
                        page = render_status(
                            outer.store.status(), stale_seconds, outer.csrf_token, assets_dir=outer.assets_dir
                        )
                    except (OSError, UnicodeError, ValueError):
                        self._send(500, "text/plain; charset=utf-8", b"dashboard template unavailable\n")
                    else:
                        self._send(
                            200, "text/html; charset=utf-8", page.encode(),
                            {"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'"},
                        )
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found\n")

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path not in ("/api/rescan", "/api/rescan/cancel", "/api/points.csv", "/api/scan/rename", "/api/scan/delete", "/api/point/delete", "/api/scan-point/delete"):
                    self._deny()
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length < 1 or length > 4096:
                    self._send(400, "text/plain; charset=utf-8", b"invalid request\n")
                    return
                form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                supplied = form.get("csrf_token", [""])[0]
                if not secrets.compare_digest(supplied.encode("utf-8"), outer.csrf_token.encode("ascii")):
                    self._send(403, "text/plain; charset=utf-8", b"invalid action token\n")
                    return
                if path == "/api/points.csv":
                    self._send(
                        200, "text/csv; charset=utf-8", outer.store.points_csv(actor=self.client_address[0]).encode(),
                        {"Content-Disposition": "attachment; filename=bacnet-points.csv"},
                    )
                    return
                if path == "/api/scan/rename":
                    try:
                        scan_id = int(form.get("scan_id", ["0"])[0])
                    except ValueError:
                        scan_id = 0
                    ok = outer.store.rename_scan(
                        scan_id, form.get("name", [""])[0], actor=self.client_address[0]
                    )
                    self._send(
                        303 if ok else 400, "text/plain; charset=utf-8",
                        b"" if ok else b"invalid\n", {"Location": "/"} if ok else None,
                    )
                    return
                if path in ("/api/scan/delete", "/api/point/delete", "/api/scan-point/delete"):
                    field = "scan_id" if path == "/api/scan/delete" else "point_id"
                    try:
                        item_id = int(form.get(field, ["0"])[0])
                    except ValueError:
                        item_id = 0
                    if path == "/api/scan/delete":
                        ok = outer.store.delete_scan(item_id, actor=self.client_address[0])
                    elif path == "/api/scan-point/delete":
                        ok = outer.store.delete_scan_point(item_id, actor=self.client_address[0])
                    else:
                        ok = outer.store.delete_point(item_id, actor=self.client_address[0])
                    self._send(
                        303 if ok else 400, "text/plain; charset=utf-8",
                        b"" if ok else b"invalid\n", {"Location": "/"} if ok else None,
                    )
                    return
                if path == "/api/rescan/cancel":
                    cancelled = outer.cancel_scan()
                    if cancelled and "application/json" in self.headers.get("Accept", ""):
                        self._send(200, "application/json", b'{"accepted":true}')
                        return
                    self._send(
                        303 if cancelled else 409, "text/plain; charset=utf-8",
                        b"" if cancelled else b"no scan running\n",
                        {"Location": "/"} if cancelled else None,
                    )
                    return
                accepted = request_scan(self.client_address[0])
                if accepted:
                    if "application/json" in self.headers.get("Accept", ""):
                        self._send(202, "application/json", b'{"accepted":true}')
                    else:
                        self._send(303, "text/plain; charset=utf-8", b"", {"Location": "/"})
                else:
                    self._send(409, "text/plain; charset=utf-8", b"scan already running\n")

            def _deny(self) -> None:
                self._send(405, "text/plain; charset=utf-8", b"read-only console\n", {"Allow": "GET"})

            def do_DELETE(self) -> None:  # noqa: N802
                self._deny()
            do_PUT = do_PATCH = _deny

            def _send(
                self, status: int, content_type: str, body: bytes, extra: dict[str, str] | None = None
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                extra_headers = extra or {}
                self.send_header(
                    "Content-Security-Policy",
                    extra_headers.get("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"),
                )
                for key, value in extra_headers.items():
                    if key == "Content-Security-Policy":
                        continue
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = Thread(target=self._server.serve_forever, name="dashboard", daemon=True)

    @property
    def bound_port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
