"""
RGB LED strip router.
"""

from fastapi import APIRouter
from models.schemas import RGBRequest
from services.rgb_controller import rgb_controller

router = APIRouter()


@router.post("/set")
async def set_rgb(req: RGBRequest):
    """Set RGB LED strip pattern."""
    result = await rgb_controller.set_pattern(
        pattern=req.pattern,
        color=req.color or "#00FF00",
        speed_ms=req.speed_ms,
    )
    return result


@router.post("/off")
async def turn_off():
    """Turn off RGB LED strip."""
    return await rgb_controller.set_pattern("off")
