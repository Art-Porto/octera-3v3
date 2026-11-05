#!/usr/bin/env python3
"""Offline tests for the verified-reuse helpers (interpolated check)."""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "octera_mesh_verify", REPO / "klippy/extras/octera_mesh_verify.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def probe(surface, bounds, count):
    xs, ys = MODULE.grid_points(bounds, count)
    return [[surface(x, y) for x in xs] for y in ys]


def run() -> dict:
    failures, checks = [], 0
    rng = random.Random(7)
    margin = MODULE.VERIFY_MARGIN_MM

    # Any grid, odd or even, gets a check over the part plus margin, clipped.
    found = {}
    for _ in range(3000):
        count = rng.choice([3, 4, 5, 6, 7, 8, 9])
        x0, y0 = rng.uniform(15, 120), rng.uniform(15, 120)
        x1, y1 = rng.uniform(x0 + 60, 285), rng.uniform(y0 + 60, 285)
        stored = [x0, y0, x1, y1]
        rx0, ry0 = rng.uniform(x0, x1 - 5), rng.uniform(y0, y1 - 5)
        required = [rx0, ry0, rng.uniform(rx0 + 1, x1), rng.uniform(ry0 + 1, y1)]
        grid = MODULE.select_verify_grid(stored, [count, count], required)
        checks += 1
        found.setdefault(count, [0, 0])[1] += 1
        if grid is None:
            failures.append({"no_grid": [stored, count, required]})
            continue
        found[count][0] += 1
        b = grid["bounds"]
        for axis in (0, 1):
            want_low = max(stored[axis], required[axis] - margin)
            want_high = min(stored[axis + 2], required[axis + 2] + margin)
            if (abs(b[axis] - want_low) > 1e-9 or abs(b[axis + 2] - want_high) > 1e-9
                    or b[axis] < stored[axis] or b[axis + 2] > stored[axis + 2]):
                failures.append({"bad_bounds": [b, stored, required]})

    # Outside, or too coarse to interpolate, is refused.
    for stored, count, required in (
        ([100, 100, 200, 200], 5, [90, 120, 150, 150]),     # low side outside
        ([100, 100, 200, 200], 5, [120, 120, 200.1, 150]),  # 0.1 past a 5x5 end
        ([100, 100, 200, 200], 2, [120, 120, 150, 150]),
    ):
        checks += 1
        if MODULE.select_verify_grid(stored, [count, count], required) is not None:
            failures.append({"should_refuse": [stored, count, required]})
    # bed_mesh's floored spacing: an 8x8 ending 0.06 short still covers.
    checks += 1
    if MODULE.select_verify_grid([41.4, 67.8, 258.54, 232.16], [8, 8],
                                 [41.3995, 67.7995, 258.6, 232.2]) is None:
        failures.append("rounded 8x8 refused")

    # Interpolation: a plane is reproduced exactly by bilinear on any grid.
    def plane(x, y):
        return 0.001 * (x - 150) - 0.0007 * (y - 150) + 0.02
    for count in (5, 6, 7, 8, 9):
        stored = [41.4, 67.8, 258.54, 232.16]
        matrix = probe(plane, stored, count)
        grid = MODULE.select_verify_grid(stored, [count, count], [100, 100, 180, 180])
        checked = probe(plane, grid["bounds"], 3)
        result = MODULE.compare_verify(matrix, stored, checked, grid["bounds"], 0.05)
        checks += 1
        if not result["pass"] or result["max_deviation"] > 1e-9:
            failures.append({"plane": count, "result": result})
        shifted = [[v + 0.06 for v in row] for row in checked]
        checks += 1
        if MODULE.compare_verify(matrix, stored, shifted, grid["bounds"], 0.05)["pass"]:
            failures.append({"offset_passed": count})

    # On nodes the weight is zero: the comparison is the node itself.
    matrix = [[rng.uniform(-0.2, 0.2) for _ in range(5)] for _ in range(5)]
    stored = [20.0, 20.0, 260.0, 260.0]
    nodes = [[matrix[j][i] for i in (0, 2, 4)] for j in (0, 2, 4)]
    result = MODULE.compare_verify(matrix, stored, nodes, stored, 0.0001)
    checks += 1
    if not result["pass"] or result["max_deviation"] > 1e-12:
        failures.append({"on_node": result})

    # A step between nodes: the check sees it where the stored mesh cannot.
    def step(x, y):
        return 0.0 if x < 150 else 0.12
    stored = [20.0, 20.0, 260.0, 260.0]
    matrix = probe(lambda x, y: 0.0, stored, 7)
    grid = MODULE.select_verify_grid(stored, [7, 7], [100, 100, 200, 200])
    result = MODULE.compare_verify(matrix, stored, probe(step, grid["bounds"], 3),
                                   grid["bounds"], 0.05)
    checks += 1
    if result["pass"] or result["reason"] != "VERIFY_DEVIATION":
        failures.append({"step_passed": result})

    checks += 2
    if MODULE.compare_verify(matrix, stored, nodes[:2], stored, 0.05)["reason"] \
            != "VERIFY_DIMENSION_MISMATCH":
        failures.append("2x3 accepted")
    bad = [row[:] for row in nodes]
    bad[1][1] = math.nan
    if MODULE.compare_verify(matrix, stored, bad, stored, 0.05)["reason"] != "VERIFY_NONFINITE":
        failures.append("NaN accepted")
    rate = {k: round(v[0] / v[1], 2) for k, v in sorted(found.items())}
    return {"pass": not failures, "checks": checks, "grid_rate": rate,
            "failures": failures[:10]}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
