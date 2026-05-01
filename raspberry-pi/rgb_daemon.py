#!/usr/bin/env python3
"""
CT TestBench RGB LED Daemon
Controls a WS2812B LED strip connected to Raspberry Pi GPIO.
Runs as a standalone HTTP server so Docker containers can control it.

Usage: python3 rgb_daemon.py [--port 8001] [--led-count 60] [--led-pin 18]
"""

import asyncio
import argparse
import json
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

try:
    from rpi_ws281x import PixelStrip, Color
    HAS_NEOPIXEL = True
except ImportError:
    print("WARNING: rpi_ws281x not available — running in simulation mode")
    HAS_NEOPIXEL = False
    # Stub classes
    class Color:
        def __init__(self, r, g, b): pass
    class PixelStrip:
        def __init__(self, *a, **kw): pass
        def begin(self): pass
        def numPixels(self): return 60
        def setPixelColor(self, i, c): pass
        def show(self): pass


# ── Global strip ──────────────────────────────────────────────────────────────

strip: PixelStrip = None
current_task: asyncio.Task = None
loop: asyncio.AbstractEventLoop = None

# ── Color helpers ─────────────────────────────────────────────────────────────

def hex_to_color(hex_str: str) -> Color:
    h = hex_str.lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return Color(r, g, b)


def wheel(pos: int) -> Color:
    pos = 255 - pos
    if pos < 85:
        return Color(255 - pos * 3, 0, pos * 3)
    elif pos < 170:
        pos -= 85
        return Color(0, pos * 3, 255 - pos * 3)
    else:
        pos -= 170
        return Color(pos * 3, 255 - pos * 3, 0)


# ── Patterns ──────────────────────────────────────────────────────────────────

def fill_solid(color: Color):
    if not strip:
        return
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, color)
    strip.show()


async def blink_loop(color: Color, speed_ms: int):
    on = True
    off_color = Color(0, 0, 0)
    while True:
        fill_solid(color if on else off_color)
        on = not on
        await asyncio.sleep(speed_ms / 1000)


async def rainbow_loop(speed_ms: int):
    j = 0
    while True:
        if not strip:
            break
        for i in range(strip.numPixels()):
            pos = (i * 256 // strip.numPixels() + j) & 255
            strip.setPixelColor(i, wheel(pos))
        strip.show()
        j = (j + 1) & 255
        await asyncio.sleep(speed_ms / 1000)


async def pulse_loop(color_hex: str, speed_ms: int):
    """Breathing/pulse effect."""
    import math
    h = color_hex.lstrip("#")
    r_max, g_max, b_max = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    step = 0
    while True:
        brightness = (math.sin(step * 0.05) + 1) / 2
        c = Color(int(r_max * brightness), int(g_max * brightness), int(b_max * brightness))
        fill_solid(c)
        step += 1
        await asyncio.sleep(speed_ms / 1000 / 60)


# ── Pattern controller ────────────────────────────────────────────────────────

async def set_pattern(pattern: str, color_hex: str = "#00FF00", speed_ms: int = 500):
    global current_task

    # Cancel existing pattern
    if current_task and not current_task.done():
        current_task.cancel()
        try:
            await current_task
        except asyncio.CancelledError:
            pass

    if pattern == "off":
        fill_solid(Color(0, 0, 0))
    elif pattern == "solid":
        fill_solid(hex_to_color(color_hex))
    elif pattern == "blink":
        current_task = asyncio.create_task(blink_loop(hex_to_color(color_hex), speed_ms))
    elif pattern == "rainbow":
        current_task = asyncio.create_task(rainbow_loop(speed_ms))
    elif pattern == "pulse":
        current_task = asyncio.create_task(pulse_loop(color_hex, speed_ms))
    else:
        return {"error": f"Unknown pattern: {pattern}"}

    return {"success": True, "pattern": pattern, "color": color_hex}


# ── HTTP Server ───────────────────────────────────────────────────────────────

class RGBHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/rgb/set":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                data = json.loads(body)
                pattern = data.get("pattern", "off")
                color = data.get("color", "#00FF00")
                speed = data.get("speed_ms", 500)

                future = asyncio.run_coroutine_threadsafe(
                    set_pattern(pattern, color, speed), loop
                )
                result = future.result(timeout=5)
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "Not found"})

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "service": "rgb-daemon"})
        else:
            self._respond(404, {"error": "Not found"})

    def _respond(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress default logging


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global strip, loop

    parser = argparse.ArgumentParser(description="CT TestBench RGB LED Daemon")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--led-count", type=int, default=60)
    parser.add_argument("--led-pin", type=int, default=18)
    args = parser.parse_args()

    # Initialise strip
    if HAS_NEOPIXEL:
        strip = PixelStrip(args.led_count, args.led_pin, 800000, 10, False, 255, 0)
        strip.begin()
        print(f"RGB strip initialised: {args.led_count} LEDs on GPIO {args.led_pin}")
    else:
        print("Running in simulation mode (no GPIO)")

    # Asyncio event loop in background thread
    loop = asyncio.new_event_loop()

    async def _run_loop():
        # Boot animation
        await set_pattern("rainbow", speed_ms=50)
        await asyncio.sleep(2)
        await set_pattern("solid", "#00FF00")
        # Then idle
        while True:
            await asyncio.sleep(3600)

    loop_thread = Thread(target=lambda: loop.run_until_complete(_run_loop()), daemon=True)
    loop_thread.start()

    # HTTP server
    server = HTTPServer(("0.0.0.0", args.port), RGBHandler)
    print(f"RGB daemon listening on port {args.port}")

    def shutdown(sig, frame):
        print("\nShutting down...")
        if HAS_NEOPIXEL and strip:
            fill_solid(Color(0, 0, 0))
        server.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
