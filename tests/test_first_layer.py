#!/usr/bin/env python3
"""Offline tests for octera_first_layer (first-layer footprint for KAMP).

Regression for 2026-10-29: the support base reached 13 mm beyond the object
polygon and the line purge landed on it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "octera_first_layer", REPO / "klippy/extras/octera_first_layer.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# Shape of an OrcaSlicer file: start G-code, layer one with a support base
# below the model, layer two wider still (must be ignored).
GCODE = """\
EXCLUDE_OBJECT_DEFINE NAME=part CENTER=150,150 POLYGON=[[130,95],[170,95],[170,205],[130,205]]
START_PRINT EXTRUDER_TEMP=230 BED_TEMP=55
G90
M83
;TYPE:Custom
G1 X5 Y5 E10 ; start G-code purge, not part of the print
;LAYER_CHANGE
;Z:0.2
G1 Z0.2
;TYPE:Support
G1 X133.5 Y82.5 F6000
G1 X166 Y82.5 E1.2
G1 X166 Y217.6 E4.0
;TYPE:Outer wall
G1 X130.4 Y110 E0.5
G2 X140 Y100 I5 J-5 E0.4
G92 E0
;TYPE:Custom
G1 X290 Y290 E0.1
;LAYER_CHANGE
;Z:0.4
G1 X1 Y1 E5
"""


def scan(text):
    scanner = MODULE.FirstLayerScanner()
    for line in text.split("\n"):
        scanner.feed(line)
    return scanner


def run() -> dict:
    failures = []

    def require(ok, name, detail=None):
        if not ok:
            failures.append({"check": name, "detail": detail})

    start = GCODE.index("START_PRINT")
    bounds = scan(GCODE[start:]).result()
    require(bounds is not None, "found_first_layer")
    if bounds:
        require(bounds[1] == 82.5 and bounds[3] == 217.6, "support_in_bounds", bounds)
        require(bounds[2] < 200, "custom_and_layer_two_ignored", bounds)
        # Arc centre (135.4,105) radius 7.07: the circle reaches x=128.33.
        require(abs(bounds[0] - 128.329) < 0.01, "arc_circle_included", bounds)

    absolute = scan("G90\nM82\n;LAYER_CHANGE\nG1 X10 Y10 E1\nG1 X20 Y10 E1\n"
                    "G1 X30 Y30 E2\nG92 E0\nG1 X40 Y40 E0.5\n;LAYER_CHANGE\n")
    require(absolute.result() == [10.0, 10.0, 40.0, 40.0], "absolute_e_and_g92",
            absolute.result())

    travel_only = scan(";LAYER_CHANGE\nG1 X10 Y10\nG1 X20 Y20 E-0.5\n;LAYER_CHANGE\n")
    require(travel_only.result() is None, "travel_and_retract_not_counted")

    relative_xy = scan("G91\n;LAYER_CHANGE\nG1 X10 Y10 E1\nG1 X5 Y-20 E1\n")
    require(relative_xy.result() is None or relative_xy.result()[1] < 0,
            "relative_moves_accumulate", relative_xy.result())

    require(scan("G1 X10 Y10 E1\n").result() is None, "no_layer_marker_no_result")

    part = [{"name": "PART", "polygon": [[130, 95], [170, 95], [170, 205], [130, 205]]}]
    require(MODULE.needs_extra_object([130.4, 82.5, 169.6, 217.6], part, 1.0),
            "support_beyond_polygon_defines")
    require(not MODULE.needs_extra_object([130.4, 95.2, 169.6, 205.5], part, 1.0),
            "within_tolerance_no_object")
    require(MODULE.needs_extra_object([1, 1, 2, 2], [], 1.0), "no_objects_defines")
    require(not MODULE.needs_extra_object(
        [0, 0, 300, 300], part + [{"name": MODULE.OBJECT_NAME, "polygon": []}], 1.0),
        "defined_once")

    command = MODULE.define_command([130.4, 82.5, 169.6, 217.6])
    require(command.startswith("EXCLUDE_OBJECT_DEFINE NAME=OCTERA_FIRST_LAYER CENTER=150.000,150.050 POLYGON="),
            "define_command_format", command)
    polygon = json.loads(command.split("POLYGON=", 1)[1])
    require(polygon[0] == [130.4, 82.5] and polygon[2] == [169.6, 217.6],
            "define_polygon_corners", polygon)

    failures.extend(klipper_wrapper_checks())

    start_print = (REPO / "config/kamp/Start_Print.cfg").read_text(encoding="utf-8")
    first = start_print.find("OCTERA_FIRST_LAYER_DEFINE")
    require(first != -1 and first < start_print.find("OCTERA_MESH_REUSE_OR_CALIBRATE")
            and first < start_print.find("_LINE_PURGE")
            and first < start_print.find("_SMART_PARK"),
            "start_print_defines_before_mesh_purge_park")
    settings = (REPO / "config/kamp/KAMP_Settings.cfg").read_text(encoding="utf-8")
    require("[octera_first_layer]" in settings, "config_section_present")
    return {"pass": not failures, "failures": failures}


class FakeReactor:
    def monotonic(self):
        return 0.0

    def pause(self, waketime):
        return waketime


class FakeConfig:
    def __init__(self, printer):
        self.printer = printer

    def get_printer(self):
        return self.printer

    def getfloat(self, name, default, **kw):
        return default

    def getint(self, name, default, **kw):
        return default


class FakeGcode:
    def __init__(self):
        self.scripts, self.handlers = [], {}

    def register_command(self, name, handler, desc=None):
        self.handlers[name] = handler

    def run_script_from_command(self, script):
        self.scripts.append(script)


class FakeStatus:
    def __init__(self, status):
        self.status = status

    def get_status(self, eventtime):
        return self.status


class FakePrinter:
    def __init__(self, objects):
        self.reactor, self.gcode = FakeReactor(), FakeGcode()
        self.objects = objects

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == "gcode":
            return self.gcode
        return self.objects.get(name, default)


class FakeGcmd:
    def __init__(self):
        self.messages = []

    def respond_info(self, msg):
        self.messages.append(msg)


def klipper_wrapper_checks():
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "job.gcode"
        path.write_text(GCODE, encoding="utf-8")
        position = GCODE.index("START_PRINT")
        part = [{"name": "PART", "polygon": [[130, 95], [170, 95], [170, 205], [130, 205]]}]
        printer = FakePrinter({
            "virtual_sdcard": FakeStatus({"file_path": str(path), "file_position": position}),
            "exclude_object": FakeStatus({"objects": part}),
        })
        owner = MODULE.OcteraFirstLayer(FakeConfig(printer))
        owner.chunk = 64  # many chunks: exercises the line carry-over
        gcmd = FakeGcmd()
        printer.gcode.handlers["OCTERA_FIRST_LAYER_DEFINE"](gcmd)
        if len(printer.gcode.scripts) != 1 or "OCTERA_FIRST_LAYER" not in printer.gcode.scripts[0]:
            failures.append({"check": "wrapper_defines_object", "detail": printer.gcode.scripts})
        if owner.get_status()["result"] != "DEFINED":
            failures.append({"check": "wrapper_status", "detail": owner.get_status()})

        idle = FakePrinter({"virtual_sdcard": FakeStatus({"file_path": None})})
        owner = MODULE.OcteraFirstLayer(FakeConfig(idle))
        owner.cmd_OCTERA_FIRST_LAYER_DEFINE(FakeGcmd())
        if idle.gcode.scripts or owner.get_status()["result"] != "SKIP_NO_FILE":
            failures.append({"check": "no_file_does_nothing", "detail": owner.get_status()})
    return failures


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
