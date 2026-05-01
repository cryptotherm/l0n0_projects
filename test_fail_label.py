#!/usr/bin/env python3
"""
Test script to generate and print a full-format FAIL diagnostic label.
Run on BenchPi to verify label output.
"""
import sys
sys.path.insert(0, './server')

import PIL.Image
if not hasattr(PIL.Image, "ANTIALIAS"):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

from services.label_printer import generate_fail_label_bytes, print_raw_bytes
import asyncio

async def test_print():
    """Generate test FAIL label with rich data and print to USB."""
    test_bytes = generate_fail_label_bytes(
        model="Antminer S19a Pro",
        serial="Y0IDAABAA1JH00CC",
        mac="A4:B3:3D:8B:FE:39",
        worker="minting3",
        fault_summary="Left hashboard failed — 22/88 chips dead, temperature critical",
        ai_issue="Left hashboard has failed chips and over temperature (85C avg / 92C max)",
        ai_fix="Reseat the LEFT hashboard connector (bottom of miner, 10-pin white plug), check thermal paste, verify power delivery",
        next_action="Reseat LEFT hashboard connector, check thermal paste, reboot miner, retest",
        hw_errors=[
            "Left hashboard: 22/88 chips dead (75% health)",
            "Temperature: 85C avg / 92C max (over 80C threshold)",
            "Hashrate: 0.0/37.38 TH/s (0% of expected)",
            "Active hashboards: 2/3"
        ],
        warnings=[
            "Left hashboard operating at reduced capacity",
            "Thermal warning — excessive temperature detected",
            "Fan 3 running but not reducing temps"
        ],
        hashboards=[
            {
                "index": 0, 
                "position": "LEFT",
                "chip_count": 66, 
                "expected_chips": 88, 
                "missing_chips": 22, 
                "health_pct": 75, 
                "temp_avg": 85, 
                "temp_max": 92, 
                "active": True
            },
            {
                "index": 1, 
                "position": "CENTER",
                "chip_count": 88, 
                "expected_chips": 88, 
                "missing_chips": 0, 
                "health_pct": 100, 
                "temp_avg": 62, 
                "temp_max": 68, 
                "active": True
            },
            {
                "index": 2, 
                "position": "RIGHT",
                "chip_count": 88, 
                "expected_chips": 88, 
                "missing_chips": 0, 
                "health_pct": 100, 
                "temp_avg": 61, 
                "temp_max": 67, 
                "active": True
            },
        ],
        fan_speeds=[3200, 3150, 0],
        psu_voltage=11.8,
        psu_current=22.5,
    )
    
    print(f"✅ Generated FAIL label: {len(test_bytes)} bytes")
    
    # Print to BenchPi printer via USB
    result = await print_raw_bytes(test_bytes, printer_identifier="usb://0x04f9:0x209c")
    print(f"📋 Print result: {result}")
    
    if result.get('success'):
        print("✅ LABEL PRINTED SUCCESSFULLY")
    else:
        print(f"❌ Print failed: {result.get('message')}")
    
    return result

if __name__ == "__main__":
    result = asyncio.run(test_print())
    sys.exit(0 if result.get('success') else 1)
