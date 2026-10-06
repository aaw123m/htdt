"""REV57-AUD regression tests — #621 acoustic channel-identity / polarity
verification, #634 listener-area coverage / acoustic-aim qualification,
#628 installed loudspeaker instance variation, #632 media-playback
capability qualification."""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_channel_identity_authority import (
    CadAcousticEndpointObservation,
    CadIdentityHop,
    CadIdentityStimulusSpec,
    CadPolarityLayerState,
    build_channel_identity_chain,
    build_channel_identity_test,
    build_endpoint_observation,
    build_polarity_record,
    evaluate_channel_identity,
)
from htdt.cad_channel_identity_repository import (
    CadChannelIdentityRepository,
    ChannelIdentityIntegrityError,
)
from htdt.cad_coverage_aim_authority import (
    CadAimAxis,
    CadCoverageListenerPosition,
    CadCoverageObservation,
    CadCoveragePathPrediction,
    CadCoverageProfileRef,
    CadCoverageSourceBinding,
    CadPathBandLevel,
    build_aim_state,
    build_coverage_measurement_set,
    build_coverage_prediction,
    build_listener_area,
    evaluate_coverage_qualification,
)
from htdt.cad_coverage_aim_repository import (
    CadCoverageAimRepository,
    CoverageAimIntegrityError,
)
from htdt.cad_instance_variation_authority import (
    CadInstanceObservableRecord,
    CadInstanceObservableValue,
    build_instance_evidence,
    build_matched_set,
    build_model_instance_delta,
    classify_instance_delta,
    evaluate_matched_set,
    validate_delta_compatibility,
)
from htdt.cad_instance_variation_repository import (
    CadInstanceVariationRepository,
    InstanceVariationIntegrityError,
)
from htdt.cad_media_playback_authority import (
    CadOperationResultRow,
    CadPlaybackObservation,
    build_capability_record,
    build_media_requirement,
    build_operation_run,
    build_playback_stack,
    evaluate_playback_capability,
    CadWaveProfileBinding,
)
from htdt.cad_media_playback_repository import (
    CadMediaPlaybackRepository,
    MediaPlaybackIntegrityError,
)


DOC = 'doc-rev57-aud'
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T01:00:00+00:00'
T2 = '2026-10-05T02:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _ref(kind: str, ref_id: str, sha: str = SHA_A) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=ref_id, ref_sha256=sha)


# ---------------------------------------------------------------------------
# #621 acoustic channel-identity / polarity verification
# ---------------------------------------------------------------------------


def _hops():
    return (
        CadIdentityHop(
            kind='renderer_output', state='known', label='AVR ch1',
        ),
        CadIdentityHop(
            kind='physical_cable_path', state='known',
            ref=_ref('physical_interconnect', 'cable-fl'),
        ),
        CadIdentityHop(
            kind='loudspeaker_instance', state='known',
            ref=_ref('installed_instance', 'spk-fl', SHA_B),
        ),
        CadIdentityHop(
            kind='acoustic_endpoint', state='unknown',
        ),
    )


def _chain(**overrides):
    kwargs = dict(
        document_id=DOC,
        logical_channel='FL',
        channel_class='bed_channel',
        expected_speaker_entity_ids=('spk-fl',),
        hops=_hops(),
        physical_path_ref=_ref('physical_interconnect', 'cable-fl'),
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_channel_identity_chain(**kwargs)


def _id_stimulus(**overrides):
    kwargs = dict(
        stimulus_class='channel_id_test_signal',
        spectrum_descriptor='id_sequence',
        channel_assignment='FL',
        routing_mode='pcm_direct',
    )
    kwargs.update(overrides)
    return CadIdentityStimulusSpec(**kwargs)


def _observation(entities=('spk-fl',), **overrides):
    kwargs = dict(
        document_id=DOC,
        method='measurement_mic_level',
        observed_speaker_entity_ids=entities,
        confidence='high',
        observed_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_endpoint_observation(**kwargs)


def _layers(wiring='normal', dsp='not_present', acoustic='consistent'):
    states = {
        'physical_wiring': wiring,
        'dsp_inversion': dsp,
        'acoustic_relative': acoustic,
    }
    method = {
        'physical_wiring': 'manual_wiring_inspection',
        'dsp_inversion': 'manufacturer_diagnostic',
        'acoustic_relative': 'impulse_sign',
    }
    return tuple(
        CadPolarityLayerState(layer=layer, state=state, method=method[layer])
        for layer, state in states.items()
        if state != 'not_present'
    )


def test_known_hop_requires_ref_or_label() -> None:
    with pytest.raises(ValueError, match='known'):
        CadIdentityHop(kind='renderer_output', state='known')
    CadIdentityHop(kind='renderer_output', state='known', label='AVR ch1')
    CadIdentityHop(
        kind='acoustic_endpoint',
        state='known',
        ref=_ref('installed_instance', 'spk-fl'),
    )


def test_unknown_hop_cannot_pin_ref() -> None:
    with pytest.raises(ValueError, match='unknown hop'):
        CadIdentityHop(
            kind='acoustic_endpoint',
            state='unknown',
            ref=_ref('installed_instance', 'spk-fl'),
        )


def test_id_stimulus_requires_exact_pin_or_descriptor() -> None:
    with pytest.raises(ValueError, match='channel-id test signal'):
        CadIdentityStimulusSpec(
            stimulus_class='channel_id_test_signal',
        )
    CadIdentityStimulusSpec(
        stimulus_class='channel_id_test_signal',
        stimulus_ref=_ref('stimulus_asset', 'stim-id'),
    )
    with pytest.raises(ValueError, match='sha256'):
        CadIdentityStimulusSpec(
            stimulus_class='channel_id_test_signal',
            stimulus_ref=AuthorityRef(kind='stimulus_asset', ref_id='x'),
        )


def test_chain_rejects_duplicate_hop_kind() -> None:
    with pytest.raises(ValueError, match='one.*hop kind|hop kind.*once'):
        build_channel_identity_chain(
            document_id=DOC,
            logical_channel='FL',
            hops=(
                CadIdentityHop(kind='renderer_output', state='inferred'),
                CadIdentityHop(kind='renderer_output', state='inferred'),
            ),
        )


def test_chain_rejects_duplicate_expected_speakers() -> None:
    with pytest.raises(ValueError, match='unique'):
        build_channel_identity_chain(
            document_id=DOC,
            logical_channel='FL',
            expected_speaker_entity_ids=('spk-fl', 'spk-fl'),
        )


def test_endpoint_observation_requires_unique_speakers() -> None:
    with pytest.raises(ValueError, match='unique'):
        build_endpoint_observation(
            document_id=DOC,
            method='operator_listening',
            observed_speaker_entity_ids=('spk-fl', 'spk-fl'),
        )


def test_identity_test_binds_observations() -> None:
    chain = _chain()
    observation = _observation()
    test = build_channel_identity_test(
        document_id=DOC,
        chain=chain,
        stimulus=_id_stimulus(),
        observation_refs=(
            AuthorityRef(
                kind='acoustic_endpoint_observation',
                ref_id=observation.observation_id,
                ref_sha256=observation.observation_sha256,
            ),
        ),
        tested_at_utc=T1,
    )
    assert test.chain_ref.ref_id == chain.chain_id
    assert test.observation_refs[0].ref_id == observation.observation_id
    with pytest.raises(ValueError, match='acoustic endpoint'):
        build_channel_identity_test(
            document_id=DOC,
            chain=chain,
            stimulus=_id_stimulus(),
            observation_refs=(_ref('installed_instance', 'x'),),
        )


def test_polarity_acoustic_layer_requires_method() -> None:
    with pytest.raises(ValueError, match=r'declared\s+measurement method'):
        CadPolarityLayerState(
            layer='acoustic_relative', state='consistent',
        )
    CadPolarityLayerState(
        layer='acoustic_relative',
        state='consistent',
        method='impulse_sign',
    )
    CadPolarityLayerState(layer='acoustic_relative', state='unknown')


def test_polarity_phase_claim_requires_timebase() -> None:
    chain = _chain()
    with pytest.raises(ValueError, match='timebase'):
        build_polarity_record(
            document_id=DOC,
            chain=chain,
            layers=(
                CadPolarityLayerState(
                    layer='frequency_dependent_phase',
                    state='consistent',
                    method='relative_transfer_function',
                ),
            ),
            measured_at_utc=T1,
        )
    build_polarity_record(
        document_id=DOC,
        chain=chain,
        layers=(
            CadPolarityLayerState(
                layer='frequency_dependent_phase',
                state='ambiguous',
                method='relative_transfer_function',
            ),
        ),
        measured_at_utc=T1,
    )
    build_polarity_record(
        document_id=DOC,
        chain=chain,
        layers=(
            CadPolarityLayerState(
                layer='frequency_dependent_phase',
                state='consistent',
                method='relative_transfer_function',
            ),
        ),
        timebase_ref=_ref('measurement_timebase', 'tb-1'),
        measured_at_utc=T1,
    )


def test_dsp_inversion_cannot_claim_normal_plus_consistent() -> None:
    chain = _chain()
    with pytest.raises(ValueError, match='contradict'):
        build_polarity_record(
            document_id=DOC,
            chain=chain,
            layers=(
                CadPolarityLayerState(
                    layer='physical_wiring', state='normal',
                    method='manual_wiring_inspection',
                ),
                CadPolarityLayerState(
                    layer='dsp_inversion', state='inverted',
                    method='manufacturer_diagnostic',
                ),
                CadPolarityLayerState(
                    layer='acoustic_relative', state='consistent',
                    method='impulse_sign',
                ),
            ),
            measured_at_utc=T1,
        )


def _evaluated(chain, tests=(), observations=(), records=(), **kw):
    return evaluate_channel_identity(
        document_id=DOC,
        chain=chain,
        tests=tests,
        observations=observations,
        polarity_records=records,
        evaluated_at_utc=T2,
        **kw,
    )


def _tested_chain(observation=None, stimulus=None, entities=('spk-fl',)):
    chain = _chain()
    observation = observation or _observation(entities)
    stimulus = stimulus or _id_stimulus()
    test = build_channel_identity_test(
        document_id=DOC,
        chain=chain,
        stimulus=stimulus,
        observation_refs=(
            AuthorityRef(
                kind='acoustic_endpoint_observation',
                ref_id=observation.observation_id,
                ref_sha256=observation.observation_sha256,
            ),
        ),
        tested_at_utc=T1,
    )
    return chain, test, observation


def test_identity_no_evidence_is_insufficient() -> None:
    evaluation = _evaluated(_chain())
    assert evaluation.verdict == 'insufficient_evidence'
    assert evaluation.reconciliation == 'insufficient_evidence'


def test_identity_verified_all_match() -> None:
    chain, test, observation = _tested_chain()
    evaluation = _evaluated(
        chain,
        tests=(test,),
        observations=(observation,),
        records=(
            build_polarity_record(
                document_id=DOC,
                chain=chain,
                layers=_layers(),
                measured_at_utc=T1,
            ),
        ),
    )
    assert evaluation.verdict == 'verified'
    assert evaluation.reconciliation == 'all_match'
    assert evaluation.evidence_class == 'instrumented'


def test_identity_unexpected_endpoint_is_mismatch() -> None:
    chain, test, observation = _tested_chain(
        entities=('spk-fl', 'spk-fr'),
    )
    evaluation = _evaluated(
        chain, tests=(test,), observations=(observation,),
    )
    assert evaluation.verdict == 'identity_mismatch'
    assert evaluation.reconciliation == 'multiple_unexpected_endpoints'
    assert 'spk-fr' in evaluation.unexpected_speaker_entity_ids


def test_identity_missing_endpoint_is_mismatch() -> None:
    chain, test, observation = _tested_chain(entities=())
    evaluation = _evaluated(
        chain, tests=(test,), observations=(observation,),
    )
    assert evaluation.verdict == 'identity_mismatch'
    assert evaluation.reconciliation == 'acoustic_endpoint_mismatch'
    assert 'spk-fl' in evaluation.missing_speaker_entity_ids


def test_identity_device_map_mismatch_reported() -> None:
    chain, test, observation = _tested_chain()
    evaluation = _evaluated(
        chain,
        tests=(test,),
        observations=(observation,),
        device_map_speaker_entity_ids=('spk-fr',),
    )
    assert evaluation.reconciliation == 'device_map_mismatch'
    assert evaluation.verdict == 'verified'


def test_identity_program_material_only_is_limited() -> None:
    chain, test, observation = _tested_chain(
        stimulus=CadIdentityStimulusSpec(
            stimulus_class='program_material',
        ),
    )
    evaluation = _evaluated(
        chain, tests=(test,), observations=(observation,),
    )
    assert evaluation.verdict == 'verified_with_limitations'


def test_identity_manual_evidence_is_limited() -> None:
    observation = _observation(method='operator_listening')
    chain, test, _ = _tested_chain(observation=observation)
    evaluation = _evaluated(
        chain, tests=(test,), observations=(observation,),
    )
    assert evaluation.verdict == 'verified_with_limitations'
    assert evaluation.evidence_class == 'manual'


def test_identity_dsp_compensated_wiring_stays_defect() -> None:
    chain, test, observation = _tested_chain()
    record = build_polarity_record(
        document_id=DOC,
        chain=chain,
        layers=_layers(
            wiring='reversed', dsp='inverted', acoustic='consistent'
        ),
        policy_acceptance='accepted',
        measured_at_utc=T1,
    )
    evaluation = _evaluated(
        chain,
        tests=(test,),
        observations=(observation,),
        records=(record,),
    )
    assert evaluation.verdict == 'verified_compensated'
    assert evaluation.physical_wiring_state == 'reversed'
    assert evaluation.dsp_polarity_state == 'inverted'


def test_identity_reversed_wiring_is_fault() -> None:
    chain, test, observation = _tested_chain()
    record = build_polarity_record(
        document_id=DOC,
        chain=chain,
        layers=_layers(wiring='reversed'),
        measured_at_utc=T1,
    )
    evaluation = _evaluated(
        chain,
        tests=(test,),
        observations=(observation,),
        records=(record,),
    )
    assert evaluation.verdict == 'polarity_fault'


def test_identity_stale_on_invalidation_trigger() -> None:
    chain, test, observation = _tested_chain()
    evaluation = _evaluated(
        chain,
        tests=(test,),
        observations=(observation,),
        invalidation_triggers=('avr_dsp_reset', 'cable_port_service'),
    )
    assert evaluation.verdict == 'stale'
    assert set(evaluation.stale_triggers) == {
        'avr_dsp_reset', 'cable_port_service'
    }


def test_identity_render_fallback_not_dead_speaker() -> None:
    chain = _chain()
    evaluation = _evaluated(
        chain,
        tests=(
            build_channel_identity_test(
                document_id=DOC,
                chain=chain,
                stimulus=_id_stimulus(),
                observation_refs=(),
                tested_at_utc=T1,
            ),
        ),
        observations=(),
        render_fallback_active=True,
    )
    assert evaluation.verdict == 'not_driven_by_renderer'
    assert evaluation.renderer_fallback is True


def test_channel_identity_repository_roundtrip(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadChannelIdentityRepository(scene)
    chain = _chain()
    repo.save_chain(chain)
    assert repo.get_chain(chain.chain_id) == chain
    repo.save_chain(chain)  # idempotent

    observation = _observation()
    repo.save_observation(observation)
    assert repo.get_observation(observation.observation_id) == observation

    test = build_channel_identity_test(
        document_id=DOC,
        chain=chain,
        stimulus=_id_stimulus(),
        observation_refs=(
            AuthorityRef(
                kind='acoustic_endpoint_observation',
                ref_id=observation.observation_id,
                ref_sha256=observation.observation_sha256,
            ),
        ),
        tested_at_utc=T1,
    )
    repo.save_test(test)
    assert repo.get_test(test.test_id) == test

    record = build_polarity_record(
        document_id=DOC,
        chain=chain,
        layers=_layers(),
        measured_at_utc=T1,
    )
    repo.save_polarity_record(record)
    assert repo.get_polarity_record(record.record_id) == record

    evaluation = evaluate_channel_identity(
        document_id=DOC,
        chain=chain,
        tests=(test,),
        observations=(observation,),
        polarity_records=(record,),
        evaluated_at_utc=T2,
    )
    repo.save_evaluation(evaluation)
    assert repo.get_evaluation(evaluation.evaluation_id) == evaluation
    assert len(repo.list_evaluations(DOC)) == 1
    assert len(repo.list_chains(DOC)) == 1


def test_channel_identity_repository_rejects_tampered_row(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadChannelIdentityRepository(scene)
    chain = _chain()
    repo.save_chain(chain)
    with repo._connect() as connection:
        connection.execute(
            "UPDATE cad_channel_identity_chains "
            "SET logical_channel='FR' WHERE chain_id=?",
            (chain.chain_id,),
        )
        connection.commit()
    with pytest.raises(ChannelIdentityIntegrityError):
        repo.get_chain(chain.chain_id)


# ---------------------------------------------------------------------------
# #634 listener-area coverage / acoustic-aim qualification
# ---------------------------------------------------------------------------


def _position(position_id='p-1', role='design', **overrides):
    kwargs = dict(
        position_id=position_id,
        ear_xyz_m=(2.0, 1.5, 1.1),
        role=role,
    )
    kwargs.update(overrides)
    return CadCoverageListenerPosition(**kwargs)


def _area(n_design=1, n_holdout=0, **overrides):
    positions = [
        _position(f'p-{i+1}') for i in range(n_design)
    ] + [
        _position(f'h-{i+1}', role='holdout') for i in range(n_holdout)
    ]
    kwargs = dict(
        document_id=DOC,
        label='main row',
        positions=tuple(positions),
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_listener_area(**kwargs)


def _source(speaker='spk-fl', **overrides):
    kwargs = dict(
        speaker_entity_id=speaker,
        directivity_class='measured_3d_dataset',
        directivity_ref=_ref('speaker_directivity', 'dir-fl'),
        aim_ref=_ref('acoustic_aim_state', 'aim-fl', SHA_B),
    )
    kwargs.update(overrides)
    return CadCoverageSourceBinding(**kwargs)


def _path(position_id='p-1', speaker='spk-fl', level=80.0, **overrides):
    kwargs = dict(
        position_id=position_id,
        speaker_entity_id=speaker,
        off_axis_angle_deg=10.0,
        distance_m=2.5,
        direct_arrival_time_s=0.0073,
        band_levels=(
            CadPathBandLevel(band_hz=500.0, level_db=level),
        ),
        occlusion='clear',
        validity='within_capability',
    )
    kwargs.update(overrides)
    return CadCoveragePathPrediction(**kwargs)


def _prediction(area=None, **overrides):
    area = area or _area()
    kwargs = dict(
        document_id=DOC,
        area=area,
        quantity='direct_sound',
        sources=(_source(),),
        paths=(_path(),),
        multi_source_class='single_physical_source',
        summation_model='independent',
        predicted_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_coverage_prediction(**kwargs)


def _observation_cov(position_id='p-1', speaker='spk-fl', level=80.5):
    return CadCoverageObservation(
        position_id=position_id,
        speaker_entity_id=speaker,
        band_levels=(
            CadPathBandLevel(band_hz=500.0, level_db=level),
        ),
        observed_at_utc=T1,
    )


def _measurement_set(area=None, observations=None, **overrides):
    area = area or _area()
    observations = observations or (
        _observation_cov(),
    )
    kwargs = dict(
        document_id=DOC,
        area=area,
        speaker_entity_ids=('spk-fl',),
        quantity='direct_sound',
        observations=observations,
        measured_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_coverage_measurement_set(**kwargs)


def test_aim_axis_requires_unit_vector() -> None:
    with pytest.raises(ValueError):
        CadAimAxis(
            kind='acoustic_reference_axis', vector=(1.0, 0.0, 0.5)
        )
    CadAimAxis(kind='cabinet_pose', vector=(1.0, 0.0, 0.0))
    CadAimAxis(kind='as_built_observed_aim')


def test_aim_state_rejects_duplicate_axis_kind() -> None:
    with pytest.raises(ValueError, match='aim-axis kind'):
        build_aim_state(
            document_id=DOC,
            speaker_entity_id='spk-fl',
            aim_axes=(
                CadAimAxis(kind='cabinet_pose'),
                CadAimAxis(kind='cabinet_pose'),
            ),
        )


def test_aim_state_rotation_must_be_unit_quaternion() -> None:
    with pytest.raises(ValueError):
        build_aim_state(
            document_id=DOC,
            speaker_entity_id='spk-fl',
            cabinet_to_dataset_rotation=(1.0, 0.0, 0.0, 0.5),
        )
    build_aim_state(
        document_id=DOC,
        speaker_entity_id='spk-fl',
        cabinet_to_dataset_rotation=(1.0, 0.0, 0.0, 0.0),
    )


def test_listener_area_requires_position() -> None:
    with pytest.raises(ValueError):
        build_listener_area(
            document_id=DOC, label='empty', positions=(),
        )
    area = _area(n_design=2, n_holdout=1)
    assert area.design_position_ids == frozenset({'p-1', 'p-2'})
    assert area.holdout_position_ids == frozenset({'h-1'})


def test_3d_directivity_requires_dataset_ref() -> None:
    with pytest.raises(ValueError, match='directivity'):
        CadCoverageSourceBinding(
            speaker_entity_id='spk-fl',
            directivity_class='measured_3d_dataset',
        )
    CadCoverageSourceBinding(
        speaker_entity_id='spk-fl',
        directivity_class='nominal_beamwidth_only',
    )


def test_transfer_applied_exactly_once() -> None:
    with pytest.raises(ValueError, match='exactly once'):
        CadCoverageSourceBinding(
            speaker_entity_id='spk-fl',
            applied_transfers=(
                ('screen', 'screen-1'),
                ('screen', 'screen-1'),
            ),
        )


def test_early_arriving_requires_window_profile() -> None:
    with pytest.raises(ValueError, match='early_window_profile'):
        build_coverage_prediction(
            document_id=DOC,
            area=_area(),
            quantity='early_arriving_energy',
            sources=(_source(),),
            paths=(_path(),),
        )


def test_coherent_summation_requires_timebase() -> None:
    with pytest.raises(ValueError, match='coherent'):
        build_coverage_prediction(
            document_id=DOC,
            area=_area(),
            quantity='direct_sound',
            sources=(_source(),),
            paths=(_path(),),
            summation_model='coherent_complex',
        )
    build_coverage_prediction(
        document_id=DOC,
        area=_area(),
        quantity='direct_sound',
        sources=(_source(),),
        paths=(_path(),),
        summation_model='coherent_complex',
        timebase_capability_ref=_ref('measurement_timebase', 'tb-1'),
    )


def test_single_source_class_forbids_arrays() -> None:
    with pytest.raises(ValueError, match='single_physical_source'):
        build_coverage_prediction(
            document_id=DOC,
            area=_area(),
            quantity='direct_sound',
            sources=(
                _source('spk-fl'),
                _source('spk-sub', directivity_class='unavailable',
                        directivity_ref=None),
            ),
            paths=(_path(), _path(speaker='spk-sub')),
            multi_source_class='single_physical_source',
        )


def test_paths_must_reference_declared_sources() -> None:
    with pytest.raises(ValueError, match='declared source'):
        build_coverage_prediction(
            document_id=DOC,
            area=_area(),
            quantity='direct_sound',
            sources=(_source(),),
            paths=(_path(speaker='spk-fr'),),
        )


def test_observation_band_levels_unique() -> None:
    with pytest.raises(ValueError, match='unique per band_hz'):
        CadCoveragePathPrediction(
            position_id='p-1',
            speaker_entity_id='spk-fl',
            band_levels=(
                CadPathBandLevel(band_hz=500.0, level_db=80.0),
                CadPathBandLevel(band_hz=500.0, level_db=81.0),
            ),
        )


def test_observation_arrival_requires_timebase() -> None:
    with pytest.raises(ValueError, match='timebase'):
        CadCoverageObservation(
            position_id='p-1',
            speaker_entity_id='spk-fl',
            arrival_time_s=0.0073,
        )


def test_coverage_no_evidence_is_indeterminate() -> None:
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=_area(),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'indeterminate'


def test_coverage_unresolved_profile_is_ambiguous() -> None:
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=_area(),
        prediction=_prediction(),
        measurement_set=_measurement_set(),
        profile=CadCoverageProfileRef(
            kind='a102_family', revision_label='draft', resolved=False,
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'profile_source_ambiguous'


def test_coverage_prediction_only() -> None:
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=_area(),
        prediction=_prediction(),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'predicted_only'


def test_coverage_nominal_beamwidth_capped() -> None:
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=_area(),
        prediction=_prediction(
            sources=(
                CadCoverageSourceBinding(
                    speaker_entity_id='spk-fl',
                    directivity_class='nominal_beamwidth_only',
                ),
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'qualified_with_limitations'


def test_coverage_limiting_validity_reported() -> None:
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=_area(),
        prediction=_prediction(
            paths=(
                _path(validity='outside_angular_domain'),
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'source_directivity_limited'
    assert 'p-1' in qualification.limiting_position_ids
    assert 'source_dataset_limitation' in qualification.residual_attributions


def test_coverage_occluded_path_limited() -> None:
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=_area(),
        prediction=_prediction(
            paths=(
                _path(occlusion='occluded'),
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'occlusion_limited'
    assert 'geometry_occlusion' in qualification.residual_attributions


def test_coverage_single_position_field_measured() -> None:
    area = _area(n_design=1)
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=area,
        measurement_set=_measurement_set(area=area),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'field_measured'


def test_coverage_multi_position_needs_holdout() -> None:
    area = _area(n_design=2, n_holdout=1)
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=area,
        measurement_set=_measurement_set(
            area=area,
            observations=(
                _observation_cov('p-1'),
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'spatial_sampling_insufficient'


def test_coverage_holdout_measured_qualifies() -> None:
    area = _area(n_design=2, n_holdout=1)
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=area,
        measurement_set=_measurement_set(
            area=area,
            observations=(
                _observation_cov('p-1'),
                _observation_cov('p-2'),
                _observation_cov('h-1'),
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'field_measured'


def test_coverage_agree_within_envelope() -> None:
    area = _area(n_design=1, n_holdout=1)
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=area,
        prediction=_prediction(area=area),
        measurement_set=_measurement_set(
            area=area,
            observations=(
                _observation_cov('p-1'),
                _observation_cov('h-1', level=81.0),
            ),
        ),
        profile=CadCoverageProfileRef(
            kind='project', envelope_db=2.0,
        ),
        evaluated_at_utc=T2,
    )
    assert (
        qualification.coverage_state
        == 'predicted_and_measured_agree_within_envelope'
    )


def test_coverage_residual_exceeds_envelope() -> None:
    area = _area(n_design=1, n_holdout=1)
    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=area,
        prediction=_prediction(area=area),
        measurement_set=_measurement_set(
            area=area,
            observations=(
                _observation_cov('p-1', level=90.0),
                _observation_cov('h-1'),
            ),
        ),
        profile=CadCoverageProfileRef(
            kind='project', envelope_db=2.0,
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.coverage_state == 'qualified_with_limitations'
    assert 'measurement_uncertainty' in (
        qualification.residual_attributions
    )


def test_coverage_rejects_area_mismatch() -> None:
    area = _area()
    other = _area(label='other row')
    prediction = _prediction(area=other)
    with pytest.raises(ValueError, match='listener area'):
        evaluate_coverage_qualification(
            document_id=DOC,
            area=area,
            prediction=prediction,
            evaluated_at_utc=T2,
        )


def test_coverage_rejects_quantity_mismatch() -> None:
    area = _area()
    with pytest.raises(ValueError, match='quantities differ'):
        evaluate_coverage_qualification(
            document_id=DOC,
            area=area,
            prediction=_prediction(area=area),
            measurement_set=_measurement_set(
                area=area, quantity='steady_state',
            ),
            evaluated_at_utc=T2,
        )


def test_coverage_repository_roundtrip(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadCoverageAimRepository(scene)
    aim = build_aim_state(
        document_id=DOC,
        speaker_entity_id='spk-fl',
        aim_axes=(
            CadAimAxis(
                kind='acoustic_reference_axis', vector=(1.0, 0.0, 0.0)
            ),
            CadAimAxis(kind='design_aim_target', vector=(0.9, 0.0, 0.4359)),
        ),
        cabinet_to_dataset_rotation=(1.0, 0.0, 0.0, 0.0),
        declared_at_utc=T0,
    )
    repo.save_aim_state(aim)
    assert repo.get_aim_state(aim.aim_id) == aim
    repo.save_aim_state(aim)  # idempotent

    area = _area(n_design=2, n_holdout=1)
    repo.save_listener_area(area)
    assert repo.get_listener_area(area.area_id) == area

    prediction = _prediction(area=area)
    repo.save_prediction(prediction)
    assert repo.get_prediction(prediction.prediction_id) == prediction

    measurement_set = _measurement_set(
        area=area,
        observations=(
            _observation_cov('p-1'),
            _observation_cov('p-2'),
            _observation_cov('h-1'),
        ),
    )
    repo.save_measurement_set(measurement_set)
    assert repo.get_measurement_set(measurement_set.set_id) == (
        measurement_set
    )

    qualification = evaluate_coverage_qualification(
        document_id=DOC,
        area=area,
        prediction=prediction,
        measurement_set=measurement_set,
        evaluated_at_utc=T2,
    )
    repo.save_qualification(qualification)
    assert repo.get_qualification(
        qualification.qualification_id
    ) == qualification
    assert len(repo.list_qualifications(DOC)) == 1


def test_coverage_repository_rejects_tampered_row(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadCoverageAimRepository(scene)
    area = _area()
    repo.save_listener_area(area)
    with repo._connect() as connection:
        connection.execute(
            "UPDATE cad_coverage_listener_areas SET label='other' "
            'WHERE area_id=?',
            (area.area_id,),
        )
        connection.commit()
    with pytest.raises(CoverageAimIntegrityError):
        repo.get_listener_area(area.area_id)


# ---------------------------------------------------------------------------
# #628 installed loudspeaker instance variation
# ---------------------------------------------------------------------------


def _instance_ref(name='inst-fl'):
    return _ref('installed_instance', name)


def _model_ref():
    return _ref('model_speaker', 'model-x', SHA_B)


def _observable(
    name='sensitivity', value=87.5, unit='dB_2.83V_1m', **overrides
):
    kwargs = dict(
        observable=name,
        measurement_level_class='low_level',
        domain='free_field',
        summary_value=value,
        summary_unit=unit,
    )
    kwargs.update(overrides)
    return CadInstanceObservableRecord(**kwargs)


def _instance_evidence(instance='inst-fl', value=87.5, **overrides):
    kwargs = dict(
        document_id=DOC,
        instance_ref=_instance_ref(instance),
        model_ref=_model_ref(),
        evidence_level='instance_lab_measurement',
        evidence_source='instance',
        observables=(_observable(value=value),),
        measurement_domain='free_field',
        measured_at_utc=T1,
        declared_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_instance_evidence(**kwargs)


def test_observable_value_requires_unit() -> None:
    with pytest.raises(ValueError):
        CadInstanceObservableValue(value=87.5, unit='')
    CadInstanceObservableValue(
        band_hz=1000.0, value=87.5, unit='dB_2.83V_1m',
    )


def test_observable_summary_requires_unit() -> None:
    with pytest.raises(ValueError, match='unit'):
        CadInstanceObservableRecord(
            observable='sensitivity', summary_value=87.5,
        )
    with pytest.raises(ValueError, match='summary_value'):
        CadInstanceObservableRecord(
            observable='sensitivity', summary_unit='dB',
        )


def test_instance_level_requires_instance_source() -> None:
    with pytest.raises(ValueError, match='instance-sourced'):
        build_instance_evidence(
            document_id=DOC,
            instance_ref=_instance_ref(),
            evidence_level='instance_lab_measurement',
            evidence_source='model',
            observables=(_observable(),),
        )
    with pytest.raises(ValueError, match='observable'):
        build_instance_evidence(
            document_id=DOC,
            instance_ref=_instance_ref(),
            evidence_level='instance_lab_measurement',
            evidence_source='instance',
            observables=(),
        )


def test_reference_level_cannot_be_instance_sourced() -> None:
    with pytest.raises(ValueError, match='reference-level'):
        build_instance_evidence(
            document_id=DOC,
            model_ref=_model_ref(),
            evidence_level='model_reference_only',
            evidence_source='instance',
            observables=(_observable(),),
        )


def test_in_room_cannot_claim_free_field() -> None:
    with pytest.raises(ValueError, match='free-field'):
        build_instance_evidence(
            document_id=DOC,
            instance_ref=_instance_ref(),
            evidence_level='instance_in_room_measurement',
            evidence_source='instance',
            observables=(_observable(),),
            measurement_domain='free_field',
        )
    build_instance_evidence(
        document_id=DOC,
        instance_ref=_instance_ref(),
        evidence_level='instance_in_room_measurement',
        evidence_source='instance',
        observables=(_observable(domain='in_room'),),
        measurement_domain='in_room',
    )


def test_instance_ref_must_be_installed_instance() -> None:
    with pytest.raises(ValueError, match='installed_instance'):
        build_instance_evidence(
            document_id=DOC,
            instance_ref=_ref('model_speaker', 'm', SHA_B),
            evidence_level='instance_lab_measurement',
            evidence_source='instance',
            observables=(_observable(),),
        )


def _delta(instance_evidence=None, reference=None, **overrides):
    instance_evidence = instance_evidence or _instance_evidence()
    reference = reference or build_instance_evidence(
        document_id=DOC,
        model_ref=_model_ref(),
        evidence_level='golden_sample_reference',
        evidence_source='golden',
        observables=(_observable(),),
        measurement_domain='free_field',
        measured_at_utc=T0,
        declared_at_utc=T0,
    )
    kwargs = dict(
        document_id=DOC,
        reference_evidence=reference,
        instance_evidence=instance_evidence,
        quantity='on_axis_response',
        algorithm='envelope_delta',
        algorithm_version='1.0',
        frequency_min_hz=200.0,
        frequency_max_hz=8000.0,
        delta_summary=(('200-8k', 0.8),),
        max_delta_db=0.8,
        derived_at_utc=T2,
    )
    kwargs.update(overrides)
    return build_model_instance_delta(**kwargs)


def test_delta_requires_evidence_refs() -> None:
    from htdt.cad_instance_variation_authority import (
        CadModelToInstanceDelta,
    )

    delta = _delta()
    payload = delta.model_dump(mode='json')
    payload['reference_evidence_ref'] = _ref(
        'installed_instance', 'x', SHA_A
    ).model_dump(mode='json')
    with pytest.raises(ValueError, match='instance_acoustic_evidence'):
        CadModelToInstanceDelta.model_validate(payload)


def test_delta_summary_names_unique() -> None:
    with pytest.raises(ValueError, match='unique'):
        _delta(delta_summary=(('a', 1.0), ('a', 2.0)))


def test_delta_frequency_domain_ordered() -> None:
    with pytest.raises(ValueError, match='exceed'):
        _delta(frequency_min_hz=8000.0, frequency_max_hz=200.0)


def test_directivity_3d_requires_free_field_both_sides() -> None:
    instance = _instance_evidence(
        observables=(_observable(domain='in_room'),),
        measurement_domain='in_room',
        evidence_level='instance_in_room_measurement',
    )
    delta = _delta(
        instance_evidence=instance,
        quantity='directivity_full_3d',
    )
    reference = build_instance_evidence(
        document_id=DOC,
        model_ref=_model_ref(),
        evidence_level='golden_sample_reference',
        evidence_source='golden',
        observables=(_observable(),),
        measurement_domain='free_field',
        declared_at_utc=T0,
    )
    with pytest.raises(ValueError, match='free-field'):
        validate_delta_compatibility(
            delta=delta, reference=reference, instance=instance,
        )


def test_large_signal_requires_large_signal_observables() -> None:
    delta = _delta(quantity='large_signal_capability')
    instance = _instance_evidence()
    reference = build_instance_evidence(
        document_id=DOC,
        model_ref=_model_ref(),
        evidence_level='golden_sample_reference',
        evidence_source='golden',
        observables=(_observable(),),
        measurement_domain='free_field',
        declared_at_utc=T0,
    )
    with pytest.raises(ValueError, match='large-signal'):
        validate_delta_compatibility(
            delta=delta, reference=reference, instance=instance,
        )


def test_impedance_curve_requires_impedance_observable() -> None:
    delta = _delta(quantity='impedance_curve')
    instance = _instance_evidence()
    reference = build_instance_evidence(
        document_id=DOC,
        model_ref=_model_ref(),
        evidence_level='golden_sample_reference',
        evidence_source='golden',
        observables=(_observable(),),
        measurement_domain='free_field',
        declared_at_utc=T0,
    )
    with pytest.raises(ValueError, match='impedance'):
        validate_delta_compatibility(
            delta=delta, reference=reference, instance=instance,
        )
    instance_z = _instance_evidence(
        observables=(
            _observable('impedance_zf', value=8.1, unit='ohm'),
        ),
    )
    delta_z = _delta(
        instance_evidence=instance_z, quantity='impedance_curve',
    )
    validate_delta_compatibility(
        delta=delta_z, reference=reference, instance=instance_z,
    )


def test_classify_delta_ladder() -> None:
    delta = _delta(max_delta_db=0.4)
    assert classify_instance_delta(
        delta=delta, confirmed_fault=True,
    ) == 'confirmed_fault'
    assert classify_instance_delta(
        delta=delta, environmental_confound=True,
    ) == 'environment_dependent'
    assert classify_instance_delta(
        delta=delta, inconclusive=True,
    ) == 'measurement_inconclusive'
    assert classify_instance_delta(
        delta=delta, suspected_defect=True,
    ) == 'suspected_defect'
    assert classify_instance_delta(
        delta=delta,
    ) == 'no_population_tolerance_available'
    assert classify_instance_delta(
        delta=delta, tolerance_db=0.5,
        tolerance_source='manufacturer_population',
    ) == 'within_declared_tolerance'
    assert classify_instance_delta(
        delta=delta, tolerance_db=0.5, tolerance_source='project',
    ) == 'matched_within_project_tolerance'
    assert classify_instance_delta(
        delta=delta, tolerance_db=0.3,
        tolerance_source='manufacturer_population',
    ) == 'outlier'


_ALL_SET_TOLERANCES = {
    'level_spread': (0.5, 'dB'),
    'fr_deviation': (1.5, 'dB'),
    'polarity_consistency': (0.0, 'ratio'),
    'impedance_spread': (1.0, 'ohm'),
    'delay_spread': (0.5, 'ms'),
    'nonlinear_spread': (0.2, '%'),
}


def _full_observables(sensitivity: float = 87.5):
    """One observable record per matched-set metric."""
    return (
        _observable('sensitivity', sensitivity, 'dB_2.83V_1m'),
        _observable('frequency_response_deviation', 0.8, 'dB'),
        _observable('polarity', 1.0, 'ratio'),
        _observable('impedance_zf', 8.1, 'ohm'),
        _observable('phase_group_delay', 1.2, 'ms'),
        _observable('thd_nonlinear', 0.4, 'pct'),
    )


def test_matched_set_requires_two_members() -> None:
    with pytest.raises(ValueError):
        build_matched_set(
            document_id=DOC,
            role='l_r',
            member_instance_refs=(_instance_ref('inst-fl'),),
        )
    with pytest.raises(ValueError, match='unique'):
        build_matched_set(
            document_id=DOC,
            role='l_r',
            member_instance_refs=(
                _instance_ref('inst-fl'),
                _instance_ref('inst-fl'),
            ),
        )


def test_matched_set_members_must_be_instances() -> None:
    with pytest.raises(ValueError, match='installed_instance'):
        build_matched_set(
            document_id=DOC,
            role='l_r',
            member_instance_refs=(
                _ref('model_speaker', 'm', SHA_B),
                _instance_ref('inst-fr'),
            ),
        )


def test_matched_set_no_evidence_insufficient() -> None:
    declaration = build_matched_set(
        document_id=DOC,
        role='l_r',
        member_instance_refs=(
            _instance_ref('inst-fl'),
            _instance_ref('inst-fr'),
        ),
    )
    qualification = evaluate_matched_set(
        document_id=DOC,
        declaration=declaration,
        evidence_by_instance={},
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'insufficient_evidence'
    assert all(
        verdict.state == 'unmeasured'
        for verdict in qualification.metric_verdicts
    )


def test_matched_set_environment_confounded() -> None:
    declaration = build_matched_set(
        document_id=DOC,
        role='l_r',
        member_instance_refs=(
            _instance_ref('inst-fl'),
            _instance_ref('inst-fr'),
        ),
    )
    evidence = _instance_evidence(
        observables=(
            _observable(environmental_confound=True),
        ),
    )
    qualification = evaluate_matched_set(
        document_id=DOC,
        declaration=declaration,
        evidence_by_instance={
            'inst-fl': evidence,
            'inst-fr': _instance_evidence('inst-fr', value=87.0),
        },
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'environment_dependent'


def test_matched_set_within_project_tolerance() -> None:
    declaration = build_matched_set(
        document_id=DOC,
        role='l_r',
        member_instance_refs=(
            _instance_ref('inst-fl'),
            _instance_ref('inst-fr'),
        ),
    )
    qualification = evaluate_matched_set(
        document_id=DOC,
        declaration=declaration,
        evidence_by_instance={
            'inst-fl': _instance_evidence(
                'inst-fl', observables=_full_observables(87.5),
            ),
            'inst-fr': _instance_evidence(
                'inst-fr', observables=_full_observables(87.2),
            ),
        },
        tolerances=_ALL_SET_TOLERANCES,
        tolerance_source='project',
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'matched_within_project_tolerance'
    level = next(
        v for v in qualification.metric_verdicts
        if v.metric == 'level_spread'
    )
    assert level.state == 'within'
    assert level.spread_value == pytest.approx(0.3)


def test_matched_set_outlier_flags_limiting_instance() -> None:
    declaration = build_matched_set(
        document_id=DOC,
        role='l_c_r',
        member_instance_refs=(
            _instance_ref('inst-fl'),
            _instance_ref('inst-c'),
            _instance_ref('inst-fr'),
        ),
    )
    qualification = evaluate_matched_set(
        document_id=DOC,
        declaration=declaration,
        evidence_by_instance={
            'inst-fl': _instance_evidence(
                'inst-fl', observables=_full_observables(87.0),
            ),
            'inst-c': _instance_evidence(
                'inst-c', observables=_full_observables(83.0),
            ),
            'inst-fr': _instance_evidence(
                'inst-fr', observables=_full_observables(87.2),
            ),
        },
        tolerances={**_ALL_SET_TOLERANCES, 'level_spread': (1.0, 'dB')},
        tolerance_source='project',
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'outlier_detected'
    level = next(
        v for v in qualification.metric_verdicts
        if v.metric == 'level_spread'
    )
    assert level.state == 'outside'
    assert 'inst-c' in level.limiting_instance_ids


def test_matched_set_without_tolerance_unmeasured() -> None:
    declaration = build_matched_set(
        document_id=DOC,
        role='l_r',
        member_instance_refs=(
            _instance_ref('inst-fl'),
            _instance_ref('inst-fr'),
        ),
    )
    qualification = evaluate_matched_set(
        document_id=DOC,
        declaration=declaration,
        evidence_by_instance={
            'inst-fl': _instance_evidence('inst-fl', value=87.5),
            'inst-fr': _instance_evidence('inst-fr', value=87.2),
        },
        tolerances=None,
        tolerance_source='none',
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'no_population_tolerance_available'


def test_instance_variation_repository_roundtrip(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadInstanceVariationRepository(scene)
    evidence = _instance_evidence()
    repo.save_evidence(evidence)
    assert repo.get_evidence(evidence.evidence_id) == evidence
    repo.save_evidence(evidence)  # idempotent

    delta = _delta(instance_evidence=evidence)
    repo.save_delta(delta)
    assert repo.get_delta(delta.delta_id) == delta

    declaration = build_matched_set(
        document_id=DOC,
        role='l_r',
        member_instance_refs=(
            _instance_ref('inst-fl'),
            _instance_ref('inst-fr'),
        ),
    )
    repo.save_matched_set(declaration)
    assert repo.get_matched_set(declaration.set_id) == declaration

    qualification = evaluate_matched_set(
        document_id=DOC,
        declaration=declaration,
        evidence_by_instance={
            'inst-fl': evidence,
            'inst-fr': _instance_evidence('inst-fr', value=87.2),
        },
        evaluated_at_utc=T2,
    )
    repo.save_qualification(qualification)
    assert repo.get_qualification(
        qualification.qualification_id
    ) == qualification
    assert len(repo.list_qualifications(DOC)) == 1


def test_instance_variation_repository_rejects_tampered_row(
    tmp_path,
) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadInstanceVariationRepository(scene)
    evidence = _instance_evidence()
    repo.save_evidence(evidence)
    with repo._connect() as connection:
        connection.execute(
            "UPDATE cad_instance_acoustic_evidence "
            "SET evidence_level='instance_in_room_measurement' "
            'WHERE evidence_id=?',
            (evidence.evidence_id,),
        )
        connection.commit()
    with pytest.raises(InstanceVariationIntegrityError):
        repo.get_evidence(evidence.evidence_id)


# ---------------------------------------------------------------------------
# #632 media-playback capability qualification
# ---------------------------------------------------------------------------


def _stack(**overrides):
    kwargs = dict(
        document_id=DOC,
        source_class='streaming_app',
        device_identity='avrx-6800',
        os_version='avr-os 3.1',
        app_name='streamer',
        app_version='9.4',
        playback_engine='native',
        firmware='fw-1.20',
        license_state='licensed',
        source_mode='network',
        observed_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_playback_stack(**kwargs)


def _media(**overrides):
    kwargs = dict(
        document_id=DOC,
        label='atmos-4k60-hdr',
        container='mp4',
        delivery_class='segmented_adaptive',
        video_codec='hevc',
        video_profile='main10',
        resolution='3840x2160',
        frame_rate_hz=60.0,
        bit_depth=10,
        hdr_metadata_profile='dv_p8',
        audio_codec='eac3',
        audio_profile='joc',
        audio_layout='5.1.4',
        audio_sample_rate_hz=48000,
        encryption_requirement='required',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_media_requirement(**kwargs)


def _observation_pb(output='requested_profile_rendered', **overrides):
    kwargs = dict(
        output_state=output,
        bitstream_mode='bitstream',
        hdr_output_state='hdr_rendered',
    )
    kwargs.update(overrides)
    return CadPlaybackObservation(**kwargs)


def _record(
    stack=None,
    media=None,
    capability='tested_playable',
    evidence='empirically_tested',
    output='requested_profile_rendered',
    **overrides,
):
    stack = stack or _stack()
    media = media or _media()
    kwargs = dict(
        document_id=DOC,
        stack=stack,
        media=media,
        capability_class=capability,
        evidence_class=evidence,
        observation=_observation_pb(output) if output else None,
        observed_at_utc=T1,
    )
    kwargs.update(overrides)
    return build_capability_record(**kwargs)


def test_wave_binding_requires_segmented_delivery() -> None:
    with pytest.raises(ValueError, match='segmented'):
        _media(
            delivery_class='local_file',
            wave_profile=CadWaveProfileBinding(
                wave_profile='WAVE-PCM',
            ),
        )
    _media(
        delivery_class='segmented_adaptive',
        wave_profile=CadWaveProfileBinding(
            wave_profile='WAVE-PCM',
            test_maturity='validated',
        ),
    )


def test_tested_capability_requires_empirical_evidence() -> None:
    with pytest.raises(ValueError, match='empirically_tested'):
        _record(
            capability='tested_playable',
            evidence='api_reported',
        )


def test_unsupported_requires_failure_attribution() -> None:
    with pytest.raises(ValueError, match='failure attribution'):
        _record(
            capability='unsupported',
            failure_attribution='unknown',
            output=None,
        )


def test_tested_playable_requires_exact_output() -> None:
    with pytest.raises(ValueError, match='requested profile'):
        _record(
            capability='tested_playable',
            output='sdr_fallback',
        )
    _record(
        capability='tested_fallback',
        output='sdr_fallback',
    )


def test_capability_record_duplicate_operations_rejected() -> None:
    with pytest.raises(ValueError, match='once'):
        _record(
            operations=(
                CadOperationResultRow(
                    operation='random_access_seek', result='passed',
                ),
                CadOperationResultRow(
                    operation='random_access_seek', result='failed',
                ),
            ),
        )


def test_capability_record_ref_kinds_enforced() -> None:
    from htdt.cad_media_playback_authority import (
        CadPlaybackCapabilityRecord,
    )

    record = _record()
    payload = record.model_dump(mode='json')
    payload['stack_ref'] = _ref(
        'media_profile_requirement', 'x', SHA_A
    ).model_dump(mode='json')
    with pytest.raises(ValueError, match='playback_stack_identity'):
        CadPlaybackCapabilityRecord.model_validate(payload)


def test_playback_no_evidence_insufficient() -> None:
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=_stack(),
        media=_media(),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'insufficient_evidence'


def test_playback_declared_only_insufficient() -> None:
    stack = _stack()
    media = _media()
    record = build_capability_record(
        document_id=DOC,
        stack=stack,
        media=media,
        capability_class='declared_by_device_api',
        evidence_class='api_reported',
        observed_at_utc=T1,
    )
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(record,),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'insufficient_evidence'


def test_playback_exact_profile_qualified() -> None:
    stack = _stack()
    media = _media()
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(_record(stack=stack, media=media),),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'qualified_exact_profile'
    assert qualification.fallback_state is None


def test_playback_fallback_qualified_with_fallback() -> None:
    stack = _stack()
    media = _media()
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(
            _record(
                stack=stack, media=media,
                capability='tested_fallback',
                output='multichannel_to_stereo_downmix',
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'qualified_with_fallback'
    assert (
        qualification.fallback_state
        == 'multichannel_to_stereo_downmix'
    )


def test_playback_unexpected_output_is_mismatch() -> None:
    stack = _stack()
    media = _media()
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(
            _record(
                stack=stack, media=media,
                capability='tested_with_limitations',
                output='unexpected_output',
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'output_profile_mismatch'


def test_playback_unsupported_decoder_fault() -> None:
    stack = _stack()
    media = _media()
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(
            _record(
                stack=stack, media=media,
                capability='unsupported',
                failure_attribution='decoder_player_failure',
                output=None,
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'player_unsupported'


def test_playback_transport_failure_not_decoder_fault() -> None:
    stack = _stack()
    media = _media()
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(
            _record(
                stack=stack, media=media,
                capability='unsupported',
                failure_attribution='network_delivery_failure',
                output=None,
            ),
        ),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'transport_dependency_failed'


def test_playback_intermittent_on_conflicting_tests() -> None:
    stack = _stack()
    media = _media()
    ok = _record(
        stack=stack, media=media,
        capability='tested_playable',
        output='requested_profile_rendered',
    )
    limited = _record(
        stack=stack, media=media,
        capability='tested_with_limitations',
        output='requested_profile_rendered',
    )
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(ok, limited),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'intermittent'


def test_playback_failed_operation_is_limitation() -> None:
    stack = _stack()
    media = _media()
    run = build_operation_run(
        document_id=DOC,
        stack=stack,
        media=media,
        scenario='movie_session',
        operations=(
            CadOperationResultRow(
                operation='initial_start', result='passed',
            ),
            CadOperationResultRow(
                operation='representation_switch', result='failed',
            ),
        ),
        started_at_utc=T1,
    )
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(_record(stack=stack, media=media),),
        runs=(run,),
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'qualified_with_limitations'
    assert 'representation_switch' in qualification.failed_operations


def test_playback_stack_update_stales_evidence() -> None:
    stack = _stack()
    media = _media()
    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(_record(stack=stack, media=media),),
        stack_updated_since=True,
        evaluated_at_utc=T2,
    )
    assert qualification.verdict == 'insufficient_evidence'
    assert qualification.stale is True


def test_media_playback_repository_roundtrip(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadMediaPlaybackRepository(scene)
    stack = _stack()
    repo.save_stack(stack)
    assert repo.get_stack(stack.stack_id) == stack
    repo.save_stack(stack)  # idempotent

    media = _media()
    repo.save_requirement(media)
    assert repo.get_requirement(media.requirement_id) == media

    record = _record(stack=stack, media=media)
    repo.save_record(record)
    assert repo.get_record(record.record_id) == record

    run = build_operation_run(
        document_id=DOC,
        stack=stack,
        media=media,
        scenario='movie_session',
        operations=(
            CadOperationResultRow(
                operation='initial_start', result='passed',
            ),
        ),
        started_at_utc=T1,
    )
    repo.save_run(run)
    assert repo.get_run(run.run_id) == run

    qualification = evaluate_playback_capability(
        document_id=DOC,
        stack=stack,
        media=media,
        records=(record,),
        runs=(run,),
        evaluated_at_utc=T2,
    )
    repo.save_qualification(qualification)
    assert repo.get_qualification(
        qualification.qualification_id
    ) == qualification
    assert len(repo.list_qualifications(DOC)) == 1


def test_media_playback_repository_rejects_tampered_row(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadMediaPlaybackRepository(scene)
    stack = _stack()
    repo.save_stack(stack)
    with repo._connect() as connection:
        connection.execute(
            "UPDATE cad_playback_stack_identities "
            "SET firmware='fw-9.99' WHERE stack_id=?",
            (stack.stack_id,),
        )
        connection.commit()
    with pytest.raises(MediaPlaybackIntegrityError):
        repo.get_stack(stack.stack_id)
