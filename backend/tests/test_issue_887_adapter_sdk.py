"""Tests for issue #887 — provider/device adapter SDK + conformance suite.

Covers the sealed ``AdapterSdkContract`` descriptor (validators, seal,
id integrity), the executable conformance suite (every verdict path —
fail-closed especially, suite-version and descriptor-sha staleness),
the ``assert_conforming`` gate (every typed-error kind), the
``cad_adapter_sdk_descriptors`` / ``cad_adapter_conformance_results``
append-only authorities (round-trip, tamper detection, audit coverage),
and the honest outcomes the suite records for every in-repo adapter.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_adapter_conformance import (  # noqa: E402
    ADAPTER_CONFORMANCE_SUITE_VERSION,
    CONFORMANCE_SCENARIO_NAMES,
    AvrLanConformanceSubject,
    CamillaDSPConformanceSubject,
    ConformanceResultRecord,
    ConformanceSuiteRunner,
    DiscoveryConformanceSubject,
    EqualizerApoConformanceSubject,
    FileAdapterConformanceSubject,
    MiniDSPConformanceSubject,
    ProviderGateConformanceSubject,
    SubjectResult,
    ConformanceProbe,
    required_scenarios_for,
    run_in_repo_conformance,
)
from htdt.cad_adapter_sdk import (  # noqa: E402
    ADAPTER_SDK_CONTRACT_VERSION,
    AdapterConformanceError,
    AdapterSafetyInvariants,
    AdapterSdkContract,
    assert_conforming,
    build_adapter_descriptor,
    conformance_is_stale,
    descriptor_ref,
)
from htdt.cad_adapter_sdk_repository import (  # noqa: E402
    CadAdapterSdkRepository,
    DeploymentIntegrityError,
)
from htdt.cad_calibration import (  # noqa: E402
    CadCalibrationChannel,
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    build_generic_biquad_export,
)
import types  # noqa: E402

from htdt.cad_delegated_provider import (  # noqa: E402
    DelegatedProviderManifest,
    build_rew_provider_manifest,
)
from htdt.cad_device_adapter import (  # noqa: E402
    AdapterCapabilityReport,
)
from htdt.cad_device_discovery import DiscoveryObservation  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_schema import connect_sqlite, ensure_native_schema  # noqa: E402
from htdt.canonical_json import canonical_sha256  # noqa: E402
from htdt.native_authority_audit import audit_table_modes  # noqa: E402
from htdt.native_row_integrity import (  # noqa: E402
    assert_row_integrity_registry_complete,
    scan_native_row_integrity,
)

NOW = '2026-10-08T00:00:00+00:00'
DOC = 'doc-887'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _channel(channel_id: str, gain_db: float) -> CadCalibrationChannel:
    return CadCalibrationChannel(
        channel_id=channel_id,
        role_id=channel_id.upper(),
        source_entity_id=f'spk-{channel_id}',
        physical_output_id=f'out-{channel_id}',
        sample_rate_hz=48000,
        gain_db=gain_db,
        delay_s=0.0,
        polarity='normal',
        crossovers=(),
        peq=(),
        routing=(f'out-{channel_id}',),
    )


def _plan(
    channels: tuple[CadCalibrationChannel, ...] = (
        _channel('fl', 1.5), _channel('fr', 1.0),
    ),
) -> CadCalibrationPlan:
    payload = {
        'plan_id': 'plan-887',
        'plan_version': '1',
        'created_at_utc': NOW,
        'source_kind': 'provided_fixture',
        'document_id': DOC,
        'scene_revision_id': 'rev-1',
        'scene_content_hash': 'a' * 64,
        'system_variant_id': 'var-1',
        'system_variant_sha256': 'b' * 64,
        'source_measurement_id': 'm-1',
        'source_measurement_sha256': 'c' * 64,
        'source_dataset_id': 'd-1',
        'source_dataset_sha256': 'e' * 64,
        'measurement_quality_report_id': 'q-1',
        'measurement_quality_report_sha256': 'f' * 64,
        'sample_rate_hz': 48000,
        'channels': channels,
        'target_curve': None,
        'max_boost_db': 6.0,
        'max_cut_db': 10.0,
        'device_constraints': CadDeviceCapabilityConstraints(
            capability_id='test-device-1',
            capability_version='1',
            supported_sample_rates_hz=(48000,),
            supported_filter_types=('peaking',),
            channel_gain_resolution_db=0.5,
        ),
        'support_state': 'SUPPORTED',
        'unsupported_reasons': (),
        'plan_semantic_sha256': '0' * 64,
    }
    provisional = CadCalibrationPlan.model_construct(**payload)
    return CadCalibrationPlan(
        **{
            **payload,
            'plan_semantic_sha256': hashlib.sha256(
                json.dumps(
                    provisional.semantic_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ).encode('utf-8')
            ).hexdigest(),
        }
    )


def _export(
    channels: tuple[CadCalibrationChannel, ...] = (
        _channel('fl', 1.5), _channel('fr', 1.0),
    ),
):
    return build_generic_biquad_export(
        plan=_plan(channels), created_at_utc=NOW)


def _capability(**overrides) -> AdapterCapabilityReport:
    fields = {
        'adapter_id': 'stub-adapter',
        'adapter_version': '1.0.0',
        'adapter_kind': 'network_api',
        'device_family': 'stub-family',
        'supports_apply': True,
        'supports_read_back': True,
        'deploy_mechanism': 'machine_write',
        'readback_mechanism': 'machine_exact',
        'rollback_mechanism': 'previous_config',
        'runtime_observation': 'telemetry',
        'supported_features': ('peq', 'gain'),
        'auth_requirements': ('operator_confirmed_apply',),
        'protocol_authority': 'documented',
    }
    fields.update(overrides)
    return AdapterCapabilityReport(**fields)


def _descriptor(**overrides) -> AdapterSdkContract:
    kwargs = {
        'document_id': DOC,
        'capability': _capability(),
        'supported_operations': (
            'capability', 'materialize', 'apply', 'read_back',
            'capture_baseline', 'rollback',
        ),
        'safety_invariants': AdapterSafetyInvariants(
            credential_boundary='vendor-tls-handle',
        ),
        'contract_status': 'production',
        'declared_at_utc': NOW,
    }
    kwargs.update(overrides)
    return build_adapter_descriptor(**kwargs)


class _StubSubject:
    """Deterministic subject driven by a name→SubjectResult table."""

    def __init__(
        self,
        descriptor: AdapterSdkContract,
        lanes: frozenset[str],
        *,
        provenance='live',
        results: dict[str, SubjectResult] | None = None,
        default: SubjectResult | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._lanes = lanes
        self._provenance = provenance
        self._results = results or {}
        self._default = default or SubjectResult(
            ok=True, transcript=('stub ok',),
            probes=(ConformanceProbe(
                check='stub check', expected='ok', observed='ok',
                ok=True,
            ),),
        )

    @property
    def descriptor(self) -> AdapterSdkContract:
        return self._descriptor

    @property
    def lanes(self):
        return self._lanes

    @property
    def evidence_provenance(self):
        return self._provenance

    def run(self, name: str) -> SubjectResult:
        return self._results.get(name, self._default)


def _run(subject, suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION):
    return ConformanceSuiteRunner(suite_version=suite_version).run(
        subject, issued_at_utc=NOW,
    )


def _repo(tmp_path: Path) -> CadAdapterSdkRepository:
    return CadAdapterSdkRepository(
        SceneRepository(tmp_path / 'adapter-sdk.sqlite3'))


_FULL_LANES = frozenset({
    'capability', 'materialize', 'apply', 'read_back',
    'capture_baseline', 'rollback', 'observe_runtime',
})


# ---------------------------------------------------------------------------
# Descriptor contract: mint, seal, validators
# ---------------------------------------------------------------------------

def test_descriptor_mints_sealed_record() -> None:
    descriptor = _descriptor()
    assert descriptor.descriptor_id.startswith('asd-')
    assert len(descriptor.descriptor_sha256) == 64
    assert descriptor.sdk_version == ADAPTER_SDK_CONTRACT_VERSION
    assert descriptor.capability_sha256 == canonical_sha256(
        descriptor.capability_report.model_dump(mode='json'))
    # Deterministic: same payload → same sha/id.
    assert _descriptor().descriptor_sha256 == descriptor.descriptor_sha256
    assert _descriptor().descriptor_id == descriptor.descriptor_id


def test_descriptor_ref_pins_contract_sha() -> None:
    descriptor = _descriptor()
    ref = descriptor_ref(descriptor)
    assert ref.kind == 'adapter_sdk_contract'
    assert ref.ref_id == descriptor.descriptor_id
    assert ref.ref_sha256 == descriptor.descriptor_sha256


def test_descriptor_rejects_tampered_sha() -> None:
    descriptor = _descriptor()
    payload = descriptor.model_dump(mode='python')
    payload['notes'] = ('forged note',)
    with pytest.raises(Exception, match='hash mismatch'):
        AdapterSdkContract(**payload)


def test_descriptor_requires_capability_operation() -> None:
    with pytest.raises(ValueError):
        _descriptor(supported_operations=('apply', 'read_back'))


def test_descriptor_rejects_duplicate_operations() -> None:
    with pytest.raises(ValueError):
        _descriptor(
            supported_operations=(
                'capability', 'apply', 'apply'))


def test_descriptor_rejects_capability_field_mismatch() -> None:
    descriptor = _descriptor()
    payload = descriptor.model_dump(mode='python')
    payload['adapter_id'] = 'other-adapter'
    with pytest.raises(ValueError, match='identity fields'):
        AdapterSdkContract(**payload)


def test_descriptor_rejects_bogus_capability_sha() -> None:
    descriptor = _descriptor()
    bad = descriptor.model_dump(mode='python')
    bad['capability_sha256'] = '0' * 64
    with pytest.raises(Exception):
        AdapterSdkContract(**bad)


def test_simulated_status_requires_simulated_authority() -> None:
    with pytest.raises(ValueError):
        _descriptor(
            contract_status='simulated',
            capability=_capability(protocol_authority='documented'),
        )
    # A simulated adapter kind carries it honestly.
    descriptor = _descriptor(
        contract_status='simulated',
        capability=_capability(
            adapter_kind='simulated',
            protocol_authority='simulated',
        ),
    )
    assert descriptor.contract_status == 'simulated'


def test_safety_invariants_reject_unknown_forbidden_kind() -> None:
    with pytest.raises(Exception):
        AdapterSafetyInvariants(
            credential_boundary='vault',
            forbidden_operations=('totally_fine_thing',),
        )


def test_safety_invariants_default_mutation_gate() -> None:
    invariants = AdapterSafetyInvariants(credential_boundary='vault')
    assert invariants.mutation_requires_one_shot_authorization is True


# ---------------------------------------------------------------------------
# Runner: every verdict path
# ---------------------------------------------------------------------------

def test_conforming_verdict_for_clean_live_subject() -> None:
    result = _run(_StubSubject(_descriptor(), _FULL_LANES))
    assert result.verdict == 'conforming'
    assert result.result_id.startswith('acr-')
    assert result.required_failures == ()
    assert result.limitations == ()
    assert {s.scenario for s in result.scenario_results} == set(
        CONFORMANCE_SCENARIO_NAMES)


def test_optional_unverifiable_lands_limitation() -> None:
    subject = _StubSubject(
        _descriptor(), _FULL_LANES,
        results={'cancellation': SubjectResult(
            ok=False, verifiable=False,
            detail='no cancellation lane on this transport')},
    )
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert 'cancellation' in result.limitations


def test_required_failure_is_non_conforming() -> None:
    """A deliberately broken adapter fails its required happy path."""
    subject = _StubSubject(
        _descriptor(), _FULL_LANES,
        results={
            'happy_path': SubjectResult(
                ok=False,
                detail='apply acked but readback diverged'),
            'readback_mismatch': SubjectResult(
                ok=True, detail='mismatch still detected'),
        },
    )
    result = _run(subject)
    assert result.verdict == 'non_conforming'
    assert result.required_failures == ('happy_path',)


def test_required_unverifiable_is_unverifiable_verdict() -> None:
    subject = _StubSubject(
        _descriptor(), _FULL_LANES,
        results={'happy_path': SubjectResult(
            ok=False, verifiable=False,
            detail='cannot reach the transport at all')},
    )
    result = _run(subject)
    assert result.verdict == 'unverifiable'


def test_non_live_provenance_is_always_limited() -> None:
    subject = _StubSubject(
        _descriptor(), _FULL_LANES, provenance='simulated')
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert 'evidence_provenance=simulated' in result.limitations


def test_non_production_contract_status_is_limited() -> None:
    descriptor = _descriptor(contract_status='assisted_only')
    subject = _StubSubject(descriptor, frozenset({'file_export'}))
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert 'contract_status=assisted_only' in result.limitations


def test_scenario_required_marks_follow_lanes() -> None:
    lanes = frozenset({'apply', 'read_back', 'rollback'})
    result = _run(_StubSubject(_descriptor(), lanes))
    by_name = {s.scenario: s for s in result.scenario_results}
    assert by_name['authentication_failure'].required
    assert by_name['readback_mismatch'].required
    assert by_name['rollback_success'].required
    assert not by_name['timeout'].required
    assert not by_name['firmware_drift'].required


def test_required_scenarios_for_lane_matrix() -> None:
    assert required_scenarios_for(frozenset()) == frozenset(
        {'happy_path', 'unsupported_feature'})
    assert 'authentication_failure' in required_scenarios_for(
        frozenset({'apply'}))
    assert 'readback_mismatch' in required_scenarios_for(
        frozenset({'file_verify'}))
    assert 'rollback_success' in required_scenarios_for(
        frozenset({'rollback'}))


def test_result_covers_every_scenario_exactly_once() -> None:
    result = _run(_StubSubject(_descriptor(), _FULL_LANES))
    names = [s.scenario for s in result.scenario_results]
    assert sorted(names) == sorted(CONFORMANCE_SCENARIO_NAMES)


def test_scenario_result_rejects_tampered_transcript() -> None:
    result = _run(_StubSubject(_descriptor(), _FULL_LANES))
    scenario = result.scenario_results[0]
    payload = scenario.model_dump(mode='python')
    payload['transcript'] = ('forged line',)
    with pytest.raises(Exception, match='transcript_sha256 mismatch'):
        type(scenario)(**payload)


def test_result_rejects_tampered_sha() -> None:
    result = _run(_StubSubject(_descriptor(), _FULL_LANES))
    payload = result.model_dump(mode='python')
    payload['verdict'] = 'unverifiable'
    with pytest.raises(Exception, match='hash mismatch'):
        ConformanceResultRecord(**payload)


def test_result_rejects_unknown_scenario_coverage() -> None:
    result = _run(_StubSubject(_descriptor(), _FULL_LANES))
    payload = result.model_dump(mode='python')
    payload['scenario_results'] = [
        s for s in payload['scenario_results']
        if s['scenario'] != 'happy_path'
    ]
    with pytest.raises(Exception, match='every named scenario'):
        ConformanceResultRecord(**payload)


# ---------------------------------------------------------------------------
# Staleness + the fail-closed gate
# ---------------------------------------------------------------------------

def test_stale_on_descriptor_drift() -> None:
    descriptor = _descriptor()
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    drifted = _descriptor(
        capability=_capability(adapter_version='1.0.1'))
    assert descriptor.descriptor_sha256 != drifted.descriptor_sha256
    assert conformance_is_stale(
        result, drifted,
        suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)


def test_stale_on_suite_bump() -> None:
    descriptor = _descriptor()
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    assert conformance_is_stale(
        result, descriptor, suite_version='conformance-suite-2')
    assert not conformance_is_stale(
        result, descriptor,
        suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)


def test_stale_on_missing_ref() -> None:
    descriptor = _descriptor()
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    forged = type('Forged', (), {
        'descriptor_ref': None,
        'suite_version': ADAPTER_CONFORMANCE_SUITE_VERSION,
    })()
    assert conformance_is_stale(
        forged, descriptor,
        suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)


def test_gate_passes_clean_conformance() -> None:
    descriptor = _descriptor()
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    assert_conforming(
        descriptor, result,
        suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)


def test_gate_rejects_no_evidence() -> None:
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            _descriptor(), None,
            suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)
    assert exc.value.kind == 'unproven'


def test_gate_rejects_insufficient_version() -> None:
    descriptor = _descriptor(sdk_version='adapter-sdk-0')
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            descriptor, result,
            suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)
    assert exc.value.kind == 'insufficient_version'


def test_gate_rejects_stale_descriptor() -> None:
    result = _run(_StubSubject(_descriptor(), _FULL_LANES))
    drifted = _descriptor(
        capability=_capability(adapter_version='1.0.1'))
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            drifted, result,
            suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)
    assert exc.value.kind == 'stale_descriptor'


def test_gate_rejects_stale_suite() -> None:
    descriptor = _descriptor()
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            descriptor, result, suite_version='conformance-suite-99')
    assert exc.value.kind == 'stale_suite'


def test_gate_rejects_non_conforming() -> None:
    descriptor = _descriptor()
    result = _run(_StubSubject(
        descriptor, _FULL_LANES,
        results={'happy_path': SubjectResult(ok=False)}))
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            descriptor, result,
            suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)
    assert exc.value.kind == 'non_conforming'


def test_gate_rejects_unverifiable() -> None:
    descriptor = _descriptor()
    result = _run(_StubSubject(
        descriptor, _FULL_LANES,
        results={'happy_path': SubjectResult(ok=False, verifiable=False)}))
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            descriptor, result,
            suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)
    assert exc.value.kind == 'unverifiable'


def test_gate_limited_only_when_disallowed() -> None:
    descriptor = _descriptor()
    subject = _StubSubject(
        descriptor, _FULL_LANES, provenance='simulated')
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    # Default: limitations are acceptable for advisory use.
    assert_conforming(
        descriptor, result,
        suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            descriptor, result,
            suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION,
            allow_limitations=False)
    assert exc.value.kind == 'limited'


def test_gate_rejects_unknown_verdict() -> None:
    descriptor = _descriptor()
    forged = type('Forged', (), {
        'descriptor_ref': descriptor_ref(descriptor),
        'suite_version': ADAPTER_CONFORMANCE_SUITE_VERSION,
        'verdict': 'mostly_fine',
    })()
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            descriptor, forged,
            suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)
    assert exc.value.kind == 'unverifiable'


# ---------------------------------------------------------------------------
# Repository: round-trip, append-only, tamper detection, audit wiring
# ---------------------------------------------------------------------------

def test_repository_descriptor_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    descriptor = _descriptor()
    repo.save_descriptor(descriptor)
    assert repo.get_descriptor(descriptor.descriptor_id) == descriptor
    assert repo.list_descriptors(DOC) == (descriptor,)
    assert repo.list_descriptors('other-doc') == ()


def test_repository_result_roundtrip_and_latest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    descriptor = _descriptor()
    repo.save_descriptor(descriptor)
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    repo.save_result(result)
    assert repo.get_result(result.result_id) == result
    assert repo.latest_result_for(
        descriptor.descriptor_sha256) == result
    assert repo.latest_result_for('0' * 64) is None


def test_repository_append_only_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    descriptor = _descriptor()
    repo.save_descriptor(descriptor)
    repo.save_descriptor(descriptor)  # identical payload is a no-op
    assert repo.list_descriptors(DOC) == (descriptor,)


def test_repository_rejects_unsealed_record(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    descriptor = _descriptor()
    forged = AdapterSdkContract.model_construct(
        **{**descriptor.model_dump(mode='python'),
           'adapter_version': '9.9.9'})
    with pytest.raises(DeploymentIntegrityError):
        repo.descriptors.save(forged)


def test_repository_tamper_detection(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    descriptor = _descriptor()
    repo.save_descriptor(descriptor)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            "UPDATE cad_adapter_sdk_descriptors "
            "SET adapter_version='9.9.9' WHERE descriptor_id=?",
            (descriptor.descriptor_id,))
        connection.commit()
    with pytest.raises(DeploymentIntegrityError):
        repo.get_descriptor(descriptor.descriptor_id)


def test_repository_result_column_tamper_detected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    descriptor = _descriptor()
    repo.save_descriptor(descriptor)
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    repo.save_result(result)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            "UPDATE cad_adapter_conformance_results "
            "SET verdict='non_conforming' WHERE result_id=?",
            (result.result_id,))
        connection.commit()
    with pytest.raises(DeploymentIntegrityError):
        repo.get_result(result.result_id)


def test_audit_coverage_is_replay_canonical(tmp_path: Path) -> None:
    modes = audit_table_modes()
    assert modes['cad_adapter_sdk_descriptors'] == 'replay_canonical'
    assert modes['cad_adapter_conformance_results'] == 'replay_canonical'
    assert_row_integrity_registry_complete()


def test_row_integrity_scans_new_tables(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    descriptor = _descriptor()
    repo.save_descriptor(descriptor)
    result = _run(_StubSubject(descriptor, _FULL_LANES))
    repo.save_result(result)
    with connect_sqlite(repo.path) as connection:
        drifts = scan_native_row_integrity(connection)
    assert drifts == ()


def test_fresh_database_has_tables(tmp_path: Path) -> None:
    path = tmp_path / 'fresh.sqlite3'
    ensure_native_schema(path)
    with connect_sqlite(path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert 'cad_adapter_sdk_descriptors' in names
    assert 'cad_adapter_conformance_results' in names


# ---------------------------------------------------------------------------
# In-repo adapter subjects — honest outcomes
# ---------------------------------------------------------------------------

def test_avr_subject_conformes_with_simulated_limitations() -> None:
    subject = AvrLanConformanceSubject(_export())
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert 'evidence_provenance=simulated' in result.limitations
    assert 'contract_status=simulated' in result.limitations
    assert result.required_failures == ()
    # Required scenarios genuinely verified: apply auth refusal,
    # readback divergence detection, rollback under dead transport.
    by_name = {s.scenario: s for s in result.scenario_results}
    assert by_name['authentication_failure'].status == 'passed'
    assert by_name['readback_mismatch'].status == 'passed'
    assert by_name['rollback_success'].status == 'passed'


def test_camilladsp_subject_production_contract_simulated_evidence() -> None:
    subject = CamillaDSPConformanceSubject(_export())
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert subject.descriptor.contract_status == 'production'
    assert 'evidence_provenance=simulated' in result.limitations
    assert result.required_failures == ()


def test_minidsp_subject_stays_assisted_only() -> None:
    subject = MiniDSPConformanceSubject(_export())
    result = _run(subject)
    assert subject.descriptor.contract_status == 'assisted_only'
    assert result.verdict == 'conforming_with_limitations'
    assert 'contract_status=assisted_only' in result.limitations
    # The biquad-file lane itself is verified; live deploy is honestly
    # recorded as a limitation, never faked.
    by_name = {s.scenario: s for s in result.scenario_results}
    assert by_name['happy_path'].status == 'passed'


def test_file_adapter_subject_verify_lane(tmp_path: Path) -> None:
    subject = FileAdapterConformanceSubject(
        _export(), root=tmp_path / 'file-lane')
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert result.required_failures == ()


def test_apo_installer_subject_file_verified_only(
    tmp_path: Path,
) -> None:
    subject = EqualizerApoConformanceSubject(
        work_dir=tmp_path / 'apo-lane')
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert subject.descriptor.contract_status == 'assisted_only'


def test_discovery_subject_read_only() -> None:
    subject = DiscoveryConformanceSubject(
        observations={
            'telnet://avr.local:23': DiscoveryObservation(
                endpoint='telnet://avr.local:23',
                manufacturer='Denon',
                model='AVR-X3800H',
                suggested_adapter_id='htdt-avr-lan',
            ),
        },
        approved_endpoints=('telnet://avr.local:23',),
    )
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert subject.descriptor.contract_status == 'read_only'
    assert 'evidence_provenance=read_only' in result.limitations


def _rew_manifest() -> DelegatedProviderManifest:
    session = types.SimpleNamespace(
        engine_version='5.31.3',
        adapter_id='htdt-rew-api',
        adapter_version='2.0.0',
        endpoint='http://localhost:4735',
        observed_at_utc=NOW,
        capability_snapshot={'capabilities': {}},
    )
    return build_rew_provider_manifest(session, document_id=DOC)


def test_provider_gate_subject_read_only() -> None:
    subject = ProviderGateConformanceSubject(manifest=_rew_manifest())
    result = _run(subject)
    assert result.verdict == 'conforming_with_limitations'
    assert result.required_failures == ()


def test_in_repo_suite_sweep_records_honest_verdicts(tmp_path: Path) -> None:
    results = run_in_repo_conformance(
        export=_export(),
        work_dir=tmp_path / 'conformance',
        rew_manifest=_rew_manifest(),
        issued_at_utc=NOW,
    )
    assert set(results) >= {
        'htdt-avr-lan', 'htdt-camilladsp-deploy',
        'htdt-minidsp-biquad-export', 'htdt-file-adapter',
        'equalizer-apo-installer', 'configured-endpoint-scan',
        'htdt-rew-api',
    }
    for adapter_id, result in results.items():
        # No in-repo adapter can claim unconditional protocol
        # conformance from deterministic fakes — the verdict must say so.
        assert result.verdict in (
            'conforming_with_limitations', 'conforming',
        ), adapter_id
        assert result.required_failures == (), adapter_id
        assert result.descriptor_ref.ref_sha256, adapter_id
        assert result.suite_version == (
            ADAPTER_CONFORMANCE_SUITE_VERSION)


def test_deliberately_broken_adapter_fails_and_gates(tmp_path: Path) -> None:
    """An adapter that silently swallows apply failures is unusable."""
    descriptor = _descriptor()
    subject = _StubSubject(
        descriptor, _FULL_LANES,
        results={
            'happy_path': SubjectResult(
                ok=False,
                detail='apply returned success but nothing landed'),
            'readback_mismatch': SubjectResult(
                ok=False,
                detail='mismatch silently passed as matched'),
        })
    result = _run(subject)
    assert result.verdict == 'non_conforming'
    repo = _repo(tmp_path)
    repo.save_descriptor(descriptor)
    repo.save_result(result)
    assert repo.latest_result_for(
        descriptor.descriptor_sha256).verdict == 'non_conforming'
    with pytest.raises(AdapterConformanceError) as exc:
        assert_conforming(
            descriptor, result,
            suite_version=ADAPTER_CONFORMANCE_SUITE_VERSION)
    assert exc.value.kind == 'non_conforming'
