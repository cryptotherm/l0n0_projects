"""
RGB LED strip controller for WS2812B via Raspberry Pi GPIO.
Falls back gracefully if rpi_ws281x is not available.
"""

from __future__ import annotations
import asyncio
import structlog
from config import settings

logger = structlog.get_logger()

try:
    from rpi_ws281x import PixelStrip, Color
    _HAS_NEOPIXEL = True
except ImportError:
    _HAS_NEOPIXEL = False
    logger.warning("rpi_ws281x not available — RGB LED control disabled")


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


class RGBController:
    def __init__(self):
        self._strip = None
        self._task: asyncio.Task | None = None

    def _init_strip(self):
        if not _HAS_NEOPIXEL or not settings.RGB_ENABLED:
            return False
        if self._strip is None:
            self._strip = PixelStrip(
                settings.RGB_LED_COUNT,
                settings.RGB_LED_PIN,
                800000,  # LED signal frequency
                10,      # DMA channel
                False,   # Invert signal
                255,     # Brightness
                0,       # Channel
            )
            self._strip.begin()
        return True

    async def set_pattern(self, pattern: str, color: str = "#00FF00", speed_ms: int = 500) -> dict:
        """
        Set RGB strip pattern.
        pattern: solid | blink | rainbow | off
        """
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if not self._init_strip():
            return {"success": False, "message": "RGB LED not available on this platform"}

        try:
            if pattern == "off":
                self._fill(0, 0, 0)
            elif pattern == "solid":
                r, g, b = _hex_to_rgb(color)
                self._fill(r, g, b)
            elif pattern == "blink":
                r, g, b = _hex_to_rgb(color)
                self._task = asyncio.create_task(self._blink_loop(r, g, b, speed_ms))
            elif pattern == "rainbow":
                self._task = asyncio.create_task(self._rainbow_loop(speed_ms))
            else:
                return {"success": False, "message": f"Unknown pattern: {pattern}"}

            return {"success": True, "pattern": pattern}
        except Exception as e:
            logger.error("RGB set_pattern error", error=str(e))
            return {"success": False, "message": str(e)}

    def _fill(self, r: int, g: int, b: int):
        if self._strip:
            for i in range(self._strip.numPixels()):
                self._strip.setPixelColor(i, Color(r, g, b))
            self._strip.show()

    async def _blink_loop(self, r: int, g: int, b: int, speed_ms: int):
        on = True
        while True:
            self._fill(r if on else 0, g if on else 0, b if on else 0)
            on = not on
            await asyncio.sleep(speed_ms / 1000)

    async def _rainbow_loop(self, speed_ms: int):
        if not self._strip:
            return
        j = 0
        while True:
            for i in range(self._strip.numPixels()):
                pos = (i * 256 // self._strip.numPixels() + j) & 255
                self._strip.setPixelColor(i, Color(*_wheel(pos)))
            self._strip.show()
            j = (j + 1) & 255
            await asyncio.sleep(speed_ms / 1000)


def _wheel(pos: int) -> tuple[int, int, int]:
    pos = 255 - pos
    if pos < 85:
        return (255 - pos * 3, 0, pos * 3)
    elif pos < 170:
        pos -= 85
        return (0, pos * 3, 255 - pos * 3)
    else:
        pos -= 170
        return (pos * 3, 255 - pos * 3, 0)


# Singleton
rgb_controller = RGBController()
