#!/usr/bin/env python3
"""Independently replay pinned MFEM 1/2/3 numerical reference for #938.

An MFEM rerun cannot promote production physics; replay verdicts and
independent solver self-convergence are separate from PFFDTD/BRAS.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))
from htdt.r130d_general3d_validation import (
    compare_complex_transfer, load_validation_plan, semantic_hash,
)
from run_r130d_general3d_validation import (
    _git_head, _run_reference_level,
)


def summarize(*, original: dict, actual: list[dict], plan) -> dict:
    if [int(x["refinement"]) for x in actual] != [1, 2, 3]:
        raise ValueError("MFEM 1/2/3 reference schedule incomplete")
    old = {int(x["refinement"]): x for x in original["mfem_levels"]}
    tolerance = 1e-6  # reproduction identity threshold, not convergence threshold
    replay = []
    for row in actual:
        refinement = int(row["refinement"])
        previous = np.array([complex(*v) for v in old[refinement]["transfer_pa_per_m3_s"]])
        current = np.array([complex(*v) for v in row["transfer_pa_per_m3_s"]])
        delta = float(np.max(np.abs(current - previous)))
        replay.append({
            "refinement": refinement, "maximum_complex_replay_difference": delta,
            "baseline_within_replay_tolerance": delta <= tolerance,
        })
    adjacent = []
    for low, high in zip(actual, actual[1:]):
        metrics = compare_complex_transfer(
            reference=high["transfer_pa_per_m3_s"],
            candidate=low["transfer_pa_per_m3_s"],
            frequency_hz=plan.physical_quantity.frequency_hz,
            magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
        ).model_dump(mode="json")
        adjacent.append({
            "coarse_refinement": int(low["refinement"]),
            "fine_refinement": int(high["refinement"]),
            **metrics,
        })
    t = plan.acceptance.reference_self_convergence
    fine = adjacent[-1]
    final_within = (
        fine["complex_rms_relative"] <= t.complex_rms_relative_max
        and fine["magnitude_max_relative"] <= t.magnitude_max_relative
        and fine["phase_max_deg"] <= t.phase_max_deg
    )
    monotone = all(
        adjacent[i]["complex_rms_relative"] < adjacent[i - 1]["complex_rms_relative"]
        and adjacent[i]["magnitude_max_relative"] < adjacent[i - 1]["magnitude_max_relative"]
        and adjacent[i]["phase_max_deg"] < adjacent[i - 1]["phase_max_deg"]
        for i in range(1, len(adjacent))
    )
    return {
        "baseline_replay_state": (
            "MATCH" if all(r["baseline_within_replay_tolerance"] for r in replay)
            else "BUILD_OR_NUMERIC_DIFFERENCE"
        ),
        "baseline_replay": replay,
        "reference_adjacent_metrics": adjacent,
        "mfem_self_convergence": (
            "SELF_CONVERGENCE_PASS_THIS_BOUNDED_FROZEN_SERIES_ONLY"
            if final_within and monotone else "SELF_CONVERGENCE_FAILED"
        ),
        "mfem_final_pair_within_frozen_thresholds": final_within,
        "mfem_errors_strictly_decreasing": monotone,
        "cross_solver_eligible": False,
        "general_3d_physical_validation": "NOT_VALIDATED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--run25-summary", required=True, type=Path)
    parser.add_argument("--mfem-root", required=True, type=Path)
    parser.add_argument("--mfem-executable", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    a = parser.parse_args()
    parent = load_validation_plan(a.plan)
    frozen = json.loads(a.run25_summary.read_text(encoding="utf-8"))
    if parent.plan_sha256() != frozen["plan"]["plan_sha256"]:
        raise ValueError("run25 and frozen parent plan differ")
    if _git_head(a.mfem_root) != parent.independent_reference.source_commit_sha:
        raise ValueError("MFEM checkout source hash is not pinned")
    if not a.mfem_executable.is_file():
        raise ValueError("MFEM executable is missing")
    if (np.__version__, scipy.__version__) != (
        parent.independent_reference.modal_numpy_version,
        parent.independent_reference.modal_scipy_version,
    ):
        raise ValueError("NumPy/SciPy not pinned to the frozen reference")
    levels = []
    begin = time.perf_counter()
    for refinement, count in zip(
        parent.independent_reference.uniform_refinements,
        parent.independent_reference.expected_element_counts,
    ):
        print(f"MFEM ref={refinement} / elements={count}", flush=True)
        result = _run_reference_level(
            parent, executable=a.mfem_executable, work_root=a.work_root,
            refinement=refinement, expected_elements=count,
        )
        levels.append(result)
        print(f"MFEM ref={refinement} finished", flush=True)
    report = summarize(original=frozen, actual=levels, plan=parent)
    payload = {
        "schema_version": "htdt.r130d.mfem-independent-reference-replay-1",
        "plan_sha256": parent.plan_sha256(),
        "source_commit": _git_head(a.mfem_root),
        "repository_head": _git_head(Path(__file__).resolve().parents[1]),
        "runtime": {
            "platform": platform.platform(), "python": sys.version.split()[0],
            "numpy": np.__version__, "scipy": scipy.__version__,
            "elapsed_seconds": time.perf_counter() - begin,
        },
        "levels": levels,
        "analysis": report,
        "nonclaims": {
            "pffdtd_self_convergence": "NOT_REASSESSED",
            "cross_solver_comparison": "BLOCKED",
            "production_solver_selected": False,
            "owned_room_validated": False,
        },
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
