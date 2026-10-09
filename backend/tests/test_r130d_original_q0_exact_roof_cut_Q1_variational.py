"""Original 8node Cartesian Q1 on the exact cut Neumann roof: full negative evidence."""
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
    generalized_native_original_Q1_full_physical_neumann_modes,
    _degree_four_six_positive_triangle_rule)
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_roof_p1_consistent_mass import (
    consistent_physical_P1_tensored_operators,
    true_full_3d_consistent_P1_MK)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_original_q0_true_roof_cut_Q1_galerkin import validate_plan,ARMS

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_evidence_2026-10-09.json"
OLD=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_p1_consistent_mass_evidence_2026-10-09.json"
REAL=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"


def test_positive_true_triangle_exact_polynomial_quadrature():
    nodes,weights=_degree_four_six_positive_triangle_rule()
    assert len(nodes)==6 and len(weights)==6
    assert np.all(weights>0)
    assert np.all(nodes>0)
    np.testing.assert_allclose(nodes.sum(axis=1),1,rtol=0,atol=1e-14)
    # area-normalized barycentric moments E[lambda1^i lambda2^j]
    # = 2*i!*j!/(i+j+2)! on a uniform triangle, degree <=4.
    from math import factorial
    for i,j in ((1,0),(0,1),(1,1),(2,1),(3,1),(4,0),(2,2)):
        exact=2*factorial(i)*factorial(j)/factorial(i+j+2)
        val=np.dot(weights,nodes[:,0]**i*nodes[:,1]**j)
        assert abs(val-exact)<5e-14


def synthetic():
    axis=np.arange(-.2,4.201,.2)
    fem=build_original_native_conforming_roof_p1(
        [axis,axis,axis],max_yz_nodes=1000,max_3d_nodes=40000)
    q1=cut_roof_native_original_Q1_galerkin_yz(
        axis,axis,max_active_yz_nodes=1100)
    return axis,fem,q1


def test_true_cartesian_Q1_physical_manufactured_consistent_mass_and_energy():
    _,f,q=synthetic()
    mx,_,kx,_=consistent_physical_P1_tensored_operators(f)
    mass,K=true_full_3d_consistent_P1_MK(mx,q.mass,kx,q.stiffness)
    n=mass.shape[0]
    assert q.native_yz_modes==len(q.yz_original_flat_indices)
    assert q.polygon_intersecting_native_cells>100
    assert q.occupied_cartesian_original_exterior_nodes>0
    assert abs(q.physical_area_m2-14)<2e-8
    assert q.minimum_strict_positive_cut_polygon_m2>0
    assert mass.nnz>n
    assert abs(mass.sum()-56)<2e-8
    assert np.min(mass.diagonal())>0
    assert np.max(np.abs(K@np.ones(n)))/max(np.max(abs(K.diagonal())),1)<1e-10
    x=f.x_positions_m[:,None]
    z=q.physical_active_yz_node_positions_m[None,:,1]
    phi=(x+z).ravel()
    # Exact physical ∫(x+z)^2 dV=2780/3; ∫c²|grad(x+z)|²=112c².
    assert abs(float(phi@(mass@phi))/(2780/3)-1)<2e-10
    assert abs(float(phi@(K@phi))/(112*343.2**2)-1)<2e-10


def test_original_native_eightpoint_shape_support_is_unchanged():
    a,f,q=synthetic()
    native=(len(a),)*3
    ix=[int(np.where(np.isclose(a,x))[0][0]) for x in (1.4,1.6)]
    iy=[int(np.where(np.isclose(a,y))[0][0]) for y in (1.8,2.0)]
    iz=[int(np.where(np.isclose(a,z))[0][0]) for z in (1.8,2.0)]
    indices=[];weights=[]
    for i,wx in zip(ix,(.35,.65)):
        for j,wy in zip(iy,(.4,.6)):
            for k,wz in zip(iz,(.2,.8)):
                indices.append(np.ravel_multi_index((i,j,k),native))
                weights.append(wx*wy*wz)
    px,py,error=original_eightnode_native_HDF5_Q1_source_receiver(
        native,f.x_native_map,q,np.asarray(indices),np.asarray(weights),
        len(f.x_positions_m))
    assert error<1e-13
    assert abs(px.sum()-1)<1e-13 and abs(py.sum()-1)<1e-13
    assert np.count_nonzero(px)==2 and np.count_nonzero(py)==4
    np.testing.assert_allclose(sorted(px[px>0]),sorted((.35,.65)))
    np.testing.assert_allclose(sorted(py[py>0]),sorted((
        .4*.2,.4*.8,.6*.2,.6*.8)))
    with pytest.raises(ValueError,match="eightpoint source modified"):
        original_eightnode_native_HDF5_Q1_source_receiver(
            native,f.x_native_map,q,np.asarray(indices),
            .8*np.asarray(weights),len(f.x_positions_m))


def test_true_all_cut_Q1_generalized_modes_including_singular_slivers():
    _,_,q=synthetic()
    eig,vec,checks=generalized_native_original_Q1_full_physical_neumann_modes(
        q.mass,q.stiffness)
    assert len(eig)==q.native_yz_modes
    assert eig[0]==0
    assert eig[1]>0
    assert eig[-1]>eig[1]
    assert checks["full_original_native_Q1_Galerkin_modes_retained_no_cut"]
    assert checks["analytic_exact_neumann_rigid_zero"]
    assert checks["strict_all_original_Q1_modes_generalized_residual"]<2e-7
    assert checks["strict_M_orthonormality_Gram_maxerror"]<5e-8
    assert checks["raw_neumann_zero_to_full_spectrum_ratio"]<1e-13
    assert np.linalg.norm(q.stiffness@vec[:,0])<1e-6


@pytest.mark.parametrize("name,bad",[
    ("complex_gate",1.),
    ("magnitude_gate",10.),
    ("phase_deg_gate",90)])
def test_original_three_acceptance_gates_cannot_be_relaxed(name,bad):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["independent_checks"][name]=bad
    with pytest.raises(ValueError,match="preregistration drift"):
        validate_plan(p)


def test_original_native_q0_P1_vs_Q1_true_full_five_grid_evidence_negative():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    previous=json.loads(OLD.read_text(encoding="utf-8"))
    raw=json.loads(REAL.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["preregistered_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["canonical_upstream_original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physical_validation"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_native_PFFDTD_waves"]==e["new_GitHub_Actions_runs"]==0
    assert e["original_PFFDTD_canonical_still_failed"]
    cases=e["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]
    prior={z["ppw"]:z for z in previous["actual_conforming_P1_consistent_mass_original_q0_cases"]}
    source={z["ppw"]:z for z in raw["actual_native_wave_cases"]}
    assert [z["ppw"] for z in cases]==list(PPW)
    assert sum(z["real_original_native_3D_full_true_Q1_modes"] for z in cases)==401630
    for r in cases:
        k=r["ppw"]
        assert r["original_SHA256_native_comms"]==source[k]["original_native_comm_sha256"]
        assert r["original_SHA256_native_voxel"]==source[k]["original_solver_geometry_sha256"]
        assert r["original_native_full_Nt"]==prior[k]["native_Nt"]
        assert abs(r["original_native_Ts_s"]-prior[k]["native_Ts_s"])<1e-12
        assert r["real_original_native_3D_full_true_Q1_modes"]==prior[k][
            "all_true_nonfiltered_consistent_FEM_3D_modes"]
        assert abs(r["true_physical_exact_cross_section_area_m2"]-14)<2e-8
        assert abs(r["true_full_3D_original_roof_volume_m3"]-56)<2e-8
        assert r["true_3D_Q1_manufactured_affine_mass_rel"]<2e-9
        assert r["true_3D_Q1_manufactured_affine_stiffness_rel"]<2e-9
        assert r["true_3D_Q1_constant_neumann_relative"]<1e-10
        assert r["same_original_eight_native_source_8_receiver_weights_and_full_q0"]
        assert r["no_high_mode_cut_or_sliver_point_removal"]
        assert r["new_true_physical_Q1_3D_offdiagonal_mass_nnz"]>r["real_original_native_3D_full_true_Q1_modes"]
        ycheck=r["all_yz_cartesian_cut_Q1_physical_M_generalized_eigencheck"]
        assert ycheck["full_original_native_Q1_Galerkin_modes_retained_no_cut"]
        assert ycheck["strict_all_original_Q1_modes_generalized_residual"]<2e-7
        assert ycheck["strict_M_orthonormality_Gram_maxerror"]<5e-8
        np.testing.assert_allclose(
            unpairs(r["prior_physically_conforming_P1_original_q0_full_signed_40_80"]),
            unpairs(prior[k]["new_full_consistent_P1_250ms_original_q0_signed_40_80"]))
        assert np.isfinite(unpairs(
            r["new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80"])).all()
    scores=e["original_signed_all_four_adjacent_three_gate_results"]
    assert [(z["coarse_ppw"],z["fine_ppw"]) for z in scores]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,pair in enumerate(scores):
        assert set(pair["arms"])==set(ARMS)
        for arm,key in (
            ("preobserved_conforming_P1_consistent_mass_original_8node",
             "prior_physically_conforming_P1_original_q0_full_signed_40_80"),
            ("new_native_cartesian_Q1_exact_roof_consistent_Galerkin_original_8node",
             "new_native_Q1_true_roof_q0_full_original_250ms_signed_40_80")):
            c=cases[i][key]
            f=cases[i+1][key]
            s=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            x=pair["arms"][arm]
            for metric in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(s[metric]-x[
                    "original_frozen_complex_magnitude_phase_and_frequency_bins"][metric])<1e-11
            np.testing.assert_allclose(unpairs(
                x["signed_original_q0_40_80_coarse_minus_fine"]),
                unpairs(c)-unpairs(f),atol=1e-8)
            passed=s["complex_rms_relative"]<=.2 and s["magnitude_max_relative"]<=.25 and s["phase_max_deg"]<=15
            assert passed is x["all_three_original_frozen_gates_pass"]
        assert not pair["arms"][
            "new_native_cartesian_Q1_exact_roof_consistent_Galerkin_original_8node"][
            "all_three_original_frozen_gates_pass"]
    for v in e["full_original_q0_five_grid_Q1_variational_convergence_verdict"].values():
        assert not v["all_four_pairs_pass_original_three_gates"]
        assert not v["all_three_metric_strictly_monotone"]
        assert v["not_original_native_PFFDTD_qualification"]
