"""Canonical effective-measurement resolver (#839/#844).

Disposition gates every normal-use consumer; the pinned correction supplies
the effective semantic binding; the immutable record stays intact.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_disposition import (
    build_measurement_correction,
    build_measurement_disposition,
)
from htdt.cad_measurement_effective import CadEffectiveMeasurementResolver
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id='doc-effective',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='Front Left',
                speaker_role='FL',
                position=Position3(x_m=1.35, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
            SceneEntity(
                entity_id='point-right',
                kind='measurement_point',
                name='Right seat',
                position=Position3(x_m=4.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _setup(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    return revision, measurement_repository, quality_repository


def _save_measurement(
    repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
):
    declared_raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        processing={'fixture': measurement_id},
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        processing_json=canonical_json({'fixture': measurement_id}),
        source_sha256=sha256(declared_raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=declared_raw,
    )
    return record, dataset


def test_resolve_defaults_to_eligible(tmp_path: Path):
    revision, mrepo, qrepo = _setup(tmp_path)
    record, _ = _save_measurement(mrepo, revision, 'm-1')
    resolver = CadEffectiveMeasurementResolver(mrepo, qrepo)
    evidence = resolver.require_normal_use('m-1', purpose='test')
    assert evidence.is_normally_eligible
    assert evidence.is_selected_head
    assert evidence.channel_role == 'front_left'
    assert evidence.measurement_entity_id == 'point-mlp'


def test_excluded_disposition_fails_closed(tmp_path: Path):
    revision, mrepo, qrepo = _setup(tmp_path)
    record, _ = _save_measurement(mrepo, revision, 'm-excl')
    resolver = CadEffectiveMeasurementResolver(mrepo, qrepo)
    qrepo.save_disposition(
        build_measurement_disposition(
            document_id=revision.document_id,
            measurement_id='m-excl',
            disposition='excluded_from_normal_use',
            reason='test-sweep residue',
        )
    )
    with pytest.raises(ValueError, match='excluded_from_normal_use'):
        resolver.require_normal_use('m-excl', purpose='test')
    # The immutable record itself stays readable — history is never rewritten.
    assert mrepo.get_measurement('m-excl') is not None


def test_correction_overlays_effective_binding(tmp_path: Path):
    revision, mrepo, qrepo = _setup(tmp_path)
    record, dataset = _save_measurement(mrepo, revision, 'm-corr')
    resolver = CadEffectiveMeasurementResolver(mrepo, qrepo)
    correction = build_measurement_correction(
        document_id=revision.document_id,
        measurement_id='m-corr',
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        reason='mic was at the right seat',
        measurement_entity_id='point-right',
        channel_role='front_right',
    )
    qrepo.save_correction(correction)
    qrepo.save_disposition(
        build_measurement_disposition(
            document_id=revision.document_id,
            measurement_id='m-corr',
            disposition='corrected',
            reason='seat corrected',
            correction_id=correction.correction_id,
        )
    )
    evidence = resolver.require_normal_use('m-corr', purpose='test')
    assert evidence.is_normally_eligible
    assert evidence.measurement_entity_id == 'point-right'
    assert evidence.channel_role == 'front_right'
    # Identity fields never drift: the original record is untouched.
    assert evidence.measurement.channel_role == 'front_left'
    assert evidence.measurement.measurement_entity_id == 'point-mlp'


def test_unknown_measurement_rejected(tmp_path: Path):
    _, mrepo, qrepo = _setup(tmp_path)
    resolver = CadEffectiveMeasurementResolver(mrepo, qrepo)
    with pytest.raises(ValueError, match='does not exist'):
        resolver.require_normal_use('ghost', purpose='test')
