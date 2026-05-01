"""
Label printer service — generates labels for Brother QL-810W (62mm tape).
Uses brother_ql library with network (TCP) backend.
"""

from __future__ import annotations
import io
import structlog
from PIL import Image, ImageDraw, ImageFont
import qrcode
import barcode
from barcode.writer import ImageWriter
from typing import Optional
from models.schemas import LabelData
from config import settings

logger = structlog.get_logger()

# Label dimensions for 62mm continuous tape at 300dpi
# 62mm wide = ~732 px at 300dpi
LABEL_WIDTH_PX = 732
LABEL_DPI = 300

# Font sizes (using PIL default font; in production use a .ttf)
FONT_LARGE = 32
FONT_MEDIUM = 24
FONT_SMALL = 18

STATUS_ICONS = {
    "ready_to_deploy": "✓ READY TO DEPLOY",
    "needs_repair": "★ NEEDS REPAIR",
    "parts_unit": "⚠ PARTS UNIT",
    "psu_fault": "✗ PSU FAULT",
    "control_board_fault": "✗ CONTROL BOARD FAULT",
    "unknown": "? UNKNOWN",
}

STATUS_COLORS = {
    "ready_to_deploy": (0, 180, 0),
    "needs_repair": (220, 120, 0),
    "parts_unit": (180, 0, 180),
    "psu_fault": (220, 0, 0),
    "control_board_fault": (220, 0, 0),
    "unknown": (100, 100, 100),
}

HB_HEALTH_COLOR = {
    "good": (0, 180, 0),
    "warn": (220, 150, 0),
    "bad": (220, 0, 0),
}


def _get_font(size: int):
    """Try to load a bundled TTF, fall back to PIL default."""
    try:
        from PIL import ImageFont
        # Try DejaVu which is usually available on Linux
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        try:
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
        except Exception:
            return ImageFont.load_default()


def _hb_color(health_pct: float) -> tuple:
    if health_pct >= 85:
        return HB_HEALTH_COLOR["good"]
    elif health_pct >= 60:
        return HB_HEALTH_COLOR["warn"]
    else:
        return HB_HEALTH_COLOR["bad"]


def generate_label_image(label: LabelData) -> Image.Image:
    """
    62mm continuous tape label layout (732px wide):
    ┌──────────────────────────────────────────────────────────────┐
    │ [QR 160px] │ MODEL                    [STATUS PILL]         │
    │            │ IP: 192.168.3.4   Slot: AL1-3                  │
    │            │ S/N: xxx  MAC: xx:xx:xx:xx                     │
    │            │ Worker: cryptotherm.CTM-046                    │
    ├────────────┼──────────────────────────────────────────────── │
    │            │ HASHBOARDS                                      │
    │            │ ⚠ LEFT: DEAD — REPLACE/RESEAT (0/100 chips)   │  <- dead
    │            │ ✓ CENTER: 100% | 50.0 TH/s | 75°C avg         │  <- good
    │            │ ✓ RIGHT:  98%  | 49.5 TH/s | 73°C avg         │  <- good
    ├────────────┴──────────────────────────────────────────────── │
    │ [Barcode: serial number]                                     │
    └──────────────────────────────────────────────────────────────┘
    """
    MARGIN  = 14
    QR_SIZE = 160
    INFO_X  = QR_SIZE + MARGIN * 2
    INFO_W  = LABEL_WIDTH_PX - INFO_X - MARGIN

    font_xl  = _get_font(30)
    font_lg  = _get_font(24)
    font_md  = _get_font(20)
    font_sm  = _get_font(17)
    font_xs  = _get_font(14)

    height = 900
    img  = Image.new("RGB", (LABEL_WIDTH_PX, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    y = MARGIN

    # ── QR code ──────────────────────────────────────────────────────────────
    qr = qrcode.QRCode(version=2, box_size=5, border=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(label.qr_data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((QR_SIZE, QR_SIZE), Image.LANCZOS)
    img.paste(qr_img, (MARGIN, MARGIN))

    sn_text = (label.serial_number or "")[:18]
    if sn_text and sn_text != "— pending scan —":
        bbox = draw.textbbox((0, 0), sn_text, font=font_xs)
        tw = bbox[2] - bbox[0]
        draw.text((MARGIN + (QR_SIZE - tw) // 2, MARGIN + QR_SIZE + 4),
                  sn_text, font=font_xs, fill=(80, 80, 80))

    # ── Determine dead/good hashboards ───────────────────────────────────────
    dead_hbs  = [hb for hb in (label.hashboards or []) if not hb.active]
    good_hbs  = [hb for hb in (label.hashboards or []) if hb.active]
    has_fault = bool(dead_hbs)

    # Override status color if hardware fault
    effective_status = label.status
    if has_fault and effective_status == "ready_to_deploy":
        effective_status = "needs_repair"

    status_color = STATUS_COLORS.get(effective_status, (100, 100, 100))
    status_text  = STATUS_ICONS.get(effective_status, effective_status.upper())

    # ── Row 1: Model + status pill ────────────────────────────────────────────
    draw.text((INFO_X, y), label.model, font=font_xl, fill=(0, 0, 0))
    pill_bbox = draw.textbbox((0, 0), status_text, font=font_sm)
    pill_w = pill_bbox[2] - pill_bbox[0] + 16
    pill_h = pill_bbox[3] - pill_bbox[1] + 8
    pill_x = LABEL_WIDTH_PX - MARGIN - pill_w
    draw.rectangle([pill_x, y + 2, pill_x + pill_w, y + 2 + pill_h], fill=status_color)
    draw.text((pill_x + 8, y + 6), status_text, font=font_sm, fill=(255, 255, 255))
    y += 36

    # ── Row 2: IP + Slot ──────────────────────────────────────────────────────
    ip_text   = f"IP: {label.ip_address}" if label.ip_address else "IP: unknown"
    slot_text = f"Slot: {label.location}" if label.location else "Slot: unassigned"
    draw.text((INFO_X, y), ip_text, font=font_sm, fill=(0, 80, 180))
    slot_bbox = draw.textbbox((0, 0), slot_text, font=font_sm)
    draw.text((INFO_X + INFO_W - (slot_bbox[2] - slot_bbox[0]), y),
              slot_text, font=font_sm, fill=(0, 100, 50))
    y += 24

    # ── Row 3: S/N + MAC ─────────────────────────────────────────────────────
    sn_disp = label.serial_number if label.serial_number and label.serial_number != "— pending scan —" else "S/N: —"
    if not sn_disp.startswith("S/N:"):
        sn_disp = f"S/N: {sn_disp}"
    draw.text((INFO_X, y), sn_disp, font=font_xs, fill=(60, 60, 60))
    mac_text = f"MAC: {label.mac_address}"
    mac_bbox = draw.textbbox((0, 0), mac_text, font=font_xs)
    draw.text((INFO_X + INFO_W - (mac_bbox[2] - mac_bbox[0]), y),
              mac_text, font=font_xs, fill=(60, 60, 60))
    y += 22

    # ── Row 4: Worker + Hashrate ──────────────────────────────────────────────
    wk = label.worker or "—"
    hr = f"{label.hashrate:.1f} TH/s" if label.hashrate else "0 TH/s"
    draw.text((INFO_X, y), f"Worker: {wk}", font=font_xs, fill=(60, 60, 60))
    hr_bbox = draw.textbbox((0, 0), hr, font=font_md)
    hr_color = (0, 140, 0) if label.hashrate and label.hashrate > 10 else (180, 60, 0)
    draw.text((INFO_X + INFO_W - (hr_bbox[2] - hr_bbox[0]), y), hr, font=font_md, fill=hr_color)
    y += 26

    draw.line((INFO_X, y, LABEL_WIDTH_PX - MARGIN, y), fill=(0, 0, 0), width=2)
    y += 8

    # ── Hashboards section ───────────────────────────────────────────────────
    if label.hashboards:
        draw.text((INFO_X, y), "HASHBOARDS", font=font_sm, fill=(0, 0, 0))
        y += 22

        # Estimate per-board hashrate (divide total evenly among active boards)
        active_count = len(good_hbs) or 1
        per_hb_hr = label.hashrate / active_count if label.hashrate else 0.0

        for hb in label.hashboards:
            pos = (hb.position or f"HB{hb.index}").upper()
            if not hb.active:
                # Dead board — prominent warning
                warn_color = (200, 0, 0)
                draw.rectangle([INFO_X, y, LABEL_WIDTH_PX - MARGIN, y + 32],
                                fill=(255, 230, 230), outline=warn_color, width=2)
                warn_text = f"  X  {pos}: DEAD — REPLACE/RESEAT  ({hb.chip_count}/{hb.expected_chips} chips)"
                draw.text((INFO_X + 4, y + 6), warn_text, font=font_sm, fill=warn_color)
                y += 36
            else:
                # Good board — show health, estimated hashrate, temp
                pct   = int(hb.health_pct or 0)
                temp  = f"{hb.temp_avg:.0f}C" if hb.temp_avg else "—"
                color = _hb_color(hb.health_pct)
                check = "OK" if pct >= 85 else "WARN"
                line  = f"  {check}  {pos}: {pct}% health | ~{per_hb_hr:.1f} TH/s | {temp} avg"
                draw.text((INFO_X, y), line, font=font_sm, fill=color)
                y += 24

        y += 4
        draw.line((INFO_X, y, LABEL_WIDTH_PX - MARGIN, y), fill=(180, 180, 180), width=1)
        y += 8

    # ── AI fix note (only for faults) ────────────────────────────────────────
    if label.ai_fix and label.ai_fix not in ("None required.", ""):
        ai_lines = _wrap_text(f"Action: {label.ai_fix}", font_xs, INFO_W)
        for line in ai_lines[:2]:
            draw.text((INFO_X, y), line, font=font_xs, fill=(0, 0, 160))
            y += 19
        y += 4

    # ── Barcode strip ─────────────────────────────────────────────────────────
    raw_sn = label.serial_number or ""
    barcode_value = raw_sn.strip() if raw_sn.strip() not in ("", "— pending scan —") else ""
    if barcode_value:
        try:
            from io import BytesIO
            Code128 = barcode.get_barcode_class("code128")
            bc = Code128(barcode_value, writer=ImageWriter())
            bc_buf = BytesIO()
            bc.write(bc_buf, options={
                "module_width": 0.8, "module_height": 10.0,
                "font_size": 7, "text_distance": 2.0,
                "quiet_zone": 2.0, "background": "white", "foreground": "black",
                "write_text": True,
            })
            bc_buf.seek(0)
            bc_img = Image.open(bc_buf).convert("RGB")
            bc_w   = int((LABEL_WIDTH_PX - MARGIN * 2) * 0.78)
            bc_h   = int(bc_img.height * bc_w / bc_img.width * 1.2)
            bc_img = bc_img.resize((bc_w, bc_h), Image.LANCZOS)
            needed = y + bc_h + MARGIN + 4
            if needed > img.height:
                canvas = Image.new("RGB", (LABEL_WIDTH_PX, needed + 20), (255, 255, 255))
                canvas.paste(img, (0, 0))
                img  = canvas
                draw = ImageDraw.Draw(img)
            img.paste(bc_img, (MARGIN, y + 4))
            y += bc_h + MARGIN
        except Exception:
            pass

    final_h = max(MARGIN + QR_SIZE + 30, y + MARGIN)
    return img.crop((0, 0, LABEL_WIDTH_PX, final_h))


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    """Simple word-wrap."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        # ImageFont doesn't have getsize anymore — use getlength
        try:
            w = font.getlength(test)
        except AttributeError:
            w = len(test) * 10  # fallback
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines



def generate_fail_label_bytes(
    model: str = "",
    serial: str = "",
    mac: str = "",
    worker: str = "",
    fault_summary: str = "",
    ai_issue: str = "",
    ai_fix: str = "",
    next_action: str = "",
    hw_errors: list = None,
    warnings: list = None,
    hashboards: list = None,
    fan_speeds: list = None,
    psu_voltage: float = 0.0,
    psu_current: float = 0.0,
) -> bytes:
    """
    Long-form FAIL diagnostic label.
    Sections: hashboard breakdown (HB0/1/2), system diagnosis, likely causes, repair steps.
    62mm continuous tape.
    """
    from datetime import datetime
    from PIL import Image, ImageDraw
    import io as _io, re as _re

    W       = LABEL_WIDTH_PX
    M       = 14
    font_xl = _get_font(27)
    font_lg = _get_font(21)
    font_md = _get_font(18)
    font_sm = _get_font(15)
    font_xs = _get_font(12)
    font_xx = _get_font(11)

    def wrap(text, font, max_w):
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            try:    tw = font.getlength(test)
            except: tw = len(test) * 8
            if tw <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    CW = W - M * 2

    def _hb(h, key, default=None):
        return h.get(key, default) if isinstance(h, dict) else getattr(h, key, default)

    hbs = hashboards or []
    pos_map = {}
    for h in hbs:
        idx = _hb(h, 'index', None)
        pos = (_hb(h, 'position', '') or '').upper()
        if idx:
            pos_map[idx] = h
        elif pos:
            pos_map[{'LEFT': 1, 'CENTER': 2, 'RIGHT': 3}.get(pos, 99)] = h
    ordered_hbs = [pos_map.get(i, None) for i in [1, 2, 3]]

    fans = fan_speeds or []
    no_fan_data = len(fans) == 0

    dead_boards = []
    degraded_boards = []
    for slot_idx, h in enumerate(ordered_hbs, start=1):
        name = {1: 'LEFT', 2: 'CENTER', 3: 'RIGHT'}[slot_idx]
        if h is None:
            dead_boards.append((slot_idx, name))
            continue
        chips   = _hb(h, 'chip_count', 0) or 0
        missing = _hb(h, 'missing_chips', 0) or 0
        expected= _hb(h, 'expected_chips', 0) or 0
        active  = _hb(h, 'active', True)
        if not active or chips == 0:
            dead_boards.append((slot_idx, name))
        elif missing > 0:
            pct = round(chips / max(expected, 1) * 100)
            degraded_boards.append((slot_idx, name, pct, missing))

    repair_steps = []
    repair_steps.append("Power cycle miner and re-check before disassembly")
    repair_steps.append("Verify all PSU connector tabs fully seated on each board")
    for idx, name in dead_boards:
        repair_steps.append(
            f"HB{idx-1} ({name}): Reseat data ribbon cable and both power headers"
        )
        repair_steps.append(
            f"HB{idx-1} ({name}): If still 0 chips after reboot — replace this board"
        )
    for idx, name, pct, missing in degraded_boards:
        repair_steps.append(
            f"HB{idx-1} ({name}): {missing} dead chips ({pct}% health) — reseat board; "
            f"if chips don't recover replace board"
        )
    if ai_fix and ai_fix.strip() not in ("None required.", ""):
        skip_kw = ['power cycle', 'cycle the miner', 'check all hashboard data cables']
        raw = _re.split(r'\d+[).]?\s+', ai_fix)
        for s in raw:
            s = s.strip().rstrip('.')
            if s and len(s) > 8:
                if not any(kw in s.lower() for kw in skip_kw):
                    if s not in repair_steps:
                        repair_steps.append(s)
    if next_action and next_action.strip() and next_action.strip() not in repair_steps:
        repair_steps.append(next_action.strip())
    repair_steps.append("After repairs: re-scan miner on bench and verify hashrate before deploying")

    # estimate canvas height
    est_h = (
        50 + 80 + 20 +
        30 + len(ordered_hbs) * 42 + 40 +
        30 + len(hw_errors or []) * 24 + len(warnings or []) * 22 +
        30 + 44 +
        30 + len(repair_steps) * 26 +
        100
    )
    img  = Image.new("RGB", (W, est_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    y    = 0

    def section_bar(label, color):
        nonlocal y
        draw.rectangle([0, y, W, y + 27], fill=color)
        draw.text((M, y + 5), label, font=font_md, fill=(255, 255, 255))
        y += 31

    def hr(color=(180, 180, 180), width=1):
        nonlocal y
        draw.line([M, y, W - M, y], fill=color, width=width)
        y += 7

    # FAIL bar
    draw.rectangle([0, y, W, y + 42], fill=(180, 0, 0))
    draw.text((M, y + 7), "  FAIL  \u2014  NEEDS REPAIR", font=font_lg, fill=(255, 255, 255))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    tb = draw.textbbox((0, 0), ts, font=font_xs)
    draw.text((W - M - (tb[2] - tb[0]), y + 14), ts, font=font_xs, fill=(255, 180, 180))
    y += 46

    draw.text((M, y), model or "Unknown", font=font_xl, fill=(0, 0, 0))
    y += 33
    draw.text((M, y), f"S/N: {serial or chr(8212)}    MAC: {mac or chr(8212)}", font=font_sm, fill=(40, 40, 40))
    y += 21
    wk_line = f"Worker: {worker or chr(8212)}"
    if psu_voltage and psu_voltage > 0:
        wk_line += f"    PSU: {psu_voltage:.1f}V / {psu_current:.1f}A"
    draw.text((M, y), wk_line, font=font_xs, fill=(80, 80, 80))
    y += 20
    hr(color=(180, 0, 0), width=2)

    # SECTION 1 — hashboard breakdown
    section_bar("HASHBOARD BREAKDOWN    HB0=LEFT  |  HB1=CENTER  |  HB2=RIGHT", (50, 50, 50))

    for slot_idx, h in enumerate(ordered_hbs, start=1):
        hb_num = slot_idx - 1
        name   = {1: 'LEFT', 2: 'CENTER', 3: 'RIGHT'}[slot_idx]
        label  = f"HB{hb_num} ({name})"

        if h is None:
            draw.rectangle([M, y, W - M, y + 32], fill=(255, 215, 215), outline=(180, 0, 0), width=1)
            draw.text((M + 6, y + 8), f"  X  {label}: NOT DETECTED  \u2014  board absent or CBB cannot communicate", font=font_sm, fill=(160, 0, 0))
            y += 36
            continue

        chips    = _hb(h, 'chip_count', 0) or 0
        expected = _hb(h, 'expected_chips', 0) or 0
        missing  = _hb(h, 'missing_chips', 0) or 0
        temp_avg = _hb(h, 'temp_avg', 0.0) or 0.0
        temp_max = _hb(h, 'temp_max', 0.0) or 0.0
        health   = _hb(h, 'health_pct', 0.0) or 0.0
        active   = _hb(h, 'active', True)

        if not active or chips == 0:
            chip_str = f"{chips}/{expected} chips" if expected else "0 chips"
            temp_str = "N/A" if temp_avg == 0 else f"{temp_avg:.0f}C avg / {temp_max:.0f}C max"
            draw.rectangle([M, y, W - M, y + 36], fill=(255, 215, 215), outline=(180, 0, 0), width=1)
            draw.text((M + 5, y + 4),  f"  X  {label}:  DEAD    Chips: {chip_str}    Temp: {temp_str}", font=font_sm, fill=(160, 0, 0))
            draw.text((M + 5, y + 21), f"     Missing: all active chips gone  \u2014  board not contributing to hashrate", font=font_xx, fill=(160, 0, 0))
            y += 40
        elif missing > 0:
            pct = round(chips / max(expected, 1) * 100)
            temp_str = f"{temp_avg:.0f}C avg / {temp_max:.0f}C max"
            draw.rectangle([M, y, W - M, y + 36], fill=(255, 245, 200), outline=(160, 100, 0), width=1)
            draw.text((M + 5, y + 4),  f"  ! {label}:  {pct}% health    {chips}/{expected} chips    {missing} DEAD chips", font=font_sm, fill=(130, 70, 0))
            draw.text((M + 5, y + 21), f"     Temp: {temp_str}    Health: {health:.0f}%  \u2014  degraded, partial output", font=font_xx, fill=(130, 70, 0))
            y += 40
        else:
            pct = round(chips / max(expected, 1) * 100) if expected else 100
            temp_str = f"{temp_avg:.0f}C avg / {temp_max:.0f}C max"
            draw.text((M + 5, y + 3),  f"  OK  {label}:  {pct}%    {chips}/{expected} chips    OK", font=font_sm, fill=(0, 110, 40))
            draw.text((M + 5, y + 20), f"     Temp: {temp_str}    Health: {health:.0f}%", font=font_xx, fill=(40, 40, 40))
            y += 34

    y += 4
    if no_fan_data:
        draw.rectangle([M, y, W - M, y + 32], fill=(240, 240, 240), outline=(120, 120, 120), width=1)
        draw.text((M + 6, y + 4),  "  FANS: N/A  \u2014  miner did not report fan data at scan time", font=font_sm, fill=(80, 80, 80))
        draw.text((M + 6, y + 20), "  NOTE: fan diagnostic was skipped (offline or no response from miner)", font=font_xx, fill=(100, 100, 100))
        y += 36
    else:
        fan_parts = [f"Fan{i+1}: {rpm} RPM" for i, rpm in enumerate(fans)]
        fan_warn  = any(rpm < 1000 for rpm in fans)
        bg = (255, 230, 230) if fan_warn else (230, 255, 235)
        fg = (150, 0, 0) if fan_warn else (0, 90, 30)
        draw.rectangle([M, y, W - M, y + 24], fill=bg, outline=fg, width=1)
        draw.text((M + 6, y + 5), "  FANS: " + "  |  ".join(fan_parts), font=font_sm, fill=fg)
        if fan_warn:
            y += 22
            draw.text((M + 6, y + 2), "  WARNING: fan below 1000 RPM \u2014 check/replace fan assembly", font=font_xx, fill=fg)
            y += 18
        else:
            y += 28

    y += 6
    hr()

    # SECTION 2 — system diagnosis
    section_bar("SYSTEM DIAGNOSIS", (160, 25, 25))
    err_list = list(hw_errors or [])
    if not err_list and fault_summary:
        err_list = [s.strip() for s in fault_summary.split(";") if s.strip()]
    for e in err_list:
        for line in wrap("  \u2022  " + e, font_sm, CW - 10):
            draw.text((M + 8, y), line, font=font_sm, fill=(150, 0, 0))
            y += 22
    for w in (warnings or []):
        for line in wrap("  \u2022  " + w, font_sm, CW - 10):
            draw.text((M + 8, y), line, font=font_sm, fill=(140, 85, 0))
            y += 20
    if not err_list and not (warnings or []):
        draw.text((M + 8, y), "  \u2022  No hardware faults logged \u2014 see hashboard data above", font=font_sm, fill=(80, 80, 80))
        y += 22
    y += 6
    hr()

    # SECTION 3 — likely causes
    section_bar("LIKELY CAUSES", (155, 95, 0))
    cause = ai_issue or fault_summary or "Review hashboard breakdown above."
    for line in wrap(cause, font_sm, CW - 10):
        draw.text((M + 8, y), line, font=font_sm, fill=(110, 60, 0))
        y += 22
    y += 6
    hr()

    # SECTION 4 — repair steps
    section_bar("REPAIR STEPS", (0, 100, 50))
    for i, step in enumerate(repair_steps, 1):
        prefix = f"{i}. "
        first  = True
        for line in wrap(prefix + step, font_sm, CW - 10):
            draw.text((M + (0 if first else 20), y), line, font=font_sm, fill=(0, 75, 35))
            y += 23
            first = False
    y += 8

    # barcode
    sn_clean = (serial or "").strip()
    if sn_clean and sn_clean not in ("--- no S/N ---", "--- pending scan ---"):
        try:
            import barcode as _bc
            from barcode.writer import ImageWriter as _IW
            from io import BytesIO as _BIO
            Code128 = _bc.get_barcode_class("code128")
            bc = Code128(sn_clean, writer=_IW())
            buf = _BIO()
            bc.write(buf, options={
                "module_width": 0.8, "module_height": 10.0,
                "font_size": 7, "text_distance": 2.0,
                "quiet_zone": 2.0, "background": "white", "foreground": "black",
                "write_text": True,
            })
            buf.seek(0)
            bc_img = Image.open(buf).convert("RGB")
            bc_w = int((W - M * 2) * 0.85)
            bc_h = int(bc_img.height * bc_w / bc_img.width)
            bc_img = bc_img.resize((bc_w, bc_h), Image.LANCZOS)
            if y + bc_h + 20 > img.height:
                canvas = Image.new("RGB", (W, y + bc_h + 30), (255, 255, 255))
                canvas.paste(img, (0, 0))
                img  = canvas
                draw = ImageDraw.Draw(img)
            img.paste(bc_img, (M, y))
            y += bc_h + 8
        except Exception:
            pass

    final_h = min(y + 20, img.height)
    label_img = img.crop((0, 0, W, final_h))
    label_img = label_img.rotate(90, expand=True)

    from brother_ql.raster import BrotherQLRaster
    import brother_ql.conversion as _conv
    qlr = BrotherQLRaster("QL-810W")
    _conv.convert(
        qlr=qlr,
        images=[label_img],
        label="62",
        rotate="auto",
        threshold=70.0,
        dither=False,
        compress=False,
        red=False,
        dpi_600=False,
        hq=True,
        cut=True,
    )
    return bytes(qlr.data)


def generate_label_bytes(label: LabelData) -> bytes:
    """Generate label PNG bytes."""
    img = generate_label_image(label)
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(LABEL_DPI, LABEL_DPI))
    return buf.getvalue()


async def print_label(label: LabelData, printer_identifier: Optional[str] = None) -> dict:
    """
    Print label to Brother QL-810W.
    Auto-detects USB (file:// or /dev/) vs network (tcp://) backend.
    Never blocks longer than 8 seconds.
    """
    import asyncio
    identifier = printer_identifier or settings.PRINTER_IDENTIFIER
    try:
        from brother_ql.backends.helpers import send
        from brother_ql.conversion import convert
        from brother_ql.raster import BrotherQLRaster
        import PIL.Image
        if not hasattr(PIL.Image, "ANTIALIAS"):
            PIL.Image.ANTIALIAS = PIL.Image.LANCZOS
    except ImportError:
        logger.error("brother_ql not installed")
        return {"success": False, "message": "brother_ql library not installed"}

    # Auto-detect backend from identifier prefix
    if identifier.startswith("usb://") or identifier.startswith("file://") or identifier.startswith("/dev/bus/usb"):
        # Always use pyusb backend for USB — discover the clean identifier
        backend = "pyusb"
        try:
            from brother_ql.backends.helpers import discover as _disc
            found = _disc(backend_identifier="pyusb")
            if found:
                # Use the first found identifier — it's the right one
                identifier = found[0].get("identifier", identifier)
                logger.info("printer discovered", identifier=identifier)
        except Exception as e:
            logger.warning("printer discover failed, using raw identifier", error=str(e))
        # Strip any garbage suffix after underscore that brother_ql can't parse
        # e.g. usb://0x04f9:0x209c_Љ -> usb://0x04f9:0x209c
        if "_" in identifier:
            # Only strip if suffix after _ is not pure hex
            parts = identifier.split("_", 1)
            suffix = parts[1] if len(parts) > 1 else ""
            try:
                int(suffix, 16)  # valid hex suffix — keep it
            except ValueError:
                identifier = parts[0]  # garbage suffix — strip it
                logger.info("stripped garbage suffix from printer identifier", identifier=identifier)
    elif identifier.startswith("/dev/") and not identifier.startswith("/dev/bus"):
        backend = "linux_kernel"
    else:
        backend = "network"

    def _do_send():
        img = generate_label_image(label)
        qlr = BrotherQLRaster(settings.PRINTER_MODEL)
        qlr.exception_on_warning = False
        instructions = convert(
            qlr=qlr,
            images=[img],
            label=settings.LABEL_TYPE,
            rotate="0",
            threshold=70.0,
            dither=False,
            compress=False,
            red=False,
            dpi_600=False,
            hq=True,
            cut=True,
        )
        return send(
            instructions=instructions,
            printer_identifier=identifier,
            backend_identifier=backend,
            blocking=True,
        )

    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _do_send),
            timeout=8.0,
        )
        logger.info("Label sent to printer", printer=identifier, backend=backend, result=result)
        return {"success": True, "message": "Label sent", "result": str(result)}
    except asyncio.TimeoutError:
        logger.error("Label print timed out", printer=identifier)
        return {"success": False, "message": f"Printer timeout — is {identifier} connected and powered on?"}
    except Exception as e:
        logger.error("Label print failed", error=str(e), printer=identifier)
        return {"success": False, "message": str(e)}


async def print_raw_bytes(raw: bytes, printer_identifier: Optional[str] = None) -> dict:
    """Send pre-built raster instruction bytes directly to the printer."""
    import asyncio
    identifier = printer_identifier or settings.PRINTER_IDENTIFIER

    try:
        from brother_ql.backends.helpers import send, discover
        import PIL.Image
        if not hasattr(PIL.Image, "ANTIALIAS"):
            PIL.Image.ANTIALIAS = PIL.Image.LANCZOS
    except ImportError:
        return {"success": False, "message": "brother_ql library not installed"}

    if identifier.startswith("usb://") or identifier.startswith("/dev/bus/usb"):
        backend = "pyusb"
        try:
            found = discover(backend_identifier="pyusb")
            if found:
                identifier = found[0].get("identifier", identifier)
        except Exception:
            pass
        if "_" in identifier:
            parts = identifier.split("_", 1)
            try: int(parts[1], 16)
            except ValueError: identifier = parts[0]
    elif identifier.startswith("/dev/") and not identifier.startswith("/dev/bus"):
        backend = "linux_kernel"
    else:
        backend = "network"

    def _send():
        return send(
            instructions=raw,
            printer_identifier=identifier,
            backend_identifier=backend,
            blocking=True,
        )

    try:
        result = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _send),
            timeout=8.0,
        )
        did_print = bool(result and result.get("did_print"))
        return {
            "success":           did_print,
            "message":           "Label sent" if did_print else "Printer error",
            "result":            str(result),
            "did_print":         did_print,
            "ready_for_next_job": bool(result and result.get("ready_for_next_job")),
        }
    except asyncio.TimeoutError:
        return {"success": False, "message": "Printer timeout (8s)"}
    except Exception as e:
        logger.error("print_raw_bytes error", error=str(e))
        return {"success": False, "message": str(e)}

