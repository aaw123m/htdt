from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace

import pytest

from htdt.cad_applicability import (
    AUTOMATED_EVALUATOR_BY_CODE,
    evaluate_applicability,
    resolve_applicability_context,
)
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_model_validation import build_full_model_validation
from htdt.cad_model_validation_repository import (
    CadModelValidationIntegrityError,
    CadModelValidationRepository,
)
from htdt.cad_objective_authority import ResolvedObjectiveInput
from htdt.cad_objective_models import (
    CadObjectiveInputRef,
    canonical_objective_sha256,
)
from htdt.cad_objectives import build_objective_evaluation
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_results import (
    canonical_roomsim_result_json,
    roomsim_attempt_frequency_response,
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
from htdt.cad_validation_metrics import (
    CadObjectiveValidationSample,
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


def _fr(offset: float, tilt: float = 0.0) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(
            80.0 + offset,
            81.0 + tilt + offset,
            79.0 + offset,
            80.0 + 0.5 * tilt + offset,
        ),
    )


def _response_json(offset: float, tilt: float = 0.0) -> str:
    response = _fr(offset, tilt)
    return json.dumps({
        'frequency_hz': list(response.frequency_hz),
        'magnitude': list(response.level_db),
    })


def _tilted_fr(tilt: float) -> FrequencyResponse:
    """Response whose shape (not just level offset) varies with ``tilt``."""

    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(80.0, 81.0 + tilt, 79.0, 80.0 + 0.5 * tilt),
    )


def _tilted_response_json(tilt: float) -> str:
    response = _tilted_fr(tilt)
    return canonical_roomsim_result_json({
        'source_name': None,
        'mic_position': 'Main',
        'message': 'fixture',
        'unit': 'SPL',
        'smoothing': 'None',
        'start_frequency_hz': 20.0,
        'points_per_octave': 96.0,
        'frequency_step_hz': None,
        'frequency_hz': list(response.frequency_hz),
        'magnitude': list(response.level_db),
        'phase_deg': None,
    })


class _RoomSimEvidence:
    def __init__(self, path, spec, candidate_set_sha256, candidates):
        self.path = path
        self.spec = spec
        self.candidate_set_sha256 = candidate_set_sha256
        self.candidates = {candidate.candidate_id: candidate for candidate in candidates}

    def get_attempt(self, attempt_id):
        if not attempt_id.startswith('pred:'):
            return None
        candidate_id = attempt_id.removeprefix('pred:')
        if candidate_id not in self.candidates:
            return None
        index = list(self.candidates).index(candidate_id)
        response_json = _tilted_response_json(float(index) * 2.0)
        return SimpleNamespace(
            attempt_id=attempt_id,
            batch_run_id='batch',
            candidate_id=candidate_id,
            status='completed',
            result=SimpleNamespace(
                model_version='fixture-1',
                response_json=response_json,
                response_sha256=sha256(response_json.encode()).hexdigest(),
            ),
        )

    def get_batch_spec(self, batch_run_id):
        if batch_run_id != 'batch':
            return None
        return SimpleNamespace(
            batch_run_id='batch',
            document_id=self.spec.document_id,
            scene_revision_id=self.spec.scene_revision_id,
            scene_content_hash=self.spec.scene_content_hash,
            search_spec_id=self.spec.search_spec_id,
            search_spec_sha256=self.spec.search_spec_sha256,
            candidate_set_sha256=self.candidate_set_sha256,
            model_id='rew-roomsim',
            binding_json=json.dumps({'geometry_mode': 'exact_rectangular'}),
            batch_spec_sha256='cd' * 32,
        )


class _PreMeasurementEvidence:
    def __init__(self, path):
        self.path = path

    def latest_measurement_plans(self, search_spec_id):
        return ()


def _measurement_fixture_resolver(measurement_repository):
    """Resolve cad_measurement refs against the in-memory fixture evidence."""

    def resolve(context, ref):
        record = measurement_repository.get_measurement(ref.source_id)
        dataset = measurement_repository.dataset_for_measurement(ref.source_id)
        if record is None or dataset is None:
            raise ValueError(
                f'measured evidence does not exist: {ref.source_id}'
            )
        if record.document_id != context.evaluation.document_id:
            raise ValueError('measured evidence belongs to a different document')
        response = FrequencyResponse(
            frequency_hz=tuple(
                float(value) for value in dataset.frequency_hz
            ),
            level_db=tuple(float(value) for value in dataset.level_db),
        )
        return ResolvedObjectiveInput(
            ref=ref,
            source_sha256=canonical_objective_sha256(
                {
                    'frequency_hz': list(response.frequency_hz),
                    'level_db': list(response.level_db),
                }
            ),
            response=response,
        )

    return resolve


class _MeasurementEvidence:
    def __init__(self, path, document_id, candidate_set_sha256, candidate_ids, campaign_id):
        self.path = path
        self.document_id = document_id
        self.candidate_set_sha256 = candidate_set_sha256
        self.campaign_id = campaign_id
        self.records = {}
        self.datasets = {}
        self.plans = []
        # Simulates file-backed raw assets that can disappear or corrupt
        # after the O60 record was saved.
        self.missing_assets = set()
        offsets = {candidate_ids[0]: 0.2, candidate_ids[1]: 2.2, candidate_ids[2]: 4.2}
        for candidate_id in candidate_ids:
            measurement_id = f'meas:{candidate_id}'
            revision_id = f'applied:{candidate_id}'
            self._add(measurement_id, revision_id, offsets[candidate_id], 'owned_room')
            ids = [measurement_id]
            if candidate_id == candidate_ids[0]:
                self._add('repeat:a:1', revision_id, offsets[candidate_id], 'owned_room')
                self._add('repeat:a:2', revision_id, offsets[candidate_id] + 0.1, 'owned_room')
                ids.extend(('repeat:a:1', 'repeat:a:2'))
            self.plans.append(SimpleNamespace(
                plan_id=f'plan:{candidate_id}',
                status='measured',
                candidate_id=candidate_id,
                candidate_set_sha256=candidate_set_sha256,
                measurement_ids=tuple(ids),
                applied_scene_revision_id=revision_id,
                plan_sha256=sha256(
                    f'plan:{candidate_id}'.encode()
                ).hexdigest(),
            ))

    def _add(self, measurement_id, revision_id, offset, validation_scope):
        self.records[measurement_id] = SimpleNamespace(
            measurement_id=measurement_id,
            evidence_type='measured',
            document_id=self.document_id,
            scene_revision_id=revision_id,
            routing_evidence='verified',
            provenance_json=json.dumps({
                'validation_scope': validation_scope,
                'validation_campaign_id': self.campaign_id,
            }, separators=(',', ':')),
            captured_at='2030-01-01T00:00:00+00:00',
        )
        response = _tilted_fr(offset)
        self.datasets[measurement_id] = SimpleNamespace(
            frequency_hz=response.frequency_hz,
            level_db=response.level_db,
        )

    def get_measurement(self, measurement_id):
        return self.records.get(measurement_id)

    def dataset_for_measurement(self, measurement_id):
        return self.datasets.get(measurement_id)

    def verify_measurement_asset_authority(self, measurement_id):
        if measurement_id in self.missing_assets or measurement_id not in self.datasets:
            raise ValueError(
                'measurement raw asset is unavailable for dataset verification'
            )

    def list_measurement_plans(self, search_spec_id):
        return tuple(self.plans)


def _fixture(tmp_path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='o60-full',
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
        target_response=CadValidationTargetResponse(
            frequency_hz=(20.0, 40.0, 80.0, 160.0),
            level_db=(0.0, 0.0, 0.0, 0.0),
        ),
        reference_band_hz=(20.0, 160.0),
        sensitivity=(
            CadValidationCampaignSensitivity(
                objective_id='response.shape_rms_db',
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                max_observed_sensitivity_per_m=6.0,
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
    campaign_repository = CadValidationCampaignRepository(
        search_repo,
        _PreMeasurementEvidence(scene_repo.path),
    )
    registration = campaign_repository.save(campaign)

    measurement_repo = _MeasurementEvidence(
        scene_repo.path,
        document.document_id,
        page.candidate_set_sha256,
        candidate_ids,
        campaign.campaign_id,
    )
    roomsim_repo = _RoomSimEvidence(
        scene_repo.path,
        spec,
        page.candidate_set_sha256,
        candidates,
    )
    objective_repo = CadObjectiveRepository(
        scene_repo,
        search_repo,
        roomsim_repository=roomsim_repo,
        input_resolvers={
            'cad_measurement': _measurement_fixture_resolver(
                measurement_repo
            ),
        },
    )

    campaign_spec = json.loads(campaign.objective_evaluation_spec_json)
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

    def _campaign_vector(candidate_id: str, response: FrequencyResponse):
        full = target_response_objectives(
            candidate_id,
            response,
            target_response,
            response_spec,
            prefix='response',
        )
        return ObjectiveVector(
            candidate_id=candidate_id,
            metrics=tuple(
                full.metric(objective_id)
                for objective_id in campaign_spec['objectives']
            ),
        )

    objective_samples = []
    for index, candidate in enumerate(candidates, start=1):
        split = 'calibration' if index == 3 else 'holdout'
        for evidence_class, source_id in (
            ('predicted', f'pred:{candidate.candidate_id}'),
            ('measured', f'meas:{candidate.candidate_id}'),
        ):
            if evidence_class == 'predicted':
                response = roomsim_attempt_frequency_response(
                    roomsim_repo.get_attempt(source_id)
                )
            else:
                dataset = measurement_repo.dataset_for_measurement(source_id)
                response = FrequencyResponse(
                    frequency_hz=tuple(
                        float(value) for value in dataset.frequency_hz
                    ),
                    level_db=tuple(
                        float(value) for value in dataset.level_db
                    ),
                )
            vector = _campaign_vector(candidate.candidate_id, response)
            evaluation = build_objective_evaluation(
                revision,
                spec,
                candidate.candidate_id,
                vector,
                evaluation_spec=campaign_spec,
                input_refs=(
                    CadObjectiveInputRef(
                        evidence_class=evidence_class,
                        source_kind='cad_measurement' if evidence_class == 'measured' else 'cad_roomsim_attempt',
                        source_id=source_id,
                    ),
                ),
            )
            objective_repo.save_evaluation(evaluation)
            metric_value = float(vector.metric('response.shape_rms_db').value)
            if evidence_class == 'predicted':
                predicted_id = evaluation.evaluation_id
                predicted_value = metric_value
            else:
                measured_id = evaluation.evaluation_id
                objective_samples.append(CadObjectiveValidationSample(
                    candidate_id=candidate.candidate_id,
                    split=split,
                    objective_id='response.shape_rms_db',
                    unit='dB',
                    predicted_evaluation_id=predicted_id,
                    measured_evaluation_id=measured_id,
                    predicted_value=predicted_value,
                    measured_value=metric_value,
                ))

    left_x = candidates[0].positions['fl']['x_m']
    right_x = candidates[1].positions['fl']['x_m']
    sample_map = {
        sample.candidate_id: sample for sample in objective_samples
    }
    sensitivity = build_sensitivity_check(
        objective_id='response.shape_rms_db',
        unit='dB',
        candidate_a_id=candidate_ids[0],
        candidate_b_id=candidate_ids[1],
        placement_delta_m=abs(right_x - left_x),
        predicted_a=sample_map[candidate_ids[0]].predicted_value,
        predicted_b=sample_map[candidate_ids[1]].predicted_value,
        measured_a=sample_map[candidate_ids[0]].measured_value,
        measured_b=sample_map[candidate_ids[1]].measured_value,
        max_observed_sensitivity_per_m=6.0,
        max_model_error_per_m=1.0,
    )
    repeatability = build_repeatability_check(
        scene_revision_id=f'applied:{candidate_ids[0]}',
        measurements=(
            ('repeat:a:1', _tilted_fr(0.2)),
            ('repeat:a:2', _tilted_fr(0.3)),
        ),
        low_hz=20.0,
        high_hz=160.0,
    )
    separation = build_candidate_separation_check(
        candidate_a_id=candidate_ids[0],
        candidate_b_id=candidate_ids[1],
        measurement_a_id=f'meas:{candidate_ids[0]}',
        measurement_b_id=f'meas:{candidate_ids[1]}',
        response_a=_tilted_fr(0.2),
        response_b=_tilted_fr(2.2),
        low_hz=20.0,
        high_hz=160.0,
        repeatability_floor_db=repeatability.rms_floor_db,
        min_repeatability_multiple=2.0,
    )

    applicability_context = resolve_applicability_context(
        document_id=document.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='fixture-1',
        evidence_scope='owned_room',
        campaign_id=campaign.campaign_id,
        requested_band_hz=(20.0, 160.0),
        pair_attempt_ids=tuple(
            f'pred:{candidate_id}' for candidate_id in candidate_ids
        ),
        pair_measurement_ids=tuple(
            f'meas:{candidate_id}' for candidate_id in candidate_ids
        ),
        scoped_measurement_ids=(
            tuple(f'meas:{candidate_id}' for candidate_id in candidate_ids)
            + ('repeat:a:1', 'repeat:a:2')
        ),
        search_repository=search_repo,
        roomsim_repository=roomsim_repo,
        measurement_repository=measurement_repo,
    )
    applicability_checks = tuple(
        evaluate_applicability(
            applicability_context,
            code=code,
            evaluator_id=AUTOMATED_EVALUATOR_BY_CODE[code],
            detail=detail,
        )
        for code, detail in (
            ('geometry', 'fixture supported'),
            ('band', '20-160 Hz supported'),
            ('routing', 'routing verified'),
        )
    )
    assert all(check.passed for check in applicability_checks)

    record = build_full_model_validation(
        document_id=document.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        campaign_id=campaign.campaign_id,
        campaign_sha256=campaign.campaign_sha256,
        campaign_registration_id=registration.registration_id,
        campaign_registration_sha256=registration.registration_sha256,
        model_id='rew-roomsim',
        model_version='fixture-1',
        response_samples=tuple(
            (
                candidate_id,
                'calibration' if index == 2 else 'holdout',
                f'pred:{candidate_id}',
                f'meas:{candidate_id}',
                _tilted_fr(float(index) * 2.0),
                _tilted_fr(float(index) * 2.0 + 0.2),
            )
            for index, candidate_id in enumerate(candidate_ids)
        ),
        objective_samples=tuple(objective_samples),
        sensitivity_checks=(sensitivity,),
        repeatability_checks=(repeatability,),
        separation_checks=(separation,),
        applicability_checks=applicability_checks,
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=1.0,
        evidence_scope='owned_room',
        trend_min_agreement_ratio=0.75,
    )
    repository = CadModelValidationRepository(
        search_repo,
        roomsim_repo,
        measurement_repo,
        objective_repo,
    )
    return record, repository, measurement_repo


def _rehashed(record, **updates):
    """Return a copy of ``record`` with a recomputed identity hash."""
    tampered = record.model_copy(update=updates)
    digest = sha256(
        json.dumps(
            tampered.identity_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()
    return tampered.model_copy(update={'validation_sha256': digest})


def test_full_validation_repository_recomputes_cross_evidence_authority(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)

    repository.save(record)

    assert record.recommendation_gate == 'eligible'
    assert repository.get(record.validation_id) == record
    assert repository.latest_eligible_for_search_spec(record.search_spec_id) == record


def test_full_validation_repository_rejects_tampered_holdout_rms(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    assert record.holdout_rms_db is not None
    tampered = _rehashed(
        record,
        holdout_rms_db=record.max_holdout_rms_db * 0.5,
        residual_gate='pass',
    )

    with pytest.raises(ValueError, match='residuals do not match'):
        repository.save(tampered)


def test_full_validation_repository_rejects_tampered_pair_shape(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    pair = record.pairs[0].model_copy(update={'shape_rms_db': 0.01})
    tampered = _rehashed(record, pairs=(pair,) + record.pairs[1:])

    with pytest.raises(ValueError, match='residuals do not match'):
        repository.save(tampered)


def test_full_validation_read_fails_closed_on_tampered_payload(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    repository.save(record)
    assert repository.latest_eligible_for_search_spec(record.search_spec_id) == record

    tampered = _rehashed(record, calibration_rms_db=0.01)
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'UPDATE cad_model_validations SET payload_json=? WHERE validation_id=?',
            (tampered.model_dump_json(), record.validation_id),
        )

    with pytest.raises(ValueError, match='residuals do not match'):
        repository.get(record.validation_id)
    with pytest.raises(ValueError, match='residuals do not match'):
        repository.latest_eligible_for_search_spec(record.search_spec_id)
    with pytest.raises(ValueError, match='residuals do not match'):
        repository.list_for_search_spec(record.search_spec_id)


def test_owned_room_validation_rejects_unclassified_or_synthetic_measurement(tmp_path):
    record, repository, measurement_repo = _fixture(tmp_path)
    target = next(iter(measurement_repo.records.values()))
    target.provenance_json = '{"validation_scope":"synthetic_fixture"}'

    with pytest.raises(ValueError, match='validation_scope=owned_room'):
        repository.save(record)


def test_owned_room_validation_requires_persisted_campaign_registration(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_validation_campaign_registrations WHERE campaign_id=?',
            (record.campaign_id,),
        )

    with pytest.raises(ValueError, match='campaign registration is missing'):
        repository.save(record)


def test_owned_room_validation_rejects_foreign_registration_authority(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    foreign = _rehashed(
        record,
        campaign_registration_id=(
            'o60-validation-campaign-registration:' + '9' * 64
        ),
        campaign_registration_sha256='9' * 64,
    )

    with pytest.raises(ValueError, match='campaign registration mismatch'):
        repository.save(foreign)


def test_eligible_read_fails_closed_when_raw_asset_disappears(tmp_path):
    """A missing/corrupt file-backed raw asset makes the eligible read fail closed."""
    record, repository, measurement_repo = _fixture(tmp_path)
    repository.save(record)
    assert repository.latest_eligible_for_search_spec(record.search_spec_id) == record

    measurement_repo.missing_assets.add(f'meas:{record.pairs[0].candidate_id}')

    with pytest.raises(
        CadModelValidationIntegrityError, match='raw asset'
    ) as exc_info:
        repository.latest_eligible_for_search_spec(record.search_spec_id)
    assert exc_info.value.validation_id == record.validation_id
    with pytest.raises(CadModelValidationIntegrityError, match='raw asset'):
        repository.get(record.validation_id)
    with pytest.raises(CadModelValidationIntegrityError, match='raw asset'):
        repository.list_for_search_spec(record.search_spec_id)


def test_eligible_read_fails_closed_when_roomsim_attempt_disappears(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    repository.save(record)

    repository.roomsim_repository.candidates.clear()

    with pytest.raises(
        CadModelValidationIntegrityError, match='completed Room Simulator attempt'
    ):
        repository.latest_eligible_for_search_spec(record.search_spec_id)
    with pytest.raises(
        CadModelValidationIntegrityError, match='completed Room Simulator attempt'
    ):
        repository.get(record.validation_id)


def test_eligible_read_fails_closed_when_roomsim_batch_disappears(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    repository.save(record)

    original = repository.roomsim_repository.get_batch_spec
    repository.roomsim_repository.get_batch_spec = lambda batch_run_id: None
    try:
        with pytest.raises(
            CadModelValidationIntegrityError, match='prediction batch does not exist'
        ):
            repository.latest_eligible_for_search_spec(record.search_spec_id)
    finally:
        repository.roomsim_repository.get_batch_spec = original


def test_eligible_read_fails_closed_when_o30_evaluation_disappears(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    repository.save(record)

    sample = record.objective_samples[0]
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_objective_evaluations WHERE evaluation_id=?',
            (sample.measured_evaluation_id,),
        )

    with pytest.raises(
        CadModelValidationIntegrityError, match='unknown evaluation'
    ):
        repository.latest_eligible_for_search_spec(record.search_spec_id)
    with pytest.raises(
        CadModelValidationIntegrityError, match='unknown evaluation'
    ):
        repository.get(record.validation_id)


def test_eligible_read_fails_closed_when_repeatability_evidence_breaks(tmp_path):
    record, repository, measurement_repo = _fixture(tmp_path)
    repository.save(record)

    del measurement_repo.records['repeat:a:1']

    with pytest.raises(
        CadModelValidationIntegrityError,
        match='repeatability evidence binding mismatch',
    ):
        repository.latest_eligible_for_search_spec(record.search_spec_id)


def test_eligible_read_fails_closed_when_separation_measurement_disappears(tmp_path):
    record, repository, measurement_repo = _fixture(tmp_path)
    repository.save(record)

    check = record.separation_checks[0]
    del measurement_repo.records[check.measurement_b_id]

    with pytest.raises(
        CadModelValidationIntegrityError,
        match='must reference measured evidence',
    ):
        repository.latest_eligible_for_search_spec(record.search_spec_id)


def test_eligible_read_fails_closed_when_campaign_registration_disappears(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    repository.save(record)

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_validation_campaign_registrations WHERE campaign_id=?',
            (record.campaign_id,),
        )

    with pytest.raises(
        CadModelValidationIntegrityError,
        match='campaign registration is missing',
    ):
        repository.latest_eligible_for_search_spec(record.search_spec_id)


def test_eligible_read_fails_closed_when_measurement_plan_unlinks(tmp_path):
    record, repository, measurement_repo = _fixture(tmp_path)
    repository.save(record)

    measurement_repo.plans.clear()

    with pytest.raises(CadModelValidationIntegrityError, match='not linked'):
        repository.latest_eligible_for_search_spec(record.search_spec_id)


def test_stale_record_remains_inspectable_as_history(tmp_path):
    """Stale rows are never reclassified or deleted: they stay inspectable
    through the diagnostic views while every authority read fails closed."""
    record, repository, measurement_repo = _fixture(tmp_path)
    repository.save(record)
    measurement_repo.missing_assets.add(f'meas:{record.pairs[0].candidate_id}')

    assert repository.inspect(record.validation_id) == record
    assert repository.inspect_for_search_spec(record.search_spec_id) == (record,)

    problems = repository.integrity_problems()
    assert len(problems) == 1
    assert record.validation_id in problems[0]
    assert 'raw asset' in problems[0]
    assert repository.integrity_problems(record.search_spec_id) == problems
    assert repository.integrity_problems('other-search-spec') == []


def test_integrity_scan_is_clean_for_intact_records(tmp_path):
    record, repository, _measurement_repo = _fixture(tmp_path)
    repository.save(record)

    assert repository.integrity_problems() == []
    assert repository.inspect(record.validation_id) == record
    assert repository.inspect('missing-validation-id') is None


def test_save_rejects_invalid_evidence_with_plain_value_error(tmp_path):
    """Save keeps reporting caller input problems as plain ``ValueError``;
    the typed integrity failure is reserved for persisted records."""
    record, repository, measurement_repo = _fixture(tmp_path)
    measurement_repo.missing_assets.add(f'meas:{record.pairs[0].candidate_id}')

    with pytest.raises(ValueError, match='raw asset') as exc_info:
        repository.save(record)
    assert not isinstance(exc_info.value, CadModelValidationIntegrityError)
