"""Fail-closed ORIGINAL native PFFDTD full-q0 spectral x numerical-scheme A/B."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend" / "src"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_original_native_modal_drift import unpairs, pairs
from run_r130d_original_q0_spectral_x_ab import (
    ARMS, continuum_neumann_point_x_coupling, validate_plan,
)

PLAN = ROOT / "benchmarks/acoustics/r130d_original_q0_spectral_x_dispersion_coupling_plan_2026-10-09.json"
EVIDENCE = ROOT / "benchmarks/acoustics/r130d_original_q0_spectral_x_dispersion_coupling_evidence_2026-10-09.json"
ORIGINAL = ROOT / "benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json"


def test_frozen_plan_and_unchanged_physical_points():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["input"]["ppw"]==[28,32,36,40,44]
    assert p["input"]["source_xyz_m"]==[1.5,2,2]
    assert p["input"]["receiver_xyz_m"]==[2.5,2,2]
    assert p["input"]["record_s"]==.25 and p["input"]["frequency_hz"]==[40,80]
    assert p["input"]["original_8_node_source_receiver_for_native_arms"]
    assert p["input"]["original_native_dt_nt_rho_c_per_ppw"]
    assert p["method"]["source_q0_initial_discrete_kick_frozen_in_all_arms"]
    assert p["limits"]["max_new_native_pffdtd_wave_runs"]==0


@pytest.mark.parametrize("field,value", [
    ("complex_limit",0.5),
    ("magnitude_limit",1.0),
    ("phase_deg_limit",180),
])
def test_cannot_relax_original_limits(field,value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["evaluation"][field]=value
    with pytest.raises(ValueError,match="plan changed"):
        validate_plan(p)


def test_continuous_point_neumann_x_coupling_zero_mode_and_fixed_points():
    for n in (32,44,47):
        x=continuum_neumann_point_x_coupling(n)
        assert np.isclose(x[0],1/n,atol=1e-15)
        assert len(x)==n and np.isfinite(x).all()
        m=np.arange(n)
        expectation=(2/n)*np.cos(np.pi*m*1.5/4)*np.cos(np.pi*m*2.5/4)
        expectation[0]=1/n
        assert np.max(abs(x-expectation))<1e-14
    with pytest.raises(ValueError):
        continuum_neumann_point_x_coupling(44,source_x=1.51)


def test_all_original_modes_native_waves_and_frozen_signed_failures():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    raw=PLAN.read_bytes()
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    original=json.loads(ORIGINAL.read_text(encoding="utf-8"))
    assert e["prospective_plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert e["preregistered_plan"]==p
    assert e["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["physical_validation"]=="NOT_VALIDATED" and e["product"]=="NO_GO"
    assert e["new_native_PFFDTD_wave_runs"]==e["new_GitHub_Actions_runs"]==0
    assert [x["ppw"] for x in e["cases"]]==[28,32,36,40,44]
    reference={x["ppw"]:x for x in original["actual_original_unmodified_all_mode_native_cases"]}
    for row in e["cases"]:
        assert row["all_original_3d_modes_retained"]
        assert row["experimental_arms_not_native_pffdtd"]
        assert row["native_original_full_mode_count"]==reference[row["ppw"]]["actual_native_full_3D_modes_count"]
        assert row["full_modes_against_native_saved_wave_relative"]<2e-6
        assert np.allclose(unpairs(row["original_native_q0_full_wave_signed_40_80"]),
                           unpairs(reference[row["ppw"]]["exact_original_saved_true_PFFDTD_q0_signed_40_80"]),
                           rtol=1e-10,atol=5e-7)
        assert abs(row["first_nonzero_corrected_x_Neumann_frequency_hz"]-42.9)<1e-10
        assert abs(row["x_original_source_receiver_constant_mode"]-
                   row["x_continuum_source_receiver_constant_mode"])<1e-12
        assert list(row["all_signed_q0_full_250ms_40_80_P_over_Q_by_arm"])==list(ARMS)
        for arm,transfer in row["all_signed_q0_full_250ms_40_80_P_over_Q_by_arm"].items():
            assert unpairs(transfer).shape==(2,)
            assert np.all(np.isfinite(unpairs(transfer)))
    assert [(x["coarse_ppw"],x["fine_ppw"]) for x in e["adjacent_ppw"]]==[
        (28,32),(32,36),(36,40),(40,44)]
    grid={x["ppw"]:x for x in e["cases"]}
    for pair in e["adjacent_ppw"]:
        assert set(pair["all_arms"])==set(ARMS)
        for arm,record in pair["all_arms"].items():
            coarse=grid[pair["coarse_ppw"]]["all_signed_q0_full_250ms_40_80_P_over_Q_by_arm"][arm]
            fine=grid[pair["fine_ppw"]]["all_signed_q0_full_250ms_40_80_P_over_Q_by_arm"][arm]
            delta=unpairs(coarse)-unpairs(fine)
            assert np.allclose(unpairs(record["signed_complex_coarse_minus_fine"]),delta,
                               rtol=1e-11,atol=1e-9)
            actual=compare_complex_transfer(reference=fine,candidate=coarse,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            assert np.isclose(actual["complex_rms_relative"],
                              record["metrics"]["complex_rms_relative"],atol=1e-12)
            pass_gate=(actual["complex_rms_relative"]<=.2
                and actual["magnitude_max_relative"]<=.25
                and actual["phase_max_deg"]<=15)
            assert pass_gate is record["frozen_three_limits_met"]
        assert not pair["all_arms"]["native_leapfrog"]["frozen_three_limits_met"]
