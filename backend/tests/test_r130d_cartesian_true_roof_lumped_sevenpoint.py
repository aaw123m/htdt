"""Cartesian 7point / exact true-roof Neumann row-sum-mass local tests."""
from __future__ import annotations
import json
from pathlib import Path
import sys

import numpy as np
import pytest

from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import consistent_physical_P1_tensored_operators
from htdt.r130d_cartesian_true_roof_hybrid_flux import build_native_cartesian_true_roof_hybrid_flux
from htdt.r130d_cartesian_true_roof_lumped_sevenpoint import build_cartesian_sevenpoint_true_roof

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_q0_cartesian_roof_row_lumped_mass import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_roof_row_lumped_mass_plan_2026-10-10.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_roof_row_lumped_mass_evidence_2026-10-10.json"
PRIOR=ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_roof_hybrid_evidence_2026-10-10.json"
NATIVE=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"

def test_real_physical_cartesian_native_sevenpoint_mass_stencil_and_roof_affine():
    # Synthetic mesh tests standalone 7point algebra, NO original five-grid evaluation.
    axis=np.linspace(0.,4.,17)
    axes=[axis.copy(),axis.copy(),axis.copy()]
    fem=build_original_native_conforming_roof_p1(axes,max_yz_nodes=3000,max_3d_nodes=180000)
    mx,_,kx,_=consistent_physical_P1_tensored_operators(fem)
    hy=build_native_cartesian_true_roof_hybrid_flux(axis,axis,max_active_yz_nodes=3300)
    z=build_cartesian_sevenpoint_true_roof(
        [fem.x_positions_m,axis,axis],mx,kx,hy)
    assert z.physical.all_modes==z.mass.shape[0]
    assert abs(z.total_true_room_m3-56.)<1e-8
    assert abs(z.representative_interior_mass_m3-(4/16)**3)<1e-12
    assert z.seven_neighbor_stencil_max_relative<1e-10
    assert z.mass.nnz==z.mass.shape[0]
    assert min(z.mass.diagonal())>0
    assert max(abs((z.stiffness-z.stiffness.T).data),default=0.)<1e-7
    assert max(abs(z.stiffness@np.ones(z.mass.shape[0])))<2e-8
    y=hy.cut_q1.physical_active_yz_node_positions_m
    u=y[:,0]-.25*y[:,1]
    roof=(abs(4-.25*y[:,0]-y[:,1])<.5)&(y[:,0]>.5)&(y[:,0]<3.5)&(y[:,1]>.5)
    assert roof.sum()>5
    assert max(abs((hy.stiffness@u)[roof]))<1e-7

@pytest.mark.parametrize("kind,key,replacement",[
    ("original","ppw",[28,32,36]),
    ("original","frequencies_signed_hz",[40]),
    ("original","record_s",.20),
    ("new_operator","all_generalized_3d_modes_retained",False),
    ("caps","new_actions_runs",1),
])
def test_mutated_frozen_plan_rejected(kind,key,replacement):
    p=json.loads(PLAN.read_text(encoding="utf8"))
    p[kind][key]=replacement
    with pytest.raises(ValueError,match="preregistered original q0"):
        validate_plan(p)

def test_frozen_plan_git_historical_prereg_and_release():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf8")))
    assert p["repo"]=="aaw123m/htdt"
    assert p["original"]["ppw"]==[28,32,36,40,44]
    assert p["authority"]["product"]=="NO_GO"
    assert p["authority"]["PFFDTD_original"]=="SELF_CONVERGENCE_FAILED"

def test_real_full_fivegrid_original_sha_q0_complete_lumped_diagnostic_evidence():
    p=json.loads(PLAN.read_text(encoding="utf8"))
    e=json.loads(EVIDENCE.read_text(encoding="utf8"))
    prior=json.loads(PRIOR.read_text(encoding="utf8"))
    native=json.loads(NATIVE.read_text(encoding="utf8"))
    assert e["preregistered_plan"]==p
    assert e["live_GitHub_frozen_preregistration_sha"]=="d2ea0a531853988e79ae38f8c9a2922ab9be0ae7"
    assert e["original_PFFDTD"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physics"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_native_PFFDTD_waves"]==0 and e["new_GitHub_Actions_runs"]==0
    cases=e["real_original_native_8node_q0_sevenpoint_lumped_mass_cases"]
    assert [c["ppw"] for c in cases]==[28,32,36,40,44]
    prev={c["ppw"]:c for c in prior["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"]}
    orig={c["ppw"]:c for c in native["actual_native_wave_cases"]}
    assert [c["all_3D_original_native_physical_modes"] for c in cases]==[
        37835,53001,75504,102949,132341]
    for c in cases:
        ppw=c["ppw"]
        assert c["original_native_comm_sha256"]==orig[ppw]["original_native_comm_sha256"]
        assert c["original_native_voxel_sha256"]==orig[ppw]["original_solver_geometry_sha256"]
        assert c["native_original_full_Nt"]==prev[ppw]["original_native_full_Nt"]
        assert abs(c["native_original_Ts_s"]-prev[ppw]["original_native_Ts_s"])<1e-14
        assert abs(c["exact_true_room_volume_m3"]-56)<2e-8
        assert c["strict_cartesian_sevenpoint_interior_relative"]<1e-10
        assert c["all_true_roof_affine_neumann_weak_max"]<1e-7
        assert c["all_3D_affine_stiffness_relative"]<2e-9
        assert c["all_3D_neumann_constant_relative"]<1e-10
        assert 0<=c["all_original_native_modes_above_nyquist_kept"]<=c["all_3D_original_native_physical_modes"]
        # Zero modes above native Nyquist is a legitimate spectral result;
        # never require an artificial above-Nyquist mode or delete real ones.
        assert len(c["independent_true_roof_original_64pair_three_weak_witnesses"])==3
        assert c["original_full_modes_untruncated_and_native_record_unchanged"]
        assert c["all_positive_native_yz_Q1_support_nodes"]==prev[ppw]["real_original_native_cartesian_yz_Q1_physical_support_modes"]
    scores=e["original_full_q0_250ms_signed_adjacent_gates"]
    assert [(a["coarse_ppw"],a["fine_ppw"]) for a in scores]==[
        (28,32),(32,36),(36,40),(40,44)]
    assert not any(s["all_three_original_gates_pass"] for s in scores)
    assert e["five_grid_all_three_gates_and_strict_monotonicity"]=={
        "all_four_pairs_pass":False,"all_three_strictly_monotonic":False,
        "all_acceptance_pass_experimental_only":False,
        "original_PFFDTD_requalification":False}
    expected=((.3183565453919536,2.095265741959669,14.127787030752245),
              (.32707543783766774,.7356148488421779,13.573646275623304),
              (.22642249584867022,.681252859475253,10.446999913737642),
              (.5890342353311704,.16342610567739088,175.65870575293718))
    for case,triplet in zip(scores,expected):
        measured=case["original_frozen_complex_magnitude_phase"]
        np.testing.assert_allclose(
            [measured[k] for k in (
                "complex_rms_relative","magnitude_max_relative","phase_max_deg")],
            triplet,atol=1e-8,rtol=1e-8)
