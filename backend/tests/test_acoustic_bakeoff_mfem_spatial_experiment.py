from __future__ import annotations

import json
from pathlib import Path

import pytest

from htdt.acoustic_bakeoff import BakeoffFixtureEvidence, BakeoffPlatform, load_bakeoff_candidate_manifest
from htdt.acoustic_bakeoff_mfem_spatial_experiment import (
    SpatialLevelResult,
    build_typed_record,
    evaluate_spatial_transfers,
    load_spatial_refinement_plan,
    resource_status_fail_closed,
    scored_frequency_grid,
    validate_current_fixture_contract,
    validate_exact_authority_binding,
    validate_level_order,
    validate_semidiscrete_metadata,
    validate_typed_record_binding,
)
from htdt.acoustic_bakeoff_mfem_transient_experiment import load_experiment_plan
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "benchmarks" / "acoustics" / "r100b_mfem_spatial_refinement_plan.json"
R100A = ROOT / "benchmarks" / "acoustics" / "r100a_manifest.json"
CANDIDATES = ROOT / "benchmarks" / "acoustics" / "r100b_candidates.json"
LEGACY = ROOT / "benchmarks" / "acoustics" / "r100b_mfem_transient_experiment_plan.json"

def authorities():
    return load_spatial_refinement_plan(PLAN), load_acoustic_benchmark_manifest(R100A), load_bakeoff_candidate_manifest(CANDIDATES)

def target_fixture(benchmark):
    return next(x for x in benchmark.fixtures if x.fixture_id == "wave-rectangular-convergence-v1")

def blocked_level(refinement: int):
    return SpatialLevelResult(refinement=refinement, status="BLOCKED", reason_code="test", reason="test")

def completed_level(refinement: int):
    return SpatialLevelResult(refinement=refinement, status="COMPLETED", reason_code="completed", reason="completed")

def semidiscrete_metadata(fx, refinement: int):
    vs = fx.regions[0].vertices
    xs = sorted({float(v.position.x_m) for v in vs})
    ys = sorted({float(v.position.y_m) for v in vs})
    zs = sorted({float(v.position.z_m) for v in vs})
    s, r = fx.sources[0].position, fx.receivers[0].position
    return {
        "schema_version": "r100b-mfem-rectangular-semidiscrete-system-1",
        "fixture_id": fx.fixture_id,
        "boundary_model": "natural-neumann-rigid",
        "primary_field": "velocity_potential_phi",
        "governing_equation": "M*phi_tt+Kc2*phi=c^2*b*q",
        "mass_assembly": "MFEM MassIntegrator",
        "stiffness_assembly": "MFEM DiffusionIntegrator(c^2)",
        "source_functional_assembly": "MFEM DomainLFIntegrator(DeltaCoefficient)",
        "receiver_functional_assembly": "MFEM DomainLFIntegrator(DeltaCoefficient)",
        "matrix_format": "csr_full",
        "order": 2,
        "uniform_refinements": refinement,
        "source_normalization": fx.sources[0].normalization,
        "origin_m": [xs[0], ys[0], zs[0]],
        "dimensions_m": [xs[1] - xs[0], ys[1] - ys[0], zs[1] - zs[0]],
        "source_position_m": [s.x_m, s.y_m, s.z_m],
        "receiver_position_m": [r.x_m, r.y_m, r.z_m],
        "density_kg_m3": fx.environment.density_kg_m3,
        "sound_speed_m_s": fx.environment.sound_speed_m_s,
    }

def platform():
    return BakeoffPlatform(
        os="Windows-2025Server", architecture="AMD64", python_version="3.12",
        cpu="CI CPU", logical_threads=4, thread_budget=4, gpu=None,
        device_notes="focused spatial experiment test",
    )

def test_frozen_plan_identity_and_current_authority_binding():
    plan, benchmark, candidates = authorities()
    assert plan.plan_id == "r100b-mfem-rigid-rectangular-h-refinement-current-authority-2026-09-21"
    assert plan.authority["r100a_semantic_hash"] == "a9d45a3d650f20747368dd5610a6a91f93cdad881dcb10fcac88cd9d17e211e7"
    assert plan.authority["candidate_manifest_hash"] == "8fda56df1087fd64f55cfd17e241e46d236b60e242d86c546650d8f9d4194707"
    validate_exact_authority_binding(plan, benchmark, candidates)
    validate_current_fixture_contract(plan, target_fixture(benchmark))

def test_refinement_series_is_exact_zero_one_two():
    plan, _, _ = authorities()
    assert tuple(plan.fixture_contract["uniform_refinements"]) == (0, 1, 2)
    payload = plan.model_dump(mode="json")
    payload["fixture_contract"]["uniform_refinements"] = [0, 1, 2, 3]
    with pytest.raises(ValueError, match="refinements"):
        type(plan).model_validate(payload)

def test_exact_four_gl2_substeps_is_frozen():
    plan, _, _ = authorities()
    assert plan.temporal_integrator["algorithm_id"] == "gauss-legendre-2stage-pade22-linear"
    assert plan.temporal_integrator["substeps_per_output_interval"] == 4
    legacy = load_experiment_plan(LEGACY)
    for key in (
        "algorithm_id", "algorithm_version", "order", "propagation_form", "dissipation_model",
        "candidate_matrix_policy", "step_factorization", "mass_factorization", "linear_solver",
        "permutation", "diagonal_pivot_threshold", "equilibration", "iterative_refinement",
        "numerical_precision", "scipy_version", "numpy_version", "residual_relative_tolerance",
        "residual_check_interval_steps", "substeps_per_output_interval", "substep_policy",
    ):
        assert plan.temporal_integrator[key] == getattr(legacy.integrator, key)
    payload = plan.model_dump(mode="json")
    payload["temporal_integrator"]["substeps_per_output_interval"] = 2
    with pytest.raises(ValueError, match="GL2 substeps"):
        type(plan).model_validate(payload)

def test_output_sample_and_internal_step_construction():
    plan, benchmark, _ = authorities()
    fx = target_fixture(benchmark)
    output_rate = plan.temporal_integrator["output_sample_rate_hz"]
    substeps = plan.temporal_integrator["substeps_per_output_interval"]
    sample_count = round(fx.comparison.observation_time_s * output_rate)
    assert output_rate == 12000
    assert output_rate * substeps == 48000
    assert sample_count == 24000
    assert (sample_count - 1) * substeps == 95996
    assert 1.0 / (output_rate * substeps) == pytest.approx((1.0 / output_rate) / 4.0)

def test_non_spatial_parameter_mutation_is_rejected():
    plan, benchmark, _ = authorities()
    fx = target_fixture(benchmark)
    payload = semidiscrete_metadata(fx, 1)
    validate_semidiscrete_metadata(plan, fx, payload, 1)
    payload["density_kg_m3"] = float(payload["density_kg_m3"]) + 0.01
    with pytest.raises(ValueError, match="density_kg_m3"):
        validate_semidiscrete_metadata(plan, fx, payload, 1)

def test_refinement_result_ordering_is_fail_closed():
    plan, _, _ = authorities()
    ordered = [blocked_level(0), blocked_level(1), blocked_level(2)]
    validate_level_order(plan, ordered)
    with pytest.raises(ValueError, match="0/1/2"):
        validate_level_order(plan, [ordered[1], ordered[0], ordered[2]])

def test_r100a_authority_hash_mismatch_is_rejected():
    plan, benchmark, candidates = authorities()
    payload = plan.model_dump(mode="json")
    payload["authority"]["r100a_semantic_hash"] = "0" * 64
    stale = type(plan).model_validate(payload)
    with pytest.raises(ValueError, match="r100a_semantic_hash"):
        validate_exact_authority_binding(stale, benchmark, candidates)

def test_resource_blocked_result_fails_closed_without_retry():
    assert resource_status_fail_closed([completed_level(0), completed_level(1), completed_level(2)]) == "PASS"
    levels = [completed_level(0), completed_level(1), SpatialLevelResult(refinement=2, status="RESOURCE_BLOCKED", reason_code="ram_resource_ceiling", reason="blocked")]
    assert resource_status_fail_closed(levels) == "BLOCKED"
    assert [x.refinement for x in levels] == [0, 1, 2]

def test_typed_readiness_evidence_has_exact_current_binding():
    plan, benchmark, candidates = authorities()
    fx = target_fixture(benchmark)
    evidence = BakeoffFixtureEvidence(
        fixture_id=fx.fixture_id, status="blocked", evidence_ref="artifact:test/report.json",
        adapter_id="htdt-r100b-mfem-spatial-refinement", adapter_version="1",
        backend_version="d964264cdb9a", precision="float64",
        diagnostics=("focused binding test",),
    )
    record = build_typed_record(plan, benchmark, candidates, evidence, platform(), "test", "artifact:test/report.json")
    validate_typed_record_binding(record, plan, benchmark, candidates)
    assert record.candidate_manifest_hash == candidates.semantic_hash()
    assert record.r100a_semantic_hash == benchmark.semantic_hash()

def test_stale_or_incorrect_fixture_evidence_is_rejected():
    plan, benchmark, candidates = authorities()
    fx = target_fixture(benchmark)
    evidence = BakeoffFixtureEvidence(
        fixture_id=fx.fixture_id, status="blocked", evidence_ref="artifact:test/report.json",
        adapter_id="htdt-r100b-mfem-spatial-refinement", adapter_version="1",
        backend_version="d964264cdb9a", precision="float64",
    )
    record = build_typed_record(plan, benchmark, candidates, evidence, platform(), "test-stale", "artifact:test/report.json")
    stale = record.model_copy(update={"r100a_semantic_hash": "0" * 64})
    with pytest.raises(ValueError, match="r100a_semantic_hash"):
        validate_typed_record_binding(stale, plan, benchmark, candidates)
    wrong_manifest = record.model_copy(update={"candidate_manifest_hash": "1" * 64})
    with pytest.raises(ValueError, match="candidate_manifest_hash"):
        validate_typed_record_binding(wrong_manifest, plan, benchmark, candidates)

def test_current_r100a_complex_convergence_semantics_and_frequency_coverage():
    _, benchmark, _ = authorities()
    fx = target_fixture(benchmark)
    frequencies = scored_frequency_grid(fx)
    assert len(frequencies) == 281
    assert frequencies[0] == 20.0 and frequencies[-1] == 300.0
    fine = [complex(1.0 + 0.001 * i, 0.2) for i in range(len(frequencies))]
    medium = [z + complex(0.005, 0.0) for z in fine]
    coarse = [z + complex(0.05, 0.0) for z in fine]
    observable, adjacent, against = evaluate_spatial_transfers(fx, frequencies, {0: coarse, 1: medium, 2: fine})
    assert observable.status == "pass"
    assert adjacent[1].complex_rms_absolute < adjacent[0].complex_rms_absolute
    assert against[1].complex_rms_absolute <= fx.observables[0].tolerance.absolute
    assert against[1].complex_rms_relative <= fx.observables[0].tolerance.relative

def test_pr289_legacy_transient_plan_parsing_regression():
    legacy = load_experiment_plan(LEGACY)
    assert legacy.integrator.substeps_per_output_interval == 4
    assert [x.sample_rate_hz for x in legacy.attempts] == [6000, 9000, 12000]
    assert legacy.integrator.algorithm_id == "gauss-legendre-2stage-pade22-linear"
