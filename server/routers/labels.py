"""
Labels router — generate and print labels for miners.
"""

import logging
from datetime import datetime
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from db.session import get_db
from models.miner_model import MinerRecord
from models.schemas import LabelData, HashboardSchema
from services.label_printer import generate_label_bytes, print_label

logger = logging.getLogger(__name__)
router = APIRouter(tags=["labels"])

# ── Model expected specs ──────────────────────────────────────────────────────
# (ideal_th, expected_hashboards, tolerance_pct)
MODEL_SPECS = {
    "Antminer S19":        (95.0,  3, 5),
    "Antminer S19 Pro":    (110.0, 3, 5),
    "Antminer S19a":       (95.0,  3, 5),
    "Antminer S19a Pro":   (110.0, 3, 5),
    "Antminer S19j":       (90.0,  3, 5),
    "Antminer S19j Pro":   (100.0, 3, 5),
    "Antminer S19 XP":     (140.0, 3, 5),
    "Antminer S19 XP Hyd": (255.0, 3, 5),
    "Antminer S21":        (200.0, 3, 5),
    "Antminer T19":        (84.0,  3, 5),
    "M30S+":               (100.0, 3, 5),
    "M30S++":              (112.0, 3, 5),
    "M30S":                (88.0,  3, 5),
}


def _get_model_specs(model: str):
    """Return (ideal_th, expected_hb_count, tolerance_pct) or None."""
    for k, v in MODEL_SPECS.items():
        if k.lower() in (model or "").lower():
            return v
    return None


def _readiness_check(m) -> dict:
    """
    3-tier readiness check.

    tier = "pass"      — print normal deployment label
    tier = "info_only" — only missing serial/slot (operator can fix in <30s), hardware OK
    tier = "hardware"  — confirmed hardware fault: auto-print FAIL label, no blocking prompt

    Returns:
      tier, pass, hw_errors, info_errors, errors (compat), warnings,
      best_hashrate, active_hashboards, fault_summary
    """
    hw_errors   = []   # definitive hardware faults
    info_errors = []   # missing info only
    warnings    = []

    sn  = m.serial_number or ""
    loc = getattr(m, "location", None) or ""

    # ── Info gaps (fixable by operator) ──────────────────────────────────────
    if not sn or sn.strip() in ("", "— pending scan —"):
        info_errors.append("serial_missing")
    if not loc or loc.strip() in ("", "testbench"):
        info_errors.append("slot_missing")
    if not (m.worker or "").strip():
        warnings.append("No worker/pool assigned")

    # ── Hardware checks ──────────────────────────────────────────────────────
    specs  = _get_model_specs(m.model or "")
    rt     = m.rt_hashrate or 0
    avg    = m.avg_hashrate or 0
    best   = max(rt, avg)
    hbs    = m.hashboards or []
    active_hbs = len([
        h for h in hbs
        if (h.get("chip_count", 0) if isinstance(h, dict) else getattr(h, "chip_count", 0)) > 0
    ]) if hbs else 0

    fault_parts = []

    if m.psu_fault:
        hw_errors.append("PSU fault reported by miner")
        fault_parts.append("PSU fault")

    dead = m.dead_chips or 0
    if dead > 0:
        hw_errors.append(f"{dead} dead chips detected")
        fault_parts.append(f"{dead} dead chips")

    if specs:
        ideal_th, expected_hb, tol = specs
        min_th = ideal_th * (1 - tol / 100)

        if best < ideal_th * 0.05:
            # Near-zero hashrate — definitive hardware fault
            hw_errors.append(f"Hashrate {best:.1f} TH/s — miner not hashing (expected ≥{ideal_th:.0f})")
            fault_parts.append(f"0 TH/s ({ideal_th:.0f} expected)")
        elif best < min_th:
            # Below tolerance but not zero — marginal, flag as warning
            pct = round(best / ideal_th * 100)
            warnings.append(
                f"Hashrate {best:.1f} / {ideal_th:.0f} TH/s ({pct}% of ideal) — within 5% tolerance required"
            )

        if hbs and active_hbs < expected_hb:
            bad = [
                (h.get("position") if isinstance(h, dict) else getattr(h, "position", "?"))
                for h in hbs
                if (h.get("chip_count", 0) if isinstance(h, dict) else getattr(h, "chip_count", 0)) == 0
            ]
            desc = ", ".join(b for b in bad if b)
            hw_errors.append(
                f"Only {active_hbs}/{expected_hb} hashboards active"
                + (f" — dead board(s): {desc}" if desc else "")
            )
            if desc:
                fault_parts.append(f"Dead board: {desc}")
    else:
        warnings.append(f"Unknown model '{m.model}' — skipping hashrate validation")

    fault_summary = "; ".join(fault_parts)

    # ── Determine tier ───────────────────────────────────────────────────────
    if hw_errors:
        tier = "hardware"
    elif info_errors:
        tier = "info_only"
    else:
        tier = "pass"

    # backwards-compat combined errors list
    compat_errors = hw_errors + [
        "Serial number missing" if e == "serial_missing" else "Shelf slot not confirmed"
        for e in info_errors
    ]

    return {
        "tier":             tier,
        "pass":             tier == "pass",
        "hw_errors":        hw_errors,
        "info_errors":      info_errors,
        "errors":           compat_errors,
        "warnings":         warnings,
        "best_hashrate":    best,
        "active_hashboards":active_hbs,
        "fault_summary":    fault_summary,
    }


def _build_worker_instructions(m, check: dict) -> list:
    """Generate step-by-step instructions for the worker at the bench."""
    steps = []
    model = m.model or "this miner"
    slot  = getattr(m, "location", None) or "unknown slot"

    steps.append(f"STOP: {model} at {slot} is NOT ready to deploy. Complete these steps:")
    step = 1

    sn = m.serial_number or ""
    if not sn or sn.strip() in ("", "— pending scan —"):
        steps.append(f"  {step}. Scan or type the serial number (barcode sticker on miner body).")
        step += 1

    loc = getattr(m, "location", None) or ""
    if not loc or loc.strip() in ("", "testbench"):
        steps.append(
            f"  {step}. Confirm shelf slot — tap Verify Slot on the Info tab "
            f"or run Auto-Detect on the Bench tab."
        )
        step += 1

    specs = _get_model_specs(m.model or "")
    rt    = m.rt_hashrate or 0
    avg   = m.avg_hashrate or 0
    best  = max(rt, avg)

    if specs:
        ideal_th, expected_hb, _ = specs
        if best < ideal_th * 0.1:
            steps.append(
                f"  {step}. Hashrate near zero ({best:.1f} TH/s). Check:"
            )
            steps.append("       - All 3 power connectors firmly seated on each hashboard")
            steps.append("       - Ethernet cable connected and port light on")
            steps.append("       - Control board fan is spinning")
            steps.append("       - Reboot: Bench tab > Cycle Power on this slot")
            step += 1
        elif best < ideal_th * 0.95:
            pct = round(best / ideal_th * 100)
            steps.append(
                f"  {step}. Low hashrate {best:.1f} / {ideal_th:.0f} TH/s ({pct}%). "
                f"Reseat hashboard connectors."
            )
            step += 1

        act = check.get("active_hashboards", 0)
        if act < expected_hb:
            steps.append(
                f"  {step}. Only {act}/{expected_hb} hashboards active. "
                f"Check all board connectors and power headers."
            )
            step += 1

    dead = m.dead_chips or 0
    if dead > 0:
        steps.append(
            f"  {step}. {dead} dead chips detected. "
            f"Review the AI tab for diagnosis and repair suggestion."
        )
        step += 1

    if m.psu_fault:
        steps.append(f"  {step}. PSU fault active. Test with a known-good PSU or replace.")
        step += 1

    ai_fix = getattr(m, "ai_suggested_fix", None)
    if ai_fix and ai_fix not in ("None required.", ""):
        steps.append(f"  {step}. AI recommends: {ai_fix}")
        step += 1

    steps.append("  ✅ Once all issues are resolved, tap Print Label again.")
    return steps


def _build_label_data(m: MinerRecord) -> LabelData:
    hashboards = []
    if m.hashboards:
        for hb in m.hashboards:
            hashboards.append(HashboardSchema(**hb))

    serial  = m.serial_number or ""
    qr_id   = m.mac_address or serial or str(m.id)
    qr_data = "CRYPTOTHERM|" + qr_id + "|" + (m.model or "") + "|" + (m.mac_address or "")

    # Auto-correct status: dead hashboard = needs_repair regardless of DB status
    status = m.status or "unknown"
    dead_hbs = [hb for hb in hashboards if not hb.active]
    if dead_hbs and status == "ready_to_deploy":
        status = "needs_repair"

    return LabelData(
        serial_number    = serial if serial else "— pending scan —",
        model            = m.model or "Unknown",
        mac_address      = m.mac_address or "??:??:??:??:??:??",
        worker           = m.worker or "",
        hashrate         = float(m.rt_hashrate or m.avg_hashrate or 0.0),
        firmware_version = m.firmware_version or "",
        hashboards       = hashboards,
        ai_summary       = m.ai_issue_summary or "No issues detected.",
        ai_fix           = m.ai_suggested_fix or "None required.",
        status           = status,
        qr_data          = qr_data,
        location         = getattr(m, "location", None) or "",
        ip_address       = m.ip_address or "",
    )


# ── Routes ─────────────────────────────────────────────────────────────────


@router.get("/{miner_id}/check")
async def check_miner_readiness(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Dry-run readiness check — returns tier/errors without printing anything."""
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")
    try:
        check = _readiness_check(m)
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
    return {
        "miner_id":      miner_id,
        "tier":          check["tier"],
        "pass":          check["pass"],
        "hw_errors":     check["hw_errors"],
        "info_errors":   check["info_errors"],
        "warnings":      check["warnings"],
        "fault_summary": check["fault_summary"],
        "best_hashrate": check["best_hashrate"],
    }

@router.get("/{miner_id}/preview")
async def preview_label(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Return label as PNG image for preview."""
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")

    label     = _build_label_data(m)
    png_bytes = generate_label_bytes(label)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f"inline; filename=label_{miner_id}.png"},
    )


@router.post("/{miner_id}/print")
async def print_miner_label(
    miner_id:           int,
    printer_identifier: Optional[str] = None,
    force:              bool = Query(False),
    db:                 AsyncSession = Depends(get_db),
):
    """
    Print label for a miner.
    Runs a readiness check first — fails with 422 + worker instructions if not ready.
    Pass ?force=true to skip checks (e.g. reprints).
    """
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")

    try:
        check = _readiness_check(m)
    except Exception as exc:
        logger.error("readiness check error miner=%s: %s", miner_id, exc)
        raise HTTPException(500, detail=f"Readiness check error: {exc}")

    import asyncio as _aio
    from services.ai_diagnosis import diagnose_miner, get_next_action
    hb_dicts = [
        dict(h) if hasattr(h, "items") else vars(h)
        for h in (m.hashboards or [])
    ]

    tier = check["tier"]

    # ── TIER: HARDWARE FAULT — but collect serial/slot first if missing ─────────
    if tier == "hardware" and not force:
        # Before printing fail label, ensure we have enough info to identify the unit
        missing_info = check["info_errors"]  # serial_missing / slot_missing
        if missing_info:
            # Can't print fail label without knowing which unit it is
            # Ask operator to provide missing info first, then auto-retry
            next_action = ""
            if "serial_missing" in missing_info and "slot_missing" in missing_info:
                next_action = "Scan the serial number barcode on the miner — then confirm the shelf slot."
            elif "serial_missing" in missing_info:
                next_action = "Scan the serial number barcode on the miner body before the FAIL label can print."
            else:
                next_action = "Confirm the shelf slot (Auto-Detect on Bench tab) so the FAIL label shows the correct rack position."

            instructions = _build_worker_instructions(m, check)
            raise HTTPException(
                status_code=422,
                detail={
                    "type":         "hw_info_needed",
                    "tier":         "hardware",
                    "info_errors":  missing_info,
                    "hw_errors":    check["hw_errors"],
                    "warnings":     check["warnings"],
                    "next_action":  next_action,
                    "fault_summary": check["fault_summary"],
                    "miner_id":     miner_id,
                    "model":        m.model or "Unknown",
                    "worker":       m.worker or "",
                    "serial":       m.serial_number or "",
                    "location":     getattr(m, "location", None) or "",
                    "ai_issue":     m.ai_issue_summary or "",
                    "ai_fix":       m.ai_suggested_fix or "",
                    "instructions": instructions,
                    "note": "Provide the missing info then tap Print Label — the FAIL diagnostic label will print automatically.",
                },
            )

        diagnosis   = None
        next_action = ""
        try:
            _res = await _aio.gather(
                diagnose_miner(
                    model          = m.model or "",
                    serial         = m.serial_number or "",
                    hashboards     = hb_dicts,
                    logs           = m.raw_logs or "",
                    rt_hashrate    = float(m.rt_hashrate or 0),
                    ideal_hashrate = float(m.ideal_hashrate or 0),
                ),
                get_next_action(
                    errors     = check["hw_errors"],
                    model      = m.model or "",
                    hashboards = hb_dicts,
                ),
                return_exceptions=True,
            )
            if not isinstance(_res[0], Exception):
                diagnosis = _res[0]
                m.ai_issue_summary = diagnosis.issue_summary
                m.ai_suggested_fix = diagnosis.suggested_fix
                m.ai_severity      = diagnosis.severity
                m.ai_auto_fixable  = diagnosis.auto_fixable
                try:
                    await db.commit()
                except Exception:
                    pass
            if not isinstance(_res[1], Exception):
                next_action = str(_res[1])
        except Exception:
            pass

        from services.label_printer import generate_fail_label_bytes, print_raw_bytes
        _hbs = m.hashboards if m.hashboards else []
        if _hbs and hasattr(_hbs[0], '__dict__'):
            _hbs = [h.__dict__ for h in _hbs]
        fail_bytes = generate_fail_label_bytes(
            model         = m.model or "Unknown",
            serial        = m.serial_number or "— no S/N —",
            mac           = m.mac_address or "",
            worker        = m.worker or "",
            fault_summary = check["fault_summary"],
            ai_issue      = (diagnosis.issue_summary if diagnosis else m.ai_issue_summary) or "",
            ai_fix        = (diagnosis.suggested_fix if diagnosis else m.ai_suggested_fix) or "",
            next_action   = next_action,
            hw_errors     = check["hw_errors"],
            warnings      = check["warnings"],
            hashboards    = _hbs,
            fan_speeds    = list(m.fan_speeds or []),
            psu_voltage   = float(m.psu_voltage or 0),
            psu_current   = float(m.psu_current or 0),
        )
        result = await print_raw_bytes(fail_bytes, printer_identifier=printer_identifier)
        return {
            **result,
            "label_type":    "fail",
            "tier":          "hardware",
            "hw_errors":     check["hw_errors"],
            "fault_summary": check["fault_summary"],
            "next_action":   next_action,
        }

    # ── TIER: INFO ONLY — missing serial and/or slot, hardware OK ────────────
    if tier == "info_only" and not force:
        # For info-only issues, use deterministic instructions (no LLM needed)
        if "serial_missing" in check["info_errors"] and "slot_missing" in check["info_errors"]:
            next_action = "Scan the serial number barcode on the miner body — slot confirm will follow."
        elif "serial_missing" in check["info_errors"]:
            next_action = "Scan the serial number barcode on the miner body."
        else:
            next_action = "Run Auto-Detect on the Bench tab to confirm this miner's shelf slot."

        raise HTTPException(
            status_code=422,
            detail={
                "type":        "info_needed",
                "tier":        "info_only",
                "info_errors": check["info_errors"],
                "warnings":    check["warnings"],
                "next_action": next_action,
                "miner_id":    miner_id,
                "model":       m.model or "Unknown",
                "worker":      m.worker or "",
                "serial":      m.serial_number or "",
                "location":    getattr(m, "location", None) or "",
            },
        )

    # ── TIER: PASS (or forced) — print normal deployment label ───────────────
    label  = _build_label_data(m)
    result = await print_label(label, printer_identifier=printer_identifier)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("message", "Print failed"))
    if tier == "pass":
        m.smart_test_completed    = True
        m.smart_test_completed_at = datetime.utcnow()
        m.test_count              = (m.test_count or 0) + 1
        await db.commit()
    return {**result, "label_type": "deploy", "tier": tier}


@router.get("/{miner_id}/data")
async def get_label_data(miner_id: int, db: AsyncSession = Depends(get_db)):
    """Return label data as JSON (for iOS app rendering)."""
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")
    return _build_label_data(m)


@router.post("/shelf/{slot}/print")
async def print_shelf_label(slot: str):
    """Print a simple shelf position barcode label."""
    from services.label_printer import print_shelf_label as _print_shelf
    try:
        result = await _print_shelf(slot.upper())
        return {"ok": True, "slot": slot.upper(), "result": result}
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))


@router.get("/shelf/{slot}/preview")
async def preview_shelf_label(slot: str):
    """Return shelf label as PNG for preview."""
    from services.label_printer import generate_shelf_label_image
    import io as _io
    img = generate_shelf_label_image(slot.upper())
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


class ShelfSlot(BaseModel):
    label: str
    rack: str
    phase: str
    outlet: int
    ip_range: str = ""

class ShelfBarcodesRequest(BaseModel):
    slots: list[ShelfSlot]


@router.post("/shelf-barcodes")
async def print_shelf_barcodes(req: ShelfBarcodesRequest, db: AsyncSession = Depends(get_db)):
    """Print a barcode label for every shelf slot."""
    from PIL import Image, ImageDraw, ImageFont
    import barcode as python_barcode
    from barcode.writer import ImageWriter
    import io

    LABEL_W = 696   # 62mm @ 300dpi
    LABEL_H = 271   # ~29mm tall — compact shelf label

    printed = 0
    errors = []

    for slot in req.slots:
        try:
            img = Image.new("RGB", (LABEL_W, LABEL_H), "white")
            draw = ImageDraw.Draw(img)

            # Try to load fonts
            try:
                from PIL import ImageFont
                font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
                font_md = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
                font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
            except Exception:
                font_lg = font_md = font_sm = ImageFont.load_default()

            # ── Barcode (Code128, slot label) ──────────────────────────────
            try:
                Code128 = python_barcode.get_barcode_class("code128")
                bc_io = io.BytesIO()
                writer = ImageWriter()
                writer.set_options({
                    "module_height": 8.0,
                    "module_width": 0.9,
                    "font_size": 0,
                    "text_distance": 0,
                    "quiet_zone": 2.0,
                    "write_text": False,
                })
                Code128(slot.label, writer=writer).write(bc_io)
                bc_io.seek(0)
                bc_img = Image.open(bc_io).convert("RGB")
                bc_w = int(LABEL_W * 0.52)
                bc_h = int(LABEL_H * 0.65)
                bc_img = bc_img.resize((bc_w, bc_h), Image.LANCZOS)
                img.paste(bc_img, (8, LABEL_H - bc_h - 8))
            except Exception as e:
                errors.append(f"{slot.label}: barcode err {e}")

            # ── Slot label (big, top-left) ─────────────────────────────────
            draw.text((12, 8), slot.label, fill="black", font=font_lg)

            # ── Rack / Phase / Outlet info ─────────────────────────────────
            info_x = int(LABEL_W * 0.56)
            draw.text((info_x, 10), f"Rack {slot.rack}", fill="black", font=font_md)
            draw.text((info_x, 40), f"Phase {slot.phase}", fill="#444444", font=font_sm)
            draw.text((info_x, 65), f"PDU outlet {slot.outlet}", fill="#444444", font=font_sm)
            if slot.ip_range:
                draw.text((info_x, 92), slot.ip_range, fill="#2266aa", font=font_sm)

            # ── Divider line ───────────────────────────────────────────────
            draw.line([(info_x - 6, 6), (info_x - 6, LABEL_H - 6)], fill="#cccccc", width=1)

            # ── Print via library (same path as miner labels) ─────────────
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)

            try:
                from services.label_printer import print_raw_bytes
                result = await print_raw_bytes(buf.getvalue())
                if result.get("did_print") or result.get("success"):
                    printed += 1
                else:
                    errors.append(f"{slot.label}: {result.get('message','print failed')}")
            except Exception as pe:
                errors.append(f"{slot.label}: {pe}")

        except Exception as e:
            errors.append(f"{slot.label}: {e}")

    return {
        "ok": printed > 0,
        "printed": printed,
        "errors": errors,
        "total": len(req.slots),
    }


@router.post("/{miner_id}/print/fail")
async def print_fail_label(
    miner_id:           int,
    printer_identifier: Optional[str] = None,
    db:                 AsyncSession = Depends(get_db),
):
    """Always print the long-form fail/diagnostic label for a miner."""
    m = await db.get(MinerRecord, miner_id)
    if not m:
        raise HTTPException(status_code=404, detail="Miner not found")

    try:
        check = _readiness_check(m)
    except Exception as exc:
        check = {"hw_errors": [], "warnings": [], "fault_summary": "", "tier": "pass"}

    from services.label_printer import generate_fail_label_bytes, print_raw_bytes

    _hbs2 = m.hashboards if m.hashboards else []
    if _hbs2 and hasattr(_hbs2[0], '__dict__'):
        _hbs2 = [h.__dict__ for h in _hbs2]
    fail_bytes = generate_fail_label_bytes(
        model         = m.model or "Unknown",
        serial        = m.serial_number or "— no S/N —",
        mac           = m.mac_address or "",
        worker        = m.worker or "",
        fault_summary = check.get("fault_summary", ""),
        ai_issue      = m.ai_issue_summary or "",
        ai_fix        = m.ai_suggested_fix or "",
        next_action   = "",
        hw_errors     = check.get("hw_errors", []),
        warnings      = check.get("warnings", []),
        hashboards    = _hbs2,
        fan_speeds    = list(m.fan_speeds or []),
        psu_voltage   = float(m.psu_voltage or 0),
        psu_current   = float(m.psu_current or 0),
    )
    result = await print_raw_bytes(fail_bytes, printer_identifier=printer_identifier)
    return {
        **result,
        "label_type":    "fail",
        "miner_id":      miner_id,
        "fault_summary": check.get("fault_summary", ""),
    }
