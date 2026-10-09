"""Independent unchanged-source, full-mode/frozen gate hybrid evidence audit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from htdt.r130d_general3d_validation import compare_complex_transfer
from htdt.r130d_causal_first_roof_echo import ROOF_WIDTHS_S

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_original_q0_cartesian_flux_true_roof_hybrid import validate_plan
from run_r130d_original_point_quadratic_pffdtd import PPW

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_cut_roof_hybrid_plan_2026-10-10.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_roof_hybrid_evidence_2026-10-10.json"
ORIGINAL=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
Q1=ROOT/"benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_evidence_2026-10-09.json"


def test_hybrid_unfiltered_original_q0_real_sha_and_all_five_fixed_three_gates():
    plan=validate_plan(json.loads(PLAN.read_text(encoding="utf8")))
    proof=json.loads(EVIDENCE.read_text(encoding="utf8"))
    old=json.loads(ORIGINAL.read_text(encoding="utf8"))
    q1=json.loads(Q1.read_text(encoding="utf8"))
    assert proof["preregistered_plan"]==plan
    assert proof["preregistered_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert proof["canonical_upstream_original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert proof["independent_physical_validation"]=="NOT_VALIDATED"
    assert proof["product"]=="NO_GO"
    assert proof["new_native_PFFDTD_waves"]==0
    assert proof["new_GitHub_Actions_runs"]==0
    assert proof["original_PFFDTD_canonical_still_failed"]
    rows=proof["actual_native_original_point_q0_cartesian_hybrid_full_modes_cases"]
    assert [r["ppw"] for r in rows]==list(PPW)
    reference={r["ppw"]:r for r in old["actual_native_wave_cases"]}
    prior={r["ppw"]:r for r in q1["actual_native_original_point_q0_Q1_cutroof_full_modes_cases"]}
    for r in rows:
        ppw=r["ppw"]
        assert r["original_SHA256_native_comms"]==reference[ppw]["original_native_comm_sha256"]
        assert r["original_SHA256_native_voxel"]==reference[ppw]["original_solver_geometry_sha256"]
        assert r["original_native_full_Nt"]==prior[ppw]["original_native_full_Nt"]
        assert abs(r["original_native_Ts_s"]-prior[ppw]["original_native_Ts_s"])<1e-12
        assert r["real_original_native_3D_full_true_Q1_modes"]==prior[ppw][
            "real_original_native_3D_full_true_Q1_modes"]
        assert r["real_original_native_cartesian_yz_Q1_physical_support_modes"]==prior[ppw][
            "real_original_native_cartesian_yz_Q1_physical_support_modes"]
        assert r["hybrid_full_cartesian_axis_flux_rectangles"]>100
        assert r["hybrid_true_cut_Q1_rectangles"]>1
        assert r["hybrid_full_cartesian_axis_flux_rectangles"]+r["hybrid_true_cut_Q1_rectangles"]==r[
            "real_original_native_2D_cut_Q1_cell_count"]
        assert r["hybrid_exact_roof_affine_weak_peak"]<1e-7
        assert r["true_3D_Q1_constant_neumann_relative"]<1e-10
        assert r["true_3D_Q1_manufactured_affine_mass_rel"]<2e-9
        assert r["true_3D_Q1_manufactured_affine_stiffness_rel"]<2e-9
        assert abs(r["true_full_3D_original_roof_volume_m3"]-56)<2e-8
        assert r["no_high_mode_cut_or_sliver_point_removal"]
        assert r["same_original_eight_native_source_8_receiver_weights_and_full_q0"]
        assert len(r["hybrid_first_roof_echo_frozen_allmode_3width_diagnostic"])==3
        widths=[]
        for diag in r["hybrid_first_roof_echo_frozen_allmode_3width_diagnostic"]:
            widths.append(diag["physical_roof_echo_witness_width_s"])
            assert np.isfinite(diag["full_allmode_hybrid_native_pressure_weak"])
            assert abs(diag["independent_true_roof_64_pair_single_bounce_weak"])>0
            assert diag["all_original_high_modes_retained"]
            assert diag["cannot_isolate_first_roof_from_numerical_direct_tail"]
            assert diag["number_frozen_witness_samples"]>=8
        assert tuple(widths)==ROOF_WIDTHS_S
        assert np.isfinite(unpairs(r["new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80"])).all()
    compare=proof["original_signed_all_four_adjacent_three_gate_results"]
    assert [(r["coarse_ppw"],r["fine_ppw"]) for r in compare]==[
        (28,32),(32,36),(36,40),(40,44)]
    arm="new_native_cartesian_interior_fivepoint_true_roof_hybrid_original_8node"
    metrics=("complex_rms_relative","magnitude_max_relative","phase_max_deg")
    for pair,coarse,fine in zip(compare,rows[:-1],rows[1:]):
        m=compare_complex_transfer(
            reference=fine["new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80"],
            candidate=coarse["new_native_hybrid_true_roof_q0_full_original_250ms_signed_40_80"],
            frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
        observed=pair["arms"][arm]["original_frozen_complex_magnitude_phase_and_frequency_bins"]
        for key in metrics:
            assert abs(observed[key]-m[key])<1e-10
        passed=all(m[k]<=v for k,v in zip(metrics,(.2,.25,15)))
        assert passed==pair["arms"][arm]["all_three_original_frozen_gates_pass"]
    assert not proof["full_original_q0_five_grid_cartesian_hybrid_convergence_verdict"][arm][
        "all_four_pairs_pass_original_three_gates"]
    assert not proof["full_original_q0_five_grid_cartesian_hybrid_convergence_verdict"][arm][
        "all_three_metric_strictly_monotone"]
