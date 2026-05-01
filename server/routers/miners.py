"""
Miners router — full CRUD + test, diagnose, LED control, fan control.
"""

from __future__ import annotations
import asyncio
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from db.session import get_db
from models.miner_model import MinerRecord, MinerStatus
from models.schemas import MinerResponse, MinerUpdate, LabelData, HashboardSchema
from services.miner_client import collect_miner, flash_led, control_fans
from services.ai_diagnosis import diagnose_miner
from services.websocket_manager import manager as ws_manager
from config import settings

import structlog
logger = structlog.get_logger()
router = APIRouter()



def _update_ip(miner, new_ip: str) -> bool:
    """
    Update miner IP address, pushing the old one into ip_history.
    MAC = digital identity, serial = physical identity.
    IP is just the last known address — always changes as miner moves.
    Returns True if IP changed.
    """
    if not new_ip or new_ip == "0.0.0.0":
        return False
    old_ip = miner.ip_address
    if old_ip == new_ip:
        return False
    # Push old IP into history (keep last 20)
    history = list(miner.ip_history or [])
    if old_ip and old_ip not in ("0.0.0.0", "") and old_ip not in history:
        history.append(old_ip)
    miner.ip_history = history[-20:]
    miner.ip_address = new_ip
    return True


def _to_response(m: MinerRecord) -> MinerResponse:
    hbs = []
    if m.hashboards:
        for hb in m.hashboards:
            hbs.append(HashboardSchema(**hb))
    return MinerResponse(
        id=m.id,
        ip_address=m.ip_address,
        mac_address=m.mac_address,
        ip_history=m.ip_history,
        serial_number=m.serial_number,
        model=m.model,
        firmware_version=m.firmware_version,
        miner_type=m.miner_type,
        pool_url=m.pool_url,
        worker=m.worker,
        hashboards=hbs,
        fan_speeds=m.fan_speeds,
        psu_voltage=m.psu_voltage,
        psu_current=m.psu_current,
        rt_hashrate=m.rt_hashrate,
        avg_hashrate=m.avg_hashrate,
        ideal_hashrate=m.ideal_hashrate,
        psu_fault=m.psu_fault,
        control_board_fault=m.control_board_fault,
        dead_chips=m.dead_chips,
        ai_issue_summary=m.ai_issue_summary,
        ai_suggested_fix=m.ai_suggested_fix,
        ai_auto_fixable=m.ai_auto_fixable,
        ai_severity=m.ai_severity,
        status=m.status,
        site_id=m.site_id,
        rack_id=m.rack_id,
        location=getattr(m, 'location', None),
        first_seen=m.first_seen,
        last_tested=m.last_tested,
        last_seen=m.last_seen,
    )


@router.get("", response_model=List[MinerResponse])
async def list_miners(
    status: Optional[str] = Query(None),
    site_id: Optional[int] = Query(None),
    rack_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MinerRecord)
    if status:
        stmt = stmt.where(MinerRecord.status == status)
    if site_id:
        stmt = stmt.where(MinerRecord.site_id == site_id)
    if rack_id:
        stmt = stmt.where(MinerRecord.rack_id == rack_id)
    result = await db.execute(stmt)
    miners = result.scalars().all()
    return [_to_response(m) for m in miners]


@router.get("/{miner_id}", response_model=MinerResponse)
async def get_miner(miner_id: int, db: AsyncSession = Depends(get_db)):
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")
    return _to_response(m)


@router.post("/test", response_model=MinerResponse)
async def test_miner(
    ip_address: str,
    miner_password: Optional[str] = None,
    run_ai: bool = True,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
):
    """
    Full miner test: collect data, run AI diagnosis, classify status, upsert DB record.
    """
    logger.info("Testing miner", ip=ip_address)

    data = await collect_miner(
        ip_address,
        password=miner_password or settings.MINER_PASSWORD,
    )
    if data is None:
        raise HTTPException(status_code=503, detail=f"Cannot reach miner at {ip_address}")

    # Convert hashboards to JSON-serialisable list
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

    # AI diagnosis
    diagnosis = None
    if run_ai:
        diagnosis = await diagnose_miner(
            model=data.model or "Unknown",
            serial=data.serial_number or "",
            hashboards=hb_list,
            logs=data.raw_logs,
            rt_hashrate=data.rt_hashrate,
            ideal_hashrate=data.ideal_hashrate,
        )

    # Auto-classify status
    status = _classify_status(data, diagnosis)

    # Upsert miner record — identity priority: MAC > worker name > IP
    m = None

    # 1. Try MAC address (most reliable — survives DHCP changes)
    if data.mac_address:
        stmt = select(MinerRecord).where(MinerRecord.mac_address == data.mac_address)
        result = await db.execute(stmt)
        m = result.scalar_one_or_none()
        if m:
            logger.info("Matched miner by MAC", mac=data.mac_address, old_ip=m.ip_address, new_ip=ip_address)
            if m.ip_address != ip_address:
                # IP changed — log it in history
                history = m.ip_history or []
                if m.ip_address and m.ip_address not in history:
                    history.append(m.ip_address)
                m.ip_history = history[-10:]  # keep last 10
            m.ip_address = ip_address  # always update to current IP

    # 2. Try worker name (e.g. "CTBench.S19-004") — secondary identity
    if m is None and data.worker:
        stmt = select(MinerRecord).where(MinerRecord.worker == data.worker)
        result = await db.execute(stmt)
        candidate = result.scalar_one_or_none()
        if candidate:
            logger.info("Matched miner by worker name", worker=data.worker, ip=ip_address)
            m = candidate
            if m.ip_address != ip_address:
                history = m.ip_history or []
                if m.ip_address and m.ip_address not in history:
                    history.append(m.ip_address)
                m.ip_history = history[-10:]
            m.ip_address = ip_address

    # 3. Fall back to IP (new miner or no MAC/worker yet)
    if m is None:
        stmt = select(MinerRecord).where(MinerRecord.ip_address == ip_address)
        result = await db.execute(stmt)
        m = result.scalar_one_or_none()

    if m is None:
        logger.info("New miner discovered", ip=ip_address, mac=data.mac_address)
        m = MinerRecord(ip_address=ip_address)
        db.add(m)

    m.mac_address = data.mac_address or m.mac_address
    m.serial_number = data.serial_number or m.serial_number
    m.model = data.model or m.model
    m.firmware_version = data.firmware_version or m.firmware_version
    m.miner_type = data.miner_type
    m.pool_url = data.pool_url
    m.worker = data.worker
    m.hashboards = hb_list
    m.fan_speeds = data.fan_speeds
    m.psu_voltage = data.psu_voltage
    m.psu_current = data.psu_current
    m.rt_hashrate = data.rt_hashrate
    m.avg_hashrate = data.avg_hashrate
    m.ideal_hashrate = data.ideal_hashrate
    m.psu_fault = data.psu_fault
    m.control_board_fault = data.control_board_fault
    m.dead_chips = data.dead_chips
    m.raw_logs = data.raw_logs[:32000] if data.raw_logs else None
    m.last_tested = datetime.utcnow()
    m.last_seen = datetime.utcnow()
    m.status = status

    if diagnosis:
        m.ai_issue_summary = diagnosis.issue_summary
        m.ai_suggested_fix = diagnosis.suggested_fix
        m.ai_auto_fixable = diagnosis.auto_fixable
        m.ai_severity = diagnosis.severity

    await db.flush()
    await db.refresh(m)

    response = _to_response(m)

    # Broadcast update
    await ws_manager.broadcast_miner_update(response.model_dump(mode="json"))
    if diagnosis:
        await ws_manager.broadcast_diagnosis_complete(m.id, diagnosis.severity)

    # Auto-fix if applicable
    if diagnosis and diagnosis.auto_fixable:
        background_tasks.add_task(_attempt_autofix, ip_address, data.miner_type, diagnosis)

    return response


@router.patch("/{miner_id}", response_model=MinerResponse)
async def update_miner(
    miner_id: int,
    updates: MinerUpdate,
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")

    if updates.status is not None:
        m.status = updates.status
    if updates.site_id is not None:
        m.site_id = updates.site_id
    if updates.rack_id is not None:
        m.rack_id = updates.rack_id

    # serial_number update with duplicate check
    if getattr(updates, "serial_number", None) is not None:
        sn = updates.serial_number.strip().upper()
        if sn:
            # Same miner already has this exact SN — return 200 with flag
            if m.serial_number == sn:
                await db.flush()
                await db.refresh(m)
                r = _to_response(m)
                r.__dict__["already_set"] = True
                return r
            from sqlalchemy import select as _sel
            dup = await db.execute(
                _sel(MinerRecord).where(
                    MinerRecord.serial_number == sn,
                    MinerRecord.id != miner_id
                )
            )
            dup_m = dup.scalar_one_or_none()
            if dup_m:
                raise HTTPException(status_code=409,
                    detail=f"S/N {sn} already in database (Miner ID {dup_m.id})")
            m.serial_number = sn

    # mac_address update — MAC is the digital identity, never wiped
    if getattr(updates, "mac_address", None) is not None:
        m.mac_address = updates.mac_address.upper()

    # ip_address update — always track history, IP changes as miner moves
    if getattr(updates, "ip_address", None) is not None:
        _update_ip(m, updates.ip_address)

    # location update
    if getattr(updates, "location", None) is not None:
        m.location = updates.location

    await db.flush()
    await db.refresh(m)
    return _to_response(m)


@router.post("/{miner_id}/led")
async def control_led(
    miner_id: int,
    blink: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Flash or unflash the miner LED."""
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")

    result = await flash_led(m.ip_address, m.miner_type, blink)
    return {"success": True, "blink": blink, "response": result}


@router.post("/{miner_id}/fans")
async def control_fan(
    miner_id: int,
    mode: str = "auto",
    db: AsyncSession = Depends(get_db),
):
    """Control fan speed. mode: off | min | max | auto | 0-100"""
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")

    result = await control_fans(m.ip_address, m.miner_type, mode)
    return {"success": True, "mode": mode, "response": result}


@router.post("/{miner_id}/mark_ready")
async def mark_ready(miner_id: int, db: AsyncSession = Depends(get_db)):
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")
    m.status = MinerStatus.ready_to_deploy.value
    await db.flush()
    return {"status": m.status}


@router.post("/{miner_id}/mark_parts")
async def mark_parts(miner_id: int, db: AsyncSession = Depends(get_db)):
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")
    m.status = MinerStatus.parts_unit.value
    await db.flush()
    return {"status": m.status}


def _classify_status(data, diagnosis=None) -> str:
    """Auto-classify miner status based on data and AI diagnosis."""
    if data.control_board_fault:
        return MinerStatus.control_board_fault.value
    if data.psu_fault:
        return MinerStatus.psu_fault.value

    if diagnosis:
        if diagnosis.severity == "critical":
            return MinerStatus.needs_repair.value
        if diagnosis.severity == "ok":
            return MinerStatus.ready_to_deploy.value

    # Rule-based fallback
    min_health = min((hb.health_pct for hb in data.hashboards), default=100)
    if min_health < 50 or data.dead_chips > 20:
        return MinerStatus.needs_repair.value
    if min_health >= 80 and not data.psu_fault and not data.control_board_fault:
        return MinerStatus.ready_to_deploy.value

    return MinerStatus.needs_repair.value


async def _attempt_autofix(ip: str, miner_type: str, diagnosis):
    """Attempt auto-fix actions for fixable issues."""
    logger.info("Attempting auto-fix", ip=ip, severity=diagnosis.severity)
    fix_lower = (diagnosis.suggested_fix or "").lower()

    try:
        if "restart" in fix_lower or "reboot" in fix_lower:
            if miner_type == "antminer":
                from services.miner_client import AntminerClient
                c = AntminerClient(ip)
                await c.reboot()
                logger.info("Auto-fix: rebooted miner", ip=ip)
            elif miner_type == "whatsminer":
                from services.miner_client import WhatsminerClient
                c = WhatsminerClient(ip)
                await c.reboot()
                logger.info("Auto-fix: rebooted miner", ip=ip)
        elif "temp" in fix_lower or "thermal" in fix_lower:
            # Increase fan speed
            await control_fans(ip, miner_type, "max")
            logger.info("Auto-fix: set fans to max", ip=ip)
    except Exception as e:
        logger.error("Auto-fix failed", ip=ip, error=str(e))
