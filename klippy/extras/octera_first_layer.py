"""Make KAMP see what the first layer really covers.

KAMP places the adaptive mesh, the line purge and smart park from the object
polygons (EXCLUDE_OBJECT_DEFINE). OrcaSlicer's polygons outline the model
only: support bases, brims and skirts on the first layer lie outside them, so
the purge line could land on a support base and fuse with it (2026-10-29,
support 13 mm beyond the polygon).

OCTERA_FIRST_LAYER_DEFINE, run from START_PRINT, reads the first layer of the
file being printed and, when its extrusions reach beyond the object polygons,
defines one more object, OCTERA_FIRST_LAYER, covering them. The KAMP macros
and the mesh reuse module already take the union of all polygons, so they
follow without changes. If anything is unclear (no file, no layer markers,
nothing extruded) the command does nothing and printing goes on as before.
"""

from __future__ import annotations

import json
import logging

OBJECT_NAME = "OCTERA_FIRST_LAYER"
SKIPPED_TYPES = {"Custom"}
EXTRUDE_EPSILON = 1e-5


def _word(line, letter):
    """Value of a G-code word such as X12.5, or None."""
    for token in line.split():
        if token[0] in (letter, letter.lower()) and len(token) > 1:
            try:
                return float(token[1:])
            except ValueError:
                return None
    return None


class FirstLayerScanner:
    """Feed G-code lines; collects the extent of extrusions on layer one.

    Layers are delimited by OrcaSlicer's ';LAYER_CHANGE' markers: collection
    starts at the first and stops at the second. Moves follow Klipper's rules:
    G90/G91 switch XYZ and E together, M82/M83 switch E only, G92 resets.
    """

    def __init__(self):
        self.layer = 0
        self.done = False
        self.feature = None
        self.abs_xy = True
        self.abs_e = True
        self.x = self.y = None
        self.e = 0.0
        self.bounds = None
        self.extrusions = 0

    def _include(self, x, y):
        if x is None or y is None:
            return
        if self.bounds is None:
            self.bounds = [x, y, x, y]
        else:
            b = self.bounds
            b[0] = min(b[0], x)
            b[1] = min(b[1], y)
            b[2] = max(b[2], x)
            b[3] = max(b[3], y)

    def feed(self, raw):
        if self.done:
            return
        line = raw.strip()
        if not line:
            return
        if line[0] == ";":
            if line.startswith(";LAYER_CHANGE"):
                self.layer += 1
                if self.layer > 1:
                    self.done = True
            elif line.startswith(";TYPE:"):
                self.feature = line[6:].strip()
            return
        code = line.split(";", 1)[0].strip()
        if not code:
            return
        head = code.split()[0].upper()
        if head == "G90":
            self.abs_xy = self.abs_e = True
        elif head == "G91":
            self.abs_xy = self.abs_e = False
        elif head == "M82":
            self.abs_e = True
        elif head == "M83":
            self.abs_e = False
        elif head == "G92":
            for letter in "XYE":
                value = _word(code, letter)
                if value is None:
                    continue
                if letter == "X":
                    self.x = value
                elif letter == "Y":
                    self.y = value
                else:
                    self.e = value
        elif head in ("G0", "G1", "G2", "G3"):
            self._move(head, code)

    def _move(self, head, code):
        start_x, start_y = self.x, self.y
        x, y, e = _word(code, "X"), _word(code, "Y"), _word(code, "E")
        if x is not None:
            self.x = x if self.abs_xy or self.x is None else self.x + x
        if y is not None:
            self.y = y if self.abs_xy or self.y is None else self.y + y
        extruded = False
        if e is not None:
            if self.abs_e:
                extruded = e > self.e + EXTRUDE_EPSILON
                self.e = e
            else:
                extruded = e > EXTRUDE_EPSILON
        if (not extruded or self.layer != 1
                or self.feature in SKIPPED_TYPES):
            return
        self.extrusions += 1
        self._include(start_x, start_y)
        self._include(self.x, self.y)
        if head in ("G2", "G3") and start_x is not None:
            i, j = _word(code, "I") or 0.0, _word(code, "J") or 0.0
            cx, cy = start_x + i, start_y + j
            r = (i * i + j * j) ** 0.5
            # Conservative: the whole circle the arc belongs to.
            self._include(cx - r, cy - r)
            self._include(cx + r, cy + r)

    def result(self):
        if self.layer < 1 or self.extrusions == 0:
            return None
        return [round(v, 3) for v in self.bounds]


def polygon_bounds(objects):
    points = [p for obj in objects for p in (obj.get("polygon") or [])]
    if not points:
        return None
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def needs_extra_object(first_layer, objects, tolerance):
    """True when the first layer reaches beyond the object polygons."""
    if first_layer is None:
        return False
    if any(obj.get("name") == OBJECT_NAME for obj in objects):
        return False
    poly = polygon_bounds(objects)
    if poly is None:
        return True
    return (first_layer[0] < poly[0] - tolerance
            or first_layer[1] < poly[1] - tolerance
            or first_layer[2] > poly[2] + tolerance
            or first_layer[3] > poly[3] + tolerance)


def define_command(bounds):
    x0, y0, x1, y1 = bounds
    polygon = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return "EXCLUDE_OBJECT_DEFINE NAME=%s CENTER=%.3f,%.3f POLYGON=%s" % (
        OBJECT_NAME, (x0 + x1) / 2.0, (y0 + y1) / 2.0,
        json.dumps(polygon, separators=(",", ":")))


class OcteraFirstLayer:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.tolerance = config.getfloat("tolerance", 1.0, minval=0.)
        self.max_bytes = config.getint("max_bytes", 16 * 1024 * 1024,
                                       minval=65536)
        self.chunk = 65536
        self.last = {"result": "NOT_RUN"}
        gcode = self.printer.lookup_object("gcode")
        gcode.register_command(
            "OCTERA_FIRST_LAYER_DEFINE", self.cmd_OCTERA_FIRST_LAYER_DEFINE,
            desc="Add the real first-layer footprint (supports, brim) to the "
                 "objects KAMP plans around")

    def _current_file(self):
        sd = self.printer.lookup_object("virtual_sdcard", None)
        if sd is None:
            return None, None
        status = sd.get_status(self.reactor.monotonic())
        path = status.get("file_path")
        if not path and hasattr(sd, "file_path"):
            path = sd.file_path()
        return path, int(status.get("file_position") or 0)

    def _scan(self, path, position):
        scanner = FirstLayerScanner()
        read = 0
        rest = ""
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            stream.seek(position)
            while not scanner.done and read < self.max_bytes:
                data = stream.read(self.chunk)
                if not data:
                    break
                read += len(data)
                lines = (rest + data).split("\n")
                rest = lines.pop()
                for line in lines:
                    scanner.feed(line)
                    if scanner.done:
                        break
                # Let the reactor run between chunks (heaters, MCU traffic).
                self.reactor.pause(self.reactor.monotonic() + 0.001)
            if not scanner.done and rest:
                scanner.feed(rest)
        return scanner, read

    def cmd_OCTERA_FIRST_LAYER_DEFINE(self, gcmd):
        path, position = self._current_file()
        if not path:
            self.last = {"result": "SKIP_NO_FILE"}
            gcmd.respond_info("OCTERA_FIRST_LAYER: no file being printed; skipped")
            return
        try:
            scanner, read = self._scan(path, position)
        except Exception:
            logging.exception("octera_first_layer: scan failed")
            self.last = {"result": "SKIP_SCAN_ERROR"}
            gcmd.respond_info("OCTERA_FIRST_LAYER: could not read the file; skipped")
            return
        bounds = scanner.result()
        exclude = self.printer.lookup_object("exclude_object", None)
        objects = []
        if exclude is not None:
            objects = exclude.get_status(self.reactor.monotonic()).get("objects", [])
        poly = polygon_bounds(objects)
        self.last = {"result": "NO_CHANGE", "first_layer": bounds,
                     "objects": poly, "bytes_read": read}
        if bounds is None:
            self.last["result"] = "SKIP_NO_FIRST_LAYER"
            gcmd.respond_info("OCTERA_FIRST_LAYER: first layer not found; skipped")
            return
        if exclude is None or not needs_extra_object(bounds, objects, self.tolerance):
            gcmd.respond_info("OCTERA_FIRST_LAYER: first layer %s within the objects"
                              % (bounds,))
            return
        self.printer.lookup_object("gcode").run_script_from_command(
            define_command(bounds))
        self.last["result"] = "DEFINED"
        gcmd.respond_info(
            "OCTERA_FIRST_LAYER: first layer %s reaches beyond the objects %s; "
            "defined %s so mesh, purge and park account for it"
            % (bounds, [round(v, 3) for v in poly] if poly else None, OBJECT_NAME))

    def get_status(self, eventtime=None):
        return dict(self.last)


def load_config(config):
    return OcteraFirstLayer(config)
