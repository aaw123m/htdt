from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

import htdt.cad_system_variant_measurement_campaign as campaign_module
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
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Direction3, Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_system_variant import ChannelRoleBinding, ProposedEntitySpec, build_system_variant
from htdt.cad_system_variant_lifecycle import (
    CadSystemVariantLifecycleRepository,
    build_system_variant_as_built_record,
)
from htdt.cad_system_variant_measured_lifecycle import (
    CadSystemVariantMeasuredLifecycleRepository,
)
from htdt.cad_system_variant_measurement_campaign import (
    CadSystemVariantMeasurementCampaignRepository,
    SystemVariantMeasurementTarget,
    VariantMeasurementAcquisitionRequirement,
    build_system_variant_measurement_campaign,
    build_system_variant_measurement_campaign_registration,
    build_system_variant_measurement_plan,
    complete_system_variant_measurement_campaign,
    complete_system_variant_measurement_plan,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


DOCUMENT_ID = 'o100g-variant-campaign-fixture'
PLAN_TIME = '2026-09-20T00:03:00+00:00'
CAMPAIGN_TIME = '2026-09-20T00:04:00+00:00'
REGISTER_TIME = '2026-09-20T00:04:30+00:00'
CAPTURE_TIME = '2026-09-20T00:05:00+00:00'
COMPLETE_TIME = '2026-09-20T00:06:00+00:00'


@pytest.fixture(autouse=True)
def _repository_commit_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the durable commit timestamp the repository attests at save."""
    monkeypatch.setattr(
        campaign_module,
        '_utc_now',
        lambda: REGISTER_TIME,
    )


def _speaker(entity_id: str, role: str, x_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=role,
        speaker_role=role,
        position=Position3(x_m=x_m, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )


def _fixture(tmp_path: Path, *, as_built_deviation: bool = False):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
            entities=(
                _speaker('fl', 'FL', 1.0),
                SceneEntity(
                    entity_id='mlp',
                    kind='measurement_point',
                    name='MLP',
                    position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
                ),
            ),
        ),
        parent_revision_id=None,
    ).revision
    variants = CadSystemVariantRepository(scene)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed surrounds',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-sl',
                entity=_speaker('sl', 'SL', 0.7),
                role_binding_id='SL',
            ),
        ),
        created_at_utc='2026-09-20T00:00:00+00:00',
    )
    variants.save_variant(variant)
    application = variants.apply_variant(
        variant.variant_id,
        selected_by='fixture',
        selected_at_utc='2026-09-20T00:01:00+00:00',
    )
    applied = scene.get(application.applied_revision_id)
    assert applied is not None
    as_built_revision = applied
    if as_built_deviation:
        changed = applied.document.model_copy(
            update={
                'entities': tuple(
                    entity.model_copy(
                        update={
                            'position': entity.position.model_copy(
                                update={'x_m': 0.75}
                            )
                        }
                    )
                    if entity.entity_id == 'sl'
                    else entity
                    for entity in applied.document.entities
                )
            }
        )
        as_built_revision = scene.save(
            changed,
            parent_revision_id=applied.revision_id,
        ).revision

    lifecycle = CadSystemVariantLifecycleRepository(
        scene_repository=scene,
        variant_repository=variants,
    )
    as_built = build_system_variant_as_built_record(
        scene_repository=scene,
        variant_repository=variants,
        application=application,
        variant=variant,
        as_built_revision=as_built_revision,
        confirmed_by='installer',
        confirmed_at_utc='2026-09-20T00:02:00+00:00',
    )
    lifecycle.save(as_built)
    measurements = CadMeasurementRepository(scene)
    quality = CadMeasurementQualityRepository(measurements)
    measured = CadSystemVariantMeasuredLifecycleRepository(
        scene_repository=scene,
        lifecycle_repository=lifecycle,
        measurement_repository=measurements,
        quality_repository=quality,
    )
    campaigns = CadSystemVariantMeasurementCampaignRepository(
        scene_repository=scene,
        variant_repository=variants,
        lifecycle_repository=lifecycle,
        measurement_repository=measurements,
        quality_repository=quality,
        measured_lifecycle_repository=measured,
    )
    return {
        'scene': scene,
        'variants': variants,
        'variant': variant,
        'application': application,
        'applied': applied,
        'as_built_revision': as_built_revision,
        'lifecycle': lifecycle,
        'as_built': as_built,
        'measurements': measurements,
        'quality': quality,
        'measured': measured,
        'campaigns': campaigns,
    }


def _target(fx, **updates):
    base = SystemVariantMeasurementTarget(
        target_id='sl-at-mlp',
        measurement_point_entity_id='mlp',
        measurement_position=fx['as_built_revision'].document.entity('mlp').position,
        source_entity_ids=('sl',),
        channel_role='SL',
        observable='magnitude_response',
        acquisition=VariantMeasurementAcquisitionRequirement(require_context=True),
        expected_measurement_count=1,
        validation_purpose='variant measured lifecycle',
    )
    return base.model_copy(update=updates)


def _plan_and_campaign(fx, target=None):
    plan = build_system_variant_measurement_plan(
        scene_repository=fx['scene'],
        variant_repository=fx['variants'],
        lifecycle_repository=fx['lifecycle'],
        as_built_record=fx['as_built'],
        targets=(target or _target(fx),),
        created_at_utc=PLAN_TIME,
        purpose='measure installed proposal',
    )
    fx['campaigns'].save_plan(plan)
    campaign = build_system_variant_measurement_campaign(
        plans=(plan,),
        purpose='SystemVariant measurement campaign',
        preregistered_at_utc=CAMPAIGN_TIME,
    )
    registration = fx['campaigns'].save_campaign(campaign)
    return plan, campaign, registration


def _save_evidence(
    fx,
    *,
    measurement_id='measure-sl',
    captured_at=CAPTURE_TIME,
    channel_role='SL',
    source_speaker_ids=('sl',),
    point_entity_id='mlp',
    acquisition=True,
    phase=False,
):
    processing = {'fixture_raw': f'raw:{measurement_id}'}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.5),
        phase_deg=(0.0, 5.0, 8.0) if phase else None,
        phase_status='valid' if phase else 'absent',
        processing=processing,
    )
    record = measurement_record_for_revision(
        fx['as_built_revision'],
        point_entity_id,
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role=channel_role,
        source_speaker_ids=source_speaker_ids,
        radiation_scope='single',
        routing_evidence='verified',
        captured_at=captured_at,
        imported_at=CAPTURE_TIME,
        source_kind='unknown',
        external_source_id=f'rew:{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset:{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.5),
        phase_deg=(0.0, 5.0, 8.0) if phase else None,
        phase_status='valid' if phase else 'absent',
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    fx['measurements'].save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    context = (
        CadAcquisitionContextBinding(
            acquisition_context_id='acq:fixture',
            acquisition_context_sha256=sha256(b'acq:fixture').hexdigest(),
            source_kind='native',
        )
        if acquisition
        else None
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(),
        acquisition_context=context,
        report_id=f'report:{measurement_id}',
        created_at_utc=CAPTURE_TIME,
    )
    fx['quality'].save_report(report)
    return record, dataset, report


def test_plan_binds_exact_variant_application_and_as_built_and_preserves_deviation(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, as_built_deviation=True)
    plan, _, _ = _plan_and_campaign(fx)

    assert plan.variant_id == fx['variant'].variant_id
    assert plan.variant_sha256 == fx['variant'].variant_sha256
    assert plan.application_id == fx['application'].application_id
    assert plan.applied_revision_id == fx['applied'].revision_id
    assert plan.as_built_revision_id == fx['as_built_revision'].revision_id
    assert plan.applied_revision_id != plan.as_built_revision_id
    assert fx['applied'].document.entity('sl').position.x_m == pytest.approx(0.7)
    assert fx['as_built_revision'].document.entity('sl').position.x_m == pytest.approx(0.75)


@pytest.mark.parametrize(
    ('target_update', 'match'),
    [
        (
            {'measurement_position': Position3(x_m=3.1, y_m=3.0, z_m=1.1)},
            'exact AsBuilt position',
        ),
        (
            {'source_entity_ids': ('missing-speaker',)},
            'source entity is absent',
        ),
    ],
)
def test_plan_creation_fails_closed_on_wrong_exact_target(
    tmp_path: Path,
    target_update,
    match,
) -> None:
    fx = _fixture(tmp_path)
    with pytest.raises(ValueError, match=match):
        build_system_variant_measurement_plan(
            scene_repository=fx['scene'],
            variant_repository=fx['variants'],
            lifecycle_repository=fx['lifecycle'],
            as_built_record=fx['as_built'],
            targets=(_target(fx, **target_update),),
            created_at_utc=PLAN_TIME,
        )


@pytest.mark.parametrize(
    ('evidence_kwargs', 'match'),
    [
        ({'channel_role': 'SR'}, 'wrong channel role'),
        ({'source_speaker_ids': ('fl',)}, 'wrong source entity role'),
        ({'captured_at': '2026-09-20T00:03:30+00:00'}, 'pre-registration'),
        ({'acquisition': False}, 'requires acquisition context'),
    ],
)
def test_campaign_exact_matching_rejects_wrong_or_incomplete_evidence(
    tmp_path: Path,
    evidence_kwargs,
    match,
) -> None:
    fx = _fixture(tmp_path)
    plan, campaign, registration = _plan_and_campaign(fx)
    _save_evidence(fx, **evidence_kwargs)

    with pytest.raises(ValueError, match=match):
        complete_system_variant_measurement_plan(
            campaign=campaign,
            registration=registration,
            plan=plan,
            assignments={plan.targets[0].target_id: ('measure-sl',)},
            measurement_repository=fx['measurements'],
            quality_repository=fx['quality'],
            completed_at_utc=COMPLETE_TIME,
        )


def test_insufficient_quality_capability_rejected(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    target = _target(fx, observable='phase_response')
    plan, campaign, registration = _plan_and_campaign(fx, target)
    _save_evidence(fx, phase=False)

    with pytest.raises(ValueError, match='quality capability is insufficient'):
        complete_system_variant_measurement_plan(
            campaign=campaign,
            registration=registration,
            plan=plan,
            assignments={target.target_id: ('measure-sl',)},
            measurement_repository=fx['measurements'],
            quality_repository=fx['quality'],
            completed_at_utc=COMPLETE_TIME,
        )


def test_generic_measurement_does_not_promote_variant_without_campaign_completion(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    _save_evidence(fx)

    assert fx['measured'].list_for_as_built(fx['as_built'].record_id) == ()
    assert fx['campaigns'].get_campaign_completion('missing') is None


def test_campaign_completion_promotes_exact_measured_lifecycle_and_reopens(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    plan, campaign, registration = _plan_and_campaign(fx)
    measurement, _, _ = _save_evidence(fx)

    completion, plan_completions, measured = (
        complete_system_variant_measurement_campaign(
            scene_repository=fx['scene'],
            lifecycle_repository=fx['lifecycle'],
            measured_lifecycle_repository=fx['measured'],
            campaign=campaign,
            registration=registration,
            plans=(plan,),
            assignments_by_plan={
                plan.plan_id: {
                    plan.targets[0].target_id: (measurement.measurement_id,)
                }
            },
            measurement_repository=fx['measurements'],
            quality_repository=fx['quality'],
            completed_at_utc=COMPLETE_TIME,
        )
    )
    for item in plan_completions:
        fx['campaigns'].save_plan_completion(item)
    fx['campaigns'].save_campaign_completion(completion)

    assert measured.variant_id == fx['variant'].variant_id
    states = {item.entity_id: item.state for item in measured.entity_lifecycle}
    assert states['sl'] == 'measured'
    assert completion.measured_record_id == measured.record_id
    assert completion.campaign_registration_id == registration.registration_id
    assert (
        completion.campaign_registration_sha256 == registration.registration_sha256
    )
    for item in plan_completions:
        assert item.campaign_registration_id == registration.registration_id
        assert (
            item.campaign_registration_sha256
            == registration.registration_sha256
        )

    reopened_scene = SceneRepository(fx['scene'].path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_lifecycle = CadSystemVariantLifecycleRepository(
        scene_repository=reopened_scene,
        variant_repository=reopened_variants,
    )
    reopened_measurements = CadMeasurementRepository(reopened_scene)
    reopened_quality = CadMeasurementQualityRepository(reopened_measurements)
    reopened_measured = CadSystemVariantMeasuredLifecycleRepository(
        scene_repository=reopened_scene,
        lifecycle_repository=reopened_lifecycle,
        measurement_repository=reopened_measurements,
        quality_repository=reopened_quality,
    )
    reopened_campaigns = CadSystemVariantMeasurementCampaignRepository(
        scene_repository=reopened_scene,
        variant_repository=reopened_variants,
        lifecycle_repository=reopened_lifecycle,
        measurement_repository=reopened_measurements,
        quality_repository=reopened_quality,
        measured_lifecycle_repository=reopened_measured,
    )

    assert reopened_campaigns.get_plan(plan.plan_id) == plan
    assert reopened_campaigns.get_campaign(campaign.campaign_id) == campaign
    assert (
        reopened_campaigns.get_plan_completion(plan_completions[0].completion_id)
        == plan_completions[0]
    )
    assert reopened_campaigns.get_campaign_completion(campaign.campaign_id) == completion
    assert (
        reopened_campaigns.get_campaign_registration(campaign.campaign_id)
        == registration
    )


def test_old_plan_and_campaign_are_immutable_when_new_target_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _fixture(tmp_path)
    first_plan, first_campaign, first_registration = _plan_and_campaign(fx)
    second_target = _target(
        fx,
        target_id='sl-at-mlp-repeat',
        expected_measurement_count=2,
        repeatability_required=True,
    )
    second_plan = build_system_variant_measurement_plan(
        scene_repository=fx['scene'],
        variant_repository=fx['variants'],
        lifecycle_repository=fx['lifecycle'],
        as_built_record=fx['as_built'],
        targets=(second_target,),
        created_at_utc='2026-09-20T00:07:00+00:00',
        purpose='new campaign after target change',
    )
    fx['campaigns'].save_plan(second_plan)
    second_campaign = build_system_variant_measurement_campaign(
        plans=(second_plan,),
        purpose='new target requires new campaign',
        preregistered_at_utc='2026-09-20T00:07:30+00:00',
    )
    monkeypatch.setattr(
        campaign_module, '_utc_now', lambda: '2026-09-20T00:08:00+00:00'
    )
    second_registration = fx['campaigns'].save_campaign(second_campaign)

    assert second_plan.plan_id != first_plan.plan_id
    assert second_campaign.campaign_id != first_campaign.campaign_id
    assert second_registration.campaign_id == second_campaign.campaign_id
    assert second_registration.registered_at_utc == '2026-09-20T00:08:00+00:00'
    assert (
        second_registration.registration_id != first_registration.registration_id
    )
    assert fx['campaigns'].get_plan(first_plan.plan_id) == first_plan
    assert fx['campaigns'].get_campaign(first_campaign.campaign_id) == first_campaign
    assert (
        fx['campaigns'].get_campaign_registration(first_campaign.campaign_id)
        == first_registration
    )


def _plan_only(fx):
    plan = build_system_variant_measurement_plan(
        scene_repository=fx['scene'],
        variant_repository=fx['variants'],
        lifecycle_repository=fx['lifecycle'],
        as_built_record=fx['as_built'],
        targets=(_target(fx),),
        created_at_utc=PLAN_TIME,
        purpose='measure installed proposal',
    )
    fx['campaigns'].save_plan(plan)
    return plan


def test_campaign_commit_time_is_repository_attested_not_caller_supplied(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    plan = _plan_only(fx)
    # Caller backdates the claimed preregistration as far as the plans allow.
    campaign = build_system_variant_measurement_campaign(
        plans=(plan,),
        purpose='backdated claim stays planning metadata',
        preregistered_at_utc='2026-09-20T00:03:30+00:00',
    )

    registration = fx['campaigns'].save_campaign(campaign)

    assert registration.campaign_id == campaign.campaign_id
    assert registration.campaign_sha256 == campaign.campaign_sha256
    assert registration.registered_at_utc == REGISTER_TIME
    assert registration.registered_at_utc != campaign.preregistered_at_utc
    assert (
        fx['campaigns'].get_campaign_registration(campaign.campaign_id)
        == registration
    )
    # The caller claim remains hashed campaign metadata; the durable fact did
    # not move.
    persisted = fx['campaigns'].get_campaign(campaign.campaign_id)
    assert persisted.preregistered_at_utc == '2026-09-20T00:03:30+00:00'


def test_campaign_commit_time_is_not_rewriteable_by_resave(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    plan, campaign, registration = _plan_and_campaign(fx)

    assert fx['campaigns'].save_campaign(campaign) == registration
    assert registration.registered_at_utc == REGISTER_TIME


def test_retrospective_campaign_registration_rejected_when_evidence_exists(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    plan = _plan_only(fx)
    # Qualifying measured evidence is committed before the campaign is saved.
    _save_evidence(fx)
    campaign = build_system_variant_measurement_campaign(
        plans=(plan,),
        purpose='retrospective campaign',
        preregistered_at_utc=CAMPAIGN_TIME,
    )

    with pytest.raises(ValueError, match='qualifying measurement evidence'):
        fx['campaigns'].save_campaign(campaign)

    assert fx['campaigns'].get_campaign(campaign.campaign_id) is None
    assert (
        fx['campaigns'].get_campaign_registration(campaign.campaign_id) is None
    )


def test_unrelated_measurement_does_not_block_campaign_registration(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    plan = _plan_only(fx)
    # Evidence for a different channel role cannot satisfy a preregistered
    # target, so it is not qualifying evidence for this campaign.
    _save_evidence(fx, measurement_id='measure-sr', channel_role='SR')
    campaign = build_system_variant_measurement_campaign(
        plans=(plan,),
        purpose='unrelated evidence does not block registration',
        preregistered_at_utc=CAMPAIGN_TIME,
    )

    registration = fx['campaigns'].save_campaign(campaign)
    assert registration.registered_at_utc == REGISTER_TIME


def test_campaign_claimed_preregistration_cannot_postdate_commit(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    plan = _plan_only(fx)
    campaign = build_system_variant_measurement_campaign(
        plans=(plan,),
        purpose='claim after durable commit',
        preregistered_at_utc='2026-09-20T00:04:45+00:00',
    )

    with pytest.raises(ValueError, match='cannot postdate durable registration'):
        fx['campaigns'].save_campaign(campaign)


def test_evidence_captured_before_durable_registration_cannot_satisfy_target(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    plan, campaign, registration = _plan_and_campaign(fx)
    # Captured after the caller-claimed preregistered_at_utc but before the
    # repository-attested registered_at_utc: only the durable authority gates.
    _save_evidence(fx, captured_at='2026-09-20T00:04:15+00:00')

    with pytest.raises(ValueError, match='pre-registration'):
        complete_system_variant_measurement_plan(
            campaign=campaign,
            registration=registration,
            plan=plan,
            assignments={plan.targets[0].target_id: ('measure-sl',)},
            measurement_repository=fx['measurements'],
            quality_repository=fx['quality'],
            completed_at_utc=COMPLETE_TIME,
        )


def test_completion_rejects_foreign_registration_authority(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    plan, campaign, registration = _plan_and_campaign(fx)
    _save_evidence(fx)
    other_plan = build_system_variant_measurement_plan(
        scene_repository=fx['scene'],
        variant_repository=fx['variants'],
        lifecycle_repository=fx['lifecycle'],
        as_built_record=fx['as_built'],
        targets=(
            _target(
                fx,
                target_id='sl-at-mlp-repeat',
                expected_measurement_count=2,
                repeatability_required=True,
            ),
        ),
        created_at_utc='2026-09-20T00:03:30+00:00',
        purpose='other plan',
    )
    other_campaign = build_system_variant_measurement_campaign(
        plans=(other_plan,),
        purpose='other campaign',
        preregistered_at_utc=CAMPAIGN_TIME,
    )
    foreign = build_system_variant_measurement_campaign_registration(
        campaign=other_campaign,
        registered_at_utc=REGISTER_TIME,
    )

    with pytest.raises(ValueError, match='registration authority mismatch'):
        complete_system_variant_measurement_plan(
            campaign=campaign,
            registration=foreign,
            plan=plan,
            assignments={plan.targets[0].target_id: ('measure-sl',)},
            measurement_repository=fx['measurements'],
            quality_repository=fx['quality'],
            completed_at_utc=COMPLETE_TIME,
        )


def test_plan_completion_requires_persisted_registration(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    plan = _plan_only(fx)
    campaign = build_system_variant_measurement_campaign(
        plans=(plan,),
        purpose='campaign persisted without durable registration',
        preregistered_at_utc=CAMPAIGN_TIME,
    )
    # Simulate a pre-authority row: campaign persisted with no registration.
    with closing(sqlite3.connect(fx['scene'].path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO cad_system_variant_measurement_campaigns(
                campaign_id, campaign_sha256, variant_id, as_built_record_id,
                payload_json, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                campaign.campaign_id,
                campaign.campaign_sha256,
                campaign.variant_id,
                campaign.as_built_record_id,
                campaign.model_dump_json(),
                campaign.preregistered_at_utc,
            ),
        )
    _save_evidence(fx)
    registration = build_system_variant_measurement_campaign_registration(
        campaign=campaign,
        registered_at_utc=REGISTER_TIME,
    )
    completion, _ = complete_system_variant_measurement_plan(
        campaign=campaign,
        registration=registration,
        plan=plan,
        assignments={plan.targets[0].target_id: ('measure-sl',)},
        measurement_repository=fx['measurements'],
        quality_repository=fx['quality'],
        completed_at_utc=COMPLETE_TIME,
    )

    # No historical registration time is silently inferred.
    assert fx['campaigns'].get_campaign_registration(campaign.campaign_id) is None
    with pytest.raises(ValueError, match='registration authority missing/stale'):
        fx['campaigns'].save_plan_completion(completion)
    with pytest.raises(ValueError, match='registration authority missing/stale'):
        fx['campaigns'].save_campaign(campaign)
