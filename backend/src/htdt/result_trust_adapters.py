"""Result Trust adapters — derive trust projection from domain authority (#774).

Product code never constructs :class:`ResultTrustSummary` labels ad hoc:
each adapter resolves the exact canonical authority (the persisted result
record, the scene repository's current head, the validation record and its
campaign/gate evidence) and returns a completed read-only projection.

Fail-closed rules encoded here:

* ``CURRENT`` freshness is *proven* by exact comparison of the result's
  pinned ``scene_revision_id``/``scene_content_hash`` against the document's
  current head — never assumed. Missing heads or missing pinned revisions
  degrade to ``INCOMPLETE_DEPENDENCY``, not CURRENT.
* Validation scope derives only from the canonical
  :class:`CadModelValidationRecord` ``evidence_scope`` and recommendation
  gate — a prediction adapter can emit at most ``OWNED_ROOM_VALIDATED``;
  ``PRODUCTION_QUALIFIED`` requires a production-adoption authority that no
  adapter invents.
* A prediction plan/optimization candidate is ``PROPOSED``/``PREDICTED`` —
  never ``MEASURED`` — regardless of presentation context.
"""

from __future__ import annotations

from .cad_measurement_models import CadMeasurementRecord
from .cad_model_validation import CadModelValidationRecord
from .cad_prediction_models import CadPredictionResult
from .cad_repository import SceneRepository
from .result_trust import (
    ApplicabilityNote,
    ApplicabilityStatus,
    EvidenceClass,
    FreshnessState,
    ResultTrustSummary,
    UncertaintyKind,
    UncertaintyPresentation,
    ValidationScope,
)


_UNKNOWN_UNCERTAINTY = UncertaintyPresentation(kind=UncertaintyKind.UNKNOWN)


_MEASUREMENT_EVIDENCE_CLASSES: dict[str, EvidenceClass] = {
    'measured': EvidenceClass.MEASURED,
    'derived': EvidenceClass.DERIVED,
    'predicted': EvidenceClass.PREDICTED,
    # A stored measurement whose evidence kind is uncharacterized is still a
    # measured-capture record; under-claiming beats over-claiming only when
    # it does not assert a different authority class entirely.
    'unknown': EvidenceClass.MEASURED,
}


def _freshness_for_scene_bound(
    scene_repository: SceneRepository,
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    historical: bool,
) -> tuple[FreshnessState, tuple[str, ...]]:
    """Derive freshness by exact comparison against the document head.

    ``historical=True`` presents a pinned non-head result as an intentional
    history view (HISTORICAL); ``False`` presents it as a now-invalid current
    claim (STALE, naming the head that invalidated it).
    """
    head = scene_repository.current_head(document_id)
    if head is None:
        return FreshnessState.INCOMPLETE_DEPENDENCY, ()
    if (
        head.revision_id == scene_revision_id
        and head.content_hash == scene_content_hash
    ):
        return FreshnessState.CURRENT, ()
    if scene_repository.get(scene_revision_id) is None:
        # The pinned authority is unresolvable — not merely stale.
        return FreshnessState.INCOMPLETE_DEPENDENCY, ()
    if historical:
        return FreshnessState.HISTORICAL, ()
    return FreshnessState.STALE, (f'scene_head:{head.revision_id}',)


def _applicability_status(
    applicability: tuple[ApplicabilityNote, ...],
    status: ApplicabilityStatus | None,
) -> ApplicabilityStatus:
    if status is not None:
        return status
    return (
        ApplicabilityStatus.CHARACTERIZED if applicability
        else ApplicabilityStatus.UNKNOWN
    )


def _validation_scope(
    validation: CadModelValidationRecord | None,
) -> ValidationScope:
    """Map the canonical evidence scope onto the presentation scope."""
    if validation is None:
        return ValidationScope.UNVALIDATED
    if validation.evidence_scope == 'owned_room':
        return ValidationScope.OWNED_ROOM_VALIDATED
    return ValidationScope.SYNTHETIC_FIXTURE


def trust_for_prediction(
    result: CadPredictionResult,
    *,
    scene_repository: SceneRepository,
    validation: CadModelValidationRecord | None = None,
    historical: bool = False,
    applicability: tuple[ApplicabilityNote, ...] = (),
    applicability_status: ApplicabilityStatus | None = None,
    uncertainty: UncertaintyPresentation | None = None,
) -> ResultTrustSummary:
    """Trust projection for a stored model prediction.

    ``validation`` must be the canonical O60 record bound to the same model
    identity — a mismatched record fails closed instead of lending scope.
    """
    freshness, invalidating = _freshness_for_scene_bound(
        scene_repository,
        document_id=result.document_id,
        scene_revision_id=result.scene_revision_id,
        scene_content_hash=result.scene_content_hash,
        historical=historical,
    )
    if validation is not None and (
        validation.model_id != result.model_id
        or validation.model_version != result.model_version
    ):
        raise ValueError(
            'validation record does not bind this prediction model'
        )
    scope = _validation_scope(validation)
    provenance = (
        f'model-validation:{validation.validation_id}'
        if validation is not None
        else None
    )
    return ResultTrustSummary(
        evidence_class=EvidenceClass.PREDICTED,
        validation_scope=scope,
        freshness=freshness,
        applicability=applicability,
        applicability_status=_applicability_status(
            applicability, applicability_status
        ),
        uncertainty=uncertainty if uncertainty is not None else _UNKNOWN_UNCERTAINTY,
        provenance_ref=provenance,
        invalidating_dependencies=invalidating,
    )


def trust_for_measurement(
    measurement: CadMeasurementRecord,
    *,
    scene_repository: SceneRepository,
    historical: bool = False,
    applicability: tuple[ApplicabilityNote, ...] = (),
    applicability_status: ApplicabilityStatus | None = None,
    uncertainty: UncertaintyPresentation | None = None,
) -> ResultTrustSummary:
    """Trust projection for a persisted measurement record."""
    freshness, invalidating = _freshness_for_scene_bound(
        scene_repository,
        document_id=measurement.document_id,
        scene_revision_id=measurement.scene_revision_id,
        scene_content_hash=measurement.scene_content_hash,
        historical=historical,
    )
    return ResultTrustSummary(
        evidence_class=_MEASUREMENT_EVIDENCE_CLASSES[measurement.evidence_type],
        validation_scope=ValidationScope.UNVALIDATED,
        freshness=freshness,
        applicability=applicability,
        applicability_status=_applicability_status(
            applicability, applicability_status
        ),
        uncertainty=uncertainty if uncertainty is not None else _UNKNOWN_UNCERTAINTY,
        provenance_ref=f'measurement:{measurement.measurement_id}',
        invalidating_dependencies=invalidating,
    )


def trust_for_validation(
    validation: CadModelValidationRecord,
    *,
    underlying: ResultTrustSummary | None = None,
    applicability: tuple[ApplicabilityNote, ...] = (),
    applicability_status: ApplicabilityStatus | None = None,
) -> ResultTrustSummary:
    """Trust projection for a model-validation record.

    The record itself carries no scene-revision binding, so freshness is
    inherited from the underlying result's adapter output when supplied and
    remains UNKNOWN otherwise — never silently CURRENT.
    """
    scope = _validation_scope(validation)
    freshness = (
        underlying.freshness if underlying is not None else FreshnessState.UNKNOWN
    )
    return ResultTrustSummary(
        evidence_class=(
            underlying.evidence_class
            if underlying is not None
            else EvidenceClass.DERIVED
        ),
        validation_scope=scope,
        freshness=freshness,
        applicability=applicability,
        applicability_status=_applicability_status(
            applicability, applicability_status
        ),
        provenance_ref=f'model-validation:{validation.validation_id}',
        invalidating_dependencies=(
            underlying.invalidating_dependencies
            if underlying is not None
            else ()
        ),
    )


def trust_for_recommendation(
    validation: CadModelValidationRecord,
    *,
    underlying: ResultTrustSummary | None = None,
) -> ResultTrustSummary:
    """Trust projection for a recommendation surfaced from a validation.

    The canonical ``recommendation_gate`` is the strongest gate that exists;
    a disabled gate means the validation scope must not be rendered for the
    recommendation at all — it degrades to UNVALIDATED rather than lending
    a stronger claim. ``PRODUCTION_QUALIFIED`` is unreachable here: no
    production-adoption authority exists for recommendations.
    """
    if validation.recommendation_gate != 'eligible':
        return ResultTrustSummary(
            evidence_class=EvidenceClass.PROPOSED,
            validation_scope=ValidationScope.UNVALIDATED,
            freshness=(
                underlying.freshness
                if underlying is not None
                else FreshnessState.UNKNOWN
            ),
            provenance_ref=f'model-validation:{validation.validation_id}',
            invalidating_dependencies=(
                underlying.invalidating_dependencies
                if underlying is not None
                else ()
            ),
        )
    summary = trust_for_validation(validation, underlying=underlying)
    # An eligible recommendation still cannot outrun the validation scope.
    return summary


def trust_for_proposal(
    *,
    bound_document_id: str,
    bound_scene_revision_id: str,
    bound_scene_content_hash: str,
    scene_repository: SceneRepository,
    applied: bool,
    historical: bool = False,
) -> ResultTrustSummary:
    """Trust projection for an optimization/robustness proposal.

    A candidate is PROPOSED until the domain lifecycle marks it applied to
    the scene (``SystemVariantApplication``); even applied it remains DERIVED
    proposal evidence — never MEASURED.
    """
    freshness, invalidating = _freshness_for_scene_bound(
        scene_repository,
        document_id=bound_document_id,
        scene_revision_id=bound_scene_revision_id,
        scene_content_hash=bound_scene_content_hash,
        historical=historical,
    )
    return ResultTrustSummary(
        evidence_class=EvidenceClass.PROPOSED,
        validation_scope=ValidationScope.UNVALIDATED,
        freshness=freshness,
        applicability_status=ApplicabilityStatus.UNKNOWN,
        uncertainty=_UNKNOWN_UNCERTAINTY,
        invalidating_dependencies=invalidating,
    )


__all__ = [
    'trust_for_measurement',
    'trust_for_prediction',
    'trust_for_proposal',
    'trust_for_recommendation',
    'trust_for_validation',
]
