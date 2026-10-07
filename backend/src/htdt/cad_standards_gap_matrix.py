"""Sealed standards gap-matrix authority (issue #805).

The gap matrix is a versioned, content-addressed record that enumerates
every criterion HTDT intends to cover for each built-in standard/profile
family, and the exact coverage state of each criterion:

* ``implemented`` — the criterion is encoded in a current built-in
  ``StandardsProfile`` revision; ``implemented`` pins every
  (profile_id, version, criterion_id, criterion_sha256) revision binding so
  the claim cannot drift from the actual emitted criteria.
* ``evidence_missing`` — the criterion's boundary is public, but honest
  evaluation requires evidence of a class no current HTDT authority can
  produce or validate (a declared format intent, a modality authority, a
  measured composite quantity no lane attests). It is deliberately left out
  of every profile: it stays ``UNKNOWN`` rather than risk a fabricated
  verdict on an unverifiable scalar.
* ``unsupported`` — no usable public authoritative boundary exists
  (unpublished or license-gated criteria, a published nominal with no
  tolerance, a sign-inconsistent published table, or a boundary that is
  format-conditional in a way HTDT cannot scope honestly).

Fail-closed rules: every entry carries a mandatory
``CriterionSource`` (publisher/title/version/reference) and an explicit
applicability declaration — an entry without source and revision cannot
exist. ``implemented`` entries must carry at least one revision binding;
non-implemented entries must carry none. ``validate_gap_matrix`` replays
every implemented ref against the emitted profiles and requires every
encoded criterion to appear in the matrix, so the record can neither claim
coverage that does not exist nor silently omit a shipped criterion.

The matrix records coverage state only — it is never a score, and it never
promotes an ``evidence_missing`` or ``unsupported`` criterion into an
evaluated one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import canonical_sha256 as _digest
from .cad_standards import (
    CriterionDefinition,
    CriterionSource,
    StandardsProfile,
    _criterion_digest,
)


STANDARDS_GAP_MATRIX_AUTHORITY_VERSION = 'standards-gap-matrix-1'
STANDARDS_GAP_MATRIX_ID_PREFIX = 'sgm'
BUILTIN_GAP_MATRIX_VERSION = 'builtin-1'

GapCriterionState = Literal['implemented', 'evidence_missing', 'unsupported']

_PERFORMANCE_LEVELS = frozenset({1, 2, 3, 4})


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


class GapMatrixImplementedRef(BaseModel):
    """One exact revision binding proving a criterion is implemented."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    criterion_id: str = Field(min_length=1)
    criterion_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class GapMatrixCriterion(BaseModel):
    """One intended criterion and its exact coverage state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    @field_validator(
        'applicable_domains',
        'performance_levels',
        'missing_evidence',
    )
    @classmethod
    def canonical_string_set(cls, values: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(sorted(values))

    criterion_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    source: CriterionSource
    applicable_domains: tuple[str, ...]
    performance_levels: tuple[int, ...] = ()
    state: GapCriterionState
    state_reason: str = Field(min_length=1)
    implemented: tuple[GapMatrixImplementedRef, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    quantity: str | None = Field(default=None, min_length=1)
    unit: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_entry(self) -> 'GapMatrixCriterion':
        if not self.applicable_domains:
            raise ValueError(
                'gap-matrix criterion must declare at least one '
                'applicable domain'
            )
        if len(set(self.applicable_domains)) != len(self.applicable_domains):
            raise ValueError('applicable domains must be unique')
        if any(not value for value in self.applicable_domains):
            raise ValueError('applicable domains must not be empty')
        for level in self.performance_levels:
            if level not in _PERFORMANCE_LEVELS:
                raise ValueError(
                    'performance_levels entries must be RP22 levels 1..4'
                )
        if len(set(self.performance_levels)) != len(self.performance_levels):
            raise ValueError('performance_levels must be unique')
        if len(set(self.missing_evidence)) != len(self.missing_evidence):
            raise ValueError('missing_evidence must be unique')
        if any(not value for value in self.missing_evidence):
            raise ValueError('missing_evidence must not contain empty values')
        if (self.quantity is None) != (self.unit is None):
            raise ValueError('quantity and unit must be supplied together')
        if self.state == 'implemented':
            if not self.implemented:
                raise ValueError(
                    'an implemented criterion must pin at least one '
                    'profile revision'
                )
            for ref in self.implemented:
                if ref.criterion_id != self.criterion_id:
                    raise ValueError(
                        'implemented ref criterion id does not match the '
                        'gap-matrix criterion id'
                    )
            keys = {
                (ref.profile_id, ref.profile_version)
                for ref in self.implemented
            }
            if len(keys) != len(self.implemented):
                raise ValueError(
                    'implemented refs must be unique per profile revision'
                )
            if self.quantity is None:
                raise ValueError(
                    'an implemented criterion must record quantity and unit'
                )
        else:
            if self.implemented:
                raise ValueError(
                    'a non-implemented criterion must not carry '
                    'implemented refs'
                )
        if self.state == 'evidence_missing' and not self.missing_evidence:
            raise ValueError(
                'an evidence_missing criterion must name the evidence '
                'authority it lacks'
            )
        return self


class GapMatrixStandard(BaseModel):
    """One intended built-in standard/profile family and its criteria."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    standard_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    intended_profile_ids: tuple[str, ...] = ()
    criteria: tuple[GapMatrixCriterion, ...]
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_standard(self) -> 'GapMatrixStandard':
        if not self.criteria:
            raise ValueError('a gap-matrix standard must declare criteria')
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError('gap-matrix criterion ids must be unique')
        if len(set(self.intended_profile_ids)) != len(
            self.intended_profile_ids
        ):
            raise ValueError('intended_profile_ids must be unique')
        if any(not value for value in self.intended_profile_ids):
            raise ValueError('intended_profile_ids must not be empty')
        return self

    def criterion(self, criterion_id: str) -> GapMatrixCriterion | None:
        return next(
            (
                criterion
                for criterion in self.criteria
                if criterion.criterion_id == criterion_id
            ),
            None,
        )


class StandardsGapMatrix(BaseModel):
    """Sealed, versioned gap-matrix record over all intended standards."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'standards-gap-matrix-1'
    ] = STANDARDS_GAP_MATRIX_AUTHORITY_VERSION
    matrix_id: str = Field(
        pattern=rf'^{STANDARDS_GAP_MATRIX_ID_PREFIX}-[0-9a-f]{{24}}$'
    )
    matrix_version: str = Field(min_length=1)
    standards: tuple[GapMatrixStandard, ...]
    created_at_utc: str = Field(min_length=1)
    matrix_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_matrix(self) -> 'StandardsGapMatrix':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        standard_ids = [standard.standard_id for standard in self.standards]
        if len(standard_ids) != len(set(standard_ids)):
            raise ValueError('gap-matrix standard ids must be unique')
        if not self.standards:
            raise ValueError('gap matrix must declare at least one standard')
        expected = _digest(self.semantic_payload())
        if self.matrix_sha256 != expected:
            raise ValueError('StandardsGapMatrix semantic hash mismatch')
        expected_id = (
            f'{STANDARDS_GAP_MATRIX_ID_PREFIX}-{expected[:24]}'
        )
        if self.matrix_id != expected_id:
            raise ValueError('StandardsGapMatrix id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'matrix_id', 'matrix_sha256'},
        )

    def standard(self, standard_id: str) -> GapMatrixStandard | None:
        return next(
            (
                standard
                for standard in self.standards
                if standard.standard_id == standard_id
            ),
            None,
        )

    def criteria(
        self,
        *,
        state: GapCriterionState | None = None,
    ) -> tuple[GapMatrixCriterion, ...]:
        """Every entry across standards, optionally filtered by state."""
        items = [
            criterion
            for standard in self.standards
            for criterion in standard.criteria
        ]
        if state is not None:
            items = [item for item in items if item.state == state]
        return tuple(items)


def build_standards_gap_matrix(
    *,
    standards: Sequence[GapMatrixStandard],
    matrix_version: str,
    created_at_utc: str,
) -> StandardsGapMatrix:
    """Seal a gap matrix: id and hash derive from the semantic payload."""

    probe = StandardsGapMatrix.model_construct(
        authority_version=STANDARDS_GAP_MATRIX_AUTHORITY_VERSION,
        matrix_id=f'{STANDARDS_GAP_MATRIX_ID_PREFIX}-{"0" * 24}',
        matrix_version=matrix_version,
        standards=tuple(standards),
        created_at_utc=created_at_utc,
        matrix_sha256='0' * 64,
    )
    digest = _digest(probe.semantic_payload())
    return StandardsGapMatrix(
        matrix_id=f'{STANDARDS_GAP_MATRIX_ID_PREFIX}-{digest[:24]}',
        matrix_version=matrix_version,
        standards=probe.standards,
        created_at_utc=created_at_utc,
        matrix_sha256=digest,
    )


def validate_gap_matrix(
    matrix: StandardsGapMatrix,
    *,
    profiles: Sequence[StandardsProfile],
) -> tuple[str, ...]:
    """Replay every implemented ref and the emitted criteria, fail closed.

    Returns the list of integrity problems; an empty tuple means the
    matrix exactly describes the supplied profile set. The function never
    mutates anything — it is a pure audit of the declared coverage.
    """

    errors: list[str] = []
    by_revision = {
        (profile.profile_id, profile.version): profile
        for profile in profiles
    }
    encoded_keys: set[tuple[str, str, str]] = set()
    for (profile_id, version), profile in by_revision.items():
        for criterion in profile.criteria:
            encoded_keys.add(
                (profile_id, version, criterion.criterion_id)
            )

    implemented_keys: set[tuple[str, str, str]] = set()
    for standard in matrix.standards:
        for entry in standard.criteria:
            for ref in entry.implemented:
                key = (ref.profile_id, ref.profile_version, ref.criterion_id)
                if key in implemented_keys:
                    errors.append(
                        'implemented ref appears twice in the matrix: '
                        f'{key}'
                    )
                implemented_keys.add(key)
                profile = by_revision.get(
                    (ref.profile_id, ref.profile_version)
                )
                if profile is None:
                    errors.append(
                        f'{entry.criterion_id}: implemented ref resolves '
                        f'no profile {ref.profile_id}@{ref.profile_version}'
                    )
                    continue
                criterion = next(
                    (
                        item
                        for item in profile.criteria
                        if item.criterion_id == ref.criterion_id
                    ),
                    None,
                )
                if criterion is None:
                    errors.append(
                        f'{entry.criterion_id}: implemented ref resolves '
                        f'no criterion {ref.criterion_id} in '
                        f'{ref.profile_id}@{ref.profile_version}'
                    )
                    continue
                if _citation_fields(criterion.source) != _citation_fields(
                    entry.source
                ):
                    errors.append(
                        f'{entry.criterion_id}: gap-matrix source does '
                        'not match the encoded criterion source'
                    )
                digest = _criterion_digest(criterion)
                if digest != ref.criterion_sha256:
                    errors.append(
                        f'{entry.criterion_id}: implemented ref criterion '
                        f'sha256 {ref.criterion_sha256} does not match '
                        f'the emitted criterion hash {digest}'
                    )

    missing = sorted(encoded_keys - implemented_keys)
    for profile_id, version, criterion_id in missing:
        errors.append(
            f'encoded criterion {criterion_id} in '
            f'{profile_id}@{version} is absent from the gap matrix'
        )
    return tuple(errors)


# ---------------------------------------------------------------------------
# Built-in gap matrix
# ---------------------------------------------------------------------------

def _citation_fields(
    source: CriterionSource,
) -> tuple[Any, ...]:
    """Citation identity, excluding the per-revision authority binding.

    A criterion id legitimately resolves to different content-addressed
    authorities across revisions (e.g. per-level RP22 authorities), so the
    matrix compares the human-facing citation fields and pins each exact
    revision separately in ``implemented``.
    """

    return (
        source.publisher,
        source.document_title,
        source.document_version,
        source.reference,
        source.source_uri,
        source.note,
        source.content_kind,
    )


def _strip_authority_binding(source: CriterionSource) -> CriterionSource:
    return CriterionSource(
        publisher=source.publisher,
        document_title=source.document_title,
        document_version=source.document_version,
        reference=source.reference,
        source_uri=source.source_uri,
        note=source.note,
        content_kind=source.content_kind,
    )


def _level_of(profile: StandardsProfile) -> int | None:
    suffix = profile.profile_id.rsplit('-', 1)[-1]
    if suffix.isdigit() and int(suffix) in _PERFORMANCE_LEVELS:
        return int(suffix)
    return None


def _collect_implemented(
    profiles: Sequence[StandardsProfile],
) -> dict[str, dict[str, Any]]:
    """Group every encoded criterion by id across the emitted revisions."""

    grouped: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        level = _level_of(profile)
        for criterion in profile.criteria:
            slot = grouped.setdefault(
                criterion.criterion_id,
                {
                    'name': criterion.name,
                    'source': _strip_authority_binding(criterion.source),
                    'applicable_domains': criterion.applicable_domains,
                    'quantity': criterion.quantity,
                    'unit': criterion.unit,
                    'refs': [],
                    'levels': set(),
                },
            )
            slot['refs'].append(
                GapMatrixImplementedRef(
                    profile_id=profile.profile_id,
                    profile_version=profile.version,
                    criterion_id=criterion.criterion_id,
                    criterion_sha256=_criterion_digest(criterion),
                )
            )
            if level is not None:
                slot['levels'].add(level)
    return grouped


def _implemented_entry(
    criterion_id: str,
    slot: dict[str, Any],
    *,
    reason: str,
    name: str | None = None,
    applicable_domains: tuple[str, ...] | None = None,
) -> GapMatrixCriterion:
    return GapMatrixCriterion(
        criterion_id=criterion_id,
        name=name or slot['name'],
        source=slot['source'],
        applicable_domains=applicable_domains or slot['applicable_domains'],
        performance_levels=tuple(sorted(slot['levels'])),
        state='implemented',
        state_reason=reason,
        implemented=tuple(slot['refs']),
        quantity=slot['quantity'],
        unit=slot['unit'],
    )


# ---------------------------------------------------------------------------
# Declared-but-not-encoded criteria.
#
# Every entry below is a criterion HTDT intends to track for a built-in
# standard but does NOT encode in any profile revision. The record itself
# is the deliverable: the criterion exists in the matrix with its state
# and reason, and evaluating a document against it stays UNKNOWN instead
# of silently fabricating a verdict.

# Entries whose criterion_id uses the same prefix convention as encoded
# criteria ('rp22.pNN.*') so the matrix stays traceable to the published
# parameter numbering.


def _evidence_missing_entry(
    *,
    criterion_id: str,
    name: str,
    source: CriterionSource,
    applicable_domains: tuple[str, ...],
    performance_levels: tuple[int, ...] = (),
    missing_evidence: tuple[str, ...],
    reason: str,
    quantity: str | None = None,
    unit: str | None = None,
) -> GapMatrixCriterion:
    return GapMatrixCriterion(
        criterion_id=criterion_id,
        name=name,
        source=source,
        applicable_domains=applicable_domains,
        performance_levels=performance_levels,
        state='evidence_missing',
        state_reason=reason,
        missing_evidence=missing_evidence,
        quantity=quantity,
        unit=unit,
    )


def _unsupported_entry(
    *,
    criterion_id: str,
    name: str,
    source: CriterionSource,
    applicable_domains: tuple[str, ...],
    performance_levels: tuple[int, ...] = (),
    reason: str,
    quantity: str | None = None,
    unit: str | None = None,
) -> GapMatrixCriterion:
    return GapMatrixCriterion(
        criterion_id=criterion_id,
        name=name,
        source=source,
        applicable_domains=applicable_domains,
        performance_levels=performance_levels,
        state='unsupported',
        state_reason=reason,
        quantity=quantity,
        unit=unit,
    )


def builtin_standards_gap_matrix(
    profiles: Sequence[StandardsProfile],
    *,
    matrix_version: str = BUILTIN_GAP_MATRIX_VERSION,
    created_at_utc: str,
) -> StandardsGapMatrix:
    """Build the gap matrix over every emitted built-in profile revision.

    ``implemented`` entries are derived from the supplied profiles so the
    matrix cannot claim coverage a criterion does not have; entries not in
    any emitted revision come from the static declarations below.
    """

    grouped = _collect_implemented(profiles)

    rp22_standard = _rp22_standard(grouped)
    dolby_standard = _dolby_standard(grouped)
    auro_standard = _auro_standard(grouped)
    dtsx_standard = _dtsx_standard()
    viewing_standard = _viewing_standard()

    matrix = build_standards_gap_matrix(
        standards=(
            rp22_standard,
            dolby_standard,
            auro_standard,
            dtsx_standard,
            viewing_standard,
        ),
        matrix_version=matrix_version,
        created_at_utc=created_at_utc,
    )

    errors = validate_gap_matrix(matrix, profiles=profiles)
    if errors:
        raise ValueError(
            'built-in gap matrix does not describe the emitted profiles: '
            + '; '.join(errors)
        )
    return matrix


def _rp22_standard(grouped: dict[str, dict[str, Any]]) -> GapMatrixStandard:
    source_note = (
        'CEDIA/CTA RP22 v1.2 Appendix A per-level table '
        '(publicly available).'
    )
    criteria: list[GapMatrixCriterion] = []

    criteria.append(
        _evidence_missing_entry(
            criterion_id='rp22.p02.discrete-rendered-speaker-count',
            name='Discrete rendered-speaker count',
            source=CriterionSource(
                publisher='CEDIA / CTA',
                document_title=(
                    'CEDIA/CTA RP22 Immersive Audio Design '
                    'Recommended Practice'
                ),
                document_version='v1.2',
                reference='Appendix A, Parameter 2',
                content_kind='normative',
            ),
            applicable_domains=('room', 'speaker_layout'),
            performance_levels=(1, 2, 3, 4),
            missing_evidence=(
                'declared-immersive-format-design-intent-v1',
            ),
            reason=(
                'The published boundary is format-conditional: Levels '
                '3/4 require 15 discrete rendered speakers, or 13 for '
                'an Auro-3D room design. Selecting the boundary requires '
                'a declared immersive-format design intent that HTDT '
                'does not retain as a resolved authority, so the '
                'criterion stays UNKNOWN rather than apply a threshold '
                'that changes meaning with context.'
            ),
            quantity='discrete_rendered_speaker_count',
            unit='count',
        )
    )

    reason_by_prefix = {
        'rp22.p01.': 'Encoded in every spatial profile revision.',
        'rp22.p03.': 'Encoded in every spatial profile revision.',
        'rp22.p04.': (
            'Encoded in every performance profile revision; predicted '
            'evidence covers anechoic propagation only.'
        ),
        'rp22.p05.': 'Encoded in spatial profile revisions for Levels 2-4.',
        'rp22.p06.': 'Encoded in every performance profile revision.',
        'rp22.p07.': 'Encoded in every spatial profile revision.',
        'rp22.p08.': (
            'Encoded in spatial profile revisions for Levels 3-4 (the '
            'only levels that prohibit up-firing elevation speakers).'
        ),
        'rp22.p09.': 'Encoded in spatial profile revisions for Levels 2-4.',
        'rp22.p10.': 'Encoded in every performance profile revision.',
        'rp22.p11.': 'Encoded in spatial profile revisions for Levels 2-4.',
        'rp22.p12.': (
            'Encoded in every performance profile revision; consuming '
            'exact SPL-capability evidence refs.'
        ),
        'rp22.p13.': (
            'Encoded in every performance profile revision; consuming '
            'exact SPL-capability evidence refs.'
        ),
        'rp22.p14.': (
            'Encoded in every performance profile revision; consuming '
            'exact LFE-band SPL-capability evidence refs.'
        ),
        'rp22.p15.': (
            'Encoded in every performance profile revision as a '
            'measured-only criterion.'
        ),
        'rp22.p16.': (
            'Encoded in every performance profile revision as a '
            'measured-only criterion.'
        ),
        'rp22.p17.': (
            'Encoded in performance profile revisions for Levels 3-4 '
            'as a measured-only criterion.'
        ),
        'rp22.p18.': (
            'Encoded in every performance profile revision; the '
            'no-distortion/no-rattle condition still requires '
            'verification evidence.'
        ),
        'rp22.p19.': (
            'Encoded in every performance profile revision as a '
            'measured-only criterion.'
        ),
        'rp22.p20.': (
            'Encoded in performance profile revisions for Levels 2-4 '
            'as a measured-only criterion.'
        ),
        'rp22.p21.': (
            'Encoded in performance profile revisions for Levels 2-4 '
            'as a measured-only criterion.'
        ),
    }
    for criterion_id in sorted(grouped):
        if not criterion_id.startswith('rp22.'):
            continue
        slot = grouped[criterion_id]
        reason = next(
            (
                note
                for prefix, note in reason_by_prefix.items()
                if criterion_id.startswith(prefix)
            ),
            'Encoded in the emitted built-in profile revisions.',
        )
        criteria.append(_implemented_entry(criterion_id, slot, reason=reason))

    return GapMatrixStandard(
        standard_id='cedia-cta-rp22-v1.2',
        name=(
            'CEDIA/CTA RP22 Immersive Audio Design Recommended '
            'Practice v1.2'
        ),
        intended_profile_ids=(
            'cedia-cta-rp22-spatial-level-1',
            'cedia-cta-rp22-spatial-level-2',
            'cedia-cta-rp22-spatial-level-3',
            'cedia-cta-rp22-spatial-level-4',
            'cedia-cta-rp22-performance-level-1',
            'cedia-cta-rp22-performance-level-2',
            'cedia-cta-rp22-performance-level-3',
            'cedia-cta-rp22-performance-level-4',
        ),
        criteria=tuple(sorted(criteria, key=lambda item: item.criterion_id)),
        note=source_note,
    )


def _dolby_standard(grouped: dict[str, dict[str, Any]]) -> GapMatrixStandard:
    criteria: list[GapMatrixCriterion] = []
    for criterion_id in sorted(grouped):
        if not criterion_id.startswith('dolby.'):
            continue
        slot = grouped[criterion_id]
        criteria.append(
            _implemented_entry(
                criterion_id,
                slot,
                reason='Encoded in the Dolby 5.1.2 layout profile.',
            )
        )

    criteria.extend(
        (
            _evidence_missing_entry(
                criterion_id='dolby.5.1.2.atmos-enabled-speaker-mode',
                name=(
                    'Dolby Atmos enabled (up-firing) speaker layout mode'
                ),
                source=CriterionSource(
                    publisher='Dolby Laboratories',
                    document_title=(
                        'Dolby Atmos Home Theater Installation Guidelines'
                    ),
                    document_version='R3.1',
                    reference=(
                        'Figure 12, page 28 — Atmos enabled speaker '
                        'alternative'
                    ),
                    content_kind='guidance',
                ),
                applicable_domains=('speaker_layout',),
                missing_evidence=(
                    'declared-speaker-rendering-mode-v1',
                ),
                reason=(
                    'The published 5.1.2 alternative substitutes '
                    'Atmos-enabled (up-firing) speakers for overhead '
                    'ones and carries its own elevation expectations. '
                    'Applying it requires a declared speaker '
                    'rendering-mode authority HTDT does not retain, so '
                    'the layout stays UNKNOWN rather than judged '
                    'against the overhead-speaker window.'
                ),
            ),
            _evidence_missing_entry(
                criterion_id='dolby.5.1.2.overhead-speaker-height-ratio',
                name=(
                    'Overhead speaker height ratio to listener-level '
                    'speakers'
                ),
                source=CriterionSource(
                    publisher='Dolby Laboratories',
                    document_title=(
                        'Dolby Atmos Home Theater Installation Guidelines'
                    ),
                    document_version='R3.1',
                    reference='§2, overhead speaker placement guidance',
                    content_kind='guidance',
                ),
                applicable_domains=('speaker_layout',),
                missing_evidence=(
                    'declared-listener-level-reference-height-v1',
                ),
                reason=(
                    'The guidance bounds overhead speaker height to '
                    '2-3x the listener-level speaker height. Evaluating '
                    'the ratio requires a declared listener-level '
                    'reference height set the scene does not currently '
                    'retain as a resolved authority.'
                ),
                quantity='overhead_to_listener_level_height_ratio',
                unit='ratio',
            ),
            _unsupported_entry(
                criterion_id='dolby.5.1.2.center-channel-azimuth',
                name='Center-channel azimuth',
                source=CriterionSource(
                    publisher='Dolby Laboratories',
                    document_title=(
                        'Dolby Atmos Home Theater Installation Guidelines'
                    ),
                    document_version='R3.1',
                    reference=(
                        'Figure 12, page 28 — nominal center placement'
                    ),
                    content_kind='guidance',
                ),
                applicable_domains=('speaker_layout',),
                reason=(
                    'The source publishes a nominal 0 deg placement '
                    'with no stated tolerance; encoding a '
                    'zero-tolerance boundary would fabricate failures, '
                    'so the criterion is not evaluatable.'
                ),
            ),
        )
    )

    return GapMatrixStandard(
        standard_id='dolby-atmos-home-r3.1',
        name=(
            'Dolby Atmos Home Theater Installation Guidelines R3.1 '
            '(5.1.2 layout)'
        ),
        intended_profile_ids=('dolby-atmos-home-5.1.2-layout',),
        criteria=tuple(sorted(criteria, key=lambda item: item.criterion_id)),
        note='Public Dolby installation guidance.',
    )


def _auro_standard(grouped: dict[str, dict[str, Any]]) -> GapMatrixStandard:
    criteria: list[GapMatrixCriterion] = []
    for criterion_id in sorted(grouped):
        if not criterion_id.startswith('auro.'):
            continue
        slot = grouped[criterion_id]
        criteria.append(
            _implemented_entry(
                criterion_id,
                slot,
                reason='Encoded in the AURO-3D home layout profile.',
            )
        )

    criteria.append(
        _unsupported_entry(
            criterion_id='auro.v12.horizontal-azimuth-bounds',
            name='Horizontal azimuth bounds (lower and height layers)',
            source=CriterionSource(
                publisher='AURO-3D',
                document_title=(
                    'AURO-3D Home Theater Setup Installation Guidelines'
                ),
                document_version='Rev.12',
                reference=(
                    'Table 3 — Normative Speaker Positions '
                    '(horizontal azimuth rows)'
                ),
                content_kind='normative',
            ),
            applicable_domains=('speaker_layout',),
            reason=(
                'The published table carries an apparent sign '
                'inconsistency for the Height Right azimuth rows; '
                'HTDT does not silently repair source data, so no '
                'horizontal-azimuth criterion is encoded.'
            ),
        )
    )

    return GapMatrixStandard(
        standard_id='auro-3d-home-rev12',
        name='AURO-3D Home Theater Setup Installation Guidelines Rev.12',
        intended_profile_ids=('auro3d-home-layout',),
        criteria=tuple(sorted(criteria, key=lambda item: item.criterion_id)),
        note='Public AURO-3D installation guidelines.',
    )


def _dtsx_standard() -> GapMatrixStandard:
    source = CriterionSource(
        publisher='DTS, Inc. (Xperi)',
        document_title='DTS:X immersive audio home theater layout',
        document_version=(
            'no public authoritative criteria edition identified'
        ),
        reference='no public normative criteria published',
    )
    criteria = (
        _unsupported_entry(
            criterion_id='dtsx.listener-level-layout',
            name='DTS:X listener-level speaker layout',
            source=source,
            applicable_domains=('speaker_layout',),
            reason=(
                'DTS/Xperi does not publish an authoritative public '
                'placement or count boundary for listener-level DTS:X '
                'speakers. Third-party diagrams are descriptive, not a '
                'conformance boundary, so the criterion remains '
                'UNKNOWN by design.'
            ),
        ),
        _unsupported_entry(
            criterion_id='dtsx.upper-layer-layout',
            name='DTS:X upper/height-layer speaker layout',
            source=source,
            applicable_domains=('speaker_layout',),
            reason=(
                'No public authoritative boundary exists for DTS:X '
                'upper-layer placement; a criterion here would invent '
                'proprietary criteria.'
            ),
        ),
        _unsupported_entry(
            criterion_id='dtsx.renderer-capability',
            name='DTS:X renderer capability / channel support',
            source=source,
            applicable_domains=('room',),
            reason=(
                'No public authoritative capability boundary exists '
                'to evaluate a DTS:X rendering claim against.'
            ),
        ),
    )
    return GapMatrixStandard(
        standard_id='dts-x-home',
        name='DTS:X home theater immersive audio',
        intended_profile_ids=(),
        criteria=criteria,
        note=(
            'No authoritative public criteria could be identified; '
            'every entry is deliberately unsupported so evaluation '
            'stays UNKNOWN rather than reading as conformance.'
        ),
    )


def _viewing_standard() -> GapMatrixStandard:
    criteria = (
        _unsupported_entry(
            criterion_id='viewing.horizontal-viewing-angle',
            name='Horizontal viewing angle to screen edges',
            source=CriterionSource(
                publisher='THX Ltd.',
                document_title='THX Certified viewing geometry',
                document_version=(
                    'no public authoritative criteria edition identified'
                ),
                reference='license-gated published criteria',
            ),
            applicable_domains=('seat',),
            reason=(
                'THX publishes a recommended horizontal viewing angle '
                'range but the authoritative criterion text is '
                'license-gated; HTDT does not encode licensed '
                'boundaries it cannot cite exactly.'
            ),
            quantity='horizontal_viewing_angle',
            unit='deg',
        ),
        _unsupported_entry(
            criterion_id='viewing.screen-size-distance-ratio',
            name='Screen size / viewing distance geometry',
            source=CriterionSource(
                publisher='SMPTE',
                document_title='SMPTE EG 18 (viewing geometry)',
                document_version=(
                    'no public authoritative criteria edition identified'
                ),
                reference='license-gated published criteria',
            ),
            applicable_domains=('seat',),
            reason=(
                'SMPTE EG 18 is license-gated; the viewing-geometry '
                'criterion stays unsupported rather than encode an '
                'unverifiable paraphrase of the published range.'
            ),
        ),
    )
    return GapMatrixStandard(
        standard_id='theater-viewing-screen',
        name='Screen / viewing geometry (THX, SMPTE EG 18)',
        intended_profile_ids=(),
        criteria=criteria,
        note=(
            'Screen/viewing criteria are license-gated in the public '
            'sources HTDT may cite; they remain unsupported until a '
            'citable public or licensed extraction exists.'
        ),
    )
