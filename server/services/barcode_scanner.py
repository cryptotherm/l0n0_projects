"""
Barcode scanner service — reads USB HID barcode scanner via evdev.

The QDTECH MPI700 presents as a keyboard (/dev/input/event5).
Scans arrive as a sequence of KEY_* events ending with KEY_ENTER.
We buffer keystrokes into a string and emit the complete barcode
via an asyncio.Queue consumed by the scan router.
"""
from __future__ import annotations
import asyncio
import threading
import structlog
from typing import Optional

logger = structlog.get_logger()

# Auto-detect scanner by name — never hardcode event number (shifts when devices plug in)
SCANNER_NAMES = ["MPI700", "MPI7002", "QDTECH", "Barcode", "Scanner", "STM32"]
DEVICE_PATH = "/dev/input/event6"  # fallback only

def _find_scanner_device():
    """Return the event path for the barcode scanner by matching device name."""
    try:
        import evdev
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                name = dev.name or ""
                if any(s.lower() in name.lower() for s in SCANNER_NAMES):
                    return path
            except Exception:
                continue
    except Exception:
        pass
    return DEVICE_PATH  # fallback

_scan_queue: asyncio.Queue = asyncio.Queue(maxsize=32)
_running = False
_task: Optional[asyncio.Task] = None
_loop: Optional[asyncio.AbstractEventLoop] = None

KEYMAP = {}

def _build_keymap():
    try:
        from evdev import ecodes as e
        return {
            e.KEY_0: '0', e.KEY_1: '1', e.KEY_2: '2', e.KEY_3: '3',
            e.KEY_4: '4', e.KEY_5: '5', e.KEY_6: '6', e.KEY_7: '7',
            e.KEY_8: '8', e.KEY_9: '9',
            e.KEY_A: 'A', e.KEY_B: 'B', e.KEY_C: 'C', e.KEY_D: 'D',
            e.KEY_E: 'E', e.KEY_F: 'F', e.KEY_G: 'G', e.KEY_H: 'H',
            e.KEY_I: 'I', e.KEY_J: 'J', e.KEY_K: 'K', e.KEY_L: 'L',
            e.KEY_M: 'M', e.KEY_N: 'N', e.KEY_O: 'O', e.KEY_P: 'P',
            e.KEY_Q: 'Q', e.KEY_R: 'R', e.KEY_S: 'S', e.KEY_T: 'T',
            e.KEY_U: 'U', e.KEY_V: 'V', e.KEY_W: 'W', e.KEY_X: 'X',
            e.KEY_Y: 'Y', e.KEY_Z: 'Z',
            e.KEY_MINUS: '-', e.KEY_DOT: '.', e.KEY_SLASH: '/',
            e.KEY_SPACE: ' ',
        }
    except Exception as ex:
        logger.warning("evdev keymap build failed", error=str(ex))
        return {}


def get_queue() -> asyncio.Queue:
    return _scan_queue

_exclusive = False

def set_exclusive(val: bool):
    """When True, _always_on_scanner yields barcodes to the exclusive consumer."""
    global _exclusive
    _exclusive = val

def is_exclusive() -> bool:
    return _exclusive


def _reader_thread():
    """Blocking thread that reads the scanner and puts barcodes into the queue."""
    global _loop
    try:
        import evdev
        from evdev import ecodes as e
    except ImportError:
        logger.error("evdev not installed")
        return

    keymap = _build_keymap()
    buf = ""
    shift = False
    dev = None

    while _running:
        try:
            detected = _find_scanner_device()
            dev = evdev.InputDevice(detected)
            # Do NOT grab exclusively — would block touchscreen on same device
            logger.info("Barcode scanner opened (non-exclusive)", device=detected, name=dev.name)

            for event in dev.read_loop():  # blocking — runs in thread
                if not _running:
                    break
                if event.type != e.EV_KEY:
                    continue
                kev = evdev.categorize(event)
                if event.value == 1:  # key down
                    if kev.keycode in ('KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT'):
                        shift = True
                        continue
                    if kev.keycode in ('KEY_ENTER', 'KEY_KPENTER'):
                        if buf:
                            barcode = buf.strip().upper()
                            logger.info("Barcode scanned", barcode=barcode)
                            if _loop and not _loop.is_closed():
                                asyncio.run_coroutine_threadsafe(
                                    _scan_queue.put(barcode), _loop
                                )
                            buf = ""
                        continue
                    if kev.keycode == 'KEY_BACKSPACE':
                        buf = buf[:-1]
                        continue
                    code = getattr(e, kev.keycode, None)
                    if code is not None and code in keymap:
                        ch = keymap[code]
                        if not shift:
                            ch = ch.lower()
                        buf += ch
                        logger.info("key", buf=buf)
                    else:
                        logger.info("unmapped", keycode=kev.keycode)
                elif event.value == 0:
                    if kev.keycode in ('KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT'):
                        shift = False

        except Exception as ex:
            logger.error("Scanner thread error", error=str(ex))
            import time
            time.sleep(2)
        finally:
            if dev:
                try: dev.close()
                except Exception: pass
                dev = None
            buf = ""
            shift = False


async def start():
    global _task, _running, _loop
    if _task and not _task.done():
        return
    _running = True
    _loop = asyncio.get_event_loop()
    logger.info("Barcode scanner service started", device=DEVICE_PATH)
    t = threading.Thread(target=_reader_thread, daemon=True, name="barcode-scanner")
    t.start()
    # Keep a dummy task so stop() has something to cancel
    _task = asyncio.create_task(asyncio.sleep(86400 * 365))


async def stop():
    global _running, _task
    _running = False
    if _task and not _task.done():
        _task.cancel()
        try: await _task
        except asyncio.CancelledError: pass
    _task = None
