"""Time-MATCHED full original 8-node q0 native spatial-only geometry A/B."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_matched_newmark_spatial_ab_q0 import (
    PPW,validate_plan,score_adjacent,unpairs)

PLAN=ROOT/"benchmarks/acoustics/r130d_original_point_q0_matched_newmark_spatial_ab_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_point_q0_matched_newmark_spatial_ab_evidence_2026-10-09.json"

def frozen():
    raw=PLAN.read_bytes()
    p=validate_plan(json.loads(raw))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert d["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert d["preregistered_plan"]==p
    return p,d

def test_original_q0_unmodified_and_no_github_actions_or_promotion():
    p,d=frozen()
    assert p["native_control"]["ppw"]==list(PPW)
    assert p["native_control"]["source_xyz_m"]==[1.5,2,2]
    assert p["native_control"]["receiver_xyz_m"]==[2.5,2,2]
    assert p["native_control"]["compare_bins_hz"]==[40,80]
    assert p["matched_time"]["true_relative_residual_max"]==5e-10
    assert p["limits"]["run_github_actions"] is False
    assert d["github_actions_run_count"]==0
    assert d["original_canonical"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["product"]=="NO_GO"

@pytest.mark.parametrize("change",[
    lambda p:p["native_control"].update(ppw=[28,32]),
    lambda p:p["native_control"].update(source_xyz_m=[1.45,2,2]),
    lambda p:p["native_control"].update(compare_bins_hz=[40]),
    lambda p:p["matched_time"].update(rtol=1e-3),
    lambda p:p["acceptance"].update(frozen_adjacent_original_complex_max=1),
    lambda p:p["limits"].update(run_github_actions=True),
    lambda p:p["authority"].update(product="GO")
])
def test_preregistered_matched_time_ab_cannot_be_tuned_after_observation(change):
    p,_=frozen()
    change(p)
    with pytest.raises(ValueError):validate_plan(p)

def test_original_staircase_vs_cut_roof_same_implicit_newmark_q0_signed_all_five():
    p,d=frozen()
    native=json.loads((ROOT/p["native_control"]["source_original"]).read_text(encoding="utf-8"))
    cut=json.loads((ROOT/p["preregistered_treatment"]["evidence"]).read_text(encoding="utf-8"))
    r=d["actual_matched_time_spatial_AB_cases"]
    assert [x["ppw"] for x in r]==list(PPW)
    for case,old,new in zip(r,native["actual_native_wave_cases"],
                            cut["actual_native_grid_point_impulse_exact_roof_cases"]):
        assert case["ppw"]==old["ppw"]==new["ppw"]
        assert case["original_exact_native_voxel_sha256"]==old["original_solver_geometry_sha256"]
        assert case["original_exact_native_comms_sha256"]==old["original_native_comm_sha256"]
        assert case["original_native_full_baseline_P_T_Q_40_80"]==old["unmodified_original_transfer_pa_per_m3_s"]
        assert case["treatment_same_newmark_exact_roof_P_T_Q_40_80"]==new["experimental_signed_P_T_over_Q_T_40_80"]
        assert case["control_staircase_graph_room_nodes"]>0
        assert case["treatment_exact_roof_room_nodes"]>0
        assert case["control_staircase_graph_K_nnz"]>case["control_staircase_graph_room_nodes"]
        assert case["control_local_wall_seconds"]>0
        assert case["control_staircase_graph_room_nodes"]!=case["treatment_exact_roof_room_nodes"]
        assert case["treatment_exact_cutcell_volume_m3"]==pytest.approx(56,abs=2e-8)
        assert case["original_physical_point_source_q0_unchanged"] is True
        assert case["time_source_receiver_sampling_newmark_matched"] is True
        signed=unpairs(case["control_same_newmark_native_staircase_P_T_Q_40_80"])
        signed_other=unpairs(case["treatment_same_newmark_exact_roof_P_T_Q_40_80"])
        assert np.all(np.isfinite(signed)) and np.all(np.isfinite(signed_other))
        ratio=float(np.linalg.norm(signed-signed_other)/np.linalg.norm(signed_other))
        assert case["per_grid_signed_spatial_only_complex_relative_change"]==pytest.approx(ratio,abs=1e-10)
        st=case["control_implicit_solver"]
        assert st["maximum_CG_iterations"]<=500
        assert st["maximum_true_linear_relative_residual"]<=5e-10
        assert st["relative_energy_drift_after_source"]<=1e-6
        assert st["original_discrete_q0_unchanged"] is True
        assert st["no_point_source_smoothing_or_taper"] is True

def test_same_newmark_spatial_ab_all_four_adjacent_original_frozen_bin_metrics():
    p,d=frozen()
    ab=d["matched_time_spatial_adjacent"]
    cases=d["actual_matched_time_spatial_AB_cases"]
    stair=score_adjacent(cases,"control_same_newmark_native_staircase_P_T_Q_40_80")
    roof=score_adjacent(cases,"treatment_same_newmark_exact_roof_P_T_Q_40_80")
    for a,b in zip(stair,ab["native_staircase_control"]):
        for col in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert a[col]==pytest.approx(b[col],rel=5e-12,abs=5e-12)
    for a,b in zip(roof,ab["exact_roof_treatment"]):
        for col in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert a[col]==pytest.approx(b[col],rel=5e-12,abs=5e-12)
    assert len(stair)==len(roof)==4
    assert ab["original_PFFDTD_8node_8_10_12_selfconvergence_still_FAILED"] is True
    assert ab["same_exact_original_source_receiver_q0_and_native_dt"] is True
    assert ab["not_a_production_solver_promotion"] is True
    assert ab["exact_roof_threshold_flags"]==[True,False,True,False]
    assert ab["final_pair_experimental_exact_roof_pass"] is False
