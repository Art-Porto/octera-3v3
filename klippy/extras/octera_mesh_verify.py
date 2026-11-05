"""Pure helpers for Octera verified mesh reuse (V3, interpolated check).

A stored mesh is checked by probing a small square grid (3x3) over the part
area plus a margin, clipped to the stored mesh, and comparing each probed
point with the bilinear height of the stored probe matrix at that point
(the method of the K2's octera_mesh_cache). Any stored grid works, odd or
even. When a probed point lands on a stored node the interpolation weight is
zero and the comparison is the node itself, so an odd grid needs no second
path.

Used by octera_mesh_reuse; see docs/design/MESH_REUSE_V3.md in the
engineering repository.
"""

from __future__ import annotations

import math

VERIFY_MARGIN_MM = 5.0


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
                   tolerance):
    """Compare a probed grid with the stored mesh interpolated at its points.

    verify_bounds are the bounds bed_mesh actually probed (mesh_min/max after
    the check), so the floored spacing of the check grid is accounted for.
    """
    result = {"pass": False, "max_deviation": None, "deviations": [],
              "method": "bilinear"}
    size = len(verify_matrix)
    if size < 2 or any(len(row) != size for row in verify_matrix):
        result["reason"] = "VERIFY_DIMENSION_MISMATCH"
        return result
    xs, ys = grid_points(verify_bounds, size)
    worst = 0.0
    for r, y in enumerate(ys):
        for c, x in enumerate(xs):
            measured = float(verify_matrix[r][c])
            stored = bilinear(stored_matrix, stored_bounds, x, y)
            if not (math.isfinite(measured) and math.isfinite(stored)):
                result["reason"] = "VERIFY_NONFINITE"
                return result
            deviation = measured - stored
            result["deviations"].append(round(deviation, 6))
            worst = max(worst, abs(deviation))
    result["max_deviation"] = worst
    result["pass"] = worst <= float(tolerance)
    result["reason"] = "VERIFY_PASS" if result["pass"] else "VERIFY_DEVIATION"
    return result
