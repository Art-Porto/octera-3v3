#!/usr/bin/env python3
"""Run every offline suite and print one line per suite."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITES = [
    "tests/test_probe_plan_mirror.py",
    "tests/test_mesh_reuse_v1.py",
    "tests/test_mesh_reuse_hardening.py",
]


def main() -> int:
    failed = 0
    for suite in SUITES:
        done = subprocess.run(
            [sys.executable, suite], cwd=REPO, capture_output=True, text=True
        )
        # The suites report failure in their JSON as well as the exit code.
        ok = (done.returncode == 0 and '"status": "FAIL"' not in done.stdout
              and '"pass": false,\n  "checks"' not in done.stdout)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {suite}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
