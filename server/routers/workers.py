"""
Worker profiles router — manage pool/worker assignments and push them to miners.
"""
from __future__ import annotations
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from pydantic import BaseModel
from db.session import get_db
from models.miner_model import MinerRecord
from services.miner_client import AntminerClient, WhatsminerClient, cgminer_command
import structlog

logger = structlog.get_logger()
router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class WorkerProfileCreate(BaseModel):
    name: str                      # display name e.g. "Luxor Main"
    pool_url: str                  # e.g. stratum+tcp://btc.global.luxor.tech:700
    pool_url_2: Optional[str] = None
    pool_url_3: Optional[str] = None
    worker_template: str           # e.g. "cryptotherm.{ct_id}" or "cryptotherm.bench01"
    password: str = "x"
    site_label: Optional[str] = None   # e.g. "CTHome Rack A"
    notes: Optional[str] = None

class WorkerProfileResponse(WorkerProfileCreate):
    id: int
    class Config:
        from_attributes = True

class AssignRequest(BaseModel):
    profile_id: int
    miner_ids: Optional[List[int]] = None   # None = all miners


# ── DB helpers (raw SQL, no ORM model needed) ─────────────────────────────────

async def _ensure_table(db: AsyncSession):
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS worker_profiles (
            id SERIAL PRIMARY KEY,
            name VARCHAR(128) NOT NULL,
            pool_url VARCHAR(256) NOT NULL,
            pool_url_2 VARCHAR(256),
            pool_url_3 VARCHAR(256),
            worker_template VARCHAR(128) NOT NULL,
            password VARCHAR(64) DEFAULT 'x',
            site_label VARCHAR(128),
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    await db.commit()


async def _row_to_dict(row) -> dict:
    return {c: getattr(row, c) for c in row._fields} if hasattr(row, '_fields') else dict(row._mapping)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[dict])
async def list_profiles(db: AsyncSession = Depends(get_db)):
    await _ensure_table(db)
    result = await db.execute(text("SELECT * FROM worker_profiles ORDER BY id"))
    rows = result.fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("", response_model=dict, status_code=201)
async def create_profile(profile: WorkerProfileCreate, db: AsyncSession = Depends(get_db)):
    await _ensure_table(db)
    result = await db.execute(text("""
        INSERT INTO worker_profiles (name, pool_url, pool_url_2, pool_url_3, worker_template, password, site_label, notes)
        VALUES (:name, :pool_url, :pool_url_2, :pool_url_3, :worker_template, :password, :site_label, :notes)
        RETURNING *
    """), profile.model_dump())
    await db.commit()
    row = result.fetchone()
    return dict(row._mapping)


@router.put("/{profile_id}", response_model=dict)
async def update_profile(profile_id: int, profile: WorkerProfileCreate, db: AsyncSession = Depends(get_db)):
    await _ensure_table(db)
    data = profile.model_dump()
    data["id"] = profile_id
    result = await db.execute(text("""
        UPDATE worker_profiles SET
            name=:name, pool_url=:pool_url, pool_url_2=:pool_url_2, pool_url_3=:pool_url_3,
            worker_template=:worker_template, password=:password, site_label=:site_label, notes=:notes
        WHERE id=:id RETURNING *
    """), data)
    await db.commit()
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    return dict(row._mapping)


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    await _ensure_table(db)
    await db.execute(text("DELETE FROM worker_profiles WHERE id=:id"), {"id": profile_id})
    await db.commit()


@router.post("/assign")
async def assign_workers(req: AssignRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Push worker/pool config to one or all miners."""
    await _ensure_table(db)

    # Load profile
    result = await db.execute(text("SELECT * FROM worker_profiles WHERE id=:id"), {"id": req.profile_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    profile = dict(row._mapping)

    # Load miners
    if req.miner_ids:
        stmt = select(MinerRecord).where(MinerRecord.id.in_(req.miner_ids))
    else:
        stmt = select(MinerRecord)
    miners_result = await db.execute(stmt)
    miners = miners_result.scalars().all()

    background_tasks.add_task(_push_workers, miners, profile)
    return {"queued": len(miners), "profile": profile["name"]}


async def _push_workers(miners: list, profile: dict):
    """Background task: push pool config to each miner."""
    results = []
    for m in miners:
        try:
            # Build worker name from template
            worker = profile["worker_template"]
            worker = worker.replace("{ct_id}", m.ct_id or "unknown")
            worker = worker.replace("{serial}", m.serial_number or "unknown")
            worker = worker.replace("{mac}", (m.mac_address or "").replace(":", ""))
            worker = worker.replace("{id}", str(m.id))

            ok = await _push_to_miner(m.ip_address, m.miner_type or "antminer", profile, worker)
            logger.info("Worker assigned", ip=m.ip_address, worker=worker, ok=ok)
            results.append({"ip": m.ip_address, "worker": worker, "ok": ok})
        except Exception as e:
            logger.error("Worker assign failed", ip=m.ip_address, error=str(e))
            results.append({"ip": m.ip_address, "ok": False, "error": str(e)})
    return results


async def _push_to_miner(ip: str, miner_type: str, profile: dict, worker: str) -> bool:
    """Push pool/worker config to a single miner."""
    pools = [{"url": profile["pool_url"], "user": worker, "pass": profile["password"]}]
    if profile.get("pool_url_2"):
        pools.append({"url": profile["pool_url_2"], "user": worker, "pass": profile["password"]})
    if profile.get("pool_url_3"):
        pools.append({"url": profile["pool_url_3"], "user": worker, "pass": profile["password"]})

    if miner_type == "antminer":
        # Try HTTP then HTTPS
        for user, pwd in [("root", "root"), ("admin", "admin")]:
            client = AntminerClient(ip, user, pwd)
            conf = await client.get_miner_conf()
            if conf:
                conf["pools"] = pools
                result = await client._post("cgi-bin/set_miner_conf.cgi", conf)
                if result:
                    return True
        return False

    elif miner_type == "whatsminer":
        # Whatsminer uses pools command
        for i, p in enumerate(pools[:3]):
            cmd = {
                "command": "updatepools",
                "parameter": f"{i+1},{p['url']},{p['user']},{p['pass']}"
            }
            await cgminer_command(ip, cmd)
        return True

    return False


@router.post("/auto-pool/{miner_id}")
async def auto_assign_luxor(miner_id: int, db: AsyncSession = Depends(get_db)):
    """
    Push the default Luxor pool to a miner that has no pool configured.
    Worker name = LUXOR_WORKER_TEMPLATE with {username} replaced by LUXOR_USERNAME.
    Always uses "intesting" suffix so it shows up as a testing worker in Luxor dashboard.
    Returns immediately; push happens in background.
    """
    from config import settings
    result = await db.execute(select(MinerRecord).where(MinerRecord.id == miner_id))
    miner = result.scalar_one_or_none()
    if not miner:
        raise HTTPException(status_code=404, detail="Miner not found")

    if not miner.ip_address:
        return {"skipped": True, "reason": "no IP address"}

    username = settings.LUXOR_USERNAME or "cryptotherm"
    template = getattr(settings, "LUXOR_WORKER_TEMPLATE", "{username}.intesting")
    worker = template.replace("{username}", username)                      .replace("{ct_id}", miner.ct_id or "")                      .replace("{serial}", miner.serial_number or "")                      .replace("{id}", str(miner.id))

    pool_url   = getattr(settings, "LUXOR_POOL_URL",   "stratum+tcp://btc.global.luxor.tech:700")
    pool_url_2 = getattr(settings, "LUXOR_POOL_URL_2", pool_url)

    profile = {
        "pool_url":   pool_url,
        "pool_url_2": pool_url_2,
        "pool_url_3": None,
        "password":   "x",
    }

    miner_type = miner.miner_type or "antminer"
    ok = await _push_to_miner(miner.ip_address, miner_type, profile, worker)

    if ok:
        # Update DB record
        miner.pool_url = pool_url
        miner.worker   = worker
        await db.commit()
        logger.info("Auto-pool pushed", ip=miner.ip_address, worker=worker)

    return {
        "ok": ok,
        "worker": worker,
        "pool_url": pool_url,
        "miner_id": miner_id,
        "ip": miner.ip_address,
    }

