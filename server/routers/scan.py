"""
Scan router — barcode scan → flash LED → confirm/add miner flow.

Flow:
1. POST /api/scan/start         — kiosk tells server which slot user touched
2. ESP32 flashes that slot      — 3s white flash so user knows which miner
3. Slot goes yellow (scanning)  — waiting for barcode
4. User scans barcode            — scanner service emits barcode via queue
5. GET  /api/scan/result        — kiosk polls (or WS push) for result
6. Server matches barcode → DB:
   - Serial matches miner in slot  → ✅ confirmed
   - Serial found in DB, wrong slot → ⚠ mismatch (show history)
   - Serial not in DB              → 🆕 add it, assign to slot
   - No miner in slot              → 🆕 new miner at this slot
"""
from __future__ import annotations
import asyncio
import httpx
import structlog
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.session import get_db
from models.miner_model import MinerRecord
from services import barcode_scanner
from services import websocket_manager as _wsmod

logger = structlog.get_logger()
router = APIRouter(prefix="/api/scan", tags=["scan"])

# Injected at startup
_led_host: str = "http://10.0.0.1"

def set_ws_manager(mgr):
    pass  # uses module-level manager directly

def set_led_host(host: str):
    global _led_host
    _led_host = host


# ── In-memory scan session ────────────────────────────────────────────────────
class ScanSession(BaseModel):
    slot_idx: int
    miner_id: Optional[int] = None
    slot_label: Optional[str] = None
    started_at: datetime = datetime.utcnow()
    status: str = "waiting"   # waiting | scanned | done | cancelled
    barcode: Optional[str] = None
    result: Optional[dict] = None

_active_session: Optional[ScanSession] = None
_scan_task: Optional[asyncio.Task] = None


# ── Slot → LED index mapping (matches BENCH_LAYOUT in kiosk) ─────────────────
SLOT_LABELS = [
    "AL1-4","AL1-3","AL1-2","AL1-1",   # slots 0-3
    "BL2-7","BL2-6","BL2-5",           # slots 4-6
    "CL2-4","CL2-3","CL2-2","CL2-1",   # slots 7-10
    "DL1-8","DL1-7","DL1-6","DL1-5",   # slots 11-14
]


async def _flash_slot(slot_idx: int, color: str = "white", duration_ms: int = 2500):
    """Tell ESP32 to flash a slot a specific colour for duration_ms."""
    if not _led_host:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{_led_host}/flash", json={
                "slot": slot_idx,
                "color": color,
                "duration_ms": duration_ms,
            })
    except Exception as ex:
        logger.warning("LED flash failed", slot=slot_idx, error=str(ex))


async def _set_slot_color(slot_idx: int, color: str):
    """Set a slot to a persistent colour (yellow = scanning)."""
    if not _led_host:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{_led_host}/slot_color", json={
                "slot": slot_idx,
                "color": color,
            })
    except Exception as ex:
        logger.warning("LED set color failed", slot=slot_idx, error=str(ex))


async def _scan_waiter(session: ScanSession, db: AsyncSession):
    """Background task: wait for barcode, process it, push WS result."""
    global _active_session
    queue = barcode_scanner.get_queue()

    # 1. Flash white — "I see you"
    await _flash_slot(session.slot_idx, color="#ffffff", duration_ms=2500)
    await asyncio.sleep(2.5)

    # 2. Yellow — waiting for scan
    await _set_slot_color(session.slot_idx, color="#ffaa00")

    await _wsmod.manager.broadcast("scan_ready", {"slot": session.slot_idx, "label": session.slot_label})

    # 3. Wait for barcode (30s timeout)
    try:
        barcode = await asyncio.wait_for(queue.get(), timeout=30.0)
    except asyncio.TimeoutError:
        session.status = "cancelled"
        session.result = {"ok": False, "reason": "timeout", "message": "No scan received within 30s"}
        await _set_slot_color(session.slot_idx, color="restore")
        await _wsmod.manager.broadcast("scan_timeout", {"slot": session.slot_idx})
        return

    session.barcode = barcode
    session.status = "scanned"
    logger.info("Barcode received", barcode=barcode, slot=session.slot_idx)

    # 4. Process barcode against DB
    result = await _process_barcode(barcode, session, db)
    session.result = result
    session.status = "done"

    # 5. LED feedback
    if result["ok"]:
        await _flash_slot(session.slot_idx, color="#00ff00", duration_ms=1500)
    elif result.get("mismatch"):
        await _flash_slot(session.slot_idx, color="#ff6600", duration_ms=2000)
    else:
        await _flash_slot(session.slot_idx, color="#ff0000", duration_ms=2000)

    # Restore normal LED polling after flash
    await asyncio.sleep(2.0)
    await _set_slot_color(session.slot_idx, color="restore")

    # 6. Push result to kiosk
    await _wsmod.manager.broadcast("scan_result", {"slot": session.slot_idx, "barcode": barcode, **result})


async def _process_barcode(barcode: str, session: ScanSession, db: AsyncSession) -> dict:
    """Match barcode against DB and return result dict."""

    # Try serial number match first (most barcodes on Antminers are serial)
    stmt = select(MinerRecord).where(MinerRecord.serial_number == barcode)
    result = await db.execute(stmt)
    by_serial = result.scalar_one_or_none()

    # Also try MAC (some scanners read QR with MAC)
    by_mac = None
    if not by_serial and len(barcode) == 17 and barcode.count(':') == 5:
        stmt2 = select(MinerRecord).where(MinerRecord.mac_address == barcode.upper())
        result2 = await db.execute(stmt2)
        by_mac = result2.scalar_one_or_none()

    found = by_serial or by_mac
    match_field = "serial_number" if by_serial else ("mac_address" if by_mac else None)

    # Get miner currently assigned to this slot
    slot_miner = None
    if session.miner_id:
        stmt3 = select(MinerRecord).where(MinerRecord.id == session.miner_id)
        r3 = await db.execute(stmt3)
        slot_miner = r3.scalar_one_or_none()

    # ── Case 1: barcode matches miner already in this slot ──────────────────
    if found and slot_miner and found.id == slot_miner.id:
        return {
            "ok": True,
            "action": "confirmed",
            "message": f"✅ Confirmed: {found.model or 'Miner'} (S/N {barcode})",
            "miner": _miner_summary(found),
        }

    # ── Case 2: barcode found in DB but wrong slot ───────────────────────────
    if found:
        expected_label = found.location or "unassigned"
        return {
            "ok": False,
            "mismatch": True,
            "action": "mismatch",
            "message": f"⚠ Mismatch: this serial belongs to {found.model or 'Miner'} "
                       f"expected at slot {expected_label}, "
                       f"but you scanned it at slot {session.slot_label or session.slot_idx}",
            "miner": _miner_summary(found),
            "expected_slot": expected_label,
            "scanned_slot": session.slot_label,
            "prompt": "Is this miner in the wrong slot, or did you scan the wrong miner?",
        }

    # ── Case 3: new serial, slot has a known miner — update serial ───────────
    if slot_miner and not slot_miner.serial_number:
        slot_miner.serial_number = barcode
        await db.flush()
        await db.refresh(slot_miner)
        logger.info("Serial assigned to existing miner", miner_id=slot_miner.id, serial=barcode)
        return {
            "ok": True,
            "action": "serial_assigned",
            "message": f"✅ Serial {barcode} assigned to {slot_miner.model or 'Miner'} at {session.slot_label}",
            "miner": _miner_summary(slot_miner),
        }

    # ── Case 4: new serial, slot has a miner with different serial ───────────
    if slot_miner and slot_miner.serial_number and slot_miner.serial_number != barcode:
        return {
            "ok": False,
            "mismatch": True,
            "action": "serial_mismatch",
            "message": f"⚠ Serial mismatch: slot has {slot_miner.serial_number}, scanned {barcode}",
            "miner": _miner_summary(slot_miner),
            "db_serial": slot_miner.serial_number,
            "scanned_serial": barcode,
            "prompt": "Wrong miner in slot, or was this unit swapped out?",
        }

    # ── Case 5: completely new — create stub record ───────────────────────────
    new_miner = MinerRecord(
        ip_address="0.0.0.0",   # unknown until scanned
        serial_number=barcode,
        location=session.slot_label,
        status="new",
        last_seen=datetime.utcnow(),
    )
    db.add(new_miner)
    await db.flush()
    await db.refresh(new_miner)
    logger.info("New miner created from barcode scan", serial=barcode, slot=session.slot_label)
    return {
        "ok": True,
        "action": "created",
        "message": f"🆕 New miner added — S/N {barcode} at {session.slot_label}. "
                   f"Connect to network to auto-detect IP and model.",
        "miner": _miner_summary(new_miner),
    }


def _miner_summary(m: MinerRecord) -> dict:
    return {
        "id": m.id,
        "serial_number": m.serial_number,
        "mac_address": m.mac_address,
        "model": m.model,
        "ip_address": m.ip_address,
        "status": m.status,
        "location": m.location,
        "ip_history": m.ip_history,
    }


# ── API endpoints ─────────────────────────────────────────────────────────────

class StartScanRequest(BaseModel):
    slot_idx: int
    miner_id: Optional[int] = None
    slot_label: Optional[str] = None
    led_host: Optional[str] = None


@router.post("/start")
async def start_scan(req: StartScanRequest, db: AsyncSession = Depends(get_db)):
    global _active_session, _scan_task, _led_host

    # Cancel any existing session
    if _scan_task and not _scan_task.done():
        _scan_task.cancel()

    if req.led_host:
        _led_host = req.led_host

    session = ScanSession(
        slot_idx=req.slot_idx,
        miner_id=req.miner_id,
        slot_label=req.slot_label or (SLOT_LABELS[req.slot_idx] if req.slot_idx < len(SLOT_LABELS) else str(req.slot_idx)),
    )
    _active_session = session
    _scan_task = asyncio.create_task(_scan_waiter(session, db))

    return {
        "ok": True,
        "slot": session.slot_idx,
        "label": session.slot_label,
        "message": "Flashing slot — point scanner at miner barcode",
    }


@router.get("/result")
async def get_result():
    if not _active_session:
        raise HTTPException(status_code=404, detail="No active scan session")
    return {
        "status": _active_session.status,
        "slot": _active_session.slot_idx,
        "barcode": _active_session.barcode,
        "result": _active_session.result,
    }


@router.post("/cancel")
async def cancel_scan():
    global _scan_task, _active_session
    if _scan_task and not _scan_task.done():
        _scan_task.cancel()
    if _active_session:
        _active_session.status = "cancelled"
    _active_session = None
    return {"ok": True}


@router.post("/inject")
async def inject_barcode(barcode: str = Query(...)):
    """Dev/test endpoint — inject a barcode as if scanner read it."""
    await barcode_scanner.get_queue().put(barcode)
    return {"ok": True, "barcode": barcode}


# ── Direct miner scan (no slot required) ─────────────────────────────────────
# Shared slot for miner scan handoff from always-on scanner
# Queue for miner-scan barcode handoff from always-on scanner
# Recreated fresh per scan session (inside running loop)
_miner_scan_queue: asyncio.Queue = None

def set_miner_scan_barcode(bc: str):
    """Called by always-on scanner when exclusive mode is active."""
    if _miner_scan_queue is not None:
        try:
            _miner_scan_queue.put_nowait(bc)
        except asyncio.QueueFull:
            pass

@router.post("/miner/{miner_id}")
async def start_miner_scan(miner_id: int, db: AsyncSession = Depends(get_db)):
    """
    Start a 30s barcode scan session for a specific miner.
    The next barcode scanned gets saved as the serial_number on that miner.
    Returns the result directly (blocks until scan or timeout).
    """
    global _active_session, _miner_scan_barcode
    miner_session = ScanSession(slot_idx=-1, miner_id=miner_id, slot_label=f"miner-{miner_id}")
    miner_session.status = "waiting"
    _active_session = miner_session

    # Fresh queue per scan session — created inside running loop, no cross-loop issues
    global _miner_scan_queue
    _miner_scan_queue = asyncio.Queue(maxsize=1)
    barcode_scanner.set_exclusive(True)

    try:
        barcode = await asyncio.wait_for(_miner_scan_queue.get(), timeout=30.0)
    except asyncio.TimeoutError:
        miner_session.status = "done"
        miner_session.result = {"ok": False, "reason": "timeout", "message": "No scan received within 30s"}
        return miner_session.result
    finally:
        barcode_scanner.set_exclusive(False)
        _miner_scan_queue = None

    barcode = barcode.strip().upper()
    miner_session.barcode = barcode
    miner_session.status = "scanned"

    # Save to miner record
    stmt = select(MinerRecord).where(MinerRecord.id == miner_id)
    result = await db.execute(stmt)
    m = result.scalar_one_or_none()
    if not m:
        return {"ok": False, "reason": "not_found", "message": "Miner not found"}

    # Check if this serial already exists on another miner
    if len(barcode) != 17 or barcode.count(':') != 5:
        dup = await db.execute(
            select(MinerRecord).where(
                MinerRecord.serial_number == barcode,
                MinerRecord.id != miner_id
            )
        )
        dup_miner = dup.scalar_one_or_none()
        if dup_miner:
            return {
                "ok": False,
                "reason": "duplicate",
                "message": f"S/N {barcode} already in database (Miner ID {dup_miner.id})",
                "existing_id": dup_miner.id,
            }
        # Check if THIS miner already has this exact serial
        if m.serial_number == barcode:
            return {
                "ok": False,
                "reason": "already_set",
                "message": f"S/N {barcode} already saved on this miner",
            }

    # Detect if barcode looks like a MAC (17 chars with colons)
    if len(barcode) == 17 and barcode.count(':') == 5:
        m.mac_address = barcode.upper()
        field = "mac_address"
    else:
        m.serial_number = barcode
        field = "serial_number"
        if len(barcode) == 12 and all(c in '0123456789abcdefABCDEF' for c in barcode):
            formatted = ':'.join(barcode[i:i+2] for i in range(0, 12, 2)).upper()
            m.mac_address = formatted
            field = "serial+mac"

    await db.commit()
    result = {
        "ok": True,
        "barcode": barcode,
        "field": field,
        "message": f"Saved {field}: {barcode}",
        "miner_id": miner_id,
    }
    miner_session.result = result
    miner_session.status = "done"
    return result


# ── Free scan — match or new-miner popup ─────────────────────────────────────
@router.post("/free")
async def free_scan(db: AsyncSession = Depends(get_db)):
    """
    Wait for any barcode scan (30s).
    - If barcode matches a DB miner (by serial or MAC): return found=True + miner data.
    - If not found: return found=False + barcode so kiosk can show add-miner popup.
    """
    from services import barcode_scanner as _bs
    queue = _bs.get_queue()
    # Drain stale entries
    while not queue.empty():
        try: queue.get_nowait()
        except Exception: break

    try:
        barcode = await asyncio.wait_for(queue.get(), timeout=30.0)
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "timeout", "message": "No scan received within 30s"}

    barcode = barcode.strip()

    # Try serial match
    stmt = select(MinerRecord).where(MinerRecord.serial_number == barcode)
    r = await db.execute(stmt)
    found = r.scalar_one_or_none()

    # Try MAC match (with or without colons)
    if not found:
        mac = barcode.upper()
        if len(mac) == 12:  # no colons — format it
            mac = ':'.join(mac[i:i+2] for i in range(0, 12, 2))
        stmt2 = select(MinerRecord).where(MinerRecord.mac_address == mac)
        r2 = await db.execute(stmt2)
        found = r2.scalar_one_or_none()

    if found:
        return {
            "ok": True,
            "found": True,
            "barcode": barcode,
            "miner": _miner_summary(found),
        }
    else:
        return {
            "ok": True,
            "found": False,
            "barcode": barcode,
            "message": f"Barcode '{barcode}' not found in database",
        }
