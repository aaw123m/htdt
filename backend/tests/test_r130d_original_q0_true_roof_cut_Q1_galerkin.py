"""True original-Cartesian weak Q1 physically clipped 56m3 Galerkin q0 failures."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy import sparse

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    cut_roof_native_original_Q1_galerkin_yz,
    original_eightnode_native_HDF5_Q1_source_receiver,
    generalized_native_original_Q1_full_physical_neumann_modes)
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,
    true_full_3d_consistent_P1_MK)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_original_q0_true_roof_cut_Q1_galerkin import (
    validate_plan,ARMS)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_evidence_2026-10-09.json"
PREVIOUS=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_p1_consistent_mass_evidence_2026-10-09.json"
RAW=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"


def synthetic():
    axes=np.arange(-.2,4.201,.2)
    old=build_original_native_conforming_roof_p1(
        [axes,axes,axes],max_yz_nodes=1000,max_3d_nodes=40000)
    fresh=cut_roof_native_original_Q1_galerkin_yz(axes,axes,
        max_active_yz_nodes=1100)
    return axes,old,fresh


def test_original_native_cut_Q1_exact_analytic_linear_mass_stiffness_and_neumann():
    axes,fem,cross=synthetic()
    mx,_,kx,_=consistent_physical_P1_tensored_operators(fem)
    M,K=true_full_3d_consistent_P1_MK(mx,cross.mass,kx,cross.stiffness)
    dofs=len(fem.x_positions_m)*cross.native_yz_modes
    assert M.shape==(dofs,dofs)
    assert abs(cross.physical_area_m2-14)<2e-10
    assert abs(float(M.sum())-56)<2e-10
    assert cross.occupied_cartesian_original_exterior_nodes>0
    assert cross.minimum_strict_positive_cut_polygon_m2>0
    assert cross.mass.nnz>cross.native_yz_modes
    assert np.min(cross.mass.diagonal())>0
    assert np.max(abs(K@np.ones(dofs)))/max(np.max(abs(K.diagonal())),1)<1e-10
    asym=(K-K.T).tocoo()
    assert float(max(abs(asym.data),default=0.))<1e-7
    u=(fem.x_positions_m[:,None]+
       cross.physical_active_yz_node_positions_m[None,:,1]).ravel()
    mass_exact=2780/3
    stiff_exact=112*343.2**2
    assert abs(float(u@(M@u))-mass_exact)/mass_exact<2e-10
    assert abs(float(u@(K@u))-stiff_exact)/stiff_exact<2e-10
    assert M.nnz>dofs and K.nnz>dofs


def test_exact_original_quadrature_full_modes_no_cut_on_true_roof():
    _,fem,cross=synthetic()
    L,V,proof=generalized_native_original_Q1_full_physical_neumann_modes(
        cross.mass,cross.stiffness)
    assert len(L)==cross.native_yz_modes
    assert len(L)==V.shape[0]==V.shape[1]
    assert L[0]==0 and L[1]>0
    assert proof["full_original_native_Q1_Galerkin_modes_retained_no_cut"]
    assert proof["analytic_exact_neumann_rigid_zero"]
    assert proof["strict_all_original_Q1_modes_generalized_residual"]<2e-7
    assert proof["strict_M_orthonormality_Gram_maxerror"]<5e-8
    assert (V[:,0].max()-V[:,0].min())<1e-9


def test_original_exact_eightnode_PFFDTD_trilinear_equals_Q1_shapes_and_physical_moments():
    axes,fem,q1=synthetic()
    coordinates=(1.53,1.93,2.07)
    ids=[];w=[]
    for dim,v in enumerate(coordinates):
        start=int(np.searchsorted(axes,v,side="right")-1)
        mix=(v-axes[start])/(axes[start+1]-axes[start])
        ids.append([start,start+1]);w.append([1-mix,mix])
    x,y,z=ids
    weights=[];flat=[]
    for ia,wx in zip(x,w[0]):
        for ja,wy in zip(y,w[1]):
            for ka,wz in zip(z,w[2]):
                flat.append(np.ravel_multi_index(
                    (ia,ja,ka),(len(axes),len(axes),len(axes))))
                weights.append(wx*wy*wz)
    xw,yw,error=original_eightnode_native_HDF5_Q1_source_receiver(
        (len(axes),)*3,fem.x_native_map,q1,
        np.asarray(flat),np.asarray(weights),len(fem.x_positions_m))
    assert error<1e-13
    assert abs(sum(xw)-1)<1e-12 and abs(sum(yw)-1)<1e-12
    assert abs(xw@fem.x_positions_m-coordinates[0])<1e-12
    assert abs(yw@q1.physical_active_yz_node_positions_m[:,0]-coordinates[1])<1e-12
    assert abs(yw@q1.physical_active_yz_node_positions_m[:,1]-coordinates[2])<1e-12
    with pytest.raises(ValueError,match="eightpoint source modified"):
        original_eightnode_native_HDF5_Q1_source_receiver(
            (len(axes),)*3,fem.x_native_map,q1,
            np.asarray(flat),.9*np.asarray(weights),len(fem.x_positions_m))


@pytest.mark.parametrize("bad",[
    "complex_gate","magnitude_gate","phase_deg_gate"])
def test_Q1_original_score_acceptance_gate_change_fails_closed(bad):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["independent_checks"][bad]=99
    with pytest.raises(ValueError,match="preregistration drift"):
        validate_plan(p)


def test_true_original_q0_cartesian_Q1_precommitted_plan_not_source_filtered():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["original"]["five_native_PPWs"]==list(PPW)
    assert p["original"]["original_source_xyz"]==[1.5,2,2]
    assert p["original"]["original_receiver_xyz"]==[2.5,2,2]
    assert p["original"]["full_original_record_s"]==.25
    assert p["original"]["signed_freq_hz"]==[40,80]
    assert p["new_operator"]["no_sliver_drop_or_ghost_node_deletion_if_positive_support"]
    assert p["new_operator"]["no_high_modal_cutoff_or_clamp"]
    assert p["caps"]["new_native_PFFDTD_wave_runs"]==0
    assert p["caps"]["new_github_actions_runs"]==0


def test_true_Cartesian_original_Q1_full_all5grid_signed_original_q0_FAIL():
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    previous=json.loads(PREVIOUS.read_text(encoding="utf-8"))
    original=json.loads(RAW.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["preregistered_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["canonical_upstream_original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physical_validation"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_native_PFFDTD_waves"]==e["new_GitHub_Actions_runs"]==0
    assert e["original_PFFDTD_canonical_still_failed"]
    raw={z["ppw"]:z for z in original["actual_native_wave_cases"]}
    old={z["ppw"]:z for z in previous["actual_conforming_P1_consistent_mass_original_q0_cases"]}
    q=e["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]
    assert [x["ppw"] for x in q]==list(PPW)
    assert sum(r["real_original_native_3D_full_true_Q1_modes"] for r in q)==401630
    for x in q:
        ppw=x["ppw"]
        assert x["original_SHA256_native_comms"]==raw[ppw]["original_native_comm_sha256"]
        assert x["original_SHA256_native_voxel"]==raw[ppw]["original_solver_geometry_sha256"]
        assert x["original_native_full_Nt"]==old[ppw]["native_Nt"]
        assert x["original_native_Ts_s"]==old[ppw]["native_Ts_s"]
        assert x["real_original_native_3D_full_true_Q1_modes"]==old[ppw][
            "all_true_nonfiltered_consistent_FEM_3D_modes"]
        assert abs(x["true_physical_exact_cross_section_area_m2"]-14)<2e-8
        assert abs(x["true_full_3D_original_roof_volume_m3"]-56)<2e-8
        assert x["real_native_Q1_exterior_nodes_with_positive_physical_support_kept"]>0
        assert x["real_min_original_native_positive_cut_Q1_polygon_m2"]>0
        assert x["true_original_Q1_modes_above_native_Nyquist_included"]>0
        assert x["no_high_mode_cut_or_sliver_point_removal"]
        assert x["same_original_eight_native_source_8_receiver_weights_and_full_q0"]
        assert x["true_3D_Q1_manufactured_affine_mass_rel"]<2e-9
        assert x["true_3D_Q1_manufactured_affine_stiffness_rel"]<2e-9
        assert x["true_3D_Q1_constant_neumann_relative"]<1e-10
        assert x["all_yz_cartesian_cut_Q1_physical_M_generalized_eigencheck"][
            "strict_all_original_Q1_modes_generalized_residual"]<2e-7
        assert x["all_yz_cartesian_cut_Q1_physical_M_generalized_eigencheck"][
            "strict_M_orthonormality_Gram_maxerror"]<5e-8
        assert x["native_original_eight_src_Q1_factorization_error"]<1e-12
        assert x["native_original_eight_receiver_Q1_factorization_error"]<1e-12
        np.testing.assert_allclose(
            unpairs(x["prior_physically_conforming_P1_original_q0_full_signed_40_80"]),
            unpairs(old[ppw]["new_full_consistent_P1_250ms_original_q0_signed_40_80"]),
            rtol=1e-10,atol=1e-8)
    comparisons=e["original_signed_all_four_adjacent_three_gate_results"]
    assert [(x["coarse_ppw"],x["fine_ppw"]) for x in comparisons]==[
        (28,32),(32,36),(36,40),(40,44)]
    for j,compare in enumerate(comparisons):
        assert set(compare["arms"])==set(ARMS)
        for arm,key in (
            ("preobserved_conforming_P1_consistent_mass_original_8node",
             "prior_physically_conforming_P1_original_q0_full_signed_40_80"),
            ("new_native_cartesian_Q1_exact_roof_consistent_Galerkin_original_8node",
             "new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80")):
            c=q[j][key];f=q[j+1][key]
            score=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            evidence=compare["arms"][arm]
            for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(score[k]-evidence["original_frozen_complex_magnitude_phase_and_frequency_bins"][k])<1e-10
            np.testing.assert_allclose(
                unpairs(evidence["signed_original_q0_40_80_coarse_minus_fine"]),
                unpairs(c)-unpairs(f),rtol=1e-11,atol=1e-8)
            flag=score["complex_rms_relative"]<=.2 and score["magnitude_max_relative"]<=.25 and score["phase_max_deg"]<=15
            assert flag is evidence["all_three_original_frozen_gates_pass"]
        assert not compare["arms"]["new_native_cartesian_Q1_exact_roof_consistent_Galerkin_original_8node"][
            "all_three_original_frozen_gates_pass"]
    for v in e["full_original_q0_five_grid_Q1_variational_convergence_verdict"].values():
        assert not v["all_four_pairs_pass_original_three_gates"]
        assert not v["all_three_metric_strictly_monotone"]
        assert v["not_original_native_PFFDTD_qualification"]
