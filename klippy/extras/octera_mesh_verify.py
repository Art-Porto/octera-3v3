"""Pure helpers for Octera verified mesh reuse (V3, interpolated check).

A stored mesh is checked by probing a small square grid (3x3) over the part
area plus a margin, clipped to the stored mesh, and comparing each probed
point with the bilinear height of the stored probe matrix at that point
(the method of the K2's octera_mesh_cache). Any stored grid works, odd or
even. When a probed point lands on a stored node the interpolation weight is
zero and the comparison is the node itself, so an odd grid needs no second
path.

On the 3V3 the Creality firmware anchors every mesh at its own area center
(the center reads exactly noz_ex_com, 0.040 mm), so a stored mesh and the
check grid are levelled at different points. The check therefore splits the
differences into a measured offset (their mean) and a shape (each difference
minus the mean). The shape must fit the tolerance; the measured offset is
only a guard against a large change (plate swapped, Z offset changed).

The installed level does not come from the measured offset: the 3x3 is
anchored at its own center too, so the noise of that one point moves the
eight others with it and the mean does not cancel it (2026-11-05: measured
-0.055, true 0.000). A new mesh of the part would read the anchor at the
center of its area, so the stored mesh is shifted by the predicted amount
anchor - bilinear(stored, center of the calibration area), which reproduces
it without any probe noise.

Used by octera_mesh_reuse; see docs/design/MESH_REUSE_V3.md in the
engineering repository.
"""

from __future__ import annotations

import math

VERIFY_MARGIN_MM = 5.0
VERIFY_MAX_OFFSET_MM = 0.50


def rounding_allowance(count):
    """How far bed_mesh may pull mesh_max in from the requested maximum.

    Klipper floors the probe spacing to 0.01 mm, so the last node lands up to
    0.01 mm short per interval: an 8x8 over 217.2 mm ends at 258.54, not
    258.60. Only the high side moves; mesh_min is kept as requested.
    """
    return 0.01 * max(int(count) - 1, 0)


def grid_points(bounds, size):
    """Node coordinates (xs, ys) of a size x size grid over bounds."""
    x0, y0, x1, y1 = [float(v) for v in bounds]
    xs = [x0 + (x1 - x0) * k / (size - 1) for k in range(size)]
    ys = [y0 + (y1 - y0) * k / (size - 1) for k in range(size)]
    return xs, ys


def bilinear(matrix, bounds, x, y):
    """Height of a probe matrix (rows along y) at (x, y), clamped to bounds."""
    x0, y0, x1, y1 = [float(v) for v in bounds]
    rows, cols = len(matrix), len(matrix[0])
    fx = (float(x) - x0) / (x1 - x0) * (cols - 1) if x1 > x0 else 0.0
    fy = (float(y) - y0) / (y1 - y0) * (rows - 1) if y1 > y0 else 0.0
    fx = min(max(fx, 0.0), cols - 1.0)
    fy = min(max(fy, 0.0), rows - 1.0)
    i = min(int(math.floor(fx)), cols - 2)
    j = min(int(math.floor(fy)), rows - 2)
    tx, ty = fx - i, fy - j
    low = float(matrix[j][i]) * (1 - tx) + float(matrix[j][i + 1]) * tx
    high = float(matrix[j + 1][i]) * (1 - tx) + float(matrix[j + 1][i + 1]) * tx
    return low * (1 - ty) + high * ty


def select_verify_grid(stored_bounds, stored_count, required_bounds, size=3,
                       margin=VERIFY_MARGIN_MM, epsilon=0.05):
    """Where to probe the check: the part area plus margin, inside the mesh.

    Returns {"bounds": [x0, y0, x1, y1], "size": n} or None when the stored
    mesh is too coarse to interpolate or does not cover the required area
    (the high side allows bed_mesh's floored spacing, see
    rounding_allowance).
    """
    cols, rows = int(stored_count[0]), int(stored_count[1])
    if cols < 3 or rows < 3:
        return None
    bounds = [0.0] * 4
    for axis, count in ((0, cols), (1, rows)):
        low, high = float(stored_bounds[axis]), float(stored_bounds[axis + 2])
        need_low = float(required_bounds[axis])
        need_high = float(required_bounds[axis + 2])
        if (need_low < low - epsilon
                or need_high > high + max(epsilon, rounding_allowance(count))):
            return None
        bounds[axis] = max(low, need_low - margin)
        bounds[axis + 2] = min(high, need_high + margin)
        if bounds[axis + 2] - bounds[axis] < 1.0:
            return None
    return {"bounds": bounds, "size": int(size)}


def compare_verify(stored_matrix, stored_bounds, verify_matrix, verify_bounds,
                   tolerance, max_offset=VERIFY_MAX_OFFSET_MM):
    """Compare a probed grid with the stored mesh interpolated at its points.

    verify_bounds are the bounds bed_mesh actually probed (mesh_min/max after
    the check), so the floored spacing of the check grid is accounted for.
    deviations and max_deviation are the shape (offset removed); offset is
    the mean difference, to add to the stored mesh when it is installed.
    """
    result = {"pass": False, "max_deviation": None, "deviations": [],
              "offset": None, "method": "bilinear-shape"}
    size = len(verify_matrix)
    if size < 2 or any(len(row) != size for row in verify_matrix):
        result["reason"] = "VERIFY_DIMENSION_MISMATCH"
        return result
    xs, ys = grid_points(verify_bounds, size)
    raw = []
    for r, y in enumerate(ys):
        for c, x in enumerate(xs):
            measured = float(verify_matrix[r][c])
            stored = bilinear(stored_matrix, stored_bounds, x, y)
            if not (math.isfinite(measured) and math.isfinite(stored)):
                result["reason"] = "VERIFY_NONFINITE"
                return result
            raw.append(measured - stored)
    offset = sum(raw) / len(raw)
    shape = [value - offset for value in raw]
    worst = max(abs(value) for value in shape)
    result["offset"] = round(offset, 6)
    result["deviations"] = [round(value, 6) for value in shape]
    result["max_deviation"] = worst
    if abs(offset) > float(max_offset):
        result["reason"] = "VERIFY_OFFSET"
    elif worst > float(tolerance):
        result["reason"] = "VERIFY_DEVIATION"
    else:
        result["pass"] = True
        result["reason"] = "VERIFY_PASS"
    return result


def predicted_shift(stored_matrix, stored_bounds, area_bounds, anchor):
    """Shift that gives the stored mesh the level of a new mesh of the area.

    The firmware sets a new mesh to `anchor` at the center of the area it
    probes; area_bounds are the bounds a new calibration would use.
    """
    cx = (float(area_bounds[0]) + float(area_bounds[2])) / 2.0
    cy = (float(area_bounds[1]) + float(area_bounds[3])) / 2.0
    return float(anchor) - bilinear(stored_matrix, stored_bounds, cx, cy)


def shifted(matrix, offset):
    """The stored matrix with the check offset added to every point."""
    return [[float(value) + float(offset) for value in row] for row in matrix]
