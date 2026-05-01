"""
WebSocket router — real-time push updates to iOS app.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from services.websocket_manager import manager
import structlog

logger = structlog.get_logger()
router = APIRouter()


@router.websocket("/miners")
async def ws_miners(ws: WebSocket):
    """
    WebSocket endpoint for real-time miner status updates.
    
    Connect to: ws://<host>:8000/ws/miners
    
    Message format (server → client):
    {
        "event": "miner_update" | "miner_discovered" | "diagnosis_complete",
        "data": { ... }
    }
    """
    await manager.connect(ws)
    try:
        while True:
            # Keep connection alive, handle ping/pong from client
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as e:
        logger.error("WebSocket error", error=str(e))
        await manager.disconnect(ws)
