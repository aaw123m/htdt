"""Independent recomputation of actual scientific evidence, no mocked runs."""
from pathlib import Path
import json
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "benchmarks/acoustics/r130d_physical_pulse_sem_evidence_2026-10-10.json"


def z(values):
    a = np.asarray(values,float)
    return a[:,0]+1j*a[:,1]


def independent_scores(coarse, fine):
    c, f = z(coarse),z(fine)
    return [np.linalg.norm(c-f)/np.linalg.norm(f),
            np.max(abs(abs(c)-abs(f))/abs(f)),
            np.max(abs(np.angle(c*np.conj(f),deg=True)))]


def test_all_five_physical_grids_and_all_three_metrics_converge():
    doc = json.loads(EVIDENCE.read_text(encoding="utf8"))
    rows = doc["sem_cases"]
    assert [r["ppw"] for r in rows] == [28,32,36,40,44]
    score = np.array([independent_scores(a["actual_midpoint_signed_40_80"],b["actual_midpoint_signed_40_80"])
                      for a,b in zip(rows,rows[1:])])
    assert np.all(score <= [.2,.25,15])
    assert np.all(np.diff(score,axis=0) < 0)
    for r in rows:
        assert abs(r["volume_m3"]-56.) < 1e-9
        assert r["all_3d_modes"] == r["x_dofs"]*r["yz_dofs"]
        assert abs(r["steps"]*r["dt_s"]-.25) < 1e-14
        assert r["minimum_positive_yz_mass"] > 0
    assert doc["numerical_model_qualification"] == "PASS_FINITE_BAND_R130D"
    assert doc["original_point_q0"] == "SELF_CONVERGENCE_FAILED"
    assert doc["physical_measured_room_validation"] == "NOT_VALIDATED"


def test_actual_independent_mfem_cross_method_and_temporal_order():
    doc = json.loads(EVIDENCE.read_text(encoding="utf8"))
    rows = doc["mfem_cases"]
    assert [r["dofs"] for r in rows] == [729,4913,35937]
    score = np.array([independent_scores(a["signed_40_80"],b["signed_40_80"])
                      for a,b in zip(rows,rows[1:])])
    assert np.all(score[1] < score[0])
    assert np.all(score[-1] <= [.05,.08,5])
    for r in rows:
        assert r["max_true_residual"] <= 1e-8
        assert r["max_cg_iterations"] <= 350
        assert len(r["system_sha256"]) == 64
    time = doc["fixed_spatial_time_refinement"]
    same = next(r for r in time if r["steps"] == rows[-1]["steps"])
    assert np.all(np.array(independent_scores(same["signed_40_80"],rows[-1]["signed_40_80"])) <= [.05,.08,5])
    fine = doc["sem_cases"][-1]["exact_causal_signed_40_80"]
    errors = [independent_scores(r["signed_40_80"],fine)[0] for r in time]
    assert min(np.log2(np.array(errors[:-1])/errors[1:])) > 1.8
    assert errors[-1] < .01


def test_gaussian_tail_is_physical_drive_and_original_q0_not_relabelled():
    doc = json.loads(EVIDENCE.read_text(encoding="utf8"))
    plan = doc["preregistered_plan"]
    assert plan["source"] == {"model":"causal Gaussian volume velocity", "center_s":.04,"sigma_s":.004,"amplitude_m3_s":1}
    assert plan["source_xyz_m"] == [1.5,2,2]
    assert plan["receiver_xyz_m"] == [2.5,2,2]
    assert plan["all_modes"] is True
    assert doc["new_original_PFFDTD_runs"] == doc["new_github_actions"] == 0
