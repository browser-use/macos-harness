"""The drawn pointer: its shape and its motion.

Numbers and pure functions: no clock, no state, no drawing.
Units are points. The origin is the click point, x right, y down.
Angles are degrees, clockwise positive.
A pose is (x, y, a, sx, sy, b): draw as
translate(x, y) rotate(a) scale(sx, sy) rotate(b) about PIVOT.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]  # left, top, right, bottom
Curve = tuple[Point, Point, Point, Point]  # a cubic: start, two handles, end

# --- look -----------------------------------------------------------------
# What is seen = paint x cover.
# Paint: a flat black tint, the white arrow fill, the white rim.
# Cover: 1 inside the arrow outline, the fog row around it.
TIP, WING, NOTCH, TAIL = (-4.95, -5.0), (11.55, 0.7), (3.95, 4.35), (0.5, 12.3)
CORNERS = ((TIP, 1.6), (WING, 1.6), (NOTCH, 0.0), (TAIL, 1.6))  # corner, its radius
FILL, RIM, TINT = 0.16, 0.8, 0.37  # alpha of the white fill, white rim, black tint
RIM_WIDTH = 1.5  # centred on the edge
FOG_RADIUS = 36.0
# Cover alpha every 4 points out from the click point, as (distance, alpha) rows.
FOG_ALPHAS = (0.786, 0.751, 0.658, 0.526, 0.371, 0.228, 0.125, 0.057, 0.022, 0.0)
FOG = tuple(zip(range(0, 37, 4), FOG_ALPHAS, strict=True))
PIVOT = (1.4, 2.0)  # centre of turn and stretch
PRESS_ANCHOR = (-2.7, -2.7)  # centre of the press
# Press pulse: the scale by seconds since the press, as (seconds, scale) rows.
PRESS_TIMES = (0.0, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.25, 0.3)
PRESS_SCALES = (1.0, 0.971, 0.913, 0.881, 0.887, 0.933, 0.972, 0.992, 1.0)
PRESS = tuple(zip(PRESS_TIMES, PRESS_SCALES, strict=True))

# --- motion ---------------------------------------------------------------
HEADING = math.radians(-44.0)  # rest heading of the arrow, from straight up
U = (math.sin(HEADING), -math.cos(HEADING))  # the arrow points along U
U_ANGLE = math.degrees(math.atan2(U[1], U[0]))
REST = (0.0, 1.0, 1.0, 0.0)  # (a, sx, sy, b) of a pointer that is not moving
STILL = 0.5  # closer than this: no move
HOP_MAX = 196.0  # up to this far: a straight hop; beyond: a glide along a curve
HOP = (0.24, 0.84)  # spring (response seconds, damping fraction) of a hop
GLIDE_DAMPING, SCALER, RESPONSE_MIN, RESPONSE_MAX = 0.9, 0.7, 0.12, 2.2
START_HANDLE, END_HANDLE = 0.41960295031576633, 0.15
ARC_SIZE, ARC_FLOW = 0.2765523188064277, 0.5783555327868779
ARC_SCALES, FLOW_SCALES = (0.55, 0.8, 1.05), (0.65, 1.0, 1.35)
MARGIN, SAMPLES = 20.0, 24
STRETCH, SQUASH, TILT_MAX, THIN_MAX, THIN_SPEED = 0.38, 0.18, 76.0, 1.5, 3000.0
ARRIVED = (0.995, 3.157)  # spring progress and distance that count as arrived
TAIL_TIME = 1.4  # animation length in responses; the spring is within 0.1% by then
FRAME = 1 / 60  # the window over which speed is measured


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def add(p: Point, v: Point, k: float = 1.0) -> Point:
    return (p[0] + v[0] * k, p[1] + v[1] * k)


def toward(p: Point, q: Point) -> Point:
    """The unit vector from p to q."""
    d = math.dist(p, q)
    return ((q[0] - p[0]) / d, (q[1] - p[1]) / d)


def interpolate(rows: tuple[tuple[float, float], ...], key: float) -> float:
    """Linear between the rows of a (key, value) table; the end values outside it."""
    for (k0, v0), (k1, v1) in itertools.pairwise(rows):
        if key < k1:
            return v0 + (v1 - v0) * clamp((key - k0) / (k1 - k0))
    return rows[-1][1]


def press_scale(t: float) -> float:
    return interpolate(PRESS, t)


def outline_points(chords: int = 6) -> list[Point]:
    """The arrow outline as a polygon. A corner with a radius is cut by that circle,
    drawn as `chords` chords."""
    points: list[Point] = []
    for i, (corner, radius) in enumerate(CORNERS):
        a = toward(corner, CORNERS[i - 1][0])
        b = toward(corner, CORNERS[(i + 1) % len(CORNERS)][0])
        half = math.acos(a[0] * b[0] + a[1] * b[1]) / 2  # half the corner's angle
        inward = toward((0, 0), add(a, b))
        centre = add(corner, inward, radius / math.sin(half))
        # From the centre toward where the circle touches each of the two edges.
        spokes = [
            (e[0] * math.cos(half) - inward[0], e[1] * math.cos(half) - inward[1])
            for e in (a, b)
        ]
        first, last = (math.atan2(y, x) for x, y in spokes)
        sweep = (last - first + math.pi) % (2 * math.pi) - math.pi
        for k in range(chords + 1 if radius else 1):
            turn = first + sweep * k / chords
            points.append(add(centre, (math.cos(turn), math.sin(turn)), radius))
    return points


def spring(t: float, response: float, damping: float) -> float:
    """Step response of a damped spring released at rest: 0 at t = 0, settles at 1."""
    w = 2 * math.pi / response
    wd = w * math.sqrt(1 - damping * damping)
    swing = math.cos(wd * t) + damping * w / wd * math.sin(wd * t)
    return 1 - math.exp(-damping * w * t) * swing


def cubic(curve: Curve, t: float) -> Point:
    p0, c1, c2, p3 = curve
    a = 1 - t
    return tuple(  # type: ignore[return-value]
        p0[i] * a**3 + 3 * c1[i] * a * a * t + 3 * c2[i] * a * t * t + p3[i] * t**3
        for i in (0, 1)
    )


def tangent(curve: Curve, t: float) -> Point:
    p0, c1, c2, p3 = curve
    a = 1 - t
    return tuple(  # type: ignore[return-value]
        3 * a * a * (c1[i] - p0[i])
        + 6 * a * t * (c2[i] - c1[i])
        + 3 * t * t * (p3[i] - c2[i])
        for i in (0, 1)
    )


def reach(bounds: Bounds, p: Point, v: Point, length: float) -> Point:
    """The point `length` from p along v, pulled back so it stays inside the bounds."""
    for i in (0, 1):
        if v[i]:
            length = min(length, (bounds[i if v[i] < 0 else i + 2] - p[i]) / v[i])
    return add(p, v, max(0.0, length))


def candidates(start: Point, end: Point, bounds: Bounds) -> list[list[Curve]]:
    """20 paths, each one or two cubics. All leave start along U and reach end
    along U; no handle leaves the bounds."""
    d, along = math.dist(start, end), toward(start, end)
    hs = max(48, min(640, d * START_HANDLE, d * 0.9))
    he = max(48, min(640, d * END_HANDLE, d * 0.9))
    back = (-U[0], -U[1])

    def handles(k: float) -> tuple[Point, Point]:
        return reach(bounds, start, U, hs * k), reach(bounds, end, back, he * k)

    # One curve: with the full handles, then with shorter ones.
    paths = [[(start, *handles(k), end)] for k in (1, 0.65)]
    # Two curves through a point bowed to one side; they keep the full handles.
    leave, land = handles(1)
    normal = (-along[1], along[0])
    middle = add(((start[0] + end[0]) / 2, (start[1] + end[1]) / 2), U, hs * 0.16)
    bow, flow = clamp(d * ARC_SIZE, 50, 520), clamp(d * ARC_FLOW, 38, 440)
    for s in ARC_SCALES:
        for f in FLOW_SCALES:
            for side in (1, -1):
                arc = add(middle, normal, side * bow * s)
                into, out = add(arc, along, -flow * f), add(arc, along, flow * f)
                paths.append([(start, leave, into, arc), (arc, out, land, end)])
    return paths


def measure(
    path: list[Curve], bounds: Bounds
) -> tuple[float, float, float, float, bool]:
    """Length, sum of turn squared, sharpest turn, sum of turns, and whether the
    path stays MARGIN inside the bounds."""
    length = energy = sharpest = turn = 0.0
    inside, prev, last = True, path[0][0], None
    for curve in path:
        for k in range(SAMPLES + 1):  # k = 0 is the start point: it counts too
            q = cubic(curve, k / SAMPLES)
            step = math.dist(prev, q)
            length += step
            inside = inside and all(
                bounds[i] + MARGIN <= q[i] <= bounds[i + 2] - MARGIN for i in (0, 1)
            )
            if step > 0.01:
                angle = math.atan2(q[1] - prev[1], q[0] - prev[0])
                if last is not None:
                    change = abs((angle - last + math.pi) % (2 * math.pi) - math.pi)
                    energy += change * change
                    sharpest = max(sharpest, change)
                    turn += change
                last = angle
            prev = q
    return length, energy, sharpest, turn, inside


def glide(start: Point, end: Point, bounds: Bounds) -> tuple[list[Curve], float]:
    """The best of the candidate paths, and its spring response."""
    d = math.dist(start, end)
    along_u = ((end[0] - start[0]) * U[0] + (end[1] - start[1]) * U[1]) / d
    against = clamp((-0.08 - along_u) / 0.92)  # how far the target is behind the arrow
    best: tuple[Any, ...] = ((True, math.inf), [], 0.0)
    for path in candidates(start, end, bounds):
        length, energy, sharpest, turn, inside = measure(path, bounds)
        excess, arc = max(length / d - 1, 0), len(path) == 2
        bends = 140 * energy + 180 * sharpest + 18 * turn
        score = length + 320 * excess + bends + 90 * against + 45 * arc
        shape = clamp(
            0.42 * clamp(excess / 0.55)
            + 0.38 * clamp(turn / (1.4 * math.pi))
            + 0.2 * clamp(energy / 1.25)
        )
        far = clamp((length - 180) / 760)
        total = 0.42 + 0.22 * far + 0.12 * shape + 0.28 * against + 0.04 * arc
        response = SCALER * total * (0.9 if arc else 1)
        # A path that stays inside the bounds beats any that does not.
        if (not inside, score) < best[0]:
            best = ((not inside, score), path, response)
    return best[1], clamp(best[2], RESPONSE_MIN, RESPONSE_MAX)


def plan(start: Point, end: Point, bounds: Bounds) -> dict[str, Any]:
    """Everything a move needs, decided once: mode, path, spring, duration, and
    the time it arrives."""
    move: dict[str, Any] = {"mode": "still", "start": end, "end": end}
    move.update(duration=0.0, arrive=0.0)
    d = math.dist(start, end)
    if d < STILL:
        return move
    move.update(mode="hop", start=start, response=HOP[0], damping=HOP[1])
    if d > HOP_MAX:
        path, response = glide(start, end, bounds)
        move.update(mode="glide", path=path, response=response, damping=GLIDE_DAMPING)
    move["duration"] = TAIL_TIME * move["response"]
    t = 0.0
    while t < move["duration"] and not (
        spring(t, move["response"], move["damping"]) >= ARRIVED[0]
        and math.dist(position(move, t)[0], end) <= ARRIVED[1]
    ):
        t += 0.001
    move["arrive"] = t
    return move


def position(move: dict[str, Any], t: float) -> tuple[Point, Point]:
    """Where the click point is at time t, and the direction of the path there
    (glides only; else no direction)."""
    if t >= move["duration"]:
        return move["end"], (0.0, 0.0)
    p = spring(t, move["response"], move["damping"])
    if move["mode"] == "hop":
        return add(move["start"], add(move["end"], move["start"], -1), p), (0.0, 0.0)
    q = clamp(p) * len(move["path"])
    i = min(int(q), len(move["path"]) - 1)
    return cubic(move["path"][i], q - i), tangent(move["path"][i], q - i)


def pose(move: dict[str, Any], t: float) -> tuple[float, ...]:
    """(x, y, a, sx, sy, b) at time t. A hop stretches along its travel and tilts.
    A glide turns into its path and thins at speed. At rest: (x, y, *REST)."""
    (x, y), heading = position(move, t)
    if t >= move["duration"]:
        return (x, y, *REST)
    if move["mode"] == "hop":
        dx, dy = toward(move["start"], move["end"])
        p = clamp(spring(t, move["response"], move["damping"]))
        e = min(1, p / 0.22) * (1 - p) ** 0.62  # rises fast, falls to 0 on arrival
        axis = math.degrees(math.atan2(dy, dx))
        axis = axis - 180 if axis > 90 else axis + 180 if axis < -90 else axis
        tilt = clamp(0.75 * dx - 0.62 * dy, -1, 1) * TILT_MAX * e
        return (x, y, axis, 1 + STRETCH * e, 1 - SQUASH * e, tilt - axis)
    turn = 0.0  # a handle pulled back to nothing has no direction: the rest heading
    if math.hypot(*heading) >= 0.001:
        angle = math.degrees(math.atan2(heading[1], heading[0]))
        turn = (angle - U_ANGLE + 180) % 360 - 180
    t0, t1 = max(t - FRAME / 2, 0), t + FRAME / 2
    speed = math.dist(position(move, t0)[0], position(move, t1)[0]) / (t1 - t0)
    thin = 1 / (1 + THIN_MAX * clamp(speed / THIN_SPEED))
    return (x, y, turn + U_ANGLE, 1.0, thin, -U_ANGLE)
