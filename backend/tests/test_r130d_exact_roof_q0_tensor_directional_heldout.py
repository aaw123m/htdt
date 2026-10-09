"""Prospectively preserved all-five-grid true point-q0 directional heldout FAIL."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_tensor_directional_dispersion import ARMS
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_exact_roof_q0_tensor_directional_heldout import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_tensor_directional_heldout_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_tensor_directional_heldout_evidence_2026-10-09.json"
PREVIOUS=ROOT/"benchmarks/acoustics/r130d_exact_roof_q0_tensor_directional_dispersion_evidence_2026-10-09.json"
ARCHIVED=ROOT/"benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_evidence_2026-10-09.json"


def test_precommitted_physically_original_heldout_plan():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["status_of_preexisting_observations"]["new_heldout_ppw"]==[28,32,36]
    assert p["status_of_preexisting_observations"]["entire_refinement_grid"]==[28,32,36,40,44]
    assert p["status_of_preexisting_observations"]["all_arms"]==list(ARMS)
    assert p["frozen"]["frequency_hz"]==[40,80]
    assert p["frozen"]["record_s"]==.25
    assert p["frozen"]["physical_source_xyz"]==[1.5,2,2]
    assert p["frozen"]["physical_receiver_xyz"]==[2.5,2,2]
    assert p["limits"]["new_GitHub_Actions_runs"]==0
    assert p["limits"]["new_original_pffdtd_wave_runs"]==0


@pytest.mark.parametrize("key,value",[
    ("original_complex_limit",.5),
    ("original_magnitude_limit",1.0),
    ("original_phase_limit_deg",180)])
def test_frozen_qualification_threshold_cannot_move(key,value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["evaluation"][key]=value
    with pytest.raises(ValueError,match="plan changed"):
        validate_plan(p)


def test_three_heldout_real_native_original_q0_spectra_and_all_negative_gates():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    old=json.loads(PREVIOUS.read_text(encoding="utf-8"))
    cg=json.loads(ARCHIVED.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["pre_observation_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["physical_validation"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_upstream_pffdtd_wave_runs"]==e["new_github_actions_runs"]==0
    assert e["not_original_PFFDTD_canonical_or_independent_physics"]
    heldout=e["newly_calculated_heldout_cases"]
    retained=e["previously_saved_PP40_PP44_cases"]
    assert [q["ppw"] for q in heldout]==[28,32,36]
    assert [q["ppw"] for q in retained]==[40,44]
    control={q["ppw"]:q for q in cg["actual_native_grid_point_impulse_exact_roof_cases"]}
    previous={q["ppw"]:q for q in old["true_fullmode_original_native_cases"]}
    for q in heldout:
        ppw=q["ppw"]
        assert q["all_modes_including_high_and_point_q0_kept"]
        assert q["true_all_eigenmodes_count"]==control[ppw]["original_nodal_active_cutcell_count"]
        assert q["original_Nt"]==control[ppw]["original_record_samples"]
        assert abs(q["original_native_Ts_s"]-control[ppw]["original_native_dt_s"])<1e-12
        assert abs(q["room_physical_exact_volume_m3"]-56)<2e-8
        assert q["full_kmk_arm_vs_prior_KmkK_full_mode_relative"] is None
        assert q["native_newmark_base_vs_prior_true_original_CG_wave_relative"]<2e-5
        assert set(q["every_full_mode_250ms_signed_transfer_40_80"])==set(ARMS)
        assert set(q["true_independent_sparse_operator_checks"])==set(ARMS)
        for arm in ARMS:
            x=unpairs(q["every_full_mode_250ms_signed_transfer_40_80"][arm])
            assert x.shape==(2,) and np.isfinite(x).all()
            y=q["true_independent_sparse_operator_checks"][arm]
            assert y["rigid_neumann_constant_null_residual_relative"]<1e-8
            assert y["true_3D_eigenresidual_relative"]<2e-5
    for q in retained:
        o=previous[q["ppw"]]
        assert q==o
    cases=heldout+retained
    pairs=e["all_five_grid_adjacent"]
    assert [(q["coarse_ppw"],q["fine_ppw"]) for q in pairs]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,pair in enumerate(pairs):
        assert set(pair["arms"])==set(ARMS)
        for arm in ARMS:
            score=pair["arms"][arm]
            c=cases[i]["every_full_mode_250ms_signed_transfer_40_80"][arm]
            f=cases[i+1]["every_full_mode_250ms_signed_transfer_40_80"][arm]
            assert np.allclose(unpairs(score["original_full_PP_coarse_minus_fine_signed_40_80"]),
                unpairs(c)-unpairs(f),rtol=1e-11,atol=1e-8)
            met=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            for metric in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(met[metric]-score["original_unchanged_three_gate_metrics"][metric])<1e-11
            passed=met["complex_rms_relative"]<=.2 and met["magnitude_max_relative"]<=.25 and met["phase_max_deg"]<=15
            assert passed is score["passes_all_original_limits"]
    for arm in ARMS:
        v=e["heldout_refinement_all_arm_verdicts"][arm]
        assert not v["all_four_original_adjacent_pairs_below_limits"]
        assert not v["original_complex_magnitude_phase_all_strictly_decrease"]
        assert v["original_canonical_not_requalified"]
        assert v["any_pair_that_fails"]
