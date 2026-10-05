"""REV56-BASSSTIM (#574/#608): bass-management/crossover qualification +
test-stimulus/calibration-asset registry — splice cancellation detection,
failure-reason attribution, verdict honesty (UNKNOWN/INSUFFICIENT),
fail-closed claim gating, sealed-record integrity, append-only
persistence."""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from htdt.cad_bass_management import (
    CrossoverSpec,
    LFEPathRule,
    MainChannelBassRule,
    SubwooferOutputGroup,
    build_bass_management_profile,
)
from htdt.cad_bass_management_qualification import (
    BassManagementQualification,
    ProcessingStage,
    ResponseCurve,
    SubHeadroomBudget,
    SubPathLoad,
    UsableBandEvidence,
    build_splice_evidence,
    delay_cancels,
    evaluate_bass_management_qualification,
    evaluate_splice,
    evaluate_sub_headroom,
    evaluate_usable_band,
    gate_status_for,
)
from htdt.cad_bass_qualification_repository import (
    BassQualificationConflictError,
    BassQualificationIntegrityError,
    CadBassQualificationRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.cad_stimulus_registry import (
    PlaybackPathReport,
    PlaybackTransformation,
    ProcedureStimulusRequirement,
    StandardProfileRef,
    StimulusAssetEntry,
    StimulusGeneratorSpec,
    StimulusLevelSemantics,
    StimulusMediaFormat,
    StochasticRealization,
    build_procedure_requirement,
    build_stimulus_asset,
    build_stimulus_pin,
    evaluate_stimulus_eligibility,
    measurement_stimulus_pin_state,
    stimulus_claim_allowed,
)
from htdt.cad_stimulus_registry_repository import (
    CadStimulusRegistryRepository,
    StimulusRegistryConflictError,
    StimulusRegistryIntegrityError,
)


DOC = 'doc-bassstim'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(
        make_empty_scene(doc_id), parent_revision_id=None
    )
    return scene_repository


# ---------------------------------------------------------------------------
# Stimulus registry fixtures (#608)


def _ess_asset(**overrides) -> StimulusAssetEntry:
    """A Farina/ESS-style log sweep with full reproduction params — the
    duration/amplitude/fade fields are part of the stimulus identity."""
    kwargs = dict(
        stimulus_id='stim-ess-20s',
        document_id=DOC,
        label='ESS log sweep 20 Hz–20 kHz, 20 s, -6 dBFS peak',
        origin_class='deterministic_generated_audio',
        subtype='ess_log_sweep',
        media_format=StimulusMediaFormat(
            container='wav',
            codec='pcm_s24le',
            sample_rate_hz=48000.0,
            bit_depth=24,
            channel_count=1,
            duration_s=20.0,
        ),
        generator_spec=StimulusGeneratorSpec(
            generator_id='htdt-sweepgen',
            generator_version='1.0',
            signal_family='ess_log_sweep',
            start_frequency_hz=20.0,
            end_frequency_hz=20000.0,
            sweep_law='log',
            sample_rate_hz=48000.0,
            duration_s=20.0,
            amplitude_dbfs=-6.0,
            fade_in_s=0.02,
            fade_out_s=0.02,
            generated_content_sha256=SHA_A,
        ),
        level_semantics=StimulusLevelSemantics(
            digital_peak_dbfs=-6.0,
            crest_factor_db=3.01,
            full_scale_convention='dBFS peak',
        ),
        content_sha256=SHA_A,
        signal_band_low_hz=20.0,
        signal_band_high_hz=20000.0,
        purposes=('frequency_response', 'impulse_response'),
        registered_at_utc='2026-10-05T00:00:00Z',
    )
    kwargs.update(overrides)
    return build_stimulus_asset(**kwargs)


def _music_noise_asset(**overrides) -> StimulusAssetEntry:
    """AES75 Music-Noise fixed asset carrying the publisher checksum."""
    kwargs = dict(
        stimulus_id='stim-aes75-music-noise-48k',
        document_id=DOC,
        label='AES75 Music-Noise (48 kHz official asset)',
        origin_class='standard_profiled_asset',
        subtype='music_noise',
        media_format=StimulusMediaFormat(
            container='wav',
            codec='pcm_s24le',
            sample_rate_hz=48000.0,
            bit_depth=24,
            channel_count=1,
        ),
        standard_profiles=(
            StandardProfileRef(
                standard_id='AES75',
                revision='2023',
                asset_role='music_noise_48k',
                publisher_checksum=SHA_B,
                checksum_algorithm='sha256',
            ),
        ),
        publisher='Audio Engineering Society',
        content_sha256=SHA_B,
        signal_band_low_hz=100.0,
        signal_band_high_hz=16000.0,
        level_semantics=StimulusLevelSemantics(
            digital_peak_dbfs=-3.0,
            crest_factor_db=12.0,
        ),
        rights='standard_body_downloadable',
        registered_at_utc='2026-10-05T00:00:00Z',
    )
    kwargs.update(overrides)
    return build_stimulus_asset(**kwargs)


def _requirement(**overrides) -> ProcedureStimulusRequirement:
    kwargs = dict(procedure_id='frf-measurement-1')
    kwargs.update(overrides)
    return build_procedure_requirement(**kwargs)


# --- STIM: identity + sealing -----------------------------------------------


def test_stim10_asset_seal_and_identity_blocks():
    asset = _ess_asset()
    assert asset.stimulus_sha256 == asset.stimulus_sha256
    assert asset.has_content_identity
    assert asset.stimulus_sha256 == (
        StimulusAssetEntry.model_validate_json(
            asset.model_dump_json()
        ).stimulus_sha256
    )
    # same params → identical seal (deterministic id)
    assert _ess_asset().stimulus_sha256 == asset.stimulus_sha256
    # different duration → different stimulus (duration is identity)
    assert (
        _ess_asset(
            generator_spec=StimulusGeneratorSpec(
                generator_id='htdt-sweepgen',
                generator_version='1.0',
                signal_family='ess_log_sweep',
                start_frequency_hz=20.0,
                end_frequency_hz=20000.0,
                sweep_law='log',
                sample_rate_hz=48000.0,
                duration_s=10.0,
                amplitude_dbfs=-6.0,
            )
        ).stimulus_sha256
        != asset.stimulus_sha256
    )


def test_stim20_kind_label_is_never_identity():
    """Two ess_log_sweep assets with different params are distinct."""
    a = _ess_asset(stimulus_id='sweep-a', content_sha256=SHA_A)
    b = _ess_asset(
        stimulus_id='sweep-b',
        content_sha256=SHA_B,
        generator_spec=StimulusGeneratorSpec(
            generator_id='htdt-sweepgen',
            generator_version='1.0',
            signal_family='ess_log_sweep',
            start_frequency_hz=10.0,
            end_frequency_hz=20000.0,
            sweep_law='log',
            sample_rate_hz=48000.0,
            duration_s=20.0,
            amplitude_dbfs=-6.0,
        ),
    )
    assert a.subtype == b.subtype == 'ess_log_sweep'
    assert a.stimulus_sha256 != b.stimulus_sha256


def test_stim21_fixed_asset_requires_content_hash():
    with pytest.raises(ValidationError):
        build_stimulus_asset(
            stimulus_id='unpinned-file',
            document_id=DOC,
            label='file without hash',
            origin_class='fixed_audio_file',
            subtype='pink_noise',
        )


def test_stim22_stochastic_realization_rules():
    with pytest.raises(ValidationError):
        StochasticRealization(realization_class='seeded_generation')
    with pytest.raises(ValidationError):
        StochasticRealization(realization_class='fixed_realization')
    seeded = StochasticRealization(
        realization_class='seeded_generation',
        seed=12345,
        rng_algorithm='numpy.PCG64',
    )
    assert seeded.bit_exact_replayable
    assert not StochasticRealization(
        realization_class='unseeded_live'
    ).bit_exact_replayable


def test_stim23_standard_profile_checksum_requires_algorithm():
    with pytest.raises(ValidationError):
        StandardProfileRef(
            standard_id='AES75',
            revision='2023',
            publisher_checksum=SHA_B,
        )


# --- STIM: repository -------------------------------------------------------


def test_stim30_repository_round_trip_and_append_only(tmp_path):
    repository = CadStimulusRegistryRepository(_scene_repo(tmp_path))
    asset = _ess_asset()
    repository.save_asset(asset)
    assert repository.get_asset(asset.stimulus_id) == asset
    assert repository.get_asset_by_hash(asset.stimulus_sha256) == asset
    # identical save is a no-op
    repository.save_asset(asset)
    # divergent payload under the same id is a conflict
    renamed = _ess_asset(label='renamed sweep')
    assert renamed.stimulus_sha256 != asset.stimulus_sha256
    with pytest.raises(StimulusRegistryConflictError):
        repository.save_asset(renamed)


def test_stim31_pin_requires_registered_asset(tmp_path):
    repository = CadStimulusRegistryRepository(_scene_repo(tmp_path))
    asset = _ess_asset()
    pin = build_stimulus_pin(
        pin_id='pin-1',
        document_id=DOC,
        measurement_ref='meas-001',
        entry=asset,
        pinned_at_utc='2026-10-05T01:00:00Z',
    )
    with pytest.raises(StimulusRegistryIntegrityError):
        repository.save_pin(pin)
    repository.save_asset(asset)
    repository.save_pin(pin)
    assert repository.pins_for_measurement('meas-001') == (pin,)
    assert repository.get_pin(pin.pin_id) == pin


def test_stim32_lookup_by_content_hash(tmp_path):
    repository = CadStimulusRegistryRepository(_scene_repo(tmp_path))
    a = _music_noise_asset()
    # same bytes registered under a second label
    b = _music_noise_asset(
        stimulus_id='stim-music-noise-alias', label='alias'
    )
    repository.save_asset(a)
    repository.save_asset(b)
    found = repository.lookup_by_content_hash(SHA_B)
    assert {e.stimulus_id for e in found} == {
        a.stimulus_id,
        b.stimulus_id,
    }


def test_stim33_row_payload_tamper_detected(tmp_path):
    scene_repo = _scene_repo(tmp_path)
    repository = CadStimulusRegistryRepository(scene_repo)
    asset = _ess_asset()
    repository.save_asset(asset)
    with sqlite3.connect(scene_repo.path) as connection:
        connection.execute(
            'UPDATE cad_stimulus_assets SET subtype=? WHERE stimulus_id=?',
            ('white_noise', asset.stimulus_id),
        )
    with pytest.raises(StimulusRegistryIntegrityError):
        repository.get_asset(asset.stimulus_id)


# --- STIM: eligibility verdicts ----------------------------------------------


def test_stim40_eligible_when_all_gates_pass():
    record = evaluate_stimulus_eligibility(
        entry=_ess_asset(),
        requirement=_requirement(
            allowed_subtypes=('ess_log_sweep', 'linear_sweep'),
            required_sample_rate_hz=48000.0,
            required_band_low_hz=20.0,
            required_band_high_hz=20000.0,
            max_crest_factor_db=6.0,
        ),
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    assert record.verdict == 'ELIGIBLE'
    assert record.eligibility_id.startswith('stimelig-')


def test_stim41_wrong_revision_is_specific():
    """IEC 60268-16: the 2011 vs 2020 male speech spectrum differs — a
    revision pin is a hard identity check, not a hint."""
    record = evaluate_stimulus_eligibility(
        entry=_music_noise_asset(
            standard_profiles=(
                StandardProfileRef(
                    standard_id='IEC 60268-16',
                    revision='2011',
                    publisher_checksum=SHA_B,
                    checksum_algorithm='sha256',
                ),
            ),
        ),
        requirement=_requirement(
            required_standard_profiles=(('IEC 60268-16', '2020'),),
        ),
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    assert record.verdict == 'WRONG_REVISION'


def test_stim42_publisher_checksum_substitution_is_incompatible():
    """AES75 official assets publish checksums — registered bytes that
    disagree are a substituted file, never the official stimulus."""
    record = evaluate_stimulus_eligibility(
        entry=_music_noise_asset(content_sha256=SHA_C),
        requirement=_requirement(
            required_standard_profiles=(('AES75', '2023'),),
        ),
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    assert record.verdict == 'INCOMPATIBLE'
    assert 'substituted' in ' '.join(record.reasons)


def test_stim43_wrong_sample_rate():
    record = evaluate_stimulus_eligibility(
        entry=_ess_asset(
            media_format=StimulusMediaFormat(
                sample_rate_hz=44100.0, channel_count=1
            )
        ),
        requirement=_requirement(required_sample_rate_hz=48000.0),
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    assert record.verdict == 'WRONG_SAMPLE_RATE'


def test_stim44_wrong_crest_factor():
    record = evaluate_stimulus_eligibility(
        entry=_ess_asset(),
        requirement=_requirement(max_crest_factor_db=3.0),
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    assert record.verdict == 'WRONG_LEVEL_OR_CREST_FACTOR'


def test_stim45_band_coverage_incompatible():
    record = evaluate_stimulus_eligibility(
        entry=_music_noise_asset(),  # 100 Hz–16 kHz
        requirement=_requirement(
            required_band_low_hz=20.0, required_band_high_hz=20000.0
        ),
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    assert record.verdict == 'INCOMPATIBLE'


def test_stim46_no_content_hash_is_insufficient_evidence():
    asset = build_stimulus_asset(
        stimulus_id='live-noise',
        document_id=DOC,
        label='unseeded live noise',
        origin_class='stochastic_generated_audio',
        subtype='pink_noise',
        stochastic=StochasticRealization(
            realization_class='unseeded_live',
            crest_factor_db=12.0,
        ),
        registered_at_utc='2026-10-05T00:00:00Z',
    )
    assert not asset.has_content_identity
    record = evaluate_stimulus_eligibility(
        entry=asset,
        requirement=_requirement(),
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    assert record.verdict == 'INSUFFICIENT_EVIDENCE'


def test_stim47_bit_exact_delivery_path_gates():
    requirement = _requirement(bit_exact_delivery_required=True)
    asset = _ess_asset()
    # no playback report at all
    assert (
        evaluate_stimulus_eligibility(
            entry=asset,
            requirement=requirement,
            evaluated_at_utc='2026-10-05T02:00:00Z',
        ).verdict
        == 'INSUFFICIENT_EVIDENCE'
    )
    # a non-transparent transform (resampling) breaks bit-exactness
    assert (
        evaluate_stimulus_eligibility(
            entry=asset,
            requirement=requirement,
            playback_path=PlaybackPathReport(
                delivery_verification='delivered_signal_verified',
                transformations=(
                    PlaybackTransformation(
                        kind='player_resampling',
                        bit_transparent=False,
                    ),
                ),
            ),
            evaluated_at_utc='2026-10-05T02:00:00Z',
        ).verdict
        == 'TRANSFORMED_NOT_BIT_EXACT'
    )
    # source-only verification limits the claim
    assert (
        evaluate_stimulus_eligibility(
            entry=asset,
            requirement=requirement,
            playback_path=PlaybackPathReport(
                delivery_verification='source_asset_known',
            ),
            evaluated_at_utc='2026-10-05T02:00:00Z',
        ).verdict
        == 'ELIGIBLE_WITH_LIMITATIONS'
    )
    # fully verified clean path
    assert (
        evaluate_stimulus_eligibility(
            entry=asset,
            requirement=requirement,
            playback_path=PlaybackPathReport(
                delivery_verification='delivered_signal_verified',
                transformations=(
                    PlaybackTransformation(
                        kind='os_mixer', bit_transparent=True
                    ),
                ),
            ),
            evaluated_at_utc='2026-10-05T02:00:00Z',
        ).verdict
        == 'ELIGIBLE'
    )


def test_stim48_unreplayable_stimulus_fails_bit_exact_requirement():
    asset = build_stimulus_asset(
        stimulus_id='live-noise-2',
        document_id=DOC,
        label='live noise',
        origin_class='stochastic_generated_audio',
        subtype='band_limited_noise',
        stochastic=StochasticRealization(
            realization_class='unseeded_live',
            crest_factor_db=12.0,
            realization_sha256=None,
        ),
        content_sha256=SHA_C,
        registered_at_utc='2026-10-05T00:00:00Z',
    )
    record = evaluate_stimulus_eligibility(
        entry=asset,
        requirement=_requirement(bit_exact_delivery_required=True),
        playback_path=PlaybackPathReport(
            delivery_verification='delivered_signal_verified'
        ),
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    assert record.verdict == 'INCOMPATIBLE'


def test_stim49_eligibility_repository_round_trip(tmp_path):
    repository = CadStimulusRegistryRepository(_scene_repo(tmp_path))
    asset = _ess_asset()
    requirement = _requirement(allowed_subtypes=('ess_log_sweep',))
    record = evaluate_stimulus_eligibility(
        entry=asset,
        requirement=requirement,
        evaluated_at_utc='2026-10-05T02:00:00Z',
    )
    repository.save_asset(asset)
    repository.save_eligibility(record)
    assert repository.get_eligibility(record.eligibility_id) == record


# --- STIM: measurement pin gate (fail-closed) --------------------------------


def test_stim60_unpinned_measurement_cannot_claim_calibration():
    pins = ()
    assert measurement_stimulus_pin_state(pins, 'meas-001') == 'unpinned'
    allowed, reason = stimulus_claim_allowed(
        pins=pins, measurement_ref='meas-001', claim='calibrated'
    )
    assert not allowed
    assert reason.startswith('claim_blocked_no_stimulus_pin')


def test_stim61_pinned_measurement_may_claim():
    asset = _ess_asset()
    pin = build_stimulus_pin(
        pin_id='pin-1',
        document_id=DOC,
        measurement_ref='meas-001',
        entry=asset,
        pinned_at_utc='2026-10-05T01:00:00Z',
    )
    assert (
        measurement_stimulus_pin_state((pin,), 'meas-001') == 'pinned'
    )
    allowed, _reason = stimulus_claim_allowed(
        pins=(pin,), measurement_ref='meas-001', claim='calibrated'
    )
    assert allowed


# ---------------------------------------------------------------------------
# Bass-management qualification fixtures (#574)


def _curve(
    level_db: float,
    *,
    phase_deg: float = 0.0,
    delay_s: float | None = None,
    polarity: str = 'normal',
    dip_hz: float | None = None,
    dip_db: float = 0.0,
) -> ResponseCurve:
    freqs = (40.0, 56.0, 70.0, 80.0, 90.0, 113.0, 160.0)
    mags = []
    phases = []
    for f in freqs:
        m = level_db
        if dip_hz is not None and abs(f - dip_hz) < 1.0:
            m -= dip_db
        mags.append(m)
        phases.append(phase_deg)
    return ResponseCurve(
        frequencies_hz=freqs,
        magnitude_db=tuple(mags),
        phase_deg=tuple(phases),
        delay_s_applied=delay_s,
        polarity=polarity,
    )


def _profile(
    *,
    lifecycle: str = 'applied',
    handling: str = 'high_pass',
    filter_family: str = 'linkwitz_riley',
    destinations: tuple[str, ...] = ('sub-main',),
    lfe: bool = True,
    duplication_policy: str = 'independent',
):
    rules = (
        MainChannelBassRule(
            logical_role_id='front_left',
            handling=handling,
            high_pass=(
                CrossoverSpec(
                    frequency_hz=80.0,
                    slope_db_per_octave=24.0,
                    filter_family=filter_family,
                )
                if handling == 'high_pass'
                else None
            ),
            redirected_destinations=destinations,
        ),
    )
    return build_bass_management_profile(
        profile_id='bm-avr-1',
        version='1',
        lifecycle=lifecycle,
        processor_ref='avr-1',
        main_rules=rules,
        lfe_path=(
            LFEPathRule(
                lfe_input_id='lfe',
                low_pass=CrossoverSpec(
                    frequency_hz=120.0,
                    slope_db_per_octave=24.0,
                    filter_family='linkwitz_riley',
                ),
                in_band_boost_db=10.0,
                destinations=('sub-main',),
                duplication_policy=duplication_policy,
            )
            if lfe
            else None
        ),
        sub_groups=(
            SubwooferOutputGroup(
                group_id='sub-main',
                grouping='independent_output',
                member_sub_ids=('sub-1',),
            ),
        ),
    )


def _evidence(
    path: str,
    curve: ResponseCurve,
    *,
    seat: str = 'seat-1',
    seat_role: str = 'control',
    state: str = 'remeasured_post_apply',
    evidence_id: str | None = None,
):
    return build_splice_evidence(
        evidence_id=evidence_id or f'ev-{path}-{seat}',
        document_id=DOC,
        role_id='front_left',
        sub_group_id='sub-main',
        seat_id=seat,
        seat_role=seat_role,
        path=path,
        curve=curve,
        observed_state=state,
        captured_at_utc='2026-10-05T03:00:00Z',
    )


def _usable_band() -> dict[str, UsableBandEvidence]:
    return {
        'front_left': UsableBandEvidence(
            main_usable_low_hz=55.0, sub_usable_high_hz=160.0
        )
    }


def _headroom() -> dict[str, SubHeadroomBudget]:
    return {
        'sub-main': SubHeadroomBudget(
            sub_group_id='sub-main',
            loads=(
                SubPathLoad(kind='redirected_bass', level_db=-10.0),
                SubPathLoad(kind='lfe', level_db=-10.0),
                SubPathLoad(kind='eq_boost', level_db=-20.0),
            ),
            capability_headroom_db=12.0,
            level_axis='dbfs',
            required_margin_db=3.0,
        )
    }


# --- BMQ: splice unit behaviour ----------------------------------------------


def test_bmq10_constructive_splice():
    verdict = evaluate_splice(
        main_curve=_curve(-12.0),
        sub_curve=_curve(-12.0),
        summed_curve=_curve(-6.0),  # +6 dB coherent sum
        crossover_band_hz=(56.6, 113.0),
    )
    assert verdict.status == 'constructive'
    assert not verdict.failure_reasons
    assert verdict.metrics.worst_splice_margin_db == pytest.approx(6.0)


def test_bmq20_cancellation_detected():
    verdict = evaluate_splice(
        main_curve=_curve(-10.0),
        sub_curve=_curve(-10.0),
        summed_curve=_curve(-10.0, dip_hz=80.0, dip_db=12.0),
        crossover_band_hz=(56.6, 113.0),
    )
    assert verdict.status == 'cancellation'
    assert 'phase_cancellation_at_splice' in verdict.failure_reasons
    assert verdict.metrics.worst_splice_margin_db == pytest.approx(-12.0)
    assert verdict.metrics.worst_margin_frequency_hz == pytest.approx(80.0)


def test_bmq30_polarity_mismatch_attributed():
    verdict = evaluate_splice(
        main_curve=_curve(-10.0),
        sub_curve=_curve(-10.0, polarity='inverted'),
        summed_curve=_curve(-10.0, dip_hz=80.0, dip_db=15.0),
        crossover_band_hz=(56.6, 113.0),
    )
    assert verdict.status == 'cancellation'
    assert 'polarity_mismatch' in verdict.failure_reasons
    assert 'phase_cancellation_at_splice' not in verdict.failure_reasons


def test_bmq40_delay_mismatch_attributed():
    # 80 Hz period is 12.5 ms — a 6.25 ms delay is λ/2 (cancels)
    assert delay_cancels(0.00625, 80.0)
    assert not delay_cancels(0.0125, 80.0)  # full period → in phase
    verdict = evaluate_splice(
        main_curve=_curve(-10.0),
        sub_curve=_curve(-10.0, delay_s=0.00625),
        summed_curve=_curve(-10.0, dip_hz=80.0, dip_db=10.0),
        crossover_band_hz=(56.6, 113.0),
    )
    assert verdict.status == 'cancellation'
    assert 'delay_mismatch' in verdict.failure_reasons


def test_bmq50_prediction_error_flags_device_state():
    """Isolated paths predict a constructive complex sum, but the measured
    sum disagrees — the deployed device state is not the declared one."""
    main = _curve(-12.0, phase_deg=0.0)
    sub = _curve(-12.0, phase_deg=0.0)
    predicted_db = -12.0 + 6.02  # coherent in-phase sum
    measured = _curve(predicted_db - 8.0)  # 8 dB below prediction
    verdict = evaluate_splice(
        main_curve=main,
        sub_curve=sub,
        summed_curve=measured,
        crossover_band_hz=(56.6, 113.0),
        prediction_tolerance_db=3.0,
    )
    assert 'device_state_mismatch' in verdict.failure_reasons
    assert (
        verdict.metrics.predicted_vs_measured_error_db
        == pytest.approx(8.0, abs=0.2)
    )


def test_bmq51_missing_isolated_paths_is_unknown():
    verdict = evaluate_splice(
        main_curve=None,
        sub_curve=_curve(-10.0),
        summed_curve=_curve(-6.0),
        crossover_band_hz=(56.6, 113.0),
    )
    assert verdict.status == 'unknown'
    verdict2 = evaluate_splice(
        main_curve=_curve(-10.0),
        sub_curve=_curve(-10.0),
        summed_curve=None,
        crossover_band_hz=(56.6, 113.0),
    )
    assert verdict2.status == 'unknown'


def test_bmq60_usable_band_gates():
    status, reasons, _ = evaluate_usable_band(
        crossover_hz=80.0, evidence=_usable_band()['front_left']
    )
    assert status == 'pass' and not reasons
    status, reasons, _ = evaluate_usable_band(
        crossover_hz=80.0,
        evidence=UsableBandEvidence(
            main_usable_low_hz=78.0, sub_usable_high_hz=160.0
        ),
    )
    assert status == 'fail'
    assert 'main_too_weak_below_crossover' in reasons
    status, reasons, _ = evaluate_usable_band(crossover_hz=80.0, evidence=None)
    assert status == 'not_evaluated' and not reasons


def test_bmq70_sub_headroom_gates():
    status, reasons, _ = evaluate_sub_headroom(_headroom()['sub-main'])
    assert status == 'pass' and not reasons
    overloaded = SubHeadroomBudget(
        sub_group_id='sub-main',
        loads=(
            SubPathLoad(kind='redirected_bass', level_db=4.0),
            SubPathLoad(kind='lfe', level_db=4.0),
        ),
        capability_headroom_db=6.0,
        level_axis='dbfs',
    )
    status, reasons, detail = evaluate_sub_headroom(overloaded)
    assert status == 'fail'
    assert 'insufficient_sub_headroom' in reasons
    assert 'exceeds declared headroom' in detail
    status, reasons, _ = evaluate_sub_headroom(None)
    assert status == 'not_evaluated'


# --- BMQ: qualification verdict synthesis ------------------------------------


def _full_evidence(seat: str = 'seat-1', seat_role: str = 'control'):
    return (
        _evidence(
            'main_only', _curve(-12.0), seat=seat, seat_role=seat_role,
            evidence_id=f'ev-main-{seat}',
        ),
        _evidence(
            'sub_only', _curve(-12.0), seat=seat, seat_role=seat_role,
            evidence_id=f'ev-sub-{seat}',
        ),
        _evidence(
            'main_plus_sub_summed',
            _curve(-6.0),
            seat=seat,
            seat_role=seat_role,
            evidence_id=f'ev-sum-{seat}',
        ),
    )


def test_bmq80_qualified_region_requires_region_evidence():
    qualification = evaluate_bass_management_qualification(
        profile=_profile(lifecycle='applied'),
        document_id=DOC,
        evidence=(
            *_full_evidence('seat-1', 'control'),
            *_full_evidence('seat-2', 'holdout'),
        ),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        processing_order=(
            ProcessingStage(kind='bass_management_hpf_lpf'),
        ),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert qualification.status == 'qualified'
    assert qualification.scope == 'qualified_region'
    assert qualification.qualification_id.startswith('bmq-')
    assert gate_status_for(qualification.gates, 'routing') == 'pass'
    assert gate_status_for(qualification.gates, 'deployed_state') == 'pass'


def test_bmq81_control_seat_cancellation_is_not_qualified():
    evidence = list(_full_evidence('seat-1', 'control'))
    evidence[2] = _evidence(
        'main_plus_sub_summed',
        _curve(-10.0, dip_hz=80.0, dip_db=12.0),
        seat='seat-1',
        seat_role='control',
        evidence_id='ev-sum-seat-1',
    )
    qualification = evaluate_bass_management_qualification(
        profile=_profile(lifecycle='applied'),
        document_id=DOC,
        evidence=tuple(evidence),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert qualification.status == 'not_qualified'
    assert qualification.scope == 'unqualified'
    assert 'phase_cancellation_at_splice' in qualification.failure_reasons


def test_bmq82_holdout_cancellation_caps_scope():
    evidence = list(_full_evidence('seat-1', 'control'))
    evidence += list(_full_evidence('seat-2', 'holdout'))
    evidence[5] = _evidence(
        'main_plus_sub_summed',
        _curve(-10.0, dip_hz=80.0, dip_db=12.0),
        seat='seat-2',
        seat_role='holdout',
        evidence_id='ev-sum-seat-2',
    )
    qualification = evaluate_bass_management_qualification(
        profile=_profile(lifecycle='applied'),
        document_id=DOC,
        evidence=tuple(evidence),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert 'multi_seat_instability' in qualification.failure_reasons
    assert qualification.scope != 'qualified_region'


def test_bmq83_no_splice_evidence_is_insufficient():
    qualification = evaluate_bass_management_qualification(
        profile=_profile(lifecycle='applied'),
        document_id=DOC,
        evidence=(),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert qualification.status == 'insufficient_evidence'
    assert qualification.scope == 'candidate'


def test_bmq84_double_bass_flagged():
    qualification = evaluate_bass_management_qualification(
        profile=_profile(lifecycle='applied'),
        document_id=DOC,
        evidence=_full_evidence(),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        double_bass_roles=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert 'double_bass' in qualification.failure_reasons
    assert qualification.status == 'not_qualified'


def test_bmq85_lfe_routing_error():
    profile = _profile(lifecycle='applied')
    broken = build_bass_management_profile(
        profile_id='bm-avr-1',
        version='2',
        lifecycle='applied',
        main_rules=profile.main_rules,
        lfe_path=LFEPathRule(
            lfe_input_id='lfe',
            destinations=('nowhere-real',),
            duplication_policy='independent',
        ),
        sub_groups=profile.sub_groups,
    )
    qualification = evaluate_bass_management_qualification(
        profile=broken,
        document_id=DOC,
        evidence=_full_evidence(),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert qualification.status == 'not_qualified'
    assert 'lfe_routing_error' in qualification.failure_reasons


def test_bmq86_unknown_topology_is_limitation_not_pass():
    qualification = evaluate_bass_management_qualification(
        profile=_profile(lifecycle='applied', filter_family='unknown'),
        document_id=DOC,
        evidence=(
            *_full_evidence('seat-1', 'control'),
            *_full_evidence('seat-2', 'holdout'),
        ),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert gate_status_for(qualification.gates, 'filter_topology') == (
        'limitation'
    )
    assert qualification.status == 'qualified_with_limitations'
    assert qualification.limitations


def test_bmq87_nominal_declared_evidence_caps_scope():
    evidence = _full_evidence('seat-1', 'control')
    evidence = tuple(
        build_splice_evidence(
            evidence_id=ev.evidence_id,
            document_id=DOC,
            role_id=ev.role_id,
            sub_group_id=ev.sub_group_id,
            seat_id=ev.seat_id,
            seat_role=ev.seat_role,
            path=ev.path,
            curve=ev.curve,
            observed_state='nominal_declared',
            captured_at_utc='2026-10-05T03:00:00Z',
        )
        for ev in evidence
    )
    qualification = evaluate_bass_management_qualification(
        profile=_profile(lifecycle='applied'),
        document_id=DOC,
        evidence=evidence,
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert (
        gate_status_for(qualification.gates, 'deployed_state')
        == 'limitation'
    )
    assert qualification.scope != 'qualified_region'


def test_bmq88_qualification_binds_profile_hash():
    """A profile edited after evaluation can never reuse the verdict."""
    profile = _profile(lifecycle='applied')
    q1 = evaluate_bass_management_qualification(
        profile=profile,
        document_id=DOC,
        evidence=_full_evidence(),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    assert q1.profile_sha256 == profile.profile_sha256
    tampered = q1.model_copy(
        update={'status': 'qualified', 'scope': 'qualified_region'}
    )
    with pytest.raises(ValidationError):
        BassManagementQualification.model_validate(
            tampered.model_dump()
        )


# --- BMQ: repository ----------------------------------------------------------


def test_bmq90_repository_round_trip_and_append_only(tmp_path):
    repository = CadBassQualificationRepository(_scene_repo(tmp_path))
    ev = _full_evidence()[0]
    repository.save_evidence(ev)
    assert repository.get_evidence(ev.evidence_id) == ev
    repository.save_evidence(ev)  # no-op
    divergent = build_splice_evidence(
        evidence_id=ev.evidence_id,
        document_id=DOC,
        role_id=ev.role_id,
        sub_group_id=ev.sub_group_id,
        seat_id=ev.seat_id,
        seat_role='holdout',
        path=ev.path,
        curve=ev.curve,
        observed_state=ev.observed_state,
        captured_at_utc='2026-10-05T03:00:00Z',
    )
    with pytest.raises(BassQualificationConflictError):
        repository.save_evidence(divergent)

    qualification = evaluate_bass_management_qualification(
        profile=_profile(lifecycle='applied'),
        document_id=DOC,
        evidence=_full_evidence(),
        usable_band=_usable_band(),
        headroom_budgets=_headroom(),
        expected_role_ids=('front_left',),
        evaluated_at_utc='2026-10-05T04:00:00Z',
    )
    repository.save_qualification(qualification)
    assert (
        repository.get_qualification(qualification.qualification_id)
        == qualification
    )
    listed = repository.qualifications_for_profile(
        qualification.profile_sha256
    )
    assert qualification in listed


def test_bmq91_row_payload_tamper_detected(tmp_path):
    scene_repo = _scene_repo(tmp_path)
    repository = CadBassQualificationRepository(scene_repo)
    ev = _full_evidence()[0]
    repository.save_evidence(ev)
    with sqlite3.connect(scene_repo.path) as connection:
        connection.execute(
            'UPDATE cad_bass_splice_evidence SET path=? WHERE evidence_id=?',
            ('lfe_only', ev.evidence_id),
        )
    with pytest.raises(BassQualificationIntegrityError):
        repository.get_evidence(ev.evidence_id)


# --- UI display helpers --------------------------------------------------------


def test_ui_labels_cover_taxonomies():
    from htdt.measurement_evidence_display import (
        bass_failure_label,
        bass_status_label,
        stimulus_pin_line,
        stimulus_verdict_label,
    )

    for verdict in (
        'ELIGIBLE',
        'ELIGIBLE_WITH_LIMITATIONS',
        'WRONG_REVISION',
        'WRONG_SAMPLE_RATE',
        'WRONG_LEVEL_OR_CREST_FACTOR',
        'TRANSFORMED_NOT_BIT_EXACT',
        'INCOMPATIBLE',
        'INSUFFICIENT_EVIDENCE',
    ):
        assert stimulus_verdict_label(verdict) != verdict
    for reason in (
        'phase_cancellation_at_splice',
        'polarity_mismatch',
        'delay_mismatch',
        'lfe_routing_error',
        'redirected_bass_routing_error',
        'double_bass',
        'insufficient_sub_headroom',
        'unknown_device_filter_topology',
        'device_state_mismatch',
        'multi_seat_instability',
        'main_too_weak_below_crossover',
        'sub_too_weak_above_crossover',
    ):
        assert bass_failure_label(reason) != reason
    assert bass_status_label('qualified') == '適格'
    pin = build_stimulus_pin(
        pin_id='pin-1',
        document_id=DOC,
        measurement_ref='meas-001',
        entry=_ess_asset(),
        pinned_at_utc='2026-10-05T01:00:00Z',
    )
    assert 'pin-1' not in stimulus_pin_line(pin)
    assert 'stim-ess-20s' in stimulus_pin_line(pin)
