from __future__ import annotations

import argparse
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_benchmark import (
    AcousticBenchmarkManifest,
    BenchmarkCapability,
    canonical_benchmark_json,
    load_acoustic_benchmark_manifest,
)


CandidateRole = Literal['wave_primary_evaluation', 'wave_reference', 'geometric_reference']
MethodFamily = Literal['fdtd', 'fem', 'geometric']
EvaluationScope = Literal['shipping_candidate', 'reference_only']
RedistributionStatus = Literal['pass', 'pending', 'fail']
WindowsPackagingStatus = Literal[
    'documented_native',
    'linux_upstream_port_required',
    'python_package_probe_pending',
    'reference_only',
    'unsupported',
]
ExecutionPathStatus = Literal['documented_upstream', 'probe_pending', 'unsupported']
CompiledRepresentation = Literal['voxel_grid', 'volume_mesh', 'polyhedral_room']
EvidenceStatus = Literal['pass', 'fail', 'blocked', 'unsupported', 'not_run']
ObservableStatus = Literal['pass', 'fail', 'not_evaluated']
GateStatus = Literal['pass', 'fail', 'not_run', 'not_applicable']
DecisionStatus = Literal['open', 'selected', 'no_go']


class BakeoffCandidate(BaseModel):
    """Version-pinned R100B candidate definition.

    A probe capability is only permission to run a fixture. It is not evidence that
    the upstream project or an HTDT adapter has passed that capability.
    """

    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    role: CandidateRole
    method_family: MethodFamily
    evaluation_scope: EvaluationScope
    upstream_url: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    source_evidence: str = Field(min_length=1)
    license_spdx: str = Field(min_length=1)
    license_evidence: str = Field(min_length=1)
    redistribution_status: RedistributionStatus
    redistribution_notes: str = Field(min_length=1)
    windows_packaging_status: WindowsPackagingStatus
    windows_packaging_notes: str = Field(min_length=1)
    cpu_path_status: ExecutionPathStatus
    gpu_path_status: ExecutionPathStatus
    required_representation: CompiledRepresentation
    probe_capabilities: tuple[BenchmarkCapability, ...] = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_candidate(self) -> 'BakeoffCandidate':
        if len(self.probe_capabilities) != len(set(self.probe_capabilities)):
            raise ValueError('probe capabilities must be unique')
        if self.role == 'wave_primary_evaluation' and self.method_family != 'fdtd':
            raise ValueError('wave_primary_evaluation must use the fdtd method family')
        if self.role == 'wave_reference' and self.method_family != 'fem':
            raise ValueError('wave_reference must use the fem method family')
        if self.role == 'geometric_reference' and self.method_family != 'geometric':
            raise ValueError('geometric_reference must use the geometric method family')
        if self.method_family in {'fdtd', 'fem'} and not any(
            item.startswith('wave_') for item in self.probe_capabilities
        ):
            raise ValueError('wave candidate must declare at least one wave probe capability')
        if self.method_family == 'geometric' and not any(
            item.startswith('geometric_') or item == 'stochastic_rays'
            for item in self.probe_capabilities
        ):
            raise ValueError('geometric candidate must declare a geometric probe capability')
        if self.evaluation_scope == 'reference_only' and self.windows_packaging_status not in {
            'reference_only',
            'python_package_probe_pending',
        }:
            raise ValueError('reference-only candidate has inconsistent Windows packaging status')
        return self


class BakeoffCandidateManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-candidates-1'] = 'r100b-candidates-1'
    manifest_id: str = Field(min_length=1)
    researched_on: str = Field(min_length=1)
    candidates: tuple[BakeoffCandidate, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def required_roles_and_unique_ids(self) -> 'BakeoffCandidateManifest':
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError('candidate ids must be unique')
        roles = {item.role for item in self.candidates}
        required_roles = {'wave_primary_evaluation', 'wave_reference', 'geometric_reference'}
        missing = sorted(required_roles - roles)
        if missing:
            raise ValueError(f'candidate manifest is missing required R100B roles: {missing}')
        return self

    def canonical_json(self) -> str:
        return canonical_benchmark_json(self.model_dump(mode='json'))

    def semantic_hash(self) -> str:
        return sha256(self.canonical_json().encode('utf-8')).hexdigest()


class BakeoffAdoptionProfile(BaseModel):
    """Fail-closed R100B product-adoption requirements kept separate from run hashes."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-adoption-1'] = 'r100b-adoption-1'
    profile_id: str = Field(min_length=1)
    selection_scope: Literal['low_band_wave_solver'] = 'low_band_wave_solver'
    allowed_candidate_roles: tuple[CandidateRole, ...] = Field(min_length=1)
    required_capabilities: tuple[BenchmarkCapability, ...] = Field(min_length=1)
    required_fixture_ids: tuple[str, ...] = Field(min_length=1)
    deferred_fixture_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_profile(self) -> 'BakeoffAdoptionProfile':
        if len(self.allowed_candidate_roles) != len(set(self.allowed_candidate_roles)):
            raise ValueError('adoption profile candidate roles must be unique')
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError('adoption profile required capabilities must be unique')
        if len(self.required_fixture_ids) != len(set(self.required_fixture_ids)):
            raise ValueError('adoption profile required fixture ids must be unique')
        if len(self.deferred_fixture_ids) != len(set(self.deferred_fixture_ids)):
            raise ValueError('adoption profile deferred fixture ids must be unique')
        overlap = set(self.required_fixture_ids) & set(self.deferred_fixture_ids)
        if overlap:
            raise ValueError(
                f'adoption profile fixtures cannot be both required and deferred: {sorted(overlap)}'
            )
        if 'geometric_reference' in self.allowed_candidate_roles:
            raise ValueError('low-band wave adoption cannot select geometric_reference role')
        return self

    def canonical_json(self) -> str:
        return canonical_benchmark_json(self.model_dump(mode='json'))

    def semantic_hash(self) -> str:
        return sha256(self.canonical_json().encode('utf-8')).hexdigest()

class BakeoffObservableEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    observable_id: str = Field(min_length=1)
    status: ObservableStatus
    summary: str = Field(min_length=1)
    absolute_error: float | None = Field(default=None, ge=0.0)
    relative_error: float | None = Field(default=None, ge=0.0)
    phase_error_deg: float | None = Field(default=None, ge=0.0)
    statistical_stddev: float | None = Field(default=None, ge=0.0)
    difference_from_peer: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def finite_metrics(self) -> 'BakeoffObservableEvidence':
        values = (
            self.absolute_error,
            self.relative_error,
            self.phase_error_deg,
            self.statistical_stddev,
            self.difference_from_peer,
        )
        if any(value is not None and not isfinite(float(value)) for value in values):
            raise ValueError('observable evidence metrics must be finite')
        return self


class BakeoffFixtureEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    fixture_id: str = Field(min_length=1)
    status: EvidenceStatus
    evidence_ref: str | None = None
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    backend_version: str = Field(min_length=1)
    precision: Literal['float32', 'float64', 'mixed']
    compile_s: float | None = Field(default=None, ge=0.0)
    solve_s: float | None = Field(default=None, ge=0.0)
    postprocess_s: float | None = Field(default=None, ge=0.0)
    peak_ram_mb: float | None = Field(default=None, ge=0.0)
    disk_mb: float | None = Field(default=None, ge=0.0)
    output_mb: float | None = Field(default=None, ge=0.0)
    observables: tuple[BakeoffObservableEvidence, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode='after')
    def evidence_requirements(self) -> 'BakeoffFixtureEvidence':
        values = (
            self.compile_s,
            self.solve_s,
            self.postprocess_s,
            self.peak_ram_mb,
            self.disk_mb,
            self.output_mb,
        )
        if any(value is not None and not isfinite(float(value)) for value in values):
            raise ValueError('fixture resource metrics must be finite')
        if self.status in {'pass', 'fail'} and not self.evidence_ref:
            raise ValueError('executed fixture evidence requires evidence_ref')
        if self.status == 'pass':
            if not self.observables:
                raise ValueError('passing fixture requires observable evidence')
            if any(item.status != 'pass' for item in self.observables):
                raise ValueError('passing fixture cannot contain non-passing observables')
        if self.status in {'blocked', 'unsupported', 'not_run'} and self.observables:
            raise ValueError('non-executed fixture must not contain observable evidence')
        observable_ids = [item.observable_id for item in self.observables]
        if len(observable_ids) != len(set(observable_ids)):
            raise ValueError('fixture observable evidence ids must be unique')
        return self


class BakeoffHardGateEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: Literal[
        'physics_correctness',
        'cpu_baseline',
        'windows_packaging',
        'license_redistribution',
        'required_capability',
        'reproducible_authority',
    ]
    status: GateStatus
    evidence_ref: str | None = None
    summary: str = Field(min_length=1)

    @model_validator(mode='after')
    def passing_gate_has_evidence(self) -> 'BakeoffHardGateEvidence':
        if self.status in {'pass', 'fail'} and not self.evidence_ref:
            raise ValueError('executed hard gate requires evidence_ref')
        return self


class BakeoffPlatform(BaseModel):
    model_config = ConfigDict(frozen=True)

    os: str = Field(min_length=1)
    architecture: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    cpu: str = Field(min_length=1)
    logical_threads: int = Field(ge=1)
    thread_budget: int = Field(ge=1)
    gpu: str | None = None
    device_notes: str = Field(min_length=1)


class BakeoffRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-run-1'] = 'r100b-run-1'
    run_id: str = Field(min_length=1)
    r100a_manifest_id: str = Field(min_length=1)
    r100a_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_manifest_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    platform: BakeoffPlatform
    fixture_evidence: tuple[BakeoffFixtureEvidence, ...] = Field(min_length=1)
    hard_gates: tuple[BakeoffHardGateEvidence, ...] = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def unique_evidence_ids(self) -> 'BakeoffRun':
        fixture_ids = [item.fixture_id for item in self.fixture_evidence]
        if len(fixture_ids) != len(set(fixture_ids)):
            raise ValueError('fixture evidence ids must be unique')
        categories = [item.category for item in self.hard_gates]
        if len(categories) != len(set(categories)):
            raise ValueError('hard gate evidence categories must be unique')
        return self

    def canonical_json(self) -> str:
        return canonical_benchmark_json(self.model_dump(mode='json'))

    def semantic_hash(self) -> str:
        return sha256(self.canonical_json().encode('utf-8')).hexdigest()


class BakeoffDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: DecisionStatus = 'open'
    selected_candidate_id: str | None = None
    accepted_run_id: str | None = None
    adoption_profile_id: str | None = Field(default=None, min_length=1)
    adoption_profile_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def selection_fields(self) -> 'BakeoffDecision':
        if self.status == 'selected':
            if not self.selected_candidate_id or not self.accepted_run_id:
                raise ValueError('selected decision requires candidate and accepted run ids')
            if not self.adoption_profile_id or not self.adoption_profile_sha256:
                raise ValueError('selected decision requires exact adoption profile binding')
        elif (
            self.selected_candidate_id is not None
            or self.accepted_run_id is not None
            or self.adoption_profile_id is not None
            or self.adoption_profile_sha256 is not None
        ):
            raise ValueError(
                'non-selected decision must not name an accepted candidate/run/adoption profile'
            )
        return self



def load_bakeoff_candidate_manifest(path: str | Path) -> BakeoffCandidateManifest:
    return BakeoffCandidateManifest.model_validate_json(Path(path).read_text(encoding='utf-8'))


def load_bakeoff_adoption_profile(path: str | Path) -> BakeoffAdoptionProfile:
    return BakeoffAdoptionProfile.model_validate_json(Path(path).read_text(encoding='utf-8'))

def load_bakeoff_run(path: str | Path) -> BakeoffRun:
    return BakeoffRun.model_validate_json(Path(path).read_text(encoding='utf-8'))


def applicable_fixture_ids(
    benchmark: AcousticBenchmarkManifest,
    candidate: BakeoffCandidate,
) -> tuple[str, ...]:
    capabilities = set(candidate.probe_capabilities)
    return tuple(
        fixture.fixture_id
        for fixture in benchmark.fixtures
        if set(fixture.required_capabilities).issubset(capabilities)
    )


def validate_bakeoff_adoption_profile(
    benchmark: AcousticBenchmarkManifest,
    profile: BakeoffAdoptionProfile,
) -> None:
    fixture_by_id = {item.fixture_id: item for item in benchmark.fixtures}
    referenced = set(profile.required_fixture_ids) | set(profile.deferred_fixture_ids)
    benchmark_fixture_ids = set(fixture_by_id)
    unknown = sorted(referenced - benchmark_fixture_ids)
    if unknown:
        raise ValueError(f'adoption profile references unknown fixtures: {unknown}')
    unclassified = sorted(benchmark_fixture_ids - referenced)
    if unclassified:
        raise ValueError(
            f'adoption profile leaves benchmark fixtures unclassified: {unclassified}'
        )

    required_capabilities = set(profile.required_capabilities)
    fixture_capabilities = set().union(
        *(
            set(fixture_by_id[fixture_id].required_capabilities)
            for fixture_id in profile.required_fixture_ids
        )
    )
    missing = sorted(fixture_capabilities - required_capabilities)
    if missing:
        raise ValueError(
            f'adoption profile required capabilities do not cover required fixtures: {missing}'
        )

def _required_gate_categories(
    benchmark: AcousticBenchmarkManifest,
    candidate: BakeoffCandidate,
) -> set[str]:
    if candidate.evaluation_scope == 'shipping_candidate':
        return {
            gate.category
            for gate in benchmark.hard_gates
            if gate.applies_to in {'candidate', 'both'}
        }
    return {
        gate.category
        for gate in benchmark.hard_gates
        if gate.applies_to == 'both'
    }


def observable_tolerance_violations(expected, evidence: BakeoffObservableEvidence) -> tuple[str, ...]:
    """Return R100A tolerance violations for one evaluated observable."""

    tolerance = expected.tolerance
    violations: list[str] = []

    def require_and_bound(name: str, value: float | None, limit: float | None) -> None:
        if limit is None:
            return
        if value is None:
            violations.append(
                f'passing observable {expected.observable_id} is missing {name} evidence'
            )
        elif value > limit:
            violations.append(
                f'passing observable {expected.observable_id} exceeds {name} tolerance: '
                f'{value} > {limit}'
            )

    if expected.acceptance_relation == 'must_differ_from_peer':
        minimum = tolerance.minimum_difference
        if minimum is None:
            violations.append('must-differ observable authority is missing minimum_difference')
        elif evidence.difference_from_peer is None:
            violations.append(
                f'passing observable {expected.observable_id} is missing difference_from_peer'
            )
        elif evidence.difference_from_peer < minimum:
            violations.append(
                f'passing observable {expected.observable_id} does not meet minimum difference: '
                f'{evidence.difference_from_peer} < {minimum}'
            )
        return tuple(violations)

    if expected.kind == 'transfer_phase_deg':
        require_and_bound('phase_error_deg', evidence.phase_error_deg, tolerance.phase_deg)
    elif expected.kind == 'complex_reflection_coefficient':
        require_and_bound('absolute_error', evidence.absolute_error, tolerance.absolute)
        require_and_bound('relative_error', evidence.relative_error, tolerance.relative)
        require_and_bound('phase_error_deg', evidence.phase_error_deg, tolerance.phase_deg)
    else:
        require_and_bound('absolute_error', evidence.absolute_error, tolerance.absolute)
        require_and_bound('relative_error', evidence.relative_error, tolerance.relative)
        if tolerance.phase_deg is not None:
            require_and_bound('phase_error_deg', evidence.phase_error_deg, tolerance.phase_deg)

    if tolerance.statistical_stddev_max is not None:
        require_and_bound(
            'statistical_stddev',
            evidence.statistical_stddev,
            tolerance.statistical_stddev_max,
        )

    return tuple(violations)


def _validate_observable_tolerance(expected, evidence: BakeoffObservableEvidence) -> None:
    if evidence.status != 'pass':
        return
    violations = observable_tolerance_violations(expected, evidence)
    if violations:
        raise ValueError(violations[0])

def validate_bakeoff_run(
    benchmark: AcousticBenchmarkManifest,
    candidates: BakeoffCandidateManifest,
    run: BakeoffRun,
) -> None:
    if run.r100a_manifest_id != benchmark.manifest_id:
        raise ValueError('bakeoff run references the wrong R100A manifest id')
    if run.r100a_semantic_hash != benchmark.semantic_hash():
        raise ValueError('bakeoff run R100A semantic hash does not match authority')
    if run.candidate_manifest_hash != candidates.semantic_hash():
        raise ValueError('bakeoff run candidate manifest hash does not match authority')

    candidate_by_id = {item.candidate_id: item for item in candidates.candidates}
    candidate = candidate_by_id.get(run.candidate_id)
    if candidate is None:
        raise ValueError(f'unknown bakeoff candidate: {run.candidate_id}')
    if run.candidate_source_commit_sha != candidate.source_commit_sha:
        raise ValueError('bakeoff run candidate source commit does not match pinned candidate')

    fixture_by_id = {item.fixture_id: item for item in benchmark.fixtures}
    applicable = set(applicable_fixture_ids(benchmark, candidate))
    for evidence in run.fixture_evidence:
        fixture = fixture_by_id.get(evidence.fixture_id)
        if fixture is None:
            raise ValueError(f'unknown R100A fixture evidence: {evidence.fixture_id}')
        if evidence.fixture_id not in applicable and evidence.status in {'pass', 'fail'}:
            raise ValueError(
                f'candidate cannot execute {evidence.fixture_id} without all required probe capabilities'
            )

        if evidence.status == 'pass':
            required_observables = {item.observable_id for item in fixture.observables}
            observed = {item.observable_id for item in evidence.observables}
            if observed != required_observables:
                raise ValueError(
                    f'passing fixture {evidence.fixture_id} must report every required observable'
                )
            expected_by_id = {item.observable_id: item for item in fixture.observables}
            for observable_evidence in evidence.observables:
                _validate_observable_tolerance(
                    expected_by_id[observable_evidence.observable_id],
                    observable_evidence,
                )

        budget = fixture.resource_budget
        resource_checks = (
            ('compile_s', evidence.compile_s, budget.max_compile_s),
            ('solve_s', evidence.solve_s, budget.max_solve_s),
            ('postprocess_s', evidence.postprocess_s, budget.max_postprocess_s),
            ('peak_ram_mb', evidence.peak_ram_mb, float(budget.ram_budget_mb)),
            ('disk_mb', evidence.disk_mb, float(budget.disk_budget_mb)),
            ('output_mb', evidence.output_mb, budget.max_output_mb),
        )
        if evidence.status == 'pass':
            missing = [name for name, value, _ in resource_checks if value is None]
            if missing:
                raise ValueError(
                    f'passing fixture {evidence.fixture_id} is missing resource evidence: {missing}'
                )
            exceeded = [
                f'{name}={value} > {limit}'
                for name, value, limit in resource_checks
                if value is not None and value > limit
            ]
            if run.platform.thread_budget > budget.cpu_thread_budget:
                exceeded.append(
                    f'thread_budget={run.platform.thread_budget} > {budget.cpu_thread_budget}'
                )
            if exceeded:
                raise ValueError(
                    f'passing fixture {evidence.fixture_id} exceeds resource budget: {exceeded}'
                )

    required_gates = _required_gate_categories(benchmark, candidate)
    gate_by_category = {item.category: item for item in run.hard_gates}
    missing_gates = sorted(required_gates - set(gate_by_category))
    if missing_gates:
        raise ValueError(f'bakeoff run is missing required hard-gate evidence: {missing_gates}')

    unexpected_na = sorted(
        category
        for category in required_gates
        if gate_by_category[category].status == 'not_applicable'
    )
    if unexpected_na:
        raise ValueError(f'required hard gates cannot be not_applicable: {unexpected_na}')


def validate_bakeoff_decision(
    benchmark: AcousticBenchmarkManifest,
    candidates: BakeoffCandidateManifest,
    runs: tuple[BakeoffRun, ...],
    decision: BakeoffDecision,
    adoption_profile: BakeoffAdoptionProfile | None = None,
) -> None:
    if decision.status != 'selected':
        return
    if adoption_profile is None:
        raise ValueError('production selection requires an explicit adoption profile')
    validate_bakeoff_adoption_profile(benchmark, adoption_profile)
    if (
        decision.adoption_profile_id != adoption_profile.profile_id
        or decision.adoption_profile_sha256 != adoption_profile.semantic_hash()
    ):
        raise ValueError('selected decision adoption profile binding is stale or mismatched')

    candidate = next(
        (item for item in candidates.candidates if item.candidate_id == decision.selected_candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError('selected bakeoff candidate is unknown')
    if candidate.evaluation_scope != 'shipping_candidate':
        raise ValueError('reference-only candidate cannot be selected for the production stack')
    if candidate.role not in set(adoption_profile.allowed_candidate_roles):
        raise ValueError(
            f'selected candidate role {candidate.role} is outside adoption profile'
        )

    missing_capabilities = sorted(
        set(adoption_profile.required_capabilities) - set(candidate.probe_capabilities)
    )
    if missing_capabilities:
        raise ValueError(
            f'production selection is blocked by missing required capabilities: '
            f'{missing_capabilities}'
        )

    run = next((item for item in runs if item.run_id == decision.accepted_run_id), None)
    if run is None or run.candidate_id != candidate.candidate_id:
        raise ValueError('accepted run does not belong to the selected candidate')
    validate_bakeoff_run(benchmark, candidates, run)

    required_gates = _required_gate_categories(benchmark, candidate)
    gate_by_category = {item.category: item.status for item in run.hard_gates}
    failed = sorted(
        category for category in required_gates if gate_by_category.get(category) != 'pass'
    )
    if failed:
        raise ValueError(f'production selection is blocked by hard gates: {failed}')

    evidence_by_fixture = {item.fixture_id: item.status for item in run.fixture_evidence}
    incomplete = sorted(
        fixture_id
        for fixture_id in adoption_profile.required_fixture_ids
        if evidence_by_fixture.get(fixture_id) != 'pass'
    )
    if incomplete:
        raise ValueError(
            f'production selection is blocked by required adoption fixtures: {incomplete}'
        )



def preflight_summary(
    benchmark: AcousticBenchmarkManifest,
    candidates: BakeoffCandidateManifest,
    adoption_profile: BakeoffAdoptionProfile | None = None,
) -> dict[str, object]:
    coverage = {
        candidate.candidate_id: set(applicable_fixture_ids(benchmark, candidate))
        for candidate in candidates.candidates
    }
    covered = set().union(*coverage.values()) if coverage else set()
    if adoption_profile is not None:
        validate_bakeoff_adoption_profile(benchmark, adoption_profile)

    return {
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'candidate_manifest_id': candidates.manifest_id,
        'candidate_manifest_hash': candidates.semantic_hash(),
        'uncovered_fixture_ids': sorted(
            fixture.fixture_id for fixture in benchmark.fixtures if fixture.fixture_id not in covered
        ),
        'adoption_profile': (
            {
                'profile_id': adoption_profile.profile_id,
                'profile_sha256': adoption_profile.semantic_hash(),
                'selection_scope': adoption_profile.selection_scope,
                'required_fixture_ids': list(adoption_profile.required_fixture_ids),
                'required_capabilities': list(adoption_profile.required_capabilities),
            }
            if adoption_profile is not None
            else None
        ),
        'candidates': [
            {
                'candidate_id': candidate.candidate_id,
                'role': candidate.role,
                'evaluation_scope': candidate.evaluation_scope,
                'source_ref': candidate.source_ref,
                'source_commit_sha': candidate.source_commit_sha,
                'applicable_fixture_ids': list(applicable_fixture_ids(benchmark, candidate)),
                'required_gate_categories': sorted(_required_gate_categories(benchmark, candidate)),
                'adoption_missing_capabilities': (
                    sorted(
                        set(adoption_profile.required_capabilities)
                        - set(candidate.probe_capabilities)
                    )
                    if adoption_profile is not None
                    and candidate.role in set(adoption_profile.allowed_candidate_roles)
                    else []
                ),
            }
            for candidate in candidates.candidates
        ],
    }



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='R100B solver bakeoff authority/preflight')
    subparsers = parser.add_subparsers(dest='command', required=True)

    preflight = subparsers.add_parser('preflight', help='validate authorities and print candidate coverage')
    preflight.add_argument('--manifest', required=True, type=Path)
    preflight.add_argument('--candidates', required=True, type=Path)
    preflight.add_argument('--adoption-profile', type=Path)

    validate = subparsers.add_parser('validate-run', help='validate a recorded R100B evidence run')
    validate.add_argument('--manifest', required=True, type=Path)
    validate.add_argument('--candidates', required=True, type=Path)
    validate.add_argument('--run', required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)

    if args.command == 'preflight':
        adoption_profile = (
            load_bakeoff_adoption_profile(args.adoption_profile)
            if args.adoption_profile is not None
            else None
        )
        print(
            json.dumps(
                preflight_summary(benchmark, candidates, adoption_profile),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    run = load_bakeoff_run(args.run)
    validate_bakeoff_run(benchmark, candidates, run)
    print(
        json.dumps(
            {
                'status': 'valid',
                'run_id': run.run_id,
                'run_semantic_hash': run.semantic_hash(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
