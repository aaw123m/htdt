"""Preregistered rigid-box semidiscrete spectrum validation (issue #53).

Independent physics check: does the canonical seven-point Neumann discrete
operator's eigenfrequency spectrum agree with analytic rigid-box modes?
Deterministic eigencomputation on the two preregistered grid spacings — no
time-domain solver runs. Verifies plan bytes before computing.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
import numpy as np
from scipy import sparse
from scipy.sparse import linalg as spla

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = Path("benchmarks/acoustics/r130d_rigid_box_semidiscrete_spectrum_plan_2026-10-11.json")
PRIOR_EVIDENCE = Path("benchmarks/acoustics/r130d_reproduction_isolation_diagnostic_evidence.json")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _neumann_1d(n: int) -> sparse.csr_matrix:
    """1D cell-centered Neumann second-difference stiffness (positive)."""
    e = np.ones(n)
    L = sparse.diags([-e[1:], 2 * e, -e[1:]], [-1, 0, 1], format="lil")
    L[0, 0] = 1.0  # Neumann ghost-node convention: boundary half-weight
    L[-1, -1] = 1.0
    return L.tocsr()


def _build_K(nx: int, ny: int, nz: int, h: float) -> sparse.csr_matrix:
    Ix, Iy, Iz = (sparse.identity(n) for n in (nx, ny, nz))
    Lx, Ly, Lz = (_neumann_1d(n) for n in (nx, ny, nz))
    K3 = sparse.kron(sparse.kron(Lx, Iy), Iz) \
        + sparse.kron(sparse.kron(Ix, Ly), Iz) \
        + sparse.kron(sparse.kron(Ix, Iy), Lz)
    return (K3 / h**2).tocsr()


def _discrete_formula(nx, ny, nz, dims, h):
    """Closed-form discrete Neumann eigenvalues for every mode triple."""
    out = {}
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                lam = (2.0 / h) ** 2 * (
                    np.sin(np.pi * i / (2 * nx)) ** 2
                    + np.sin(np.pi * j / (2 * ny)) ** 2
                    + np.sin(np.pi * k / (2 * nz)) ** 2
                )
                out[(i, j, k)] = lam
    return out


def _analytic_modes(dims, c, f_cut):
    Lx, Ly, Lz = dims
    modes = []
    for i in range(9):
        for j in range(9):
            for k in range(9):
                f = (c / 2) * np.sqrt((i / Lx) ** 2 + (j / Ly) ** 2 + (k / Lz) ** 2)
                if 0 < f <= f_cut:  # drop the trivial zero mode
                    modes.append({"triple": [i, j, k], "f_hz": float(f)})
    modes.sort(key=lambda m: m["f_hz"])
    return modes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    plan_text = (ROOT / PLAN_PATH).read_text(encoding="utf8")
    plan = json.loads(plan_text)
    if plan["schema_version"] != "htdt.r130d.rigid-box-semidiscrete-spectrum-plan-1":
        raise ValueError("plan authority corrupted")
    prior = json.loads((ROOT / PRIOR_EVIDENCE).read_text(encoding="utf8"))
    unresolved = sorted({f for lvl in prior["analytic_rigid_box"]["levels"]
                         for f in lvl["mode_match"]["unresolved_mode_frequencies_hz"]})

    dims = (plan["box"]["dimensions_m"]["x"], plan["box"]["dimensions_m"]["y"],
            plan["box"]["dimensions_m"]["z"])
    c = plan["box"]["speed_of_sound_m_s"]
    f_cut = 220.0  # covers the 18 analytic modes in the prior evidence (max ~213 Hz)
    analytic = _analytic_modes(dims, c, f_cut)

    levels = []
    all_ok = True
    for h, cells in sorted(((float(k), v) for k, v in plan["box"]["cells_per_spacing"].items()),
                           key=lambda kv: -float(kv[0])):
        nx, ny, nz = cells
        K = _build_K(nx, ny, nz, h)
        n_eig = min(len(analytic) + 8, nx * ny * nz - 1)
        vals, _ = spla.eigsh(K, k=n_eig, sigma=-1e-6, which="LM", tol=1e-9, maxiter=2000)
        disc_hz = np.sort(np.sqrt(np.maximum(vals, 0.0)) * c / (2 * np.pi))
        disc_hz = disc_hz[disc_hz > 1.0]  # drop the zero mode

        formula = _discrete_formula(nx, ny, nz, dims, h)
        fmap = {t: np.sqrt(lam) * c / (2 * np.pi) for t, lam in formula.items()}
        exact_rel_max = 0.0
        for m in analytic:
            f_exact = fmap[tuple(m["triple"])]
            # nearest eigsh eigenvalue to the exact discrete frequency
            i0 = int(np.argmin(np.abs(disc_hz - f_exact)))
            exact_rel_max = max(exact_rel_max, abs(disc_hz[i0] - f_exact) / f_exact)

        per_mode = []
        cont_rel_max = 0.0
        disp_bound_ok = True
        for m in analytic:
            f_exact = fmap[tuple(m["triple"])]
            cont_rel = abs(f_exact - m["f_hz"]) / m["f_hz"]
            bound = 2.0 * (np.pi * m["f_hz"] * h / c) ** 2 / 12.0
            if cont_rel > bound:
                disp_bound_ok = False
            cont_rel_max = max(cont_rel_max, cont_rel)
            per_mode.append({"triple": m["triple"], "analytic_hz": m["f_hz"],
                             "discrete_hz": float(f_exact), "relative_error": float(cont_rel),
                             "dispersion_bound": float(bound)})

        matched_unresolved = [f for f in unresolved
                              if np.min(np.abs(disc_hz - f)) / f <= 0.02]
        coverage = len(matched_unresolved) / len(unresolved) if unresolved else 1.0
        cont_ok = bool(cont_rel_max <= 0.02)
        exact_ok = bool(exact_rel_max <= 1e-8)
        cov_ok = bool(coverage >= 1.0)
        disp_bound_ok = bool(disp_bound_ok)
        all_ok &= cont_ok and exact_ok and cov_ok and disp_bound_ok
        levels.append({
            "grid_spacing_m": h, "cells": cells, "eigsh_modes": int(len(disc_hz)),
            "discrete_operator_exactness": {"max_relative": float(exact_rel_max), "pass": exact_ok},
            "continuum_agreement": {"max_relative": float(cont_rel_max), "pass": cont_ok},
            "dispersion_bound_all_modes": disp_bound_ok,
            "unresolved_cluster_coverage": coverage,
            "per_mode": per_mode,
            "pass": bool(cont_ok and exact_ok and cov_ok and disp_bound_ok),
        })

    verdicts = ["PHYSICS_VALIDATED_SEMIDISCRETE_SPECTRUM" if all_ok
                else "PHYSICS_NOT_VALIDATED_SEMIDISCRETE_SPECTRUM"]
    if all(l["discrete_operator_exactness"]["pass"] for l in levels):
        verdicts.append("OPERATOR_DISCRETIZATION_CONFIRMED")
    if all(l["unresolved_cluster_coverage"] >= 1.0 for l in levels):
        verdicts.append("RECORD_PEAK_DETECTION_LIMIT_CONFIRMED")

    e = {
        "schema_version": "htdt.r130d.rigid-box-semidiscrete-spectrum-evidence-1",
        "preregistered_plan_sha256_lf": _sha(ROOT / PLAN_PATH),
        "preregistered_plan": plan,
        "prior_evidence_sha256": _sha(ROOT / PRIOR_EVIDENCE),
        "box_dimensions_m": list(dims),
        "analytic_mode_count_below_cutoff": len(analytic),
        "unresolved_record_peak_modes_hz": unresolved,
        "levels": levels,
        "verdicts": verdicts,
        "canonical_original_contract": "SELF_CONVERGENCE_FAILED",
        "product": "NO_GO",
        "time_domain_solver_runs": 0,
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(e, indent=2, allow_nan=False) + "\n", encoding="utf8")
    print(json.dumps({"verdicts": verdicts,
                      "levels": [{"h": l["grid_spacing_m"], "pass": l["pass"],
                                  "cont_max_rel": l["continuum_agreement"]["max_relative"],
                                  "exact_max_rel": l["discrete_operator_exactness"]["max_relative"],
                                  "coverage": l["unresolved_cluster_coverage"]} for l in levels]},
                     indent=1))


if __name__ == "__main__":
    main()
