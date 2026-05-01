"""
services/power_analyzer.py — PDU outlet power ramp analyzer

Continuously polls PDU outlets, logs power readings, and detects state
transitions based on power draw patterns.

States:
  off          → outlet off / nothing connected
  plugged_in   → <50W, just connected
  booting      → 50-600W, PSU init / control board coming up
  ramping      → 600-2500W, hashboards warming, chips init
  hashing      → >2500W sustained, full operation
  fault        → stuck in booting range >5min (PSU/CB fault)
  throttling   → oscillating power, overtemp
  hashboard_lost → sudden drop from hashing state
  psu_fault    → spike then collapse

Events fired on each state transition → outlet_power_events table
"""

import asyncio
import asyncpg
import logging
import json
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Optional

logger = logging.getLogger("power_analyzer")

DB_URL = "postgresql://testbench:motmaster@localhost/testbench"
POLL_INTERVAL = 5   # seconds between PDU polls
HISTORY_WINDOW = 60 # samples to keep in memory per outlet (5min at 5s)

# ── Power state thresholds ────────────────────────────────────────────────────
# Using current_a × 120V (North America) since APDU power_w is unreliable
# Adjust VOLTAGE if your bench is 240V
BENCH_VOLTAGE = 120.0  # volts

def amps_to_watts(amps: float) -> float:
    return amps * BENCH_VOLTAGE

# State bands (watts)
STATE_BANDS = [
    ("off",           0,     10),
    ("plugged_in",    10,    50),
    ("booting",       50,    600),
    ("ramping",       600,   2500),
    ("hashing",       2500,  5000),
]

def classify_watts(w: float) -> str:
    for name, lo, hi in STATE_BANDS:
        if lo <= w < hi:
            return name
    return "off"

# How long a miner must be STUCK in booting before calling it a fault
FAULT_BOOTING_SECONDS = 300   # 5 minutes
# Drop threshold to detect hashboard loss (from hashing state)
HASHBOARD_LOSS_DROP_PCT = 0.20  # 20% sudden drop
# Oscillation detection (thermal throttle)
OSCILLATION_WINDOW = 10        # samples
OSCILLATION_THRESHOLD = 0.15   # 15% variance = throttling


class OutletState:
    def __init__(self, outlet_num: int):
        self.outlet_num = outlet_num
        self.state = "off"
        self.state_since = datetime.utcnow()
        self.history = deque(maxlen=HISTORY_WINDOW)  # (ts, watts)
        self.peak_watts = 0.0
        self.last_watts = 0.0

    def add_sample(self, watts: float, ts: datetime = None):
        ts = ts or datetime.utcnow()
        self.history.append((ts, watts))
        if watts > self.peak_watts:
            self.peak_watts = watts
        self.last_watts = watts

    def time_in_state(self) -> float:
        return (datetime.utcnow() - self.state_since).total_seconds()

    def is_oscillating(self) -> bool:
        """Check if recent power is oscillating (thermal throttle)."""
        if len(self.history) < self.OSCILLATION_WINDOW:
            return False
        recent = [w for _, w in list(self.history)[-self.OSCILLATION_WINDOW:]]
        if not recent or sum(recent) == 0:
            return False
        avg = sum(recent) / len(recent)
        variance = sum((w - avg) ** 2 for w in recent) / len(recent)
        cv = (variance ** 0.5) / avg if avg > 0 else 0
        return cv > self.OSCILLATION_THRESHOLD

    OSCILLATION_WINDOW = OSCILLATION_WINDOW


class PowerAnalyzer:
    def __init__(self):
        self.outlet_states: dict[int, OutletState] = {
            i: OutletState(i) for i in range(1, 25)
        }
        self.db_pool: Optional[asyncpg.Pool] = None
        self.running = False

    async def start(self):
        self.db_pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
        self.running = True
        logger.info("Power analyzer started")
        asyncio.create_task(self._poll_loop())

    async def stop(self):
        self.running = False
        if self.db_pool:
            await self.db_pool.close()

    async def _poll_loop(self):
        while self.running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.warning(f"Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _poll_once(self):
        try:
            from apdu_client import get_pdu
            pdu = get_pdu()
            snap = pdu.snapshot()
        except Exception as e:
            logger.debug(f"PDU unavailable: {e}")
            return

        now = datetime.utcnow()
        async with self.db_pool.acquire() as conn:
            for outlet in snap.outlets:
                n = outlet.outlet
                # Calculate watts from current × voltage (APDU power_w unreliable)
                watts = amps_to_watts(outlet.current_a) if outlet.on else 0.0

                state = self.outlet_states[n]
                state.add_sample(watts, now)

                new_state = await self._analyze_state(state, watts, outlet.on)

                # Log to time series
                await conn.execute("""
                    INSERT INTO outlet_power_log
                        (pdu_host, outlet_num, ts, current_a, voltage_v, watts, on_state, power_state, state_change)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """, '10.0.0.226', n, now,
                    float(outlet.current_a), BENCH_VOLTAGE,
                    round(watts, 1), outlet.on,
                    new_state, new_state != state.state)

                # Fire event on state change
                if new_state != state.state:
                    await self._fire_event(conn, n, state.state, new_state, watts, outlet.current_a)
                    state.state = new_state
                    state.state_since = now

    async def _analyze_state(self, state: OutletState, watts: float, on: bool) -> str:
        """Determine current state based on power draw + history."""
        if not on:
            return "off"

        band = classify_watts(watts)

        # ── Fault detection: stuck in booting too long ────────────────────────
        if state.state == "booting" and band == "booting":
            if state.time_in_state() > FAULT_BOOTING_SECONDS:
                return "fault"

        # ── Hashboard loss: sudden drop from hashing ──────────────────────────
        if state.state == "hashing" and band in ("booting", "ramping"):
            if state.last_watts > 0 and watts < state.last_watts * (1 - HASHBOARD_LOSS_DROP_PCT):
                return "hashboard_lost"

        # ── PSU fault: spike then collapse ───────────────────────────────────
        if state.peak_watts > 2000 and watts < 100 and on:
            return "psu_fault"

        # ── Thermal throttle: oscillating power ───────────────────────────────
        if state.state == "hashing" and state.is_oscillating():
            return "throttling"

        # Normal band progression
        return band

    async def _fire_event(self, conn, outlet_num: int, old_state: str, new_state: str,
                          watts: float, current_a: float):
        """Map state transitions to meaningful event types."""
        event_map = {
            ("off",       "plugged_in"):    "plugged_in",
            ("off",       "booting"):       "plugged_in",
            ("plugged_in","booting"):       "boot_start",
            ("booting",   "ramping"):       "ramp_start",
            ("ramping",   "hashing"):       "hashing",
            ("hashing",   "throttling"):    "thermal_throttle",
            ("hashing",   "hashboard_lost"):"hashboard_lost",
            ("booting",   "fault"):         "fault_detected",
            ("ramping",   "fault"):         "fault_detected",
            ("hashing",   "psu_fault"):     "psu_fault",
            ("ramping",   "psu_fault"):     "psu_fault",
            ("hashing",   "off"):           "unplugged",
            ("hashing",   "booting"):       "rebooting",
            ("ramping",   "booting"):       "rebooting",
        }
        key = (old_state, new_state)
        event_type = event_map.get(key)
        if not event_type:
            # Generic transition
            event_type = f"state_{old_state}_to_{new_state}"

        # Get linked miner
        row = await conn.fetchrow(
            "SELECT miner_id FROM pdu_outlets WHERE outlet_num=$1 AND pdu_host='10.0.0.226'",
            outlet_num
        )
        miner_id = row["miner_id"] if row else None

        details = {
            "old_state": old_state,
            "new_state": new_state,
            "watts": round(watts, 1),
        }

        await conn.execute("""
            INSERT INTO outlet_power_events
                (pdu_host, outlet_num, ts, event_type, watts_at_event, current_a, details, miner_id)
            VALUES ($1,$2,NOW(),$3,$4,$5,$6,$7)
        """, '10.0.0.226', outlet_num, event_type,
            round(watts, 1), float(current_a),
            json.dumps(details), miner_id)

        logger.info(f"Outlet {outlet_num}: {old_state} → {new_state} ({event_type}) @ {watts:.0f}W")

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_outlet_timeline(self, outlet_num: int, minutes: int = 60):
        """Return power timeline for an outlet."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT ts, watts, current_a, power_state, state_change
                FROM outlet_power_log
                WHERE outlet_num=$1 AND pdu_host='10.0.0.226'
                  AND ts > NOW() - ($2 || ' minutes')::interval
                ORDER BY ts ASC
            """, outlet_num, str(minutes))
            return [dict(r) for r in rows]

    async def get_outlet_events(self, outlet_num: int, limit: int = 50):
        """Return power events for an outlet."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT ts, event_type, watts_at_event, current_a, details
                FROM outlet_power_events
                WHERE outlet_num=$1 AND pdu_host='10.0.0.226'
                ORDER BY ts DESC LIMIT $2
            """, outlet_num, limit)
            return [dict(r) for r in rows]

    async def get_all_current_states(self):
        """Return current state of all outlets."""
        return {
            n: {
                "state": s.state,
                "watts": round(s.last_watts, 1),
                "peak_watts": round(s.peak_watts, 1),
                "time_in_state_s": round(s.time_in_state()),
                "history_len": len(s.history),
            }
            for n, s in self.outlet_states.items()
        }


# Singleton
_analyzer: Optional[PowerAnalyzer] = None

def get_analyzer() -> PowerAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = PowerAnalyzer()
    return _analyzer
