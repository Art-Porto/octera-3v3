"""Pure helpers for Octera verified mesh reuse (V2).

A stored mesh is checked by probing a small square grid (3x3 by default) whose points coincide with
nodes of the stored grid, then comparing node by node. No interpolation is
involved, so the check does not depend on the mesh algorithm.

Not wired into Klipper yet; see docs/design/MESH_REUSE_V2.md in the
engineering repository.
"""

from __future__ import annotations

import math


def node_coordinate(low, high, count, index):
    return low + (high - low) * index / (count - 1)


def _axis_nodes(low, high, count, need_low, need_high, sizes):
    step = (high - low) / (count - 1)
    first = max(0, int(math.floor((need_low - low) / step + 1e-9)))
    last = min(count - 1, int(math.ceil((need_high - low) / step - 1e-9)))
    # Widen the span until it splits evenly into (size - 1) steps.
    for size in sizes:
        for extra in range(0, count):
            for lo, hi in ((first, last + extra), (first - extra, last),
                           (first - extra, last + extra)):
                if lo < 0 or hi > count - 1 or hi - lo < size - 1:
                    continue
                if (hi - lo) % (size - 1) == 0:
                    stride = (hi - lo) // (size - 1)
                    return [lo + k * stride for k in range(size)]
    return None


def select_verify_grid(stored_bounds, stored_count, required_bounds,
                       sizes=(3,), epsilon=0.05):
    """Pick a small square grid on stored nodes that covers the required area.

    Returns {"bounds": [x0, y0, x1, y1], "size": n, "x_index": [...],
    "y_index": [...]} or None when the stored mesh is not square, does not
    cover the required area, or no grid of an allowed size fits on its nodes
    (for example a full-width 6x6 mesh: five steps cannot split in two).
    """
    cols, rows = int(stored_count[0]), int(stored_count[1])
    if cols != rows or cols < 5:
        return None
    for size in sizes:
        indices = []
        for axis in (0, 1):
            low, high = float(stored_bounds[axis]), float(stored_bounds[axis + 2])
            need_low = float(required_bounds[axis])
            need_high = float(required_bounds[axis + 2])
            if need_low < low - epsilon or need_high > high + epsilon:
                return None
            nodes = _axis_nodes(low, high, cols, need_low + epsilon,
                                need_high - epsilon, (size,))
            if nodes is None:
                break
            indices.append(nodes)
        if len(indices) == 2:
            xi, yi = indices
            bounds = [
                node_coordinate(stored_bounds[0], stored_bounds[2], cols, xi[0]),
                node_coordinate(stored_bounds[1], stored_bounds[3], cols, yi[0]),
                node_coordinate(stored_bounds[0], stored_bounds[2], cols, xi[-1]),
                node_coordinate(stored_bounds[1], stored_bounds[3], cols, yi[-1]),
            ]
            return {"bounds": bounds, "size": size, "x_index": xi, "y_index": yi}
    return None


def compare_verify(stored_matrix, grid, verify_matrix, tolerance):
    """Compare a probed 3x3 against the stored nodes it was placed on."""
    result = {"pass": False, "max_deviation": None, "deviations": []}
    size = grid["size"]
    if (len(verify_matrix) != size
            or any(len(row) != size for row in verify_matrix)):
        result["reason"] = "VERIFY_DIMENSION_MISMATCH"
        return result
    worst = 0.0
    for r, j in enumerate(grid["y_index"]):
        for c, i in enumerate(grid["x_index"]):
            measured = float(verify_matrix[r][c])
            stored = float(stored_matrix[j][i])
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
