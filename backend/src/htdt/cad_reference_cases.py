"""Reference Project & Case Study Library manifest (#806).

Immutable packaged educational/regression content — separate from user
projects, never seeding synthetic/demo authority into a real project.

- ``ReferenceCaseManifest`` is the machine-readable case contract:
  purpose, exact project/template/package refs, external dependencies,
  license, expected capability states, expected result summaries,
  known limitations, tutorial steps, owner/review date, and a
  CURRENT/NEEDS_REVIEW/SUPERSEDED/ARCHIVED drift state.
- ``CaseExpectation`` records machine-checkable expectations
  (authority counts/states, reason codes, semantic hashes, tolerance
  windows, expected artifacts, stale transitions) — never
  screenshot-only results.
- ``open_reference_case`` produces a read-only inspection view or a
  working-copy descriptor with a FRESH project identity; sample
  evidence never lands silently in the user's library.
- ``validate_manifest`` enforces the case-family rules (>=1 golden
  path, >=3 negative cases, tutorial steps, expected-result contract).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


CaseFamily = Literal[
    'golden_path',
    'negative',
    'geometry_import',
    'solver_validation',
    'optimization',
    'treatment_material',
    'commissioning_device',
]

CaseDriftState = Literal['CURRENT', 'NEEDS_REVIEW', 'SUPERSEDED', 'ARCHIVED']

CaseLayer = Literal['public_reference', 'private_owner']

ExpectationKind = Literal[
    'authority_count',
    'authority_state',
    'reason_code',
    'semantic_hash',
    'numeric_window',
    'artifact_presence',
    'stale_transition',
    'capability_state',
]

CASE_SCHEMA_VERSION = 1






class CaseExpectation(BaseModel):
    """One machine-checkable expected result (#806 §4)."""

    model_config = ConfigDict(frozen=True)

    kind: ExpectationKind
    subject: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    tolerance: str | None = None
    note: str = ''


class ExternalAssetRef(BaseModel):
    """An external asset the case requires but cannot redistribute —
    shipped as manifest + acquisition instructions (#806 §7)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    license: str = Field(min_length=1)
    attribution: str = ''
    redistribution_allowed: bool = False
    source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    acquisition_note: str = ''


class TutorialStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_index: int = Field(ge=0)
    title: str = Field(min_length=1)
    instruction: str = Field(min_length=1)


class ReferenceCaseManifest(BaseModel):
    """Reproducible case manifest (#806 §3) — immutable packaged
    educational/regression content."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    case_id: str = Field(min_length=1)
    case_version: str = Field(min_length=1)
    family: CaseFamily
    layer: CaseLayer = 'public_reference'
    drift_state: CaseDriftState = 'CURRENT'
    purpose: str = Field(min_length=1)
    difficulty: Literal['introductory', 'intermediate', 'advanced'] = (
        'introductory'
    )
    htdt_min_version: str = Field(min_length=1)
    project_refs: tuple[str, ...] = ()
    external_assets: tuple[ExternalAssetRef, ...] = ()
    license: str = Field(min_length=1)
    expected_capability_states: tuple[str, ...] = ()
    expectations: tuple[CaseExpectation, ...] = ()
    expected_result_summaries: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    tutorial_steps: tuple[TutorialStep, ...] = ()
    case_owner: str = Field(min_length=1)
    review_date_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_manifest(self) -> 'ReferenceCaseManifest':
        if not self.case_id.startswith('reference-case:'):
            raise ValueError('case id must use reference-case: prefix')
        if self.family == 'golden_path' and not self.tutorial_steps:
            raise ValueError('golden path cases need tutorial steps')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('case manifest hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'case_id': self.case_id,
            'case_version': self.case_version,
            'family': self.family,
            'layer': self.layer,
            'drift_state': self.drift_state,
            'purpose': self.purpose,
            'difficulty': self.difficulty,
            'htdt_min_version': self.htdt_min_version,
            'project_refs': list(self.project_refs),
            'external_assets': [
                item.model_dump(mode='json')
                for item in self.external_assets
            ],
            'license': self.license,
            'expected_capability_states': list(
                self.expected_capability_states
            ),
            'expectations': [
                item.model_dump(mode='json')
                for item in self.expectations
            ],
            'expected_result_summaries': list(
                self.expected_result_summaries
            ),
            'known_limitations': list(self.known_limitations),
            'tutorial_steps': [
                item.model_dump(mode='json')
                for item in self.tutorial_steps
            ],
            'case_owner': self.case_owner,
            'review_date_utc': self.review_date_utc,
        }


class CaseLibrary(BaseModel):
    """A curated set of case manifests with library-level invariants
    (#806 §2): at least one golden path and at least three negative
    cases before the library may be marked usable."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    library_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    cases: tuple[ReferenceCaseManifest, ...]
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_library(self) -> 'CaseLibrary':
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('case library hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'library_id': self.library_id,
            'created_at_utc': self.created_at_utc,
            'cases': [
                item.model_dump(mode='json') for item in self.cases
            ],
        }


class CaseOpenResult(BaseModel):
    """Result of opening a case: read-only view or working copy with a
    fresh project identity — never mixing sample evidence into the
    user's current project (#806 §1)."""

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    mode: Literal['inspect', 'working_copy', 'tutorial']
    working_project_id: str | None = None
    note: str = ''


def build_case_manifest(**payload: Any) -> ReferenceCaseManifest:
    provisional = ReferenceCaseManifest.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ReferenceCaseManifest.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def build_case_library(
    *,
    library_id: str,
    created_at_utc: str,
    cases: tuple[ReferenceCaseManifest, ...],
) -> CaseLibrary:
    provisional = CaseLibrary.model_construct(
        library_id=library_id,
        created_at_utc=created_at_utc,
        cases=cases,
        semantic_sha256='0' * 64,
    )
    return CaseLibrary.model_validate(
        {
            'library_id': library_id,
            'created_at_utc': created_at_utc,
            'cases': cases,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def validate_library_readiness(library: CaseLibrary) -> tuple[str, ...]:
    """Return the gaps that keep the library from satisfying #806 §2:
    >=1 golden path and >=3 negative cases required."""
    gaps: list[str] = []
    golden = [c for c in library.cases if c.family == 'golden_path']
    negative = [c for c in library.cases if c.family == 'negative']
    if not golden:
        gaps.append('no golden_path case present (RP10 required)')
    if len(negative) < 3:
        gaps.append(
            f'only {len(negative)} negative cases; at least 3 required'
        )
    for case in library.cases:
        if not case.expectations and not case.expected_result_summaries:
            gaps.append(
                f'{case.case_id}: no machine-checkable expectations '
                'or result summaries'
            )
    return tuple(gaps)


def open_reference_case(
    manifest: ReferenceCaseManifest,
    *,
    mode: Literal['inspect', 'working_copy', 'tutorial'],
    working_project_id: str | None = None,
) -> CaseOpenResult:
    """Open a case read-only or as a working copy with fresh project
    identity. Archived cases can only be inspected."""
    if manifest.drift_state == 'ARCHIVED' and mode != 'inspect':
        raise ValueError(
            'archived cases are inspect-only; restore or supersede first'
        )
    if mode == 'working_copy' and not working_project_id:
        raise ValueError('working copy requires a fresh project id')
    return CaseOpenResult(
        case_id=manifest.case_id,
        mode=mode,
        working_project_id=(
            working_project_id if mode == 'working_copy' else None
        ),
        note=(
            'sample evidence stays inside the case; the working copy '
            'gets a fresh project identity'
        ),
    )


def build_initial_reference_library(
    *,
    created_at_utc: str,
    htdt_version: str,
) -> CaseLibrary:
    """The curated public case set shipped with the product.

    One golden-path case (RP10) and three negative cases (RP20) —
    the minimum viable library per #806 — plus a solver-validation
    case (RP40). All cases are synthetic/bounded fixtures and carry
    machine-checkable expectations, never screenshots only."""
    golden = build_case_manifest(
        case_id='reference-case:golden-small-room',
        case_version='1',
        family='golden_path',
        purpose=(
            'small home-theater golden path: room -> system -> '
            'measurement -> prediction readiness -> optimize -> '
            'proposal -> apply record -> verify -> report'
        ),
        difficulty='introductory',
        htdt_min_version=htdt_version,
        project_refs=('project-template:small-room-5-1',),
        license='htdt-internal',
        expected_capability_states=(
            'prediction_ready:READY',
            'calibration_apply:SUPPORTED',
        ),
        expectations=(
            CaseExpectation(
                kind='capability_state',
                subject='prediction_ready',
                expected='READY',
                note='bounded synthetic measurement makes prediction ready',
            ),
            CaseExpectation(
                kind='artifact_presence',
                subject='calibration_apply_record',
                expected='present',
            ),
            CaseExpectation(
                kind='reason_code',
                subject='verification',
                expected='VERIFIED',
            ),
        ),
        expected_result_summaries=(
            'report artifact generated; apply record pinned to plan',
        ),
        known_limitations=(
            'synthetic room and measurement; no acoustic solver output',
        ),
        tutorial_steps=(
            TutorialStep(
                step_index=0,
                title='Open the room',
                instruction='Inspect the bundled small-room project.',
            ),
            TutorialStep(
                step_index=1,
                title='Check readiness',
                instruction='See prediction readiness report READY.',
            ),
            TutorialStep(
                step_index=2,
                title='Run calibration',
                instruction='Materialize settings for the file adapter.',
            ),
        ),
        case_owner='htdt-maintainers',
        review_date_utc=created_at_utc,
    )
    negative_missing_directivity = build_case_manifest(
        case_id='reference-case:missing-directivity',
        case_version='1',
        family='negative',
        purpose=(
            'teach why prediction blocks when speaker directivity is '
            'absent — negative cases are learning assets'
        ),
        difficulty='introductory',
        htdt_min_version=htdt_version,
        license='htdt-internal',
        expected_capability_states=('prediction_ready:BLOCKED',),
        expectations=(
            CaseExpectation(
                kind='reason_code',
                subject='prediction_ready',
                expected='BLOCKED',
                note='missing directivity is a stated blocking reason',
            ),
        ),
        expected_result_summaries=(
            'prediction unavailable with directivity gap surfaced',
        ),
        known_limitations=('fixture contains no directivity data',),
        tutorial_steps=(
            TutorialStep(
                step_index=0,
                title='Open blocked case',
                instruction='Read the blocking reason list.',
            ),
        ),
        case_owner='htdt-maintainers',
        review_date_utc=created_at_utc,
    )
    negative_unresolved_mesh = build_case_manifest(
        case_id='reference-case:unresolved-mesh',
        case_version='1',
        family='negative',
        purpose=(
            'imported mesh with open boundaries — prediction readiness '
            'explained via geometry health (#762)'
        ),
        difficulty='intermediate',
        htdt_min_version=htdt_version,
        license='htdt-internal',
        expected_capability_states=(
            'wave_closed_volume:BLOCKED',
            'visual_mesh:ready',
        ),
        expectations=(
            CaseExpectation(
                kind='capability_state',
                subject='wave_closed_volume',
                expected='blocked',
            ),
        ),
        expected_result_summaries=(
            'health summary shows open_boundary blocker with action',
        ),
        known_limitations=(
            'single-triangle fixture; not a real room scan',
        ),
        tutorial_steps=(
            TutorialStep(
                step_index=0,
                title='Inspect mesh health',
                instruction='Open the health summary, note the blockers.',
            ),
        ),
        case_owner='htdt-maintainers',
        review_date_utc=created_at_utc,
    )
    negative_unknown_firmware = build_case_manifest(
        case_id='reference-case:unknown-firmware',
        case_version='1',
        family='negative',
        purpose=(
            'device capability qualified on firmware 1.0 but observed '
            'on 2.26 — NEEDS_REQUALIFICATION teaching case (#792)'
        ),
        difficulty='intermediate',
        htdt_min_version=htdt_version,
        license='htdt-internal',
        expected_capability_states=('peq:NEEDS_REQUALIFICATION',),
        expectations=(
            CaseExpectation(
                kind='capability_state',
                subject='peq',
                expected='NEEDS_REQUALIFICATION',
            ),
        ),
        expected_result_summaries=(
            'firmware drift surfaces as requalification, writes gated',
        ),
        known_limitations=('fixture adapter only; no hardware',),
        tutorial_steps=(
            TutorialStep(
                step_index=0,
                title='Resolve capability',
                instruction='Run firmware resolution against the matrix.',
            ),
        ),
        case_owner='htdt-maintainers',
        review_date_utc=created_at_utc,
    )
    solver_validation = build_case_manifest(
        case_id='reference-case:analytic-miki',
        case_version='1',
        family='solver_validation',
        purpose=(
            'analytic Miki-model TMM fixture validating absorption '
            'derivation against a hand-computed reference (#790, RP40)'
        ),
        difficulty='advanced',
        htdt_min_version=htdt_version,
        license='htdt-internal',
        expected_capability_states=('material_derivation:IMPLEMENTED',),
        expectations=(
            CaseExpectation(
                kind='numeric_window',
                subject='absorption_500hz',
                expected='0.93 +/- 0.01',
                tolerance='0.01',
            ),
        ),
        expected_result_summaries=(
            '100mm porous layer alpha(500Hz) ~= 0.936 matches analytic',
        ),
        known_limitations=(
            'normal-incidence model only; not a measured room',
        ),
        tutorial_steps=(
            TutorialStep(
                step_index=0,
                title='Derive evidence',
                instruction='Build construction, derive material evidence.',
            ),
        ),
        case_owner='htdt-maintainers',
        review_date_utc=created_at_utc,
    )
    return build_case_library(
        library_id='reference-library:public',
        created_at_utc=created_at_utc,
        cases=(
            golden,
            negative_missing_directivity,
            negative_unresolved_mesh,
            negative_unknown_firmware,
            solver_validation,
        ),
    )


__all__ = [
    'CASE_SCHEMA_VERSION',
    'CaseDriftState',
    'CaseExpectation',
    'CaseFamily',
    'CaseLayer',
    'CaseLibrary',
    'CaseOpenResult',
    'ExpectationKind',
    'ExternalAssetRef',
    'ReferenceCaseManifest',
    'TutorialStep',
    'build_case_library',
    'build_case_manifest',
    'build_initial_reference_library',
    'open_reference_case',
    'validate_library_readiness',
]
