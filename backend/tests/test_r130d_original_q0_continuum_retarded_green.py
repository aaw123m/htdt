"""Independent causal 3D Green and exact original source/receiver point moments.

These are ANALYTIC reference tests, NOT retroactive native PFFDTD validation:
no source smoothing or high-mode filtering is applied to any original run.
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
from htdt.r130d_retarded_point_green import (
    C,RHO,FREQUENCIES_HZ,ROOM_WALLS,
    green_retarded_signed,original_8node_retarded_signed,
    true_wall_specular_reflection,physical_point_first_reflections)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs
from run_r130d_original_point_quadratic_pffdtd import PPW
from run_r130d_original_q0_continuum_retarded_green import validate_plan

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_continuum_retarded_green_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_continuum_retarded_green_evidence_2026-10-09.json"
RAW=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
MODAL=ROOT/"benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json"


@pytest.mark.parametrize("s,r",[
    ((1.5,2,2),(2.5,2,2)),
    ((.8,1.2,2.1),(2.4,2.2,1.5)),
    ((1.9,2.8,2.3),(.7,1.6,1.3))])
def test_causal_3d_retarded_G_exact_signed_frequency_and_reciprocity(s,r):
    s=np.array(s,dtype=float);r=np.array(r,dtype=float)
    distance=float(np.linalg.norm(s-r))
    frequencies=np.array(FREQUENCIES_HZ)
    theoretical=np.exp(2j*np.pi*frequencies*distance/C)/(4*np.pi*distance)
    observed=green_retarded_signed(s,r)
    np.testing.assert_allclose(observed,theoretical,rtol=1e-14)
    np.testing.assert_allclose(observed,green_retarded_signed(r,s),rtol=1e-14)
    shift=np.array([.4,-.1,.2])
    np.testing.assert_allclose(observed,green_retarded_signed(s+shift,r+shift))
    assert np.isfinite(observed).all()
    assert np.all(np.abs(observed)>0)


def test_time_domain_causal_gaussian_distribution_fixture_ONLY_not_user_q0():
    # Numerical integral of smooth compact-scale TEST FUNCTION against the
    # retarded delta distribution. This Gaussian approximates δ solely for
    # proving the analytical continuum Green formula; it is NOT used in the
    # original HDF5 q0 wave/assessment or physical intervention.
    s=np.array([1.5,2,2]);r=np.array([2.5,2,2])
    distance=np.linalg.norm(r-s)
    travel=distance/C
    sigma=2e-5
    t=np.linspace(travel-10*sigma,travel+10*sigma,6001)
    regularized=np.exp(-.5*((t-travel)/sigma)**2)/(np.sqrt(2*np.pi)*sigma)
    signed=np.array([np.trapz(
        regularized*np.exp(2j*np.pi*f*t),t)/(4*np.pi*distance)
        for f in FREQUENCIES_HZ])
    true=green_retarded_signed(s,r)
    # The exact frequency Gaussian error is exp(-.5(omega sigma)^2);
    # check it explicitly and divide it out, no q0 smoothing.
    factor=np.exp(-.5*(2*np.pi*np.array(FREQUENCIES_HZ)*sigma)**2)
    np.testing.assert_allclose(signed/factor,true,rtol=4e-8,atol=1e-11)


def test_all_six_reflected_true_physical_faces_and_first_arrival_are_causal():
    s=np.array([1.5,2,2]);r=np.array([2.5,2,2])
    model=physical_point_first_reflections(s,r)
    assert len(model["six_true_planar_neumann_wall_reflections"])==6
    assert 0<model["direct_original_physical_arrival_s"]<model["first_supported_reflection_s"]<.25
    assert abs(model["direct_original_physical_arrival_s"]-1/C)<1e-15
    one=[]
    for wall in ROOM_WALLS:
        v=true_wall_specular_reflection(s,r,wall)
        assert v["wall"]==wall.name
        assert v["reflecting_point_on_true_finite_room_wall"]
        n=np.asarray(wall.normal)
        foot=np.asarray(v["reflection_xyz_m"])
        image=np.asarray(v["source_image_xyz_m"])
        # Fermat/reflection image principle: equal optical paths.
        assert abs(np.linalg.norm(s-foot)+np.linalg.norm(r-foot)-
                   v["single_bounce_path_m"])<2e-12
        assert abs(np.linalg.norm(image-r)-
                   v["single_bounce_path_m"])<2e-12
        assert abs(np.dot(n,foot)-wall.plane_rhs_m)<2e-12
        assert 0<v["specular_fraction_from_image"]<1
        assert v["travel_time_s"]>1/C
        one.append(v["travel_time_s"])
    assert abs(model["first_supported_reflection_s"]-min(one))<1e-15
    assert model["first_supported_reflection_s"]-1/C>0


@pytest.mark.parametrize("bad,err",[
    ("orig_source","invalid original"),
    ("weight","original SHA-pinned native"),
    ("collocated","singular continuum point Green")])
def test_fail_closed_original_green_source_and_point_singularity(bad,err):
    x=np.linspace(-.05,.05,8)
    src=np.column_stack((1.5+x,np.full(8,2.),np.full(8,2.)))
    rec=np.column_stack((2.5+x,np.full(8,2.),np.full(8,2.)))
    w=np.ones(8)/8
    if bad=="orig_source":
        src[0,2]=10
        with pytest.raises(ValueError,match="original SHA-pinned native"):
            original_8node_retarded_signed(src,w,rec,w)
    elif bad=="weight":
        with pytest.raises(ValueError,match="original SHA-pinned native"):
            original_8node_retarded_signed(src,.5*w,rec,w)
    else:
        with pytest.raises(ValueError,match="singular continuum point Green"):
            green_retarded_signed(src[0],src[0])


@pytest.mark.parametrize("key,new_value",[
    ("original_full_signed_complex_gate",7.),
    ("original_full_magnitude_gate",8.),
    ("original_full_phase_deg_gate",180.)])
def test_canonical_original_room_three_gate_relaxations_are_denied(key,new_value):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["prospective_original_frozen"][key]=new_value
    with pytest.raises(ValueError,match="prospective retarded Green"):
        validate_plan(p)


def test_original_real_fivegrid_all64point_green_vs_entire250ms_signed_evidence():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    original=json.loads(RAW.read_text(encoding="utf-8"))
    modal=json.loads(MODAL.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["preregistered_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["independent_physical_validation"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_native_PFFDTD_wave_runs"]==e["new_GitHub_Actions_runs"]==0
    assert e["analytic_single_bounce_not_used_for_native_fullroom_convergence_acceptance"]
    assert e["all_existing_true_original_q0_unmasked_high_modes_retained"]
    cases=e["actual_original_8node_HDF5_5grid_analytic_direct_point_Green_cases"]
    actual={row["ppw"]:row for row in original["actual_native_wave_cases"]}
    known={row["ppw"]:row for row in modal["actual_original_unmodified_all_mode_native_cases"]}
    assert [x["ppw"] for x in cases]==list(PPW)
    image=e["unit_true_point_free_space_and_six_single_wall_image_geometry"]
    direct=green_retarded_signed((1.5,2,2),(2.5,2,2))
    np.testing.assert_allclose(unpairs(image["direct_G_signed"]),direct)
    assert abs(image["direct_original_physical_arrival_s"]-1/C)<1e-15
    assert image["first_supported_reflection_s"]>1/C
    for q in cases:
        k=q["ppw"]
        assert q["original_native_original_source_comms_HDF5_SHA256"]==actual[k]["original_native_comm_sha256"]
        assert q["original_native_original_room_voxel_HDF5_SHA256"]==actual[k]["original_solver_geometry_sha256"]
        assert q["original_native_original_250ms_real_wave_HDF5_SHA256"]==known[k]["original_native_sim_output_SHA256"]
        assert q["native_original_Nt"]==known[k]["original_record_samples"]
        assert abs(q["native_original_Ts_s"]-known[k]["original_native_time_step_s"])<1e-12
        assert abs(q["original_source_first_moment_error_m"])<3e-10
        assert abs(q["original_receiver_first_moment_error_m"])<3e-10
        assert q["original_physical_8node_first_moments_unchanged"]
        assert q["analytic_retarded_direct_only_is_not_full_250ms_room_response"]
        assert q["full_original_native_q0_and_all_higher_modes_untouched"]
        src=np.asarray(q["original_native_eight_source_xyz_m"])
        recv=np.asarray(q["original_native_eight_receiver_xyz_m"])
        sw=np.asarray(q["original_native_source_HDF5_normalized_eight_coefficients"])
        rw=np.asarray(q["original_native_receiver_HDF5_eight_coefficients"])
        recomputed=original_8node_retarded_signed(src,sw,recv,rw)
        expected=unpairs(q["exact_64_original_native_pairs_analytic_retarded_G_40_80"])
        np.testing.assert_allclose(recomputed["all_64_original_pairs_retarded_free_space_G_signed"],
                                   expected,rtol=1e-12,atol=1e-13)
        np.testing.assert_allclose(unpairs(q[
            "physical_continuum_single_true_Dirac_point_retarded_G_40_80"]),direct)
        assert abs(np.linalg.norm(expected-direct)/np.linalg.norm(direct)-
                   q["original_native_eightnode_analytic_retarded_vs_physical_point_complex_relative"])<1e-12
        assert set(q["original_eightnode_analytic_individual_finite_wall_first_reflections"])==set(
            wall.name for wall in ROOM_WALLS)
        for wall in ROOM_WALLS:
            qwall=q["original_eightnode_analytic_individual_finite_wall_first_reflections"][wall.name]
            computed=recomputed["original_8node_first_bounce_per_true_wall"][wall.name]
            assert qwall["original_native_64_source_receiver_pairs_specular_on_true_finite_face"]==computed[
                "original_native_64_source_receiver_pairs_specular_on_true_finite_face"]
            np.testing.assert_allclose(
                unpairs(qwall["physically_supported_first_bounce_G_signed"]),
                computed["physically_supported_first_bounce_G_signed"],atol=2e-14)
        np.testing.assert_allclose(
            unpairs(q["exact_original_full_250ms_PFFDTD_signed_room_P_over_Q_40_80"]),
            unpairs(actual[k]["unmodified_original_transfer_pa_per_m3_s"]))
        np.testing.assert_allclose(
            unpairs(q["real_original_allmode_native_full250ms_signed_P_over_Q_40_80"]),
            unpairs(known[k]["exact_original_saved_true_PFFDTD_q0_signed_40_80"]))
    results=e["native_original_5grid_4pair_scores_and_analytic_direct_8node_diagnostic"]
    assert [(r["coarse_ppw"],r["fine_ppw"]) for r in results]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,r in enumerate(results):
        for name,key in (
            ("original_true_FULL_250ms_room_q0_signed_original_frozen_scores",
             "exact_original_full_250ms_PFFDTD_signed_room_P_over_Q_40_80"),
            ("independent_analytic_free_space_eightnode_geometric_only_scores_NOT_250ms_room",
             "exact_64_original_native_pairs_analytic_retarded_G_40_80")):
            score=compare_complex_transfer(reference=cases[i+1][key],
                candidate=cases[i][key],frequency_hz=[40,80],
                magnitude_mask_relative_db=-50).model_dump(mode="json")
            for metric in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(score[metric]-r[name][metric])<1e-12
        s=r["original_true_FULL_250ms_room_q0_signed_original_frozen_scores"]
        passed=(s["complex_rms_relative"]<=.2 and s["magnitude_max_relative"]<=.25
                and s["phase_max_deg"]<=15)
        assert passed is r["original_true_room_original_all_3_gates_pass"]
        assert not passed
        assert r["physically_full_room_true_original_green_not_known_from_single_bounce"]
    assert not e["original_real_room_PFFDTD_all_four_pairs_three_gates_pass"]
