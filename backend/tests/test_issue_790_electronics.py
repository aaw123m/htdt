"""Issue #790 regression tests — playback-electronics / electrical
audio-path qualification authority.

Covers the sealed-record contract, every evaluator verdict path
(fail-closed especially), the interface de-embedding semantics (#699),
path/state applicability isolation, channel matching vs crosstalk
separation (#650), domain separation vs #192/#695, device-epoch staling
(#592/#595), and the repository's verified read/write paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_playback_electronics import (
    ElectricalTransferMeasurement,
    ElectronicAudioPathProfile,
    ElectronicLinearityEvidence,
    ElectronicsLevelPoint,
    NonlinearQuantityEvidence,
    PlaybackElectronicsQualification,
    evaluate_electronics_claim,
)
from htdt.cad_playback_electronics_repository import (
    CadPlaybackElectronicsRepository,
    PlaybackElectronicsConflictError,
    PlaybackElectronicsIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256
from htdt.native_authority_audit import audit_table_modes
from htdt.native_row_integrity import (
    assert_row_integrity_registry_complete,
    scan_native_row_integrity,
)


DOC = 'doc-e790'
_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _eapp(**kw) -> ElectronicAudioPathProfile:
    payload = dict(
        document_id=DOC,
        path_class='digital_in_analog_line_out',
        input_endpoint='player:hdmi:main',
        output_endpoint='avr:preout:FL',
        path_stages=(
            'media_output', 'digital_interface', 'dsp',
            'dac', 'preamp'),
        device_model='AVR-X4800H',
        firmware_version='2.1.0',
        channel_scope='single_channel',
        sample_rate_hz=48000.0,
        sample_format='pcm24',
        volume_position='-10.0dB',
        processing_preset='stereo',
        observed_processing_states=('none_detected',),
        channels_driven=1,
        load_kind='line_level_input',
        thermal_state='warmed_up',
        measurement_interface_ref=_ref('interface', 'if-1'),
        device_state_ref=_ref('device_state', 'epoch-a'),
    )
    payload.update(kw)
    return ElectronicAudioPathProfile.create(**payload)


def _etm(
    profile: ElectronicAudioPathProfile | None = None,
    **kw,
) -> ElectricalTransferMeasurement:
    p = profile if profile is not None else _eapp()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('eap', p.profile_id, p.profile_sha256),
        channel_label='FL',
        measurement_class='linear_transfer',
        evidence_origin='measured',
        path_class_observed='digital_in_analog_line_out',
        frequency_low_hz=20.0,
        frequency_high_hz=20000.0,
        gain_db=-0.05,
        magnitude_response_ref=_ref('fr', 'fr-1'),
        phase_response_ref=_ref('fr', 'fr-2'),
        delay_ms=0.42,
        delay_method='pure_delay',
        stimulus_kind='sweep',
        stimulus_level=-3.0,
        stimulus_level_unit='dbfs',
        deembedding_state='de_embedded',
        interface_correction_ref=_ref('interface_correction', 'corr-1'),
        method_references=('AES17@2020',),
        sample_rate_hz=48000.0,
        input_format='pcm24/48k',
        output_format='analog_line',
        reference_convention='volts_rms',
        clock_state='common_clock',
        uncertainty_db=0.2,
    )
    payload.update(kw)
    return ElectricalTransferMeasurement.create(**payload)


def _ele(
    profile: ElectronicAudioPathProfile | None = None,
    **kw,
) -> ElectronicLinearityEvidence:
    p = profile if profile is not None else _eapp()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('eap', p.profile_id, p.profile_sha256),
        domain='electronic_path',
        evidence_origin='measured',
        level_points=(
            ElectronicsLevelPoint(
                input_level_db=-30.0, observed_gain_db=-0.05,
                point_state='linear'),
            ElectronicsLevelPoint(
                input_level_db=-3.0, observed_gain_db=-0.05,
                point_state='linear'),
        ),
        nonlinear_quantities=(
            NonlinearQuantityEvidence(
                quantity='thd_plus_n',
                value=0.003,
                unit='percent',
                method_reference='IEC 60268-3@2018',
                stimulus='1 kHz sine',
                bandwidth_hz=22000.0,
                signal_level='-3 dBFS',
                load='line input 10 kOhm'),
        ),
        observed_regime='linear_range',
        method_references=('AES17@2020',),
        safe_level_bound='output <= 2 Vrms into 10 kOhm',
    )
    payload.update(kw)
    return ElectronicLinearityEvidence.create(**payload)


def _peq(
    profile: ElectronicAudioPathProfile | None = None,
    **kw,
) -> PlaybackElectronicsQualification:
    p = profile if profile is not None else _eapp()
    m = _etm(p)
    e = _ele(p)
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('eap', p.profile_id, p.profile_sha256),
        measurement_refs=(
            _ref('etm', m.measurement_id, m.measurement_sha256),),
        linearity_refs=(
            _ref('ele', e.evidence_id, e.evidence_sha256),),
        qualification_state='qualified_linear_transfer',
        evidence_composition='measured_linear_transfer_applied',
        valid_low_hz=20.0,
        valid_high_hz=20000.0,
        level_domain='swept -30..-3 dBFS',
        max_gain_difference_db=0.1,
        max_phase_difference_deg=2.0,
        bypass_verdict='observed_transfer_confirmed',
        uncertainty_notes='±0.2 dB magnitude',
    )
    payload.update(kw)
    return PlaybackElectronicsQualification.create(**payload)


# ---------------------------------------------------------------------------
# sealed identity + validators
# ---------------------------------------------------------------------------

class TestSealedIdentity:
    def test_create_prefixes(self) -> None:
        assert _eapp().profile_id.startswith('eapp-')
        assert _etm().measurement_id.startswith('etm-')
        assert _ele().evidence_id.startswith('ele-')
        assert _peq().qualification_id.startswith('peq-')

    def test_seal_matches_payload(self) -> None:
        for record, sha_field, id_field in (
            (_eapp(), 'profile_sha256', 'profile_id'),
            (_etm(), 'measurement_sha256', 'measurement_id'),
            (_ele(), 'evidence_sha256', 'evidence_id'),
            (_peq(), 'qualification_sha256', 'qualification_id'),
        ):
            digest = canonical_sha256(record.identity_payload())
            assert getattr(record, sha_field) == digest
            prefix = getattr(record, id_field).rsplit('-', 1)[0]
            assert getattr(record, id_field) \
                == f'{prefix}-{digest[:24]}'

    def test_frozen(self) -> None:
        record = _eapp()
        with pytest.raises(ValidationError):
            record.path_class = 'other'  # type: ignore[misc]


class TestProfileValidation:
    def test_missing_endpoints_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _eapp(input_endpoint='')
        with pytest.raises(ValidationError):
            _eapp(output_endpoint='')

    def test_missing_device_state_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _eapp(device_model='')
        with pytest.raises(ValidationError):
            _eapp(firmware_version='')

    def test_empty_path_stages_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _eapp(path_stages=())

    def test_none_detected_cannot_coexist(self) -> None:
        with pytest.raises(ValidationError):
            _eapp(observed_processing_states=(
                'none_detected', 'eq_filter'))

    def test_refs_must_pin_sha(self) -> None:
        with pytest.raises(ValidationError):
            _eapp(device_state_ref=AuthorityRef(
                kind='device_state', ref_id='epoch-a'))


class TestMeasurementValidation:
    def test_unpinned_profile_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _etm(profile_ref=AuthorityRef(kind='eap', ref_id='p1'))

    def test_no_bound_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _etm(
                gain_db=None,
                magnitude_response_ref=None,
                phase_response_ref=None,
                delay_ms=None,
                noise_level_db=None,
            )

    def test_delay_requires_method(self) -> None:
        with pytest.raises(ValidationError):
            _etm(delay_ms=1.0, delay_method=None)
        with pytest.raises(ValidationError):
            _etm(delay_ms=1.0, delay_method='unknown')

    def test_de_embedded_requires_correction_ref(self) -> None:
        with pytest.raises(ValidationError):
            _etm(
                deembedding_state='de_embedded',
                interface_correction_ref=None,
            )

    def test_negligible_requires_basis(self) -> None:
        with pytest.raises(ValidationError):
            _etm(
                deembedding_state='negligible_with_evidence',
                interface_correction_ref=None,
                notes=None,
            )

    def test_method_references_pin_edition(self) -> None:
        with pytest.raises(ValidationError):
            _etm(method_references=('AES17',))
        assert _etm(method_references=('ANSI/CTA-490@B',))

    def test_inverted_band_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _etm(frequency_low_hz=100.0, frequency_high_hz=50.0)


class TestLinearityValidation:
    def test_empty_evidence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _ele(level_points=(), nonlinear_quantities=())

    def test_linear_range_contradiction_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _ele(
                observed_regime='linear_range',
                level_points=(ElectronicsLevelPoint(
                    input_level_db=0.0, point_state='clipped'),),
            )

    def test_nonlinear_quantity_requires_method_and_stimulus(
        self,
    ) -> None:
        with pytest.raises(ValidationError):
            NonlinearQuantityEvidence(
                quantity='thd', value=0.1, unit='percent',
                method_reference='IEC 60268-3', stimulus='1 kHz')
        with pytest.raises(ValidationError):
            NonlinearQuantityEvidence(
                quantity='thd', value=0.1, unit='percent',
                method_reference='IEC 60268-3@2018', stimulus='')


class TestQualificationValidation:
    def test_qualified_requires_measurements(self) -> None:
        with pytest.raises(ValidationError):
            _peq(measurement_refs=())

    def test_qualified_requires_domain(self) -> None:
        with pytest.raises(ValidationError):
            _peq(valid_low_hz=None)
        with pytest.raises(ValidationError):
            _peq(valid_low_hz=100.0, valid_high_hz=50.0)

    def test_qualified_requires_measured_composition(self) -> None:
        with pytest.raises(ValidationError):
            _peq(evidence_composition='ideal_electronics_assumed')
        with pytest.raises(ValidationError):
            _peq(evidence_composition='device_transfer_unknown')

    def test_qualified_with_limitations_requires_reasons(self) -> None:
        with pytest.raises(ValidationError):
            _peq(
                qualification_state='qualified_with_limitations',
                evidence_composition=(
                    'measured_linear_transfer_applied'),
                limitation_reasons=(),
            )

    def test_unverified_needs_no_evidence(self) -> None:
        q = _peq(
            qualification_state='path_unverified',
            evidence_composition='device_transfer_unknown',
            measurement_refs=(),
            valid_low_hz=None,
            valid_high_hz=None,
        )
        assert q.qualification_state == 'path_unverified'


# ---------------------------------------------------------------------------
# evaluator — fail-closed verdict paths (fixtures EAP10..EAP80)
# ---------------------------------------------------------------------------

class TestEvaluator:
    def test_no_profile_is_unknown(self) -> None:
        verdict, _ = evaluate_electronics_claim(None, (_etm(),))
        assert verdict == 'unknown'

    def test_eap10_transparent_line_path_qualified(self) -> None:
        verdict, detail = evaluate_electronics_claim(
            _eapp(), (_etm(),), (_ele(),), _peq())
        assert verdict == 'electronics_qualified'
        assert 'qualified_linear_transfer' in detail

    def test_unbound_evidence_is_insufficient(self) -> None:
        # Measurement bound to a different profile sha never applies.
        other = _etm(profile_ref=_ref('eap', 'other-id', _SHA))
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (other,), (), _peq())
        assert verdict == 'insufficient_evidence'
        # Predicted/simulated evidence never substitutes for measured.
        predicted = _etm(evidence_origin='predicted')
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (predicted,), (), _peq())
        assert verdict == 'insufficient_evidence'

    def test_path_class_isolation(self) -> None:
        # A measurement taken analog-in -> speaker-out never qualifies
        # the digital-in -> line-out path.
        mismatched = _etm(path_class_observed='analog_in_speaker_out')
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (mismatched,), (), _peq())
        assert verdict == 'insufficient_evidence'

    def test_eap30_sample_rate_state_dependent(self) -> None:
        verdict, detail = evaluate_electronics_claim(
            _eapp(sample_rate_hz=48000.0),
            (_etm(sample_rate_hz=96000.0),),
            (_ele(),),
            _peq(),
        )
        assert verdict == 'state_dependent'
        assert 'sample-rate' in detail

    def test_eap50_interface_confounded_then_clean(self) -> None:
        confounded = _etm(
            deembedding_state='included_in_result',
            interface_correction_ref=None,
        )
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (confounded,), (), _peq())
        assert verdict == 'interface_confounded'
        # After #699 de-embedding the same path qualifies.
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (_etm(),), (), _peq())
        assert verdict == 'electronics_qualified'

    def test_eap20_bypass_label_contradicted(self) -> None:
        profile = _eapp(
            provider_bypass_label='Pure Direct',
            observed_processing_states=('eq_filter', 'bass_management'),
        )
        verdict, detail = evaluate_electronics_claim(
            profile, (_etm(profile),), (), _peq(profile))
        assert verdict == 'processing_detected'
        assert 'eq_filter' in detail

    def test_eap40_hard_limiting_is_nonlinear_limited(self) -> None:
        profile = _eapp(
            channels_driven=7,
            load_kind='resistive_dummy_load',
            load_impedance_ohm=8.0,
            thermal_state='sustained_stress',
        )
        evidence = _ele(
            profile,
            observed_regime='hard_clipping',
            level_points=(
                ElectronicsLevelPoint(
                    input_level_db=-10.0, point_state='linear'),
                ElectronicsLevelPoint(
                    input_level_db=0.0, point_state='clipped'),
            ),
        )
        verdict, _ = evaluate_electronics_claim(
            profile,
            (_etm(profile),), (evidence,), _peq(profile))
        assert verdict == 'nonlinear_limited'

    def test_soft_limiting_qualifies_with_limitations(self) -> None:
        evidence = _ele(
            observed_regime='gain_compression',
            level_points=(
                ElectronicsLevelPoint(
                    input_level_db=-10.0, point_state='linear'),
                ElectronicsLevelPoint(
                    input_level_db=-2.0, point_state='compressed'),
            ),
        )
        verdict, detail = evaluate_electronics_claim(
            _eapp(), (_etm(),), (evidence,), _peq())
        assert verdict == 'electronics_qualified_with_limitations'
        assert 'compressed' in detail

    def test_eap60_channel_mismatch(self) -> None:
        profile = _eapp(channel_matching_tolerance_db=0.5)
        qualification = _peq(profile, max_gain_difference_db=1.2)
        verdict, detail = evaluate_electronics_claim(
            profile, (_etm(profile),), (_ele(profile),), qualification)
        assert verdict == 'channel_mismatch'
        assert '1.2' in detail

    def test_phase_mismatch(self) -> None:
        profile = _eapp(phase_matching_tolerance_deg=5.0)
        qualification = _peq(profile, max_phase_difference_deg=12.0)
        verdict, _ = evaluate_electronics_claim(
            profile, (_etm(profile),), (_ele(profile),), qualification)
        assert verdict == 'channel_mismatch'

    def test_eap70_cross_domain_evidence_does_not_bind(self) -> None:
        # An electroacoustic distortion record (#192 domain) is not
        # electrical-path evidence: a clean electrical path stays
        # qualified even while the acoustic system distorts.
        acoustic = _ele(
            domain='electroacoustic_system',
            observed_regime='hard_clipping',
            level_points=(ElectronicsLevelPoint(
                input_level_db=0.0, point_state='clipped'),),
        )
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (_etm(),), (acoustic,), _peq())
        assert verdict == 'electronics_qualified'

    def test_eap80_stale_after_device_change(self) -> None:
        profile = _eapp()
        moved = _ref('device_state', 'epoch-b')
        verdict, _ = evaluate_electronics_claim(
            profile, (_etm(),), (_ele(),), _peq(),
            current_device_state_ref=moved)
        assert verdict == 'stale_after_device_change'
        # Same epoch keeps the evidence valid.
        verdict, _ = evaluate_electronics_claim(
            profile, (_etm(),), (_ele(),), _peq(),
            current_device_state_ref=profile.device_state_ref)
        assert verdict == 'electronics_qualified'

    def test_staled_by_ref_supersedes(self) -> None:
        qualification = _peq(
            staled_by_ref=_ref('device_change', 'fw-210'))
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (_etm(),), (_ele(),), qualification)
        assert verdict == 'stale_after_device_change'

    def test_no_qualification_record_is_insufficient(self) -> None:
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (_etm(),), (_ele(),), None)
        assert verdict == 'insufficient_evidence'

    def test_qualification_states(self) -> None:
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (_etm(),), (),
            _peq(qualification_state='unknown',
                 evidence_composition='device_transfer_unknown',
                 measurement_refs=(),
                 valid_low_hz=None, valid_high_hz=None))
        assert verdict == 'unknown'
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (_etm(),), (),
            _peq(qualification_state='path_unverified',
                 evidence_composition='device_transfer_unknown',
                 measurement_refs=(),
                 valid_low_hz=None, valid_high_hz=None))
        assert verdict == 'path_unverified'

    def test_measured_processing_effects(self) -> None:
        # Processing detected in the qualification record.
        q = _peq(
            qualification_state='measured_processing_effects',
            bypass_verdict='processing_detected')
        verdict, _ = evaluate_electronics_claim(
            _eapp(), (_etm(),), (), q)
        assert verdict == 'measured_processing_effects'

    def test_partial_interface_state_limits(self) -> None:
        clean = _etm()
        unknown_iface = _etm(
            deembedding_state='unknown',
            channel_label='FR',
        )
        verdict, detail = evaluate_electronics_claim(
            _eapp(), (clean, unknown_iface), (_ele(),), _peq())
        assert verdict == 'electronics_qualified_with_limitations'
        assert 'interface' in detail

    def test_limited_qualification_record(self) -> None:
        q = _peq(
            qualification_state='qualified_with_limitations',
            limitation_reasons=('hf rolloff above 15 kHz',))
        verdict, detail = evaluate_electronics_claim(
            _eapp(), (_etm(),), (_ele(),), q)
        assert verdict == 'electronics_qualified_with_limitations'
        assert 'hf rolloff' in detail


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _repo(tmp_path: Path) -> CadPlaybackElectronicsRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadPlaybackElectronicsRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    records = (
        ('eapp', _eapp(), repo.save_path_profile,
         repo.get_path_profile, 'profile_id'),
        ('etm', _etm(), repo.save_transfer_measurement,
         repo.get_transfer_measurement, 'measurement_id'),
        ('ele', _ele(), repo.save_linearity_evidence,
         repo.get_linearity_evidence, 'evidence_id'),
        ('peq', _peq(), repo.save_qualification,
         repo.get_qualification, 'qualification_id'),
    )
    for _name, record, save, get, id_field in records:
        save(record)
        rid = getattr(record, id_field)
        assert get(rid) == record


def test_repository_list_scopes_document(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    a = _eapp()
    b = _eapp(document_id='doc-other')
    repo.save_path_profile(a)
    repo.save_path_profile(b)
    assert repo.list_path_profiles(DOC) == (a,)
    assert set(repo.list_path_profiles()) == {a, b}
    measurements = repo.list_transfer_measurements(DOC)
    assert measurements == ()
    repo.save_transfer_measurement(_etm())
    assert len(repo.list_transfer_measurements(DOC)) == 1
    repo.save_linearity_evidence(_ele())
    assert len(repo.list_linearity_evidence(DOC)) == 1
    repo.save_qualification(_peq())
    assert len(repo.list_qualifications(DOC)) == 1


def test_repository_append_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = _eapp()
    repo.save_path_profile(record)
    # Idempotent re-save of the identical sealed record is a no-op.
    repo.save_path_profile(record)
    # A different payload under a forged same-id ref conflicts.
    forged = record.model_copy(update={'notes': 'tampered'})
    with pytest.raises(PlaybackElectronicsIntegrityError):
        repo.save_path_profile(forged)
    differing = _eapp(notes='other')
    assert differing.profile_id != record.profile_id


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = _etm()
    repo.save_transfer_measurement(record)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_electrical_transfer_measurements '
            "SET deembedding_state='unknown' "
            'WHERE measurement_id=?',
            (record.measurement_id,),
        )
        connection.execute(
            'UPDATE cad_electrical_transfer_measurements '
            "SET channel_label='FR' "
            'WHERE measurement_id=?',
            (record.measurement_id,),
        )
        connection.commit()
    with pytest.raises(PlaybackElectronicsIntegrityError):
        repo.get_transfer_measurement(record.measurement_id)


def test_repository_detects_document_tamper_on_list(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    record = _eapp()
    repo.save_path_profile(record)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_electronic_audio_path_profiles '
            "SET document_id='doc-forged' "
            'WHERE profile_id=?',
            (record.profile_id,),
        )
        connection.commit()
    with pytest.raises(PlaybackElectronicsIntegrityError):
        repo.list_path_profiles()


def test_electronics_tables_exist_after_fresh_migrate(
    tmp_path: Path,
) -> None:
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
        'cad_electronic_audio_path_profiles',
        'cad_electrical_transfer_measurements',
        'cad_electronic_linearity_evidence',
        'cad_playback_electronics_qualifications',
    }
    assert expected <= tables


def test_row_integrity_registry_covers_new_tables(tmp_path: Path) -> None:
    assert_row_integrity_registry_complete()
    repo = _repo(tmp_path)
    repo.save_path_profile(_eapp())
    repo.save_transfer_measurement(_etm())
    repo.save_linearity_evidence(_ele())
    repo.save_qualification(_peq())
    with connect_sqlite(repo.path) as connection:
        drifts = scan_native_row_integrity(connection)
    assert drifts == ()


def test_audit_coverage_modes_registered() -> None:
    modes = audit_table_modes()
    for table in (
        'cad_electronic_audio_path_profiles',
        'cad_electrical_transfer_measurements',
        'cad_electronic_linearity_evidence',
        'cad_playback_electronics_qualifications',
    ):
        assert modes[table] == 'replay_canonical'
