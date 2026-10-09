"""Immutable high-resolution convex wave diagnostics; do not conceal magnitude stalls."""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]/"benchmarks"/"acoustics"
def read(name):
    return json.loads((ROOT/name).read_text(encoding="utf-8"))
def complex_of(row):
    return np.asarray([complex(*x) for x in row["complex_transfer_40_80_hz"]])


def test_preregistered_convergent_high_grid_experiment_is_nonproduction():
    r=read("r130d_convex_high_resolution_followup_evidence_2026-10-09.json")
    plan=read("r130d_convex_high_resolution_followup_plan_2026-10-09.json")
    assert r["plan"]==plan
    assert r["schema_version"]=="htdt.r130d.experimental-convex-high-fv-evidence-1"
    assert r["production_enabled"] is False
    assert r["independent_cross_solver_new_geometries"]=="PENDING_MFEM_R4"
    assert r["original_r130d_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert [x["name"] for x in r["cases"]]==["planar_wedge","three_axis_diagonal"]


@pytest.mark.parametrize("name",["planar_wedge","three_axis_diagonal"])
def test_actual_source_all_three_refinements_preserve_both_frequency_bins(name):
    r=read("r130d_convex_high_resolution_followup_evidence_2026-10-09.json")
    room=next(x for x in r["cases"] if x["name"]==name)
    levels=room["history"]
    assert [x["n"] for x in levels]==[20,24,28,32]
    assert levels[0]["previously_executed_frozen_level"] is True
    assert all(x["previously_executed_frozen_level"] is False for x in levels[1:])
    for row in levels:
        assert complex_of(row).shape==(2,)
        assert np.all(np.isfinite(complex_of(row)))
    for a,b,m in zip(levels,levels[1:],room["adjacent"]):
        aa,bb=complex_of(a),complex_of(b)
        assert m["complex_l2_relative"]==pytest.approx(
            np.linalg.norm(aa-bb)/np.linalg.norm(bb),abs=1e-12)
        assert m["magnitude_max_relative"]==pytest.approx(
            max(abs(abs(aa)-abs(bb))/abs(bb)),abs=1e-12)
        assert m["phase_max_deg"]==pytest.approx(
            max(abs(np.angle(aa/bb,deg=True))),abs=1e-11)
    assert room["complex_l2_monotone"] is True
    assert all(x["phase_max_deg"]>y["phase_max_deg"]
               for x,y in zip(room["adjacent"],room["adjacent"][1:]))


def test_unsafe_amplitude_nonmonotone_segments_cannot_be_disguised_as_pass():
    r=read("r130d_convex_high_resolution_followup_evidence_2026-10-09.json")
    wedge=next(x for x in r["cases"] if x["name"]=="planar_wedge")
    diag=next(x for x in r["cases"] if x["name"]=="three_axis_diagonal")
    # Wedge n24→28 0.0080, n28→32 0.0123; diagonal n20→24
    # 0.0052, n24→28 0.0148: amplitude error is NOT strictly monotone.
    assert wedge["adjacent"][2]["magnitude_max_relative"]>wedge["adjacent"][1]["magnitude_max_relative"]
    assert diag["adjacent"][1]["magnitude_max_relative"]>diag["adjacent"][0]["magnitude_max_relative"]
    assert r["independent_cross_solver_new_geometries"]=="PENDING_MFEM_R4"


def test_high_cut_geometry_keeps_exact_air_volume_and_neumann_constant_mode():
    r=read("r130d_convex_high_resolution_followup_evidence_2026-10-09.json")
    for room in r["cases"]:
        analytic=149/3 if room["name"]=="planar_wedge" else 160/3
        for row in room["history"][1:]:
            assert row["air_volume_m3"]==pytest.approx(analytic,rel=1e-10)
            assert row["minimum_fluid_fraction"]>1e-12
            assert row["normalized_neumann_residual"]<1e-11
            assert row["dofs"]<row["n"]**3
