"""Frozen evidence checks for experimental R130D conservative embedded FV."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
EVIDENCE=ROOT/"benchmarks"/"acoustics"


def read(name):
    return json.loads((EVIDENCE/name).read_text(encoding="utf-8"))


def test_preregistered_plan_binds_original_reference_and_source():
    p=read("r130d_embedded_neumann_fv_refinement_plan.json")
    baseline=EVIDENCE/"r130d_embedded_neumann_fv_driven_baseline_2026-10-09.json"
    assert hashlib.sha256(baseline.read_bytes()).hexdigest()==p["baseline_record_sha256"]
    assert p["planned_followup_levels"]==[28,32]
    assert p["baseline_levels"]==[12,16,20,24]
    assert p["frequencies_hz"]==[40,80]
    assert p["time"]["dt_s"]==0.00025
    assert p["time"]["duration_s"]==0.25
    assert p["q_smooth_gaussian"]=={
      "center_s":0.012,"sigma_s":0.003,"amplitude_m3_s":1,
    }
    assert p["acceptance"]["canonical_pffdtd_self_convergence"]=="SELF_CONVERGENCE_FAILED"
    assert p["acceptance"]["cross_solver_eligible"] is False


def test_fv_refinement_predeclared_continuation_is_monotone_but_not_adopted():
    plan=read("r130d_embedded_neumann_fv_refinement_plan.json")
    baseline=read("r130d_embedded_neumann_fv_driven_baseline_2026-10-09.json")
    followup=read("r130d_embedded_neumann_fv_driven_followup_2026-10-09.json")
    assert [v["grid_cells_per_axis"] for v in baseline["levels"]]==plan["baseline_levels"]
    assert [v["grid_cells_per_axis"] for v in followup["levels"]]==[20,24,28,32]
    assert followup["actual_driven_solver_execution"] is True
    assert followup["physical_model_change_from_frozen_run25"] is True
    assert followup["cross_solver_eligible"] is False
    assert followup["production_ready"] is False
    assert followup["pffdtd_canonical_self_convergence"]=="SELF_CONVERGENCE_FAILED"
    assert followup["levels"][:2]==baseline["levels"][-2:]
    complex_errors=[]
    for coarse,fine,metrics in zip(followup["levels"],followup["levels"][1:],
                                   followup["pairs"]):
        x=np.asarray([complex(*v) for v in coarse["transfer_complex_40_80_hz"]])
        y=np.asarray([complex(*v) for v in fine["transfer_complex_40_80_hz"]])
        relative=float(np.linalg.norm(x-y)/np.linalg.norm(y))
        assert metrics["normalized_complex_rms"]==pytest.approx(relative,rel=1e-12)
        complex_errors.append(relative)
    assert len(complex_errors)==3
    assert all(complex_errors[i+1]<complex_errors[i] for i in range(2))
    assert complex_errors[-1]<0.03


def test_eigen_evidence_exact_room_volume_and_energy():
    evidence=read("r130d_embedded_neumann_fv_eigen_evidence_2026-10-09.json")
    assert evidence["production_pffdtd_modified"] is False
    assert evidence["cross_solver_eligible"] is False
    assert [x["cells_per_axis"] for x in evidence["levels"]]==[6,8,12,16]
    assert all(x["integrated_fluid_volume_m3"]==pytest.approx(56.0)
               for x in evidence["levels"])
    assert all(x["undriven_midpoint_energy"]["max_relative_energy_drift"]<1e-9
               for x in evidence["levels"])
