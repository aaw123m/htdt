"""#778: product compute envelope — preflight, degradation, fidelity."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_compute_envelope import (
    ComputeEvidenceRepository,
    HardwareEnvelope,
    REFERENCE_HARDWARE_PROFILES,
    WorkloadSpec,
    classify_workload,
    fidelity_stage_label,
    plan_adaptation,
    preflight_workload,
    record_benchmark,
)


def _spec(**kw) -> WorkloadSpec:
    base = dict(
        provider_id='wave-low',
        backend='gpu',
        upper_frequency_hz=80.0,
        mesh_resolution_m=0.05,
        domain_volume_m3=20.0,
        geometry_triangle_count=400,
        source_count=2,
        receiver_count=8,
        field_sample_count=50_000,
        candidate_count=24,
    )
    base.update(kw)
    return WorkloadSpec(**base)


def _env(**kw) -> HardwareEnvelope:
    base = dict(
        profile_id='test-pc',
        label='Test PC',
        cpu_cores=8,
        ram_mb=32 * 1024,
        vram_mb=8 * 1024,
        scratch_mb=64 * 1024,
        gpu_available=True,
    )
    base.update(kw)
    return HardwareEnvelope(**base)


def test_small_workload_supported() -> None:
    report = preflight_workload(_spec(), _env())
    assert report.problem_class == 'c_small'
    assert report.supported


def test_outside_envelope_classified() -> None:
    spec = _spec(upper_frequency_hz=600.0, mesh_resolution_m=0.01)
    report = preflight_workload(spec, _env())
    assert report.problem_class == 'c_outside_local_envelope'
    assert not report.supported
    assert 'RAM' in report.detail


def test_dominant_drivers_reported() -> None:
    spec = _spec(source_count=64, candidate_count=200)
    report = preflight_workload(spec, _env())
    assert report.estimate.dominant_drivers
    assert 'source_count' in report.estimate.dominant_drivers


def test_gpu_to_cpu_is_equivalent_execution() -> None:
    env = _env(gpu_available=False, vram_mb=1024)
    spec = _spec(backend='gpu')
    proposal = plan_adaptation(spec, env)
    assert proposal.kind == 'equivalent_execution'
    assert proposal.executed_spec.backend == 'cpu'
    assert not proposal.requires_user_acceptance


def test_lower_fidelity_requires_acceptance() -> None:
    env = _env(ram_mb=8 * 1024, vram_mb=512, scratch_mb=2048)
    spec = _spec(backend='cpu', upper_frequency_hz=400.0,
                 mesh_resolution_m=0.01, source_count=1)
    proposal = plan_adaptation(spec, env)
    assert proposal.kind == 'lower_fidelity_proposal'
    assert proposal.requires_user_acceptance
    assert proposal.executed_spec.upper_frequency_hz < spec.upper_frequency_hz
    assert 'frequency' in proposal.resolution_impact


def test_fail_closed_carries_no_spec() -> None:
    env = _env(ram_mb=128, vram_mb=64, scratch_mb=64)
    spec = _spec(backend='cpu', upper_frequency_hz=1000.0,
                 mesh_resolution_m=0.001, field_sample_count=10_000_000)
    proposal = plan_adaptation(spec, env)
    assert proposal.kind == 'fail_closed'
    assert proposal.executed_spec is None


def test_fidelity_stages_labeled() -> None:
    assert 'not the final' in fidelity_stage_label('screening')
    assert 'final' in fidelity_stage_label('high_fidelity')


def test_reference_profiles_bounded() -> None:
    assert len(REFERENCE_HARDWARE_PROFILES) >= 3
    ids = {p.profile_id for p in REFERENCE_HARDWARE_PROFILES}
    assert 'ref-minimum-supported' in ids


def test_benchmark_evidence_repository(tmp_path: Path) -> None:
    repo = ComputeEvidenceRepository(tmp_path / 'cad.sqlite3')
    spec = _spec()
    env = _env()
    record = record_benchmark(
        spec,
        env,
        case_name='rectangular_low_band',
        solver_identity='wave-low 0.1',
        runtime_s=12.5,
        peak_memory_mb=2048.0,
        output_size_mb=64.0,
        recorded_at_utc='2026-09-24T00:00:00+00:00',
    )
    repo.save_record(record)
    repo.save_record(record)
    assert repo.get_record(record.benchmark_id) == record
    assert [
        r.benchmark_id for r in repo.list_records_for_case('rectangular_low_band')
    ] == [record.benchmark_id]


def test_classify_thresholds() -> None:
    tiny = _env(ram_mb=4 * 1024 * 1024, vram_mb=4 * 1024 * 1024,
                scratch_mb=4 * 1024 * 1024)
    assert classify_workload(_spec(), tiny)[0] == 'c_small'
    tight = _env(ram_mb=2048, vram_mb=1024, scratch_mb=1024)
    cls, _ = classify_workload(_spec(upper_frequency_hz=300.0), tight)
    assert cls in {'c_medium', 'c_large', 'c_outside_local_envelope'}
