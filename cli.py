from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .bacnet import BacpypesReadOnlyClient
from .collector import Collector
from .config import ConfigError, load_config
from .dashboard import Dashboard
from .db import Store


async def async_main(config_path: str) -> None:
    config = load_config(config_path)
    store = Store(config.database_path)
    store.register_config(config.devices)
    client = BacpypesReadOnlyClient(config.bind, config.request_timeout_seconds)
    dashboard = Dashboard(store, config.dashboard_host, config.dashboard_port, config.stale_after_seconds)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    dashboard.start()
    try:
        await Collector(config, store, client).run(stop)
    finally:
        dashboard.close()
        client.close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Strictly read-only BACnet/IP collector")
    parser.add_argument("--config", default="/etc/bacnet-collector/config.yaml")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.check_config:
            load_config(args.config)
            print("configuration is valid")
            return
        asyncio.run(async_main(args.config))
    except ConfigError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
