#!/usr/bin/env python3
"""The installed level comes from the anchor, not from the measured offset.

Real case of 2026-11-05 (tests/fixtures/reuse_square_20261105.json): the 3x3
check of the stored 6x6 measured an offset of -0.055 and the mesh was
installed 0.055 low; the fresh 5x5 of the same area, probed right after,
showed the true shift was 0 (the stored mesh reads the anchor, 0.040, at the
center of the area). The predicted shift must reproduce the fresh mesh.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "octera_mesh_verify_anchor", REPO / "klippy/extras/octera_mesh_verify.py")
V = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V)
CASE = json.loads((REPO / "tests/fixtures/reuse_square_20261105.json").read_text())


def against_fresh(installed, stored_bounds, fresh):
    xs, ys = V.grid_points(fresh["bounds"], len(fresh["points"]))
    diffs = [fresh["points"][j][i] - V.bilinear(installed, stored_bounds, x, y)
             for j, y in enumerate(ys) for i, x in enumerate(xs)]
    return max(abs(d) for d in diffs), sum(diffs) / len(diffs)


def run() -> dict:
    results = []

    def check(name, condition, detail=None):
        results.append({"name": name, "pass": bool(condition),
                        "detail": None if condition else detail})

    stored, check3, fresh = CASE["stored"], CASE["check3"], CASE["fresh5"]
    result = V.compare_verify(stored["points"], stored["bounds"], check3["points"],
                              check3["bounds"], 0.08)
    check("the logged check is reproduced: shape passes, offset -0.055",
          result["pass"] and abs(result["offset"] - check3["logged_offset"]) < 1e-5
          and abs(result["max_deviation"] - check3["logged_shape"]) < 1e-5, result)

    shift = V.predicted_shift(stored["points"], stored["bounds"], CASE["area"],
                              CASE["anchor"])
    check("predicted shift is ~0 (stored reads the anchor at the area center)",
          abs(shift) < 0.002, shift)
    worst, mean = against_fresh(V.shifted(stored["points"], shift),
                                stored["bounds"], fresh)
    check("predicted level against the fresh 5x5: worst <= 0.05, mean <= 0.02",
          worst <= 0.05 and abs(mean) <= 0.02, {"worst": worst, "mean": mean})
    worst_m, mean_m = against_fresh(V.shifted(stored["points"], result["offset"]),
                                    stored["bounds"], fresh)
    check("the measured offset was worse (the bug this replaces)",
          worst_m > 0.08 and mean_m > 0.04, {"worst": worst_m, "mean": mean_m})
    # The fresh mesh itself reads the anchor at its center.
    fb = fresh["bounds"]
    check("fresh mesh reads the anchor at its center",
          abs(V.bilinear(fresh["points"], fb, (fb[0] + fb[2]) / 2, (fb[1] + fb[3]) / 2)
              - CASE["anchor"]) < 0.0005)
    # Off the stored center the shift is the stored reading there.
    plane = [[0.002 * (i - 2) for i in range(5)] for _ in range(5)]
    bounds = [100.0, 100.0, 200.0, 200.0]
    shift = V.predicted_shift(plane, bounds, [100.0, 100.0, 150.0, 200.0], 0.04)
    check("off-center area: shift = anchor - stored at its center",
          abs(shift - (0.04 - V.bilinear(plane, bounds, 125.0, 150.0))) < 1e-12
          and abs(shift - 0.042) < 1e-9, shift)

    failures = [r for r in results if not r["pass"]]
    return {"pass": not failures, "checks": len(results), "failures": failures}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2, default=str))
    sys.exit(0 if outcome["pass"] else 1)
