"""Versioned interoperability compatibility corpus authority (#892).

Parsers and exporters each carry their own unit tests; what they cannot
catch alone is a semantic drift between them — a version bump, an
optional field, a unit/coordinate convention, a vendor quirk, or a
silently dropped unsupported command turning a real-world round-trip
into a lie. This module is the sealed, versioned contract that corpus
answers to:

- :class:`InteropFixtureEntry` is the sealed per-fixture declaration:
  provenance (source kind, producer tool/version, licence and
  redistribution state), format id/version, the payload pin
  (``content_sha256`` + ``size_bytes``), the declared unit/coordinate
  semantics, the expected supported/unsupported feature sets, the exact
  warning/degradation token set the importer is expected to surface, the
  canonical semantic assertions, and the exercise mode
  (``round_trip`` / ``import_only`` / ``unsupported_assert``).
- :class:`InteropCorpusManifest` seals the whole corpus into one
  versioned manifest (``corpus_version`` + ``manifest_sha256``) that
  release verification and run records pin.
- :class:`InteropFixtureRunRecord` is the sealed per-fixture run record
  (``icr-``): importer/exporter versions exercised, every assertion
  outcome (``holds`` / ``violated`` / ``skipped_unverifiable``), the
  observed warning/degradation sets, the imported vs re-imported
  semantic hashes, and the verdict —
  ``semantically_equal`` / ``degraded_as_declared`` /
  ``unsupported_as_declared`` / ``regression`` / ``unexpected_failure``.
- :class:`InteropCorpusRunRecord` is the sealed whole-corpus run record
  (``icx-``) binding every fixture run to the exact manifest it ran
  against; its verdict is ``regression_detected`` when any fixture
  regressed or failed unexpectedly.

Honesty rules (fail-closed):

- A fixture whose declared ``unsupported`` verdict is observed never
  counts as compatibility evidence; it counts as
  ``unsupported_as_declared`` — a PASS only in the negative sense the
  declaration carries.
- Declared warnings/degradations are compared as *sets*: an undeclared
  degradation OR a declared degradation that no longer appears is a
  ``regression`` — silence in either direction is a semantic change.
- Unknown/unsupported source material must follow the family policy:
  preserved-as-opaque or fail-closed rejection — the assertion set
  names which policy applies, and a fixture that violates it is a
  ``regression``, never an unverifiable pass.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


INTEROP_CORPUS_AUTHORITY_VERSION = 'interop-corpus-1'
INTEROP_CORPUS_SCHEMA_VERSION: Literal[1] = 1

_SHA256 = r'^[0-9a-f]{64}$'

InteropFormatFamily = Literal[
    'rew_text',
    'equalizer_apo',
    'camilladsp',
    'clf',
    'ifc_step',
    'htdt_project_bundle',
]
"""Strategic external-interchange families exercised by the corpus (#892
§initial format families), restricted to lanes an in-repo importer or
qualifier actually implements — support is never invented."""

InteropRoundTripMode = Literal[
    'round_trip',
    'import_only',
    'unsupported_assert',
]
"""How a fixture exercises its family:

- ``round_trip`` — EXTERNAL FIXTURE → IMPORT → canonical state → EXPORT
  → RE-IMPORT → SEMANTIC COMPARE (issue #892 goal line);
- ``import_only`` — IMPORT → canonical state → semantic assertions, for
  families with no exporter or where export is a different contract;
- ``unsupported_assert`` — qualify/classify → the declared
  unsupported/rejected verdict must be observed verbatim (licensed
  binaries and formats HTDT deliberately never parses).
"""

InteropAssertionComparator = Literal[
    'equals',
    'approx',
    'set_equals',
    'contains',
]
"""Assertion comparators — ``approx`` carries ``tolerance``; ``contains``
and ``set_equals`` operate on the observed collection/string."""

InteropAssertionState = Literal[
    'holds',
    'violated',
    'skipped_unverifiable',
]
"""Per-assertion outcome. ``skipped_unverifiable`` is reserved for
assertions the lane could not evaluate at all (e.g. an optional export
lane absent by design); it never counts toward a pass."""

InteropFixtureVerdict = Literal[
    'semantically_equal',
    'degraded_as_declared',
    'unsupported_as_declared',
    'regression',
    'unexpected_failure',
]
"""Corpus-run verdict per fixture (#892 / REV70 slice contract).

- ``semantically_equal`` — every declared assertion holds, the observed
  warning/degradation sets equal the declared sets (both may be empty),
  and a round-trip lane re-imports to a semantically equal state;
- ``degraded_as_declared`` — same, with a non-empty declared
  warning/degradation set observed exactly;
- ``unsupported_as_declared`` — an ``unsupported_assert`` fixture whose
  declared verdict/family was observed exactly;
- ``regression`` — any violated assertion, an undeclared degradation, a
  declared degradation not observed, or a round-trip semantic mismatch;
- ``unexpected_failure`` — the lane raised, the fixture drifted from its
  pin, or the record could not be produced.
"""

InteropCorpusVerdict = Literal['passed', 'regression_detected']
"""A corpus run passes only when no fixture reports ``regression`` or
``unexpected_failure``. Degraded-as-declared is a pass — degradation is
evidence, not failure, when declared."""


class InteropSemanticAssertion(BaseModel):
    """One canonical semantic assertion over a lane's observed state.

    ``path`` names the observed-semantics key (a stable, documented
    dotted path the family lane emits — e.g. ``channel.ALL.preamp_db``,
    ``import.verdict``, ``row_count``); a failure reports this exact
    path, satisfying the "failures identify the exact semantic
    assertion" acceptance criterion.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    path: str = Field(min_length=1)
    comparator: InteropAssertionComparator = 'equals'
    expected: Any = None
    tolerance: float | None = Field(default=None, gt=0.0)
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'InteropSemanticAssertion':
        if self.comparator == 'approx' and self.tolerance is None:
            raise ValueError('approx assertions require a tolerance')
        if self.comparator != 'approx' and self.tolerance is not None:
            raise ValueError(
                'tolerance is only meaningful for approx assertions'
            )
        if self.comparator in ('set_equals', 'contains') and not isinstance(
            self.expected, (list, tuple, str)
        ):
            raise ValueError(
                'set_equals/contains assertions expect a sequence or string'
            )
        return self


class InteropFixtureProvenance(BaseModel):
    """Where a fixture came from and whether it may be redistributed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_kind: Literal[
        'repo_synthesized', 'external_licensed', 'external_no_license'
    ]
    producer_tool: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    origin_note: str = ''
    licence_id: str = Field(min_length=1)
    redistribution_state: Literal[
        'redistributable', 'repo_synthesized', 'withheld'
    ]

    @model_validator(mode='after')
    def _check(self) -> 'InteropFixtureProvenance':
        if self.source_kind == 'external_no_license' and (
            self.redistribution_state != 'withheld'
        ):
            raise ValueError(
                'unlicensed external fixtures must be withheld, never '
                'redistributed'
            )
        return self


class InteropFixtureEntry(BaseModel):
    """Sealed declaration of one corpus fixture (#892 corpus metadata)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INTEROP_CORPUS_SCHEMA_VERSION
    fixture_id: str = Field(min_length=1)
    format_family: InteropFormatFamily
    format_version: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    content_sha256: str = Field(pattern=_SHA256)
    size_bytes: int = Field(ge=0)
    round_trip_mode: InteropRoundTripMode
    provenance: InteropFixtureProvenance
    units_semantics: str = ''
    coordinate_semantics: str = ''
    expected_supported_features: tuple[str, ...] = ()
    expected_unsupported_features: tuple[str, ...] = ()
    expected_warnings: tuple[str, ...] = ()
    expected_degradations: tuple[str, ...] = ()
    assertions: tuple[InteropSemanticAssertion, ...] = ()
    fixture_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def _check(self) -> 'InteropFixtureEntry':
        if '\\' in self.relative_path or self.relative_path.startswith('/'):
            raise ValueError('relative_path must be a posix corpus path')
        for collection, name in (
            (self.expected_supported_features, 'expected_supported_features'),
            (self.expected_unsupported_features, 'expected_unsupported_features'),
            (self.expected_warnings, 'expected_warnings'),
            (self.expected_degradations, 'expected_degradations'),
        ):
            if len(set(collection)) != len(collection):
                raise ValueError(f'{name} must be unique')
        paths = [a.path for a in self.assertions]
        if len(paths) != len(set(paths)):
            raise ValueError('assertion paths must be unique')
        if self.fixture_sha256 != _hash(self.identity_payload()):
            raise ValueError('interop fixture entry hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'fixture_id', 'fixture_sha256'}
        )


def build_interop_fixture_entry(**payload: Any) -> InteropFixtureEntry:
    """Seal a fixture entry — ``fixture_id``/``fixture_sha256`` derive."""
    probe = InteropFixtureEntry.model_construct(
        **canonicalize_payload(
            InteropFixtureEntry,
            dict(
                schema_version=INTEROP_CORPUS_SCHEMA_VERSION,
                fixture_id='',
                fixture_sha256='',
                **payload,
            ),
        )
    )
    digest = _hash(probe.identity_payload())
    return InteropFixtureEntry(
        **probe.model_dump(mode='python', exclude={'fixture_id', 'fixture_sha256'}),
        fixture_id=f'icf-{digest[:24]}',
        fixture_sha256=digest,
    )


class InteropCorpusManifest(BaseModel):
    """Sealed, versioned manifest of the interoperability corpus."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INTEROP_CORPUS_SCHEMA_VERSION
    authority_version: Literal['interop-corpus-1'] = (
        INTEROP_CORPUS_AUTHORITY_VERSION
    )
    manifest_id: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_SHA256)
    corpus_id: str = Field(min_length=1)
    corpus_version: str = Field(min_length=1)
    issued_on: str = Field(min_length=1)
    fixtures: tuple[InteropFixtureEntry, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'InteropCorpusManifest':
        ids = [f.fixture_id for f in self.fixtures]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate fixture ids')
        paths = [f.relative_path for f in self.fixtures]
        if len(paths) != len(set(paths)):
            raise ValueError('duplicate fixture paths')
        families = {f.format_family for f in self.fixtures}
        if not families:
            raise ValueError('corpus must cover at least one family')
        if self.manifest_sha256 != _hash(self.identity_payload()):
            raise ValueError('interop corpus manifest hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'manifest_id', 'manifest_sha256'}
        )

    @classmethod
    def create(
        cls,
        *,
        corpus_id: str,
        corpus_version: str,
        issued_on: str,
        fixtures: tuple[InteropFixtureEntry, ...] | list[InteropFixtureEntry],
    ) -> 'InteropCorpusManifest':
        probe = cls.model_construct(
            **canonicalize_payload(
                cls,
                dict(
                    schema_version=INTEROP_CORPUS_SCHEMA_VERSION,
                    authority_version=INTEROP_CORPUS_AUTHORITY_VERSION,
                    manifest_id='',
                    manifest_sha256='',
                    corpus_id=corpus_id,
                    corpus_version=corpus_version,
                    issued_on=issued_on,
                    fixtures=tuple(fixtures),
                ),
            )
        )
        digest = _hash(probe.identity_payload())
        return cls(
            **probe.model_dump(
                mode='python', exclude={'manifest_id', 'manifest_sha256'}
            ),
            manifest_sha256=digest,
            manifest_id=f'icm-{digest[:24]}',
        )


class InteropAssertionOutcome(BaseModel):
    """The recorded outcome of evaluating one declared assertion."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    path: str = Field(min_length=1)
    comparator: InteropAssertionComparator
    state: InteropAssertionState
    expected_repr: str = ''
    observed_repr: str = ''
    detail: str = ''


class InteropFixtureRunRecord(BaseModel):
    """Sealed run record for one corpus fixture (#892 harness evidence).

    ``imported_semantic_sha256``/``reimported_semantic_sha256`` pin the
    canonical states actually produced; on a ``round_trip`` lane they
    must carry the same semantic hash for ``semantically_equal`` —
    byte equality is never required where the format admits equivalent
    representations.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INTEROP_CORPUS_SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    run_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    fixture_ref: AuthorityRef
    manifest_sha256: str = Field(pattern=_SHA256)
    corpus_version: str = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    format_family: InteropFormatFamily
    round_trip_mode: InteropRoundTripMode
    verdict: InteropFixtureVerdict
    outcomes: tuple[InteropAssertionOutcome, ...] = ()
    observed_warnings: tuple[str, ...] = ()
    observed_degradations: tuple[str, ...] = ()
    imported_semantic_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    reimported_semantic_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    detail: str = ''
    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'InteropFixtureRunRecord':
        if self.fixture_ref.ref_sha256 is None:
            raise ValueError('fixture_ref must pin the fixture sha256')
        if self.fixture_ref.kind != 'interop_fixture':
            raise ValueError('fixture_ref kind must be interop_fixture')
        if self.run_sha256 != _hash(self.identity_payload()):
            raise ValueError('interop fixture run hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'run_id', 'run_sha256'}
        )


def build_interop_fixture_run(**payload: Any) -> InteropFixtureRunRecord:
    """Seal a fixture run record — ``run_id``/``run_sha256`` derive."""
    probe = InteropFixtureRunRecord.model_construct(
        **canonicalize_payload(
            InteropFixtureRunRecord,
            dict(
                schema_version=INTEROP_CORPUS_SCHEMA_VERSION,
                run_id='',
                run_sha256='',
                **payload,
            ),
        )
    )
    digest = _hash(probe.identity_payload())
    return InteropFixtureRunRecord(
        **probe.model_dump(mode='python', exclude={'run_id', 'run_sha256'}),
        run_id=f'icr-{digest[:24]}',
        run_sha256=digest,
    )


class InteropCorpusRunRecord(BaseModel):
    """Sealed whole-corpus run record — the releasable verdict (#892)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INTEROP_CORPUS_SCHEMA_VERSION
    corpus_run_id: str = Field(min_length=1)
    corpus_run_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    manifest_ref: AuthorityRef
    corpus_version: str = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    verdict: InteropCorpusVerdict
    fixture_run_ids: tuple[str, ...] = Field(min_length=1)
    verdict_counts: dict[str, int] = Field(default_factory=dict)
    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'InteropCorpusRunRecord':
        if self.manifest_ref.ref_sha256 is None:
            raise ValueError('manifest_ref must pin the manifest sha256')
        if self.manifest_ref.kind != 'interop_corpus_manifest':
            raise ValueError(
                'manifest_ref kind must be interop_corpus_manifest'
            )
        if len(set(self.fixture_run_ids)) != len(self.fixture_run_ids):
            raise ValueError('duplicate fixture run ids')
        if self.corpus_run_sha256 != _hash(self.identity_payload()):
            raise ValueError('interop corpus run hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'corpus_run_id', 'corpus_run_sha256'}
        )


def build_interop_corpus_run(**payload: Any) -> InteropCorpusRunRecord:
    """Seal a corpus run record — ``corpus_run_id``/``run_sha256`` derive."""
    probe = InteropCorpusRunRecord.model_construct(
        **canonicalize_payload(
            InteropCorpusRunRecord,
            dict(
                schema_version=INTEROP_CORPUS_SCHEMA_VERSION,
                corpus_run_id='',
                corpus_run_sha256='',
                **payload,
            ),
        )
    )
    digest = _hash(probe.identity_payload())
    return InteropCorpusRunRecord(
        **probe.model_dump(
            mode='python', exclude={'corpus_run_id', 'corpus_run_sha256'}
        ),
        corpus_run_id=f'icx-{digest[:24]}',
        corpus_run_sha256=digest,
    )


def corpus_run_verdict(
    runs: tuple[InteropFixtureRunRecord, ...] | list[InteropFixtureRunRecord],
) -> InteropCorpusVerdict:
    """Aggregate fixture verdicts — fail closed on regression/failure."""
    for run in runs:
        if run.verdict in ('regression', 'unexpected_failure'):
            return 'regression_detected'
    return 'passed'


__all__ = [
    'INTEROP_CORPUS_AUTHORITY_VERSION',
    'INTEROP_CORPUS_SCHEMA_VERSION',
    'InteropAssertionComparator',
    'InteropAssertionOutcome',
    'InteropAssertionState',
    'InteropCorpusManifest',
    'InteropCorpusRunRecord',
    'InteropCorpusVerdict',
    'InteropFixtureEntry',
    'InteropFixtureProvenance',
    'InteropFixtureRunRecord',
    'InteropFixtureVerdict',
    'InteropFormatFamily',
    'InteropRoundTripMode',
    'InteropSemanticAssertion',
    'build_interop_corpus_run',
    'build_interop_fixture_entry',
    'build_interop_fixture_run',
    'corpus_run_verdict',
]
