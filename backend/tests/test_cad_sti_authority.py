"""Regression tests for the #605 speech-intelligibility authority.

Fixture map (issue §fixtures): STI10 identity/paths; STI20 method
classes; STI30 noise-state binding; STI40 seat distribution; STI50
occlusion flag independence; STI60 level/method eligibility; STI70
before-after comparability.
"""

from __future__ import annotations

from math import cos, pi, sin
from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_room_noise_metrics import (
    NoiseAcquisitionContext,
    NoiseBandLevels,
    NoiseOperatingState,
    NoisePositionSpectrum,
    S12_2_OCTAVE_BANDS_HZ,
    build_noise_measurement,
)
from htdt.cad_sti_authority import (
    STIEvidenceBundle,
    STIPathContext,
    STISeatResult,
    STITargetBinding,
    STI_OCTAVE_BANDS_HZ,
    STI_MODULATION_FREQUENCIES_HZ,
    apply_noise_correction,
    build_dialogue_intelligibility_assessment,
    compare_sti_evidence,
    compute_sti,
    evaluate_sti_measurement,
    evaluate_sti_prediction,
    mtr_from_band_ir,
    mtr_to_snr_db,
    seed_speech_intelligibility_profiles,
)
from htdt.cad_sti_repository import (
    CadSTIRepository,
    STIConflictError,
    STIIntegrityError,
)


_DOC = 'doc-sti'


def _noise_measurement():
    return build_noise_measurement(
        document_id=_DOC,
        acquisition=NoiseAcquisitionContext(
            instrument_id='slm-1', method='integrated_leq'
        ),
        positions=(
            NoisePositionSpectrum(
                position_label='mlp',
                spectrum=NoiseBandLevels(
                    band_center_hz=S12_2_OCTAVE_BANDS_HZ,
                    band_level_db=(
                        30.0, 28.0, 26.0, 24.0, 22.0,
                        20.0, 18.0, 16.0, 15.0, 14.0,
                    ),
                    band_spec='octave',
                    level_semantics='absolute_spl',
                ),
            ),
        ),
        temporal_class='steady_state',
        operating_state=NoiseOperatingState(
            hvac='on', projector='on', occupancy='occupied'
        ),
        captured_at_utc='2026-10-01T00:00:00+00:00',
    )


def _profiles():
    return seed_speech_intelligibility_profiles(
        document_id=_DOC, created_at_utc='2026-10-01T00:00:00+00:00'
    )


def _path(**overrides):
    kwargs = dict(
        speech_source_label='dialogue-center',
        channel_id='center',
        speaker_entity_ref='speaker-center',
        routing_state_ref='routing-v1',
        eq_state_ref='eq-v1',
        room_revision='rev-7',
        seat_ref='seat-mlp',
        signal_chain_class='acoustic_only',
        speech_level_db=65.0,
        speech_level_reference_point='seat-ear-height',
        calibration_ref='cal-2026-01',
    )
    kwargs.update(overrides)
    return STIPathContext(**kwargs)


def _perfect_mtr() -> tuple[tuple[float, ...], ...]:
    return tuple(
        (1.0,) * len(STI_MODULATION_FREQUENCIES_HZ)
        for _ in STI_OCTAVE_BANDS_HZ
    )


def _degraded_mtr(depth: float) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(1.0 - depth for _ in STI_MODULATION_FREQUENCIES_HZ)
        for _ in STI_OCTAVE_BANDS_HZ
    )


# ---------------------------------------------------------------------------
# STI math — verified against the published model
# ---------------------------------------------------------------------------


def test_perfect_transmission_scores_one() -> None:
    profile = _profiles()[0]
    sti, snr = compute_sti(_perfect_mtr(), profile)
    assert sti == pytest.approx(1.0, abs=1e-6)
    assert all(s == pytest.approx(15.0) for s in snr)


def test_sti_monotonic_in_mtr_and_noise_correction() -> None:
    profile = _profiles()[0]
    good, _ = compute_sti(_degraded_mtr(0.1), profile)
    poor, _ = compute_sti(_degraded_mtr(0.6), profile)
    assert 0.0 < poor < good <= 1.0
    # Noise correction reduces the effective MTR.
    assert apply_noise_correction(0.8, 0.0) == pytest.approx(0.4)
    assert apply_noise_correction(0.8, None) == 0.8
    assert mtr_to_snr_db(0.5) == pytest.approx(0.0)
    assert mtr_to_snr_db(1.0) == 15.0
    assert mtr_to_snr_db(0.0) == -15.0


def test_ir_derived_mtr_recovers_sinusoidal_modulation() -> None:
    """h^2 = 1 + A·cos(2πf_t n/fs) yields m(f_t) = A/2 when the record
    spans an integer number of modulation cycles."""
    fs = 8000.0
    f_target = 4.0  # one of the IEC 60268-16 modulation frequencies
    cycles = 4
    n = int(fs / f_target) * cycles
    a = 0.6
    energy = tuple(
        1.0 + a * cos(2 * pi * f_target * k / fs) for k in range(n)
    )
    m = mtr_from_band_ir(energy, fs)
    idx = STI_MODULATION_FREQUENCIES_HZ.index(f_target)
    assert m[idx] == pytest.approx(a / 2, abs=1e-6)

    # A decaying energy envelope (as from a real IR) attenuates
    # modulation with frequency. For h^2 = c^k the MTF has the closed
    # form m(f) = (1-c) / |1 - c·e^{-j2πf/fs}| — monotone decreasing.
    c = 0.9995
    decay = tuple(c ** k for k in range(n))
    m_decay = mtr_from_band_ir(decay, fs)
    for k, fm in enumerate(STI_MODULATION_FREQUENCIES_HZ):
        theta = 2 * pi * fm / fs
        # finite-length geometric series: (1-c^N)/(1-c·e^{-jθ})
        num = abs(1 - (c * complex(cos(theta), -sin(theta))) ** n)
        expected = (1 - c) * num / (
            (1 - c ** n)
            * abs(1 - c * complex(cos(theta), -sin(theta)))
        )
        assert m_decay[k] == pytest.approx(expected, abs=0.005)
    assert all(
        m_decay[i] > m_decay[i + 1] for i in range(13)
    )  # monotone attenuation


# ---------------------------------------------------------------------------
# STI30 — noise-state binding is mandatory
# ---------------------------------------------------------------------------


def test_measurement_requires_noise_measurement_binding() -> None:
    profile = _profiles()[0]
    evidence = STIEvidenceBundle(mtr_matrix=_perfect_mtr())
    with pytest.raises(Exception):
        # Missing #580 binding cannot mint a sealed record.
        evaluate_sti_measurement(
            profile=profile,
            path=_path(),
            evidence=evidence,
            document_id=_DOC,
            noise_measurement_id='',
            noise_measurement_sha256='',
        )
    measurement = evaluate_sti_measurement(
        profile=profile,
        path=_path(),
        evidence=evidence,
        document_id=_DOC,
        noise_measurement_id=_noise_measurement().measurement_id,
        noise_measurement_sha256=_noise_measurement().measurement_sha256,
    )
    assert measurement.sti_value == pytest.approx(1.0, abs=1e-6)
    assert measurement.method == 'direct_sti_measurement'


def test_fluctuating_noise_and_codec_chain_marked_honestly() -> None:
    profile = _profiles()[0]
    noise = _noise_measurement()
    measurement = evaluate_sti_measurement(
        profile=profile,
        path=_path(signal_chain_class='codec_compressed'),
        evidence=STIEvidenceBundle(mtr_matrix=_degraded_mtr(0.2)),
        document_id=_DOC,
        noise_measurement_id=noise.measurement_id,
        noise_measurement_sha256=noise.measurement_sha256,
        noise_temporal_class='low_frequency_fluctuating',
    )
    assert measurement.applicability == 'limited'
    assert 'sti_not_validated_for_compressed_chain' in measurement.limitations
    assert (
        'sti_valid_for_captured_steady_state_only'
        in measurement.limitations
    )


def test_simulated_method_cannot_be_a_measurement() -> None:
    """Predicted results live in STIPrediction only (issue: distinct
    evidence class)."""
    profile = _profiles()[3]  # simulated profile
    noise = _noise_measurement()
    with pytest.raises(Exception, match='simulated'):
        evaluate_sti_measurement(
            profile=profile,
            path=_path(),
            evidence=STIEvidenceBundle(mtr_matrix=_perfect_mtr()),
            document_id=_DOC,
            noise_measurement_id=noise.measurement_id,
            noise_measurement_sha256=noise.measurement_sha256,
        )

    # And a simulated prediction requires the #566 validation pin.
    with pytest.raises(Exception, match='validation'):
        evaluate_sti_prediction(
            profile=profile,
            path=_path(),
            document_id=_DOC,
            model_version='sim-1',
            noise_measurement_id=noise.measurement_id,
            noise_measurement_sha256=noise.measurement_sha256,
            evidence=STIEvidenceBundle(mtr_matrix=_perfect_mtr()),
        )
    prediction = evaluate_sti_prediction(
        profile=profile,
        path=_path(),
        document_id=_DOC,
        model_version='sim-1',
        noise_measurement_id=noise.measurement_id,
        noise_measurement_sha256=noise.measurement_sha256,
        evidence=STIEvidenceBundle(mtr_matrix=_perfect_mtr()),
        validation_ref='566-validation-abc',
        uncertainty_db=0.05,
        uncertainty_ref='604-unc-xyz',
    )
    assert prediction.sti_value == pytest.approx(1.0, abs=1e-6)
    assert 'predicted_not_measured' in prediction.limitations


# ---------------------------------------------------------------------------
# STI40/50 — seat distribution; occlusion independent
# ---------------------------------------------------------------------------


def test_assessment_keeps_per_seat_evidence_and_worst() -> None:
    assessment = build_dialogue_intelligibility_assessment(
        document_id=_DOC,
        seat_results=(
            STISeatResult(seat_label='mlp', role='reference',
                          sti_value=0.82),
            STISeatResult(seat_label='rear-left', sti_value=0.51),
            STISeatResult(seat_label='rear-right', sti_value=0.48,
                          occlusion_flag=True,
                          note='sofa arm occlusion'),
        ),
        reference_seat_label='mlp',
        candidate_causes=('early_reflections', 'background_noise'),
        independent_observations=(
            'occlusion geometry handled separately under #590',
        ),
    )
    assert assessment.worst_seat_label == 'rear-right'
    assert assessment.percentile_10 == pytest.approx(0.48)
    assert assessment.wording_class == (
        'objective_speech_transmission_intelligibility_evidence'
    )
    # No target declared -> no invented pass/fail.
    assert assessment.target is None

    targeted = build_dialogue_intelligibility_assessment(
        document_id=_DOC,
        seat_results=assessment.seat_results,
        target=STITargetBinding(
            target_class='project_target',
            source='project-brief-v3',
            threshold=0.6,
            wording='project requirement: STI >= 0.6 all seats',
        ),
    )
    assert targeted.target is not None
    assert targeted.target.target_class == 'project_target'


# ---------------------------------------------------------------------------
# STI70 — before/after comparability
# ---------------------------------------------------------------------------


def test_before_after_comparison_is_strict() -> None:
    profile = _profiles()[0]
    noise = _noise_measurement()
    before = evaluate_sti_measurement(
        profile=profile, path=_path(),
        evidence=STIEvidenceBundle(mtr_matrix=_degraded_mtr(0.4)),
        document_id=_DOC,
        noise_measurement_id=noise.measurement_id,
        noise_measurement_sha256=noise.measurement_sha256,
    )
    after = evaluate_sti_measurement(
        profile=profile, path=_path(),
        evidence=STIEvidenceBundle(mtr_matrix=_perfect_mtr()),
        document_id=_DOC,
        noise_measurement_id=noise.measurement_id,
        noise_measurement_sha256=noise.measurement_sha256,
    )
    assert compare_sti_evidence(before, after) == 'improved'
    assert compare_sti_evidence(before, before) == 'unchanged'

    moved = evaluate_sti_measurement(
        profile=profile, path=_path(seat_ref='seat-rear'),
        evidence=STIEvidenceBundle(mtr_matrix=_perfect_mtr()),
        document_id=_DOC,
        noise_measurement_id=noise.measurement_id,
        noise_measurement_sha256=noise.measurement_sha256,
    )
    assert compare_sti_evidence(before, moved) == 'incomparable'

    leveled = evaluate_sti_measurement(
        profile=profile, path=_path(speech_level_db=75.0),
        evidence=STIEvidenceBundle(mtr_matrix=_perfect_mtr()),
        document_id=_DOC,
        noise_measurement_id=noise.measurement_id,
        noise_measurement_sha256=noise.measurement_sha256,
    )
    assert compare_sti_evidence(before, leveled) == 'incomparable'


# ---------------------------------------------------------------------------
# Persistence round-trips
# ---------------------------------------------------------------------------


def test_repository_round_trip_and_commit_order(tmp_path: Path) -> None:
    repository = CadSTIRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = _profiles()[0]
    repository.save_profile(profile)
    repository.save_profile(profile)
    assert repository.get_profile(profile.profile_id) == profile

    noise = _noise_measurement()
    measurement = evaluate_sti_measurement(
        profile=profile, path=_path(),
        evidence=STIEvidenceBundle(mtr_matrix=_degraded_mtr(0.2)),
        document_id=_DOC,
        noise_measurement_id=noise.measurement_id,
        noise_measurement_sha256=noise.measurement_sha256,
    )
    repository.save_measurement(measurement)
    repository.save_measurement(measurement)
    assert (
        repository.get_measurement(measurement.measurement_id)
        == measurement
    )
    assert repository.list_measurements(_DOC) == (measurement,)

    orphan = CadSTIRepository(SceneRepository(tmp_path / 'o.sqlite3'))
    with pytest.raises(STIIntegrityError):
        orphan.save_measurement(measurement)


def test_repository_conflict_and_prediction_round_trip(
    tmp_path: Path,
) -> None:
    repository = CadSTIRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profiles = _profiles()
    repository.save_profile(profiles[3])
    noise = _noise_measurement()
    prediction = evaluate_sti_prediction(
        profile=profiles[3], path=_path(),
        document_id=_DOC, model_version='sim-1',
        noise_measurement_id=noise.measurement_id,
        noise_measurement_sha256=noise.measurement_sha256,
        evidence=STIEvidenceBundle(mtr_matrix=_perfect_mtr()),
        validation_ref='566-v',
        predicted_at_utc='2026-10-01T00:00:00+00:00',
    )
    repository.save_prediction(prediction)
    assert repository.get_prediction(prediction.prediction_id) == prediction

    assessment = build_dialogue_intelligibility_assessment(
        document_id=_DOC,
        seat_results=(
            STISeatResult(seat_label='mlp', sti_value=0.8),
            STISeatResult(seat_label='rear', sti_value=0.5),
        ),
        assessed_at_utc='2026-10-01T00:00:00+00:00',
    )
    repository.save_assessment(assessment)
    assert (
        repository.get_assessment(assessment.assessment_id)
        == assessment
    )

    # Forged reuse (stale sha over changed payload) is rejected.
    forged = assessment.model_copy(
        update={
            'seat_results': (
                STISeatResult(seat_label='mlp', sti_value=0.9),
            )
        }
    )
    with pytest.raises(STIIntegrityError):
        repository.save_assessment(forged)
