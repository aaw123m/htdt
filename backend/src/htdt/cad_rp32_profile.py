"""REV56-CAMPPROFILE (#585): CEDIA RP32 commissioning profile authority.

CEDIA RP32 ("Audio System Measurement and Verification") is the companion
recommended practice to RP22: where RP22 defines *target* performance
parameters/levels, RP32 defines the *measurement and verification* process
that demonstrates whether a built system achieves the targeted RP22 level.

This module provides:

* ``CommissioningDocumentIdentity`` — exact external-profile identity
  (publisher, document, revision, source, hash, parser version) so a
  project always records *which RP32 revision* it claimed, and historical
  projects keep their original profile.
* ``Rp32CommissioningProfile`` — a sealed profile listing requirement
  domains and per-requirement mappings to HTDT authorities. Mappings may
  be populated ONLY from a lawfully reviewed exact source; until then
  every clause stays honestly ``unmapped`` / the profile stays
  ``unpopulated_pending_lawful_source``.
* ``Rp32VerificationPlan`` / ``Rp32VerificationRecord`` — sealed
  verification attempts binding profile + design target + measurement
  evidence, preserving failed baselines across correction loops, and
  linking the result back to an RP22 verification state.
* ``evaluate_measurement_readiness`` — a fail-closed readiness gate
  (READY / READY_WITH_LIMITATIONS / INCOMPATIBLE / INSUFFICIENT_EVIDENCE)
  composing the existing measurement-state, uncertainty, stimulus-pin,
  registration and spatial-campaign authorities instead of duplicating
  them.
* ``evaluate_verification_freshness`` — retest/staleness lifecycle after
  equipment, firmware, DSP, position, room, seating, or topology changes.
* ``build_commissioning_report`` — a reproducible, sealed evidence
  package stating exactly what was verified, by what revision, with which
  limitations.

Source note (public material): CEDIA publicly describes RP32 as
"objective, repeatable methods for measuring and verifying audio system
performance", verifying a system against RP22 parameters to demonstrate
whether the targeted RP22 level was achieved (CEDIA Expo session
descriptions and the ISE/CEDIA RP32 workshop pages, retrieved 2026-10).
Public pages do not expose clause-level procedures or thresholds, so the
bundled profile ships zero asserted clause mappings — the mapping
framework and every honesty state exist, but nothing is claimed without
an exact reviewed source.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import canonical_sha256 as _digest


RP32_PROFILE_SCHEMA_VERSION = 1
RP32_PROFILE_AUTHORITY_VERSION = 'rev56-rp32-profile-1'
RP32_PLAN_AUTHORITY_VERSION = 'rev56-rp32-plan-1'
RP32_RECORD_AUTHORITY_VERSION = 'rev56-rp32-record-1'
RP32_READINESS_ALGORITHM_VERSION = 'rev56-rp32-readiness-1'
RP32_RECONCILIATION_ALGORITHM_VERSION = 'rev56-rp32-reconciliation-1'
RP32_FRESHNESS_ALGORITHM_VERSION = 'rev56-rp32-freshness-1'
RP32_REPORT_AUTHORITY_VERSION = 'rev56-rp32-report-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _parse_timestamp(value: str, label: str):
    from datetime import datetime

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')
    return parsed


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f'{label} must be unique')


# --- external document identity ---------------------------------------------


class CommissioningDocumentIdentity(BaseModel):
    """Exact identity of the external commissioning profile (§RP32-10).

    ``document_sha256`` + ``source_access_kind`` keep provenance honest: a
    profile built from a public announcement says so, and cannot later be
    confused with one built from the full purchased document.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    publisher: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    source_uri: str | None = Field(default=None, min_length=1)
    source_access_kind: Literal[
        'full_document', 'excerpt', 'public_announcement', 'workshop_summary'
    ]
    document_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    license_note: str | None = Field(default=None, min_length=1)
    parser_version: str = Field(min_length=1)
    related_rp22_revision: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def honest_provenance(self) -> 'CommissioningDocumentIdentity':
        if self.source_access_kind == 'full_document' and (
            self.document_sha256 is None
        ):
            raise ValueError(
                'a full-document identity must pin the document sha256'
            )
        return self


# --- requirement domains and clause mapping ---------------------------------

Rp32CapabilityDomain = Literal[
    'measurement_environment',
    'instrumentation',
    'calibration',
    'stimulus',
    'measurement_positions',
    'frequency_response',
    'level_alignment',
    'delay_distance',
    'subwoofer_integration',
    'seat_to_seat',
    'noise_floor',
    'dynamic_state',
    'documentation_report',
]

MappingStatus = Literal[
    'supported',
    'supported_with_limitations',
    'manual_evidence_required',
    'external_tool_required',
    'unsupported',
    'not_applicable',
    'unmapped',
]

#: Domains HTDT knows how to talk about at all. Every domain ships in the
#: bundled profile; the *mapping status* records what is honestly
#: deliverable today.
RP32_CAPABILITY_DOMAINS: tuple[Rp32CapabilityDomain, ...] = (
    'measurement_environment',
    'instrumentation',
    'calibration',
    'stimulus',
    'measurement_positions',
    'frequency_response',
    'level_alignment',
    'delay_distance',
    'subwoofer_integration',
    'seat_to_seat',
    'noise_floor',
    'dynamic_state',
    'documentation_report',
)


class Rp32RequirementMapping(BaseModel):
    """One RP32 requirement → HTDT authority mapping.

    ``clause_id`` is recorded only when the exact source text was lawfully
    reviewed; a mapping may exist at *domain* granularity with
    ``clause_id=None`` while the document remains unreviewed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    requirement_id: str = Field(min_length=1)
    domain: Rp32CapabilityDomain
    clause_id: str | None = Field(default=None, min_length=1)
    status: MappingStatus
    # HTDT authorities this requirement is satisfied by, when supported.
    authority_refs: tuple[str, ...] = ()
    limitation_note: str | None = Field(default=None, min_length=1)
    # Evidence describing how the mapping was reviewed (source excerpt ref,
    # reviewer, date) — required for any non-unmapped/non-unsupported claim.
    review_evidence: str | None = Field(default=None, min_length=1)

    @field_validator('authority_refs')
    @classmethod
    def canonical_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'authority refs')
        if any(not value for value in values):
            raise ValueError('authority refs must not be empty')
        return tuple(sorted(values))

    @model_validator(mode='after')
    def honest_mapping(self) -> 'Rp32RequirementMapping':
        if self.status in {'supported', 'supported_with_limitations'}:
            if not self.authority_refs:
                raise ValueError(
                    'supported mappings must name the satisfying authorities'
                )
            if self.review_evidence is None:
                raise ValueError(
                    'supported mappings require recorded review evidence'
                )
        if self.status == 'supported_with_limitations' and (
            self.limitation_note is None
        ):
            raise ValueError(
                'supported_with_limitations requires the limitation note'
            )
        if self.status in {'unmapped', 'unsupported'} and self.authority_refs:
            raise ValueError(
                f'{self.status} mappings cannot name satisfying authorities'
            )
        if self.clause_id is not None and self.review_evidence is None:
            raise ValueError(
                'a clause-level mapping requires recorded review evidence'
            )
        return self


ClauseMappingState = Literal[
    'unpopulated_pending_lawful_source',
    'partially_mapped',
    'mapped',
]


class Rp32CommissioningProfile(BaseModel):
    """Sealed commissioning profile authority (issue #585 goal).

    ``clause_mapping_state`` is derived, never asserted: with zero clause
    mappings the profile reports itself honestly as
    ``unpopulated_pending_lawful_source``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = RP32_PROFILE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-rp32-profile-1'
    ] = RP32_PROFILE_AUTHORITY_VERSION

    profile_id: str = Field(pattern=r'^rp32-profile:[0-9a-f]{64}$')
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    document: CommissioningDocumentIdentity
    requirement_mappings: tuple[Rp32RequirementMapping, ...]
    # Derived mapping state, stored as a first-class column so the
    # row-level payload agreement check covers it; the validator recomputes
    # it from the mappings and rejects anything else.
    clause_mapping_state: ClauseMappingState
    created_at_utc: str = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @field_validator('requirement_mappings')
    @classmethod
    def canonical_mappings(
        cls, values: tuple[Rp32RequirementMapping, ...]
    ) -> tuple[Rp32RequirementMapping, ...]:
        ids = [mapping.requirement_id for mapping in values]
        if len(ids) != len(set(ids)):
            raise ValueError('requirement ids must be unique')
        return tuple(sorted(values, key=lambda mapping: mapping.requirement_id))

    @model_validator(mode='after')
    def valid_profile(self) -> 'Rp32CommissioningProfile':
        _require_iso8601(self.created_at_utc, 'profile created_at_utc')
        expected = _digest(self.semantic_payload())
        if self.profile_sha256 != expected:
            raise ValueError('Rp32CommissioningProfile semantic hash mismatch')
        if self.profile_id != f'rp32-profile:{expected}':
            raise ValueError('Rp32CommissioningProfile id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'profile_id', 'profile_sha256'}
        )

    @model_validator(mode='after')
    def honest_mapping_state(self) -> 'Rp32CommissioningProfile':
        if self.clause_mapping_state != self._derived_mapping_state():
            raise ValueError(
                'clause_mapping_state must be the derived value, never an '
                'asserted one'
            )
        return self

    def _derived_mapping_state(self) -> ClauseMappingState:
        mapped = [
            m
            for m in self.requirement_mappings
            if m.clause_id is not None and m.status not in {'unmapped'}
        ]
        if not mapped:
            return 'unpopulated_pending_lawful_source'
        if any(
            m.status == 'unmapped' or m.clause_id is None
            for m in self.requirement_mappings
        ):
            return 'partially_mapped'
        return 'mapped'

    def mappings_for_domain(
        self, domain: Rp32CapabilityDomain
    ) -> tuple[Rp32RequirementMapping, ...]:
        return tuple(
            mapping
            for mapping in self.requirement_mappings
            if mapping.domain == domain
        )

    def is_rp32_claimable(self) -> bool:
        """Whether any 'RP32 verified' claim is currently supportable."""

        return (
            self.document.source_access_kind == 'full_document'
            and self.clause_mapping_state == 'mapped'
        )


def rp32_builtin_profile(*, created_at_utc: str) -> Rp32CommissioningProfile:
    """The bundled RP32 profile — honest public-announcement scope.

    Ships a requirement *domain* skeleton with every entry ``unmapped``
    because CEDIA has not published clause-level RP32 content in the public
    material reviewed (Expo/ISE workshop descriptions). No clause text is
    invented.
    """

    document = CommissioningDocumentIdentity(
        publisher='CEDIA',
        document_id='RP32',
        title='Audio System Measurement and Verification',
        revision='announced-2024',
        source_uri='https://cediaexpo.com/',
        source_access_kind='public_announcement',
        document_sha256=None,
        license_note=(
            'RP32 document content is licensed CEDIA material and is not '
            'bundled; clause-level mappings stay unpopulated until the exact '
            'document is lawfully reviewed.'
        ),
        parser_version='rp32-identity-1',
        related_rp22_revision='RP22 (2024)',
        note=(
            'Identity and scope from public CEDIA/ISE announcements: RP32 '
            'provides objective, repeatable methods for measuring and '
            'verifying audio system performance against RP22 parameters.'
        ),
    )
    mappings = tuple(
        Rp32RequirementMapping(
            requirement_id=f'rp32-domain-{domain}',
            domain=domain,
            clause_id=None,
            status='unmapped',
        )
        for domain in RP32_CAPABILITY_DOMAINS
    )
    return build_rp32_profile(
        document=document,
        requirement_mappings=mappings,
        created_at_utc=created_at_utc,
        note='Bundled profile — zero asserted clause mappings.',
    )


def build_rp32_profile(
    *,
    document: CommissioningDocumentIdentity,
    requirement_mappings: Sequence[Rp32RequirementMapping],
    created_at_utc: str,
    note: str | None = None,
) -> Rp32CommissioningProfile:
    payload: dict[str, Any] = {
        'schema_version': RP32_PROFILE_SCHEMA_VERSION,
        'authority_version': RP32_PROFILE_AUTHORITY_VERSION,
        'document': document.model_dump(mode='json'),
        'requirement_mappings': [
            mapping.model_dump(mode='json')
            for mapping in sorted(
                requirement_mappings, key=lambda m: m.requirement_id
            )
        ],
        'created_at_utc': created_at_utc,
        'note': note,
    }
    sorted_mappings = tuple(
        sorted(requirement_mappings, key=lambda m: m.requirement_id)
    )
    mapped = [
        m
        for m in sorted_mappings
        if m.clause_id is not None and m.status not in {'unmapped'}
    ]
    if not mapped:
        mapping_state: ClauseMappingState = (
            'unpopulated_pending_lawful_source'
        )
    elif any(
        m.status == 'unmapped' or m.clause_id is None
        for m in sorted_mappings
    ):
        mapping_state = 'partially_mapped'
    else:
        mapping_state = 'mapped'
    payload['requirement_mappings'] = [
        m.model_dump(mode='json') for m in sorted_mappings
    ]
    payload['clause_mapping_state'] = mapping_state
    digest = _digest(payload)
    return Rp32CommissioningProfile(
        schema_version=RP32_PROFILE_SCHEMA_VERSION,
        authority_version=RP32_PROFILE_AUTHORITY_VERSION,
        profile_id=f'rp32-profile:{digest}',
        profile_sha256=digest,
        document=document,
        requirement_mappings=sorted_mappings,
        clause_mapping_state=mapping_state,
        created_at_utc=created_at_utc,
        note=note,
    )


# --- design target vs as-built -----------------------------------------------


class DesignTargetBinding(BaseModel):
    """What the project was designed to (§RP32-20).

    Pins the scene revision, system variant, RP22 revision+level, approved
    assumptions and listening area that verification is measured against.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = Field(default=None, min_length=1)
    rp22_profile_id: str | None = Field(default=None, min_length=1)
    rp22_revision: str = Field(min_length=1)
    rp22_level: str = Field(min_length=1)
    listening_area_design_id: str | None = Field(default=None, min_length=1)
    listening_area_design_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    expected_speaker_count: int = Field(ge=0)
    expected_subwoofer_count: int = Field(ge=0)
    expected_channel_labels: tuple[str, ...] = ()
    approved_equipment_ids: tuple[str, ...] = ()
    approved_assumptions: tuple[str, ...] = ()

    @field_validator(
        'expected_channel_labels', 'approved_equipment_ids',
        'approved_assumptions',
    )
    @classmethod
    def canonical_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'binding list')
        if any(not value for value in values):
            raise ValueError('binding list entries must not be empty')
        return tuple(sorted(values))

    @model_validator(mode='after')
    def paired_area(self) -> 'DesignTargetBinding':
        if (self.listening_area_design_id is None) != (
            self.listening_area_design_sha256 is None
        ):
            raise ValueError(
                'listening-area design id/sha must be supplied together'
            )
        return self


class AsBuiltObservation(BaseModel):
    """What was actually found in the room (§RP32-20 input)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    observed_speaker_count: int | None = Field(default=None, ge=0)
    observed_subwoofer_count: int | None = Field(default=None, ge=0)
    observed_channel_labels: tuple[str, ...] | None = None
    observed_equipment_ids: tuple[str, ...] | None = None
    observed_seating_positions_declared: bool = True
    room_matches_design: bool | None = None
    note: str | None = Field(default=None, min_length=1)


ReconciliationState = Literal[
    'reconciled', 'reconciled_with_findings', 'incompatible',
    'insufficient_evidence',
]


class AsBuiltReconciliation(BaseModel):
    """Sealed designed-vs-built comparison (§RP32-20).

    Measurement against the wrong physical configuration is meaningless:
    a hard mismatch blocks verification rather than producing a quiet
    mis-verification.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-rp32-reconciliation-1'
    ] = RP32_RECONCILIATION_ALGORITHM_VERSION

    reconciliation_id: str = Field(
        pattern=r'^rp32-reconciliation:[0-9a-f]{64}$'
    )
    reconciliation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    target: DesignTargetBinding
    state: ReconciliationState
    findings: tuple[str, ...]
    unknown_fields: tuple[str, ...]
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_reconciliation(self) -> 'AsBuiltReconciliation':
        _require_iso8601(
            self.evaluated_at_utc, 'reconciliation evaluated_at_utc'
        )
        if self.state == 'incompatible' and not self.findings:
            raise ValueError('incompatible reconciliations require findings')
        if self.state == 'insufficient_evidence' and not self.unknown_fields:
            raise ValueError(
                'insufficient_evidence reconciliations require unknown fields'
            )
        expected = _digest(self.semantic_payload())
        if self.reconciliation_sha256 != expected:
            raise ValueError('AsBuiltReconciliation semantic hash mismatch')
        if self.reconciliation_id != f'rp32-reconciliation:{expected}':
            raise ValueError('AsBuiltReconciliation id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'reconciliation_id', 'reconciliation_sha256'},
        )


def evaluate_designed_built_reconciliation(
    *,
    document_id: str,
    target: DesignTargetBinding,
    as_built: AsBuiltObservation,
    evaluated_at_utc: str,
) -> AsBuiltReconciliation:
    """Fail-closed designed-vs-built check before measurement (§RP32-20)."""

    findings: list[str] = []
    unknown: list[str] = []

    def compare(name: str, expected, observed, finding: str) -> None:
        if observed is None:
            unknown.append(name)
        elif observed != expected:
            findings.append(finding)

    compare(
        'speaker_count',
        target.expected_speaker_count,
        as_built.observed_speaker_count,
        'speaker_count_mismatch',
    )
    compare(
        'subwoofer_count',
        target.expected_subwoofer_count,
        as_built.observed_subwoofer_count,
        'subwoofer_count_mismatch',
    )
    compare(
        'channel_labels',
        target.expected_channel_labels,
        (
            tuple(sorted(as_built.observed_channel_labels))
            if as_built.observed_channel_labels is not None
            else None
        ),
        'channel_topology_mismatch',
    )
    if target.approved_equipment_ids:
        compare(
            'equipment_ids',
            target.approved_equipment_ids,
            (
                tuple(sorted(as_built.observed_equipment_ids))
                if as_built.observed_equipment_ids is not None
                else None
            ),
            'equipment_mismatch',
        )
    if as_built.room_matches_design is None:
        unknown.append('room_geometry')
    elif not as_built.room_matches_design:
        findings.append('room_geometry_mismatch')
    if not as_built.observed_seating_positions_declared:
        findings.append('seating_positions_undeclared')

    if findings:
        state: ReconciliationState = 'incompatible'
    elif unknown:
        state = 'insufficient_evidence'
    elif as_built.note:
        state = 'reconciled_with_findings'
    else:
        state = 'reconciled'

    payload: dict[str, Any] = {
        'authority_version': RP32_RECONCILIATION_ALGORITHM_VERSION,
        'document_id': document_id,
        'target': target.model_dump(mode='json'),
        'state': state,
        'findings': tuple(sorted(set(findings))),
        'unknown_fields': tuple(sorted(set(unknown))),
        'evaluated_at_utc': evaluated_at_utc,
    }
    digest = _digest(payload)
    return AsBuiltReconciliation(
        authority_version=RP32_RECONCILIATION_ALGORITHM_VERSION,
        reconciliation_id=f'rp32-reconciliation:{digest}',
        reconciliation_sha256=digest,
        document_id=document_id,
        target=target,
        state=state,
        findings=tuple(sorted(set(findings))),
        unknown_fields=tuple(sorted(set(unknown))),
        evaluated_at_utc=evaluated_at_utc,
    )


# --- instrument evidence ------------------------------------------------------


class InstrumentEvidence(BaseModel):
    """Instrument/calibration evidence binding (§RP32-30).

    Unknown RP32 instrument requirements stay UNKNOWN: an instrument bound
    without calibration evidence never upgrades a requirement verdict.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    instrument_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    calibration_certificate_id: str | None = Field(default=None, min_length=1)
    calibration_due_utc: str | None = None
    calibrator_check_ids: tuple[str, ...] = ()
    serial: str | None = Field(default=None, min_length=1)

    @field_validator('calibrator_check_ids')
    @classmethod
    def canonical_checks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'calibrator check ids')
        return tuple(sorted(values))

    @model_validator(mode='after')
    def valid_instrument(self) -> 'InstrumentEvidence':
        if self.calibration_due_utc is not None:
            _require_iso8601(
                self.calibration_due_utc, 'calibration_due_utc'
            )
        return self

    def calibration_state(self, *, at_utc: str) -> str:
        """'calibrated' | 'expired' | 'unknown' at a point in time."""

        if self.calibration_certificate_id is None:
            return 'unknown'
        if self.calibration_due_utc is not None:
            due = _parse_timestamp(
                self.calibration_due_utc, 'calibration_due_utc'
            )
            if _parse_timestamp(at_utc, 'at_utc') > due:
                return 'expired'
        return 'calibrated'


# --- verification plan --------------------------------------------------------


class Rp32TaskSpec(BaseModel):
    """One verification task in the generated measurement plan (§RP32-35).

    ``rp32_clause_ids`` stays empty while the profile is unpopulated — the
    task is then an HTDT commissioning task, not an "RP32-required" item.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    task_id: str = Field(min_length=1)
    domain: Rp32CapabilityDomain
    rp32_clause_ids: tuple[str, ...] = ()
    description: str = Field(min_length=1)
    required_instrument_roles: tuple[str, ...] = ()
    required_observable: str | None = Field(default=None, min_length=1)
    measurement_plan_ref: str | None = Field(default=None, min_length=1)

    @field_validator('rp32_clause_ids', 'required_instrument_roles')
    @classmethod
    def canonical_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'task id list')
        return tuple(sorted(values))


class Rp32VerificationPlan(BaseModel):
    """Sealed verification plan: profile × target × spatial campaign (§RP32-35)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-rp32-plan-1'
    ] = RP32_PLAN_AUTHORITY_VERSION

    plan_id: str = Field(pattern=r'^rp32-plan:[0-9a-f]{64}$')
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    profile_id: str = Field(pattern=r'^rp32-profile:[0-9a-f]{64}$')
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    target: DesignTargetBinding
    spatial_design_id: str | None = Field(default=None, min_length=1)
    spatial_design_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    tasks: tuple[Rp32TaskSpec, ...]
    instrument_evidence: tuple[InstrumentEvidence, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @field_validator('tasks')
    @classmethod
    def canonical_tasks(
        cls, values: tuple[Rp32TaskSpec, ...]
    ) -> tuple[Rp32TaskSpec, ...]:
        ids = [task.task_id for task in values]
        if len(ids) != len(set(ids)):
            raise ValueError('task ids must be unique')
        return tuple(sorted(values, key=lambda task: task.task_id))

    @model_validator(mode='after')
    def valid_plan(self) -> 'Rp32VerificationPlan':
        _require_iso8601(self.created_at_utc, 'plan created_at_utc')
        if (self.spatial_design_id is None) != (
            self.spatial_design_sha256 is None
        ):
            raise ValueError(
                'spatial design id/sha must be supplied together'
            )
        expected = _digest(self.semantic_payload())
        if self.plan_sha256 != expected:
            raise ValueError('Rp32VerificationPlan semantic hash mismatch')
        if self.plan_id != f'rp32-plan:{expected}':
            raise ValueError('Rp32VerificationPlan id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'plan_id', 'plan_sha256'}
        )


def build_verification_plan(
    *,
    document_id: str,
    profile: Rp32CommissioningProfile,
    target: DesignTargetBinding,
    tasks: Sequence[Rp32TaskSpec],
    created_at_utc: str,
    spatial_design_id: str | None = None,
    spatial_design_sha256: str | None = None,
    instrument_evidence: Sequence[InstrumentEvidence] = (),
) -> Rp32VerificationPlan:
    """Seal a verification plan.

    Fail-closed honesty: a task may claim RP32 clause coverage only when
    the profile actually carries a mapping for that clause.
    """

    mapped_clauses = {
        mapping.clause_id
        for mapping in profile.requirement_mappings
        if mapping.clause_id is not None
        and mapping.status in {'supported', 'supported_with_limitations'}
    }
    for task in tasks:
        unmapped = [
            clause for clause in task.rp32_clause_ids
            if clause not in mapped_clauses
        ]
        if unmapped:
            raise ValueError(
                f'task {task.task_id} claims clauses the profile does not '
                f'support: {sorted(unmapped)}'
            )
    payload: dict[str, Any] = {
        'authority_version': RP32_PLAN_AUTHORITY_VERSION,
        'document_id': document_id,
        'profile_id': profile.profile_id,
        'profile_sha256': profile.profile_sha256,
        'target': target.model_dump(mode='json'),
        'spatial_design_id': spatial_design_id,
        'spatial_design_sha256': spatial_design_sha256,
        'tasks': [
            task.model_dump(mode='json')
            for task in sorted(tasks, key=lambda t: t.task_id)
        ],
        'instrument_evidence': [
            evidence.model_dump(mode='json') for evidence in instrument_evidence
        ],
        'created_at_utc': created_at_utc,
    }
    digest = _digest(payload)
    return Rp32VerificationPlan(
        authority_version=RP32_PLAN_AUTHORITY_VERSION,
        plan_id=f'rp32-plan:{digest}',
        plan_sha256=digest,
        document_id=document_id,
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        target=target,
        spatial_design_id=spatial_design_id,
        spatial_design_sha256=spatial_design_sha256,
        tasks=tuple(sorted(tasks, key=lambda t: t.task_id)),
        instrument_evidence=tuple(instrument_evidence),
        created_at_utc=created_at_utc,
    )


# --- readiness gate ------------------------------------------------------------


ReadinessState = Literal[
    'ready', 'ready_with_limitations', 'incompatible', 'insufficient_evidence'
]


class Rp32ReadinessAssessment(BaseModel):
    """Fail-closed readiness gate composing existing authorities (§RP32-40).

    Consumes — does not duplicate — the spatial-campaign evaluation,
    measurement-state snapshots, uncertainty budgets, stimulus pins and
    registration partitions.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-rp32-readiness-1'
    ] = RP32_READINESS_ALGORITHM_VERSION

    assessment_id: str = Field(pattern=r'^rp32-readiness:[0-9a-f]{64}$')
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r'^rp32-plan:[0-9a-f]{64}$')
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    state: ReadinessState
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    unknowns: tuple[str, ...]
    assessed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'Rp32ReadinessAssessment':
        _require_iso8601(self.assessed_at_utc, 'readiness assessed_at_utc')
        if self.state == 'incompatible' and not self.reasons:
            raise ValueError('incompatible readiness requires reasons')
        if self.state == 'insufficient_evidence' and not self.unknowns:
            raise ValueError(
                'insufficient_evidence readiness requires unknowns'
            )
        if self.state == 'ready_with_limitations' and not self.limitations:
            raise ValueError('ready_with_limitations requires limitations')
        if self.state == 'ready' and (
            self.reasons or self.limitations or self.unknowns
        ):
            raise ValueError('ready assessments carry no flags')
        expected = _digest(self.semantic_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('Rp32ReadinessAssessment semantic hash mismatch')
        if self.assessment_id != f'rp32-readiness:{expected}':
            raise ValueError('Rp32ReadinessAssessment id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'assessment_id', 'assessment_sha256'}
        )


def evaluate_measurement_readiness(
    plan: Rp32VerificationPlan,
    *,
    reconciliation: AsBuiltReconciliation | None = None,
    spatial_evaluation_state: str | None = None,
    measurement_state_ready: bool | None = None,
    uncertainty_budgets_present: bool | None = None,
    stimulus_pins_present: bool | None = None,
    assessed_at_utc: str,
) -> Rp32ReadinessAssessment:
    """Compose the readiness verdict (§RP32-40).

    - ``reconciliation``: result of
      ``evaluate_designed_built_reconciliation`` (None = not performed).
    - ``spatial_evaluation_state``: ``CampaignDesignEvaluation.state``
      from ``cad_spatial_campaign`` (None = no spatial design bound).
    - ``measurement_state_ready`` / ``uncertainty_budgets_present`` /
      ``stimulus_pins_present``: caller-summarized signals from #573/#572/
      #574 authorities (None = not assessed → INSUFFICIENT_EVIDENCE).
    """

    reasons: list[str] = []
    limitations: list[str] = []
    unknowns: list[str] = []

    if plan.spatial_design_id is None:
        reasons.append('no_spatial_campaign_bound')
    elif spatial_evaluation_state is None:
        unknowns.append('spatial_evaluation')
    elif spatial_evaluation_state == 'invalid':
        reasons.append('spatial_design_invalid')
    elif spatial_evaluation_state == 'valid_with_warnings':
        limitations.append('spatial_design_warnings')

    if reconciliation is None:
        unknowns.append('designed_built_reconciliation')
    elif reconciliation.state == 'incompatible':
        reasons.append('as_built_incompatible')
    elif reconciliation.state == 'insufficient_evidence':
        unknowns.append('as_built_reconciliation')
    elif reconciliation.state == 'reconciled_with_findings':
        limitations.append('as_built_findings')

    if measurement_state_ready is None:
        unknowns.append('measurement_state')
    elif not measurement_state_ready:
        reasons.append('measurement_state_not_ready')

    if uncertainty_budgets_present is None:
        unknowns.append('uncertainty_budgets')
    elif not uncertainty_budgets_present:
        limitations.append('uncertainty_budgets_missing')

    if stimulus_pins_present is None:
        unknowns.append('stimulus_pins')
    elif not stimulus_pins_present:
        limitations.append('stimulus_pins_missing')

    for evidence in plan.instrument_evidence:
        state = evidence.calibration_state(at_utc=assessed_at_utc)
        if state == 'unknown':
            unknowns.append(f'instrument_calibration:{evidence.instrument_id}')
        elif state == 'expired':
            reasons.append(
                f'instrument_calibration_expired:{evidence.instrument_id}'
            )

    if reasons:
        state: ReadinessState = 'incompatible'
    elif unknowns:
        state = 'insufficient_evidence'
    elif limitations:
        state = 'ready_with_limitations'
    else:
        state = 'ready'

    payload: dict[str, Any] = {
        'authority_version': RP32_READINESS_ALGORITHM_VERSION,
        'plan_id': plan.plan_id,
        'plan_sha256': plan.plan_sha256,
        'document_id': plan.document_id,
        'state': state,
        'reasons': tuple(sorted(set(reasons))),
        'limitations': tuple(sorted(set(limitations))),
        'unknowns': tuple(sorted(set(unknowns))),
        'assessed_at_utc': assessed_at_utc,
    }
    digest = _digest(payload)
    return Rp32ReadinessAssessment(
        authority_version=RP32_READINESS_ALGORITHM_VERSION,
        assessment_id=f'rp32-readiness:{digest}',
        assessment_sha256=digest,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        document_id=plan.document_id,
        state=state,
        reasons=tuple(sorted(set(reasons))),
        limitations=tuple(sorted(set(limitations))),
        unknowns=tuple(sorted(set(unknowns))),
        assessed_at_utc=assessed_at_utc,
    )


# --- exceptions, records, freshness, report -------------------------------------


class CommissioningException(BaseModel):
    """An accepted deviation on a verification item (§RP32-45).

    Client acceptance is recorded but never upgrades the technical result:
    the underlying task verdict keeps its own honest state.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    exception_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    accepted_by: str | None = Field(default=None, min_length=1)
    client_acceptance: bool = False
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_exception(self) -> 'CommissioningException':
        _require_iso8601(self.recorded_at_utc, 'exception recorded_at_utc')
        return self


VerificationItemState = Literal[
    'pass', 'fail', 'indeterminate', 'insufficient_evidence',
    'not_applicable', 'unverified',
]

Rp22VerificationState = Literal[
    'rp22_design_target',
    'rp22_as_built_predicted',
    'rp22_rp32_measured_verified',
    'rp22_rp32_measured_verified_with_limitations',
    'rp22_rp32_measured_failed',
    'rp22_verification_incomplete',
]

VerificationOverallState = Literal[
    'verified', 'verified_with_limitations', 'failed',
    'incomplete', 'inconclusive',
]


class Rp32VerificationItemResult(BaseModel):
    """One task's measured outcome (§RP32-40/45)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    task_id: str = Field(min_length=1)
    state: VerificationItemState
    evidence_kind: Literal[
        'measured', 'derived', 'predicted', 'unknown', 'ineligible'
    ]
    measurement_ids: tuple[str, ...] = ()
    uncertainty_budget_id: str | None = Field(default=None, min_length=1)
    commissioning_check_id: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @field_validator('measurement_ids')
    @classmethod
    def canonical_measurements(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'measurement ids')
        return tuple(sorted(values))

    @model_validator(mode='after')
    def honest_result(self) -> 'Rp32VerificationItemResult':
        if self.state == 'pass' and self.evidence_kind in {'unknown'}:
            raise ValueError('a pass cannot rest on unknown evidence')
        if self.evidence_kind == 'measured' and not self.measurement_ids:
            raise ValueError(
                'measured results must name the backing measurements'
            )
        return self


class Rp32VerificationRecord(BaseModel):
    """Sealed record of one verification attempt (§RP32-40/50).

    ``prior_record_ids`` chains attempts so a failed baseline is never
    silently replaced — remeasurement adds a record, it does not rewrite
    the old one.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-rp32-record-1'
    ] = RP32_RECORD_AUTHORITY_VERSION

    record_id: str = Field(pattern=r'^rp32-record:[0-9a-f]{64}$')
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_id: str = Field(pattern=r'^rp32-plan:[0-9a-f]{64}$')
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)

    readiness_assessment_id: str = Field(min_length=1)
    reconciliation_id: str | None = Field(default=None, min_length=1)
    item_results: tuple[Rp32VerificationItemResult, ...]
    exceptions: tuple[CommissioningException, ...] = ()
    prior_record_ids: tuple[str, ...] = ()
    overall_state: VerificationOverallState
    rp22_state: Rp22VerificationState
    completed_at_utc: str = Field(min_length=1)

    @field_validator('item_results')
    @classmethod
    def canonical_results(
        cls, values: tuple[Rp32VerificationItemResult, ...]
    ) -> tuple[Rp32VerificationItemResult, ...]:
        ids = [item.task_id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError('item result task ids must be unique')
        return tuple(sorted(values, key=lambda item: item.task_id))

    @field_validator('prior_record_ids')
    @classmethod
    def canonical_priors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, 'prior record ids')
        return tuple(sorted(values))

    @model_validator(mode='after')
    def valid_record(self) -> 'Rp32VerificationRecord':
        _require_iso8601(self.completed_at_utc, 'record completed_at_utc')
        task_ids = {item.task_id for item in self.item_results}
        for exception in self.exceptions:
            if exception.task_id not in task_ids:
                raise ValueError(
                    f'exception names unknown task {exception.task_id!r}'
                )
        expected = _digest(self.semantic_payload())
        if self.record_sha256 != expected:
            raise ValueError('Rp32VerificationRecord semantic hash mismatch')
        if self.record_id != f'rp32-record:{expected}':
            raise ValueError('Rp32VerificationRecord id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'record_id', 'record_sha256'}
        )


def build_verification_record(
    *,
    plan: Rp32VerificationPlan,
    readiness_assessment_id: str,
    item_results: Sequence[Rp32VerificationItemResult],
    completed_at_utc: str,
    reconciliation_id: str | None = None,
    exceptions: Sequence[CommissioningException] = (),
    prior_record_ids: Sequence[str] = (),
) -> Rp32VerificationRecord:
    """Seal one verification attempt and derive its overall + RP22 state."""

    plan_task_ids = {task.task_id for task in plan.tasks}
    result_task_ids = {item.task_id for item in item_results}
    unknown_results = result_task_ids - plan_task_ids
    if unknown_results:
        raise ValueError(
            f'item results name tasks outside the plan: '
            f'{sorted(unknown_results)}'
        )
    missing = plan_task_ids - result_task_ids

    states = {item.state for item in item_results}
    if 'fail' in states:
        overall: VerificationOverallState = 'failed'
    elif missing or states & {'unverified', 'insufficient_evidence'}:
        overall = 'incomplete'
    elif states & {'indeterminate'}:
        overall = 'inconclusive'
    elif any(
        exception.client_acceptance for exception in exceptions
    ) or states & {'not_applicable'}:
        overall = 'verified_with_limitations'
    else:
        overall = 'verified'

    if overall == 'verified':
        rp22_state: Rp22VerificationState = 'rp22_rp32_measured_verified'
    elif overall == 'verified_with_limitations':
        rp22_state = 'rp22_rp32_measured_verified_with_limitations'
    elif overall == 'failed':
        rp22_state = 'rp22_rp32_measured_failed'
    else:
        rp22_state = 'rp22_verification_incomplete'

    payload: dict[str, Any] = {
        'authority_version': RP32_RECORD_AUTHORITY_VERSION,
        'plan_id': plan.plan_id,
        'plan_sha256': plan.plan_sha256,
        'document_id': plan.document_id,
        'readiness_assessment_id': readiness_assessment_id,
        'reconciliation_id': reconciliation_id,
        'item_results': [
            item.model_dump(mode='json')
            for item in sorted(item_results, key=lambda i: i.task_id)
        ],
        'exceptions': [
            exception.model_dump(mode='json') for exception in exceptions
        ],
        'prior_record_ids': tuple(sorted(set(prior_record_ids))),
        'overall_state': overall,
        'rp22_state': rp22_state,
        'completed_at_utc': completed_at_utc,
    }
    digest = _digest(payload)
    return Rp32VerificationRecord(
        authority_version=RP32_RECORD_AUTHORITY_VERSION,
        record_id=f'rp32-record:{digest}',
        record_sha256=digest,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        document_id=plan.document_id,
        readiness_assessment_id=readiness_assessment_id,
        reconciliation_id=reconciliation_id,
        item_results=tuple(
            sorted(item_results, key=lambda i: i.task_id)
        ),
        exceptions=tuple(exceptions),
        prior_record_ids=tuple(sorted(set(prior_record_ids))),
        overall_state=overall,
        rp22_state=rp22_state,
        completed_at_utc=completed_at_utc,
    )


# --- freshness / retest lifecycle ----------------------------------------------


FreshnessState = Literal['fresh', 'stale', 'insufficient_evidence']

#: Change events that invalidate a verification record (§RP32-55).
STALENESS_TRIGGERS: tuple[str, ...] = (
    'equipment_changed',
    'firmware_changed',
    'dsp_configuration_changed',
    'speaker_position_changed',
    'microphone_position_changed',
    'room_geometry_changed',
    'seating_changed',
    'channel_topology_changed',
    'calibration_expired',
)


class VerificationFreshnessReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-rp32-freshness-1'
    ] = RP32_FRESHNESS_ALGORITHM_VERSION
    record_id: str
    state: FreshnessState
    staleness_reasons: tuple[str, ...]
    assessed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_freshness(self) -> 'VerificationFreshnessReport':
        _require_iso8601(self.assessed_at_utc, 'freshness assessed_at_utc')
        if self.state == 'stale' and not self.staleness_reasons:
            raise ValueError('stale freshness requires reasons')
        return self


def evaluate_verification_freshness(
    record: Rp32VerificationRecord,
    *,
    change_events: Iterable[str],
    assessed_at_utc: str,
) -> VerificationFreshnessReport:
    """Staleness check: listed change events invalidate the record.

    ``change_events`` are caller-supplied tags (from ``STALENESS_TRIGGERS``)
    describing what changed since the record completed. Unknown event tags
    are themselves flagged — a retest regime cannot quietly ignore them.
    """

    known = set(STALENESS_TRIGGERS)
    reasons: list[str] = []
    unknown: list[str] = []
    for event in change_events:
        if event in known:
            reasons.append(event)
        else:
            unknown.append(f'unrecognized_change_event:{event}')
    if unknown:
        state: FreshnessState = 'insufficient_evidence'
        reasons.extend(unknown)
    elif reasons:
        state = 'stale'
    else:
        state = 'fresh'
    return VerificationFreshnessReport(
        authority_version=RP32_FRESHNESS_ALGORITHM_VERSION,
        record_id=record.record_id,
        state=state,
        staleness_reasons=tuple(sorted(set(reasons))),
        assessed_at_utc=assessed_at_utc,
    )


# --- reproducible evidence package ----------------------------------------------


class CommissioningReport(BaseModel):
    """Sealed, reproducible evidence package for the client (§RP32-50/60).

    States exactly what was verified, by which exact profile revision,
    against which design target, with which limitations — and says so even
    when the profile carries no clause mappings.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rev56-rp32-report-1'
    ] = RP32_REPORT_AUTHORITY_VERSION

    report_id: str = Field(pattern=r'^rp32-report:[0-9a-f]{64}$')
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    profile_id: str = Field(pattern=r'^rp32-profile:[0-9a-f]{64}$')
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    clause_mapping_state: ClauseMappingState
    plan_id: str = Field(pattern=r'^rp32-plan:[0-9a-f]{64}$')
    record_id: str = Field(pattern=r'^rp32-record:[0-9a-f]{64}$')
    overall_state: VerificationOverallState
    rp22_state: Rp22VerificationState
    item_summary: dict[str, int]
    limitations: tuple[str, ...]
    claim_text: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_report(self) -> 'CommissioningReport':
        _require_iso8601(self.generated_at_utc, 'report generated_at_utc')
        expected = _digest(self.semantic_payload())
        if self.report_sha256 != expected:
            raise ValueError('CommissioningReport semantic hash mismatch')
        if self.report_id != f'rp32-report:{expected}':
            raise ValueError('CommissioningReport id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'report_id', 'report_sha256'}
        )


def build_commissioning_report(
    *,
    profile: Rp32CommissioningProfile,
    plan: Rp32VerificationPlan,
    record: Rp32VerificationRecord,
    generated_at_utc: str,
    limitations: Sequence[str] = (),
) -> CommissioningReport:
    """Assemble the sealed client-facing evidence package.

    Fail-closed: the record must belong to the plan and the plan to the
    profile; the claim text never asserts CEDIA certification and states
    the clause-mapping state verbatim.
    """

    if record.plan_id != plan.plan_id:
        raise ValueError('record does not belong to this plan')
    if plan.profile_id != profile.profile_id:
        raise ValueError('plan does not belong to this profile')

    item_summary: dict[str, int] = {}
    for item in record.item_results:
        item_summary[item.state] = item_summary.get(item.state, 0) + 1
    uncovered = len(plan.tasks) - len(record.item_results)
    if uncovered > 0:
        item_summary['unverified'] = (
            item_summary.get('unverified', 0) + uncovered
        )

    doc = profile.document
    claim = (
        f"Verification performed under {doc.publisher} {doc.document_id} "
        f"({doc.revision}; source access: {doc.source_access_kind}; "
        f"clause mapping state: {profile.clause_mapping_state}) against "
        f"design target RP22 {plan.target.rp22_revision} level "
        f"{plan.target.rp22_level}. Overall state: {record.overall_state}; "
        f"RP22 linkage: {record.rp22_state}. This report asserts HTDT "
        f"commissioning evidence only — it is not a CEDIA certification."
    )

    payload: dict[str, Any] = {
        'authority_version': RP32_REPORT_AUTHORITY_VERSION,
        'document_id': plan.document_id,
        'profile_id': profile.profile_id,
        'profile_sha256': profile.profile_sha256,
        'clause_mapping_state': profile.clause_mapping_state,
        'plan_id': plan.plan_id,
        'record_id': record.record_id,
        'overall_state': record.overall_state,
        'rp22_state': record.rp22_state,
        'item_summary': item_summary,
        'limitations': tuple(sorted(set(limitations))),
        'claim_text': claim,
        'generated_at_utc': generated_at_utc,
    }
    digest = _digest(payload)
    return CommissioningReport(
        authority_version=RP32_REPORT_AUTHORITY_VERSION,
        report_id=f'rp32-report:{digest}',
        report_sha256=digest,
        document_id=plan.document_id,
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        clause_mapping_state=profile.clause_mapping_state,
        plan_id=plan.plan_id,
        record_id=record.record_id,
        overall_state=record.overall_state,
        rp22_state=record.rp22_state,
        item_summary=item_summary,
        limitations=tuple(sorted(set(limitations))),
        claim_text=claim,
        generated_at_utc=generated_at_utc,
    )


__all__ = [
    'AsBuiltObservation',
    'AsBuiltReconciliation',
    'ClauseMappingState',
    'CommissioningDocumentIdentity',
    'CommissioningException',
    'CommissioningReport',
    'DesignTargetBinding',
    'FreshnessState',
    'InstrumentEvidence',
    'MappingStatus',
    'RP32_CAPABILITY_DOMAINS',
    'RP32_FRESHNESS_ALGORITHM_VERSION',
    'RP32_PLAN_AUTHORITY_VERSION',
    'RP32_PROFILE_AUTHORITY_VERSION',
    'RP32_PROFILE_SCHEMA_VERSION',
    'RP32_READINESS_ALGORITHM_VERSION',
    'RP32_RECONCILIATION_ALGORITHM_VERSION',
    'RP32_RECORD_AUTHORITY_VERSION',
    'RP32_REPORT_AUTHORITY_VERSION',
    'ReadinessState',
    'ReconciliationState',
    'Rp22VerificationState',
    'Rp32CapabilityDomain',
    'Rp32CommissioningProfile',
    'Rp32ReadinessAssessment',
    'Rp32RequirementMapping',
    'Rp32TaskSpec',
    'Rp32VerificationItemResult',
    'Rp32VerificationPlan',
    'Rp32VerificationRecord',
    'STALENESS_TRIGGERS',
    'VerificationFreshnessReport',
    'VerificationItemState',
    'VerificationOverallState',
    'build_commissioning_report',
    'build_rp32_profile',
    'build_verification_plan',
    'build_verification_record',
    'evaluate_designed_built_reconciliation',
    'evaluate_measurement_readiness',
    'evaluate_verification_freshness',
    'rp32_builtin_profile',
]
