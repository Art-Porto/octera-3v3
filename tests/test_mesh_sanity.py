#!/usr/bin/env python3
"""Slope and zig-zag sanity on real 3V3 meshes (2026-11-04/05).

mesh1: the stored 8x8 of 2026-11-04 whose first row zig-zags (rejected by
the 3x3 check on 2026-11-05). mesh2: the 6x6 probed in its place. full9: the
full-bed 9x9 (default profile). corner3: a 3x3 at 55 mm spacing in a corner,
the steepest good mesh by raw jump.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "octera_mesh_reuse_sanity", REPO / "klippy/extras/octera_mesh_reuse.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MESHES = json.loads((REPO / "tests/fixtures/sanity_meshes_20261105.json").read_text())

LIMITS = {"max_abs": 1.0, "max_range": 0.8, "max_neighbor_jump": 0.5,
          "max_slope": 0.90, "max_zigzag": 2}


def sanity(points, bounds, **extra):
    limits = dict(LIMITS, **extra)
    return MODULE.mesh_sanity({"bounds": bounds, "probed_matrix": points,
                               "algorithm": "bicubic"}, **limits)


def run() -> dict:
    results = []

    def check(name, condition, detail=None):
        results.append({"name": name, "pass": bool(condition),
                        "detail": None if condition else detail})

    mesh1 = MESHES["mesh1"]
    result = sanity(mesh1["points"], mesh1["bounds"])
    check("mesh #1 rejected by slope and zig-zag, not by the old limits",
          not result["pass"]
          and set(result["failure_codes"]) == {"SANITY_SLOPE_EXCEEDED", "SANITY_ZIGZAG"}
          and abs(result["max_slope"] - 1.532) < 0.002 and result["zigzag"] == 4,
          result)
    old = sanity(mesh1["points"], mesh1["bounds"], max_slope=None, max_zigzag=None)
    check("without the new criteria mesh #1 passed (as on 2026-11-04)",
          old["pass"], old)
    for name in ("mesh2", "full9", "corner3"):
        mesh = MESHES[name]
        result = sanity(mesh["points"], mesh["bounds"])
        check("%s passes" % name,
              result["pass"] and result["max_slope"] < 0.6 and result["zigzag"] <= 1,
              result)

    # Synthetic rows on a flat 9x9, 30 mm spacing: k sign changes of 0.1 mm
    # steps, then flat.
    bounds = [20.0, 20.0, 260.0, 260.0]
    for changes, should_pass in ((2, True), (3, False)):
        row = [0.1 * (k % 2) for k in range(changes + 2)]
        row += [row[-1]] * (9 - len(row))
        points = [list(row)] + [[0.0] * 9 for _ in range(8)]
        result = sanity(points, bounds)
        check("zig-zag of %d changes %s" % (changes, "passes" if should_pass else "rejected"),
              result["zigzag"] == changes and result["pass"] == should_pass
              and (should_pass or result["failure_codes"] == ["SANITY_ZIGZAG"]),
              {"row": row, "result": result})
    # Steps under 0.05 mm are noise, not a zig-zag.
    noisy = [[0.04 * ((i + j) % 2) for i in range(9)] for j in range(9)]
    result = sanity(noisy, bounds)
    check("0.04 mm alternation is noise", result["pass"] and result["zigzag"] == 0, result)
    # Slope is per 100 mm: the same 0.3 mm jump passes at 55 mm, fails at 30.
    for spacing, should_pass in ((55.0, True), (30.0, False)):
        b = [20.0, 20.0, 20.0 + 2 * spacing, 20.0 + 2 * spacing]
        points = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.3]]
        result = sanity(points, b)
        check("0.3 mm jump at %.0f mm spacing %s" % (spacing, "passes" if should_pass else "rejected"),
              result["pass"] == should_pass, result)
    check("zigzag_run counts only consecutive changes",
          MODULE.zigzag_run([0, 0.1, 0, 0.1, 0.1, 0.1, 0, 0.1]) == 2)

    failures = [r for r in results if not r["pass"]]
    return {"pass": not failures, "checks": len(results), "failures": failures}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2, default=str))
    sys.exit(0 if outcome["pass"] else 1)
