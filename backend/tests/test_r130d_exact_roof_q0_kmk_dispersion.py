"""Independently check conservative KM^-1K SPD, uniform-grid dispersion and FAIL."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from scipy import sparse, linalg
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from htdt.r130d_conservative_dispersion_correction import (
    corrected_neumann_stiffness, corrected_eigenvalues)
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_exact_roof_q0_kmk_dispersion import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_kmk_dispersion_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_kmk_dispersion_evidence_2026-10-09.json"
PREVIOUS=ROOT/"benchmarks/acoustics/r130d_native_exact_roof_full_xy_z_modal_q0_evidence_2026-10-09.json"


def uniform_1d_neumann(n=40, length=4.0, c=343.2):
    h=length/n
    L=sparse.diags([-np.ones(n-1),[1,2,*([2]*(n-3)),1],-np.ones(n-1)],
                   offsets=[-1,0,1],shape=(n,n),format="csr")
    M=sparse.eye(n,format="csr")*h
    K=(c*c/h)*L
    return h,M,K


def test_true_conservative_spd_constant_kernel_and_energy():
    h,M,K=uniform_1d_neumann()
    c=343.2
    K4=corrected_neumann_stiffness(M,K,h_m=h,sound_speed_m_s=c)
    mat=K4.toarray()
    assert np.max(abs(mat-mat.T))<1e-7
    assert np.max(abs(mat@np.ones(40)))<1e-7
    eigen=np.linalg.eigvalsh(mat)
    assert eigen[0]>-2e-7
    assert eigen[1]>0
    for x in (np.sin(np.arange(40)*.21),np.arange(40)/40):
        d=float(x@(K4-K)@x)
        assert d>=-1e-9*max(float(x@K@x),1)
    assert (K4-K).nnz>0


def test_uniform_grid_low_mode_spatial_dispersion_improves():
    n=40
    h,M,K=uniform_1d_neumann(n)
    K4=corrected_neumann_stiffness(M,K,h_m=h)
    original=linalg.eigh(K.toarray(),M.toarray(),eigvals_only=True)
    corrected=linalg.eigh(K4.toarray(),M.toarray(),eigvals_only=True)
    analytic=(343.2*np.pi/4)**2
    assert abs(corrected[1]-analytic)<abs(original[1]-analytic)
    assert np.allclose(corrected[1:],
        corrected_eigenvalues(original[1:],h_m=h),
        rtol=2e-11,atol=2e-5)


@pytest.mark.parametrize("bad",[-1.0,0.0])
def test_negative_or_zero_mass_rejected(bad):
    h,M,K=uniform_1d_neumann()
    v=M.diagonal().copy()
    v[3]=bad
    with pytest.raises(ValueError):
        corrected_neumann_stiffness(sparse.diags(v,format="csr"),K,h_m=h)


def test_frozen_plan_fail_closed():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    assert validate_plan(p)==p
    assert p["scheme"]["alpha_multiplier_h2_over_c2"]==1/12
    assert p["frozen_input"]["original_native_q0"].startswith("q[0]=1")
    assert p["frozen_input"]["original_native_HDF5_source_receiver_eight_nodes"]
    assert p["limits"]["new_github_actions_runs"]==0
    for field,value in (("frozen_complex_limit",.8),
                        ("frozen_magnitude_limit",7),
                        ("frozen_phase_deg_limit",180)):
        changed=json.loads(PLAN.read_text(encoding="utf-8"))
        changed["precommitted_tests"][field]=value
        with pytest.raises(ValueError,match="plan changed"):
            validate_plan(changed)


def test_true_full_mode_evidence_baseline_and_candidate_fail_preserved():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    ev=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    previous=json.loads(PREVIOUS.read_text(encoding="utf-8"))
    assert ev["preregistered_plan"]==p
    assert ev["plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert ev["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert ev["physical_validation"]=="NOT_VALIDATED" and ev["product"]=="NO_GO"
    assert ev["new_native_PFFDTD_wave_runs"]==ev["new_GitHub_Actions_runs"]==0
    ref={q["ppw"]:q for q in previous["actual_untruncated_3D_native_eigenmode_cases"]}
    assert [q["ppw"] for q in ev["actual_full_mode_exact_roof_kmk_cases"]]==[40,44]
    grid={q["ppw"]:q for q in ev["actual_full_mode_exact_roof_kmk_cases"]}
    for ppw in (40,44):
        case=grid[ppw]
        assert case["all_true_3D_native_roof_modes_count"]==ref[ppw]["total_full_untruncated_3D_native_modes"]
        assert abs(case["volume_m3"]-56)<2e-8
        assert case["native_original_q0_and_250ms_and_40_80_unchanged"]
        assert case["new_native_PFFDTD_full_wave_runs"]==0
        assert case["native_newmark_control_vs_archived_true_wave_complex_relative"]<2e-5
        assert case["actual_true_corrected_sparse_symmetry"]
        assert case["actual_true_corrected_sparse_constant_neumann_relative_residual"]<1e-8
        assert case["actual_true_corrected_sparse_eigenmode_relative_residual"]<2e-5
        assert case["actual_true_KmkK_3D_sparse_nnz"]>case["all_true_3D_native_roof_modes_count"]
        baseline=unpairs(case["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"]["native_exact_roof_newmark"])
        archived=unpairs(ref[ppw]["archived_direct_full_wave_250ms_signed_40_80"])
        assert np.linalg.norm(baseline-archived)/np.linalg.norm(archived)<2e-5
        for r in case["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"].values():
            assert unpairs(r).shape==(2,)
            assert np.isfinite(unpairs(r)).all()
    assert ev["candidate_is_experimental_not_original_native_pffdtd"]
    assert set(ev["all_original_ppw40_44_signed_comparisons"])=={
        "native_exact_roof_newmark","conservative_kmk_dispersion_newmark"}
    for name,row in ev["all_original_ppw40_44_signed_comparisons"].items():
        c=grid[40]["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"][name]
        f=grid[44]["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"][name]
        actual=compare_complex_transfer(reference=f,candidate=c,
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        recorded=row["unchanged_two_bin_original_metrics"]
        for field in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert abs(actual[field]-recorded[field])<1e-11
        assert np.allclose(unpairs(row["ppw40_minus_44_signed_transfer"]),
                           unpairs(c)-unpairs(f),rtol=1e-12,atol=1e-9)
        assert not row["passes_frozen_all_limits"]
        assert actual["complex_rms_relative"]>.2 or actual["magnitude_max_relative"]>.25 or actual["phase_max_deg"]>15
    original=ev["all_original_ppw40_44_signed_comparisons"]["native_exact_roof_newmark"]["unchanged_two_bin_original_metrics"]
    candidate=ev["all_original_ppw40_44_signed_comparisons"]["conservative_kmk_dispersion_newmark"]["unchanged_two_bin_original_metrics"]
    assert candidate["complex_rms_relative"]<original["complex_rms_relative"]
    assert candidate["magnitude_max_relative"]>original["magnitude_max_relative"]
