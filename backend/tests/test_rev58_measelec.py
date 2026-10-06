"""REV58-MEASELEC regression tests — #699 audio-interface
transfer/loopback calibration, #651 gain structure / noise floor /
clipping margin, #649 playback dynamics / limiter, #665 active multi-way
crossover, #693 measurement-method reproducibility."""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_interface_loopback import (
    CadDeembeddingEvidence,
    CadFrequencyCoverage,
    CadInterfaceIoPath,
    CadPhaseSemantics,
    build_interface_calibration,
    build_loopback_observation,
    evaluate_interface_correction,
    sample_rate_applicability,
)
from htdt.cad_interface_loopback_repository import (
    CadInterfaceLoopbackRepository,
    InterfaceLoopbackAuthorityConflictError,
    InterfaceLoopbackAuthorityIntegrityError,
)
from htdt.cad_gain_noise_structure import (
    CadSnrDeclaration,
    CadStageIdentity,
    CadStageMargin,
    build_clipping_margin,
    build_noise_floor_observation,
    build_signal_level_reference,
    evaluate_gain_structure,
)
from htdt.cad_gain_noise_structure_repository import (
    CadGainStructureRepository,
    GainNoiseAuthorityConflictError,
    GainNoiseAuthorityIntegrityError,
)
from htdt.cad_playback_dynamics import (
    CadContentMetadata,
    CadDynamicsMechanismRecord,
    CadLevelSweepPoint,
    build_dynamics_state,
    build_level_sweep_observation,
    evaluate_playback_dynamics,
)
from htdt.cad_playback_dynamics_repository import (
    CadPlaybackDynamicsRepository,
    PlaybackDynamicsAuthorityConflictError,
    PlaybackDynamicsAuthorityIntegrityError,
)
from htdt.cad_active_crossover import (
    CadDriverWay,
    CadManufacturerEnvelope,
    CadSpliceAssessment,
    CadWayAlignment,
    CadWayFilterSpec,
    CadWayRoutingProof,
    build_crossover_plan,
    build_driver_alignment_measurement,
    build_multiway_speaker,
    evaluate_active_crossover,
)
from htdt.cad_active_crossover_repository import (
    ActiveCrossoverAuthorityConflictError,
    ActiveCrossoverAuthorityIntegrityError,
    CadActiveCrossoverRepository,
)
from htdt.cad_method_reproducibility import (
    CadCampaignRun,
    CadConditionAssignment,
    CadMetricPrecision,
    CadVarianceComponent,
    build_method_procedure,
    build_precision_model,
    build_reproducibility_campaign,
    evaluate_reproducibility,
)
from htdt.cad_method_reproducibility_repository import (
    CadMethodReproducibilityRepository,
    ReproducibilityAuthorityConflictError,
    ReproducibilityAuthorityIntegrityError,
)


DOC = 'doc-rev58-measelec'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64
SHA_E = 'e' * 64
SHA_F = 'f' * 64

STIMULUS_REF = AuthorityRef(
    kind='stimulus_signal', ref_id='stim-ess-1', ref_sha256=SHA_A
)
TIMEBASE_REF = AuthorityRef(
    kind='timebase_authority', ref_id='tb-1', ref_sha256=SHA_B
)
CALIBRATION_REF = AuthorityRef(
    kind='calibration_event', ref_id='cal-1', ref_sha256=SHA_C
)
MEASCHAIN_REF = AuthorityRef(
    kind='measchain_qualification', ref_id='mchain-1', ref_sha256=SHA_D
)
STATE_REF = AuthorityRef(
    kind='measurement_state', ref_id='state-1', ref_sha256=SHA_E
)
ORIGIN_REF = AuthorityRef(
    kind='source_origin', ref_id='origin-1', ref_sha256=SHA_F
)


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


# ---------------------------------------------------------------------------
# #699 — audio-interface transfer / loopback calibration
# ---------------------------------------------------------------------------


def _io_path(**overrides) -> CadInterfaceIoPath:
    kwargs = dict(
        output_device='iface-1',
        output_port='line-out-1',
        output_channel='1',
        input_device='iface-1',
        input_port='line-in-1',
        input_channel='1',
        input_gain_db=0.0,
        sample_rate_hz=48000.0,
        bit_depth=24,
        loopback_path_kind='analog',
        acquisition_class='analog_interface',
    )
    kwargs.update(overrides)
    return CadInterfaceIoPath(**kwargs)


def _coverage(**overrides) -> CadFrequencyCoverage:
    kwargs = dict(
        measured_low_hz=10.0,
        measured_high_hz=24000.0,
        usable_low_hz=20.0,
        usable_high_hz=20000.0,
        interpolation='linear_db',
        out_of_range_policy='reject',
    )
    kwargs.update(overrides)
    return CadFrequencyCoverage(**kwargs)


def _calibration(**overrides):
    kwargs = dict(
        document_id=DOC,
        io_path=_io_path(),
        calibration_kind='combined_dac_analog_adc_loopback',
        quantities=('magnitude_response',),
        frequency_coverage=_coverage(),
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_interface_calibration(**kwargs)


def test_loopback_observation_seals_and_needs_stimulus_sha() -> None:
    observation = build_loopback_observation(
        document_id=DOC,
        io_path=_io_path(),
        stimulus_ref=STIMULUS_REF,
        measchain_qualification_ref=MEASCHAIN_REF,
        declared_at_utc=T0,
    )
    assert observation.observation_id.startswith('ifcobs-')
    assert len(observation.observation_sha256) == 64

    with pytest.raises(ValueError, match='sha256'):
        build_loopback_observation(
            document_id=DOC,
            io_path=_io_path(),
            stimulus_ref=AuthorityRef(
                kind='stimulus_signal', ref_id='stim-ess-1'
            ),
            declared_at_utc=T0,
        )


def test_component_kind_calibration_needs_deembedding() -> None:
    # A single-side claim without an independent reference is rejected —
    # one combined loopback can never solve two unknown transfers.
    with pytest.raises(ValueError, match='de-embedding'):
        _calibration(
            calibration_kind=(
                'output_path_characterized_with_reference_input'
            ),
        )

    ok = _calibration(
        calibration_kind=(
            'output_path_characterized_with_reference_input'
        ),
        deembedding=CadDeembeddingEvidence(
            reference_instrument='audio analyzer APx',
            method='calibrated source through input',
        ),
    )
    assert ok.calibration_id.startswith('ifccal-')


def test_digital_loopback_cannot_carry_analog_quantities() -> None:
    with pytest.raises(ValueError, match='routing/format'):
        _calibration(
            calibration_kind='digital_loopback',
            quantities=('magnitude_response',),
        )


def test_complex_response_needs_timebase() -> None:
    with pytest.raises(ValueError, match='phase_semantics'):
        _calibration(quantities=('complex_response',))

    ok = _calibration(
        quantities=('complex_response',),
        phase_semantics=CadPhaseSemantics(
            timebase_ref=TIMEBASE_REF,
            unwrapping_convention='minimum_phase_removed',
        ),
    )
    assert ok.carries('complex_response')


def test_usb_integrated_capture_rejects_analog_loopback() -> None:
    with pytest.raises(ValueError, match='USB'):
        _calibration(
            io_path=_io_path(
                acquisition_class='usb_integrated_capture',
            ),
        )


def test_correction_evaluation_fail_closed() -> None:
    calibration = _calibration()

    # Missing calibration.
    verdict = evaluate_interface_correction(
        document_id=DOC,
        calibration=None,
        measurement_io_path=_io_path(),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'correction_missing'
    assert dict(verdict.capabilities)[
        'magnitude_correction_valid'
    ] == 'invalid'

    # Clean path match inside the usable band.
    verdict = evaluate_interface_correction(
        document_id=DOC,
        calibration=calibration,
        measurement_io_path=_io_path(),
        requested_quantities=('magnitude_response',),
        requested_band_low_hz=20.0,
        requested_band_high_hz=20000.0,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'correction_applied'
    assert dict(verdict.capabilities)[
        'magnitude_correction_valid'
    ] == 'valid'

    # A band outside usable coverage limits, never silently extends.
    verdict = evaluate_interface_correction(
        document_id=DOC,
        calibration=calibration,
        measurement_io_path=_io_path(),
        requested_quantities=('magnitude_response',),
        requested_band_low_hz=1.0,
        requested_band_high_hz=48000.0,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'correction_applied_with_limitations'

    # Different sample rate without declared equivalence → ineligible.
    verdict = evaluate_interface_correction(
        document_id=DOC,
        calibration=calibration,
        measurement_io_path=_io_path(sample_rate_hz=96000.0),
        requested_quantities=('magnitude_response',),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'correction_ineligible'
    assert verdict.sample_rate_applicability == 'rate_mismatch'

    # Different input gain is a material mismatch.
    verdict = evaluate_interface_correction(
        document_id=DOC,
        calibration=calibration,
        measurement_io_path=_io_path(input_gain_db=10.0),
        requested_quantities=('magnitude_response',),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'correction_ineligible'
    assert 'input_gain_db' in verdict.path_mismatches


def test_component_truth_needs_single_side_evidence() -> None:
    calibration = _calibration()
    verdict = evaluate_interface_correction(
        document_id=DOC,
        calibration=calibration,
        measurement_io_path=_io_path(),
        requested_quantities=('magnitude_response',),
        requested_band_low_hz=20.0,
        requested_band_high_hz=20000.0,
        requested_component_truth=True,
        evaluated_at_utc=T1,
    )
    # Combined loopback is never single-side truth.
    assert dict(verdict.capabilities)[
        'component_truth_valid'
    ] == 'invalid'

    component = _calibration(
        calibration_kind=(
            'input_path_characterized_with_reference_source'
        ),
        deembedding=CadDeembeddingEvidence(
            reference_instrument='signal generator',
            method='known stimulus into ADC input',
        ),
    )
    verdict = evaluate_interface_correction(
        document_id=DOC,
        calibration=component,
        measurement_io_path=_io_path(),
        requested_quantities=('magnitude_response',),
        requested_band_low_hz=20.0,
        requested_band_high_hz=20000.0,
        requested_component_truth=True,
        evaluated_at_utc=T1,
    )
    assert dict(verdict.capabilities)[
        'component_truth_valid'
    ] == 'valid'


def test_interface_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadInterfaceLoopbackRepository(scene_repository)

    observation = build_loopback_observation(
        document_id=DOC,
        io_path=_io_path(),
        stimulus_ref=STIMULUS_REF,
        measchain_qualification_ref=MEASCHAIN_REF,
        declared_at_utc=T0,
    )
    repo.save_observation(observation)
    repo.save_observation(observation)
    assert repo.get_observation(observation.observation_id) == observation
    assert repo.list_observations(DOC) == (observation,)

    calibration = _calibration()
    repo.save_calibration(calibration)
    assert repo.get_calibration(calibration.calibration_id) == calibration
    assert repo.list_calibrations(DOC) == (calibration,)

    verdict = evaluate_interface_correction(
        document_id=DOC,
        calibration=calibration,
        measurement_io_path=_io_path(),
        requested_quantities=('magnitude_response',),
        evaluated_at_utc=T1,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict

    forged = calibration.model_copy(
        update={'calibration_kind': 'provider_calibration'}
    )
    with pytest.raises(InterfaceLoopbackAuthorityIntegrityError):
        repo.save_calibration(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_interface_transfer_calibrations '
            'SET calibration_kind=? WHERE calibration_id=?',
            ('provider_calibration', calibration.calibration_id),
        )
    with pytest.raises(InterfaceLoopbackAuthorityIntegrityError):
        repo.get_calibration(calibration.calibration_id)


# ---------------------------------------------------------------------------
# #651 — gain structure / noise floor / clipping margin
# ---------------------------------------------------------------------------


def _stage(label: str = 'dac-output', **overrides) -> CadStageIdentity:
    kwargs = dict(stage_label=label, device='iface-1', port='out-1')
    kwargs.update(overrides)
    return CadStageIdentity(**kwargs)


def _level_ref(**overrides):
    kwargs = dict(
        document_id=DOC,
        stage=_stage(),
        digital_level_dbfs=-20.0,
        analog_level=4.0,
        analog_unit='dbu',
        evidence_class='electrically_measured',
        level_semantics='rms',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_signal_level_reference(**kwargs)


def test_level_reference_requires_analog_unit() -> None:
    ref = _level_ref()
    assert ref.reference_id.startswith('lvlref-')
    with pytest.raises(ValueError, match='unit'):
        _level_ref(analog_unit='unknown')
    with pytest.raises(ValueError, match='V RMS'):
        _level_ref(analog_unit='v_rms', analog_level=-1.0)


def test_snr_declaration_needs_weighting() -> None:
    with pytest.raises(ValueError, match='weighting'):
        CadSnrDeclaration(
            value_db=90.0,
            reference_level_dbfs_or_unit='-20 dBFS = +4 dBu',
            noise_condition='input terminated',
            weighting='unknown',
        )
    ok = CadSnrDeclaration(
        value_db=90.0,
        reference_level_dbfs_or_unit='-20 dBFS',
        noise_condition='input terminated',
        weighting='a',
        bandwidth_hz=22000.0,
    )
    assert ok.value_db == 90.0


def test_noise_observation_and_floor_limit() -> None:
    obs = build_noise_floor_observation(
        document_id=DOC,
        stage=_stage('adc-input'),
        noise_class='electrical_chain',
        noise_level=-100.0,
        noise_unit='dbfs',
        weighting='a',
        instrument_ref=CALIBRATION_REF,
        evidence_class='electrically_measured',
        analyzer_floor_level=-110.0,
        declared_at_utc=T0,
    )
    assert obs.observation_id.startswith('gnobs-')
    # -100 - (-110) = 10 dB above the floor: not limited.
    assert not obs.measurement_floor_limited()

    floor = build_noise_floor_observation(
        document_id=DOC,
        noise_class='electrical_chain',
        noise_level=-106.0,
        noise_unit='dbfs',
        analyzer_floor_level=-110.0,
        declared_at_utc=T0,
    )
    assert floor.measurement_floor_limited()


def test_clipping_margin_needs_typed_threshold() -> None:
    with pytest.raises(ValueError, match='threshold'):
        build_clipping_margin(
            document_id=DOC,
            stage=_stage(),
            clip_mechanism='analog_stage_clip',
            declared_at_utc=T0,
        )

    margin = build_clipping_margin(
        document_id=DOC,
        stage=_stage(),
        clip_mechanism='analog_stage_clip',
        threshold_level=24.0,
        threshold_unit='dbu',
        nominal_level=4.0,
        nominal_unit='dbu',
        load_stress='single_channel',
        declared_at_utc=T0,
    )
    assert margin.margin_id.startswith('clipm-')
    assert margin.clipping_margin_db() == 20.0


def test_gain_structure_evaluation_fail_closed() -> None:
    ref = _level_ref()
    noise = build_noise_floor_observation(
        document_id=DOC,
        stage=_stage('adc-input'),
        noise_class='electrical_chain',
        noise_level=-100.0,
        noise_unit='dbfs',
        weighting='a',
        evidence_class='electrically_measured',
        declared_at_utc=T0,
    )
    margin = build_clipping_margin(
        document_id=DOC,
        stage=_stage(),
        clip_mechanism='analog_stage_clip',
        threshold_level=24.0,
        threshold_unit='dbu',
        nominal_level=4.0,
        nominal_unit='dbu',
        load_stress='single_channel',
        evidence_class='electrically_measured',
        declared_at_utc=T0,
    )
    margin_link = CadStageMargin(
        stage=_stage(),
        nominal_level=4.0,
        nominal_unit='dbu',
        max_linear_level=24.0,
        max_level_unit='dbu',
        clip_mechanism='analog_stage_clip',
        clip_threshold=24.0,
        clip_threshold_unit='dbu',
        headroom_db=20.0,
    )

    # No evidence at all → unqualified.
    verdict = evaluate_gain_structure(
        document_id=DOC,
        use_case='measurement sweep',
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'unqualified_insufficient_evidence'

    # Fully attributed chain → qualified.
    verdict = evaluate_gain_structure(
        document_id=DOC,
        use_case='measurement sweep',
        level_references=(ref,),
        noise_observations=(noise,),
        clipping_margins=(margin,),
        stage_margins=(margin_link,),
        required_headroom_db=12.0,
        requested_load_stress='single_channel',
        noise_target=CadSnrDeclaration(
            value_db=90.0,
            reference_level_dbfs_or_unit='-20 dBFS',
            noise_condition='terminated input',
            weighting='a',
        ),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'gain_structure_qualified'
    states = dict(verdict.capabilities)
    assert states['level_mapping_valid'] == 'valid'
    assert states['headroom_chain_valid'] == 'valid'
    assert verdict.limiting_stage_label == 'dac-output'

    # Electrical noise without stage attribution.
    unattributed = build_noise_floor_observation(
        document_id=DOC,
        noise_class='electrical_chain',
        noise_level=-90.0,
        noise_unit='dbfs',
        declared_at_utc=T0,
    )
    verdict = evaluate_gain_structure(
        document_id=DOC,
        use_case='measurement sweep',
        level_references=(ref,),
        noise_observations=(unattributed,),
        clipping_margins=(margin,),
        stage_margins=(margin_link,),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'noise_floor_unattributed'

    # Analyzer-floor-dominated reading → measurement-floor-limited.
    floored = build_noise_floor_observation(
        document_id=DOC,
        stage=_stage('adc-input'),
        noise_class='electrical_chain',
        noise_level=-106.0,
        noise_unit='dbfs',
        analyzer_floor_level=-110.0,
        evidence_class='electrically_measured',
        declared_at_utc=T0,
    )
    verdict = evaluate_gain_structure(
        document_id=DOC,
        use_case='noise floor claim',
        level_references=(ref,),
        noise_observations=(floored,),
        clipping_margins=(margin,),
        stage_margins=(margin_link,),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'measurement_floor_limited'
    assert dict(verdict.capabilities)[
        'measurement_floor_not_exceeded'
    ] == 'invalid'

    # Multi-channel stress never inherits a single-channel margin.
    verdict = evaluate_gain_structure(
        document_id=DOC,
        use_case='max output claim',
        level_references=(ref,),
        noise_observations=(noise,),
        clipping_margins=(margin,),
        stage_margins=(margin_link,),
        requested_load_stress='multi_channel_stress',
        evaluated_at_utc=T1,
    )
    assert dict(verdict.capabilities)[
        'multi_channel_stress_valid'
    ] == 'invalid'


def test_clipping_observed_without_stage_fails() -> None:
    # A typed mechanism on a bare label stage is 'clip_stage_unresolved'.
    ref = _level_ref()
    margin = build_clipping_margin(
        document_id=DOC,
        stage=CadStageIdentity(stage_label='unknown stage'),
        clip_mechanism='analog_stage_clip',
        threshold_level=10.0,
        threshold_unit='dbu',
        declared_at_utc=T0,
    )
    verdict = evaluate_gain_structure(
        document_id=DOC,
        use_case='clip check',
        level_references=(ref,),
        clipping_margins=(margin,),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'clip_stage_unresolved'
    assert dict(verdict.capabilities)[
        'clip_stage_localized'
    ] == 'invalid'


def test_gain_structure_repository_roundtrip(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadGainStructureRepository(scene_repository)

    ref = _level_ref()
    repo.save_level_reference(ref)
    repo.save_level_reference(ref)
    assert repo.get_level_reference(ref.reference_id) == ref
    assert repo.list_level_references(DOC) == (ref,)

    noise = build_noise_floor_observation(
        document_id=DOC,
        stage=_stage('adc-input'),
        noise_class='electrical_chain',
        noise_level=-100.0,
        noise_unit='dbfs',
        declared_at_utc=T0,
    )
    repo.save_noise_observation(noise)
    assert repo.get_noise_observation(noise.observation_id) == noise

    margin = build_clipping_margin(
        document_id=DOC,
        stage=_stage(),
        clip_mechanism='analog_stage_clip',
        threshold_level=24.0,
        threshold_unit='dbu',
        declared_at_utc=T0,
    )
    repo.save_clipping_margin(margin)
    assert repo.get_clipping_margin(margin.margin_id) == margin

    verdict = evaluate_gain_structure(
        document_id=DOC,
        use_case='repo roundtrip',
        level_references=(ref,),
        noise_observations=(noise,),
        clipping_margins=(margin,),
        evaluated_at_utc=T1,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict

    forged = ref.model_copy(update={'analog_unit': 'dbv'})
    with pytest.raises(GainNoiseAuthorityIntegrityError):
        repo.save_level_reference(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_noise_floor_observations SET noise_class=? '
            'WHERE observation_id=?',
            ('mechanical', noise.observation_id),
        )
    with pytest.raises(GainNoiseAuthorityIntegrityError):
        repo.get_noise_observation(noise.observation_id)


# ---------------------------------------------------------------------------
# #649 — playback dynamics / limiter
# ---------------------------------------------------------------------------


def _dyn_state(**overrides):
    kwargs = dict(
        document_id=DOC,
        device='avr-1',
        renderer_decoder='avr decoder',
        codec_format='pcm',
        firmware='fw-1.2',
        preset='direct',
        master_volume='0.0',
        output_mode='native_multichannel',
        room_correction_state='off',
        mechanisms=(
            CadDynamicsMechanismRecord(
                kind='dialogue_normalization',
                state='inactive',
                evidence_class='runtime_readback_known',
            ),
            CadDynamicsMechanismRecord(
                kind='peak_limiter',
                state='inactive',
                evidence_class='runtime_readback_known',
            ),
        ),
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_dynamics_state(**kwargs)


def test_dynamics_state_seals_and_content_metadata_rules() -> None:
    state = _dyn_state()
    assert state.state_id.startswith('dynstate-')
    assert len(state.state_sha256) == 64

    # An applied dialnorm shift requires the decoder apply flag.
    with pytest.raises(ValueError, match='decoder_configured_to_apply'):
        _dyn_state(
            content_metadata=CadContentMetadata(
                dialnorm_db=-31.0,
                applied_gain_shift_db=-4.0,
            ),
        )

    ok = _dyn_state(
        content_metadata=CadContentMetadata(
            dialnorm_db=-31.0,
            decoder_configured_to_apply=True,
            applied_gain_shift_db=-4.0,
        ),
    )
    assert ok.content_metadata.applied_gain_shift_db == -4.0


def test_engaged_state_needs_runtime_evidence() -> None:
    with pytest.raises(ValueError, match='runtime'):
        _dyn_state(
            mechanisms=(
                CadDynamicsMechanismRecord(
                    kind='peak_limiter',
                    state='engaged_during_capture',
                    evidence_class='configured_state_known',
                ),
            ),
        )


def test_level_sweep_needs_two_levels_and_sha_stimulus() -> None:
    with pytest.raises(ValueError, match='sha256'):
        build_level_sweep_observation(
            document_id=DOC,
            stimulus_ref=AuthorityRef(
                kind='stimulus_signal', ref_id='stim'
            ),
            points=(
                CadLevelSweepPoint(level_dbfs=-30.0),
                CadLevelSweepPoint(level_dbfs=-10.0),
            ),
            verdict='linear_invariant_within_tested_range',
            declared_at_utc=T1,
        )
    with pytest.raises(ValueError, match='two levels'):
        build_level_sweep_observation(
            document_id=DOC,
            stimulus_ref=STIMULUS_REF,
            points=(CadLevelSweepPoint(level_dbfs=-30.0),),
            verdict='linear_invariant_within_tested_range',
            declared_at_utc=T1,
        )
    with pytest.raises(ValueError, match='not_performed'):
        build_level_sweep_observation(
            document_id=DOC,
            stimulus_ref=STIMULUS_REF,
            points=(
                CadLevelSweepPoint(level_dbfs=-30.0),
                CadLevelSweepPoint(level_dbfs=-10.0),
            ),
            verdict='not_performed',
            declared_at_utc=T1,
        )
    with pytest.raises(ValueError, match='hypothesis'):
        build_level_sweep_observation(
            document_id=DOC,
            stimulus_ref=STIMULUS_REF,
            points=(
                CadLevelSweepPoint(level_dbfs=-30.0),
                CadLevelSweepPoint(level_dbfs=-10.0),
            ),
            verdict='limiter_compression_suspected',
            stage_attribution='confirmed_stage',
            declared_at_utc=T1,
        )


def test_dynamics_evaluation_state_controlled() -> None:
    state = _dyn_state()
    sweep = build_level_sweep_observation(
        document_id=DOC,
        stimulus_ref=STIMULUS_REF,
        dynamics_state_ref=AuthorityRef(
            kind='playback_dynamics_state',
            ref_id=state.state_id,
            ref_sha256=state.state_sha256,
        ),
        points=(
            CadLevelSweepPoint(
                level_dbfs=-30.0, normalized_residual_db=0.1
            ),
            CadLevelSweepPoint(
                level_dbfs=-10.0, normalized_residual_db=0.1
            ),
        ),
        verdict='linear_invariant_within_tested_range',
        declared_at_utc=T1,
    )
    verdict = evaluate_playback_dynamics(
        document_id=DOC,
        dynamics_state=state,
        purpose='fr_transfer',
        level_sweep=sweep,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'dynamics_state_controlled'
    assert dict(verdict.capabilities)['fr_transfer_valid'] == 'valid'


def test_dynamics_hidden_processing_limits_max_output() -> None:
    state = _dyn_state(
        mechanisms=(
            CadDynamicsMechanismRecord(
                kind='unknown_dynamic_processing',
                state='unknown',
                evidence_class='processing_topology_unknown',
            ),
        ),
    )
    verdict = evaluate_playback_dynamics(
        document_id=DOC,
        dynamics_state=state,
        purpose='max_capability',
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'hidden_processing_uncharacterized'

    sweep = build_level_sweep_observation(
        document_id=DOC,
        stimulus_ref=STIMULUS_REF,
        points=(
            CadLevelSweepPoint(level_dbfs=-30.0),
            CadLevelSweepPoint(level_dbfs=-10.0),
        ),
        verdict='protection_engaged',
        declared_at_utc=T1,
    )
    verdict = evaluate_playback_dynamics(
        document_id=DOC,
        dynamics_state=_dyn_state(),
        purpose='max_capability',
        level_sweep=sweep,
        evaluated_at_utc=T1,
    )
    assert dict(verdict.capabilities)[
        'max_output_valid'
    ] == 'invalid'


def test_before_after_comparison_gated_on_state_match() -> None:
    baseline = _dyn_state()
    changed = _dyn_state(
        mechanisms=(
            CadDynamicsMechanismRecord(
                kind='dialogue_normalization',
                state='active',
                evidence_class='runtime_readback_known',
            ),
            CadDynamicsMechanismRecord(
                kind='peak_limiter',
                state='inactive',
                evidence_class='runtime_readback_known',
            ),
        ),
    )
    verdict = evaluate_playback_dynamics(
        document_id=DOC,
        dynamics_state=changed,
        purpose='before_after_comparison',
        baseline_state=baseline,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'state_mismatch'
    assert dict(verdict.capabilities)[
        'comparison_eligible'
    ] == 'invalid'
    assert dict(verdict.capabilities)[
        'causal_attribution_valid'
    ] == 'invalid'
    assert any(
        'dialogue_normalization' in m for m in verdict.state_mismatches
    )

    verdict = evaluate_playback_dynamics(
        document_id=DOC,
        dynamics_state=baseline,
        purpose='before_after_comparison',
        baseline_state=_dyn_state(),
        evaluated_at_utc=T1,
    )
    assert dict(verdict.capabilities)[
        'comparison_eligible'
    ] == 'valid'


def test_dynamics_repository_roundtrip(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadPlaybackDynamicsRepository(scene_repository)

    state = _dyn_state()
    repo.save_state(state)
    repo.save_state(state)
    assert repo.get_state(state.state_id) == state
    assert repo.list_states(DOC) == (state,)

    sweep = build_level_sweep_observation(
        document_id=DOC,
        stimulus_ref=STIMULUS_REF,
        points=(
            CadLevelSweepPoint(level_dbfs=-30.0),
            CadLevelSweepPoint(level_dbfs=-10.0),
        ),
        verdict='level_dependent_gain',
        declared_at_utc=T1,
    )
    repo.save_level_sweep(sweep)
    assert repo.get_level_sweep(sweep.observation_id) == sweep

    verdict = evaluate_playback_dynamics(
        document_id=DOC,
        dynamics_state=state,
        purpose='fr_transfer',
        level_sweep=sweep,
        evaluated_at_utc=T1,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict

    forged = state.model_copy(update={'preset': 'forged'})
    with pytest.raises(PlaybackDynamicsAuthorityIntegrityError):
        repo.save_state(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_playback_dynamics_states SET output_mode=? '
            'WHERE state_id=?',
            ('stereo_downmix', state.state_id),
        )
    with pytest.raises(PlaybackDynamicsAuthorityIntegrityError):
        repo.get_state(state.state_id)


# ---------------------------------------------------------------------------
# #665 — active multi-way crossover
# ---------------------------------------------------------------------------


def _ways():
    return (
        CadDriverWay(
            way_label='woofer',
            role='woofer',
            driver_identity='w1-8inch',
            amplifier_channel='amp-ch1',
            dsp_output='dsp-out-1',
            physical_polarity='normal',
            dsp_polarity_inversion=False,
        ),
        CadDriverWay(
            way_label='tweeter',
            role='tweeter',
            driver_identity='t1-25mm',
            amplifier_channel='amp-ch2',
            dsp_output='dsp-out-2',
            physical_polarity='normal',
            dsp_polarity_inversion=False,
        ),
    )


def _speaker(**overrides):
    kwargs = dict(
        document_id=DOC,
        speaker_instance='front-left',
        ways=_ways(),
        acoustic_origin_ref=ORIGIN_REF,
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_multiway_speaker(**kwargs)


def _routing_proofs():
    return tuple(
        CadWayRoutingProof(
            way_label=label,
            method='per_way_stimulus_identification',
            verified=True,
            channel_identity_checked=True,
            polarity_checked=True,
            cross_route_absent=True,
            observed_at_utc=T0,
        )
        for label in ('woofer', 'tweeter')
    )


def _filter_specs():
    return (
        CadWayFilterSpec(
            way_label='woofer',
            filter_kind='lpf',
            family='linkwitz_riley',
            order=4,
            frequency_hz=2000.0,
            topology='iir',
            domain_target='acoustic',
        ),
        CadWayFilterSpec(
            way_label='tweeter',
            filter_kind='hpf',
            family='linkwitz_riley',
            order=4,
            frequency_hz=2000.0,
            topology='iir',
            domain_target='acoustic',
        ),
        CadWayFilterSpec(
            way_label='tweeter',
            filter_kind='protection',
            family='butterworth',
            order=2,
            frequency_hz=800.0,
            topology='iir',
            mandatory_protection=True,
        ),
    )


def _plan(**overrides):
    kwargs = dict(
        document_id=DOC,
        speaker_ref=_speaker(),
        filter_specs=_filter_specs(),
        alignments=(
            CadWayAlignment(way_label='woofer', requested_delay_ms=0.4),
            CadWayAlignment(way_label='tweeter'),
        ),
        routing_proofs=_routing_proofs(),
        dsp_device='dsp-1',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_crossover_plan(**kwargs)


def _measurement(way: str = 'woofer', **overrides):
    kwargs = dict(
        document_id=DOC,
        speaker_ref=_speaker(),
        way_label=way,
        stimulus_ref=STIMULUS_REF,
        timebase_ref=TIMEBASE_REF,
        calibration_ref=CALIBRATION_REF,
        measchain_ref=MEASCHAIN_REF,
        state_ref=STATE_REF,
        mic_position='on-axis-1m',
        measured_delay_ms=0.4 if way == 'woofer' else 0.0,
        acoustic_polarity='normal',
        declared_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_driver_alignment_measurement(**kwargs)


def _splice(**overrides):
    kwargs = dict(
        lower_way='woofer',
        upper_way='tweeter',
        crossover_hz=2000.0,
        verdict='coherent_sum',
        relative_delay_ms=0.4,
        relative_phase_deg=15.0,
        measurement_refs=(
            AuthorityRef(
                kind='driver_alignment_measurement',
                ref_id='axomeas-x',
                ref_sha256=SHA_A,
            ),
        ),
    )
    kwargs.update(overrides)
    return CadSpliceAssessment(**kwargs)


def test_multiway_definition_needs_two_drivers() -> None:
    speaker = _speaker()
    assert speaker.definition_id.startswith('axospk-')
    assert speaker.way_labels() == ('woofer', 'tweeter')

    with pytest.raises(ValueError, match='two ways'):
        _speaker(ways=_ways()[:1])

    # A way without a physical driver is rejected — role labels are not
    # driver evidence.
    with pytest.raises(ValueError, match='physical driver'):
        _speaker(
            ways=(
                CadDriverWay(way_label='woofer', role='woofer'),
                _ways()[1],
            )
        )


def test_routing_proof_partial_checks_never_verify() -> None:
    with pytest.raises(ValueError, match='channel identity'):
        CadWayRoutingProof(
            way_label='tweeter',
            method='per_way_stimulus_identification',
            verified=True,
            channel_identity_checked=True,
            polarity_checked=False,
            cross_route_absent=True,
        )
    with pytest.raises(ValueError, match='method unknown'):
        CadWayRoutingProof(
            way_label='tweeter',
            method='unknown',
            verified=True,
            channel_identity_checked=True,
            polarity_checked=True,
            cross_route_absent=True,
        )


def test_protection_filter_semantics() -> None:
    with pytest.raises(ValueError, match='mandatory_protection'):
        CadWayFilterSpec(
            way_label='tweeter',
            filter_kind='protection',
            frequency_hz=800.0,
        )
    with pytest.raises(ValueError, match='protective edge'):
        CadWayFilterSpec(
            way_label='tweeter',
            filter_kind='lpf',
            frequency_hz=800.0,
            mandatory_protection=True,
        )


def test_splice_coherent_sum_needs_complex_evidence() -> None:
    with pytest.raises(ValueError, match='complex/phase'):
        _splice(magnitude_only=True)
    with pytest.raises(ValueError, match='unevaluated'):
        _splice(verdict='unevaluated')
    with pytest.raises(ValueError, match='sha256'):
        _splice(
            measurement_refs=(
                AuthorityRef(
                    kind='driver_alignment_measurement',
                    ref_id='x',
                ),
            )
        )


def test_crossover_evaluation_fail_closed() -> None:
    speaker = _speaker()
    measurements = (_measurement('woofer'), _measurement('tweeter'))

    # Unproven routing → safety failure before anything else.
    plan_unproven = _plan(
        routing_proofs=(
            CadWayRoutingProof(
                way_label='woofer',
                method='documented_patchbay',
                verified=True,
                channel_identity_checked=True,
                polarity_checked=True,
                cross_route_absent=True,
            ),
        ),
    )
    verdict = evaluate_active_crossover(
        document_id=DOC,
        definition=speaker,
        plan=plan_unproven,
        measurements=measurements,
        splices=(_splice(),),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'routing_unproven'
    assert dict(verdict.capabilities)[
        'room_correction_eligible'
    ] == 'invalid'

    # Missing mandatory protection → compromised.
    plan_no_protection = _plan(
        filter_specs=_filter_specs()[:2],
        manufacturer_envelopes=(
            CadManufacturerEnvelope(
                way_label='tweeter',
                minimum_hpf_hz=800.0,
                source='datasheet',
            ),
        ),
    )
    verdict = evaluate_active_crossover(
        document_id=DOC,
        definition=speaker,
        plan=plan_no_protection,
        measurements=measurements,
        splices=(_splice(),),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'protection_compromised'
    assert dict(verdict.capabilities)[
        'protection_intact'
    ] == 'invalid'

    # Destructive splice → incoherent, room correction gated off.
    verdict = evaluate_active_crossover(
        document_id=DOC,
        definition=speaker,
        plan=_plan(),
        measurements=measurements,
        splices=(_splice(verdict='cancellation_observed'),),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'splice_incoherent'
    assert dict(verdict.capabilities)[
        'room_correction_eligible'
    ] == 'invalid'

    # Deployed state diverges from the plan.
    verdict = evaluate_active_crossover(
        document_id=DOC,
        definition=speaker,
        plan=_plan(),
        measurements=measurements,
        splices=(_splice(),),
        deployed_state_differs=True,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'deployed_state_mismatch'

    # Fully coherent chain + recombined measurement → qualified.
    verdict = evaluate_active_crossover(
        document_id=DOC,
        definition=speaker,
        plan=_plan(),
        measurements=measurements,
        splices=(_splice(),),
        recombined_measurement_ref=AuthorityRef(
            kind='measurement_capture', ref_id='recomb-1',
            ref_sha256=SHA_D,
        ),
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'crossover_qualified'
    assert dict(verdict.capabilities)[
        'room_correction_eligible'
    ] == 'valid'
    # On-axis-only splices never claim off-axis coherence.
    assert dict(verdict.capabilities)['off_axis_valid'] == 'limited'


def test_crossover_repository_roundtrip(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadActiveCrossoverRepository(scene_repository)

    speaker = _speaker()
    repo.save_definition(speaker)
    repo.save_definition(speaker)
    assert repo.get_definition(speaker.definition_id) == speaker
    assert repo.list_definitions(DOC) == (speaker,)

    plan = _plan()
    repo.save_plan(plan)
    assert repo.get_plan(plan.plan_id) == plan
    assert repo.list_plans(DOC) == (plan,)

    measurement = _measurement('tweeter')
    repo.save_alignment_measurement(measurement)
    assert (
        repo.get_alignment_measurement(measurement.measurement_id)
        == measurement
    )

    verdict = evaluate_active_crossover(
        document_id=DOC,
        definition=speaker,
        plan=plan,
        measurements=(measurement, _measurement('woofer')),
        splices=(_splice(),),
        evaluated_at_utc=T1,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict

    forged = plan.model_copy(update={'dsp_device': 'forged-dsp'})
    with pytest.raises(ActiveCrossoverAuthorityIntegrityError):
        repo.save_plan(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_driver_alignment_measurements SET way_label=? '
            'WHERE measurement_id=?',
            ('forged-way', measurement.measurement_id),
        )
    with pytest.raises(ActiveCrossoverAuthorityIntegrityError):
        repo.get_alignment_measurement(measurement.measurement_id)


# ---------------------------------------------------------------------------
# #693 — measurement-method reproducibility
# ---------------------------------------------------------------------------


def _procedure(**overrides):
    kwargs = dict(
        document_id=DOC,
        method_name='speaker commissioning sweep',
        procedure_version='1.0',
        documented=True,
        stimulus_ref=STIMULUS_REF,
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_method_procedure(**kwargs)


def _runs(operators=('A', 'B')) -> tuple[CadCampaignRun, ...]:
    runs: list[CadCampaignRun] = []
    for index, op in enumerate(operators):
        for rep in range(2):
            runs.append(
                CadCampaignRun(
                    run_label=f'op{op}-run{rep}',
                    conditions=(
                        CadConditionAssignment(
                            factor='operator', level=op
                        ),
                    ),
                    artifact_ref=AuthorityRef(
                        kind='measurement_capture',
                        ref_id=f'cap-{op}-{rep}',
                        ref_sha256=SHA_A,
                    ),
                    observed_at_utc=T1,
                )
            )
    return tuple(runs)


def _campaign(**overrides):
    kwargs = dict(
        document_id=DOC,
        procedure_ref=_procedure(),
        design_class='crossed_factorial',
        varied_factors=('operator',),
        runs=_runs(),
        evidence_tier='internal_reproducibility',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_reproducibility_campaign(**kwargs)


def _model(**overrides):
    kwargs = dict(
        document_id=DOC,
        campaign_ref=_campaign(),
        metrics=(
            CadMetricPrecision(
                metric_kind='spl_band',
                metric_label='SPL 1kHz band',
                unit='db_spl',
                repeatability_std=0.1,
                components=(
                    CadVarianceComponent(
                        factor='residual_within_run',
                        variance=0.01,
                        std_dev=0.1,
                    ),
                    CadVarianceComponent(
                        factor='operator',
                        variance=0.04,
                        std_dev=0.2,
                        n_levels=2,
                    ),
                ),
                repeatability_limit_r=0.28,
                reproducibility_limit_R=0.62,
                n_runs=4,
            ),
        ),
        analysis_method='one-way ANOVA variance components',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_precision_model(**kwargs)


def test_procedure_and_campaign_seal_rules() -> None:
    procedure = _procedure()
    assert procedure.procedure_id.startswith('repproc-')

    campaign = _campaign()
    assert campaign.campaign_id.startswith('repcamp-')
    assert len(campaign.completed_runs()) == 4

    # A factor cannot be both varied and held.
    with pytest.raises(ValueError, match='varied and held'):
        _campaign(held_factors=('operator',))

    # Beyond-repeatability tier needs a varied factor.
    with pytest.raises(ValueError, match='varied factor'):
        _campaign(
            varied_factors=(),
            evidence_tier='internal_reproducibility',
        )

    # Internal campaigns are never formal ISO 5725 conformance.
    with pytest.raises(ValueError, match='crossed factorial'):
        _campaign(
            design_class='repeated_only',
            evidence_tier='interlaboratory_formal',
        )

    # Runs: a completed run needs its artifact pin; an excluded run
    # needs its reason.
    with pytest.raises(ValueError, match='artifact'):
        CadCampaignRun(run_label='x', status='completed')
    with pytest.raises(ValueError, match='exclusion reason'):
        CadCampaignRun(
            run_label='x',
            status='excluded_with_reason',
        )


def test_precision_model_component_rules() -> None:
    with pytest.raises(ValueError, match='consistent'):
        CadVarianceComponent(
            factor='operator', variance=0.04, std_dev=0.5
        )
    with pytest.raises(ValueError, match='R must exceed'):
        CadMetricPrecision(
            metric_kind='spl_band',
            metric_label='x',
            unit='db',
            repeatability_limit_r=1.0,
            reproducibility_limit_R=0.5,
        )


def test_reproducibility_evaluation_gates() -> None:
    procedure = _procedure()
    campaign = _campaign()
    model = _model()

    # No campaign → unqualified.
    verdict = evaluate_reproducibility(
        document_id=DOC,
        procedure=procedure,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'unqualified_insufficient_evidence'
    assert dict(verdict.capabilities)[
        'prediction_gate_eligible'
    ] == 'invalid'

    # Undocumented procedure never produces evidence.
    undocumented = _procedure(documented=False)
    verdict = evaluate_reproducibility(
        document_id=DOC,
        procedure=undocumented,
        campaign=campaign,
        model=model,
        evaluated_at_utc=T1,
    )
    assert any('documented' in r for r in verdict.reasons)

    # Repeatability-only campaign: within-run established, gates closed.
    rep_only = _campaign(
        design_class='repeated_only',
        varied_factors=(),
        runs=_runs(operators=('A',)),
        evidence_tier='repeatability_only',
    )
    verdict = evaluate_reproducibility(
        document_id=DOC,
        procedure=procedure,
        campaign=rep_only,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'repeatability_only_established'
    assert dict(verdict.capabilities)[
        'repeatability_known'
    ] == 'valid'
    assert dict(verdict.capabilities)[
        'between_operator_known'
    ] == 'invalid'
    assert dict(verdict.capabilities)[
        'prediction_gate_eligible'
    ] == 'invalid'

    # Full internal-reproducibility campaign + model → established.
    verdict = evaluate_reproducibility(
        document_id=DOC,
        procedure=procedure,
        campaign=campaign,
        model=model,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'precision_model_established'
    assert verdict.evidence_tier == 'internal_reproducibility'
    assert dict(verdict.capabilities)[
        'between_operator_known'
    ] == 'valid'
    assert dict(verdict.capabilities)[
        'prediction_gate_eligible'
    ] == 'valid'
    assert dict(verdict.capabilities)[
        'decision_gate_eligible'
    ] == 'valid'

    # Single-factor-at-a-time confounds the varied factor.
    confounded = _campaign(design_class='single_factor_at_a_time')
    verdict = evaluate_reproducibility(
        document_id=DOC,
        procedure=procedure,
        campaign=confounded,
        model=model,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'confounded_design'
    assert dict(verdict.capabilities)[
        'between_operator_known'
    ] == 'limited'
    assert dict(verdict.capabilities)[
        'prediction_gate_eligible'
    ] == 'limited'


def test_reproducibility_repository_roundtrip(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadMethodReproducibilityRepository(scene_repository)

    procedure = _procedure()
    repo.save_procedure(procedure)
    repo.save_procedure(procedure)
    assert repo.get_procedure(procedure.procedure_id) == procedure
    assert repo.list_procedures(DOC) == (procedure,)

    campaign = _campaign()
    repo.save_campaign(campaign)
    assert repo.get_campaign(campaign.campaign_id) == campaign
    assert repo.list_campaigns(DOC) == (campaign,)

    model = _model()
    repo.save_precision_model(model)
    assert repo.get_precision_model(model.model_id) == model
    assert repo.list_precision_models(DOC) == (model,)

    verdict = evaluate_reproducibility(
        document_id=DOC,
        procedure=procedure,
        campaign=campaign,
        model=model,
        evaluated_at_utc=T1,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict

    forged = campaign.model_copy(
        update={'evidence_tier': 'interlaboratory_formal'}
    )
    with pytest.raises(ReproducibilityAuthorityIntegrityError):
        repo.save_campaign(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_method_procedures SET documented=0 '
            'WHERE procedure_id=?',
            (procedure.procedure_id,),
        )
    with pytest.raises(ReproducibilityAuthorityIntegrityError):
        repo.get_procedure(procedure.procedure_id)


# ---------------------------------------------------------------------------
# Cross-authority: replay probes resolve the new tables
# ---------------------------------------------------------------------------


def test_measelec_tables_in_native_schema(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    with sqlite3.connect(scene_repository.path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    for table in (
        'cad_interface_loopback_observations',
        'cad_interface_transfer_calibrations',
        'cad_interface_correction_qualifications',
        'cad_signal_level_references',
        'cad_noise_floor_observations',
        'cad_clipping_margins',
        'cad_gain_structure_qualifications',
        'cad_playback_dynamics_states',
        'cad_level_sweep_observations',
        'cad_playback_dynamics_qualifications',
        'cad_multiway_speaker_definitions',
        'cad_active_crossover_plans',
        'cad_driver_alignment_measurements',
        'cad_active_crossover_qualifications',
        'cad_method_procedures',
        'cad_reproducibility_campaigns',
        'cad_method_precision_models',
        'cad_reproducibility_qualifications',
    ):
        assert table in names
