"""Shared geometry for the live and captured virtual pointer."""

from __future__ import annotations

POINTER_WIDTH = 42.0
POINTER_HEIGHT = 50.0
POINTER_HOTSPOT = (5.0, 3.0)
POINTER_POINTS = (
    (5.0, 3.0),
    (5.0, 46.0),
    (18.0, 33.0),
    (37.0, 33.0),
)
POINTER_PRESS_SCALE = 0.96


def pointer_points(*, pressed: bool = False) -> tuple[tuple[float, float], ...]:
    """Return arrow points in top-left coordinates, scaled around its hotspot."""
    scale = POINTER_PRESS_SCALE if pressed else 1.0
    hot_x, hot_y = POINTER_HOTSPOT
    return tuple(
        (hot_x + (x - hot_x) * scale, hot_y + (y - hot_y) * scale)
        for x, y in POINTER_POINTS
    )
