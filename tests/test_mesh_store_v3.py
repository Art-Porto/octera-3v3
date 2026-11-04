#!/usr/bin/env python3
"""Mesh reuse V3: meshes kept by bed temperature and area, with no age limit.

A simulated bed answers every probe from a height function, so the full
dispatcher runs offline: calibration, storage, restart, 3x3 check, reuse,
eviction and the decision history.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "octera_mesh_reuse_v3", REPO / "klippy/extras/octera_mesh_reuse.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BED_MIN, BED_MAX, STEP = (15.0, 15.0), (285.0, 285.0), 33.75
FINGERPRINT = "a" * 64


def flat_bed(x, y):
    return round(0.001 * (x - 150.0) + 0.0005 * (y - 150.0), 6)


class Error(Exception):
    pass


class Gcmd:
    def __init__(self, bed_target=60.0, **params):
        self.bed_target = bed_target
        self.params = params
        self.messages = []

    def get_float(self, name, default=None, **kw):
        if name == "BED_TARGET":
            return self.bed_target
        return self.params.get(name, default)

    def get(self, name, default=None):
        return self.params.get(name, default)

    def respond_info(self, message):
        self.messages.append(message)

    def error(self, message):
        return Error(message)


class Bed:
    """bed_mesh stand-in: probes a height function, keeps profiles."""

    def __init__(self):
        self.surface = flat_bed
        self.active = ""
        self.probed = [[]]
        self.bounds = [0.0, 0.0, 0.0, 0.0]
        self.pmgr = type("PM", (), {})()
        self.pmgr.profiles = {}
        self.calibrations = []  # (bounds, count) of every full calibration
        self.checks = []        # bounds of every 3x3 verification
        self.bad_next = False

    def probe(self, bounds, count, name):
        x0, y0, x1, y1 = bounds
        # Like bed_mesh: the spacing is floored to 0.01 mm, mesh_max moves in.
        x1 = x0 + math.floor((x1 - x0) / (count - 1) * 100) / 100 * (count - 1)
        y1 = y0 + math.floor((y1 - y0) / (count - 1) * 100) / 100 * (count - 1)
        matrix = [[self.surface(x0 + (x1 - x0) * i / (count - 1),
                                y0 + (y1 - y0) * j / (count - 1))
                   for i in range(count)] for j in range(count)]
        if self.bad_next:
            matrix[count // 2][count // 2] = 0.95
            self.bad_next = False
        params = {"min_x": x0, "max_x": x1, "min_y": y0, "max_y": y1,
                  "x_count": count, "y_count": count,
                  "algo": "bicubic" if count > 6 else "lagrange"}
        self.pmgr.profiles[name] = {"points": matrix, "mesh_params": params}
        self.load(name)

    def load(self, name):
        if name not in self.pmgr.profiles:
            return
        profile = self.pmgr.profiles[name]
        params = profile["mesh_params"]
        self.active = name
        self.probed = profile["points"]
        self.bounds = [params["min_x"], params["min_y"], params["max_x"], params["max_y"]]

    def clear(self):
        self.active, self.probed, self.bounds = "", [[]], [0.0, 0.0, 0.0, 0.0]

    def get_status(self, eventtime):
        profiles = {name: {"points": p["points"], "mesh_params": p["mesh_params"]}
                    for name, p in self.pmgr.profiles.items()}
        return {"profile_name": self.active, "profiles": profiles,
                "probed_matrix": self.probed,
                "mesh_min": self.bounds[:2], "mesh_max": self.bounds[2:]}


def plan_count(bounds):
    span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    return min(max(int(math.ceil(span / STEP - 1e-9)) + 1, 3), 9)


class Printer:
    """One Klipper process: a new Printer on the same store is a restart."""

    def __init__(self, store_path, bed=None, max_meshes=6, fingerprint=FINGERPRINT,
                 verify_tolerance=0.05):
        self.bed = bed or Bed()
        self.part = [40.0, 40.0, 150.0, 150.0]
        self.fingerprint = fingerprint
        o = MODULE.OcteraMeshReuse.__new__(MODULE.OcteraMeshReuse)
        self.o = o
        o.reactor = type("R", (), {"monotonic": lambda self: time.monotonic()})()
        o.enabled, o.verbose, o.metadata = True, False, None
        o.manual_invalidated, o.validation_sequence = False, 0
        o.session_id = "session-%f" % time.monotonic()
        o.last_decision = {"decision": "CALIBRATE", "reason": "REJECT_METADATA_MISSING"}
        o.pending_validation, o.last_rejection = None, None
        o.validation_state, o.last_invalidation_reason = "NO_VALID_MESH", "x"
        o.mesh_time_base, o.mesh_time_per_point = 55.0, 4.5
        o.bed_temp_tolerance, o.density_epsilon = 3.0, 0.05
        o.sanity_max_abs, o.sanity_max_range, o.sanity_max_neighbor_jump = 1.0, 0.8, 0.5
        o.sanity_probe_duration_factor, o.sanity_probe_duration_slack = 2.0, 120.0
        o.verify_enabled, o.verify_tolerance = True, verify_tolerance
        o.verify_min_full_count, o.verify_max_age_hours = 5, 0.0
        o.max_meshes = max_meshes
        o.store_path, o.pending_verify, o.last_verify = store_path, None, None
        o._store_data, o._history_open = None, None
        o.gcode = type("G", (), {"run_script_from_command": staticmethod(self.run)})()
        bed_ref = self.bed
        heater = type("H", (), {"get_status": staticmethod(lambda e: {"temperature": 60.0})})()
        o.printer = type("P", (), {"lookup_object": staticmethod(
            lambda name, default=None: bed_ref if name == "bed_mesh" else heater)})()
        o._settings = lambda eventtime: {"bed_mesh": {"probe_count": [9, 9]}}
        o._kamp_status = lambda eventtime: {"reuse_loaded_mesh": 1}
        o._config_fingerprint = lambda eventtime: (self.fingerprint, {})
        o._request = self.request

    def request(self, eventtime, bed_target):
        count = plan_count(self.part)
        return {
            "required_probe_count": [count, count], "required_probe_points": count * count,
            "estimated_probe_seconds": 55.0 + 4.5 * count * count,
            "config_fingerprint": self.fingerprint,
            "required_bounds": list(self.part), "kamp_bounds": list(self.part),
            "baseline_spacing": [STEP, STEP], "bed_temperature": float(bed_target),
            "bed_target": float(bed_target), "session_id": self.o.session_id,
            "printer_state": "printing", "homed_axes": "xyz", "manual_invalidated": False,
        }

    def run(self, script):
        for line in script.splitlines():
            if line == "BED_MESH_CLEAR":
                self.bed.clear()
            elif line == "BED_MESH_CALIBRATE":
                count = plan_count(self.part)
                self.bed.calibrations.append((list(self.part), count))
                self.bed.probe(self.part, count, "adaptive")
            elif line.startswith("_BED_MESH_CALIBRATE MESH_MIN="):
                numbers = [float(v) for v in re.findall(r"-?\d+\.\d+", line)]
                bounds = numbers[:4]
                self.bed.checks.append(bounds)
                self.bed.probe(bounds, 3, "octera_verify")
            elif line.startswith("BED_MESH_PROFILE LOAD="):
                self.bed.load(line.split("=", 1)[1])
            elif line.startswith("OCTERA_MESH_METADATA_CAPTURE"):
                self.o.cmd_OCTERA_MESH_METADATA_CAPTURE(Gcmd(self.last_target))
            elif line == "OCTERA_MESH_VERIFY_FINISH":
                self.o.cmd_OCTERA_MESH_VERIFY_FINISH(Gcmd(self.last_target))

    def start_print(self, part, bed_target=60.0):
        self.part = list(part)
        self.last_target = bed_target
        gcmd = Gcmd(bed_target, ORIGIN="adaptive")
        self.o.cmd_OCTERA_MESH_REUSE_OR_CALIBRATE(gcmd)
        return gcmd

    def history(self):
        return self.o._store()["history"]

    def last(self):
        return self.history()[-1]

    def ids(self):
        return sorted(entry["id"] for entry in self.o._store()["entries"])

    def areas(self):
        return sorted(entry["bounds"] for entry in self.o._store()["entries"])


A = [20.0, 20.0, 140.0, 140.0]      # 5x5 at the front left
B = [160.0, 160.0, 280.0, 280.0]    # 5x5 at the back right
PART_A = [25.0, 25.0, 135.0, 135.0]
PART_B = [165.0, 165.0, 275.0, 275.0]

RESULTS = []


def check(name, condition, detail=None):
    RESULTS.append({"name": name, "pass": bool(condition),
                    "detail": None if condition else detail})


def fresh_path():
    return os.path.join(tempfile.mkdtemp(), "octera-mesh-store.json")


def run():
    # 1. Restart, same part: checked with 9 points and reused.
    path = fresh_path()
    p = Printer(path)
    p.start_print(A)
    check("first print probes and stores", p.last()["result"] == "PROBED"
          and p.ids() == [1] and len(p.bed.calibrations) == 1, p.history())
    bed = p.bed
    p = Printer(path, bed=bed)  # Klipper restart: metadata gone, store on disk
    gcmd = p.start_print(PART_A)
    check("restart reuses after a 3x3 check",
          p.last()["result"] == "REUSED_VERIFIED" and p.last()["mesh_id"] == 1
          and p.last()["probes"] == 9 and len(bed.calibrations) == 1
          and p.o.metadata["origin"] == "verified" and bed.active == "adaptive",
          {"history": p.history(), "messages": gcmd.messages})
    # Same session, mesh still warm: V1 reuse without probing, recorded.
    p.start_print(PART_A)
    check("session reuse recorded with no probes",
          p.last()["result"] == "REUSED_SESSION" and p.last()["probes"] == 0
          and len(bed.checks) == 1, p.history())

    # 2. Two areas at one temperature; each part finds its own.
    path = fresh_path()
    p = Printer(path)
    p.start_print(A)
    p.start_print(B)
    check("second area stored beside the first", p.areas() == [A, B], p.areas())
    bed = p.bed
    p = Printer(path, bed=bed)
    p.start_print(PART_A)
    first = p.last()
    p = Printer(path, bed=bed)
    p.start_print(PART_B)
    second = p.last()
    check("each part is checked on its own area",
          first.get("mesh_id") == 1 and second.get("mesh_id") == 2
          and first["result"] == second["result"] == "REUSED_VERIFIED"
          and len(bed.calibrations) == 2, [first, second])

    # 3. A part across both areas probes only its own area, not a union.
    across = [90.0, 90.0, 210.0, 210.0]
    p = Printer(path, bed=bed)
    p.start_print(across)
    check("part across two areas probes only itself",
          bed.calibrations[-1][0] == across and p.areas() == sorted([A, B, across])
          and p.last()["result"] == "PROBED", p.areas())
    check("its reason names the refusal", p.last().get("verify_skipped") == "VERIFY_NO_GRID"
          or p.last().get("reason") == "REJECT_METADATA_MISSING", p.last())

    # 4. A larger mesh replaces the smaller ones it covers.
    big = [20.0, 20.0, 280.0, 280.0]
    p.start_print(big)
    check("covering mesh replaces covered ones", p.areas() == [big]
          and sorted(p.last()["replaced"]) == [1, 2, 3], p.history()[-1])

    # 5. The smallest covering mesh is preferred over a full-bed one.
    p.o.metadata = None
    p.start_print(PART_A)
    check("part inside the big mesh is checked on it", p.last()["result"] == "REUSED_VERIFIED"
          and p.last()["mesh_id"] == 4, p.last())
    p.o.metadata, p.o.verify_enabled = None, False
    p.start_print(A)
    p.o.verify_enabled = True
    check("small mesh kept beside the big one", p.areas() == sorted([big, A]), p.areas())
    p = Printer(path, bed=bed)
    p.start_print(PART_A)
    small_id = [e["id"] for e in p.o._store()["entries"] if e["bounds"] == A][0]
    check("smallest covering mesh chosen", p.last().get("mesh_id") == small_id,
          p.last())

    # 6. Temperature: within tolerance reuses, outside keeps its own meshes.
    path = fresh_path()
    p = Printer(path)
    p.start_print(A, bed_target=60.0)
    bed = p.bed
    p = Printer(path, bed=bed)
    p.start_print(PART_A, bed_target=62.0)
    check("2 C away is checked and reused", p.last()["result"] == "REUSED_VERIFIED",
          p.last())
    p = Printer(path, bed=bed)
    p.start_print(PART_A, bed_target=70.0)
    check("10 C away is probed and kept apart",
          p.last()["result"] == "PROBED" and len(p.ids()) == 2
          and sorted(MODULE.temperature_key(e["bed_target"])
                     for e in p.o._store()["entries"]) == ["60", "70"], p.history())
    p = Printer(path, bed=bed)
    p.start_print(PART_A, bed_target=60.0)
    check("back at 60 C uses the 60 C mesh",
          p.last()["result"] == "REUSED_VERIFIED" and p.last()["mesh_id"] == 1, p.last())

    # 7. Per-temperature limit drops the least recently used mesh.
    path = fresh_path()
    p = Printer(path, max_meshes=2)
    p.start_print(A)
    p.start_print(B)
    bed = p.bed
    p = Printer(path, bed=bed, max_meshes=2)
    p.start_print(PART_A)  # A is now more recently used than B
    C = [20.0, 160.0, 140.0, 280.0]
    p.o.metadata = None
    p.start_print(C)
    check("least recently used mesh evicted",
          p.areas() == sorted([A, C]) and p.last()["evicted"] == [2], p.history()[-1])

    # 8. The bed moved: the check fails, the mesh is dropped, the part probed.
    path = fresh_path()
    p = Printer(path)
    p.start_print(A)
    bed = p.bed
    bed.surface = lambda x, y: flat_bed(x, y) + 0.08
    p = Printer(path, bed=bed)
    gcmd = p.start_print(PART_A)
    item = p.last()
    check("failed check drops the mesh and probes",
          item["result"] == "PROBED" and item["reason"] == "VERIFY_DEVIATION"
          and abs(item["deviation"] - 0.08) < 1e-6 and item["probes"] == 25 + 9
          and item["checked_id"] == 1 and item["mesh_id"] == 2
          and p.ids() == [2] and len(bed.calibrations) == 2, {"item": item, "m": gcmd.messages})

    # 9. No age limit: a 30-day-old mesh is still checked and reused.
    path = fresh_path()
    p = Printer(path)
    p.start_print(A)
    for entry in p.o._store()["entries"]:
        entry["saved_at"] = entry["last_used"] = time.time() - 30 * 86400
    p.o._save_store()
    bed = p.bed
    p = Printer(path, bed=bed)
    p.start_print(PART_A)
    check("no age limit by default", p.last()["result"] == "REUSED_VERIFIED", p.last())

    # 10. A config change: old meshes refused, then removed by the next store.
    p = Printer(path, bed=bed, fingerprint="b" * 64)
    p.start_print(PART_A)
    item = p.last()
    check("fingerprint change refuses and replaces",
          item["result"] == "PROBED" and item.get("verify_skipped") == "VERIFY_CONFIG_DRIFT"
          and item["evicted"] == [1] and p.ids() == [2], item)

    # 11. History: capped, survives restarts, shown in status.
    for _ in range(45):
        p.start_print(PART_A)
    p = Printer(path, bed=bed, fingerprint="b" * 64)
    history = p.history()
    status = p.o.get_status(0.0)
    check("history capped at 40 and kept on disk",
          len(history) == MODULE.HISTORY_LIMIT and history[-1]["result"] == "REUSED_SESSION",
          len(history))
    check("status shows history and stored meshes",
          len(status["history"]) == MODULE.STATUS_HISTORY
          and status["stored_meshes"][0]["id"] == 2
          and status["store_schema"] == MODULE.STORE_SCHEMA, status["stored_meshes"])
    json.dumps(status)
    gcmd = Gcmd()
    p.o.cmd_OCTERA_MESH_HISTORY(gcmd)
    check("history command lists meshes and decisions",
          "#2 60C 5x5" in gcmd.messages[0] and "REUSED_SESSION" in gcmd.messages[0],
          gcmd.messages)

    # 12. Invalidation by temperature keeps the other temperatures.
    path = fresh_path()
    p = Printer(path)
    p.start_print(A, bed_target=60.0)
    p.start_print(A, bed_target=80.0)
    gcmd = Gcmd(BED_TEMP=80.0)
    p.o.cmd_OCTERA_MESH_INVALIDATE(gcmd)
    check("invalidate one temperature",
          [MODULE.temperature_key(e["bed_target"]) for e in p.o._store()["entries"]] == ["60"]
          and p.last()["result"] == "INVALIDATED" and p.o.manual_invalidated,
          {"m": gcmd.messages, "h": p.last()})
    p.o.cmd_OCTERA_MESH_INVALIDATE(Gcmd())
    p = Printer(path, bed=p.bed)
    check("invalidate all keeps the history",
          p.ids() == [] and len(p.history()) == 4
          and os.path.exists(path + ".invalidated"), p.history())

    # 13. A corrupt store is treated as empty and rewritten.
    path = fresh_path()
    with open(path, "w") as stream:
        stream.write("{not json")
    p = Printer(path)
    p.start_print(A)
    with open(path) as stream:
        data = json.load(stream)
    check("corrupt store recovered", data["schema"] == MODULE.STORE_SCHEMA
          and len(data["entries"]) == 1, data.get("schema"))
    broken = copy.deepcopy(data)
    broken["entries"][0]["points"][0] = [0.0]
    broken["entries"].append({"id": 9, "bounds": "x"})
    check("malformed entries dropped on load",
          MODULE.normalize_store(broken)["entries"] == []
          and MODULE.normalize_store(broken)["next_id"] >= 2, None)

    # 14. Small part: a 3x3 check is not worth it, so it probes.
    path = fresh_path()
    p = Printer(path)
    p.start_print(A)
    bed = p.bed
    p = Printer(path, bed=bed)
    p.start_print([60.0, 60.0, 100.0, 100.0])
    check("small part probes, reason recorded",
          p.last()["result"] == "PROBED"
          and p.last().get("verify_skipped") == "VERIFY_NOT_WORTH_IT", p.last())

    # 15. Sanity rejection closes the open item as REJECTED.
    p = Printer(fresh_path())
    p.bed.bad_next = True
    try:
        p.start_print(A)
        raised = False
    except Error:
        raised = True
    check("rejected mesh recorded and not stored",
          raised and p.last()["result"] == "REJECTED" and p.ids() == []
          and "SANITY_RANGE_EXCEEDED" in p.last()["failure_codes"], p.history())

    # 16. Store helpers on their own.
    store = MODULE.empty_store()
    entry = {"bounds": A, "probe_count": [5, 5], "points": [[0.0] * 5] * 5,
             "mesh_params": {}, "config_fingerprint": FINGERPRINT,
             "bed_target": 60.0, "saved_at": 1.0}
    MODULE.store_add_entry(store, dict(entry), 6, 3.0)
    other = dict(entry, bed_target=70.0)
    result = MODULE.store_add_entry(store, other, 6, 3.0)
    check("covering at another temperature does not replace",
          result["replaced"] == [] and len(store["entries"]) == 2, store["entries"])

    # 17. bed_mesh floors the spacing, so mesh_max falls short of the part
    # (2026-11-04: the same part reprinted never matched its own 8x8).
    real = [41.3995, 67.7995, 258.6, 232.2]   # 8x8, spacing floored to 31.02 / 23.48
    path = fresh_path()
    p = Printer(path)
    p.start_print(real)
    check("rounded mesh ends short of the part",
          abs(p.bed.bounds[2] - p.bed.bounds[0] - 7 * 31.02) < 1e-6
          and abs(p.bed.bounds[3] - p.bed.bounds[1] - 7 * 23.48) < 1e-6,
          p.bed.bounds)
    p.start_print(real)
    check("same part, same session: reused despite the rounding",
          p.last()["result"] == "REUSED_SESSION" and len(p.bed.calibrations) == 1,
          p.history())
    p = Printer(path, bed=p.bed)
    p.start_print(real)
    check("8x8 after restart: no 3x3 fits its 7 intervals, probes",
          p.last()["result"] == "PROBED" and p.last().get("verify_skipped") == "VERIFY_NO_GRID",
          p.history())
    wide = [20.0, 20.0, 270.079, 270.079]     # 9x9, spacing 31.2599 -> 31.25
    path = fresh_path()
    p = Printer(path)
    p.start_print(wide)
    p = Printer(path, bed=p.bed)
    p.start_print(wide)
    check("9x9 0.079 short after restart: checked and reused",
          p.last()["result"] == "REUSED_VERIFIED" and len(p.bed.calibrations) == 1,
          p.history())
    beyond = [20.0, 20.0, 270.2, 270.2]       # 0.2 past the stored mesh
    p.start_print(beyond)
    check("part 0.2 mm past the mesh still probes",
          p.last()["result"] == "PROBED", p.history())

    failures = [r for r in RESULTS if not r["pass"]]
    return {"pass": not failures, "checks": len(RESULTS), "failures": failures}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2, default=str))
    sys.exit(0 if outcome["pass"] else 1)
