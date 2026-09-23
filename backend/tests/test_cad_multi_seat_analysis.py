from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_multi_seat_analysis import (
    MULTI_SEAT_ALGORITHM_SHA256,
    MultiSeatAnalysisResult,
    MultiSeatMember,
    build_multi_seat_set,
    replay_multi_seat_analysis,
    run_multi_seat_analysis,
)
from htdt.cad_multi_seat_analysis_repository import CadMultiSeatAnalysisRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.comparison import FrequencyResponse


def _three_seat_scene(document_id: str) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
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
            *tuple(
                SceneEntity(
                    entity_id=f'point-{seat}',
                    kind='measurement_point',
                    name=seat.upper(),
                    position=Position3(x_m=x, y_m=3.0, z_m=1.1),
                )
                for seat, x in (('mlp', 3.0), ('left', 2.0), ('right', 4.0))
            ),
        ),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _three_seat_scene('doc-seats'), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    return revision, measurement_repository, CadMultiSeatAnalysisRepository(
        measurement_repository
    )


def _save_measurement(
    repository: CadMeasurementRepository,
    revision,
    seat: str,
    measurement_id: str,
    level_db: tuple[float, ...],
    channel_role: str = 'front_left',
):
    declared_raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=level_db,
        phase_status='absent',
        processing={'fixture_seat': seat},
    )
    record = measurement_record_for_revision(
        revision,
        f'point-{seat}',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role=channel_role,
        source_speaker_ids=('speaker-fl',),
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=level_db,
        phase_status='absent',
        processing_json=canonical_json({'fixture_seat': seat}),
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


def _set(revision, members, **overrides):
    kwargs = {
        'document_id': revision.document_id,
        'created_at': '2026-09-23T00:00:00+00:00',
    }
    kwargs.update(overrides)
    return build_multi_seat_set(tuple(members), **kwargs)


def test_set_records_mixed_context_warnings(tmp_path: Path):
    revision, repository, _ = _repositories(tmp_path)
    m1, d1 = _save_measurement(repository, revision, 'mlp', 'm-1', (70.0, 71.0, 69.0))
    m2, d2 = _save_measurement(
        repository, revision, 'left', 'm-2', (68.0, 69.0, 67.0), channel_role='center'
    )
    analysis_set = _set(
        revision,
        (
            MultiSeatMember(
                measurement_id=m1.measurement_id,
                dataset_id=d1.dataset_id,
                dataset_sha256=d1.dataset_sha256,
                target_entity_id='point-mlp',
                seat_label='MLP',
                channel_role='front_left',
                evidence_type='measured',
                scene_revision_id=revision.revision_id,
                scene_content_hash=revision.content_hash,
                is_mlp=True,
            ),
            MultiSeatMember(
                measurement_id=m2.measurement_id,
                dataset_id=d2.dataset_id,
                dataset_sha256=d2.dataset_sha256,
                target_entity_id='point-left',
                seat_label='Left seat',
                channel_role='center',
                evidence_type='measured',
                scene_revision_id=revision.revision_id,
                scene_content_hash=revision.content_hash,
            ),
        ),
    )
    assert any(w.startswith('mixed_channel_role') for w in analysis_set.warnings)


def test_envelope_mean_spread_and_outlier(tmp_path: Path):
    revision, repository, _ = _repositories(tmp_path)
    m1, d1 = _save_measurement(repository, revision, 'mlp', 'm-1', (70.0, 72.0, 70.0))
    m2, d2 = _save_measurement(repository, revision, 'left', 'm-2', (66.0, 66.0, 66.0))
    m3, d3 = _save_measurement(repository, revision, 'right', 'm-3', (74.0, 74.0, 74.0))
    members = tuple(
        MultiSeatMember(
            measurement_id=m.measurement_id,
            dataset_id=d.dataset_id,
            dataset_sha256=d.dataset_sha256,
            target_entity_id=f'point-{seat}',
            seat_label=seat,
            channel_role='front_left',
            evidence_type='measured',
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            is_mlp=(seat == 'mlp'),
        )
        for (m, d), seat in zip(
            ((m1, d1), (m2, d2), (m3, d3)), ('mlp', 'left', 'right'), strict=True
        )
    )
    analysis_set = _set(revision, members)
    result = run_multi_seat_analysis(
        analysis_set,
        tuple(
            FrequencyResponse(d.frequency_hz, d.level_db) for d in (d1, d2, d3)
        ),
        low_hz=20.0,
        high_hz=80.0,
        central_tendency='arithmetic_mean_in_db',
        outlier_band_hz=(20.0, 80.0),
        created_at='2026-09-23T01:00:00+00:00',
    )
    assert result.grid_hz and len(result.min_db) == len(result.grid_hz)
    for i, f in enumerate(result.grid_hz):
        assert result.min_db[i] <= result.mean_db[i] <= result.max_db[i]
        assert result.spread_db[i] == pytest.approx(result.max_db[i] - result.min_db[i])
        lo = result.lower_envelope_member_indices[i]
        hi = result.upper_envelope_member_indices[i]
        assert result.member_levels_db[lo][i] == result.min_db[i]
        assert result.member_levels_db[hi][i] == result.max_db[i]
    # Left seat is flat 66 vs mean ~70 — the band outlier.
    assert result.outlier_member_index == 1


def test_replay_and_persisted_validation(tmp_path: Path):
    revision, repository, analysis_repository = _repositories(tmp_path)
    m1, d1 = _save_measurement(repository, revision, 'mlp', 'm-1', (70.0, 72.0, 70.0))
    m2, d2 = _save_measurement(repository, revision, 'left', 'm-2', (66.0, 66.0, 66.0))
    m3, d3 = _save_measurement(repository, revision, 'right', 'm-3', (74.0, 74.0, 74.0))
    members = tuple(
        MultiSeatMember(
            measurement_id=m.measurement_id,
            dataset_id=d.dataset_id,
            dataset_sha256=d.dataset_sha256,
            target_entity_id=f'point-{seat}',
            seat_label=seat,
            channel_role='front_left',
            evidence_type='measured',
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
        )
        for (m, d), seat in zip(
            ((m1, d1), (m2, d2), (m3, d3)), ('mlp', 'left', 'right'), strict=True
        )
    )
    analysis_set = _set(revision, members)
    analysis_repository.save_set(analysis_set)
    result = run_multi_seat_analysis(
        analysis_set,
        tuple(FrequencyResponse(d.frequency_hz, d.level_db) for d in (d1, d2, d3)),
        low_hz=20.0,
        high_hz=80.0,
        outlier_band_hz=(20.0, 80.0),
        created_at='2026-09-23T01:00:00+00:00',
    )
    analysis_repository.save_result(result)
    assert analysis_repository.get_set(analysis_set.set_id) == analysis_set
    assert analysis_repository.get_result(result.result_id) == result
    assert analysis_repository.list_results(analysis_set.set_id) == (result,)
    replay_multi_seat_analysis(
        result,
        analysis_set,
        tuple(FrequencyResponse(d.frequency_hz, d.level_db) for d in (d1, d2, d3)),
    )
    # A tampered result fails closed at parse time.
    payload = result.model_dump(mode='python')
    payload['mean_db'] = tuple(0.0 for _ in payload['mean_db'])
    with pytest.raises(Exception, match='hash mismatch'):
        MultiSeatAnalysisResult(**payload)


def test_no_overlap_fails_closed(tmp_path: Path):
    revision, repository, _ = _repositories(tmp_path)
    m1, d1 = _save_measurement(repository, revision, 'mlp', 'm-1', (70.0, 72.0, 70.0))
    members = (
        MultiSeatMember(
            measurement_id=m1.measurement_id,
            dataset_id=d1.dataset_id,
            dataset_sha256=d1.dataset_sha256,
            target_entity_id='point-mlp',
            seat_label='MLP',
            channel_role='front_left',
            evidence_type='measured',
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
        ),
    )
    analysis_set = _set(revision, members)
    with pytest.raises(ValueError, match='do not overlap'):
        run_multi_seat_analysis(
            analysis_set,
            (FrequencyResponse((20.0, 40.0, 80.0), (70.0, 72.0, 70.0)),),
            low_hz=200.0,
            high_hz=400.0,
            created_at='2026-09-23T01:00:00+00:00',
        )


def test_algorithm_identity_is_versioned():
    assert MULTI_SEAT_ALGORITHM_SHA256 and len(MULTI_SEAT_ALGORITHM_SHA256) == 64
