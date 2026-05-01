"""
apdu_client.py — APDU Smart PDU client for CT TestBench

Supports:
  - Read 3-phase power (voltage, current, watts, energy, PF)
  - Read per-outlet status (current, power, energy, on/off state)
  - Control outlets: ON / OFF / CYCLE
  - Measure power for a specific outlet number

API endpoints (no auth cookie needed after /login):
  GET /login?a=<user>&b=<pass>   → "OK"
  GET /getstatus?a=0             → phase data string
  GET /getoutput                 → per-outlet data string
  GET /getdevice                 → device totals + settings
  GET /setcontrol?a=<outlet>&b=<action>  → action: 1=OFF, 2=ON, 3=CYCLE
"""

import os
import time
import logging
import requests
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

logger = logging.getLogger("apdu_client")

PDU_HOST     = os.getenv("PDU_HOST",     "http://10.0.0.226")
PDU_USER     = os.getenv("PDU_USER",     "admin")
PDU_PASSWORD = os.getenv("PDU_PASSWORD", "admin")


@dataclass
class PhaseStatus:
    phase: int        # 1, 2, or 3
    current_a: float
    current_alarm: bool
    voltage_v: float
    voltage_alarm: bool
    power_w: float
    energy_kwh: float
    power_factor: float


@dataclass
class OutletStatus:
    outlet: int       # 1-based
    name: str
    on: bool          # True = ON
    current_a: float
    current_alarm: bool
    power_w: float
    energy_kwh: float
    power_factor: float


@dataclass
class PDUSnapshot:
    phases: List[PhaseStatus]
    outlets: List[OutletStatus]
    device_name: str
    total_current_a: float    # sum of phase currents
    total_power_w: float      # sum of phase powers
    timestamp: float = field(default_factory=time.time)

    def outlet(self, n: int) -> Optional[OutletStatus]:
        """Get outlet by number (1-based)."""
        for o in self.outlets:
            if o.outlet == n:
                return o
        return None


class APDUClient:
    """
    Lightweight APDU PDU client.
    Auth is session-less — each request is independent after login cookie.
    We use a requests.Session to persist the login cookie.
    """

    def __init__(
        self,
        host: str = PDU_HOST,
        user: str = PDU_USER,
        password: str = PDU_PASSWORD,
    ):
        self.host = host.rstrip("/")
        self.user = user
        self.password = password
        self._session = requests.Session()
        self._logged_in = False

    # ── Auth ──────────────────────────────────────────────────────────────────

    def login(self) -> bool:
        try:
            r = self._session.get(
                f"{self.host}/login?a={self.user}&b={self.password}&",
                timeout=5,
            )
            if r.text.strip() in ("OK", "OK1"):
                self._logged_in = True
                logger.info(f"PDU login OK ({self.host})")
                return True
            logger.warning(f"PDU login failed: {r.text!r}")
        except Exception as e:
            logger.error(f"PDU login error: {e}")
        return False

    def _get(self, path: str) -> Optional[str]:
        """GET with auto-relogin on ERR."""
        if not self._logged_in:
            if not self.login():
                return None
        try:
            r = self._session.get(f"{self.host}{path}", timeout=5)
            if r.text.strip() == "ERR":
                logger.info("PDU session expired, re-logging in")
                self._logged_in = False
                if not self.login():
                    return None
                r = self._session.get(f"{self.host}{path}", timeout=5)
            return r.text
        except Exception as e:
            logger.error(f"PDU GET {path} error: {e}")
            return None

    # ── Parsing ───────────────────────────────────────────────────────────────

    def get_phase_status(self) -> List[PhaseStatus]:
        raw = self._get("/getstatus?a=0&")
        if not raw:
            return []
        gv = raw.rstrip("?").split("?")
        phases = []
        try:
            n = int(gv[0])
            for i in range(min(n, 3)):
                phases.append(PhaseStatus(
                    phase=i + 1,
                    current_a=float(gv[1 + i * 2]) / 100.0,
                    current_alarm=bool(int(gv[2 + i * 2])),
                    voltage_v=float(gv[7 + i * 2]) / 10.0,
                    voltage_alarm=bool(int(gv[8 + i * 2])),
                    power_w=float(gv[13 + i]) / 10.0,
                    energy_kwh=float(gv[16 + i]) / 10.0,
                    power_factor=float(gv[19 + i]) / 10.0,
                ))
        except (IndexError, ValueError) as e:
            logger.error(f"Phase parse error: {e} | raw={raw!r}")
        return phases

    def get_outlets(self) -> List[OutletStatus]:
        raw = self._get("/getoutput")
        if not raw:
            return []
        gv = raw.rstrip("?").split("?")
        outlets = []
        try:
            num = int(gv[0])
            for i in range(num):
                base = 1 + i * 7
                status_code = int(gv[base + 1])
                outlets.append(OutletStatus(
                    outlet=i + 1,
                    name=gv[base],
                    on=(status_code == 2),
                    current_a=float(gv[base + 2]) / 100.0,
                    current_alarm=bool(int(gv[base + 3])),
                    power_w=float(gv[base + 4]) / 10.0,
                    energy_kwh=float(gv[base + 5]) / 10.0,
                    power_factor=float(gv[base + 6]) / 10.0,
                ))
        except (IndexError, ValueError) as e:
            logger.error(f"Outlet parse error: {e} | raw={raw!r}")
        return outlets

    def get_device_name(self) -> str:
        raw = self._get("/getdevice")
        if not raw:
            return "unknown"
        gv = raw.rstrip("?").split("?")
        try:
            return urllib.parse.unquote(gv[33])
        except IndexError:
            return "unknown"

    def snapshot(self) -> Optional[PDUSnapshot]:
        """Full PDU snapshot — phases + all outlets."""
        phases = self.get_phase_status()
        outlets = self.get_outlets()
        if not phases and not outlets:
            return None
        name = self.get_device_name()
        total_current = sum(p.current_a for p in phases)
        total_power   = sum(p.power_w   for p in phases)
        return PDUSnapshot(
            phases=phases,
            outlets=outlets,
            device_name=name,
            total_current_a=total_current,
            total_power_w=total_power,
        )

    # ── Control ───────────────────────────────────────────────────────────────

    def outlet_on(self, outlet: int) -> bool:
        """Turn outlet ON."""
        r = self._get(f"/setcontrol?a={outlet}&b=2&")
        ok = r is not None and r.strip() == "OK"
        logger.info(f"PDU outlet {outlet} ON → {'OK' if ok else 'FAIL'}")
        return ok

    def outlet_off(self, outlet: int) -> bool:
        """Turn outlet OFF."""
        r = self._get(f"/setcontrol?a={outlet}&b=1&")
        ok = r is not None and r.strip() == "OK"
        logger.info(f"PDU outlet {outlet} OFF → {'OK' if ok else 'FAIL'}")
        return ok

    def outlet_cycle(self, outlet: int) -> bool:
        """Power-cycle outlet (OFF then ON with configured delay)."""
        r = self._get(f"/setcontrol?a={outlet}&b=3&")
        ok = r is not None and r.strip() == "OK"
        logger.info(f"PDU outlet {outlet} CYCLE → {'OK' if ok else 'FAIL'}")
        return ok

    def outlet_all_on(self) -> bool:
        """Turn ALL outlets ON (outlet 25 = all)."""
        r = self._get("/setcontrol?a=25&b=2&")
        return r is not None and r.strip() == "OK"

    def outlet_all_off(self) -> bool:
        """Turn ALL outlets OFF."""
        r = self._get("/setcontrol?a=25&b=1&")
        return r is not None and r.strip() == "OK"

    # ── Power Measurement ─────────────────────────────────────────────────────

    def measure_outlet_power(
        self, outlet: int, samples: int = 3, interval: float = 2.0
    ) -> Dict[str, Any]:
        """
        Sample an outlet's power over N readings.
        Returns avg/min/max watts + current, or None if outlet not found.
        """
        readings = []
        for _ in range(samples):
            outlets = self.get_outlets()
            for o in outlets:
                if o.outlet == outlet:
                    readings.append({"current_a": o.current_a, "power_w": o.power_w, "pf": o.power_factor})
                    break
            if _ < samples - 1:
                time.sleep(interval)

        if not readings:
            return {}

        watts = [r["power_w"] for r in readings]
        amps  = [r["current_a"] for r in readings]
        return {
            "outlet": outlet,
            "samples": samples,
            "power_w_avg": sum(watts) / len(watts),
            "power_w_min": min(watts),
            "power_w_max": max(watts),
            "current_a_avg": sum(amps) / len(amps),
            "power_factor": readings[-1]["pf"],
        }


# Singleton
_pdu: Optional[APDUClient] = None

def get_pdu() -> APDUClient:
    global _pdu
    if _pdu is None:
        _pdu = APDUClient()
    return _pdu
