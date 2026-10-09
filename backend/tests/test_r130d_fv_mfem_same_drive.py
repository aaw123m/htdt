"""Failure-closed evidence tests for actual same-source independent MFEM/FV comparison."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))

from run_r130d_fv_mfem_same_drive_comparison import (
    metrics, validate_mfem_system,
)


def _json(name: str) -> dict:
    return json.loads((ROOT/"benchmarks"/"acoustics"/name).read_text(encoding="utf-8"))


def test_identical_physical_input_and_original_gates_remain_failed():
    r = _json("r130d_fv_mfem_same_drive_evidence_2026-10-09.json")
    p = _json("r130d_fv_mfem_same_drive_plan_2026-10-09.json")
    assert r["schema_version"] == "htdt.r130d.fv-mfem-same-drive-comparison-evidence-1"
    assert r["plan"] == p
    assert [x["refinement"] for x in r["mfem_levels"]]==[1,2,3]
    assert [x["dofs"] for x in r["mfem_levels"]]==[125,729,4913]
    assert all(x["actual_integrated_gaussian_source"] is True
               for x in r["mfem_levels"])
    assert [x["grid_cells_per_axis"] for x in r["fv_levels"]]==[20,24,28,32]
    assert r["gate"]["same_source_cross_solver_diagnostic_executed"] is True
    assert r["gate"]["canonical_r130d_self_convergence"] == "SELF_CONVERGENCE_FAILED"
    assert r["gate"]["original_mfem_impulse_self_convergence"] == "SELF_CONVERGENCE_FAILED"
    assert r["gate"]["production_ready"] is False
    assert r["gate"]["cross_solver_production_eligible"] is False


def test_mfem_and_fv_independent_adjacent_trajectories_are_recomputed():
    r = _json("r130d_fv_mfem_same_drive_evidence_2026-10-09.json")
    f = r["mfem_levels"]
    for a,b,actual in zip(f,f[1:],r["mfem_adjacent"]):
        result=metrics(a["transfer_complex_40_80_hz"],
                       b["transfer_complex_40_80_hz"])
        for k,v in result.items():
            assert actual[k]==pytest.approx(v,abs=1e-12)
    for a,b,actual in zip(r["fv_levels"],r["fv_levels"][1:],r["fv_adjacent"]):
        result=metrics(a["transfer_complex_40_80_hz"],
                       b["transfer_complex_40_80_hz"])
        for k,v in result.items():
            assert actual[k]==pytest.approx(v,abs=1e-12)
    for row,actual in zip(r["fv_levels"],r["cross_solver_vs_mfem_r3"]):
        result=metrics(row["transfer_complex_40_80_hz"],
                       f[-1]["transfer_complex_40_80_hz"])
        for k,v in result.items():
            assert actual[k]==pytest.approx(v,abs=1e-12)


def test_same_source_comparison_reduces_error_but_mfem_is_not_converged():
    r=_json("r130d_fv_mfem_same_drive_evidence_2026-10-09.json")
    errors=[x["normalized_complex_l2"] for x in r["cross_solver_vs_mfem_r3"]]
    assert errors==sorted(errors,reverse=True)
    assert errors[-1]<0.09
    assert r["mfem_adjacent"][-1]["normalized_complex_l2"]>0.18
    assert r["mfem_adjacent"][0]["normalized_complex_l2"]>0.80
    assert r["mfem_adjacent"][-1]["phase_difference_deg_by_hz"][1]>9


def test_metric_invalid_reference_and_phase_wrapping():
    with pytest.raises(ValueError):
        metrics([[1,0],[2,0]],[[0,0],[1,0]])
    with pytest.raises(ValueError):
        metrics([[1,0]],[[1,0]])
    m=metrics([[np.cos(np.deg2rad(179)),np.sin(np.deg2rad(179))],
               [1,0]],
              [[np.cos(np.deg2rad(-179)),np.sin(np.deg2rad(-179))],
               [1,0]])
    assert m["phase_difference_deg_by_hz"][0]==pytest.approx(2,abs=1e-12)


def test_independent_mfem_sparse_authority_mutation_is_rejected():
    p=_json("r130d_fv_mfem_same_drive_plan_2026-10-09.json")
    base={
        "schema_version":"htdt.r130d.mfem-sloped-system-1",
        "geometry":"exact-eight-vertex-sloped-polyhedron",
        "boundary_model":"natural_neumann_rigid",
        "primary_field":"velocity_potential_phi",
        "source_normalization":"volume_velocity_m3_s",
        "governing_equation":"M*phi_tt+Kc2*phi=c^2*b*q",
        "uniform_refinements":3,"ndofs":4913,"order":2,
        "base_volume_m3":56,"sound_speed_m_s":343.2,
        "density_kg_m3":1.2,"source_position_m":[1.5,2,2],
        "receiver_position_m":[2.5,2,2],
    }
    validate_mfem_system(base,refinement=3,ndofs=4913,plan=p)
    for k,bad in [
        ("boundary_model","absorbing"),
        ("order",1),
        ("source_position_m",[1.4,2,2]),
        ("base_volume_m3",55.0),
        ("governing_equation","different"),
        ("density_kg_m3",1.3),
    ]:
        doc=copy.deepcopy(base)
        doc[k]=bad
        with pytest.raises(ValueError):
            validate_mfem_system(doc,refinement=3,ndofs=4913,plan=p)


def test_committed_independent_mfem_sparse_matrices_are_byte_identical():
    from run_r130d_fv_mfem_same_drive_comparison import read_payload_and_hash
    plan = _json("r130d_fv_mfem_same_drive_plan_2026-10-09.json")
    folder = ROOT / "benchmarks" / "acoustics" / "r130d_mfem_independent_sparse_systems"
    for ref in (1, 2, 3):
        matrix = read_payload_and_hash(
            folder / f"mfem-r{ref}.json.gz",
            expected_sha256=plan["mfem_sparse_export_sha256_by_refinement"][str(ref)],
        )
        assert matrix["ndofs"] == plan["mfem_dofs"][ref-1]
        assert matrix["uniform_refinements"] == ref
        assert matrix["boundary_model"] == "natural_neumann_rigid"
        assert matrix["source_position_m"] == plan["source_xyz_m"]
        assert matrix["receiver_position_m"] == plan["receiver_xyz_m"]


def test_mfem_gzip_digest_mutation_is_rejected(tmp_path):
    from run_r130d_fv_mfem_same_drive_comparison import read_payload_and_hash
    file = tmp_path / "changed.json.gz"
    import gzip
    data = gzip.compress(b'{"ndofs": 4}', mtime=0)
    file.write_bytes(data)
    with pytest.raises(ValueError, match="digest mismatch"):
        read_payload_and_hash(file,expected_sha256="0"*64)
