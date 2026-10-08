"""REV59-DIGCHAIN regression tests — clock jitter (#745), word-length /
dither path (#744), playback SRC (#739), interchannel crosstalk (#650)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_clock_jitter import (
    ConverterJitterSusceptibility,
    JitterTransferMeasurement,
    SampleClockJitterObservation,
    SampleClockJitterProfile,
    evaluate_jitter_claim,
)
from htdt.cad_interchannel_crosstalk import (
    ChannelSeparationQualification,
    InterchannelLeakageMeasurement,
    evaluate_separation_claim,
)
from htdt.cad_playback_src import (
    ClockDomainCrossingRecord,
    PlaybackSrcProfile,
    SrcQualificationRecord,
    evaluate_src_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_signal_integrity_repository import (
    CadSignalIntegrityRepository,
    SignalIntegrityIntegrityError,
)
from htdt.cad_wordlength_path import (
    DigitalPathTransformRecord,
    DitherNoiseShapeProfile,
    evaluate_low_level_claim,
)
from htdt.canonical_json import canonical_sha256


DOC = 'doc-digchain'
_SHA = canonical_sha256({'fixture': 'sha'})
_OTHER_SHA = canonical_sha256({'fixture': 'other'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _jitter_profile(**kw) -> SampleClockJitterProfile:
    payload = dict(
        document_id=DOC,
        covered_kinds=('wideband', 'spectrum'),
        spectrum_capable=True,
        instrument_ref=_ref('instrument', 'inst-1'),
    )
    payload.update(kw)
    return SampleClockJitterProfile.create(**payload)


def _jitter_obs(**kw) -> SampleClockJitterObservation:
    p = _jitter_profile()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref(
            'jitter_profile', p.profile_id, p.profile_sha256),
        clock_domain='dac',
        jitter_kind='wideband',
        value_ps=42.0,
    )
    payload.update(kw)
    return SampleClockJitterObservation.create(**payload)


def _dither_profile(**kw) -> DitherNoiseShapeProfile:
    payload = dict(
        document_id=DOC,
        dither_kind='non_subtractive_tpdf',
        target_word_length_bits=16,
    )
    payload.update(kw)
    return DitherNoiseShapeProfile.create(**payload)


def _transform(**kw) -> DigitalPathTransformRecord:
    payload = dict(
        document_id=DOC,
        transform_kind='requantization',
        input_representation='floating_point',
        output_representation='integer_pcm',
        output_word_length_bits=16,
        rounding_mode='dithered',
        dither_profile_ref=_ref('dither_profile', 'dns-1'),
    )
    payload.update(kw)
    return DigitalPathTransformRecord.create(**payload)


def _src_profile(**kw) -> PlaybackSrcProfile:
    payload = dict(
        document_id=DOC,
        input_rate_hz=44100.0,
        output_rate_hz=48000.0,
        algorithm='asynchronous_src',
        crossing_kind='asrc_crossing',
    )
    payload.update(kw)
    return PlaybackSrcProfile.create(**payload)


def _src_qual(
    profile: PlaybackSrcProfile, **kw
) -> SrcQualificationRecord:
    payload = dict(
        document_id=DOC,
        src_profile_ref=_ref(
            'playback_src_profile',
            profile.profile_id,
            profile.profile_sha256,
        ),
        alias_rejection_db=-100.0,
        measurement_ref=_ref('measurement', 'm-1'),
    )
    payload.update(kw)
    return SrcQualificationRecord.create(**payload)


def _leakage(**kw) -> InterchannelLeakageMeasurement:
    payload = dict(
        document_id=DOC,
        driven_channel='L',
        observed_channel='C',
        stage='power_amplifier',
        method='iec60268_3',
        level_db=-85.0,
    )
    payload.update(kw)
    return InterchannelLeakageMeasurement.create(**payload)


def _separation(**kw) -> ChannelSeparationQualification:
    payload = dict(
        document_id=DOC,
        stage='power_amplifier',
        threshold_db=-60.0,
        worst_case_db=-75.0,
        measured_pairs=6,
        required_pairs=6,
        verdict='qualified',
    )
    payload.update(kw)
    return ChannelSeparationQualification.create(**payload)


class TestClockJitter:
    def test_sealed_create(self) -> None:
        p = _jitter_profile()
        assert p.profile_id.startswith('jmp-')
        assert len(p.profile_sha256) == 64

    def test_profile_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValidationError):
            _jitter_profile(covered_kinds=('wideband', 'unknown'))

    def test_spectrum_capable_requires_spectrum_kind(self) -> None:
        with pytest.raises(ValidationError):
            _jitter_profile(
                covered_kinds=('wideband',), spectrum_capable=True)

    def test_observation_requires_kind_and_value(self) -> None:
        with pytest.raises(ValidationError):
            _jitter_obs(jitter_kind='unknown')
        with pytest.raises(ValidationError):
            _jitter_obs(value_ps=None, measurement_data_ref=None)

    def test_observation_requires_pinned_profile(self) -> None:
        with pytest.raises(ValidationError):
            _jitter_obs(profile_ref=AuthorityRef(
                kind='jitter_profile', ref_id='x', ref_sha256=None))

    def test_lock_only_not_low_jitter(self) -> None:
        verdict, reason = evaluate_jitter_claim(
            'locked_operation', (), None)
        assert verdict == 'supported_with_limitations'
        assert reason == 'lock_only_not_jitter'

    def test_low_jitter_needs_observation(self) -> None:
        verdict, _ = evaluate_jitter_claim('low_jitter', (), None)
        assert verdict == 'insufficient_evidence'
        verdict, reason = evaluate_jitter_claim(
            'low_jitter', (_jitter_obs(),), None)
        assert verdict == 'supported_with_limitations'
        assert reason == 'scalar_only'
        obs = _jitter_obs(jitter_kind='spectrum')
        verdict, _ = evaluate_jitter_claim(
            'low_jitter', (obs,), None)
        assert verdict == 'supported'

    def test_immunity_needs_susceptibility(self) -> None:
        verdict, reason = evaluate_jitter_claim(
            'converter_immune', (_jitter_obs(),), None)
        assert verdict == 'insufficient_evidence'
        assert reason == 'no_susceptibility_test'
        sus = ConverterJitterSusceptibility.create(
            document_id=DOC,
            converter_ref=_ref('converter', 'dac-1'),
            test_method_ref=_ref('test_method', 'tm-1'),
            result_data_ref=_ref('data', 'd-1'),
            susceptible=False,
        )
        verdict, _ = evaluate_jitter_claim(
            'converter_immune', (_jitter_obs(),), sus)
        assert verdict == 'supported'

    def test_susceptibility_verdict_needs_data(self) -> None:
        with pytest.raises(ValidationError):
            ConverterJitterSusceptibility.create(
                document_id=DOC,
                converter_ref=_ref('converter', 'dac-1'),
                test_method_ref=_ref('test_method', 'tm-1'),
                susceptible=False,
            )


class TestWordlengthPath:
    def test_sealed_create(self) -> None:
        p = _dither_profile()
        assert p.profile_id.startswith('dns-')

    def test_noise_shaped_requires_order(self) -> None:
        with pytest.raises(ValidationError):
            _dither_profile(dither_kind='noise_shaped')
        with pytest.raises(ValidationError):
            _dither_profile(noise_shape_order=5)

    def test_requantization_needs_rounding_or_dither(self) -> None:
        with pytest.raises(ValidationError):
            _transform(rounding_mode='unknown', dither_profile_ref=None)

    def test_dithered_requires_profile(self) -> None:
        with pytest.raises(ValidationError):
            _transform(dither_profile_ref=None)

    def test_digital_gain_requires_gain(self) -> None:
        with pytest.raises(ValidationError):
            _transform(
                transform_kind='digital_gain', rounding_mode='unknown',
                dither_profile_ref=None, gain_db=None)

    def test_undithered_truncation_uncontrolled(self) -> None:
        t = _transform(
            rounding_mode='truncation', dither_profile_ref=None)
        verdict, reason = evaluate_low_level_claim(16, (t,))
        assert verdict == 'uncontrolled'
        assert reason == 'undithered_requantization'

    def test_dithered_path_controlled(self) -> None:
        verdict, reason = evaluate_low_level_claim(16, (_transform(),))
        assert verdict == 'controlled'
        assert reason == 'all_requantizations_dithered'

    def test_no_evidence_insufficient(self) -> None:
        verdict, _ = evaluate_low_level_claim(16, ())
        assert verdict == 'insufficient_evidence'

    def test_clipping_uncontrolled(self) -> None:
        t = _transform(
            transform_kind='clipping_event',
            rounding_mode='round_to_nearest',
            dither_profile_ref=None,
        )
        verdict, _ = evaluate_low_level_claim(24, (t,))
        assert verdict == 'uncontrolled'


class TestPlaybackSrc:
    def test_sealed_create(self) -> None:
        p = _src_profile()
        assert p.profile_id.startswith('srcp-')

    def test_algorithm_must_be_declared(self) -> None:
        with pytest.raises(ValidationError):
            _src_profile(algorithm='unknown')

    def test_equal_rates_not_async(self) -> None:
        with pytest.raises(ValidationError):
            _src_profile(
                input_rate_hz=48000.0, output_rate_hz=48000.0,
                algorithm='asynchronous_src')

    def test_qualification_needs_figure(self) -> None:
        with pytest.raises(ValidationError):
            _src_qual(
                _src_profile(),
                alias_rejection_db=None, passband_ripple_db=None)

    def test_crossing_observed_needs_evidence(self) -> None:
        with pytest.raises(ValidationError):
            ClockDomainCrossingRecord.create(
                document_id=DOC,
                input_domain='spdif',
                output_domain='system',
                observed_kind='asrc_crossing',
            )

    def test_exact_claim_not_sample_exact_with_src(self) -> None:
        p = _src_profile()
        q = _src_qual(p)
        verdict, reason = evaluate_src_claim(
            (p,), (q,), exact_sample_claim=True)
        assert verdict == 'converted_qualified'
        assert reason == 'identity_claim_impossible_with_src'

    def test_unqualified_stage(self) -> None:
        verdict, reason = evaluate_src_claim((_src_profile(),), ())
        assert verdict == 'converted_unqualified'
        assert reason == 'unqualified_conversion_stage'

    def test_hidden_conversion(self) -> None:
        c = ClockDomainCrossingRecord.create(
            document_id=DOC,
            input_domain='spdif',
            output_domain='system',
            declared_kind='same_domain',
            observed_kind='asrc_crossing',
            observation_ref=_ref('observation', 'o-1'),
        )
        verdict, _ = evaluate_src_claim((), (), (c,))
        assert verdict == 'hidden_conversion'

    def test_hidden_conversion_drift_compensated(self) -> None:
        # Drift compensation is a clock-domain crossing mechanism — a
        # declared same-domain path observed compensating is equally a
        # hidden conversion.
        c = ClockDomainCrossingRecord.create(
            document_id=DOC,
            input_domain='spdif',
            output_domain='system',
            declared_kind='same_domain',
            observed_kind='drift_compensated',
            observation_ref=_ref('observation', 'o-1'),
        )
        verdict, _ = evaluate_src_claim((), (), (c,))
        assert verdict == 'hidden_conversion'

    def test_no_profile_exact(self) -> None:
        verdict, _ = evaluate_src_claim((), (), ())
        assert verdict == 'sample_exact'


class TestInterchannelCrosstalk:
    def test_sealed_create(self) -> None:
        m = _leakage()
        assert m.measurement_id.startswith('xtk-')

    def test_same_channel_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _leakage(observed_channel='L')

    def test_method_must_be_declared(self) -> None:
        with pytest.raises(ValidationError):
            _leakage(method='unknown')

    def test_measurement_needs_value(self) -> None:
        with pytest.raises(ValidationError):
            _leakage(level_db=None, measurement_data_ref=None)

    def test_threshold_must_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            _separation(threshold_db=0.0)

    def test_qualified_requires_full_pairs(self) -> None:
        with pytest.raises(ValidationError):
            _separation(measured_pairs=3)
        with pytest.raises(ValidationError):
            _separation(worst_case_db=-50.0)

    def test_routing_alone_insufficient(self) -> None:
        verdict, reason = evaluate_separation_claim(True, None)
        assert verdict == 'insufficient_evidence'
        assert reason == 'no_separation_evidence'

    def test_qualified_verdict(self) -> None:
        verdict, _ = evaluate_separation_claim(True, _separation())
        assert verdict == 'qualified'

    def test_qualified_without_routing_limited(self) -> None:
        verdict, _ = evaluate_separation_claim(False, _separation())
        assert verdict == 'qualified_with_limitations'

    def test_unqualified_propagates(self) -> None:
        q = _separation(verdict='unqualified')
        verdict, _ = evaluate_separation_claim(True, q)
        assert verdict == 'unqualified'


def _repo(tmp_path: Path) -> CadSignalIntegrityRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadSignalIntegrityRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    jp = _jitter_profile()
    jo = _jitter_obs()
    jt_record = JitterTransferMeasurement.create(
        document_id=DOC,
        profile_ref=_ref(
            'jitter_profile', jp.profile_id, jp.profile_sha256),
        input_domain='spdif',
        output_domain='system',
        transfer_data_ref=_ref('data', 't-1'),
    )
    sus = ConverterJitterSusceptibility.create(
        document_id=DOC,
        converter_ref=_ref('converter', 'dac-1'),
        test_method_ref=_ref('test_method', 'tm-1'),
    )
    dns = _dither_profile()
    dpt = _transform()
    srcp = _src_profile()
    srcq = _src_qual(srcp)
    cdc = ClockDomainCrossingRecord.create(
        document_id=DOC,
        input_domain='spdif',
        output_domain='system',
        declared_kind='asrc_crossing',
    )
    xtk = _leakage()
    csep = _separation()

    repo.save_jitter_profile(jp)
    repo.save_jitter_observation(jo)
    repo.save_jitter_transfer(jt_record)
    repo.save_converter_susceptibility(sus)
    repo.save_dither_profile(dns)
    repo.save_path_transform(dpt)
    repo.save_src_profile(srcp)
    repo.save_src_qualification(srcq)
    repo.save_clock_crossing(cdc)
    repo.save_leakage_measurement(xtk)
    repo.save_separation_qualification(csep)

    assert repo.get_jitter_profile(jp.profile_id) == jp
    assert repo.get_jitter_observation(jo.observation_id) == jo
    assert repo.get_jitter_transfer(
        jt_record.measurement_id) == jt_record
    assert repo.get_converter_susceptibility(
        sus.susceptibility_id) == sus
    assert repo.get_dither_profile(dns.profile_id) == dns
    assert repo.get_path_transform(dpt.transform_id) == dpt
    assert repo.get_src_profile(srcp.profile_id) == srcp
    assert repo.get_src_qualification(
        srcq.qualification_id) == srcq
    assert repo.get_clock_crossing(cdc.crossing_id) == cdc
    assert repo.get_leakage_measurement(
        xtk.measurement_id) == xtk
    assert repo.get_separation_qualification(
        csep.qualification_id) == csep


def test_repository_idempotent_resave(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    jp = _jitter_profile()
    repo.save_jitter_profile(jp)
    repo.save_jitter_profile(jp)


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    xtk = _leakage()
    repo.save_leakage_measurement(xtk)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_interchannel_leakage_measurements '
            "SET stage='dac_stage' WHERE measurement_id=?",
            (xtk.measurement_id,),
        )
        connection.commit()
    with pytest.raises(SignalIntegrityIntegrityError):
        repo.get_leakage_measurement(xtk.measurement_id)


def test_digchain_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_jitter_profiles',
        'cad_jitter_observations',
        'cad_jitter_transfer_measurements',
        'cad_converter_jitter_susceptibility',
        'cad_dither_profiles',
        'cad_digital_path_transforms',
        'cad_playback_src_profiles',
        'cad_src_qualifications',
        'cad_clock_domain_crossings',
        'cad_interchannel_leakage_measurements',
        'cad_channel_separation_qualifications',
    }
    assert expected <= tables
