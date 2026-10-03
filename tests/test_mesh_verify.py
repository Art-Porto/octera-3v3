#!/usr/bin/env python3
"""Offline tests for the verified-reuse helpers."""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "octera_mesh_verify", REPO / "klippy/extras/octera_mesh_verify.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def run() -> dict:
    failures, checks = [], 0
    rng = random.Random(7)
    found = {5: [0, 0], 7: [0, 0], 9: [0, 0], 6: [0, 0], 8: [0, 0]}
    for _ in range(3000):
        count = rng.choice([5, 6, 7, 8, 9])
        x0, y0 = rng.uniform(15, 120), rng.uniform(15, 120)
        x1, y1 = rng.uniform(x0 + 60, 285), rng.uniform(y0 + 60, 285)
        stored = [x0, y0, x1, y1]
        rx0, ry0 = rng.uniform(x0, x1 - 5), rng.uniform(y0, y1 - 5)
        required = [rx0, ry0, rng.uniform(rx0 + 1, x1), rng.uniform(ry0 + 1, y1)]
        grid = MODULE.select_verify_grid(stored, [count, count], required)
        checks += 1
        found[count][1] += 1
        if grid is None:
            continue
        found[count][0] += 1
        b = grid["bounds"]
        covers = (b[0] <= required[0] + 0.05 and b[1] <= required[1] + 0.05
                  and b[2] >= required[2] - 0.05 and b[3] >= required[3] - 0.05)
        for idx in (grid["x_index"], grid["y_index"]):
            steps = {b - a for a, b in zip(idx, idx[1:])}
            if not (0 <= idx[0] and idx[-1] <= count - 1 and len(idx) == 3
                    and len(steps) == 1 and min(steps) > 0):
                failures.append({"bad_index": idx, "count": count})
        if not covers:
            failures.append({"not_covering": [b, required]})
        # Grid points must sit exactly on stored nodes.
        for axis, idx in ((0, grid["x_index"]), (1, grid["y_index"])):
            for k, i in enumerate((0, -1)):
                node = MODULE.node_coordinate(stored[axis], stored[axis + 2], count, idx[i])
                if abs(node - b[axis + 2 * k]) > 1e-9:
                    failures.append({"off_node": [node, b]})
    # Odd meshes can always host a 3x3 on their nodes (widen to an even span).
    for count in (5, 7, 9):
        if found[count][0] != found[count][1]:
            failures.append({"odd_mesh_without_grid": count, "found": found[count]})
    # Outside or too-coarse cases are refused.
    for stored, count, required in (
        ([100, 100, 200, 200], 5, [90, 120, 150, 150]),
        ([100, 100, 200, 200], 3, [120, 120, 150, 150]),
        ([100, 100, 200, 200], 4, [120, 120, 150, 150]),
    ):
        checks += 1
        if MODULE.select_verify_grid(stored, [count, count], required) is not None:
            failures.append({"should_refuse": [stored, count, required]})
    # Comparison: exact nodes pass, offset beyond tolerance fails.
    matrix = [[0.01 * (i + j) for i in range(7)] for j in range(7)]
    grid = MODULE.select_verify_grid([20, 20, 260, 260], [7, 7], [100, 100, 180, 180])
    probe = [[matrix[j][i] for i in grid["x_index"]] for j in grid["y_index"]]
    checks += 3
    if not MODULE.compare_verify(matrix, grid, probe, 0.05)["pass"]:
        failures.append("identical nodes failed")
    shifted = [[v + 0.06 for v in row] for row in probe]
    if MODULE.compare_verify(matrix, grid, shifted, 0.05)["pass"]:
        failures.append("0.06 mm offset passed")
    if MODULE.compare_verify(matrix, grid, probe[:2], 0.05)["reason"] != "VERIFY_DIMENSION_MISMATCH":
        failures.append("2x3 accepted")
    rate = {k: round(v[0] / v[1], 2) for k, v in found.items() if v[1]}
    return {"pass": not failures, "checks": checks, "grid_rate": rate,
            "failures": failures[:10]}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
