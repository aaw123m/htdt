"""SHA-pinned five-grid physical x-fitted q0 experiment: fail-closed evidence."""
import json
from pathlib import Path
import numpy as np

R=Path(__file__).resolve().parents[2]/"benchmarks/acoustics"
E=R/"r130d_original_q0_boundary_fitted_uniform_x_true_roof_evidence_2026-10-10.json"
P=R/"r130d_original_q0_boundary_fitted_uniform_x_true_roof_plan_2026-10-10.json"
N=R/"r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"

def test_true_original_HDF5_all5_native_record_and_source_moments():
    j=json.loads(E.read_text(encoding="utf8"))
    p=json.loads(P.read_text(encoding="utf8"))
    o=json.loads(N.read_text(encoding="utf8"))
    raw={x["ppw"]:x for x in o["actual_native_wave_cases"]}
    assert j["preregistered_plan"]==p
    assert j["frozen_remote_plan_sha"]=="61c0de9d664280b42cf16f97eb579d0d976dc741"
    assert j["original_PFFDTD"]=="SELF_CONVERGENCE_FAILED"
    assert j["independent_physics"]=="NOT_VALIDATED"
    assert j["product"]=="NO_GO"
    assert j["new_native_original_PFFDTD_waves"]==j["new_github_actions"]==0
    cases=j["five_native_original_sha_fitted_x_cases"]
    assert [v["ppw"] for v in cases]==[28,32,36,40,44]
    for v in cases:
        assert v["original_source_SHA"]==raw[v["ppw"]]["original_native_comm_sha256"]
        assert v["original_geometry_SHA"]==raw[v["ppw"]]["original_solver_geometry_sha256"]
        assert abs(v["true_total_room_volume_m3"]-56)<2e-8
        assert v["uniform_x_node_count"]==v["uniform_x_segment_count"]+1
        assert v["uniform_x_segment_count"]==int(np.ceil(4/v["native_h"]))
        assert v["exact_uniform_x_end_half_segment_support"]
        assert abs(v["uniform_endpoint_min_physical_mass_length"]-v["uniform_x_pitch"]/2)<1e-12
        assert v["x_affine_true_Neumann_relative"]<2e-9
        assert v["roof_manufactured_tangent_weak_absolute"]<1e-7
        assert v["full_3d_allmode_count"]>35000
        assert not v["native_full_explicit_leapfrog_stable"]
        assert v["full_native_dt2_lambda_max"]>4
        assert v["unstable_complete_mode_count"]>0
        assert v["all_spatial_modes_and_true_physical_support_kept"]
        assert v["experimental_x_P1_reprojection_changes_discrete_operator_not_original_authority"]
        assert len(v["independent_first_true_roof_echo_analytic_3width"])==3
        assert len(v["complete_original_signed_250ms_40_80"])==2
        for k,expected in (("original_8_source_physical_moment_projection",[1.5,2,2]),
                           ("original_8_receiver_physical_moment_projection",[2.5,2,2])):
            a=v[k]
            assert a["number_original_native_HDF5_nodes"]==8
            assert a["original_native_HDF5_eight_weights_preserved"]
            np.testing.assert_allclose(a["original_physical_centroid_xyz_m"],expected,atol=2e-9,rtol=0)
            assert a["first_physical_moment_max_absolute_error_m"]<2e-9
            assert a["source_receiver_original_Q1_tensor_factorization_max"]<1e-12

def test_full_q0_original_unrelaxed_four_gate_failures_preserved():
    e=json.loads(E.read_text(encoding="utf8"))
    pairs=e["four_adjacent_complete_original_250ms_three_gate_scores"]
    assert [(x["coarse_ppw"],x["fine_ppw"]) for x in pairs]==[
        (28,32),(32,36),(36,40),(40,44)]
    expected=[(.36628817928121893,5.205373769624302,22.389152017952284),
              (.19048882197069844,.756504824789252,25.353056553559952),
              (.08033224160124128,.05836585432909604,10.241755368823362),
              (.8124780255869486,.5943406724716257,173.9762246806538)]
    for pair,values in zip(pairs,expected):
        np.testing.assert_allclose([
            pair["metrics"][s] for s in ("complex_rms_relative",
            "magnitude_max_relative","phase_max_deg")],values,rtol=1e-8,atol=1e-8)
    assert [x["all_original_three_gates_pass"] for x in pairs]==[
        False,False,True,False]
    v=e["frozen_full_fivegrid_acceptance_experimental_only"]
    assert not v["all_four_adjacent_three_gates"]
    assert not v["all_three_metrics_strict_monotone"]
    assert not v["experiment_only_all_acceptance_pass"]
    assert not v["original_upstream_PFFDTD_requalified"]
