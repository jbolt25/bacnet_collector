from __future__ import annotations

from pathlib import Path

import pytest


BASE_CONFIG = """
network:
  bind: 192.168.50.10/24
  request_timeout_seconds: 1
  inter_request_delay_seconds: 0
poll_interval_seconds: 300
scan:
  response_window_seconds: 1
  max_devices: 10
  max_objects_per_device: 10
storage:
  database: data/test.sqlite3
dashboard:
  host: 192.168.50.10
  port: 8080
devices:
  - name: AHU
    instance: 1001
    points:
      - name: SAT
        object: analog-input,1
        property: present-value
        units: F
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    return path

