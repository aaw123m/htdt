from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace

import pytest

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_model_validation import build_model_validation
from htdt.cad_model_validation_repository import CadModelValidationRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_results import canonical_roomsim_result_json
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import build_cad_search_spec
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.comparison import FrequencyResponse


def _fr(offset: float) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(80.0 + offset, 81.0 + offset, 79.0 + offset, 80.0 + offset),
    )


def _response_json(offset: float) -> str:
    response = _fr(offset)
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


def _dataset(offset: float):
    response = _fr(offset)
    return SimpleNamespace(
        frequency_hz=response.frequency_hz,
        level_db=response.level_db,
    )


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


def _search(tmp_path):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='o60-fixture',
        schema_version=2,
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl', kind='speaker', name='FL', speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
            ),
        ),
    )
    revision = scene_repo.save(document, parent_revision_id=None).revision
    spec, _ = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=document.document_id, constraints=()),
        (CadSearchAxis(entity_id='fl', axis='x', min_m=1.0, max_m=2.0, step_m=1.0),),
        candidate_limit=10,
    )
    search_repo = CadSearchRepository(scene_repo)
    search_repo.save(spec)
    return scene_repo, search_repo, spec


class _RoomSimEvidence:
    def __init__(self, path, spec, candidate_set_sha256):
        self.path = path
        self.spec = spec
        self.candidate_set_sha256 = candidate_set_sha256

    def get_attempt(self, attempt_id):
        if attempt_id != 'attempt-a':
            return None
        return SimpleNamespace(
            attempt_id=attempt_id,
            batch_run_id='batch-a',
            candidate_id='candidate-a',
            status='completed',
            result=SimpleNamespace(
                model_version='rew-fixture',
                response_json=_response_json(0.0),
            ),
        )

    def get_batch_spec(self, batch_run_id):
        if batch_run_id != 'batch-a':
            return None
        return SimpleNamespace(
            document_id=self.spec.document_id,
            search_spec_id=self.spec.search_spec_id,
            search_spec_sha256=self.spec.search_spec_sha256,
            candidate_set_sha256=self.candidate_set_sha256,
            model_id='rew-roomsim',
        )


class _MeasurementEvidence:
    def __init__(self, path, candidate_set_sha256, *, linked=True, dataset_offset=0.5):
        self.path = path
        self.candidate_set_sha256 = candidate_set_sha256
        self.linked = linked
        self.dataset_offset = dataset_offset

    def get_measurement(self, measurement_id):
        if measurement_id != 'measurement-a':
            return None
        return SimpleNamespace(measurement_id=measurement_id, evidence_type='measured')

    def dataset_for_measurement(self, measurement_id):
        if measurement_id != 'measurement-a':
            return None
        return _dataset(self.dataset_offset)

    def list_measurement_plans(self, search_spec_id):
        measurement_ids = ('measurement-a',) if self.linked else ('other-measurement',)
        return (
            SimpleNamespace(
                status='measured',
                candidate_id='candidate-a',
                candidate_set_sha256=self.candidate_set_sha256,
                measurement_ids=measurement_ids,
            ),
        )


def _record(spec, candidate_set_sha256, *, measured_offset=0.5):
    return build_model_validation(
        document_id=spec.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='rew-fixture',
        samples=(
            (
                'candidate-a',
                'holdout',
                'attempt-a',
                'measurement-a',
                _fr(0.0),
                _fr(measured_offset),
            ),
        ),
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=3.0,
    )


def _repository(scene_repo, search_repo, spec, candidate_set_sha256, *, linked=True, dataset_offset=0.5):
    return CadModelValidationRepository(
        search_repo,
        _RoomSimEvidence(scene_repo.path, spec, candidate_set_sha256),
        _MeasurementEvidence(
            scene_repo.path,
            candidate_set_sha256,
            linked=linked,
            dataset_offset=dataset_offset,
        ),
    )


def test_validation_repository_persists_cross_evidence_authority(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = 'a' * 64
    repository = _repository(scene_repo, search_repo, spec, candidate_set_sha256)
    record = _record(spec, candidate_set_sha256)

    repository.save(record)

    assert repository.get(record.validation_id) == record
    assert repository.list_for_search_spec(spec.search_spec_id) == (record,)


def test_validation_repository_rejects_measurement_not_linked_to_candidate_plan(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = 'b' * 64
    repository = _repository(
        scene_repo, search_repo, spec, candidate_set_sha256, linked=False
    )

    with pytest.raises(ValueError, match='not linked'):
        repository.save(_record(spec, candidate_set_sha256))


def test_validation_repository_rejects_tampered_identity_hash(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = 'c' * 64
    repository = _repository(scene_repo, search_repo, spec, candidate_set_sha256)
    tampered = _record(spec, candidate_set_sha256).model_copy(
        update={'validation_sha256': '0' * 64}
    )

    with pytest.raises(ValueError, match='identity hash mismatch'):
        repository.save(tampered)


def test_validation_repository_rejects_tampered_pair_rms(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = 'd' * 64
    repository = _repository(scene_repo, search_repo, spec, candidate_set_sha256)
    record = _record(spec, candidate_set_sha256)
    pair = record.pairs[0].model_copy(update={'rms_difference_db': 0.01})
    tampered = _rehashed(record, pairs=(pair,))

    with pytest.raises(ValueError, match='residuals do not match'):
        repository.save(tampered)


def test_validation_repository_rejects_tampered_pair_shape_rms(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = 'e' * 64
    repository = _repository(scene_repo, search_repo, spec, candidate_set_sha256)
    record = _record(spec, candidate_set_sha256)
    pair = record.pairs[0].model_copy(update={'shape_rms_db': 0.01})
    tampered = _rehashed(record, pairs=(pair,))

    with pytest.raises(ValueError, match='residuals do not match'):
        repository.save(tampered)


def test_validation_repository_rejects_tampered_holdout_rms(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = 'f' * 64
    repository = _repository(scene_repo, search_repo, spec, candidate_set_sha256)
    record = _record(spec, candidate_set_sha256)
    tampered = _rehashed(record, holdout_rms_db=0.01)

    with pytest.raises(ValueError, match='residuals do not match'):
        repository.save(tampered)


def test_validation_repository_rejects_tampered_calibration_rms(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = '1' * 64
    repository = _repository(scene_repo, search_repo, spec, candidate_set_sha256)
    record = _record(spec, candidate_set_sha256)
    assert record.calibration_rms_db is None
    tampered = _rehashed(record, calibration_rms_db=0.01)

    with pytest.raises(ValueError, match='residuals do not match'):
        repository.save(tampered)


def test_validation_repository_rejects_fabricated_residual_gate(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = '2' * 64
    repository = _repository(
        scene_repo, search_repo, spec, candidate_set_sha256, dataset_offset=6.0
    )
    record = _record(spec, candidate_set_sha256, measured_offset=6.0)
    assert record.holdout_rms_db > record.max_holdout_rms_db
    assert record.residual_gate == 'fail'

    # An attacker who lowers the claimed holdout RMS, flips the gate to pass,
    # and recomputes the dependent gate reasons plus identity hash still fails.
    tampered = _rehashed(
        record,
        holdout_rms_db=0.5,
        residual_gate='pass',
        gate_reasons=tuple(
            reason
            for reason in record.gate_reasons
            if reason != 'holdout residual gate is fail'
        ),
    )
    with pytest.raises(ValueError, match='residuals do not match'):
        repository.save(tampered)


def test_validation_repository_read_fails_closed_on_tampered_payload(tmp_path):
    scene_repo, search_repo, spec = _search(tmp_path)
    candidate_set_sha256 = '3' * 64
    repository = _repository(scene_repo, search_repo, spec, candidate_set_sha256)
    record = _record(spec, candidate_set_sha256)
    repository.save(record)

    tampered = _rehashed(record, holdout_rms_db=0.01)
    with sqlite3.connect(scene_repo.path) as connection:
        connection.execute(
            'UPDATE cad_model_validations SET payload_json=? WHERE validation_id=?',
            (tampered.model_dump_json(), record.validation_id),
        )

    with pytest.raises(ValueError, match='residuals do not match'):
        repository.get(record.validation_id)
    with pytest.raises(ValueError, match='residuals do not match'):
        repository.list_for_search_spec(spec.search_spec_id)
