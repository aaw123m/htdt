from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import htdt.cad_robustness_validation_repository as validation_repository_module
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_measurement_loop import (
    bind_measurement_plan_prediction,
    build_measurement_plan,
    complete_measurement_plan,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadAcquisitionContextBinding,
    CadMeasurementQualityEvidence,
    build_measurement_quality_profile,
    build_measurement_quality_report,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_model_validation import build_full_model_validation
from htdt.cad_objective_models import CadObjectiveInputRef
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_objectives import build_objective_evaluation
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_results import roomsim_attempt_frequency_response
from htdt.cad_robustness_repository import CadRobustnessRepository
from htdt.cad_robustness_validation_repository import (
    CadRobustnessValidationRepository,
)
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import (
    build_cad_search_spec,
    candidate_preview_document,
    generate_cad_candidates,
)
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
from htdt.cad_validation_metrics import (
    CadApplicabilityEvidenceRef,
    CadObjectiveValidationSample,
    build_applicability_check,
    build_candidate_separation_check,
    build_repeatability_check,
    build_sensitivity_check,
)
from htdt.comparison import FrequencyResponse
from htdt.optimization_objectives import (
    ObjectiveMetric,
    ObjectiveVector,
    ResponseObjectiveSpec,
    target_response_objectives,
)
from htdt.optimization_robustness import UncertaintyAxis, build_robustness_spec
from htdt.optimization_robustness_validation import (
    build_o90e_decision,
    build_o90e_validation_case,
)


def _manual_check(code: str, detail: str):
    """Fixture attestation-bound check; these tests never hit a repository."""
    return build_applicability_check(
        code=code,
        passed=True,
        evaluator_id='o60-applicability-manual',
        evaluator_version='1',
        subject={
            'code': code,
            'document_id': 'o90e-owned-room-fixture',
            'scope': {},
            'search_spec_id': 'o90e-fixture-spec',
            'search_spec_sha256': '6' * 64,
        },
        evidence_refs=(
            CadApplicabilityEvidenceRef(
                source_kind='o60_applicability_attestation',
                source_id='o60-applicability-attestation:' + '5' * 64,
                source_sha256='5' * 64,
            ),
        ),
        detail=detail,
    )


class _ModelValidationRepository:
    def __init__(self, path):
        self.path = path
        self.record = None

    def get(self, validation_id):
        if self.record is None or self.record.validation_id != validation_id:
            return None
        return self.record


class _RoomSimRepository:
    def __init__(
        self,
        path,
        *,
        search_spec,
        candidate_set_sha256,
        model_id,
        model_version,
        candidate_ids,
    ):
        self.path = path
        self.batch = SimpleNamespace(
            batch_run_id='batch:o90e',
            document_id=search_spec.document_id,
            scene_revision_id=search_spec.scene_revision_id,
            scene_content_hash=search_spec.scene_content_hash,
            search_spec_id=search_spec.search_spec_id,
            search_spec_sha256=search_spec.search_spec_sha256,
            candidate_set_sha256=candidate_set_sha256,
            model_id=model_id,
            adapter_version='fixture-adapter',
            binding_sha256=sha256(b'o90e-binding').hexdigest(),
            batch_spec_sha256=sha256(b'o90e-batch').hexdigest(),
        )
        self.attempts = {}
        for index, candidate_id in enumerate(candidate_ids):
            response_json = _response_json(float(index) * 0.1)
            self.attempts[candidate_id] = SimpleNamespace(
                attempt_id=f'pred:{candidate_id}',
                batch_run_id=self.batch.batch_run_id,
                candidate_id=candidate_id,
                status='completed',
                model_version=model_version,
                response_json=response_json,
                response_sha256=sha256(response_json.encode()).hexdigest(),
                attempt_sha256=sha256(
                    f'o90e-attempt:{candidate_id}'.encode()
                ).hexdigest(),
            )

    def get_attempt(self, attempt_id):
        candidate_id = attempt_id.removeprefix('pred:')
        attempt = self.attempts.get(candidate_id)
        if attempt is None or attempt.attempt_id != attempt_id:
            return None
        return attempt

    def get_batch_spec(self, batch_run_id):
        return self.batch if batch_run_id == self.batch.batch_run_id else None


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id='o90e-owned-room-fixture',
        schema_version=3,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=0.6, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
            ),
            SceneEntity(
                entity_id='listener-main',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _response(offset: float) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(70.0 + offset, 71.0 + offset, 69.0 + offset, 70.0 + offset),
    )


def _response_json(offset: float) -> str:
    response = _response(offset)
    return json.dumps(
        {
            'frequency_hz': list(response.frequency_hz),
            'magnitude': list(response.level_db),
        },
        separators=(',', ':'),
    )


def _candidate_x(candidate) -> float:
    return float(candidate.positions['speaker-fl']['x_m'])


def _save_quality(
    env,
    measurement_id: str,
    *,
    report_id: str | None = None,
    acquisition: bool = True,
    usable_band=(20.0, 160.0),
):
    measurement = env.measurement_repository.get_measurement(measurement_id)
    dataset = env.measurement_repository.dataset_for_measurement(measurement_id)
    profile = build_measurement_quality_profile(
        profile_version='o90e-fixture-1',
        minimum_snr_db=20.0,
        required_usable_band_hz=(20.0, 160.0),
    )
    evidence = CadMeasurementQualityEvidence(
        clipping_detected=False,
        peak_dbfs=-6.0,
        noise_floor_db_spl=30.0,
        signal_level_db_spl=70.0,
        snr_db=40.0,
        usable_frequency_band_hz=usable_band,
        evidence_source='rew_metadata',
    )
    context = (
        CadAcquisitionContextBinding(
            acquisition_context_id=f'acq:{measurement_id}',
            acquisition_context_sha256=sha256(
                f'acq:{measurement_id}'.encode()
            ).hexdigest(),
            source_kind='native',
        )
        if acquisition
        else None
    )
    report = build_measurement_quality_report(
        measurement=measurement,
        dataset=dataset,
        evidence=evidence,
        profile=profile,
        acquisition_context=context,
        report_id=report_id or f'quality:{measurement_id}',
        created_at_utc='2030-01-01T02:00:00+00:00',
    )
    env.quality_repository.save_report(report)
    return report


def _build_o60_record(
    env,
    *,
    model_version=None,
    sensitivity_checks=None,
    band=None,
    evidence_scope='owned_room',
):
    model_version = model_version or env.spec.model_version
    band = band or (20.0, 160.0)
    predicted_values = {
        env.minus.candidate_id: 1.8,
        env.nominal.candidate_id: 2.0,
        env.plus.candidate_id: 2.2,
        env.calibration.candidate_id: 2.4,
    }
    measured_values = {
        candidate_id: value + 0.05
        for candidate_id, value in predicted_values.items()
    }
    splits = {
        env.minus.candidate_id: 'holdout',
        env.nominal.candidate_id: 'holdout',
        env.plus.candidate_id: 'holdout',
        env.calibration.candidate_id: 'calibration',
    }

    response_samples = []
    objective_samples = []
    for candidate in (env.minus, env.nominal, env.plus, env.calibration):
        candidate_id = candidate.candidate_id
        measurement_id = env.measurement_ids[candidate_id]
        predicted = _response(predicted_values[candidate_id] * 0.1)
        measured = _response(predicted_values[candidate_id] * 0.1 + 0.05)
        response_samples.append(
            (
                candidate_id,
                splits[candidate_id],
                f'pred:{candidate_id}',
                measurement_id,
                predicted,
                measured,
            )
        )
        objective_samples.append(
            CadObjectiveValidationSample(
                candidate_id=candidate_id,
                split=splits[candidate_id],
                objective_id='response.shape_rms_db',
                unit='dB',
                predicted_evaluation_id=f'pred-eval:{candidate_id}',
                measured_evaluation_id=f'meas-eval:{candidate_id}',
                predicted_value=predicted_values[candidate_id],
                measured_value=measured_values[candidate_id],
            )
        )

    checks = sensitivity_checks
    if checks is None:
        checks = (
            build_sensitivity_check(
                objective_id='response.shape_rms_db',
                unit='dB',
                candidate_a_id=env.nominal.candidate_id,
                candidate_b_id=env.minus.candidate_id,
                placement_delta_m=0.2,
                predicted_a=2.0,
                predicted_b=1.8,
                measured_a=2.05,
                measured_b=1.85,
                max_observed_sensitivity_per_m=2.0,
                max_model_error_per_m=1.0,
            ),
            build_sensitivity_check(
                objective_id='response.shape_rms_db',
                unit='dB',
                candidate_a_id=env.nominal.candidate_id,
                candidate_b_id=env.plus.candidate_id,
                placement_delta_m=0.2,
                predicted_a=2.0,
                predicted_b=2.2,
                measured_a=2.05,
                measured_b=2.25,
                max_observed_sensitivity_per_m=2.0,
                max_model_error_per_m=1.0,
            ),
        )

    repeatability = build_repeatability_check(
        scene_revision_id=env.applied[env.nominal.candidate_id].revision_id,
        measurements=(
            (env.measurement_ids[env.nominal.candidate_id], _response(0.20)),
            ('repeat:nominal', _response(0.22)),
        ),
        low_hz=20.0,
        high_hz=160.0,
        reference_band_hz=(20.0, 160.0),
    )
    separation = build_candidate_separation_check(
        candidate_a_id=env.nominal.candidate_id,
        candidate_b_id=env.minus.candidate_id,
        measurement_a_id=env.measurement_ids[env.nominal.candidate_id],
        measurement_b_id=env.measurement_ids[env.minus.candidate_id],
        response_a=_response(0.20),
        response_b=_response(0.00),
        low_hz=20.0,
        high_hz=160.0,
        repeatability_floor_db=repeatability.rms_floor_db,
        min_repeatability_multiple=1.0,
    )
    record = build_full_model_validation(
        document_id=env.spec.document_id,
        search_spec_id=env.spec.search_spec_id,
        search_spec_sha256=env.spec.search_spec_sha256,
        candidate_set_sha256=env.spec.candidate_set_sha256,
        model_id=env.spec.model_id,
        model_version=model_version,
        response_samples=tuple(response_samples),
        objective_samples=tuple(objective_samples),
        sensitivity_checks=tuple(checks),
        repeatability_checks=(repeatability,),
        separation_checks=(separation,),
        applicability_checks=(
            _manual_check('geometry', 'exact software fixture geometry'),
            _manual_check('band', '20-160 Hz fixture band'),
            _manual_check('routing', 'fixture source routing is explicit'),
        ),
        low_hz=band[0],
        high_hz=band[1],
        max_holdout_rms_db=1.0,
        evidence_scope=evidence_scope,
        campaign_id=(
            env.campaign.campaign_id if evidence_scope == 'owned_room' else None
        ),
        campaign_sha256=(
            env.campaign.campaign_sha256 if evidence_scope == 'owned_room' else None
        ),
        campaign_registration_id=(
            env.registration.registration_id
            if evidence_scope == 'owned_room'
            else None
        ),
        campaign_registration_sha256=(
            env.registration.registration_sha256
            if evidence_scope == 'owned_room'
            else None
        ),
    )
    return record


def _capture_measurement(
    env,
    revision,
    measurement_id: str,
    *,
    captured_at: str = '2030-01-01T01:30:00+00:00',
    imported_at: str = '2030-01-01T01:31:00+00:00',
    external_source_id: str | None = None,
    processing: dict | None = None,
    levels: tuple[float, ...] = (70.0, 71.0, 69.0, 70.0),
):
    record = measurement_record_for_revision(
        revision,
        'listener-main',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        captured_at=captured_at,
        imported_at=imported_at,
        source_kind='unknown',
        external_source_id=external_source_id,
        provenance={
            'validation_scope': 'owned_room',
            'validation_campaign_id': env.campaign.campaign_id,
        },
    )
    processing = processing or {'fixture_raw': measurement_id}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=levels,
        phase_status='absent',
        level_reference='spl',
        processing=processing,
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset:{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=levels,
        phase_status='absent',
        level_reference='spl',
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    env.measurement_repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    return record


def _fixture(tmp_path, *, measured: bool = True):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    source = scene_repository.save(_scene(), parent_revision_id=None).revision
    search_repository = CadSearchRepository(scene_repository)
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)

    search_spec, _ = build_cad_search_spec(
        source,
        CadConstraintSet(document_id=source.document_id, constraints=()),
        (
            CadSearchAxis(
                entity_id='speaker-fl',
                axis='x',
                min_m=0.8,
                max_m=1.4,
                step_m=0.2,
            ),
        ),
        candidate_limit=20,
        name='O90E fixture search',
    )
    search_repository.save(search_spec)
    page = generate_cad_candidates(scene_repository, search_spec, limit=20)
    by_x = {round(_candidate_x(candidate), 6): candidate for candidate in page.candidates}
    minus = by_x[0.8]
    nominal = by_x[1.0]
    plus = by_x[1.2]
    calibration = by_x[1.4]

    applied = {}
    planned = {}
    for candidate in (minus, nominal, plus, calibration):
        preview = candidate_preview_document(source.document, candidate)
        # Measurement plans require each applied revision to descend directly
        # from the SearchSpec source, so the fixture deliberately writes
        # non-head sibling lineage.
        revision = scene_repository.save(
            preview,
            parent_revision_id=source.revision_id,
            allow_branch=True,
        ).revision
        applied[candidate.candidate_id] = revision
        plan = build_measurement_plan(
            scene_repository,
            search_repository,
            search_spec_id=search_spec.search_spec_id,
            candidate_id=candidate.candidate_id,
            applied_scene_revision_id=revision.revision_id,
        )
        measurement_repository.save_measurement_plan(plan)
        planned[candidate.candidate_id] = plan

    campaign = build_validation_campaign(
        document_id=source.document_id,
        search_spec_id=search_spec.search_spec_id,
        search_spec_sha256=search_spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='fixture-model',
        model_version='fixture-1',
        requested_band_hz=(20.0, 160.0),
        max_holdout_rms_db=1.0,
        candidates=(
            CadValidationCampaignCandidate(
                candidate_id=minus.candidate_id,
                split='holdout',
            ),
            CadValidationCampaignCandidate(
                candidate_id=nominal.candidate_id,
                split='holdout',
            ),
            CadValidationCampaignCandidate(
                candidate_id=plus.candidate_id,
                split='holdout',
            ),
            CadValidationCampaignCandidate(
                candidate_id=calibration.candidate_id,
                split='calibration',
            ),
        ),
        objective_ids=('response.shape_rms_db',),
        target_response=CadValidationTargetResponse(
            frequency_hz=(20.0, 40.0, 80.0, 160.0),
            level_db=(0.0, 0.0, 0.0, 0.0),
        ),
        reference_band_hz=(20.0, 160.0),
        sensitivity=(
            CadValidationCampaignSensitivity(
                objective_id='response.shape_rms_db',
                candidate_a_id=nominal.candidate_id,
                candidate_b_id=minus.candidate_id,
                max_observed_sensitivity_per_m=2.0,
                max_model_error_per_m=1.0,
            ),
            CadValidationCampaignSensitivity(
                objective_id='response.shape_rms_db',
                candidate_a_id=nominal.candidate_id,
                candidate_b_id=plus.candidate_id,
                max_observed_sensitivity_per_m=2.0,
                max_model_error_per_m=1.0,
            ),
        ),
        repeatability=(
            CadValidationCampaignRepeatability(
                candidate_id=nominal.candidate_id,
                min_measurements=2,
                reference_band_hz=(20.0, 160.0),
            ),
        ),
        separation=(
            CadValidationCampaignSeparation(
                candidate_a_id=nominal.candidate_id,
                candidate_b_id=minus.candidate_id,
                repeatability_candidate_id=nominal.candidate_id,
                min_repeatability_multiple=1.0,
            ),
        ),
        required_applicability_codes=('geometry', 'band', 'routing'),
    )
    campaign_repository = CadValidationCampaignRepository(
        search_repository,
        measurement_repository,
    )
    registration = campaign_repository.save(campaign)

    roomsim_repository = _RoomSimRepository(
        scene_repository.path,
        search_spec=search_spec,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='fixture-model',
        model_version='fixture-1',
        candidate_ids=tuple(
            candidate.candidate_id
            for candidate in (minus, nominal, plus, calibration)
        ),
    )
    prediction_id = f'pred:{nominal.candidate_id}'
    campaign_spec = json.loads(campaign.objective_evaluation_spec_json)
    predicted_response = roomsim_attempt_frequency_response(
        roomsim_repository.get_attempt(prediction_id)
    )
    target_response = FrequencyResponse(
        frequency_hz=tuple(
            float(value)
            for value in campaign_spec['target_response']['frequency_hz']
        ),
        level_db=tuple(
            float(value)
            for value in campaign_spec['target_response']['level_db']
        ),
    )
    response_spec = ResponseObjectiveSpec(
        low_hz=float(campaign_spec['response_band_hz'][0]),
        high_hz=float(campaign_spec['response_band_hz'][1]),
        reference_band_hz=(
            None
            if campaign_spec.get('reference_band_hz') is None
            else tuple(
                float(value)
                for value in campaign_spec['reference_band_hz']
            )
        ),
        excluded_bands=tuple(
            (float(band[0]), float(band[1]))
            for band in campaign_spec.get('excluded_bands') or ()
        ),
    )
    full_vector = target_response_objectives(
        nominal.candidate_id,
        predicted_response,
        target_response,
        response_spec,
        prefix='response',
    )
    objective = build_objective_evaluation(
        source,
        search_spec,
        nominal.candidate_id,
        ObjectiveVector(
            candidate_id=nominal.candidate_id,
            metrics=tuple(
                full_vector.metric(objective_id)
                for objective_id in campaign_spec['objectives']
            ),
        ),
        evaluation_spec=campaign_spec,
        input_refs=(
            CadObjectiveInputRef(
                evidence_class='derived',
                source_kind='candidate_geometry',
                source_id=nominal.candidate_id,
            ),
            CadObjectiveInputRef(
                evidence_class='predicted',
                source_kind='cad_roomsim_attempt',
                source_id=prediction_id,
            ),
        ),
    )
    spec = build_robustness_spec(
        source_revision=source,
        search_spec=search_spec,
        candidate=nominal,
        candidate_set_sha256=page.candidate_set_sha256,
        nominal_objective=objective,
        nominal_prediction_result_ref=prediction_id,
        model_id='fixture-model',
        model_version='fixture-1',
        prediction_provider_id='fixture-adapter',
        fidelity='fixture-owned-room',
        axes=(
            UncertaintyAxis(
                axis_id='speaker-x',
                entity_id='speaker-fl',
                parameter='speaker_x_m',
                unit='m',
                nominal_value=1.0,
                minus_delta=0.2,
                plus_delta=0.2,
            ),
        ),
        software_version='test',
        created_at_utc='2030-01-01T00:00:00+00:00',
    )
    objective_repository = CadObjectiveRepository(
        scene_repository,
        search_repository,
        roomsim_repository=roomsim_repository,
    )
    objective_repository.save_evaluation(objective)
    robustness_repository = CadRobustnessRepository(
        scene_repository=scene_repository,
        search_repository=search_repository,
        objective_repository=objective_repository,
    )
    robustness_repository.save_spec(spec)

    model_validation_repository = _ModelValidationRepository(scene_repository.path)
    validation_repository = CadRobustnessValidationRepository(
        robustness_repository=robustness_repository,
        model_validation_repository=model_validation_repository,
        campaign_repository=campaign_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        roomsim_repository=roomsim_repository,
    )

    minus_case = validation_repository.preregister_case(
        robustness_spec_id=spec.robustness_spec_id,
        axis_id='speaker-x',
        direction='minus',
        nominal_measurement_plan_id=planned[nominal.candidate_id].plan_id,
        perturbation_measurement_plan_id=planned[minus.candidate_id].plan_id,
        o60_campaign_id=campaign.campaign_id,
        observable_id='response.shape_rms_db',
        receiver_entity_id='listener-main',
        required_capability='magnitude_response',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
    )
    plus_case = validation_repository.preregister_case(
        robustness_spec_id=spec.robustness_spec_id,
        axis_id='speaker-x',
        direction='plus',
        nominal_measurement_plan_id=planned[nominal.candidate_id].plan_id,
        perturbation_measurement_plan_id=planned[plus.candidate_id].plan_id,
        o60_campaign_id=campaign.campaign_id,
        observable_id='response.shape_rms_db',
        receiver_entity_id='listener-main',
        required_capability='magnitude_response',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
    )

    env = SimpleNamespace(
        scene_repository=scene_repository,
        source=source,
        search_repository=search_repository,
        search_spec=search_spec,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        campaign_repository=campaign_repository,
        campaign=campaign,
        registration=registration,
        robustness_repository=robustness_repository,
        model_validation_repository=model_validation_repository,
        roomsim_repository=roomsim_repository,
        validation_repository=validation_repository,
        spec=spec,
        minus=minus,
        nominal=nominal,
        plus=plus,
        calibration=calibration,
        applied=applied,
        planned=planned,
        minus_case=minus_case,
        plus_case=plus_case,
        measurement_ids={},
    )

    if measured:
        for index, candidate in enumerate(
            (minus, nominal, plus, calibration), start=1
        ):
            candidate_id = candidate.candidate_id
            measurement_id = f'measurement:{candidate_id}'
            revision = applied[candidate_id]
            _capture_measurement(
                env,
                revision,
                measurement_id,
                captured_at=f'2030-01-01T01:0{index}:00+00:00',
                imported_at=f'2030-01-01T01:1{index}:00+00:00',
                external_source_id=f'rew:{candidate_id}',
                processing={'fixture_raw': f'o90e:{candidate_id}'},
                levels=tuple(_response(index * 0.1).level_db),
            )
            completed = complete_measurement_plan(
                planned[candidate_id],
                measurement_repository,
                (measurement_id,),
            )
            measurement_repository.save_measurement_plan(completed)
            env.measurement_ids[candidate_id] = measurement_id
            _save_quality(env, measurement_id)

        record = _build_o60_record(env)
        assert record.recommendation_gate == 'eligible'
        env.model_validation_repository.record = record
        env.o60_record = record
    return env


def _decision(env, *, case_ids=None, decided_at='2030-01-01T03:00:00+00:00'):
    return env.validation_repository.evaluate_and_save_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.model_validation_repository.record.validation_id,
        case_ids=case_ids
        if case_ids is not None
        else (env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc=decided_at,
    )


def test_o90e_complete_preregistered_o60_sensitivity_evidence_is_eligible_and_reopens(
    tmp_path,
) -> None:
    env = _fixture(tmp_path)

    decision = _decision(env)

    assert decision.production_gate == 'eligible'
    assert decision.support_state == 'full'
    assert decision.reasons == ('eligible',)
    assert all(item.status == 'supported' for item in decision.assessments)
    assert all(item.sensitivity_evidence_sha256 for item in decision.assessments)
    assert all(item.state == 'full' for item in decision.axis_coverage)

    reopened = CadRobustnessValidationRepository(
        robustness_repository=env.robustness_repository,
        model_validation_repository=env.model_validation_repository,
        campaign_repository=env.campaign_repository,
        measurement_repository=env.measurement_repository,
        quality_repository=env.quality_repository,
        roomsim_repository=env.roomsim_repository,
    )
    assert reopened.get_case(env.minus_case.case_id) == env.minus_case
    assert reopened.get_decision(decision.decision_id) == decision


def test_o90e_wrong_spec_candidate_scene_and_observable_are_rejected(tmp_path) -> None:
    env = _fixture(tmp_path)
    nominal_plan = env.planned[env.nominal.candidate_id]
    minus_plan = env.planned[env.minus.candidate_id]

    with pytest.raises(ValueError, match='campaign/search authority mismatch'):
        foreign_spec = env.spec.model_copy(
            update={'search_spec_sha256': '0' * 64},
        )
        build_o90e_validation_case(
            spec=foreign_spec,
            axis_id='speaker-x',
            direction='minus',
            nominal_plan=nominal_plan,
            perturbation_plan=minus_plan,
            nominal_revision=env.applied[env.nominal.candidate_id],
            perturbation_revision=env.applied[env.minus.candidate_id],
            campaign=env.campaign,
            campaign_registration=env.registration,
            observable_id='response.shape_rms_db',
            receiver_entity_id='listener-main',
            required_capability='magnitude_response',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            radiation_scope='single',
            preregistered_at_utc='2030-01-01T00:30:00+00:00',
        )

    with pytest.raises(ValueError, match='nominal MeasurementPlan/candidate'):
        env.validation_repository.preregister_case(
            robustness_spec_id=env.spec.robustness_spec_id,
            axis_id='speaker-x',
            direction='minus',
            nominal_measurement_plan_id=minus_plan.plan_id,
            perturbation_measurement_plan_id=nominal_plan.plan_id,
            o60_campaign_id=env.campaign.campaign_id,
            observable_id='response.shape_rms_db',
            receiver_entity_id='listener-main',
            required_capability='magnitude_response',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            radiation_scope='single',
        )

    with pytest.raises(ValueError, match='MeasurementPlan SceneRevision mismatch'):
        build_o90e_validation_case(
            spec=env.spec,
            axis_id='speaker-x',
            direction='minus',
            nominal_plan=nominal_plan,
            perturbation_plan=minus_plan,
            nominal_revision=env.applied[env.minus.candidate_id],
            perturbation_revision=env.applied[env.nominal.candidate_id],
            campaign=env.campaign,
            campaign_registration=env.registration,
            observable_id='response.shape_rms_db',
            receiver_entity_id='listener-main',
            required_capability='magnitude_response',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            radiation_scope='single',
            preregistered_at_utc='2030-01-01T00:30:00+00:00',
        )

    with pytest.raises(ValueError, match='observable is not preregistered'):
        env.validation_repository.preregister_case(
            robustness_spec_id=env.spec.robustness_spec_id,
            axis_id='speaker-x',
            direction='minus',
            nominal_measurement_plan_id=nominal_plan.plan_id,
            perturbation_measurement_plan_id=minus_plan.plan_id,
            o60_campaign_id=env.campaign.campaign_id,
            observable_id='response.not-preregistered',
            receiver_entity_id='listener-main',
            required_capability='magnitude_response',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            radiation_scope='single',
        )


def test_o90e_wrong_system_variant_lineage_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    env = _fixture(tmp_path)
    perturbation_revision_id = env.planned[
        env.minus.candidate_id
    ].applied_scene_revision_id

    def fake_lineage(revision_id):
        if revision_id != perturbation_revision_id:
            return None
        application = SimpleNamespace(
            application_id='application:foreign',
            application_sha256='1' * 64,
            variant_id='variant:foreign',
            variant_sha256='2' * 64,
        )
        variant = SimpleNamespace(
            variant_id='variant:foreign',
            variant_sha256='2' * 64,
        )
        return application, variant

    monkeypatch.setattr(
        env.validation_repository.system_variant_repository,
        'proposal_lineage_for_revision',
        fake_lineage,
    )

    with pytest.raises(ValueError, match='SystemVariant lineage mismatch'):
        env.validation_repository.preregister_case(
            robustness_spec_id=env.spec.robustness_spec_id,
            axis_id='speaker-x',
            direction='minus',
            nominal_measurement_plan_id=env.planned[
                env.nominal.candidate_id
            ].plan_id,
            perturbation_measurement_plan_id=env.planned[
                env.minus.candidate_id
            ].plan_id,
            o60_campaign_id=env.campaign.campaign_id,
            observable_id='response.shape_rms_db',
            receiver_entity_id='listener-main',
            required_capability='magnitude_response',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            radiation_scope='single',
        )


def test_o90e_phase_claim_is_blocked_by_magnitude_only_measurement(tmp_path) -> None:
    env = _fixture(tmp_path)
    phase_case = env.validation_repository.preregister_case(
        robustness_spec_id=env.spec.robustness_spec_id,
        axis_id='speaker-x',
        direction='plus',
        nominal_measurement_plan_id=env.planned[
            env.nominal.candidate_id
        ].plan_id,
        perturbation_measurement_plan_id=env.planned[
            env.plus.candidate_id
        ].plan_id,
        o60_campaign_id=env.campaign.campaign_id,
        observable_id='response.shape_rms_db',
        receiver_entity_id='listener-main',
        required_capability='phase_response',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
    )

    decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(env.minus_case.case_id, phase_case.case_id),
        decided_at_utc='2030-01-01T03:05:00+00:00',
    )

    assert decision.production_gate == 'closed'
    assert 'insufficient_measurement_capability' in decision.reasons
    plus_assessment = next(
        item for item in decision.assessments if item.direction == 'plus'
    )
    assert plus_assessment.perturbation_measurement is not None
    assert plus_assessment.perturbation_measurement.capability_decision == 'BLOCKED'


def test_o90e_missing_acquisition_or_capability_keeps_gate_closed(tmp_path) -> None:
    env = _fixture(tmp_path)
    measurement_id = env.measurement_ids[env.plus.candidate_id]
    _save_quality(
        env,
        measurement_id,
        report_id='quality:plus:no-acquisition',
        acquisition=False,
    )

    decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T03:10:00+00:00',
    )

    assert decision.production_gate == 'closed'
    assert 'insufficient_measurement_capability' in decision.reasons
    assert decision.support_state == 'partially_supported'


def test_o90e_forged_eligible_decision_without_signed_coverage_is_rejected(
    tmp_path,
) -> None:
    env = _fixture(tmp_path)
    forged = build_o90e_decision(
        spec=env.spec,
        validation_id=env.o60_record.validation_id,
        validation_sha256=env.o60_record.validation_sha256,
        campaign_id=env.campaign.campaign_id,
        campaign_sha256=env.campaign.campaign_sha256,
        assessments=(),
        axis_coverage=(),
        support_state='full',
        reasons=(),
        decided_at_utc='2030-01-01T03:15:00+00:00',
    )
    assert forged.production_gate == 'eligible'

    with pytest.raises(ValueError, match='cover every RobustnessSpec axis'):
        env.validation_repository.save_decision(forged)


def test_o90e_partial_coverage_and_nominal_validation_do_not_validate_robust_domain(
    tmp_path,
) -> None:
    env = _fixture(tmp_path)

    partial = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(env.minus_case.case_id,),
        decided_at_utc='2030-01-01T03:20:00+00:00',
    )
    assert partial.production_gate == 'closed'
    assert partial.support_state == 'partially_supported'
    assert 'missing_preregistration' in partial.reasons
    assert 'incomplete_required_perturbations' in partial.reasons

    nominal_only = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(),
        decided_at_utc='2030-01-01T03:21:00+00:00',
    )
    assert nominal_only.production_gate == 'closed'
    assert nominal_only.support_state == 'model_conditioned_only'
    assert 'missing_preregistration' in nominal_only.reasons
    assert 'incomplete_required_perturbations' in nominal_only.reasons


def test_o90e_missing_matching_sensitivity_and_wrong_band_fail_closed(tmp_path) -> None:
    env = _fixture(tmp_path)
    minus_only = tuple(
        check
        for check in env.o60_record.sensitivity_checks
        if env.minus.candidate_id
        in {check.candidate_a_id, check.candidate_b_id}
    )
    env.model_validation_repository.record = _build_o60_record(
        env,
        sensitivity_checks=minus_only,
    )
    decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.model_validation_repository.record.validation_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T03:30:00+00:00',
    )
    assert decision.production_gate == 'closed'
    assert 'perturbation_domain_outside_validated_applicability' in decision.reasons
    assert 'incomplete_required_perturbations' in decision.reasons

    env.model_validation_repository.record = _build_o60_record(
        env,
        band=(30.0, 160.0),
    )
    band_decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.model_validation_repository.record.validation_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T03:31:00+00:00',
    )
    assert band_decision.production_gate == 'closed'
    assert 'wrong_observable_or_band' in band_decision.reasons


def test_o90e_stale_model_and_missing_validation_do_not_convert_measured_to_validated(
    tmp_path,
) -> None:
    env = _fixture(tmp_path)
    env.model_validation_repository.record = _build_o60_record(
        env,
        model_version='stale-model-version',
    )
    stale = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.model_validation_repository.record.validation_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T03:40:00+00:00',
    )
    assert stale.production_gate == 'closed'
    assert 'stale_model_or_result' in stale.reasons

    missing_id = env.o60_record.validation_id
    env.model_validation_repository.record = None
    measured_only = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=missing_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T03:41:00+00:00',
    )
    assert measured_only.production_gate == 'closed'
    assert 'missing_underlying_model_validation' in measured_only.reasons


def test_o90e_missing_measurement_evidence_fails_closed(tmp_path) -> None:
    env = _fixture(tmp_path)
    missing_id = env.measurement_ids[env.plus.candidate_id]

    # Direct tamper simulates an authority that cannot be re-resolved after reopen.
    # SQLite foreign keys are intentionally not enabled on this raw audit connection.
    with sqlite3.connect(env.scene_repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_frequency_responses WHERE measurement_id=?',
            (missing_id,),
        )
        connection.execute(
            'DELETE FROM cad_measurements WHERE measurement_id=?',
            (missing_id,),
        )

    decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T03:45:00+00:00',
    )

    assert decision.production_gate == 'closed'
    assert 'missing_measurement_evidence' in decision.reasons


def test_o90e_explicit_quality_failure_is_not_interpolated_to_pass(tmp_path) -> None:
    env = _fixture(tmp_path)
    measurement_id = env.measurement_ids[env.plus.candidate_id]
    measurement = env.measurement_repository.get_measurement(measurement_id)
    dataset = env.measurement_repository.dataset_for_measurement(measurement_id)
    report = build_measurement_quality_report(
        measurement=measurement,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            clipping_detected=True,
            peak_dbfs=0.0,
            noise_floor_db_spl=30.0,
            signal_level_db_spl=70.0,
            snr_db=40.0,
            usable_frequency_band_hz=(20.0, 160.0),
            evidence_source='rew_metadata',
        ),
        profile=build_measurement_quality_profile(
            profile_version='o90e-quality-failure-1',
            minimum_snr_db=20.0,
            required_usable_band_hz=(20.0, 160.0),
        ),
        acquisition_context=CadAcquisitionContextBinding(
            acquisition_context_id='acq:quality-failure',
            acquisition_context_sha256=sha256(b'acq:quality-failure').hexdigest(),
            source_kind='native',
        ),
        report_id='quality:plus:explicit-failure',
        created_at_utc='2030-01-01T02:30:00+00:00',
    )
    assert report.clipping.status == 'FAIL'
    env.quality_repository.save_report(report)

    decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T03:46:00+00:00',
    )

    assert decision.production_gate == 'closed'
    assert 'quality_failure' in decision.reasons


def test_o90e_posthoc_backdated_prospective_case_is_rejected(tmp_path) -> None:
    env = _fixture(tmp_path)
    forged = build_o90e_validation_case(
        spec=env.spec,
        axis_id='speaker-x',
        direction='minus',
        nominal_plan=env.planned[env.nominal.candidate_id],
        perturbation_plan=env.planned[env.minus.candidate_id],
        nominal_revision=env.applied[env.nominal.candidate_id],
        perturbation_revision=env.applied[env.minus.candidate_id],
        campaign=env.campaign,
        campaign_registration=env.registration,
        observable_id='response.shape_rms_db',
        receiver_entity_id='listener-main',
        required_capability='magnitude_response',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        preregistered_at_utc='2030-01-01T00:30:00+00:00',
    )
    assert forged.preregistration_status == 'prospective'

    with pytest.raises(
        ValueError,
        match='must be persisted before measurement completion',
    ):
        env.validation_repository.save_case(forged)


def test_o90e_retrospective_case_never_masquerades_as_preregistered(tmp_path) -> None:
    env = _fixture(tmp_path)
    retrospective = env.validation_repository.preregister_case(
        robustness_spec_id=env.spec.robustness_spec_id,
        axis_id='speaker-x',
        direction='minus',
        nominal_measurement_plan_id=env.planned[env.nominal.candidate_id].plan_id,
        perturbation_measurement_plan_id=env.planned[env.minus.candidate_id].plan_id,
        o60_campaign_id=env.campaign.campaign_id,
        observable_id='response.shape_rms_db',
        receiver_entity_id='listener-main',
        required_capability='magnitude_response',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
    )
    assert retrospective.preregistration_status == 'retrospective'

    decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(retrospective.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T03:50:00+00:00',
    )
    assert decision.production_gate == 'closed'
    assert 'retrospective_evidence' in decision.reasons


def test_o90e_synthetic_o60_evidence_never_opens_production_gate(tmp_path) -> None:
    env = _fixture(tmp_path)
    synthetic = _build_o60_record(
        env,
        evidence_scope='synthetic_fixture',
    )
    env.model_validation_repository.record = synthetic

    decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=synthetic.validation_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T04:00:00+00:00',
    )
    assert decision.production_gate == 'closed'
    assert decision.support_state == 'unsupported'
    assert 'synthetic_evidence' in decision.reasons


def test_o90e_superseded_decision_fails_closed_and_new_evidence_adds_new_decision(
    tmp_path,
) -> None:
    env = _fixture(tmp_path)
    first = _decision(env, decided_at='2030-01-01T04:10:00+00:00')

    measurement_id = env.measurement_ids[env.plus.candidate_id]
    newer_report = _save_quality(
        env,
        measurement_id,
        report_id='quality:plus:new-evidence',
        acquisition=True,
    )
    second = _decision(env, decided_at='2030-01-01T04:20:00+00:00')

    assert first.decision_id != second.decision_id
    assert first.assessments[-1].perturbation_measurement.quality_report_id != (
        newer_report.report_id
    )
    assert second.assessments[-1].perturbation_measurement.quality_report_id == (
        newer_report.report_id
    )

    # The superseded row is never rewritten, but it no longer reproduces the
    # canonical evaluation of the current authorities, so authoritative reads
    # fail closed instead of serving a stale production-gate decision.
    with pytest.raises(ValueError, match='canonical evaluation'):
        env.validation_repository.get_decision(first.decision_id)
    with pytest.raises(ValueError, match='canonical evaluation'):
        env.validation_repository.list_decisions(env.spec.robustness_spec_id)
    assert env.validation_repository.get_decision(second.decision_id) == second


def _phase_case(env):
    """Preregister a plus case whose magnitude-only evidence blocks the claim."""
    return env.validation_repository.preregister_case(
        robustness_spec_id=env.spec.robustness_spec_id,
        axis_id='speaker-x',
        direction='plus',
        nominal_measurement_plan_id=env.planned[env.nominal.candidate_id].plan_id,
        perturbation_measurement_plan_id=env.planned[env.plus.candidate_id].plan_id,
        o60_campaign_id=env.campaign.campaign_id,
        observable_id='response.shape_rms_db',
        receiver_entity_id='listener-main',
        required_capability='phase_response',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
    )


def _rebuild_decision(env, canonical, *, assessments=None, axis_coverage=None):
    """Recompute a self-hash-valid decision over mutated caller fields."""
    return build_o90e_decision(
        spec=env.spec,
        validation_id=canonical.o60_validation_id,
        validation_sha256=canonical.o60_validation_sha256,
        campaign_id=canonical.o60_campaign_id,
        campaign_sha256=canonical.o60_campaign_sha256,
        assessments=(
            canonical.assessments if assessments is None else assessments
        ),
        axis_coverage=(
            canonical.axis_coverage if axis_coverage is None else axis_coverage
        ),
        support_state=canonical.support_state,
        reasons=canonical.reasons,
        decided_at_utc=canonical.decided_at_utc,
    )


def test_o90e_fabricated_eligible_decision_over_real_evidence_is_rejected(
    tmp_path,
) -> None:
    env = _fixture(tmp_path)
    phase_case = _phase_case(env)
    canonical = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(env.minus_case.case_id, phase_case.case_id),
        decided_at_utc='2030-01-01T04:40:00+00:00',
    )
    assert canonical.production_gate == 'closed'
    blocked = next(
        item for item in canonical.assessments if item.direction == 'plus'
    )
    assert blocked.status == 'unsupported'

    # Every referenced authority stays real; only the caller-supplied
    # interpretation is fabricated into an internally self-hash-valid payload.
    forged = build_o90e_decision(
        spec=env.spec,
        validation_id=canonical.o60_validation_id,
        validation_sha256=canonical.o60_validation_sha256,
        campaign_id=canonical.o60_campaign_id,
        campaign_sha256=canonical.o60_campaign_sha256,
        assessments=tuple(
            item.model_copy(update={'status': 'supported', 'reasons': ()})
            for item in canonical.assessments
        ),
        axis_coverage=tuple(
            item.model_copy(update={'state': 'full'})
            for item in canonical.axis_coverage
        ),
        support_state='full',
        reasons=('eligible',),
        decided_at_utc=canonical.decided_at_utc,
    )
    assert forged.production_gate == 'eligible'

    with pytest.raises(ValueError, match='canonical evaluation'):
        env.validation_repository.save_decision(forged)

    # The same fabricated payload written straight into the store is detected
    # by the read-side canonical replay as well.
    with sqlite3.connect(env.scene_repository.path) as connection:
        connection.execute(
            '''
            INSERT INTO cad_robustness_validation_decisions(
                decision_id, robustness_spec_id, candidate_id,
                production_gate, support_state, decision_sha256,
                decided_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                forged.decision_id,
                forged.robustness_spec_id,
                forged.candidate_id,
                forged.production_gate,
                forged.support_state,
                forged.decision_sha256,
                forged.decided_at_utc,
                forged.model_dump_json(),
            ),
        )
    with pytest.raises(ValueError, match='canonical evaluation'):
        env.validation_repository.get_decision(forged.decision_id)
    with pytest.raises(ValueError, match='canonical evaluation'):
        env.validation_repository.list_decisions(env.spec.robustness_spec_id)


def test_o90e_fabricated_assessment_fields_are_rejected(tmp_path) -> None:
    env = _fixture(tmp_path)
    phase_case = _phase_case(env)
    canonical = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(env.minus_case.case_id, phase_case.case_id),
        decided_at_utc='2030-01-01T04:50:00+00:00',
    )
    assert canonical.production_gate == 'closed'
    blocked = next(
        item for item in canonical.assessments if item.direction == 'plus'
    )
    assert blocked.perturbation_measurement.capability_decision == 'BLOCKED'

    # Embedded capability decision flipped to ALLOWED while the exact
    # report/gate still says BLOCKED.
    forged_ref = blocked.perturbation_measurement.model_copy(
        update={'capability_decision': 'ALLOWED'}
    )
    forged_capability = _rebuild_decision(
        env,
        canonical,
        assessments=tuple(
            item.model_copy(update={'perturbation_measurement': forged_ref})
            if item.direction == 'plus'
            else item
            for item in canonical.assessments
        ),
    )
    with pytest.raises(ValueError, match='canonical evaluation'):
        env.validation_repository.save_decision(forged_capability)

    # Assessment axis/direction/delta changed while retaining the real case id.
    forged_binding = _rebuild_decision(
        env,
        canonical,
        assessments=tuple(
            item.model_copy(
                update={
                    'axis_id': 'speaker-y',
                    'direction': 'plus',
                    'target_delta': 0.2,
                }
            )
            if item.direction == 'minus'
            else item
            for item in canonical.assessments
        ),
    )
    with pytest.raises(ValueError, match='canonical evaluation'):
        env.validation_repository.save_decision(forged_binding)

    # Sensitivity evidence hash and derived reasons replaced.
    forged_evidence = _rebuild_decision(
        env,
        canonical,
        assessments=tuple(
            item.model_copy(
                update={
                    'sensitivity_evidence_sha256': '0' * 64,
                    'reasons': (),
                }
            )
            if item.direction == 'minus'
            else item
            for item in canonical.assessments
        ),
    )
    with pytest.raises(ValueError, match='canonical evaluation'):
        env.validation_repository.save_decision(forged_evidence)


def test_o90e_unknown_authority_version_fails_closed(tmp_path) -> None:
    env = _fixture(tmp_path)
    decision = env.validation_repository.evaluate_decision(
        robustness_spec_id=env.spec.robustness_spec_id,
        o60_validation_id=env.o60_record.validation_id,
        case_ids=(env.minus_case.case_id, env.plus_case.case_id),
        decided_at_utc='2030-01-01T05:00:00+00:00',
    )

    foreign = decision.model_copy(
        update={'authority_version': 'o90e-owned-room-validation-999'}
    )
    with pytest.raises(ValueError):
        env.validation_repository.save_decision(foreign)


def test_o90e_raw_measurement_asset_tamper_rejected_on_reopen(tmp_path) -> None:
    env = _fixture(tmp_path)
    decision = _decision(env, decided_at='2030-01-01T04:25:00+00:00')

    measurement_id = env.measurement_ids[env.plus.candidate_id]
    dataset = env.measurement_repository.dataset_for_measurement(measurement_id)
    assert dataset is not None
    asset_path = env.measurement_repository.assets_dir / dataset.source_sha256
    asset_path.write_bytes(b'tampered-o90e-raw-asset')

    # The tampered asset fails the dataset's content-address verification
    # before the decision layer even replays its own bindings.
    with pytest.raises(ValueError, match='content does not match its content address'):
        env.validation_repository.get_decision(decision.decision_id)


def test_o90e_tamper_rejected_on_reopen(tmp_path) -> None:
    env = _fixture(tmp_path)
    decision = _decision(env, decided_at='2030-01-01T04:30:00+00:00')

    with sqlite3.connect(env.scene_repository.path) as connection:
        row = connection.execute(
            'SELECT payload_json FROM cad_robustness_validation_decisions WHERE decision_id=?',
            (decision.decision_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload['candidate_id'] = 'tampered-candidate'
        connection.execute(
            'UPDATE cad_robustness_validation_decisions SET payload_json=? WHERE decision_id=?',
            (
                json.dumps(payload, sort_keys=True, separators=(',', ':')),
                decision.decision_id,
            ),
        )

    with pytest.raises(ValueError, match='semantic hash mismatch'):
        env.validation_repository.get_decision(decision.decision_id)


def _minus_case_kwargs(env) -> dict:
    return dict(
        robustness_spec_id=env.spec.robustness_spec_id,
        axis_id='speaker-x',
        direction='minus',
        nominal_measurement_plan_id=env.planned[env.nominal.candidate_id].plan_id,
        perturbation_measurement_plan_id=env.planned[env.minus.candidate_id].plan_id,
        o60_campaign_id=env.campaign.campaign_id,
        observable_id='response.shape_rms_db',
        receiver_entity_id='listener-main',
        required_capability='magnitude_response',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
    )


def _complete_plan(env, candidate, measurement_id: str) -> None:
    _capture_measurement(
        env,
        env.applied[candidate.candidate_id],
        measurement_id,
    )
    env.measurement_repository.save_measurement_plan(
        complete_measurement_plan(
            env.planned[candidate.candidate_id],
            env.measurement_repository,
            (measurement_id,),
        )
    )


def _plan_head(env, plan_id: str):
    return next(
        plan
        for plan in env.measurement_repository.latest_measurement_plans(
            env.spec.search_spec_id
        )
        if plan.plan_id == plan_id
    )


def test_o90e_registration_fails_closed_when_completion_commits_mid_registration(
    tmp_path, monkeypatch
) -> None:
    env = _fixture(tmp_path, measured=False)
    repository = env.validation_repository
    real_connect = repository._connect
    injected = []

    def raced_connect():
        if not injected:
            # The completion commits between the registration's planned-state
            # resolution and the transaction that inserts the case.
            injected.append(True)
            _complete_plan(env, env.nominal, 'measurement:nominal-raced')
        return real_connect()

    monkeypatch.setattr(repository, '_connect', raced_connect)

    with pytest.raises(ValueError, match='before measurement completion'):
        repository.preregister_case(**_minus_case_kwargs(env))

    # The measured head stays valid; no additional case row was committed.
    assert (
        _plan_head(env, env.planned[env.nominal.candidate_id].plan_id).status
        == 'measured'
    )
    assert {
        case.case_id
        for case in repository.list_cases(env.spec.robustness_spec_id)
    } == {env.minus_case.case_id, env.plus_case.case_id}


def test_o90e_prospective_case_rejects_superseded_planned_head(tmp_path) -> None:
    env = _fixture(tmp_path, measured=False)
    nominal_plan = env.planned[env.nominal.candidate_id]
    case = build_o90e_validation_case(
        spec=env.spec,
        axis_id='speaker-x',
        direction='minus',
        nominal_plan=nominal_plan,
        perturbation_plan=env.planned[env.minus.candidate_id],
        nominal_revision=env.applied[env.nominal.candidate_id],
        perturbation_revision=env.applied[env.minus.candidate_id],
        campaign=env.campaign,
        campaign_registration=env.registration,
        observable_id='response.shape_rms_db',
        receiver_entity_id='listener-main',
        required_capability='magnitude_response',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        preregistered_at_utc='2030-01-01T00:30:00+00:00',
    )
    assert case.preregistration_status == 'prospective'

    # The head advances to a NEW planned version after the case was built:
    # the older row stays 'planned' in history but is no longer the head.
    binding = SimpleNamespace(
        consumer_kind='O50_MEASUREMENT_PLAN',
        consumer_id=nominal_plan.plan_id,
        required_observables=('frequency_response_magnitude',),
        binding_id='r170a-provider-binding:' + ('a' * 64),
        semantic_sha256='b' * 64,
    )
    env.measurement_repository.save_measurement_plan(
        bind_measurement_plan_prediction(nominal_plan, binding)
    )

    with pytest.raises(ValueError, match='no longer the current planned head'):
        env.validation_repository.save_case(case)

    # A fresh registration still resolves and binds the current planned head.
    rebound = env.validation_repository.preregister_case(**_minus_case_kwargs(env))
    assert rebound.preregistration_status == 'prospective'
    assert rebound.nominal_preregistered_plan_sha256 != (
        case.nominal_preregistered_plan_sha256
    )


def test_o90e_preregistration_fails_closed_on_qualifying_evidence_before_completion(
    tmp_path,
) -> None:
    env = _fixture(tmp_path, measured=False)
    # Measured evidence already exists under the nominal plan's applied
    # revision even though the plan head is still 'planned'.
    _capture_measurement(
        env,
        env.applied[env.nominal.candidate_id],
        'measurement:early-o90e',
    )

    with pytest.raises(ValueError, match='qualifying measurement evidence'):
        env.validation_repository.preregister_case(**_minus_case_kwargs(env))

    assert (
        _plan_head(env, env.planned[env.nominal.candidate_id].plan_id).status
        == 'planned'
    )
    assert {
        case.case_id
        for case in env.validation_repository.list_cases(env.spec.robustness_spec_id)
    } == {env.minus_case.case_id, env.plus_case.case_id}


def test_o90e_case_commits_before_racing_measurement_completion(
    tmp_path, monkeypatch
) -> None:
    env = _fixture(tmp_path, measured=False)
    repository = env.validation_repository
    barrier = threading.Barrier(2)
    real_utc_now = validation_repository_module._utc_now
    entered = []

    def gated_utc_now():
        if not entered:
            # The completer reaches its own write while this registration
            # holds the BEGIN IMMEDIATE lock, so it can only commit after us.
            entered.append(True)
            barrier.wait(timeout=30)
        return real_utc_now()

    monkeypatch.setattr(validation_repository_module, '_utc_now', gated_utc_now)

    results: list[object] = []

    def completer() -> None:
        barrier.wait(timeout=30)
        try:
            _complete_plan(env, env.nominal, 'measurement:nominal-after-case')
            results.append('measured')
        except Exception as exc:  # noqa: BLE001 - collect for assertion
            results.append(exc)

    thread = threading.Thread(target=completer)
    thread.start()
    case = repository.preregister_case(**_minus_case_kwargs(env))
    thread.join(timeout=60)
    assert not thread.is_alive()

    assert case.preregistration_status == 'prospective'
    assert repository.get_case(case.case_id) == case
    # Registration committed first; the racing completion remains valid and
    # lands as the measured head afterwards.
    assert results == ['measured']
    head = _plan_head(env, env.planned[env.nominal.candidate_id].plan_id)
    assert head.status == 'measured'
    assert head.measurement_ids == ('measurement:nominal-after-case',)


def test_o90e_concurrent_identical_preregistrations_commit_once(
    tmp_path, monkeypatch
) -> None:
    env = _fixture(tmp_path, measured=False)
    # Both threads build the identical case: only one commit can win.
    monkeypatch.setattr(
        validation_repository_module,
        '_utc_now',
        lambda: '2030-01-01T00:40:00+00:00',
    )
    barrier = threading.Barrier(2)
    results: list[object] = []

    def worker() -> None:
        barrier.wait(timeout=30)
        try:
            results.append(
                env.validation_repository.preregister_case(
                    **_minus_case_kwargs(env)
                )
            )
        except Exception as exc:  # noqa: BLE001 - collect for assertion
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    committed = [item for item in results if not isinstance(item, Exception)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(committed) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert 'already exists' in str(failures[0])
    assert env.validation_repository.get_case(committed[0].case_id) == committed[0]
