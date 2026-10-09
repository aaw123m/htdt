"""Immutable canonical PFFDTD run76 8-node source/receiver moment analysis."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_pffdtd_trilinear_effective_width import (
    SOURCE_SHA,validate_plan,operator_width,analyze)
PLAN=ROOT/"benchmarks/acoustics/r130d_pffdtd_trilinear_effective_width_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_pffdtd_trilinear_effective_width_evidence_2026-10-09.json"

def frozen():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    d=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    return p,d

def test_preregistered_original_ppw8_10_12_and_canonical_no_go():
    p,d=frozen()
    assert d["preregistered_plan"]==p
    assert d["original_run76_sha256"]==SOURCE_SHA
    assert d["plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert d["canonical_pffdtd_fullband"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]=="NOT_VALIDATED"
    assert d["production_ready"] is False
    assert d["changed_solver_or_original_stencils"] is False
    assert [row["ppw"] for row in d["levels"]]==[8,10,12]

@pytest.mark.parametrize("change",[
    lambda x:x["grid"].update(ppw=[8,12]),
    lambda x:x["grid"].update(source_xyz_m=[2,2,2]),
    lambda x:x["grid"].update(fixed_reference_gaussian_sigma_m=[0.35]),
    lambda x:x["limits"].update(max_levels=4),
    lambda x:x["authority"].update(production_ready=True),
])
def test_original_width_plan_changes_fail_closed(change):
    p,_=frozen()
    change(p)
    with pytest.raises(ValueError):validate_plan(p)

def test_native_trilinear_corner_mass_fails_closed_if_changed():
    p,_=frozen()
    raw=(ROOT/p["immutable_source"]["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==SOURCE_SHA
    original=json.loads(raw)
    item=copy.deepcopy(original["interpolation_stencil"]["levels"][0]["source"])
    h=original["spatial_representation"]["levels"][0]["grid_spacing_m"]
    item["interpolation_weights"][0]+=0.01
    with pytest.raises(ValueError):
        operator_width(item,h,p["grid"]["source_xyz_m"])

def test_all_original_widths_match_independent_archived_data():
    p,d=frozen()
    original=json.loads((ROOT/p["immutable_source"]["path"]).read_text())
    recalculated=analyze(p,original)
    for key in ("levels","source_trend","receiver_trend"):
        assert d[key]==recalculated[key]
    assert d["source_trend"]["strictly_decreasing"] is False
    assert d["receiver_trend"]["strictly_decreasing"] is True
    source=d["source_trend"]["values_m"]
    assert source[0]<source[1] and source[2]<source[1]
