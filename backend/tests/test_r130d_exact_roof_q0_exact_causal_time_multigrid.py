"""Five real original-native q0 exact-roof joint space/time controls and FAIL."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_exact_semidiscrete_q0 import (
    exact_semidiscrete_velocity_impulse_signed,
    exact_semidiscrete_one_sample_hold_signed,
    exact_semidiscrete_one_sample_hold_phi)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_exact_roof_q0_exact_causal_time_multigrid import (
    validate_plan,ARMS)

PLAN=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_exact_causal_time_multigrid_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_exact_causal_time_multigrid_evidence_2026-10-09.json"
OLD=ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_evidence_2026-10-09.json"


@pytest.mark.parametrize("nt,dt",[(10,.002),(24,.0001310847543)])
def test_true_cutcell_over_nyquist_exact_time_integrators_vs_direct_wave(nt,dt):
    # Crucial genuine cutcell FV issue: omega dt can be far ABOVE Nyquist.
    # No clamping/cutting/alias-replacement of physical semidiscrete theta.
    theta=np.array([0.,.014,.79,2.9,2*np.pi+.19,6*np.pi+.18,13*np.pi+.42,42.4])
    A=np.array([.9,.023,1.7,-.44,.17,-.6,.002,1.9])
    rho=1.2
    impulse=np.zeros((nt,len(theta)))
    held=np.zeros((nt,len(theta)))
    for n in range(1,nt):
        impulse[n]=A*n*np.sinc(n*theta/np.pi)
        held[n]=exact_semidiscrete_one_sample_hold_phi(n,theta,A)
    for wave,fn in ((impulse,exact_semidiscrete_velocity_impulse_signed),
                    (held,exact_semidiscrete_one_sample_hold_signed)):
        pressure=np.zeros_like(wave)
        pressure[0]=rho*(-3*wave[0]+4*wave[1]-wave[2])/(2*dt)
        pressure[1:-1]=rho*(wave[2:]-wave[:-2])/(2*dt)
        pressure[-1]=rho*(3*wave[-1]-4*wave[-2]+wave[-3])/(2*dt)
        predicted=fn(theta,A,native_dt_s=dt,native_nt=nt)
        for j,f in enumerate((40.,80.)):
            actual=np.sum(pressure*np.exp(2j*np.pi*f*dt*np.arange(nt)[:,None]),
                          axis=0)
            np.testing.assert_allclose(predicted[j],actual,rtol=3e-9,atol=3e-8)


def test_actual_joint_roof_q0_plan_frozen():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["frozen"]["ppw"]==list(PPW)
    assert p["frozen"]["source_m"]==[1.5,2,2]
    assert p["frozen"]["receiver_m"]==[2.5,2,2]
    assert p["frozen"]["record_s"]==.25
    assert p["frozen"]["signed_hz"]==[40,80]
    assert [a["id"] for a in p["arms"]]==list(ARMS)
    assert p["operator"]["sliver_cell_high_eigenfrequencies_are_NOT_clamped_to_Nyquist"]
    assert p["limits"]["new_github_actions_runs"]==0
    assert p["limits"]["new_native_pffdtd_full_wave_runs"]==0


@pytest.mark.parametrize("k,v",[
    ("original_frozen_complex_relative_limit",1.2),
    ("original_frozen_magnitude_relative_limit",2),
    ("original_frozen_max_phase_deg_limit",180),
    ("old_newmark_fullmode_vs_archived_real_CG_wave_rel_max",.1)])
def test_joint_true_3d_exact_time_gate_relaxation_denied(k,v):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["predeclared_verification"][k]=v
    with pytest.raises(ValueError,match="plan changed"):
        validate_plan(p)


def test_true_five_grid_joint_fv_spacetime_all_modes_original_q0_evidence():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    ev=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    prior=json.loads(OLD.read_text(encoding="utf-8"))
    assert ev["preregistered_plan"]==p
    assert ev["pre_observation_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert ev["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert ev["physical_validation"]=="NOT_VALIDATED"
    assert ev["product"]=="NO_GO"
    assert ev["new_pffdtd_wave_runs"]==0
    assert ev["new_github_actions_runs"]==0
    assert ev["original_canonical_not_requalified"]
    cases=ev["actual_joint_spacetime_allmode_cases"]
    archive={z["ppw"]:z for z in prior["actual_native_grid_point_impulse_exact_roof_cases"]}
    assert [z["ppw"] for z in cases]==list(PPW)
    assert sum(c["all_exact_roof_native_modes_count"] for c in cases)==359880
    for c in cases:
        k=c["ppw"]
        assert c["original_raw_q0_source_receiver_and_scored_full_window_unchanged"]
        assert c["all_exact_roof_native_modes_count"]==archive[k]["original_nodal_active_cutcell_count"]
        assert c["native_Nt"]==archive[k]["original_record_samples"]
        assert abs(c["native_Ts_s"]-archive[k]["original_native_dt_s"])<1e-12
        assert abs(c["actual_physical_exact_room_volume_m3"]-56)<2e-8
        assert c["previous_archived_direct_true_Newmark_CG_wave_relative"]<2e-5
        assert c["all_sliver_cell_modes_retained_above_nyquist"]>=0
        assert c["highest_semidiscrete_native_omega_dt"]>0
        check=c["actual_physical_3D_sparse_K_Neumann_tensor_proof"]
        assert check["native_full_active_nodes"]==c["all_exact_roof_native_modes_count"]
        assert set(c["full_250ms_unmasked_signed_40_80_by_numerical_operator"])==set(ARMS)
        old=unpairs(c["full_250ms_unmasked_signed_40_80_by_numerical_operator"]["existing_exact_roof_newmark"])
        original=unpairs(archive[k]["experimental_signed_P_T_over_Q_T_40_80"])
        assert np.linalg.norm(old-original)/max(np.linalg.norm(original),1e-14)<2e-5
        for arm in ARMS:
            assert np.isfinite(unpairs(c["full_250ms_unmasked_signed_40_80_by_numerical_operator"][arm])).all()
    rows=ev["adjacent_full_signed_40_80"]
    assert [(x["coarse_ppw"],x["fine_ppw"]) for x in rows]==[
        (28,32),(32,36),(36,40),(40,44)]
    for j,pair in enumerate(rows):
        assert set(pair["arms"])==set(ARMS)
        for arm in ARMS:
            c=cases[j]["full_250ms_unmasked_signed_40_80_by_numerical_operator"][arm]
            f=cases[j+1]["full_250ms_unmasked_signed_40_80_by_numerical_operator"][arm]
            record=pair["arms"][arm]
            m=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            assert np.allclose(
                unpairs(record["signed_coarse_minus_fine_P_T_over_Q_T_40_80"]),
                unpairs(c)-unpairs(f),rtol=1e-10,atol=1e-8)
            for key in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(m[key]-record["original_frozen_complex_magnitude_phase_metrics"][key])<1e-10
            flag=m["complex_rms_relative"]<=.2 and m["magnitude_max_relative"]<=.25 and m["phase_max_deg"]<=15
            assert flag is record["all_original_three_limits_pass"]
    assert set(ev["all_three_joint_spacetime_arm_5grid_verdicts"])==set(ARMS)
    for arm,s in ev["all_three_joint_spacetime_arm_5grid_verdicts"].items():
        assert s["not_native_original_pffdtd_qualification"]
        assert not (s["all_four_original_adjacent_pairs_pass_all_gates"] and
                    s["all_three_metric_strict_monotone_decrease"])
