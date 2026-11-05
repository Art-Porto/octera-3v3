#!/usr/bin/env python3
"""install.sh puts the binary-only pip.conf in each plugin venv.

Regression for 2026-11-05: the OctoEverywhere 5.4.1 update ran
"pip install -U -r requirements.txt" with an unpinned "pillow" and built it
from source on the printer for minutes. The pip.conf keeps compiled packages
on wheels only; it must land in every plugin venv present, be refreshed when
it differs, and leave a second run with nothing to do.

The block is cut out of install.sh (with its say/change/backup helpers) and
run alone, so the test needs no symlinks and runs on any host with sh.
"""

from __future__ import annotations

import configparser
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONF = REPO / "files/pip/plugin-pip.conf"
SH = shutil.which("sh") or shutil.which("bash")


def install_block() -> str:
    text = (REPO / "install.sh").read_text(encoding="utf-8")
    helpers = text[text.index("say() {"):text.index("link() {")]
    block = text[text.index("# --- pip in the plugin envs"):text.index("# --- Moonraker update manager")]
    return helpers + block


def fake_root(tmp: Path, envs) -> Path:
    root = tmp / "root"
    files = root / "usr/data/octera/files/pip"
    files.mkdir(parents=True)
    shutil.copy(CONF, files / CONF.name)
    for env in envs:
        b = root / "usr/data" / env / "bin"
        b.mkdir(parents=True)
        (b / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
        (b / "python3").chmod(0o755)
    return root


def run_install(root: Path, *args: str) -> str:
    r = str(root).replace("\\", "/")
    script = ('ROOT="%s"; REPO="$ROOT/usr/data/octera"; BACKUP="$ROOT/backup"; '
              'CHECK=%d; CHANGES=0\n' % (r, 1 if "--check" in args else 0)) + install_block()
    done = subprocess.run([SH, "-ec", script], capture_output=True, text=True)
    return done.stdout + done.stderr


def run() -> dict:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    parser = configparser.ConfigParser()
    parser.read(CONF, encoding="utf-8")
    only = {p.strip() for p in parser.get("global", "only-binary", fallback="").split(",")}
    check("conf: pillow and zstandard binary-only", {"pillow", "zstandard"} <= only, sorted(only))
    check("conf: not :all: (pure packages may still upgrade)", ":all:" not in only)
    check("conf: prefer-binary", parser.getboolean("global", "prefer-binary", fallback=False))

    with tempfile.TemporaryDirectory() as t:
        root = fake_root(Path(t), ["octoeverywhere-env", "octoapp-env"])
        out = run_install(root)
        for env in ("octoeverywhere-env", "octoapp-env"):
            dst = root / "usr/data" / env / "pip.conf"
            check("installed in %s" % env,
                  dst.exists() and dst.read_bytes() == CONF.read_bytes(), out[-400:])
        out2 = run_install(root, "--check")
        check("second run: no pip.conf change", "pip.conf" not in out2, out2[-300:])
        stale = root / "usr/data/octoapp-env/pip.conf"
        stale.write_text("[global]\n", encoding="utf-8")
        out3 = run_install(root)
        check("stale pip.conf refreshed", stale.read_bytes() == CONF.read_bytes(), out3[-300:])

    with tempfile.TemporaryDirectory() as t:
        root = fake_root(Path(t), ["octoeverywhere-env"])
        run_install(root)
        check("absent venv not created", not (root / "usr/data/octoapp-env").exists())

    return {"pass": all(c["pass"] for c in checks), "checks": checks}


if __name__ == "__main__":
    outcome = run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["pass"] else 1)
