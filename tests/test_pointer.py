from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from macos_harness.pointer import (
    FRAME,
    NOTCH,
    REST,
    outline_points,
    plan,
    pose,
    press_scale,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/pointer/pointer-motion.json").read_text()
)
BOUNDS = (0, 0, 1440, 900)


def test_outline_is_a_small_arrow_with_three_round_corners_and_a_sharp_notch() -> None:
    outline = outline_points()
    xs, ys = [x for x, _ in outline], [y for _, y in outline]

    size = (max(xs) - min(xs), max(ys) - min(ys))
    assert outline[0] == pytest.approx((-4.00, -1.97), abs=0.01)  # the tip arc starts
    assert NOTCH in outline
    assert size == pytest.approx((13.0, 13.4), abs=0.1)
    assert min(xs) < 0 < max(xs)  # the click point is inside the arrow
    assert min(ys) < 0 < max(ys)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_motion_matches_the_shared_fixture(case: dict) -> None:
    tolerance = FIXTURE["tolerance"]
    move = plan(tuple(case["start"]), tuple(case["end"]), tuple(case["bounds"]))

    assert (move["mode"], len(move.get("path", []))) == (case["mode"], case["parts"])
    assert move["duration"] == pytest.approx(case["duration"], abs=tolerance["time"])
    assert move["arrive"] == pytest.approx(case["arrive"], abs=tolerance["time"])
    steps = max(1, math.ceil(move["duration"] / FRAME))
    assert len(case["frames"]) == steps + 1
    for i, want in enumerate(case["frames"]):
        x, y, a, sx, sy, b = pose(move, move["duration"] * i / steps)
        assert (x, y) == pytest.approx(want[:2], abs=tolerance["position"])
        assert (sx, sy) == pytest.approx(want[3:5], abs=tolerance["scale"])
        # The same angle, whether or not it is whole turns apart.
        for got, wanted in ((a, want[2]), (b, want[5])):
            assert abs((got - wanted + 180) % 360 - 180) <= tolerance["angle"]


@pytest.mark.parametrize(
    "name",
    [
        "along the top edge",
        "out of the top left corner",
        "into the bottom right corner",
    ],
)
def test_a_move_at_an_edge_stays_inside_the_bounds_and_lands_at_rest(name: str) -> None:
    case = next(case for case in FIXTURE["cases"] if case["name"] == name)
    left, top, right, bottom = case["bounds"]
    move = plan(tuple(case["start"]), tuple(case["end"]), tuple(case["bounds"]))

    for ms in range(0, round(move["duration"] * 1000) + 1, 4):
        x, y, *_ = pose(move, ms / 1000)
        assert left - 2 <= x <= right + 2 and top - 2 <= y <= bottom + 2
    assert pose(move, move["duration"]) == (*case["end"], *REST)


def test_distance_picks_no_move_a_hop_or_a_glide() -> None:
    assert plan((100, 100), (100.3, 100), BOUNDS)["mode"] == "still"
    hop = plan((100, 100), (296, 100), BOUNDS)
    assert (hop["mode"], round(hop["arrive"], 3)) == ("hop", 0.173)
    assert plan((100, 100), (297, 100), BOUNDS)["mode"] == "glide"


def test_press_pulse_dips_once_and_returns() -> None:
    assert [press_scale(t) for t in (-1, 0, 0.3, 9)] == [1.0] * 4
    lowest = min(range(300), key=lambda ms: press_scale(ms / 1000))
    assert (lowest, press_scale(lowest / 1000)) == (75, 0.881)
