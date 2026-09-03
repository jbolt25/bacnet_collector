from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
import math
import subprocess
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


PRIVATE_NETWORKS = tuple(
    IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
IP_COMMAND = "/usr/sbin/ip"


def _resolve_auto_bind() -> IPv4Interface:
    try:
        route = subprocess.run(
            [IP_COMMAND, "-o", "-4", "route", "get", "1.1.1.1"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        interface_name = route[route.index("dev") + 1]
        source_address = route[route.index("src") + 1]
        addresses = subprocess.run(
            [IP_COMMAND, "-o", "-4", "addr", "show", "dev", interface_name, "scope", "global"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError) as exc:
        raise ConfigError("network.bind auto could not resolve the active IPv4 interface") from exc

    for line in addresses:
        fields = line.split()
        if "inet" not in fields:
            continue
        candidate = IPv4Interface(fields[fields.index("inet") + 1])
        if str(candidate.ip) == source_address:
            return candidate
    raise ConfigError("network.bind auto could not match the active IPv4 address")


@dataclass(frozen=True)
class PointConfig:
    name: str
    object_id: str
    property_id: str = "present-value"
    units: str | None = None


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    instance: int
    address: str | None
    points: tuple[PointConfig, ...]


@dataclass(frozen=True)
class ConsoleConfig:
    bind: str
    poll_interval_seconds: float
    request_timeout_seconds: float
    inter_request_delay_seconds: float
    scan_timeout_seconds: float
    scan_max_duration_seconds: float
    scan_max_devices: int
    scan_max_objects_per_device: int
    database_path: Path
    retention_days: int
    dashboard_host: str
    dashboard_port: int
    stale_after_seconds: float
    devices: tuple[DeviceConfig, ...]
    # Maximum number of point values packed into one ReadPropertyMultiple call.
    rpm_batch_size: int = 20


def _number(raw: dict[str, Any], key: str, default: float, minimum: float) -> float:
    value = raw.get(key, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum):
        raise ConfigError(f"{key} must be a number >= {minimum}")
    return float(value)


def _positive_int(raw: dict[str, Any], key: str, default: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ConfigError(f"{key} must be an integer from 1 to {maximum}")
    return value


def _manual_address(value: Any, where: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}.address must be an IPv4 address, optionally with :port")
    host, separator, port_text = value.partition(":")
    try:
        IPv4Address(host)
    except ValueError as exc:
        raise ConfigError(f"{where}.address is not a valid IPv4 address") from exc
    if separator and (not port_text.isdigit() or not 1 <= int(port_text) <= 65535):
        raise ConfigError(f"{where}.address has an invalid UDP port")
    return value


def load_config(path: str | Path) -> ConsoleConfig:
    config_path = Path(path).resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    network = raw.get("network", {})
    scan = raw.get("scan", {})
    storage = raw.get("storage", {})
    dashboard = raw.get("dashboard", {})
    if not all(isinstance(section, dict) for section in (network, scan, storage, dashboard)):
        raise ConfigError("network, scan, storage, and dashboard must be mappings")

    bind = network.get("bind", "auto")
    if not isinstance(bind, str):
        raise ConfigError("network.bind must be auto or an IPv4 interface such as 192.168.50.10/24")
    if bind == "auto":
        bind_interface = _resolve_auto_bind()
        bind = str(bind_interface)
    else:
        try:
            bind_interface = IPv4Interface(bind)
        except ValueError as exc:
            raise ConfigError("network.bind must be auto or a valid IPv4 interface with prefix length") from exc
    if not any(bind_interface.ip in private for private in PRIVATE_NETWORKS):
        raise ConfigError("network.bind must use an RFC1918 private LAN address")

    dashboard_host = dashboard.get("host", "auto")
    if dashboard_host == "auto":
        dashboard_host = str(bind_interface.ip)
    if dashboard_host != str(bind_interface.ip):
        raise ConfigError("dashboard.host must exactly match the host address in network.bind")
    dashboard_port = dashboard.get("port", 8080)
    if isinstance(dashboard_port, bool) or not isinstance(dashboard_port, int) or not 1 <= dashboard_port <= 65535:
        raise ConfigError("dashboard.port must be an integer from 1 to 65535")

    db_value = storage.get("database", "data/console.sqlite3")
    if not isinstance(db_value, str) or not db_value.strip():
        raise ConfigError("storage.database must be a path")
    database_path = Path(db_value)
    if not database_path.is_absolute():
        database_path = (config_path.parent / database_path).resolve()

    devices_raw = raw.get("devices", [])
    if not isinstance(devices_raw, list):
        raise ConfigError("devices must be a list")
    devices: list[DeviceConfig] = []
    seen_instances: set[int] = set()
    for device_index, item in enumerate(devices_raw):
        where = f"devices[{device_index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping")
        name = item.get("name")
        instance = item.get("instance")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where}.name is required")
        if isinstance(instance, bool) or not isinstance(instance, int) or not 0 <= instance <= 4194302:
            raise ConfigError(f"{where}.instance must be from 0 to 4194302")
        if instance in seen_instances:
            raise ConfigError(f"duplicate device instance {instance}")
        seen_instances.add(instance)
        address = _manual_address(item.get("address"), where)
        points_raw = item.get("points", [])
        if not isinstance(points_raw, list):
            raise ConfigError(f"{where}.points must be a list")
        points: list[PointConfig] = []
        point_names: set[str] = set()
        for point_index, point in enumerate(points_raw):
            point_where = f"{where}.points[{point_index}]"
            if not isinstance(point, dict):
                raise ConfigError(f"{point_where} must be a mapping")
            point_name = point.get("name")
            object_id = point.get("object")
            property_id = point.get("property", "present-value")
            if not isinstance(point_name, str) or not point_name.strip():
                raise ConfigError(f"{point_where}.name is required")
            if point_name in point_names:
                raise ConfigError(f"duplicate point name {point_name!r} in {name}")
            point_names.add(point_name)
            if not isinstance(object_id, str) or "," not in object_id:
                raise ConfigError(f"{point_where}.object must look like analog-input,1")
            obj_type, obj_instance = object_id.rsplit(",", 1)
            if not obj_type or not obj_instance.isdigit() or not 0 <= int(obj_instance) <= 4194302:
                raise ConfigError(f"{point_where}.object is invalid")
            if not isinstance(property_id, str) or not property_id:
                raise ConfigError(f"{point_where}.property is required")
            units = point.get("units")
            if units is not None and not isinstance(units, str):
                raise ConfigError(f"{point_where}.units must be text")
            points.append(PointConfig(point_name, object_id, property_id, units))
        devices.append(DeviceConfig(name, instance, address, tuple(points)))

    retention_days = storage.get("retention_days", 90)
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
        raise ConfigError("storage.retention_days must be an integer >= 1")

    poll = _number(raw, "poll_interval_seconds", 300, 60)
    timeout = _number(network, "request_timeout_seconds", 5, 0.5)
    request_gap = _number(network, "inter_request_delay_seconds", 0.25, 0)
    scan_timeout = _number(scan, "response_window_seconds", 5, 1)
    scan_max_duration = _number(scan, "max_duration_seconds", 1800, 30)
    rpm_batch_size = _positive_int(network, "read_multiple_batch_size", 20, 100)
    stale = _number(dashboard, "stale_after_seconds", max(poll * 3, 900), poll)
    return ConsoleConfig(
        bind=bind,
        poll_interval_seconds=poll,
        request_timeout_seconds=timeout,
        inter_request_delay_seconds=request_gap,
        scan_timeout_seconds=scan_timeout,
        scan_max_duration_seconds=scan_max_duration,
        scan_max_devices=_positive_int(scan, "max_devices", 50, 500),
        scan_max_objects_per_device=_positive_int(scan, "max_objects_per_device", 200, 5000),
        database_path=database_path,
        retention_days=retention_days,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        stale_after_seconds=stale,
        devices=tuple(devices),
        rpm_batch_size=rpm_batch_size,
    )
