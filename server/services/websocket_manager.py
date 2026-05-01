"""
WebSocket connection manager — broadcasts real-time miner updates to iOS app and web clients.
"""

from __future__ import annotations
import json
import asyncio
import structlog
from fastapi import WebSocket

logger = structlog.get_logger()


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.append(ws)
        logger.info("WebSocket client connected", total=len(self.active))

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)
        logger.info("WebSocket client disconnected", total=len(self.active))

    async def broadcast(self, event: str, data: dict):
        message = json.dumps({"event": event, "data": data})
        dead = []
        async with self._lock:
            clients = list(self.active)
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception as e:
                logger.debug("WebSocket send failed", error=str(e))
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def broadcast_miner_update(self, miner_data: dict):
        await self.broadcast("miner_update", miner_data)

    async def broadcast_discovery(self, ip: str, miner_type: str = "unknown"):
        await self.broadcast("miner_discovered", {"ip_address": ip, "miner_type": miner_type})

    async def broadcast_diagnosis_complete(self, miner_id: int, severity: str):
        await self.broadcast("diagnosis_complete", {"miner_id": miner_id, "severity": severity})


# Singleton
manager = ConnectionManager()
