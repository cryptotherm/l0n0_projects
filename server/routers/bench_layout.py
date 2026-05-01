"""
routers/bench_layout.py — PDU outlet ↔ shelf position mapping + power fingerprinting

Endpoints:
  GET  /api/bench/layout          → full bench layout (outlets + miner assignments + live power)
  POST /api/bench/outlet/{n}/assign → assign outlet to shelf position
  POST /api/bench/outlet/{n}/unassign
  POST /api/bench/outlet/{n}/cycle   → power cycle via PDU
  POST /api/bench/outlet/{n}/on
  POST /api/bench/outlet/{n}/off
  POST /api/bench/identify        → scan all outlets, match power draw → miner model
  GET  /api/bench/fingerprint     → known power fingerprints per model
"""

import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from db.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

logger = logging.getLogger("bench_layout")
router = APIRouter(prefix="/api/bench", tags=["bench"])

# ── Power fingerprints (watts at full load) ───────────────────────────────────
# Measured ranges — use ±15% tolerance for matching
POWER_FINGERPRINTS = [
    {"model": "Antminer S19 XP",     "watts_min": 3050, "watts_max": 3350, "th": 140},
    {"model": "Antminer S19 Pro",     "watts_min": 2900, "watts_max": 3200, "th": 110},
    {"model": "Antminer S19a Pro",    "watts_min": 2750, "watts_max": 3050, "th": 100},
    {"model": "Antminer S19",         "watts_min": 2800, "watts_max": 3100, "th": 95},
    {"model": "Antminer S19j Pro",    "watts_min": 2800, "watts_max": 3100, "th": 104},
    {"model": "Whatsminer M30S+",     "watts_min": 3200, "watts_max": 3600, "th": 100},
    {"model": "Whatsminer M30S++ VE", "watts_min": 3400, "watts_max": 3800, "th": 112},
    {"model": "Whatsminer M50",       "watts_min": 3200, "watts_max": 3600, "th": 126},
    {"model": "Antminer S21",         "watts_min": 3400, "watts_max": 3800, "th": 200},
    {"model": "Antminer T19",         "watts_min": 2700, "watts_max": 3000, "th": 84},
    # Startup / idle states
    {"model": "BOOTING",              "watts_min": 50,   "watts_max": 300,  "th": 0},
    {"model": "IDLE/STANDBY",         "watts_min": 300,  "watts_max": 600,  "th": 0},
]

def fingerprint_power(watts: float) -> Optional[dict]:
    """Match a wattage reading to a known miner model."""
    if watts < 10:
        return None  # nothing connected / off
    for fp in POWER_FINGERPRINTS:
        if fp["watts_min"] <= watts <= fp["watts_max"]:
            return fp
    # Fuzzy match — find closest
    closest = min(POWER_FINGERPRINTS, key=lambda f: abs(watts - (f["watts_min"] + f["watts_max"]) / 2))
    mid = (closest["watts_min"] + closest["watts_max"]) / 2
    if abs(watts - mid) / mid < 0.20:  # within 20%
        return {**closest, "fuzzy": True}
    return {"model": "UNKNOWN", "watts_min": 0, "watts_max": 0, "th": 0}


# ── PDU helpers ───────────────────────────────────────────────────────────────
def _get_pdu():
    try:
        from apdu_client import get_pdu
        return get_pdu()
    except Exception as e:
        raise HTTPException(503, detail=f"PDU unavailable: {e}")


def _get_db():
    try:
        from database import get_db_connection
        return get_db_connection()
    except Exception:
        import asyncpg, os
        return None  # handled per-endpoint


async def _db():
    import asyncpg
    return await asyncpg.connect('postgresql://testbench:motmaster@localhost/testbench')


# ── Models ────────────────────────────────────────────────────────────────────
class OutletAssign(BaseModel):
    shelf_label: Optional[str] = None   # e.g. "A1", "B3", "Shelf-2-Left"
    shelf_row: Optional[int] = None     # numeric row on bench
    shelf_col: Optional[int] = None     # numeric column/position
    miner_id: Optional[int] = None      # FK to miners.id
    notes: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/layout")
async def get_bench_layout():
    """
    Returns full bench layout: all 24 outlets with shelf assignments,
    live PDU power readings, and matched miner (by IP or fingerprint).
    """
    pdu = _get_pdu()
    snap = pdu.snapshot()
    db = await _db()

    try:
        # Get all outlet assignments
        rows = await db.fetch("""
            SELECT p.*, m.ip_address, m.model, m.ct_id, m.status, m.rt_hashrate,
                   m.ai_severity, m.ai_issue_summary
            FROM pdu_outlets p
            LEFT JOIN miners m ON p.miner_id = m.id
            ORDER BY p.outlet_num
        """)

        # Build outlet map from PDU snapshot
        pdu_outlets = {o.outlet: o for o in snap.outlets}

        result = []
        for row in rows:
            n = row["outlet_num"]
            pdu_o = pdu_outlets.get(n)
            watts = pdu_o.power_w if pdu_o else 0
            current_a = pdu_o.current_a if pdu_o else 0
            on = pdu_o.on if pdu_o else False
            energy_kwh = pdu_o.energy_kwh if pdu_o else 0

            fp = fingerprint_power(watts) if on else None

            outlet_data = {
                "outlet_num": n,
                "outlet_name": (getattr(pdu_o,"name","") or getattr(pdu_o,"label","") or row["outlet_name"] or ""),
                "shelf_label": row["shelf_label"],
                "shelf_row": row["shelf_row"],
                "shelf_col": row["shelf_col"],
                "notes": row["notes"],
                # Live power
                "on": on,
                "watts": round(watts, 1),
                "current_a": round(current_a, 2),
                "energy_kwh": round(energy_kwh, 1),
                "current_alarm": pdu_o.current_alarm if pdu_o else False,
                # Power fingerprint
                "power_fingerprint": fp["model"] if fp else None,
                "fingerprint_fuzzy": fp.get("fuzzy", False) if fp else False,
                # Assigned miner
                "miner": {
                    "id": row["id"] if row["miner_id"] else None,
                    "ip": row["ip_address"],
                    "model": row["model"],
                    "ct_id": row["ct_id"],
                    "status": row["status"],
                    "hashrate_th": row["rt_hashrate"],
                    "ai_severity": row["ai_severity"],
                    "ai_summary": row["ai_issue_summary"],
                } if row["miner_id"] else None,
            }
            result.append(outlet_data)

        # Summary stats
        total_w = sum(o["watts"] for o in result)
        active_outlets = sum(1 for o in result if o["watts"] > 50)

        return {
            "pdu_host": "10.0.0.226",
            "total_power_w": round(total_w, 1),
            "total_power_kw": round(total_w / 1000, 2),
            "active_outlets": active_outlets,
            "phases": [
                {"phase": p.phase, "current_a": p.current_a, "voltage_v": p.voltage_v,
                 "power_w": p.power_w, "energy_kwh": p.energy_kwh}
                for p in snap.phases
            ],
            "outlets": result,
        }
    finally:
        await db.close()


@router.post("/outlet/{n}/assign")
async def assign_outlet(n: int, body: OutletAssign):
    """Assign an outlet to a shelf position and/or miner."""
    if not 1 <= n <= 24:
        raise HTTPException(400, "Outlet must be 1-24")
    db = await _db()
    try:
        await db.execute("""
            UPDATE pdu_outlets
            SET shelf_label=$1, shelf_row=$2, shelf_col=$3,
                miner_id=$4, notes=$5, updated_at=NOW()
            WHERE outlet_num=$6 AND pdu_host='10.0.0.226'
        """, body.shelf_label, body.shelf_row, body.shelf_col,
             body.miner_id, body.notes, n)
        return {"ok": True, "outlet": n, "shelf": body.shelf_label}
    finally:
        await db.close()


@router.post("/outlet/{n}/unassign")
async def unassign_outlet(n: int):
    db = await _db()
    try:
        await db.execute("""
            UPDATE pdu_outlets
            SET shelf_label=NULL, shelf_row=NULL, shelf_col=NULL,
                miner_id=NULL, updated_at=NOW()
            WHERE outlet_num=$1 AND pdu_host='10.0.0.226'
        """, n)
        return {"ok": True, "outlet": n}
    finally:
        await db.close()


@router.post("/outlet/{n}/cycle")
async def cycle_outlet(n: int):
    pdu = _get_pdu()
    result = pdu.cycle_outlet(n)
    return {"ok": result, "outlet": n, "action": "cycle"}


@router.post("/outlet/{n}/on")
async def outlet_on(n: int):
    pdu = _get_pdu()
    result = pdu.outlet_on(n)
    return {"ok": result, "outlet": n, "action": "on"}


@router.post("/outlet/{n}/off")
async def outlet_off(n: int):
    pdu = _get_pdu()
    result = pdu.outlet_off(n)
    return {"ok": result, "outlet": n, "action": "off"}


@router.post("/identify")
async def identify_miners():
    """
    Power fingerprint identification:
    1. Read live wattage from all active outlets
    2. Match to known miner models by power draw
    3. Cross-reference with known miner IPs to suggest assignments
    4. Return suggestions for user to confirm
    """
    pdu = _get_pdu()
    snap = pdu.snapshot()
    db = await _db()

    try:
        # Get known miners
        miners = await db.fetch("""
            SELECT id, ip_address, model, ct_id, status, rt_hashrate
            FROM miners ORDER BY ct_id
        """)
        miner_map = {m["ip_address"]: dict(m) for m in miners}

        suggestions = []
        for outlet in snap.outlets:
            if not outlet.on or outlet.power_w < 10:
                continue
            fp = fingerprint_power(outlet.power_w)
            # Find network-visible miner matching this fingerprint
            network_match = None
            for ip, m in miner_map.items():
                model_lower = (m["model"] or "").lower()
                fp_lower = (fp["model"] if fp else "").lower()
                # Rough model family match
                if fp and any(kw in fp_lower for kw in ["s19", "antminer"]) and "antminer" in model_lower:
                    network_match = m
                    break
                elif fp and "whatsminer" in fp_lower and "whatsminer" in model_lower:
                    network_match = m
                    break

            suggestions.append({
                "outlet_num": outlet.outlet,
                "watts": round(outlet.power_w, 1),
                "current_a": round(outlet.current_a, 2),
                "fingerprint": fp["model"] if fp else "unknown",
                "fingerprint_fuzzy": fp.get("fuzzy", False) if fp else False,
                "estimated_th": fp["th"] if fp else None,
                "network_miner_suggestion": network_match,
                "confidence": "high" if fp and not fp.get("fuzzy") else "medium" if fp else "low",
            })

        return {
            "scanned_outlets": len(suggestions),
            "suggestions": suggestions,
            "note": "Use POST /api/bench/outlet/{n}/assign to confirm assignments"
        }
    finally:
        await db.close()


@router.get("/fingerprint")
async def get_fingerprints():
    """Return known power fingerprint database."""
    return {"fingerprints": POWER_FINGERPRINTS}

# ── Power timeline endpoints (added by power_analyzer) ───────────────────────

@router.get('/outlet/{n}/timeline')
async def outlet_timeline(n: int, minutes: int = 60):
    """Power draw timeline — watts over time for ramp analysis."""
    from services.power_analyzer import get_analyzer
    analyzer = get_analyzer()
    data = await analyzer.get_outlet_timeline(n, minutes)
    state = analyzer.outlet_states.get(n)
    return {
        'outlet_num': n,
        'current_state': state.state if state else 'unknown',
        'current_watts': round(state.last_watts, 1) if state else 0,
        'peak_watts': round(state.peak_watts, 1) if state else 0,
        'time_in_state_s': round(state.time_in_state()) if state else 0,
        'minutes': minutes,
        'points': len(data),
        'timeline': data,
    }

@router.get('/outlet/{n}/events')
async def outlet_events(n: int, limit: int = 50):
    """Power state transition events — boot, fault, hashboard loss, etc."""
    from services.power_analyzer import get_analyzer
    analyzer = get_analyzer()
    return {'outlet_num': n, 'events': await analyzer.get_outlet_events(n, limit)}

@router.get('/states')
async def all_states():
    """Current power state of all 24 outlets."""
    from services.power_analyzer import get_analyzer
    analyzer = get_analyzer()
    return {'states': await analyzer.get_all_current_states()}


@router.post("/autodetect")
async def autodetect_shelf_positions(db=Depends(get_db)):
    """
    Auto-detect which miner is on which PDU outlet.
    For each outlet drawing >2A: power it off, wait 15s, check which
    known miner IP went unreachable, map it, power back on, settle 10s.
    Uses TCP port 80 check (fast) rather than full HTTP probe.
    """
    import asyncio
    from sqlalchemy import select as _sel, text
    from models.miner_model import MinerRecord

    async def tcp_reachable(ip: str, port: int = 80, timeout: float = 2.0) -> bool:
        """Fast TCP connect check — no HTTP overhead."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    pdu = _get_pdu()
    snap = pdu.snapshot()

    # Only test outlets drawing >2A (real miners)
    active_outlets = [o for o in snap.outlets
                      if o.on and o.current_a > 2.0]
    if not active_outlets:
        raise HTTPException(400, detail="No outlets drawing >2A — are miners powered on?")

    # Load all non-archived miners with IPs
    result = await db.execute(_sel(MinerRecord).where(
        MinerRecord.ip_address != None,
        MinerRecord.status.notin_(["archived", "parts_unit"])
    ))
    all_miners = result.scalars().all()

    # Baseline reachability — only keep miners that respond right now
    baseline = await asyncio.gather(*[tcp_reachable(m.ip_address) for m in all_miners])
    reachable_ips = {m.ip_address for m, ok in zip(all_miners, baseline) if ok}
    reachable_miners = [m for m in all_miners if m.ip_address in reachable_ips]

    if not reachable_miners:
        raise HTTPException(400, detail="No miners are reachable on the network right now")

    mappings = []

    for outlet in active_outlets:
        n = outlet.outlet
        outlet_name = getattr(outlet, "name", "") or getattr(outlet, "label", "") or f"outlet{n}"

        # Power OFF
        try:
            pdu.outlet_off(n)
        except Exception as e:
            mappings.append({"outlet_num": n, "outlet_name": outlet_name, "error": str(e)})
            continue

        # Wait for the miner to drop — Antminers take 10-20s after power cut
        await asyncio.sleep(15)

        # Check which previously-reachable miners are now offline
        recheck = await asyncio.gather(*[tcp_reachable(m.ip_address) for m in reachable_miners])
        went_down = [m for m, ok in zip(reachable_miners, recheck) if not ok]

        # Power back ON
        try:
            pdu.outlet_on(n)
        except Exception:
            pass

        # Settle before next outlet test
        await asyncio.sleep(10)

        if len(went_down) == 1:
            miner = went_down[0]
            # Write to DB
            await db.execute(
                text("""UPDATE pdu_outlets
                           SET miner_id=:mid, shelf_label=:slot, updated_at=NOW()
                         WHERE outlet_num=:n"""),
                {"mid": miner.id, "slot": outlet_name, "n": n}
            )
            miner.location = outlet_name
            mappings.append({
                "outlet_num": n,
                "outlet_name": outlet_name,
                "miner_id": miner.id,
                "miner_ip": miner.ip_address,
                "model": miner.model,
                "worker": miner.worker or "",
                "confirmed": True,
            })
        elif len(went_down) == 0:
            mappings.append({
                "outlet_num": n,
                "outlet_name": outlet_name,
                "result": "no miner went offline — try longer wait or check IP",
                "confirmed": False,
            })
        else:
            mappings.append({
                "outlet_num": n,
                "outlet_name": outlet_name,
                "result": f"ambiguous — {len(went_down)} miners went offline simultaneously",
                "candidates": [m.ip_address for m in went_down],
                "confirmed": False,
            })

    await db.commit()
    return {
        "mappings": mappings,
        "total_outlets_tested": len(active_outlets),
        "miners_confirmed": sum(1 for m in mappings if m.get("confirmed")),
    }



# ── SLOT → SUBNET MAP ────────────────────────────────────────────────────────
# Each rack is isolated on its own /28 via managed switch VLANs
# Pi eth0 is on 192.168.3.13 (Rack A subnet) — can reach all rack subnets via routing
_SLOT_SUBNET_MAP = {
    "AL1-1": "192.168.3.0/28", "AL1-2": "192.168.3.0/28",
    "AL1-3": "192.168.3.0/28", "AL1-4": "192.168.3.0/28",
    "BL2-5": "192.168.2.0/28", "BL2-6": "192.168.2.0/28", "BL2-7": "192.168.2.0/28",
    "CL2-1": "192.168.5.0/28", "CL2-2": "192.168.5.0/28",
    "CL2-3": "192.168.5.0/28", "CL2-4": "192.168.5.0/28",
    "DL1-5": "192.168.6.0/28", "DL1-6": "192.168.6.0/28",
    "DL1-7": "192.168.6.0/28", "DL1-8": "192.168.6.0/28",
}


@router.post("/slot/{slot_label}/listen_ip")
async def listen_for_ip(slot_label: str, db: AsyncSession = Depends(_db)):
    """Single-poll: scan subnet for this slot, return any new IP not in DB."""
    import re
    from sqlalchemy import text as _text

    label = slot_label.upper()
    subnet_prefix = _SLOT_SUBNET_MAP.get(label)
    if not subnet_prefix:
        return {"status": "waiting", "reason": f"Unknown slot {label}"}

    result = await db.execute(
        _text("SELECT ip_address FROM miners WHERE ip_address NOT IN ('0.0.0.0','') AND status NOT IN ('archived','ready_to_deploy')")
    )
    known_ips = {row[0] for row in result.fetchall()}

    subnet = _SLOT_SUBNET_MAP.get(label, "192.168.3.0/28")
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmap", "-sn", "-T4", "--host-timeout", "3s", subnet,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        text_out = stdout.decode()
    except Exception as e:
        logger.warning("nmap failed: %s", e)
        return {"status": "waiting"}

    found_ips = re.findall(r"Nmap scan report for [\w\-\.]+\s*\(?(\d+\.\d+\.\d+\.\d+)\)?", text_out)
    if not found_ips:
        found_ips = re.findall(r"(\d+\.\d+\.\d+\.\d+)", text_out)

    for ip in found_ips:
        if ip not in known_ips and not ip.endswith(".0") and not ip.endswith(".255"):
            logger.info("listen_ip: new IP %s for slot %s", ip, label)
            return {"status": "found", "ip": ip, "slot": label}

    return {"status": "waiting"}
