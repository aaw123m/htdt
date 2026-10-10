#!/usr/bin/env python3
"""Prospective full five-grid, two-propagator conforming degree-four SEM."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/src"))
from htdt.r130d_boundary_fitted_sem import build_boundary_fitted_sem, original_eight_functional, all_mass_normalized_modes
from htdt.r130d_exact_semidiscrete_causal_q0 import (
    exact_continuous_modal_delta_impulse_signed_original_250ms,
    exact_continuous_modal_delta_impulse_roof_weak)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_q0_cartesian_flux_true_roof_hybrid import fixed_first_roof_weak_full_modes
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from run_r130d_native_exact_roof_fv_q0 import pairs
from run_r130d_original_point_quadratic_pffdtd import file_hash

PLAN = "benchmarks/acoustics/r130d_boundary_fitted_sem_plan_2026-10-10.json"
PLAN_COMMIT = "b46bbf8"
PPW = (28, 32, 36, 40, 44)
ARMS = ("native_dt_newmark", "native_sample_exact_continuous_time")


def verify_plan():
    raw = (ROOT / PLAN).read_bytes().replace(b"\r\n", b"\n")
    frozen = subprocess.check_output(["git", "-C", str(ROOT), "show", PLAN_COMMIT+":"+PLAN])
    if frozen.replace(b"\r\n", b"\n") != raw:
        raise ValueError("prospective spectral element plan modified")
    subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", PLAN_COMMIT,
                    "origin/feat/r130d-embedded-neumann-fv-20261009"], check=True)
    plan = json.loads(raw)
    if (plan["degree"] != 4 or plan["ppw"] != list(PPW)
            or plan["temporal_arms"] != list(ARMS)
            or plan["acceptance"] != {"complex_rms_relative": .2,
                                     "magnitude_max_relative": .25, "phase_max_deg": 15}):
        raise ValueError("spectral element frozen controls invalid")
    return plan, hashlib.sha256(raw).hexdigest()


def one_case(ppw, folder, native, plan, cache):
    started = time.perf_counter()
    hashes = {}
    for name, key in [("comms_out.h5", "original_native_comm_sha256"),
                       ("vox_out.h5", "original_solver_geometry_sha256")]:
        hashes[name] = file_hash(folder / name)
        if hashes[name] != native[key]:
            raise ValueError("original physical HDF5 SHA mismatch")
    with h5py.File(folder / "vox_out.h5", "r") as f:
        axes = [np.asarray(f[k][:], float) for k in ("xv", "yv", "zv")]
    dims = tuple(map(len, axes))
    with h5py.File(folder / "comms_out.h5", "r") as f:
        si, ri = np.asarray(f["in_ixyz"][:], int), np.asarray(f["out_ixyz"][:], int)
        sig = np.asarray(f["in_sigs"][:], float)
        rw = np.asarray(f["out_alpha"][:], float).ravel()
        nt = int(f["Nt"][()])
        if sig.shape != (8, nt) or np.any(sig[:, 1:] != 0) or int(f["diff"][()]) != 0:
            raise ValueError("original eight-node q0 time samples changed")
        strength = float(sig[:, 0].sum())
        sw = sig[:, 0] / strength
    with h5py.File(folder / "sim_consts.h5", "r") as f:
        dt, h, c = (float(f[k][()]) for k in ("Ts", "h", "c"))
        l2 = float(f["l2"][()])
    if abs(c-343.2) > 1e-12 or abs(h-c/(100*ppw)) > 1e-12 or abs(strength-l2/h) > 1e-10:
        raise ValueError("original physical native source normalization changed")
    def coordinates(ids):
        ijk = np.unravel_index(ids, dims)
        return np.column_stack([a[i] for a, i in zip(axes, ijk)])
    source, receiver = coordinates(si), coordinates(ri)
    np.testing.assert_allclose(sw @ source, [1.5, 2, 2], atol=3e-10, rtol=0)
    np.testing.assert_allclose(rw @ receiver, [2.5, 2, 2], atol=3e-10, rtol=0)
    fem = build_boundary_fitted_sem(h, degree=plan["degree"])
    sx, sy, sp = original_eight_functional(fem, source, sw)
    rx, ry, rp = original_eight_functional(fem, receiver, rw)
    if len(fem.yz_mass) > plan["max_yz_nodes"] or len(fem.x.mass)*len(fem.yz_mass) > plan["max_3d_modes"]:
        raise ValueError("preregistered SEM capacity exceeded")
    xlam, xv, xp = all_mass_normalized_modes(fem.x.mass, fem.x.stiffness)
    ylam, yv, yp = all_mass_normalized_modes(fem.yz_mass, fem.yz_stiffness)
    lam = (xlam[:, None] + ylam[None, :]).ravel()
    coupling = np.outer((sx @ xv)*(rx @ xv), (sy @ yv)*(ry @ yv)).ravel()
    kick = (c*dt)**2 * coupling / (1.+dt**2*lam/4.)
    newmark = entire_original_finite_record_signed_modes(lam, kick, dt, nt, 1.2).sum(axis=1)
    exact = exact_continuous_modal_delta_impulse_signed_original_250ms(lam, coupling, dt, nt).sum(axis=1)
    roof_nm = fixed_first_roof_weak_full_modes(lam, kick, dt, nt, axes, dims, si, sw, ri, rw)
    roof_ex = exact_continuous_modal_delta_impulse_roof_weak(lam, coupling, dt, nt)
    for actual, anchor in zip(roof_ex, roof_nm):
        expected = anchor["independent_true_roof_64_pair_single_bounce_weak"]
        ratio = actual["true_original_native_exact_time_allmode_roof_window_weak"] / expected
        actual.update(independent_true_roof_64_pair_single_bounce_weak=expected,
                      signed_over_analytic_roof_ratio=ratio,
                      relative_vs_analytic_roof=abs(ratio-1.))
    y, z = fem.yz_positions.T
    energy_errors = {}
    for name, u, energy in [("y", y, 14*c*c), ("z", z, 14*c*c),
                            ("roof_tangent", y-.25*z, 14*1.0625*c*c)]:
        energy_errors[name] = float(abs(u @ (fem.yz_stiffness @ u)/energy-1.))
    roof_ids = np.arange(1, len(fem.y.nodes)-1)*len(fem.eta.nodes)+len(fem.eta.nodes)-1
    roof_residual = float(np.max(abs((fem.yz_stiffness @ (y-.25*z))[roof_ids])))
    if max(energy_errors.values()) > 2e-10 or roof_residual > 2e-7:
        raise ValueError("physical spectral affine/roof Neumann invariant invalid")
    # Persist every mode for independent transient/observer checks, without filtering.
    cache.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache / f"ppw{ppw}.npz", lam=lam, coupling=coupling, dt=dt, nt=nt,
                        source=source, receiver=receiver, sw=sw, rw=rw)
    for name, expected_hash in hashes.items():
        if file_hash(folder/name) != expected_hash:
            raise ValueError("original HDF5 changed during experiment")
    return {"ppw": ppw, "original_hdf5_sha256": hashes,
            "native_h_m": h, "native_dt_s": dt, "native_Nt": nt,
            "source_original_total_kick": strength, "degree": fem.x.degree,
            "elements_each_axis": fem.x.elements, "x_nodes": len(xlam),
            "yz_nodes": len(ylam), "complete_3d_modes": len(lam),
            "physical_volume_m3": float(fem.x.mass.sum()*fem.yz_mass.sum()),
            "minimum_positive_yz_nodal_mass_m2": float(min(fem.yz_mass)),
            "no_cut_cells_or_cut_sliver_supports": True,
            "affine_energy_relative_errors": energy_errors,
            "roof_tangent_weak_residual": roof_residual,
            "source_functional_proof": sp, "receiver_functional_proof": rp,
            "x_complete_modes": xp, "yz_complete_modes": yp,
            "native_dt_squared_lambda_max": float(dt**2*lam[-1]),
            "native_explicit_leapfrog_stable": bool(dt**2*lam[-1] < 4.),
            "signed_original_250ms_40_80": {ARMS[0]: pairs(newmark), ARMS[1]: pairs(exact)},
            "first_roof_weak": {ARMS[0]: roof_nm, ARMS[1]: roof_ex},
            "elapsed_s": time.perf_counter()-started}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-sims-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    plan, sha = verify_plan()
    native = json.loads((ROOT / "benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json").read_text(encoding="utf8"))
    originals = {r["ppw"]: r for r in native["actual_native_wave_cases"]}
    folders = {}
    for f in args.original_sims_root.rglob("comms_out.h5"):
        digest = file_hash(f)
        hit = [ppw for ppw in PPW if digest == originals[ppw]["original_native_comm_sha256"]]
        if len(hit) == 1:
            if hit[0] in folders:
                raise ValueError("ambiguous original eight-node source SHA")
            folders[hit[0]] = f.parent
    if set(folders) != set(PPW):
        raise ValueError("all five original SHA-pinned HDF5 sources required")
    result = {"schema_version": "htdt.r130d.boundary-fitted-sem-evidence-1",
              "plan_commit": PLAN_COMMIT, "plan_sha256_lf": sha, "preregistered_plan": plan,
              "original_PFFDTD": "SELF_CONVERGENCE_FAILED", "physical_validation": "NOT_VALIDATED",
              "product": "NO_GO", "cases": [], "adjacent_pairs": [],
              "new_original_PFFDTD_waves": 0, "new_github_actions": 0}
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False)+"\n", encoding="utf8")
    for ppw in PPW:
        case = one_case(ppw, folders[ppw], originals[ppw], plan, ROOT / "scratch/boundary-fitted-sem")
        result["cases"].append(case)
        save()
        print("SEM_FULL", ppw, "modes", case["complete_3d_modes"], "seconds", round(case["elapsed_s"], 2),
              "signed", case["signed_original_250ms_40_80"], flush=True)
    for coarse, fine in zip(result["cases"], result["cases"][1:]):
        row = {"coarse_ppw": coarse["ppw"], "fine_ppw": fine["ppw"], "arms": {}}
        for arm in ARMS:
            score = compare_complex_transfer(reference=fine["signed_original_250ms_40_80"][arm],
                candidate=coarse["signed_original_250ms_40_80"][arm], frequency_hz=[40,80],
                magnitude_mask_relative_db=-50).model_dump(mode="json")
            passed = all(score[k] <= limit for k, limit in plan["acceptance"].items())
            row["arms"][arm] = {"metrics": score, "all_three_gates_pass": passed}
            print("SEM_PAIR", coarse["ppw"], fine["ppw"], arm, score, passed, flush=True)
        result["adjacent_pairs"].append(row)
    result["verdict"] = {}
    for arm in ARMS:
        rows = [r["arms"][arm] for r in result["adjacent_pairs"]]
        gates = all(r["all_three_gates_pass"] for r in rows)
        mono = all(all(b["metrics"][k] < a["metrics"][k] for k in plan["acceptance"])
                   for a, b in zip(rows, rows[1:]))
        result["verdict"][arm] = {"all_four_pairs_pass": gates, "strict_monotone": mono,
                                  "experimental_convergence_pass": gates and mono}
    save()
    print("SEM_VERDICT", result["verdict"], flush=True)


if __name__ == "__main__":
    main()
