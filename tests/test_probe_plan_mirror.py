#!/usr/bin/env python3
"""Mirror test: octera_mesh_reuse.square_probe_plan vs the real KAMP macro.

Renders BED_MESH_CALIBRATE from Adaptive_Meshing.cfg with Jinja2 (the engine
Klipper uses) and requires the Python plan to predict the same PROBE_COUNT
for every object size.  Regression for the 2026-10-01 abort, where a small
object was probed 3x3 while the module expected 4x4.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
import sys
from pathlib import Path

import jinja2

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "klippy/extras/octera_mesh_reuse.py"
SPEC = importlib.util.spec_from_file_location("octera_mesh_reuse", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

MACRO_PATH = REPO / "config/kamp/Adaptive_Meshing.cfg"
SETTINGS_PATH = REPO / "config/kamp/KAMP_Settings.cfg"
BED_MIN, BED_MAX = [15.0, 15.0], [285.0, 285.0]


def macro_template() -> jinja2.Template:
    lines = MACRO_PATH.read_text(encoding="utf-8").splitlines()
    start = lines.index("gcode:") + 1
    env = jinja2.Environment("{%", "%}", "{", "}", extensions=["jinja2.ext.do"])
    return env.from_string("\n".join(lines[start:]))


def shipped_settings() -> dict:
    settings = {}
    for line in SETTINGS_PATH.read_text(encoding="utf-8").splitlines():
        match = re.match(r"variable_(\w+):\s*([^#]+)", line)
        if match:
            settings[match.group(1)] = json.loads(
                match.group(2).strip().replace("True", "true").replace("False", "false")
            )
    return settings


def macro_count(template, settings, probe_count, polygon):
    printer = {
        "exclude_object": {"objects": [{"polygon": polygon}] if polygon else []},
        "configfile": {"settings": {"bed_mesh": {
            "mesh_min": BED_MIN, "mesh_max": BED_MAX, "probe_count": probe_count,
        }}},
        "gcode_macro _KAMP_Settings": dict(settings, verbose_enable=False),
    }
    rendered = template.render(printer=printer)
    match = re.search(r"ALGORITHM=(\w+) PROBE_COUNT=(\d+),(\d+)", rendered)
    assert match, rendered
    return [int(match.group(2)), int(match.group(3))], match.group(1)


def module_plan(settings, probe_count, polygon):
    objects = [{"polygon": polygon}] if polygon else []
    geometry = MODULE.required_bounds(
        objects, BED_MIN, BED_MAX,
        settings.get("mesh_margin", 5.0), settings.get("reuse_coverage_margin", 0.0),
    )
    return MODULE.square_probe_plan(
        geometry["required"], BED_MIN, BED_MAX, probe_count,
        minimum=settings.get("prtouch_v1_square_min_probe_count", 4),
        maximum=settings.get("prtouch_v1_square_max_probe_count", 0),
        adaptive_min=settings.get("adaptive_min_probe_count", 3),
        bicubic_min=settings.get("adaptive_bicubic_min_probe_count", 4),
        adaptive_max=settings.get("adaptive_max_probe_count", 0),
        square_safe=settings.get("prtouch_v1_square_safe", 0),
        odd=settings.get("octera_odd_probe_count", 0),
    )


def pending_count(origin: str) -> list:
    """Expected grid recorded for validation when a calibration is dispatched."""
    owner = MODULE.OcteraMeshReuse.__new__(MODULE.OcteraMeshReuse)
    owner.reactor = type("R", (), {"monotonic": lambda self: 1.0})()
    owner.enabled, owner.metadata, owner.verbose = True, None, False
    owner.mesh_time_base, owner.mesh_time_per_point = 55.0, 4.5
    owner.validation_sequence, owner.manual_invalidated = 0, False
    owner._mesh_candidate = lambda eventtime: {
        "profile_name": "", "bounds": [0.0] * 4, "probed_matrix": [[]], "algorithm": "",
    }
    owner._request = lambda eventtime, bed_target: {
        "required_probe_count": [3, 3], "required_probe_points": 9,
        "estimated_probe_seconds": 95.5, "config_fingerprint": "f" * 64,
        "required_bounds": [116.6, 95.6, 179.9, 129.1], "bed_temperature": 55.0,
    }
    owner._kamp_status = lambda eventtime: {"reuse_loaded_mesh": 1}
    owner._settings = lambda eventtime: {"bed_mesh": {"probe_count": [9, 9]}}
    owner._policy = lambda: {}
    owner.gcode = type("G", (), {"run_script_from_command": lambda self, script: None})()
    gcmd = type("C", (), {
        "get_float": lambda self, name, **kw: 55.0,
        "get": lambda self, name, default=None: origin,
        "respond_info": lambda self, message: None,
    })()
    owner.cmd_OCTERA_MESH_REUSE_OR_CALIBRATE(gcmd)
    return owner.pending_validation["request"]["required_probe_count"]


def run() -> dict:
    template = macro_template()
    shipped = shipped_settings()
    failures, checks = [], 0
    variants = [shipped]
    for square_min, square_max, adaptive_max, adaptive_min, odd in itertools.product(
        (3, 4, 5), (0, 5), (0, 6, 8, 9), (3, 4), (0, 1)
    ):
        variants.append(dict(
            shipped,
            octera_odd_probe_count=odd,
            prtouch_v1_square_min_probe_count=square_min,
            prtouch_v1_square_max_probe_count=square_max,
            adaptive_max_probe_count=adaptive_max,
            adaptive_min_probe_count=adaptive_min,
        ))
    sizes = [5, 20, 40, 51.33, 60, 61.5, 62, 90, 120, 160, 200, 250, 290]
    for settings in variants:
        for probe_count in ([9, 9], [7, 7]):
            cases = [None] + [
                [[150 - w / 2, 150 - h / 2], [150 + w / 2, 150 + h / 2]]
                for w in sizes for h in sizes
            ]
            for polygon in cases:
                expected, algorithm = macro_count(template, settings, probe_count, polygon)
                plan = module_plan(settings, probe_count, polygon)
                checks += 1
                if plan["count"] != expected or plan["algorithm"] != algorithm:
                    failures.append({
                        "polygon": polygon, "probe_count": probe_count,
                        "macro": [expected, algorithm],
                        "module": [plan["count"], plan["algorithm"]],
                    })
    # The exact object envelope of the aborted 2026-10-01 print.
    incident = [[122.611, 101.616], [173.94, 123.056]]
    incident_plan = module_plan(shipped, [9, 9], incident)
    checks += 1
    if incident_plan["count"] != [3, 3]:
        failures.append({"incident": incident_plan["count"]})
    # A full-bed calibration probes the configured grid even when the object
    # is small; the adaptive plan must not be used to validate it.
    for origin, expected in (("adaptive", [3, 3]), ("full-bed", [9, 9])):
        checks += 1
        got = pending_count(origin)
        if got != expected:
            failures.append({"origin": origin, "pending_count": got})
    # Odd-only mode: every grid is square, odd and valid for its algorithm.
    for size in sizes:
        polygon = [[150 - size / 2, 150 - size / 2], [150 + size / 2, 150 + size / 2]]
        plan = module_plan(dict(shipped, octera_odd_probe_count=1), [9, 9], polygon)
        count, checks = plan["count"], checks + 1
        if (count[0] != count[1] or count[0] % 2 == 0
                or (count[0] > 6) != (plan["algorithm"] == "bicubic")):
            failures.append({"shipped_grid": count, "algorithm": plan["algorithm"]})
    return {"pass": not failures, "checks": checks, "failures": failures[:20]}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
