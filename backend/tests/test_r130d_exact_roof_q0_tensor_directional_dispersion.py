"""Fail-closed x/roof-yz full 3D conservative dispersion separation controls."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy import sparse
from scipy import linalg

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_tensor_directional_dispersion import (
    ARMS,directional_eigenvalues,directional_stiffness)
from htdt.r130d_native_exact_roof_separable import NativeRoofSeparable
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_exact_roof_q0_tensor_directional_dispersion import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_tensor_directional_dispersion_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_tensor_directional_dispersion_evidence_2026-10-09.json"
PRIOR_FULL_KMK=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_kmk_dispersion_evidence_2026-10-09.json"
PRIOR_CG=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_kmk_direct_wave_replay_evidence_2026-10-09.json"


def toy_roof_separable():
    # Exact small tensor Neumann SPD fixture with nonuniform *true* control masses.
    mx=np.array([.5,.8,1.2,1.5])
    my=np.array([.12,.2,.3,.21,.17])
    def neumann(weights):
        n=len(weights)+1
        d=np.r_[weights[0],weights[:-1]+weights[1:],weights[-1]]
        return sparse.diags([-weights,d,-weights],[-1,0,1],format="csr")
    xk=neumann(np.array([2.2,3.1,1.7]))
    yk=neumann(np.array([.7,1.2,1.8,1.1]))
    return NativeRoofSeparable(
        np.arange(4),np.arange(5),mx,my,
        sparse.diags(mx,format="csr"),sparse.diags(my,format="csr"),xk,yk,
        5,1,(4,5,1),.1)


def test_true_toy_tensor_3d_sparse_eigenvalues_and_psd():
    sep=toy_roof_separable()
    x,ux=linalg.eigh(sep.Kx.toarray(),sep.Mx.toarray())
    y,uy=linalg.eigh(sep.Kyz.toarray(),sep.Myz.toarray())
    analytic=directional_eigenvalues(x,y,h_m=.1)
    fullmass=sparse.kron(sep.Mx,sep.Myz,format="csr").toarray()
    for arm in ARMS:
        k=directional_stiffness(sep,arm=arm).toarray()
        assert np.max(abs(k-k.T))<1e-9
        assert np.max(abs(k@np.ones(sep.total_cells)))<1e-9
        true=linalg.eigh(k,fullmass,eigvals_only=True)
        assert true[0]>-1e-8
        assert np.max(abs(true-np.sort(analytic[arm].ravel())))<3e-8


def test_directional_exact_prewritten_physics_and_thresholds():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["operator"]["arms"]==list(ARMS)
    assert p["operator"]["alpha_fraction"]==1/12
    assert p["original_provenance"]["ppw"]==[40,44]
    assert p["original_provenance"]["target_frequency_hz"]==[40,80]
    assert p["original_provenance"]["physical_source_m"]==[1.5,2,2]
    assert p["original_provenance"]["physical_receiver_m"]==[2.5,2,2]
    assert p["limits"]["new_original_pffdtd_wave_runs"]==0
    assert p["limits"]["new_github_actions_runs"]==0


@pytest.mark.parametrize("key,value",[
    ("complex_limit",.5),("magnitude_limit",.9),("phase_deg_limit",180),
    ("full_kmk_vs_prior_modal_relative_max",.9)])
def test_plan_metric_relaxation_rejected(key,value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["evaluation"][key]=value
    with pytest.raises(ValueError,match="plan changed"):
        validate_plan(p)


def test_no_hidden_high_frequency_filter_and_full_original_fail_flags():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    cg=json.loads(PRIOR_CG.read_text(encoding="utf-8"))
    original=json.loads(PRIOR_FULL_KMK.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["prospective_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["physical_validation"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_upstream_pffdtd_wave_runs"]==e["new_github_actions_runs"]==0
    assert e["not_an_original_pffdtd_production_or_bras_gate"]
    grid=e["true_fullmode_original_native_cases"]
    assert [q["ppw"] for q in grid]==[40,44]
    archived={q["ppw"]:q for q in original["actual_full_mode_exact_roof_kmk_cases"]}
    for q in grid:
        assert q["all_modes_including_high_and_point_q0_kept"]
        ppw=q["ppw"]
        assert q["true_all_eigenmodes_count"]==archived[ppw]["all_true_3D_native_roof_modes_count"]
        assert abs(q["room_physical_exact_volume_m3"]-56)<2e-8
        assert q["native_newmark_base_vs_prior_true_original_CG_wave_relative"]<2e-5
        assert q["full_kmk_arm_vs_prior_KmkK_full_mode_relative"]<2e-8
        assert set(q["true_independent_sparse_operator_checks"])==set(ARMS)
        assert set(q["every_full_mode_250ms_signed_transfer_40_80"])==set(ARMS)
        for arm in ARMS:
            op=q["true_independent_sparse_operator_checks"][arm]
            assert op["rigid_neumann_constant_null_residual_relative"]<1e-8
            assert op["true_3D_eigenresidual_relative"]<2e-5
            assert op["true_3D_sparse_nonzero_count"]>=q["true_all_eigenmodes_count"]
            z=unpairs(q["every_full_mode_250ms_signed_transfer_40_80"][arm])
            assert z.shape==(2,) and np.isfinite(z).all()
        assert q["true_independent_sparse_operator_checks"]["full_kmk"][
            "matches_independently_assembled_full_3D_KmkK_relative_max"]<1e-10
        actual=unpairs(q["every_full_mode_250ms_signed_transfer_40_80"]["full_kmk"])
        earlier=unpairs(archived[ppw]["full_untruncated_source_receiver_q0_signed_two_bin_by_arm"][
            "conservative_kmk_dispersion_newmark"])
        assert np.linalg.norm(actual-earlier)/np.linalg.norm(earlier)<2e-8
    assert [q["arm"] for q in e["all_arms_40_to_44"]]==list(ARMS)
    for q in e["all_arms_40_to_44"]:
        arm=q["arm"]
        c=grid[0]["every_full_mode_250ms_signed_transfer_40_80"][arm]
        f=grid[1]["every_full_mode_250ms_signed_transfer_40_80"][arm]
        delta=unpairs(c)-unpairs(f)
        assert np.allclose(delta,unpairs(q["signed_original_PP40_minus_PP44_two_bin"]),
                           rtol=1e-11,atol=1e-8)
        actual=compare_complex_transfer(reference=f,candidate=c,
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        score=q["frozen_original_complex_magnitude_phase"]
        for name in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert abs(actual[name]-score[name])<1e-10
        passes=(actual["complex_rms_relative"]<=.2 and
                actual["magnitude_max_relative"]<=.25 and
                actual["phase_max_deg"]<=15)
        assert passes is q["passes_original_three_limits"]
        assert not passes
    # Guard independent full-state replay and the actual complete full corrected K.
    direct=cg["actual_corrected_full_state_PP40_44_original_three_gate_metrics"]
    full=[q for q in e["all_arms_40_to_44"] if q["arm"]=="full_kmk"][0]
    assert abs(full["frozen_original_complex_magnitude_phase"]["complex_rms_relative"]-
               direct["complex_rms_relative"])<3e-8
