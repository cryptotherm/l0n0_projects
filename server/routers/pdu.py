"""
routers/pdu.py — APDU Smart PDU endpoints for CT TestBench API

Endpoints:
  GET  /api/pdu/snapshot              → full PDU status (phases + outlets)
  GET  /api/pdu/outlets               → outlets only
  POST /api/pdu/outlet/{n}/on         → turn outlet N on
  POST /api/pdu/outlet/{n}/off        → turn outlet N off
  POST /api/pdu/outlet/{n}/cycle      → power-cycle outlet N
  GET  /api/pdu/outlet/{n}/power      → measure avg power over 3 samples
  POST /api/pdu/all/on                → all outlets on
  POST /api/pdu/all/off               → all outlets off
"""

import logging
from fastapi import APIRouter, HTTPException

logger = logging.getLogger("pdu_router")

router = APIRouter(prefix="/api/pdu", tags=["pdu"])


def _get_pdu():
    try:
        from apdu_client import get_pdu
        return get_pdu()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"PDU unavailable: {e}")


@router.get("/snapshot")
async def pdu_snapshot():
    """Full PDU status — 3-phase power + all outlets."""
    pdu = _get_pdu()
    snap = pdu.snapshot()
    if not snap:
        raise HTTPException(status_code=502, detail="PDU snapshot failed")
    return {
        "device": snap.device_name,
        "total_current_a": snap.total_current_a,
        "total_power_w": snap.total_power_w,
        "timestamp": snap.timestamp,
        "phases": [
            {
                "phase": p.phase,
                "current_a": p.current_a,
                "current_alarm": p.current_alarm,
                "voltage_v": p.voltage_v,
                "voltage_alarm": p.voltage_alarm,
                "power_w": p.power_w,
                "energy_kwh": p.energy_kwh,
                "power_factor": p.power_factor,
            }
            for p in snap.phases
        ],
        "outlets": [
            {
                "outlet": o.outlet,
                "name": o.name,
                "on": o.on,
                "current_a": o.current_a,
                "current_alarm": o.current_alarm,
                "power_w": o.power_w,
                "energy_kwh": o.energy_kwh,
                "power_factor": o.power_factor,
            }
            for o in snap.outlets
        ],
    }


@router.get("/outlets")
async def pdu_outlets():
    pdu = _get_pdu()
    outlets = pdu.get_outlets()
    return {
        "outlets": [
            {
                "outlet": o.outlet,
                "name": o.name,
                "on": o.on,
                "current_a": o.current_a,
                "current_alarm": o.current_alarm,
                "power_w": o.power_w,
                "energy_kwh": o.energy_kwh,
            }
            for o in outlets
        ]
    }


@router.post("/outlet/{outlet}/on")
async def outlet_on(outlet: int):
    if not 1 <= outlet <= 24:
        raise HTTPException(status_code=400, detail="Outlet must be 1-24")
    ok = _get_pdu().outlet_on(outlet)
    if not ok:
        raise HTTPException(status_code=502, detail=f"PDU outlet {outlet} ON failed")
    return {"outlet": outlet, "action": "on", "ok": True}


@router.post("/outlet/{outlet}/off")
async def outlet_off(outlet: int):
    if not 1 <= outlet <= 24:
        raise HTTPException(status_code=400, detail="Outlet must be 1-24")
    ok = _get_pdu().outlet_off(outlet)
    if not ok:
        raise HTTPException(status_code=502, detail=f"PDU outlet {outlet} OFF failed")
    return {"outlet": outlet, "action": "off", "ok": True}


@router.post("/outlet/{outlet}/cycle")
async def outlet_cycle(outlet: int):
    """Cycle: OFF, wait 10s, ON."""
    import asyncio
    pdu = _get_pdu()
    pdu.outlet_off(outlet)
    await asyncio.sleep(10)
    ok = pdu.outlet_on(outlet)
    return {"action": "cycle", "outlet": outlet, "ok": ok}


@router.get("/outlet/{outlet}/power")
async def outlet_power(outlet: int, samples: int = 3):
    """Measure avg power for a specific outlet."""
    if not 1 <= outlet <= 24:
        raise HTTPException(status_code=400, detail="Outlet must be 1-24")
    result = _get_pdu().measure_outlet_power(outlet, samples=samples)
    if not result:
        raise HTTPException(status_code=404, detail=f"Outlet {outlet} not found")
    return result


@router.post("/all/on")
async def all_on():
    pdu = _get_pdu()
    pdu.login()  # ensure fresh session
    ok = pdu.outlet_all_on()
    return {"action": "all_on", "ok": ok}


@router.post("/all/off")
async def all_off():
    pdu = _get_pdu()
    pdu.login()
    ok = pdu.outlet_all_off()
    return {"action": "all_off", "ok": ok}


@router.post("/all/cycle")
async def cycle_all():
    """Cycle all outlets: OFF, wait 10s, ON."""
    import asyncio
    pdu = _get_pdu()
    pdu.login()
    off_ok = pdu.outlet_all_off()
    await asyncio.sleep(10)
    on_ok = pdu.outlet_all_on()
    return {"action": "cycle_all", "ok": off_ok and on_ok}
