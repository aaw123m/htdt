from __future__ import annotations

import argparse
import json
import math
import os
from hashlib import sha256
from pathlib import Path
import platform as platform_module
import subprocess
import threading
import time

import numpy as np
import psutil
import scipy
from scipy import sparse
from scipy.sparse import linalg as sparse_linalg

from htdt.acoustic_bakeoff import BakeoffFixtureEvidence, BakeoffPlatform, load_bakeoff_adoption_profile, load_bakeoff_candidate_manifest
from htdt.acoustic_bakeoff_mfem_spatial_experiment import (
    ADAPTER_ID, ADAPTER_VERSION, CANDIDATE_ID, FIXTURE_ID, SpatialLevelResult,
    build_typed_record, candidate, evaluate_spatial_transfers, fixture,
    load_spatial_refinement_plan, rectangular_box_geometry, scored_frequency_grid,
    semantic_hash, validate_current_fixture_contract, validate_exact_authority_binding,
    validate_level_order, validate_semidiscrete_metadata, validate_typed_record_binding,
)
from htdt.acoustic_bakeoff_readiness import BakeoffReadinessEvidenceLedger, build_production_adoption_readiness_report, load_readiness_evidence_ledger
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest

REPORT_SCHEMA = "r100b-mfem-spatial-refinement-report-1"
TRANSFER_SCHEMA = "r100b-mfem-spatial-transfer-1"

class PeakMonitor:
    def __init__(self, pid: int | None = None):
        self.pid = pid or os.getpid()
        self.stop_event = threading.Event()
        self.peak = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
    def _run(self):
        try:
            root = psutil.Process(self.pid)
        except psutil.Error:
            return
        while not self.stop_event.wait(0.01):
            total = 0
            items = [root]
            try: items += root.children(recursive=True)
            except psutil.Error: pass
            for item in items:
                try: total += item.memory_info().rss
                except psutil.Error: pass
            self.peak = max(self.peak, total)
    def start(self): self.thread.start()
    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
        return self.peak / (1024.0 * 1024.0)

def file_sha256(path: Path) -> str:
    h = sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024): h.update(chunk)
    return h.hexdigest()

def directory_mb(path: Path) -> float:
    if not path.exists(): return 0.0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / (1024.0 * 1024.0)

def git_head(root: Path) -> str:
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip().lower()

def platform_evidence(thread_budget: int) -> BakeoffPlatform:
    logical = os.cpu_count() or 1
    return BakeoffPlatform(
        os=platform_module.platform(), architecture=platform_module.machine() or "unknown",
        python_version=platform_module.python_version(),
        cpu=platform_module.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
        logical_threads=logical, thread_budget=min(thread_budget, logical), gpu=None,
        device_notes="GitHub-hosted Windows MFEM v4.10 serial H1 p2 spatial h-refinement / sparse GL2 experiment.",
    )

def csr(payload: dict, ndofs: int, name: str):
    rows, cols, nnz = int(payload["rows"]), int(payload["cols"]), int(payload["nnz"])
    indptr = np.asarray(payload["row_offsets"], dtype=np.int64)
    indices = np.asarray(payload["column_indices"], dtype=np.int64)
    values = np.asarray(payload["values"], dtype=np.float64)
    if rows != ndofs or cols != ndofs or indptr.shape != (ndofs + 1,) or indices.shape != (nnz,) or values.shape != (nnz,):
        raise ValueError(f"{name} CSR dimensions are inconsistent")
    if indptr[0] != 0 or indptr[-1] != nnz or np.any(np.diff(indptr) < 0) or np.any(indices < 0) or np.any(indices >= ndofs) or not np.all(np.isfinite(values)):
        raise ValueError(f"{name} CSR data are invalid")
    out = sparse.csr_matrix((values, indices, indptr), shape=(ndofs, ndofs), dtype=np.float64)
    out.sum_duplicates(); out.sort_indices()
    diff = out - out.T
    sym = 0.0 if diff.nnz == 0 else float(np.max(np.abs(diff.data)))
    if sym > (1e-12 if name == "mass_matrix" else 1e-8):
        raise ValueError(f"{name} is not symmetric: {sym}")
    return out

def factorize(matrix):
    return sparse_linalg.splu(
        matrix.tocsc(), permc_spec="COLAMD", diag_pivot_thresh=1.0,
        options={"Equil": True, "IterRefine": "DOUBLE"},
    )

def sparse_hash(matrix) -> str:
    m = matrix.tocsr(copy=True); m.sum_duplicates(); m.sort_indices()
    h = sha256()
    h.update(np.asarray(m.shape, dtype=np.int64).tobytes())
    h.update(m.indptr.astype(np.int64, copy=False).tobytes())
    h.update(m.indices.astype(np.int64, copy=False).tobytes())
    h.update(m.data.astype(np.float64, copy=False).tobytes())
    return h.hexdigest()

def build_pade_blocks(mass, stiffness, dt):
    a0 = mass - (dt * dt / 12.0) * stiffness
    den = sparse.bmat([[a0, (-0.5 * dt) * mass], [(0.5 * dt) * stiffness, a0]], format="csc", dtype=np.float64)
    num = sparse.bmat([[a0, (0.5 * dt) * mass], [(-0.5 * dt) * stiffness, a0]], format="csr", dtype=np.float64)
    return den, num

def direct_transfer(pressures: np.ndarray, dt: float, frequencies: tuple[float, ...], source_amplitude: float):
    n = np.arange(pressures.size, dtype=np.float64)
    q = dt * source_amplitude
    if not math.isfinite(q) or q == 0.0: raise ValueError("source spectrum is not finite/nonzero")
    values = []
    for frequency in frequencies:
        kernel = np.exp(1j * (2.0 * math.pi * frequency * dt) * n)
        p = dt * np.dot(pressures, kernel)
        values.append(complex(p / q))
    return values, abs(q)

def assemble_system(executable: Path, fx, refinement: int, out: Path, timeout_s: float):
    origin, dims = rectangular_box_geometry(fx)
    src, rx = fx.sources[0].position, fx.receivers[0].position
    cmd = [
        str(executable), "--origin-x", str(origin[0]), "--origin-y", str(origin[1]), "--origin-z", str(origin[2]),
        "--lx", str(dims[0]), "--ly", str(dims[1]), "--lz", str(dims[2]),
        "--density", str(fx.environment.density_kg_m3), "--sound-speed", str(fx.environment.sound_speed_m_s),
        "--source-x", str(src.x_m), "--source-y", str(src.y_m), "--source-z", str(src.z_m),
        "--receiver-x", str(rx.x_m), "--receiver-y", str(rx.y_m), "--receiver-z", str(rx.z_m),
        "--order", "2", "--uniform-refinements", str(refinement), "--output", str(out),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    monitor = PeakMonitor(proc.pid); monitor.start()
    try: stdout, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill(); stdout, _ = proc.communicate()
        raise RuntimeError(f"MFEM assembly timed out: {stdout[-2000:]}")
    peak = monitor.stop()
    if proc.returncode != 0: raise RuntimeError(f"MFEM assembly exited {proc.returncode}: {stdout[-4000:]}")
    if not out.is_file(): raise RuntimeError("MFEM assembly produced no semidiscrete JSON")
    return peak, stdout[-4000:]

def run_level(plan, fx, executable: Path, work_dir: Path, refinement: int):
    level_dir = work_dir / f"h{refinement}"; level_dir.mkdir(parents=True, exist_ok=True)
    system_path = level_dir / "semidiscrete_system.json"
    child_peak, stdout_tail = assemble_system(executable, fx, refinement, system_path, float(plan.resource_ceiling["subprocess_wall_timeout_s"]))
    data = json.loads(system_path.read_text(encoding="utf-8"))
    validate_semidiscrete_metadata(plan, fx, data, refinement)
    ndofs = int(data["ndofs"])
    mass = csr(data["mass_matrix"], ndofs, "mass_matrix")
    stiffness = csr(data["stiffness_c2_matrix"], ndofs, "stiffness_c2_matrix")
    source = np.asarray(data["source_functional"], dtype=np.float64)
    receiver = np.asarray(data["receiver_functional"], dtype=np.float64)
    if source.shape != (ndofs,) or receiver.shape != (ndofs,) or not np.all(np.isfinite(source)) or not np.all(np.isfinite(receiver)):
        raise ValueError("MFEM source/receiver functionals are invalid")
    deterministic_system = {k: v for k, v in data.items() if k != "assembly_s"}
    system_identity = semantic_hash(deterministic_system)

    if scipy.__version__ != plan.temporal_integrator["scipy_version"] or np.__version__ != plan.temporal_integrator["numpy_version"]:
        raise RuntimeError(f"frozen numerical runtime mismatch: scipy={scipy.__version__}, numpy={np.__version__}")

    process_monitor = PeakMonitor(); process_monitor.start()
    mass_started = time.perf_counter(); mass_lu = factorize(mass); mass_factor_s = time.perf_counter() - mass_started
    output_rate = int(plan.temporal_integrator["output_sample_rate_hz"])
    substeps = int(plan.temporal_integrator["substeps_per_output_interval"])
    output_dt = 1.0 / output_rate; internal_dt = output_dt / substeps
    count = int(round(float(fx.comparison.observation_time_s) * output_rate))
    expected_steps = (count - 1) * substeps
    c = float(fx.environment.sound_speed_m_s); rho = float(fx.environment.density_kg_m3)
    amp = float(fx.sources[0].amplitude)
    rhs0 = (c * c * output_dt * amp) * source
    v0 = mass_lu.solve(rhs0)
    mass_residual = float(np.linalg.norm(mass @ v0 - rhs0) / max(float(np.linalg.norm(rhs0)), 1e-30))
    tol = float(plan.temporal_integrator["residual_relative_tolerance"])
    if not math.isfinite(mass_residual) or mass_residual > tol:
        raise RuntimeError(f"mass solve residual {mass_residual} exceeds frozen tolerance")

    den, num = build_pade_blocks(mass, stiffness, internal_dt)
    factor_started = time.perf_counter(); step_lu = factorize(den); pade_factor_s = time.perf_counter() - factor_started
    state = np.zeros(2 * ndofs, dtype=np.float64); state[ndofs:] = v0
    pressures = np.empty(count, dtype=np.float64)
    steps = 0; checked = 0; max_residual = 0.0
    check_interval = int(plan.temporal_integrator["residual_check_interval_steps"])
    step_started = time.perf_counter()
    for index in range(count):
        pressures[index] = rho * float(receiver @ state[ndofs:])
        if index + 1 == count: break
        for _ in range(substeps):
            rhs = num @ state
            nxt = step_lu.solve(rhs)
            if not np.all(np.isfinite(nxt)): raise RuntimeError(f"non-finite GL2 state at internal step {steps}")
            steps += 1
            if steps % check_interval == 0 or steps == expected_steps:
                residual = den @ nxt - rhs
                relative = float(np.linalg.norm(residual) / max(float(np.linalg.norm(rhs)), 1e-30))
                if not math.isfinite(relative) or relative > tol:
                    raise RuntimeError(f"GL2 residual {relative} exceeds frozen tolerance at step {steps}")
                max_residual = max(max_residual, relative); checked += 1
            state = nxt
    stepping_s = time.perf_counter() - step_started
    if steps != expected_steps: raise RuntimeError(f"exact four-substep count mismatch: {steps} != {expected_steps}")

    post_started = time.perf_counter()
    frequencies = scored_frequency_grid(fx)
    transfer, source_min = direct_transfer(pressures, output_dt, frequencies, amp)
    post_s = time.perf_counter() - post_started
    python_peak = process_monitor.stop()
    transfer_identity = semantic_hash({
        "system_identity": system_identity, "temporal_hash": plan.temporal_hash(), "refinement": refinement,
        "frequencies_hz": list(frequencies), "transfer": [[z.real, z.imag] for z in transfer],
    })
    transfer_path = level_dir / "transfer.json"
    transfer_path.write_text(json.dumps({
        "schema_version": TRANSFER_SCHEMA, "fixture_id": FIXTURE_ID, "uniform_refinement": refinement,
        "h1_order": 2, "output_sample_rate_hz": output_rate, "substeps_per_output_interval": substeps,
        "internal_rate_hz": output_rate * substeps, "internal_step_count": steps,
        "record_interval": "[0,T)", "observation_time_s": float(fx.comparison.observation_time_s),
        "frequencies_hz": list(frequencies), "transfer_real": [z.real for z in transfer],
        "transfer_imag": [z.imag for z in transfer], "source_spectrum_min_abs": source_min,
        "pressure_record_sha256": sha256(np.asarray(pressures, dtype="<f8").tobytes()).hexdigest(),
        "semidiscrete_identity_sha256": system_identity, "transfer_identity_sha256": transfer_identity,
        "mass_solve_relative_residual": mass_residual, "max_checked_step_relative_residual": max_residual,
        "checked_step_residual_count": checked,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    total_solve = mass_factor_s + pade_factor_s + stepping_s
    peak = max(child_peak, python_peak)
    work_mb = directory_mb(level_dir); output_mb = transfer_path.stat().st_size / (1024.0 * 1024.0)
    status, code, reason = "COMPLETED", "completed", "predeclared spatial level completed"
    if total_solve > float(plan.resource_ceiling["max_solve_s_per_refinement"]):
        status, code, reason = "RESOURCE_BLOCKED", "solve_resource_ceiling", f"solve {total_solve} s exceeds frozen ceiling"
    elif peak > float(plan.resource_ceiling["ram_budget_mb"]):
        status, code, reason = "RESOURCE_BLOCKED", "ram_resource_ceiling", f"peak RSS {peak} MiB exceeds frozen ceiling"
    elif work_mb > float(plan.resource_ceiling["disk_budget_mb"]):
        status, code, reason = "RESOURCE_BLOCKED", "disk_resource_ceiling", f"work disk {work_mb} MiB exceeds frozen ceiling"
    elif output_mb > float(plan.resource_ceiling["max_output_mb_per_refinement"]):
        status, code, reason = "RESOURCE_BLOCKED", "output_resource_ceiling", f"transfer output {output_mb} MiB exceeds frozen ceiling"

    result = SpatialLevelResult(
        refinement=refinement, status=status, reason_code=code, reason=reason,
        element_count=int(data["elements"]), dof_count=ndofs, mass_nnz=int(mass.nnz), stiffness_nnz=int(stiffness.nnz),
        assembly_s=float(data["assembly_s"]), mass_factorization_s=mass_factor_s, pade_factorization_s=pade_factor_s,
        stepping_s=stepping_s, total_solve_s=total_solve, peak_rss_mb=peak, work_disk_mb=work_mb,
        output_disk_mb=output_mb, internal_step_count=steps, source_spectrum_min_abs=source_min,
        mass_solve_relative_residual=mass_residual, max_checked_step_relative_residual=max_residual,
        checked_step_residual_count=checked, semidiscrete_identity_sha256=system_identity,
        transfer_identity_sha256=transfer_identity,
    )
    detail = {
        "system_path": str(system_path), "system_file_sha256": file_sha256(system_path),
        "transfer_path": str(transfer_path), "transfer_file_sha256": file_sha256(transfer_path),
        "stdout_tail": stdout_tail, "postprocess_s": post_s,
        "mass_matrix_numeric_sha256": sparse_hash(mass), "stiffness_matrix_numeric_sha256": sparse_hash(stiffness),
        "pade_denominator_numeric_sha256": sparse_hash(den), "pade_denominator_nnz": int(den.nnz),
        "pade_lu_nnz": int(step_lu.L.nnz + step_lu.U.nnz),
    }
    return result, transfer, detail

def candidate_report(report, candidate_id):
    return next(x for x in report.candidates if x.candidate_id == candidate_id)

def parser():
    p = argparse.ArgumentParser()
    for flag in ("manifest", "candidates", "plan", "adoption-profile", "readiness-ledger", "mfem-root", "work-dir", "output", "readiness-record-output", "provenance-output"):
        p.add_argument("--" + flag, required=True, type=Path)
    p.add_argument("--executable", type=Path)
    p.add_argument("--native-build-s", type=float, default=0.0)
    p.add_argument("--blocked-reason")
    return p

def main(argv=None):
    args = parser().parse_args(argv)
    plan = load_spatial_refinement_plan(args.plan)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    profile = load_bakeoff_adoption_profile(args.adoption_profile)
    ledger = load_readiness_evidence_ledger(args.readiness_ledger)
    validate_exact_authority_binding(plan, benchmark, candidates)
    fx = fixture(benchmark); validate_current_fixture_contract(plan, fx)
    c = candidate(candidates)
    if git_head(args.mfem_root) != c.source_commit_sha:
        raise SystemExit("MFEM checkout does not match frozen candidate source commit")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    run_id = os.environ.get("GITHUB_RUN_ID", "manual")
    platform = platform_evidence(fx.resource_budget.cpu_thread_budget)
    evidence_ref = f"artifact:r100b-mfem-spatial-refinement-{run_id}/report.json"
    level_results, transfers, details = [], {}, {}
    total_post = 0.0

    if args.blocked_reason:
        for r in (0, 1, 2):
            level_results.append(SpatialLevelResult(refinement=r, status="BLOCKED", reason_code="setup_blocked", reason=args.blocked_reason))
    else:
        if args.executable is None or not args.executable.is_file(): raise SystemExit("--executable is required for numerical run")
        for r in (0, 1, 2):
            try:
                result, transfer, detail = run_level(plan, fx, args.executable, args.work_dir, r)
                level_results.append(result); transfers[r] = transfer; details[f"h{r}"] = detail; total_post += float(detail["postprocess_s"])
            except Exception as exc:
                level_results.append(SpatialLevelResult(refinement=r, status="BLOCKED", reason_code="execution_error", reason=f"{type(exc).__name__}: {exc}"))
                for rr in range(r + 1, 3):
                    level_results.append(SpatialLevelResult(refinement=rr, status="BLOCKED", reason_code="prior_level_blocked", reason=f"h{r} execution blocked; no result-driven retry permitted"))
                break
    validate_level_order(plan, level_results)

    observable = None; adjacent = (); against_fine = ()
    if set(transfers) == {0, 1, 2}:
        observable, adjacent, against_fine = evaluate_spatial_transfers(fx, scored_frequency_grid(fx), transfers)
    if against_fine:
        spatial_status = "PASS" if (against_fine[0].complex_rms_absolute > against_fine[1].complex_rms_absolute and against_fine[0].complex_rms_relative > against_fine[1].complex_rms_relative) else "FAIL"
        tol = fx.observables[0].tolerance
        tolerance_status = "PASS" if ((tol.absolute is None or against_fine[1].complex_rms_absolute <= tol.absolute) and (tol.relative is None or against_fine[1].complex_rms_relative <= tol.relative)) else "FAIL"
    else:
        spatial_status = tolerance_status = "BLOCKED"

    aggregate_solve = sum(x.total_solve_s or 0.0 for x in level_results)
    peak_rss = max((x.peak_rss_mb or 0.0) for x in level_results)
    disk_mb = directory_mb(args.work_dir)
    output_mb = sum(x.output_disk_mb or 0.0 for x in level_results)
    any_level_blocked = any(x.status != "COMPLETED" for x in level_results)
    rb = fx.resource_budget
    resource_status = "BLOCKED" if (
        any_level_blocked or args.native_build_s > rb.max_compile_s or aggregate_solve > rb.max_solve_s
        or total_post > rb.max_postprocess_s or peak_rss > rb.ram_budget_mb
        or disk_mb > rb.disk_budget_mb or output_mb > rb.max_output_mb
    ) else "PASS"

    fixture_status = "blocked" if resource_status == "BLOCKED" or observable is None else ("pass" if observable.status == "pass" else "fail")
    fixture_evidence = BakeoffFixtureEvidence(
        fixture_id=FIXTURE_ID, status=fixture_status, evidence_ref=evidence_ref,
        adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION, backend_version=c.source_commit_sha[:12],
        precision="float64", compile_s=args.native_build_s, solve_s=aggregate_solve, postprocess_s=total_post,
        peak_ram_mb=peak_rss, disk_mb=disk_mb, output_mb=output_mb,
        observables=(() if fixture_status == "blocked" else (observable,)),
        diagnostics=("Exact H1 p2 uniform refinements 0/1/2.", "Exact four GL2/Padé[2/2] substeps per 12000 Hz output interval.", "Current R100A complex RMS authority; no dB/phase acceptance gate."),
    )
    record = build_typed_record(plan, benchmark, candidates, fixture_evidence, platform, run_id, evidence_ref)
    validate_typed_record_binding(record, plan, benchmark, candidates)

    before = build_production_adoption_readiness_report(benchmark, candidates, profile, ledger)
    preview_ledger = BakeoffReadinessEvidenceLedger(ledger_id=f"{ledger.ledger_id}-spatial-preview-{run_id}", records=ledger.records + (record,))
    after = build_production_adoption_readiness_report(benchmark, candidates, profile, preview_ledger)
    after_candidate = candidate_report(after, CANDIDATE_ID)
    if observable is None:
        outcome = "BLOCKED"
    elif spatial_status == "FAIL" or tolerance_status == "FAIL":
        outcome = "FAIL"
    elif resource_status != "PASS":
        outcome = "BLOCKED"
    else:
        outcome = "PASS"

    deterministic_identity = semantic_hash({
        "plan_sha256": file_sha256(args.plan), "authority": plan.authority,
        "level_identities": [{"r": x.refinement, "system": x.semidiscrete_identity_sha256, "transfer": x.transfer_identity_sha256} for x in level_results],
        "adjacent": [x.model_dump(mode="json") for x in adjacent],
        "against_finest": [x.model_dump(mode="json") for x in against_fine],
        "spatial_status": spatial_status, "tolerance_status": tolerance_status, "outcome": outcome,
    })
    report = {
        "schema_version": REPORT_SCHEMA, "run_id": run_id,
        "workflow_execution": "PASS", "spatial_numerical_convergence": spatial_status,
        "current_r100a_fixture_tolerance": tolerance_status, "resource_suitability": resource_status,
        "typed_evidence_admissibility": "PASS", "candidate_wide_readiness": after_candidate.decision,
        "production_solver_selection": False, "experiment_outcome": outcome,
        "task_start_main_sha": "9e6066259ec58c093e9a7587550ccf907f28402f",
        "implementation_head_sha": os.environ.get("HTDT_PR_HEAD_SHA", os.environ.get("GITHUB_SHA", "unknown")),
        "frozen_plan_commit_sha": "c181fb62993e5dcbff719feac02b013a0459d5d2",
        "frozen_plan_sha256": file_sha256(args.plan), "plan": plan.model_dump(mode="json"),
        "frequency_count": len(scored_frequency_grid(fx)), "frequency_start_hz": scored_frequency_grid(fx)[0],
        "frequency_stop_hz": scored_frequency_grid(fx)[-1],
        "level_results": [x.model_dump(mode="json") for x in level_results],
        "adjacent_complex_metrics": [x.model_dump(mode="json") for x in adjacent],
        "authority_against_finest_metrics": [x.model_dump(mode="json") for x in against_fine],
        "observable_evidence": None if observable is None else observable.model_dump(mode="json"),
        "fixture_evidence": fixture_evidence.model_dump(mode="json"),
        "readiness_record": record.model_dump(mode="json"),
        "readiness_before": before.model_dump(mode="json"),
        "readiness_after_preview": after.model_dump(mode="json"),
        "resource_aggregate": {"native_build_s": args.native_build_s, "solve_s": aggregate_solve, "postprocess_s": total_post, "peak_rss_mb": peak_rss, "disk_mb": disk_mb, "output_mb": output_mb},
        "level_details": details, "deterministic_report_identity_sha256": deterministic_identity,
        "known_limitations": ["This slice qualifies only the current rigid rectangular spatial-convergence fixture.", "Experiment PASS does not select a production solver."],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.readiness_record_output.write_text(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output.parent / "readiness_before.json").write_text(json.dumps(before.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output.parent / "readiness_after_preview.json").write_text(json.dumps(after.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    provenance = {
        "r100a_manifest_sha256": file_sha256(args.manifest), "candidate_manifest_sha256": file_sha256(args.candidates),
        "plan_sha256": file_sha256(args.plan), "mfem_source_commit_sha": c.source_commit_sha,
        "r100a_semantic_hash": benchmark.semantic_hash(), "candidate_manifest_semantic_hash": candidates.semantic_hash(),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"), "github_job": os.environ.get("GITHUB_JOB"),
        "github_sha": os.environ.get("GITHUB_SHA"), "implementation_head_sha": report["implementation_head_sha"],
        "deterministic_report_identity_sha256": deterministic_identity,
    }
    args.provenance_output.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "experiment_outcome": outcome, "spatial_numerical_convergence": spatial_status,
        "current_r100a_fixture_tolerance": tolerance_status, "resource_suitability": resource_status,
        "typed_evidence_admissibility": "PASS", "candidate_wide_readiness": after_candidate.decision,
        "production_solver_selected": False, "report": str(args.output),
    }, indent=2, sort_keys=True))
    print("R100B_SPATIAL_READINESS_RECORD=" + json.dumps(record.model_dump(mode="json"), separators=(",", ":")))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
