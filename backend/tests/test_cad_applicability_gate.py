from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace

import pytest

from htdt.cad_applicability import (
    AUTOMATED_EVALUATOR_BY_CODE,
    APPLICABILITY_MANUAL_EVALUATOR_ID,
    build_applicability_attestation,
    evaluate_applicability,
    resolve_applicability_context,
)
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_model_validation import build_full_model_validation
from htdt.cad_model_validation_repository import CadModelValidationRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_results import canonical_roomsim_result_json
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_validation_metrics import (
    CadApplicabilityCheck,
    CadApplicabilityEvidenceRef,
    build_applicability_check,
)
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


class _RoomSimEvidence:
    def __init__(self, path, spec, candidate_set_sha256, *, geometry_mode='exact_rectangular'):
        self.path = path
        self.spec = spec
        self.candidate_set_sha256 = candidate_set_sha256
        self.geometry_mode = geometry_mode

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
            batch_run_id='batch-a',
            document_id=self.spec.document_id,
            scene_revision_id=self.spec.scene_revision_id,
            scene_content_hash=self.spec.scene_content_hash,
            search_spec_id=self.spec.search_spec_id,
            search_spec_sha256=self.spec.search_spec_sha256,
            candidate_set_sha256=self.candidate_set_sha256,
            model_id='rew-roomsim',
            binding_json=json.dumps({'geometry_mode': self.geometry_mode}),
            batch_spec_sha256='ab' * 32,
        )


class _MeasurementEvidence:
    def __init__(self, path, document_id, candidate_set_sha256, *, routing_evidence='verified'):
        self.path = path
        self.document_id = document_id
        self.candidate_set_sha256 = candidate_set_sha256
        self.routing_evidence = routing_evidence

    def get_measurement(self, measurement_id):
        if measurement_id != 'measurement-a':
            return None
        return SimpleNamespace(
            measurement_id=measurement_id,
            evidence_type='measured',
            routing_evidence=self.routing_evidence,
            document_id=self.document_id,
            scene_revision_id='applied:candidate-a',
        )

    def dataset_for_measurement(self, measurement_id):
        if measurement_id != 'measurement-a':
            return None
        response = _fr(0.5)
        return SimpleNamespace(
            frequency_hz=response.frequency_hz,
            level_db=response.level_db,
        )

    def list_measurement_plans(self, search_spec_id):
        return (
            SimpleNamespace(
                plan_id='plan:candidate-a',
                status='measured',
                candidate_id='candidate-a',
                candidate_set_sha256=self.candidate_set_sha256,
                measurement_ids=('measurement-a',),
                applied_scene_revision_id='applied:candidate-a',
                plan_sha256='cd' * 32,
            ),
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


def _fixture(tmp_path, *, routing_evidence='verified', geometry_mode='exact_rectangular'):
    scene_repo = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='o60-applicability',
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
    page = generate_cad_candidates(scene_repo, spec, limit=10)

    roomsim = _RoomSimEvidence(
        scene_repo.path,
        spec,
        page.candidate_set_sha256,
        geometry_mode=geometry_mode,
    )
    measurements = _MeasurementEvidence(
        scene_repo.path,
        document.document_id,
        page.candidate_set_sha256,
        routing_evidence=routing_evidence,
    )
    repository = CadModelValidationRepository(search_repo, roomsim, measurements)
    context = resolve_applicability_context(
        document_id=spec.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='rew-fixture',
        evidence_scope='synthetic_fixture',
        campaign_id=None,
        requested_band_hz=(20.0, 160.0),
        pair_attempt_ids=('attempt-a',),
        pair_measurement_ids=('measurement-a',),
        scoped_measurement_ids=('measurement-a',),
        search_repository=search_repo,
        roomsim_repository=roomsim,
        measurement_repository=measurements,
        attestation_repository=repository.applicability_attestations,
    )
    return SimpleNamespace(
        scene_repo=scene_repo,
        search_repo=search_repo,
        spec=spec,
        page=page,
        roomsim=roomsim,
        measurements=measurements,
        repository=repository,
        context=context,
    )


def _derived_checks(env, *, codes=('geometry', 'band', 'routing')):
    return tuple(
        evaluate_applicability(
            env.context,
            code=code,
            evaluator_id=AUTOMATED_EVALUATOR_BY_CODE[code],
            detail=f'fixture {code}',
        )
        for code in codes
    )


def _record(env, applicability_checks):
    return build_full_model_validation(
        document_id=env.spec.document_id,
        search_spec_id=env.spec.search_spec_id,
        search_spec_sha256=env.spec.search_spec_sha256,
        candidate_set_sha256=env.page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='rew-fixture',
        response_samples=(
            (
                'candidate-a',
                'holdout',
                'attempt-a',
                'measurement-a',
                _fr(0.0),
                _fr(0.5),
            ),
        ),
        objective_samples=(),
        sensitivity_checks=(),
        repeatability_checks=(),
        separation_checks=(),
        applicability_checks=applicability_checks,
        low_hz=20.0,
        high_hz=160.0,
        max_holdout_rms_db=3.0,
        evidence_scope='synthetic_fixture',
    )


def test_applicability_check_requires_authority_fields():
    # A bare (code, passed, detail) claim is not even a valid check anymore.
    with pytest.raises(ValueError):
        CadApplicabilityCheck(code='geometry', passed=True, detail='supported')


def test_applicability_check_rejects_tampered_decision(tmp_path):
    env = _fixture(tmp_path)
    check = _derived_checks(env, codes=('geometry',))[0]
    tampered = check.model_copy(update={'passed': False})

    with pytest.raises(ValueError, match='decision hash'):
        CadApplicabilityCheck.model_validate(tampered.model_dump(mode='python'))


def test_applicability_check_rejects_tampered_subject(tmp_path):
    env = _fixture(tmp_path)
    check = _derived_checks(env, codes=('geometry',))[0]
    subject = dict(check.subject())
    subject['scene_revision_id'] = 'other-revision'
    tampered = check.model_copy(update={'subject_json': json.dumps(subject)})

    with pytest.raises(ValueError, match='subject'):
        CadApplicabilityCheck.model_validate(tampered.model_dump(mode='python'))


def test_honest_applicability_checks_persist_and_reopen(tmp_path):
    env = _fixture(tmp_path)
    checks = _derived_checks(env)
    assert all(check.passed for check in checks)
    record = _record(env, checks)

    env.repository.save(record)

    assert env.repository.get(record.validation_id) == record
    assert env.repository.list_for_search_spec(env.spec.search_spec_id) == (record,)
    reopened = env.repository.get(record.validation_id)
    geometry = next(c for c in reopened.applicability_checks if c.code == 'geometry')
    assert geometry.evaluator_id == 'o60-applicability-geometry'
    assert geometry.subject()['scene_revision_id'] == env.spec.scene_revision_id
    assert any(
        ref.source_kind == 'cad_scene_revision'
        and ref.source_id == env.spec.scene_revision_id
        for ref in geometry.evidence_refs
    )


def test_fabricated_pass_fails_closed_on_save(tmp_path):
    env = _fixture(tmp_path)
    checks = _derived_checks(env)
    fabricated = build_applicability_check(
        code='geometry',
        passed=True,
        evaluator_id='o60-applicability-geometry',
        evaluator_version='1',
        subject={
            'document_id': env.spec.document_id,
            'scene_revision_id': 'invented-revision',
            'scene_content_hash': '9' * 64,
        },
        evidence_refs=(
            CadApplicabilityEvidenceRef(
                source_kind='cad_scene_revision',
                source_id='invented-revision',
                source_sha256='9' * 64,
            ),
        ),
        detail='fabricated PASS',
    )
    record = _record(env, (fabricated,) + checks[1:])

    with pytest.raises(ValueError, match='does not match evidence authority'):
        env.repository.save(record)


def test_claimed_result_mismatch_fails_closed(tmp_path):
    env = _fixture(tmp_path, routing_evidence='manual')
    checks = _derived_checks(env)
    routing = next(check for check in checks if check.code == 'routing')
    assert not routing.passed

    # Attacker keeps the honest subject/refs but flips the claimed decision.
    forged = build_applicability_check(
        code='routing',
        passed=True,
        evaluator_id=routing.evaluator_id,
        evaluator_version=routing.evaluator_version,
        subject=routing.subject(),
        evidence_refs=routing.evidence_refs,
        detail=routing.detail,
    )
    record = _record(env, tuple(forged if c.code == 'routing' else c for c in checks))
    with pytest.raises(ValueError, match='does not match evidence authority'):
        env.repository.save(record)


def test_unregistered_evaluator_fails_closed(tmp_path):
    env = _fixture(tmp_path)
    checks = _derived_checks(env)
    rogue = build_applicability_check(
        code='geometry',
        passed=True,
        evaluator_id='unregistered-evaluator',
        evaluator_version='1',
        subject=checks[0].subject(),
        evidence_refs=checks[0].evidence_refs,
    )
    record = _record(env, (rogue,) + checks[1:])

    with pytest.raises(ValueError, match='not registered'):
        env.repository.save(record)


def test_persisted_applicability_tamper_fails_closed_on_read(tmp_path):
    env = _fixture(tmp_path)
    checks = _derived_checks(env)
    record = _record(env, checks)
    env.repository.save(record)

    geometry = checks[0]
    forged = build_applicability_check(
        code='geometry',
        passed=geometry.passed,
        evaluator_id=geometry.evaluator_id,
        evaluator_version=geometry.evaluator_version,
        subject={
            'document_id': env.spec.document_id,
            'scene_revision_id': 'forged-revision',
            'scene_content_hash': '8' * 64,
        },
        evidence_refs=(
            CadApplicabilityEvidenceRef(
                source_kind='cad_scene_revision',
                source_id='forged-revision',
                source_sha256='8' * 64,
            ),
        ),
        detail=geometry.detail,
    )
    tampered = _rehashed(
        record,
        applicability_checks=(forged,) + record.applicability_checks[1:],
    )
    with sqlite3.connect(env.repository.path) as connection:
        connection.execute(
            'UPDATE cad_model_validations SET payload_json=? WHERE validation_id=?',
            (tampered.model_dump_json(), record.validation_id),
        )

    with pytest.raises(ValueError, match='does not match evidence authority'):
        env.repository.get(record.validation_id)
    with pytest.raises(ValueError, match='does not match evidence authority'):
        env.repository.list_for_search_spec(env.spec.search_spec_id)


def test_manual_attestation_check_persists_and_reopens(tmp_path):
    env = _fixture(tmp_path)
    attestation = build_applicability_attestation(
        document_id=env.spec.document_id,
        search_spec_id=env.spec.search_spec_id,
        search_spec_sha256=env.spec.search_spec_sha256,
        code='operator_review',
        decision='pass',
        actor='operator:ka092',
        evidence={'measurement_ids': ['measurement-a'], 'note': 'physically verified'},
        subject_scope={'measurement_ids': ['measurement-a']},
        attested_at_utc='2030-01-02T00:00:00+00:00',
    )
    env.repository.save_attestation(attestation)

    check = evaluate_applicability(
        env.context,
        code='operator_review',
        evaluator_id=APPLICABILITY_MANUAL_EVALUATOR_ID,
        attestation_id=attestation.attestation_id,
        detail='operator confirmed',
    )
    assert check.passed
    assert check.evidence_refs[0].source_id == attestation.attestation_id

    record = _record(env, _derived_checks(env) + (check,))
    env.repository.save(record)
    assert env.repository.get(record.validation_id) == record


def test_manual_attestation_for_another_scope_is_rejected(tmp_path):
    env = _fixture(tmp_path)
    foreign = build_applicability_attestation(
        document_id=env.spec.document_id,
        search_spec_id='other-search-spec',
        search_spec_sha256='7' * 64,
        code='operator_review',
        decision='pass',
        actor='operator:ka092',
        evidence={'note': 'elsewhere'},
    )
    env.repository.save_attestation(foreign)

    with pytest.raises(ValueError, match='another scope'):
        evaluate_applicability(
            env.context,
            code='operator_review',
            evaluator_id=APPLICABILITY_MANUAL_EVALUATOR_ID,
            attestation_id=foreign.attestation_id,
        )


def test_manual_check_referencing_missing_attestation_is_rejected(tmp_path):
    env = _fixture(tmp_path)
    check = build_applicability_check(
        code='operator_review',
        passed=True,
        evaluator_id=APPLICABILITY_MANUAL_EVALUATOR_ID,
        evaluator_version='1',
        subject={
            'code': 'operator_review',
            'document_id': env.spec.document_id,
            'scope': {},
            'search_spec_id': env.spec.search_spec_id,
            'search_spec_sha256': env.spec.search_spec_sha256,
        },
        evidence_refs=(
            CadApplicabilityEvidenceRef(
                source_kind='o60_applicability_attestation',
                source_id='o60-applicability-attestation:' + '1' * 64,
                source_sha256='1' * 64,
            ),
        ),
    )
    record = _record(env, _derived_checks(env) + (check,))

    with pytest.raises(ValueError, match='does not exist'):
        env.repository.save(record)


def test_attestation_semantics_are_immutable(tmp_path):
    env = _fixture(tmp_path)
    attestation = build_applicability_attestation(
        document_id=env.spec.document_id,
        search_spec_id=env.spec.search_spec_id,
        search_spec_sha256=env.spec.search_spec_sha256,
        code='operator_review',
        decision='pass',
        actor='operator:ka092',
        evidence={'note': 'verified'},
    )
    env.repository.save_attestation(attestation)
    assert env.repository.get_attestation(attestation.attestation_id) == attestation

    conflicting = build_applicability_attestation(
        document_id=env.spec.document_id,
        search_spec_id=env.spec.search_spec_id,
        search_spec_sha256=env.spec.search_spec_sha256,
        code='operator_review',
        decision='fail',
        actor='operator:other',
        evidence={'note': 'disagreement'},
    )
    # Re-saving identical content is idempotent; a different attestation has a
    # different derived id and must not collide.
    env.repository.save_attestation(attestation)
    assert conflicting.attestation_id != attestation.attestation_id


def test_attestation_rejects_tampered_payload():
    attestation = build_applicability_attestation(
        document_id='doc',
        search_spec_id='spec',
        search_spec_sha256='1' * 64,
        code='routing',
        decision='pass',
        actor='operator:ka092',
        evidence={'note': 'verified'},
        attested_at_utc='2030-01-02T00:00:00+00:00',
    )
    tampered = attestation.model_copy(update={'decision': 'fail'})
    with pytest.raises(ValueError, match='semantic hash'):
        type(attestation).model_validate(tampered.model_dump(mode='python'))
