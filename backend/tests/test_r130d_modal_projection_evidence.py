"""True physical original impulse 12-mode projection evidence and full-basis identity."""
from __future__ import annotations
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy.linalg import eigh

from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann,
    discrete_source_complex_transfer,
    interior_point_stencil,
)

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_impulse_modal_projection import evaluate,simulate_modal


def load(name):
    return json.loads((ROOT/"benchmarks"/"acoustics"/name).read_text(encoding="utf-8"))


def sources():
    return (
       load("r130d_original_impulse_12mode_projection_plan_2026-10-09.json"),
       load("r130d_sloped_neumann_modal_spectrum_evidence_2026-10-09.json"),
       load("r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json"),
       load("r130d_original_impulse_modal_projection_evidence_2026-10-09.json")
    )


def test_original_fullband_impulse_did_not_acquire_modal_false_pass():
    plan,modes,full,e=sources()
    assert e["plan"]==plan
    assert e["schema_version"]=="htdt.r130d.original-impulse-low-modal-projection-diagnostic-1"
    assert e["same_original_unsmoothed_discrete_impulse"] is True
    assert e["low_modal_truncation_is_NOT_a_production_solver"] is True
    assert e["low_mode_spectrum_not_a_causal_proof_of_highband_failure"] is True
    assert e["canonical_fullband_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert e["production_ready"] is False
    assert e["physical_measurements"]=="NOT_VALIDATED"
    assert full["numerical_diagnostic"]["exact_unit_impulse_candidate_numerical_pass"] is False


def test_all_six_physical_bases_and_original_two_frequency_samples_recomputed():
    plan,modes,full,e=sources()
    actual=evaluate(plan,modes,full)
    assert [(x["method"],x["level"]) for x in actual["cases"]]==[
       ("embedded_neumann_FV",12),
       ("embedded_neumann_FV",20),
       ("embedded_neumann_FV",32),
       ("independent_pinned_MFEM_P2",2),
       ("independent_pinned_MFEM_P2",3),
       ("independent_pinned_MFEM_P2",4)
    ]
    for a,b in zip(actual["cases"],e["cases"]):
        assert a["number_of_reconstructed_modes_including_zero"]==12
        assert a["dofs_full"]==b["dofs_full"]
        for fld in (
           "first_twelve_modal_original_impulse_transfer_40_80_hz",
           "true_fullspace_original_impulse_transfer_40_80_hz",
        ):
            np.testing.assert_allclose(a[fld],b[fld],rtol=2e-10,atol=2e-9)
        assert a["fullspace_vs_12mode_relative_error"]["normalized_complex_l2"]==pytest.approx(
          b["fullspace_vs_12mode_relative_error"]["normalized_complex_l2"],rel=2e-11,abs=2e-11)
    for a,b in zip(actual["modal_vs_full_grid_pairs"],e["modal_vs_full_grid_pairs"]):
        assert a["method"]==b["method"]
        assert a["12modal_adjacent"]["normalized_complex_l2"]==pytest.approx(
            b["12modal_adjacent"]["normalized_complex_l2"],rel=2e-11,abs=2e-11)
    assert e["modal_vs_full_grid_pairs"][1]["12modal_adjacent"]["normalized_complex_l2"]<.02
    assert e["modal_vs_full_grid_pairs"][1]["fullspace_adjacent"]["normalized_complex_l2"]>.26


@pytest.mark.parametrize("n",[3,4])
def test_all_modes_equal_direct_full_cut_cell_wave_at_true_original_impulse(n):
    """Independent dense generalized eigendecomposition validates modal ODE.

    This is an actual full-wave check: with ALL spatial modes restored,
    modal propagated q[0]=1 must equal the separate direct sparse midpoint
    solver to tight floating precision. The published 12-mode truncation
    intentionally omits high modes at larger n.
    """
    op=build_sloped_embedded_neumann(n)
    ev,V=eigh(op.stiffness.toarray(),op.mass.toarray())
    b=interior_point_stencil(op,(1.5,2,2))
    r=interior_point_stencil(op,(2.5,2,2))
    full_modes=[]
    for i,(lam,vec) in enumerate(zip(ev,V.T)):
        assert lam>-1e-6
        err=np.linalg.norm(op.stiffness@vec-lam*(op.mass@vec))
        assert err<1e-6
        full_modes.append({
           "mode":i,
           "lambda_radians2_s2":float(max(lam,0)),
           "signed_source_receiver_modal_product":float((b@vec)*(r@vec)),
           "generalized_eigen_relative_residual":0.
        })
    q=np.zeros(1000);q[0]=1
    direct=discrete_source_complex_transfer(op,q)
    reduced=simulate_modal(
         {"c_m_s":343.2,"rho_kg_m3":1.2},
         full_modes,.00025,1000,required_modes=len(full_modes))
    rebuilt=np.array([complex(*x) for x in reduced])
    np.testing.assert_allclose(rebuilt,direct,rtol=2e-6,atol=2e-5)


def test_precommitted_original_unit_impulse_and_modal_modes_required():
    p,m,full,e=sources()
    corrupted=json.loads(json.dumps(p))
    corrupted["source"]["q"]="gaussian"
    with pytest.raises(ValueError):
        evaluate(corrupted,m,full)
    bad=json.loads(json.dumps(m))
    bad["cases"].pop()
    with pytest.raises(ValueError):
        evaluate(p,bad,full)
    corrupted=json.loads(json.dumps(m))
    corrupted["cases"][0]["modes"].pop()
    with pytest.raises(ValueError):
        evaluate(p,corrupted,full)
