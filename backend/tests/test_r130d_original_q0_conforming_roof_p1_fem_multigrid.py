"""True nontruncated physical sloping-roof P1 FEM, manufactured affine + q0 replay."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_conforming_roof_p1_fem import (
    build_original_native_conforming_roof_p1,
    original_eightnode_FEM_source_receiver,
    generalized_conforming_p1_neumann_modes)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_original_q0_conforming_roof_p1_fem_multigrid import (
    validate_plan,validate_ppw44_resource_addendum)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_roof_p1_fem_multigrid_plan_2026-10-09.json"
ADDENDUM=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_roof_p1_fem_ppw44_capacity_addendum_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_roof_p1_fem_multigrid_evidence_2026-10-09.json"
CG=ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_evidence_2026-10-09.json"
ORIGINAL=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"


def synthetic():
    axis=np.arange(-.2,4.201,.2)
    fem=build_original_native_conforming_roof_p1([axis,axis,axis],
                 max_yz_nodes=1000,max_3d_nodes=40000)
    return axis,fem


def test_manufactured_affine_energy_exact_physical_56m3_and_natural_neumann():
    a,f=synthetic()
    M,K=f.sparse_full_3d_operators()
    assert len(f.yz_triangles)>500
    assert abs(f.exact_cross_section_area_m2-14)<2e-10
    assert abs(np.sum(M.diagonal())-56)<2e-10
    assert M.shape==(f.total_cells,f.total_cells)
    assert np.all(M.diagonal()>0)
    skew=(K-K.T).tocoo()
    assert float(max(abs(skew.data),default=0.))<1e-8
    assert np.max(np.abs(K@np.ones(f.total_cells)))/np.max(abs(K.diagonal()))<1e-11
    xx=f.x_positions_m[:,None]
    yy=f.yz_positions_m[None,:,0]
    zz=f.yz_positions_m[None,:,1]
    g=np.array([.37,.81,-.2])
    u=(.37*xx+.81*yy-.2*zz+.4).ravel()
    physical_energy=float(u @ (K@u))
    exact_energy=56*(343.2**2)*float(g@g)
    assert abs(physical_energy-exact_energy)/exact_energy<2e-10
    # Every P1 triangle reproduces y,z-affine functions: discrete exact
    # energy is not a fit to the original acoustic PPW data.
    ly,vy,e=generalized_conforming_p1_neumann_modes(f.Myz,f.Kyz)
    assert ly.shape==(len(f.yz_positions_m),)
    assert ly[0]==0
    assert e["analytically_exact_rigid_neumann_zero_mode"]
    assert e["strict_max_generalized_relative_eigenresidual"]<2e-7
    assert e["strict_max_M_orthonormality_error"]<5e-8


def test_all_original_eight_nodes_retained_with_exact_fixed_weights():
    axis,f=synthetic()
    i=[int(np.where(np.isclose(axis,x))[0][0]) for x in (1.4,1.6)]
    j=[int(np.where(np.isclose(axis,y))[0][0]) for y in (1.8,2.)]
    k=[int(np.where(np.isclose(axis,z))[0][0]) for z in (1.8,2.)]
    inds=[];weights=[]
    for ai,wi in zip(i,(.3,.7)):
        for bj,wj in zip(j,(.4,.6)):
            for ck,wk in zip(k,(.2,.8)):
                inds.append(np.ravel_multi_index((ai,bj,ck),(len(axis),)*3))
                weights.append(wi*wj*wk)
    sx,sy,error=original_eightnode_FEM_source_receiver(f,np.array(inds),
                                                       np.array(weights))
    assert error<1e-14
    assert abs(sx.sum()-1)<1e-14 and abs(sy.sum()-1)<1e-14
    assert np.count_nonzero(sx)==2 and np.count_nonzero(sy)==4
    assert np.allclose(sorted(sx[sx!=0]),[.3,.7])
    assert np.allclose(sorted(sy[sy!=0]),sorted([
        .4*.2,.4*.8,.6*.2,.6*.8]))
    with pytest.raises(ValueError,match="eight source/receiver"):
        original_eightnode_FEM_source_receiver(f,np.array(inds),
                                                .9*np.array(weights))


def test_resource_limit_fails_closed_without_cutting_near_boundary_modes():
    axis,f=synthetic()
    with pytest.raises(ValueError,match="vertex count"):
        build_original_native_conforming_roof_p1([axis,axis,axis],
                  max_yz_nodes=200,max_3d_nodes=50000)
    with pytest.raises(ValueError,match="roof changed"):
        build_original_native_conforming_roof_p1([axis,axis,axis],slope=.3)
    assert f.total_cells==len(f.x_positions_m)*len(f.yz_positions_m)


@pytest.mark.parametrize("field,new",[
    ("frozen_complex_gate",.3),("frozen_magnitude_gate",.4),
    ("frozen_phase_deg_gate",90)])
def test_original_acceptance_thresholds_still_frozen(field,new):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["verification"][field]=new
    with pytest.raises(ValueError,match="frozen plan drift"):
        validate_plan(p)


def test_ppw44_resource_addendum_was_prospective_and_physics_fixed():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    a=validate_ppw44_resource_addendum(
        json.loads(ADDENDUM.read_text(encoding="utf-8")),p)
    assert a["original_precommitted_plan_head"]=="4950dc5bc33754fbb482700972aa01fc111893d2"
    assert a["resource_only_original_and_new"]["original_max_each_yz_vertices"]==2200
    assert a["resource_only_original_and_new"]["new_ppw44_only_max_each_yz_vertices"]==3000
    assert a["resource_only_original_and_new"]["max_true_3d_fem_dofs_unchanged"]==180000
    assert a["frozen_original"]["original_complex_gate"]==.2
    assert a["frozen_original"]["original_magnitude_gate"]==.25
    assert a["frozen_original"]["original_phase_deg_gate"]==15
    a["resource_only_original_and_new"]["new_ppw44_only_max_each_yz_vertices"]=6000
    with pytest.raises(ValueError,match="addendum mutated"):
        validate_ppw44_resource_addendum(a,p)


def test_all_five_true_natively_sourced_p1_fem_fullband_signed_q0_negative():
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    a=json.loads(ADDENDUM.read_text(encoding="utf-8"))
    previous=json.loads(CG.read_text(encoding="utf-8"))
    raw=json.loads(ORIGINAL.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["pre_observation_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["PPW44_resource_only_pre_observation_addendum"]==a
    assert e["PPW44_addendum_pre_observation_sha256_lf"]==hashlib.sha256(
        ADDENDUM.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["PPW28_32_36_40_data_were_observed_before_PP44_capacity_addendum"]
    assert e["canonical_original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physical_validation"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_original_pffdtd_waves"]==e["new_github_actions_runs"]==0
    assert e["original_upstream_PFFDTD_self_convergence_still_failed"]
    cases=e["actual_original_q0_conforming_roof_P1_FEM_cases"]
    assert [q["ppw"] for q in cases]==list(PPW)
    assert sum(q["full_original_native_grid_spanning_all_FEM_modes"] for q in cases)==401630
    old={q["ppw"]:q for q in previous["actual_native_grid_point_impulse_exact_roof_cases"]}
    orig={q["ppw"]:q for q in raw["actual_native_wave_cases"]}
    for row in cases:
        ppw=row["ppw"]
        assert row["original_native_comm_sha256"]==orig[ppw]["original_native_comm_sha256"]
        assert row["original_native_voxel_sha256"]==orig[ppw]["original_solver_geometry_sha256"]
        assert row["original_native_Nt"]==old[ppw]["original_record_samples"]
        assert abs(row["original_native_Ts_s"]-old[ppw]["original_native_dt_s"])<1e-12
        assert abs(row["physical_area_yz_m2"]-14)<2e-8
        assert abs(row["physical_true_volume_m3"]-56)<2e-8
        assert row["original_frozen_250ms_no_modal_removal_or_frequency_mask"]
        assert row["all_original_eight_input_nodes_and_receiver_nodes_mapped_without_reinterpolation"]
        assert row["additional_physical_roof_FEM_nodes_receive_no_direct_original_point_q0_force"]
        assert row["true_full_FEM_symmetric_neumann_eigenpair_rel"]<3e-7
        assert row["true_full_FEM_symmetric_Neumann_constant_null_rel"]<1e-10
        assert row["true_full_FEM_stiffness_asym_rel"]<1e-12
        assert row["true_FEM_yz_generalized_mode_checks"]["strict_max_generalized_relative_eigenresidual"]<2e-7
        assert row["full_original_native_grid_spanning_all_FEM_modes"]==(
             row["true_FEM_x_nodes"]*row["true_FEM_yz_nodes"])
        assert row["true_FEM_yz_triangles"]>=row["true_FEM_yz_nodes"]
        assert row["minimum_original_native_P1_triangle_m2"]>0
        assert np.isfinite(unpairs(row["new_FEM_full_250ms_original_point_q0_signed_40_80"])).all()
        assert np.allclose(unpairs(row["prior_exact_roof_FV_CG_full_250ms_original_point_q0_signed_40_80"]),
            unpairs(old[ppw]["experimental_signed_P_T_over_Q_T_40_80"]))
    scores=e["all_four_frozen_original_three_gate_adjacent"]
    assert [(z["coarse_ppw"],z["fine_ppw"]) for z in scores]==[
        (28,32),(32,36),(36,40),(40,44)]
    for j,record in enumerate(scores):
        for arm,key in (("old_exact_roof_FV_CG","prior_exact_roof_FV_CG_full_250ms_original_point_q0_signed_40_80"),
                        ("new_conforming_roof_P1_FEM","new_FEM_full_250ms_original_point_q0_signed_40_80")):
            c=cases[j][key];f=cases[j+1][key]
            actual=compare_complex_transfer(reference=f,candidate=c,frequency_hz=[40,80],
                magnitude_mask_relative_db=-50).model_dump(mode="json")
            scored=record["arms"][arm]
            for metric in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(actual[metric]-scored["full_original_frozen_complex_mag_phase"][metric])<1e-10
            assert np.allclose(
                unpairs(scored["true_original_40_80_signed_coarse_minus_fine"]),
                unpairs(c)-unpairs(f),rtol=1e-11,atol=1e-8)
            passed=actual["complex_rms_relative"]<=.2 and actual["magnitude_max_relative"]<=.25 and actual["phase_max_deg"]<=15
            assert passed is scored["all_original_three_limits_pass"]
    for arm,v in e["comparison_full_five_grid_verdicts"].items():
        assert not v["all_four_original_three_gate_acceptance"]
        assert not v["strict_all_three_metric_monotonicity"]
        assert v["no_canonical_upstream_native_requalification"]
