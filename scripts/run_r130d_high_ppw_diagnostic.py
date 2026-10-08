#!/usr/bin/env python3
"""R130D #938: preregistered, physical PFFDTD PPW=28..44 follow-up.

Only original sloped fixture, frozen physical transfer/40/80 Hz/250ms/
solver SHA/acceptance criteria. The committed 24-PPW record is a
continuation anchor, not a replacement numerical reference.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np
import scipy

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_SCRIPT_DIR.parent / "backend" / "src"))

from htdt.r130d_general3d_validation import (
    compare_complex_transfer, load_validation_plan, semantic_hash,
)
from htdt.acoustic_pffdtd_polyhedral_geometry import (
    PffdtdPolyhedralCandidateWaveExecutor,
    register_r120b_polyhedral_authorities,
)
from run_r130d_extended_ppw_diagnostic import run_one
from run_r130a_candidate_wave_execution import _fixture as build_fixture
from run_r130d_general3d_validation import _git_head
from run_r130d_polyhedral_candidate_wave_execution import _make_polyhedron

ADDITIONAL_PPW = (28, 32, 36, 40, 44)
EXPECTED_PREVIOUS_PPW = (8, 10, 12, 16, 20, 24)
SCHEMA = "htdt.r130d.high-ppw-independent-diagnostic-evidence-1"
FROZEN_LIMITS = {
    "complex_rms_relative_max": 0.2,
    "magnitude_max_relative": 0.25,
    "phase_max_deg": 15,
}


def check_plan(plan: dict, parent, previous: dict, previous_plan: dict) -> None:
    if plan.get("schema_version") != "htdt.r130d.high-ppw-independent-diagnostic-plan-1":
        raise ValueError("unknown high-PPW plan schema")
    if tuple(plan["additional_ppw"]) != ADDITIONAL_PPW:
        raise ValueError("preregistered high-PPW levels changed")
    if plan["parent_plan_sha256"] != parent.plan_sha256():
        raise ValueError("frozen R130D plan identity changed")
    if (plan["previous_extended_plan_semantic_sha256"] != semantic_hash(previous_plan)
            or previous["plan_sha256"] != semantic_hash(previous_plan)):
        raise ValueError("previous preregistered experiment identity changed")
    if plan["solver_commit"] != parent.pffdtd.source_commit_sha:
        raise ValueError("solver SHA not pinned")
    if (tuple(plan["frequency_hz"]) != parent.physical_quantity.frequency_hz
            or plan["duration_s"] != parent.physical_quantity.duration_s):
        raise ValueError("physical observable changed")
    if plan["frozen_acceptance"] != FROZEN_LIMITS:
        raise ValueError("original numerical tolerances altered")
    if plan["frozen_acceptance"] != parent.acceptance.pffdtd_self_convergence.model_dump(
            exclude_none=True):
        raise ValueError("parent numerical tolerances differ")
    if plan["grid_ceilings"] != {
        "max_grid_cells": parent.pffdtd.max_grid_cells,
        "max_time_steps": parent.pffdtd.max_time_steps,
    }:
        raise ValueError("resource ceilings altered")
    if plan["qualification"] != {
        "diagnostic_only": True,
        "physical_validation": "NOT_VALIDATED",
        "cross_solver_eligible": False,
        "original_8_10_12_status": "SELF_CONVERGENCE_FAILED",
    }:
        raise ValueError("diagnostic may not override a canonical gate")
    if tuple(x["ppw"] for x in previous["levels"]) != EXPECTED_PREVIOUS_PPW:
        raise ValueError("anchoring previous levels incomplete")
    if (previous["analysis"]["canonical_8_10_12_replay"] != "PASS"
            or previous["analysis"]["self_convergence_accepted"] is not False
            or previous["decision"]["canonical_run25_status"] != "SELF_CONVERGENCE_FAILED"):
        raise ValueError("prior failed gate not preserved")
    if plan["previous_level_ppw"] != previous["levels"][-1]["ppw"]:
        raise ValueError("24-PPW anchor missing")


def analyze(continuation: list[dict], parent) -> dict:
    if tuple(x["ppw"] for x in continuation) != (24, *ADDITIONAL_PPW):
        raise ValueError("continuation series incomplete or reordered")
    pairs = []
    for coarse, fine in zip(continuation, continuation[1:]):
        metric = compare_complex_transfer(
            reference=fine["transfer_pa_per_m3_s"],
            candidate=coarse["transfer_pa_per_m3_s"],
            frequency_hz=parent.physical_quantity.frequency_hz,
            magnitude_mask_relative_db=parent.acceptance.magnitude_mask_relative_db,
        ).model_dump(mode="json")
        pairs.append({
            "coarse_ppw": coarse["ppw"],
            "fine_ppw": fine["ppw"],
            **metric,
        })
    l = parent.acceptance.pffdtd_self_convergence
    passing = [
        (item["complex_rms_relative"] <= l.complex_rms_relative_max
         and item["magnitude_max_relative"] <= l.magnitude_max_relative
         and item["phase_max_deg"] <= l.phase_max_deg)
        for item in pairs
    ]
    monotone = all(
        pairs[i]["complex_rms_relative"] < pairs[i - 1]["complex_rms_relative"]
        and pairs[i]["magnitude_max_relative"] < pairs[i - 1]["magnitude_max_relative"]
        and pairs[i]["phase_max_deg"] < pairs[i - 1]["phase_max_deg"]
        for i in range(1, len(pairs))
    )
    return {
        "pairs": pairs,
        "each_pair_below_frozen_limits": passing,
        "all_adjacent_metric_errors_strictly_decrease": monotone,
        "final_pair_within_frozen_limits": passing[-1],
        "diagnostic_numerical_state": (
            "HIGH_PPW_TREND_BOUNDED_DIAGNOSTIC_ONLY"
            if passing[-1] and monotone
            else "HIGH_PPW_NONCONVERGENT_OR_PREASYMPTOTIC"
        ),
        "original_8_10_12_status": "SELF_CONVERGENCE_FAILED",
        "cross_solver_eligible": False,
        "physical_validation": "NOT_VALIDATED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--parent-plan", type=Path, required=True)
    parser.add_argument("--previous-plan", type=Path, required=True)
    parser.add_argument("--previous-evidence", type=Path, required=True)
    parser.add_argument("--pffdtd-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parent = load_validation_plan(args.parent_plan)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    prev_plan = json.loads(args.previous_plan.read_text(encoding="utf-8"))
    prev = json.loads(args.previous_evidence.read_text(encoding="utf-8"))
    check_plan(plan, parent, prev, prev_plan)
    if _git_head(args.pffdtd_root) != plan["solver_commit"]:
        raise ValueError("PFFDTD pinned SHA mismatch")
    if (np.__version__, scipy.__version__) != (
            parent.independent_reference.modal_numpy_version,
            parent.independent_reference.modal_scipy_version):
        raise ValueError("NumPy/SciPy pins changed")
    if args.work_root.exists() and any(args.work_root.iterdir()):
        raise ValueError("R130D work-root must be fresh")
    fixture = build_fixture(
        args.work_root / "pffdtd-fixture", args.pffdtd_root,
        boundary_mode="rigid",
        fixture_id="r130d-general3d-independent-validation-v1",
    )
    snapshot = fixture["snapshot"]
    source = fixture["source"].source_acoustic_reference_world_position
    receiver = snapshot.receivers[0].world_position
    if (source.x_m, source.y_m, source.z_m) != parent.fixture.source_position_m:
        raise ValueError("source coordinates mismatch")
    if (receiver.x_m, receiver.y_m, receiver.z_m) != parent.fixture.receiver_position_m:
        raise ValueError("receiver coordinates mismatch")
    surface = snapshot.surface_boundary_configuration[0]
    sem, compiled = _make_polyhedron(
        snapshot=snapshot, source_key=parent.fixture.source_key,
        vertices=parent.fixture.vertices_m, faces=parent.fixture.faces,
        material_ref=surface.material_authority,
    )
    sem_ref, comp_ref = register_r120b_polyhedral_authorities(
        fixture["store"], semantic=sem, compiled=compiled
    )
    executor = PffdtdPolyhedralCandidateWaveExecutor(
        base_executor=fixture["executor"], containment_tolerance_m=1e-9,
    )
    levels = [prev["levels"][-1]]
    start = time.perf_counter()
    for ppw in ADDITIONAL_PPW:
        print(f"RUN HIGH PPW {ppw}", flush=True)
        row = run_one(
            parent, fixture=fixture, executor=executor,
            semantic_ref=sem_ref, compiled_ref=comp_ref,
            rigid_boundary_ref=surface.boundary_physics_authority, ppw=ppw,
        )
        levels.append(row)
        print(f"DONE HIGH PPW {ppw}", flush=True)
    analysis = analyze(levels, parent)
    result = {
        "schema_version": SCHEMA,
        "plan_sha256": semantic_hash(plan),
        "original_plan_sha256": parent.plan_sha256(),
        "previous_extended_plan_sha256": semantic_hash(prev_plan),
        "previous_experiment_repository_head": prev["repository_head"],
        "repository_head": _git_head(_SCRIPT_DIR.parent),
        "solver_sha": _git_head(args.pffdtd_root),
        "runtime": {
            "os": platform.platform(), "python": sys.version.split()[0],
            "numpy": np.__version__, "scipy": scipy.__version__,
            "elapsed_seconds": time.perf_counter() - start,
        },
        "levels": levels,
        "analysis": analysis,
        "decision": {
            "diagnostic_execution": "PASS",
            "canonical_self_convergence": "SELF_CONVERGENCE_FAILED",
            "cross_solver": "CROSS_SOLVER_BLOCKED",
            "production": "NO_GO_NOT_VALIDATED",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(json.dumps(analysis, indent=2), flush=True)
    print(f"EVIDENCE {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
