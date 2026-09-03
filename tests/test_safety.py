from __future__ import annotations

import ast
from pathlib import Path

from bacnet_console.bacnet import TrendOnlyView


class DummyScanClient:
    async def discover_all(self, response_window: float):
        return ()

    async def discover_target(self, instance: int, address: str, response_window: float):
        return None

    async def read(self, address: str, object_id: str, property_id: str):
        return None

    def close(self) -> None:
        pass


def test_trend_capability_has_no_discovery_or_control_surface() -> None:
    view = TrendOnlyView(DummyScanClient())
    public = {name for name in dir(view) if not name.startswith("_")}
    assert public == {"read", "read_multiple"}


def test_bacnet_source_contains_no_mutation_calls() -> None:
    paths = [
        Path("src/bacnet_console/bacnet.py"),
        Path("src/bacnet_console/collector.py"),
        Path("src/bacnet_console/scanner.py"),
        Path("src/bacnet_console/reads.py"),
    ]
    called: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called.update(
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        )
    forbidden = {
        name for name in called
        if name.startswith(("write_property", "subscribe", "command", "create_object", "delete_object"))
    }
    assert forbidden == set()


def test_no_automatic_scan_call_from_startup_or_collector() -> None:
    startup = Path("src/bacnet_console/cli.py").read_text(encoding="utf-8")
    collector = Path("src/bacnet_console/collector.py").read_text(encoding="utf-8")
    assert "request_scan(" in startup  # callback exists
    assert "scanner.request_scan(requested_by)" in startup
    assert "request_scan(" not in collector
    assert "discover" not in collector
