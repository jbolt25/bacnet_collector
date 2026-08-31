from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.parse import urlsplit

from .db import Store


def _age_class(value: str | None, stale_seconds: float) -> str:
    if not value:
        return "bad"
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    except ValueError:
        return "bad"
    return "ok" if age <= stale_seconds else "warn"


def render_status(data: dict[str, Any], stale_seconds: float) -> str:
    state = data["collector"]
    device_blocks = []
    for device in data["devices"]:
        rows = []
        for point in device["points"]:
            health = "bad" if point["last_error"] else _age_class(point["last_success_at"], stale_seconds)
            rows.append(
                "<tr>"
                f"<td>{html.escape(point['name'])}</td>"
                f"<td><code>{html.escape(point['object_id'])} / {html.escape(point['property_id'])}</code></td>"
                f"<td>{html.escape(point['last_value_text'] or '—')} {html.escape(point['units'] or '')}</td>"
                f"<td class='{health}'>{'error' if point['last_error'] else 'ok' if health == 'ok' else 'stale'}</td>"
                f"<td>{html.escape(point['last_success_at'] or 'never')}</td>"
                f"<td>{html.escape(point['last_error'] or '')}</td>"
                "</tr>"
            )
        address = device["current_address"] or "unresolved"
        device_blocks.append(
            f"<section><h2>{html.escape(device['name'])}</h2>"
            f"<p>Device {device['instance']} · {html.escape(address)} · last seen {html.escape(device['last_seen_at'] or 'never')}</p>"
            "<table><thead><tr><th>Point</th><th>BACnet object</th><th>Value</th><th>Health</th><th>Last success (UTC)</th><th>Error</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )
    heartbeat_class = _age_class(state["heartbeat_at"], stale_seconds)
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta http-equiv='refresh' content='30'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>BACnet collector status</title>
<style>body{{font:15px system-ui;margin:2rem;max-width:1200px;color:#20252b}}header{{display:flex;gap:1rem;align-items:center}}
.badge{{padding:.25rem .6rem;border-radius:1rem;background:#e8edf2}}.ok{{color:#08783e}}.warn{{color:#9a6500}}.bad{{color:#b42318}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:.55rem;border-bottom:1px solid #d9dee3}}section{{margin-top:2rem}}
code{{font-size:.9em}}@media(max-width:700px){{table{{display:block;overflow:auto}}body{{margin:1rem}}}}</style></head>
<body><header><h1>Read-only BACnet collector</h1><span class='badge {heartbeat_class}'>heartbeat {html.escape(state['heartbeat_at'] or 'never')}</span></header>
<p>Cycles completed: {state['cycles_completed']} · started: {html.escape(state['started_at'] or 'never')} · no controls are available</p>
{''.join(device_blocks)}<p><a href='/api/status'>JSON status</a></p></body></html>"""


class Dashboard:
    def __init__(self, store: Store, host: str, port: int, stale_seconds: float) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.stale_seconds = stale_seconds
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/healthz":
                    self._send(200, "text/plain; charset=utf-8", b"ok\n")
                elif path == "/api/status":
                    body = json.dumps(outer.store.status(), separators=(",", ":")).encode()
                    self._send(200, "application/json", body)
                elif path == "/":
                    body = render_status(outer.store.status(), outer.stale_seconds).encode()
                    self._send(200, "text/html; charset=utf-8", body)
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found\n")

            def _deny(self) -> None:
                self._send(405, "text/plain; charset=utf-8", b"read-only dashboard\n", {"Allow": "GET"})

            do_POST = do_PUT = do_PATCH = do_DELETE = _deny

            def _send(self, status: int, content_type: str, body: bytes, extra: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = Thread(target=self._server.serve_forever, name="dashboard", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

