from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import sqlite3

import pytest

import htdt.cad_validation_campaign as campaign_module
import htdt.cad_validation_campaign_repository as campaign_repository_module
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_measurement_loop import (
    build_measurement_plan,
    complete_measurement_plan,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import measurement_record_for_revision
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import (
    build_cad_search_spec,
    candidate_preview_document,
    generate_cad_candidates,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_validation_campaign import (
    CadValidationCampaign,
    CadValidationCampaignCandidate,
    CadValidationCampaignRepeatability,
    CadValidationCampaignSensitivity,
    CadValidationCampaignSeparation,
    CadValidationTargetResponse,
    build_validation_campaign,
    build_validation_campaign_registration,
    exact_campaign_registration,
)
from htdt.cad_validation_campaign_repository import CadValidationCampaignRepository


CLAIMED_TIME = '2020-01-01T00:00:00+00:00'
REGISTER_TIME = '2026-09-21T00:04:30+00:00'


def _fixture(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='campaign-fixture',
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
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )
    revision = scene_repository.save(document, parent_revision_id=None).revision
    spec, _ = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=document.document_id, constraints=()),
        (CadSearchAxis(entity_id='fl', axis='x', min_m=1.0, max_m=1.4, step_m=0.2),),
        candidate_limit=10,
    )
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(spec)
    page = generate_cad_candidates(scene_repository, spec, limit=10)
    candidate_ids = tuple(candidate.candidate_id for candidate in page.candidates[:3])
    measurement_repository = CadMeasurementRepository(scene_repository)
    campaign_repository = CadValidationCampaignRepository(
        search_repository,
        measurement_repository,
    )
    return (
        scene_repository,
        search_repository,
        measurement_repository,
        campaign_repository,
        spec,
        page,
        candidate_ids,
    )


def _campaign(
    spec,
    page,
    candidate_ids,
    *,
    search_spec_sha256: str | None = None,
    candidate_set_sha256: str | None = None,
):
    return build_validation_campaign(
        document_id=spec.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=search_spec_sha256 or spec.search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256 or page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='5.40',
        requested_band_hz=(20.0, 160.0),
        max_holdout_rms_db=4.0,
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
                max_observed_sensitivity_per_m=20.0,
                max_model_error_per_m=10.0,
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


def _applied_revision(scene_repository, source, candidate):
    preview = candidate_preview_document(source.document, candidate)
    return scene_repository.save(
        preview,
        parent_revision_id=source.revision_id,
        allow_branch=True,
    ).revision


def _save_measurement(
    measurement_repository,
    revision,
    measurement_id,
    *,
    provenance=None,
    evidence_type='measured',
    captured_at='2030-01-01T00:00:00+00:00',
):
    record = measurement_record_for_revision(
        revision,
        'mlp',
        measurement_id=measurement_id,
        evidence_type=evidence_type,
        captured_at=captured_at,
        source_kind='rew_api',
        provenance=provenance,
    )
    raw = measurement_id.encode()
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset:{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(70.0, 71.0, 69.0, 70.0),
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version='test-1',
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.bin',
        raw_bytes=raw,
    )
    return record


def _candidate(page, candidate_id):
    return next(item for item in page.candidates if item.candidate_id == candidate_id)


def test_campaign_round_trip_preregisters_split_and_thresholds(tmp_path):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    campaign = _campaign(spec, page, candidate_ids)

    registration = repository.save(campaign)

    assert repository.get(campaign.campaign_id) == campaign
    assert repository.find_by_sha(spec.search_spec_id, campaign.campaign_sha256) == campaign
    assert repository.list_for_search_spec(spec.search_spec_id) == (campaign,)
    assert [item.split for item in campaign.candidates] == [
        'holdout',
        'holdout',
        'calibration',
    ]
    assert registration.campaign_id == campaign.campaign_id
    assert registration.campaign_sha256 == campaign.campaign_sha256
    assert repository.get_registration(campaign.campaign_id) == registration



def test_campaign_rejects_search_spec_hash_mismatch(tmp_path):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    campaign = _campaign(
        spec,
        page,
        candidate_ids,
        search_spec_sha256='f' * 64,
    )

    with pytest.raises(ValueError, match='SearchSpec hash mismatch'):
        repository.save(campaign)


def test_campaign_rejects_candidate_set_hash_mismatch(tmp_path):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    campaign = _campaign(
        spec,
        page,
        candidate_ids,
        candidate_set_sha256='e' * 64,
    )

    with pytest.raises(ValueError, match='candidate-set hash mismatch'):
        repository.save(campaign)


def test_campaign_rejects_candidate_outside_exact_search_set(tmp_path):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    campaign = build_validation_campaign(
        document_id=spec.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=page.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='5.40',
        requested_band_hz=(20.0, 160.0),
        max_holdout_rms_db=4.0,
        candidates=(
            CadValidationCampaignCandidate(candidate_id=candidate_ids[0], split='holdout'),
            CadValidationCampaignCandidate(candidate_id=candidate_ids[1], split='holdout'),
            CadValidationCampaignCandidate(candidate_id='not-a-candidate', split='calibration'),
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
                max_observed_sensitivity_per_m=20.0,
                max_model_error_per_m=10.0,
            ),
        ),
        repeatability=(
            CadValidationCampaignRepeatability(candidate_id=candidate_ids[0]),
        ),
        separation=(
            CadValidationCampaignSeparation(
                candidate_a_id=candidate_ids[0],
                candidate_b_id=candidate_ids[1],
                repeatability_candidate_id=candidate_ids[0],
                min_repeatability_multiple=2.0,
            ),
        ),
        required_applicability_codes=('geometry',),
    )

    with pytest.raises(ValueError, match='outside SearchSpec'):
        repository.save(campaign)


def test_campaign_cannot_be_registered_after_candidate_plan_is_measured(tmp_path):
    (
        scene_repository,
        search_repository,
        measurement_repository,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    source = scene_repository.get(spec.scene_revision_id)
    applied = _applied_revision(
        scene_repository,
        source,
        _candidate(page, candidate_ids[2]),
    )
    plan = build_measurement_plan(
        scene_repository,
        search_repository,
        search_spec_id=spec.search_spec_id,
        candidate_id=candidate_ids[2],
        applied_scene_revision_id=applied.revision_id,
    )
    measurement_repository.save_measurement_plan(plan)
    _save_measurement(measurement_repository, applied, 'measurement:early')
    completed = complete_measurement_plan(
        plan,
        measurement_repository,
        ('measurement:early',),
    )
    measurement_repository.save_measurement_plan(completed)

    campaign = _campaign(spec, page, candidate_ids)

    with pytest.raises(ValueError, match='qualifying measurement evidence'):
        repository.save(campaign)
    assert repository.get(campaign.campaign_id) is None
    assert repository.get_registration(campaign.campaign_id) is None


def test_campaign_commit_time_is_repository_attested_not_caller_supplied(
    tmp_path,
    monkeypatch,
):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    # The caller backdates the claimed preregistration time arbitrarily.
    monkeypatch.setattr(campaign_module, '_utc_now', lambda: CLAIMED_TIME)
    monkeypatch.setattr(
        campaign_repository_module,
        '_utc_now',
        lambda: REGISTER_TIME,
    )
    campaign = _campaign(spec, page, candidate_ids)
    assert campaign.created_at_utc == CLAIMED_TIME

    registration = repository.save(campaign)

    assert registration.campaign_id == campaign.campaign_id
    assert registration.campaign_sha256 == campaign.campaign_sha256
    assert registration.registered_at_utc == REGISTER_TIME
    assert registration.registered_at_utc != campaign.created_at_utc
    assert repository.get_registration(campaign.campaign_id) == registration
    # The caller claim stays hashed campaign metadata; the durable commit
    # timestamp did not move.
    persisted = repository.get(campaign.campaign_id)
    assert persisted.created_at_utc == CLAIMED_TIME


def test_campaign_commit_time_is_not_rewriteable_by_resave(tmp_path, monkeypatch):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    monkeypatch.setattr(campaign_module, '_utc_now', lambda: CLAIMED_TIME)
    monkeypatch.setattr(
        campaign_repository_module,
        '_utc_now',
        lambda: REGISTER_TIME,
    )
    campaign = _campaign(spec, page, candidate_ids)
    registration = repository.save(campaign)

    monkeypatch.setattr(
        campaign_repository_module,
        '_utc_now',
        lambda: '2030-06-01T00:00:00+00:00',
    )
    assert repository.save(campaign) == registration
    assert registration.registered_at_utc == REGISTER_TIME


def test_campaign_timestamp_change_invalidates_registration_identity(
    tmp_path,
    monkeypatch,
):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    monkeypatch.setattr(campaign_module, '_utc_now', lambda: CLAIMED_TIME)
    campaign = _campaign(spec, page, candidate_ids)
    registration = repository.save(campaign)

    # A campaign identical except for the claimed timestamp is a different
    # campaign identity.
    monkeypatch.setattr(
        campaign_module,
        '_utc_now',
        lambda: '2020-01-01T00:00:01+00:00',
    )
    retimed = _campaign(spec, page, candidate_ids)
    assert retimed.campaign_sha256 != campaign.campaign_sha256
    assert {
        key: value
        for key, value in retimed.model_dump(mode='python').items()
        if key not in {'campaign_id', 'created_at_utc', 'campaign_sha256'}
    } == {
        key: value
        for key, value in campaign.model_dump(mode='python').items()
        if key not in {'campaign_id', 'created_at_utc', 'campaign_sha256'}
    }
    with pytest.raises(ValueError, match='registration authority mismatch'):
        exact_campaign_registration(retimed, registration)

    # Tampering with only the timestamp cannot keep the original identity.
    tampered = campaign.model_copy(
        update={'created_at_utc': '2020-06-01T00:00:00+00:00'}
    )
    with pytest.raises(ValueError, match='identity hash mismatch'):
        CadValidationCampaign.model_validate(tampered.model_dump(mode='python'))
    foreign = build_validation_campaign_registration(
        campaign=retimed,
        registered_at_utc=REGISTER_TIME,
    )
    with pytest.raises(ValueError, match='registration authority mismatch'):
        exact_campaign_registration(campaign, foreign)


def test_campaign_claimed_preregistration_cannot_postdate_commit(
    tmp_path,
    monkeypatch,
):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    # A campaign built before the commit cannot claim a preregistration
    # timestamp later than the durable registration time.
    monkeypatch.setattr(
        campaign_module,
        '_utc_now',
        lambda: '2030-01-01T00:00:00+00:00',
    )
    campaign = _campaign(spec, page, candidate_ids)

    with pytest.raises(ValueError, match='cannot postdate durable registration'):
        repository.save(campaign)
    assert repository.get(campaign.campaign_id) is None


def test_campaign_registration_rejects_naive_and_malformed_timestamps(tmp_path):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    campaign = _campaign(spec, page, candidate_ids)

    naive = campaign.model_copy(
        update={'created_at_utc': '2020-01-01T00:00:00'}
    )
    with pytest.raises(ValueError, match='timezone-aware'):
        repository.save(naive)

    malformed = campaign.model_copy(
        update={'created_at_utc': 'not-a-timestamp'}
    )
    with pytest.raises(ValueError, match='ISO-8601'):
        repository.save(malformed)


def test_raw_candidate_evidence_blocks_registration_before_plan_completion(
    tmp_path,
):
    (
        scene_repository,
        search_repository,
        measurement_repository,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    source = scene_repository.get(spec.scene_revision_id)
    applied = _applied_revision(
        scene_repository,
        source,
        _candidate(page, candidate_ids[1]),
    )
    plan = build_measurement_plan(
        scene_repository,
        search_repository,
        search_spec_id=spec.search_spec_id,
        candidate_id=candidate_ids[1],
        applied_scene_revision_id=applied.revision_id,
    )
    measurement_repository.save_measurement_plan(plan)
    # Raw measured evidence already exists under the candidate's applied
    # revision even though the plan was never marked measured.
    _save_measurement(measurement_repository, applied, 'measurement:raw')

    campaign = _campaign(spec, page, candidate_ids)

    with pytest.raises(ValueError, match='qualifying measurement evidence'):
        repository.save(campaign)


def test_measurement_claiming_campaign_provenance_blocks_registration(tmp_path):
    (
        scene_repository,
        _search,
        measurement_repository,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    campaign = _campaign(spec, page, candidate_ids)
    # A measurement imported with owned-room provenance already naming this
    # campaign is qualifying evidence and must block late registration.
    source = scene_repository.get(spec.scene_revision_id)
    _save_measurement(
        measurement_repository,
        source,
        'measurement:claims-campaign',
        provenance={
            'validation_scope': 'owned_room',
            'validation_campaign_id': campaign.campaign_id,
        },
    )

    with pytest.raises(ValueError, match='qualifying measurement evidence'):
        repository.save(campaign)


def test_unrelated_measurement_does_not_block_campaign_registration(tmp_path):
    (
        scene_repository,
        _search,
        measurement_repository,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    source = scene_repository.get(spec.scene_revision_id)
    # Measured evidence on the source revision without campaign provenance is
    # not candidate-linked qualifying evidence for this campaign.
    _save_measurement(
        measurement_repository,
        source,
        'measurement:unrelated',
    )
    campaign = _campaign(spec, page, candidate_ids)

    registration = repository.save(campaign)
    assert repository.get_registration(campaign.campaign_id) == registration


def test_campaign_built_before_measurement_is_rejected_when_saved_after(tmp_path):
    (
        scene_repository,
        search_repository,
        measurement_repository,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    # The campaign object is built first...
    campaign = _campaign(spec, page, candidate_ids)
    # ...but qualifying evidence commits before the campaign is registered.
    source = scene_repository.get(spec.scene_revision_id)
    applied = _applied_revision(
        scene_repository,
        source,
        _candidate(page, candidate_ids[1]),
    )
    plan = build_measurement_plan(
        scene_repository,
        search_repository,
        search_spec_id=spec.search_spec_id,
        candidate_id=candidate_ids[1],
        applied_scene_revision_id=applied.revision_id,
    )
    measurement_repository.save_measurement_plan(plan)
    _save_measurement(measurement_repository, applied, 'measurement:raced')
    measurement_repository.save_measurement_plan(
        complete_measurement_plan(
            plan,
            measurement_repository,
            ('measurement:raced',),
        )
    )

    with pytest.raises(ValueError, match='qualifying measurement evidence'):
        repository.save(campaign)


def test_missing_registration_fails_closed(tmp_path):
    (
        _scene,
        _search,
        _measurement,
        repository,
        spec,
        page,
        candidate_ids,
    ) = _fixture(tmp_path)
    campaign = _campaign(spec, page, candidate_ids)
    # Simulate a pre-authority row: campaign persisted with no registration.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''INSERT INTO cad_validation_campaigns(
                campaign_id, document_id, search_spec_id, model_id, model_version,
                candidate_set_sha256, campaign_sha256, payload_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                campaign.campaign_id,
                campaign.document_id,
                campaign.search_spec_id,
                campaign.model_id,
                campaign.model_version,
                campaign.candidate_set_sha256,
                campaign.campaign_sha256,
                campaign.model_dump_json(),
                campaign.created_at_utc,
            ),
        )

    # No historical registration time is silently inferred.
    assert repository.get_registration(campaign.campaign_id) is None
    with pytest.raises(ValueError, match='registration authority missing/stale'):
        repository.save(campaign)


def test_campaign_requires_two_holdout_candidates():
    with pytest.raises(ValueError, match='at least two holdout'):
        build_validation_campaign(
            document_id='doc',
            search_spec_id='spec',
            search_spec_sha256='1' * 64,
            candidate_set_sha256='2' * 64,
            model_id='rew-roomsim',
            model_version='5.40',
            requested_band_hz=(20.0, 160.0),
            max_holdout_rms_db=4.0,
            candidates=(
                CadValidationCampaignCandidate(candidate_id='a', split='holdout'),
                CadValidationCampaignCandidate(candidate_id='b', split='calibration'),
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
                    candidate_a_id='a',
                    candidate_b_id='b',
                    max_observed_sensitivity_per_m=20.0,
                    max_model_error_per_m=10.0,
                ),
            ),
            repeatability=(
                CadValidationCampaignRepeatability(candidate_id='a'),
            ),
            separation=(
                CadValidationCampaignSeparation(
                    candidate_a_id='a',
                    candidate_b_id='b',
                    repeatability_candidate_id='a',
                    min_repeatability_multiple=2.0,
                ),
            ),
            required_applicability_codes=('geometry',),
        )
