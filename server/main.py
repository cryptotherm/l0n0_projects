"""
CT TestBench Server — FastAPI entrypoint.
"""

import asyncio
import logging
import structlog

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
import os
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from db.session import init_db, close_db, AsyncSessionLocal
from routers import discovery, miners, labels, sites, rgb, websocket as ws_router, pdu, bench_layout
from routers import settings as settings_router
from routers.chat import router as chat_router
from routers.scan import router as scan_router, set_led_host as scan_set_led, set_ws_manager as scan_set_ws
from routers.diagnostics import router as diagnostics_router
from routers.admin import router as admin_router
from services import barcode_scanner
from services.autoscan_service import autoscan_loop

# ── structured logging ────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logging.basicConfig(level=logging.INFO)
logger = structlog.get_logger()

@asynccontextmanager

async def lifespan(app: FastAPI):
    logger.info("CT TestBench server starting up")
    await init_db()
    # start UDP discovery listener in background
    from services.discovery_service import UDPDiscoveryService
    udp = UDPDiscoveryService()
    task = asyncio.create_task(udp.listen())
    app.state.udp_discovery = udp
    app.state.udp_task = task
    
    # Barcode scanner + LED host
    await barcode_scanner.start()
    
    # AutoScan loop — discover new miners every 60 seconds
    # DISABLED TEMPORARILY FOR DEBUGGING
    # autoscan_task = asyncio.create_task(autoscan_loop(scan_interval_s=60))
    # app.state.autoscan_task = autoscan_task
    # logger.info("AutoScan service started (60s interval)")
    logger.info("AutoScan service disabled for debugging")
    
    # Background miner stat poller (every 30s) — ALSO DISABLED
    # DISABLED TEMPORARILY FOR DEBUGGING
    async def _poll_known_miners():
        import asyncio
        from sqlalchemy import select
        from models.miner_model import MinerRecord
        from services.miner_client import collect_miner
        from datetime import datetime, timedelta

        async def poll_one(ip, miner_id):
            try:
                data = await asyncio.wait_for(collect_miner(ip), timeout=12.0)
                return miner_id, ip, data, None
            except Exception as e:
                return miner_id, ip, None, str(e)

        while True:
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(MinerRecord))
                    miners = result.scalars().all()
                    valid = [(m.id, m.ip_address) for m in miners if m.ip_address and m.ip_address != '0.0.0.0']

                    # Poll all miners concurrently
                    results = await asyncio.gather(*[poll_one(ip, mid) for mid, ip in valid])

                    # Map id -> result
                    res_map = {mid: (data, err) for mid, ip, data, err in results}

                    for m in miners:
                        if m.id not in res_map:
                            continue
                        data, err = res_map[m.id]
                        if data:
                            m.last_seen       = datetime.utcnow()
                            m.rt_hashrate     = data.rt_hashrate or 0
                            m.avg_hashrate    = data.avg_hashrate or 0
                            m.ideal_hashrate  = data.ideal_hashrate or m.ideal_hashrate
                            m.firmware_version= data.firmware_version or m.firmware_version
                            m.mac_address     = data.mac_address or m.mac_address
                            if data.worker:    m.worker   = data.worker
                            if data.pool_url:  m.pool_url = data.pool_url
                            # psu / fault fields
                            m.psu_fault           = data.psu_fault
                            m.control_board_fault = data.control_board_fault
                            m.dead_chips          = data.dead_chips or 0
                            if data.hashboards:
                                m.hashboards = [{"index": getattr(hb, "index", 0), "position": getattr(hb, "position", ""), "chip_count": getattr(hb, "chip_count", 0), "expected_chips": getattr(hb, "expected_chips", 0), "missing_chips": getattr(hb, "missing_chips", 0), "temp_avg": getattr(hb, "temp_avg", 0.0), "temp_max": getattr(hb, "temp_max", 0.0), "health_pct": getattr(hb, "health_pct", 100.0), "active": getattr(hb, "active", True)} for hb in data.hashboards]
                            m.last_seen       = datetime.utcnow()
                            rt = m.rt_hashrate or 0
                            avg = m.avg_hashrate or 0
                            if (rt > 0 or avg > 0) and m.status in ('offline', None, 'new'):
                                m.status = 'ready_to_deploy'
                            elif rt == 0 and avg == 0 and m.status == 'ready_to_deploy':
                                m.status = 'offline'
                        else:
                            # Unreachable — zero stale hashrate after 5 min
                            if m.last_seen and (datetime.utcnow() - m.last_seen) > timedelta(minutes=5):
                                m.rt_hashrate = 0
                                m.avg_hashrate = 0
                                if m.status == 'ready_to_deploy':
                                    m.status = 'offline'
                    await db.commit()
            except Exception as e:
                print("POLL ERR:", e)
            await asyncio.sleep(30)
    # DISABLED TEMPORARILY FOR DEBUGGING
    # asyncio.create_task(_poll_known_miners())
    logger.info("Miner stat poller disabled for debugging")

    # Always-on barcode listener — broadcasts every scan as barcode_scanned WS event
    async def _always_on_scanner():
        from services.websocket_manager import manager as _ws
        from sqlalchemy import select as _sel, or_
        from models.miner_model import MinerRecord

        queue = barcode_scanner.get_queue()
        while True:
            try:
                barcode = await queue.get()
                barcode = barcode.strip().upper()
                if not barcode:
                    continue
                # If a miner scan button is active, put it back for that consumer
                if barcode_scanner.is_exclusive():
                    # Hand off to scan/miner endpoint via Event
                    from routers.scan import set_miner_scan_barcode
                    set_miner_scan_barcode(barcode)
                    await asyncio.sleep(0.1)
                    continue

                # Look up miner by serial_number OR mac_address
                async with AsyncSessionLocal() as db:
                    is_mac = (len(barcode) == 17 and barcode.count(':') == 5)
                    if is_mac:
                        stmt = _sel(MinerRecord).where(MinerRecord.mac_address == barcode)
                    else:
                        stmt = _sel(MinerRecord).where(
                            or_(MinerRecord.serial_number == barcode,
                                MinerRecord.mac_address == barcode)
                        )
                    res = await db.execute(stmt)
                    miner = res.scalar_one_or_none()

                # Check if barcode is a shelf slot label (e.g. AL1-3, BL2-5)
                SLOT_LABELS = {
                    'AL1-1','AL1-2','AL1-3','AL1-4',
                    'BL2-5','BL2-6','BL2-7',
                    'CL2-1','CL2-2','CL2-3','CL2-4',
                    'DL1-5','DL1-6','DL1-7','DL1-8',
                }
                if barcode in SLOT_LABELS and miner is None:
                    # Look up miner by slot location
                    async with AsyncSessionLocal() as db2:
                        slot_res = await db2.execute(
                            _sel(MinerRecord).where(MinerRecord.location == barcode)
                        )
                        slot_miner = slot_res.scalar_one_or_none()
                    payload = {
                        "barcode": barcode,
                        "found": slot_miner is not None,
                        "miner_id": slot_miner.id if slot_miner else None,
                        "model": slot_miner.model if slot_miner else None,
                        "ip_address": slot_miner.ip_address if slot_miner else None,
                        "is_slot": True,
                        "slot_label": barcode,
                    }
                else:
                    payload = {
                        "barcode": barcode,
                        "found": miner is not None,
                        "miner_id": miner.id if miner else None,
                        "model": miner.model if miner else None,
                        "ip_address": miner.ip_address if miner else None,
                        "is_slot": False,
                    }
                await _ws.broadcast("barcode_scanned", payload)

            except Exception as e:
                await asyncio.sleep(0.5)

    asyncio.create_task(_always_on_scanner())

    led_host = os.getenv("LED_HOST", "http://10.0.0.1")
    scan_set_led(led_host)

    
    yield
    logger.info("CT TestBench server shutting down")
    task.cancel()
    autoscan_task.cancel()
    await close_db()

from version import get_build_info

app = FastAPI(
    title="CT TestBench API",
    description="Cryptotherm ASIC Miner Test Bench Platform",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/version")
async def api_version():
    """Current build version, branch, and channel."""
    return get_build_info()

app.include_router(admin_router)
app.include_router(discovery.router, prefix="/api/discovery", tags=["Discovery"])
app.include_router(scan_router)
app.include_router(miners.router, prefix="/api/miners", tags=["Miners"])
app.include_router(labels.router, prefix="/api/labels", tags=["Labels"])
app.include_router(sites.router, prefix="/api/sites", tags=["Sites"])
app.include_router(rgb.router, prefix="/api/rgb", tags=["RGB LED"])
app.include_router(ws_router.router, prefix="/ws", tags=["WebSocket"])
app.include_router(pdu.router, tags=["PDU"])
app.include_router(bench_layout.router, tags=["Bench"])
app.include_router(settings_router.router)
app.include_router(chat_router)
app.include_router(diagnostics_router)

_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    app.mount("/static", StaticFiles(directory=_static), name="static")


@app.get("/", include_in_schema=False)
@app.get("/kiosk", include_in_schema=False)
async def kiosk():
    resp = FileResponse(os.path.join(_static, "index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ct-testbench"}
