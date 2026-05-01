"""
UDP discovery service — listens for broadcast pings from miners when the IP button is pressed.
Also handles manual IP scan.
"""

from __future__ import annotations
import asyncio
import socket
import struct
import json
import ipaddress
import structlog
from typing import Callable, Optional

logger = structlog.get_logger()

DISCOVERY_PORT = 14235
DISCOVERY_MAGIC = b"CTMINER"

# Callbacks registered by router
_callbacks: list[Callable] = []


def register_callback(cb: Callable):
    _callbacks.append(cb)


def deregister_callback(cb: Callable):
    _callbacks.remove(cb)


class UDPDiscoveryService:
    """
    Listens on UDP port for miner broadcasts.
    Antminer broadcasts a UDP packet on the network when the IP button is pressed.
    Packet format varies by model — we accept any UDP packet from port 14235
    and extract the source IP.
    """

    def __init__(self, port: int = DISCOVERY_PORT):
        self.port = port
        self._running = False
        self.discovered_ips: set[str] = set()

    async def listen(self):
        self._running = True
        logger.info("UDP discovery listener started", port=self.port)

        loop = asyncio.get_event_loop()

        # Create UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        try:
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            logger.error("Could not bind UDP discovery port", port=self.port, error=str(e))
            return

        try:
            while self._running:
                try:
                    data, addr = await loop.run_in_executor(None, lambda: self._recv(sock))
                    if data is not None:
                        ip = addr[0]
                        logger.info("UDP discovery: miner broadcast received", ip=ip)
                        self.discovered_ips.add(ip)
                        for cb in _callbacks:
                            try:
                                await cb(ip, data)
                            except Exception as e:
                                logger.error("Discovery callback error", error=str(e))
                except Exception as e:
                    if self._running:
                        logger.error("UDP discovery loop error", error=str(e))
                    await asyncio.sleep(0.1)
        finally:
            sock.close()

    def _recv(self, sock: socket.socket):
        """Blocking recv with a short timeout so we can check _running."""
        sock.settimeout(1.0)
        try:
            return sock.recvfrom(4096)
        except socket.timeout:
            return None, None

    def stop(self):
        self._running = False


async def scan_ip_range(
    start_ip: str,
    end_ip: str,
    concurrency: int = 50,
    timeout: float = 3.0,
) -> list[str]:
    """
    Scan an IP range for reachable miners (try CGMiner port 4028 + HTTP port 80).
    Returns list of reachable IPs.
    """
    start = ipaddress.IPv4Address(start_ip)
    end = ipaddress.IPv4Address(end_ip)

    ips = [str(ipaddress.IPv4Address(i)) for i in range(int(start), int(end) + 1)]
    logger.info("Scanning IP range", start=start_ip, end=end_ip, count=len(ips))

    sem = asyncio.Semaphore(concurrency)
    results = []

    async def probe(ip: str) -> Optional[str]:
        async with sem:
            for port in (4028, 80):
                try:
                    _, wr = await asyncio.wait_for(
                        asyncio.open_connection(ip, port), timeout=timeout
                    )
                    wr.close()
                    return ip
                except Exception:
                    pass
        return None

    tasks = [probe(ip) for ip in ips]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        if result:
            results.append(result)

    logger.info("IP range scan complete", found=len(results))
    return results


async def scan_cidr(cidr: str, **kwargs) -> list[str]:
    network = ipaddress.IPv4Network(cidr, strict=False)
    hosts = list(network.hosts())
    if not hosts:
        return []
    return await scan_ip_range(str(hosts[0]), str(hosts[-1]), **kwargs)
