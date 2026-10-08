"""Reproducible, independently driven MFEM r4 / cut-cell FV candidate pass.

Crucially, passing is limited to the *new Gaussian source and new numerical
operator*. The original run25 physical authority remains FAILED and must
never be updated by these tests.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from htdt.r130d_candidate_same_source_qualification import assess,metrics


ROOT = Path(__file__).resolve().parents[2]/"benchmarks"/"acoustics"


def read(name):
    return json.loads((ROOT/name).read_text(encoding="utf-8"))


def _inputs():
    return {
        "candidate_plan":read("r130d_candidate_same_source_numerical_acceptance_plan_2026-10-09.json"),
        "parent_frozen_plan":read("r130d_general3d_validation_plan.json"),
        "fv_evidence":read("r130d_embedded_neumann_fv_driven_followup_2026-10-09.json"),
        "mfem_r123_evidence":read("r130d_fv_mfem_same_drive_evidence_2026-10-09.json"),
        "mfem_r4_evidence":read("r130d_mfem_ref4_same_drive_v2_evidence_2026-10-09.json"),
    }


def test_tighter_iterative_solver_did_not_relax_any_physical_threshold():
    a=read("r130d_mfem_ref4_same_drive_plan_2026-10-09.json")
    b=read("r130d_mfem_ref4_same_drive_solver_v2_plan_2026-10-09.json")
    assert a["linear_solver"]["atol"]==1e-12
    assert b["linear_solver"]["atol"]==0
    for k in ("rtol","maxiter","preconditioner","true_residual_relative_max"):
        assert a["linear_solver"][k]==b["linear_solver"][k]
    for k in ("source_q_m3_s","frequencies_hz","dt_s","duration_s",
              "steps","source_xyz_m","receiver_xyz_m","expected_dofs",
              "geometry","expected_tetra_elements"):
        assert a[k]==b[k]
    assert b["independent_ref4_system_sha256"]==(
        "e1c67d02db77a6e8775a0a59d8e75fa996434d58f8efae077f4cec2cf2c831ed")


def test_real_mfem_r4_matrix_is_independently_reproduced_and_integrity_bound():
    evidence=read("r130d_mfem_ref4_same_drive_v2_evidence_2026-10-09.json")
    packed=(ROOT/"r130d_mfem_independent_sparse_systems"/"mfem-r4.json.gz").read_bytes()
    raw=gzip.decompress(packed)
    assert hashlib.sha256(raw).hexdigest()==evidence["mfem_sparse_export_sha256"]
    doc=json.loads(raw)
    assert doc["ndofs"]==evidence["mfem_dofs"]==35937
    assert doc["elements"]==evidence["mfem_element_count"]==24576
    assert doc["uniform_refinements"]==4
    assert doc["order"]==2
    assert doc["boundary_model"]=="natural_neumann_rigid"
    assert doc["source_position_m"]==[1.5,2,2]
    assert doc["receiver_position_m"]==[2.5,2,2]
    assert evidence["plan_schema_version"]==(
        "htdt.r130d.same-source-independent-mfem-r4-solver-revision-2")
    assert evidence["actual_sparse_solver"]["max_conjugate_gradient_iterations"]<=350
    assert evidence["actual_sparse_solver"]["max_true_relative_linear_residual"]<=1e-8
    assert evidence["gate"]["production_ready"] is False


def test_mfem_r4_and_cross_solver_real_measurements_recalculate():
    i=_inputs()
    r3=i["mfem_r123_evidence"]["mfem_levels"][-1]
    r4=i["mfem_r4_evidence"]
    fv=i["fv_evidence"]["levels"][-1]
    def transfers(x,key):
        a=np.asarray(x[key],dtype=np.float64)
        return a[:,0]+1j*a[:,1]
    a=transfers(r3,"transfer_complex_40_80_hz")
    b=transfers(r4,"transfer_complex_40_80_hz")
    c=transfers(fv,"transfer_complex_40_80_hz")
    assert metrics(a,b)["complex_rms_relative"]==pytest.approx(
        r4["fem_r3_to_r4_metrics"]["normalized_complex_l2"],abs=1e-12)
    assert metrics(c,b)["complex_rms_relative"]==pytest.approx(
        r4["fv_n32_to_mfem_r4_metrics"]["normalized_complex_l2"],abs=1e-12)


def test_actual_candidate_pass_is_bounded_and_fail_closed_to_production():
    inputs=_inputs()
    verdict=assess(**inputs)
    saved=read("r130d_candidate_same_source_numerical_pass_2026-10-09.json")
    assert verdict==saved["gate"]
    assert verdict["candidate_numerical_evidence_state"]==(
        "EXPERIMENTAL_CANDIDATE_NUMERICAL_PASS_ONLY")
    assert verdict["fv_self_state"]=="SELF_CONVERGENCE_PASS_CANDIDATE_ONLY"
    assert verdict["mfem_self_state"]=="SELF_CONVERGENCE_PASS_CANDIDATE_ONLY"
    assert verdict["cross_solver_state"]=="CROSS_SOLVER_PASS_CANDIDATE_ONLY"
    assert verdict["immutable_production_gate"]=={
        "canonical_r130d_impulse":"SELF_CONVERGENCE_FAILED",
        "production_enabled":False,
        "physical_validation":"NOT_VALIDATED",
    }
    assert saved["production_ready"] is False


def test_ref4_identity_mutation_or_missing_evidence_still_blocks_adoption():
    inputs=_inputs()
    assert assess(**{**inputs,"mfem_r4_evidence":None})[
        "cross_solver_state"]=="CROSS_SOLVER_BLOCKED"
    broken=copy.deepcopy(inputs)
    broken["mfem_r4_evidence"]["mfem_source_sha"]="wrong-source"
    with pytest.raises(ValueError):
        assess(**broken)
    broken=copy.deepcopy(inputs)
    broken["candidate_plan"]["strict_thresholds_unmodified_from_parent"][
        "fine_fine"]["complex_rms_relative_max"]=9000
    with pytest.raises(ValueError):
        assess(**broken)
