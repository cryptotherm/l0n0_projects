"""
AutoScan Service — continuously discover and register miners on testbench subnets.

Every 60 seconds:
1. Scan all testbench CIDR ranges
2. For each discovered IP:
   - Check if already in DB
   - If new → auto-register with status "new"
   - Update last_seen timestamp
3. Remove miners with no IP for 3+ consecutive scans
"""

import asyncio
import structlog
from datetime import datetime, timedelta
from typing import Optional, Set
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.miner_model import MinerRecord
from services.discovery_service import scan_cidr
from services.miner_client import detect_miner_type, collect_miner

logger = structlog.get_logger()

# Testbench subnets (Shelf A, B, C, D)
TESTBENCH_CIDRS = [
    "192.168.3.0/28",  # Shelf A (L1-1~4)
    "192.168.2.0/28",  # Shelf B (L2-5~7) — Whatsminer
    "192.168.5.0/28",  # Shelf C (L2-1~4)
    "192.168.6.0/28",  # Shelf D (L1-5~8)
]

# Track consecutive misses per IP
_missed_scans: dict[str, int] = {}
MAX_MISSES = 3  # Remove after 3 missed scans (3 minutes)


async def autoscan_loop(scan_interval_s: int = 60):
    """
    Continuous discovery loop.
    Every scan_interval_s seconds, scan all testbench subnets.
    """
    logger.info("AutoScan service starting", interval_s=scan_interval_s, cidrs=TESTBENCH_CIDRS)
    
    while True:
        try:
            await _scan_cycle()
        except Exception as e:
            logger.error("AutoScan cycle failed", error=str(e))
        
        await asyncio.sleep(scan_interval_s)


async def _scan_cycle():
    """
    One complete scan cycle across all testbench subnets.
    """
    # Discover all IPs currently on testbench
    discovered_ips: Set[str] = set()
    
    for cidr in TESTBENCH_CIDRS:
        try:
            ips = await scan_cidr(cidr, concurrency=20, timeout=2.0)
            discovered_ips.update(ips)
            logger.debug("Scanned CIDR", cidr=cidr, found=len(ips))
        except Exception as e:
            logger.warning("CIDR scan failed", cidr=cidr, error=str(e))
    
    logger.info("AutoScan cycle", total_discovered=len(discovered_ips))
    
    # Now process discovered IPs: register new ones, update last_seen, remove stale
    async with AsyncSessionLocal() as db:
        # Get all miners currently in DB
        result = await db.execute(select(MinerRecord))
        db_miners = {m.ip_address: m for m in result.scalars().all() if m.ip_address and m.ip_address != "0.0.0.0"}
        
        # ─── Process discovered IPs ───────────────────────────────────────────
        for ip in discovered_ips:
            miner = db_miners.get(ip)
            
            if miner:
                # Already in DB — just update last_seen
                miner.last_seen = datetime.utcnow()
                # Reset missed counter
                _missed_scans.pop(ip, None)
                logger.debug("Updated existing miner", ip=ip)
            else:
                # New IP — probe and register
                try:
                    miner_type = await detect_miner_type(ip)
                    if miner_type:
                        data = await collect_miner(ip, miner_type=miner_type, password="root")
                        
                        # Create new miner record
                        new_miner = MinerRecord(
                            ip_address=ip,
                            miner_type=miner_type,
                            model=data.model if data else None,
                            mac_address=data.mac_address if data else None,
                            serial_number=data.serial_number if data else None,
                            firmware_version=data.firmware_version if data else None,
                            pool_url=data.pool_url if data else None,
                            worker=data.worker if data else None,
                            status="new",  # Mark as new for initial 10-min test phase
                            first_seen=datetime.utcnow(),
                            last_seen=datetime.utcnow(),
                            rt_hashrate=data.rt_hashrate if data else 0.0,
                            avg_hashrate=data.avg_hashrate if data else 0.0,
                            ideal_hashrate=data.ideal_hashrate if data else 0.0,
                            hashboards=_convert_hashboards(data.hashboards) if data else [],
                        )
                        db.add(new_miner)
                        logger.info("Auto-registered new miner", ip=ip, model=new_miner.model)
                        _missed_scans.pop(ip, None)
                except Exception as e:
                    logger.warning("Failed to probe new IP", ip=ip, error=str(e))
        
        # ─── Process miners NOT found in scan (no IP) ───────────────────────
        for ip, miner in db_miners.items():
            if ip not in discovered_ips:
                # Miner not found in this scan — increment miss counter
                misses = _missed_scans.get(ip, 0) + 1
                _missed_scans[ip] = misses
                
                if misses >= MAX_MISSES:
                    # Remove miner after 3 consecutive misses
                    logger.info("Removing stale miner (no IP for 3+ scans)", ip=ip, model=miner.model)
                    await db.delete(miner)
                    _missed_scans.pop(ip, None)
                else:
                    logger.debug("Miner offline", ip=ip, misses=misses)
        
        await db.commit()


def _convert_hashboards(hashboards) -> list[dict]:
    """Convert hashboard objects to JSON-serializable dicts."""
    if not hashboards:
        return []
    return [
        {
            "index": getattr(hb, "index", 0),
            "position": getattr(hb, "position", ""),
            "chip_count": getattr(hb, "chip_count", 0),
            "expected_chips": getattr(hb, "expected_chips", 0),
            "missing_chips": getattr(hb, "missing_chips", 0),
            "temp_avg": float(getattr(hb, "temp_avg", 0.0)),
            "temp_max": float(getattr(hb, "temp_max", 0.0)),
            "health_pct": float(getattr(hb, "health_pct", 100.0)),
            "active": getattr(hb, "active", True),
        }
        for hb in hashboards
    ]
