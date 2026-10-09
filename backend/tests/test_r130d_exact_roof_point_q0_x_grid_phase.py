"""Exact physical point conservation on translated native x-only grids."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_exact_roof_point_q0_x_grid_phase import (
    PPW,PHASES,verify_plan,trilinear_physical_point,
    exact_same_original_stencil,unpairs,evaluate_signed_error)

PLAN=ROOT/"benchmarks/acoustics/r130d_exact_roof_original_point_q0_x_grid_phase_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_exact_roof_original_point_q0_x_grid_phase_evidence_2026-10-09.json"

def prereg():
    raw=PLAN.read_bytes()
    return verify_plan(json.loads(raw.decode("utf-8")))

def frozen():
    raw=PLAN.read_bytes()
    p=verify_plan(json.loads(raw.decode("utf-8")))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert d["preregistered_plan"]==p
    return p,d

def test_original_x_grid_phase_plan_only_predefined_two_offsets_no_actions():
    p=prereg()
    assert p["reference_data"]["native_ppw"]==list(PPW)==[40,44]
    assert p["controlled_perturbation"]["x_grid_offset_in_native_h"]==list(PHASES)
    assert p["controlled_perturbation"]["unchanged_yz_node_coordinates"] is True
    assert p["reference_data"]["physical_point_source_m"]==[1.5,2,2]
    assert p["reference_data"]["physical_point_receiver_m"]==[2.5,2,2]
    assert p["limits"]["no_github_actions_runs"] is True
    assert p["qualification"]["original_PFFDTD_point_q0"]=="SELF_CONVERGENCE_FAILED"
    assert p["qualification"]["product"]=="NO_GO"

@pytest.mark.parametrize("field,value",[
    ("native_ppw",[40]),
    ("physical_point_source_m",[1.4,2,2]),
    ("analysis_frequencies_hz",[40]),
])
def test_predeclared_source_authority_fails_closed(field,value):
    p=prereg();p["reference_data"][field]=value
    with pytest.raises(ValueError):verify_plan(p)

def test_grid_shift_and_qualification_limit_mutations_fails_closed():
    p=prereg();p["controlled_perturbation"]["x_grid_offset_in_native_h"]=[-.1,.1]
    with pytest.raises(ValueError):verify_plan(p)
    p=prereg();p["analysis"]["canonical_acceptance_original_complex_max"]=2.
    with pytest.raises(ValueError):verify_plan(p)
    p=prereg();p["qualification"]["product"]="GO"
    with pytest.raises(ValueError):verify_plan(p)

@pytest.mark.parametrize("phase",[-.25,0.,.25])
@pytest.mark.parametrize("physical_xyz",[(1.5,2,2),(2.5,2,2)])
def test_cartesian_exact_physical_point_trilinear_moments_after_shift(phase,physical_xyz):
    h=.078
    original=np.arange(-.273,4.4,h)
    axes=[original+phase*h,original,original]
    ix,w,meta=trilinear_physical_point(axes,physical_xyz)
    assert ix.shape==w.shape==(8,)
    assert w.sum()==pytest.approx(1,abs=1e-14)
    assert np.min(w)>0
    assert meta["reconstructed_first_moment_xyz_m"]==pytest.approx(physical_xyz,abs=1e-12)
    assert meta["support_node_count"]==8
    assert meta["stencil_spatial_RMS_radius_m"]>0
    ii,ww,second=trilinear_physical_point(axes,physical_xyz)
    assert exact_same_original_stencil(ix,w,ii,ww)
    assert not exact_same_original_stencil(ix,w,ii,np.roll(ww,1))

def test_physical_point_grid_interpolator_rejects_outside_and_nonuniform_grid():
    xyz=[np.arange(-.273,4.4,.078)]*3
    with pytest.raises(ValueError,match="outside"):
        trilinear_physical_point(xyz,(99,2,2))
    with pytest.raises(ValueError):
        trilinear_physical_point((np.array([0,1,1.]),xyz[1],xyz[2]),(1,2,2))

def test_saved_exact_four_actual_native_point_q0_full_waves_and_no_promotion():
    p,d=frozen()
    assert d["pffdtd_pin"]==p["reference_data"]["pffdtd_pin"]
    assert d["new_github_actions_started"]==0
    assert d["product"]=="NO_GO"
    assert d["original_canonical"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["phase_study_not_full_5_grid_original_point_qualification"] is True
    assert d["cannot_claim_any_production_approval"] is True
    rows=d["actual_shifted_x_grid_full_original_q0_wave_cases"]
    assert [(r["native_grid_x_shift_fraction_h"],r["ppw"]) for r in rows]==[
        (-.25,40),(-.25,44),(.25,40),(.25,44)]
    old=json.loads((ROOT/p["reference_data"]["original_native_exact_comms"]).read_text(encoding="utf-8"))
    baseline=json.loads((ROOT/p["reference_data"]["original_exact_roof_control"]).read_text(encoding="utf-8"))
    src={r["ppw"]:r for r in old["actual_native_wave_cases"]}
    ctrl={r["ppw"]:r for r in baseline["actual_native_grid_point_impulse_exact_roof_cases"]}
    for r in rows:
        ppw=r["ppw"]
        assert r["original_pinned_native_comms_sha256"]==src[ppw]["original_native_comm_sha256"]
        assert r["original_pinned_native_vox_sha256"]==src[ppw]["original_solver_geometry_sha256"]
        assert r["native_unmodified_original_q0_stencil_confirmed"] is True
        assert r["source_receiver_xyz_and_q0_unchanged"] is True
        assert r["native_solver_original_code_and_hdf5_not_modified"] is True
        assert r["discrete_8node_weights_changed_due_only_to_node_shift"] is True
        assert r["unmodified_native_sample_count"]==ctrl[ppw]["original_record_samples"]
        assert r["unmodified_native_dt_s"]==pytest.approx(ctrl[ppw]["original_native_dt_s"],abs=1e-12)
        assert r["physical_volume_m3"]==pytest.approx(56.,abs=2e-8)
        assert r["active_exact_cutcells"]<=p["limits"]["max_active_cut_cells"]
        assert r["minimum_cell_volume_fraction_h3"]>0
        assert np.all(np.isfinite(unpairs(r["new_actual_unmodified_physical_point_q0_signed_40_80"])))
        assert r["saved_prior_unshifted_control_signed_40_80"]==ctrl[ppw]["experimental_signed_P_T_over_Q_T_40_80"]
        assert r["shifted_same_physical_source_trilinear"]["reconstructed_first_moment_xyz_m"]==pytest.approx([1.5,2,2],abs=1e-12)
        assert r["shifted_same_physical_receiver_trilinear"]["reconstructed_first_moment_xyz_m"]==pytest.approx([2.5,2,2],abs=1e-12)
        so=r["solver"]
        assert so["maximum_CG_iterations"]<=500
        assert so["maximum_true_linear_relative_residual"]<=5e-10
        assert so["relative_energy_drift_after_source"]<=1e-6
        assert so["original_discrete_q0_unchanged"] is True
        assert so["no_point_source_smoothing_or_taper"] is True
        assert r["elapsed_wall_seconds"]>0

def test_full_shifted_adjacent_metrics_recompute_from_signed_40_80():
    p,d=frozen()
    rows=d["actual_shifted_x_grid_full_original_q0_wave_cases"]
    scores=d["two_grid_phase_sensitivity"]
    assert len(scores)==2
    for phase,metric in zip(PHASES,scores):
        assert metric["phase"]==phase
        assert metric["ppw_pair"]==[40,44]
        x=next(r for r in rows if r["ppw"]==40 and r["native_grid_x_shift_fraction_h"]==phase)
        y=next(r for r in rows if r["ppw"]==44 and r["native_grid_x_shift_fraction_h"]==phase)
        recomputed=evaluate_signed_error(
            x["new_actual_unmodified_physical_point_q0_signed_40_80"],
            y["new_actual_unmodified_physical_point_q0_signed_40_80"])
        for f in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert metric[f]==pytest.approx(recomputed[f],rel=5e-12,abs=5e-12)
        assert metric["point_source_xyz_unchanged"] is True
        assert metric["point_receiver_xyz_unchanged"] is True
    roof=json.loads((ROOT/p["reference_data"]["original_exact_roof_control"]).read_text(encoding="utf-8"))
    vals={r["ppw"]:r for r in roof["actual_native_grid_point_impulse_exact_roof_cases"]}
    base=evaluate_signed_error(
        vals[40]["experimental_signed_P_T_over_Q_T_40_80"],
        vals[44]["experimental_signed_P_T_over_Q_T_40_80"])
    assert d["unshifted_ppw40_44_original_exact_roof_control"]["complex_rms_relative"]==pytest.approx(
        base["complex_rms_relative"],rel=5e-12,abs=5e-12)
