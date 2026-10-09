"""No-promotion regression for preregistered original five-grid modal evidence."""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
PATH=ROOT/"benchmarks/acoustics/r130d_original_q0_hybrid_modal_endpoint_attribution_evidence_2026-10-10.json"
PRIOR=ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_roof_hybrid_evidence_2026-10-10.json"
NATIVE=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_hybrid_modal_endpoint_attribution_plan_2026-10-10.json"

def complex2(x):
    a=np.asarray(x,dtype=float)
    assert a.shape==(2,2) and np.isfinite(a).all()
    return a[:,0]+1j*a[:,1]

@pytest.fixture(scope="module")
def evidences():
    p=json.loads(PLAN.read_text(encoding="utf8"))
    a=json.loads(PRIOR.read_text(encoding="utf8"))
    b=json.loads(NATIVE.read_text(encoding="utf8"))
    e=json.loads(PATH.read_text(encoding="utf8"))
    return p,a,b,e

def test_exact_frozen_prospective_remote_plan_and_no_promotion(evidences):
    p,a,b,e=evidences
    assert e["schema_version"]=="htdt.r130d.original-q0-hybrid-modal-endpoint-evidence-1"
    assert e["preregistered_plan"]==p
    assert e["preregistered_remote_plan_commit"]=="ff0f3b96ffa27aa3dc0fc072450f93f7a8fcb4ac"
    assert e["original_PFFDTD"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physics"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_native_PFFDTD_runs"]==0
    assert e["new_GitHub_Actions_runs"]==0
    assert not e["canonical_full_original_250ms_signed_five_grid_verdict"]["all_four_three_gate_pass"]
    assert e["canonical_full_original_250ms_signed_five_grid_verdict"]["no_original_solver_requalification"]

def test_all_five_pinned_native_8node_full_mode_reconstruction(evidences):
    _,a,b,e=evidences
    original={q["ppw"]:q for q in b["actual_native_wave_cases"]}
    old={q["ppw"]:q for q in a["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"]}
    cases=e["all_five_original_q0_modal_endpoint_cases"]
    assert [x["ppw"] for x in cases]==[28,32,36,40,44]
    assert [x["all_original_3D_modes"] for x in cases]==[37835,53001,75504,102949,132341]
    for q in cases:
        ppw=q["ppw"]
        assert q["native_comms_sha"]==original[ppw]["original_native_comm_sha256"]
        assert q["native_room_sha"]==original[ppw]["original_solver_geometry_sha256"]
        assert q["original_native_Nt"]==old[ppw]["original_native_full_Nt"]
        assert q["original_native_Ts_s"]==old[ppw]["original_native_Ts_s"]
        assert q["all_original_Q1_positive_supported_yz_modes"]==old[ppw]["real_original_native_cartesian_yz_Q1_physical_support_modes"]
        ref=complex2(old[ppw]["new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80"])
        np.testing.assert_allclose(complex2(q["frozen_full_signed_40_80"]),ref,rtol=0,atol=0)
        np.testing.assert_allclose(complex2(q["recomputed_unmodified_full_signed_40_80"]),ref,rtol=2e-8,atol=1e-8)
        endpoint=q["endpoint_signed_40_80"]
        assert list(endpoint)==[
            "first_forward_derivative_sample_n0",
            "interior_centered_derivative_samples_n1_to_N_minus_2",
            "last_backward_derivative_sample_nN_minus_1"]
        total=sum((complex2(z) for z in endpoint.values()),np.zeros(2,complex))
        np.testing.assert_allclose(total,ref,rtol=2e-8,atol=1e-7)
        bands=q["frequency_bands_signed_all_modes"]
        assert len(bands)==7
        assert sum(x["mode_count"] for x in bands)==q["all_original_3D_modes"]
        np.testing.assert_allclose(sum((complex2(z["signed_40_80"]) for z in bands),
                                       np.zeros(2,complex)),ref,rtol=2e-8,atol=1e-7)
        for j,name in enumerate(endpoint):
            z=sum((complex2(band["endpoint_signed_40_80"][name]) for band in bands),
                  np.zeros(2,complex))
            np.testing.assert_allclose(z,complex2(endpoint[name]),rtol=2e-8,atol=1e-7)
        np.testing.assert_allclose(
            complex2(q["native_at_or_below_nyquist_signed_40_80"])
            +complex2(q["native_above_nyquist_signed_40_80"]),
            ref,rtol=2e-8,atol=1e-7)
        assert q["native_above_nyquist_mode_count"]+q["native_at_or_below_nyquist_mode_count"]==q["all_original_3D_modes"]
        assert all(v<=2e-8 for v in q["reconstruction_relative"].values())
        assert q["all_high_modes_retained_not_accepted_without_canonical_gates"]

def test_original_four_frozen_acceptance_scores_and_additive_signed_projections(evidences):
    _,a,_,e=evidences
    pairs=e["all_four_original_full_250ms_signed_three_gates_and_diagnostic_deltas"]
    old=a["original_signed_all_four_adjacent_three_gate_results"]
    arm="new_native_cartesian_interior_fivepoint_true_roof_hybrid_original_8node"
    assert [(q["coarse_ppw"],q["fine_ppw"]) for q in pairs]==[(28,32),(32,36),(36,40),(40,44)]
    for i,(p,prior) in enumerate(zip(pairs,old)):
        metrics=p["unchanged_full_complex_magnitude_phase"]
        ref=prior["arms"][arm]["original_frozen_complex_magnitude_phase_and_frequency_bins"]
        for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
            assert abs(metrics[k]-ref[k])<1e-8
        assert p["canonical_all_three_gates_pass"]==(i==3)
        assert p["no_frequency_band_or_endpoint_removed_from_original_transfer"]
        assert p["signed_projection_sums_to_one_for_each_bin"]
        assert len(p["by_unfiltered_disjoint_modal_bands"])==7
        assert len(p["by_native_pressure_derivative_endpoint_parts"])==3
        assert len(p["by_native_nyquist_diagnostic"])==2
        for channel in (
            "by_unfiltered_disjoint_modal_bands",
            "by_native_pressure_derivative_endpoint_parts",
            "by_native_nyquist_diagnostic"):
            projections=np.asarray(list(p[channel].values()),dtype=float)
            assert projections.shape[1:]==(2,)
            np.testing.assert_allclose(projections.sum(axis=0),[1.,1.],rtol=2e-8,atol=2e-8)
    key="last_backward_derivative_sample_nN_minus_1"
    assert abs(pairs[1]["by_native_pressure_derivative_endpoint_parts"][key][0]-.557486668858012)<2e-6
