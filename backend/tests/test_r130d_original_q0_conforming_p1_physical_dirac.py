"""True variational P1 weak Dirac, exact affine moment, all raw original q0 grids."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from htdt.r130d_conforming_roof_p1_fem import build_original_native_conforming_roof_p1
from htdt.r130d_conforming_p1_physical_dirac import (
    physical_roof_P1_point_shape,
    physical_P1_point_tensor_modal_projection)
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import unpairs
from run_r130d_original_q0_conforming_p1_physical_dirac import (
    validate_plan,ARMS)
from run_r130d_original_point_quadratic_pffdtd import PPW

PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_p1_dirac_shape_source_receiver_plan_2026-10-09.json"
EVIDENCE=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_p1_physical_dirac_evidence_2026-10-09.json"
PREVIOUS=ROOT/"benchmarks/acoustics/r130d_original_q0_conforming_p1_consistent_mass_evidence_2026-10-09.json"
RAW=ROOT/"benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"


@pytest.mark.parametrize("point",[
    (1.5,2,2),(2.5,2,2),(.8,1.7,1.9),(4,0,3.6),
    (1.4,4,3),(2,0,0),(0,2.4,2.7)])
def test_true_P1_point_dirac_exact_weak_affine_and_partition(point):
    axes=np.arange(-.2,4.201,.2)
    fem=build_original_native_conforming_roof_p1([axes,axes,axes],
                                                  max_yz_nodes=1000,
                                                  max_3d_nodes=40000)
    source=physical_roof_P1_point_shape(fem,point)
    assert source.physical_xyz_m==point
    assert source.x_nnz<=2 and source.yz_nnz<=3
    assert source.support_native_3d_nodes<=6
    assert source.max_moment_error_m<2e-10
    assert source.partition_error<2e-12
    assert np.min(source.x_weights)>=-1e-12
    assert np.min(source.yz_weights)>=-1e-12
    v=np.array(point)
    a=np.array([.6,-.7,1.13])
    # True P1 shape evaluation is EXACT action of point Dirac on
    # affine FE function f=.25+.6*x-.7*y+1.13*z.
    ux=.6*fem.x_positions_m+.25
    uy=-.7*fem.yz_positions_m[:,0]+1.13*fem.yz_positions_m[:,1]
    actual=source.x_weights@ux+source.yz_weights@uy
    expected=.25+float(np.dot(a,v))
    assert abs(actual-expected)<2e-10
    # The source's global weak load sums to exactly 1 (no mass fitting).
    assert abs(float(sum(source.x_weights)*sum(source.yz_weights))-1)<2e-12


def test_real_original_physical_dirac_point_outside_roof_rejected():
    axes=np.arange(-.2,4.201,.2)
    fem=build_original_native_conforming_roof_p1([axes,axes,axes],
                                                  max_yz_nodes=1000,
                                                  max_3d_nodes=40000)
    for pos in ((2,2,3.6),(-.1,2,2),(4.01,1,1),(0,4.1,1),
                (2,2,float("nan"))):
        with pytest.raises(ValueError,match="physical Dirac point moved"):
            physical_roof_P1_point_shape(fem,pos)
    with pytest.raises(ValueError,match="complete original-grid"):
        physical_P1_point_tensor_modal_projection(
            fem,physical_roof_P1_point_shape(fem,(1.5,2,2)),
            np.eye(3),np.eye(4))


@pytest.mark.parametrize("name",[
    "canonical_original_three_gates_complex","magnitude","phase_deg"])
def test_not_allowed_to_relax_original_real_frozen_score(name):
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    p["verification"][name]=900.
    with pytest.raises(ValueError,match="preregistration drift"):
        validate_plan(p)


def test_true_P1_dirac_original_source_control_precommitted_invariant():
    p=validate_plan(json.loads(PLAN.read_text(encoding="utf-8")))
    assert p["frozen_original"]["ppw"]==list(PPW)
    assert p["frozen_original"]["original_source_xyz_m"]==[1.5,2,2]
    assert p["frozen_original"]["original_receiver_xyz_m"]==[2.5,2,2]
    assert p["frozen_original"]["record_s"]==.25
    assert p["frozen_original"]["signed_hz"]==[40,80]
    assert p["frozen_original"]["true_original_native_8point_in_sigs_and_out_alpha_preserved_and_SHA_verified"]
    assert p["limits"]["new_native_pffdtd_wave_runs"]==0
    assert p["limits"]["new_github_actions"]==0
    assert p["authority"]["original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"


def test_true_P1_weak_source_and_point_receiver_full_original_q0_fivegrid_FAIL():
    p=json.loads(PLAN.read_text(encoding="utf-8"))
    e=json.loads(EVIDENCE.read_text(encoding="utf-8"))
    earlier=json.loads(PREVIOUS.read_text(encoding="utf-8"))
    original=json.loads(RAW.read_text(encoding="utf-8"))
    assert e["preregistered_plan"]==p
    assert e["preregistered_plan_sha256_lf"]==hashlib.sha256(
        PLAN.read_bytes().replace(b"\r\n",b"\n")).hexdigest()
    assert e["canonical_original_PFFDTD_q0"]=="SELF_CONVERGENCE_FAILED"
    assert e["external_independent_physical"]=="NOT_VALIDATED"
    assert e["product"]=="NO_GO"
    assert e["new_original_upstream_PFFDTD_waves"]==0
    assert e["new_GitHub_Actions_runs"]==0
    assert e["point_shape_is_experimental_not_original_eightnode_observation"]
    assert e["no_qualified_original_or_product_PFFDTD"]
    old={x["ppw"]:x for x in earlier["actual_conforming_P1_consistent_mass_original_q0_cases"]}
    raw={x["ppw"]:x for x in original["actual_native_wave_cases"]}
    rows=e["actual_original_input_full_5grid_P1_exact_weak_dirac_cases"]
    assert [r["ppw"] for r in rows]==list(PPW)
    assert sum(r["true_physical_P1_all_mass_modes_count"] for r in rows)==401630
    for row in rows:
        k=row["ppw"]
        assert row["native_original_q0_source_hdf5_SHA256"]==raw[k]["original_native_comm_sha256"]
        assert row["native_original_voxel_hdf5_SHA256"]==raw[k]["original_solver_geometry_sha256"]
        assert row["native_Ts_s"]==old[k]["native_Ts_s"]
        assert row["native_Nt"]==old[k]["native_Nt"]
        assert row["true_physical_P1_all_mass_modes_count"]==old[k][
            "all_true_nonfiltered_consistent_FEM_3D_modes"]
        assert abs(row["true_physical_3D_exact_roof_volume_m3"]-56)<2e-8
        assert row["true_physical_P1_yz_triangles"]==old[k]["physical_true_FEM_yz_triangles"]
        assert row["native_rigid_Neumann_physical_zero_x_yz"]
        assert row["all_actual_physical_FEM_modes_retained_including_high_freq"]
        assert row["same_real_native_q0_250ms_40_80_unchanged"]
        assert row["original_PFFDTD_eightnode_vs_preobserved_signed_relative"]<2e-10
        np.testing.assert_allclose(
            unpairs(row["original_8node_consistent_mass_250ms_signed_40_80"]),
            unpairs(old[k]["new_full_consistent_P1_250ms_original_q0_signed_40_80"]),
            rtol=2e-10,atol=1e-8)
        for field,xyz in (
            ("new_P1_weak_dirac_shape_source",[1.5,2,2]),
            ("new_P1_weak_dirac_point_receiver",[2.5,2,2])):
            source=row[field]
            assert source["point_xyz_m"]==xyz
            assert source["original_physical_point_unchanged"]
            assert source["true_P1_x_nonzero_support"]<=2
            assert source["true_P1_yz_nonzero_support"]<=3
            assert source["true_P1_support_3d_vertices"]<=6
            assert source["exact_dirac_affine_coordinate_error_m"]<2e-10
            assert source["P1_partition_error"]<2e-12
        assert np.isfinite(unpairs(row[
            "true_P1_dirac_weak_source_point_receiver_250ms_signed_40_80"])).all()
        new=unpairs(row["true_P1_dirac_weak_source_point_receiver_250ms_signed_40_80"])
        ref=unpairs(row["original_8node_consistent_mass_250ms_signed_40_80"])
        np.testing.assert_allclose(
            unpairs(row["physical_point_dirac_minus_original_8node_signed_40_80"]),
            new-ref,rtol=1e-10,atol=1e-8)
    compare=e["all_four_complete_native_refinement_pairs"]
    assert [(row["coarse_ppw"],row["fine_ppw"]) for row in compare]==[
        (28,32),(32,36),(36,40),(40,44)]
    for i,r in enumerate(compare):
        assert set(r["arms"])==set(ARMS)
        for arm,key in (
            ("preobserved_original_eightnode_consistent_mass_P1",
             "original_8node_consistent_mass_250ms_signed_40_80"),
            ("true_physical_P1_weak_dirac_source_point_receiver",
             "true_P1_dirac_weak_source_point_receiver_250ms_signed_40_80")):
            c=rows[i][key];f=rows[i+1][key]
            s=compare_complex_transfer(reference=f,candidate=c,
                frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")
            check=r["arms"][arm]
            for k in ("complex_rms_relative","magnitude_max_relative","phase_max_deg"):
                assert abs(s[k]-check["unchanged_original_three_metrics_and_per_bin"][k])<1e-10
            np.testing.assert_allclose(
                unpairs(check["signed_original_40_80_coarse_minus_fine"]),
                unpairs(c)-unpairs(f),atol=1e-8)
            flag=(s["complex_rms_relative"]<=.2 and s["magnitude_max_relative"]<=.25
                  and s["phase_max_deg"]<=15)
            assert flag is check["all_three_original_frozen_gates_pass"]
        assert not r["arms"]["true_physical_P1_weak_dirac_source_point_receiver"][
            "all_three_original_frozen_gates_pass"]
    for verdict in e["entire_original_native_5grid_three_gate_convergence_verdicts"].values():
        assert not verdict["all_four_original_three_gate_pass"]
        assert not verdict["three_metric_strict_monotone"]
        assert verdict["no_original_native_PFFDTD_requalification"]
