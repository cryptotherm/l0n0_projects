"""
SQLAlchemy ORM model for miners.
"""

from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Float, Integer, Boolean, DateTime, JSON, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from db.session import Base
import enum


class MinerStatus(str, enum.Enum):
    ready_to_deploy = "ready_to_deploy"
    needs_repair = "needs_repair"
    parts_unit = "parts_unit"
    psu_fault = "psu_fault"
    control_board_fault = "control_board_fault"
    unknown = "unknown"
    testing = "testing"


class MinerType(str, enum.Enum):
    antminer = "antminer"
    whatsminer = "whatsminer"
    avalon = "avalon"
    unknown = "unknown"


class MinerRecord(Base):
    __tablename__ = "miners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False, index=True)
    mac_address: Mapped[str | None] = mapped_column(String(17), unique=True, index=True)
    ip_history: Mapped[dict | None] = mapped_column(JSON, default=list)  # previous IPs
    serial_number: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(64))
    firmware_version: Mapped[str | None] = mapped_column(String(128))
    miner_type: Mapped[str] = mapped_column(String(32), default="unknown")

    # Pool / worker
    pool_url: Mapped[str | None] = mapped_column(String(256))
    worker: Mapped[str | None] = mapped_column(String(128))

    # Hashboards (JSON list of {index, position, chip_count, temp, health_pct})
    hashboards: Mapped[dict | None] = mapped_column(JSON)

    # Electrical
    fan_speeds: Mapped[dict | None] = mapped_column(JSON)  # list[int]
    psu_voltage: Mapped[float | None] = mapped_column(Float)
    psu_current: Mapped[float | None] = mapped_column(Float)

    # Hashrate
    rt_hashrate: Mapped[float | None] = mapped_column(Float)
    avg_hashrate: Mapped[float | None] = mapped_column(Float)
    ideal_hashrate: Mapped[float | None] = mapped_column(Float)

    # Error flags
    psu_fault: Mapped[bool] = mapped_column(Boolean, default=False)
    control_board_fault: Mapped[bool] = mapped_column(Boolean, default=False)
    dead_chips: Mapped[int] = mapped_column(Integer, default=0)

    # AI diagnosis
    ai_issue_summary: Mapped[str | None] = mapped_column(String(1024))
    ai_suggested_fix: Mapped[str | None] = mapped_column(String(1024))
    ai_auto_fixable: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_severity: Mapped[str | None] = mapped_column(String(16))  # ok/warning/critical

    # Status
    status: Mapped[str] = mapped_column(
        String(32), default=MinerStatus.unknown.value
    )

    # Site assignment
    site_id: Mapped[int | None] = mapped_column(Integer, index=True)
    rack_id: Mapped[int | None] = mapped_column(Integer, index=True)

    # Timestamps
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_tested: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Raw logs snapshot
    raw_logs: Mapped[str | None] = mapped_column(String(32768))
