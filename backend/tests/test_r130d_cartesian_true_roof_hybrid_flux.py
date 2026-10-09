"""Independent frozen-experiment guardrails for Cartesian/true-roof hybrid."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy import linalg

from htdt.r130d_cartesian_true_roof_hybrid_flux import (
    build_native_cartesian_true_roof_hybrid_flux)
from htdt.r130d_native_cut_roof_Q1_galerkin import (
    cut_roof_native_original_Q1_galerkin_yz)

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_q0_cartesian_flux_true_roof_hybrid import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_cut_roof_hybrid_plan_2026-10-10.json"


def make(h):
    # Entire 4 m square includes exact clipped roof and original Q1 supports.
    a=np.arange(0.,4.+h/2.,h)
    return build_native_cartesian_true_roof_hybrid_flux(a,a)


def test_algebraic_full_cartesian_fivepoint_flux_no_diagonal_and_psd_penalty():
    c=343.2
    # Independent exact Q1 full element weak gradient and Cartesian axis flux:
    q1=np.array([[2/3,-1/6,-1/6,-1/3],
                 [-1/6,2/3,-1/3,-1/6],
                 [-1/6,-1/3,2/3,-1/6],
                 [-1/3,-1/6,-1/6,2/3]])
    s=np.array([1.,-1.,-1.,1.])
    expected=np.array([[1.,-.5,-.5,0.],
                       [-.5,1.,0.,-.5],
                       [-.5,0.,1.,-.5],
                       [0.,-.5,-.5,1.]])
    np.testing.assert_allclose(q1+np.outer(s,s)/3,expected,atol=1e-15)
    assert np.min(np.linalg.eigvalsh(np.outer(s,s)))>-1e-14
    assert np.allclose(expected@np.ones(4),0.)
    assert c>0


@pytest.mark.parametrize("h",[.5,.25,.2])
def test_exact_roof_affine_neumann_mass_zero_energy_and_all_positive_support(h):
    sys=make(h)
    q=sys.cut_q1
    u=q.physical_active_yz_node_positions_m[:,0]-.25*q.physical_active_yz_node_positions_m[:,1]
    wetsep=4.-.25*q.physical_active_yz_node_positions_m[:,0]-q.physical_active_yz_node_positions_m[:,1]
    coords=q.physical_active_yz_node_positions_m
    roof=(coords[:,0]>2*h)&(coords[:,0]<4-2*h)&(coords[:,1]>2*h)&(abs(wetsep)<2*h)
    assert np.any(roof)
    assert max(abs((sys.stiffness@u)[roof]))<1e-7
    assert max(abs(sys.stiffness@np.ones(sys.modes)))<5e-8
    assert abs(float(sys.mass.sum())-14)<2e-8
    assert sys.cartesian_full_rectangle_count>10
    assert sys.exact_roof_cut_rectangle_count>0
    assert sys.modes==q.native_yz_modes
    np.testing.assert_allclose(sys.mass.toarray(),q.mass.toarray(),rtol=0,atol=0)
    assert np.min(sys.mass.diagonal())>0
    assert np.min(np.linalg.eigvalsh(sys.stiffness.toarray()))>-3e-8


def test_undriven_midpoint_energy_no_unstable_nonlinear_modification():
    sys=make(.5)
    m=sys.mass.toarray()
    k=sys.stiffness.toarray()
    # Full consistent mass is SPD (the diagonal alone does not prove SPD).
    linalg.cholesky(m,lower=True,check_finite=True)
    rng=np.random.default_rng(938)
    u=rng.normal(size=sys.modes)
    v=rng.normal(size=sys.modes)
    dt=0.00002
    e=lambda a,b:float(.5*(a@(k@a)+b@(m@b)))
    before=e(u,v)
    next_u=linalg.solve(m+dt**2/4*k,(m-dt**2/4*k)@u+dt*m@v,assume_a="pos")
    next_v=2/dt*(next_u-u)-v
    assert abs(e(next_u,next_v)/before-1)<2e-11


@pytest.mark.parametrize("scope,value",[
    ("ppw",[28,32,36,40]),
    ("frequency_signed_hz",[40]),
    ("acceptance",{"complex_rms_relative_max":2.,
                   "magnitude_max_relative":.25,"phase_max_deg":15})])
def test_preregistered_original_unfiltered_q0_plan_is_immutable(scope,value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["original"][scope]=value
    with pytest.raises(ValueError,match="preregistered plan"):
        validate_plan(p)


def test_original_preregistered_plan_matches_fixed_axis_flux_coefficient():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["original"]["original_SHA_native_eight_source_receiver"]
    assert p["original"]["require_all_four_adjacent_pairs"]
    assert p["caps"]["run_original_PFFDTD"]==0
    assert p["release"]["product"]=="NO_GO"
    h=.5
    s=make(h)
    assert s.full_rectangle_coefficient_c_squared_over_three==(343.2**2/3.)
    assert s.cartesian_full_rectangle_count+s.exact_roof_cut_rectangle_count==s.cut_q1.polygon_intersecting_native_cells
