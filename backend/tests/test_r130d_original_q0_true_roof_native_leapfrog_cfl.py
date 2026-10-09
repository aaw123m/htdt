"""Native original q0 explicit leapfrog CFL and pressure mode independent tests."""
from __future__ import annotations
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from htdt.r130d_original_q0_leapfrog_modal_observer import original_native_leapfrog_full_modal

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability import verify_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability_plan_2026-10-10.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability_evidence_2026-10-10.json"
HISTORY=ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_roof_row_lumped_mass_evidence_2026-10-10.json"

@pytest.mark.parametrize("freqset",[(40.,80.),(0.,40.)])
def test_independent_native_leapfrog_direct_time_pressure_full_rectangle(freqset):
    # Independent direct recurrence, pressure forward/center/backward original
    # slopes and signed rectangular time sum, including exact rigid zero mode.
    dt=.002;nt=93;c=343.2;rho=1.2
    theta=np.array([0.,.15,.32,.90,1.75,2.6])
    lam=(2*np.sin(theta/2)/dt)**2
    weight=np.array([.002,-.007,.009,.003,-.002,.001])
    result=original_native_leapfrog_full_modal(
        lam,weight,dt,nt,c,rho,frequencies_hz=freqset)
    q=dt*dt*c*c*weight
    phi=np.zeros((nt,len(lam)))
    phi[1]=q
    for i in range(1,nt-1):
        phi[i+1]=(2-dt*dt*lam)*phi[i]-phi[i-1]
    p=np.zeros_like(phi)
    p[0]=rho*(-3*phi[0]+4*phi[1]-phi[2])/(2*dt)
    p[1:-1]=rho*(phi[2:]-phi[:-2])/(2*dt)
    p[-1]=rho*(3*phi[-1]-4*phi[-2]+phi[-3])/(2*dt)
    direct=np.array([(p*np.exp(2j*np.pi*f*dt*np.arange(nt))[:,None]).sum(axis=0)
                     for f in freqset])
    np.testing.assert_allclose(result,direct,rtol=2e-9,atol=1e-10)
    np.testing.assert_allclose(result.sum(axis=1),direct.sum(axis=1),rtol=2e-9,atol=1e-10)

@pytest.mark.parametrize("lam",[[4.],[-.1],[4.0001]])
def test_no_native_cfl_unstable_mode_can_be_truncated(lam):
    with pytest.raises(ValueError):
        original_native_leapfrog_full_modal(
            np.array(lam),np.ones(len(lam)),1.,40,c_m_s=1.)

def test_frozen_native_physical_plan_and_reject_mutation():
    plan=json.loads(PLAN.read_text(encoding="utf8"))
    assert verify_plan(plan)==plan
    assert plan["temporal"]["cfl"].find("ANY grid fails")!=-1
    for field,value in (("record_seconds",.2),("frequency_hz",[40]),
                        ("ppw",[28,32,36]),("c_m_s",340)):
        tmp=json.loads(json.dumps(plan));tmp["frozen"][field]=value
        with pytest.raises(ValueError,match="FROZEN_CFL_EXPERIMENT"):
            verify_plan(tmp)

def test_complete_fivegrid_native_CFL_real_original_SHA_release():
    e=json.loads(EVIDENCE.read_text(encoding="utf8"))
    frozen=json.loads(PLAN.read_text(encoding="utf8"))
    old=json.loads(HISTORY.read_text(encoding="utf8"))
    assert e["preregistered_plan"]==frozen
    assert e["preregistered_GitHub_commit"]=="98913220e22d50fe8f0a19454a2e26749e5f758f"
    assert e["canonical_original_PFFDTD"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physics"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_original_PFFDTD_waves"]==0 and e["github_actions_runs"]==0
    cases=e["all_five_native_original_grid_CFL"]
    assert [q["ppw"] for q in cases]==[28,32,36,40,44]
    oldcases={q["ppw"]:q for q in old["real_original_native_8node_q0_sevenpoint_lumped_mass_cases"]}
    for q in cases:
        ppw=q["ppw"];ref=oldcases[ppw]
        assert q["full_original_modal_count"]==ref["all_3D_original_native_physical_modes"]
        assert q["original_hdf5_comms_sha256"]==ref["original_native_comm_sha256"]
        assert q["original_hdf5_vox_sha256"]==ref["original_native_voxel_sha256"]
        assert q["original_native_Nt"]==ref["native_original_full_Nt"]
        assert q["original_native_dt_s"]==ref["native_original_Ts_s"]
        assert abs(q["roof_volume_m3"]-56)<2e-8
        assert q["original_7point_interior_relative"]<1e-10
        assert q["true_neumann_constant_relative"]<1e-10
        assert q["original_unmodified_source_and_observer"]
        assert q["native_dt2_lambda_max"]>=0
        assert q["native_explicit_leapfrog_stable_all_modes"]==(q["native_dt2_lambda_max"]<4)
    assert e["original_frozen_convergence_still_failed"]
    if any(not q["native_explicit_leapfrog_stable_all_modes"] for q in cases):
        assert e["five_grid_native_leapfrog_status"]=="UNSTABLE_AT_ORIGINAL_DT"
        assert e["no_unstable_high_mode_omission"]
        assert e["no_unstable_alternate_full_250ms_scoring"]
        assert not e["full_signed_leapfrog_40_80_if_ALL_stable"]
        assert not e["all_original_four_adjacent_refinement_scores_if_ALL_stable"]
    else:
        assert e["five_grid_native_leapfrog_status"]=="STABLE_ALL_FIVE_AT_ORIGINAL_DT"
        assert len(e["full_signed_leapfrog_40_80_if_ALL_stable"])==5
        assert len(e["all_original_four_adjacent_refinement_scores_if_ALL_stable"])==4
