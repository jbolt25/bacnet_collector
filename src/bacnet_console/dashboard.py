from __future__ import annotations

import html
import json
import secrets
import re
import sys
from datetime import datetime, timezone
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


def _time_markup(value: str | None) -> str:
    """Render compact UTC time while retaining the full value on hover."""
    raw = value or "never"
    try:
        compact = datetime.fromisoformat(raw).astimezone(timezone.utc).strftime("%m/%d %H:%M:%SZ")
    except (TypeError, ValueError):
        compact = raw
    return f"<time datetime='{html.escape(raw, quote=True)}' title='{html.escape(raw, quote=True)}'>{html.escape(compact)}</time>"


def _approved_sections(devices: list[dict[str, Any]], stale_seconds: float, csrf_token: str) -> str:
    if not devices:
        return "<p class='muted'>No devices or points are approved yet. Operator scans can be used for commissioning.</p>"
    blocks = []
    for device in devices:
        device_health = "bad" if device["last_error"] else _health(device["last_seen_at"], stale_seconds)
        device_label = (
            "error" if device["last_error"] else "healthy" if device_health == "ok" else "not recently seen"
        )
        rows = []
        for point in device["points"]:
            health = "bad" if point["last_error"] else _health(point["last_success_at"], stale_seconds)
            label = "error" if point["last_error"] else "healthy" if health == "ok" else "stale"
            rows.append(
                "<tr>"
                f"<td>{html.escape(point['name'])}</td><td><code>{html.escape(point['object_id'])}</code></td>"
                f"<td>{html.escape(point['last_value_text'] or '—')} {html.escape(point['units'] or '')}</td>"
                f"<td class='{health}'>{label}</td><td>{_time_markup(point['last_success_at'])}</td>"
                f"<td>{html.escape(point['last_error'] or '')}</td>"
                f"<td><form method='post' action='/api/point/delete'>"
                f"<input type='hidden' name='csrf_token' value='{csrf_token}'>"
                f"<input type='hidden' name='point_id' value='{point['id']}'>"
                f"<button type='submit'>Delete</button></form></td></tr>"
            )
        blocks.append(
            f"<article><h3>{html.escape(device['name'])} <small>device {device['instance']}</small></h3>"
            f"<p>{html.escape(device['current_address'] or 'address unresolved')} · last seen "
            f"{_time_markup(device['last_seen_at'])} · <span class='{device_health}'>{device_label}</span>"
            f"{' · ' + html.escape(device['last_error']) if device['last_error'] else ''}</p>"
            + ("<div class='table-scroll' tabindex='0' role='region' aria-label='Approved points'><table><thead><tr><th>Point</th><th>Object</th><th>Value</th><th>Health</th>"
               "<th>Last success</th><th>Error</th><th>Action</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
               if rows else "<p class='muted'>Device approved with no trend points.</p>")
            + "</article>"
        )
    return "".join(blocks)


def _scan_sections(devices: list[dict[str, Any]], csrf_token: str, running: bool = False) -> str:
    disabled = 'disabled title="Stop the scan before removing points"' if running else ''
    if not devices:
        return "<p class='muted'>No completed scan results yet.</p>"
    blocks = []
    for device in devices:
        rows = []
        for point in device["points"]:
            health = "bad" if point["read_error"] else "ok"
            rows.append(
                f"<tr><td><code>{html.escape(point['object_id'])}</code></td>"
                f"<td>{html.escape(point['object_name'] or '—')}</td>"
                f"<td>{html.escape(point['value_text'] or '—')} {html.escape(point['units'] or '')}</td>"
                f"<td>{'approved' if point['approved'] else 'discovered'}</td>"
                f"<td class='{health}'>{html.escape(point['read_error'] or 'readable')}</td>"
                f"<td><form method='post' action='/api/scan-point/delete'>"
                f"<input type='hidden' name='csrf_token' value='{csrf_token}'>"
                f"<input type='hidden' name='point_id' value='{point['id']}'>"
                f"<button type='submit' {disabled}>Remove</button></form></td></tr>"
            )
        blocks.append(
            f"<article><h3>{html.escape(device['object_name'] or 'Unnamed device')} "
            f"<small>device {device['instance']} · {html.escape(device['address'])}</small></h3>"
            f"<p>{'Approved' if device['approved'] else 'Discovered, not approved'}"
            f"{' · ' + html.escape(device['object_error']) if device['object_error'] else ''}</p>"
            "<div class='table-scroll' tabindex='0' role='region' aria-label='Saved device points'><table><thead><tr><th>Object</th><th>Name</th><th>Value</th><th>Status</th><th>Health</th><th>Action</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></article>"
        )
    return "".join(blocks)


def _asset_root(assets_dir: str | Path | None) -> Path:
    if assets_dir is not None:
        return Path(assets_dir)
    if (Path.cwd() / "templates/dashboard.html").is_file():
        return Path.cwd()
    return Path(sys.prefix) / "share/bacnet-console"


def _read_asset(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    return path.read_text(encoding="utf-8")


def render_status(
    data: dict[str, Any], stale_seconds: float, csrf_token: str,
    script_nonce: str | None = None, assets_dir: str | Path | None = None,
) -> str:
    del script_nonce  # retained for compatibility; external scripts need no nonce
    state = data["collector"]
    heartbeat = _health(state["heartbeat_at"], stale_seconds)
    history = "".join(
        f"<tr><td>{scan['id']}</td><td>"
        f"<form method='post' action='/api/scan/rename'>"
        f"<input type='hidden' name='csrf_token' value='{csrf_token}'>"
        f"<input type='hidden' name='scan_id' value='{scan['id']}'>"
        f"<input name='name' maxlength='120' value='{html.escape(scan.get('name') or 'Unnamed scan', quote=True)}' "
        f"aria-label='Scan {scan['id']} name'><button type='submit'>Rename</button></form></td>"
        f"<td>{_time_markup(scan['started_at'])}</td>"
        f"<td>{html.escape(scan['requested_by'])}</td><td>{scan['status']}</td>"
        f"<td>{scan['device_count']}</td><td>{scan['point_count']}</td>"
        f"<td>{html.escape(scan['error'] or '')}</td><td><form method='post' action='/api/scan/delete'>"
        f"<input type='hidden' name='csrf_token' value='{csrf_token}'>"
        f"<input type='hidden' name='scan_id' value='{scan['id']}'>"
        f"<button type='submit' {'disabled' if scan['status'] == 'running' else ''}>Delete</button></form></td></tr>" for scan in data["scans"]
    )
    audit = "".join(
        f"<tr><td>{_time_markup(event['occurred_at'])}</td><td>{html.escape(event['action'])}</td>"
        f"<td>{html.escape(event['actor'] or 'system')}</td><td>{html.escape(event['detail'] or '')}</td></tr>"
        for event in data.get("audit_events", [])
    )
    saved_results = "".join(
        f"<details {'open' if index == 0 else ''}><summary><strong>{html.escape(scan.get('name') or 'Unnamed scan')}</strong> · "
        f"{html.escape(scan['status'])} · {scan['device_count']} devices · {scan['point_count']} points</summary>"
        f"<p>Started {_time_markup(scan['started_at'])} UTC · requested by {html.escape(scan['requested_by'])}</p>"
        f"<p class='bad'>{html.escape(scan['error'] or '')}</p>"
        f"{_scan_sections(scan['devices'], csrf_token, scan['status'] == 'running')}</details>"
        for index, scan in enumerate(data.get("saved_scans", []))
    )
    template = _read_asset(_asset_root(assets_dir), "templates/dashboard.html")
    replacements = {
        "{{HEARTBEAT_CLASS}}": heartbeat,
        "{{HEARTBEAT}}": _time_markup(state["heartbeat_at"]),
        "{{CYCLES}}": str(state["cycles_completed"]),
        "{{LAST_CYCLE}}": _time_markup(state["last_cycle_finished_at"]),
        "{{COLLECTOR_ERROR}}": (
            " · collector error: " + html.escape(state["fatal_error"])
            if state["fatal_error"] else ""
        ),
        "{{CSRF_TOKEN}}": html.escape(csrf_token, quote=True),
        "{{APPROVED_SECTIONS}}": _approved_sections(data["approved_devices"], stale_seconds, csrf_token),
        "{{SAVED_RESULTS}}": saved_results or '<p class="muted">No saved scans yet.</p>',
        "{{HISTORY}}": history or '<tr><td colspan="9">No scans requested</td></tr>',
        "{{AUDIT}}": audit or '<tr><td colspan="4">No audit events</td></tr>',
    }
    if any(marker not in template for marker in replacements):
        raise ValueError("dashboard template is missing a required placeholder")
    # Substitute once: controller text containing template markers is data.
    return re.sub(r"\{\{[A-Z_]+\}\}", lambda match: replacements.get(match[0], match[0]), template)


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
                    self._send_redirect_or_error(ok)
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
                    self._send_redirect_or_error(ok)
                    return
                if path == "/api/rescan/cancel":
                    cancelled = outer.cancel_scan()
                    if cancelled and "application/json" in self.headers.get("Accept", ""):
                        self._send(200, "application/json", b'{"accepted":true}')
                        return
                    self._send_redirect_or_error(cancelled, 409, b"no scan running\n")
                    return
                accepted = request_scan(self.client_address[0])
                if accepted and "application/json" in self.headers.get("Accept", ""):
                    self._send(202, "application/json", b'{"accepted":true}')
                else:
                    self._send_redirect_or_error(accepted, 409, b"scan already running\n")

            def _send_redirect_or_error(
                self, success: bool, error_status: int = 400, error_message: bytes = b"invalid\n"
            ) -> None:
                self._send(
                    303 if success else error_status,
                    "text/plain; charset=utf-8",
                    b"" if success else error_message,
                    {"Location": "/"} if success else None,
                )

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
