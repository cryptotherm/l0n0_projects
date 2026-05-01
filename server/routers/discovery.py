"""
Discovery router — manual IP scan and UDP discovery management.
"""

import asyncio
from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from db.session import get_db
from pydantic import BaseModel
from typing import Optional, List
from services.discovery_service import scan_ip_range, scan_cidr, UDPDiscoveryService
from services.miner_client import detect_miner_type, collect_miner
from services.websocket_manager import manager as ws_manager
import structlog

logger = structlog.get_logger()
router = APIRouter()


class ScanRangeRequest(BaseModel):
    # Accepts any of:
    #   ip_range: "10.0.0.0/28" or "10.0.0.0/24" (CIDR) — preferred
    #   cidrs:    ["10.0.0.0/28","10.0.0.16/28"]   (multi-subnet)
    #   start_ip + end_ip: legacy range
    ip_range:   str  = ""
    cidrs:      list = []
    start_ip:   str  = ""
    end_ip:     str  = ""
    concurrency:int  = 50
    timeout:    float = 3.0
    auto_detect:bool  = True   # auto-detect local subnets if nothing specified


class ScanCIDRRequest(BaseModel):
    cidr: str
    concurrency: int = 30


class ManualIPRequest(BaseModel):
    ip_address: str
    miner_password: Optional[str] = None


@router.post("/scan/range")
async def scan_range(req: ScanRangeRequest, background_tasks: BackgroundTasks):
    """Scan an IP range for miners."""
    ips = await scan_ip_range(req.start_ip, req.end_ip, req.concurrency, req.timeout)
    return {"found": len(ips), "ip_addresses": ips}


@router.post("/scan/cidr")
async def scan_cidr_endpoint(req: ScanCIDRRequest):
    """Scan a CIDR block for miners."""
    ips = await scan_cidr(req.cidr, concurrency=req.concurrency)
    return {"found": len(ips), "ip_addresses": ips}


@router.post("/probe")
async def probe_ip(req: ManualIPRequest):
    """
    Probe a single IP address to detect miner type.
    Returns miner type and basic system info.
    """
    miner_type = await detect_miner_type(req.ip_address)
    if miner_type is None:
        raise HTTPException(status_code=404, detail=f"No miner found at {req.ip_address}")

    data = await collect_miner(
        req.ip_address,
        miner_type=miner_type,
        password=req.miner_password or "root",
    )

    if data is None:
        return {
            "ip_address": req.ip_address,
            "reachable": True,
            "miner_type": miner_type,
            "model": None,
            "mac_address": None,
        }

    return {
        "ip_address": req.ip_address,
        "reachable": True,
        "miner_type": data.miner_type,
        "model": data.model,
        "mac_address": data.mac_address,
        "serial_number": data.serial_number,
        "worker": data.worker,
    }


@router.get("/udp/status")
async def udp_discovery_status():
    """Return current UDP discovery status."""
    from fastapi import Request
    return {"status": "listening", "port": 14235}

@router.post("/scan/full")
async def scan_full(req: ScanRangeRequest, db: AsyncSession = Depends(get_db)):
    """
    Full subnet scan: probe each IP for Antminer API (no ping pre-filter,
    miners often block ICMP). Upsert new miners. Returns new/updated counts.
    """
    import ipaddress as _ip
    from sqlalchemy import select as _sel, or_
    from models.miner_model import MinerRecord
    from datetime import datetime

    # Build IP list from CIDR(s), range, or auto-detect
    all_ips = []

    def _cidr_to_ips(cidr: str) -> list:
        net = _ip.ip_network(cidr, strict=False)
        # For /28 and smaller use all hosts; for larger use hosts() (excludes net/bcast)
        if net.prefixlen >= 28:
            return [str(h) for h in net.hosts()]
        return [str(h) for h in net.hosts()]

    def _auto_detect_subnets() -> list:
        """Find local network interfaces + always include rack subnets."""
        import subprocess, re
        # Always scan all rack subnets regardless of interface config
        cidrs = [
            '192.168.2.0/28',   # Rack B Whatsminers
            '192.168.3.0/28',   # Rack A
            '192.168.5.0/28',   # Rack C
            '192.168.6.0/28',   # Rack D Whatsminers
        ]
        try:
            out = subprocess.check_output(['ip','-4','addr','show'], text=True)
            for m in re.finditer(r'inet (\d+\.\d+\.\d+\.\d+)/(\d+)', out):
                addr, prefix = m.group(1), int(m.group(2))
                if addr.startswith('127.') or addr.startswith('100.'):
                    continue  # skip loopback + tailscale
                net = _ip.ip_network(f"{addr}/{prefix}", strict=False)
                # If it's a /24 or larger, return the /28 block the Pi is in
                # plus all other /28 blocks in the same /24
                if prefix <= 24:
                    base = _ip.ip_network(f"{str(net.network_address)}/24", strict=False)
                    for subnet in base.subnets(new_prefix=28):
                        cidrs.append(str(subnet))
                else:
                    cidrs.append(str(net))
        except Exception:
            cidrs.append('10.0.0.0/24')
        return cidrs

    if req.cidrs:
        for cidr in req.cidrs:
            all_ips += _cidr_to_ips(cidr)
    elif req.ip_range:
        all_ips = _cidr_to_ips(req.ip_range)
    elif req.start_ip and req.end_ip:
        start_int = int(_ip.ip_address(req.start_ip))
        end_int   = int(_ip.ip_address(req.end_ip))
        all_ips   = [str(_ip.ip_address(i)) for i in range(start_int, end_int + 1)]
    elif req.auto_detect:
        detected_cidrs = _auto_detect_subnets()
        for cidr in detected_cidrs:
            all_ips += _cidr_to_ips(cidr)

    # Deduplicate while preserving order
    seen = set()
    all_ips = [ip for ip in all_ips if not (ip in seen or seen.add(ip))]

    # Load known IPs and MACs once
    result = await db.execute(_sel(MinerRecord))
    existing = result.scalars().all()
    known_ips  = {m.ip_address for m in existing if m.ip_address}
    known_macs = {(m.mac_address or '').upper() for m in existing if m.mac_address}

    new_miners     = []
    updated_miners = []
    errors         = []

    async def probe_one(ip: str):
        try:
            miner_type = await asyncio.wait_for(detect_miner_type(ip), timeout=2.5)
            if not miner_type:
                return
            data = await asyncio.wait_for(collect_miner(ip, miner_type=miner_type), timeout=6.0)
            if not data:
                return
            mac = (data.mac_address or '').upper()
            return ip, data, mac
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            import logging
            logging.getLogger("discovery").debug(f"probe_one {ip} error: {e}")
            return None

    # Probe all IPs concurrently — asyncio handles this fine at 254 concurrent
    # With 1s detect timeout, full /24 completes in ~2s even if all IPs timeout
    import math
    results = await asyncio.gather(*[probe_one(ip) for ip in all_ips])
    probe_results = [r for r in results if r]

    # Now do DB work serially (single session)
    for ip, data, mac in probe_results:
        try:
            miner = None
            if mac and mac in known_macs:
                r = await db.execute(_sel(MinerRecord).where(MinerRecord.mac_address == mac))
                miner = r.scalar_one_or_none()
            if not miner and ip in known_ips:
                r = await db.execute(_sel(MinerRecord).where(MinerRecord.ip_address == ip))
                miner = r.scalar_one_or_none()

            if miner:
                changed = miner.ip_address != ip
                miner.ip_address       = ip
                if data.mac_address:   miner.mac_address = data.mac_address
                if data.model:         miner.model = data.model
                if data.worker:        miner.worker = data.worker
                if data.pool_url:      miner.pool_url = data.pool_url
                miner.rt_hashrate      = data.rt_hashrate or 0
                miner.avg_hashrate     = data.avg_hashrate or 0
                miner.firmware_version = data.firmware_version or miner.firmware_version
                miner.last_seen        = datetime.utcnow()
                # Auto-diagnose: 0 TH/s + no active hashboards = dead boards
                if (data.rt_hashrate or 0) == 0 and data.hashboards is not None:
                    active_hbs = [h for h in data.hashboards if getattr(h, 'hashrate_avg', 0) > 0]
                    if len(data.hashboards) == 0 or (len(active_hbs) == 0 and miner.status not in ('archived', 'ready_to_deploy')):
                        total_hbs = len(data.hashboards)
                        miner.status = 'needs_repair'
                        fault = "0/%s hashboards active. 0 TH/s. Likely dead hashboard(s) or control board fault." % (total_hbs if total_hbs else '?')
                        updated_miners.append({"id": miner.id, "ip": ip, "model": miner.model, "fault": fault})
                    else:
                        if changed:
                            updated_miners.append({"id": miner.id, "ip": ip, "model": miner.model})
                else:
                    if changed:
                        updated_miners.append({"id": miner.id, "ip": ip, "model": miner.model})
            else:
                new_m = MinerRecord(
                    ip_address            = ip,
                    mac_address           = data.mac_address if data.mac_address else None,  # Don't set empty string — violates unique constraint
                    model                 = data.model or '',
                    miner_type            = data.miner_type or 'antminer',
                    worker                = data.worker or '',
                    pool_url              = data.pool_url or '',
                    firmware_version      = data.firmware_version or '',
                    rt_hashrate           = data.rt_hashrate or 0,
                    avg_hashrate          = data.avg_hashrate or 0,
                    status                = 'ready_to_deploy',
                    psu_fault             = data.psu_fault or False,
                    control_board_fault   = data.control_board_fault or False,
                    dead_chips            = data.dead_chips or 0,
                    ai_auto_fixable       = False,
                    first_seen            = datetime.utcnow(),
                    last_seen             = datetime.utcnow(),
                )
                db.add(new_m)
                await db.flush()
                await db.refresh(new_m)
                new_miners.append({"id": new_m.id, "ip": ip,
                                   "model": new_m.model, "mac": new_m.mac_address})
                known_ips.add(ip)
                if mac: known_macs.add(mac)
        except Exception as e:
            errors.append({"ip": ip, "error": str(e)})

    # Mark ALL miners that didn't respond as offline and clear last_seen
    responded_ips = {ip for ip, _, _ in probe_results}
    offline_count = 0
    for miner in existing:
        if miner.ip_address and miner.ip_address in all_ips and miner.ip_address not in responded_ips:
            if miner.status not in ('archived',):
                miner.status = 'offline'
                miner.rt_hashrate = 0
                # Set last_seen to epoch so isActive() (1-min window) hides it
                miner.last_seen = datetime(2000, 1, 1)
                offline_count += 1

    await db.commit()
    return {
        "scanned":       len(all_ips),
        "probed":        len(probe_results),
        "new":           len(new_miners),
        "updated":       len(updated_miners),
        "offline":       offline_count,
        "new_miners":    new_miners,
        "updated_miners": updated_miners,
    }
