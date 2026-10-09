"""Preregistered physical one-sided Gaussian operator and original q0 integrity."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import numpy as np
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_one_sided_spatial_operator import (
    validate_plan,pinned_baselines,reference_case,compare_adjacent,
    WIDTHS,FV_NS,P2_RS,OPERATORS,POINT_SHA,BOTH_SHA,wave_plan)
from run_r130d_fixed_spatial_kernel import fv_functionals,impulse_transfer
from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann,interior_point_stencil)
PLAN=ROOT/"benchmarks/acoustics/r130d_one_sided_spatial_operator_plan_2026-10-09.json"

def frozen():
    return validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))

def test_frozen_plan_operator_cases_and_canonical_status():
    p=frozen()
    assert tuple(p["grid"]["fv_n"])==FV_NS
    assert tuple(p["grid"]["p2_mfem_refinement"])==P2_RS
    assert tuple(p["spatial_kernel"]["sigma_m"])==WIDTHS
    assert p["source_waveform"]["no_time_smoothing"] is True
    assert p["source_waveform"]["no_taper"] is True
    assert p["authority"]["production_ready"] is False
    assert p["authority"]["original_pffdtd_fullband"]=="SELF_CONVERGENCE_FAILED"
    assert len(OPERATORS)==2

@pytest.mark.parametrize("change",[
    lambda p:p["spatial_kernel"].update(sigma_m=[0.5]),
    lambda p:p["operator_cases"].pop(),
    lambda p:p["grid"].update(fv_n=[20]),
    lambda p:p["source_waveform"].update(q0=0.5),
    lambda p:p["source_waveform"].update(frequencies_hz=[40]),
    lambda p:p["source_waveform"].update(no_time_smoothing=False),
    lambda p:p["authority"].update(production_ready=True),
])
def test_changing_registered_operator_or_waveform_fails_closed(change):
    p=frozen();change(p)
    with pytest.raises(ValueError):validate_plan(p)

def test_control_evidence_is_exactly_pinned_and_canonical_point_not_promoted():
    p=frozen();a,b=pinned_baselines(p)
    for kind,level in (("fv",20),("fv",32),("mfem",3),("mfem",4)):
        control=reference_case(a,b,kind,level,0.7)
        assert len(control)==2
        assert len(control["point_source_point_receiver"])==2
        assert len(control["gaussian_source_gaussian_receiver"])==2
        assert np.all(np.isfinite(control["point_source_point_receiver"]))
    assert a["original_fullband_r130d"]=="SELF_CONVERGENCE_FAILED"
    assert b["production_ready"] is False

def test_fv_gaussian_point_are_distinct_and_conservative():
    p=frozen();sys=build_sloped_embedded_neumann(4)
    bp=interior_point_stencil(sys,tuple(p["geometry"]["source_xyz_m"]))
    rp=interior_point_stencil(sys,tuple(p["geometry"]["receiver_xyz_m"]))
    fg,_=fv_functionals(sys,{"geometry":p["geometry"],"spatial_kernel":{"sigma_m":list(WIDTHS)}})
    for bg,rg in fg:
        assert bg.shape==rp.shape==bp.shape==rg.shape
        np.testing.assert_allclose(np.sum(bg),1,atol=1e-12)
        np.testing.assert_allclose(np.sum(rg),1,atol=1e-12)
        assert np.linalg.norm(bg-bp)>0.05
        assert np.linalg.norm(rg-rp)>0.05
        for b,r in ((bg,rp),(bp,rg)):
            w=impulse_transfer(sys.mass,sys.stiffness,b,r,wave_plan(p),use_pcg=False)
            assert w["sampled_time_impulse_q0_only"] is True
            assert np.all(np.isfinite(w["complex_40_80_hz"]))


def test_exact_fv_x_reflection_maps_source_to_receiver_without_numerical_fitting():
    """Geometry and source/receiver centers are mirror pairs along x."""
    p=frozen()
    n=8
    sys=build_sloped_embedded_neumann(n)
    index={tuple(map(int,xyz)):z for z,xyz in enumerate(sys.cell_coordinates_ijk)}
    mirror=np.array([index[(n-1-int(i),int(j),int(k))]
        for i,j,k in sys.cell_coordinates_ijk])
    np.testing.assert_allclose(sys.mass.diagonal()[mirror],
                               sys.mass.diagonal(),rtol=0,atol=1e-12)
    np.testing.assert_allclose(sys.stiffness[mirror,:][:,mirror].toarray(),
                               sys.stiffness.toarray(),rtol=0,atol=1e-8)
    point_s=interior_point_stencil(sys,tuple(p["geometry"]["source_xyz_m"]))
    point_r=interior_point_stencil(sys,tuple(p["geometry"]["receiver_xyz_m"]))
    np.testing.assert_allclose(point_s[mirror],point_r,rtol=0,atol=1e-12)
    gauss,_=fv_functionals(sys,{"geometry":p["geometry"],
             "spatial_kernel":{"sigma_m":list(WIDTHS)}})
    for g_source,g_receiver in gauss:
        np.testing.assert_allclose(g_source[mirror],g_receiver,rtol=0,atol=1e-10)
