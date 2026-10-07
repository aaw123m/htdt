"""Sealed authoritative standards source matrix (issue #839).

Where #805's gap matrix enumerates every intended *criterion* and its
coverage state, this matrix enumerates the *sources themselves*: every
external document HTDT may evaluate against, its exact edition identity,
its source status, the StandardsProfile taxonomy class it may serve, and
the explicit rights/access/redistribution boundary around it.

The central rule the matrix encodes:

    private-home design recommended practice
    != format-vendor installation guidance
    != production reference layout
    != AV-system measurement/classification standard
    != subjective-test reference room

Using the wrong document as universal home-theater truth is worse than
returning UNKNOWN, so the matrix refuses to exist without an explicit
per-source declaration of every axis a citation could silently smuggle:

* ``source_status`` — the #839 §13.1 state machine: ``executable`` /
  ``metadata_only`` / ``licensed_source_required`` / ``unsupported`` /
  ``source_conflict`` / ``no_authoritative_numeric_criteria``.
* ``profile_class`` — the #839 §11 taxonomy; ``private_theater_
  recommended_practice`` is not a synonym for "standard".
* ``conformance_scope`` — whether the document may ever carry
  private-home conformance weight. Production, subjective-test and
  AV-measurement classes are mechanically bound to ``non_home_reference``.
* ``precedence`` — the #839 §10 source ordering. ``third_party_summary``
  can never be an ``executable`` source: a summary may aid discovery but
  never overrides first-party material.
* ``rights_class`` / ``normative_access`` / ``redistribution_rights`` —
  public URL accessibility does not establish unrestricted redistribution
  rights, and PUBLIC_METADATA_CONFIRMED != NORMATIVE_PROFILE_AVAILABLE.
* ``conflicting_observations`` — equal-tier official surfaces that
  disagree (AVIXA A103:2022 vs A103.01:2023) are kept as competing
  observations under ``source_conflict``; the record never silently picks
  a revision.
* ``page_identity`` — a webpage-only guidance source must pin its
  checked-at page identity, so an unversioned vendor page cannot later
  masquerade as a document revision.

Fail-closed rules are mechanical: an ``executable`` source needs an
identified document edition, a reachable normative access path and a
non-metadata rights class; ``no_authoritative_numeric_criteria`` sources
(DTS:X) may only be used to define UNKNOWN for fixed numeric claims; and
at most one entry may hold the ``primary_private_theater_profile`` role —
CEDIA/CTA RP22.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import canonical_sha256 as _digest
from .cad_external_standards import (
    StandardRightsClass,
    StandardsSourceTier,
)


STANDARDS_SOURCE_MATRIX_AUTHORITY_VERSION = 'standards-source-matrix-1'
STANDARDS_SOURCE_MATRIX_ID_PREFIX = 'ssm'
BUILTIN_SOURCE_MATRIX_VERSION = 'builtin-1'

# #839 §13.1 — the per-source status vocabulary #805 is updated with.
SourceMatrixStatus = Literal[
    'executable',
    'metadata_only',
    'licensed_source_required',
    'unsupported',
    'source_conflict',
    'no_authoritative_numeric_criteria',
]

# #839 §11 — StandardsProfile taxonomy. One STANDARD boolean is not
# enough: a production reference layout is not private-home truth.
StandardsProfileClass = Literal[
    'private_theater_recommended_practice',
    'format_vendor_home_guidance',
    'production_reference_layout',
    'subjective_test_reference_room',
    'av_system_measurement_standard',
    'project_defined_profile',
    'research_only_profile',
]

# #839 §12 — the HTDT role column.
SourceMatrixRole = Literal[
    'primary_private_theater_profile',
    'format_layout_recommendation',
    'vendor_capability_layout_statement',
    'conventional_multichannel_reference',
    'advanced_immersive_production_reference',
    'critical_listening_test_reference',
    'measurement_design_commissioning_procedure',
    'supplementary_project_or_research_source',
]

# #839 §10 — source precedence. A lower tier may aid discovery but can
# never silently override a higher-authority source.
SourcePrecedence = Literal[
    'project_selected_profile',
    'first_party_standard',
    'first_party_vendor_guidance',
    'first_party_support_page',
    'official_secondary',
    'third_party_summary',
]

_PRECEDENCE_RANK = {
    'project_selected_profile': 0,
    'first_party_standard': 1,
    'first_party_vendor_guidance': 2,
    'first_party_support_page': 3,
    'official_secondary': 4,
    'third_party_summary': 5,
}

# How the normative text of the source is reachable.
SourceAccess = Literal[
    'public_download',
    'public_webpage',
    'public_catalog_metadata',
    'licensed_purchase',
    'mixed_official_surfaces',
    'no_identified_public_source',
]

# Explicit redistribution boundary — public accessibility is not a
# redistribution grant.
SourceRedistribution = Literal[
    'permitted',
    'restricted',
    'prohibited',
    'unknown',
]

# Whether the document may carry private-home conformance weight.
SourceConformanceScope = Literal[
    'private_home_conformance',
    'vendor_home_guidance_only',
    'non_home_reference',
    'not_a_conformance_source',
]

# #839 §12 — "research can define now?" column.
SourceMatrixDefinability = Literal[
    'definable',
    'definable_bounded_to_source',
    'definable_for_unknown',
    'partially_licensed',
    'not_definable',
]


# Role -> taxonomy class. A role is only coherent inside its class: a
# production reference is never home conformance, a vendor capability
# statement is never a recommended practice.
_ROLE_CLASS: dict[str, str] = {
    'primary_private_theater_profile': 'private_theater_recommended_practice',
    'format_layout_recommendation': 'format_vendor_home_guidance',
    'vendor_capability_layout_statement': 'format_vendor_home_guidance',
    'conventional_multichannel_reference': 'production_reference_layout',
    'advanced_immersive_production_reference': 'production_reference_layout',
    'critical_listening_test_reference': 'subjective_test_reference_room',
    'measurement_design_commissioning_procedure': (
        'av_system_measurement_standard'
    ),
    'supplementary_project_or_research_source': 'research_only_profile',
}

# Taxonomy class -> the only conformance scope it may claim.
_CLASS_CONFORMANCE: dict[str, str] = {
    'private_theater_recommended_practice': 'private_home_conformance',
    'format_vendor_home_guidance': 'vendor_home_guidance_only',
    'production_reference_layout': 'non_home_reference',
    'subjective_test_reference_room': 'non_home_reference',
    'av_system_measurement_standard': 'non_home_reference',
    'project_defined_profile': 'not_a_conformance_source',
    'research_only_profile': 'not_a_conformance_source',
}

# Definability states admissible per source status.
_STATUS_DEFINABILITY: dict[str, frozenset[str]] = {
    'executable': frozenset({'definable', 'definable_bounded_to_source'}),
    'metadata_only': frozenset({'partially_licensed', 'not_definable'}),
    'licensed_source_required': frozenset(
        {'partially_licensed', 'not_definable'}
    ),
    'unsupported': frozenset({'not_definable'}),
    'source_conflict': frozenset({'partially_licensed', 'not_definable'}),
    'no_authoritative_numeric_criteria': frozenset(
        {'definable_for_unknown'}
    ),
}


def _require_iso8601(value: str, label: str) -> None:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc


class SourceConflictObservation(BaseModel):
    """One recorded conflicting claim from an official surface.

    Equal-authority sources that disagree are kept side by side — the
    matrix records both observations and stays ``source_conflict`` rather
    than guessing which revision wins.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    observation_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    surface: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    tier: StandardsSourceTier
    observed_at: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_observation(self) -> 'SourceConflictObservation':
        _require_iso8601(self.observed_at, 'observed_at')
        return self


class SourceMatrixEntry(BaseModel):
    """One external source document and every axis it may be cited on."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    @field_validator(
        'source_uris',
        'related_standard_ids',
        'intended_profile_ids',
    )
    @classmethod
    def canonical_string_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(values))

    source_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    checked_at: str = Field(min_length=1)
    source_uris: tuple[str, ...]
    precedence: SourcePrecedence
    profile_class: StandardsProfileClass
    htdt_role: SourceMatrixRole
    source_status: SourceMatrixStatus
    normative_access: SourceAccess
    rights_class: StandardRightsClass
    redistribution_rights: SourceRedistribution
    definability: SourceMatrixDefinability
    conformance_scope: SourceConformanceScope
    conflicting_observations: tuple[SourceConflictObservation, ...] = ()
    page_identity: str | None = Field(default=None, min_length=1)
    related_standard_ids: tuple[str, ...] = ()
    intended_profile_ids: tuple[str, ...] = ()
    notes: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_entry(self) -> 'SourceMatrixEntry':
        _require_iso8601(self.checked_at, 'checked_at')

        # A source matrix exists to pin authoritative surfaces — every row
        # must declare at least one concrete source URI, whatever its status.
        if not self.source_uris:
            raise ValueError(
                'a source-matrix entry must declare at least one source URI'
            )

        # --- taxonomy / role / conformance coherence ---------------------
        expected_class = _ROLE_CLASS.get(self.htdt_role)
        if expected_class is not None and self.profile_class != expected_class:
            raise ValueError(
                f'source-matrix role {self.htdt_role} requires profile '
                f'class {expected_class}, not {self.profile_class}'
            )
        expected_conformance = _CLASS_CONFORMANCE[self.profile_class]
        if self.conformance_scope != expected_conformance:
            raise ValueError(
                f'profile class {self.profile_class} requires conformance '
                f'scope {expected_conformance}, not {self.conformance_scope}'
            )

        # --- precedence --------------------------------------------------
        if self.precedence == 'third_party_summary':
            # A third-party summary may aid discovery but can never stand
            # as normative truth where first-party sources exist.
            if self.source_status == 'executable':
                raise ValueError(
                    'a third_party_summary source can never be executable'
                )

        # --- source-status rules ------------------------------------------
        admissible = _STATUS_DEFINABILITY[self.source_status]
        if self.definability not in admissible:
            raise ValueError(
                f'source status {self.source_status} requires definability '
                f'in {sorted(admissible)}, not {self.definability}'
            )

        if self.source_status == 'executable':
            if not self.source_uris:
                raise ValueError(
                    'an executable source must pin at least one source URI'
                )
            if self.normative_access == 'no_identified_public_source':
                raise ValueError(
                    'an executable source must have an identified normative '
                    'access path'
                )
            if self.rights_class in {'public_metadata_only', 'unknown_rights'}:
                raise ValueError(
                    'an executable source requires a rights class that '
                    'lawfully covers derived criteria, not '
                    f'{self.rights_class}'
                )
        if self.source_status == 'metadata_only':
            if self.rights_class != 'public_metadata_only':
                raise ValueError(
                    'a metadata_only source must declare rights class '
                    'public_metadata_only'
                )
        if (
            self.source_status == 'no_authoritative_numeric_criteria'
            and self.normative_access == 'no_identified_public_source'
        ):
            raise ValueError(
                'no_authoritative_numeric_criteria still requires an '
                'identified first-party surface making the statement'
            )

        # --- conflict observations ----------------------------------------
        if self.source_status == 'source_conflict':
            if len(self.conflicting_observations) < 2:
                raise ValueError(
                    'a source_conflict entry must keep at least two '
                    'conflicting observations'
                )
            claims = {
                observation.claim for observation in self.conflicting_observations
            }
            if len(claims) < 2:
                raise ValueError(
                    'conflicting observations must record at least two '
                    'distinct claims — identical claims are not a conflict'
                )
            for claim in claims:
                if self.document_version == claim:
                    raise ValueError(
                        'a source_conflict entry must not silently select '
                        'one conflicting revision as its document_version'
                    )
        elif self.conflicting_observations:
            raise ValueError(
                'conflicting observations are only admissible on a '
                'source_conflict entry'
            )

        # --- webpage-only identity ----------------------------------------
        if (
            self.normative_access == 'public_webpage'
            and self.page_identity is None
        ):
            raise ValueError(
                'a webpage-only source must pin its checked-at page '
                'identity — an unversioned page is not a document revision'
            )

        return self


class StandardsSourceMatrix(BaseModel):
    """Sealed, versioned source matrix over all declared external sources."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'standards-source-matrix-1'
    ] = STANDARDS_SOURCE_MATRIX_AUTHORITY_VERSION
    matrix_id: str = Field(
        pattern=rf'^{STANDARDS_SOURCE_MATRIX_ID_PREFIX}-[0-9a-f]{{24}}$'
    )
    matrix_version: str = Field(min_length=1)
    entries: tuple[SourceMatrixEntry, ...]
    created_at_utc: str = Field(min_length=1)
    matrix_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_matrix(self) -> 'StandardsSourceMatrix':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if not self.entries:
            raise ValueError('a source matrix must declare entries')
        source_ids = [entry.source_id for entry in self.entries]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError('source-matrix source ids must be unique')
        primaries = [
            entry.source_id
            for entry in self.entries
            if entry.htdt_role == 'primary_private_theater_profile'
        ]
        if len(primaries) > 1:
            raise ValueError(
                'at most one source may hold the '
                'primary_private_theater_profile role — the private-home '
                'engineering profile is not a committee vote: '
                f'{sorted(primaries)}'
            )
        expected = _digest(self.semantic_payload())
        if self.matrix_sha256 != expected:
            raise ValueError('StandardsSourceMatrix semantic hash mismatch')
        expected_id = (
            f'{STANDARDS_SOURCE_MATRIX_ID_PREFIX}-{expected[:24]}'
        )
        if self.matrix_id != expected_id:
            raise ValueError('StandardsSourceMatrix id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'matrix_id', 'matrix_sha256'},
        )

    def entry(self, source_id: str) -> SourceMatrixEntry | None:
        return next(
            (entry for entry in self.entries if entry.source_id == source_id),
            None,
        )

    def entries_by_status(
        self,
        status: SourceMatrixStatus,
    ) -> tuple[SourceMatrixEntry, ...]:
        return tuple(
            entry for entry in self.entries if entry.source_status == status
        )


def build_standards_source_matrix(
    *,
    entries: Sequence[SourceMatrixEntry],
    matrix_version: str,
    created_at_utc: str,
) -> StandardsSourceMatrix:
    """Seal a source matrix: id and hash derive from the semantic payload."""

    probe = StandardsSourceMatrix.model_construct(
        authority_version=STANDARDS_SOURCE_MATRIX_AUTHORITY_VERSION,
        matrix_id=f'{STANDARDS_SOURCE_MATRIX_ID_PREFIX}-{"0" * 24}',
        matrix_version=matrix_version,
        entries=tuple(entries),
        created_at_utc=created_at_utc,
        matrix_sha256='0' * 64,
    )
    digest = _digest(probe.semantic_payload())
    return StandardsSourceMatrix(
        matrix_id=f'{STANDARDS_SOURCE_MATRIX_ID_PREFIX}-{digest[:24]}',
        matrix_version=matrix_version,
        entries=probe.entries,
        created_at_utc=created_at_utc,
        matrix_sha256=digest,
    )


def validate_source_matrix(
    matrix: StandardsSourceMatrix,
    *,
    profiles: Sequence[Any] = (),
    gap_standard_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Replay the matrix's cross-authority claims, fail closed.

    Per-entry validators and the seal already guarantee internal honesty;
    this audit checks the claims that point *outside* the matrix: pinned
    intended profile ids must resolve to an emitted profile, and declared
    related gap-matrix standard ids must exist when a gap-matrix id set is
    supplied. Returns the integrity problems found — empty means the
    matrix's outgoing references are exactly consistent with the supplied
    authorities.
    """

    errors: list[str] = []
    emitted_profile_ids = {
        getattr(profile, 'profile_id', None) for profile in profiles
    }
    emitted_profile_ids.discard(None)
    known_standard_ids = set(gap_standard_ids)
    for entry in matrix.entries:
        if profiles:
            missing = sorted(
                set(entry.intended_profile_ids) - emitted_profile_ids
            )
            for profile_id in missing:
                errors.append(
                    f'{entry.source_id}: intended profile id {profile_id} '
                    'does not resolve to an emitted StandardsProfile'
                )
        if gap_standard_ids:
            missing_standards = sorted(
                set(entry.related_standard_ids) - known_standard_ids
            )
            for standard_id in missing_standards:
                errors.append(
                    f'{entry.source_id}: related standard id '
                    f'{standard_id} does not resolve to a gap-matrix '
                    'standard'
                )
        if entry.htdt_role == 'primary_private_theater_profile' and (
            entry.profile_class != 'private_theater_recommended_practice'
            or entry.conformance_scope != 'private_home_conformance'
        ):
            errors.append(
                f'{entry.source_id}: the primary private-theater profile '
                'must be a private-home conformance recommended practice'
            )
    return tuple(errors)


# ---------------------------------------------------------------------------
# Built-in source matrix (#839 §12 immediate matrix).
#
# Every entry pins the exact source identity, the checked-at review date
# and the explicit rights/access boundary. Entries not backed by a public
# authoritative boundary are NOT executable — the record exists so that a
# citation of the wrong document class fails closed instead of silently
# becoming home-theater truth.


def _entry(**kwargs: Any) -> SourceMatrixEntry:
    return SourceMatrixEntry(**kwargs)


def _rp22_entry() -> SourceMatrixEntry:
    return _entry(
        source_id='cedia-cta-rp22',
        name=(
            'CEDIA/CTA RP22 Immersive Audio Design Recommended Practice'
        ),
        publisher='CEDIA / CTA',
        document_version='v1.2 (September 2023)',
        checked_at='2026-10-07',
        source_uris=(
            'https://cedia.org/smart-home-professionals/advocacy/'
            'standards-best-practices/immersive-audio-design-excellence/',
            'https://www.cta.tech/standards/cediacta-rp22/',
        ),
        precedence='first_party_standard',
        profile_class='private_theater_recommended_practice',
        htdt_role='primary_private_theater_profile',
        source_status='executable',
        normative_access='mixed_official_surfaces',
        rights_class='derived_rules_allowed',
        redistribution_rights='unknown',
        definability='definable',
        conformance_scope='private_home_conformance',
        related_standard_ids=('cedia-cta-rp22-v1.2',),
        intended_profile_ids=(
            'cedia-cta-rp22-performance-level-1',
            'cedia-cta-rp22-performance-level-2',
            'cedia-cta-rp22-performance-level-3',
            'cedia-cta-rp22-performance-level-4',
            'cedia-cta-rp22-spatial-level-1',
            'cedia-cta-rp22-spatial-level-2',
            'cedia-cta-rp22-spatial-level-3',
            'cedia-cta-rp22-spatial-level-4',
        ),
        notes=(
            'The primary private-home engineering profile: four '
            'performance levels, 21 Appendix A parameters (detailed '
            'mapping owned by #579). Official surfaces expose different '
            'access paths (CEDIA download/catalog vs CTA commercial '
            'access); public URL accessibility does not establish '
            'unrestricted redistribution rights — #599 tracks '
            'rights/access separately from availability. The CEDIA '
            '2025-10-08 Reference Audio Level / SPL Capabilities white '
            'paper is a clarification, not a new edition: HTDT keeps '
            'MAX_CLEAN_CAPABILITY, CALIBRATION_LEVEL and '
            'USER_LISTENING_LEVEL distinct, and a high RP22 level is '
            'never a recommendation to listen continuously at '
            'theatrical reference level.'
        ),
    )


def _dolby_entry() -> SourceMatrixEntry:
    return _entry(
        source_id='dolby-atmos-home',
        name='Dolby Atmos Home Theater Installation Guidelines',
        publisher='Dolby Laboratories',
        document_version='R3.1 (2018-12-13)',
        checked_at='2026-10-07',
        source_uris=(
            'https://www.dolby.com/about/support/guide/'
            'dolby-atmos-speaker-setup',
            'https://www.dolby.com/about/support/guide/'
            'speaker-setup-guides',
        ),
        precedence='first_party_vendor_guidance',
        profile_class='format_vendor_home_guidance',
        htdt_role='format_layout_recommendation',
        source_status='executable',
        normative_access='mixed_official_surfaces',
        rights_class='derived_rules_allowed',
        redistribution_rights='unknown',
        definability='definable_bounded_to_source',
        conformance_scope='vendor_home_guidance_only',
        page_identity=(
            'Dolby speaker-setup guide pages, checked 2026-10-07'
        ),
        related_standard_ids=('dolby-atmos-home-r3.1',),
        intended_profile_ids=('dolby-atmos-home-5.1.2-layout',),
        notes=(
            'Format-vendor home guidance, not universal compliance: '
            'evaluation against these guides is never presented as '
            'Dolby certification or RP22 compliance. Version rule — '
            'PDF-derived criteria pin the exact PDF revision '
            '(R3.1, 2018-12-13); webpage-only guidance pins '
            'checked-at/page identity; old PDF values and later '
            'webpage text are never silently merged into a timeless '
            'DOLBY_ATMOS truth.'
        ),
    )


def _auro_entry() -> SourceMatrixEntry:
    return _entry(
        source_id='auro-3d-home',
        name='Auro-3D Home Theater Setup Guidelines',
        publisher='AURO-3D / NEWAURO BV',
        document_version='Rev.12 (2024-05-16)',
        checked_at='2026-10-07',
        source_uris=(
            'https://www.auro-3d.com/setup-guide/',
            'https://www.auro-3d.com/wp-content/uploads/2024/05/'
            'Auro-3D-Home-Theater-Setup-Guidelines-v12-20240516.pdf',
        ),
        precedence='first_party_vendor_guidance',
        profile_class='format_vendor_home_guidance',
        htdt_role='format_layout_recommendation',
        source_status='executable',
        normative_access='public_download',
        rights_class='derived_rules_allowed',
        redistribution_rights='unknown',
        definability='definable',
        conformance_scope='vendor_home_guidance_only',
        related_standard_ids=('auro-3d-home-rev12',),
        intended_profile_ids=('auro3d-home-layout',),
        notes=(
            'Current first-party versionable source — better authority '
            'than the old v6/v8 copies circulating on third-party '
            'sites. Rev.12 is the default source for new AURO '
            'mappings; historical older profiles are retained under '
            'their own revision identity, and AURO vendor guidance '
            'stays distinct from RP22 criteria that quote or '
            'reference AURO.'
        ),
    )


def _dtsx_entry() -> SourceMatrixEntry:
    return _entry(
        source_id='dts-x',
        name='DTS:X immersive audio public documentation',
        publisher='DTS, Inc. (Xperi)',
        document_version=(
            'no public authoritative criteria edition identified'
        ),
        checked_at='2026-10-07',
        source_uris=(
            'https://dts.com/dts-x/',
            'https://dts.com/insights/'
            'welcome-to-dtsx-open-immersive-and-flexible-object-based-'
            'audio-coming-to-cinema-and-home/',
        ),
        precedence='first_party_vendor_guidance',
        profile_class='format_vendor_home_guidance',
        htdt_role='vendor_capability_layout_statement',
        source_status='no_authoritative_numeric_criteria',
        normative_access='public_webpage',
        rights_class='reference_only',
        redistribution_rights='unknown',
        definability='definable_for_unknown',
        conformance_scope='vendor_home_guidance_only',
        page_identity=(
            'dts.com/dts-x and DTS insights article, checked 2026-10-07'
        ),
        related_standard_ids=('dts-x-home',),
        notes=(
            'Current DTS public material describes DTS:X as adaptable '
            'to the viewing environment, not tied to a prescribed '
            'speaker configuration and flexible in speaker layout — a '
            'useful negative result. HTDT does not create fixed DTS:X '
            'angle rules from third-party diagrams or competitor '
            'implementations; fixed numeric home-layout criteria stay '
            'UNKNOWN / NOT ADMITTED. An RP22 criterion that references '
            'DTS keeps source=RP22 — it is never relabelled as an '
            'independent DTS-authored rule.'
        ),
    )


def _itu_bs775_entry() -> SourceMatrixEntry:
    return _entry(
        source_id='itu-r-bs775',
        name='ITU-R BS.775 Multichannel stereophonic sound system',
        publisher='ITU-R',
        document_version='BS.775-4 (12/2022)',
        checked_at='2026-10-07',
        source_uris=(
            'https://www.itu.int/rec/R-REC-BS.775/',
        ),
        precedence='first_party_standard',
        profile_class='production_reference_layout',
        htdt_role='conventional_multichannel_reference',
        source_status='executable',
        normative_access='public_download',
        rights_class='public_open_standard',
        redistribution_rights='restricted',
        definability='definable',
        conformance_scope='non_home_reference',
        notes=(
            'In force, free download. Roles: conventional multichannel '
            'reference layout, coordinate/role reference, '
            'production/reproduction comparison. It is not a '
            'replacement for RP22 or format-vendor home guidance and '
            'never carries private-home conformance weight.'
        ),
    )


def _itu_bs2051_entry() -> SourceMatrixEntry:
    return _entry(
        source_id='itu-r-bs2051',
        name='ITU-R BS.2051 Advanced sound system',
        publisher='ITU-R',
        document_version='BS.2051-3 (05/2022)',
        checked_at='2026-10-07',
        source_uris=(
            'https://www.itu.int/rec/R-REC-BS.2051/',
        ),
        precedence='first_party_standard',
        profile_class='production_reference_layout',
        htdt_role='advanced_immersive_production_reference',
        source_status='executable',
        normative_access='public_download',
        rights_class='public_open_standard',
        redistribution_rights='restricted',
        definability='definable',
        conformance_scope='non_home_reference',
        notes=(
            'In force, free download. Roles: advanced sound-system '
            'role/position vocabulary, immersive reference-layout '
            'comparison, metadata/coordinate interoperability, '
            'production reference. Not private-home performance '
            'conformance.'
        ),
    )


def _itu_bs1116_entry() -> SourceMatrixEntry:
    return _entry(
        source_id='itu-r-bs1116',
        name='ITU-R BS.1116 Methods for the subjective assessment',
        publisher='ITU-R',
        document_version='BS.1116-3 (02/2015)',
        checked_at='2026-10-07',
        source_uris=(
            'https://www.itu.int/rec/R-REC-BS.1116/en',
        ),
        precedence='first_party_standard',
        profile_class='subjective_test_reference_room',
        htdt_role='critical_listening_test_reference',
        source_status='executable',
        normative_access='public_download',
        rights_class='public_open_standard',
        redistribution_rights='restricted',
        definability='definable_bounded_to_source',
        conformance_scope='non_home_reference',
        notes=(
            'In force, free download. Roles: the #778 '
            'critical-listening profile, controlled listening '
            'experiments, research-room comparison. Its room '
            'conditions are never imported as universal home-theater '
            'criteria unless explicitly selected — context-limited by '
            'construction.'
        ),
    )


def _avixa_entry(
    *,
    source_id: str,
    name: str,
    document_version: str,
    notes: str,
    source_status: SourceMatrixStatus = 'licensed_source_required',
    conflicting_observations: tuple[SourceConflictObservation, ...] = (),
    definability: SourceMatrixDefinability = 'partially_licensed',
) -> SourceMatrixEntry:
    return _entry(
        source_id=source_id,
        name=name,
        publisher='AVIXA',
        document_version=document_version,
        checked_at='2026-10-07',
        source_uris=(
            'https://www.avixa.org/resources/standards/'
            'published-standards',
        ),
        precedence='first_party_standard',
        profile_class='av_system_measurement_standard',
        htdt_role='measurement_design_commissioning_procedure',
        source_status=source_status,
        normative_access='public_catalog_metadata',
        rights_class='public_metadata_only',
        redistribution_rights='prohibited',
        definability=definability,
        conformance_scope='non_home_reference',
        conflicting_observations=conflicting_observations,
        notes=notes,
    )


def _avixa_entries() -> tuple[SourceMatrixEntry, ...]:
    return (
        _avixa_entry(
            source_id='avixa-a102',
            name='AVIXA A102 Audio Coverage Uniformity',
            document_version='A102.01:2017',
            notes=(
                'Official metadata confirmed via the AVIXA '
                'published-standards catalog; the normative document '
                'is licensed. Useful for HTDT design/commissioning '
                'semantics — PUBLIC_METADATA_CONFIRMED is not '
                'NORMATIVE_PROFILE_AVAILABLE, so an exact evaluator '
                'requires lawful access to the document.'
            ),
        ),
        _avixa_entry(
            source_id='avixa-a103',
            name='AVIXA A103 Sound System Spectral Balance',
            document_version='A103.01 (official revision conflict)',
            source_status='source_conflict',
            definability='partially_licensed',
            conflicting_observations=(
                SourceConflictObservation(
                    observation_id='avixa-a103-detail-page',
                    claim='A103.01:2022',
                    surface='AVIXA standard detail page',
                    source_uri=(
                        'https://www.avixa.org/resources/standards/'
                        'published-standards'
                    ),
                    tier='standards_body_catalog',
                    observed_at='2026-10-07',
                ),
                SourceConflictObservation(
                    observation_id='avixa-a103-catalog',
                    claim='A103.01:2023',
                    surface='AVIXA published-standards catalog',
                    source_uri=(
                        'https://www.avixa.org/resources/standards/'
                        'published-standards'
                    ),
                    tier='standards_body_catalog',
                    observed_at='2026-10-07',
                ),
            ),
            notes=(
                'Official AVIXA surfaces disagree on the current A103 '
                'revision: a detail page shows ANSI/AVIXA '
                'A103.01:2022 while the current catalog shows '
                'A103.01:2023. The matrix preserves SOURCE_CONFLICT '
                'rather than guessing — both observations are kept '
                'and no revision is selected. Licensed normative '
                'content still requires lawful access via #599.'
            ),
        ),
        _avixa_entry(
            source_id='avixa-a104',
            name='AVIXA A104 Dynamic Range in Audiovisual Systems',
            document_version='A104.01:2017',
            notes=(
                'Official metadata confirmed via the AVIXA '
                'published-standards catalog; the normative document '
                'is licensed. An exact evaluator requires lawful '
                'access to the relevant document/profile — public '
                'summaries do not expose the normative criteria.'
            ),
        ),
        _avixa_entry(
            source_id='avixa-v202',
            name='AVIXA V202 Display Image Size for 2D Content',
            document_version='V202.01:2020',
            notes=(
                'Official metadata confirmed via the AVIXA '
                'published-standards catalog; the normative document '
                'is licensed. Licensed-profile ingestion arrives '
                'through the #599 licensed-source path, never '
                'through paraphrased third-party values.'
            ),
        ),
        _avixa_entry(
            source_id='avixa-d402',
            name='AVIXA D402 AV Systems Performance Verification',
            document_version='D402.01:2022',
            notes=(
                'Official metadata confirmed via the AVIXA '
                'published-standards catalog; the normative document '
                'is licensed. Measurement/design/commissioning '
                'procedure authority — never a private-home '
                'conformance source.'
            ),
        ),
    )


def builtin_standards_source_matrix(
    *,
    matrix_version: str = BUILTIN_SOURCE_MATRIX_VERSION,
    created_at_utc: str,
) -> StandardsSourceMatrix:
    """Emit the #839 §12 source matrix as a sealed built-in record.

    The rows are static declarations pinned by the regression suite —
    HTDT's statement of which external documents exist, what role each
    may play, and where each is blocked. A row that cannot lawfully or
    honestly drive an evaluator is never marked ``executable``.
    """

    entries: tuple[SourceMatrixEntry, ...] = (
        _rp22_entry(),
        _dolby_entry(),
        _auro_entry(),
        _dtsx_entry(),
        _itu_bs775_entry(),
        _itu_bs2051_entry(),
        _itu_bs1116_entry(),
        *_avixa_entries(),
    )
    matrix = build_standards_source_matrix(
        entries=entries,
        matrix_version=matrix_version,
        created_at_utc=created_at_utc,
    )
    errors = validate_source_matrix(matrix)
    if errors:
        raise ValueError(
            'built-in source matrix is not internally consistent: '
            + '; '.join(errors)
        )
    return matrix


__all__ = [
    'BUILTIN_SOURCE_MATRIX_VERSION',
    'SourceAccess',
    'SourceConflictObservation',
    'SourceConformanceScope',
    'SourceMatrixDefinability',
    'SourceMatrixEntry',
    'SourceMatrixRole',
    'SourceMatrixStatus',
    'SourcePrecedence',
    'SourceRedistribution',
    'StandardsProfileClass',
    'StandardsSourceMatrix',
    'STANDARDS_SOURCE_MATRIX_AUTHORITY_VERSION',
    'STANDARDS_SOURCE_MATRIX_ID_PREFIX',
    'build_standards_source_matrix',
    'builtin_standards_source_matrix',
    'validate_source_matrix',
]
