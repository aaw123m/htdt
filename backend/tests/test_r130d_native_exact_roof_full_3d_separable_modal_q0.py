"""No-truncation true 3D exact roof Neumann q0 modal forensic regression.

Never treat partial 384 modes, full experimental cutcell FV or high-mode
frequency band decomposition as original pinned PFFDTD qualification.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_native_grid_exact_roof_fv import build_native_exact_roof_fv
from htdt.r130d_native_exact_roof_separable import (
    original_native_xy_z_factorization,prove_native_full_kronecker_equal,
    generalized_neumann_modes)
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,finite_record_pressure_transfer)
from run_r130d_native_exact_roof_xy_z_separable_modal_q0 import (
    validate_plan as validate_partial_plan,unpairs)
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import (
    validate_plan,entire_original_finite_record_signed_modes,PPW,BANDS,FREQ)

PARTIAL_PLAN=ROOT/"benchmarks/acoustics/r130d_native_exact_roof_xy_z_separable_modal_q0_plan_2026-10-09.json"
PARTIAL_EVIDENCE=ROOT/"benchmarks/acoustics/r130d_native_exact_roof_xy_z_separable_modal_q0_evidence_2026-10-09.json"
FULL_PLAN=ROOT/"benchmarks/acoustics/r130d_native_exact_roof_full_xy_z_modal_finite_window_q0_plan_2026-10-09.json"
FULL_EVIDENCE=ROOT/"benchmarks/acoustics/r130d_native_exact_roof_full_xy_z_modal_q0_evidence_2026-10-09.json"

def frozen(partial=False):
    planpath=PARTIAL_PLAN if partial else FULL_PLAN
    evidencepath=PARTIAL_EVIDENCE if partial else FULL_EVIDENCE
    raw=planpath.read_bytes()
    plan=(validate_partial_plan if partial else validate_plan)(json.loads(raw.decode("utf-8")))
    data=json.loads(evidencepath.read_text(encoding="utf-8"))
    assert data["plan_sha256_lf"]==hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest()
    assert data["preregistered_plan"]==plan
    return plan,data

def test_exhaustive_two_mode_studies_prospectively_frozen_and_no_promotion():
    p,d=frozen()
    pp,dd=frozen(partial=True)
    assert p["pinned"]["native_PPW"]==list(PPW)==[40,44]
    assert p["pinned"]["source_xyz_m"]==[1.5,2,2]
    assert p["pinned"]["receiver_xyz_m"]==[2.5,2,2]
    assert p["pinned"]["frequency_bins_hz"]==[40,80]
    assert p["pinned"]["full_record_s"]==.25
    assert p["decomposition"]["semidiscrete_frequency_bands_hz"]==[list(v) for v in BANDS]
    assert pp["modes"]["yz_modes"]==32
    assert pp["modes"]["lowest_x_modes_contributing"]==12
    assert d["new_github_actions_runs"]==dd["new_GitHub_Actions_runs"]==0
    assert d["canonical_original_selfconvergence"]==dd["canonical_original_q0"]=="SELF_CONVERGENCE_FAILED"
    assert d["physical_validation"]==dd["physical_validation"]=="NOT_VALIDATED"
    assert d["product"]==dd["product"]=="NO_GO"
    assert d["higher_modes_not_truncated_from_final_250ms_q0_result"] is True
    assert d["original_full_canonical_q0_not_requalified"] is True

@pytest.mark.parametrize("key,value",[
    ("native_PPW",[40]),("source_xyz_m",[1.48,2,2]),
    ("receiver_xyz_m",[2.52,2,2]),("frequency_bins_hz",[30,60]),
    ("full_record_s",.1)])
def test_full_modal_preregistered_source_and_bins_fail_closed(key,value):
    p,_=frozen();p["pinned"][key]=value
    with pytest.raises(ValueError):validate_plan(p)

@pytest.mark.parametrize("mutation",[
    lambda p:p["numerical"].update(low_384_relative_max=1),
    lambda p:p["numerical"].update(full_exact_modal_vs_earlier_true_CG_wave_relative_max=1),
    lambda p:p["decomposition"].update(semidiscrete_frequency_bands_hz=[[0,80],[80,1000000000]]),
    lambda p:p["limits"].update(run_github_actions=True),
    lambda p:p["release"].update(product="GO")
])
def test_full_modal_preregistered_verification_or_release_cannot_be_mutated(mutation):
    p,_=frozen();mutation(p)
    with pytest.raises(ValueError):validate_plan(p)

@pytest.mark.parametrize("theta",[0.,1e-7,.01,.25,1.2,2.9,3.13])
def test_exact_original_q0_finite_dtft_closed_form_vs_independent_native_250ms_direct_samples(theta):
    nt=600
    dt=.0003
    lam=np.array([(2*np.tan(theta/2)/dt)**2])
    amp=np.array([.004])
    analytic=entire_original_finite_record_signed_modes(lam,amp,dt,nt,1.2)[:,0]
    n=np.arange(nt)
    direct_phi=amp[0]*(n if theta==0 else np.sin(n*theta)/np.sin(theta))
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        direct_phi,time_step_s=dt,density_kg_m3=1.2)
    source=np.zeros(nt);source[0]=1.
    true=finite_record_pressure_transfer(
        pressure,source,time_step_s=dt,frequency_hz=FREQ)
    np.testing.assert_allclose(analytic,true,rtol=1e-8,atol=2e-6)

def test_small_3D_original_roof_tensor_operator_exact_geometry_and_32_cross_section_modes():
    a=np.arange(-.45,4.56,.3)
    raw=build_native_exact_roof_fv((a,a,a))
    sep=original_native_xy_z_factorization((a,a,a))
    identity=prove_native_full_kronecker_equal(raw,sep)
    assert identity["ordered_original_native_indices_exact"] is True
    assert identity["3D_original_full_mass_Kronecker_max_absolute"]<=1e-10
    assert identity["3D_original_full_stiffness_Kronecker_max_absolute"]<=2e-8
    assert identity["native_full_active_nodes"]==raw.number_of_cells
    assert identity["exact_total_fluid_volume_m3"]==pytest.approx(56,abs=2e-8)
    xlam,xvec,xd=generalized_neumann_modes(sep.Mx,sep.Kx)
    yzlam,yzvec,yd=generalized_neumann_modes(sep.Myz,sep.Kyz,nmodes=12)
    assert xlam[0]==pytest.approx(0,abs=1e-6)
    assert yzlam[0]==pytest.approx(0,abs=1e-6)
    assert xd["highest_true_mass_normalized_eigen_residual"]<2e-7
    assert yd["highest_true_mass_normalized_eigen_residual"]<2e-7
    np.testing.assert_allclose(np.sqrt(xlam[1])/(2*np.pi),42.803,rtol=2e-4)

def test_all_actual_native_ppw40_44_3D_partial_384_modal_signed_complex_and_error():
    p,d=frozen(partial=True)
    rows=d["actual_original_3D_separable_native_cases"]
    assert [x["ppw"] for x in rows]==list(PPW)
    for r in rows:
        matrix=r["actual_exact_original_3D_Kronecker_matrix_identity"]
        assert matrix["ordered_original_native_indices_exact"] is True
        assert matrix["3D_original_full_mass_Kronecker_max_absolute"]<=p["exact_math"]["max_mass_abs"]
        assert matrix["3D_original_full_stiffness_Kronecker_max_absolute"]<=p["exact_math"]["max_stiffness_abs"]
        assert matrix["exact_total_fluid_volume_m3"]==pytest.approx(56,abs=2e-8)
        assert len(r["actual_original_q0_first_384_product_modes"])==384
        for operator in ("source","receiver"):
            assert r["original_source_receiver_8node_tensor_factorization"][operator]["tensor_separability_residual"]<1e-12
        z=sum((unpairs(v["signed_original_q0_P_T_over_Q_T_40_80"])
               for v in r["actual_original_q0_first_384_product_modes"]),np.zeros(2,dtype=complex))
        np.testing.assert_allclose(z,unpairs(
            r["finite_record_partial_time_transfer_integrity"]["original_250ms_modal_partial_signed_transfer_40_80"]),
            rtol=3e-10,atol=1e-7)
        assert r["remaining_high_mode_relative_to_full_40_80_L2"]>.2
        assert len(r["yz_native_roof_generalized_Neumann_modes"]["all_mode_eigenfrequency_hz"])==32
    assert d["original_full_3D_point_q0_unchanged_and_still_FAILED"] is True

def test_entire_original_ppw40_44_eigenspectrum_40_80_signed_bands_and_CG_match():
    p,d=frozen()
    rows=d["actual_untruncated_3D_native_eigenmode_cases"]
    partial=json.loads(PARTIAL_EVIDENCE.read_text(encoding="utf-8"))
    partrows={z["ppw"]:z for z in partial["actual_original_3D_separable_native_cases"]}
    assert [r["ppw"] for r in rows]==list(PPW)
    for r in rows:
        assert r["total_full_untruncated_3D_native_modes"]==r["full_x_eigenmode_count"]*r["full_yz_eigenmode_count"]
        assert r["total_full_untruncated_3D_native_modes"]==r["original_neumann_Kronecker_identity"]["native_full_active_nodes"]
        assert r["true_full_2D_yz_mode_numerics"]["highest_true_mass_normalized_eigen_residual"]<p["numerical"]["true_eigen_relative_residual_max"]
        assert r["true_full_2D_yz_mode_numerics"]["maximum_M_orthonormality_error"]<p["numerical"]["true_mass_orthonormality_max"]
        assert r["analytic_384_vs_previous_actual_temporal_integration_rel"]<p["numerical"]["low_384_relative_max"]
        assert r["all_mode_analytic_vs_archived_true_CG_full_wave_rel"]<p["numerical"]["full_exact_modal_vs_earlier_true_CG_wave_relative_max"]
        np.testing.assert_allclose(unpairs(r["analytic_first_384_modal_signed_250ms_transfer"]),
            unpairs(partrows[r["ppw"]]["finite_record_partial_time_transfer_integrity"]["original_250ms_modal_partial_signed_transfer_40_80"]),
            rtol=2e-9,atol=2e-6)
        full=unpairs(r["all_original_Neumann_3D_modes_full_signed_P_T_over_Q_T"])
        old=unpairs(r["archived_direct_full_wave_250ms_signed_40_80"])
        np.testing.assert_allclose(full,old,rtol=2e-5,atol=2e-5)
        bands=r["every_original_mode_in_one_signed_frequency_band"]
        assert [z["frequency_band_hz"] for z in bands]==[list(x) for x in BANDS]
        assert sum(z["included_native_3D_mode_count"] for z in bands)==r["total_full_untruncated_3D_native_modes"]
        np.testing.assert_allclose(sum((unpairs(z["signed_full_q0_pressure_transfer_40_80"])
             for z in bands),np.zeros(2,dtype=complex)),full,rtol=1e-9,atol=2e-6)
        for label in ("40","80"):
            assert len(r["top24_individual_signed_250ms_wave_modes_each_original_bin"][label])==24
        assert r["source_receiver_fixed_exact_xyz_and_q0"] is True
        assert r["original_full_waveform_temporal_q0_without_taper"] is True
        assert r["experiment_not_the_canonical_PFFDTD_source_solver"] is True
    separated=d["all_original_3D_modes_signed_PP40_minus_PP44_adjacent_fourier_difference_by_band"]
    assert [z["physical_semidiscrete_band_hz"] for z in separated]==[list(x) for x in BANDS]
    total=unpairs(rows[0]["all_original_Neumann_3D_modes_full_signed_P_T_over_Q_T"])-unpairs(rows[1]["all_original_Neumann_3D_modes_full_signed_P_T_over_Q_T"])
    np.testing.assert_allclose(sum((unpairs(z["signed_adjacent_PP40_minus_PP44_complex_40_80"])
                     for z in separated),np.zeros(2,dtype=complex)),total,rtol=1e-10,atol=3e-6)
    for z in separated:
        actual=float(np.linalg.norm(unpairs(z["signed_adjacent_PP40_minus_PP44_complex_40_80"]))/np.linalg.norm(total))
        assert z["two_bin_difference_norm_relative_to_complete_original"]==pytest.approx(actual,rel=1e-10)
    assert separated[-1]["two_bin_difference_norm_relative_to_complete_original"]>1.
    assert d["all_mode_reconstructed_original_PP40_44_exact_roof_full_8_8_complex_metric"]["complex_rms_relative"]>.8
