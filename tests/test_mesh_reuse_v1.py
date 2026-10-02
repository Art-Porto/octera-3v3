#!/usr/bin/env python3
"""Offline contract, property and K2 A/B tests for Mesh Reuse V1."""

from __future__ import annotations

import copy
import csv
import importlib.util
import json
import math
import random
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "klippy/extras/octera_mesh_reuse.py"
SPEC = importlib.util.spec_from_file_location("octera_mesh_reuse", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def matrix(size: int, scale: float = 0.01) -> list[list[float]]:
    return [
        [round((x + y - size) * scale, 6) for x in range(size)]
        for y in range(size)
    ]


def base_state() -> tuple[dict, dict, dict, dict]:
    candidate = {
        "bounds": [90.0, 60.0, 210.0, 240.0],
        "probed_matrix": matrix(7),
        "algorithm": "lagrange",
        "profile_name": "adaptive",
    }
    sanity = MODULE.mesh_sanity(
        candidate, max_abs=1.0, max_range=0.8, max_neighbor_jump=0.5
    )
    metadata = {
        "bounds": list(candidate["bounds"]),
        "probe_count": sanity["count"],
        "spacing": sanity["spacing"],
        "bed_target": 55.0,
        "bed_temperature": 55.0,
        "session_id": "session-a",
        "thermal_valid": True,
        "config_fingerprint": "fingerprint-a",
        "origin": "adaptive",
    }
    request = {
        "required_bounds": [100.0, 70.0, 200.0, 230.0],
        "baseline_spacing": [33.75, 33.75],
        "bed_target": 55.0,
        "bed_temperature": 55.0,
        "session_id": "session-a",
        "config_fingerprint": "fingerprint-a",
        "printer_state": "printing",
        "homed_axes": "xyz",
        "manual_invalidated": False,
    }
    policy = {
        "bed_temp_tolerance": 3.0,
        "density_epsilon": 0.05,
        "sanity_max_abs": 1.0,
        "sanity_max_range": 0.8,
        "sanity_max_neighbor_jump": 0.5,
    }
    return candidate, metadata, request, policy


def set_mesh(candidate: dict, metadata: dict, size: int, bounds: list[float]) -> None:
    candidate["bounds"] = list(bounds)
    candidate["probed_matrix"] = matrix(size)
    metadata["bounds"] = list(bounds)
    metadata["probe_count"] = [size, size]
    metadata["spacing"] = [
        (bounds[2] - bounds[0]) / (size - 1),
        (bounds[3] - bounds[1]) / (size - 1),
    ]


def mutate(name: str, candidate: dict, metadata: dict | None,
           request: dict) -> dict | None:
    if name in {"exact", "exact_margin"}:
        request["required_bounds"] = list(candidate["bounds"])
    elif name in {"contained", "second_identical", "temp_same", "same_session",
                  "fingerprint_same", "density_good", "dispatch_contract"}:
        pass
    elif name == "outside_x_min":
        request["required_bounds"][0] = candidate["bounds"][0] - 0.051
    elif name == "outside_x_max":
        request["required_bounds"][2] = candidate["bounds"][2] + 0.051
    elif name == "outside_y_min":
        request["required_bounds"][1] = candidate["bounds"][1] - 0.051
    elif name == "outside_y_max":
        request["required_bounds"][3] = candidate["bounds"][3] + 0.051
    elif name == "large_mesh":
        set_mesh(candidate, metadata, 9, [15.0, 15.0, 285.0, 285.0])
    elif name == "density_bad":
        set_mesh(candidate, metadata, 3, [90.0, 60.0, 210.0, 240.0])
    elif name == "mesh_6":
        set_mesh(candidate, metadata, 6, [40.0, 40.0, 200.0, 200.0])
        request["required_bounds"] = [60.0, 60.0, 180.0, 180.0]
    elif name == "mesh_8":
        set_mesh(candidate, metadata, 8, [20.0, 20.0, 250.0, 250.0])
    elif name == "mesh_9":
        set_mesh(candidate, metadata, 9, [15.0, 15.0, 285.0, 285.0])
    elif name == "rect_bounds_square":
        set_mesh(candidate, metadata, 8, [95.0, 30.0, 165.0, 240.0])
        request["required_bounds"] = [105.0, 80.0, 150.0, 225.0]
    elif name == "temp_plus_2":
        request["bed_target"] = request["bed_temperature"] = 57.0
    elif name == "temp_minus_2":
        request["bed_target"] = request["bed_temperature"] = 53.0
    elif name == "temp_plus_4":
        request["bed_target"] = request["bed_temperature"] = 59.0
    elif name == "temp_petg":
        request["bed_target"] = request["bed_temperature"] = 80.0
    elif name == "thermal_invalid":
        metadata["thermal_valid"] = False
    elif name == "new_session":
        request["session_id"] = "session-b"
    elif name == "fingerprint_changed":
        request["config_fingerprint"] = "fingerprint-b"
    elif name == "empty_matrix":
        candidate["probed_matrix"] = [[]]
    elif name == "nonfinite":
        candidate["probed_matrix"][0][0] = math.inf
    elif name == "absurd_range":
        candidate["probed_matrix"][0][0] = -0.6
        candidate["probed_matrix"][-1][-1] = 0.6
    elif name == "absurd_jump":
        candidate["probed_matrix"][2][2] = 0.7
    elif name in {"no_metadata", "manual_no_metadata"}:
        return None
    elif name == "full_bed":
        set_mesh(candidate, metadata, 9, [15.0, 15.0, 285.0, 285.0])
        metadata["origin"] = "full-bed"
        request["required_bounds"] = [130.0, 130.0, 170.0, 170.0]
    elif name == "third_smaller":
        request["required_bounds"] = [120.0, 100.0, 170.0, 180.0]
    elif name == "third_outside":
        request["required_bounds"] = [89.0, 100.0, 170.0, 180.0]
    else:
        raise AssertionError(f"unknown mutation {name}")
    return metadata


def k2_coverage(candidate: dict, required: list[float], enabled: bool = True) -> bool:
    if not enabled or not candidate.get("probed_matrix"):
        return False
    bounds = candidate["bounds"]
    return (
        bounds[0] <= required[0] + MODULE.COVERAGE_EPSILON_MM
        and bounds[1] <= required[1] + MODULE.COVERAGE_EPSILON_MM
        and bounds[2] >= required[2] - MODULE.COVERAGE_EPSILON_MM
        and bounds[3] >= required[3] - MODULE.COVERAGE_EPSILON_MM
    )


def run() -> dict:
    fixture = json.loads(
        (REPO / "tests/fixtures/kamp_mesh_reuse_v1_cases.json").read_text(encoding="utf-8")
    )
    failures = []
    results = []
    adaptive_macro = (
        REPO / "config/kamp/Adaptive_Meshing.cfg"
    ).read_text(encoding="utf-8")
    for line in adaptive_macro.splitlines():
        if "RESPOND " in line and ";" in line:
            failures.append("semicolon in RESPOND message breaks Creality parser")
    geometry = MODULE.required_bounds(
        [{"polygon": [[120, 120], [180, 120], [180, 180], [120, 180]]}],
        [15, 15], [285, 285], 5.0, 1.0,
    )
    if geometry["kamp"] != [115.0, 115.0, 185.0, 185.0]:
        failures.append("KAMP geometry margin changed unexpectedly")
    if geometry["required"] != [114.0, 114.0, 186.0, 186.0]:
        failures.append("reuse calibration envelope omits coverage margin")
    rounded_candidate, rounded_metadata, rounded_request, rounded_policy = copy.deepcopy(base_state())
    set_mesh(rounded_candidate, rounded_metadata, 4, [114.0, 114.0, 185.99, 185.99])
    rounded_request["required_bounds"] = [114.0, 114.0, 186.0, 186.0]
    if MODULE.evaluate_reuse(
        rounded_candidate, rounded_metadata, rounded_request, rounded_policy
    )["reason"] != "REUSE_PASS":
        failures.append("PRTouch 0.01mm upper-bound rounding rejected identical reuse")
    for case in fixture["cases"]:
        candidate, metadata, request, policy = copy.deepcopy(base_state())
        metadata = mutate(case["mutation"], candidate, metadata, request)
        result = MODULE.evaluate_reuse(candidate, metadata, request, policy)
        actual = result["reason"]
        if case["mutation"] == "dispatch_contract":
            start = (REPO / "config/kamp/Start_Print.cfg").read_text(encoding="utf-8")
            plugin = MODULE_PATH.read_text(encoding="utf-8")
            contract = (
                "BED_MESH_CLEAR" not in start
                and 'decision["decision"] == "REUSE"' in plugin
                and '"BED_MESH_CLEAR"' in plugin
            )
            actual = "REUSE_PASS" if contract else "REJECT_UNKNOWN"
        passed = actual == case["expected"]
        results.append({**case, "actual": actual, "pass": passed})
        if not passed:
            failures.append(f"{case['id']}:{case['name']} expected={case['expected']} actual={actual}")

    # Property tests: deterministic random geometry and fail-safe gates.
    rng = random.Random(31032026)
    properties = {name: 0 for name in [
        "contained", "outside", "density", "temperature", "fingerprint",
        "sanity", "deterministic", "uncertainty", "square_only", "k2_ab",
    ]}
    for _ in range(250):
        candidate, metadata, request, policy = copy.deepcopy(base_state())
        xmin = rng.uniform(15.0, 120.0); ymin = rng.uniform(15.0, 120.0)
        xmax = rng.uniform(180.0, 285.0); ymax = rng.uniform(180.0, 285.0)
        # 9x9 across any clipped bed span is never sparser than the canonical
        # 33.75 mm full-bed reference, isolating coverage semantics for A/B.
        size = 9
        set_mesh(candidate, metadata, size, [xmin, ymin, xmax, ymax])
        request["required_bounds"] = [
            rng.uniform(xmin, (xmin + xmax) / 2),
            rng.uniform(ymin, (ymin + ymax) / 2),
            rng.uniform((xmin + xmax) / 2, xmax),
            rng.uniform((ymin + ymax) / 2, ymax),
        ]
        first = MODULE.evaluate_reuse(candidate, metadata, request, policy)
        second = MODULE.evaluate_reuse(candidate, metadata, request, policy)
        if first["reason"] == "REJECT_COVERAGE":
            failures.append("contained property rejected by coverage")
        properties["contained"] += 1
        if first != second:
            failures.append("decision is not deterministic")
        properties["deterministic"] += 1
        outside = copy.deepcopy(request); outside["required_bounds"][2] = xmax + 0.1
        if MODULE.evaluate_reuse(candidate, metadata, outside, policy)["reason"] != "REJECT_COVERAGE":
            failures.append("outside property produced reuse")
        properties["outside"] += 1
        drift = copy.deepcopy(request); drift["config_fingerprint"] = "drift"
        if MODULE.evaluate_reuse(candidate, metadata, drift, policy)["reason"] != "REJECT_CONFIG_DRIFT":
            failures.append("config drift property produced reuse")
        properties["fingerprint"] += 1
        hot = copy.deepcopy(request); hot["bed_target"] = hot["bed_temperature"] = 80.0
        if MODULE.evaluate_reuse(candidate, metadata, hot, policy)["reason"] != "REJECT_TEMP":
            failures.append("temperature property produced reuse")
        properties["temperature"] += 1
        plan = MODULE.square_probe_plan(
            request["required_bounds"], [15, 15], [285, 285], [9, 9], minimum=4
        )
        if plan["count"][0] != plan["count"][1]:
            failures.append("square-only plan generated rectangle")
        properties["square_only"] += 1
        k2 = k2_coverage(candidate, request["required_bounds"])
        if k2 != (first["reason"] == "REUSE_PASS"):
            # With all added gates passing, V1 must preserve K2 geometry semantics.
            failures.append("K2/V1 geometry equivalence drift")
        properties["k2_ab"] += 1

    candidate, metadata, request, policy = copy.deepcopy(base_state())
    set_mesh(candidate, metadata, 3, [15.0, 15.0, 285.0, 285.0])
    if MODULE.evaluate_reuse(candidate, metadata, request, policy)["reason"] != "REJECT_DENSITY":
        failures.append("worse density property produced reuse")
    properties["density"] += 1
    candidate, metadata, request, policy = copy.deepcopy(base_state())
    candidate["probed_matrix"][1][1] = 5.0
    if MODULE.evaluate_reuse(candidate, metadata, request, policy)["reason"] != "REJECT_SANITY":
        failures.append("sanity property produced reuse")
    properties["sanity"] += 1
    candidate, metadata, request, policy = copy.deepcopy(base_state())
    request.pop("homed_axes")
    if MODULE.evaluate_reuse(candidate, metadata, request, policy)["reason"] != "REJECT_PRINTER_STATE":
        failures.append("uncertainty did not fail safe")
    properties["uncertainty"] += 1

    return {
        "status": "PASS" if not failures else "FAIL",
        "fixture_cases": len(results),
        "fixture_passed": sum(item["pass"] for item in results),
        "property_checks": sum(properties.values()),
        "properties": properties,
        "false_reuse": sum(
            1 for item in results
            if item["actual"] == "REUSE_PASS" and item["expected"] != "REUSE_PASS"
        ),
        "false_calibrate": sum(
            1 for item in results
            if item["actual"] != "REUSE_PASS" and item["expected"] == "REUSE_PASS"
        ),
        "failures": failures[:50],
        "results": results,
    }


if __name__ == "__main__":
    outcome = run()
    if len(sys.argv) == 3 and sys.argv[1] == "--matrix":
        target = Path(sys.argv[2])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=("id", "name", "mutation", "expected", "actual", "pass"),
            )
            writer.writeheader()
            writer.writerows(outcome["results"])
    print(json.dumps(outcome, indent=2, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if outcome["status"] == "PASS" else 1)
