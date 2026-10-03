#!/usr/bin/env python3
"""Every Klipper extra in the package must be linked by install.sh.

Regression for 2026-10-03: octera_mesh_verify.py was added but not linked,
and Klipper failed at connect because octera_mesh_reuse imports it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run() -> dict:
    install = (REPO / "install.sh").read_text(encoding="utf-8")
    match = re.search(r"^for module in ([^;]+); do", install, re.M)
    linked = set(match.group(1).split()) if match else set()
    shipped = {p.stem for p in (REPO / "klippy/extras").glob("*.py")}
    missing = sorted(shipped - linked)
    stale = sorted(linked - shipped)
    return {"pass": not missing and not stale, "missing": missing, "stale": stale}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
