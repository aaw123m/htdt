from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

import htdt.cad_system_variant_measurement_campaign as campaign_module
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    acquisition_context_binding,
    build_acquisition_context,
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
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_lifecycle import (
    CadSystemVariantLifecycleRepository,
    build_system_variant_as_built_record,
)
from htdt.cad_system_variant_measured_lifecycle import (
    CadSystemVariantMeasuredLifecycleRepository,
    build_system_variant_measured_record,
)
from htdt.cad_system_variant_measurement_campaign import (
    CadSystemVariantMeasurementCampaignRepository,
    SystemVariantMeasurementTarget,
    VariantMeasurementAcquisitionRequirement,
    build_system_variant_measurement_campaign,
    build_system_variant_measurement_plan,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


DOCUMENT_ID = 'o100g-measured-lifecycle-fixture'
NOW = '2026-09-20T00:00:00+00:00'
PLAN_TIME = '2026-09-20T00:02:30+00:00'
CAMPAIGN_TIME = '2026-09-20T00:03:00+00:00'
REGISTER_TIME = '2026-09-20T00:03:30+00:00'
CAPTURE_TIME = '2026-09-20T00:04:30+00:00'
COMPLETE_TIME = '2026-09-20T00:05:00+00:00'


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


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
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

    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=baseline,
        name='Proposed surrounds',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
            ChannelRoleBinding(role_id='SR', display_name='SR'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-sl',
                entity=_speaker('sl', 'SL', 0.7),
                role_binding_id='SL',
            ),
            ProposedEntitySpec(
                spec_id='proposal-sr',
                entity=_speaker('sr', 'SR', 5.3),
                role_binding_id='SR',
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    application = variant_repository.apply_variant(
        variant.variant_id,
        selected_by='fixture',
        selected_at_utc='2026-09-20T00:01:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)
    assert applied is not None

    lifecycle_repository = CadSystemVariantLifecycleRepository(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
    )
    as_built = build_system_variant_as_built_record(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
        application=application,
        variant=variant,
        as_built_revision=applied,
        confirmed_by='installer',
        confirmed_at_utc='2026-09-20T00:02:00+00:00',
    )
    lifecycle_repository.save(as_built)

    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(
        measurement_repository
    )
    measured = CadSystemVariantMeasuredLifecycleRepository(
        scene_repository=scene_repository,
        lifecycle_repository=lifecycle_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    campaigns = CadSystemVariantMeasurementCampaignRepository(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
        lifecycle_repository=lifecycle_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        measured_lifecycle_repository=measured,
    )

    # The preregistered campaign is the only measured-promotion authority:
    # its exact target/acquisition requirements are committed before any
    # qualifying measurement evidence may exist.
    target = SystemVariantMeasurementTarget(
        target_id='sl-at-mlp',
        measurement_point_entity_id='mlp',
        measurement_position=applied.document.entity('mlp').position,
        source_entity_ids=('sl',),
        channel_role='SL',
        observable='magnitude_response',
        acquisition=VariantMeasurementAcquisitionRequirement(
            require_context=True
        ),
        expected_measurement_count=1,
        validation_purpose='variant measured lifecycle',
    )
    plan = build_system_variant_measurement_plan(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
        lifecycle_repository=lifecycle_repository,
        as_built_record=as_built,
        targets=(target,),
        created_at_utc=PLAN_TIME,
        purpose='measure installed proposal',
    )
    campaigns.save_plan(plan)
    campaign = build_system_variant_measurement_campaign(
        plans=(plan,),
        purpose='SystemVariant measurement campaign',
        preregistered_at_utc=CAMPAIGN_TIME,
    )
    monkeypatch.setattr(
        campaign_module, '_utc_now', lambda: REGISTER_TIME
    )
    registration = campaigns.save_campaign(campaign)

    return {
        'scene_repository': scene_repository,
        'variant_repository': variant_repository,
        'lifecycle_repository': lifecycle_repository,
        'measurement_repository': measurement_repository,
        'quality_repository': quality_repository,
        'measured': measured,
        'campaigns': campaigns,
        'applied': applied,
        'as_built': as_built,
        'target': target,
        'plan': plan,
        'campaign': campaign,
        'registration': registration,
    }


def _campaign_refs(fx) -> dict[str, str]:
    """The exact campaign authority a measured record must bind."""
    return {
        'campaign_id': fx['campaign'].campaign_id,
        'campaign_sha256': fx['campaign'].campaign_sha256,
        'campaign_registration_id': fx['registration'].registration_id,
        'campaign_registration_sha256': fx['registration'].registration_sha256,
    }


def _save_measurement(
    fx,
    *,
    revision,
    measurement_id: str,
    source_speaker_ids: tuple[str, ...],
    evidence_type: str = 'measured',
    with_acquisition_context: bool = True,
    captured_at: str = CAPTURE_TIME,
):
    measurement_repository = fx['measurement_repository']
    quality_repository = fx['quality_repository']
    processing = {'fixture_raw': f'raw:{measurement_id}'}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.5),
        phase_status='absent',
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        'mlp',
        measurement_id=measurement_id,
        evidence_type=evidence_type,
        channel_role='SL',
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
        phase_status='absent',
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    acquisition = None
    if with_acquisition_context:
        # The binding must resolve to a persisted acquisition-context
        # authority covering this measurement (#392); an all-None timing
        # attestation keeps the report's empty evidence verbatim.
        context = build_acquisition_context(
            acquisition_context_id=f'acq:{measurement_id}',
            source_kind='native',
            subject_measurement_ids=(measurement_id,),
            created_at_utc=CAPTURE_TIME,
        )
        quality_repository.save_acquisition_context(context)
        acquisition = acquisition_context_binding(context)
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(),
        acquisition_context=acquisition,
        report_id=f'report:{measurement_id}',
        created_at_utc=CAPTURE_TIME,
    )
    quality_repository.save_report(report)
    return record, dataset, report


def test_measured_record_promotes_only_explicit_proposed_source_speaker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    measurement, dataset, report = _save_measurement(
        fx,
        revision=fx['applied'],
        measurement_id='measure-sl',
        source_speaker_ids=('sl',),
    )

    completion, _, record = fx['campaigns'].complete_campaign(
        campaign=fx['campaign'],
        assignments_by_plan={
            fx['plan'].plan_id: {
                fx['target'].target_id: (measurement.measurement_id,)
            }
        },
        completed_at_utc=COMPLETE_TIME,
    )

    # The measured record binds the exact preregistered campaign and its
    # durable registration, so its promotion provenance is re-resolvable.
    assert record.campaign_id == fx['campaign'].campaign_id
    assert record.campaign_sha256 == fx['campaign'].campaign_sha256
    assert record.campaign_registration_id == (
        fx['registration'].registration_id
    )
    assert record.campaign_registration_sha256 == (
        fx['registration'].registration_sha256
    )
    assert completion.measured_record_id == record.record_id
    assert completion.measured_record_sha256 == record.record_sha256

    # The same record reproduces from the builder given the campaign
    # authority refs.
    rebuilt = build_system_variant_measured_record(
        scene_repository=fx['scene_repository'],
        as_built_record=fx['as_built'],
        evidence=((measurement, dataset, report),),
        **_campaign_refs(fx),
        bound_at_utc=COMPLETE_TIME,
    )
    assert rebuilt == record

    lifecycle = {item.entity_id: item for item in record.entity_lifecycle}
    assert lifecycle['sl'].state == 'measured'
    assert lifecycle['sl'].measurement_ids == ('measure-sl',)
    assert lifecycle['sr'].state == 'as_built'
    assert lifecycle['sr'].measurement_ids == ()
    assert report.capability('magnitude_response').decision == 'ALLOWED'
    assert report.retake_recommendation == 'UNKNOWN'
    assert record.measurements[0].allowed_capability_claims == (
        'magnitude_response',
    )
    assert record.measurements[0].acquisition_context_id == 'acq:measure-sl'

    reopened_scene = SceneRepository(fx['scene_repository'].path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_lifecycle = CadSystemVariantLifecycleRepository(
        scene_repository=reopened_scene,
        variant_repository=reopened_variants,
    )
    reopened_measurements = CadMeasurementRepository(reopened_scene)
    reopened_quality = CadMeasurementQualityRepository(
        reopened_measurements
    )
    reopened = CadSystemVariantMeasuredLifecycleRepository(
        scene_repository=reopened_scene,
        lifecycle_repository=reopened_lifecycle,
        measurement_repository=reopened_measurements,
        quality_repository=reopened_quality,
    )
    assert reopened.get(record.record_id) == record
    assert reopened.list_for_as_built(fx['as_built'].record_id) == (record,)


def test_measured_record_requires_exact_as_built_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    descendant_document = fx['applied'].document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={
                        'position': entity.position.model_copy(
                            update={'x_m': entity.position.x_m + 0.01}
                        )
                    }
                )
                if entity.entity_id == 'mlp'
                else entity
                for entity in fx['applied'].document.entities
            )
        }
    )
    descendant = fx['scene_repository'].save(
        descendant_document,
        parent_revision_id=fx['applied'].revision_id,
    ).revision
    assert descendant.revision_id != fx['applied'].revision_id
    measurement, dataset, report = _save_measurement(
        fx,
        revision=descendant,
        measurement_id='measure-descendant',
        source_speaker_ids=('sl',),
    )

    with pytest.raises(
        ValueError,
        match='must bind exact as-built revision',
    ):
        build_system_variant_measured_record(
            scene_repository=fx['scene_repository'],
            as_built_record=fx['as_built'],
            evidence=((measurement, dataset, report),),
            **_campaign_refs(fx),
            bound_at_utc=COMPLETE_TIME,
        )


def test_measured_record_rejects_derived_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    measurement, dataset, report = _save_measurement(
        fx,
        revision=fx['applied'],
        measurement_id='derived-sl',
        source_speaker_ids=('sl',),
        evidence_type='derived',
    )

    with pytest.raises(
        ValueError,
        match='requires measured evidence',
    ):
        build_system_variant_measured_record(
            scene_repository=fx['scene_repository'],
            as_built_record=fx['as_built'],
            evidence=((measurement, dataset, report),),
            **_campaign_refs(fx),
            bound_at_utc=COMPLETE_TIME,
        )


def test_measured_record_can_bind_configuration_measurement_without_promoting_entity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    measurement, dataset, report = _save_measurement(
        fx,
        revision=fx['applied'],
        measurement_id='measure-fl',
        source_speaker_ids=('fl',),
    )

    record = build_system_variant_measured_record(
        scene_repository=fx['scene_repository'],
        as_built_record=fx['as_built'],
        evidence=((measurement, dataset, report),),
        **_campaign_refs(fx),
        bound_at_utc=COMPLETE_TIME,
    )

    assert {item.state for item in record.entity_lifecycle} == {'as_built'}
    assert {item.entity_id for item in record.entity_lifecycle} == {'sl', 'sr'}


def test_measured_record_requires_explicit_acquisition_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    measurement, dataset, report = _save_measurement(
        fx,
        revision=fx['applied'],
        measurement_id='measure-no-acq',
        source_speaker_ids=('sl',),
        with_acquisition_context=False,
    )

    with pytest.raises(
        ValueError,
        match='requires explicit acquisition context',
    ):
        build_system_variant_measured_record(
            scene_repository=fx['scene_repository'],
            as_built_record=fx['as_built'],
            evidence=((measurement, dataset, report),),
            **_campaign_refs(fx),
            bound_at_utc=COMPLETE_TIME,
        )


def test_measured_record_validation_requires_persisted_campaign_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record bound to campaign refs that were never persisted cannot
    validate: the preregistered campaign is the promotion authority."""
    fx = _fixture(tmp_path, monkeypatch)
    measurement, dataset, report = _save_measurement(
        fx,
        revision=fx['applied'],
        measurement_id='measure-sl',
        source_speaker_ids=('sl',),
    )
    record = build_system_variant_measured_record(
        scene_repository=fx['scene_repository'],
        as_built_record=fx['as_built'],
        evidence=((measurement, dataset, report),),
        campaign_id='system-variant-measurement-campaign:' + '0' * 64,
        campaign_sha256='0' * 64,
        campaign_registration_id=(
            'system-variant-measurement-campaign-registration:' + '0' * 64
        ),
        campaign_registration_sha256='0' * 64,
        bound_at_utc=COMPLETE_TIME,
    )

    with pytest.raises(ValueError, match='campaign authority missing'):
        fx['measured']._validate(record)


def test_measured_record_cannot_persist_without_campaign_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct measured-lifecycle persistence fails closed.

    There is no public save path and the internal write primitive refuses
    to commit while the bound campaign's completion is not in the same
    transaction.
    """
    fx = _fixture(tmp_path, monkeypatch)
    measurement, dataset, report = _save_measurement(
        fx,
        revision=fx['applied'],
        measurement_id='measure-sl',
        source_speaker_ids=('sl',),
    )
    record = build_system_variant_measured_record(
        scene_repository=fx['scene_repository'],
        as_built_record=fx['as_built'],
        evidence=((measurement, dataset, report),),
        **_campaign_refs(fx),
        bound_at_utc=COMPLETE_TIME,
    )

    assert not hasattr(fx['measured'], 'save')
    with closing(
        sqlite3.connect(fx['scene_repository'].path)
    ) as connection:
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(
            ValueError, match='campaign completion authority'
        ):
            fx['measured']._save_in_transaction(connection, record)
        connection.rollback()

    assert fx['measured'].get(record.record_id) is None
    assert fx['measured'].list_for_as_built(fx['as_built'].record_id) == ()


def test_persisted_measured_record_requires_completion_provenance_on_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-side fail-closed: a measured row without its persisted campaign
    completion cannot prove why the entities entered measured state."""
    fx = _fixture(tmp_path, monkeypatch)
    measurement, _, _ = _save_measurement(
        fx,
        revision=fx['applied'],
        measurement_id='measure-sl',
        source_speaker_ids=('sl',),
    )
    _, _, record = fx['campaigns'].complete_campaign(
        campaign=fx['campaign'],
        assignments_by_plan={
            fx['plan'].plan_id: {
                fx['target'].target_id: (measurement.measurement_id,)
            }
        },
        completed_at_utc=COMPLETE_TIME,
    )
    assert fx['measured'].get(record.record_id) == record

    # Simulate a pre-authority/corrupt durable state: the measured row lost
    # its campaign completion authority.
    with closing(
        sqlite3.connect(fx['scene_repository'].path)
    ) as connection, connection:
        connection.execute(
            'DELETE FROM cad_system_variant_measurement_campaign_completions'
        )

    with pytest.raises(ValueError, match='campaign completion authority'):
        fx['measured'].get(record.record_id)
    with pytest.raises(ValueError, match='campaign completion authority'):
        fx['measured'].list_for_as_built(fx['as_built'].record_id)
