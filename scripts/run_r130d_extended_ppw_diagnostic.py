#!/usr/bin/env python3
"""Predeclared R130D PPW extension (#938), diagnostic only.

Do not mutate the original 8/10/12 acceptance contract or claim physical
validation. Uses the same pinned solver, sloped geometry, source, receiver,
duration, Fourier convention, and thresholds as run25. New levels were
registered separately before execution.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

import numpy as np
import scipy

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from htdt.acoustic_pffdtd_polyhedral_geometry import (
    PffdtdPolyhedralCandidateWaveExecutor,
    register_r120b_polyhedral_authorities,
)
from htdt.cad_candidate_wave_execution import (
    CandidateResourceConfiguration, build_pffdtd_candidate_configuration,
)
from htdt.r130d_general3d_validation import (
    compare_complex_transfer, load_validation_plan, semantic_hash,
)
from run_r130a_candidate_wave_execution import _fixture as build_fixture
from run_r130d_general3d_validation import (
    _create_pffdtd_dispatch, _git_head,
)
from run_r130d_polyhedral_candidate_wave_execution import (
    _make_polyhedron, _restore_pinned_pffdtd_checkout, _result_evidence,
)


SCHEDULE = (8, 10, 12, 16, 20, 24)
SCHEMA = "htdt.r130d.extended-ppw-diagnostic-evidence-1"
FAIL_STATE = "DIAGNOSTIC_NOT_NUMERICALLY_QUALIFIED"


def check_spec(spec: dict, parent) -> None:
    if spec.get("schema_version") != "htdt.r130d.extended-ppw-diagnostic-plan-1":
        raise ValueError("unexpected diagnostic schema")
    if tuple(spec["levels_ppw"]) != SCHEDULE:
        raise ValueError("diagnostic PPW series modified")
    if spec["parent_plan_semantic_sha256"] != parent.plan_sha256():
        raise ValueError("parent frozen plan SHA differs")
    if spec["pffdtd_commit"] != parent.pffdtd.source_commit_sha:
        raise ValueError("upstream solver pin differs")
    if tuple(spec["frequency_hz"]) != parent.physical_quantity.frequency_hz:
        raise ValueError("frequency axis modified")
    if spec["duration_s"] != parent.physical_quantity.duration_s:
        raise ValueError("record duration modified")
    if spec["fmax_hz"] != parent.pffdtd.fmax_hz:
        raise ValueError("fmax modified")
    if spec["phase_mask_db"] != parent.acceptance.magnitude_mask_relative_db:
        raise ValueError("phase mask modified")
    if spec["unchanged_thresholds"] != parent.acceptance.pffdtd_self_convergence.model_dump():
        raise ValueError("frozen acceptance thresholds modified")
    if spec["limits"] != {
        "max_grid_cells": parent.pffdtd.max_grid_cells,
        "max_time_steps": parent.pffdtd.max_time_steps,
        "max_output_bytes": parent.pffdtd.max_output_bytes,
        "max_solver_wall_seconds": parent.pffdtd.max_solver_wall_seconds,
    }:
        raise ValueError("resource ceilings modified")
    if spec["decision"] != {
        "diagnostic_only": True, "may_replace_canonical_run25": False,
        "may_unblock_cross_solver": False, "may_enable_production": False,
    }:
        raise ValueError("diagnostic cannot authorize a production claim")


def run_one(parent, *, fixture, executor, semantic_ref, compiled_ref,
            rigid_boundary_ref, ppw: int) -> dict:
    base = fixture["configuration"]
    cfg = build_pffdtd_candidate_configuration(
        expected_pffdtd_commit_sha=parent.pffdtd.source_commit_sha,
        fmax_hz=parent.pffdtd.fmax_hz,
        points_per_wavelength=float(ppw),
        duration_s=parent.physical_quantity.duration_s,
        frequency_samples_hz=parent.physical_quantity.frequency_hz,
        density_kg_m3=parent.fixture.density_kg_m3,
        density_authority_ref=base.density_authority_ref,
        relative_humidity_percent=float(base.relative_humidity_percent),
        humidity_authority_ref=base.humidity_authority_ref,
        resource=CandidateResourceConfiguration(
            solver_threads=parent.pffdtd.solver_threads,
            setup_processes=parent.pffdtd.setup_processes,
            max_grid_cells=parent.pffdtd.max_grid_cells,
            max_time_steps=parent.pffdtd.max_time_steps,
            max_output_bytes=parent.pffdtd.max_output_bytes,
            max_solver_wall_seconds=parent.pffdtd.max_solver_wall_seconds,
        ),
    )
    dispatch = _create_pffdtd_dispatch(fixture, configuration=cfg)
    execution, _, _ = executor.compile_input(
        dispatch_binding_id=dispatch.binding_id, configuration=cfg,
        semantic_geometry_ref=semantic_ref, compiled_geometry_ref=compiled_ref,
        rigid_boundary_physics_ref=rigid_boundary_ref,
    )
    _restore_pinned_pffdtd_checkout(fixture["executor"])
    result = executor.execute(
        dispatch_binding_id=dispatch.binding_id, configuration=cfg,
        semantic_geometry_ref=semantic_ref, compiled_geometry_ref=compiled_ref,
        rigid_boundary_physics_ref=rigid_boundary_ref,
    )
    evidence = _result_evidence(fixture, result)
    artifact = fixture["store"].read_payload(result.artifacts[0].artifact_authority)
    provenance = fixture["store"].read_payload(result.execution_provenance_ref)
    q_by_frequency = {
        float(s.frequency_hz): complex(float(s.real_m3_s), float(s.imag_m3_s))
        for s in fixture["excitation"].samples
    }
    q = np.asarray([q_by_frequency[float(f)] for f in parent.physical_quantity.frequency_hz])
    pressure = np.asarray(evidence["pressure_real_pa"][0], dtype=np.float64) + (
        1j * np.asarray(evidence["pressure_imag_pa"][0], dtype=np.float64)
    )
    if np.any(np.abs(q) == 0):
        raise RuntimeError("zero exact source spectrum")
    transfer = pressure / q
    if not np.all(np.isfinite(transfer)):
        raise RuntimeError("nonfinite transfer")
    sampling = artifact["time_sampling"]
    if (tuple(map(float, artifact["frequency_axis_hz"]))
            != parent.physical_quantity.frequency_hz):
        raise RuntimeError("frequency axis mismatch")
    if not math.isclose(float(sampling["requested_duration_s"]),
                        parent.physical_quantity.duration_s, abs_tol=1e-12):
        raise RuntimeError("record duration mismatch")
    return {
        "ppw": ppw,
        "transfer_pa_per_m3_s": [[float(z.real), float(z.imag)] for z in transfer],
        "result_sha256": result.semantic_sha256,
        "execution_input_sha256": execution.semantic_sha256,
        "grid_spacing_m": float(evidence["grid_spacing_m"]),
        "grid_dimensions": evidence["grid_dimensions"],
        "boundary_mask_sha256": evidence["boundary_mask_logical_sha256"],
        "time_step_s": float(provenance["time_step_s"]),
        "time_step_count": int(provenance["time_step_count"]),
        "timings_s": evidence["timings_s"],
    }


def analyze(levels: list[dict], parent, canonical: dict, *, tolerance: float) -> dict:
    if tuple(x["ppw"] for x in levels) != SCHEDULE:
        raise ValueError("incomplete or reordered refinement schedule")
    pinned = {int(x["points_per_wavelength"]): x for x in canonical["pffdtd_levels"]}
    baseline_deltas = []
    for level in levels[:3]:
        ppw = level["ppw"]
        expected = np.asarray([complex(*x) for x in pinned[ppw]["transfer_pa_per_m3_s"]])
        measured = np.asarray([complex(*x) for x in level["transfer_pa_per_m3_s"]])
        discrepancy = float(np.max(np.abs(expected - measured)))
        baseline_deltas.append({"ppw": ppw, "max_abs_component_error": discrepancy})
    replay_pass = all(x["max_abs_component_error"] <= tolerance for x in baseline_deltas)
    if not replay_pass:
        raise RuntimeError(f"frozen 8/10/12 reproduction mismatch: {baseline_deltas}")
    pairs = []
    for coarse, fine in zip(levels, levels[1:]):
        metrics = compare_complex_transfer(
            reference=fine["transfer_pa_per_m3_s"],
            candidate=coarse["transfer_pa_per_m3_s"],
            frequency_hz=parent.physical_quantity.frequency_hz,
            magnitude_mask_relative_db=parent.acceptance.magnitude_mask_relative_db,
        ).model_dump(mode="json")
        pairs.append({"coarse_ppw": coarse["ppw"], "fine_ppw": fine["ppw"], **metrics})
    # A diagnostic trend is not an adoption gate, even if a late adjacent
    # pair happens to satisfy the frozen numerical thresholds.
    limits = parent.acceptance.pffdtd_self_convergence
    last = pairs[-1]
    final_bounded = (
        last["complex_rms_relative"] <= limits.complex_rms_relative_max
        and last["magnitude_max_relative"] <= limits.magnitude_max_relative
        and last["phase_max_deg"] <= limits.phase_max_deg
    )
    descending = all(
        pairs[i]["complex_rms_relative"] < pairs[i-1]["complex_rms_relative"]
        and pairs[i]["magnitude_max_relative"] < pairs[i-1]["magnitude_max_relative"]
        and pairs[i]["phase_max_deg"] < pairs[i-1]["phase_max_deg"]
        for i in range(1, len(pairs))
    )
    return {
        "canonical_8_10_12_replay": "PASS",
        "canonical_replay_errors": baseline_deltas,
        "adjacent_pairs": pairs,
        "extended_final_pair_below_frozen_limits": final_bounded,
        "all_adjacent_errors_strictly_decrease": descending,
        "diagnostic_trend_state": ("WITHIN_LIMITS_DIAGNOSTIC_ONLY" if final_bounded and descending
                                   else "NONCONVERGENT_OR_PREASYMPTOTIC_DIAGNOSTIC"),
        "self_convergence_accepted": False,
        "cross_solver_eligible": False,
        "physical_validation": "NOT_VALIDATED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--parent-plan", type=Path, required=True)
    parser.add_argument("--canonical-summary", type=Path, required=True)
    parser.add_argument("--pffdtd-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.plan.read_text(encoding="utf-8"))
    parent = load_validation_plan(args.parent_plan)
    check_spec(spec, parent)
    if _git_head(args.pffdtd_root) != parent.pffdtd.source_commit_sha:
        raise ValueError("PFFDTD checkout is not the pinned source")
    if np.__version__ != parent.independent_reference.modal_numpy_version or (
        scipy.__version__ != parent.independent_reference.modal_scipy_version
    ):
        raise ValueError("numpy/scipy differ from frozen independent plan")
    canonical = json.loads(args.canonical_summary.read_text(encoding="utf-8"))
    if canonical["plan"]["plan_sha256"] != parent.plan_sha256():
        raise ValueError("run25 summary not from exact frozen parent plan")
    fixture = build_fixture(
        args.work_root / "pffdtd-fixture", args.pffdtd_root,
        boundary_mode="rigid", fixture_id="r130d-general3d-independent-validation-v1",
    )
    snapshot = fixture["snapshot"]
    source = fixture["source"].source_acoustic_reference_world_position
    receiver = snapshot.receivers[0].world_position
    if (source.x_m, source.y_m, source.z_m) != parent.fixture.source_position_m:
        raise ValueError("source location mismatched")
    if (receiver.x_m, receiver.y_m, receiver.z_m) != parent.fixture.receiver_position_m:
        raise ValueError("receiver location mismatched")
    surface = snapshot.surface_boundary_configuration[0]
    sem, comp = _make_polyhedron(
        snapshot=snapshot, source_key=parent.fixture.source_key,
        vertices=parent.fixture.vertices_m, faces=parent.fixture.faces,
        material_ref=surface.material_authority,
    )
    sem_ref, comp_ref = register_r120b_polyhedral_authorities(
        fixture["store"], semantic=sem, compiled=comp
    )
    executor = PffdtdPolyhedralCandidateWaveExecutor(
        base_executor=fixture["executor"], containment_tolerance_m=1e-9,
    )
    levels = []
    start = time.perf_counter()
    for ppw in SCHEDULE:
        print(f"RUN ppw={ppw}", flush=True)
        row = run_one(parent, fixture=fixture, executor=executor,
                      semantic_ref=sem_ref, compiled_ref=comp_ref,
                      rigid_boundary_ref=surface.boundary_physics_authority,
                      ppw=ppw)
        levels.append(row)
        print(f"DONE ppw={ppw}: {row['transfer_pa_per_m3_s']}", flush=True)
    analysis = analyze(levels, parent, canonical,
                       tolerance=float(spec["canonical_replay_tolerance_abs_complex_component"]))
    result = {
        "schema_version": SCHEMA,
        "plan_sha256": semantic_hash(spec),
        "parent_plan_sha256": parent.plan_sha256(),
        "pffdtd_source_commit_sha": parent.pffdtd.source_commit_sha,
        "repository_head": _git_head(Path(__file__).resolve().parents[1]),
        "environment": {
            "platform": platform.platform(), "python": sys.version.split()[0],
            "numpy": np.__version__, "scipy": scipy.__version__,
        },
        "levels": levels, "analysis": analysis,
        "runtime_wall_seconds": time.perf_counter() - start,
        "decision": {
            "diagnostic_execution": "PASS",
            "canonical_run25_status": "SELF_CONVERGENCE_FAILED",
            "production_readiness": FAIL_STATE,
            "cross_solver": "BLOCKED_PENDING_BOTH_INDEPENDENT_SELF_CONVERGENCES",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["analysis"], indent=2), flush=True)
    print(f"EVIDENCE {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
