"""True independent 3D Neumann FV/FEM modal eigenpairs, never impulse PASS."""
from __future__ import annotations
from pathlib import Path
import json
import math
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_sloped_neumann_modal_spectrum import analyze,load_plan


def evidence():
    d=ROOT/"benchmarks"/"acoustics"
    plan=load_plan(d/"r130d_sloped_neumann_modal_spectrum_plan_2026-10-09.json")
    e=json.loads((d/"r130d_sloped_neumann_modal_spectrum_evidence_2026-10-09.json").read_text(encoding="utf-8"))
    return plan,e


def test_precisely_six_independent_original_rigid_room_modal_grids():
    plan,e=evidence()
    assert e["preregistered_plan"]==plan
    assert e["schema_version"]=="htdt.r130d.sloped-neumann-modal-spectral-evidence-1"
    assert [(x["method"],x.get("n",x.get("r"))) for x in e["cases"]]==[
       ("embedded_neumann_FV",12),
       ("embedded_neumann_FV",20),
       ("embedded_neumann_FV",32),
       ("independent_pinned_MFEM_P2",2),
       ("independent_pinned_MFEM_P2",3),
       ("independent_pinned_MFEM_P2",4),
    ]
    assert e["original_R130D_impulse"]=="SELF_CONVERGENCE_FAILED"
    assert e["physical_validation"]=="NOT_VALIDATED"
    assert e["production_ready"] is False
    assert e["not_fullband_impulse_time_transfer"] is True
    assert e["not_causal_proof"] is True


@pytest.mark.parametrize("i",range(6))
def test_mass_orthonormalized_true_Neumann_modes_and_residuals(i):
    plan,e=evidence()
    row=e["cases"][i]
    modes=row["modes"]
    assert len(modes)==12
    assert row["degrees_of_freedom"]<=plan["limits"]["max_dofs"]
    assert row["time_seconds"]<plan["limits"]["max_per_case_wall_seconds"]
    assert [x["mode"] for x in modes]==list(range(12))
    f=np.array([x["natural_frequency_hz"] for x in modes])
    assert np.all(np.isfinite(f))
    assert f[0]<plan["operators"]["zero_mode_frequency_hz_max"]
    assert np.all(f[1:]>0)
    assert np.all(np.diff(f)>=0)
    assert max(x["generalized_eigen_relative_residual"] for x in modes)<1e-7
    for m in modes:
        assert m["mass_normalization"]==pytest.approx(1.0,abs=1e-7)
        assert m["natural_frequency_hz"]==pytest.approx(
            math.sqrt(max(m["lambda_radians2_s2"],0))/(2*math.pi),
            abs=1e-9)
        assert m["signed_source_receiver_modal_product"]==pytest.approx(
            m["source_modal_weight"]*m["receiver_modal_weight"],rel=1e-12,abs=1e-12)
    assert row["closest_mode_to_40hz"]==1
    assert row["closest_mode_to_80hz"]==7
    assert 41<f[1]<43
    assert 78<f[7]<80
    assert 84<f[8]<87
    if row["method"]=="independent_pinned_MFEM_P2":
        assert row["original_pinned_system_sha256"]==plan["mfem"]["system_sha256_by_ref"][str(row["r"])]
        assert row["independent_P2_matrix_reused"] is True
    else:
        assert row["fluid_volume_m3"]==pytest.approx(56,abs=1e-10)
        assert row["independent_P2_matrix_reused"] is False


def test_strict_FV_and_independent_P2_ordered_low_mode_refinement_drift():
    plan,e=evidence()
    check=analyze(plan,e["cases"])
    assert check["original_R130D_impulse"]=="SELF_CONVERGENCE_FAILED"
    for x,y in zip(check["adjacent_modal_frequency_drift"],e["adjacent_modal_frequency_drift"]):
        assert x["method"]==y["method"]
        assert x["coarse_grid"]==y["coarse_grid"]
        assert x["fine_grid"]==y["fine_grid"]
        assert x["largest_relative_ordered_mode_frequency_difference"]==pytest.approx(
            y["largest_relative_ordered_mode_frequency_difference"],rel=1e-10,abs=1e-12)
        np.testing.assert_allclose(
            x["per_mode_relative_frequency_difference"],
            y["per_mode_relative_frequency_difference"],
            rtol=1e-10,atol=1e-12,
        )
    assert len(check["adjacent_modal_frequency_drift"])==4
    by=check["adjacent_modal_frequency_drift"]
    assert by[1]["largest_relative_ordered_mode_frequency_difference"]<.003
    assert by[3]["largest_relative_ordered_mode_frequency_difference"]<.0005


def test_low_frequency_modes_converged_but_original_unit_impulse_DID_NOT():
    _,modes=evidence()
    old=json.loads((ROOT/"benchmarks"/"acoustics"/
         "r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json"
    ).read_text(encoding="utf-8"))
    assert old["numerical_diagnostic"]["exact_unit_impulse_candidate_numerical_pass"] is False
    assert old["numerical_diagnostic"]["fv_self_pass"] is False
    assert old["numerical_diagnostic"]["mfem_self_pass"] is False
    # Strong partial finding: 11 low eigenfrequencies converge well, but
    # full-band sampled point impulse P_T/Q_T does not. That contradicts
    # simplistic low-mode-eigenvalue-only causal explanations.
    assert modes["adjacent_modal_frequency_drift"][3][
         "largest_relative_ordered_mode_frequency_difference"]<.0005
    assert old["numerical_diagnostic"]["mfem_adjacent"][-1][
         "normalized_complex_l2"]>.26


def test_tampered_source_freq_or_product_authority_rejected(tmp_path):
    plan,_=evidence()
    for mutation in (
        lambda d:d["operators"].update({"smallest_modes_including_zero":8}),
        lambda d:d["operators"].update({"compare_bins_hz":[42,80]}),
        lambda d:d["geometry"].update({"sound_speed_m_s":355.}),
        lambda d:d["mfem"].update({"pinned_commit":"latest"}),
        lambda d:d["interpretation"].update({"production_NO_GO":False}),
    ):
        data=json.loads(json.dumps(plan))
        mutation(data)
        file=tmp_path/"tampered.json"
        file.write_text(json.dumps(data),encoding="utf-8")
        with pytest.raises(ValueError):
            load_plan(file)
