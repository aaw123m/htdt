from __future__ import annotations

import json
from types import SimpleNamespace

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_model_validation_repository import CadModelValidationRepository
from htdt.cad_model_validation_service import (
    CadModelValidationBuildSpec,
    CadModelValidationService,
    CadValidationApplicabilitySpec,
    CadValidationCandidateBinding,
    CadValidationObjectiveBinding,
    CadValidationRepeatabilitySpec,
    CadValidationSensitivitySpec,
    CadValidationSeparationSpec,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.optimization_objectives import ObjectiveMetric, ObjectiveVector


def _binding_json() -> str:
    return json.dumps({'geometry_mode': 'exact_rectangular'})


def _response_payload(offset: float) -> str:
    return json.dumps({
        'frequency_hz': [20.0, 40.0, 80.0, 160.0],
        'magnitude': [80.0 + offset, 81.0 + offset, 79.0 + offset, 80.0 + offset],
    })


class _RoomSim:
    def __init__(self, path, candidates, spec=None, candidate_set_sha256=None):
        self.path = path
        self.candidates = {candidate.candidate_id: candidate for candidate in candidates}
        self.spec = spec
        self.candidate_set_sha256 = candidate_set_sha256

    def get_attempt(self, attempt_id):
        candidate_id = attempt_id.removeprefix('pred:')
        if candidate_id not in self.candidates:
            return None
        index = list(self.candidates).index(candidate_id)
        return SimpleNamespace(
            attempt_id=attempt_id,
            batch_run_id='batch',
            status='completed',
            candidate_id=candidate_id,
            model_version='fixture-1',
            response_json=_response_payload(float(index) * 2.0),
        )

    def get_batch_spec(self, batch_run_id):
        if batch_run_id != 'batch' or self.spec is None:
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
            binding_json=_binding_json(),
            batch_spec_sha256='b' * 64,
        )


class _Measurements:
    def __init__(self, path, candidate_ids, document_id=None, candidate_set_sha256=None):
        self.path = path
        self.document_id = document_id
        self.candidate_set_sha256 = candidate_set_sha256
        self.records = {}
        self.datasets = {}
        self.plans = []
        for index, candidate_id in enumerate(candidate_ids):
            revision_id = f'applied:{candidate_id}'
            measurement_ids = [f'meas:{candidate_id}']
            self._add(measurement_ids[0], revision_id, float(index) * 2.0 + 0.2)
            if index == 0:
                self._add('repeat:1', revision_id, 0.2)
                self._add('repeat:2', revision_id, 0.3)
                measurement_ids.extend(('repeat:1', 'repeat:2'))
            self.plans.append(SimpleNamespace(
                plan_id=f'plan:{candidate_id}',
                status='measured',
                candidate_id=candidate_id,
                candidate_set_sha256=candidate_set_sha256,
                measurement_ids=tuple(measurement_ids),
                applied_scene_revision_id=revision_id,
                plan_sha256=f'{(index + 1):064x}',
            ))

    def _add(self, measurement_id, revision_id, offset):
        self.records[measurement_id] = SimpleNamespace(
            measurement_id=measurement_id,
            evidence_type='measured',
            document_id=self.document_id,
            scene_revision_id=revision_id,
            routing_evidence='verified',
        )
        response = json.loads(_response_payload(offset))
        self.datasets[measurement_id] = SimpleNamespace(
            frequency_hz=tuple(response['frequency_hz']),
            level_db=tuple(response['magnitude']),
        )

    def get_measurement(self, measurement_id):
        return self.records.get(measurement_id)

    def dataset_for_measurement(self, measurement_id):
        return self.datasets.get(measurement_id)

    def list_measurement_plans(self, search_spec_id):
        return tuple(self.plans)


class _Objectives:
    def __init__(self, path, candidate_ids, document_id=None, spec=None):
        self.path = path
        self.evaluations = {}
        for index, candidate_id in enumerate(candidate_ids, start=1):
            for prefix, value in (('pred-eval', float(index)), ('meas-eval', float(index) + 0.1)):
                evaluation_id = f'{prefix}:{candidate_id}'
                predicted = prefix == 'pred-eval'
                self.evaluations[evaluation_id] = SimpleNamespace(
                    evaluation_id=evaluation_id,
                    document_id=document_id,
                    search_spec_id=None if spec is None else spec.search_spec_id,
                    search_spec_sha256=None if spec is None else spec.search_spec_sha256,
                    candidate_id=candidate_id,
                    input_refs=(
                        SimpleNamespace(
                            evidence_class='predicted' if predicted else 'measured',
                            source_kind=(
                                'cad_roomsim_attempt' if predicted else 'cad_measurement'
                            ),
                            source_id=(
                                f'pred:{candidate_id}'
                                if predicted
                                else f'meas:{candidate_id}'
                            ),
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

    def get_evaluation(self, evaluation_id):
        return self.evaluations.get(evaluation_id)


def test_validation_service_builds_full_record_from_repository_evidence(tmp_path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='o60-service',
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

    roomsim = _RoomSim(scene_repo.path, candidates, spec, page.candidate_set_sha256)
    measurements = _Measurements(
        scene_repo.path,
        candidate_ids,
        document.document_id,
        page.candidate_set_sha256,
    )
    objectives = _Objectives(scene_repo.path, candidate_ids)
    service = CadModelValidationService(search_repo, roomsim, measurements, objectives)

    candidate_bindings = tuple(
        CadValidationCandidateBinding(
            candidate_id=candidate_id,
            split='calibration' if index == 2 else 'holdout',
            prediction_attempt_id=f'pred:{candidate_id}',
            measurement_id=f'meas:{candidate_id}',
            objectives=(
                CadValidationObjectiveBinding(
                    objective_id='response.shape_rms_db',
                    predicted_evaluation_id=f'pred-eval:{candidate_id}',
                    measured_evaluation_id=f'meas-eval:{candidate_id}',
                ),
            ),
        )
        for index, candidate_id in enumerate(candidate_ids)
    )
    build_spec = CadModelValidationBuildSpec(
        search_spec_id=spec.search_spec_id,
        candidate_set_sha256=page.candidate_set_sha256,
        campaign_id='campaign-fixture',
        campaign_sha256='4' * 64,
        campaign_registration_id='o60-validation-campaign-registration:' + '5' * 64,
        campaign_registration_sha256='6' * 64,
        model_id='rew-roomsim',
        model_version='fixture-1',
        evidence_scope='owned_room',
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=1.0,
        candidates=candidate_bindings,
        sensitivity=(
            CadValidationSensitivitySpec(
                objective_id='response.shape_rms_db',
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                max_observed_sensitivity_per_m=6.0,
                max_model_error_per_m=1.0,
            ),
        ),
        repeatability=(
            CadValidationRepeatabilitySpec(
                measurement_ids=('repeat:1', 'repeat:2'),
            ),
        ),
        separation=(
            CadValidationSeparationSpec(
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                measurement_a_id=f'meas:{candidate_ids[0]}',
                measurement_b_id=f'meas:{candidate_ids[1]}',
                repeatability_measurement_ids=('repeat:1', 'repeat:2'),
                min_repeatability_multiple=2.0,
            ),
        ),
        applicability=(
            CadValidationApplicabilitySpec(code='geometry', detail='supported'),
            CadValidationApplicabilitySpec(code='band', detail='supported'),
            CadValidationApplicabilitySpec(code='routing', detail='verified'),
        ),
    )

    record = service.build(build_spec)

    assert record.recommendation_gate == 'eligible'
    assert len(record.trend_checks) == 1
    assert record.trend_checks[0].gate == 'pass'
    assert record.sensitivity_checks[0].gate == 'pass'
    assert record.repeatability_checks[0].rms_floor_db > 0
    assert record.separation_checks[0].gate == 'pass'


def test_service_built_record_persists_and_reopens_unchanged(tmp_path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='o60-service-persist',
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

    roomsim = _RoomSim(scene_repo.path, candidates, spec, page.candidate_set_sha256)
    measurements = _Measurements(
        scene_repo.path,
        candidate_ids,
        document.document_id,
        page.candidate_set_sha256,
    )
    objectives = _Objectives(
        scene_repo.path,
        candidate_ids,
        document.document_id,
        spec,
    )
    service = CadModelValidationService(search_repo, roomsim, measurements, objectives)

    build_spec = CadModelValidationBuildSpec(
        search_spec_id=spec.search_spec_id,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='fixture-1',
        evidence_scope='synthetic_fixture',
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=1.0,
        candidates=tuple(
            CadValidationCandidateBinding(
                candidate_id=candidate_id,
                split='calibration' if index == 2 else 'holdout',
                prediction_attempt_id=f'pred:{candidate_id}',
                measurement_id=f'meas:{candidate_id}',
                objectives=(
                    CadValidationObjectiveBinding(
                        objective_id='response.shape_rms_db',
                        predicted_evaluation_id=f'pred-eval:{candidate_id}',
                        measured_evaluation_id=f'meas-eval:{candidate_id}',
                    ),
                ),
            )
            for index, candidate_id in enumerate(candidate_ids)
        ),
        sensitivity=(
            CadValidationSensitivitySpec(
                objective_id='response.shape_rms_db',
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                max_observed_sensitivity_per_m=6.0,
                max_model_error_per_m=1.0,
            ),
        ),
        repeatability=(
            CadValidationRepeatabilitySpec(
                measurement_ids=('repeat:1', 'repeat:2'),
            ),
        ),
        separation=(
            CadValidationSeparationSpec(
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                measurement_a_id=f'meas:{candidate_ids[0]}',
                measurement_b_id=f'meas:{candidate_ids[1]}',
                repeatability_measurement_ids=('repeat:1', 'repeat:2'),
                min_repeatability_multiple=2.0,
            ),
        ),
        applicability=(
            CadValidationApplicabilitySpec(code='geometry', detail='supported'),
            CadValidationApplicabilitySpec(code='band', detail='supported'),
            CadValidationApplicabilitySpec(code='routing', detail='verified'),
        ),
    )
    record = service.build(build_spec)

    repository = CadModelValidationRepository(
        search_repo,
        roomsim,
        measurements,
        objectives,
    )
    repository.save(record)

    assert repository.get(record.validation_id) == record
    assert repository.list_for_search_spec(spec.search_spec_id) == (record,)
