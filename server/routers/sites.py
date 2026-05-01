"""
Sites and racks router — manage deployment sites and rack IP ranges.
"""

from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.session import get_db
from models.site_model import Site, Rack
from models.miner_model import MinerRecord, MinerStatus
from models.schemas import SiteCreate, SiteResponse, RackCreate, RackResponse
from services.discovery_service import scan_ip_range, scan_cidr
from services.miner_client import collect_miner, flash_led, control_fans
from services.websocket_manager import manager as ws_manager

import structlog
logger = structlog.get_logger()
router = APIRouter()


# ── Sites ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=List[SiteResponse])
async def list_sites(db: AsyncSession = Depends(get_db)):
    stmt = select(Site).options(selectinload(Site.racks))
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=SiteResponse, status_code=201)
async def create_site(site: SiteCreate, db: AsyncSession = Depends(get_db)):
    s = Site(name=site.name, location=site.location)
    db.add(s)
    await db.flush()
    await db.refresh(s)
    return s


@router.get("/{site_id}", response_model=SiteResponse)
async def get_site(site_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Site).where(Site.id == site_id).options(selectinload(Site.racks))
    result = await db.execute(stmt)
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Site not found")
    return s


@router.delete("/{site_id}", status_code=204)
async def delete_site(site_id: int, db: AsyncSession = Depends(get_db)):
    s = await db.get(Site, site_id)
    if not s:
        raise HTTPException(status_code=404, detail="Site not found")
    await db.delete(s)


# ── Racks ──────────────────────────────────────────────────────────────────────

@router.post("/{site_id}/racks", response_model=RackResponse, status_code=201)
async def create_rack(site_id: int, rack: RackCreate, db: AsyncSession = Depends(get_db)):
    s = await db.get(Site, site_id)
    if not s:
        raise HTTPException(status_code=404, detail="Site not found")
    r = Rack(
        site_id=site_id,
        name=rack.name,
        ip_range_start=rack.ip_range_start,
        ip_range_end=rack.ip_range_end,
        ip_cidr=rack.ip_cidr,
    )
    db.add(r)
    await db.flush()
    await db.refresh(r)
    return r


@router.get("/{site_id}/racks/{rack_id}/scan")
async def scan_rack(
    site_id: int,
    rack_id: int,
    run_tests: bool = False,
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    """
    Scan all miners in a rack. Optionally run full tests.
    Returns list of discovered IPs and known miner records.
    """
    r = await db.get(Rack, rack_id)
    if not r or r.site_id != site_id:
        raise HTTPException(status_code=404, detail="Rack not found")

    # Discover IPs
    if r.ip_cidr:
        ips = await scan_cidr(r.ip_cidr)
    elif r.ip_range_start and r.ip_range_end:
        ips = await scan_ip_range(r.ip_range_start, r.ip_range_end)
    else:
        raise HTTPException(status_code=400, detail="Rack has no IP range configured")

    if run_tests:
        background_tasks.add_task(_test_rack_miners, ips, rack_id, site_id)

    # Load known miners in this rack
    stmt = select(MinerRecord).where(MinerRecord.rack_id == rack_id)
    result = await db.execute(stmt)
    known = {m.ip_address: m for m in result.scalars().all()}

    return {
        "rack_id": rack_id,
        "site_id": site_id,
        "ip_count": len(ips),
        "discovered_ips": ips,
        "known_miners": len(known),
        "testing_in_background": run_tests,
    }


@router.get("/{site_id}/racks/{rack_id}/miners")
async def rack_miners(
    site_id: int,
    rack_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get all known miners in a rack with swap recommendations."""
    r = await db.get(Rack, rack_id)
    if not r or r.site_id != site_id:
        raise HTTPException(status_code=404, detail="Rack not found")

    stmt = select(MinerRecord).where(MinerRecord.rack_id == rack_id)
    result = await db.execute(stmt)
    miners = result.scalars().all()

    needs_swap = [m for m in miners if m.status in (MinerStatus.needs_repair.value, MinerStatus.psu_fault.value, MinerStatus.control_board_fault.value)]
    ready = [m for m in miners if m.status == MinerStatus.ready_to_deploy.value]

    return {
        "total": len(miners),
        "ready_to_deploy": len(ready),
        "needs_swap": len(needs_swap),
        "swap_suggestions": [
            {
                "miner_id": m.id,
                "ip_address": m.ip_address,
                "model": m.model,
                "status": m.status,
                "ai_summary": m.ai_issue_summary,
            }
            for m in needs_swap
        ],
    }


@router.post("/{site_id}/racks/{rack_id}/identify")
async def identify_rack_miners(
    site_id: int,
    rack_id: int,
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Flash LEDs on miners in rack (optionally filtered by status) to identify them."""
    stmt = select(MinerRecord).where(MinerRecord.rack_id == rack_id)
    if status_filter:
        stmt = stmt.where(MinerRecord.status == status_filter)
    result = await db.execute(stmt)
    miners = result.scalars().all()

    flashed = []
    errors = []
    for m in miners:
        try:
            await flash_led(m.ip_address, m.miner_type, blink=True)
            flashed.append(m.ip_address)
        except Exception as e:
            errors.append({"ip": m.ip_address, "error": str(e)})

    return {"flashed": len(flashed), "errors": errors}


async def _test_rack_miners(ips: list[str], rack_id: int, site_id: int):
    """Background task: test all miners in a rack and update DB records."""
    from db.session import AsyncSessionLocal
    from models.miner_model import MinerRecord
    from datetime import datetime
    from services.miner_client import collect_miner
    from services.ai_diagnosis import diagnose_miner
    import asyncio

    sem = asyncio.Semaphore(5)

    async def test_one(ip: str):
        async with sem:
            data = await collect_miner(ip)
            if not data:
                return

            hb_list = [
                {
                    "index": hb.index,
                    "position": hb.position,
                    "chip_count": hb.chip_count,
                    "expected_chips": hb.expected_chips,
                    "missing_chips": hb.missing_chips,
                    "temp_avg": hb.temp_avg,
                    "temp_max": hb.temp_max,
                    "health_pct": hb.health_pct,
                    "active": hb.active,
                }
                for hb in data.hashboards
            ]

            diagnosis = await diagnose_miner(
                model=data.model or "Unknown",
                serial=data.serial_number or "",
                hashboards=hb_list,
                logs=data.raw_logs,
                rt_hashrate=data.rt_hashrate,
                ideal_hashrate=data.ideal_hashrate,
            )

            async with AsyncSessionLocal() as db:
                from sqlalchemy import select as sa_select
                stmt = sa_select(MinerRecord).where(MinerRecord.ip_address == ip)
                result = await db.execute(stmt)
                m = result.scalar_one_or_none()
                if m is None:
                    m = MinerRecord(ip_address=ip)
                    db.add(m)

                m.site_id = site_id
                m.rack_id = rack_id
                m.miner_type = data.miner_type
                m.model = data.model or m.model
                m.mac_address = data.mac_address or m.mac_address
                m.hashboards = hb_list
                m.last_tested = datetime.utcnow()
                m.last_seen = datetime.utcnow()
                if diagnosis:
                    m.ai_issue_summary = diagnosis.issue_summary
                    m.ai_suggested_fix = diagnosis.suggested_fix
                    m.ai_auto_fixable = diagnosis.auto_fixable
                    m.ai_severity = diagnosis.severity
                await db.commit()

            await ws_manager.broadcast_miner_update({"ip_address": ip, "rack_id": rack_id})

    await asyncio.gather(*[test_one(ip) for ip in ips])
    logger.info("Rack scan complete", rack_id=rack_id, count=len(ips))
