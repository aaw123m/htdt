from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_av_sync import (
    AV_SYNC_SIGN_CONVENTION,
    AVSyncCondition,
    advance_av_latency_measurement,
    build_av_latency_measurement,
    build_av_sync_condition,
)
from htdt.cad_av_sync_repository import CadAVSyncRepository
from htdt.cad_repository import SceneRepository


def _repository(tmp_path: Path) -> CadAVSyncRepository:
    return CadAVSyncRepository(SceneRepository(tmp_path / 'cad.sqlite3'))


def _condition(**overrides):
    kwargs: dict = {
        'document_id': 'doc-1',
        'scene_revision_id': 'rev-1',
        'display_device_id': 'projector-x',
        'video_mode': 'game',
        'refresh_rate_hz': 120.0,
        'video_processing_mode': 'low_latency',
        'audio_path': 'earc',
        'lip_sync_offset_ms': 15.0,
        'created_at': '2026-09-23T00:00:00+00:00',
    }
    kwargs.update(overrides)
    return build_av_sync_condition(**kwargs)


def test_sign_convention_is_canonical():
    assert AV_SYNC_SIGN_CONVENTION == 'positive_means_audio_after_video'


def test_condition_identity_separates_operating_modes():
    movie = _condition(video_mode='movie', video_processing_mode='movie')
    game = _condition(video_mode='game', video_processing_mode='low_latency')
    assert movie.condition_sha256 != game.condition_sha256


def test_condition_rejects_tampered_hash():
    payload = _condition().model_dump(mode='python')
    payload['lip_sync_offset_ms'] = 30.0
    with pytest.raises(ValidationError, match='condition hash mismatch'):
        AVSyncCondition(**payload)


def test_measurement_binds_exact_condition():
    condition = _condition()
    measurement = build_av_latency_measurement(
        condition,
        method='manual_external_sync_test',
        measured_offset_ms=42.0,
        uncertainty_ms=8.0,
        captured_at='2026-09-23T01:00:00+00:00',
        source_kind='user_measured',
    )
    assert measurement.condition_id == condition.condition_id
    assert measurement.condition_sha256 == condition.condition_sha256
    assert measurement.measured_offset_ms == pytest.approx(42.0)


def test_unknown_offset_stays_unknown():
    measurement = build_av_latency_measurement(
        _condition(),
        method='unknown',
        measured_offset_ms=None,
        captured_at='2026-09-23T01:00:00+00:00',
        source_kind='unknown',
    )
    assert measurement.measured_offset_ms is None
    assert measurement.residual_offset_ms is None


def test_lifecycle_stage_values_require_status():
    with pytest.raises(ValidationError, match='residual_offset_ms requires residual_verified'):
        build_av_latency_measurement(
            _condition(),
            measured_offset_ms=40.0,
            residual_offset_ms=3.0,
            captured_at='2026-09-23T01:00:00+00:00',
        )


def test_lifecycle_advance_is_monotonic_and_keeps_stages_distinct():
    condition = _condition()
    measured = build_av_latency_measurement(
        condition,
        measured_offset_ms=40.0,
        captured_at='2026-09-23T01:00:00+00:00',
    )
    requested = advance_av_latency_measurement(
        measured,
        status='correction_requested',
        requested_correction_ms=-40.0,
        captured_at='2026-09-23T01:05:00+00:00',
    )
    applied = advance_av_latency_measurement(
        requested,
        status='setting_applied',
        applied_setting_ms=-40.0,
        captured_at='2026-09-23T01:10:00+00:00',
    )
    verified = advance_av_latency_measurement(
        applied,
        status='residual_verified',
        residual_offset_ms=3.0,
        captured_at='2026-09-23T01:20:00+00:00',
    )
    assert requested.requested_correction_ms == pytest.approx(-40.0)
    assert applied.applied_setting_ms == pytest.approx(-40.0)
    assert verified.residual_offset_ms == pytest.approx(3.0)
    # Earlier stages are immutable records, not overwritten state.
    assert measured.residual_offset_ms is None
    assert len({m.measurement_sha256 for m in (measured, requested, applied, verified)}) == 4
    with pytest.raises(ValueError, match='monotonic'):
        advance_av_latency_measurement(
            verified,
            status='measured',
            captured_at='2026-09-23T01:30:00+00:00',
        )


def test_verified_residual_may_stay_unknown():
    measured = build_av_latency_measurement(
        _condition(),
        measured_offset_ms=25.0,
        captured_at='2026-09-23T01:00:00+00:00',
    )
    verified = advance_av_latency_measurement(
        advance_av_latency_measurement(
            advance_av_latency_measurement(
                measured,
                status='correction_requested',
                requested_correction_ms=-25.0,
                captured_at='2026-09-23T01:05:00+00:00',
            ),
            status='setting_applied',
            applied_setting_ms=-25.0,
            captured_at='2026-09-23T01:10:00+00:00',
        ),
        status='residual_verified',
        residual_offset_ms=None,
        captured_at='2026-09-23T01:20:00+00:00',
    )
    assert verified.status == 'residual_verified'
    assert verified.residual_offset_ms is None


def test_repository_round_trip_and_condition_binding(tmp_path: Path):
    repository = _repository(tmp_path)
    condition = _condition()
    repository.save_condition(condition)
    measured = build_av_latency_measurement(
        condition,
        method='manual_external_sync_test',
        measured_offset_ms=42.0,
        captured_at='2026-09-23T01:00:00+00:00',
        source_kind='user_measured',
    )
    repository.save_measurement(measured)
    assert repository.get_condition(condition.condition_id) == condition
    assert repository.get_measurement(measured.measurement_id) == measured
    assert repository.list_measurements(condition.condition_id) == (measured,)
    # A measurement bound to an unpersisted condition fails closed.
    orphan = build_av_latency_measurement(
        _condition(display_device_id='other-display'),
        measured_offset_ms=1.0,
        captured_at='2026-09-23T02:00:00+00:00',
    )
    with pytest.raises(ValueError, match='persisted condition'):
        repository.save_measurement(orphan)
