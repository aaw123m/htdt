"""Extreme sliver-cell energy stress and explicit experimental-only product seam."""
from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pytest

from htdt.r130d_embedded_neumann_convex import (
    ConvexPlanarRoom,build_convex_embedded_neumann,
)
from htdt.r130d_experimental_convex_obj_wave import (
    ExperimentalConvexWaveRequest,execute_experimental_convex_obj_wave,
    EXPERIMENTAL_ADAPTER_ID,SOURCE_ID,
)


ROOT=Path(__file__).resolve().parents[2]
OBJ=ROOT/"benchmarks"/"acoustics"/"r130d_convex_obj_fixtures"/"planar_wedge.obj"


@pytest.mark.parametrize("delta",[.01,.001,.0005,.0002])
def test_explicit_implicit_midpoint_energy_under_actual_tiny_slivers(delta):
    room=ConvexPlanarRoom(((1,1,1,4+delta*.5),))
    s=build_convex_embedded_neumann(8,geometry=room)
    assert s.cell_volumes_m3.min()/s.grid_spacing_m**3 < 2e-7
    p=np.cos(np.pi*s.cell_center_xyz_m[:,0]/4)
    v=np.zeros_like(p)
    initial=s.energy(p,v)
    stepper=s.midpoint_integrator(.02) # far above explicit sliver CFL
    largest=0.
    for _ in range(100):
        p,v=stepper.step(p,v)
        largest=max(largest,abs(s.energy(p,v)/initial-1))
    assert largest<1e-9


@pytest.mark.parametrize("delta",[1e-5,1e-6])
def test_unresolved_positive_volume_is_rejected_not_silently_discarded(delta):
    room=ConvexPlanarRoom(((1,1,1,4+delta*.5),))
    with pytest.raises(ValueError,match="no silent solidification"):
        build_convex_embedded_neumann(8,geometry=room)


@pytest.mark.parametrize("n",[0,4,21,100,True])
def test_optin_limits_and_deny_by_default(n):
    request=ExperimentalConvexWaveRequest(obj_path=OBJ,grid_cells_per_axis=n,
            explicit_experimental_opt_in=True)
    with pytest.raises(ValueError):
        execute_experimental_convex_obj_wave(request)


def test_default_and_unregistered_source_cannot_enter_experimental_solver():
    base=ExperimentalConvexWaveRequest(obj_path=OBJ,grid_cells_per_axis=8)
    with pytest.raises(PermissionError):
        execute_experimental_convex_obj_wave(base)
    for request in (
        replace(base,explicit_experimental_opt_in=True,
                source_authority="htdt.original-impulse-source"),
        replace(base,explicit_experimental_opt_in=True,
                output_authority="PRODUCTION"),
    ):
        with pytest.raises(PermissionError):
            execute_experimental_convex_obj_wave(request)


def test_explicit_candidate_entry_executes_original_gaussian_with_provenance():
    base=ExperimentalConvexWaveRequest(obj_path=OBJ,grid_cells_per_axis=8,
             explicit_experimental_opt_in=True)
    result=execute_experimental_convex_obj_wave(base)
    original=json.loads((ROOT/"benchmarks"/"acoustics"/
        "r130d_convex_multiplane_cutcell_evidence_2026-10-09.json").read_text())
    wedge=next(x for x in original["cases"] if x["name"]=="planar_wedge")
    expected=np.array([complex(*x) for x in wedge["levels"][1][
        "actual_driven_transfer_40_80_hz"]])
    actual=np.array([complex(*x) for x in result["wave_authority"]["complex_transfer"]])
    np.testing.assert_allclose(actual,expected,rtol=1e-8,atol=1e-6)
    assert result["mesh_authority"]["exact_polyhedron_volume_m3"]==pytest.approx(149/3)
    assert result["solver_adapter_id"]==EXPERIMENTAL_ADAPTER_ID
    assert result["wave_authority"]["source_id"]==SOURCE_ID
    assert result["experiment_opt_in_recorded"] is True
    assert result["production_dispatch_registered"] is False
    assert result["production_ready"] is False
    assert result["physical_validation"]=="NOT_VALIDATED"
    assert result["canonical_r130d_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert len(result["provenance_sha256"])==64
    assert execute_experimental_convex_obj_wave(base)["provenance_sha256"]==result["provenance_sha256"]


def test_extra_obj_comment_changes_provenance_not_physics(tmp_path):
    original=OBJ.read_text()
    p=tmp_path/"wedge.obj"
    p.write_text("# provenance-only external CAD comment\n"+original)
    req=ExperimentalConvexWaveRequest(obj_path=p,grid_cells_per_axis=8,
                                     explicit_experimental_opt_in=True)
    result=execute_experimental_convex_obj_wave(req)
    assert result["mesh_authority"]["source_sha256"]!=(
        "396f7f6e6119a7e7062f01c3af32827e0ecefb985f434c488363e6609cb5babb")
    assert result["production_ready"] is False


def test_optin_rejects_receiver_physically_outside_oblique_wall():
    req=ExperimentalConvexWaveRequest(
        obj_path=OBJ,grid_cells_per_axis=8,
        receiver_xyz_m=(3.2,3.2,1),explicit_experimental_opt_in=True,
    )
    with pytest.raises(ValueError,match="outside"):
        execute_experimental_convex_obj_wave(req)
