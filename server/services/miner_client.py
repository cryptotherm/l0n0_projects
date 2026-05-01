"""
Miner client service — abstracts Antminer (CGMiner/Bitmain), Whatsminer, and Avalon APIs.
Returns a normalised MinerData object regardless of miner type.
"""

from __future__ import annotations
import asyncio
import json
import socket
import aiohttp
import httpx
import structlog
from typing import Optional
from dataclasses import dataclass, field

logger = structlog.get_logger()

BOARD_POSITIONS = {1: "LEFT", 2: "CENTER", 3: "RIGHT"}
CGMINER_PORT = 4028
CG_TIMEOUT = 3.0


@dataclass
class HashboardData:
    index: int
    position: str
    chip_count: int = 0
    expected_chips: int = 0
    missing_chips: int = 0
    temp_avg: float = 0.0
    temp_max: float = 0.0
    health_pct: float = 0.0
    active: bool = True


@dataclass
class MinerData:
    ip_address: str
    miner_type: str = "unknown"
    model: str = ""
    serial_number: str = ""
    firmware_version: str = ""
    mac_address: str = ""
    hostname: str = ""

    # Pool
    pool_url: str = ""
    worker: str = ""

    # Hashboards
    hashboards: list[HashboardData] = field(default_factory=list)

    # Electrical
    fan_speeds: list[int] = field(default_factory=list)
    psu_voltage: float = 0.0
    psu_current: float = 0.0

    # Hashrate (TH/s)
    rt_hashrate: float = 0.0
    avg_hashrate: float = 0.0
    ideal_hashrate: float = 0.0

    # Error flags
    psu_fault: bool = False
    control_board_fault: bool = False
    dead_chips: int = 0

    # Raw logs
    raw_logs: str = ""


# ── CGMiner socket helper ────────────────────────────────────────────────────

def _parse_cgminer_pipe(raw: str) -> dict:
    """Parse old-style pipe-delimited cgminer response into a dict."""
    result = {}
    # Response format: STATUS=S,...|SECTION,key=val,...|---
    parts = raw.split("|")
    for part in parts:
        if not part or part.strip() == "---":
            continue
        # First item may be the section name (e.g. "SUMMARY,Elapsed=...")
        pairs = part.split(",")
        section_name = pairs[0].split("=")[0] if pairs else ""
        for pair in pairs:
            if "=" in pair:
                k, _, v = pair.partition("=")
                result[k.strip()] = v.strip()
    return result


async def cgminer_command_raw(ip: str, cmd: str, port: int = CGMINER_PORT, timeout: float = CG_TIMEOUT) -> dict:
    """Send plain-text cgminer command (old protocol: just the word, no JSON)."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()

        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                if not chunk:
                    break
                buf += chunk
            except asyncio.TimeoutError:
                break

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        raw = buf.decode("utf-8", errors="replace").replace("\x00", "")
        return _parse_cgminer_pipe(raw)
    except (asyncio.TimeoutError, OSError) as e:
        logger.debug("cgminer_command_raw failed", ip=ip, cmd=cmd, error=str(e))
        return {}


async def cgminer_command(ip: str, command: dict, port: int = CGMINER_PORT, timeout: float = CG_TIMEOUT) -> dict:
    """Send JSON cgminer command; fall back to plain-text if miner rejects JSON."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.write(json.dumps(command).encode("ascii"))
        await writer.drain()

        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                if not chunk:
                    break
                buf += chunk
            except asyncio.TimeoutError:
                break

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        raw = buf.decode("utf-8", errors="replace").replace("\x00", "")

        # If miner rejected JSON ("Invalid command"), try plain-text fallback
        if "Invalid command" in raw or not raw.strip():
            cmd_name = command.get("command", "summary")
            return await cgminer_command_raw(ip, cmd_name, port, timeout)

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Response may be pipe-delimited (some firmwares mix formats)
            return _parse_cgminer_pipe(raw)

    except (asyncio.TimeoutError, OSError) as e:
        logger.debug("cgminer_command failed", ip=ip, command=command.get("command"), error=str(e))
        return {}


# ── Antminer (Bitmain firmware) ───────────────────────────────────────────────

class AntminerClient:
    """
    Antminer HTTP client using Digest authentication (required by Bitmain firmware).
    Uses httpx.AsyncClient which natively supports DigestAuth.
    """
    def __init__(self, ip: str, username: str = "root", password: str = "root"):
        self.ip = ip
        self.username = username
        self.password = password

    def _make_client(self, timeout: float = 10.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=httpx.DigestAuth(self.username, self.password),
            timeout=timeout,
        )

    async def _get(self, path: str) -> dict:
        try:
            async with self._make_client() as client:
                r = await client.get(f"http://{self.ip}/{path}")
                if r.status_code == 200:
                    return r.json()
        except Exception as e:
            logger.debug("antminer GET failed", ip=self.ip, path=path, error=str(e))
        return {}

    async def _post(self, path: str, data: dict) -> dict:
        try:
            async with self._make_client() as client:
                r = await client.post(f"http://{self.ip}/{path}", json=data)
                if r.status_code == 200:
                    return r.json()
        except Exception as e:
            logger.debug("antminer POST failed", ip=self.ip, path=path, error=str(e))
        return {}

    async def get_system_info(self) -> dict:
        return await self._get("cgi-bin/get_system_info.cgi")

    async def get_miner_conf(self) -> dict:
        return await self._get("cgi-bin/get_miner_conf.cgi")

    async def get_stats(self) -> dict:
        return await self._get("cgi-bin/stats.cgi")

    async def get_logs(self) -> str:
        try:
            async with self._make_client(timeout=15.0) as client:
                r = await client.get(f"http://{self.ip}/cgi-bin/log.cgi")
                if r.status_code == 200:
                    return r.text
        except Exception as e:
            logger.debug("antminer get_logs failed", ip=self.ip, error=str(e))
        return ""

    async def set_blink(self, state: bool) -> dict:
        return await self._post("cgi-bin/blink.cgi", {"blink": state})

    async def set_fan_speed(self, pwm_pct: int) -> dict:
        conf = await self.get_miner_conf()
        if conf:
            conf["bitmain-fan-ctrl"] = True
            conf["bitmain-fan-pwm"] = str(pwm_pct)
            return await self._post("cgi-bin/set_miner_conf.cgi", conf)
        return {}

    async def reboot(self) -> dict:
        return await self._post("cgi-bin/reboot.cgi", {})

    async def collect(self) -> MinerData:
        data = MinerData(ip_address=self.ip, miner_type="antminer")

        sys_info, conf, stats = await asyncio.gather(
            self.get_system_info(),
            self.get_miner_conf(),
            self.get_stats(),
        )

        # system info
        if sys_info:
            data.model = sys_info.get("minertype", "")
            data.mac_address = sys_info.get("macaddr", "")
            data.hostname = sys_info.get("hostname", "")

        # pool
        if conf:
            pools = conf.get("pools", [])
            if pools:
                data.pool_url = pools[0].get("url", "")
                data.worker = pools[0].get("user", "")

        # stats — stats.cgi returns {STATUS:{...}, INFO:{...}, STATS:[{...}]}
        if stats and stats.get("STATS"):
            st = stats["STATS"][0]
            info = stats.get("INFO", {})
            data.firmware_version = info.get("miner_version", "") or info.get("CompileTime", "")
            # Top-level hashrate fields (GH/s → TH/s)
            data.rt_hashrate = round(st.get("rate_5s", st.get("rate_real", 0)) / 1000, 2)
            data.avg_hashrate = round(st.get("rate_avg", st.get("rate_30m", 0)) / 1000, 2)
            data.ideal_hashrate = round(st.get("rate_ideal", 0) / 1000, 2)
            # Fan speeds are a list in stats.cgi: st["fan"] = [4410, 4650, ...]
            fan_list = st.get("fan", [])
            if isinstance(fan_list, list):
                data.fan_speeds = [f for f in fan_list if f > 0]
            else:
                # Fallback for older firmware that uses fan1/fan2 keys
                data.fan_speeds = [st.get("fan" + str(i), 0) for i in range(1, 5) if st.get("fan" + str(i))]

            chains = st.get("chain", [])
            total_dead = 0
            for i, chain in enumerate(chains, start=1):
                pos = BOARD_POSITIONS.get(i, f"HB{i}")
                chip_temps = [t for t in chain.get("temp_chip", []) if t > 0]
                expected_chips = chain.get("asic_num", 0)
                # Count working chips from the asic bitmap string ('o' = working, 'x' = dead)
                asic_str = chain.get("asic", "")
                working_chips = asic_str.count("o") if asic_str else expected_chips
                dead_chips_hb = asic_str.count("x") if asic_str else 0
                # If no asic string, fall back to asic_num as working count
                if not asic_str:
                    working_chips = expected_chips
                temp_avg = round(sum(chip_temps) / len(chip_temps), 1) if chip_temps else 0
                temp_max = max(chip_temps) if chip_temps else 0
                health = round((working_chips / max(expected_chips, 1)) * 100, 1)
                total_dead += dead_chips_hb
                hb = HashboardData(
                    index=i,
                    position=pos,
                    chip_count=working_chips,
                    expected_chips=expected_chips,
                    missing_chips=dead_chips_hb,
                    temp_avg=temp_avg,
                    temp_max=temp_max,
                    health_pct=health,
                    active=working_chips > 0,
                )
                data.hashboards.append(hb)
            data.dead_chips = total_dead

        data.raw_logs = await self.get_logs()
        return data


# ── Whatsminer (MicroBT) ──────────────────────────────────────────────────────

class WhatsminerClient:
    """
    MicroBT Whatsminer client using the btminer API.
    Commands: get_version, get_miner_info, status, get_psu, get_error_code.
    Does NOT support legacy cgminer commands (summary/devs/pools return "invalid cmd").
    """
    def __init__(self, ip: str):
        self.ip = ip

    async def _cmd(self, command: str, timeout: float = 5.0) -> dict:
        """Send a btminer JSON command and return the Msg dict."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, CGMINER_PORT), timeout=timeout
            )
            payload = json.dumps({"command": command}).encode("ascii")
            writer.write(payload)
            await writer.drain()
            buf = b""
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                    if not chunk:
                        break
                    buf += chunk
                except asyncio.TimeoutError:
                    break
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raw = buf.decode("utf-8", errors="replace").replace("\x00", "")
            parsed = json.loads(raw)
            if parsed.get("STATUS") == "S":
                msg = parsed.get("Msg", {})
                return msg if isinstance(msg, dict) else {}
            return {}
        except Exception as e:
            logger.debug("whatsminer cmd failed", ip=self.ip, cmd=command, error=str(e))
            return {}

    async def collect(self) -> MinerData:
        data = MinerData(ip_address=self.ip, miner_type="whatsminer")

        ver, info, stat, psu, errs = await asyncio.gather(
            self._cmd("get_version"),
            self._cmd("get_miner_info"),
            self._cmd("status"),
            self._cmd("get_psu"),
            self._cmd("get_error_code"),
        )

        # firmware / chip -> model
        if ver:
            chip = ver.get("chip", "")
            fw = ver.get("fw_ver", "")
            data.firmware_version = fw
            # Derive model from chip string:
            # H32A07 = M30S series, K46K = M50, K45A = M50, K88 = M56
            model_map = [
                ("H32A", "Whatsminer M30S"),
                ("K46K", "Whatsminer M50"),
                ("K45A", "Whatsminer M50"),
                ("K88",  "Whatsminer M56"),
                ("H6",   "Whatsminer M30"),
            ]
            for prefix, model_name in model_map:
                if prefix in chip:
                    data.model = model_name
                    break
            if not data.model:
                data.model = "Whatsminer"

        # network info / MAC
        if info:
            data.mac_address = info.get("mac", "")
            data.hostname = info.get("hostname", "WhatsMiner")

        # mining status
        if stat:
            miner_off = stat.get("mineroff", "true") == "true"
            hash_pct = stat.get("hash_percent", "")
            try:
                hp = float(hash_pct) if hash_pct else 0.0
            except Exception:
                hp = 0.0
            if miner_off:
                data.rt_hashrate = 0.0
                data.avg_hashrate = 0.0
            else:
                # hash_percent is % of rated; we don't know rated without model lookup
                # Use a placeholder; real hashrate comes from PSU power estimate
                data.rt_hashrate = 0.0

        # PSU data
        if psu:
            try:
                vin = float(psu.get("vin", 0)) / 100.0      # mV -> V (e.g. 21300 = 213V AC)
                pin = float(psu.get("pin", 0))                # Watts
                fan = int(psu.get("fan_speed", 0))
                data.fan_speeds = [fan] if fan else []
                data.psu_current = float(psu.get("iin", 0)) / 1000.0  # mA -> A
                data.psu_voltage = vin
                # Rough hashrate estimate from power: M30S ~3400W = 100 TH/s
                if pin > 0 and data.rt_hashrate == 0:
                    data.rt_hashrate = round(pin / 34.0, 1)  # ~34W/TH for M30S
            except Exception:
                pass

        # error codes -> fault flags
        if errs:
            error_list = errs.get("error_code", [])
            if error_list:
                codes = []
                for item in error_list:
                    if isinstance(item, dict):
                        codes.extend(item.keys())
                # Common Whatsminer error codes
                psu_codes = {"2000", "2001", "2002", "2003", "2010"}
                hb_codes  = {"3000", "3001", "3002", "3003", "3010", "3011"}
                code_set = set(codes)
                data.psu_fault = bool(code_set & psu_codes)
                data.control_board_fault = bool(code_set & hb_codes)

        # Add placeholder hashboards (btminer doesn't expose per-HB data without auth token)
        if not data.hashboards:
            # M-series = 3 hashboards typically
            for i in range(1, 4):
                data.hashboards.append(HashboardData(
                    index=i,
                    position=BOARD_POSITIONS.get(i, f"HB{i}"),
                    chip_count=0,
                    expected_chips=0,
                    active=data.rt_hashrate > 0,
                ))

        return data

    async def set_led(self, blink: bool):
        # Whatsminer LED control requires token auth — skip for now
        logger.debug("whatsminer set_led not supported without token", ip=self.ip)
        return {}

    async def reboot(self):
        logger.debug("whatsminer reboot not supported without token", ip=self.ip)
        return {}

class AvalonClient:
    """
    AvalonMiner uses CGMiner API on port 4028 for most operations.
    System info via HTTP on /cgi-bin/luci/admin/network/ifaces (OpenWrt based).
    """

    def __init__(self, ip: str, username: str = "admin", password: str = "admin"):
        self.ip = ip
        self.username = username
        self.password = password

    async def _get_luci(self, path: str) -> dict:
        try:
            async with httpx.AsyncClient(auth=httpx.DigestAuth(self.username, self.password), timeout=10.0) as client:
                r = await client.get(f"http://{self.ip}/{path}")
                if r.status_code == 200:
                    return r.json()
        except Exception as e:
            logger.debug("avalon luci GET failed", ip=self.ip, path=path, error=str(e))
        return {}

    async def collect(self) -> MinerData:
        data = MinerData(ip_address=self.ip, miner_type="avalon")

        # CGMiner stats
        estats, pools_resp, devs_resp = await asyncio.gather(
            cgminer_command(self.ip, {"command": "estats"}),
            cgminer_command(self.ip, {"command": "pools"}),
            cgminer_command(self.ip, {"command": "devs"}),
        )

        if estats.get("STATS"):
            st = estats["STATS"][0]
            data.model = st.get("Type", "Avalon")
            # Avalon estats contain "MM ID0" "MM ID1" etc. for each module
            fans = []
            for key, val in st.items():
                if key.startswith("Fan"):
                    try:
                        fans.append(int(val))
                    except:
                        pass
            data.fan_speeds = fans

            # Parse MM IDs for hashboard info
            mm_ids = [k for k in st.keys() if k.startswith("MM ID")]
            for i, mm_key in enumerate(mm_ids[:3], start=1):
                mm_str = st[mm_key]
                pos = BOARD_POSITIONS.get(i, f"HB{i}")
                # Parse key=value pairs in MM string
                mm_vals = {}
                for part in mm_str.split():
                    if "=" in part:
                        k2, v2 = part.split("=", 1)
                        mm_vals[k2] = v2

                chip_count = int(mm_vals.get("Asic", "0").split("/")[0] if "/" in mm_vals.get("Asic", "") else mm_vals.get("Asic", 0) or 0)
                total_chips = int(mm_vals.get("Asic", "0").split("/")[1] if "/" in mm_vals.get("Asic", "") else chip_count or 0)
                temps_str = mm_vals.get("Temp", "0")
                try:
                    temp_avg = float(temps_str.split("/")[0])
                    temp_max = float(temps_str.split("/")[-1]) if "/" in temps_str else temp_avg
                except:
                    temp_avg = temp_max = 0.0
                health = round((chip_count / max(total_chips, 1)) * 100, 1) if total_chips else 100.0
                hb = HashboardData(
                    index=i,
                    position=pos,
                    chip_count=chip_count,
                    expected_chips=total_chips,
                    missing_chips=max(0, total_chips - chip_count),
                    temp_avg=temp_avg,
                    temp_max=temp_max,
                    health_pct=health,
                    active=chip_count > 0,
                )
                data.hashboards.append(hb)

        if pools_resp.get("POOLS"):
            p = pools_resp["POOLS"][0]
            data.pool_url = p.get("URL", "")
            data.worker = p.get("User", "")

        return data


# ── Auto-detect miner type ────────────────────────────────────────────────────

async def detect_miner_type(ip: str) -> Optional[str]:
    """
    Probe the miner at ip and return 'antminer', 'whatsminer', 'avalon', or None.
    Detection order:
      1. btminer get_miner_info  -> MAC C8:0C or C8:11 prefix = Whatsminer
      2. Bitmain CGI get_system_info -> Antminer
      3. cgminer version command -> Antminer/Avalon fallback
    """
    # 1. Try Whatsminer btminer API first (faster than HTTP, no auth needed)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, CGMINER_PORT), timeout=3.0
        )
        writer.write(json.dumps({"command": "get_miner_info"}).encode("ascii"))
        await writer.drain()
        buf = b""
        try:
            buf = await asyncio.wait_for(reader.read(4096), timeout=3.0)
        except asyncio.TimeoutError:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        raw = buf.decode("utf-8", errors="replace").replace("\x00", "")
        if raw:
            try:
                d = json.loads(raw)
                msg = d.get("Msg", {})
                if isinstance(msg, dict):
                    mac = msg.get("mac", "").upper()
                    hostname = msg.get("hostname", "").lower()
                    if mac.startswith("C8:0C") or mac.startswith("C8:11") or "whatsminer" in hostname:
                        return "whatsminer"
            except Exception:
                pass
            # btminer responds with json always; if we got json back at all and no antminer marker
            if "WhatsMiner" in raw or "btminer" in raw.lower():
                return "whatsminer"
    except Exception:
        pass

    # 2. Try Antminer CGI
    try:
        async with httpx.AsyncClient(auth=httpx.DigestAuth("root", "root"), timeout=4.0) as client:
            r = await client.get(f"http://{ip}/cgi-bin/get_system_info.cgi")
            if r.status_code in (200, 401):
                text = r.text
                if "minertype" in text.lower() or r.status_code == 401:
                    return "antminer"
    except Exception:
        pass

    # 3. CGMiner version command (Antminer/Avalon legacy)
    resp = await cgminer_command(ip, {"command": "version"}, timeout=3.0)
    if resp:
        desc = str(resp).lower()
        if "whatsminer" in desc or "btminer" in desc:
            return "whatsminer"
        if "avalon" in desc or "mmq" in desc:
            return "avalon"
        return "antminer"

    return None


async def collect_miner(
    ip: str,
    miner_type: Optional[str] = None,
    username: str = "root",
    password: str = "root",
) -> Optional[MinerData]:
    """
    Detect and collect full data from a miner.
    """
    if miner_type is None:
        miner_type = await detect_miner_type(ip)
    if miner_type is None:
        logger.warning("Could not detect miner type", ip=ip)
        return None

    try:
        if miner_type == "antminer":
            client = AntminerClient(ip, username, password)
            return await client.collect()
        elif miner_type == "whatsminer":
            client = WhatsminerClient(ip)
            return await client.collect()
        elif miner_type == "avalon":
            client = AvalonClient(ip, username, password)
            return await client.collect()
        else:
            logger.warning("Unknown miner type", ip=ip, miner_type=miner_type)
            return None
    except Exception as e:
        logger.error("collect_miner failed", ip=ip, miner_type=miner_type, error=str(e))
        return None


async def flash_led(ip: str, miner_type: str, state: bool = True, username: str = "root", password: str = "root"):
    """Flash/unflash LED on miner."""
    if miner_type == "antminer":
        client = AntminerClient(ip, username, password)
        return await client.set_blink(state)
    elif miner_type == "whatsminer":
        client = WhatsminerClient(ip)
        return await client.set_led(state)
    elif miner_type == "avalon":
        # Avalon: use cgminer ascset
        cmd = {"command": "ascset", "parameter": f"0,led,{1 if state else 0}"}
        return await cgminer_command(ip, cmd)
    return {}


async def control_fans(ip: str, miner_type: str, mode: str, username: str = "root", password: str = "root"):
    """
    Control fan speed.
    mode: 'off' (0%), 'min' (20%), 'max' (100%), 'auto', or '0-100' (percentage)
    """
    pwm_map = {"off": 0, "min": 20, "max": 100, "auto": None}
    if mode in pwm_map:
        pwm = pwm_map[mode]
    else:
        try:
            pwm = int(mode)
        except ValueError:
            return {"error": f"Invalid fan mode: {mode}"}

    if miner_type == "antminer":
        client = AntminerClient(ip, username, password)
        if pwm is None:
            # auto — turn off manual control
            conf = await client.get_miner_conf()
            if conf:
                conf["bitmain-fan-ctrl"] = False
                return await client._post("cgi-bin/set_miner_conf.cgi", conf)
        return await client.set_fan_speed(pwm)
    elif miner_type in ("whatsminer", "avalon"):
        # Not all Whatsminer/Avalon support fan control via API
        cmd = {"command": "setconfig", "parameter": f"fan,{pwm}"}
        return await cgminer_command(ip, cmd)
    return {}
