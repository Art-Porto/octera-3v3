#!/usr/bin/env python3
"""The dispatcher reloads the session's adaptive profile before deciding."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "octera_mesh_reuse", REPO / "klippy/extras/octera_mesh_reuse.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BedMesh:
    def __init__(self, active):
        self.active = active

    def get_status(self, eventtime):
        return {"profile_name": self.active,
                "profiles": {"default": {}, "adaptive": {}}}


def owner(active, metadata, manual=False):
    o = MODULE.OcteraMeshReuse.__new__(MODULE.OcteraMeshReuse)
    bed = BedMesh(active)
    o.metadata, o.manual_invalidated, o.scripts = metadata, manual, []

    def run(script):
        o.scripts.append(script)
        if script.startswith("BED_MESH_PROFILE LOAD="):
            bed.active = script.split("=", 1)[1]
    o.gcode = type("G", (), {"run_script_from_command": staticmethod(run)})()
    o.printer = type("P", (), {"lookup_object": staticmethod(lambda name, default=None: bed)})()
    return o


def run() -> dict:
    failures = []
    meta = {"profile_name": "adaptive", "thermal_valid": True}
    cases = [
        ("default loaded, valid session", "default", meta, False, ["BED_MESH_PROFILE LOAD=adaptive"]),
        ("already active", "adaptive", meta, False, []),
        ("no metadata", "default", None, False, []),
        ("thermal session over", "default", dict(meta, thermal_valid=False), False, []),
        ("manual invalidation", "default", meta, True, []),
        ("unknown profile", "default", dict(meta, profile_name="gone"), False, []),
    ]
    for name, active, metadata, manual, expected in cases:
        o = owner(active, metadata, manual)
        o._restore_session_profile(0.0)
        if o.scripts != expected:
            failures.append({"case": name, "scripts": o.scripts})
    return {"pass": not failures, "checks": len(cases), "failures": failures}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
