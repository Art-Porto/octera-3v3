"""Octera KAMP mesh reuse for the Creality Ender-3 V3 Plus.

V1: the session mesh is reused without probing only while its metadata,
which lives in memory, is valid (same Klipper process, bed still warm, same
config fingerprint, covers the part).  A restart can never reuse a stale
mesh without probing.

V2/V3: every accepted mesh is also kept on disk, grouped by bed temperature,
several areas per temperature and with no age limit.  When V1 refuses, the
smallest stored mesh that covers the part is checked against the real bed
with a 3x3 on its own nodes and reused only if every node agrees.  Each
decision is kept in a short history that survives restarts.

The decision engine is intentionally independent from Klipper so the exact
live policy can be exercised with offline fixtures.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time

try:  # Klipper loads extras as a package; the offline tests load by path.
    from . import octera_mesh_verify as verify_helpers
except ImportError:  # pragma: no cover - offline import
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "octera_mesh_verify",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "octera_mesh_verify.py"),
    )
    verify_helpers = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(verify_helpers)

STORE_SCHEMA = "octera-mesh-store-v2"
STORE_SCHEMA_V1 = "octera-mesh-store-v1"
# Decisions kept in the store file, and how many of them get_status shows.
HISTORY_LIMIT = 40
STATUS_HISTORY = 20
# Every verified mesh is installed under this profile name, so the stock
# "default" profile in memory is never replaced by a stored copy.
VERIFIED_PROFILE = "adaptive"
# CALIBRATE reasons a verified stored mesh may stand in for.
VERIFIABLE_REASONS = {
    "REJECT_NO_MESH", "REJECT_METADATA_MISSING", "REJECT_THERMAL_SESSION",
    "REJECT_COVERAGE", "REJECT_DENSITY", "REJECT_TEMP", "REJECT_MESH_CHANGED",
}


SCHEMA = "octera-kamp-mesh-reuse-v1"
KNOWN_ALGORITHMS = {"lagrange", "bicubic"}
COVERAGE_EPSILON_MM = 0.05


def _loaded_source_sha256():
    """Hash of this file as imported, so status shows which code is in memory.

    Klipper's RESTART keeps the Python process and its imported modules; only
    a service restart loads a new version of this file.
    """
    try:
        with open(__file__.replace(".pyc", ".py"), "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()
    except Exception:
        return None


MODULE_SHA256 = _loaded_source_sha256()
REASONS = {
    "REUSE_PASS",
    "REJECT_NO_MESH",
    "REJECT_COVERAGE",
    "REJECT_DENSITY",
    "REJECT_TEMP",
    "REJECT_THERMAL_SESSION",
    "REJECT_CONFIG_DRIFT",
    "REJECT_SANITY",
    "REJECT_SANITY_MESH_CLEARED",
    "REJECT_SANITY_CLEAR_FAILED",
    "REJECT_METADATA_MISSING",
    "REJECT_MESH_CHANGED",
    "REJECT_MANUAL_INVALIDATION",
    "REJECT_PRINTER_STATE",
    "REJECT_UNKNOWN",
}

VALIDATION_STATES = {
    "NO_VALID_MESH",
    "PENDING_VALIDATION",
    "VALID_FOR_REUSE",
    "INVALIDATED",
    "REJECTED_MESH_CLEARED",
    "REJECTED_CLEAR_FAILED",
}


def _plain(value):
    """Convert Klipper status/config values into stable JSON primitives."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def stable_fingerprint(components):
    payload = json.dumps(
        _plain(components), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def required_bounds(objects, bed_min, bed_max, mesh_margin, reuse_margin):
    """Return KAMP bounds and the larger envelope calibration must cover."""
    points = []
    for obj in objects or []:
        for point in obj.get("polygon", []) or []:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                points.append((float(point[0]), float(point[1])))
    if points:
        object_bounds = [
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        ]
    else:
        object_bounds = [
            float(bed_min[0]), float(bed_min[1]),
            float(bed_max[0]), float(bed_max[1]),
        ]
    kamp = [
        max(float(bed_min[0]), object_bounds[0] - float(mesh_margin)),
        max(float(bed_min[1]), object_bounds[1] - float(mesh_margin)),
        min(float(bed_max[0]), object_bounds[2] + float(mesh_margin)),
        min(float(bed_max[1]), object_bounds[3] + float(mesh_margin)),
    ]
    required = [
        max(float(bed_min[0]), kamp[0] - float(reuse_margin)),
        max(float(bed_min[1]), kamp[1] - float(reuse_margin)),
        min(float(bed_max[0]), kamp[2] + float(reuse_margin)),
        min(float(bed_max[1]), kamp[3] + float(reuse_margin)),
    ]
    return {
        "object": object_bounds,
        "kamp": kamp,
        "required": required,
        "object_count": len(objects or []),
    }


def square_probe_plan(required_kamp_bounds, bed_min, bed_max, probe_count,
                      minimum=4, maximum=0, adaptive_min=3, bicubic_min=4,
                      adaptive_max=0, square_safe=1, odd=0):
    """Mirror the BED_MESH_CALIBRATE count policy in Adaptive_Meshing.cfg.

    The macro decides the physical grid; this function must predict it
    step for step, otherwise a good mesh is rejected as a dimension
    mismatch.  The square minimum only applies when the per-axis counts
    differ, exactly as in the macro.
    """
    configured_x = int(probe_count[0])
    configured_y = int(probe_count[1])
    full_step_x = (
        (float(bed_max[0]) - float(bed_min[0])) / (configured_x - 1)
    )
    full_step_y = (
        (float(bed_max[1]) - float(bed_min[1])) / (configured_y - 1)
    )
    cap_x, cap_y = configured_x, configured_y
    if int(adaptive_max) > 0:
        cap_x = min(cap_x, max(int(adaptive_max), 3))
        cap_y = min(cap_y, max(int(adaptive_max), 3))
    adaptive_min = max(int(adaptive_min), 3)
    bicubic_min = max(int(bicubic_min), 4)
    span_x = float(required_kamp_bounds[2]) - float(required_kamp_bounds[0])
    span_y = float(required_kamp_bounds[3]) - float(required_kamp_bounds[1])
    raw_x = int(math.ceil(span_x / full_step_x)) + 1
    raw_y = int(math.ceil(span_y / full_step_y)) + 1
    if max(raw_x, raw_y) > 6 and cap_x >= bicubic_min and cap_y >= bicubic_min:
        algorithm, min_points = "bicubic", bicubic_min
    else:
        algorithm, min_points = "lagrange", adaptive_min
    points_x = min(max(raw_x, min_points), cap_x)
    points_y = min(max(raw_y, min_points), cap_y)
    if int(square_safe) == 1 and points_x != points_y:
        square_cap = min(cap_x, cap_y)
        if int(maximum) > 0:
            square_cap = min(square_cap, max(int(maximum), 3))
        square = min(max(points_x, points_y, int(minimum)), square_cap)
        points_x = points_y = square
    if int(odd) == 1:
        if points_x % 2 == 0:
            points_x = points_x + 1 if points_x + 1 <= cap_x else max(points_x - 1, 3)
        if points_y % 2 == 0:
            points_y = points_y + 1 if points_y + 1 <= cap_y else max(points_y - 1, 3)
        algorithm = "bicubic" if max(points_x, points_y) > 6 else "lagrange"
    return {
        "count": [points_x, points_y],
        "points": points_x * points_y,
        "baseline_spacing": [full_step_x, full_step_y],
        "raw_count": [raw_x, raw_y],
        "algorithm": algorithm,
    }


def mesh_sanity(mesh, *, max_abs, max_range, max_neighbor_jump,
                expected_count=None, probe_duration_sec=None,
                expected_duration_sec=None, duration_factor=2.0,
                duration_slack=120.0, known_algorithms=KNOWN_ALGORITHMS):
    """Validate physical probe data; never count the interpolated matrix."""
    matrix = mesh.get("probed_matrix") or []
    bounds = mesh.get("bounds") or []
    algorithm = str(mesh.get("algorithm") or "").lower()
    result = {
        "pass": False,
        "reason": "REJECT_SANITY",
        "failure_codes": [],
        "count": [0, 0],
        "expected_count": None,
        "spacing": [None, None],
        "min": None,
        "max": None,
        "mean": None,
        "range": None,
        "max_neighbor_jump": None,
        "algorithm": algorithm,
        "probe_duration_sec": probe_duration_sec,
        "expected_duration_sec": expected_duration_sec,
        "probe_duration_limit_sec": None,
    }
    def reject(code):
        if code not in result["failure_codes"]:
            result["failure_codes"].append(code)
        return result

    if len(bounds) != 4:
        return reject("SANITY_DIMENSION_MISMATCH")
    try:
        numeric_bounds = [float(value) for value in bounds]
    except (TypeError, ValueError):
        return reject("SANITY_NONFINITE")
    if not all(math.isfinite(value) for value in numeric_bounds):
        return reject("SANITY_NONFINITE")
    if numeric_bounds[0] >= numeric_bounds[2] or numeric_bounds[1] >= numeric_bounds[3]:
        return reject("SANITY_DIMENSION_MISMATCH")
    if not matrix or not isinstance(matrix, (list, tuple)):
        return reject("SANITY_DIMENSION_MISMATCH")
    rows = len(matrix)
    cols = len(matrix[0]) if matrix[0] else 0
    # Report what was observed even when the shape is rejected below.
    result["count"] = [cols, rows]
    if rows < 3 or cols < 3 or rows != cols:
        return reject("SANITY_DIMENSION_MISMATCH")
    if any(not row or len(row) != cols for row in matrix):
        return reject("SANITY_DIMENSION_MISMATCH")
    if expected_count is not None:
        try:
            expected = [int(expected_count[0]), int(expected_count[1])]
        except (IndexError, TypeError, ValueError):
            return reject("SANITY_DIMENSION_MISMATCH")
        result["expected_count"] = expected
        if [cols, rows] != expected:
            return reject("SANITY_DIMENSION_MISMATCH")
    values = []
    for row in matrix:
        for raw in row:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return reject("SANITY_NONFINITE")
            if not math.isfinite(value):
                return reject("SANITY_NONFINITE")
            values.append(value)
    if algorithm not in known_algorithms:
        return reject("SANITY_ALGORITHM_UNKNOWN")
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum
    jumps = []
    for y, row in enumerate(matrix):
        for x, raw in enumerate(row):
            value = float(raw)
            if x + 1 < cols:
                jumps.append(abs(value - float(row[x + 1])))
            if y + 1 < rows:
                jumps.append(abs(value - float(matrix[y + 1][x])))
    neighbor = max(jumps) if jumps else 0.0
    spacing_x = (numeric_bounds[2] - numeric_bounds[0]) / (cols - 1)
    spacing_y = (numeric_bounds[3] - numeric_bounds[1]) / (rows - 1)
    result.update({
        "count": [cols, rows],
        "spacing": [spacing_x, spacing_y],
        "min": minimum,
        "max": maximum,
        "mean": sum(values) / len(values),
        "range": span,
        "max_neighbor_jump": neighbor,
    })
    if maximum > float(max_abs) or minimum < -float(max_abs):
        reject("SANITY_ABS_EXCEEDED")
    if span > float(max_range):
        reject("SANITY_RANGE_EXCEEDED")
    if neighbor > float(max_neighbor_jump):
        reject("SANITY_NEIGHBOR_JUMP_EXCEEDED")
    if probe_duration_sec is not None or expected_duration_sec is not None:
        try:
            actual_duration = float(probe_duration_sec)
            expected_duration = float(expected_duration_sec)
            duration_limit = max(
                expected_duration * float(duration_factor),
                expected_duration + float(duration_slack),
            )
        except (TypeError, ValueError):
            return reject("SANITY_PROBE_DURATION_ANOMALY")
        result["probe_duration_sec"] = actual_duration
        result["expected_duration_sec"] = expected_duration
        result["probe_duration_limit_sec"] = duration_limit
        if (
            not math.isfinite(actual_duration)
            or not math.isfinite(expected_duration)
            or actual_duration < 0.0
            or expected_duration <= 0.0
            or actual_duration > duration_limit
        ):
            reject("SANITY_PROBE_DURATION_ANOMALY")
    if result["failure_codes"]:
        return result
    result["pass"] = True
    result["reason"] = "REUSE_PASS"
    return result


def evaluate_reuse(candidate, metadata, request, policy):
    """Deterministic fail-safe decision shared by live code and fixtures."""
    decision = {
        "decision": "CALIBRATE",
        "reason": "REJECT_UNKNOWN",
        "gates": {},
    }
    matrix = candidate.get("probed_matrix") or []
    if not matrix or not matrix[0]:
        decision["reason"] = "REJECT_NO_MESH"
        return decision
    if request.get("manual_invalidated"):
        decision["reason"] = "REJECT_MANUAL_INVALIDATION"
        return decision
    if not metadata:
        decision["reason"] = "REJECT_METADATA_MISSING"
        return decision
    sanity = mesh_sanity(
        candidate,
        max_abs=policy["sanity_max_abs"],
        max_range=policy["sanity_max_range"],
        max_neighbor_jump=policy["sanity_max_neighbor_jump"],
    )
    decision["sanity"] = sanity
    decision["gates"]["sanity"] = sanity["pass"]
    if not sanity["pass"]:
        decision["reason"] = "REJECT_SANITY"
        return decision
    current_session = request.get("session_id")
    thermal_pass = (
        bool(metadata.get("thermal_valid"))
        and metadata.get("session_id") == current_session
    )
    decision["gates"]["thermal_session"] = thermal_pass
    if not thermal_pass:
        decision["reason"] = "REJECT_THERMAL_SESSION"
        return decision
    fingerprint_pass = (
        metadata.get("config_fingerprint")
        == request.get("config_fingerprint")
    )
    decision["gates"]["config_fingerprint"] = fingerprint_pass
    if not fingerprint_pass:
        decision["reason"] = "REJECT_CONFIG_DRIFT"
        return decision
    loaded = [float(value) for value in candidate["bounds"]]
    stored = [float(value) for value in metadata.get("bounds", [])]
    metadata_match = (
        len(stored) == 4
        and all(abs(loaded[index] - stored[index]) <= 0.05 for index in range(4))
        and list(sanity["count"]) == list(metadata.get("probe_count", []))
    )
    decision["gates"]["metadata_mesh_match"] = metadata_match
    if not metadata_match:
        # The loaded mesh is not the one the metadata describes: something
        # outside this module replaced it (homing, a manual profile load).
        decision["reason"] = "REJECT_MESH_CHANGED"
        return decision
    required = [float(value) for value in request["required_bounds"]]
    coverage = bounds_cover(loaded, required, count=sanity["count"])
    decision["gates"]["coverage"] = coverage
    if not coverage:
        decision["reason"] = "REJECT_COVERAGE"
        return decision
    baseline = request["baseline_spacing"]
    density = (
        sanity["spacing"][0] <= float(baseline[0]) + float(policy["density_epsilon"])
        and sanity["spacing"][1] <= float(baseline[1]) + float(policy["density_epsilon"])
    )
    decision["gates"]["density"] = density
    if not density:
        decision["reason"] = "REJECT_DENSITY"
        return decision
    target_delta = abs(
        float(request["bed_target"]) - float(metadata["bed_target"])
    )
    actual_delta = abs(
        float(request["bed_temperature"]) - float(metadata["bed_temperature"])
    )
    temperature = (
        target_delta <= float(policy["bed_temp_tolerance"])
        and actual_delta <= float(policy["bed_temp_tolerance"])
    )
    decision["temperature_delta"] = {
        "target": target_delta, "actual": actual_delta,
    }
    decision["gates"]["temperature"] = temperature
    if not temperature:
        decision["reason"] = "REJECT_TEMP"
        return decision
    state_pass = (
        request.get("printer_state") in ("printing", "standby")
        and set("xyz").issubset(set(request.get("homed_axes", "")))
    )
    decision["gates"]["printer_state"] = state_pass
    if not state_pass:
        decision["reason"] = "REJECT_PRINTER_STATE"
        return decision
    decision["decision"] = "REUSE"
    decision["reason"] = "REUSE_PASS"
    return decision


# ----- Mesh store (V3): pure helpers -----------------------------------------
def temperature_key(bed_target):
    return str(int(round(float(bed_target))))


def bounds_cover(outer, inner, epsilon=COVERAGE_EPSILON_MM, count=None):
    """Whether the mesh area outer covers inner.

    With count (the outer mesh's probe count), the high side also accepts
    the shortfall bed_mesh leaves when it floors the probe spacing; without
    it, a part reprinted on its own mesh never matched (2026-11-04: 8x8 to
    258.54 against 258.60 required).
    """
    slack_x = slack_y = epsilon
    if count is not None:
        # The floor loses less than 0.01 mm per interval: the larger of the
        # two slacks is enough, never their sum.
        slack_x = max(epsilon, verify_helpers.rounding_allowance(count[0]))
        slack_y = max(epsilon, verify_helpers.rounding_allowance(count[1]))
    return (float(outer[0]) <= float(inner[0]) + epsilon
            and float(outer[1]) <= float(inner[1]) + epsilon
            and float(outer[2]) >= float(inner[2]) - slack_x
            and float(outer[3]) >= float(inner[3]) - slack_y)


def bounds_area(bounds):
    return (float(bounds[2]) - float(bounds[0])) * (float(bounds[3]) - float(bounds[1]))


def empty_store():
    return {"schema": STORE_SCHEMA, "entries": [], "history": [], "next_id": 1}


def entry_is_wellformed(entry):
    """Shape check only; mesh_sanity still runs before an entry is used."""
    try:
        bounds = [float(value) for value in entry["bounds"]]
        count = [int(entry["probe_count"][0]), int(entry["probe_count"][1])]
        points = entry["points"]
        if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
            return False
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            return False
        if len(points) != count[1] or any(len(row) != count[0] for row in points):
            return False
        for row in points:
            for value in row:
                if not math.isfinite(float(value)):
                    return False
        if not isinstance(entry["mesh_params"], dict):
            return False
        if not isinstance(entry["config_fingerprint"], str):
            return False
        float(entry["bed_target"])
        float(entry["saved_at"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return True


def normalize_store(data):
    """Return a v2 store from whatever was on disk; never raises.

    A v1 store (one mesh, the V2 format) becomes the first entry.
    """
    store = empty_store()
    if not isinstance(data, dict):
        return store
    if data.get("schema") == STORE_SCHEMA_V1:
        entry = {key: value for key, value in data.items() if key != "schema"}
        entry.setdefault("uses", 0)
        entry.setdefault("last_used", entry.get("saved_at"))
        entry.setdefault("origin", "adaptive")
        entry["id"] = 1
        if entry_is_wellformed(entry):
            store["entries"].append(entry)
            store["next_id"] = 2
        return store
    if data.get("schema") != STORE_SCHEMA:
        return store
    entries = data.get("entries")
    if isinstance(entries, list):
        store["entries"] = [entry for entry in entries
                            if isinstance(entry, dict) and entry_is_wellformed(entry)]
    history = data.get("history")
    if isinstance(history, list):
        store["history"] = [item for item in history
                            if isinstance(item, dict)][-HISTORY_LIMIT:]
    used = [int(entry.get("id", 0) or 0) for entry in store["entries"]]
    try:
        next_id = int(data.get("next_id", 1))
    except (TypeError, ValueError):
        next_id = 1
    store["next_id"] = max([next_id] + [value + 1 for value in used])
    return store


def store_add_entry(store, entry, max_per_temperature, bed_temp_tolerance):
    """Add a freshly probed mesh; returns the ids it replaced and evicted.

    Entries probed under another config fingerprint can never be used
    again and go.  A new mesh replaces the stored ones it covers at the same
    temperature: they describe a part of the same bed and are older.  Past
    the per-temperature limit, the least recently used entry goes.
    """
    fingerprint = entry["config_fingerprint"]
    target = float(entry["bed_target"])
    kept, replaced, evicted = [], [], []
    for other in store["entries"]:
        if other.get("config_fingerprint") != fingerprint:
            evicted.append(other.get("id"))
        elif (abs(float(other["bed_target"]) - target) <= float(bed_temp_tolerance)
              and bounds_cover(entry["bounds"], other["bounds"],
                               count=entry.get("probe_count"))):
            replaced.append(other.get("id"))
        else:
            kept.append(other)
    entry = dict(entry)
    entry["id"] = int(store.get("next_id", 1))
    store["next_id"] = entry["id"] + 1
    entry.setdefault("uses", 0)
    entry.setdefault("last_used", entry["saved_at"])
    key = temperature_key(target)
    same = [other for other in kept if temperature_key(other["bed_target"]) == key]
    excess = len(same) + 1 - int(max_per_temperature)
    if excess > 0:
        oldest = sorted(same, key=lambda other: float(other.get("last_used") or 0.0))
        gone = set(id(other) for other in oldest[:excess])
        evicted.extend(other.get("id") for other in oldest[:excess])
        kept = [other for other in kept if id(other) not in gone]
    kept.append(entry)
    store["entries"] = kept
    return {"id": entry["id"], "replaced": replaced, "evicted": evicted}


def store_find(store, entry_id):
    for entry in store["entries"]:
        if entry.get("id") == entry_id:
            return entry
    return None


def store_forget(store, entry_id):
    before = len(store["entries"])
    store["entries"] = [entry for entry in store["entries"]
                        if entry.get("id") != entry_id]
    return before - len(store["entries"])


def store_summary(store):
    return [{
        "id": entry.get("id"),
        "bed_target": entry.get("bed_target"),
        "bounds": [round(float(value), 1) for value in entry["bounds"]],
        "probe_count": entry.get("probe_count"),
        "origin": entry.get("origin"),
        "uses": entry.get("uses", 0),
        "saved_at": entry.get("saved_at"),
        "last_used": entry.get("last_used"),
    } for entry in sorted(store["entries"], key=lambda e: (
        float(e["bed_target"]), bounds_area(e["bounds"])))]


class OcteraMeshReuse:
    """Klipper adapter around the pure decision engine."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.enabled = config.getboolean("enabled", True)
        self.reuse_margin = config.getfloat("reuse_coverage_margin", 1.0, minval=0.0)
        self.bed_temp_tolerance = config.getfloat("bed_temp_tolerance", 3.0, minval=0.0)
        self.thermal_min_absolute = config.getfloat("thermal_min_absolute", 35.0)
        self.thermal_drop_from_mesh = config.getfloat("thermal_drop_from_mesh", 15.0, minval=1.0)
        self.density_epsilon = config.getfloat("density_epsilon", 0.05, minval=0.0)
        self.sanity_max_abs = config.getfloat("sanity_max_abs", 1.0, minval=0.01)
        self.sanity_max_range = config.getfloat("sanity_max_range", 0.80, minval=0.01)
        self.sanity_max_neighbor_jump = config.getfloat(
            "sanity_max_neighbor_jump", 0.50, minval=0.01
        )
        self.sanity_probe_duration_factor = config.getfloat(
            "sanity_probe_duration_factor", 2.0, minval=1.0
        )
        self.sanity_probe_duration_slack = config.getfloat(
            "sanity_probe_duration_slack", 120.0, minval=0.0
        )
        self.mesh_time_base = config.getfloat("mesh_time_base", 55.0, minval=0.0)
        self.mesh_time_per_point = config.getfloat("mesh_time_per_point", 4.5, minval=0.0)
        self.verbose = config.getboolean("verbose", True)
        # Verified reuse (V2): check a stored mesh with a 3x3 on its nodes.
        self.verify_enabled = config.getboolean("verify_enabled", False)
        self.verify_tolerance = config.getfloat(
            "verify_tolerance", 0.05, minval=0.005, maxval=0.2)
        self.verify_min_full_count = config.getint("verify_min_full_count", 5, minval=4)
        # 0 means no age limit (V3): a stored mesh is always checked first.
        self.verify_max_age_hours = config.getfloat("verify_max_age_hours", 0.0, minval=0.0)
        self.max_meshes = config.getint("max_meshes_per_temperature", 6, minval=1)
        self.store_path = config.get(
            "store_path", "/usr/data/printer_data/octera-mesh-store.json")
        self._store_data = None
        self._history_open = None
        self.pending_verify = None
        self.last_verify = None
        self.metadata = None
        self.last_decision = {
            "decision": "CALIBRATE", "reason": "REJECT_METADATA_MISSING"
        }
        self.last_invalidation_reason = "REJECT_METADATA_MISSING"
        self.manual_invalidated = False
        self.validation_state = "NO_VALID_MESH"
        self.pending_validation = None
        self.last_rejection = None
        self.validation_sequence = 0
        self.session_id = self._session_identifier()
        self._timer = None
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)
        self.gcode.register_command(
            "OCTERA_MESH_REUSE_OR_CALIBRATE",
            self.cmd_OCTERA_MESH_REUSE_OR_CALIBRATE,
            desc="Reuse a safe session mesh or run square-max KAMP calibration",
        )
        self.gcode.register_command(
            "OCTERA_MESH_METADATA_CAPTURE",
            self.cmd_OCTERA_MESH_METADATA_CAPTURE,
            desc="Capture lateral Octera metadata for the loaded physical mesh",
        )
        self.gcode.register_command(
            "OCTERA_MESH_VERIFY_FINISH",
            self.cmd_OCTERA_MESH_VERIFY_FINISH,
            desc="Compare the verification probes with the stored mesh",
        )
        self.gcode.register_command(
            "OCTERA_MESH_INVALIDATE",
            self.cmd_OCTERA_MESH_INVALIDATE,
            desc="Explicitly invalidate the current Octera mesh session",
        )
        self.gcode.register_command(
            "OCTERA_MESH_REUSE_STATUS",
            self.cmd_OCTERA_MESH_REUSE_STATUS,
            desc="Report Octera mesh metadata and the last reuse decision",
        )
        self.gcode.register_command(
            "OCTERA_MESH_HISTORY",
            self.cmd_OCTERA_MESH_HISTORY,
            desc="List the stored meshes and the last mesh decisions",
        )

    def _session_identifier(self):
        try:
            with open("/proc/sys/kernel/random/boot_id", "r") as stream:
                boot_id = stream.read().strip()
        except Exception:
            boot_id = "unknown-boot"
        material = "%s|%s|%s" % (boot_id, os.getpid(), self.reactor.monotonic())
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]

    def _handle_ready(self):
        self._timer = self.reactor.register_timer(
            self._thermal_timer, self.reactor.monotonic() + 1.0
        )
        logging.info(
            "octera_mesh_reuse: ready schema=%s session=%s",
            SCHEMA, self.session_id,
        )

    def _handle_disconnect(self):
        self.metadata = None
        self.last_invalidation_reason = "REJECT_THERMAL_SESSION"
        self.validation_state = "INVALIDATED"
        self.pending_validation = None

    def _thermal_timer(self, eventtime):
        try:
            if self.metadata and self.metadata.get("thermal_valid"):
                heater = self.printer.lookup_object("heater_bed")
                status = heater.get_status(eventtime)
                temperature = float(status.get("temperature", 0.0))
                threshold = max(
                    self.thermal_min_absolute,
                    float(self.metadata["bed_temperature"])
                    - self.thermal_drop_from_mesh,
                )
                if temperature < threshold:
                    self.metadata["thermal_valid"] = False
                    self.metadata["thermal_invalidated_at"] = time.time()
                    self.metadata["thermal_threshold"] = threshold
                    self.last_invalidation_reason = "REJECT_THERMAL_SESSION"
                    self.validation_state = "INVALIDATED"
                    logging.info(
                        "octera_mesh_reuse: thermal session invalidated "
                        "bed=%.2f threshold=%.2f", temperature, threshold,
                    )
        except Exception:
            logging.exception("octera_mesh_reuse: thermal monitor failed safe")
            if self.metadata:
                self.metadata["thermal_valid"] = False
            self.last_invalidation_reason = "REJECT_UNKNOWN"
            self.validation_state = "INVALIDATED"
        return eventtime + 2.0

    def _policy(self):
        return {
            "bed_temp_tolerance": self.bed_temp_tolerance,
            "density_epsilon": self.density_epsilon,
            "sanity_max_abs": self.sanity_max_abs,
            "sanity_max_range": self.sanity_max_range,
            "sanity_max_neighbor_jump": self.sanity_max_neighbor_jump,
            "sanity_probe_duration_factor": self.sanity_probe_duration_factor,
            "sanity_probe_duration_slack": self.sanity_probe_duration_slack,
        }

    def _settings(self, eventtime):
        configfile = self.printer.lookup_object("configfile")
        return configfile.get_status(eventtime).get("settings", {})

    def _kamp_status(self, eventtime):
        macro = self.printer.lookup_object("gcode_macro _KAMP_Settings", None)
        return macro.get_status(eventtime) if macro is not None else {}

    def _fingerprint_components(self, eventtime):
        settings = self._settings(eventtime)
        bed = settings.get("bed_mesh", {})
        probe = settings.get("prtouch_v2", {})
        kamp = self._kamp_status(eventtime)
        bed_keys = (
            "mesh_min", "mesh_max", "probe_count", "mesh_pps", "algorithm",
            "bicubic_tension", "fade_start", "fade_end", "horizontal_move_z",
            "split_delta_z", "move_check_distance", "speed",
        )
        probe_keys = (
            "pr_version", "step_base", "pres_cnt", "noz_ex_com",
            "tilt_corr_dis", "speeds", "best_above_z", "pa_clr_down_mm",
            "rdy_xy_spd", "tri_min_hold", "tri_max_hold", "tri_hftr_cut",
            "need_self_check", "z_high_default", "min_z_pos",
        )
        kamp_keys = (
            "mesh_margin", "fuzz_amount", "adaptive_min_probe_count",
            "adaptive_bicubic_min_probe_count", "adaptive_max_probe_count",
            "prtouch_v1_square_safe", "prtouch_v1_square_min_probe_count",
            "prtouch_v1_square_max_probe_count", "octera_odd_probe_count",
            "octera_kamp_backend",
            "octera_rectangular_backend", "octera_full_bed_fallback",
            "reuse_loaded_mesh", "reuse_schema", "reuse_coverage_margin",
            "reuse_bed_temp_tolerance", "reuse_thermal_min_absolute",
            "reuse_thermal_drop_from_mesh", "reuse_density_epsilon",
            "reuse_sanity_max_abs", "reuse_sanity_max_range",
            "reuse_sanity_max_neighbor_jump",
            "reuse_sanity_probe_duration_factor",
            "reuse_sanity_probe_duration_slack",
        )
        return {
            "schema": SCHEMA,
            "bed_mesh": {key: bed.get(key) for key in bed_keys},
            "prtouch_v2": {key: probe.get(key) for key in probe_keys},
            "kamp": {key: kamp.get(key) for key in kamp_keys},
            "reuse_policy": {
                "reuse_coverage_margin": self.reuse_margin,
                "bed_temp_tolerance": self.bed_temp_tolerance,
                "thermal_min_absolute": self.thermal_min_absolute,
                "thermal_drop_from_mesh": self.thermal_drop_from_mesh,
                "density_epsilon": self.density_epsilon,
                "sanity_max_abs": self.sanity_max_abs,
                "sanity_max_range": self.sanity_max_range,
                "sanity_max_neighbor_jump": self.sanity_max_neighbor_jump,
                "sanity_probe_duration_factor": self.sanity_probe_duration_factor,
                "sanity_probe_duration_slack": self.sanity_probe_duration_slack,
            },
        }

    def _config_fingerprint(self, eventtime):
        components = self._fingerprint_components(eventtime)
        return stable_fingerprint(components), components

    def _mesh_candidate(self, eventtime):
        status = self.printer.lookup_object("bed_mesh").get_status(eventtime)
        profile_name = status.get("profile_name", "")
        profile = (status.get("profiles") or {}).get(profile_name, {})
        params = profile.get("mesh_params", {}) if profile else {}
        algorithm = params.get("algo") or params.get("algorithm")
        return {
            "profile_name": profile_name,
            "bounds": [
                float((status.get("mesh_min") or [0.0, 0.0])[0]),
                float((status.get("mesh_min") or [0.0, 0.0])[1]),
                float((status.get("mesh_max") or [0.0, 0.0])[0]),
                float((status.get("mesh_max") or [0.0, 0.0])[1]),
            ],
            "probed_matrix": status.get("probed_matrix") or [],
            "algorithm": str(algorithm or "").lower(),
        }

    @staticmethod
    def _candidate_has_mesh(candidate):
        matrix = candidate.get("probed_matrix") or []
        return bool(
            candidate.get("profile_name")
            or any(bool(row) for row in matrix if isinstance(row, (list, tuple)))
        )

    def _begin_validation(self, candidate, request, origin, trigger_reason):
        self.validation_sequence += 1
        self.metadata = None
        self.manual_invalidated = False
        self.validation_state = "PENDING_VALIDATION"
        self.last_invalidation_reason = trigger_reason
        self.pending_validation = {
            "sequence": self.validation_sequence,
            "started_at_unix": time.time(),
            "started_at_monotonic": self.reactor.monotonic(),
            "origin": origin,
            "trigger_reason": trigger_reason,
            "previous_candidate": _plain(candidate),
            "request": _plain(request),
        }

    def _reject_sanity_and_clear(self, gcmd, candidate, sanity):
        evidence = {
            "schema": SCHEMA,
            "sequence": (
                self.pending_validation.get("sequence")
                if self.pending_validation else None
            ),
            "rejected_at_unix": time.time(),
            "candidate": _plain(candidate),
            "sanity": _plain(sanity),
            "pending_validation": _plain(self.pending_validation),
            "metadata_valid_before_clear": False,
            "clear_command_count": 1,
            "retry_calibration_count": 0,
            "purge_or_extrusion_allowed": False,
            "clear_confirmed": False,
        }
        self.metadata = None
        self.last_rejection = evidence
        self._history_close("REJECTED", {
            "failure_codes": list(sanity.get("failure_codes") or [])})
        logging.error(
            "OCTERA_MESH_REJECTED_EVIDENCE_PENDING_CLEAR %s",
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        )
        try:
            self.gcode.run_script_from_command("BED_MESH_CLEAR")
        except Exception as exc:
            evidence["clear_error"] = "%s: %s" % (type(exc).__name__, exc)
            self.validation_state = "REJECTED_CLEAR_FAILED"
            self.last_invalidation_reason = "REJECT_SANITY_CLEAR_FAILED"
            self.pending_validation = None
            logging.exception(
                "OCTERA_MESH_REJECTED_EVIDENCE_CLEAR_FAILED %s",
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            )
            raise gcmd.error(
                "REJECT_SANITY_CLEAR_FAILED: unsafe mesh rejected; "
                "BED_MESH_CLEAR failed; print aborted"
            )
        after_clear = self._mesh_candidate(self.reactor.monotonic())
        evidence["after_clear"] = _plain(after_clear)
        evidence["clear_confirmed"] = not self._candidate_has_mesh(after_clear)
        if not evidence["clear_confirmed"]:
            self.validation_state = "REJECTED_CLEAR_FAILED"
            self.last_invalidation_reason = "REJECT_SANITY_CLEAR_FAILED"
            self.pending_validation = None
            logging.error(
                "OCTERA_MESH_REJECTED_EVIDENCE_CLEAR_UNCONFIRMED %s",
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            )
            raise gcmd.error(
                "REJECT_SANITY_CLEAR_FAILED: BED_MESH_CLEAR returned but "
                "the rejected mesh remains active; print aborted"
            )
        evidence["external_reason"] = "REJECT_SANITY_MESH_CLEARED"
        self.validation_state = "REJECTED_MESH_CLEARED"
        self.last_invalidation_reason = "REJECT_SANITY_MESH_CLEARED"
        self.pending_validation = None
        logging.error(
            "OCTERA_MESH_REJECTED_EVIDENCE_FINAL %s",
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        )
        raise gcmd.error(
            "REJECT_SANITY_MESH_CLEARED: unsafe mesh rejected and cleared; "
            "print aborted before purge/extrusion; no automatic retry; sanity=%s"
            % sanity
        )

    def _request(self, eventtime, bed_target):
        settings = self._settings(eventtime)
        bed_settings = settings.get("bed_mesh", {})
        bed_min = bed_settings.get("mesh_min", [15.0, 15.0])
        bed_max = bed_settings.get("mesh_max", [285.0, 285.0])
        probe_count = bed_settings.get("probe_count", [9, 9])
        kamp = self._kamp_status(eventtime)
        geometry = required_bounds(
            self.printer.lookup_object("exclude_object").get_status(eventtime).get("objects", []),
            bed_min, bed_max,
            kamp.get("mesh_margin", 5.0), self.reuse_margin,
        )
        plan = square_probe_plan(
            geometry["required"], bed_min, bed_max, probe_count,
            minimum=kamp.get("prtouch_v1_square_min_probe_count", 4),
            maximum=kamp.get("prtouch_v1_square_max_probe_count", 0),
            adaptive_min=kamp.get("adaptive_min_probe_count", 3),
            bicubic_min=kamp.get("adaptive_bicubic_min_probe_count", 4),
            adaptive_max=kamp.get("adaptive_max_probe_count", 0),
            square_safe=kamp.get("prtouch_v1_square_safe", 0),
            odd=kamp.get("octera_odd_probe_count", 0),
        )
        heater = self.printer.lookup_object("heater_bed").get_status(eventtime)
        fingerprint, components = self._config_fingerprint(eventtime)
        print_state = self.printer.lookup_object("print_stats").get_status(eventtime)
        toolhead = self.printer.lookup_object("toolhead").get_status(eventtime)
        return {
            "bed_target": float(bed_target),
            "bed_temperature": float(heater.get("temperature", 0.0)),
            "required_bounds": geometry["required"],
            "kamp_bounds": geometry["kamp"],
            "object_bounds": geometry["object"],
            "object_count": geometry["object_count"],
            "baseline_spacing": plan["baseline_spacing"],
            "required_probe_count": plan["count"],
            "required_probe_points": plan["points"],
            "estimated_probe_seconds": (
                self.mesh_time_base + plan["points"] * self.mesh_time_per_point
            ),
            "session_id": self.session_id,
            "config_fingerprint": fingerprint,
            "fingerprint_components": components,
            "printer_state": print_state.get("state", "unknown"),
            "homed_axes": toolhead.get("homed_axes", ""),
            "manual_invalidated": self.manual_invalidated,
        }

    def _respond_decision(self, gcmd, candidate, request, decision):
        seconds = int(round(request["estimated_probe_seconds"]))
        probes = request["required_probe_points"]
        if self.verbose:
            sanity = decision.get("sanity") or {}
            lines = [
                "OCTERA KAMP MESH REUSE",
                "Decision: %s" % decision["decision"],
                "Reason: %s" % decision["reason"],
                "Mesh physical probes: %sx%s" % tuple(sanity.get("count", [0, 0])),
                "Loaded bounds: %s" % candidate.get("bounds"),
                "Required bounds: %s" % [round(v, 3) for v in request["required_bounds"]],
                "Required square probes: %sx%s (%s points)" % (
                    request["required_probe_count"][0],
                    request["required_probe_count"][1], probes,
                ),
                "Bed temperature: %.2f / mesh %s C" % (
                    request["bed_temperature"],
                    "n/a" if not self.metadata else "%.2f" % self.metadata["bed_temperature"],
                ),
                "Config fingerprint: %s" % request["config_fingerprint"][:16],
                "Estimated probe saving: ~%dm%02ds" % (seconds // 60, seconds % 60),
            ]
            if sanity and all(
                sanity.get(key) is not None
                for key in ("min", "max", "mean", "range", "max_neighbor_jump")
            ):
                lines.append(
                    "Sanity: min=%.4f max=%.4f mean=%.4f range=%.4f neighbor=%.4f" % (
                        sanity["min"], sanity["max"], sanity["mean"],
                        sanity["range"], sanity["max_neighbor_jump"],
                    )
                )
            gcmd.respond_info("\n".join(lines))
        else:
            gcmd.respond_info(
                "OCTERA_MESH_REUSE decision=%s reason=%s" % (
                    decision["decision"], decision["reason"]
                )
            )

    def _restore_session_profile(self, eventtime):
        """Reload the profile the session metadata belongs to.

        Creality's homing path loads the "default" profile, so by the time a
        print reaches the dispatcher the active mesh is usually the boot
        full-bed mesh, not the adaptive one the metadata describes. The
        metadata gates still decide whether the reloaded mesh is reused.
        """
        meta = self.metadata
        if not meta or not meta.get("thermal_valid") or self.manual_invalidated:
            return False
        name = meta.get("profile_name")
        status = self.printer.lookup_object("bed_mesh").get_status(eventtime)
        if (not name or name == status.get("profile_name")
                or name not in (status.get("profiles") or {})):
            return False
        self.gcode.run_script_from_command("BED_MESH_PROFILE LOAD=%s" % name)
        return True

    def cmd_OCTERA_MESH_REUSE_OR_CALIBRATE(self, gcmd):
        eventtime = self.reactor.monotonic()
        bed_target = gcmd.get_float("BED_TARGET", minval=1.0)
        origin = gcmd.get("ORIGIN", "adaptive").lower()
        self.last_verify = None
        if self.enabled and self._restore_session_profile(eventtime):
            eventtime = self.reactor.monotonic()
        candidate = self._mesh_candidate(eventtime)
        request = self._request(eventtime, bed_target)
        kamp_enabled = int(self._kamp_status(eventtime).get("reuse_loaded_mesh", 1)) == 1
        if self.enabled and kamp_enabled:
            decision = evaluate_reuse(
                candidate, self.metadata, request, self._policy()
            )
        else:
            decision = {
                "decision": "CALIBRATE", "reason": "REJECT_UNKNOWN",
                "gates": {"enabled": False},
            }
        decision.update({
            "timestamp": time.time(),
            "candidate": candidate,
            "request": request,
        })
        self.last_decision = decision
        self._respond_decision(gcmd, candidate, request, decision)
        if decision["decision"] == "REUSE":
            gcmd.respond_info("REUSING LOADED MESH")
            self._history_add({
                "result": "REUSED_SESSION", "reason": decision["reason"],
                "origin": origin, "bed_target": float(bed_target),
                "required": request.get("required_bounds"), "probes": 0,
            })
            return
        if (origin == "adaptive" and self.enabled and kamp_enabled
                and self.verify_enabled
                and decision["reason"] in VERIFIABLE_REASONS):
            plan = self._verify_plan(request, bed_target)
            self.last_verify = {"timestamp": time.time(),
                                "plan": self._plan_summary(plan)}
            if plan["ok"]:
                self._start_verification(gcmd, candidate, request, plan, bed_target)
                return
            gcmd.respond_info("OCTERA MESH VERIFY skipped: %s" % plan["reason"])
        self._start_calibration(gcmd, candidate, request, origin,
                                decision["reason"], bed_target, eventtime)

    def _start_calibration(self, gcmd, candidate, request, origin, reason,
                           bed_target, eventtime):
        if origin == "full-bed":
            # The stock full-bed routine probes the configured grid, not the
            # adaptive plan; validate the result against what it measures.
            request = dict(request)
            probe_count = self._settings(eventtime).get(
                "bed_mesh", {}).get("probe_count", [9, 9])
            if len(probe_count) == 1:
                probe_count = [probe_count[0], probe_count[0]]
            full = [int(probe_count[0]), int(probe_count[1])]
            request["required_probe_count"] = full
            request["required_probe_points"] = full[0] * full[1]
            request["estimated_probe_seconds"] = (
                self.mesh_time_base
                + full[0] * full[1] * self.mesh_time_per_point
            )
            gcmd.respond_info("RUNNING FULL-BED CALIBRATION")
        else:
            gcmd.respond_info("RUNNING ADAPTIVE CALIBRATION")
        self._begin_validation(candidate, request, origin, reason)
        item = {
            "result": "CALIBRATING", "reason": reason, "origin": origin,
            "bed_target": float(bed_target),
            "required": request.get("required_bounds"),
            "probes": int(request.get("required_probe_points") or 0),
        }
        verify = getattr(self, "last_verify", None) or {}
        if str(reason).startswith("VERIFY_") and verify.get("result"):
            item["deviation"] = verify["result"].get("max_deviation")
            item["probes"] += len(verify["result"].get("deviations") or [])
            item["checked_id"] = (verify.get("plan") or {}).get("entry_id")
        elif verify.get("plan") and not (verify["plan"] or {}).get("ok"):
            item["verify_skipped"] = verify["plan"].get("reason")
        self._history_open = self._history_add(item)
        if origin == "full-bed":
            # CX_PRINT_LEVELING_CALIBRATION calls BED_MESH_CALIBRATE, which KAMP
            # overrides with the adaptive macro, so it never measured a full
            # bed and then loaded the stale boot "default" profile. Call the
            # stock calibration (config mesh_min/max and probe_count) directly.
            script = "\n".join([
                "BED_MESH_CLEAR",
                "_BED_MESH_CALIBRATE PROFILE=default",
                "BED_MESH_PROFILE LOAD=default",
                "OCTERA_MESH_METADATA_CAPTURE BED_TARGET=%.3f ORIGIN=full-bed" % bed_target,
            ])
        else:
            script = "\n".join([
                "BED_MESH_CLEAR",
                "BED_MESH_CALIBRATE",
                "OCTERA_MESH_METADATA_CAPTURE BED_TARGET=%.3f ORIGIN=adaptive" % bed_target,
                "BED_MESH_PROFILE SAVE=adaptive",
                "BED_MESH_PROFILE LOAD=adaptive",
            ])
        self.gcode.run_script_from_command(script)

    def cmd_OCTERA_MESH_METADATA_CAPTURE(self, gcmd):
        eventtime = self.reactor.monotonic()
        bed_target = gcmd.get_float("BED_TARGET", minval=1.0)
        origin = gcmd.get("ORIGIN", "adaptive").lower()
        candidate = self._mesh_candidate(eventtime)
        pending = self.pending_validation or {}
        request = pending.get("request") or {}
        started = pending.get("started_at_monotonic")
        duration = None if started is None else max(0.0, eventtime - float(started))
        sanity = mesh_sanity(
            candidate,
            max_abs=self.sanity_max_abs,
            max_range=self.sanity_max_range,
            max_neighbor_jump=self.sanity_max_neighbor_jump,
            expected_count=request.get("required_probe_count"),
            probe_duration_sec=duration,
            expected_duration_sec=request.get("estimated_probe_seconds"),
            duration_factor=self.sanity_probe_duration_factor,
            duration_slack=self.sanity_probe_duration_slack,
        )
        if not sanity["pass"]:
            self._reject_sanity_and_clear(gcmd, candidate, sanity)
        heater = self.printer.lookup_object("heater_bed").get_status(eventtime)
        fingerprint, components = self._config_fingerprint(eventtime)
        self.metadata = {
            "schema": SCHEMA,
            "origin": origin,
            "profile_name": candidate["profile_name"],
            "bounds": candidate["bounds"],
            "probe_count": sanity["count"],
            "spacing": sanity["spacing"],
            "bed_target": float(bed_target),
            "bed_temperature": float(heater.get("temperature", 0.0)),
            "creation_timestamp": time.time(),
            "session_id": self.session_id,
            "thermal_valid": True,
            "config_fingerprint": fingerprint,
            "fingerprint_components": components,
            "sanity": sanity,
            "kamp_version": SCHEMA,
        }
        self.manual_invalidated = False
        self.last_invalidation_reason = "REUSE_PASS"
        self.validation_state = "VALID_FOR_REUSE"
        self.pending_validation = None
        stored = self._write_store(eventtime, candidate, sanity, bed_target,
                                   fingerprint, origin) or {}
        self._history_close("PROBED", {
            "mesh_id": stored.get("id"),
            "replaced": stored.get("replaced") or [],
            "evicted": stored.get("evicted") or [],
            "count": sanity["count"],
        })
        gcmd.respond_info(
            "OCTERA_MESH_METADATA_CAPTURED origin=%s probes=%sx%s "
            "bounds=%s bed=%.2f fingerprint=%s stored_id=%s replaced=%s" % (
                origin, sanity["count"][0], sanity["count"][1],
                candidate["bounds"], self.metadata["bed_temperature"],
                fingerprint[:16], stored.get("id"), stored.get("replaced") or [],
            )
        )

    # ----- Mesh store and verified reuse (V2/V3) ---------------------------
    def _store(self):
        """The store, read from disk once per process and kept in memory."""
        if getattr(self, "_store_data", None) is None:
            self._store_data = self._load_store()
        return self._store_data

    def _load_store(self):
        try:
            with open(self.store_path, "r") as stream:
                data = json.load(stream)
        except Exception:
            data = None
        return normalize_store(data)

    def _save_store(self):
        try:
            store = self._store()
            payload = {
                "schema": STORE_SCHEMA,
                "next_id": store.get("next_id", 1),
                "entries": _plain(store["entries"]),
                "history": _plain(store["history"][-HISTORY_LIMIT:]),
            }
            tmp = self.store_path + ".tmp"
            with open(tmp, "w") as stream:
                json.dump(payload, stream, sort_keys=True)
            os.replace(tmp, self.store_path)
        except Exception:
            logging.exception("octera_mesh_reuse: could not write mesh store")

    def _history_add(self, item):
        """Append one decision to the persistent history; never raises."""
        try:
            store = self._store()
            history = store["history"]
            sequence = int(history[-1].get("seq", 0) or 0) + 1 if history else 1
            entry = {"seq": sequence, "time": round(time.time(), 1)}
            entry.update(_plain(item))
            if entry.get("required"):
                entry["required"] = [round(float(v), 1) for v in entry["required"]]
            if isinstance(entry.get("deviation"), float):
                entry["deviation"] = round(entry["deviation"], 4)
            history.append(entry)
            del history[:-HISTORY_LIMIT]
            self._save_store()
            logging.info("octera_mesh_reuse: history %s", json.dumps(entry, sort_keys=True))
            return sequence
        except Exception:
            logging.exception("octera_mesh_reuse: could not record history")
            return None

    def _history_close(self, result, fields):
        """Finish the open CALIBRATING item, or add one if none is open."""
        try:
            sequence = getattr(self, "_history_open", None)
            self._history_open = None
            store = self._store()
            for item in reversed(store["history"]):
                if sequence is not None and item.get("seq") == sequence:
                    item["result"] = result
                    item.update(_plain(fields))
                    self._save_store()
                    return
            item = {"result": result}
            item.update(fields)
            self._history_add(item)
        except Exception:
            logging.exception("octera_mesh_reuse: could not close history item")

    def _write_store(self, eventtime, candidate, sanity, bed_target, fingerprint,
                     origin="adaptive"):
        """Keep an accepted mesh so a later print can verify and reuse it."""
        try:
            status = self.printer.lookup_object("bed_mesh").get_status(eventtime)
            profile = (status.get("profiles") or {}).get(candidate["profile_name"]) or {}
            if not profile.get("points") or not profile.get("mesh_params"):
                return None
            heater = self.printer.lookup_object("heater_bed").get_status(eventtime)
            now = time.time()
            entry = {
                "saved_at": now,
                "last_used": now,
                "uses": 0,
                "origin": origin,
                "profile_name": candidate["profile_name"],
                "bounds": candidate["bounds"],
                "probe_count": sanity["count"],
                "spacing": sanity["spacing"],
                "points": _plain(profile["points"]),
                "mesh_params": _plain(profile["mesh_params"]),
                "algorithm": candidate.get("algorithm"),
                "bed_target": float(bed_target),
                "bed_temperature": float(heater.get("temperature", 0.0)),
                "config_fingerprint": fingerprint,
            }
            if not entry_is_wellformed(entry):
                return None
            result = store_add_entry(
                self._store(), entry, getattr(self, "max_meshes", 6),
                self.bed_temp_tolerance)
            self._save_store()
            return result
        except Exception:
            logging.exception("octera_mesh_reuse: could not write mesh store")
            return None

    def _drop_store(self, reason, bed_target=None):
        """Drop stored meshes (all, or one temperature); history is kept."""
        try:
            if os.path.exists(self.store_path):
                with open(self.store_path, "rb") as source:
                    data = source.read()
                with open(self.store_path + ".invalidated", "wb") as target:
                    target.write(data)
            store = self._store()
            before = len(store["entries"])
            if bed_target is None:
                store["entries"] = []
            else:
                key = temperature_key(bed_target)
                store["entries"] = [entry for entry in store["entries"]
                                    if temperature_key(entry["bed_target"]) != key]
            removed = before - len(store["entries"])
            self._history_add({
                "result": "INVALIDATED", "reason": reason, "removed": removed,
                "bed_target": None if bed_target is None else float(bed_target),
            })
            return removed
        except Exception:
            logging.exception("octera_mesh_reuse: could not drop mesh store (%s)", reason)
            return 0

    def _check_entry(self, entry, request, bed_target, now):
        """Why a stored mesh cannot be verified for this request, or its plan."""
        if self.verify_max_age_hours:
            age_hours = (now - float(entry.get("saved_at", 0))) / 3600.0
            if age_hours > self.verify_max_age_hours:
                return "VERIFY_STORE_TOO_OLD", None
        if entry.get("config_fingerprint") != request.get("config_fingerprint"):
            return "VERIFY_CONFIG_DRIFT", None
        if abs(float(entry.get("bed_target", -1000)) - float(bed_target)) > self.bed_temp_tolerance:
            return "VERIFY_TEMP", None
        mesh = {"bounds": entry["bounds"], "probed_matrix": entry["points"],
                "algorithm": entry.get("algorithm")}
        sanity = mesh_sanity(
            mesh, max_abs=self.sanity_max_abs, max_range=self.sanity_max_range,
            max_neighbor_jump=self.sanity_max_neighbor_jump,
            expected_count=entry.get("probe_count"),
        )
        if not sanity["pass"]:
            return "VERIFY_STORE_SANITY", None
        baseline = request["baseline_spacing"]
        if (sanity["spacing"][0] > float(baseline[0]) + self.density_epsilon
                or sanity["spacing"][1] > float(baseline[1]) + self.density_epsilon):
            return "VERIFY_DENSITY", None
        grid = verify_helpers.select_verify_grid(
            entry["bounds"], entry["probe_count"], request["required_bounds"])
        if grid is None:
            return "VERIFY_NOT_COVERED", None
        return None, {"grid": grid, "store_sanity": sanity}

    def _verify_plan(self, request, bed_target):
        """Pick the stored mesh to check instead of probing the whole area.

        Among the stored meshes that pass every gate and cover the part, the
        smallest was probed closest to it; ties go to the most recently used.
        """
        plan = {"ok": False, "reason": "VERIFY_NO_STORE"}
        if int(request["required_probe_count"][0]) < self.verify_min_full_count:
            plan["reason"] = "VERIFY_NOT_WORTH_IT"
            return plan
        entries = self._store()["entries"]
        if not entries:
            return plan
        now = time.time()
        refusals, usable = {}, []
        for entry in entries:
            try:
                reason, checked = self._check_entry(entry, request, bed_target, now)
            except Exception:
                logging.exception("octera_mesh_reuse: stored mesh %s unusable", entry.get("id"))
                reason, checked = "VERIFY_STORE_ERROR", None
            if reason:
                refusals[reason] = refusals.get(reason, 0) + 1
            else:
                usable.append((entry, checked))
        plan["refusals"] = refusals
        if not usable:
            plan["reason"] = (list(refusals)[0] if len(refusals) == 1
                              else "VERIFY_NO_MATCH")
            return plan
        entry, checked = min(usable, key=lambda pair: (
            bounds_area(pair[0]["bounds"]), -float(pair[0].get("last_used") or 0.0)))
        plan.update({"ok": True, "reason": "VERIFY_PLANNED", "grid": checked["grid"],
                     "store": entry, "entry_id": entry.get("id"),
                     "store_sanity": checked["store_sanity"],
                     "store_age_hours": (now - float(entry["saved_at"])) / 3600.0,
                     "candidates": len(usable)})
        return plan

    def _start_verification(self, gcmd, candidate, request, plan, bed_target):
        grid = plan["grid"]
        self.pending_verify = {
            "candidate": candidate, "request": request, "plan": plan,
            "bed_target": float(bed_target),
            "started_at_monotonic": self.reactor.monotonic(),
        }
        x0, y0, x1, y1 = grid["bounds"]
        gcmd.respond_info(
            "RUNNING MESH VERIFICATION %dx%d on stored mesh #%s %sx%s "
            "(age %.1f h, %d candidate(s))" % (
                grid["size"], grid["size"], plan.get("entry_id"),
                plan["store"]["probe_count"][0], plan["store"]["probe_count"][1],
                plan["store_age_hours"], plan.get("candidates", 1)))
        self.gcode.run_script_from_command("\n".join([
            "BED_MESH_CLEAR",
            "_BED_MESH_CALIBRATE MESH_MIN=%.3f,%.3f MESH_MAX=%.3f,%.3f "
            "PROBE_COUNT=%d,%d ALGORITHM=lagrange PROFILE=octera_verify" % (
                x0, y0, x1, y1, grid["size"], grid["size"]),
            "OCTERA_MESH_VERIFY_FINISH",
        ]))

    def _install_profile(self, name, points, mesh_params):
        bed_mesh = self.printer.lookup_object("bed_mesh")
        bed_mesh.pmgr.profiles[name] = {
            "points": [list(row) for row in points],
            "mesh_params": dict(mesh_params),
        }
        self.gcode.run_script_from_command("BED_MESH_PROFILE LOAD=%s" % name)

    @staticmethod
    def _plan_summary(plan):
        return _plain({k: v for k, v in plan.items() if k != "store"})

    def cmd_OCTERA_MESH_VERIFY_FINISH(self, gcmd):
        pending = self.pending_verify
        self.pending_verify = None
        if not pending:
            raise gcmd.error("OCTERA_MESH_VERIFY_FINISH without a pending verification")
        eventtime = self.reactor.monotonic()
        plan = pending["plan"]
        store = plan["store"]
        checked = self._mesh_candidate(eventtime)
        probed = checked.get("probed_matrix") or []
        probed_bounds = checked.get("bounds") or plan["grid"]["bounds"]
        if probed_bounds[2] <= probed_bounds[0] or probed_bounds[3] <= probed_bounds[1]:
            probed_bounds = plan["grid"]["bounds"]
        result = verify_helpers.compare_verify(
            store["points"], store["bounds"], probed, probed_bounds,
            self.verify_tolerance)
        result["probed_bounds"] = [round(float(v), 4) for v in probed_bounds]
        result["duration_sec"] = eventtime - pending["started_at_monotonic"]
        self.last_verify = {"timestamp": time.time(), "plan": self._plan_summary(plan),
                            "result": _plain(result)}
        logging.info("octera_mesh_reuse: verify %s", json.dumps(self.last_verify, sort_keys=True))
        gcmd.respond_info(
            "OCTERA MESH VERIFY %s mesh #%s max_deviation=%s tolerance=%.3f" % (
                result["reason"], plan.get("entry_id"),
                "n/a" if result["max_deviation"] is None else "%.4f" % result["max_deviation"],
                self.verify_tolerance))
        if not result["pass"]:
            # The bed no longer matches this mesh: it is not offered again.
            if store_forget(self._store(), plan.get("entry_id")):
                self._save_store()
                gcmd.respond_info("OCTERA MESH stored mesh #%s dropped" % plan.get("entry_id"))
            self._start_calibration(
                gcmd, pending["candidate"], pending["request"], "adaptive",
                result["reason"], pending["bed_target"], eventtime)
            return
        self._install_profile(VERIFIED_PROFILE, store["points"], store["mesh_params"])
        eventtime = self.reactor.monotonic()
        heater = self.printer.lookup_object("heater_bed").get_status(eventtime)
        fingerprint, components = self._config_fingerprint(eventtime)
        candidate = self._mesh_candidate(eventtime)
        self.metadata = {
            "schema": SCHEMA,
            "origin": "verified",
            "profile_name": candidate["profile_name"],
            "bounds": candidate["bounds"],
            "probe_count": store["probe_count"],
            "spacing": plan["store_sanity"]["spacing"],
            "bed_target": pending["bed_target"],
            "bed_temperature": float(heater.get("temperature", 0.0)),
            "creation_timestamp": time.time(),
            "session_id": self.session_id,
            "thermal_valid": True,
            "config_fingerprint": fingerprint,
            "fingerprint_components": components,
            "sanity": plan["store_sanity"],
            "kamp_version": SCHEMA,
            "store_id": plan.get("entry_id"),
            "verify": result,
        }
        self.manual_invalidated = False
        self.last_invalidation_reason = "REUSE_PASS"
        self.validation_state = "VALID_FOR_REUSE"
        entry = store_find(self._store(), plan.get("entry_id"))
        if entry is not None:
            entry["uses"] = int(entry.get("uses", 0) or 0) + 1
            entry["last_used"] = time.time()
        self._history_add({
            "result": "REUSED_VERIFIED", "reason": result["reason"],
            "origin": "adaptive", "bed_target": pending["bed_target"],
            "required": pending["request"].get("required_bounds"),
            "mesh_id": plan.get("entry_id"), "deviation": result["max_deviation"],
            "probes": len(result.get("deviations") or []),
            "count": store["probe_count"],
        })
        gcmd.respond_info("REUSING VERIFIED STORED MESH #%s %sx%s bounds=%s" % (
            plan.get("entry_id"), store["probe_count"][0], store["probe_count"][1],
            candidate["bounds"]))

    def cmd_OCTERA_MESH_INVALIDATE(self, gcmd):
        reason = gcmd.get("REASON", "REJECT_MANUAL_INVALIDATION").upper()
        if reason not in REASONS or reason == "REUSE_PASS":
            reason = "REJECT_MANUAL_INVALIDATION"
        bed_temp = gcmd.get_float("BED_TEMP", None)
        self.manual_invalidated = True
        self.last_invalidation_reason = reason
        self.validation_state = "INVALIDATED"
        self.pending_validation = None
        if self.metadata:
            self.metadata["thermal_valid"] = False
            self.metadata["invalidation_reason"] = reason
            self.metadata["invalidated_at"] = time.time()
        removed = self._drop_store(reason, bed_temp)
        gcmd.respond_info("OCTERA_MESH_INVALIDATED reason=%s stored_meshes_dropped=%d%s" % (
            reason, removed, "" if bed_temp is None else " bed_temp=%.0f" % bed_temp))

    def cmd_OCTERA_MESH_REUSE_STATUS(self, gcmd):
        gcmd.respond_info(json.dumps(self.get_status(self.reactor.monotonic()), sort_keys=True))

    def cmd_OCTERA_MESH_HISTORY(self, gcmd):
        store = self._store()
        lines = ["OCTERA MESH STORE: %d mesh(es), check %s, tolerance %.3f mm, "
                 "up to %d per temperature" % (
                     len(store["entries"]),
                     "on" if self.verify_enabled else "off",
                     self.verify_tolerance, getattr(self, "max_meshes", 6))]
        for item in store_summary(store):
            lines.append("#%s %sC %sx%s area %.0f,%.0f-%.0f,%.0f used %s saved %s" % (
                item["id"], temperature_key(item["bed_target"]),
                item["probe_count"][0], item["probe_count"][1],
                item["bounds"][0], item["bounds"][1], item["bounds"][2], item["bounds"][3],
                item["uses"],
                time.strftime("%m-%d %H:%M", time.localtime(float(item["saved_at"])))))
        for item in store["history"][-10:]:
            lines.append("%s %s %s%s%s%s" % (
                time.strftime("%m-%d %H:%M", time.localtime(float(item.get("time", 0)))),
                "" if item.get("bed_target") is None
                else "%sC" % temperature_key(item["bed_target"]),
                item.get("result"),
                "" if item.get("mesh_id") is None else " #%s" % item["mesh_id"],
                "" if item.get("deviation") is None
                else " worst %.3f mm" % float(item["deviation"]),
                "" if not item.get("reason") else " (%s)" % item["reason"]))
        gcmd.respond_info("\n".join(lines))

    def get_status(self, eventtime):
        fingerprint = None
        try:
            fingerprint, _ = self._config_fingerprint(eventtime)
        except Exception:
            logging.exception("octera_mesh_reuse: status fingerprint failed")
        try:
            store = self._store()
            stored, history = store_summary(store), store["history"][-STATUS_HISTORY:]
        except Exception:
            logging.exception("octera_mesh_reuse: status store failed")
            stored, history = [], []
        return {
            "schema": SCHEMA,
            "module_sha256": MODULE_SHA256,
            "enabled": self.enabled,
            "session_id": self.session_id,
            "metadata": _plain(self.metadata),
            "last_decision": _plain(self.last_decision),
            "last_invalidation_reason": self.last_invalidation_reason,
            "manual_invalidated": self.manual_invalidated,
            "validation_state": self.validation_state,
            "pending_validation": _plain(self.pending_validation),
            "last_rejection": _plain(self.last_rejection),
            "verify_enabled": self.verify_enabled,
            "last_verify": _plain(self.last_verify),
            "current_config_fingerprint": fingerprint,
            "policy": _plain(self._policy()),
            "store_schema": STORE_SCHEMA,
            "store_policy": {
                "verify_tolerance": self.verify_tolerance,
                "verify_min_full_count": self.verify_min_full_count,
                "verify_max_age_hours": self.verify_max_age_hours,
                "max_meshes_per_temperature": getattr(self, "max_meshes", 6),
            },
            "stored_meshes": _plain(stored),
            "history": _plain(history),
        }


def load_config(config):
    return OcteraMeshReuse(config)
