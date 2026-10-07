"""REV65 NEWARENA — adversarial regression tests for the post-REV61
sealed authority modules.

Each test pins one confirmed defect fixed in this round (see
docs/reviews/rev65-newarena.md): the assertion failed on the pre-fix
code and passes now.
"""

from __future__ import annotations

import pytest

from htdt.acoustic_validation_envelope import (
    build_accuracy_envelope,
    error_statistic,
)
from htdt.cad_acoustic_solver_adapter import (
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_calibration import (
    CadCalibrationExportSnapshot,
    CadCrossoverSetting,
    CadExportedChannelSettings,
)
from htdt.cad_camilladsp_deploy import (
    CAMILLADSP_DEPLOY_ADAPTER_ID,
    CamillaDSPDeploymentSession,
    compile_camilladsp_config,
    normalize_camilladsp_config,
)
from htdt.cad_device_adapter import build_device_binding
from htdt.cad_equalizer_apo_export import apo_band_support_problems
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_generic_dsp_export import ExportFilterBand
from htdt.cad_owned_room_campaign import (
    CampaignMeasurement,
    CampaignPreregistration,
    CampaignVerdict,
    ClaimVerdict,
    evaluate_campaign_promotion,
)
from htdt.cad_solver_capability_manifest import (
    SOLVER_PATH_PHENOMENA,
    SolverCapabilityRow,
    build_solver_capability_manifest,
)
from htdt.cad_solver_confidence_bound import (
    EnvironmentalBinding,
    SolverInputEnvelope,
    evaluate_input_envelope,
)
from htdt.canonical_json import canonical_sha256
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


DOC = 'doc-rev65'
NOW = '2026-10-07T00:00:00Z'
_SHA = canonical_sha256({'fixture': 'rev65'})


def _ref(kind: str, rid: str = 'x', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _xref(label: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test-authority:{label}',
        authority_version='1',
        semantic_hash_sha256='ab' * 32,
    )


# --------------------------------------------------------------- issue #838


def test_apo_notch_and_band_pass_take_no_gain() -> None:
    """notch/band_pass accept NO gain_db: a bare band is supported, a
    gained one is unsupported — the pre-fix check was inverted both
    ways."""
    for filter_type in ('notch', 'band_pass'):
        assert apo_band_support_problems(ExportFilterBand(
            filter_type=filter_type, frequency_hz=1000.0, q=4.0,
        )) == ()
        problems = apo_band_support_problems(ExportFilterBand(
            filter_type=filter_type, frequency_hz=1000.0, q=4.0,
            gain_db=-3.0,
        ))
        assert any('takes no gain_db' in p for p in problems)


def _export(channels: tuple[CadExportedChannelSettings, ...]
            ) -> CadCalibrationExportSnapshot:
    payload = dict(
        export_id='exp-rev65',
        created_at_utc=NOW,
        calibration_plan_id='plan-rev65',
        requested_plan_semantic_sha256='a' * 64,
        sample_rate_hz=48000,
        channels=channels,
        quantization_applied=False,
        quantization_notes=(),
    )
    provisional = CadCalibrationExportSnapshot.model_construct(**payload)
    return CadCalibrationExportSnapshot(
        **payload,
        exported_settings_semantic_sha256=canonical_sha256(
            provisional.semantic_payload()),
    )


def _channel(channel_id: str, **kw) -> CadExportedChannelSettings:
    payload = dict(
        channel_id=channel_id,
        role_id='SUB',
        source_entity_id='spk-sub',
        physical_output_id='0',
        gain_db=1.0,
        delay_s=0.0,
        polarity='normal',
        crossovers=(),
        peq=(),
        routing=('0',),
    )
    payload.update(kw)
    return CadExportedChannelSettings(**payload)


def _binding(routing) -> object:
    return build_device_binding(
        adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
        binding_id='adb-rev65',
        device_family='camilladsp',
        device_model='CamillaDSP',
        device_serial='camilladsp://127.0.0.1:1234',
        firmware_version='3.0.0',
        routing=routing,
        bound_at_utc=NOW,
    )


def test_compile_colliding_channel_names_fail_closed() -> None:
    """'sub.out' and 'sub_out' slug to the same htdt filter region —
    pre-fix the second channel silently overwrote the first's filters;
    now the collision lands in unsupported_items."""
    export = _export((
        _channel('sub.out', gain_db=1.0),
        _channel('sub_out', gain_db=-2.0),
    ))
    binding = _binding((('sub.out', '0'), ('sub_out', '1')))
    _config, _notes, unsupported = compile_camilladsp_config(
        export, binding, {'devices': {'samplerate': 48000}})
    assert any('collides' in item for item in unsupported)


def test_deployment_session_requires_readback_pairing() -> None:
    """A matched verdict without the pinned read-back sha is an
    unverifiable claim the ladder cannot advance on — both directions
    of the pairing are enforced."""
    ref = _ref('calibration_deployment', 'dep-1')
    base = dict(
        document_id=DOC,
        deployment_ref=ref,
        binding_sha256='a' * 64,
        materialization_sha256='b' * 64,
        candidate_config_sha256='c' * 64,
        deployed_at_utc=NOW,
    )
    with pytest.raises(ValueError):
        CamillaDSPDeploymentSession.create(
            readback_matched=True, **base)
    with pytest.raises(ValueError):
        CamillaDSPDeploymentSession.create(
            readback_config_sha256='d' * 64, **base)
    ok = CamillaDSPDeploymentSession.create(
        readback_matched=False, readback_config_sha256='d' * 64, **base)
    assert ok.readback_matched is False


def test_normalize_surfaces_deviating_crossover_q() -> None:
    """An htdt_-owned order-2 crossover is only ever written as
    Butterworth. A device-side q drift must surface as a diff, not
    normalize back to a matching channel."""
    export = _export((
        _channel(
            'sub', gain_db=0.0,
            crossovers=(CadCrossoverSetting(
                crossover_type='high_pass', frequency_hz=80.0,
                filter_order=2),),
        ),
    ))
    binding = _binding((('sub', '0'),))
    config, _notes, unsupported = compile_camilladsp_config(
        export, binding, {'devices': {'samplerate': 48000}})
    assert unsupported == ()
    normalized = normalize_camilladsp_config(config)
    assert normalized is not None
    channels, _blob = normalized
    assert channels[0].crossovers == (
        CadCrossoverSetting(
            crossover_type='high_pass', frequency_hz=80.0,
            filter_order=2),)

    # Simulate device-side drift on the htdt_-owned filter.
    for name, spec in config['filters'].items():
        if spec.get('parameters', {}).get('type') == 'Highpass':
            spec['parameters']['q'] = 1.4  # Linkwitz-ish: not ours
    channels2, _blob2 = normalize_camilladsp_config(config)
    # the drifted filter is no longer htdt-representable: it drops out
    # of the reconstruction so the field diff reports the deviation.
    assert channels2[0].crossovers == ()


# --------------------------------------------------------------- issue #811


def _solver_envelope() -> object:
    return build_accuracy_envelope(
        solver_id='wave-solver',
        solver_algorithm='fdtd',
        solver_version='2.0',
        observable='spatial_field_db',
        geometry_domain='closed room',
        boundary_material_assumptions='complex impedance',
        source_capability='measured balloon',
        receiver_capability='mic array',
        fixture_ids=('fx-1',),
        fixture_sha256s=(_SHA,),
        error_statistic_definition='absolute error in dB',
        error_distribution=error_statistic(
            'spatial_field_db', 'dB', (0.1, -0.2, 0.15)),
        threshold_policy_id='policy-1',
        threshold_policy_revision=2,
        validation_state='VALIDATED_FOR_DECLARED_DOMAIN',
        validated_at_utc=NOW,
    )


def _manifest(adapter_version: str = '2.0') -> object:
    descriptor = build_acoustic_solver_adapter_descriptor(
        adapter_id='htdt.fdtd-wave',
        adapter_version=adapter_version,
        model_solver_role_id='wave-solver',
        acoustic_domain='wave',
        solver_implementation_ref=_xref('fdtd-impl'),
        solver_configuration_schema_ref=_xref('fdtd-config'),
        supported_snapshot_schema_versions=(1,),
        supported_observables=('complex_pressure',),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0, maximum_hz=5000.0),
    )
    return build_solver_capability_manifest(
        descriptor=descriptor,
        rows=tuple(
            SolverCapabilityRow(
                phenomenon=phenomenon,
                state='SUPPORTED',
                valid_frequency_domain=FrequencyDomain(
                    minimum_hz=20.0, maximum_hz=5000.0),
            )
            for phenomenon in SOLVER_PATH_PHENOMENA
        ),
    )


def _input_envelope(**kw) -> SolverInputEnvelope:
    payload = dict(
        document_id=DOC,
        solver_request_ref=_ref('solver_request', 'req-1'),
        environment=tuple(
            EnvironmentalBinding(aspect=aspect, state='declared')
            for aspect in (
                'temperature', 'humidity', 'speed_of_sound',
                'door_opening_state', 'movable_objects',
                'playback_state')),
        declared_at_utc=NOW,
    )
    payload.update(kw)
    return SolverInputEnvelope.create(**payload)


def test_input_envelope_resolves_sealed_solver_pins() -> None:
    """solver_envelope_ref / capability_manifest_ref are sha-pinned
    authority pins — the evaluator must resolve them to the bound
    records, never silently feed whatever the caller passed."""
    env = _solver_envelope()
    manifest = _manifest()
    envelope = _input_envelope(
        solver_envelope_ref=AuthorityRef(
            kind='accuracy_envelope', ref_id=env.envelope_id,
            ref_sha256=env.envelope_sha256),
        capability_manifest_ref=AuthorityRef(
            kind='solver_capability_manifest',
            ref_id=manifest.manifest_id,
            ref_sha256=manifest.semantic_sha256),
    )
    # The pinned solver envelope is not among the supplied records.
    with pytest.raises(ValueError, match='solver_envelope'):
        evaluate_input_envelope(
            envelope, solver_envelopes=(), manifest=manifest,
            evaluated_at_utc=NOW)
    # The pinned manifest is not supplied at all.
    with pytest.raises(ValueError, match='capability_manifest'):
        evaluate_input_envelope(
            envelope, solver_envelopes=(env,), manifest=None,
            evaluated_at_utc=NOW)
    # A different manifest contradicting the pin is rejected too —
    # spoofing the pinned id still fails the sha check.
    other = _manifest(adapter_version='9.9')
    object.__setattr__(other, 'manifest_id', manifest.manifest_id)
    with pytest.raises(ValueError, match='capability_manifest'):
        evaluate_input_envelope(
            envelope, solver_envelopes=(env,), manifest=other,
            evaluated_at_utc=NOW)
    # Exactly the pinned authorities resolve.
    record = evaluate_input_envelope(
        envelope, solver_envelopes=(env,), manifest=manifest,
        evaluated_at_utc=NOW)
    assert record.rows


# --------------------------------------------------------------- issue #813


def _prereg(**kw) -> CampaignPreregistration:
    payload = dict(
        document_id=DOC,
        protocol_id='owned-room-o90',
        protocol_version='1.0',
        scene_ref=_ref('scene', 'scene-1'),
        solver_ref=_ref('solver', 'r130d'),
        calibration_condition_ids=('cond-srcA-seat1',),
        holdout_condition_ids=('cond-srcB-seat2',),
        holdout_dimensions=('receiver_position',),
        measurement_positions=('mlp', 'seat2'),
        candidate_ids=('cand-1', 'cand-2'),
        metrics=('fr_residual_db',),
        stop_conditions=('repeatability_floor_too_high',),
        preregistered_at_utc='2026-10-01T00:00:00Z',
    )
    payload.update(kw)
    return CampaignPreregistration.create(**payload)


def _measurement(
    prereg: CampaignPreregistration,
    role: str = 'holdout',
    **kw,
) -> CampaignMeasurement:
    payload = dict(
        document_id=prereg.document_id,
        campaign_ref=AuthorityRef(
            kind='campaign_preregistration',
            ref_id=prereg.preregistration_id,
            ref_sha256=prereg.preregistration_sha256),
        role=role,
        condition_id='cond-srcB-seat2',
        raw_asset_sha256='b' * 64,
        acquired_at_utc='2026-10-02T00:00:00Z',
    )
    payload.update(kw)
    return CampaignMeasurement.create(**payload)


def _mref(m: CampaignMeasurement) -> AuthorityRef:
    return AuthorityRef(
        kind='campaign_measurement',
        ref_id=m.measurement_id,
        ref_sha256=m.measurement_sha256)


def _verdict(
    prereg: CampaignPreregistration,
    measurements: tuple[CampaignMeasurement, ...],
    **kw,
) -> CampaignVerdict:
    payload = dict(
        document_id=prereg.document_id,
        campaign_ref=AuthorityRef(
            kind='campaign_preregistration',
            ref_id=prereg.preregistration_id,
            ref_sha256=prereg.preregistration_sha256),
        protocol_id=prereg.protocol_id,
        protocol_version=prereg.protocol_version,
        claim_verdicts=(
            ClaimVerdict(
                claim_kind='absolute_response', verdict='pass',
                evidence_refs=(_ref('evidence', 'a'),)),
            ClaimVerdict(
                claim_kind='candidate_ranking', verdict='pass',
                evidence_refs=(_ref('evidence', 'b'),)),
        ),
        promotion_outcome='recommendation_eligible',
        external_benchmark_ref=_ref('bench'),
        numerical_convergence_ref=_ref('conv'),
        input_qualification_ref=_ref('inpq'),
        measurement_uncertainty_ref=_ref('uncertainty'),
        holdout_residual_ref=_ref('resid'),
        repeatability_ref=_ref('repeat'),
        candidate_separation_ref=_ref('separation'),
        applicability_ref=_ref('appl'),
        holdout_measurement_refs=tuple(
            _mref(m) for m in measurements if m.role == 'holdout'),
        concluded_at_utc='2026-10-05T00:00:00Z',
    )
    payload.update(kw)
    return CampaignVerdict.create(**payload)


def test_promotion_rejects_unresolvable_evidence_refs() -> None:
    """A verdict pinning measurement authority that resolves to no
    bound campaign measurement is a structural violation — the ladder
    must floor at owned_room_insufficient, not claim eligible."""
    prereg = _prereg()
    m = _measurement(prereg)
    verdict = _verdict(
        prereg, (m,),
        holdout_measurement_refs=(
            _mref(m),
            # a measurement that does not exist in this campaign
            _ref('campaign_measurement', 'crm-deadbeef', 'c' * 64),
        ))
    assert evaluate_campaign_promotion(prereg, (m,), verdict) \
        == 'owned_room_insufficient'


def test_promotion_rejects_tampered_sha_pin() -> None:
    """A ref whose id resolves but whose pinned sha disagrees with the
    bound measurement is tampered evidence, not a holdout."""
    prereg = _prereg()
    m = _measurement(prereg)
    verdict = _verdict(
        prereg, (m,),
        holdout_measurement_refs=(
            AuthorityRef(
                kind='campaign_measurement',
                ref_id=m.measurement_id,
                ref_sha256='d' * 64),
        ))
    assert evaluate_campaign_promotion(prereg, (m,), verdict) \
        == 'owned_room_insufficient'


def test_promotion_rejects_cross_campaign_verdict() -> None:
    """A verdict sealed for a different campaign cannot ride on this
    preregistration's timing."""
    prereg = _prereg()
    other = _prereg(protocol_version='2.0')
    m = _measurement(prereg)
    verdict = _verdict(
        prereg, (m,),
        campaign_ref=AuthorityRef(
            kind='campaign_preregistration',
            ref_id=other.preregistration_id,
            ref_sha256=other.preregistration_sha256))
    assert evaluate_campaign_promotion(prereg, (m,), verdict) \
        == 'owned_room_insufficient'


def test_verdict_rejects_duplicate_claim_kinds() -> None:
    """Two rows on the same claim family silently last-won inside the
    evaluator — the model now rejects the ambiguous record."""
    prereg = _prereg()
    with pytest.raises(ValueError, match='claim_kind'):
        _verdict(
            prereg, (),
            claim_verdicts=(
                ClaimVerdict(
                    claim_kind='absolute_response', verdict='fail'),
                ClaimVerdict(
                    claim_kind='absolute_response', verdict='pass',
                    evidence_refs=(_ref('evidence', 'a'),)),
            ))
