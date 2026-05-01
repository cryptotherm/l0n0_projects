"""
Settings router — runtime config for printer, AI, credentials.
Writes values to the .env file so they persist across restarts.
"""

from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from config import settings

router = APIRouter()

# Path to .env file (same directory as this file's parent)
ENV_PATH = Path(__file__).parent.parent / ".env"


def _read_env() -> dict:
    """Read .env file into a dict."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(env: dict) -> None:
    """Write dict back to .env file."""
    lines = [f"{k}={v}" for k, v in env.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n")


def _update_env(**kwargs) -> None:
    """Update specific keys in .env without touching others."""
    env = _read_env()
    for k, v in kwargs.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    _write_env(env)


# ── General settings (printer, creds) ────────────────────────────────────────

class SettingsUpdate(BaseModel):
    printer_identifier: Optional[str] = None
    printer_model: Optional[str] = None
    miner_password: Optional[str] = None



@router.get("/settings")
async def get_settings():
    """Return current runtime settings."""
    return {
        "printer_identifier": settings.PRINTER_IDENTIFIER,
        "printer_model": settings.PRINTER_MODEL,
        "label_type": getattr(settings, "LABEL_TYPE", "62"),
        "ai_enabled": getattr(settings, "AI_ENABLED", True),
        "luxor_api_key": getattr(settings, "LUXOR_API_KEY", ""),
        "luxor_username": getattr(settings, "LUXOR_USERNAME", ""),
    }

@router.post("/settings")
async def update_settings(body: SettingsUpdate):
    updates = {}
    if body.printer_identifier is not None:
        updates["PRINTER_IDENTIFIER"] = f"tcp://{body.printer_identifier}" \
            if not body.printer_identifier.startswith("tcp://") else body.printer_identifier
        settings.PRINTER_IDENTIFIER = updates["PRINTER_IDENTIFIER"]
    if body.printer_model is not None:
        updates["PRINTER_MODEL"] = body.printer_model
        settings.PRINTER_MODEL = body.printer_model
    if body.miner_password is not None:
        updates["MINER_PASSWORD"] = body.miner_password
        settings.MINER_PASSWORD = body.miner_password
    if updates:
        _update_env(**updates)
    return {"status": "ok", "updated": list(updates.keys())}


# ── AI settings ────────────────────────────────────────────────────────────────

class AISettingsUpdate(BaseModel):
    openai_api_key: Optional[str] = None
    model: Optional[str] = None
    openai_base_url: Optional[str] = None


@router.get("/settings/ai")
async def get_ai_settings():
    env = _read_env()
    key = env.get("OPENAI_API_KEY", "") or settings.OPENAI_API_KEY
    return {
        "openai_key_set": bool(key and key.startswith("sk-")),
        "model": env.get("OPENAI_MODEL", settings.OPENAI_MODEL),
        "base_url": env.get("OPENAI_BASE_URL", ""),
    }


@router.post("/settings/ai")
async def update_ai_settings(body: AISettingsUpdate):
    updates = {}
    if body.openai_api_key:
        updates["OPENAI_API_KEY"] = body.openai_api_key
        settings.OPENAI_API_KEY = body.openai_api_key
    if body.model:
        updates["OPENAI_MODEL"] = body.model
        settings.OPENAI_MODEL = body.model
    if body.openai_base_url is not None:
        updates["OPENAI_BASE_URL"] = body.openai_base_url or ""
        if body.openai_base_url:
            settings.OPENAI_BASE_URL = body.openai_base_url
    if updates:
        _update_env(**updates)
    return {"status": "ok", "updated": list(updates.keys())}


# ── Kiosk control ─────────────────────────────────────────────────────────────

@router.post("/discover-printer")
async def discover_printer():
    """Scan for connected Brother printer via USB."""
    try:
        from brother_ql.backends.helpers import discover
        found = discover(backend_identifier="pyusb")
        if found:
            ident = found[0].get("identifier", "")
            settings.PRINTER_IDENTIFIER = ident
            _write_env("PRINTER_IDENTIFIER", ident)
            return {"identifier": ident, "all": [f.get("identifier") for f in found]}
        return {"identifier": None, "all": []}
    except Exception as e:
        return {"identifier": None, "error": str(e)}


@router.post("/api/kiosk/reload")
async def kiosk_reload():
    """Hard-reload the kiosk Chromium tab via CDP."""
    try:
        import json, urllib.request
        tabs = json.loads(urllib.request.urlopen('http://localhost:9223/json/list', timeout=3).read())
        if not tabs:
            return {'ok': False, 'error': 'no tabs'}
        import websocket
        ws = websocket.create_connection(tabs[0]['webSocketDebuggerUrl'], timeout=5)
        ws.send(json.dumps({'id': 1, 'method': 'Page.reload', 'params': {'ignoreCache': True}}))
        ws.recv()
        ws.close()
        return {'ok': True, 'tab': tabs[0].get('url', '')}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

@router.post("/version/switch")
async def switch_build_channel(payload: dict):
    """Switch between production (main) and sandbox branches, then restart."""
    import subprocess as _sp, os as _os
    channel = payload.get("channel", "main")
    if channel not in ("main", "sandbox"):
        return {"ok": False, "error": "channel must be 'main' or 'sandbox'"}
    
    repo_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__)))
    try:
        # Git checkout the target branch
        result = _sp.run(
            ["git", "checkout", channel],
            cwd=repo_root, capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            # Try pulling first
            _sp.run(["git", "fetch", "origin"], cwd=repo_root, timeout=15)
            result = _sp.run(
                ["git", "checkout", channel], 
                cwd=repo_root, capture_output=True, text=True, timeout=15
            )
        
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}
        
        # Pull latest
        _sp.run(["git", "pull", "origin", channel], cwd=repo_root, timeout=30, 
                capture_output=True)
        
        # Restart via PM2 in background
        _sp.Popen(
            ["bash", "-c", "sleep 1 && pm2 restart miner-testbench"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
        )
        
        return {
            "ok": True,
            "channel": channel,
            "message": f"Switched to {channel} branch. Restarting..."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
