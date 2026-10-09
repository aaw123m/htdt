"""Independence, prospective limits, and fail-closed convex P2 MFEM gate."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_convex_independent_mfem_drive import evaluate,_complex


def load(name):
    return json.loads((ROOT/"benchmarks"/"acoustics"/name).read_text(encoding="utf-8"))


def _baseline():
    return (
        load("r130d_convex_independent_mfem_plan_2026-10-09.json"),
        load("r130d_convex_mfem_tetra_mesh_provenance_2026-10-09.json"),
        load("r130d_convex_multiplane_cutcell_evidence_2026-10-09.json"),
    )


def test_independent_tetra_sources_hashes_and_analytical_volumes_immutable():
    plan,meshes,_=_baseline()
    assert meshes["mfem_pin"]==plan["mfem_pin"]
    folder=ROOT/"benchmarks"/"acoustics"/"r130d_convex_mfem_tetra_sources"
    for row in plan["geometry_cases"]:
        m=meshes["rooms"][row["name"]]
        raw=(folder/m["path"]).read_bytes().replace(bytes([13,10]),bytes([10]))
        assert hashlib.sha256(raw).hexdigest()==m["sha256"]
        lines=raw.decode("ascii").splitlines()
        nv,nt=map(int,lines[0].split())
        assert nv==m["vertices"] and nt==m["tetrahedra"]
        assert nt<=plan["resource_limits"]["max_tet_mesh_initial_elements"]
        vertices=np.asarray([
          [float(x) for x in s.split()] for s in lines[1:nv+1]
        ])
        tetra=np.asarray([
          [int(x) for x in s.split()] for s in lines[nv+1:]
        ])
        assert tetra.shape==(nt,4)
        volumes=[]
        for t in tetra:
            p=vertices[t]
            jac=np.stack((p[1]-p[0],p[2]-p[0],p[3]-p[0]),axis=1)
            vol=np.linalg.det(jac)/6
            assert vol>0
            volumes.append(vol)
        assert sum(volumes)==pytest.approx(row["exact_volume_m3"],rel=1e-11)


def test_two_independent_fem_and_fv_transfer_field_names_not_confused():
    datum=[[10.,20.],[30.,40.]]
    a=_complex({"transfer_complex_40_80_hz":datum})
    b=_complex({"actual_driven_transfer_40_80_hz":datum})
    np.testing.assert_allclose(a,b)
    assert a.shape==(2,)
    with pytest.raises(ValueError):
        _complex({"transfer_complex_40_80_hz":[datum[0]]})


def synthetic_levels(plan,fv):
    out={}
    for room in plan["geometry_cases"]:
        row=next(x for x in fv["cases"] if x["name"]==room["name"])
        final=np.asarray([complex(*v) for v in row["levels"][-1][
                     "actual_driven_transfer_40_80_hz"]])
        # Smoothly reducing amplitude+phase errors; geometry/source/receiver
        # *do not* come from these fake values. This exercises gate logic only.
        factors=[1.6*np.exp(.36j),1.2*np.exp(.2j),
                 1.02*np.exp(.02j),1.0+0j]
        out[room["name"]]=[
          {"room":room["name"],"uniform_refinements":idx,
           "transfer_complex_40_80_hz":[
             [float(x.real),float(x.imag)] for x in factor*final]}
          for idx,factor in zip((1,2,3,4),factors)
        ]
    return out


def test_synthetic_consistent_high_order_fem_cannot_enable_production():
    plan,meshes,fv=_baseline()
    evidence=evaluate(plan,meshes,synthetic_levels(plan,fv),fv)
    assert len(evidence["cases"])==2
    assert all(row["experimental_numeric_candidate_pass"]
               for row in evidence["cases"])
    assert evidence["production_enabled"] is False
    assert evidence["original_r130d_impulse"]=="SELF_CONVERGENCE_FAILED"


def test_missing_ref4_or_relaxed_thresholds_fail_closed():
    plan,meshes,fv=_baseline()
    levels=synthetic_levels(plan,fv)
    levels["planar_wedge"]=levels["planar_wedge"][:-1]
    with pytest.raises(ValueError,match="missing"):
        evaluate(plan,meshes,levels,fv)
    plan,meshes,fv=_baseline()
    plan["unchanged_self_limits"]["phase_max_deg"]=179.
    with pytest.raises(ValueError,match="limits"):
        evaluate(plan,meshes,synthetic_levels(plan,fv),fv)


def test_interfering_shapes_both_40_80_bins_fail_closed():
    plan,meshes,fv=_baseline()
    levels=synthetic_levels(plan,fv)
    levels["three_axis_diagonal"][-1]["transfer_complex_40_80_hz"]=[[1.,0.]]
    with pytest.raises(ValueError):
        evaluate(plan,meshes,levels,fv)
