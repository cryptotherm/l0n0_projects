"""
Continuous network polling service — runs in the background, periodically
scans the configured CIDR for miners and triggers a quick test on any new ones found.
Can be paused/resumed via the API.
"""

from __future__ import annotations
import asyncio
import structlog
from typing import Optional
from services.discovery_service import scan_cidr

logger = structlog.get_logger()

_task: Optional[asyncio.Task] = None
_paused: bool = False
_cidr: str = "10.0.0.0/24"
_interval: int = 60  # seconds between scans
_last_scan: Optional[str] = None
_found_this_scan: int = 0
_total_found: int = 0
_status: str = "idle"

# Injected by startup — callable(ip) -> None
_test_callback = None


def set_test_callback(cb):
    global _test_callback
    _test_callback = cb


def get_status() -> dict:
    return {
        "running": _task is not None and not _task.done(),
        "paused": _paused,
        "cidr": _cidr,
        "interval_seconds": _interval,
        "last_scan": _last_scan,
        "found_this_scan": _found_this_scan,
        "total_found": _total_found,
        "status": _status,
    }


def set_config(cidr: str = None, interval: int = None):
    global _cidr, _interval
    if cidr:
        _cidr = cidr
    if interval:
        _interval = interval


def pause():
    global _paused
    _paused = True
    logger.info("Continuous polling paused")


def resume():
    global _paused
    _paused = False
    logger.info("Continuous polling resumed")


async def _poll_loop():
    global _last_scan, _found_this_scan, _total_found, _status

    logger.info("Continuous miner polling started", cidr=_cidr, interval=_interval)

    while True:
        if _paused:
            _status = "paused"
            await asyncio.sleep(2)
            continue

        _status = "scanning"
        logger.info("Polling scan started", cidr=_cidr)

        try:
            ips = await scan_cidr(_cidr, concurrency=50)
            _found_this_scan = len(ips)
            _total_found += _found_this_scan
            _last_scan = asyncio.get_event_loop().time().__str__()[:10]

            logger.info("Polling scan complete", found=len(ips), cidr=_cidr)

            if ips and _test_callback:
                for ip in ips:
                    try:
                        await _test_callback(ip)
                    except Exception as e:
                        logger.warning("Test callback error", ip=ip, error=str(e))

            _status = "waiting"
        except Exception as e:
            logger.error("Polling scan error", error=str(e))
            _status = "error"

        await asyncio.sleep(_interval)


async def start(cidr: str = None, interval: int = None):
    global _task
    if cidr or interval:
        set_config(cidr=cidr, interval=interval)
    if _task and not _task.done():
        logger.info("Polling already running")
        return
    _task = asyncio.create_task(_poll_loop())
    logger.info("Continuous polling task created", cidr=_cidr, interval=_interval)


async def stop():
    global _task, _status
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
    _status = "stopped"
    logger.info("Continuous polling stopped")
