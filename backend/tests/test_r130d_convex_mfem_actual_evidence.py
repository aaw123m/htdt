"""Actual pinned independent P2 MFEM 8-run witness and bounded cross-FV result.

No physically measured dataset and no production solver promotion.
"""
from __future__ import annotations
from pathlib import Path
import copy
import json

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]/"benchmarks"/"acoustics"
def get(name):
    return json.loads((ROOT/name).read_text(encoding="utf-8"))
def zz(vals):
    return np.array([complex(*v) for v in vals])


def _authorities():
    return (
        get("r130d_convex_independent_mfem_plan_2026-10-09.json"),
        get("r130d_convex_mfem_tetra_mesh_provenance_2026-10-09.json"),
        get("r130d_convex_independent_mfem_all_level_driven_evidence_2026-10-09.json"),
        get("r130d_convex_independent_mfem_same_source_evidence_2026-10-09.json"),
        get("r130d_convex_multiplane_cutcell_evidence_2026-10-09.json"),
    )


def test_all_8_actually_time_driven_pinned_mfem_p2_runs_and_sha():
    plan,meshes,levels,verdict,fv=_authorities()
    assert verdict["plan"]==plan
    assert verdict["mesh_provenance"]==meshes
    assert verdict["schema_version"]=="htdt.r130d.convex-independent-same-source-evidence-1"
    assert len(verdict["cases"])==2
    for name in ("planar_wedge","three_axis_diagonal"):
        m=meshes["rooms"][name]
        rows=levels[name]
        assert [x["uniform_refinements"] for x in rows]==[1,2,3,4]
        assert len(rows)==4
        assert [x["dofs"] for x in rows]==sorted(x["dofs"] for x in rows)
        for row in rows:
            assert row["room"]==name
            assert row["tetra_mesh_sha256"]==m["sha256"]
            assert len(row["system_sha256"])==64
            assert row["elements"]==m["tetrahedra"]*8**row["uniform_refinements"]
            assert row["true_linear_relative_residual_max"]<1e-8
            assert row["max_cg_iterations"]<=350
            assert row["actual_midpoint_injected_source"] is True
            assert row["production_enabled"] is False
            assert zz(row["transfer_complex_40_80_hz"]).shape==(2,)
            assert np.all(np.isfinite(zz(row["transfer_complex_40_80_hz"])))


def test_full_candidate_has_only_experimental_convergence_pass():
    plan,meshes,levels,v,fv=_authorities()
    assert v["production_enabled"] is False
    assert v["original_r130d_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert v["physical_validation"]=="NOT_VALIDATED"
    for name in ("planar_wedge","three_axis_diagonal"):
        row=next(x for x in v["cases"] if x["room"]==name)
        assert row["mfem_self_pass"] is True
        assert row["fv_last_adjacent_pass"] is True
        assert row["cross_pass"] is True
        assert row["experimental_numeric_candidate_pass"] is True
        fine=row["mfem_adjacent"][-1]
        cross=row["cross_fv_n20_vs_mfem_r4"]
        assert fine["normalized_complex_l2"]<plan["unchanged_self_limits"]["complex_rms_relative_max"]
        assert max(fine["phase_difference_deg_by_hz"])<5
        assert cross["normalized_complex_l2"]<.35
        assert max(cross["phase_difference_deg_by_hz"])<25
    assert next(x for x in v["cases"] if x["room"]=="planar_wedge")[
        "cross_fv_n20_vs_mfem_r4"]["normalized_complex_l2"]==pytest.approx(.1344899136185062,abs=1e-12)
    assert next(x for x in v["cases"] if x["room"]=="three_axis_diagonal")[
        "cross_fv_n20_vs_mfem_r4"]["normalized_complex_l2"]==pytest.approx(.17728141194584265,abs=1e-12)


def test_actual_fem_gate_is_recomputed_no_ref4_or_threshold_substitution():
    import sys
    sys.path.insert(0,str(ROOT.parents[1]/"scripts"))
    from run_r130d_convex_independent_mfem_drive import evaluate
    plan,meshes,levels,verdict,fv=_authorities()
    actual=evaluate(plan,meshes,levels,fv)
    # Sparse floating reductions vary by a few ULPs between BLAS builds.
    # Preserve exact authority/gate labels and compare ONLY floats under
    # strict replay tolerance, never physical acceptance thresholds.
    def assert_same_evidence(actual_value, frozen_value):
        if isinstance(frozen_value, dict):
            assert isinstance(actual_value, dict)
            assert set(actual_value)==set(frozen_value)
            for key in frozen_value:
                assert_same_evidence(actual_value[key],frozen_value[key])
        elif isinstance(frozen_value,list):
            assert isinstance(actual_value,list)
            assert len(actual_value)==len(frozen_value)
            for left,right in zip(actual_value,frozen_value):
                assert_same_evidence(left,right)
        elif isinstance(frozen_value,float):
            assert actual_value==pytest.approx(frozen_value,rel=1e-11,abs=1e-11)
        else:
            assert actual_value==frozen_value
    assert_same_evidence(actual,verdict)
    dropped=copy.deepcopy(levels)
    dropped["planar_wedge"].pop()
    with pytest.raises(ValueError,match="missing"):
        evaluate(plan,meshes,dropped,fv)
    changed=copy.deepcopy(plan)
    changed["unchanged_cross_limits"]["phase_max_deg"]=180
    with pytest.raises(ValueError,match="limits"):
        evaluate(changed,meshes,levels,fv)


def test_high_fv_to_independent_mfem_r4_evidence_does_not_hide_amplitude_stall():
    ref=get("r130d_convex_high_fv_vs_mfem_r4_supplemental_2026-10-09.json")
    _,_,levels,verdict,fv=_authorities()
    high=get("r130d_convex_high_resolution_followup_evidence_2026-10-09.json")
    assert ref["production_ready"] is False
    assert ref["new_geometries_high_resolution_full_monotonicity_gate"]=="NOT_QUALIFIED"
    for row in ref["cases"]:
        name=row["room"]
        assert row["n20_registered_candidate_pass"] is True
        assert row["n32_supplemental_cross_validation_only"] is True
        assert row["fv_magnitude_max_strict_decrease"] is False
        comp=row["comparisons"]
        assert [x["fv_n"] for x in comp]==[20,24,28,32]
        fem=zz(levels[name][-1]["transfer_complex_40_80_hz"])
        actual=next(x for x in high["cases"] if x["name"]==name)
        for comparison,fvlevel in zip(comp,actual["history"]):
            x=zz(fvlevel["complex_transfer_40_80_hz"])
            assert comparison["complex_relative"]==pytest.approx(
                np.linalg.norm(x-fem)/np.linalg.norm(fem),abs=1e-12)
            assert comparison["magnitude_max_db"]==pytest.approx(
                max(abs(20*np.log10(abs(x)/abs(fem)))),abs=1e-11)
        assert comp[-1]["complex_relative"] < comp[0]["complex_relative"]


def test_signed_source_phase_and_missing_40_80_sample_are_not_mutable():
    plan,meshes,levels,verdict,fv=_authorities()
    assert plan["waveform"]["q"]=="exp(-.5*((t-.012)/.003)^2) m3/s"
    assert plan["waveform"]["bins_hz"]==[40,80]
    assert plan["waveform"]["dt_s"]==.00025
    assert plan["waveform"]["duration_s"]==.25
    assert plan["frozen"]["canonical_original_impulse"]=="SELF_CONVERGENCE_FAILED"
