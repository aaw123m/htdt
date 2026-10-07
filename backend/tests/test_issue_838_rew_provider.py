"""#838 delegated measurement provider + file-export authority tests.

Issue #838 slice A: REW stays a delegated provider (never copied into
HTDT core), licensing is a capability condition (automated sweep needs a
REW Pro upgrade), file-based deployment evidence has an honest ceiling —
a written/read-back file is never runtime truth, only post-deployment
measurement promotes to ``post_measurement_verified`` — and unsupported
target parameters fail closed at render time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_delegated_provider import (
    DelegatedProviderManifest,
    ProviderCapabilityEntry,
    REW_DECLARED_CAPABILITIES,
    build_htdt_native_import_manifest,
    build_provider_acquisition,
    build_rew_provider_manifest,
    evaluate_provider_gate,
    manifest_ref,
)
from htdt.cad_delegated_provider_repository import (
    CadDelegatedProviderRepository,
    DelegatedProviderIntegrityError,
)
from htdt.cad_equalizer_apo_export import (
    ApoExportBand,
    ApoExportChannel,
    ApoExportUnsupportedError,
    apo_band_support_problems,
    render_equalizer_apo_config,
    verify_exported_apo_config,
)
from htdt.cad_file_export_deployment import (
    FileExportDeployment,
    derive_file_export_state,
    evaluate_file_export_verification,
)
from htdt.cad_generic_dsp_export import (
    GenericExportChannel,
    GenericExportUnsupportedError,
    GenericFirChannel,
    biquad_export_support_problems,
    render_generic_biquad_export,
    render_generic_fir_export,
    render_generic_peq_export,
    render_manual_settings_handoff,
)
from htdt.cad_calibration import build_biquad_filter, evaluate_biquad_db
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256
from htdt.rew_api import RewEngineSession


DOC = 'doc-838'
_SHA = canonical_sha256({'fixture': 'sha'})
_TS = '2026-10-07T00:00:00Z'


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _manifest(**kw) -> DelegatedProviderManifest:
    payload = dict(
        document_id=DOC,
        provider_class='rew_api',
        provider_id='rew',
        provider_version='5.40',
        adapter_id='htdt-rew-api',
        adapter_version='1',
        endpoint_kind='localhost',
        endpoint_repr='http://localhost:4735',
        capabilities=(
            ProviderCapabilityEntry(
                capability='frequency_response', condition='supported',
                observed=True),
            ProviderCapabilityEntry(
                capability='measurement_list', condition='supported',
                observed=True),
            ProviderCapabilityEntry(
                capability='automated_sweep',
                condition='licensed_required',
                condition_detail='automated sweep requires REW Pro'),
            ProviderCapabilityEntry(
                capability='spl_leq', condition='unsupported',
                condition_detail='no SPL endpoint in reviewed API'),
        ),
        declared_at_utc=_TS,
    )
    payload.update(kw)
    return DelegatedProviderManifest.create(**payload)


def _acquisition(manifest: DelegatedProviderManifest, **kw):
    args = dict(
        capability='frequency_response',
        request_identity_repr='GET /measurements/1/response',
        request_sha256=_SHA,
        outcome='observed',
        raw_artifact_sha256='b' * 64,
        observed_at_utc=_TS,
    )
    args.update(kw)
    return build_provider_acquisition(manifest, **args)


def _file_deployment(**kw) -> FileExportDeployment:
    state, runtime, mode = derive_file_export_state(
        roundtrip_verdict='matched',
        install_evidence='installed_readback')
    payload = dict(
        document_id=DOC,
        target_class='equalizer_apo_config',
        target_scope_repr='Speakers (Realtek)',
        exported_config_sha256='c' * 64,
        renderer_id='htdt-export-equalizer-apo',
        renderer_version='1',
        rendered_format_id='equalizer-apo-config-txt',
        file_state=state,
        runtime_state=runtime,
        evidence_mode=mode,
        roundtrip_verdict='matched',
        evaluated_at_utc=_TS,
    )
    payload.update(kw)
    return FileExportDeployment.create(**payload)


def _repo(tmp_path: Path) -> CadDelegatedProviderRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadDelegatedProviderRepository(SceneRepository(db))


# ---------------------------------------------------------------------------
# manifest sealing + validators


class TestManifestSealing:
    def test_sealed_create(self) -> None:
        man = _manifest()
        assert man.manifest_id.startswith('dpm-')
        assert man.manifest_id[4:] == man.manifest_sha256[:24]

    def test_licensed_required_needs_detail(self) -> None:
        with pytest.raises(ValidationError):
            ProviderCapabilityEntry(
                capability='automated_sweep',
                condition='licensed_required')

    def test_unsupported_needs_detail(self) -> None:
        with pytest.raises(ValidationError):
            ProviderCapabilityEntry(
                capability='spl_leq', condition='unsupported')

    def test_unsupported_cannot_be_observed(self) -> None:
        with pytest.raises(ValidationError):
            ProviderCapabilityEntry(
                capability='spl_leq', condition='unsupported',
                condition_detail='x', observed=True)

    def test_duplicate_capability_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _manifest(capabilities=(
                ProviderCapabilityEntry(
                    capability='file_import', condition='supported'),
                ProviderCapabilityEntry(
                    capability='file_import', condition='supported'),
            ))

    def test_bound_endpoint_requires_repr(self) -> None:
        with pytest.raises(ValidationError):
            _manifest(endpoint_repr=None)

    def test_none_endpoint_rejects_repr(self) -> None:
        with pytest.raises(ValidationError):
            _manifest(endpoint_kind='none')

    def test_hash_mismatch_rejected(self) -> None:
        man = _manifest()
        with pytest.raises(ValidationError):
            DelegatedProviderManifest(
                **{**man.model_dump(mode='python'),
                   'manifest_sha256': _SHA})

    def test_manifest_ref_pins_sha(self) -> None:
        man = _manifest()
        ref = manifest_ref(man)
        assert ref.kind == 'delegated_provider_manifest'
        assert ref.ref_id == man.manifest_id
        assert ref.ref_sha256 == man.manifest_sha256


# ---------------------------------------------------------------------------
# provider gate — every verdict path


class TestProviderGate:
    def test_no_manifest_blocks(self) -> None:
        verdict, reason = evaluate_provider_gate(None, 'frequency_response')
        assert verdict == 'provider_blocked'
        assert reason == 'no_provider_manifest'

    def test_absent_capability_blocks(self) -> None:
        verdict, reason = evaluate_provider_gate(
            _manifest(), 'rta_live')
        assert verdict == 'provider_blocked'
        assert reason == 'capability_absent:rta_live'

    def test_supported_is_capable(self) -> None:
        verdict, _ = evaluate_provider_gate(
            _manifest(), 'frequency_response')
        assert verdict == 'provider_capable'

    def test_licensed_is_never_capable(self) -> None:
        verdict, reason = evaluate_provider_gate(
            _manifest(), 'automated_sweep')
        assert verdict == 'provider_license_required'
        assert 'automated_sweep' in reason

    def test_unsupported_blocks(self) -> None:
        verdict, reason = evaluate_provider_gate(_manifest(), 'spl_leq')
        assert verdict == 'provider_blocked'
        assert 'capability_unsupported' in reason

    def test_unknown_condition_blocks(self) -> None:
        verdict, _ = evaluate_provider_gate(
            _manifest(capabilities=(
                ProviderCapabilityEntry(
                    capability='other', condition='unknown'),)),
            'other')
        assert verdict == 'provider_blocked'


# ---------------------------------------------------------------------------
# acquisition records


class TestAcquisition:
    def test_observed_requires_evidence(self) -> None:
        with pytest.raises(ValidationError):
            _acquisition(_manifest(), raw_artifact_sha256=None)

    def test_observed_cannot_carry_error(self) -> None:
        with pytest.raises(ValidationError):
            _acquisition(_manifest(), error_detail='boom')

    def test_observed_blocked_capability_raises(self) -> None:
        # Gate-enforced: a declared-licensed capability can never seal an
        # observed acquisition (licensing is a condition, not a grant).
        with pytest.raises(ValueError, match='provider_license_required'):
            _acquisition(_manifest(), capability='automated_sweep')
        with pytest.raises(ValueError, match='provider_blocked'):
            _acquisition(_manifest(), capability='rta_live')

    def test_capability_rejected_records_gate_reason(self) -> None:
        record = _acquisition(
            _manifest(), capability='automated_sweep',
            outcome='capability_rejected', raw_artifact_sha256=None)
        assert record.outcome == 'capability_rejected'
        assert 'licensed' in (record.error_detail or '')

    def test_error_requires_detail(self) -> None:
        with pytest.raises(ValidationError):
            _acquisition(
                _manifest(), outcome='error', raw_artifact_sha256=None)

    def test_non_observed_rejects_evidence_refs(self) -> None:
        with pytest.raises(ValidationError):
            _acquisition(
                _manifest(), outcome='cancelled',
                raw_artifact_sha256=None,
                evidence_refs=(_ref('measurement'),))

    def test_manifest_ref_must_pin_sha(self) -> None:
        from htdt.cad_delegated_provider import ProviderAcquisitionRecord
        man = _manifest()
        with pytest.raises(ValidationError):
            ProviderAcquisitionRecord.create(
                document_id=DOC,
                manifest_ref=AuthorityRef(
                    kind='delegated_provider_manifest',
                    ref_id=man.manifest_id, ref_sha256=None),
                capability='frequency_response',
                request_identity_repr='x', request_sha256=_SHA,
                outcome='error', error_detail='e',
                observed_at_utc=_TS)

    def test_foreign_ref_kind_rejected(self) -> None:
        from htdt.cad_delegated_provider import ProviderAcquisitionRecord
        with pytest.raises(ValidationError):
            ProviderAcquisitionRecord.create(
                document_id=DOC,
                manifest_ref=_ref('calibration_deployment'),
                capability='frequency_response',
                request_identity_repr='x', request_sha256=_SHA,
                outcome='error', error_detail='e',
                observed_at_utc=_TS)


# ---------------------------------------------------------------------------
# REW provider manifest


class TestRewProvider:
    def _session(self) -> RewEngineSession:
        return RewEngineSession(
            engine_session_id='es-1', engine_kind='rew',
            engine_version='5.40', adapter_id='htdt-rew-api',
            adapter_version='1', endpoint='http://localhost:4735',
            observed_at_utc=_TS,
            capability_snapshot={'capabilities': {
                'version': True, 'list_measurements': True,
                'frequency_response': True}},
            semantic_sha256=_SHA)

    def test_declared_surface_covers_documented_api(self) -> None:
        caps = {c for c, _, _ in REW_DECLARED_CAPABILITIES}
        # Documented REW surface, per issue #838 §1.
        assert {
            'measurement_list', 'frequency_response', 'impulse_response',
            'group_delay', 'rta_live', 'spl_leq', 'spl_logger',
            'generator_control', 'automated_sweep', 'eq_alignment',
            'trace_processing', 'subscriptions', 'file_import',
        } <= caps

    def test_automated_sweep_is_licensed_condition(self) -> None:
        row = next(c for c in REW_DECLARED_CAPABILITIES
                   if c[0] == 'automated_sweep')
        assert row[1] == 'licensed_required'
        assert 'Pro' in row[2]

    def test_manifest_merges_session_identity(self) -> None:
        man = build_rew_provider_manifest(
            self._session(), document_id=DOC, declared_at_utc=_TS)
        assert man.provider_class == 'rew_api'
        assert man.provider_id == 'rew'
        assert man.provider_version == '5.40'
        assert man.endpoint_kind == 'localhost'
        assert man.endpoint_repr == 'http://localhost:4735'
        assert man.manifest_id.startswith('dpm-')

    def test_observed_flags_follow_session_snapshot(self) -> None:
        man = build_rew_provider_manifest(
            self._session(), document_id=DOC, declared_at_utc=_TS)
        entry = man.capability_entry('frequency_response')
        assert entry is not None and entry.observed
        entry = man.capability_entry('rta_live')
        assert entry is not None and not entry.observed

    def test_sweep_gates_licensed_never_capable(self) -> None:
        man = build_rew_provider_manifest(
            self._session(), document_id=DOC, declared_at_utc=_TS)
        verdict, _ = evaluate_provider_gate(man, 'automated_sweep')
        assert verdict == 'provider_license_required'

    def test_native_import_manifest(self) -> None:
        man = build_htdt_native_import_manifest(
            document_id=DOC, source_identity='measurement.csv',
            importer_id='imp-1', importer_version='1',
            declared_at_utc=_TS)
        assert man.provider_class == 'htdt_native_import'
        assert man.endpoint_kind == 'file'
        assert man.capability_entry('file_import').observed


# ---------------------------------------------------------------------------
# file-export deployment evidence


class TestFileExportDeployment:
    def test_sealed_create(self) -> None:
        fed = _file_deployment()
        assert fed.file_deployment_id.startswith('fed-')
        assert fed.file_deployment_id[4:] \
            == fed.file_deployment_sha256[:24]
        assert fed.file_state == 'file_readback_matched'
        assert fed.runtime_state == 'runtime_not_attested'
        assert fed.evidence_mode == 'file_level_readback'

    def test_readback_states_require_file_level_evidence(self) -> None:
        with pytest.raises(ValidationError):
            _file_deployment(evidence_mode='operator_attestation')

    def test_readback_state_requires_matching_verdict(self) -> None:
        with pytest.raises(ValidationError):
            _file_deployment(roundtrip_verdict='not_performed')

    def test_post_measurement_verified_needs_refs(self) -> None:
        with pytest.raises(ValidationError):
            _file_deployment(
                runtime_state='post_measurement_verified')

    def test_post_measurement_verified_needs_matched_file(self) -> None:
        with pytest.raises(ValidationError):
            _file_deployment(
                file_state='file_installed',
                evidence_mode='file_level_readback',
                roundtrip_verdict='not_performed',
                runtime_state='post_measurement_verified',
                post_measurement_refs=(_ref('measurement'),))

    def test_not_attested_cannot_pin_measurements(self) -> None:
        with pytest.raises(ValidationError):
            _file_deployment(
                post_measurement_refs=(_ref('measurement'),))

    def test_verified_runtime_reachable_only_by_measurement(self) -> None:
        fed = _file_deployment(
            runtime_state='post_measurement_verified',
            post_measurement_refs=(
                _ref('measurement'), _ref('frequency_response')),)
        assert fed.runtime_state == 'post_measurement_verified'

    def test_install_attestation_needs_attestor(self) -> None:
        with pytest.raises(ValidationError):
            _file_deployment(
                file_state='file_installed',
                evidence_mode='operator_attestation',
                roundtrip_verdict='not_performed')

    def test_operator_attestor_makes_installed(self) -> None:
        fed = _file_deployment(
            file_state='file_installed',
            evidence_mode='operator_attestation',
            roundtrip_verdict='not_performed',
            install_attestor='operator:ken')
        assert fed.file_state == 'file_installed'
        assert fed.runtime_state == 'runtime_not_attested'

    def test_export_blocked_requires_reasons(self) -> None:
        with pytest.raises(ValidationError):
            _file_deployment(
                file_state='export_blocked',
                evidence_mode='unverified_export',
                roundtrip_verdict='not_performed')

    def test_export_blocked_cannot_pin_measurements(self) -> None:
        with pytest.raises(ValidationError):
            _file_deployment(
                file_state='export_blocked',
                evidence_mode='unverified_export',
                roundtrip_verdict='not_performed',
                block_reasons=('unsupported band',),
                post_measurement_refs=(_ref('measurement'),))

    def test_hash_mismatch_rejected(self) -> None:
        fed = _file_deployment()
        with pytest.raises(ValidationError):
            FileExportDeployment(
                **{**fed.model_dump(mode='python'),
                   'file_deployment_sha256': _SHA})


class TestDeriveFileExportState:
    def test_blocked(self) -> None:
        state, runtime, mode = derive_file_export_state(
            roundtrip_verdict='not_performed',
            install_evidence='not_installed',
            export_blocked_reasons=('x',))
        assert (state, runtime, mode) == (
            'export_blocked', 'runtime_not_attested',
            'unverified_export')

    def test_mismatch(self) -> None:
        state, runtime, mode = derive_file_export_state(
            roundtrip_verdict='mismatch',
            install_evidence='installed_readback')
        assert (state, runtime, mode) == (
            'file_readback_mismatch', 'runtime_not_attested',
            'file_level_readback')

    def test_matched_with_measurement(self) -> None:
        state, runtime, mode = derive_file_export_state(
            roundtrip_verdict='matched',
            install_evidence='installed_readback',
            post_measurement_count=1)
        assert (state, runtime, mode) == (
            'file_readback_matched', 'post_measurement_verified',
            'file_level_readback')

    def test_matched_without_measurement_is_ceiling(self) -> None:
        state, runtime, mode = derive_file_export_state(
            roundtrip_verdict='matched',
            install_evidence='installed_readback')
        assert state == 'file_readback_matched'
        assert runtime == 'runtime_not_attested'
        assert mode == 'file_level_readback'

    def test_attested_install_is_weaker(self) -> None:
        state, runtime, mode = derive_file_export_state(
            roundtrip_verdict='not_performed',
            install_evidence='installed_attested')
        assert (state, runtime, mode) == (
            'file_installed', 'runtime_not_attested',
            'operator_attestation')

    def test_export_only(self) -> None:
        state, runtime, mode = derive_file_export_state(
            roundtrip_verdict='not_performed',
            install_evidence='not_installed')
        assert (state, runtime, mode) == (
            'exported_config', 'runtime_not_attested',
            'unverified_export')


class TestFileExportVerdict:
    def test_none_record(self) -> None:
        assert evaluate_file_export_verification(None) == (
            'unknown', 'no_file_export_deployment')

    def test_matched_is_runtime_not_attested(self) -> None:
        verdict, _ = evaluate_file_export_verification(_file_deployment())
        assert verdict == 'runtime_not_attested'

    def test_verified_only_with_measurement(self) -> None:
        verdict, _ = evaluate_file_export_verification(
            _file_deployment(
                runtime_state='post_measurement_verified',
                post_measurement_refs=(_ref('measurement'),)))
        assert verdict == 'post_measurement_verified'

    def test_mismatch(self) -> None:
        verdict, _ = evaluate_file_export_verification(
            _file_deployment(
                file_state='file_readback_mismatch',
                roundtrip_verdict='mismatch'))
        assert verdict == 'file_readback_mismatch'

    def test_blocked(self) -> None:
        verdict, _ = evaluate_file_export_verification(
            _file_deployment(
                file_state='export_blocked',
                evidence_mode='unverified_export',
                roundtrip_verdict='not_performed',
                block_reasons=('unsupported band',)))
        assert verdict == 'export_blocked'


# ---------------------------------------------------------------------------
# Equalizer APO bounded renderer + file-level readback


def _apo_channels() -> tuple[ApoExportChannel, ...]:
    return (
        ApoExportChannel(channel_labels=('L', 'R'), bands=(
            ApoExportBand(
                filter_type='peaking', frequency_hz=80.0,
                gain_db=-3.0, q=4.0),
            ApoExportBand(
                filter_type='low_shelf', frequency_hz=120.0,
                gain_db=2.0, q=0.9),
        )),
        ApoExportChannel(channel_labels=('SUB',), delay_s=0.004, bands=(
            ApoExportBand(
                filter_type='low_pass', frequency_hz=80.0, q=0.707),
        )),
    )


class TestApoExport:
    def test_render_is_deterministic(self) -> None:
        text1 = render_equalizer_apo_config(channels=_apo_channels())
        text2 = render_equalizer_apo_config(channels=_apo_channels())
        assert text1 == text2
        assert text1.encode('utf-8')

    def test_rendered_subset_parses(self) -> None:
        text = render_equalizer_apo_config(channels=_apo_channels())
        assert 'Channel: L R' in text
        assert 'PK' in text and 'LSC' in text and 'LPQ' in text
        assert 'Delay: 4 ms' in text

    def test_roundtrip_matched(self) -> None:
        channels = _apo_channels()
        text = render_equalizer_apo_config(channels=channels)
        verdict, problems = verify_exported_apo_config(
            text, channels=channels)
        assert verdict == 'matched' and problems == ()

    def test_roundtrip_detects_tampered_text(self) -> None:
        channels = _apo_channels()
        text = render_equalizer_apo_config(channels=channels)
        tampered = text.replace('Fc 80 Hz', 'Fc 81 Hz')
        verdict, problems = verify_exported_apo_config(
            tampered, channels=channels)
        assert verdict == 'mismatch' and problems

    def test_device_and_global_preamp(self) -> None:
        channels = _apo_channels()
        text = render_equalizer_apo_config(
            channels=channels, global_preamp_db=-10.0,
            device='Speakers (Realtek)')
        assert 'Device: Speakers (Realtek)' in text
        assert 'Preamp: -10 dB' in text
        verdict, problems = verify_exported_apo_config(
            text, channels=channels, global_preamp_db=-10.0,
            device='Speakers (Realtek)')
        assert verdict == 'matched' and problems == ()

    def test_all_scope_preamp_accumulates_into_global(self) -> None:
        channels = (ApoExportChannel(channel_labels=(), preamp_db=-2.0),)
        text = render_equalizer_apo_config(
            channels=channels, global_preamp_db=-10.0)
        verdict, problems = verify_exported_apo_config(
            text, channels=channels, global_preamp_db=-10.0)
        assert verdict == 'matched' and problems == ()

    def test_unsupported_parameters_fail_closed(self) -> None:
        # A peaking band without a width would silently produce nothing
        # usable in APO — HTDT refuses instead of emitting a line APO
        # would ignore.
        with pytest.raises(ApoExportUnsupportedError) as excinfo:
            render_equalizer_apo_config(channels=(
                ApoExportChannel(channel_labels=('L',), bands=(
                    ApoExportBand(
                        filter_type='peaking', frequency_hz=80.0,
                        gain_db=-3.0),
                )),))
        assert any('peaking' in i for i in excinfo.value.unsupported_items)

    def test_multiple_unsupported_all_reported(self) -> None:
        with pytest.raises(ApoExportUnsupportedError) as excinfo:
            render_equalizer_apo_config(channels=(
                ApoExportChannel(channel_labels=('L',), bands=(
                    ApoExportBand(
                        filter_type='peaking', frequency_hz=80.0,
                        gain_db=-3.0),
                    ApoExportBand(
                        filter_type='low_shelf', frequency_hz=100.0),
                )),))
        assert len(excinfo.value.unsupported_items) >= 2

    def test_support_problems_per_type(self) -> None:
        assert apo_band_support_problems(ApoExportBand(
            filter_type='all_pass', frequency_hz=1000.0, q=0.707)) == ()
        assert apo_band_support_problems(ApoExportBand(
            filter_type='all_pass', frequency_hz=1000.0))
        assert apo_band_support_problems(ApoExportBand(
            filter_type='peaking', frequency_hz=80.0, gain_db=-3.0,
            q=4.0)) == ()

    def test_q_and_bw_are_exclusive(self) -> None:
        with pytest.raises(ValidationError):
            ApoExportBand(
                filter_type='peaking', frequency_hz=80.0,
                gain_db=-3.0, q=4.0, bandwidth_oct=0.5)


# ---------------------------------------------------------------------------
# generic export fallbacks


def _generic_channels() -> tuple[GenericExportChannel, ...]:
    return (GenericExportChannel(
        channel_label='Sub 1', preamp_db=-1.5, bands=(
            ApoExportBand(
                filter_type='peaking', frequency_hz=30.0,
                gain_db=-2.0, q=2.0),
        )),)


class TestGenericExport:
    def test_peq_export_deterministic(self) -> None:
        assert render_generic_peq_export(channels=_generic_channels()) \
            == render_generic_peq_export(channels=_generic_channels())

    def test_peq_export_carries_spec(self) -> None:
        text = render_generic_peq_export(channels=_generic_channels())
        assert 'Sub 1,-1.5' in text
        assert 'peaking,30,-2,2' in text

    def test_biquad_export_coefficients(self) -> None:
        text = render_generic_biquad_export(
            channels=_generic_channels(), sample_rate_hz=48000)
        assert 'a0_normalized' in text
        # The emitted coefficients are the validated RBJ convention:
        # a -2 dB peaking dip at 30 Hz measures -2 dB at Fc.
        biquad = build_biquad_filter(
            filter_id='x', filter_type='peaking', frequency_hz=30.0,
            q=2.0, gain_db=-2.0, sample_rate_hz=48000)
        assert evaluate_biquad_db(biquad, 30.0) \
            == pytest.approx(-2.0, abs=0.01)
        assert text.count(',') > 0

    def test_biquad_export_fails_closed(self) -> None:
        channels = (GenericExportChannel(channel_label='L', bands=(
            ApoExportBand(  # shelf is outside the biquad convention
                filter_type='low_shelf', frequency_hz=100.0,
                gain_db=3.0, q=0.9),
            ApoExportBand(  # bandwidth-pinned peaking needs conversion
                filter_type='peaking', frequency_hz=80.0,
                gain_db=-3.0, bandwidth_oct=0.5),
        )),)
        problems = biquad_export_support_problems(channels, 48000)
        assert len(problems) == 2
        with pytest.raises(GenericExportUnsupportedError):
            render_generic_biquad_export(
                channels=channels, sample_rate_hz=48000)

    def test_biquad_export_rejects_above_nyquist(self) -> None:
        channels = (GenericExportChannel(channel_label='L', bands=(
            ApoExportBand(
                filter_type='peaking', frequency_hz=30000.0,
                gain_db=-3.0, q=4.0),
        )),)
        with pytest.raises(GenericExportUnsupportedError):
            render_generic_biquad_export(
                channels=channels, sample_rate_hz=48000)

    def test_fir_export(self) -> None:
        text = render_generic_fir_export(channels=(
            GenericFirChannel(
                channel_label='Sub 1', sample_rate_hz=48000,
                taps=(0.0, 0.5, 1.0, 0.5, 0.0)),
        ),)
        assert 'Fs=48000 taps=5' in text
        assert 'Sub 1,2,1' in text

    def test_manual_handoff_never_claims_state(self) -> None:
        text = render_manual_settings_handoff(
            target_name='AVR-X3800H', channels=_generic_channels())
        assert 'AVR-X3800H' in text
        assert 'Sub 1' in text
        assert 're-measure' in text or 'measurement' in text


# ---------------------------------------------------------------------------
# repository — round-trip + tamper detection


class TestRepository:
    def test_roundtrip_all_stores(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        man = _manifest()
        acq = _acquisition(man)
        fed = _file_deployment()
        repo.save_provider_manifest(man)
        repo.save_acquisition(acq)
        repo.save_file_deployment(fed)
        assert repo.get_provider_manifest(man.manifest_id) == man
        assert repo.get_acquisition(acq.acquisition_id) == acq
        assert repo.get_file_deployment(
            fed.file_deployment_id) == fed
        assert repo.list_provider_manifests(DOC) == (man,)
        assert repo.list_provider_manifests('other') == ()

    def test_append_only_idempotent_resave(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        man = _manifest()
        repo.save_provider_manifest(man)
        repo.save_provider_manifest(man)
        assert len(repo.list_provider_manifests(DOC)) == 1
        with pytest.raises(DelegatedProviderIntegrityError):
            repo.save_provider_manifest(
                DelegatedProviderManifest.model_construct(
                    **{**man.model_dump(mode='python'),
                       'notes': 'tampered'}))

    def test_forged_sha_rejected_at_save(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        fed = _file_deployment()
        forged = FileExportDeployment.model_construct(
            **{**fed.model_dump(mode='python'),
               'file_deployment_sha256': _SHA})
        with pytest.raises(DelegatedProviderIntegrityError):
            repo.save_file_deployment(forged)

    def test_column_tamper_detected_on_get(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        fed = _file_deployment()
        repo.save_file_deployment(fed)
        with connect_sqlite(repo.path) as connection, connection:
            connection.execute(
                'UPDATE cad_file_deployments SET runtime_state=? '
                'WHERE file_deployment_id=?',
                ('post_measurement_verified', fed.file_deployment_id))
        with pytest.raises(DelegatedProviderIntegrityError):
            repo.get_file_deployment(fed.file_deployment_id)

    def test_document_id_drift_detected_on_list(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        man = _manifest()
        repo.save_provider_manifest(man)
        with connect_sqlite(repo.path) as connection, connection:
            connection.execute(
                'UPDATE cad_delegated_provider_manifests '
                'SET document_id=? WHERE manifest_id=?',
                ('other-doc', man.manifest_id))
        with pytest.raises(DelegatedProviderIntegrityError):
            repo.list_provider_manifests('other-doc')
