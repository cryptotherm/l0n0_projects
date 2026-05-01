"""
routers/diagnostics.py — Slot diagnostic flow v2

State machine per slot:
  idle → scanning → awaiting_serial → cycling_N → escalated → resolved / diagnosed

New in v2:
  - POST /api/diag/slot/{label}/escalate  → fires OpenClaw event to cthome
  - POST /api/diag/slot/{label}/ai_result → cthome posts LLM result back here
  - GET  /api/diag/miner/{id}/history     → full diagnostic history for a miner
  - LED flash on slot when AI result arrives (operator attention)
  - All runs persisted to miner_diagnostics table
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from models.miner_model import MinerRecord

logger = logging.getLogger("diagnostics")
router = APIRouter(prefix="/api/diag", tags=["diagnostics"])

POWER_CYCLE_WAIT_S = 180
MAX_CYCLES = 3

# cthome OpenClaw gateway (wake endpoint)
CTHOME_GW = os.environ.get("CTHOME_GATEWAY_URL", "http://100.85.100.85:18789")
CTHOME_TOKEN = os.environ.get("CTHOME_GATEWAY_TOKEN", "b567cf381c2db1911654e6f51c78ee5bcdd7eee15c5cb0e6")

# Pi's own base URL (for cthome callback)
PI_BASE_URL = os.environ.get("PI_BASE_URL", "http://100.115.130.43:8000")

# LED host
_led_host: str = os.environ.get("LED_HOST", "http://10.0.0.1")

SLOT_OUTLET_MAP = {
    "AL1-4": 4, "AL1-3": 3, "AL1-2": 2, "AL1-1": 1,
    "BL2-7": 7, "BL2-6": 6, "BL2-5": 5,
    "CL2-4": 4, "CL2-3": 3, "CL2-2": 2, "CL2-1": 1,
    "DL1-8": 8, "DL1-7": 7, "DL1-6": 6, "DL1-5": 5,
}

SLOT_LED_IDX = {
    "AL1-4": 0, "AL1-3": 1, "AL1-2": 2, "AL1-1": 3,
    "BL2-7": 4, "BL2-6": 5, "BL2-5": 6,
    "CL2-4": 7, "CL2-3": 8, "CL2-2": 9, "CL2-1": 10,
    "DL1-8": 11,"DL1-7": 12,"DL1-6": 13,"DL1-5": 14,
}

_active: dict[str, dict] = {}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _pdu():
    try:
        from apdu_client import get_pdu
        return get_pdu()
    except Exception as e:
        raise HTTPException(503, f"PDU unavailable: {e}")


async def _flash_slot(slot_label: str, color: str = "amber", duration_ms: int = 5000):
    idx = SLOT_LED_IDX.get(slot_label.upper())
    if idx is None or not _led_host:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{_led_host}/flash", json={
                "slot": idx, "color": color, "duration_ms": duration_ms,
            })
    except Exception:
        pass


async def _probe_ip(ip: str) -> Optional[dict]:
    try:
        import sys
        sys.path.insert(0, os.path.dirname(__file__) + "/..")
        from miner_client import detect_miner
        return await asyncio.wait_for(detect_miner(ip), timeout=15)
    except Exception:
        return None


async def _persist_diag(state: dict, db_factory):
    """Write or update a miner_diagnostics row."""
    try:
        async with db_factory() as db:
            diag_id = state.get("diag_db_id")
            if diag_id:
                await db.execute(text("""
                    UPDATE miner_diagnostics SET
                        scan_found_ip   = :ip,
                        scan_found_mac  = :mac,
                        pdu_current_a   = :amps,
                        pdu_power_w     = :watts,
                        llm_severity    = :sev,
                        llm_summary     = :summary,
                        llm_suggested_fix = :fix,
                        llm_auto_fixable  = :auto,
                        llm_response    = :resp,
                        resolved        = :resolved,
                        power_cycles    = :cycles,
                        sticker_printed = :sticker
                    WHERE id = :id
                """), {
                    "id": diag_id,
                    "ip": state.get("ip"),
                    "mac": state.get("found_mac"),
                    "amps": state.get("pdu_amps"),
                    "watts": state.get("pdu_watts"),
                    "sev": state.get("llm_severity"),
                    "summary": state.get("llm_summary"),
                    "fix": state.get("llm_fix"),
                    "auto": state.get("llm_auto_fixable", False),
                    "resp": state.get("llm_response"),
                    "resolved": state.get("status") == "resolved",
                    "cycles": state.get("cycle_count", 0),
                    "sticker": state.get("sticker_printed", False),
                })
            else:
                result = await db.execute(text("""
                    INSERT INTO miner_diagnostics
                        (miner_id, slot_label, triggered_by, pdu_outlet, pdu_on)
                    VALUES (:mid, :slot, :trig, :outlet, :on)
                    RETURNING id
                """), {
                    "mid": state.get("miner_id"),
                    "slot": state.get("slot_label"),
                    "trig": state.get("triggered_by", "scan_miss"),
                    "outlet": SLOT_OUTLET_MAP.get(state.get("slot_label", "").upper()),
                    "on": True,
                })
                row = result.fetchone()
                if row:
                    state["diag_db_id"] = row[0]
            await db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist diagnostic: {e}")


async def _fire_openclaw_escalation(state: dict):
    """Send a wake event to cthome OpenClaw gateway to run the LLM diagnostic agent."""
    slot = state.get("slot_label", "?")
    ip = state.get("ip", "?")
    serial = state.get("serial", "?")
    miner_id = state.get("miner_id")
    pdu_amps = state.get("pdu_amps", 0)
    pdu_watts = state.get("pdu_watts", 0)
    outlet = SLOT_OUTLET_MAP.get(slot.upper(), "?")
    cycles = state.get("cycle_count", 0)
    log_summary = "\n".join(state.get("log", [])[-10:])

    event_text = f"""[MINER DIAGNOSTIC ESCALATION]
Slot: {slot}
IP: {ip}
Serial: {serial}
DB miner_id: {miner_id}
PDU outlet: {outlet}  current: {pdu_amps}A  power: {pdu_watts}W
Power cycles attempted: {cycles}
Pi base URL: {PI_BASE_URL}

Recent scan log:
{log_summary}

TASK: Run a full diagnostic on this miner.
1. ARP scan all known subnets (192.168.3.0/28, 192.168.2.0/28, 192.168.5.0/28, 192.168.6.0/28, 10.0.0.0/24) on cthome to find the miner's IP/MAC
2. If found, cross-reference with the Pi DB via GET {PI_BASE_URL}/api/miners (filter by mac/serial)
3. Pull miner history: status, hashrate, repair count, previous diagnostics from {PI_BASE_URL}/api/diag/miner/{miner_id}/history
4. Pull PDU data from {PI_BASE_URL}/api/pdu/outlets
5. Run a full LLM reasoning pass with ALL context gathered — what is likely wrong with this miner? Long-term prognosis? Recommended repair steps?
6. POST the result to {PI_BASE_URL}/api/diag/slot/{slot}/ai_result with JSON body:
   {{
     "severity": "ok|warning|critical|unknown",
     "summary": "...",
     "suggested_fix": "...",
     "auto_fixable": false,
     "full_response": "...",
     "found_ip": "...",
     "found_mac": "..."
   }}
"""

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{CTHOME_GW}/api/cron/wake",
                headers={"Authorization": f"Bearer {CTHOME_TOKEN}"},
                json={"text": event_text, "mode": "now"},
            )
            state["log"].append(f"OpenClaw escalation fired → cthome (HTTP {resp.status_code})")
            return resp.status_code < 300
    except Exception as e:
        state["log"].append(f"Escalation failed: {e}")
        return False


async def _run_diagnostic(slot_label: str, ip: str, pdu_amps: float, pdu_watts: float, db_factory):
    """Background diagnostic state machine."""
    state = _active[slot_label]
    state["pdu_amps"] = pdu_amps
    state["pdu_watts"] = pdu_watts
    outlet = SLOT_OUTLET_MAP.get(slot_label.upper())

    await _persist_diag(state, db_factory)

    # ── Step 1: probe IP for MAC ────────────────────────────────────────────
    state["status"] = "scanning"
    state["log"].append(f"Probing {ip}…")
    info = await _probe_ip(ip)

    async with db_factory() as db:
        if info and info.get("mac_address"):
            state["found_mac"] = info["mac_address"]
            result = await db.execute(
                select(MinerRecord).where(MinerRecord.mac_address == info["mac_address"].upper())
            )
            miner = result.scalar_one_or_none()
            if miner:
                state["status"] = "resolved"
                state["miner_id"] = miner.id
                state["serial"] = miner.serial_number
                state["log"].append(f"✓ Matched miner #{miner.id} ({miner.serial_number})")
                miner.location = slot_label
                miner.ip_address = ip
                await db.commit()
                await _persist_diag(state, db_factory)
                return

        # ── Step 2: prompt for serial ──────────────────────────────────────
        state["status"] = "awaiting_serial"
        state["log"].append("No MAC match — scan miner serial barcode…")

        for _ in range(300):  # 5 min timeout
            await asyncio.sleep(1)
            if state.get("serial_submitted") or state.get("cancelled"):
                break

        if state.get("cancelled"):
            return
        if not state.get("serial_submitted"):
            state["status"] = "timed_out"
            state["log"].append("Timed out waiting for serial")
            return

        serial = state.get("serial", "")
        state["log"].append(f"Serial: {serial}")

        # Look up or create miner record
        result = await db.execute(
            select(MinerRecord).where(MinerRecord.serial_number == serial)
        )
        miner = result.scalar_one_or_none()
        if miner:
            state["miner_id"] = miner.id
            state["log"].append(f"Found miner #{miner.id}")
        else:
            miner = MinerRecord(
                serial_number=serial, ip_address=ip, location=slot_label,
                status="unknown",
                first_seen=datetime.now(timezone.utc),
                last_seen=datetime.now(timezone.utc),
            )
            db.add(miner)
            await db.commit()
            await db.refresh(miner)
            state["miner_id"] = miner.id
            state["log"].append(f"Created new miner record #{miner.id}")

        miner.location = slot_label
        miner.ip_address = ip
        await db.commit()
        await _persist_diag(state, db_factory)

        # ── Step 3: power cycle loop ───────────────────────────────────────
        if not outlet:
            state["log"].append("No PDU outlet mapped — escalating directly")
        else:
            for attempt in range(1, MAX_CYCLES + 1):
                state["status"] = f"cycling_{attempt}"
                state["cycle_count"] = attempt
                state["log"].append(f"Power cycle {attempt}/{MAX_CYCLES} on outlet {outlet}…")
                try:
                    pdu = _pdu()
                    pdu.outlet_off(outlet)
                    await asyncio.sleep(5)
                    pdu.outlet_on(outlet)
                    state["log"].append(f"Cycled — waiting {POWER_CYCLE_WAIT_S}s for boot…")
                except Exception as e:
                    state["log"].append(f"PDU error: {e}")

                state["cycle_resume_at"] = (
                    datetime.now(timezone.utc).timestamp() + POWER_CYCLE_WAIT_S
                )
                await asyncio.sleep(POWER_CYCLE_WAIT_S)
                if state.get("cancelled"):
                    return

                state["log"].append(f"Re-probing {ip}…")
                info = await _probe_ip(ip)
                if info and info.get("mac_address"):
                    state["status"] = "resolved"
                    state["log"].append(f"✓ Responded after cycle {attempt}")
                    miner.status = "needs_repair"
                    miner.mac_address = info["mac_address"]
                    miner.model = info.get("model") or miner.model
                    await db.commit()
                    await _persist_diag(state, db_factory)
                    return
                state["log"].append(f"No response after cycle {attempt}")

        # ── Step 4: escalate to cthome LLM ────────────────────────────────
        state["status"] = "escalated"
        state["log"].append("Escalating to cthome for LLM diagnostic…")
        await _fire_openclaw_escalation(state)
        await _persist_diag(state, db_factory)

        # DISABLED: Short sticker removed. Using full FAIL label format instead.
        # await _print_diag_sticker(slot_label, serial, miner.id,
        #     f"No network after {MAX_CYCLES} power cycles.\nCheck PSU and control board.\nLLM diagnostic in progress…",
        #     state)
        await _persist_diag(state, db_factory)


# _print_diag_sticker() REMOVED — using full FAIL label format instead


# ── API endpoints ─────────────────────────────────────────────────────────────

class StartDiagRequest(BaseModel):
    ip: str
    pdu_amps: float = 0.0
    pdu_watts: float = 0.0
    triggered_by: str = "scan_miss"

class SerialSubmitRequest(BaseModel):
    serial: str

class AiResultRequest(BaseModel):
    severity: str = "unknown"
    summary: str = ""
    suggested_fix: str = ""
    auto_fixable: bool = False
    full_response: str = ""
    found_ip: Optional[str] = None
    found_mac: Optional[str] = None


@router.post("/slot/{slot_label}/start")
async def start_diagnostic(slot_label: str, req: StartDiagRequest, db: AsyncSession = Depends(get_db)):
    label = slot_label.upper()
    if label in _active and _active[label].get("status") not in (
            "resolved", "diagnosed", "timed_out", "cancelled", "escalated"):
        raise HTTPException(409, "Diagnostic already active for this slot")

    state = {
        "slot_label": label, "ip": req.ip, "status": "starting",
        "started_at": _now_iso(), "serial": None, "serial_submitted": False,
        "miner_id": None, "cycle_count": 0, "cycle_resume_at": None,
        "pdu_amps": req.pdu_amps, "pdu_watts": req.pdu_watts,
        "triggered_by": req.triggered_by,
        "sticker_printed": False, "cancelled": False,
        "llm_severity": None, "llm_summary": None, "llm_fix": None,
        "llm_response": None, "diag_db_id": None, "log": [],
    }
    _active[label] = state

    from db.session import AsyncSessionLocal
    asyncio.create_task(_run_diagnostic(label, req.ip, req.pdu_amps, req.pdu_watts, AsyncSessionLocal))
    return {"ok": True, "slot": label}


@router.post("/slot/{slot_label}/serial")
async def submit_serial(slot_label: str, req: SerialSubmitRequest):
    label = slot_label.upper()
    if label not in _active:
        raise HTTPException(404, "No active diagnostic")
    state = _active[label]
    if state["status"] != "awaiting_serial":
        raise HTTPException(400, f"Not awaiting serial (status: {state['status']})")
    state["serial"] = req.serial.strip()
    state["serial_submitted"] = True
    return {"ok": True}


@router.post("/slot/{slot_label}/ai_result")
async def receive_ai_result(slot_label: str, req: AiResultRequest, db: AsyncSession = Depends(get_db)):
    """cthome posts LLM diagnostic result back here."""
    label = slot_label.upper()
    state = _active.get(label, {
        "slot_label": label, "log": [], "miner_id": None,
        "ip": req.found_ip, "found_mac": req.found_mac,
        "diag_db_id": None, "sticker_printed": False,
        "cycle_count": 0, "pdu_amps": 0, "pdu_watts": 0,
        "triggered_by": "escalated",
    })
    _active[label] = state

    state["status"] = "diagnosed"
    state["llm_severity"] = req.severity
    state["llm_summary"] = req.summary
    state["llm_fix"] = req.suggested_fix
    state["llm_auto_fixable"] = req.auto_fixable
    state["llm_response"] = req.full_response
    if req.found_ip:
        state["ip"] = req.found_ip
    if req.found_mac:
        state["found_mac"] = req.found_mac
    state["log"].append(f"[LLM] Severity: {req.severity}")
    state["log"].append(f"[LLM] {req.summary[:120]}")

    # Update miner record with AI fields
    miner_id = state.get("miner_id")
    if miner_id:
        result = await db.execute(select(MinerRecord).where(MinerRecord.id == miner_id))
        miner = result.scalar_one_or_none()
        if miner:
            miner.ai_issue_summary = req.summary
            miner.ai_suggested_fix = req.suggested_fix
            miner.ai_auto_fixable = req.auto_fixable
            miner.ai_severity = req.severity
            if req.found_mac:
                miner.mac_address = req.found_mac.upper()
            if req.found_ip:
                miner.ip_address = req.found_ip
            await db.commit()

    from db.session import AsyncSessionLocal
    await _persist_diag(state, AsyncSessionLocal)

    # Flash slot LED to alert operator
    asyncio.create_task(_flash_slot(label, "amber", 10000))

    return {"ok": True}


@router.get("/slot/{slot_label}/status")
async def get_status(slot_label: str):
    label = slot_label.upper()
    if label not in _active:
        raise HTTPException(404, "No diagnostic for this slot")
    return _active[label]


@router.post("/slot/{slot_label}/cancel")
async def cancel_diagnostic(slot_label: str):
    label = slot_label.upper()
    if label in _active:
        _active[label]["cancelled"] = True
        _active[label]["status"] = "cancelled"
    return {"ok": True}


@router.get("/miner/{miner_id}/history")
async def get_miner_history(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Return all diagnostic runs for a miner (newest first)."""
    try:
        result = await db.execute(text("""
            SELECT id, slot_label, triggered_by, run_at,
                   scan_found_ip, scan_found_mac,
                   pdu_current_a, pdu_power_w, pdu_on,
                   llm_severity, llm_summary, llm_suggested_fix, llm_auto_fixable,
                   resolved, power_cycles, sticker_printed
            FROM miner_diagnostics
            WHERE miner_id = :mid
            ORDER BY run_at DESC
            LIMIT 50
        """), {"mid": miner_id})
        rows = result.fetchall()
        return {"miner_id": miner_id, "history": [dict(r._mapping) for r in rows]}
    except Exception as e:
        # Table may not exist yet
        return {"miner_id": miner_id, "history": [], "error": str(e)}


@router.get("/active")
async def list_active():
    return {
        "diagnostics": [
            {k: v for k, v in s.items() if k != "log"}
            for s in _active.values()
            if s.get("status") not in ("resolved", "cancelled")
        ]
    }
