"""
Pydantic schemas for request/response validation.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field
from models.miner_model import MinerStatus, MinerType


# ── Hashboard ─────────────────────────────────────────────────────────────────

class HashboardSchema(BaseModel):
    index: int = Field(..., ge=1, le=3)
    position: str = Field(..., description="LEFT, CENTER, or RIGHT")
    chip_count: int = 0
    expected_chips: int = 0
    missing_chips: int = 0
    temp_avg: float = 0.0
    temp_max: float = 0.0
    health_pct: float = Field(0.0, ge=0, le=100)
    active: bool = True


# ── AI Diagnosis ──────────────────────────────────────────────────────────────

class DiagnosisSchema(BaseModel):
    issue_summary: str
    suggested_fix: str
    auto_fixable: bool
    severity: str  # ok / warning / critical


# ── Miner ─────────────────────────────────────────────────────────────────────

class MinerBase(BaseModel):
    ip_address: str
    mac_address: Optional[str] = None
    ip_history: Optional[list] = None
    serial_number: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    miner_type: str = "unknown"
    pool_url: Optional[str] = None
    worker: Optional[str] = None
    hashboards: Optional[List[HashboardSchema]] = None
    fan_speeds: Optional[List[int]] = None
    psu_voltage: Optional[float] = None
    psu_current: Optional[float] = None
    rt_hashrate: Optional[float] = None
    avg_hashrate: Optional[float] = None
    ideal_hashrate: Optional[float] = None
    psu_fault: bool = False
    control_board_fault: bool = False
    dead_chips: int = 0
    ai_issue_summary: Optional[str] = None
    ai_suggested_fix: Optional[str] = None
    ai_auto_fixable: bool = False
    ai_severity: Optional[str] = None
    status: str = "unknown"
    site_id: Optional[int] = None
    rack_id: Optional[int] = None


class MinerCreate(MinerBase):
    pass


class MinerUpdate(BaseModel):
    status: Optional[str] = None
    site_id: Optional[int] = None
    rack_id: Optional[int] = None
    notes: Optional[str] = None
    serial_number: Optional[str] = None
    mac_address: Optional[str] = None
    # IP is last-known only — always tracked in ip_history, never the identity
    ip_address: Optional[str] = None
    location: Optional[str] = None   # bench slot label e.g. AL1-3


class MinerResponse(MinerBase):
    id: int
    first_seen: datetime
    last_tested: Optional[datetime] = None
    last_seen: datetime
    location: Optional[str] = None

    class Config:
        from_attributes = True


# ── Site / Rack ───────────────────────────────────────────────────────────────

class RackCreate(BaseModel):
    name: str
    ip_range_start: Optional[str] = None
    ip_range_end: Optional[str] = None
    ip_cidr: Optional[str] = None


class RackResponse(RackCreate):
    id: int
    site_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class SiteCreate(BaseModel):
    name: str
    location: Optional[str] = None


class SiteResponse(SiteCreate):
    id: int
    created_at: datetime
    racks: List[RackResponse] = []

    class Config:
        from_attributes = True


# ── Discovery ─────────────────────────────────────────────────────────────────

class DiscoveryRequest(BaseModel):
    ip_address: str
    miner_password: Optional[str] = None


class DiscoveryResponse(BaseModel):
    ip_address: str
    reachable: bool
    miner_type: Optional[str] = None
    model: Optional[str] = None
    mac_address: Optional[str] = None
    ip_history: Optional[list] = None


# ── LED / Fan ─────────────────────────────────────────────────────────────────

class LEDRequest(BaseModel):
    miner_id: int
    blink: bool = True


class FanControlRequest(BaseModel):
    miner_id: int
    mode: str = Field(..., description="off | min | max | auto | <0-100>")


# ── Label ─────────────────────────────────────────────────────────────────────

class LabelRequest(BaseModel):
    miner_id: int
    print_now: bool = True
    printer_identifier: Optional[str] = None


class LabelData(BaseModel):
    serial_number: str
    model: str
    mac_address: str
    worker: str
    hashrate: float = 0.0
    firmware_version: str = ""
    hashboards: List[HashboardSchema]
    ai_summary: str
    ai_fix: str
    status: str
    qr_data: str
    location: str = ""       # shelf slot e.g. AL1-3
    ip_address: str = ""     # last known IP


# ── RGB ───────────────────────────────────────────────────────────────────────

class RGBRequest(BaseModel):
    pattern: str = Field(..., description="solid | blink | rainbow | off")
    color: Optional[str] = None  # hex like #FF0000
    speed_ms: int = 500


# ── WebSocket message ─────────────────────────────────────────────────────────

class WSMessage(BaseModel):
    event: str
    data: dict
