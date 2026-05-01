"""
Admin router — operational endpoints for deployment, restart, etc.
"""

import asyncio
import subprocess
import structlog
from fastapi import APIRouter, HTTPException
from datetime import datetime
from sqlalchemy import select

from db.session import AsyncSessionLocal
from models.miner_model import MinerRecord, MinerStatus

logger = structlog.get_logger()
router = APIRouter(prefix="/api/admin", tags=["Admin"])

APP_DIR = "/home/abank/miner-testbench"
SERVER_DIR = "/home/abank/miner-testbench/server"

_last_deploy: dict = {
    "timestamp": None,
    "status": "idle",
    "message": ""
}


@router.post("/deploy")
async def deploy():
    """
    Pull latest code from git and restart the uvicorn server process.
    Runs in background — API will restart in ~5 seconds.
    """
    global _last_deploy

    _last_deploy["timestamp"] = datetime.utcnow().isoformat()
    _last_deploy["status"] = "deploying"
    _last_deploy["message"] = "Starting git pull and server restart..."

    logger.info("Admin deploy initiated")

    async def _do_deploy():
        try:
            # Git pull
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=APP_DIR,
                capture_output=True,
                timeout=30,
                text=True
            )
            if result.returncode != 0:
                msg = f"git pull failed: {result.stderr.strip()}"
                logger.error(msg)
                _last_deploy["status"] = "failed"
                _last_deploy["message"] = msg
                return

            logger.info("Git pull successful", stdout=result.stdout[:200])
            _last_deploy["status"] = "success"
            _last_deploy["message"] = "Git pull complete. Restarting server..."

            # Restart uvicorn — pkill then re-launch
            subprocess.Popen(
                ["bash", "-c",
                 f"sleep 1 && pkill -f uvicorn; sleep 2 && "
                 f"cd {SERVER_DIR} && "
                 f"nohup .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 "
                 f">> /tmp/uv.log 2>&1 &"]
            )

        except subprocess.TimeoutExpired:
            msg = "Deployment timeout (git pull took >30s)"
            logger.error(msg)
            _last_deploy["status"] = "timeout"
            _last_deploy["message"] = msg
        except Exception as e:
            msg = f"Deployment error: {str(e)}"
            logger.error(msg)
            _last_deploy["status"] = "failed"
            _last_deploy["message"] = msg

    asyncio.create_task(_do_deploy())

    return {
        "status": "deploying",
        "message": "Git pull initiated. Server will restart in ~5 seconds.",
        "timestamp": _last_deploy["timestamp"]
    }


@router.get("/deploy/status")
async def deploy_status():
    """Check status of last deployment."""
    return _last_deploy


@router.post("/sync-testbench-miners")
async def sync_testbench_miners():
    """
    Ensure all miners on testbench subnets appear in the Miners tab
    by setting their status to active (leaves needs_repair miners untouched).
    """
    testbench_subnets = [
        "192.168.2.",
        "192.168.3.",
        "192.168.5.",
        "192.168.6.",
    ]

    updated = 0
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(MinerRecord))
            miners = result.scalars().all()

            for miner in miners:
                ip = miner.ip_address or ""
                if not any(ip.startswith(s) for s in testbench_subnets):
                    continue
                # Only bump status if it's stuck at a non-actionable state
                if miner.status not in (
                    MinerStatus.needs_repair,
                    MinerStatus.ready_to_deploy,
                    MinerStatus.active,
                ):
                    miner.status = MinerStatus.ready_to_deploy
                    updated += 1
                    logger.info("sync-testbench-miners: updated", ip=ip, status="ready_to_deploy")

            await db.commit()

        return {
            "status": "success",
            "message": f"Updated {updated} testbench miners to ready_to_deploy",
            "updated_count": updated
        }

    except Exception as e:
        logger.error("sync-testbench-miners failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
