from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


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
class CollectorConfig:
    bind: str
    poll_interval_seconds: float
    request_timeout_seconds: float
    inter_request_delay_seconds: float
    discovery_enabled: bool
    discovery_interval_seconds: float
    database_path: Path
    retention_days: int
    dashboard_host: str
    dashboard_port: int
    stale_after_seconds: float
    devices: tuple[DeviceConfig, ...]


def _number(raw: dict[str, Any], key: str, default: float, minimum: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"{key} must be a number >= {minimum}")
    return float(value)


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
    if separator:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ConfigError(f"{where}.address has an invalid UDP port")
    return value


def load_config(path: str | Path) -> CollectorConfig:
    config_path = Path(path).resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    network = raw.get("network", {})
    storage = raw.get("storage", {})
    dashboard = raw.get("dashboard", {})
    if not all(isinstance(section, dict) for section in (network, storage, dashboard)):
        raise ConfigError("network, storage, and dashboard must be mappings")

    bind = network.get("bind")
    if not isinstance(bind, str):
        raise ConfigError("network.bind must be an IPv4 interface such as 192.0.2.10/24")
    try:
        IPv4Interface(bind)
    except ValueError as exc:
        raise ConfigError("network.bind must be a valid IPv4 interface with prefix length") from exc

    dashboard_host = dashboard.get("host", "127.0.0.1")
    if dashboard_host != "127.0.0.1":
        raise ConfigError("dashboard.host must be 127.0.0.1")
    dashboard_port = dashboard.get("port", 8080)
    if isinstance(dashboard_port, bool) or not isinstance(dashboard_port, int) or not 1 <= dashboard_port <= 65535:
        raise ConfigError("dashboard.port must be an integer from 1 to 65535")

    db_value = storage.get("database", "data/collector.sqlite3")
    if not isinstance(db_value, str) or not db_value.strip():
        raise ConfigError("storage.database must be a path")
    database_path = Path(db_value)
    if not database_path.is_absolute():
        database_path = (config_path.parent / database_path).resolve()

    devices_raw = raw.get("devices")
    if not isinstance(devices_raw, list) or not devices_raw:
        raise ConfigError("devices must contain at least one approved device")
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

        points_raw = item.get("points")
        if not isinstance(points_raw, list) or not points_raw:
            raise ConfigError(f"{where}.points must contain at least one approved point")
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

    discovery_enabled = network.get("discovery_enabled", True)
    if not isinstance(discovery_enabled, bool):
        raise ConfigError("network.discovery_enabled must be true or false")
    if any(device.address is None for device in devices) and not discovery_enabled:
        raise ConfigError("a device without address requires discovery_enabled: true")

    retention_days = storage.get("retention_days", 90)
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
        raise ConfigError("storage.retention_days must be an integer >= 1")

    poll = _number(raw, "poll_interval_seconds", 60, 10)
    timeout = _number(network, "request_timeout_seconds", 5, 0.5)
    inter_request_delay = _number(network, "inter_request_delay_seconds", 0.1, 0)
    discovery_interval = _number(network, "discovery_interval_seconds", 900, 60)
    stale = _number(dashboard, "stale_after_seconds", max(poll * 3, 180), poll)
    return CollectorConfig(
        bind=bind,
        poll_interval_seconds=poll,
        request_timeout_seconds=timeout,
        inter_request_delay_seconds=inter_request_delay,
        discovery_enabled=discovery_enabled,
        discovery_interval_seconds=discovery_interval,
        database_path=database_path,
        retention_days=retention_days,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        stale_after_seconds=stale,
        devices=tuple(devices),
    )
