"""Evidence integrity of actual pinned upstream PFFDTD full native Q2 point wave solves.

CI verifies mathematical integrity and saved spectra, not full upstream replay,
which requires separately archived original high-PPW HDF5 setup assets.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import sys
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_point_quadratic_pffdtd import (
    PPW,ORIG_SHA,PIN,validate_plan,native_pair_metrics)

EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
PLAN=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_plan_2026-10-09.json"

def frozen():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["plan_sha256_lf"]==hashlib.sha256(PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    return p,e

def test_no_approval_and_complete_real_native_pffdtd_five_levels():
    p,e=frozen()
    assert e["schema_version"]=="htdt.r130d.native-original-physical-point-quadratic-q0-evidence-1"
    assert e["canonical_original_8_node_point_point"]=="SELF_CONVERGENCE_FAILED"
    assert e["canonical_original_point_source_physical_validation"]=="NOT_VALIDATED"
    assert e["experimental_27_point_discretization_not_promoted"] is True
    assert e["original_8_node_PFFDTD_code_modified"] is False
    assert e["production_ready"] is False
    assert e["frozen_original_high_ppw_record_sha256"]==ORIG_SHA
    assert e["pinned_upstream_sha"]==PIN
    assert len(e["actual_native_wave_cases"])==5
    assert [r["ppw"] for r in e["actual_native_wave_cases"]]==list(PPW)
    assert [r["sample_count"] for r in [x["native_solver"] for x in e["actual_native_wave_cases"]]]==[1214,1388,1561,1734,1908]

def test_every_native_signed_wave_40_80_and_weight_exactness_and_source_q0():
    p,e=frozen()
    for case in e["actual_native_wave_cases"]:
        ppw=case["ppw"]
        assert case["original_native_comm_sha256"]==p["original_native_comms_sha_by_ppw"][str(ppw)]
        assert case["original_same_geometry_and_point_locations"] is True
        assert case["native_original_discrete_q0_unchanged"] is True
        assert case["native_solver"]["pffdtd_engine_same_pinned_upstream"] is True
        assert case["native_solver"]["sample_count"]<=p["resource_limits"]["max_native_steps"]
        assert math.prod(case["native_solver"]["grid_dimensions"])<=p["resource_limits"]["max_grid_cells"]
        assert len(case["unmodified_original_transfer_pa_per_m3_s"])==2
        assert len(case["quadratic_transfer_pa_per_m3_s"])==2
        for arr in (case["unmodified_original_transfer_pa_per_m3_s"],
                    case["quadratic_transfer_pa_per_m3_s"]):
            assert np.asarray(arr).shape==(2,2)
            assert np.all(np.isfinite(arr))
        op=case["numerical_operator"]
        assert op["native_original_27node_engine_contract"] is True
        assert op["original_native_total_unit_impulse"]==pytest.approx(op["quadratic_total_unit_impulse"],abs=1e-12)
        for part in ("source","receiver"):
            item=op[part]
            assert item["support_count"]==27
            assert item["negative_node_count"]>0
            assert item["absolute_weight_sum"]>1
            moments=item["moments"]
            assert moments["zeroth"]==pytest.approx(1,abs=1e-12)
            for key in ("first_m","axis_second_m2","cross_second_m2"):
                assert max(abs(z) for z in moments[key])<1e-10
            assert item["grid_dimensions"]==tuple(case["native_solver"]["grid_dimensions"]) or list(item["grid_dimensions"])==case["native_solver"]["grid_dimensions"]
        assert case["native_solver"]["wall_seconds"]>0
        assert len(case["native_solver"]["sim_outs_sha256"])==64

def test_preserved_unfavorable_results_and_original_thresholds():
    p,e=frozen()
    x=native_pair_metrics(e["actual_native_wave_cases"],p)
    assert x==e["adjacent_quadratic_refinement"]
    assert x["diagnostic_refinement_pass"] is False
    assert x["all_three_metrics_strictly_decreasing"] is False
    assert x["last_pair_passes_original_frozen_thresholds"] is False
    assert [(z["coarse_ppw"],z["fine_ppw"]) for z in x["pairs"]]==[
        (28,32),(32,36),(36,40),(40,44)]
    assert all(z["compared_frequency_count"]==2 for z in x["pairs"])
    for z in x["pairs"]:
        assert z["complex_rms_relative"]>0
        assert z["phase_max_deg"]>=0
        assert z["magnitude_max_relative"]>=0
    # Most unfavorable results are never omitted from the evidence.
    assert x["pairs"][0]["complex_rms_relative"]>2
    assert x["pairs"][2]["complex_rms_relative"]>2


def test_second_distinct_native_sim_execution_reproduces_every_signed_wave_and_raw_h5_sha():
    """Two native solver reruns, not simply two postprocessing passes."""
    p,e=frozen()
    replay_path=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_independent_replay_2026-10-09.json"
    replay=json.loads(replay_path.read_text(encoding="utf-8"))
    assert replay["schema_version"]==e["schema_version"]
    assert replay["preregistered_plan"]==e["preregistered_plan"]
    assert replay["plan_sha256_lf"]==e["plan_sha256_lf"]
    assert replay["frozen_original_high_ppw_record_sha256"]==e["frozen_original_high_ppw_record_sha256"]
    assert replay["adjacent_quadratic_refinement"]==e["adjacent_quadratic_refinement"]
    assert len(replay["actual_native_wave_cases"])==5
    for first,second in zip(e["actual_native_wave_cases"],replay["actual_native_wave_cases"]):
        assert first["ppw"]==second["ppw"]
        assert first["original_native_comm_sha256"]==second["original_native_comm_sha256"]
        assert first["original_solver_geometry_sha256"]==second["original_solver_geometry_sha256"]
        assert first["numerical_operator"]==second["numerical_operator"]
        assert first["unmodified_original_transfer_pa_per_m3_s"]==second["unmodified_original_transfer_pa_per_m3_s"]
        assert first["quadratic_transfer_pa_per_m3_s"]==second["quadratic_transfer_pa_per_m3_s"]
        assert first["native_solver"]["sim_outs_sha256"]==second["native_solver"]["sim_outs_sha256"]
        assert first["native_solver"]["sample_count"]==second["native_solver"]["sample_count"]
        assert first["native_solver"]["dt_s"]==second["native_solver"]["dt_s"]
        assert first["native_solver"]["boundary_halo_treatment"]==second["native_solver"]["boundary_halo_treatment"]
    assert replay["canonical_original_8_node_point_point"]=="SELF_CONVERGENCE_FAILED"
    assert replay["production_ready"] is False
