from __future__ import annotations

from hashlib import sha256

import numpy as np
import pytest

from htdt.cad_spatial_ir_metrics import (
    DirectionalIrChannelRef,
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


def _channel(
    role: str,
    samples,
    *,
    dataset: str = 'dataset-1',
    absolute: bool = True,
) -> DirectionalIrChannelRef:
    return DirectionalIrChannelRef(
        kind='measured',
        artifact_id=f'ir:{role}-1',
        artifact_sha256=_hash(f'ir-{role}'),
        decoded_pcm_sha256=_samples_hash(samples),
        sample_rate_hz=FS,
        sample_count=len(samples),
        dataset_id=dataset,
        dataset_sha256=_hash(dataset),
        channel_role=role,
        receiver_id='array-1',
        receiver_entity_id='entity-array-1',
        absolute_amplitude_authority=absolute,
    )


def _directional_pair():
    """Omni: direct spike at t=0 + frontal reflection at 30 ms.

    Figure-8 (null at source): no direct sound, the 30 ms reflection at
    cos θ = 0.5 lateral gain.
    """
    omni = np.zeros(N)
    omni[0] = 1.0
    omni[int(0.03 * FS)] = 0.5
    lateral = np.zeros(N)
    lateral[int(0.03 * FS)] = 0.25
    return lateral, omni


def _directional_spec(metric: str, **overrides):
    lateral, omni = _directional_pair()
    kwargs = dict(
        acoustic_scene_snapshot_id='snapshot-1',
        acoustic_scene_snapshot_sha256=_hash('snapshot'),
        source_scenario_id='scenario-1',
        source_scenario_sha256=_hash('scenario'),
        metric=metric,
        time_window_start_s=0.005,
        time_window_end_s=0.08,
        window_semantics='early',
    )
    kwargs.update(overrides)
    return build_spatial_ir_metric_spec(**kwargs)


def test_lateral_fraction_computed_from_directional_pair() -> None:
    lateral, omni = _directional_pair()
    spec = _directional_spec(
        'lateral_fraction',
        lateral_channel=_channel('figure8_lateral', lateral),
        omni_channel=_channel('omnidirectional', omni),
    )
    result = evaluate_spatial_ir_metric(
        spec, lateral_ir=tuple(lateral), omni_ir=tuple(omni)
    )
    assert result.state == 'computed'
    # fig-8 energy 0.0625 over omni 0→80 ms energy 1.25
    assert result.value == pytest.approx(0.05, abs=1e-12)
    assert result.value_unit == 'dimensionless'
    assert result.evidence_kind == 'measured'


def test_lateral_fraction_cosine_weights_by_cosine() -> None:
    lateral, omni = _directional_pair()
    spec = _directional_spec(
        'lateral_fraction_cosine',
        lateral_channel=_channel('figure8_lateral', lateral),
        omni_channel=_channel('omnidirectional', omni),
    )
    result = evaluate_spatial_ir_metric(
        spec, lateral_ir=tuple(lateral), omni_ir=tuple(omni)
    )
    assert result.state == 'computed'
    # |p_L·p| = 0.125 over omni 0→80 ms energy 1.25
    assert result.value == pytest.approx(0.1, abs=1e-12)
    assert result.value_unit == 'dimensionless'


def test_listener_envelopment_reports_decibel_level() -> None:
    lateral = np.zeros(N)
    lateral[int(0.09 * FS)] = 0.5  # late lateral arrival inside 80–100 ms
    reference = np.zeros(N)
    reference[0] = 1.0
    spec = _directional_spec(
        'listener_envelopment',
        time_window_start_s=0.08,
        time_window_end_s=0.1,
        window_semantics='late',
        lateral_channel=_channel('figure8_lateral', lateral),
        reference_channel=_channel(
            'free_field_omni_reference', reference, dataset='ref-free-field'
        ),
    )
    result = evaluate_spatial_ir_metric(
        spec, lateral_ir=tuple(lateral), reference_ir=tuple(reference)
    )
    assert result.state == 'computed'
    # L_J = 10·log10(0.25 / 1.0)
    assert result.value == pytest.approx(-6.02, abs=0.01)
    assert result.value_unit == 'decibel'


def test_directional_metrics_require_pinned_channels() -> None:
    # A non-IACC spec without its directional authorities fails closed at
    # authoring — no spec object may exist that could silently degrade.
    with pytest.raises(ValueError, match='figure-8'):
        _directional_spec('lateral_fraction')
    lateral, omni = _directional_pair()
    with pytest.raises(ValueError, match='co-located omni'):
        _directional_spec(
            'lateral_fraction',
            lateral_channel=_channel('figure8_lateral', lateral),
        )
    with pytest.raises(ValueError, match='free-field omni reference'):
        _directional_spec(
            'listener_envelopment',
            time_window_start_s=0.08,
            time_window_end_s=0.1,
            lateral_channel=_channel('figure8_lateral', lateral),
        )
    with pytest.raises(ValueError, match='omnidirectional channel'):
        _directional_spec(
            'lateral_fraction',
            lateral_channel=_channel('figure8_lateral', lateral),
            omni_channel=_channel('figure8_lateral', lateral),
        )
    with pytest.raises(ValueError, match='same.*dataset'):
        _directional_spec(
            'lateral_fraction',
            lateral_channel=_channel('figure8_lateral', lateral),
            omni_channel=_channel(
                'omnidirectional', omni, dataset='dataset-other'
            ),
        )
    with pytest.raises(ValueError, match='absolute-amplitude'):
        _directional_spec(
            'listener_envelopment',
            time_window_start_s=0.08,
            time_window_end_s=0.1,
            lateral_channel=_channel('figure8_lateral', lateral),
            reference_channel=_channel(
                'free_field_omni_reference',
                np.ones(N),
                dataset='ref-free-field',
                absolute=False,
            ),
        )


def test_directional_metric_blocked_and_fail_closed_paths() -> None:
    lateral, omni = _directional_pair()
    spec = _directional_spec(
        'lateral_fraction',
        lateral_channel=_channel('figure8_lateral', lateral),
        omni_channel=_channel('omnidirectional', omni),
    )
    missing = evaluate_spatial_ir_metric(spec)
    assert missing.state == 'blocked'
    assert missing.value is None
    wrong = np.roll(omni, 1)
    with pytest.raises(ValueError, match='pinned artifact'):
        evaluate_spatial_ir_metric(
            spec, lateral_ir=tuple(lateral), omni_ir=tuple(wrong)
        )
    silent_omni_spec = _directional_spec(
        'lateral_fraction',
        lateral_channel=_channel('figure8_lateral', lateral),
        omni_channel=_channel('omnidirectional', np.zeros(N)),
    )
    blocked = evaluate_spatial_ir_metric(
        silent_omni_spec,
        lateral_ir=tuple(lateral),
        omni_ir=tuple(np.zeros(N)),
    )
    assert blocked.state == 'blocked'
    assert blocked.value is None


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
