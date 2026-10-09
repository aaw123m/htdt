"""Proof obligations for a preregistered, fail-closed experimental R130D gate."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from htdt.r130d_candidate_same_source_qualification import assess,metrics
import numpy as np


ROOT=Path(__file__).resolve().parents[2]/"benchmarks"/"acoustics"


def inputs():
    def read(n):
        return json.loads((ROOT/n).read_text(encoding="utf-8"))
    return {
      "candidate_plan":read("r130d_candidate_same_source_numerical_acceptance_plan_2026-10-09.json"),
      "parent_frozen_plan":read("r130d_general3d_validation_plan.json"),
      "fv_evidence":read("r130d_embedded_neumann_fv_driven_followup_2026-10-09.json"),
      "mfem_r123_evidence":read("r130d_fv_mfem_same_drive_evidence_2026-10-09.json"),
    }


def test_pending_mfem_r4_keeps_cross_solver_blocked():
    result=assess(**inputs())
    assert result["fv_self_state"]=="SELF_CONVERGENCE_PASS_CANDIDATE_ONLY"
    assert result["mfem_self_state"]=="REFINEMENT_R4_MISSING"
    assert result["cross_solver_state"]=="CROSS_SOLVER_BLOCKED"
    assert result["candidate_numerical_evidence_state"]=="CANDIDATE_NUMERICAL_NOT_QUALIFIED"
    assert result["immutable_production_gate"]=={
        "canonical_r130d_impulse":"SELF_CONVERGENCE_FAILED",
        "production_enabled":False,
        "physical_validation":"NOT_VALIDATED",
    }


def synthetic_reference(inp, transfer):
    return {
       "schema_version":"htdt.r130d.mfem-r4-same-source-independent-result-1",
       "mfem_dofs":35937,
       "mfem_refinement":4,
       "mfem_source_sha":"d964264cdb9a13e94a201b6c236c7721e0c8765f",
       "transfer_complex_40_80_hz":transfer,
       "gate":{"production_ready":False},
    }


def test_synthetic_exact_refinement_can_pass_only_experimental_gate():
    inp=inputs()
    frozen_r3=inp["mfem_r123_evidence"]["mfem_levels"][-1]["transfer_complex_40_80_hz"]
    fake=synthetic_reference(inp,frozen_r3)
    result=assess(**inp,mfem_r4_evidence=fake)
    assert result["mfem_self_state"]=="SELF_CONVERGENCE_PASS_CANDIDATE_ONLY"
    assert result["cross_solver_state"]=="CROSS_SOLVER_PASS_CANDIDATE_ONLY"
    assert result["candidate_numerical_evidence_state"]=="EXPERIMENTAL_CANDIDATE_NUMERICAL_PASS_ONLY"
    assert result["immutable_production_gate"]["production_enabled"] is False


def test_synthetic_nonconvergent_ref4_cannot_unlock_cross_solver():
    inp=inputs()
    fake=synthetic_reference(inp,[[1000,4000],[-4000,1000]])
    result=assess(**inp,mfem_r4_evidence=fake)
    assert result["mfem_self_state"]=="SELF_CONVERGENCE_FAILED"
    assert result["cross_solver_state"]=="CROSS_SOLVER_BLOCKED"
    assert result["candidate_numerical_evidence_state"]=="CANDIDATE_NUMERICAL_NOT_QUALIFIED"


def test_canonical_thresholds_and_frozen_input_cannot_be_retuned():
    inp=inputs()
    inp["candidate_plan"]["strict_thresholds_unmodified_from_parent"]["mfem_self"]["phase_max_deg"]=180
    with pytest.raises(ValueError,match="tolerances"):
        assess(**inp)
    inp=inputs()
    inp["candidate_plan"]["bins_hz"]=[40]
    with pytest.raises(ValueError):
        assess(**inp)
    inp=inputs()
    inp["candidate_plan"]["fail_closed"]["production_enabled"]=True
    with pytest.raises(ValueError):
        assess(**inp)


def test_metric_phase_wrap_and_no_bin_cherrypicking():
    a=np.array([complex(-1,0.01),complex(3,4)])
    b=np.array([complex(-1,-0.01),complex(3,4)])
    m=metrics(a,b)
    assert m["phase_max_deg"]<2
    with pytest.raises(ValueError):
        metrics(a[:1],b[:1])
    with pytest.raises(ValueError):
        metrics(a,np.array([0j,1+1j]))
