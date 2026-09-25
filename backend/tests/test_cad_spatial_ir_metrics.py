from __future__ import annotations

from hashlib import sha256

import numpy as np
import pytest

from htdt.cad_spatial_ir_metrics import (
    ImpulseEarEvidenceRef,
    build_spatial_ir_metric_spec,
    evaluate_spatial_ir_metric,
)


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _samples_hash(samples) -> str:
    return sha256(np.asarray(samples, dtype='<f8').tobytes()).hexdigest()


FS = 48000
N = 4800


def _ear(ear: str, samples) -> ImpulseEarEvidenceRef:
    return ImpulseEarEvidenceRef(
        kind='measured',
        artifact_id=f'ir:{ear}-1',
        artifact_sha256=_hash(f'ir-{ear}'),
        decoded_pcm_sha256=_samples_hash(samples),
        sample_rate_hz=FS,
        sample_count=len(samples),
        ear=ear,
        receiver_id='seat-1',
        receiver_entity_id='entity-seat-1',
        absolute_amplitude_authority=True,
    )


def _brirs(correlated: bool = True):
    rng = np.random.RandomState(7)
    left = rng.randn(N)
    if correlated:
        right = left.copy()  # perfectly correlated pair → IACC = 1
    else:
        right = rng.randn(N)
    return left, right


def _iacc_spec(left, right, **overrides):
    kwargs = dict(
        acoustic_scene_snapshot_id='snapshot-1',
        acoustic_scene_snapshot_sha256=_hash('snapshot'),
        source_scenario_id='scenario-1',
        source_scenario_sha256=_hash('scenario'),
        metric='iacc',
        left_ear=_ear('left', left),
        right_ear=_ear('right', right),
        time_window_start_s=0.0,
        time_window_end_s=0.08,
        window_semantics='early',
        head_orientation_deg=0.0,
    )
    kwargs.update(overrides)
    return build_spatial_ir_metric_spec(**kwargs)


def test_iacc_computed_on_correlated_binaural_pair() -> None:
    left, right = _brirs(correlated=True)
    spec = _iacc_spec(left, right)
    assert spec.spec_id.startswith('spatial-ir-metric-spec:')
    result = evaluate_spatial_ir_metric(
        spec, left_ir=tuple(left), right_ir=tuple(right)
    )
    assert result.state == 'computed'
    assert result.value == pytest.approx(1.0, abs=1e-12)
    assert result.evidence_kind == 'measured'
    assert result.result_id.startswith('spatial-ir-metric-result:')


def test_iacc_decorrelated_pair_scores_low() -> None:
    left, right = _brirs(correlated=False)
    spec = _iacc_spec(left, right)
    result = evaluate_spatial_ir_metric(
        spec, left_ir=tuple(left), right_ir=tuple(right)
    )
    assert result.state == 'computed'
    assert abs(result.value) < 0.3


def test_iacc_never_from_mono_or_mispinned_ir() -> None:
    left, right = _brirs()
    with pytest.raises(ValueError, match='binaural pair'):
        build_spatial_ir_metric_spec(
            acoustic_scene_snapshot_id='snapshot-1',
            acoustic_scene_snapshot_sha256=_hash('snapshot'),
            source_scenario_id='scenario-1',
            source_scenario_sha256=_hash('scenario'),
            metric='iacc',
            left_ear=_ear('left', left),
            right_ear=None,
            time_window_start_s=0.0,
            time_window_end_s=0.08,
        )
    spec = _iacc_spec(left, right)
    wrong = np.roll(left, 1)  # different samples: not the pinned artifact
    with pytest.raises(ValueError, match='pinned artifact'):
        evaluate_spatial_ir_metric(
            spec, left_ir=tuple(wrong), right_ir=tuple(right)
        )
    missing = evaluate_spatial_ir_metric(spec)
    assert missing.state == 'blocked'


def test_non_iacc_metrics_report_unsupported() -> None:
    left, right = _brirs()
    spec = _iacc_spec(left, right, metric='lateral_fraction',
                      left_ear=None, right_ear=None)
    result = evaluate_spatial_ir_metric(
        spec, left_ir=tuple(left), right_ir=tuple(right)
    )
    assert result.state == 'unsupported'
    assert result.value is None


def test_blocked_when_window_or_signal_degenerate() -> None:
    left, right = _brirs()
    silent = np.zeros(N)
    spec = _iacc_spec(left, right)
    # Samples that don't match the pinned artifact hash fail closed.
    with pytest.raises(ValueError, match='pinned artifact'):
        evaluate_spatial_ir_metric(
            spec, left_ir=tuple(silent), right_ir=tuple(right)
        )

    silent_left = ImpulseEarEvidenceRef(
        kind='measured',
        artifact_id='ir:left-1',
        artifact_sha256=_hash('ir-left'),
        decoded_pcm_sha256=_samples_hash(silent),
        sample_rate_hz=FS,
        sample_count=N,
        ear='left',
        receiver_id='seat-1',
        receiver_entity_id='entity-seat-1',
        absolute_amplitude_authority=True,
    )
    spec_silent = _iacc_spec(left, right, left_ear=silent_left)
    result = evaluate_spatial_ir_metric(
        spec_silent, left_ir=tuple(silent), right_ir=tuple(right)
    )
    assert result.state == 'blocked'
    assert result.value is None
