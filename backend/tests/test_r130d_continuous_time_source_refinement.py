"""Frozen continuous forcing / pure temporal midpoint convergence controls."""
import copy
import json
from pathlib import Path
import sys
import numpy as np
import pytest
from scipy import sparse

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_continuous_time_source_refinement import (
    validate,sampled_input,actually_integrate,DTS
)
PLAN=ROOT/"benchmarks/acoustics/r130d_continuous_time_source_refinement_plan_2026-10-09.json"
def plan():
    return validate(json.loads(PLAN.read_text(encoding="utf-8")))

def test_frozen_physical_continuous_input_and_no_product_authority():
    p=plan()
    assert p["spatial_fixed"]["fv_n"]==20
    assert p["spatial_fixed"]["mfem_P2_r"]==2
    assert p["authority"]["original_fullband_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert p["authority"]["product_ready"] is False
    assert p["source"]["not_original_discrete_impulse"] is True

@pytest.mark.parametrize("change",[
    lambda p:p["source"].update(center_s=0.03),
    lambda p:p["source"].update(sigma_s=0.002),
    lambda p:p["dt_levels_s"].pop(),
    lambda p:p["authority"].update(product_ready=True),
    lambda p:p["metrics"].update(diagnostic_order_ratio_upper_bound=1.5),
])
def test_no_posthoc_continuous_time_or_threshold_adjustment(change):
    p=plan();change(p)
    with pytest.raises(ValueError):validate(p)

def test_exact_analytic_midpoint_samples_and_constant_physical_center():
    p=plan()
    area=[]
    for dt in DTS:
        t,q=sampled_input(p,dt)
        assert t.shape==q.shape==(round(0.25/dt),)
        assert np.all(q>=0)
        assert np.all(np.isfinite(q))
        assert 0.039<t[np.argmax(q)]<0.041
        np.testing.assert_allclose(
            q,np.exp(-0.5*((t-0.04)/0.004)**2),rtol=1e-12)
        area.append(dt*sum(q))
        transform=np.exp(2j*np.pi*np.array([40,80])[:,None]*t[None,:])
        assert np.min(abs(dt*(transform@q)))>1e-7
    assert max(area)-min(area)<1e-9

def test_one_dof_midpoint_energy_and_finite_fourier():
    p=plan()
    m=sparse.eye(1,format="csr")
    k=sparse.csr_matrix((1,1))
    w=np.ones(1)
    out=actually_integrate(m,k,w,w,p,DTS[0],fem=False)
    assert out["sampled_original_q0_impulse"] is False
    assert out["samples"]==1000
    assert len(out["complex_40_80_hz"])==2
    assert np.all(np.isfinite(out["complex_40_80_hz"]))
    assert out["true_relative_linear_residual_max"]<=1e-8
