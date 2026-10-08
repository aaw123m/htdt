#!/usr/bin/env python3
"""Exploratory R130D source regularization; never replaces the frozen impulse.

Convolve a physically identical time-domain Gaussian drive with the actual
pinned PFFDTD impulse traces. This is an LTI postprocessing experiment,
NOT a separate executed input-waveform solver leg, and it changes the
physical excitation contract of run25: no canonical R130D pass is possible.
The original impulse responses are re-derived and checked against the
committed real-solver evidence before convolution.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from htdt.acoustic_pffdtd_adapter import (
    finite_record_pressure_transfer,
    pffdtd_velocity_potential_to_pressure_trace,
    recombine_pffdtd_receiver_traces,
)
from htdt.r130d_general3d_validation import compare_complex_transfer

DRIVE_SIGMA_S = (0.002, 0.003, 0.004)
DRIVE_CENTER_S = 0.012
FREQUENCY_HZ = np.array([40.0, 80.0], dtype=np.float64)


def evaluate(high_evidence: dict, raw_work_root: Path) -> dict:
    if tuple(x["ppw"] for x in high_evidence["levels"]) != (24, 28, 32, 36, 40, 44):
        raise ValueError("high PPW frozen evidence or order differs")
    if high_evidence["analysis"]["final_pair_within_frozen_limits"] is not False:
        raise ValueError("high PPW evidence cannot be silently upgraded")
    traces = {}
    replays = []
    for row in high_evidence["levels"][1:]:
        ppw = row["ppw"]
        simulation_dir = raw_work_root / row["execution_input_sha256"] / "sim"
        with h5py.File(simulation_dir / "sim_outs.h5", "r") as handle:
            output = np.asarray(handle["u_out"][...], dtype=np.float64)
        with h5py.File(simulation_dir / "comms_out.h5", "r") as handle:
            alpha = np.asarray(handle["out_alpha"][...], dtype=np.float64)
            nt = int(handle["Nt"][()])
        dt = float(row["time_step_s"])
        potential = recombine_pffdtd_receiver_traces(
            output, alpha, receiver_count=1, nt=nt,
        )[0]
        impulse_response = pffdtd_velocity_potential_to_pressure_trace(
            potential, time_step_s=dt, density_kg_m3=1.2,
        )
        impulse_source = np.zeros(nt, dtype=np.float64)
        impulse_source[0] = 1.0
        replay = finite_record_pressure_transfer(
            impulse_response, impulse_source, time_step_s=dt,
            frequency_hz=FREQUENCY_HZ,
        )
        frozen = np.asarray(
            [complex(*v) for v in row["transfer_pa_per_m3_s"]],
            dtype=np.complex128,
        )
        discrepancy = float(np.max(np.abs(replay - frozen)))
        if discrepancy > 1e-9:
            raise RuntimeError(f"raw impulse replay mismatch at {ppw} PPW: {discrepancy}")
        traces[ppw] = (impulse_response, dt, nt)
        replays.append({"ppw": ppw, "maximum_complex_replay_delta": discrepancy})

    results = []
    for sigma in DRIVE_SIGMA_S:
        rows = []
        for ppw, (pressure, dt, nt) in traces.items():
            t = np.arange(nt, dtype=np.float64) * dt
            q = np.exp(-0.5 * ((t - DRIVE_CENTER_S) / sigma) ** 2)
            driven_pressure = np.convolve(pressure, q, mode="full")[:nt]
            transfer = finite_record_pressure_transfer(
                driven_pressure, q, time_step_s=dt, frequency_hz=FREQUENCY_HZ,
            )
            rows.append({
                "ppw": ppw,
                "transfer_complex": [[float(z.real), float(z.imag)] for z in transfer],
            })
        pairs = []
        for low, high in zip(rows, rows[1:]):
            score = compare_complex_transfer(
                reference=high["transfer_complex"],
                candidate=low["transfer_complex"],
                frequency_hz=(40.0, 80.0),
                magnitude_mask_relative_db=-50,
            ).model_dump(mode="json")
            pairs.append({
                "coarse_ppw": low["ppw"], "fine_ppw": high["ppw"], **score,
            })
        results.append({"gaussian_sigma_s": sigma, "levels": rows, "pairs": pairs})

    return {
        "schema_version": "htdt.r130d.exploratory-temporal-source-regularization-v1",
        "experimental_role": "exploratory LTI superposition; not a separately driven solver run",
        "physical_contract_changed": True,
        "canonical_replay_checks": replays,
        "drive_center_s": DRIVE_CENTER_S,
        "drive_sigma_s": list(DRIVE_SIGMA_S),
        "frozen_comparison_frequency_hz": [40, 80],
        "results": results,
        "decision": {
            "canonical_solver_self_convergence": "SELF_CONVERGENCE_FAILED",
            "independent_source_waveform_validated": False,
            "cross_solver_eligible": False,
            "production_ready": False,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--high-evidence", type=Path, required=True)
    p.add_argument("--raw-work-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    evidence = json.loads(args.high_evidence.read_text(encoding="utf-8"))
    output = evaluate(evidence, args.raw_work_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    for row in output["results"]:
        print(
            "GAUSSIAN", row["gaussian_sigma_s"], "adjacent RMS",
            [float(x["complex_rms_relative"]) for x in row["pairs"]],
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
