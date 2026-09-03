from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .bacnet import BacpypesScanClient, TrendOnlyView
from .collector import Collector
from .config import ConfigError, load_config
from .dashboard import Dashboard
from .db import Store
from .scanner import ScanManager
from .reads import ReadScheduler


async def async_main(config_path: str) -> None:
    config = load_config(config_path)
    store = Store(config.database_path)
    store.register_config(config.devices)
    transport = BacpypesScanClient(config.bind, config.request_timeout_seconds)
    trend_client = TrendOnlyView(transport)
    reader = ReadScheduler(trend_client, config)
    scanner = ScanManager(config, store, transport, reader)
    collector = Collector(config, store, trend_client, reader)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_scan(requested_by: str) -> bool:
        async def on_loop() -> bool:
            return scanner.request_scan(requested_by)

        future = asyncio.run_coroutine_threadsafe(on_loop(), loop)
        try:
            return future.result(timeout=2)
        except TimeoutError:
            future.cancel()
            return False

    def cancel_scan() -> bool:
        future = asyncio.run_coroutine_threadsafe(scanner.cancel(), loop)
        try:
            return bool(future.result(timeout=2))
        except TimeoutError:
            future.cancel()
            return False

    dashboard = Dashboard(
        store, config.dashboard_host, config.dashboard_port, config.stale_after_seconds,
        request_scan, cancel_scan,
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    dashboard.start()
    try:
        await collector.run(stop)
    finally:
        dashboard.close()
        await scanner.cancel("scan cancelled during shutdown")
        transport.close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Strictly read-only BACnet/IP console")
    parser.add_argument("--config", default="/etc/bacnet-console/config.yaml")
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
