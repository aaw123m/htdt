"""Signed original physical point q0 fixed-MK source/receiver A/B, x Neumann mode.

Preserve all negative experimental outcomes, no new Actions or promotion.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_native_exact_roof_one_sided_Q2_and_x_mode import (
    PPW,OFFSETS,COMBINATIONS,validate_plan,cardinal_q2_point,
    native_x_sep_mode,unpairs,score_pair)
from run_r130d_exact_roof_point_q0_x_grid_phase import trilinear_physical_point

PLAN=ROOT/"benchmarks/acoustics/r130d_native_exact_roof_one_sided_Q2_point_and_x_mode_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_native_exact_roof_one_sided_Q2_point_and_x_mode_evidence_2026-10-09.json"

def preplan():
    return validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))

def frozen():
    raw=PLAN.read_bytes()
    p=validate_plan(json.loads(raw.decode("utf-8")))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert d["preregistered_plan"]==p
    return p,d

def test_original_physical_q0_xmode_and_point_functionals_are_frozen():
    p=preplan()
    assert p["immutable_original"]["ppw"]==list(PPW)==[40,44]
    assert p["immutable_original"]["original_source_xyz_m"]==[1.5,2,2]
    assert p["immutable_original"]["original_receiver_xyz_m"]==[2.5,2,2]
    assert p["immutable_original"]["original_frequencies_hz"]==[40,80]
    assert p["immutable_original"]["original_q0"].startswith("unchanged discrete q[0]=1")
    assert p["spatial_operator"]["number_of_full_wave_integrations"]==4
    assert p["spatial_operator"]["exact_same_native_unshifted_ppw40_ppw44_mass_M_and_stiffness_K_for_all_four_operator_combinations"] is True
    assert p["x_spectral"]["grid_phase_offsets_h"]==list(OFFSETS)
    assert p["limits"]["no_github_actions_runs"] is True
    assert p["release"]["original_eight_node_q0"]=="SELF_CONVERGENCE_FAILED"
    assert p["release"]["product"]=="NO_GO"

@pytest.mark.parametrize("key,value",[
    ("ppw",[40]),("original_source_xyz_m",[1.51,2,2]),
    ("original_receiver_xyz_m",[2.49,2,2]),
    ("original_frequencies_hz",[40]),
])
def test_original_q0_planned_physics_mutation_rejected(key,value):
    p=preplan()
    p["immutable_original"][key]=value
    with pytest.raises(ValueError):validate_plan(p)

def test_gates_or_phase_mutations_rejected():
    p=preplan();p["comparison"]["frozen_complex_rms_relative_max"]=2
    with pytest.raises(ValueError):validate_plan(p)
    p=preplan();p["x_spectral"]["grid_phase_offsets_h"]=[0]
    with pytest.raises(ValueError):validate_plan(p)
    p=preplan();p["limits"]["no_github_actions_runs"]=False
    with pytest.raises(ValueError):validate_plan(p)
    p=preplan();p["release"]["product"]="GO"
    with pytest.raises(ValueError):validate_plan(p)

@pytest.mark.parametrize("physical",[(1.5,2.,2.),(2.5,2.,2.)])
def test_Q2_delta_interpolator_exact_0_1_2_moments_not_spatially_smooth(physical):
    h=.078
    a=np.arange(-.273,4.4,h)
    ix,w,meta=cardinal_q2_point([a,a,a],physical)
    assert len(ix)==len(w)==27 and len(set(map(int,ix)))==27
    assert w.sum()==pytest.approx(1.,abs=1e-12)
    assert np.any(w<0) and meta["negative_weight_count"]>0
    assert meta["sum_absolute_weights"]>1
    assert meta["reconstructed_first_moment_xyz_m"]==pytest.approx(physical,abs=1e-12)
    assert meta["reconstructed_second_raw_moment_xyz2_m2"]==pytest.approx(
        np.asarray(physical)**2,abs=1e-12)
    assert meta["no_gaussian_or_spatial_point_move"] is True
    ix8,w8,_=trilinear_physical_point([a,a,a],physical)
    assert len(ix8)==8 and w8.sum()==pytest.approx(1.,abs=1e-12)
    assert np.all(w8>=0)

def test_analytic_x_first_neumann_mode_reproduces_physical_4m_box():
    a=np.arange(-.273,4.4,.078)
    for shift in OFFSETS:
        m=native_x_sep_mode(a,shift,.078,343.2,[1.5,2,2],[2.5,2,2])
        assert m["mass_length_x_m"]==pytest.approx(4.,abs=1e-12)
        assert m["native_constant_Neumann_x_mode_zero_frequency_hz"]<.005
        assert 42.8<m["finite_volume_neumann_1d_first_three_positive_modes_hz"][0]<43.
        assert m["continuum_flat_x_Neumann_first_frequency_hz"]==pytest.approx(42.9,abs=1e-10)
        assert m["first_three_signed_original_point_x_mode_couplings"][0]<0
        assert m["roof_yz_operator_not_in_x_1d_diagnostic"] is True

def test_all_six_archived_native_x_axis_modes_and_original_fullwave_4_cases():
    p,d=frozen()
    assert d["schema_version"]=="htdt.r130d.original-exact-roof-fixed-mk-operator-and-x-mode-evidence-1"
    assert d["original_canonical_point_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["product"]=="NO_GO"
    assert d["new_github_actions_started"]==0
    modes=d["actual_1D_native_x_modes_and_point_overlap"]
    assert [r["ppw"] for r in modes]==list(PPW)
    for row in modes:
        assert [z["grid_phase_h"] for z in row["phase_modes"]]==list(OFFSETS)
        for z in row["phase_modes"]:
            assert z["mass_length_x_m"]==pytest.approx(4.,abs=1e-10)
            assert z["native_constant_Neumann_x_mode_zero_frequency_hz"]<.005
            assert len(z["finite_volume_neumann_1d_first_three_positive_modes_hz"])==3
            assert len(z["first_three_signed_original_point_x_mode_couplings"])==3
    cases=d["actual_3D_original_q0_full_wave_fixed_MK_operator_cases"]
    assert [(r["ppw"],r["discrete_source_operator"]) for r in cases]==[
        (40,"8"),(40,"Q2"),(44,"8"),(44,"Q2")]
    control=json.loads((ROOT/p["immutable_original"]["unshifted_prior_evidence"]).read_text(encoding="utf-8"))
    original={r["ppw"]:r for r in control["actual_native_grid_point_impulse_exact_roof_cases"]}
    for r in cases:
        base=original[r["ppw"]]
        assert r["fixed_MK_active_cells"]==base["original_nodal_active_cutcell_count"]
        assert r["fixed_MK_stiffness_nnz"]==base["native_exact_roof_stiffness_nonzeros"]
        assert r["fixed_MK_exact_roof_room_volume_m3"]==pytest.approx(56,abs=2e-8)
        assert r["time_step_s"]==pytest.approx(base["original_native_dt_s"],abs=1e-12)
        assert r["original_sample_count"]==base["original_record_samples"]
        assert r["frozen_original_geometry_sha256"]==base["original_native_geometry_sha256"]
        assert r["frozen_original_8node_source_sha256"]==base["original_native_comms_sha256"]
        assert r["source_receiver_physical_xyz_unchanged"] is True
        assert r["all_original_temporal_q0_unchanged"] is True
        assert r["experimental_not_original_PFFDTD_qualification"] is True
        assert r["source_Q2_original_physical_point"]["sum_signed_weights"]==pytest.approx(1.,abs=1e-12)
        assert r["receiver_Q2_original_physical_point"]["sum_signed_weights"]==pytest.approx(1.,abs=1e-12)
        solver=r["solver"]
        assert solver["max_cg_iterations"]<=p["spatial_operator"]["max_CG_iterations"]
        assert solver["max_true_CG_relative_residual"]<=p["spatial_operator"]["max_true_relative_residual"]
        assert solver["source_free_energy_relative_drift"]<=p["spatial_operator"]["max_energy_drift"]
        for name in ("receiver8","receiverQ2"):
            assert np.all(np.isfinite(unpairs(r["signed_original_40_80_receivers"][name])))
        if r["discrete_source_operator"]=="8":
            assert r["original_8_by_8_baseline_signed_complex_relative_reproduction"]<5e-8
            np.testing.assert_allclose(unpairs(r["signed_original_40_80_receivers"]["receiver8"]),
                unpairs(base["experimental_signed_P_T_over_Q_T_40_80"]),rtol=5e-8,atol=1e-6)

def test_full_one_sided_source_and_receiver_frozen_PPWs_recompute_errors_and_fail_closed():
    p,d=frozen()
    allcases=d["actual_3D_original_q0_full_wave_fixed_MK_operator_cases"]
    seen={}
    for ppw in PPW:
        variants={r["discrete_source_operator"]:r["signed_original_40_80_receivers"]
                  for r in allcases if r["ppw"]==ppw}
        seen[ppw]={"8/8":variants["8"]["receiver8"],
                   "8/Q2":variants["8"]["receiverQ2"],
                   "Q2/8":variants["Q2"]["receiver8"],
                   "Q2/Q2":variants["Q2"]["receiverQ2"]}
        for k,z in seen[ppw].items():
            np.testing.assert_allclose(unpairs(z),
                unpairs(d["fixed_MK_signed_40_80_operator_transfer_by_ppw"][str(ppw)][k]),
                rtol=2e-12,atol=2e-9)
    frozen_scores=d["spatial_operator_ab_original_q0_ppw40_44_adjacent_scores"]
    assert [r["source_receiver_numerical_point_operator"] for r in frozen_scores]==list(COMBINATIONS)
    for r in frozen_scores:
        key=r["source_receiver_numerical_point_operator"]
        actual=score_pair(seen[40][key],seen[44][key])
        for field in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert r[field]==pytest.approx(actual[field],rel=1e-11,abs=2e-10)
        assert r["compared_frequency_count"]==2
        assert isinstance(r["meets_frozen_original_40_44_gates"],bool)
    assert d["only_original_ppw40_44_not_full_canonical_8_10_12"] is True
    assert d["no_changed_native_original_PFFDTD_wave_solver"] is True
