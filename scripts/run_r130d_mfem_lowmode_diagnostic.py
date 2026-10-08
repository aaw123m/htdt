#!/usr/bin/env python3
"""Exploratory: isolate R130D finite-record MFEM high-mode pollution (#938).

Uses independently emitted pinned MFEM CSR systems, original point source
and receiver, original [0,0.25s) Fourier kernel, and 40/80-Hz observables.
The modes above each diagnostic cutoff are deliberately omitted; the
filtered transfer is NOT the original full-basis transfer and can never
unblock the frozen R130D reference or physical production gates.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import platform
import sys
from typing import Any

import numpy as np
import scipy
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import eigsh

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "backend" / "src"))
from htdt.r130d_general3d_validation import compare_complex_transfer

CUTOFFS_HZ = (90, 95, 100)
COMPARISON_HZ = (40.0, 80.0)
MODAL_TARGET_HZ = 40.0
MODAL_SOLVER_COUNT = 32


def _matrix(payload: dict[str, Any], ndofs: int) -> csr_matrix:
    if (payload["rows"], payload["cols"]) != (ndofs, ndofs):
        raise ValueError("MFEM CSR matrix shape mismatch")
    a = csr_matrix(
        (np.asarray(payload["values"], dtype=np.float64),
         np.asarray(payload["column_indices"], dtype=np.int32),
         np.asarray(payload["row_offsets"], dtype=np.int32)),
        shape=(ndofs, ndofs),
    )
    if a.nnz != payload["nnz"]:
        raise ValueError("MFEM CSR nonzero count mismatch")
    return a


def solve_system(p: Path) -> dict:
    doc = json.loads(p.read_text(encoding="utf-8"))
    if (doc["schema_version"] != "htdt.r130d.mfem-sloped-system-1"
            or doc["boundary_model"] != "natural_neumann_rigid"):
        raise ValueError("MFEM physical authority mismatch")
    n = int(doc["ndofs"])
    mass = _matrix(doc["mass_matrix"], n)
    stiff = _matrix(doc["stiffness_c2_matrix"], n)
    vals, vec = eigsh(
        stiff, M=mass, k=MODAL_SOLVER_COUNT,
        sigma=(2 * math.pi * MODAL_TARGET_HZ) ** 2,
        which="LM", tol=1e-11, maxiter=18000,
    )
    order = np.argsort(vals)
    vals = vals[order]
    vec = vec[:, order]
    if float(np.min(vals)) < -1e-5:
        raise ValueError("material negative eigenvalue in pinned MFEM system")
    freq = np.sqrt(np.maximum(vals, 0.0)) / (2 * math.pi)
    if float(freq.min()) > 0.01 or float(freq.max()) <= max(CUTOFFS_HZ):
        raise ValueError("incomplete low-mode coverage for declared cutoffs")
    err = np.linalg.norm(
        stiff @ vec - (mass @ vec) * vals[np.newaxis, :],
        axis=0,
    ) / np.maximum(np.linalg.norm(stiff @ vec, axis=0), 1.0)
    if np.any(err > 1e-6):
        raise ValueError("MFEM generalized low-mode residual failed")
    source = np.asarray(doc["source_functional"], dtype=np.float64).ravel()
    receiver = np.asarray(doc["receiver_functional"], dtype=np.float64).ravel()
    if source.shape != (n,) or receiver.shape != (n,):
        raise ValueError("source/receiver dimension mismatch")
    # Unit discrete source q[0]=1, full-basis formula in original runner:
    # phi_t0 = c^2 dt M^-1 b, modal velocity = c^2 dt V^T b,
    # pressure = rho r^T phi_t. Zero-lag and Fourier are unchanged.
    sample_rate = 12000
    dt = 1 / sample_rate
    time = np.arange(int(0.25 * sample_rate)) * dt
    c = float(doc["sound_speed_m_s"])
    rho = float(doc["density_kg_m3"])
    coupling = (source @ vec) * (receiver @ vec)
    amp = (rho * c * c * dt) * coupling
    kernel = np.exp(2j * math.pi * np.asarray(COMPARISON_HZ)[:, None] * time[None, :])
    transfers = {}
    for cutoff in CUTOFFS_HZ:
        selected = freq <= cutoff
        pressure = np.cos(
            2 * math.pi * freq[selected, None] * time[None, :]
        ).T @ amp[selected]
        transfer = kernel @ pressure  # exact P_T/Q_T: q[0]=1 => Q_T=dt
        transfers[str(cutoff)] = {
            "mode_count": int(np.count_nonzero(selected)),
            "transfer_complex": [[float(z.real), float(z.imag)] for z in transfer],
        }
    return {
        "refinement": int(doc["uniform_refinements"]),
        "ndofs": n,
        "lowmode_frequency_hz": [float(x) for x in freq if x <= 100],
        "lowmode_relative_eigen_residual_max": float(np.max(err)),
        "transfers": transfers,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mfem-system-root", type=Path, required=True)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if (np.__version__, scipy.__version__) != ("1.26.4", "1.14.1"):
        raise ValueError("required NumPy/SciPy references must remain pinned")
    frozen = json.loads(a.baseline.read_text(encoding="utf-8"))
    if [int(x["refinement"]) for x in frozen["mfem_levels"]] != [1, 2, 3]:
        raise ValueError("R130D archived reference schedule altered")
    levels = [
        solve_system(a.mfem_system_root / "work" / f"mfem-r{r}" / "system.json")
        for r in (1, 2, 3)
    ]
    assessments = []
    for cutoff in CUTOFFS_HZ:
        pairs = []
        for coarse, fine in zip(levels, levels[1:]):
            metrics = compare_complex_transfer(
                reference=fine["transfers"][str(cutoff)]["transfer_complex"],
                candidate=coarse["transfers"][str(cutoff)]["transfer_complex"],
                frequency_hz=COMPARISON_HZ,
                magnitude_mask_relative_db=-50,
            ).model_dump(mode="json")
            pairs.append({
                "coarse_refinement": coarse["refinement"],
                "fine_refinement": fine["refinement"],
                **metrics,
            })
        assessments.append({"cutoff_hz": cutoff, "pairs": pairs})
    result = {
        "schema_version": "htdt.r130d.mfem-exploratory-lowmode-contamination-1",
        "contract": {
            "source": "same unit discrete point impulse",
            "observable": "finite-record P_T/Q_T after EXPLICIT modal truncation",
            "physics_changed_from_canonical_full_basis": True,
            "canonical_comparison_bins_hz": list(COMPARISON_HZ),
            "duration_s": 0.25,
            "modal_cutoffs_hz": list(CUTOFFS_HZ),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "platform": platform.platform(),
        },
        "levels": levels,
        "cutoff_comparisons": assessments,
        "legacy_fullbasis_metrics": frozen["adjacent_comparisons"]["mfem"],
        "decision": {
            "exploratory_execution": "PASS",
            "canonical_mfem_self_convergence": "SELF_CONVERGENCE_FAILED",
            "cross_solver_eligible": False,
            "production_ready": False,
        },
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for row in assessments:
        print("CUTOFF",row["cutoff_hz"], "adjacent",
              [(x["complex_rms_relative"], x["phase_max_deg"])
               for x in row["pairs"]], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
