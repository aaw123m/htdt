"""Immutable preregistered multi-plane exact-geometry diagnostic evidence checks."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]/"benchmarks"/"acoustics"


def read(name):
    return json.loads((ROOT/name).read_text(encoding="utf-8"))


def _complex(row):
    data=row["actual_driven_transfer_40_80_hz"]
    return np.asarray([complex(*x) for x in data])


def test_independent_plan_remains_preregistered_and_no_production_adoption():
    plan=read("r130d_convex_multiplane_cutcell_plan_2026-10-09.json")
    r=read("r130d_convex_multiplane_cutcell_evidence_2026-10-09.json")
    assert r["plan"]==plan
    assert r["physical_authority"]=="EXPERIMENTAL_ONLY"
    assert r["production_enabled"] is False
    assert r["general_cad_qualified"] is False
    assert r["new_geometries_cross_solver_qualified"] is False
    assert r["original_solver_modified"] is False
    assert plan["frozen_gate"]["original_r130d_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert plan["frozen_gate"]["candidate_sloped_gaussian_numerical"]==(
        "EXPERIMENTAL_CANDIDATE_NUMERICAL_PASS_ONLY")
    assert plan["frozen_gate"]["production_enabled"] is False
    assert plan["drive"]["frequencies_hz"]==[40,80]
    assert [x["name"] for x in r["cases"]]==[
        "baseline_sloped","planar_wedge","three_axis_diagonal"]


@pytest.mark.parametrize("name,exact",[
    ("baseline_sloped",56.0),("planar_wedge",149/3),("three_axis_diagonal",160/3),
])
def test_exact_volume_conservative_stability_frozen_geometry(name,exact):
    r=read("r130d_convex_multiplane_cutcell_evidence_2026-10-09.json")
    room=next(x for x in r["cases"] if x["name"]==name)
    assert [x["n"] for x in room["levels"]]==[6,8,12,16,20]
    for level in room["levels"]:
        assert level["exact_cut_cell_volume_m3"]==pytest.approx(exact,rel=5e-12)
        assert level["relative_energy_drift_60_steps"]<1e-9
        assert level["null_max_normalized_by_diagonal"]<1e-11
        assert level["max_abs_stiffness_asymmetry"]<1e-8
        assert level["minimum_cut_volume_fraction"]>1e-12
        assert np.all(np.isfinite(_complex(level)))
        assert len(_complex(level))==2


def test_all_adjacent_complex_metrics_recomputed_without_bin_selection():
    r=read("r130d_convex_multiplane_cutcell_evidence_2026-10-09.json")
    for room in r["cases"]:
        assert len(room["adjacent_transfer"])==4
        for i,pair in enumerate(room["adjacent_transfer"]):
            a=_complex(room["levels"][i])
            b=_complex(room["levels"][i+1])
            assert pair["coarse_n"]==room["levels"][i]["n"]
            assert pair["fine_n"]==room["levels"][i+1]["n"]
            assert pair["complex_l2_relative"]==pytest.approx(
                np.linalg.norm(a-b)/np.linalg.norm(b),rel=1e-12)
            assert pair["magnitude_max_relative"]==pytest.approx(
                max(abs(abs(a)-abs(b))/abs(b)),rel=1e-12)
            assert pair["phase_max_deg"]==pytest.approx(
                max(abs(np.angle(a/b,deg=True))),abs=1e-11)
        assert all(x["complex_l2_relative"]>y["complex_l2_relative"]
                   for x,y in zip(room["adjacent_transfer"],room["adjacent_transfer"][1:]))
    # Phase convergence is NOT guaranteed in the first two wedge levels.
    wedge=r["cases"][1]["adjacent_transfer"]
    assert wedge[1]["phase_max_deg"]>wedge[0]["phase_max_deg"]
    assert r["new_geometries_cross_solver_qualified"] is False


def test_baseline_legacy_fv_transfer_unchanged_at_shared_levels():
    r=read("r130d_convex_multiplane_cutcell_evidence_2026-10-09.json")
    old=read("r130d_embedded_neumann_fv_driven_baseline_2026-10-09.json")
    by_n={x["grid_cells_per_axis"]:x for x in old["levels"]}
    for row in r["cases"][0]["levels"]:
        n=row["n"]
        assert row["legacy_sloped_stiffness_fro_relative"]<1e-12
        assert row["legacy_sloped_volume_max_abs_difference"]<1e-12
        if n not in by_n:
            continue
        a=_complex(row)
        b=np.array([complex(*v) for v in by_n[n]["transfer_complex_40_80_hz"]])
        np.testing.assert_allclose(a,b,atol=2e-7,rtol=2e-8)
