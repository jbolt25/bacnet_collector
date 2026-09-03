from __future__ import annotations

from pathlib import Path

import pytest

from ipaddress import IPv4Interface

from bacnet_console.config import ConfigError, load_config
from conftest import BASE_CONFIG


def test_private_lan_bind_and_allowlist_load(config_file: Path) -> None:
    config = load_config(config_file)
    assert config.dashboard_host == "192.168.50.10"
    assert config.poll_interval_seconds == 300
    assert config.devices[0].points[0].object_id == "analog-input,1"


def test_empty_discovery_only_config_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    start = BASE_CONFIG.index("\ndevices:\n")
    path.write_text(BASE_CONFIG[: start + 1] + "devices: []\n", encoding="utf-8")
    assert load_config(path).devices == ()


def test_auto_bind_uses_active_private_interface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "auto.yaml"
    path.write_text(
        BASE_CONFIG.replace("192.168.50.10/24", "auto").replace("host: 192.168.50.10", "host: auto"),
        encoding="utf-8",
    )
    monkeypatch.setattr("bacnet_console.config._resolve_auto_bind", lambda: IPv4Interface("10.14.2.84/22"))
    config = load_config(path)
    assert config.bind == "10.14.2.84/22"
    assert config.dashboard_host == "10.14.2.84"


@pytest.mark.parametrize(
    "old,new,match",
    [
        ("192.168.50.10/24", "0.0.0.0/0", "private LAN"),
        ("host: 192.168.50.10", "host: 0.0.0.0", "must exactly match"),
        ("host: 192.168.50.10", "host: 192.168.50.11", "must exactly match"),
        ("poll_interval_seconds: 300", "poll_interval_seconds: 30", "poll_interval_seconds"),
        ("max_devices: 10", "max_devices: 0", "max_devices"),
    ],
)
def test_rejects_unsafe_configuration(tmp_path: Path, old: str, new: str, match: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(BASE_CONFIG.replace(old, new), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_config(path)
