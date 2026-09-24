"""Cross-workspace result trust presentation (#592).

HTDT's backend carries precise distinctions (measured / predicted / derived /
proposed; candidate / validated / production; current / stale; bounded /
sampled / unknown uncertainty). This module introduces the shared read-side
grammar — :class:`ResultTrustSummary` — generated *from* canonical domain
authority, never stored as new truth.

UX invariants encoded here:

* **No universal confidence score.** Evidence class, validation scope,
  freshness, applicability and uncertainty are independent dimensions and
  are never collapsed into a single number or traffic light.
* **Numbers inherit trust context.** :meth:`ResultTrustSummary.compact_text`
  renders the headline value together with its trust qualifiers so a plot or
  card cannot show a crisp number with the caveat hidden elsewhere.
* **Traces carry evidence identity.** :func:`trace_identity` produces legend
  tokens distinguishing measured vs predicted and current vs stale without
  relying on color alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .localization import PresentationLocale, term_text, TermId


TRUST_SCHEMA_VERSION = 1


class EvidenceClass(StrEnum):
    """What kind of authority produced a displayed value."""

    MEASURED = 'measured'
    PREDICTED = 'predicted'
    DERIVED = 'derived'
    PROPOSED = 'proposed'
    AS_BUILT_OBSERVATION = 'as_built_observation'
    HYPOTHESIS = 'hypothesis'


class ValidationScope(StrEnum):
    """How far the producing evidence/model has been validated.

    Ordering is informational only — presentation must not map these onto a
    numeric confidence score.
    """

    UNVALIDATED = 'unvalidated'
    SYNTHETIC_FIXTURE = 'synthetic_fixture'
    SOFTWARE_VALIDATED = 'software_validated'
    OWNED_ROOM_VALIDATED = 'owned_room_validated'
    PRODUCTION_QUALIFIED = 'production_qualified'


class FreshnessState(StrEnum):
    """Whether the result is current for the active SceneRevision/Variant."""

    CURRENT = 'current'
    STALE = 'stale'
    HISTORICAL = 'historical'
    INCOMPLETE_DEPENDENCY = 'incomplete_dependency'
    UNSUPPORTED = 'unsupported'


class ApplicabilityDimension(StrEnum):
    GEOMETRY = 'geometry'
    FREQUENCY_BAND = 'frequency_band'
    SOURCE_ROUTING = 'source_routing'
    RECEIVER_POPULATION = 'receiver_population'
    OPERATING_STATE = 'operating_state'
    EQUIPMENT_VARIANT = 'equipment_variant'
    STANDARD_SCOPE = 'standard_scope'


class ApplicabilityNote(BaseModel):
    """One bounded "valid for" statement; absent = not characterized."""

    model_config = ConfigDict(frozen=True)

    dimension: ApplicabilityDimension
    description: str = Field(min_length=1)


class UncertaintyKind(StrEnum):
    BOUNDED = 'bounded'
    SAMPLED_DISTRIBUTION = 'sampled_distribution'
    REPEATABILITY = 'repeatability'
    ESTIMATE_CONFIDENCE = 'estimate_confidence'
    UNKNOWN = 'unknown'


class UncertaintyPresentation(BaseModel):
    """How uncertainty is characterized — never a confidence score."""

    model_config = ConfigDict(frozen=True)

    kind: UncertaintyKind
    # e.g. '±1.8 dB' (bounded/repeatability) or 'HIGH' (estimate confidence)
    summary: str | None = None
    sample_count: int | None = Field(default=None, ge=1)
    feasible_fraction: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def valid_uncertainty(self) -> 'UncertaintyPresentation':
        if self.kind == UncertaintyKind.SAMPLED_DISTRIBUTION:
            if self.sample_count is None:
                raise ValueError('sampled uncertainty requires sample_count')
        if self.kind == UncertaintyKind.UNKNOWN and (
            self.summary or self.sample_count or self.feasible_fraction is not None
        ):
            raise ValueError('unknown uncertainty cannot carry a characterization')
        return self


_EVIDENCE_LABELS: dict[EvidenceClass, dict[PresentationLocale, str]] = {
    EvidenceClass.MEASURED: {
        PresentationLocale.JAPANESE: '実測',
        PresentationLocale.ENGLISH: 'Measured',
    },
    EvidenceClass.PREDICTED: {
        PresentationLocale.JAPANESE: '予測',
        PresentationLocale.ENGLISH: 'Predicted',
    },
    EvidenceClass.DERIVED: {
        PresentationLocale.JAPANESE: '導出',
        PresentationLocale.ENGLISH: 'Derived',
    },
    EvidenceClass.PROPOSED: {
        PresentationLocale.JAPANESE: '提案',
        PresentationLocale.ENGLISH: 'Proposed',
    },
    EvidenceClass.AS_BUILT_OBSERVATION: {
        PresentationLocale.JAPANESE: '設置観測',
        PresentationLocale.ENGLISH: 'As-built observation',
    },
    EvidenceClass.HYPOTHESIS: {
        PresentationLocale.JAPANESE: '仮説',
        PresentationLocale.ENGLISH: 'Hypothesis',
    },
}

_SCOPE_LABELS: dict[ValidationScope, dict[PresentationLocale, str]] = {
    ValidationScope.UNVALIDATED: {
        PresentationLocale.JAPANESE: '未検証',
        PresentationLocale.ENGLISH: 'unvalidated',
    },
    ValidationScope.SYNTHETIC_FIXTURE: {
        PresentationLocale.JAPANESE: '合成fixture検証',
        PresentationLocale.ENGLISH: 'synthetic fixture',
    },
    ValidationScope.SOFTWARE_VALIDATED: {
        PresentationLocale.JAPANESE: 'ソフトウェア検証済み',
        PresentationLocale.ENGLISH: 'software validated',
    },
    ValidationScope.OWNED_ROOM_VALIDATED: {
        PresentationLocale.JAPANESE: '実室検証済み',
        PresentationLocale.ENGLISH: 'owned-room validated',
    },
    ValidationScope.PRODUCTION_QUALIFIED: {
        PresentationLocale.JAPANESE: 'production適格',
        PresentationLocale.ENGLISH: 'production qualified',
    },
}

_FRESHNESS_LABELS: dict[FreshnessState, dict[PresentationLocale, str]] = {
    FreshnessState.CURRENT: {
        PresentationLocale.JAPANESE: '現在',
        PresentationLocale.ENGLISH: 'current',
    },
    FreshnessState.STALE: {
        PresentationLocale.JAPANESE: '古い',
        PresentationLocale.ENGLISH: 'stale',
    },
    FreshnessState.HISTORICAL: {
        PresentationLocale.JAPANESE: '履歴',
        PresentationLocale.ENGLISH: 'historical',
    },
    FreshnessState.INCOMPLETE_DEPENDENCY: {
        PresentationLocale.JAPANESE: '依存が不完全',
        PresentationLocale.ENGLISH: 'incomplete dependency',
    },
    FreshnessState.UNSUPPORTED: {
        PresentationLocale.JAPANESE: '未対応',
        PresentationLocale.ENGLISH: 'unsupported',
    },
}


class ResultTrustSummary(BaseModel):
    """The shared trust grammar attached to any displayed result.

    Assembled from canonical domain authority at read time — this model is a
    presentation projection, not a stored claim. There is deliberately no
    composite score field.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = TRUST_SCHEMA_VERSION
    evidence_class: EvidenceClass
    validation_scope: ValidationScope = ValidationScope.UNVALIDATED
    freshness: FreshnessState = FreshnessState.CURRENT
    applicability: tuple[ApplicabilityNote, ...] = ()
    uncertainty: UncertaintyPresentation = UncertaintyPresentation(
        kind=UncertaintyKind.UNKNOWN
    )
    # Deep link into the authority/provenance graph explorer (#590).
    provenance_ref: str | None = None
    # For STALE results: at least one concrete invalidating dependency.
    invalidating_dependencies: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_trust(self) -> 'ResultTrustSummary':
        if len(self.applicability) != len(
            {note.dimension for note in self.applicability}
        ):
            raise ValueError('applicability dimensions must be unique')
        if self.freshness == FreshnessState.STALE and not self.invalidating_dependencies:
            raise ValueError(
                'stale results must name at least one invalidating dependency'
            )
        if self.freshness != FreshnessState.STALE and self.invalidating_dependencies:
            raise ValueError('invalidating dependencies require stale freshness')
        return self

    def label(self, locale: PresentationLocale = PresentationLocale.JAPANESE) -> str:
        """The evidence-class wording in the requested locale."""

        return _EVIDENCE_LABELS[self.evidence_class].get(
            locale, _EVIDENCE_LABELS[self.evidence_class][PresentationLocale.ENGLISH]
        )

    def compact_text(
        self,
        headline: str,
        *,
        locale: PresentationLocale = PresentationLocale.JAPANESE,
    ) -> str:
        """``headline · evidence · scope · key applicability`` rendering.

        Example: ``72.4 dB SPL · Predicted · provider candidate · 20–120 Hz``.
        The trust qualifiers always travel with the number.
        """

        parts = [headline, self.label(locale)]
        scope = _SCOPE_LABELS[self.validation_scope].get(
            locale, _SCOPE_LABELS[self.validation_scope][PresentationLocale.ENGLISH]
        )
        if self.validation_scope != ValidationScope.PRODUCTION_QUALIFIED:
            parts.append(scope)
        if self.freshness != FreshnessState.CURRENT:
            parts.append(
                _FRESHNESS_LABELS[self.freshness].get(
                    locale,
                    _FRESHNESS_LABELS[self.freshness][PresentationLocale.ENGLISH],
                )
            )
        if self.uncertainty.kind != UncertaintyKind.UNKNOWN and self.uncertainty.summary:
            parts.append(self.uncertainty.summary)
        elif self.uncertainty.kind == UncertaintyKind.SAMPLED_DISTRIBUTION:
            if self.uncertainty.feasible_fraction is not None:
                pct = f'{self.uncertainty.feasible_fraction * 100:.0f}%'
                parts.append(
                    f'{pct} / {self.uncertainty.sample_count} samples'
                )
        for note in self.applicability[:2]:
            parts.append(note.description)
        return ' · '.join(parts)


@dataclass(frozen=True, slots=True)
class TraceIdentity:
    """Legend identity for one plotted trace — text, never color-only."""

    evidence_class: EvidenceClass
    freshness: FreshnessState
    label_suffix: str

    def legend_token(self, locale: PresentationLocale) -> str:
        base = _EVIDENCE_LABELS[self.evidence_class].get(
            locale, _EVIDENCE_LABELS[self.evidence_class][PresentationLocale.ENGLISH]
        )
        if self.freshness == FreshnessState.CURRENT:
            return base
        stale = _FRESHNESS_LABELS[self.freshness].get(
            locale, _FRESHNESS_LABELS[self.freshness][PresentationLocale.ENGLISH]
        )
        return f'{base} ({stale})'


def trace_identity(
    trust: ResultTrustSummary,
    *,
    label_suffix: str = '',
) -> TraceIdentity:
    """Project a result's trust summary into a plot-legend identity."""

    return TraceIdentity(
        evidence_class=trust.evidence_class,
        freshness=trust.freshness,
        label_suffix=label_suffix,
    )


__all__ = [
    'ApplicabilityDimension',
    'ApplicabilityNote',
    'EvidenceClass',
    'FreshnessState',
    'ResultTrustSummary',
    'TRUST_SCHEMA_VERSION',
    'TraceIdentity',
    'UncertaintyKind',
    'UncertaintyPresentation',
    'ValidationScope',
    'trace_identity',
]
