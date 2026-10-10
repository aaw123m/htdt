
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import AcousticPredictionRequest, AcousticSceneSnapshot
from .cad_acoustic_treatment import AcousticTreatmentPlacement
from ...cad_scene_revisions import SceneRevision
from ...cad_system_variant import SystemVariant
from ...r120_geometry_compiler import ExactExternalAuthorityRef
from ...canonical_json import canonical_sha256 as _digest

TREATMENT_COMPARISON_SCHEMA_VERSION = 1
TREATMENT_COMPARISON_AUTHORITY_VERSION = 'acoustic-treatment-comparison-1'
TREATMENT_COMPARISON_CANDIDATE_VERSION = 'acoustic-treatment-comparison-candidate-1'
TREATMENT_COMPARISON_OUTCOME_VERSION = 'acoustic-treatment-comparison-outcome-1'

TreatmentComparisonRole = Literal['no_treatment', 'treatment']
TreatmentCandidateAvailability = Literal['evaluated', 'unavailable']
TreatmentOutcomeCompatibility = Literal[
    'compatible', 'partial', 'incompatible'
]

class TreatmentPlacementComparisonRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    instance_id: str = Field(min_length=1)
    placement_version: int = Field(ge=1)
    placement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    lifecycle: Literal['proposed', 'installed']

class TreatmentSnapshotComparisonRef(BaseModel):
    """Exact snapshot identity plus treatment-placement lineage exposed by overlays."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    snapshot_id: str = Field(pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$')
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_schema_version: int = Field(ge=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    treatment_placement_sha256s: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_ref(self) -> 'TreatmentSnapshotComparisonRef':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('snapshot SystemVariant id/hash must be supplied together')
        if self.treatment_placement_sha256s != tuple(
            sorted(set(self.treatment_placement_sha256s))
        ):
            raise ValueError(
                'snapshot treatment placement hashes must be unique and sorted'
            )
        return self

class TreatmentPredictionRequestComparisonRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    request_id: str = Field(pattern=r'^acoustic-prediction-request:[0-9a-f]{64}$')
    request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_id: str = Field(pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$')
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

class TreatmentDesignComparisonCandidate(BaseModel):
    """One named treatment design. No objective value is recomputed here."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'acoustic-treatment-comparison-candidate-1'
    ] = TREATMENT_COMPARISON_CANDIDATE_VERSION
    candidate_id: str = Field(
        pattern=r'^treatment-comparison-candidate:[0-9a-f]{64}$'
    )
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    label: str = Field(min_length=1)
    role: TreatmentComparisonRole

    document_id: str = Field(min_length=1)
    baseline_scene_revision_id: str = Field(min_length=1)
    baseline_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    placements: tuple[TreatmentPlacementComparisonRef, ...] = ()
    acoustic_scene_snapshot: TreatmentSnapshotComparisonRef | None = None
    prediction_requests: tuple[TreatmentPredictionRequestComparisonRef, ...] = ()

    @model_validator(mode='after')
    def validate_candidate(self) -> 'TreatmentDesignComparisonCandidate':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('candidate SystemVariant id/hash must be supplied together')

        placement_keys = [
            (item.instance_id, item.placement_version)
            for item in self.placements
        ]
        if len(placement_keys) != len(set(placement_keys)):
            raise ValueError('candidate treatment placement refs must be unique')

        if self.role == 'no_treatment' and self.placements:
            raise ValueError('no-treatment candidate cannot contain treatment placements')
        if self.role == 'treatment' and not self.placements:
            raise ValueError('treatment candidate requires at least one placement')

        snapshot = self.acoustic_scene_snapshot
        if snapshot is None and self.prediction_requests:
            raise ValueError(
                'prediction request refs require an exact AcousticSceneSnapshot ref'
            )
        if snapshot is not None:
            if (
                snapshot.scene_revision_id != self.baseline_scene_revision_id
                or snapshot.scene_content_hash != self.baseline_scene_content_hash
            ):
                raise ValueError('candidate snapshot baseline SceneRevision mismatch')
            if (
                snapshot.system_variant_id != self.system_variant_id
                or snapshot.system_variant_sha256 != self.system_variant_sha256
            ):
                raise ValueError('candidate snapshot SystemVariant mismatch')
            expected_placement_hashes = tuple(
                sorted(item.placement_sha256 for item in self.placements)
            )
            if snapshot.treatment_placement_sha256s != expected_placement_hashes:
                raise ValueError(
                    'candidate snapshot treatment-placement lineage mismatch'
                )
            for request in self.prediction_requests:
                if (
                    request.snapshot_id != snapshot.snapshot_id
                    or request.snapshot_sha256 != snapshot.snapshot_sha256
                ):
                    raise ValueError(
                        'candidate prediction request snapshot identity mismatch'
                    )

        digest = _digest(self.semantic_payload())
        if self.candidate_sha256 != digest:
            raise ValueError('TreatmentDesignComparisonCandidate semantic hash mismatch')
        if self.candidate_id != f'treatment-comparison-candidate:{digest}':
            raise ValueError('TreatmentDesignComparisonCandidate id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'candidate_id', 'candidate_sha256'},
        )

class TreatmentDesignComparisonSpec(BaseModel):
    """Named exact A/B/no-treatment comparison without hidden scoring."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = TREATMENT_COMPARISON_SCHEMA_VERSION
    authority_version: Literal[
        'acoustic-treatment-comparison-1'
    ] = TREATMENT_COMPARISON_AUTHORITY_VERSION
    comparison_id: str = Field(
        pattern=r'^treatment-design-comparison:[0-9a-f]{64}$'
    )
    comparison_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    name: str = Field(min_length=1)

    document_id: str = Field(min_length=1)
    baseline_scene_revision_id: str = Field(min_length=1)
    baseline_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    candidates: tuple[TreatmentDesignComparisonCandidate, ...] = Field(min_length=2)

    @model_validator(mode='after')
    def validate_comparison(self) -> 'TreatmentDesignComparisonSpec':
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError('treatment comparison candidates must be unique')
        labels = [item.label for item in self.candidates]
        if len(labels) != len(set(labels)):
            raise ValueError('treatment comparison candidate labels must be unique')
        for item in self.candidates:
            if (
                item.document_id != self.document_id
                or item.baseline_scene_revision_id != self.baseline_scene_revision_id
                or item.baseline_scene_content_hash != self.baseline_scene_content_hash
            ):
                raise ValueError('treatment comparison candidate baseline mismatch')

        digest = _digest(self.semantic_payload())
        if self.comparison_sha256 != digest:
            raise ValueError('TreatmentDesignComparisonSpec semantic hash mismatch')
        if self.comparison_id != f'treatment-design-comparison:{digest}':
            raise ValueError('TreatmentDesignComparisonSpec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'comparison_id', 'comparison_sha256'},
        )

def treatment_placement_comparison_ref(
    placement: AcousticTreatmentPlacement,
) -> TreatmentPlacementComparisonRef:
    placement = AcousticTreatmentPlacement.model_validate(
        placement.model_dump(mode='python')
    )
    return TreatmentPlacementComparisonRef(
        instance_id=placement.instance_id,
        placement_version=placement.placement_version,
        placement_sha256=placement.placement_sha256,
        definition_id=placement.definition_id,
        definition_version=placement.definition_version,
        definition_sha256=placement.definition_sha256,
        lifecycle=placement.lifecycle,
    )

def treatment_snapshot_comparison_ref(
    snapshot: AcousticSceneSnapshot,
) -> TreatmentSnapshotComparisonRef:
    snapshot = AcousticSceneSnapshot.model_validate(
        snapshot.model_dump(mode='python')
    )
    placement_hashes = sorted(
        {
            overlay.treatment_placement_hash_sha256
            for binding in snapshot.treatment_boundary_bindings
            for overlay in binding.attached_treatment_overlays
        }
    )
    return TreatmentSnapshotComparisonRef(
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.semantic_sha256,
        snapshot_schema_version=snapshot.schema_version,
        scene_revision_id=snapshot.scene_revision_id,
        scene_content_hash=snapshot.scene_content_hash,
        system_variant_id=snapshot.system_variant_id,
        system_variant_sha256=snapshot.system_variant_sha256,
        treatment_placement_sha256s=tuple(placement_hashes),
    )

def treatment_prediction_request_comparison_ref(
    request: AcousticPredictionRequest,
) -> TreatmentPredictionRequestComparisonRef:
    request = AcousticPredictionRequest.model_validate(
        request.model_dump(mode='python')
    )
    return TreatmentPredictionRequestComparisonRef(
        request_id=request.request_id,
        request_semantic_sha256=request.request_semantic_sha256,
        deterministic_input_hash=request.deterministic_input_hash,
        snapshot_id=request.acoustic_scene_snapshot_id,
        snapshot_sha256=request.acoustic_scene_snapshot_sha256,
    )

def build_treatment_design_candidate(
    *,
    baseline: SceneRevision,
    label: str,
    role: TreatmentComparisonRole,
    system_variant: SystemVariant | None = None,
    placements: Sequence[AcousticTreatmentPlacement] = (),
    acoustic_scene_snapshot: AcousticSceneSnapshot | None = None,
    prediction_requests: Sequence[AcousticPredictionRequest] = (),
) -> TreatmentDesignComparisonCandidate:
    placement_tuple = tuple(placements)
    if system_variant is not None:
        if (
            system_variant.document_id != baseline.document_id
            or system_variant.baseline_revision_id != baseline.revision_id
            or system_variant.baseline_content_hash != baseline.content_hash
        ):
            raise ValueError('treatment comparison SystemVariant baseline mismatch')
    for placement in placement_tuple:
        if (
            placement.document_id != baseline.document_id
            or placement.scene_revision_id != baseline.revision_id
            or placement.scene_content_hash != baseline.content_hash
        ):
            raise ValueError('treatment comparison placement baseline mismatch')
        if system_variant is None:
            if placement.system_variant_id is not None:
                raise ValueError(
                    'baseline treatment candidate cannot use variant-bound placement'
                )
        elif (
            placement.system_variant_id != system_variant.variant_id
            or placement.system_variant_sha256 != system_variant.variant_sha256
        ):
            raise ValueError('treatment comparison placement SystemVariant mismatch')

    snapshot_ref = (
        None
        if acoustic_scene_snapshot is None
        else treatment_snapshot_comparison_ref(acoustic_scene_snapshot)
    )
    request_refs = tuple(
        treatment_prediction_request_comparison_ref(item)
        for item in prediction_requests
    )
    placement_refs = tuple(
        sorted(
            (treatment_placement_comparison_ref(item) for item in placement_tuple),
            key=lambda item: (item.instance_id, item.placement_version),
        )
    )
    core = {
        'authority_version': TREATMENT_COMPARISON_CANDIDATE_VERSION,
        'label': label,
        'role': role,
        'document_id': baseline.document_id,
        'baseline_scene_revision_id': baseline.revision_id,
        'baseline_scene_content_hash': baseline.content_hash,
        'system_variant_id': (
            None if system_variant is None else system_variant.variant_id
        ),
        'system_variant_sha256': (
            None if system_variant is None else system_variant.variant_sha256
        ),
        'placements': [item.model_dump(mode='json') for item in placement_refs],
        'acoustic_scene_snapshot': (
            None if snapshot_ref is None else snapshot_ref.model_dump(mode='json')
        ),
        'prediction_requests': [
            item.model_dump(mode='json') for item in request_refs
        ],
    }
    digest = _digest(core)
    return TreatmentDesignComparisonCandidate(
        candidate_id=f'treatment-comparison-candidate:{digest}',
        candidate_sha256=digest,
        label=label,
        role=role,
        document_id=baseline.document_id,
        baseline_scene_revision_id=baseline.revision_id,
        baseline_scene_content_hash=baseline.content_hash,
        system_variant_id=(
            None if system_variant is None else system_variant.variant_id
        ),
        system_variant_sha256=(
            None if system_variant is None else system_variant.variant_sha256
        ),
        placements=placement_refs,
        acoustic_scene_snapshot=snapshot_ref,
        prediction_requests=request_refs,
    )

def build_treatment_design_comparison(
    *,
    name: str,
    baseline: SceneRevision,
    candidates: Sequence[TreatmentDesignComparisonCandidate],
) -> TreatmentDesignComparisonSpec:
    candidate_tuple = tuple(candidates)
    core = {
        'schema_version': TREATMENT_COMPARISON_SCHEMA_VERSION,
        'authority_version': TREATMENT_COMPARISON_AUTHORITY_VERSION,
        'name': name,
        'document_id': baseline.document_id,
        'baseline_scene_revision_id': baseline.revision_id,
        'baseline_scene_content_hash': baseline.content_hash,
        'candidates': [item.model_dump(mode='json') for item in candidate_tuple],
    }
    digest = _digest(core)
    return TreatmentDesignComparisonSpec(
        comparison_id=f'treatment-design-comparison:{digest}',
        comparison_sha256=digest,
        name=name,
        document_id=baseline.document_id,
        baseline_scene_revision_id=baseline.revision_id,
        baseline_scene_content_hash=baseline.content_hash,
        candidates=candidate_tuple,
    )

class TreatmentCandidateOutcome(BaseModel):
    """Whether one named design bound evaluated result evidence (#985).

    Only exact evaluated authorities are admitted — a request that was
    never successfully evaluated can never display a benefit.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate_id: str = Field(
        pattern=r'^treatment-comparison-candidate:[0-9a-f]{64}$'
    )
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    availability: TreatmentCandidateAvailability
    unavailable_reason: str | None = None
    evaluated_result_refs: tuple[ExactExternalAuthorityRef, ...] = ()

    @model_validator(mode='after')
    def validate_outcome(self) -> 'TreatmentCandidateOutcome':
        if self.availability == 'evaluated':
            if not self.evaluated_result_refs:
                raise ValueError(
                    'evaluated candidate outcome requires exact result refs'
                )
            if self.unavailable_reason is not None:
                raise ValueError(
                    'evaluated candidate outcome cannot carry an '
                    'unavailable reason'
                )
        else:
            if self.evaluated_result_refs:
                raise ValueError(
                    'unavailable candidate outcome cannot carry '
                    'evaluated result refs'
                )
            if not self.unavailable_reason:
                raise ValueError(
                    'unavailable candidate outcome requires an explicit '
                    'reason'
                )
        ref_ids = [item.authority_id for item in self.evaluated_result_refs]
        if len(ref_ids) != len(set(ref_ids)):
            raise ValueError('candidate outcome result refs must be unique')
        return self

class TreatmentComparisonOutcome(BaseModel):
    """Evaluated-evidence binding for a persisted comparison spec (#985)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'acoustic-treatment-comparison-outcome-1'
    ] = TREATMENT_COMPARISON_OUTCOME_VERSION
    outcome_id: str = Field(
        pattern=r'^treatment-comparison-outcome:[0-9a-f]{64}$'
    )
    outcome_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    comparison_id: str = Field(
        pattern=r'^treatment-design-comparison:[0-9a-f]{64}$'
    )
    comparison_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    outcomes: tuple[TreatmentCandidateOutcome, ...] = ()
    compatibility: TreatmentOutcomeCompatibility
    compatibility_reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_outcome(self) -> 'TreatmentComparisonOutcome':
        ids = [item.candidate_id for item in self.outcomes]
        if len(ids) != len(set(ids)):
            raise ValueError('comparison outcome candidates must be unique')
        payload = self.model_dump(
            mode='json', exclude={'outcome_id', 'outcome_sha256'}
        )
        digest = _digest(payload)
        if self.outcome_sha256 != digest:
            raise ValueError('TreatmentComparisonOutcome semantic hash mismatch')
        if self.outcome_id != f'treatment-comparison-outcome:{digest}':
            raise ValueError('TreatmentComparisonOutcome id mismatch')
        return self

def build_treatment_comparison_outcome(
    *,
    spec: TreatmentDesignComparisonSpec,
    outcomes: Sequence[TreatmentCandidateOutcome],
    evaluated_at_utc: str,
) -> TreatmentComparisonOutcome:
    """Bind evaluated candidate outcomes against the exact spec (#985 §4).

    Compatibility verdict: all candidates evaluated → ``compatible``;
    some → ``partial``; none → ``incompatible`` — never a fabricated
    benefit for an unevaluated design.
    """
    spec_ids = {item.candidate_id: item for item in spec.candidates}
    outcome_tuple = tuple(outcomes)
    for outcome in outcome_tuple:
        candidate = spec_ids.get(outcome.candidate_id)
        if candidate is None:
            raise ValueError(
                'outcome candidate does not exist in the comparison spec'
            )
        if candidate.candidate_sha256 != outcome.candidate_sha256:
            raise ValueError(
                'outcome candidate semantic hash mismatch'
            )
    evaluated = sum(
        1 for item in outcome_tuple if item.availability == 'evaluated'
    )
    if evaluated == len(spec.candidates):
        compatibility: TreatmentOutcomeCompatibility = 'compatible'
    elif evaluated == 0:
        compatibility = 'incompatible'
    else:
        compatibility = 'partial'
    missing = [
        item.label
        for item in spec.candidates
        if item.candidate_id
        not in {outcome.candidate_id for outcome in outcome_tuple}
    ]
    reasons = tuple(
        [f'{evaluated}/{len(spec.candidates)} candidates evaluated']
        + [f'no outcome bound for candidate: {label}' for label in missing]
    )
    payload: dict[str, Any] = {
        'authority_version': TREATMENT_COMPARISON_OUTCOME_VERSION,
        'comparison_id': spec.comparison_id,
        'comparison_sha256': spec.comparison_sha256,
        'document_id': spec.document_id,
        'outcomes': [item.model_dump(mode='json') for item in outcome_tuple],
        'compatibility': compatibility,
        'compatibility_reasons': list(reasons),
        'evaluated_at_utc': evaluated_at_utc,
    }
    digest = _digest(payload)
    return TreatmentComparisonOutcome(
        outcome_id=f'treatment-comparison-outcome:{digest}',
        outcome_sha256=digest,
        comparison_id=spec.comparison_id,
        comparison_sha256=spec.comparison_sha256,
        document_id=spec.document_id,
        outcomes=outcome_tuple,
        compatibility=compatibility,
        compatibility_reasons=reasons,
        evaluated_at_utc=evaluated_at_utc,
    )

