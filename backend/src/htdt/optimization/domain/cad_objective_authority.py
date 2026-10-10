"""O30 objective-evaluation authority replay.

A persisted ``CadObjectiveEvaluation`` is authoritative only when every
declared binding replays exactly: the candidate must be a member of the
canonical candidate set regenerated from the persisted SearchSpec, every
input reference must resolve to exact persisted evidence bound to the same
document/SceneRevision (and, where applicable, the same candidate and
candidate-set authority), and the stored vector must equal the vector the
versioned ``evaluation_spec`` recomputes from that resolved evidence.

Resolvers and vector evaluators are explicit registries. Source kinds and
objective methods without a registered authority fail closed; fixture or
evaluator-owned inputs stay explicit because tests and providers inject
their own resolvers/evaluators into ``CadObjectiveRepository``.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, NamedTuple

from ...measurement.domain.cad_measurement_quality import dataset_sha256
from .cad_objective_models import (
    CadObjectiveEvaluation,
    CadObjectiveInputRef,
    canonical_objective_sha256,
)
from ...cad_repository import SceneRepository, SceneRevision
from ...cad_roomsim_results import roomsim_attempt_frequency_response
from ...cad_scene import scene_content_hash
from ...cad_search import candidate_preview_document
from ...cad_search_models import CadCandidate, CadSearchSpec
from ...comparison import FrequencyResponse
from .optimization_objectives import (
    ObjectiveVector,
    ResponseObjectiveSpec,
    movement_objectives,
    target_response_objectives,
)


class ResolvedObjectiveInput(NamedTuple):
    """One input reference resolved against exact persisted authority."""

    ref: CadObjectiveInputRef
    # Semantic hash of the exact evidence consumed, or None for declared-only
    # refs that carry no upstream record.
    source_sha256: str | None
    # Typed frequency response when the evidence supplies one for canonical
    # objective replay.
    response: FrequencyResponse | None = None
    # The resolved upstream record for evaluators that need more than the
    # response (for example the provider object for r170a objectives).
    authority: Any = None


class ObjectiveAuthorityContext(NamedTuple):
    """Everything an input resolver or vector evaluator may rely on."""

    evaluation: CadObjectiveEvaluation
    scene_repository: SceneRepository
    source_revision: SceneRevision
    search_spec: CadSearchSpec
    candidate: CadCandidate
    candidate_set_sha256: str
    measurement_repository: Any
    roomsim_repository: Any
    prediction_provider_repository: Any
    inputs: tuple[ResolvedObjectiveInput, ...] = ()
    spec: Mapping[str, Any] | None = None
    hybrid_provider_repository: Any = None
    # Optional per-operation memo for validated Room Simulator batch specs
    # (see CadRoomSimRepository): evaluations in one audit or save batch
    # replay the same batch authority repeatedly; sharing it across those
    # replays keeps one batch validation per batch_run_id instead of two
    # per input reference. None preserves per-read re-validation.
    roomsim_batches: Any = None


# Evidence classes that may stay declared-only when their source kind has no
# registered resolver: derived geometry-style inputs are recomputed from the
# candidate itself, and hypothesis inputs are explicit assumptions. Measured
# and predicted inputs must always resolve to exact persisted evidence.
DECLARED_ONLY_EVIDENCE_CLASSES = frozenset({'derived', 'hypothesis'})


def _resolve_candidate_geometry(
    context: ObjectiveAuthorityContext,
    ref: CadObjectiveInputRef,
) -> ResolvedObjectiveInput:
    if ref.evidence_class != 'derived':
        raise ValueError('candidate geometry evidence class must be derived')
    if ref.source_id != context.candidate.candidate_id:
        raise ValueError(
            'candidate geometry input must name the evaluated candidate'
        )
    source_sha256 = canonical_objective_sha256({
        'candidate': context.candidate.model_dump(mode='json'),
        'candidate_set_sha256': context.candidate_set_sha256,
        'search_spec_sha256': context.search_spec.search_spec_sha256,
    })
    return ResolvedObjectiveInput(
        ref=ref,
        source_sha256=source_sha256,
        authority=context.candidate,
    )


def _resolve_cad_measurement(
    context: ObjectiveAuthorityContext,
    ref: CadObjectiveInputRef,
) -> ResolvedObjectiveInput:
    if ref.evidence_class != 'measured':
        raise ValueError('cad_measurement evidence class must be measured')
    repository = context.measurement_repository
    if repository is None:
        raise ValueError('objective measured evidence authority unavailable')
    record = repository.get_measurement(ref.source_id)
    if record is None:
        raise ValueError('objective measured evidence does not exist')
    if record.document_id != context.evaluation.document_id:
        raise ValueError('objective measured evidence belongs to another document')
    applied = context.scene_repository.get(record.scene_revision_id)
    if applied is None:
        raise ValueError(
            'objective measured evidence is not bound to a persisted SceneRevision'
        )
    if (
        applied.document_id != context.evaluation.document_id
        or applied.content_hash != record.scene_content_hash
    ):
        raise ValueError('objective measured evidence revision binding mismatch')
    expected_hash = scene_content_hash(
        candidate_preview_document(
            context.source_revision.document,
            context.candidate,
        )
    )
    if applied.content_hash != expected_hash:
        raise ValueError(
            'objective measured evidence is not bound to the evaluated '
            'candidate geometry'
        )
    if record.evidence_type != 'measured':
        raise ValueError('objective measured evidence type mismatch')
    dataset = repository.dataset_for_measurement(record.measurement_id)
    if dataset is None:
        raise ValueError('objective measured evidence has no persisted dataset')
    if dataset.measurement_id != record.measurement_id:
        raise ValueError('objective measured dataset binding mismatch')
    return ResolvedObjectiveInput(
        ref=ref,
        source_sha256=dataset_sha256(dataset),
        response=FrequencyResponse(
            frequency_hz=tuple(float(value) for value in dataset.frequency_hz),
            level_db=tuple(float(value) for value in dataset.level_db),
        ),
        authority=record,
    )


def _resolve_cad_roomsim_attempt(
    context: ObjectiveAuthorityContext,
    ref: CadObjectiveInputRef,
) -> ResolvedObjectiveInput:
    if ref.evidence_class != 'predicted':
        raise ValueError('cad_roomsim_attempt evidence class must be predicted')
    repository = context.roomsim_repository
    if repository is None:
        raise ValueError('objective predicted evidence authority unavailable')
    # Deferred import: the repository module sits below this authority layer
    # and callers may substitute duck-typed doubles without the memo kwarg.
    from ...cad_roomsim_repository import CadRoomSimRepository

    shared = context.roomsim_batches if isinstance(
        repository, CadRoomSimRepository
    ) else None
    if shared is not None:
        attempt = repository.get_attempt(ref.source_id, batches=shared)
    else:
        attempt = repository.get_attempt(ref.source_id)
    if attempt is None:
        raise ValueError('objective predicted evidence attempt does not exist')
    if attempt.candidate_id != context.candidate.candidate_id:
        raise ValueError(
            'objective predicted evidence belongs to another candidate'
        )
    if attempt.status != 'completed':
        raise ValueError('objective predicted evidence attempt is not completed')
    if shared is not None:
        batch = repository.get_batch_spec(attempt.batch_run_id, batches=shared)
    else:
        batch = repository.get_batch_spec(attempt.batch_run_id)
    if batch is None:
        raise ValueError('objective predicted evidence batch spec does not exist')
    if (
        batch.document_id != context.evaluation.document_id
        or batch.scene_revision_id != context.evaluation.scene_revision_id
        or batch.scene_content_hash != context.evaluation.scene_content_hash
        or batch.search_spec_id != context.evaluation.search_spec_id
        or batch.search_spec_sha256 != context.evaluation.search_spec_sha256
    ):
        raise ValueError('objective predicted evidence batch binding mismatch')
    if batch.candidate_set_sha256 != context.candidate_set_sha256:
        raise ValueError(
            'objective predicted evidence candidate-set authority mismatch'
        )
    if attempt.result is None:
        raise ValueError(
            'objective predicted evidence attempt has no execution result'
        )
    response = roomsim_attempt_frequency_response(attempt)
    return ResolvedObjectiveInput(
        ref=ref,
        source_sha256=attempt.result.response_sha256,
        response=response,
        authority=attempt,
    )


def _resolve_prediction_provider(
    context: ObjectiveAuthorityContext,
    ref: CadObjectiveInputRef,
) -> ResolvedObjectiveInput:
    if ref.evidence_class != 'predicted':
        raise ValueError('r170a_prediction_provider evidence class must be predicted')
    repository = context.prediction_provider_repository
    if repository is None:
        raise ValueError('prediction provider authority unavailable')
    provider = repository.get_provider(ref.source_id)
    if provider is None:
        raise ValueError('objective prediction provider does not exist')
    authority = provider.current_authority
    if (
        authority.document_id != context.evaluation.document_id
        or authority.scene_revision_id != context.evaluation.scene_revision_id
        or authority.scene_content_hash != context.evaluation.scene_content_hash
    ):
        raise ValueError(
            'objective prediction provider belongs to another SceneRevision'
        )
    return ResolvedObjectiveInput(
        ref=ref,
        source_sha256=provider.semantic_sha256,
        authority=provider,
    )


def _spec_band(payload: Any, label: str) -> tuple[float, float]:
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError(f'{label} must be a [low_hz, high_hz] pair')
    low_hz = float(payload[0])
    high_hz = float(payload[1])
    if not (low_hz > 0.0 and high_hz > low_hz):
        raise ValueError(f'{label} requires 0 < low_hz < high_hz')
    return low_hz, high_hz


def _resolve_hybrid_prediction_provider(
    context: ObjectiveAuthorityContext,
    ref: CadObjectiveInputRef,
) -> ResolvedObjectiveInput:
    if ref.evidence_class != 'predicted':
        raise ValueError(
            'r170b_hybrid_prediction_provider evidence class must be predicted'
        )
    repository = context.hybrid_provider_repository
    if repository is None:
        raise ValueError('hybrid prediction provider authority unavailable')
    spec = context.spec
    if spec is None:
        raise ValueError('hybrid provider objective spec unavailable')
    provider_id = spec.get('provider_id')
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError('hybrid provider objective spec provider_id missing')
    provider = repository.get(provider_id)
    if provider is None:
        raise ValueError('hybrid prediction provider does not exist')
    if provider.semantic_sha256 != spec.get('provider_sha256'):
        raise ValueError(
            'hybrid provider objective spec provider semantic hash mismatch'
        )
    authority = provider.base_current_authority
    if (
        authority.document_id != context.evaluation.document_id
        or authority.scene_revision_id != context.evaluation.scene_revision_id
        or authority.scene_content_hash != context.evaluation.scene_content_hash
    ):
        raise ValueError(
            'hybrid prediction provider belongs to another SceneRevision'
        )
    low_hz, high_hz = _spec_band(
        spec.get('requested_band_hz'),
        'hybrid provider objective spec requested_band_hz',
    )
    source_entity_id = spec.get('source_entity_id')
    receiver_id = spec.get('receiver_id')
    if not isinstance(source_entity_id, str) or not source_entity_id:
        raise ValueError('hybrid provider objective spec source_entity_id missing')
    if not isinstance(receiver_id, str) or not receiver_id:
        raise ValueError('hybrid provider objective spec receiver_id missing')
    # Deferred import: the provider integration module imports the repository
    # module that owns this authority replay.
    from ...acoustics.services.cad_hybrid_prediction_provider_integration import (
        build_hybrid_provider_objective_input,
    )

    objective_input = build_hybrid_provider_objective_input(
        provider,
        source_entity_id=source_entity_id,
        receiver_id=receiver_id,
        low_hz=low_hz,
        high_hz=high_hz,
    )
    if objective_input.input_id != ref.source_id:
        raise ValueError('hybrid provider objective input identity mismatch')
    if objective_input.input_id != spec.get('objective_input_id'):
        raise ValueError('hybrid provider spec objective_input_id mismatch')
    if objective_input.semantic_sha256 != spec.get('objective_input_sha256'):
        raise ValueError('hybrid provider spec objective_input_sha256 mismatch')
    return ResolvedObjectiveInput(
        ref=ref,
        source_sha256=objective_input.semantic_sha256,
        authority=provider,
    )


OBJECTIVE_INPUT_RESOLVERS: dict[
    str,
    Callable[[ObjectiveAuthorityContext, CadObjectiveInputRef], ResolvedObjectiveInput],
] = {
    'candidate_geometry': _resolve_candidate_geometry,
    'cad_measurement': _resolve_cad_measurement,
    'cad_roomsim_attempt': _resolve_cad_roomsim_attempt,
    'r170a_prediction_provider': _resolve_prediction_provider,
    'r170b_hybrid_prediction_provider': _resolve_hybrid_prediction_provider,
}


def objective_spec_authority(spec: Mapping[str, Any]) -> str:
    """Return the versioned authority key an evaluation spec replays under."""

    method = spec.get('objective_method')
    if isinstance(method, str) and method:
        return method
    authority = spec.get('authority')
    if isinstance(authority, str) and authority:
        return authority
    raise ValueError(
        'objective evaluation spec declares no replayable objective authority'
    )


def _selected_vector(vector: ObjectiveVector, spec: Mapping[str, Any]) -> ObjectiveVector:
    objective_ids = spec.get('objectives')
    if objective_ids is None:
        return vector
    if (
        not isinstance(objective_ids, list)
        or not objective_ids
        or any(not isinstance(item, str) or not item for item in objective_ids)
    ):
        raise ValueError(
            'objective spec objectives must be a non-empty list of objective ids'
        )
    try:
        metrics = tuple(vector.metric(objective_id) for objective_id in objective_ids)
    except KeyError as exc:
        raise ValueError(
            'objective spec selects a metric the canonical vector does not '
            f'produce: {exc.args[0]}'
        ) from exc
    return ObjectiveVector(candidate_id=vector.candidate_id, metrics=metrics)


def _single_response_input(context: ObjectiveAuthorityContext) -> FrequencyResponse:
    responses = [item for item in context.inputs if item.response is not None]
    if len(responses) != 1:
        raise ValueError(
            'objective evaluation requires exactly one response-bearing input'
        )
    response = responses[0].response
    assert response is not None
    return response


def _spec_frequency_response(payload: Any) -> FrequencyResponse:
    if not isinstance(payload, Mapping):
        raise ValueError('objective spec response must be a JSON object')
    frequencies = payload.get('frequency_hz')
    levels = payload.get('level_db')
    if (
        not isinstance(frequencies, list)
        or not isinstance(levels, list)
        or len(frequencies) != len(levels)
    ):
        raise ValueError('objective spec response requires frequency_hz/level_db lists')
    return FrequencyResponse(
        frequency_hz=tuple(float(value) for value in frequencies),
        level_db=tuple(float(value) for value in levels),
    )


def _spec_response_objective_spec(spec: Mapping[str, Any]) -> ResponseObjectiveSpec:
    band = spec.get('response_band_hz')
    if not (isinstance(band, list) and len(band) == 2):
        raise ValueError('objective spec requires a response_band_hz pair')
    reference_band = spec.get('reference_band_hz')
    if reference_band is not None and not (
        isinstance(reference_band, list) and len(reference_band) == 2
    ):
        raise ValueError('objective spec reference_band_hz must be a pair')
    excluded_bands = spec.get('excluded_bands', ())
    if not isinstance(excluded_bands, list):
        raise ValueError('objective spec excluded_bands must be a list')
    return ResponseObjectiveSpec(
        low_hz=float(band[0]),
        high_hz=float(band[1]),
        reference_band_hz=(
            None
            if reference_band is None
            else (float(reference_band[0]), float(reference_band[1]))
        ),
        excluded_bands=tuple(
            (float(item[0]), float(item[1])) for item in excluded_bands
        ),
    )


def _spec_prefix(spec: Mapping[str, Any], default: str) -> str:
    prefix = spec.get('prefix', default)
    if not isinstance(prefix, str) or not prefix:
        raise ValueError('objective spec prefix must be a non-empty string')
    return prefix


def _evaluate_target_response(context: ObjectiveAuthorityContext) -> ObjectiveVector:
    assert context.spec is not None
    objective_spec = _spec_response_objective_spec(context.spec)
    target = _spec_frequency_response(context.spec.get('target_response'))
    vector = target_response_objectives(
        context.evaluation.candidate_id,
        _single_response_input(context),
        target,
        objective_spec,
        prefix=_spec_prefix(context.spec, 'response'),
    )
    return _selected_vector(vector, context.spec)


def _evaluate_candidate_movement(context: ObjectiveAuthorityContext) -> ObjectiveVector:
    assert context.spec is not None
    baseline = {}
    for entity_id in context.candidate.positions:
        try:
            entity = context.source_revision.document.entity(entity_id)
        except KeyError as exc:
            raise ValueError(
                'objective candidate moves an entity missing from the source '
                f'revision: {entity_id}'
            ) from exc
        baseline[entity_id] = {
            'x_m': float(entity.position.x_m),
            'y_m': float(entity.position.y_m),
            'z_m': float(entity.position.z_m),
        }
    vector = movement_objectives(
        context.evaluation.candidate_id,
        baseline,
        context.candidate.positions,
        prefix=_spec_prefix(context.spec, 'movement'),
    )
    return _selected_vector(vector, context.spec)


def _evaluate_provider_objective(context: ObjectiveAuthorityContext) -> ObjectiveVector:
    # Deferred import: the provider integration module imports the repository
    # module that owns this authority replay.
    from ...cad_prediction_provider_integration import provider_frequency_response

    assert context.spec is not None
    provider_inputs = [
        item
        for item in context.inputs
        if item.ref.source_kind == 'r170a_prediction_provider'
    ]
    if len(provider_inputs) != 1:
        raise ValueError(
            'provider objective evaluation requires exactly one provider input'
        )
    provider = provider_inputs[0].authority
    if provider is None:
        raise ValueError('provider objective input did not resolve a provider')
    if provider.provider_id != context.spec.get('provider_id'):
        raise ValueError('provider objective spec provider identity mismatch')
    if provider.semantic_sha256 != context.spec.get('provider_sha256'):
        raise ValueError('provider objective spec provider semantic hash mismatch')
    if context.spec.get('observable') != 'frequency_response_magnitude':
        raise ValueError('provider objective spec observable mismatch')
    domain = provider.valid_frequency_domain
    valid_band = context.spec.get('valid_band_hz')
    if not (isinstance(valid_band, list) and len(valid_band) == 2) or [
        float(valid_band[0]),
        float(valid_band[1]),
    ] != [float(domain.minimum_hz), float(domain.maximum_hz)]:
        raise ValueError('provider objective spec valid band mismatch')
    receiver_id = context.spec.get('receiver_id')
    if not isinstance(receiver_id, str) or not receiver_id:
        raise ValueError('provider objective spec receiver_id mismatch')
    try:
        objective_spec = ResponseObjectiveSpec.model_validate(
            context.spec.get('objective_spec')
        )
    except ValueError as exc:
        raise ValueError(f'provider objective spec is invalid: {exc}') from exc
    target = _spec_frequency_response(context.spec.get('target'))
    response = provider_frequency_response(
        provider,
        receiver_id=receiver_id,
        low_hz=objective_spec.low_hz,
        high_hz=objective_spec.high_hz,
    )
    vector = target_response_objectives(
        context.evaluation.candidate_id,
        response,
        target,
        objective_spec,
        prefix='response',
    )
    return _selected_vector(vector, context.spec)


def _evaluate_hybrid_provider_objective(
    context: ObjectiveAuthorityContext,
) -> ObjectiveVector:
    # Deferred import: the provider module owns the R170B response contract.
    from ...acoustics.services.cad_hybrid_prediction_provider import hybrid_provider_frequency_response

    assert context.spec is not None
    provider_inputs = [
        item
        for item in context.inputs
        if item.ref.source_kind == 'r170b_hybrid_prediction_provider'
    ]
    if len(provider_inputs) != 1:
        raise ValueError(
            'hybrid provider objective evaluation requires exactly one '
            'hybrid provider input'
        )
    provider = provider_inputs[0].authority
    if provider is None:
        raise ValueError(
            'hybrid provider objective input did not resolve a provider'
        )
    if provider.provider_id != context.spec.get('provider_id'):
        raise ValueError(
            'hybrid provider objective spec provider identity mismatch'
        )
    if provider.semantic_sha256 != context.spec.get('provider_sha256'):
        raise ValueError(
            'hybrid provider objective spec provider semantic hash mismatch'
        )
    if context.spec.get('observable') != 'frequency_response_magnitude':
        raise ValueError('hybrid provider objective spec observable mismatch')
    try:
        objective_spec = ResponseObjectiveSpec.model_validate(
            context.spec.get('objective_spec')
        )
    except ValueError as exc:
        raise ValueError(f'hybrid provider objective spec is invalid: {exc}') from exc
    low_hz, high_hz = _spec_band(
        context.spec.get('requested_band_hz'),
        'hybrid provider objective spec requested_band_hz',
    )
    if (low_hz, high_hz) != (objective_spec.low_hz, objective_spec.high_hz):
        raise ValueError(
            'hybrid provider objective spec band/objective mismatch'
        )
    source_entity_id = context.spec.get('source_entity_id')
    receiver_id = context.spec.get('receiver_id')
    if not isinstance(source_entity_id, str) or not source_entity_id:
        raise ValueError('hybrid provider objective spec source_entity_id missing')
    if not isinstance(receiver_id, str) or not receiver_id:
        raise ValueError('hybrid provider objective spec receiver_id missing')
    target = _spec_frequency_response(context.spec.get('target'))
    response = hybrid_provider_frequency_response(
        provider,
        source_entity_id=source_entity_id,
        receiver_id=receiver_id,
        low_hz=objective_spec.low_hz,
        high_hz=objective_spec.high_hz,
    )
    vector = target_response_objectives(
        context.evaluation.candidate_id,
        response,
        target,
        objective_spec,
        prefix='response',
    )
    return _selected_vector(vector, context.spec)


OBJECTIVE_VECTOR_EVALUATORS: dict[
    str,
    Callable[[ObjectiveAuthorityContext], ObjectiveVector],
] = {
    'target_response': _evaluate_target_response,
    'candidate_movement': _evaluate_candidate_movement,
    'r170a-provider-objective-1': _evaluate_provider_objective,
    'r170b-hybrid-provider-objective-1': _evaluate_hybrid_provider_objective,
}
