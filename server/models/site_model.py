"""
SQLAlchemy ORM models for sites and racks.
"""

from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from db.session import Base


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    location: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    racks: Mapped[list[Rack]] = relationship("Rack", back_populates="site", cascade="all, delete-orphan")


class Rack(Base):
    __tablename__ = "racks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(Integer, ForeignKey("sites.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # IP range like "192.168.10.1-192.168.10.30"
    ip_range_start: Mapped[str | None] = mapped_column(String(45))
    ip_range_end: Mapped[str | None] = mapped_column(String(45))
    # CIDR notation as alternative "192.168.10.0/24"
    ip_cidr: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    site: Mapped[Site] = relationship("Site", back_populates="racks")
