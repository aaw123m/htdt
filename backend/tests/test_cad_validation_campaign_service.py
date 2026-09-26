from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_model_validation_service import (
    CadModelValidationService,
    CadValidationApplicabilitySpec,
)
from htdt.cad_objective_models import CadObjectiveInputRef, canonical_objective_sha256
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_results import (
    canonical_roomsim_result_json,
    canonical_roomsim_result_sha256,
)
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_validation_campaign import (
    CadValidationCampaignCandidate,
    CadValidationCampaignRepeatability,
    CadValidationCampaignSensitivity,
    CadValidationCampaignSeparation,
    CadValidationTargetResponse,
    build_validation_campaign,
)
from htdt.cad_validation_campaign_repository import CadValidationCampaignRepository
from htdt.cad_validation_campaign_service import CadValidationCampaignService
from htdt.optimization_objectives import ObjectiveMetric, ObjectiveVector


def _response(offset: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    return (
        (20.0, 40.0, 80.0, 160.0),
        (80.0 + offset, 81.0 + offset, 79.0 + offset, 80.0 + offset),
    )


class _Measurements:
    def __init__(self, path):
        self.path = path
        self.plans = ()
        self.records = {}
        self.datasets = {}

    def latest_measurement_plans(self, _search_spec_id):
        return tuple(self.plans)

    def list_measurement_plans(self, _search_spec_id):
        return tuple(self.plans)

    def get_measurement(self, measurement_id):
        return self.records.get(measurement_id)

    def dataset_for_measurement(self, measurement_id):
        return self.datasets.get(measurement_id)


class _RoomSim:
    def __init__(self, path, batch, attempts):
        self.path = path
        self.batch = batch
        self.attempts = {attempt.attempt_id: attempt for attempt in attempts}

    def list_batch_specs(self, _search_spec_id):
        return (self.batch,)

    def get_batch_spec(self, batch_run_id):
        return self.batch if batch_run_id == self.batch.batch_run_id else None

    def list_candidate_attempts(self, batch_run_id, candidate_id):
        return tuple(
            attempt
            for attempt in self.attempts.values()
            if attempt.batch_run_id == batch_run_id
            and attempt.candidate_id == candidate_id
        )

    def get_attempt(self, attempt_id):
        return self.attempts.get(attempt_id)


class _Objectives:
    def __init__(self, path):
        self.path = path
        self.evaluations = {}

    def list_evaluations(self, _search_spec_id):
        return tuple(self.evaluations.values())

    def get_evaluation(self, evaluation_id):
        return self.evaluations.get(evaluation_id)

    def save_evaluation(self, evaluation):
        self.evaluations[evaluation.evaluation_id] = evaluation


def _fixture(tmp_path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='campaign-readiness',
        schema_version=2,
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
            ),
        ),
    )
    revision = scene_repo.save(document, parent_revision_id=None).revision
    spec, _ = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=document.document_id, constraints=()),
        (CadSearchAxis(entity_id='fl', axis='x', min_m=1.0, max_m=1.4, step_m=0.2),),
        candidate_limit=10,
    )
    search_repo = CadSearchRepository(scene_repo)
    search_repo.save(spec)
    page = generate_cad_candidates(scene_repo, spec, limit=10)
    candidates = page.candidates[:3]
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)

    target_response = CadValidationTargetResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(0.0, 0.0, 0.0, 0.0),
    )
    evaluation_spec = {
        'algorithm_version': 'objective-vector-1',
        'objective_method': 'target_response',
        'objectives': ['response.shape_rms_db'],
        'response_band_hz': [20.0, 160.0],
        'reference_band_hz': [20.0, 160.0],
        'excluded_bands': [],
        'target_response': target_response.model_dump(mode='json'),
    }
    campaign = build_validation_campaign(
        document_id=document.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='fixture-1',
        requested_band_hz=(20.0, 160.0),
        max_holdout_rms_db=1.0,
        candidates=(
            CadValidationCampaignCandidate(candidate_id=candidate_ids[0], split='holdout'),
            CadValidationCampaignCandidate(candidate_id=candidate_ids[1], split='holdout'),
            CadValidationCampaignCandidate(candidate_id=candidate_ids[2], split='calibration'),
        ),
        objective_ids=('response.shape_rms_db',),
        target_response=target_response,
        reference_band_hz=(20.0, 160.0),
        sensitivity=(
            CadValidationCampaignSensitivity(
                objective_id='response.shape_rms_db',
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                max_observed_sensitivity_per_m=10.0,
                max_model_error_per_m=1.0,
            ),
        ),
        repeatability=(
            CadValidationCampaignRepeatability(
                candidate_id=candidate_ids[0],
                min_measurements=2,
            ),
        ),
        separation=(
            CadValidationCampaignSeparation(
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                repeatability_candidate_id=candidate_ids[0],
                min_repeatability_multiple=2.0,
            ),
        ),
        required_applicability_codes=('geometry', 'band', 'routing'),
    )

    measurements = _Measurements(scene_repo.path)
    campaign_repo = CadValidationCampaignRepository(search_repo, measurements)
    campaign_repo.save(campaign)

    batch = SimpleNamespace(
        batch_run_id='batch',
        document_id=document.document_id,
        scene_revision_id=spec.scene_revision_id,
        scene_content_hash=spec.scene_content_hash,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        binding_json=json.dumps({'geometry_mode': 'exact_rectangular'}),
        batch_spec_sha256='ab' * 32,
    )
    attempts = []
    objectives = _Objectives(scene_repo.path)
    eval_sha = canonical_objective_sha256(evaluation_spec)

    plan_rows = []
    for index, candidate_id in enumerate(candidate_ids):
        prediction_id = f'pred:{candidate_id}'
        frequency, predicted_levels = _response(float(index) * 2.0)
        response_payload = {
            'source_name': None,
            'mic_position': 'Main',
            'message': 'fixture',
            'unit': 'SPL',
            'smoothing': 'None',
            'start_frequency_hz': 20.0,
            'points_per_octave': 96.0,
            'frequency_step_hz': None,
            'frequency_hz': list(frequency),
            'magnitude': list(predicted_levels),
            'phase_deg': None,
        }
        attempts.append(SimpleNamespace(
            attempt_id=prediction_id,
            batch_run_id='batch',
            candidate_id=candidate_id,
            status='completed',
            result=SimpleNamespace(
                model_version='fixture-1',
                response_json=canonical_roomsim_result_json(response_payload),
                response_sha256=canonical_roomsim_result_sha256(response_payload),
            ),
        ))

        measurement_ids = [f'meas:{candidate_id}:1']
        if index == 0:
            measurement_ids.append(f'meas:{candidate_id}:2')
        for repeat_index, measurement_id in enumerate(measurement_ids):
            _, measured_levels = _response(float(index) * 2.0 + 0.1 * (repeat_index + 1))
            measurements.records[measurement_id] = SimpleNamespace(
                measurement_id=measurement_id,
                document_id=document.document_id,
                scene_revision_id=f'applied:{candidate_id}',
                evidence_type='measured',
                routing_evidence='verified',
                captured_at=f'2030-01-0{repeat_index + 1}T00:00:00+00:00',
                provenance_json=json.dumps({
                    'validation_scope': 'owned_room',
                    'validation_campaign_id': campaign.campaign_id,
                    # #848: 'verified' routing requires the exact profile pin.
                    'routing_profile': {
                        'routing_profile_id': f'profile:{candidate_id}',
                        'routing_profile_sha256': 'ab' * 32,
                    },
                }, separators=(',', ':')),
            )
            measurements.datasets[measurement_id] = CadFrequencyResponseDataset(
                dataset_id=f'dataset:{measurement_id}',
                measurement_id=measurement_id,
                frequency_hz=frequency,
                level_db=measured_levels,
                phase_status='absent',
                source_sha256=canonical_objective_sha256(
                    {'measurement_id': measurement_id}
                ),
                importer_version='campaign-service-test-1',
            )
        plan_rows.append(SimpleNamespace(
            plan_id=f'plan:{candidate_id}',
            status='measured',
            candidate_id=candidate_id,
            candidate_set_sha256=page.candidate_set_sha256,
            measurement_ids=tuple(measurement_ids),
            applied_scene_revision_id=f'applied:{candidate_id}',
            plan_sha256=f'{(index + 33):064x}',
        ))

        primary_measurement_id = measurement_ids[0]
        for evidence_class, source_kind, source_id, value, prefix in (
            ('predicted', 'cad_roomsim_attempt', prediction_id, float(index + 1), 'pred-eval'),
            ('measured', 'cad_measurement', primary_measurement_id, float(index + 1) + 0.1, 'meas-eval'),
        ):
            evaluation_id = f'{prefix}:{candidate_id}'
            objectives.evaluations[evaluation_id] = SimpleNamespace(
                evaluation_id=evaluation_id,
                evaluation_sha256=f'{index + 1:064x}' if evidence_class == 'predicted' else f'{index + 11:064x}',
                evaluation_spec_sha256=eval_sha,
                search_spec_sha256=spec.search_spec_sha256,
                candidate_id=candidate_id,
                input_refs=(
                    CadObjectiveInputRef(
                        evidence_class=evidence_class,
                        source_kind=source_kind,
                        source_id=source_id,
                    ),
                ),
                vector=ObjectiveVector(
                    candidate_id=candidate_id,
                    metrics=(
                        ObjectiveMetric(
                            objective_id='response.shape_rms_db',
                            value=value,
                            unit='dB',
                        ),
                    ),
                ),
            )

    measurements.plans = tuple(plan_rows)
    roomsim = _RoomSim(scene_repo.path, batch, attempts)
    validation_service = CadModelValidationService(
        search_repo,
        roomsim,
        measurements,
        objectives,
    )
    campaign_service = CadValidationCampaignService(
        campaign_repo,
        roomsim,
        measurements,
        objectives,
        validation_service,
    )
    return campaign, campaign_service, measurements, candidate_ids


def test_campaign_readiness_and_build_use_preregistered_evidence(tmp_path):
    campaign, service, _measurements, _candidate_ids = _fixture(tmp_path)

    readiness = service.readiness(campaign.campaign_id)

    assert readiness.evidence_ready
    assert all(not item.missing_reasons for item in readiness.candidates)

    record = service.build_validation_record(
        campaign.campaign_id,
        (
            CadValidationApplicabilitySpec(
                code='geometry', detail='exact rectangular room'
            ),
            CadValidationApplicabilitySpec(code='band', detail='20-160 Hz supported'),
            CadValidationApplicabilitySpec(code='routing', detail='routing verified'),
        ),
    )

    assert record.campaign_id == campaign.campaign_id
    assert record.campaign_sha256 == campaign.campaign_sha256
    registration = service.campaign_repository.get_registration(campaign.campaign_id)
    assert record.campaign_registration_id == registration.registration_id
    assert record.campaign_registration_sha256 == registration.registration_sha256
    assert record.recommendation_gate == 'eligible'
    assert {pair.split for pair in record.pairs} == {'calibration', 'holdout'}


def test_campaign_readiness_fails_closed_without_registration(tmp_path):
    campaign, service, _measurements, _candidate_ids = _fixture(tmp_path)
    # Simulate a pre-authority campaign row: no durable registration exists.
    with sqlite3.connect(service.campaign_repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_validation_campaign_registrations WHERE campaign_id=?',
            (campaign.campaign_id,),
        )

    with pytest.raises(ValueError, match='registration authority'):
        service.readiness(campaign.campaign_id)
    with pytest.raises(ValueError, match='registration authority'):
        service.build_validation_record(campaign.campaign_id, ())
    with pytest.raises(ValueError, match='registration authority'):
        service.materialize_objective_evidence(campaign.campaign_id)


def test_campaign_readiness_rejects_measurement_captured_before_preregistration(tmp_path):
    campaign, service, measurements, candidate_ids = _fixture(tmp_path)
    target = measurements.records[f'meas:{candidate_ids[0]}:1']
    target.captured_at = '2020-01-01T00:00:00+00:00'

    readiness = service.readiness(campaign.campaign_id)

    candidate = next(
        item for item in readiness.candidates
        if item.candidate_id == candidate_ids[0]
    )
    assert not readiness.evidence_ready
    assert any('captured before campaign' in reason for reason in candidate.missing_reasons)


def test_campaign_materializes_objectives_from_prediction_and_primary_measurement(tmp_path):
    campaign, service, _measurements, _candidate_ids = _fixture(tmp_path)
    service.objective_repository.evaluations.clear()

    saved_ids = service.materialize_objective_evidence(campaign.campaign_id)
    readiness = service.readiness(campaign.campaign_id)

    assert len(saved_ids) == len(campaign.candidates) * 2
    assert readiness.evidence_ready
    for candidate in readiness.candidates:
        assert candidate.predicted_evaluation_id is not None
        assert candidate.measured_evaluation_id is not None

    repeated = service.materialize_objective_evidence(campaign.campaign_id)
    assert repeated == saved_ids
    assert len(service.objective_repository.evaluations) == len(saved_ids)


# --- #810: owned-room readiness gates on the replay-validated quality
# authority, not only on lifecycle disposition. These fixtures use the real
# measurement repositories so the gate actually binds persisted reports.

from hashlib import sha256

from htdt.cad_measurement_loop import (
    build_measurement_plan,
    complete_measurement_plan,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset as _FrDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    acquisition_context_binding,
    build_acquisition_context,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_search import apply_candidate_positions, candidate_preview_document
from htdt.cad_document import WorkingDocument


def _real_fixture(tmp_path, *, usable_band=(20.0, 160.0), with_reports=True, with_context=True):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='campaign-quality',
        schema_version=2,
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
            ),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=2.0, z_m=1.1),
            ),
        ),
    )
    revision = scene_repo.save(document, parent_revision_id=None).revision
    spec, _ = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=document.document_id, constraints=()),
        # Axis starts clear of the baseline position so every candidate's
        # applied preview revision is a distinct child of the source revision.
        (CadSearchAxis(entity_id='fl', axis='x', min_m=1.2, max_m=1.8, step_m=0.2),),
        candidate_limit=10,
    )
    search_repo = CadSearchRepository(scene_repo)
    search_repo.save(spec)
    page = generate_cad_candidates(scene_repo, spec, limit=10)
    candidates = page.candidates[:3]
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)

    target_response = CadValidationTargetResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(0.0, 0.0, 0.0, 0.0),
    )
    campaign = build_validation_campaign(
        document_id=document.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='fixture-1',
        requested_band_hz=(20.0, 160.0),
        max_holdout_rms_db=1.0,
        candidates=(
            CadValidationCampaignCandidate(candidate_id=candidate_ids[0], split='holdout'),
            CadValidationCampaignCandidate(candidate_id=candidate_ids[1], split='holdout'),
            CadValidationCampaignCandidate(candidate_id=candidate_ids[2], split='calibration'),
        ),
        objective_ids=('response.shape_rms_db',),
        target_response=target_response,
        reference_band_hz=(20.0, 160.0),
        sensitivity=(
            CadValidationCampaignSensitivity(
                objective_id='response.shape_rms_db',
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                max_observed_sensitivity_per_m=10.0,
                max_model_error_per_m=1.0,
            ),
        ),
        repeatability=(
            CadValidationCampaignRepeatability(
                candidate_id=candidate_ids[0],
                min_measurements=2,
            ),
        ),
        separation=(
            CadValidationCampaignSeparation(
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                repeatability_candidate_id=candidate_ids[0],
                min_repeatability_multiple=2.0,
            ),
        ),
        required_applicability_codes=('geometry', 'band', 'routing'),
    )

    measurements = CadMeasurementRepository(scene_repo)
    campaign_repo = CadValidationCampaignRepository(search_repo, measurements)
    campaign_repo.save(campaign)
    quality = CadMeasurementQualityRepository(measurements)

    evaluation_spec = {
        'algorithm_version': 'objective-vector-1',
        'objective_method': 'target_response',
        'objectives': ['response.shape_rms_db'],
        'response_band_hz': [20.0, 160.0],
        'reference_band_hz': [20.0, 160.0],
        'excluded_bands': [],
        'target_response': target_response.model_dump(mode='json'),
    }
    eval_sha = canonical_objective_sha256(evaluation_spec)
    objectives = _Objectives(scene_repo.path)

    batch = SimpleNamespace(
        batch_run_id='batch',
        document_id=document.document_id,
        scene_revision_id=spec.scene_revision_id,
        scene_content_hash=spec.scene_content_hash,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        binding_json=json.dumps({'geometry_mode': 'exact_rectangular'}),
        batch_spec_sha256='ab' * 32,
    )
    attempts = []

    for index, candidate in enumerate(candidates):
        candidate_id = candidate.candidate_id
        working = WorkingDocument(document, source_revision_id=revision.revision_id)
        apply_candidate_positions(
            working,
            candidate,
            spec=spec,
            current_constraint_set=CadConstraintSet(
                document_id=document.document_id, constraints=()
            ),
        )
        applied_revision = scene_repo.save_detached_revision(
            working.committed_document,
            parent_revision_id=revision.revision_id,
            reason='owned-room campaign fixture',
        ).revision

        prediction_id = f'pred:{candidate_id}'
        frequency, predicted_levels = _response(float(index) * 2.0)
        response_payload = {
            'source_name': None,
            'mic_position': 'Main',
            'message': 'fixture',
            'unit': 'SPL',
            'smoothing': 'None',
            'start_frequency_hz': 20.0,
            'points_per_octave': 96.0,
            'frequency_step_hz': None,
            'frequency_hz': list(frequency),
            'magnitude': list(predicted_levels),
            'phase_deg': None,
        }
        attempts.append(SimpleNamespace(
            attempt_id=prediction_id,
            batch_run_id='batch',
            candidate_id=candidate_id,
            status='completed',
            result=SimpleNamespace(
                model_version='fixture-1',
                response_json=canonical_roomsim_result_json(response_payload),
                response_sha256=canonical_roomsim_result_sha256(response_payload),
            ),
        ))

        measurement_ids = [f'meas:{candidate_id}:1']
        if index == 0:
            measurement_ids.append(f'meas:{candidate_id}:2')
        for repeat_index, measurement_id in enumerate(measurement_ids):
            declared_raw = declared_fr_raw(
                frequency_hz=frequency,
                level_db=(80.0 + index, 81.0 + index, 79.0 + index, 80.0 + index),
                phase_status='absent',
                processing={'fixture': measurement_id},
            )
            record = measurement_record_for_revision(
                applied_revision,
                'mlp',
                measurement_id=measurement_id,
                evidence_type='measured',
                channel_role='front_left',
                source_speaker_ids=('fl',),
                routing_evidence='verified',
                captured_at=f'2030-01-0{index + 1}T0{repeat_index}:00:00+00:00',
                imported_at=f'2030-01-0{index + 1}T0{repeat_index}:00:10+00:00',
                source_kind='unknown',
                external_source_id=f'rew-{measurement_id}',
                provenance={
                    'validation_scope': 'owned_room',
                    'validation_campaign_id': campaign.campaign_id,
                    'routing_profile': {
                        'routing_profile_id': f'profile:{candidate_id}',
                        'routing_profile_sha256': 'ab' * 32,
                    },
                },
            )
            dataset = _FrDataset(
                dataset_id=f'dataset:{measurement_id}',
                measurement_id=measurement_id,
                frequency_hz=frequency,
                level_db=(80.0 + index, 81.0 + index, 79.0 + index, 80.0 + index),
                phase_status='absent',
                processing_json=canonical_json({'fixture': measurement_id}),
                source_sha256=sha256(declared_raw).hexdigest(),
                importer_version=HTDT_DECLARED_IMPORTER_VERSION,
            )
            measurements.save(
                record,
                dataset,
                raw_filename=f'{measurement_id}.json',
                raw_bytes=declared_raw,
            )

            if with_reports:
                context = build_acquisition_context(
                    source_kind='native',
                    subject_measurement_ids=(measurement_id,),
                    acquisition_context_id=f'ctx:{measurement_id}',
                )
                quality.save_acquisition_context(context)
                observation = build_measurement_observation(
                    measurement_id=measurement_id,
                    source_kind='manual',
                    observation_id=f'obs:{measurement_id}',
                    observed_at_utc=f'2030-01-0{index + 1}T0{repeat_index}:00:30+00:00',
                    clipping_detected=False,
                    snr_db=40.0,
                    usable_frequency_band_hz=usable_band,
                )
                quality.save_observation(observation)
                report = build_measurement_quality_report(
                    measurement=record,
                    dataset=dataset,
                    evidence=CadMeasurementQualityEvidence(
                        clipping_detected=False,
                        snr_db=40.0,
                        usable_frequency_band_hz=usable_band,
                        evidence_source='manual',
                    ),
                    profile=build_measurement_quality_profile(),
                    acquisition_context=(
                        acquisition_context_binding(context) if with_context else None
                    ),
                    acquisition_context_record=context if with_context else None,
                    observation=observation_binding(observation),
                    observation_record=observation,
                    report_id=f'report:{measurement_id}',
                    created_at_utc=f'2030-01-0{index + 1}T0{repeat_index}:01:00+00:00',
                )
                quality.save_report(report)

        plan = build_measurement_plan(
            scene_repo,
            search_repo,
            search_spec_id=spec.search_spec_id,
            candidate_id=candidate_id,
            applied_scene_revision_id=applied_revision.revision_id,
        )
        measurements.save_measurement_plan(plan)
        measurements.save_measurement_plan(
            complete_measurement_plan(plan, measurements, tuple(measurement_ids))
        )

        primary_measurement_id = measurement_ids[0]
        for evidence_class, source_kind, source_id, value, prefix in (
            ('predicted', 'cad_roomsim_attempt', prediction_id, float(index + 1), 'pred-eval'),
            ('measured', 'cad_measurement', primary_measurement_id, float(index + 1) + 0.1, 'meas-eval'),
        ):
            evaluation_id = f'{prefix}:{candidate_id}'
            objectives.evaluations[evaluation_id] = SimpleNamespace(
                evaluation_id=evaluation_id,
                evaluation_sha256=f'{index + 1:064x}' if evidence_class == 'predicted' else f'{index + 11:064x}',
                evaluation_spec_sha256=eval_sha,
                search_spec_sha256=spec.search_spec_sha256,
                candidate_id=candidate_id,
                input_refs=(
                    CadObjectiveInputRef(
                        evidence_class=evidence_class,
                        source_kind=source_kind,
                        source_id=source_id,
                    ),
                ),
                vector=ObjectiveVector(
                    candidate_id=candidate_id,
                    metrics=(
                        ObjectiveMetric(
                            objective_id='response.shape_rms_db',
                            value=value,
                            unit='dB',
                        ),
                    ),
                ),
            )

    roomsim = _RoomSim(scene_repo.path, batch, attempts)
    validation_service = CadModelValidationService(
        search_repo,
        roomsim,
        measurements,
        objectives,
    )
    campaign_service = CadValidationCampaignService(
        campaign_repo,
        roomsim,
        measurements,
        objectives,
        validation_service,
    )
    return campaign, campaign_service, quality, candidate_ids


def test_owned_room_readiness_requires_persisted_quality_report(tmp_path):
    campaign, service, _quality, _ids = _real_fixture(tmp_path, with_reports=False)

    readiness = service.readiness(campaign.campaign_id)

    assert not readiness.evidence_ready
    assert all(
        any('quality report' in reason for reason in item.missing_reasons)
        for item in readiness.candidates
    )


def test_owned_room_readiness_requires_authoritative_acquisition_context(tmp_path):
    campaign, service, _quality, _ids = _real_fixture(tmp_path, with_context=False)

    readiness = service.readiness(campaign.campaign_id)

    assert not readiness.evidence_ready
    assert all(
        any('acquisition context' in reason for reason in item.missing_reasons)
        for item in readiness.candidates
    )


def test_owned_room_readiness_rejects_insufficient_claim_band(tmp_path):
    campaign, service, _quality, _ids = _real_fixture(
        tmp_path, usable_band=(30.0, 70.0)
    )

    readiness = service.readiness(campaign.campaign_id)

    assert not readiness.evidence_ready
    assert all(
        any('capability' in reason for reason in item.missing_reasons)
        for item in readiness.candidates
    )


def test_owned_room_readiness_passes_with_complete_quality_evidence(tmp_path):
    campaign, service, _quality, _ids = _real_fixture(tmp_path)

    readiness = service.readiness(campaign.campaign_id)

    assert readiness.evidence_ready
    assert all(not item.missing_reasons for item in readiness.candidates)
