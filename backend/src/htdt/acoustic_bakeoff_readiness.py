from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_bakeoff import (
    BakeoffAdoptionProfile,
    BakeoffCandidate,
    BakeoffCandidateManifest,
    BakeoffFixtureEvidence,
    BakeoffHardGateEvidence,
    BakeoffPlatform,
    load_bakeoff_adoption_profile,
    load_bakeoff_candidate_manifest,
    observable_tolerance_violations,
    validate_bakeoff_adoption_profile,
)
from .acoustic_benchmark import (
    AcousticBenchmarkManifest,
    canonical_benchmark_json,
    load_acoustic_benchmark_manifest,
)


ReadinessStatus = Literal['PASS', 'FAIL', 'BLOCKED', 'NOT_APPLICABLE']
ReadinessDecision = Literal['READY', 'NO_GO']
EvidenceReportedStatus = Literal[
    'pass',
    'fail',
    'blocked',
    'unsupported',
    'not_run',
    'not_applicable',
]
EvidenceTargetKind = Literal['fixture', 'hard_gate']

HARD_GATE_CATEGORIES = (
    'physics_correctness',
    'cpu_baseline',
    'windows_packaging',
    'license_redistribution',
    'required_capability',
    'reproducible_authority',
)


def candidate_semantic_hash(candidate: BakeoffCandidate) -> str:
    payload = canonical_benchmark_json(candidate.model_dump(mode='json'))
    return sha256(payload.encode('utf-8')).hexdigest()


class BakeoffReadinessEvidenceRecord(BaseModel):
    """One immutable R100B evidence atom plus exact authority bindings.

    The record may preserve historical evidence without a current binding. Historical
    evidence remains visible in reports but cannot satisfy a current production gate.
    A record only becomes selection input when all exact bindings match and its typed
    existing R100B evidence payload is present and valid.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    candidate_source_commit_sha: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{40}$',
    )
    candidate_semantic_hash: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    candidate_manifest_hash: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    r100a_manifest_id: str | None = Field(default=None, min_length=1)
    r100a_semantic_hash: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    target_kind: EvidenceTargetKind
    fixture_id: str | None = Field(default=None, min_length=1)
    hard_gate_category: Literal[
        'physics_correctness',
        'cpu_baseline',
        'windows_packaging',
        'license_redistribution',
        'required_capability',
        'reproducible_authority',
    ] | None = None
    reported_status: EvidenceReportedStatus
    evidence_ref: str = Field(min_length=1)
    fixture_evidence: BakeoffFixtureEvidence | None = None
    hard_gate_evidence: BakeoffHardGateEvidence | None = None
    platform: BakeoffPlatform | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def target_and_payload_agree(self) -> 'BakeoffReadinessEvidenceRecord':
        if self.target_kind == 'fixture':
            if not self.fixture_id or self.hard_gate_category is not None:
                raise ValueError('fixture evidence record requires only fixture_id')
            if self.hard_gate_evidence is not None:
                raise ValueError('fixture evidence record cannot embed hard-gate evidence')
            if self.fixture_evidence is not None:
                if self.fixture_evidence.fixture_id != self.fixture_id:
                    raise ValueError('fixture evidence payload is bound to the wrong fixture')
                if self.fixture_evidence.status != self.reported_status:
                    raise ValueError('fixture evidence payload status disagrees with reported status')
        else:
            if not self.hard_gate_category or self.fixture_id is not None:
                raise ValueError('hard-gate evidence record requires only hard_gate_category')
            if self.fixture_evidence is not None:
                raise ValueError('hard-gate evidence record cannot embed fixture evidence')
            if self.reported_status in {'blocked', 'unsupported'}:
                if self.hard_gate_evidence is not None:
                    raise ValueError(
                        'typed hard-gate evidence uses not_run rather than blocked/unsupported'
                    )
            elif self.hard_gate_evidence is not None:
                if self.hard_gate_evidence.category != self.hard_gate_category:
                    raise ValueError('hard-gate evidence payload is bound to the wrong category')
                if self.hard_gate_evidence.status != self.reported_status:
                    raise ValueError('hard-gate evidence payload status disagrees with reported status')
        return self


class BakeoffReadinessEvidenceLedger(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-readiness-evidence-1'] = 'r100b-readiness-evidence-1'
    ledger_id: str = Field(min_length=1)
    records: tuple[BakeoffReadinessEvidenceRecord, ...] = ()

    @model_validator(mode='after')
    def unique_evidence_ids(self) -> 'BakeoffReadinessEvidenceLedger':
        ids = [item.evidence_id for item in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError('readiness evidence ids must be unique')
        return self

    def canonical_json(self) -> str:
        return canonical_benchmark_json(self.model_dump(mode='json'))

    def semantic_hash(self) -> str:
        return sha256(self.canonical_json().encode('utf-8')).hexdigest()


class ExcludedEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    reported_status: EvidenceReportedStatus
    evidence_ref: str
    reason: str


class ReadinessCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    check_id: str
    status: ReadinessStatus
    mandatory: bool
    summary: str
    evidence_ids: tuple[str, ...] = ()
    excluded_evidence_ids: tuple[str, ...] = ()


class CandidateReadinessReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    display_name: str
    role: str
    evaluation_scope: str
    implementation_repository: str
    implementation_ref: str
    implementation_commit_sha: str
    implementation_semantic_hash: str
    license_spdx: str
    redistribution_status: str
    windows_packaging_status: str
    decision: ReadinessDecision
    fixture_results: tuple[ReadinessCheck, ...]
    hard_gate_results: tuple[ReadinessCheck, ...]
    audit_checks: tuple[ReadinessCheck, ...]
    excluded_evidence: tuple[ExcludedEvidence, ...]
    negative_evidence_ids: tuple[str, ...]
    next_experiments: tuple[str, ...]


class ProductionAdoptionReadinessReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-production-readiness-report-1'] = (
        'r100b-production-readiness-report-1'
    )
    r100a_manifest_id: str
    r100a_semantic_hash: str
    candidate_manifest_id: str
    candidate_manifest_hash: str
    adoption_profile_id: str
    adoption_profile_sha256: str
    evidence_ledger_id: str
    evidence_ledger_sha256: str
    selection_scope: str
    decision: ReadinessDecision
    ready_candidate_ids: tuple[str, ...]
    production_solver_selected: Literal[False] = False
    candidates: tuple[CandidateReadinessReport, ...]
    orphan_evidence: tuple[ExcludedEvidence, ...] = ()

    def canonical_json(self) -> str:
        return canonical_benchmark_json(self.model_dump(mode='json'))

    def semantic_hash(self) -> str:
        return sha256(self.canonical_json().encode('utf-8')).hexdigest()


def load_readiness_evidence_ledger(
    path: str | Path,
) -> BakeoffReadinessEvidenceLedger:
    return BakeoffReadinessEvidenceLedger.model_validate_json(
        Path(path).read_text(encoding='utf-8')
    )


def _map_reported_status(status: EvidenceReportedStatus) -> ReadinessStatus:
    if status == 'pass':
        return 'PASS'
    if status == 'fail':
        return 'FAIL'
    if status == 'not_applicable':
        return 'NOT_APPLICABLE'
    return 'BLOCKED'


def _binding_rejection_reason(
    record: BakeoffReadinessEvidenceRecord,
    benchmark: AcousticBenchmarkManifest,
    candidate: BakeoffCandidate,
) -> str | None:
    if record.candidate_source_commit_sha is None:
        return 'missing_candidate_implementation_binding'
    if record.candidate_source_commit_sha != candidate.source_commit_sha:
        return 'stale_candidate_implementation'
    if record.r100a_manifest_id is None or record.r100a_semantic_hash is None:
        return 'missing_r100a_authority_binding'
    if record.r100a_manifest_id != benchmark.manifest_id:
        return 'stale_r100a_manifest_id'
    if record.r100a_semantic_hash != benchmark.semantic_hash():
        return 'stale_r100a_authority'
    if record.candidate_semantic_hash is None:
        return 'missing_candidate_authority_binding'
    if record.candidate_semantic_hash != candidate_semantic_hash(candidate):
        return 'stale_candidate_authority'
    return None


def _validate_passing_fixture_payload(
    benchmark: AcousticBenchmarkManifest,
    candidate: BakeoffCandidate,
    record: BakeoffReadinessEvidenceRecord,
) -> str | None:
    evidence = record.fixture_evidence
    if evidence is None:
        return 'typed_fixture_evidence_missing'

    fixture_by_id = {item.fixture_id: item for item in benchmark.fixtures}
    fixture = fixture_by_id.get(evidence.fixture_id)
    if fixture is None:
        return 'fixture_not_present_in_current_r100a'

    missing_capabilities = set(fixture.required_capabilities) - set(candidate.probe_capabilities)
    if missing_capabilities and evidence.status in {'pass', 'fail'}:
        return 'candidate_missing_fixture_capability'

    if evidence.status != 'pass':
        return None

    expected_by_id = {item.observable_id: item for item in fixture.observables}
    observed_by_id = {item.observable_id: item for item in evidence.observables}
    if set(expected_by_id) != set(observed_by_id):
        return 'passing_fixture_observable_set_mismatch'
    for observable_id, observable in observed_by_id.items():
        if observable.status != 'pass':
            return 'passing_fixture_contains_nonpassing_observable'
        if observable_tolerance_violations(expected_by_id[observable_id], observable):
            return 'passing_fixture_exceeds_current_r100a_tolerance'

    resource_fields = (
        ('compile_s', evidence.compile_s, fixture.resource_budget.max_compile_s),
        ('solve_s', evidence.solve_s, fixture.resource_budget.max_solve_s),
        (
            'postprocess_s',
            evidence.postprocess_s,
            fixture.resource_budget.max_postprocess_s,
        ),
        (
            'peak_ram_mb',
            evidence.peak_ram_mb,
            float(fixture.resource_budget.ram_budget_mb),
        ),
        ('disk_mb', evidence.disk_mb, float(fixture.resource_budget.disk_budget_mb)),
        ('output_mb', evidence.output_mb, fixture.resource_budget.max_output_mb),
    )
    if any(value is None for _, value, _ in resource_fields):
        return 'passing_fixture_missing_resource_evidence'
    if any(
        value is not None and value > limit
        for _, value, limit in resource_fields
    ):
        return 'passing_fixture_exceeds_current_resource_budget'
    if record.platform is None:
        return 'passing_fixture_missing_execution_platform'
    if record.platform.thread_budget > fixture.resource_budget.cpu_thread_budget:
        return 'passing_fixture_exceeds_thread_budget'
    return None


def _validate_hard_gate_payload(
    record: BakeoffReadinessEvidenceRecord,
) -> str | None:
    if record.hard_gate_evidence is None:
        return 'typed_hard_gate_evidence_missing'
    if record.hard_gate_category == 'windows_packaging' and record.reported_status == 'pass':
        if record.platform is None:
            return 'windows_gate_missing_execution_platform'
        if 'windows' not in record.platform.os.lower():
            return 'windows_gate_platform_is_not_windows'
    if record.hard_gate_category == 'cpu_baseline' and record.reported_status == 'pass':
        if record.platform is None:
            return 'cpu_gate_missing_execution_platform'
    return None


def _excluded(
    record: BakeoffReadinessEvidenceRecord,
    reason: str,
) -> ExcludedEvidence:
    return ExcludedEvidence(
        evidence_id=record.evidence_id,
        reported_status=record.reported_status,
        evidence_ref=record.evidence_ref,
        reason=reason,
    )


def _resolve_fixture(
    benchmark: AcousticBenchmarkManifest,
    candidate: BakeoffCandidate,
    fixture_id: str,
    records: tuple[BakeoffReadinessEvidenceRecord, ...],
) -> tuple[ReadinessCheck, tuple[ExcludedEvidence, ...]]:
    fixture = next((item for item in benchmark.fixtures if item.fixture_id == fixture_id), None)
    relevant = tuple(
        item
        for item in records
        if item.candidate_id == candidate.candidate_id
        and item.target_kind == 'fixture'
        and item.fixture_id == fixture_id
    )
    if fixture is None:
        excluded = tuple(_excluded(item, 'fixture_not_present_in_current_r100a') for item in relevant)
        return (
            ReadinessCheck(
                check_id=fixture_id,
                status='BLOCKED',
                mandatory=True,
                summary='required fixture is absent from the current R100A authority',
                excluded_evidence_ids=tuple(item.evidence_id for item in excluded),
            ),
            excluded,
        )

    missing = sorted(set(fixture.required_capabilities) - set(candidate.probe_capabilities))
    if missing:
        excluded = tuple(_excluded(item, 'candidate_missing_fixture_capability') for item in relevant)
        return (
            ReadinessCheck(
                check_id=fixture_id,
                status='BLOCKED',
                mandatory=True,
                summary=f'candidate does not claim required fixture capabilities: {missing}',
                excluded_evidence_ids=tuple(item.evidence_id for item in excluded),
            ),
            excluded,
        )

    current: list[BakeoffReadinessEvidenceRecord] = []
    excluded: list[ExcludedEvidence] = []
    for record in relevant:
        reason = _binding_rejection_reason(record, benchmark, candidate)
        if reason is None:
            reason = _validate_passing_fixture_payload(benchmark, candidate, record)
        if reason is not None:
            excluded.append(_excluded(record, reason))
        else:
            current.append(record)

    if not current:
        return (
            ReadinessCheck(
                check_id=fixture_id,
                status='BLOCKED',
                mandatory=True,
                summary='no exact current-authority typed fixture evidence is available',
                excluded_evidence_ids=tuple(sorted(item.evidence_id for item in excluded)),
            ),
            tuple(sorted(excluded, key=lambda item: item.evidence_id)),
        )

    statuses = {_map_reported_status(item.reported_status) for item in current}
    evidence_ids = tuple(sorted(item.evidence_id for item in current))
    if len(statuses) != 1:
        return (
            ReadinessCheck(
                check_id=fixture_id,
                status='BLOCKED',
                mandatory=True,
                summary='conflicting current evidence must be resolved before selection',
                evidence_ids=evidence_ids,
                excluded_evidence_ids=tuple(sorted(item.evidence_id for item in excluded)),
            ),
            tuple(sorted(excluded, key=lambda item: item.evidence_id)),
        )

    status = next(iter(statuses))
    return (
        ReadinessCheck(
            check_id=fixture_id,
            status=status,
            mandatory=True,
            summary=f'exact current-authority fixture evidence resolves to {status}',
            evidence_ids=evidence_ids,
            excluded_evidence_ids=tuple(sorted(item.evidence_id for item in excluded)),
        ),
        tuple(sorted(excluded, key=lambda item: item.evidence_id)),
    )


def _resolve_explicit_hard_gate(
    benchmark: AcousticBenchmarkManifest,
    candidate: BakeoffCandidate,
    category: str,
    records: tuple[BakeoffReadinessEvidenceRecord, ...],
) -> tuple[ReadinessCheck, tuple[ExcludedEvidence, ...]]:
    relevant = tuple(
        item
        for item in records
        if item.candidate_id == candidate.candidate_id
        and item.target_kind == 'hard_gate'
        and item.hard_gate_category == category
    )
    current: list[BakeoffReadinessEvidenceRecord] = []
    excluded: list[ExcludedEvidence] = []
    for record in relevant:
        reason = _binding_rejection_reason(record, benchmark, candidate)
        if reason is None:
            reason = _validate_hard_gate_payload(record)
        if reason is not None:
            excluded.append(_excluded(record, reason))
        else:
            current.append(record)

    if not current:
        return (
            ReadinessCheck(
                check_id=category,
                status='BLOCKED',
                mandatory=True,
                summary='no exact current-authority hard-gate evidence is available',
                excluded_evidence_ids=tuple(sorted(item.evidence_id for item in excluded)),
            ),
            tuple(sorted(excluded, key=lambda item: item.evidence_id)),
        )

    statuses = {_map_reported_status(item.reported_status) for item in current}
    evidence_ids = tuple(sorted(item.evidence_id for item in current))
    if len(statuses) != 1:
        return (
            ReadinessCheck(
                check_id=category,
                status='BLOCKED',
                mandatory=True,
                summary='conflicting current hard-gate evidence must be resolved',
                evidence_ids=evidence_ids,
                excluded_evidence_ids=tuple(sorted(item.evidence_id for item in excluded)),
            ),
            tuple(sorted(excluded, key=lambda item: item.evidence_id)),
        )
    status = next(iter(statuses))
    return (
        ReadinessCheck(
            check_id=category,
            status=status,
            mandatory=True,
            summary=f'exact current-authority hard-gate evidence resolves to {status}',
            evidence_ids=evidence_ids,
            excluded_evidence_ids=tuple(sorted(item.evidence_id for item in excluded)),
        ),
        tuple(sorted(excluded, key=lambda item: item.evidence_id)),
    )


def _aggregate_status(checks: tuple[ReadinessCheck, ...]) -> ReadinessStatus:
    statuses = {item.status for item in checks}
    if 'FAIL' in statuses:
        return 'FAIL'
    if statuses == {'PASS'}:
        return 'PASS'
    if statuses == {'NOT_APPLICABLE'}:
        return 'NOT_APPLICABLE'
    return 'BLOCKED'


def _derived_hard_gates(
    benchmark: AcousticBenchmarkManifest,
    candidate: BakeoffCandidate,
    profile: BakeoffAdoptionProfile,
    fixture_results: tuple[ReadinessCheck, ...],
    records: tuple[BakeoffReadinessEvidenceRecord, ...],
) -> tuple[tuple[ReadinessCheck, ...], tuple[ExcludedEvidence, ...]]:
    required_categories = tuple(
        gate.category
        for gate in benchmark.hard_gates
        if gate.applies_to in {'candidate', 'both'}
    )
    results: list[ReadinessCheck] = []
    excluded: list[ExcludedEvidence] = []

    for category in required_categories:
        if category == 'physics_correctness':
            status = _aggregate_status(fixture_results)
            results.append(
                ReadinessCheck(
                    check_id=category,
                    status=status,
                    mandatory=True,
                    summary='derived only from mandatory current R100A fixture results',
                )
            )
            continue

        if category == 'required_capability':
            missing = sorted(
                set(profile.required_capabilities) - set(candidate.probe_capabilities)
            )
            results.append(
                ReadinessCheck(
                    check_id=category,
                    status='PASS' if not missing else 'BLOCKED',
                    mandatory=True,
                    summary=(
                        'candidate declares every adoption-profile capability'
                        if not missing
                        else f'candidate is missing required capabilities: {missing}'
                    ),
                )
            )
            continue

        if category == 'license_redistribution':
            if candidate.redistribution_status == 'pass':
                status: ReadinessStatus = 'PASS'
            elif candidate.redistribution_status == 'fail':
                status = 'FAIL'
            else:
                status = 'BLOCKED'
            results.append(
                ReadinessCheck(
                    check_id=category,
                    status=status,
                    mandatory=True,
                    summary=(
                        f'{candidate.license_spdx}; {candidate.redistribution_notes}; '
                        f'evidence={candidate.license_evidence}'
                    ),
                )
            )
            continue

        if category == 'windows_packaging' and candidate.windows_packaging_status == 'unsupported':
            results.append(
                ReadinessCheck(
                    check_id=category,
                    status='FAIL',
                    mandatory=True,
                    summary='candidate manifest explicitly marks Windows packaging unsupported',
                )
            )
            continue

        check, rejected = _resolve_explicit_hard_gate(
            benchmark,
            candidate,
            category,
            records,
        )
        results.append(check)
        excluded.extend(rejected)

    return tuple(results), tuple(sorted(excluded, key=lambda item: item.evidence_id))


def _audit_checks(
    candidate: BakeoffCandidate,
    profile: BakeoffAdoptionProfile,
    fixture_results: tuple[ReadinessCheck, ...],
    hard_gate_results: tuple[ReadinessCheck, ...],
    excluded: tuple[ExcludedEvidence, ...],
) -> tuple[ReadinessCheck, ...]:
    fixture_by_id = {item.check_id: item for item in fixture_results}
    hard_gate_by_id = {item.check_id: item for item in hard_gate_results}

    def fixture_alias(check_id: str, fixture_id: str) -> ReadinessCheck:
        source = fixture_by_id[fixture_id]
        return ReadinessCheck(
            check_id=check_id,
            status=source.status,
            mandatory=True,
            summary=f'bound to {fixture_id}: {source.summary}',
            evidence_ids=source.evidence_ids,
            excluded_evidence_ids=source.excluded_evidence_ids,
        )

    resource_status = (
        'PASS'
        if all(item.status == 'PASS' for item in fixture_results)
        else 'BLOCKED'
    )
    freshness_status = (
        'PASS'
        if all(item.status == 'PASS' for item in fixture_results)
        and all(item.status == 'PASS' for item in hard_gate_results)
        else 'BLOCKED'
    )
    implementation_status: ReadinessStatus = (
        'PASS' if candidate.source_commit_sha and candidate.source_ref else 'BLOCKED'
    )
    scope_status: ReadinessStatus = (
        'PASS'
        if candidate.evaluation_scope == 'shipping_candidate'
        and candidate.role in set(profile.allowed_candidate_roles)
        else 'FAIL'
    )

    return (
        ReadinessCheck(
            check_id='production_role_scope',
            status=scope_status,
            mandatory=True,
            summary='reference-only or out-of-profile candidates cannot be shipping wave solvers',
        ),
        ReadinessCheck(
            check_id='implementation_identity',
            status=implementation_status,
            mandatory=True,
            summary=(
                f'{candidate.upstream_url}@{candidate.source_commit_sha} '
                f'(ref {candidate.source_ref})'
            ),
        ),
        ReadinessCheck(
            check_id='license_redistribution',
            status=hard_gate_by_id['license_redistribution'].status,
            mandatory=True,
            summary=hard_gate_by_id['license_redistribution'].summary,
        ),
        ReadinessCheck(
            check_id='windows_execution_and_packaging',
            status=hard_gate_by_id['windows_packaging'].status,
            mandatory=True,
            summary=hard_gate_by_id['windows_packaging'].summary,
            evidence_ids=hard_gate_by_id['windows_packaging'].evidence_ids,
            excluded_evidence_ids=hard_gate_by_id['windows_packaging'].excluded_evidence_ids,
        ),
        ReadinessCheck(
            check_id='cpu_correctness_baseline',
            status=hard_gate_by_id['cpu_baseline'].status,
            mandatory=True,
            summary=hard_gate_by_id['cpu_baseline'].summary,
            evidence_ids=hard_gate_by_id['cpu_baseline'].evidence_ids,
            excluded_evidence_ids=hard_gate_by_id['cpu_baseline'].excluded_evidence_ids,
        ),
        fixture_alias('rigid_analytical_reference', 'wave-rigid-rectangular-modes-v1'),
        fixture_alias('grid_or_mesh_convergence', 'wave-rectangular-convergence-v1'),
        fixture_alias('complex_fr_phase_capability', 'wave-rectangular-convergence-v1'),
        fixture_alias('explicit_impedance_reflection', 'wave-normal-incidence-impedance-v1'),
        fixture_alias('concave_geometry', 'wave-concave-l-room-v1'),
        fixture_alias('portal_region_continuity', 'wave-portal-split-room-v1'),
        ReadinessCheck(
            check_id='reflecting_obstacle',
            status='NOT_APPLICABLE',
            mandatory=False,
            summary=(
                'the low-band wave adoption profile does not use the deferred '
                'geometric reflecting-counter fixture'
            ),
        ),
        fixture_alias(
            'radiation_or_source_capability',
            'wave-explicit-radiation-termination-v1',
        ),
        ReadinessCheck(
            check_id='resource_evidence',
            status=resource_status,
            mandatory=True,
            summary=(
                'performance/resource evidence is considered only after each mandatory '
                'physics fixture has current typed evidence within its frozen budget'
            ),
        ),
        ReadinessCheck(
            check_id='deterministic_reproducible_execution',
            status=hard_gate_by_id['reproducible_authority'].status,
            mandatory=True,
            summary=hard_gate_by_id['reproducible_authority'].summary,
            evidence_ids=hard_gate_by_id['reproducible_authority'].evidence_ids,
            excluded_evidence_ids=hard_gate_by_id['reproducible_authority'].excluded_evidence_ids,
        ),
        ReadinessCheck(
            check_id='authority_freshness',
            status=freshness_status,
            mandatory=True,
            summary=(
                'stale evidence is retained in excluded_evidence but cannot satisfy '
                'current selection gates'
            ),
            excluded_evidence_ids=tuple(sorted(item.evidence_id for item in excluded)),
        ),
    )


def _next_experiments(
    candidate: BakeoffCandidate,
    profile: BakeoffAdoptionProfile,
    fixture_results: tuple[ReadinessCheck, ...],
    hard_gate_results: tuple[ReadinessCheck, ...],
) -> tuple[str, ...]:
    actions: list[str] = []
    if candidate.evaluation_scope != 'shipping_candidate':
        return (
            'No production-wave adoption experiment is defined for this reference-only candidate.',
        )

    missing_caps = sorted(
        set(profile.required_capabilities) - set(candidate.probe_capabilities)
    )
    if missing_caps:
        actions.append(
            'Qualify the exact missing production capabilities before rerunning fixtures: '
            + ', '.join(missing_caps)
        )

    for result in fixture_results:
        if result.status == 'FAIL':
            actions.append(
                f'Run a bounded follow-up that can change the explicit FAIL for {result.check_id}; '
                'do not relax the frozen R100A tolerance.'
            )
        elif result.status != 'PASS':
            actions.append(
                f'Persist exact current-authority typed evidence for {result.check_id}.'
            )

    for result in hard_gate_results:
        if result.check_id in {
            'physics_correctness',
            'license_redistribution',
            'required_capability',
        }:
            continue
        if result.status == 'FAIL':
            actions.append(
                f'Resolve the explicit {result.check_id} hard-gate failure with exact evidence.'
            )
        elif result.status != 'PASS':
            actions.append(
                f'Record exact current-authority evidence for hard gate {result.check_id}.'
            )

    return tuple(dict.fromkeys(actions))


def build_production_adoption_readiness_report(
    benchmark: AcousticBenchmarkManifest,
    candidates: BakeoffCandidateManifest,
    profile: BakeoffAdoptionProfile,
    ledger: BakeoffReadinessEvidenceLedger,
) -> ProductionAdoptionReadinessReport:
    validate_bakeoff_adoption_profile(benchmark, profile)

    known_candidate_ids = {item.candidate_id for item in candidates.candidates}
    orphan_evidence = tuple(
        sorted(
            (
                _excluded(item, 'unknown_candidate_binding')
                for item in ledger.records
                if item.candidate_id not in known_candidate_ids
            ),
            key=lambda item: item.evidence_id,
        )
    )

    candidate_reports: list[CandidateReadinessReport] = []
    for candidate in sorted(candidates.candidates, key=lambda item: item.candidate_id):
        fixture_results: list[ReadinessCheck] = []
        excluded: list[ExcludedEvidence] = []
        for fixture_id in profile.required_fixture_ids:
            result, rejected = _resolve_fixture(
                benchmark,
                candidate,
                fixture_id,
                ledger.records,
            )
            fixture_results.append(result)
            excluded.extend(rejected)

        fixture_tuple = tuple(fixture_results)
        hard_gates, hard_gate_excluded = _derived_hard_gates(
            benchmark,
            candidate,
            profile,
            fixture_tuple,
            ledger.records,
        )
        excluded.extend(hard_gate_excluded)
        excluded_tuple = tuple(
            sorted(
                {item.evidence_id: item for item in excluded}.values(),
                key=lambda item: item.evidence_id,
            )
        )
        audits = _audit_checks(
            candidate,
            profile,
            fixture_tuple,
            hard_gates,
            excluded_tuple,
        )

        mandatory_checks = tuple(item for item in audits if item.mandatory)
        decision: ReadinessDecision = (
            'READY'
            if all(item.status == 'PASS' for item in mandatory_checks)
            else 'NO_GO'
        )
        negative_ids = tuple(
            sorted(
                item.evidence_id
                for item in ledger.records
                if item.candidate_id == candidate.candidate_id
                and item.reported_status == 'fail'
            )
        )
        candidate_reports.append(
            CandidateReadinessReport(
                candidate_id=candidate.candidate_id,
                display_name=candidate.display_name,
                role=candidate.role,
                evaluation_scope=candidate.evaluation_scope,
                implementation_repository=candidate.upstream_url,
                implementation_ref=candidate.source_ref,
                implementation_commit_sha=candidate.source_commit_sha,
                implementation_semantic_hash=candidate_semantic_hash(candidate),
                license_spdx=candidate.license_spdx,
                redistribution_status=candidate.redistribution_status,
                windows_packaging_status=candidate.windows_packaging_status,
                decision=decision,
                fixture_results=fixture_tuple,
                hard_gate_results=hard_gates,
                audit_checks=audits,
                excluded_evidence=excluded_tuple,
                negative_evidence_ids=negative_ids,
                next_experiments=_next_experiments(
                    candidate,
                    profile,
                    fixture_tuple,
                    hard_gates,
                ),
            )
        )

    ready_ids = tuple(
        item.candidate_id for item in candidate_reports if item.decision == 'READY'
    )
    return ProductionAdoptionReadinessReport(
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_id=candidates.manifest_id,
        candidate_manifest_hash=candidates.semantic_hash(),
        adoption_profile_id=profile.profile_id,
        adoption_profile_sha256=profile.semantic_hash(),
        evidence_ledger_id=ledger.ledger_id,
        evidence_ledger_sha256=ledger.semantic_hash(),
        selection_scope=profile.selection_scope,
        decision='READY' if ready_ids else 'NO_GO',
        ready_candidate_ids=ready_ids,
        candidates=tuple(candidate_reports),
        orphan_evidence=orphan_evidence,
    )


def report_payload(report: ProductionAdoptionReadinessReport) -> dict[str, object]:
    payload = report.model_dump(mode='json')
    payload['report_identity_sha256'] = report.semantic_hash()
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='R100B candidate-wide production-adoption readiness audit'
    )
    subparsers = parser.add_subparsers(dest='command', required=True)
    audit = subparsers.add_parser(
        'audit',
        help='evaluate all candidates against current R100A/adoption hard gates',
    )
    audit.add_argument('--manifest', required=True, type=Path)
    audit.add_argument('--candidates', required=True, type=Path)
    audit.add_argument('--adoption-profile', required=True, type=Path)
    audit.add_argument('--evidence-ledger', required=True, type=Path)
    audit.add_argument('--output', type=Path)
    audit.add_argument('--expect-decision', choices=('READY', 'NO_GO'))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    profile = load_bakeoff_adoption_profile(args.adoption_profile)
    ledger = load_readiness_evidence_ledger(args.evidence_ledger)
    report = build_production_adoption_readiness_report(
        benchmark,
        candidates,
        profile,
        ledger,
    )
    payload = report_payload(report)
    rendered = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    print(rendered, end='')
    if args.expect_decision is not None and report.decision != args.expect_decision:
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
