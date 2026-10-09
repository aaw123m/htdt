"""Fail-closed 12/24/48 high-spatial-modal q0 impulse diagnostic."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_original_impulse_high_mode_count import (
    validate_plan,operator,EXPECTED_COUNTS,REFERENCE_SHA)
from run_r130d_impulse_modal_projection import simulate_modal
PLAN=ROOT/"benchmarks/acoustics/r130d_high_mode_count_original_impulse_plan_2026-10-09.json"
def frozen():
    return validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))

def test_precommitted_original_q0_and_12_24_48_cases():
    p=frozen()
    assert p["source"]["temporal"]=="q[0]=1 m3/s, q[n>0]=0"
    assert p["source"]["steps"]==1000
    assert tuple(p["mode_counts_including_zero"])==EXPECTED_COUNTS
    assert p["authority"]["production_ready"] is False
    assert p["full_reference"]["sha256"]==REFERENCE_SHA

@pytest.mark.parametrize("change",[
    lambda p:p["mode_counts_including_zero"].pop(),
    lambda p:p["source"].update(temporal="q[0]=q[1]=1"),
    lambda p:p["source"].update(frequencies_hz=[40]),
    lambda p:p["authority"].update(production_ready=True),
    lambda p:p["eigsolve"].update(true_generalized_relative_residual_max=1),
])
def test_no_after_observation_mode_or_source_changes(change):
    p=frozen();change(p)
    with pytest.raises(ValueError):validate_plan(p)

def test_same_point_operator_when_loading_fv():
    p=frozen()
    M,K,b,r,meta=operator(p["methods"][0],ROOT/"benchmarks/acoustics/r130d_mfem_independent_sparse_systems",p)
    assert meta["n"]==20
    assert meta["dofs"]==7200
    np.testing.assert_allclose(sum(b),1,atol=1e-12)
    np.testing.assert_allclose(sum(r),1,atol=1e-12)
    assert M.shape==K.shape==(7200,7200)

def test_modal_projection_rejects_missing_high_modes():
    modes=[{"mode":i,"lambda_radians2_s2":i*300.,
            "generalized_eigen_relative_residual":1e-10,
            "signed_source_receiver_modal_product":1./(i+1)}
           for i in range(24)]
    with pytest.raises(ValueError):
        simulate_modal({"c_m_s":343.2,"rho_kg_m3":1.2},modes[:23],
                       .00025,1000,required_modes=24)


def test_first_twelve_reconstruction_independently_reproduces_prior_modal_evidence():
    """Numerically cross-check first 12 P2/FV modes against pre-existing independent replay."""
    old=json.loads((ROOT/"benchmarks/acoustics/r130d_original_impulse_modal_projection_evidence_2026-10-09.json").read_text())
    new=json.loads((ROOT/"benchmarks/acoustics/r130d_high_mode_count_original_impulse_evidence_2026-10-09.json").read_text())
    assert len(new["cases"])==2
    for case in new["cases"]:
        original_method=("embedded_neumann_FV" if case["method"]=="FV_exact_cutcell"
                         else "independent_pinned_MFEM_P2")
        level=case.get("n",case.get("r"))
        comparison=next(x for x in old["cases"] if
                        x["method"]==original_method and x["level"]==level)
        assert case["truncations"][0]["modes_including_zero"]==12
        np.testing.assert_allclose(
            case["truncations"][0]["finite_time_original_q0_transfer_40_80_hz"],
            comparison["first_twelve_modal_original_impulse_transfer_40_80_hz"],
            rtol=2e-6,atol=1e-5)
        np.testing.assert_allclose(
            case["actual_full_state_original_q0_transfer_40_80_hz"],
            comparison["true_fullspace_original_impulse_transfer_40_80_hz"],
            rtol=0,atol=0)
