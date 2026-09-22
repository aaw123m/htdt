from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

from .cad_adaptive_repository import CadAdaptivePlanRepository
from .cad_adaptive_service import CadAdaptivePlannerService
from .cad_adaptive_extended import (
    build_adaptive_extended_observation,
    build_observation_source_ref,
)
from .cad_adaptive_extended_repository import CadAdaptiveExtendedRepository
from .cad_adaptive_extended_service import CadAdaptiveExtendedPlannerService
from .cad_constraint_models import CadConstraintSet
from .cad_extended_search import (
    CadExtendedSearchAxis,
    build_extended_model_capability,
    build_extended_search_spec,
    generate_extended_candidates,
)
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_measurement_loop import build_measurement_plan, complete_measurement_plan
from .cad_measurement_models import CadFrequencyResponseDataset
from .cad_measurement_quality import dataset_sha256
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json as canonical_measurement_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_model_validation_service import (
    CadModelValidationBuildSpec,
    CadModelValidationService,
    CadValidationApplicabilitySpec,
    CadValidationCandidateBinding,
    CadValidationObjectiveBinding,
    CadValidationRepeatabilitySpec,
    CadValidationSensitivitySpec,
    CadValidationSeparationSpec,
)
from .cad_objective_models import CadObjectiveInputRef
from .cad_objectives import build_objective_evaluation
from .cad_objective_repository import CadObjectiveRepository
from .cad_repository import SceneRepository
from .cad_roomsim import CadRoomSimBinding, CadRoomSimSourceBinding
from .cad_roomsim_batch_runner import build_cad_roomsim_batch_spec
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_roomsim_results import (
    CAD_ROOMSIM_ATTEMPT_SCHEMA_VERSION,
    CAD_ROOMSIM_EXECUTION_SCHEMA_VERSION,
    CadRoomSimCandidateAttempt,
    CadRoomSimExecutionResult,
    canonical_roomsim_result_json,
    canonical_roomsim_result_sha256,
    new_roomsim_attempt_id,
    roomsim_attempt_frequency_response,
    roomsim_result_timestamp_utc,
)
from .cad_scene import (
    Direction3,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from .cad_search import (
    build_cad_search_spec,
    candidate_preview_document,
    generate_cad_candidates,
)
from .cad_search_models import CadSearchAxis
from .cad_search_repository import CadSearchRepository
from .comparison import FrequencyResponse
from .optimization_objectives import (
    ObjectiveVector,
    ResponseObjectiveSpec,
    target_response_objectives,
)
from .rew_roomsim_batch import ROOMSIM_BATCH_ADAPTER_VERSION, ROOMSIM_MODEL_ID


SYNTHETIC_DEMO_DOCUMENT_ID = 'htdt-synthetic-o70-o80-demo-v1'
SYNTHETIC_MODEL_VERSION = 'synthetic-fixture-1'
SYNTHETIC_DIRECTIONAL_MODEL_ID = 'synthetic-directional-fixture'
SYNTHETIC_DIRECTIONAL_MODEL_VERSION = '1'


@dataclass(frozen=True)
class SyntheticDemoResult:
    document_id: str
    source_revision_id: str
    search_spec_id: str
    candidate_set_sha256: str
    validation_id: str
    adaptive_plan_id: str
    adaptive_selected_candidate_id: str
    extended_search_id: str
    extended_candidate_set_sha256: str
    adaptive_extended_plan_id: str
    adaptive_extended_selected_candidate_id: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=SYNTHETIC_DEMO_DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(
            room_id='synthetic-room',
            width_m=5.0,
            depth_m=4.0,
            height_m=2.4,
        ),
        entities=(
            SceneEntity(
                entity_id='synthetic-fl',
                kind='speaker',
                name='Synthetic FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                acoustic_reference_offset_m=Offset3(),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='synthetic-mlp',
                kind='measurement_point',
                name='Synthetic MLP',
                position=Position3(x_m=2.5, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _response(index: int, *, measured_offset: float = 0.0) -> dict:
    base = 78.0 + float(index)
    # A small per-index shape tilt gives every candidate a distinct
    # evidence-derived response.shape_rms_db value, so downstream objective
    # residuals and trend checks exercise real ordering instead of constants.
    tilt = 0.3 * float(index)
    return {
        'source_name': 'Left',
        'mic_position': 'Main',
        'message': 'HTDT synthetic fixture; not REW evidence',
        'unit': 'SPL',
        'smoothing': 'None',
        'start_frequency_hz': 20.0,
        'points_per_octave': 1.0,
        'frequency_step_hz': None,
        'frequency_hz': [20.0, 40.0, 80.0, 160.0],
        'magnitude': [
            base + measured_offset,
            base + 1.0 + tilt + measured_offset,
            base - 1.0 + measured_offset,
            base + 0.5 + 0.5 * tilt + measured_offset,
        ],
        'phase_deg': None,
    }


def _completed_attempt(
    *,
    batch_run_id: str,
    candidate_id: str,
    request_sha256: str,
    index: int,
) -> CadRoomSimCandidateAttempt:
    started = roomsim_result_timestamp_utc()
    completed = roomsim_result_timestamp_utc()
    response = _response(index)
    response_json = canonical_roomsim_result_json(response)
    response_sha = canonical_roomsim_result_sha256(response)
    pre_hash = sha256(b'htdt-synthetic-roomsim-pre-state').hexdigest()
    applied_hash = sha256(
        f'htdt-synthetic-applied:{candidate_id}'.encode('utf-8')
    ).hexdigest()
    result_identity = {
        'schema_version': CAD_ROOMSIM_EXECUTION_SCHEMA_VERSION,
        'request_sha256': request_sha256,
        'candidate_id': candidate_id,
        'model_id': ROOMSIM_MODEL_ID,
        'model_version': SYNTHETIC_MODEL_VERSION,
        'adapter_version': ROOMSIM_BATCH_ADAPTER_VERSION,
        'pre_state_sha256': pre_hash,
        'applied_state_sha256': applied_hash,
        'restored_state_sha256': pre_hash,
        'response': response,
        'response_sha256': response_sha,
    }
    result = CadRoomSimExecutionResult(
        request_sha256=request_sha256,
        candidate_id=candidate_id,
        model_id=ROOMSIM_MODEL_ID,
        model_version=SYNTHETIC_MODEL_VERSION,
        adapter_version=ROOMSIM_BATCH_ADAPTER_VERSION,
        pre_state_sha256=pre_hash,
        applied_state_sha256=applied_hash,
        restored_state_sha256=pre_hash,
        response_json=response_json,
        response_sha256=response_sha,
        result_sha256=canonical_roomsim_result_sha256(result_identity),
    )
    identity = {
        'schema_version': CAD_ROOMSIM_ATTEMPT_SCHEMA_VERSION,
        'batch_run_id': batch_run_id,
        'candidate_id': candidate_id,
        'attempt_index': 1,
        'status': 'completed',
        'result': result.model_dump(mode='json'),
        'result_sha256': result.result_sha256,
        'error_type': None,
        'error_message': None,
        'started_at_utc': started,
        'completed_at_utc': completed,
    }
    return CadRoomSimCandidateAttempt(
        attempt_id=new_roomsim_attempt_id(),
        batch_run_id=batch_run_id,
        candidate_id=candidate_id,
        attempt_index=1,
        status='completed',
        result=result,
        result_sha256=result.result_sha256,
        started_at_utc=started,
        completed_at_utc=completed,
        attempt_sha256=canonical_roomsim_result_sha256(identity),
    )


def _save_synthetic_measurement(
    measurement_repository: CadMeasurementRepository,
    revision,
    *,
    measurement_id: str,
    index: int,
    offset: float,
) -> str:
    response = _response(index, measured_offset=offset)
    processing = {
        'classification': 'htdt_synthetic_measurement_fixture',
        'evidence_scope': 'synthetic_fixture',
        'measurement_id': measurement_id,
        'synthetic_fixture': True,
        'physical_measurement': False,
    }
    # Synthetic fixture evidence persists through the declared importer: the
    # raw asset literally declares the samples, so the same verification that
    # binds REW imports binds this fixture.
    raw = declared_fr_raw(
        frequency_hz=response['frequency_hz'],
        level_db=response['magnitude'],
        phase_deg=None,
        phase_status='absent',
        level_reference='synthetic_fixture',
        smoothing='None',
        processing=processing,
    )
    digest = sha256(raw).hexdigest()
    record = measurement_record_for_revision(
        revision,
        'synthetic-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('synthetic-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        captured_at=_now(),
        imported_at=_now(),
        source_kind='unknown',
        quality_status='synthetic_fixture',
        quality_reasons=('not_physical_measurement',),
        quality_source='htdt_synthetic_demo',
        provenance={
            'validation_scope': 'synthetic_fixture',
            'synthetic_fixture': True,
            'physical_measurement': False,
            'generator': 'cad_synthetic_demo_v1',
        },
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=str(uuid4()),
        measurement_id=measurement_id,
        frequency_hz=tuple(float(value) for value in response['frequency_hz']),
        level_db=tuple(float(value) for value in response['magnitude']),
        phase_deg=None,
        phase_status='absent',
        level_reference='synthetic_fixture',
        smoothing='None',
        processing_json=canonical_measurement_json(processing),
        source_sha256=digest,
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    return measurement_id


def seed_synthetic_optimization_demo(
    scene_repository: SceneRepository,
) -> SyntheticDemoResult:
    """Create a fully persisted synthetic O10→O80 development fixture.

    The fixture intentionally writes normal immutable product records. Every
    measurement is explicitly marked synthetic and no owned-room Validation
    Campaign is created, so production recommendation gates stay closed.
    """

    if scene_repository.current_head(SYNTHETIC_DEMO_DOCUMENT_ID) is not None:
        raise ValueError(
            'synthetic optimization demo already exists in this data directory'
        )

    source = scene_repository.save(
        _scene(),
        parent_revision_id=None,
    ).revision
    search_repository = CadSearchRepository(scene_repository)
    search_spec, _estimate = build_cad_search_spec(
        source,
        CadConstraintSet(
            document_id=SYNTHETIC_DEMO_DOCUMENT_ID,
            constraints=(),
        ),
        (
            CadSearchAxis(
                entity_id='synthetic-fl',
                axis='x',
                min_m=1.2,
                max_m=2.2,
                step_m=0.2,
            ),
        ),
        candidate_limit=20,
        name='Synthetic O70/O80 development sweep',
    )
    search_repository.save(search_spec)
    page = generate_cad_candidates(
        scene_repository,
        search_spec,
        limit=20,
    )
    candidates = page.candidates
    if len(candidates) != 6:
        raise RuntimeError(
            f'synthetic fixture expected 6 candidates, got {len(candidates)}'
        )

    roomsim_repository = CadRoomSimRepository(
        scene_repository,
        search_repository,
    )
    binding = CadRoomSimBinding(
        receiver_entity_id='synthetic-mlp',
        sources=(
            CadRoomSimSourceBinding(
                entity_id='synthetic-fl',
                rew_source_name='Left',
            ),
        ),
        response_source_name='Left',
    )
    batch = build_cad_roomsim_batch_spec(
        source,
        search_spec,
        candidate_set_sha256=page.candidate_set_sha256,
        candidates=candidates,
        binding=binding,
    )
    roomsim_repository.save_batch_spec(batch)

    request_sha_by_candidate = {
        request.candidate_id: request.request_sha256 for request in batch.requests
    }
    attempt_by_candidate = {}
    for index, candidate in enumerate(candidates):
        attempt = _completed_attempt(
            batch_run_id=batch.batch_run_id,
            candidate_id=candidate.candidate_id,
            request_sha256=request_sha_by_candidate[candidate.candidate_id],
            index=index,
        )
        roomsim_repository.save_attempt(attempt)
        attempt_by_candidate[candidate.candidate_id] = attempt

    measurement_repository = CadMeasurementRepository(scene_repository)
    objective_repository = CadObjectiveRepository(
        scene_repository,
        search_repository,
        measurement_repository=measurement_repository,
        roomsim_repository=roomsim_repository,
    )
    # O30 persistence replays this spec against the referenced evidence, so the
    # synthetic fixture computes its vectors from the persisted attempts and
    # measurement datasets instead of inventing values.
    objective_spec = ResponseObjectiveSpec(
        low_hz=20.0,
        high_hz=160.0,
        reference_band_hz=(20.0, 160.0),
    )
    target_response = FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(80.0, 81.0, 79.0, 80.0),
    )
    evaluation_spec = {
        'algorithm_version': 'objective-vector-1',
        'objective_method': 'target_response',
        'objectives': ['response.shape_rms_db'],
        'response_band_hz': [20.0, 160.0],
        'reference_band_hz': [20.0, 160.0],
        'excluded_bands': [],
        'target_response': {
            'frequency_hz': list(target_response.frequency_hz),
            'level_db': list(target_response.level_db),
        },
        'synthetic_fixture': True,
    }

    def _shape_vector(candidate_id: str, response: FrequencyResponse) -> ObjectiveVector:
        full = target_response_objectives(
            candidate_id,
            response,
            target_response,
            objective_spec,
        )
        return ObjectiveVector(
            candidate_id=candidate_id,
            metrics=(full.metric('response.shape_rms_db'),),
        )

    predicted_evaluation_by_candidate = {}
    for index, candidate in enumerate(candidates, start=1):
        attempt = attempt_by_candidate[candidate.candidate_id]
        evaluation = build_objective_evaluation(
            source,
            search_spec,
            candidate.candidate_id,
            _shape_vector(
                candidate.candidate_id,
                roomsim_attempt_frequency_response(attempt),
            ),
            evaluation_spec=evaluation_spec,
            input_refs=(
                CadObjectiveInputRef(
                    evidence_class='predicted',
                    source_kind='cad_roomsim_attempt',
                    source_id=attempt.attempt_id,
                    source_sha256=attempt.result.response_sha256,
                ),
            ),
        )
        objective_repository.save_evaluation(evaluation)
        predicted_evaluation_by_candidate[candidate.candidate_id] = evaluation
    measured_candidate_ids = tuple(
        candidate.candidate_id for candidate in candidates[:4]
    )
    split_by_candidate = {
        measured_candidate_ids[0]: 'calibration',
        measured_candidate_ids[1]: 'calibration',
        measured_candidate_ids[2]: 'holdout',
        measured_candidate_ids[3]: 'holdout',
    }
    primary_measurement_by_candidate = {}
    measurement_ids_by_candidate = {}
    measured_evaluation_by_candidate = {}

    for index, candidate in enumerate(candidates[:4]):
        applied_document = candidate_preview_document(
            source.document,
            candidate,
        )
        # O50 measurement plans require each applied candidate revision to
        # descend directly from the SearchSpec source revision, so this
        # development fixture deliberately writes non-head sibling lineage.
        # The detached save never moves the document head (#626).
        applied_revision = scene_repository.save_detached_revision(
            applied_document,
            parent_revision_id=source.revision_id,
            reason='o50_measurement_plan_fixture',
        ).revision
        plan = build_measurement_plan(
            scene_repository,
            search_repository,
            search_spec_id=search_spec.search_spec_id,
            candidate_id=candidate.candidate_id,
            applied_scene_revision_id=applied_revision.revision_id,
        )
        measurement_repository.save_measurement_plan(plan)

        primary_id = f'synthetic-measurement-{candidate.feasible_index}-primary'
        ids = [
            _save_synthetic_measurement(
                measurement_repository,
                applied_revision,
                measurement_id=primary_id,
                index=index,
                offset=0.2,
            )
        ]
        if candidate.candidate_id == measured_candidate_ids[2]:
            repeat_id = (
                f'synthetic-measurement-{candidate.feasible_index}-repeat'
            )
            ids.append(
                _save_synthetic_measurement(
                    measurement_repository,
                    applied_revision,
                    measurement_id=repeat_id,
                    index=index,
                    offset=0.25,
                )
            )
        completed_plan = complete_measurement_plan(
            plan,
            measurement_repository,
            tuple(ids),
        )
        measurement_repository.save_measurement_plan(completed_plan)
        primary_measurement_by_candidate[candidate.candidate_id] = ids[0]
        measurement_ids_by_candidate[candidate.candidate_id] = tuple(ids)

        primary_dataset = measurement_repository.dataset_for_measurement(ids[0])
        if primary_dataset is None:
            raise RuntimeError('synthetic fixture lost its measurement dataset')
        measured_evaluation = build_objective_evaluation(
            source,
            search_spec,
            candidate.candidate_id,
            _shape_vector(
                candidate.candidate_id,
                FrequencyResponse(
                    frequency_hz=primary_dataset.frequency_hz,
                    level_db=primary_dataset.level_db,
                ),
            ),
            evaluation_spec=evaluation_spec,
            input_refs=(
                CadObjectiveInputRef(
                    evidence_class='measured',
                    source_kind='cad_measurement',
                    source_id=ids[0],
                    source_sha256=dataset_sha256(primary_dataset),
                ),
            ),
        )
        objective_repository.save_evaluation(measured_evaluation)
        measured_evaluation_by_candidate[
            candidate.candidate_id
        ] = measured_evaluation

    validation_service = CadModelValidationService(
        search_repository,
        roomsim_repository,
        measurement_repository,
        objective_repository,
    )
    holdout_a = measured_candidate_ids[2]
    holdout_b = measured_candidate_ids[3]
    repeatability_ids = measurement_ids_by_candidate[holdout_a]
    validation = validation_service.build(
        CadModelValidationBuildSpec(
            search_spec_id=search_spec.search_spec_id,
            candidate_set_sha256=page.candidate_set_sha256,
            model_id=ROOMSIM_MODEL_ID,
            model_version=SYNTHETIC_MODEL_VERSION,
            evidence_scope='synthetic_fixture',
            low_hz=20.0,
            high_hz=160.0,
            max_holdout_rms_db=1.0,
            candidates=tuple(
                CadValidationCandidateBinding(
                    candidate_id=candidate_id,
                    split=split_by_candidate[candidate_id],
                    prediction_attempt_id=attempt_by_candidate[
                        candidate_id
                    ].attempt_id,
                    measurement_id=primary_measurement_by_candidate[
                        candidate_id
                    ],
                    objectives=(
                        CadValidationObjectiveBinding(
                            objective_id='response.shape_rms_db',
                            predicted_evaluation_id=(
                                predicted_evaluation_by_candidate[
                                    candidate_id
                                ].evaluation_id
                            ),
                            measured_evaluation_id=(
                                measured_evaluation_by_candidate[
                                    candidate_id
                                ].evaluation_id
                            ),
                        ),
                    ),
                )
                for candidate_id in measured_candidate_ids
            ),
            sensitivity=(
                CadValidationSensitivitySpec(
                    objective_id='response.shape_rms_db',
                    candidate_a_id=holdout_a,
                    candidate_b_id=holdout_b,
                    max_observed_sensitivity_per_m=6.0,
                    max_model_error_per_m=1.0,
                ),
            ),
            repeatability=(
                CadValidationRepeatabilitySpec(
                    measurement_ids=repeatability_ids,
                ),
            ),
            separation=(
                CadValidationSeparationSpec(
                    candidate_a_id=holdout_a,
                    candidate_b_id=holdout_b,
                    measurement_a_id=primary_measurement_by_candidate[
                        holdout_a
                    ],
                    measurement_b_id=primary_measurement_by_candidate[
                        holdout_b
                    ],
                    repeatability_measurement_ids=repeatability_ids,
                    min_repeatability_multiple=2.0,
                ),
            ),
            applicability=(
                CadValidationApplicabilitySpec(
                    code='geometry',
                    detail='synthetic exact rectangular fixture',
                ),
                CadValidationApplicabilitySpec(
                    code='band',
                    detail='synthetic 20-160 Hz fixture',
                ),
                CadValidationApplicabilitySpec(
                    code='routing',
                    detail='synthetic single-source routing fixture',
                ),
            ),
            trend_min_comparable_pairs=1,
            trend_min_agreement_ratio=0.75,
        )
    )
    validation_repository = CadModelValidationRepository(
        search_repository,
        roomsim_repository,
        measurement_repository,
        objective_repository,
    )
    validation_repository.save(validation)
    if validation.recommendation_gate != 'disabled' or validation.gate_reasons != (
        'automatic recommendation requires owned-room evidence',
    ):
        raise RuntimeError(
            'synthetic O60 fixture did not stop exclusively at owned-room boundary'
        )

    adaptive_repository = CadAdaptivePlanRepository(
        search_repository,
        validation_repository,
        objective_repository,
    )
    adaptive_service = CadAdaptivePlannerService(
        search_repository,
        objective_repository,
        validation_repository,
        adaptive_repository,
    )
    adaptive_plan = adaptive_service.build_and_save(
        validation_id=validation.validation_id,
        execution_scope='development_synthetic',
        length_scale_m=0.35,
        proposal_limit=20,
    )

    extended_repository = CadExtendedSearchRepository(
        search_repository,
        validation_repository,
    )
    capability = build_extended_model_capability(
        model_id=SYNTHETIC_DIRECTIONAL_MODEL_ID,
        model_version=SYNTHETIC_DIRECTIONAL_MODEL_VERSION,
        evidence_scope='synthetic_fixture',
        supported_parameters=('aim_yaw_deg',),
        detail='software acceptance only; not owned-room evidence',
        created_at_utc=_now(),
    )
    extended_repository.save_capability(capability)
    extended_spec = build_extended_search_spec(
        source_revision=source,
        base_spec=search_spec,
        base_candidate_set_sha256=page.candidate_set_sha256,
        base_candidate_count=page.feasible_candidate_count,
        capability=capability,
        axes=(
            CadExtendedSearchAxis(
                entity_id='synthetic-fl',
                min_value=-15.0,
                max_value=15.0,
                step=15.0,
            ),
        ),
        candidate_limit=50,
        created_at_utc=_now(),
    )
    extended_repository.save_spec(extended_spec)
    extended_page = generate_extended_candidates(
        scene_repository,
        search_spec,
        extended_spec,
        limit=50,
    )

    adaptive_extended_repository = CadAdaptiveExtendedRepository(
        extended_repository,
        validation_repository,
        objective_repository,
    )
    for index, candidate in enumerate(extended_page.candidates):
        # #382: the fixture binds each observation to the exact persisted O30
        # evaluation for the extended candidate's base candidate — the stored
        # value is the evaluation's own metric, never a fabricated number.
        predicted_evaluation = predicted_evaluation_by_candidate[
            candidate.base_candidate_id
        ]
        predicted_metric = predicted_evaluation.vector.metric(
            'response.shape_rms_db'
        )
        measured_evaluation = measured_evaluation_by_candidate.get(
            candidate.base_candidate_id
        )
        measured_metric = (
            measured_evaluation.vector.metric('response.shape_rms_db')
            if measured_evaluation is not None
            else None
        )
        observation = build_adaptive_extended_observation(
            extended_spec=extended_spec,
            candidate_set_sha256=extended_page.candidate_set_sha256,
            candidate_id=candidate.candidate_id,
            evidence_scope='synthetic_fixture',
            objective_id='response.shape_rms_db',
            unit=predicted_metric.unit,
            predicted_value=float(predicted_metric.value),
            prediction_source=build_observation_source_ref(
                kind='objective_evaluation',
                source_id=predicted_evaluation.evaluation_id,
                source_sha256=predicted_evaluation.evaluation_sha256,
            ),
            measured_value=(
                None if measured_metric is None else float(measured_metric.value)
            ),
            measurement_source=(
                build_observation_source_ref(
                    kind='objective_evaluation',
                    source_id=measured_evaluation.evaluation_id,
                    source_sha256=measured_evaluation.evaluation_sha256,
                )
                if measured_evaluation is not None
                else None
            ),
            created_at_utc=_now(),
        )
        adaptive_extended_repository.save_observation(observation)

    adaptive_extended_service = CadAdaptiveExtendedPlannerService(
        extended_repository,
        validation_repository,
        adaptive_extended_repository,
    )
    adaptive_extended_plan = adaptive_extended_service.build_and_save(
        extended_search_id=extended_spec.extended_search_id,
        validation_id=validation.validation_id,
        execution_scope='development_synthetic',
        length_scale_normalized=0.45,
        proposal_limit=20,
    )
    if adaptive_extended_plan.source_evidence_scope != 'synthetic_fixture':
        raise RuntimeError('adaptive extended synthetic plan crossed evidence scope')
    if set(adaptive_extended_plan.excluded_measured_candidate_ids) & {
        proposal.candidate_id for proposal in adaptive_extended_plan.proposals
    }:
        raise RuntimeError('adaptive extended plan proposed a measured candidate')

    return SyntheticDemoResult(
        document_id=SYNTHETIC_DEMO_DOCUMENT_ID,
        source_revision_id=source.revision_id,
        search_spec_id=search_spec.search_spec_id,
        candidate_set_sha256=page.candidate_set_sha256,
        validation_id=validation.validation_id,
        adaptive_plan_id=adaptive_plan.plan_id,
        adaptive_selected_candidate_id=adaptive_plan.selected_candidate_id,
        extended_search_id=extended_spec.extended_search_id,
        extended_candidate_set_sha256=extended_page.candidate_set_sha256,
        adaptive_extended_plan_id=adaptive_extended_plan.plan_id,
        adaptive_extended_selected_candidate_id=(
            adaptive_extended_plan.selected_candidate_id
        ),
    )
