#!/usr/bin/env python3
"""Verified reuse (V2): store, plan, verification probe and outcome."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "octera_mesh_reuse_v2", REPO / "klippy/extras/octera_mesh_reuse.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

FINGERPRINT = "f" * 64
STORED = [[0.01 * (i + j) for i in range(7)] for j in range(7)]
STORE_BOUNDS = [94.0, 94.0, 206.0, 206.0]  # 7x7, ~18.7 mm spacing


class Error(Exception):
    pass


class Gcmd:
    def __init__(self):
        self.messages = []

    def get_float(self, name, **kw):
        return 55.0

    def get(self, name, default=None):
        return "adaptive"

    def respond_info(self, message):
        self.messages.append(message)

    def error(self, message):
        return Error(message)


class BedMesh:
    def __init__(self):
        self.active = ""
        self.pmgr = type("PM", (), {})()
        self.pmgr.profiles = {}
        self.probed = [[]]
        self.bounds = [0.0, 0.0, 0.0, 0.0]

    def get_status(self, eventtime):
        profiles = {name: {"points": p["points"], "mesh_params": p["mesh_params"]}
                    for name, p in self.pmgr.profiles.items()}
        return {"profile_name": self.active, "profiles": profiles,
                "probed_matrix": self.probed,
                "mesh_min": self.bounds[:2], "mesh_max": self.bounds[2:]}


def make_owner(store_path, verify_probe=None):
    o = MODULE.OcteraMeshReuse.__new__(MODULE.OcteraMeshReuse)
    bed = BedMesh()
    o.reactor = type("R", (), {"monotonic": lambda self: time.monotonic()})()
    o.enabled, o.verbose, o.metadata = True, False, None
    o.manual_invalidated, o.validation_sequence = False, 0
    o.session_id, o.pending_validation, o.last_rejection = "s", None, None
    o.validation_state, o.last_invalidation_reason = "NO_VALID_MESH", "x"
    o.mesh_time_base, o.mesh_time_per_point = 55.0, 4.5
    o.bed_temp_tolerance, o.density_epsilon = 3.0, 0.05
    o.sanity_max_abs, o.sanity_max_range, o.sanity_max_neighbor_jump = 1.0, 0.8, 0.5
    o.verify_enabled, o.verify_tolerance = True, 0.05
    o.verify_min_full_count, o.verify_max_age_hours = 5, 168.0
    o.store_path, o.pending_verify, o.last_verify = store_path, None, None
    o.scripts = []

    def run(script):
        o.scripts.append(script)
        for line in script.splitlines():
            if line.startswith("BED_MESH_PROFILE LOAD="):
                name = line.split("=", 1)[1]
                if name not in bed.pmgr.profiles:  # calibration path, not simulated
                    continue
                bed.active = name
                bed.probed = bed.pmgr.profiles[name]["points"]
                bed.bounds = list(STORE_BOUNDS)
            elif line.startswith("_BED_MESH_CALIBRATE") and verify_probe is not None:
                bed.active, bed.probed = "octera_verify", verify_probe
            elif line == "OCTERA_MESH_VERIFY_FINISH":
                o.cmd_OCTERA_MESH_VERIFY_FINISH(Gcmd())
    o.gcode = type("G", (), {"run_script_from_command": staticmethod(run)})()
    o.printer = type("P", (), {"lookup_object": staticmethod(
        lambda name, default=None: bed if name == "bed_mesh"
        else type("H", (), {"get_status": staticmethod(lambda e: {"temperature": 55.0})})())})()
    o._settings = lambda eventtime: {"bed_mesh": {"probe_count": [9, 9]}}
    o._kamp_status = lambda eventtime: {"reuse_loaded_mesh": 1}
    o._policy = lambda: {"sanity_max_abs": 1.0, "sanity_max_range": 0.8,
                         "sanity_max_neighbor_jump": 0.5, "density_epsilon": 0.05,
                         "bed_temp_tolerance": 3.0}
    o._config_fingerprint = lambda eventtime: (FINGERPRINT, {})
    o._request = lambda eventtime, bed_target: {
        "required_probe_count": [5, 5], "required_probe_points": 25,
        "estimated_probe_seconds": 167.5, "config_fingerprint": FINGERPRINT,
        "required_bounds": [120.0, 120.0, 180.0, 180.0],
        "baseline_spacing": [33.75, 33.75], "bed_temperature": 55.0,
        "bed_target": bed_target, "session_id": "s", "printer_state": "printing",
        "homed_axes": "xyz", "manual_invalidated": False,
    }
    return o, bed


def write_store(path, **overrides):
    store = {
        "schema": MODULE.STORE_SCHEMA, "saved_at": time.time(),
        "profile_name": "adaptive", "bounds": STORE_BOUNDS,
        "probe_count": [7, 7], "spacing": [18.67, 18.67], "points": STORED,
        "mesh_params": {"x_count": 7, "y_count": 7, "min_x": 94.0, "max_x": 206.0,
                        "min_y": 94.0, "max_y": 206.0, "algo": "bicubic"},
        "algorithm": "bicubic", "bed_target": 55.0, "config_fingerprint": FINGERPRINT,
    }
    store.update(overrides)
    with open(path, "w") as stream:
        json.dump(store, stream)


def run() -> dict:
    failures, checks = [], 0
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "store.json")

    # Plan refusals.
    for name, overrides, expected in (
        ("drift", {"config_fingerprint": "x"}, "VERIFY_CONFIG_DRIFT"),
        ("temp", {"bed_target": 70.0}, "VERIFY_TEMP"),
        ("old", {"saved_at": time.time() - 200 * 3600}, "VERIFY_STORE_TOO_OLD"),
        ("coarse", {"probe_count": [3, 3], "points": [[0, 0, 0]] * 3,
                    "spacing": [56, 56]}, "VERIFY_DENSITY"),
    ):
        write_store(path, **overrides)
        o, _ = make_owner(path)
        plan = o._verify_plan(o._request(0, 55.0), 55.0)
        checks += 1
        if plan["reason"] != expected:
            failures.append({"case": name, "reason": plan["reason"]})
    os.remove(path)
    o, _ = make_owner(path)
    checks += 1
    if o._verify_plan(o._request(0, 55.0), 55.0)["reason"] != "VERIFY_NO_STORE":
        failures.append("missing store not refused")

    # Verification passes: stored mesh is installed and becomes session metadata.
    write_store(path)
    o, bed = make_owner(path)
    grid = MODULE.verify_helpers.select_verify_grid(STORE_BOUNDS, [7, 7], [120, 120, 180, 180])
    good = [[STORED[j][i] + 0.01 for i in grid["x_index"]] for j in grid["y_index"]]
    o, bed = make_owner(path, verify_probe=good)
    o.cmd_OCTERA_MESH_REUSE_OR_CALIBRATE(Gcmd())
    checks += 1
    joined = "\n".join(o.scripts)
    if ("PROFILE=octera_verify" not in joined or "BED_MESH_PROFILE LOAD=adaptive" not in joined
            or "BED_MESH_CALIBRATE\n" in joined + "\n"
            or (o.metadata or {}).get("origin") != "verified"
            or bed.probed != STORED):
        failures.append({"pass_flow": o.scripts, "meta": (o.metadata or {}).get("origin")})

    # Verification fails: falls back to the normal adaptive calibration.
    bad = [[v + 0.08 for v in row] for row in good]
    o, bed = make_owner(path, verify_probe=bad)
    o.cmd_OCTERA_MESH_REUSE_OR_CALIBRATE(Gcmd())
    checks += 1
    if (not any(s.startswith("BED_MESH_CLEAR\nBED_MESH_CALIBRATE") for s in o.scripts)
            or o.pending_validation is None
            or o.last_verify["result"]["reason"] != "VERIFY_DEVIATION"):
        failures.append({"fail_flow": o.scripts})

    # Disabled: never probes a verification grid.
    o, bed = make_owner(path, verify_probe=good)
    o.verify_enabled = False
    o.cmd_OCTERA_MESH_REUSE_OR_CALIBRATE(Gcmd())
    checks += 1
    if any("octera_verify" in s for s in o.scripts):
        failures.append("verification ran while disabled")

    # Invalidation drops the store.
    o, _ = make_owner(path)
    o._drop_store("test")
    checks += 1
    if os.path.exists(path):
        failures.append("store survived invalidation")
    return {"pass": not failures, "checks": checks, "failures": failures}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
