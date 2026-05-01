"""
AI diagnosis service — uses local Qwen2.5-Coder LLM via Ollama (Alienware-1) for miner analysis.
Falls back to rule-based diagnosis if the LLM is unavailable.
"""

from __future__ import annotations
import json
import asyncio
import structlog
from openai import AsyncOpenAI
from config import settings
from models.schemas import DiagnosisSchema

logger = structlog.get_logger()

_OLLAMA_BASE_URL = "http://100.86.10.89:11434/v1"
_OLLAMA_MODEL    = "qwen2.5-coder:7b"
_OLLAMA_API_KEY  = "ollama"
_LLM_TIMEOUT_S   = 20   # hard cap on full diagnosis
_ACTION_TIMEOUT  = 10   # shorter cap for next-action (single sentence)

client = AsyncOpenAI(
    api_key=_OLLAMA_API_KEY,
    base_url=_OLLAMA_BASE_URL,
    timeout=_LLM_TIMEOUT_S,
)

SYSTEM_PROMPT = """You are a senior Bitcoin ASIC miner technician AI for Cryptotherm.
Analyse miner logs and hardware data to diagnose faults.

Respond ONLY with valid JSON matching exactly this schema:
{
  "issue_summary": "string (<=200 chars)",
  "suggested_fix":  "string (<=200 chars)",
  "auto_fixable": true|false,
  "severity": "ok"|"warning"|"critical",
  "next_action": "string (<=120 chars) — ONE imperative sentence: the single most important thing the worker should do RIGHT NOW"
}

Guidelines:
- severity=ok: miner healthy
- severity=warning: degraded performance, fix recommended
- severity=critical: hardware fault, must repair before deployment
- auto_fixable=true only if fix can be done via API (restart, reconfigure pool, adjust limits)
- next_action must be specific, direct, imperative — e.g. "Reseat the LEFT hashboard connector (bottom of miner, 10-pin white plug)."
- Be specific: mention hashboard by position (LEFT/CENTER/RIGHT), chip counts, temps
"""

ACTION_SYSTEM = """You are a Bitcoin ASIC miner technician AI for Cryptotherm.
Given a list of miner problems, reply with ONE sentence only — the most important thing the operator should do RIGHT NOW.
No preamble, no explanation, no list. Direct imperative. Max 120 characters.
Examples:
  "Scan the serial number barcode on top of the miner."
  "Reseat the LEFT hashboard power connector (10-pin white plug, bottom of miner)."
  "Run Auto-Detect on the Bench tab to confirm this miner's shelf slot."
"""


async def get_next_action(errors: list[str], model: str, hashboards: list[dict]) -> str:
    """
    Ask the LLM for a single next-action sentence for the bench worker.
    Falls back to the first error reworded as an instruction.
    """
    if not errors:
        return "Verify all connections are secure, then tap Print Label."

    try:
        hb_info = ""
        if hashboards:
            hb_info = " Hashboards: " + ", ".join(
                f"{h.get('position','HB?')} {h.get('chip_count',0)}/{h.get('expected_chips','?')} chips"
                for h in hashboards
            )

        user_msg = (
            f"Miner: {model}\n"
            f"Problems detected:\n" +
            "\n".join(f"- {e}" for e in errors) +
            hb_info
        )

        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=_OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": ACTION_SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.1,
                max_tokens=60,
            ),
            timeout=_ACTION_TIMEOUT,
        )
        action = (response.choices[0].message.content or "").strip().strip('"').strip("'")
        # Strip any accidental prefix like "Next action:" or numbering
        for prefix in ("next action:", "action:", "1.", "•", "-"):
            if action.lower().startswith(prefix):
                action = action[len(prefix):].strip()
        return action if action else _fallback_action(errors)

    except Exception as exc:
        logger.warning("get_next_action LLM failed: %s — using fallback", exc)
        return _fallback_action(errors)


def _fallback_action(errors: list[str]) -> str:
    """Rule-based fallback — pick the most actionable first error."""
    priority_keywords = [
        ("serial", "Scan the serial number barcode on the miner body."),
        ("slot", "Run Auto-Detect on the Bench tab to confirm the shelf slot."),
        ("hashboard", "Reseat all hashboard data and power connectors, then cycle power."),
        ("hashrate", "Check that all power connectors are seated, then cycle power from the Bench tab."),
        ("dead chip", "Inspect hashboards for physical damage; replace the faulted board."),
        ("psu", "Swap in a known-good PSU and retest."),
        ("mac", "Reconnect the Ethernet cable and wait 60 seconds for the miner to register."),
    ]
    first = errors[0].lower() if errors else ""
    for keyword, instruction in priority_keywords:
        if keyword in first:
            return instruction
    return errors[0] if errors else "Inspect the miner and address the errors listed above."


async def diagnose_miner(
    model:          str,
    serial:         str,
    hashboards:     list[dict],
    logs:           str,
    rt_hashrate:    float = 0,
    ideal_hashrate: float = 0,
) -> DiagnosisSchema:
    """
    Run AI diagnosis on miner data.
    Hard-capped at _LLM_TIMEOUT_S seconds; falls back to rule-based on any failure.
    """
    try:
        return await asyncio.wait_for(
            _llm_diagnose(model, serial, hashboards, logs, rt_hashrate, ideal_hashrate),
            timeout=_LLM_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("LLM diagnosis timed out — using rule-based fallback")
        return _rule_based_diagnosis(hashboards, rt_hashrate, ideal_hashrate)
    except Exception as exc:
        logger.error("LLM diagnosis error: %s — using rule-based fallback", exc)
        return _rule_based_diagnosis(hashboards, rt_hashrate, ideal_hashrate)


async def _llm_diagnose(
    model:          str,
    serial:         str,
    hashboards:     list[dict],
    logs:           str,
    rt_hashrate:    float,
    ideal_hashrate: float,
) -> DiagnosisSchema:
    hb_lines = []
    for hb in hashboards:
        idx      = hb.get("index", "?")
        pos      = hb.get("position", f"HB{idx}")
        chips    = hb.get("chip_count", 0)
        expected = hb.get("expected_chips", "?")
        t_avg    = hb.get("temp_avg", 0)
        t_max    = hb.get("temp_max", 0)
        health   = hb.get("health_pct", 0)
        hb_lines.append(
            f"  {pos}: {chips}/{expected} chips, "
            f"temp_avg={t_avg:.1f}C, temp_max={t_max:.1f}C, health={health:.1f}%"
        )
    hb_summary = "\n".join(hb_lines) if hb_lines else "  [No hashboard data]"
    eff = f"  |  Efficiency: {round((rt_hashrate/ideal_hashrate)*100,1)}%" if ideal_hashrate else ""

    user_msg = (
        f"Miner: {model}\nSerial: {serial}\n"
        f"RT Hashrate: {rt_hashrate:.2f} TH/s  |  Ideal: {ideal_hashrate:.2f} TH/s{eff}\n\n"
        f"HASHBOARDS:\n{hb_summary}\n\n"
        f"LOGS (last 2000 chars):\n{(logs or '')[-2000:] or '[No logs]'}\n"
    )

    response = await client.chat.completions.create(
        model=_OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.1,
        max_tokens=512,
    )
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
    return DiagnosisSchema(
        issue_summary = data.get("issue_summary", "Analysis complete"),
        suggested_fix = data.get("suggested_fix",  "No action required"),
        auto_fixable  = bool(data.get("auto_fixable", False)),
        severity      = data.get("severity", "ok"),
        next_action   = data.get("next_action", ""),
    )


def _rule_based_diagnosis(
    hashboards:     list[dict],
    rt_hashrate:    float,
    ideal_hashrate: float,
) -> DiagnosisSchema:
    issues   = []
    severity = "ok"
    auto_fix = False

    for hb in hashboards:
        pct      = hb.get("health_pct", 100)
        missing  = hb.get("missing_chips", 0)
        t_max    = hb.get("temp_max", 0)
        pos      = hb.get("position", f"HB{hb.get('index','?')}")

        if pct < 50:
            issues.append(f"{pos} critically degraded ({pct:.0f}%)")
            severity = "critical"
        elif pct < 80:
            issues.append(f"{pos} degraded ({pct:.0f}%)")
            if severity != "critical":
                severity = "warning"
        if missing > 0:
            issues.append(f"{pos}: {missing} dead chips")
            if severity != "critical":
                severity = "warning"
        if t_max > 90:
            issues.append(f"{pos} overtemp {t_max:.0f}C")
            if severity == "ok":
                severity = "warning"
                auto_fix = True

    if ideal_hashrate > 0 and rt_hashrate < ideal_hashrate * 0.05:
        issues.append(f"Hashrate near zero ({rt_hashrate:.1f} TH/s) — miner not hashing")
        if severity != "critical":
            severity = "critical"
        # Power cycle may resolve stuck firmware or boot loop
        if not any("dead" in i or "degraded" in i for i in issues):
            auto_fix = True
    elif ideal_hashrate > 0 and rt_hashrate < ideal_hashrate * 0.7:
        issues.append(f"Hashrate only {round((rt_hashrate/ideal_hashrate)*100)}% of ideal")
        if severity == "ok":
            severity = "warning"

    if not issues:
        return DiagnosisSchema(
            issue_summary = "No issues detected.",
            suggested_fix = "None required.",
            auto_fixable  = False,
            severity      = "ok",
            next_action   = "",
        )

    return DiagnosisSchema(
        issue_summary = "; ".join(issues[:3]),
        suggested_fix = (
            "Inspect flagged hashboards. Replace faulty boards or chips before deployment."
            if severity == "critical"
            else "Monitor closely. Consider preventive maintenance."
        ),
        auto_fixable  = auto_fix,
        severity      = severity,
        next_action   = "",
    )
