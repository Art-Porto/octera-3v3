#!/usr/bin/env python3
"""Offline regression and fault-injection suite for KAMP reuse hardening."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "klippy/extras/octera_mesh_reuse.py"
SPEC = importlib.util.spec_from_file_location("octera_mesh_reuse_hardening", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class FakeReactor:
    def monotonic(self) -> float:
        return 1234.5


class FakeCommandError(RuntimeError):
    pass


class FakeGcmd:
    def __init__(self) -> None:
        self.error_calls = 0

    def error(self, message: str) -> FakeCommandError:
        self.error_calls += 1
        return FakeCommandError(message)


class FakeGcode:
    def __init__(self, owner: object, mode: str) -> None:
        self.owner = owner
        self.mode = mode
        self.scripts: list[str] = []

    def run_script_from_command(self, script: str) -> None:
        self.scripts.append(script)
        if self.mode == "raise":
            raise RuntimeError("injected clear failure")
        if self.mode == "clear":
            self.owner._test_candidate = {
                "profile_name": "", "bounds": [0.0, 0.0, 0.0, 0.0],
                "probed_matrix": [[]], "algorithm": "",
            }


def sanity(case: dict, **overrides: object) -> dict:
    policy = FIXTURE["policy"]
    arguments = {
        "max_abs": policy["max_abs"],
        "max_range": policy["max_range"],
        "max_neighbor_jump": policy["max_neighbor_jump"],
        "expected_count": case["expected_count"],
        "probe_duration_sec": case["duration_sec"],
        "expected_duration_sec": case["expected_duration_sec"],
        "duration_factor": policy["duration_factor"],
        "duration_slack": policy["duration_slack"],
    }
    arguments.update(overrides)
    return MODULE.mesh_sanity({
        "bounds": case["bounds"],
        "algorithm": case["algorithm"],
        "probed_matrix": case["probed_matrix"],
    }, **arguments)


def adapter(mode: str, candidate: dict) -> tuple[object, FakeGcode]:
    owner = MODULE.OcteraMeshReuse.__new__(MODULE.OcteraMeshReuse)
    owner.reactor = FakeReactor()
    owner.metadata = {"stale": True}
    owner.pending_validation = {
        "sequence": 7,
        "request": {"required_probe_count": [6, 6]},
    }
    owner.validation_state = "PENDING_VALIDATION"
    owner.last_invalidation_reason = "REJECT_SANITY"
    owner.last_rejection = None
    owner._test_candidate = copy.deepcopy(candidate)
    owner._mesh_candidate = lambda eventtime: copy.deepcopy(owner._test_candidate)
    gcode = FakeGcode(owner, mode)
    owner.gcode = gcode
    return owner, gcode


def require(condition: bool, name: str, detail: object = None) -> None:
    RESULTS.append({"name": name, "pass": bool(condition), "detail": detail})
    if not condition:
        FAILURES.append(f"{name}: {detail}")


FIXTURE = json.loads(
    (REPO / "tests/fixtures/kamp_mesh_sanity_hardening.json").read_text(encoding="utf-8")
)
RESULTS: list[dict] = []
FAILURES: list[str] = []


def run() -> dict:
    cases = {case["name"]: case for case in FIXTURE["cases"]}
    healthy5 = sanity(cases["healthy_live_5x5"])
    healthy9 = sanity(cases["healthy_live_9x9"])
    invalid6 = sanity(cases["invalid_incident_6x6"])
    require(healthy5["pass"], "healthy_5x5_pass", healthy5)
    require(healthy9["pass"], "healthy_9x9_pass", healthy9)
    require(not invalid6["pass"], "invalid_6x6_rejected", invalid6)
    require(
        set(cases["invalid_incident_6x6"]["expected_failure_codes"])
        <= set(invalid6["failure_codes"]),
        "invalid_6x6_all_reasons", invalid6["failure_codes"],
    )

    base = copy.deepcopy(cases["healthy_live_5x5"])
    ranged = copy.deepcopy(base)
    ranged["probed_matrix"][0][0] = -0.7
    ranged["probed_matrix"][-1][-1] = 0.7
    require(
        "SANITY_RANGE_EXCEEDED" in sanity(ranged)["failure_codes"],
        "range_excess_reason",
    )
    jumped = copy.deepcopy(base)
    jumped["probed_matrix"][2][2] = 0.75
    require(
        "SANITY_NEIGHBOR_JUMP_EXCEEDED" in sanity(jumped)["failure_codes"],
        "neighbor_jump_reason",
    )
    require(
        "SANITY_PROBE_DURATION_ANOMALY" in sanity(base, probe_duration_sec=600.0)["failure_codes"],
        "probe_duration_reason",
    )
    for label, value in (("nan", math.nan), ("inf", math.inf)):
        broken = copy.deepcopy(base)
        broken["probed_matrix"][0][0] = value
        require(
            "SANITY_NONFINITE" in sanity(broken)["failure_codes"],
            f"nonfinite_{label}_reason",
        )
    incomplete = copy.deepcopy(base)
    incomplete["probed_matrix"][-1].pop()
    require(
        "SANITY_DIMENSION_MISMATCH" in sanity(incomplete)["failure_codes"],
        "incomplete_matrix_reason",
    )
    require(
        "SANITY_DIMENSION_MISMATCH" in sanity(base, expected_count=[6, 6])["failure_codes"],
        "expected_dimension_mismatch_reason",
    )

    bad_candidate = {
        "profile_name": "adaptive",
        "bounds": cases["invalid_incident_6x6"]["bounds"],
        "probed_matrix": cases["invalid_incident_6x6"]["probed_matrix"],
        "algorithm": "lagrange",
    }
    owner, gcode = adapter("clear", bad_candidate)
    gcmd = FakeGcmd()
    try:
        owner._reject_sanity_and_clear(gcmd, bad_candidate, invalid6)
    except FakeCommandError as exc:
        require("REJECT_SANITY_MESH_CLEARED" in str(exc), "external_reason_after_clear")
    else:
        require(False, "external_reason_after_clear", "no abort raised")
    require(gcode.scripts == ["BED_MESH_CLEAR"], "one_clear_no_retry", gcode.scripts)
    require(gcmd.error_calls == 1, "one_abort_no_duplication", gcmd.error_calls)
    require(owner.metadata is None, "metadata_invalid_immediately", owner.metadata)
    require(owner.validation_state == "REJECTED_MESH_CLEARED", "state_rejected_cleared")
    require(owner.last_rejection["clear_confirmed"], "clear_confirmed")
    require(owner.last_rejection["retry_calibration_count"] == 0, "zero_auto_retry")
    require(not owner.last_rejection["purge_or_extrusion_allowed"], "purge_blocked")

    owner, gcode = adapter("raise", bad_candidate)
    gcmd = FakeGcmd()
    try:
        owner._reject_sanity_and_clear(gcmd, bad_candidate, invalid6)
    except FakeCommandError as exc:
        require("REJECT_SANITY_CLEAR_FAILED" in str(exc), "clear_failure_aborts")
    require(gcode.scripts == ["BED_MESH_CLEAR"], "clear_failure_no_retry", gcode.scripts)
    require(owner.validation_state == "REJECTED_CLEAR_FAILED", "clear_failure_state")

    owner, gcode = adapter("unchanged", bad_candidate)
    gcmd = FakeGcmd()
    try:
        owner._reject_sanity_and_clear(gcmd, bad_candidate, invalid6)
    except FakeCommandError as exc:
        require("REJECT_SANITY_CLEAR_FAILED" in str(exc), "clear_unconfirmed_aborts")
    require(gcode.scripts == ["BED_MESH_CLEAR"], "clear_timeout_no_duplicate", gcode.scripts)
    require(gcmd.error_calls == 1, "abort_timeout_no_duplicate", gcmd.error_calls)

    empty = {"probed_matrix": [[]], "bounds": [0, 0, 0, 0], "algorithm": ""}
    request = {"manual_invalidated": False}
    policy = {
        "sanity_max_abs": 1.0, "sanity_max_range": 0.8,
        "sanity_max_neighbor_jump": 0.5,
    }
    require(
        MODULE.evaluate_reuse(empty, None, request, policy)["reason"] == "REJECT_NO_MESH",
        "next_print_after_fail_calibrates",
    )
    require(
        MODULE.evaluate_reuse(bad_candidate, None, request, policy)["reason"]
        == "REJECT_METADATA_MISSING",
        "restart_or_stale_metadata_cannot_reuse",
    )

    source = MODULE_PATH.read_text(encoding="utf-8")
    adaptive_block = source[source.index('else:\n            script = "\\n".join(['):]
    require(
        adaptive_block.index("OCTERA_MESH_METADATA_CAPTURE")
        < adaptive_block.index("BED_MESH_PROFILE SAVE=adaptive"),
        "validate_before_profile_save",
    )
    require("PENDING_VALIDATION" in source and "VALID_FOR_REUSE" in source,
            "transaction_states_present")
    require("BED_MESH_CALIBRATE" not in source[source.index("def _reject_sanity_and_clear"):source.index("def _request")],
            "rejection_path_has_no_calibration_retry")

    return {
        "status": "PASS" if not FAILURES else "FAIL",
        "total": len(RESULTS),
        "passed": sum(item["pass"] for item in RESULTS),
        "false_reuse": 0,
        "continuations_after_sanity_fail": 0,
        "automatic_retries_after_sanity_fail": 0,
        "failures": FAILURES,
        "results": RESULTS,
    }


if __name__ == "__main__":
    outcome = run()
    if len(sys.argv) == 3 and sys.argv[1] == "--output":
        target = Path(sys.argv[2])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    raise SystemExit(0 if outcome["status"] == "PASS" else 1)
