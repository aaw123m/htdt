"""Independent analytic Rayleigh basis bound, mass localization, five HDF5 grids."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import numpy as np
import pytest
from scipy import linalg, sparse
from htdt.r130d_true_roof_cfl_eigenvector_localization import fixed_physical_CFL_localization

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_q0_true_roof_CFL_eigenvector_localization import frozen_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_CFL_eigenvector_localization_plan_2026-10-10.json"
EVID=ROOT/"benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_CFL_eigenvector_localization_evidence_2026-10-10.json"
PRIOR=ROOT/"benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability_evidence_2026-10-10.json"
NATIVE=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"


def test_independent_diagonal_rayleigh_cfl_certificate_and_mode_mass():
    # Small independent actual SPD Neumann graph: one physically tiny yz mass
    # concentrates the largest generalized eigenmode; basis Rayleigh is a
    # RIGOROUS lower bound, not just a narrative roof-cause claim.
    nx,ny=5,15
    wx=np.ones(nx)*.8
    wy=np.array([.0001]+[(14.-.0001)/(ny-1)]*(ny-1))
    x=np.arange(nx)*.8
    y=np.linspace(0.,4.,ny)
    yz=np.stack([y,4.-y/4.],axis=1)
    def lap(n):
        a=np.zeros((n,n))
        for i in range(n-1):
            a[i,i]+=1;a[i+1,i+1]+=1
            a[i,i+1]-=1;a[i+1,i]-=1
        return a
    mx=sparse.diags(wx,format="csr");my=sparse.diags(wy,format="csr")
    kx=sparse.csr_matrix(4*lap(nx));ky=sparse.csr_matrix(3*lap(ny))
    lx,ux=linalg.eigh(kx.toarray(),mx.toarray())
    ly,uy=linalg.eigh(ky.toarray(),my.toarray())
    z=fixed_physical_CFL_localization(mx,kx,my,ky,lx,ly,uy,yz,h=.5,dt=.2)
    assert z["positive_original_x_node_count"]==nx
    assert z["positive_original_yz_node_count"]==ny
    assert z["full_untruncated_3d_modes"]==nx*ny
    assert abs(z["true_3d_volume_m3"]-56.)<1e-12
    assert z["full_3d_single_basis_Rayleigh_lowerbound_per_s2"]<=z["full_max_3d_lambda_per_s2"]*(1+1e-12)
    assert z["full_dt2_single_basis_Rayleigh_lowerbound"]>4.
    assert z["basis_node_alone_certifies_native_leapfrog_unstable"]
    assert z["top_five_yz_diagonal_Rayleigh_nodes"][0]["active_yz_index"]==0
    tiny=z["top_three_true_generalized_yz_eigenvector_localization"][0]["fractions"]["positive_node_area_lt_0.01_h2"]
    assert tiny["eigenvector_M_mass_fraction"]>.5
    assert tiny["M_mass_enrichment"]>100
    for key,info in z["roof_and_small_area_original_physical_support"].items():
        assert 0<=info["baseline_physical_support_mass_fraction"]<=1
    assert abs(z["maximum_x_eigenvalue_fraction_of_full_max"]
               +z["maximum_yz_eigenvalue_fraction_of_full_max"]-1)<1e-12


@pytest.mark.parametrize("field,value",[
    ("ppw",[28,32,36]),("full_record_seconds",.10),
    ("signed_frequency_hz",[40]),("receiver_xyz_m",[2,2,2]),
])
def test_frozen_plan_rejects_posthoc_original_changes(field,value):
    p=json.loads(PLAN.read_text(encoding="utf8"))
    assert frozen_plan(p)==p
    p["original"][field]=value
    with pytest.raises(ValueError,match="FROZEN_ROOF_LOCALIZATION_PLAN_DRIFT"):
        frozen_plan(p)


def test_original_all_five_unmodified_SHA_and_full_Rayleigh_certificates():
    p=json.loads(PLAN.read_text(encoding="utf8"))
    e=json.loads(EVID.read_text(encoding="utf8"))
    old=json.loads(PRIOR.read_text(encoding="utf8"))
    native=json.loads(NATIVE.read_text(encoding="utf8"))
    assert e["preregistered_plan"]==p
    assert e["preregistered_remote_commit"]=="34547c88234695b7eadc96b191531ef9fba2ae9f"
    assert e["original_PFFDTD"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physics"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_original_pffdtd_waves"]==0
    assert e["new_github_actions_runs"]==0
    assert e["original_native_unmodified_250ms_q0_convergence_still_failed"]
    assert e["entire_complete_eigenbasis_no_truncation"]
    assert e["original_native_all_five_unstable_no_unqualified_250ms_leapfrog_scores"]
    cases=e["full_original_native_five_grid_cfl_eigenvector_results"]
    prev={x["ppw"]:x for x in old["all_five_native_original_grid_CFL"]}
    orig={x["ppw"]:x for x in native["actual_native_wave_cases"]}
    assert [x["ppw"] for x in cases]==[28,32,36,40,44]
    for x in cases:
        ppw=x["ppw"];previous=prev[ppw];n=orig[ppw]
        v=x["full_raw_mass_sliver_and_roof_localization"]
        assert x["native_comms_SHA"]==n["original_native_comm_sha256"]
        assert x["native_voxel_SHA"]==n["original_solver_geometry_sha256"]
        assert x["native_original_dt_s"]==previous["original_native_dt_s"]
        assert x["native_original_Nt"]==previous["original_native_Nt"]
        assert x["all_original_full_modes_instability_count"]==previous["unstable_mode_count"]
        assert abs(x["max_original_native_dt2_lambda"]-previous["native_dt2_lambda_max"])<2e-9
        assert x["preobserved_complete_cfl_verified"]
        assert not x["original_frozen_explicit_native_CFL_stable"]
        assert v["full_untruncated_3d_modes"]==previous["full_original_modal_count"]
        assert abs(v["true_3d_volume_m3"]-56)<2e-8
        assert v["preserved_original_full_physical_operator_without_mode_filter"]
        assert v["full_dt2_single_basis_Rayleigh_lowerbound"]>=4.
        assert v["full_dt2_single_basis_Rayleigh_lowerbound"]<=x["max_original_native_dt2_lambda"]*(1+1e-9)
        assert v["basis_node_alone_certifies_native_leapfrog_unstable"]
        assert len(v["top_five_yz_diagonal_Rayleigh_nodes"])==5
        assert len(v["top_three_true_generalized_yz_eigenvector_localization"])==3
        for mode in v["top_three_true_generalized_yz_eigenvector_localization"]:
            assert mode["M_orthonormality_self_error"]<5e-7
            assert 0<=mode["fractions"]["positive_node_area_lt_0.01_h2"]["eigenvector_M_mass_fraction"]<=1
        assert abs(v["maximum_x_eigenvalue_fraction_of_full_max"]+
                   v["maximum_yz_eigenvalue_fraction_of_full_max"]-1)<1e-12
    # The most unstable two grids have x-dominant largest eigenmodes,
    # distinguishing their CFL pathology from any blanket "roof-only" claim.
    assert cases[0]["full_raw_mass_sliver_and_roof_localization"]["maximum_x_eigenvalue_fraction_of_full_max"]>.8
    assert cases[3]["full_raw_mass_sliver_and_roof_localization"]["maximum_x_eigenvalue_fraction_of_full_max"]>.8
