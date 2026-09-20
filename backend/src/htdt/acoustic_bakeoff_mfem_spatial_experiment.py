from __future__ import annotations

import json
import math
from hashlib import sha256
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_bakeoff import BakeoffFixtureEvidence, BakeoffPlatform
from .acoustic_bakeoff_observation import RawConvergenceLevel, RawObservationSample, evaluate_monotonic_convergence_observable
from .acoustic_bakeoff_readiness import BakeoffReadinessEvidenceRecord, candidate_semantic_hash

CANDIDATE_ID = "mfem-v4.10-d964264"
FIXTURE_ID = "wave-rectangular-convergence-v1"
SYSTEM_SCHEMA = "r100b-mfem-rectangular-semidiscrete-system-1"
ADAPTER_ID = "htdt-r100b-mfem-spatial-refinement"
ADAPTER_VERSION = "1"

def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def semantic_hash(value: object) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()

class MfemSpatialRefinementPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["r100b-mfem-spatial-refinement-plan-1"]
    plan_id: str
    authority: dict
    fixture_contract: dict
    numerical_contract: dict
    temporal_integrator: dict
    resource_ceiling: dict
    required_evidence: tuple[str, ...]
    acceptance: dict
    hard_rules: dict
    promotion: dict

    @model_validator(mode="after")
    def frozen_contract(self):
        a, f, n, t, r, ac, h, p = (
            self.authority, self.fixture_contract, self.numerical_contract,
            self.temporal_integrator, self.resource_ceiling, self.acceptance,
            self.hard_rules, self.promotion,
        )
        exact = (
            (a.get("fixture_id"), FIXTURE_ID, "fixture"),
            (a.get("candidate_id"), CANDIDATE_ID, "candidate"),
            (a.get("candidate_source_commit_sha"), "d964264cdb9a13e94a201b6c236c7721e0c8765f", "MFEM source"),
            (f.get("h1_order"), 2, "H1 order"),
            (tuple(f.get("uniform_refinements", ())), (0, 1, 2), "refinements"),
            (n.get("observation_time_s"), 2.0, "record duration"),
            (n.get("record_interval"), "half_open_0_T", "record interval"),
            (n.get("result_driven_masks_or_exclusions"), False, "result masks"),
            (t.get("algorithm_id"), "gauss-legendre-2stage-pade22-linear", "integrator"),
            (t.get("order"), 4, "integrator order"),
            (t.get("substeps_per_output_interval"), 4, "GL2 substeps"),
            (t.get("output_sample_rate_hz"), 12000, "output rate"),
            (t.get("internal_rate_hz"), 48000, "internal rate"),
            (t.get("adaptive_stepping"), False, "adaptive stepping"),
            (t.get("residual_relative_tolerance"), 1e-10, "residual tolerance"),
            (t.get("residual_check_interval_steps"), 256, "residual interval"),
            (r.get("ram_budget_mb"), 8192, "RAM ceiling"),
            (r.get("disk_budget_mb"), 2048, "disk ceiling"),
            (r.get("max_solve_s_per_refinement"), 300, "solve ceiling"),
            (r.get("max_output_mb_per_refinement"), 512, "output ceiling"),
            (tuple(ac.get("adjacent_pairs", ())), ("h0->h1", "h1->h2"), "adjacent pairs"),
            (tuple(ac.get("metrics", ())), ("complex_rms_absolute", "complex_rms_relative"), "metrics"),
            (h.get("spatial_refinement_is_only_primary_variable"), True, "primary variable"),
            (h.get("geometry_fitting"), False, "geometry fitting"),
            (h.get("source_receiver_shift"), False, "source/receiver shift"),
            (h.get("tolerance_relaxation"), False, "tolerance relaxation"),
            (h.get("h3_retry"), False, "h3 retry"),
            (h.get("p_order_retry"), False, "p retry"),
            (h.get("record_shortening"), False, "record shortening"),
            (h.get("frequency_reduction"), False, "frequency reduction"),
            (h.get("time_step_coarsening"), False, "time step coarsening"),
            (h.get("automatic_solver_selection"), False, "solver selection"),
            (p.get("production_solver_selected"), False, "production selection"),
        )
        for actual, expected, label in exact:
            if actual != expected:
                raise ValueError(f"{label} differs from frozen spatial experiment: {actual} != {expected}")
        return self

    def temporal_hash(self) -> str:
        return semantic_hash(self.temporal_integrator)

def load_spatial_refinement_plan(path: str | Path) -> MfemSpatialRefinementPlan:
    return MfemSpatialRefinementPlan.model_validate_json(Path(path).read_text(encoding="utf-8"))

def candidate(candidates):
    return next(x for x in candidates.candidates if x.candidate_id == CANDIDATE_ID)

def fixture(benchmark):
    return next(x for x in benchmark.fixtures if x.fixture_id == FIXTURE_ID)

def validate_exact_authority_binding(plan, benchmark, candidates) -> None:
    c = candidate(candidates)
    checks = (
        ("r100a_manifest_id", benchmark.manifest_id, plan.authority["r100a_manifest_id"]),
        ("r100a_semantic_hash", benchmark.semantic_hash(), plan.authority["r100a_semantic_hash"]),
        ("candidate_manifest_hash", candidates.semantic_hash(), plan.authority["candidate_manifest_hash"]),
        ("candidate_semantic_hash", candidate_semantic_hash(c), plan.authority["candidate_semantic_hash"]),
        ("candidate_source_commit_sha", c.source_commit_sha, plan.authority["candidate_source_commit_sha"]),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise ValueError(f"{name} binding mismatch: {actual} != {expected}")

def validate_current_fixture_contract(plan, fx) -> None:
    data = fx.model_dump(mode="json")
    if fx.fixture_id != FIXTURE_ID or len(data["regions"]) != 1 or data["obstacles"] or data["portals"] or data["terminations"]:
        raise ValueError("current R100A rectangular fixture topology differs from frozen experiment")
    if len(data["sources"]) != 1 or len(data["receivers"]) != 1:
        raise ValueError("current R100A source/receiver cardinality differs")
    if data["sources"][0]["normalization"] != "volume_velocity_m3_s":
        raise ValueError("current R100A source normalization differs")
    cmp = data["comparison"]
    if float(cmp["observation_time_s"]) != 2.0 or cmp["window"] != "none" or cmp["filter"] != "none":
        raise ValueError("current R100A finite record controls differ")
    expected_transfer = {
        "excitation_model": "causal_discrete_unit_sample_volume_velocity",
        "sample_zero_reference": "source_t0",
        "record_interval": "half_open_0_T",
        "solver_time_step_policy": "solver_native_recorded",
        "dtft_kernel": "exp(+i*2*pi*f*n*dt)",
        "dtft_measure": "dt_weighted_sum",
        "numerator_quantity": "physical_pressure",
        "numerator_record_policy": "solver_pressure_or_declared_primary_field_conversion",
        "denominator_record": "physical_volume_velocity_samples_on_solver_time_grid",
        "transfer_definition": "pressure_over_volume_velocity",
        "frequency_evaluation": "direct_scored_frequency_dtft",
        "source_spectrum_requirement": "finite_nonzero_on_scored_grid",
        "zero_padding": "none",
    }
    if cmp["finite_record_transfer"] != expected_transfer:
        raise ValueError("current R100A finite-record transfer contract differs")
    obs = data["observables"]
    if len(obs) != 1 or obs[0]["acceptance_relation"] != "monotonic_convergence" or obs[0]["tolerance"]["phase_deg"] is not None:
        raise ValueError("current R100A convergence observable differs")

def scored_frequency_grid(fx) -> tuple[float, ...]:
    g = fx.comparison.frequency_grid
    if g.values_hz:
        return tuple(float(v) for v in g.values_hz)
    if g.kind != "uniform":
        raise ValueError("R100A frequency grid must be explicit or uniform")
    start, stop, step = float(g.start_hz), float(g.stop_hz), float(g.step_hz)
    count = int(round((stop - start) / step)) + 1
    out = tuple(start + i * step for i in range(count))
    if not math.isclose(out[-1], stop, abs_tol=1e-9):
        raise ValueError("R100A uniform frequency grid does not terminate exactly")
    return out

def rectangular_box_geometry(fx):
    vs = fx.regions[0].vertices
    axes = tuple(sorted({float(getattr(v.position, k)) for v in vs}) for k in ("x_m", "y_m", "z_m"))
    if len(vs) != 8 or any(len(a) != 2 for a in axes):
        raise ValueError("R100A convergence region is not an axis-aligned box")
    actual = {(float(v.position.x_m), float(v.position.y_m), float(v.position.z_m)) for v in vs}
    expected = {(x, y, z) for x in axes[0] for y in axes[1] for z in axes[2]}
    if actual != expected:
        raise ValueError("R100A convergence vertices do not form a complete box")
    origin = tuple(a[0] for a in axes)
    dims = tuple(a[1] - a[0] for a in axes)
    return origin, dims

def validate_semidiscrete_metadata(plan, fx, data: dict, refinement: int) -> None:
    origin, dims = rectangular_box_geometry(fx)
    exact = (
        ("schema_version", data.get("schema_version"), SYSTEM_SCHEMA),
        ("fixture_id", data.get("fixture_id"), FIXTURE_ID),
        ("boundary_model", data.get("boundary_model"), "natural-neumann-rigid"),
        ("primary_field", data.get("primary_field"), "velocity_potential_phi"),
        ("governing_equation", data.get("governing_equation"), "M*phi_tt+Kc2*phi=c^2*b*q"),
        ("mass_assembly", data.get("mass_assembly"), "MFEM MassIntegrator"),
        ("stiffness_assembly", data.get("stiffness_assembly"), "MFEM DiffusionIntegrator(c^2)"),
        ("source_functional_assembly", data.get("source_functional_assembly"), "MFEM DomainLFIntegrator(DeltaCoefficient)"),
        ("receiver_functional_assembly", data.get("receiver_functional_assembly"), "MFEM DomainLFIntegrator(DeltaCoefficient)"),
        ("matrix_format", data.get("matrix_format"), "csr_full"),
        ("order", int(data.get("order", -1)), 2),
        ("uniform_refinements", int(data.get("uniform_refinements", -1)), refinement),
        ("source_normalization", data.get("source_normalization"), fx.sources[0].normalization),
    )
    for name, actual, expected in exact:
        if actual != expected:
            raise ValueError(f"semidiscrete {name} mismatch: {actual} != {expected}")
    vectors = (
        ("origin_m", origin),
        ("dimensions_m", dims),
        ("source_position_m", (fx.sources[0].position.x_m, fx.sources[0].position.y_m, fx.sources[0].position.z_m)),
        ("receiver_position_m", (fx.receivers[0].position.x_m, fx.receivers[0].position.y_m, fx.receivers[0].position.z_m)),
    )
    for name, expected in vectors:
        actual = data.get(name)
        if not isinstance(actual, list) or len(actual) != 3 or any(not math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12) for a, b in zip(actual, expected)):
            raise ValueError(f"semidiscrete non-spatial parameter {name} differs from current R100A")
    for name, expected in (("density_kg_m3", fx.environment.density_kg_m3), ("sound_speed_m_s", fx.environment.sound_speed_m_s)):
        if not math.isclose(float(data.get(name, math.nan)), float(expected), rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"semidiscrete {name} differs from current R100A")

class SpatialLevelResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    refinement: int
    status: Literal["COMPLETED", "RESOURCE_BLOCKED", "BLOCKED"]
    reason_code: str
    reason: str
    element_count: int | None = None
    dof_count: int | None = None
    mass_nnz: int | None = None
    stiffness_nnz: int | None = None
    assembly_s: float | None = None
    mass_factorization_s: float | None = None
    pade_factorization_s: float | None = None
    stepping_s: float | None = None
    total_solve_s: float | None = None
    peak_rss_mb: float | None = None
    work_disk_mb: float | None = None
    output_disk_mb: float | None = None
    internal_step_count: int | None = None
    source_spectrum_min_abs: float | None = None
    mass_solve_relative_residual: float | None = None
    max_checked_step_relative_residual: float | None = None
    checked_step_residual_count: int | None = None
    semidiscrete_identity_sha256: str | None = None
    transfer_identity_sha256: str | None = None

class AdjacentMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)
    coarse_refinement: int
    fine_refinement: int
    complex_rms_absolute: float = Field(ge=0)
    complex_rms_relative: float = Field(ge=0)

def complex_rms(coarse: Sequence[complex], fine: Sequence[complex], a: int, b: int) -> AdjacentMetrics:
    if len(coarse) != len(fine) or not coarse:
        raise ValueError("complex RMS requires equal nonempty vectors")
    se = sum(abs(complex(x) - complex(y)) ** 2 for x, y in zip(coarse, fine))
    sr = sum(abs(complex(y)) ** 2 for y in fine)
    absolute = math.sqrt(se / len(coarse))
    ref = math.sqrt(sr / len(coarse))
    relative = absolute / ref if ref else (0.0 if absolute == 0 else float("inf"))
    return AdjacentMetrics(coarse_refinement=a, fine_refinement=b, complex_rms_absolute=absolute, complex_rms_relative=relative)

def evaluate_spatial_transfers(fx, frequencies: Sequence[float], transfers: dict[int, Sequence[complex]]):
    if tuple(transfers) != (0, 1, 2):
        raise ValueError("transfer results must preserve exact 0/1/2 ordering")
    authority = scored_frequency_grid(fx)
    if tuple(float(x) for x in frequencies) != authority:
        raise ValueError("exact R100A frequency coverage mismatch")
    levels = []
    for r in (0, 1, 2):
        if len(transfers[r]) != len(authority):
            raise ValueError("transfer result omits R100A scored frequencies")
        levels.append(RawConvergenceLevel(
            level_id=f"h{r}", refinement_parameter="relative_uniform_h_scale",
            refinement_value=1.0 / (2 ** r),
            samples=tuple(RawObservationSample(sample_key=f"H@{f:g}Hz", frequency_hz=f, real_value=float(complex(v).real), imag_value=float(complex(v).imag)) for f, v in zip(authority, transfers[r])),
        ))
    obs = evaluate_monotonic_convergence_observable(fx.observables[0], tuple(levels))
    adjacent = (complex_rms(transfers[0], transfers[1], 0, 1), complex_rms(transfers[1], transfers[2], 1, 2))
    against_fine = (complex_rms(transfers[0], transfers[2], 0, 2), adjacent[1])
    return obs, adjacent, against_fine

def validate_level_order(plan, levels: Sequence[SpatialLevelResult]):
    if tuple(x.refinement for x in levels) != tuple(plan.fixture_contract["uniform_refinements"]):
        raise ValueError("refinement results must preserve exact 0/1/2 ordering")

def build_typed_record(plan, benchmark, candidates, fx_evidence: BakeoffFixtureEvidence, platform: BakeoffPlatform, run_id: str, evidence_ref: str):
    validate_exact_authority_binding(plan, benchmark, candidates)
    c = candidate(candidates)
    record = BakeoffReadinessEvidenceRecord(
        evidence_id=f"mfem-spatial-refinement-{run_id}", candidate_id=c.candidate_id,
        candidate_source_commit_sha=c.source_commit_sha, candidate_semantic_hash=candidate_semantic_hash(c),
        candidate_manifest_hash=candidates.semantic_hash(), r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(), target_kind="fixture", fixture_id=FIXTURE_ID,
        reported_status=fx_evidence.status, evidence_ref=evidence_ref, fixture_evidence=fx_evidence,
        platform=platform, notes=("Exact current-authority MFEM h0/h1/h2 spatial evidence.", "production_solver_selected=false"),
    )
    validate_typed_record_binding(record, plan, benchmark, candidates)
    return record

def validate_typed_record_binding(record, plan, benchmark, candidates) -> None:
    c = candidate(candidates)
    checks = (
        ("candidate_id", record.candidate_id, c.candidate_id),
        ("candidate_source_commit_sha", record.candidate_source_commit_sha, c.source_commit_sha),
        ("candidate_semantic_hash", record.candidate_semantic_hash, candidate_semantic_hash(c)),
        ("candidate_manifest_hash", record.candidate_manifest_hash, candidates.semantic_hash()),
        ("r100a_manifest_id", record.r100a_manifest_id, benchmark.manifest_id),
        ("r100a_semantic_hash", record.r100a_semantic_hash, benchmark.semantic_hash()),
        ("fixture_id", record.fixture_id, FIXTURE_ID),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise ValueError(f"typed readiness {name} mismatch: {actual} != {expected}")
    if record.fixture_evidence is None or record.fixture_evidence.fixture_id != FIXTURE_ID:
        raise ValueError("typed readiness fixture payload mismatch")


def resource_status_fail_closed(levels: Sequence[SpatialLevelResult]) -> Literal["PASS", "BLOCKED"]:
    return "BLOCKED" if any(item.status != "COMPLETED" for item in levels) else "PASS"
