#!/usr/bin/env python3
"""Static checks of the supervisorctl shim Moonraker uses on the 3V3.

The behaviour is tested on the printer, with fake services only, by
tests/printer/shim_service_tree.sh (it needs /proc and start-stop-daemon).
This suite guards what can be checked offline:
- the shim parses as POSIX sh and keeps the real system paths by default;
- stop reaps the whole process tree of the service (2026-11-05: the old
  OctoEverywhere python survived a restart next to the new one);
- "restart moonraker" runs detached with setsid (2026-11-05: Moonraker
  restarted itself after an update and never came back).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHIM = REPO / "files/system/supervisorctl"
PRINTER_TEST = REPO / "tests/printer/shim_service_tree.sh"


def body(text: str, name: str) -> str:
    m = re.search(r"^%s\(\) \{[^\n]*\n(.*?)^\}" % re.escape(name), text, re.M | re.S)
    return m.group(1) if m else ""


def run() -> dict:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    text = SHIM.read_text(encoding="utf-8")
    sh = shutil.which("sh") or shutil.which("bash")
    for path in (SHIM, PRINTER_TEST):
        done = subprocess.run([sh, "-n", str(path)], capture_output=True, text=True)
        check("sh -n %s" % path.name, done.returncode == 0, done.stderr[-200:])
    check("CRLF-free", b"\r" not in SHIM.read_bytes())
    check("defaults are the system paths",
          'INIT_DIR="${SHIM_INIT_DIR:-/etc/init.d}"' in text
          and 'RUN_DIR="${SHIM_RUN_DIR:-/var/run}"' in text)
    stop = body(text, "stop")
    check("stop records the tree before the init script stops",
          stop.find("service_tree") != -1 and stop.find("service_tree") < stop.find('"$script_path" stop'))
    check("stop reaps the recorded tree", "reap $tree" in stop)
    reap = body(text, "reap")
    check("reap escalates to SIGKILL", "kill -9" in reap)
    check("tree_of never includes the shim itself", '[ "$1" = "$$" ] && return 0' in body(text, "tree_of"))
    restart = body(text, "restart")
    check("restart moonraker is detached with setsid",
          '"$service" = "moonraker"' in restart and "setsid" in restart and "SHIM_DETACHED=1" in restart)
    check("restart is stop then start (waits for the old tree)",
          0 <= restart.rfind('stop "$service"') < restart.rfind('start "$service"')
          and '"$script_path" restart' not in restart)
    return {"pass": all(c["pass"] for c in checks), "checks": checks}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
