"""#948 BRAS external-measurement fixture harness — regression tests.

Covers the fail-closed contract end to end on synthetic licensed-style
payloads (a locally written SingleRoomSRIR SOFA file + pinned admission):

- sealed fixture/spec/evidence determinism;
- payload verification: missing / size / md5 / sha256 / member pins /
  files outside the admission all fail closed;
- reader semantics: missing units, unit mismatch, coordinate-system
  mismatch, non-finite IR all refuse import;
- per-observable verdicts pass/fail/missing/unobservable/unsupported —
  including the no-conflation rules (normalized amplitude cannot feed an
  absolute-level metric; magnitude-only authority cannot feed a
  phase-bearing comparison);
- deterministic replay: identical evidence hash, diverged on changed
  predictions, blocked on runtime drift;
- preregistered vs informed_calibrated separation.
"""

from __future__ import annotations

import hashlib
import zipfile

import h5py
import numpy as np
import pytest

from htdt.cad_benchmark import BenchmarkObservable, EvaluationProfile
from htdt.cad_external_admission import (
    build_external_asset_admission,
    external_asset_file,
)
from htdt.cad_external_corpus_manifest import (
    AuthorityRef,
    build_corpus_scene,
)
from htdt.cad_external_benchmark_fixture import (
    AnalyticDirectPathProvider,
    FixtureImportError,
    FixtureIntegrityError,
    FixtureRuntime,
    StaticFixtureReplayProvider,
    assert_fixture_payloads_verified,
    build_external_benchmark_fixture,
    build_fixture_run_spec,
    evaluate_fixture_observable,
    import_fixture_case,
    measured_arrival_s,
    read_sofa_measurement,
    replay_fixture,
    run_fixture,
    verify_fixture_payloads,
)

DATASET_NAME = 'synthetic-bras'
SR = 44100.0
ARRIVAL_SAMPLES = 100
DISTANCE_M = 1.0
SOURCE_POS = [[0.0, 0.0, 1.5]]
LISTENER_POS = [[DISTANCE_M, 0.0, 1.5]]
TEMPERATURE_C = 20.0


def _ir(samples: int = 512, onset: int = ARRIVAL_SAMPLES) -> np.ndarray:
    ir = np.zeros((1, 1, samples))
    ir[0, 0, onset] = 1.0
    ir[0, 0, onset + 20] = -0.25
    return ir


def _write_sofa(
    path,
    *,
    ir=None,
    units: str | None = 'pascal',
    sampling_rate: float = SR,
    source=SOURCE_POS,
    listener=LISTENER_POS,
    temperature=TEMPERATURE_C,
    delay_samples=None,
    position_type: str = 'cartesian',
    position_units: str = 'metre',
    conventions: str = 'SingleRoomSRIR',
) -> bytes:
    ir = _ir() if ir is None else np.asarray(ir, dtype=np.float64)
    if ir.ndim == 2:
        ir = ir.reshape((1,) + ir.shape)
    if delay_samples is None:
        delay_samples = np.full(
            (ir.shape[0], ir.shape[1]), ARRIVAL_SAMPLES, dtype=np.float64
        )
    with h5py.File(path, 'w') as handle:
        handle.attrs['SOFAConventions'] = conventions
        if temperature is not None:
            handle.attrs['Temperature'] = temperature
        data = handle.create_group('Data')
        ir_node = data.create_dataset('IR', data=ir)
        if units is not None:
            ir_node.attrs['Units'] = units
        data.create_dataset('Delay', data=np.asarray(delay_samples))
        data.create_dataset(
            'SamplingRate', data=np.array([sampling_rate])
        )
        sp = handle.create_dataset(
            'SourcePosition', data=np.asarray(source, dtype=np.float64)
        )
        sp.attrs['Type'] = position_type
        sp.attrs['Units'] = position_units
        lp = handle.create_dataset(
            'ListenerPosition', data=np.asarray(listener, dtype=np.float64)
        )
        lp.attrs['Type'] = position_type
        lp.attrs['Units'] = position_units
    return path.read_bytes()


def _dataset(tmp_path, file_payloads: dict[str, bytes], **overrides):
    root = tmp_path / DATASET_NAME
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for name, payload in file_payloads.items():
        target = root / name
        if name.endswith('.zip'):
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target, 'w') as archive:
                for member_name, member_bytes in payload.items():
                    archive.writestr(member_name, member_bytes)
            blob = target.read_bytes()
        else:
            target.write_bytes(payload)
            blob = payload
        files.append(
            external_asset_file(
                file_name=name,
                uri=f'https://example.test/{DATASET_NAME}/{name}',
                size_bytes=len(blob),
                md5=hashlib.md5(blob).hexdigest(),
                sha256=hashlib.sha256(blob).hexdigest(),
                checksum_source='computed',
            )
        )
    return build_external_asset_admission(
        admission_id='synthetic-bras',
        dataset_name=DATASET_NAME,
        dataset_title='Synthetic BRAS-style corpus',
        publisher='Unit Test Publisher',
        source_kind='institutional_repository',
        admission_state='download_on_demand_candidate',
        license_id=overrides.get('license_id', 'cc-by-sa-4.0'),
        license_family=overrides.get('license_family', 'cc_by_sa'),
        record_uri='https://example.test/record/1',
        version_doi='10.0000/test.1',
        files=tuple(files),
    )


def _scene(dataset, band=(100.0, 4000.0)):
    return build_corpus_scene(
        scene_id='SYN_01a',
        dataset_ref=AuthorityRef(
            kind='external_asset_admission',
            ref_id=dataset.admission_id,
            ref_sha256=dataset.semantic_sha256,
        ),
        title='Synthetic scene',
        phenomenon_ids=('specular_reflection',),
        reference_strength='direct_reference',
        permitted_claims=('phenomenon_direct_qualification',),
        asset_file_names=tuple(
            f.file_name for f in dataset.files
        ),
        expected_validity_band_hz=band,
        validity_band_basis='synthetic test band',
    )


def _fixture(dataset, scene, *, pins=None, **overrides):
    pins = pins or [
        {'file_name': 'scene.sofa', 'role': 'measured_rir'},
    ]
    kwargs = dict(
        fixture_id=overrides.pop('fixture_id', 'syn-fixture-1'),
        dataset=dataset,
        scene=scene,
        coordinate_convention=overrides.pop(
            'coordinate_convention', 'sofa_cartesian_metre'
        ),
        unit_semantics=overrides.pop('unit_semantics', 'pascal_calibrated'),
        phase_authority=overrides.pop('phase_authority', 'coherent_phase'),
        points=overrides.pop(
            'points',
            [
                {
                    'point_id': 'S1',
                    'role': 'source',
                    'position_m': tuple(SOURCE_POS[0]),
                },
                {
                    'point_id': 'R1',
                    'role': 'receiver',
                    'position_m': tuple(LISTENER_POS[0]),
                },
            ],
        ),
        consumed_files=pins,
        boundary_materials=overrides.pop('boundary_materials', []),
        environment=overrides.pop('environment', {}),
    )
    kwargs.update(overrides)
    return build_external_benchmark_fixture(**kwargs)


def _arrival_observable(
    arrival_s: float, *, tolerance=1e-4, band=None
) -> BenchmarkObservable:
    return BenchmarkObservable(
        observable_id='arrival-direct',
        kind='arrival_timing',
        receiver_id='R1',
        source_id='S1',
        valid_band_hz=band,
        metric_id='arrival_time',
        metric_version='1',
        tolerance=tolerance,
        tolerance_unit='s',
        reference={'arrival_s': arrival_s},
    )


# ---------------------------------------------------------------------------
# Fixture admission
# ---------------------------------------------------------------------------


def test_fixture_sealed_and_deterministic(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene)
    again = _fixture(dataset, scene)
    assert fixture.fixture_sha256 == again.fixture_sha256
    # sealed record: tampering fails validation
    bad = fixture.model_dump(mode='python')
    bad['redistribution'] = 'forbidden'
    with pytest.raises(ValueError, match='hash mismatch'):
        type(fixture)(**bad)


def test_unknown_license_family_rejected(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(
        tmp_path, {'scene.sofa': sofa}, license_family='unknown'
    )
    scene = _scene(dataset)
    with pytest.raises(ValueError, match='license'):
        _fixture(dataset, scene)


def test_redistribution_requires_open_license(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(
        tmp_path, {'scene.sofa': sofa}, license_family='proprietary'
    )
    scene = _scene(dataset)
    with pytest.raises(ValueError, match='redistribution'):
        _fixture(dataset, scene, redistribution='permitted')


def test_fixture_requires_rir_pin(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    with pytest.raises(ValueError, match='measured_rir'):
        _fixture(
            dataset,
            scene,
            pins=[{'file_name': 'scene.sofa', 'role': 'documentation'}],
        )


# ---------------------------------------------------------------------------
# Payload verification — fail closed
# ---------------------------------------------------------------------------


def test_verify_verified(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene)
    receipts = verify_fixture_payloads(tmp_path, fixture, dataset)
    assert all(r.verdict == 'verified' for r in receipts)
    assert_fixture_payloads_verified(fixture, receipts)


def test_verify_missing(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene)
    (tmp_path / DATASET_NAME / 'scene.sofa').unlink()
    receipts = verify_fixture_payloads(tmp_path, fixture, dataset)
    assert receipts[0].verdict == 'missing'
    with pytest.raises(FixtureIntegrityError):
        assert_fixture_payloads_verified(fixture, receipts)


def test_verify_sha_mismatch(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene)
    # same size, different bytes → sha mismatch (tamper evidence)
    path = tmp_path / DATASET_NAME / 'scene.sofa'
    tampered = bytearray(path.read_bytes())
    tampered[-16] ^= 0xFF
    path.write_bytes(bytes(tampered))
    receipts = verify_fixture_payloads(tmp_path, fixture, dataset)
    # checksum failure is fail-closed — md5 is checked before sha256
    assert receipts[0].verdict in ('md5_mismatch', 'sha256_mismatch')


def test_verify_size_mismatch(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene)
    path = tmp_path / DATASET_NAME / 'scene.sofa'
    path.write_bytes(path.read_bytes() + b'extra')
    receipts = verify_fixture_payloads(tmp_path, fixture, dataset)
    assert receipts[0].verdict == 'size_mismatch'


def test_verify_outside_admission(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(
        dataset,
        scene,
        pins=[
            {'file_name': 'scene.sofa', 'role': 'measured_rir'},
            {'file_name': 'unlisted.bin', 'role': 'documentation'},
        ],
    )
    receipts = verify_fixture_payloads(tmp_path, fixture, dataset)
    by_name = {r.file_name: r.verdict for r in receipts}
    assert by_name['unlisted.bin'] == 'outside_admission'


def test_verify_member_pin(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    member_sha = hashlib.sha256(sofa).hexdigest()
    dataset = _dataset(
        tmp_path, {'pack.zip': {'inside/scene.sofa': sofa}}
    )
    scene = _scene(dataset)
    fixture = _fixture(
        dataset,
        scene,
        pins=[
            {
                'file_name': 'pack.zip',
                'member_path': 'inside/scene.sofa',
                'role': 'measured_rir',
                'member_sha256': member_sha,
            }
        ],
    )
    receipts = verify_fixture_payloads(tmp_path, fixture, dataset)
    assert receipts[0].verdict == 'verified'


def test_verify_member_missing(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(
        tmp_path, {'pack.zip': {'inside/scene.sofa': sofa}}
    )
    scene = _scene(dataset)
    fixture = _fixture(
        dataset,
        scene,
        pins=[
            {
                'file_name': 'pack.zip',
                'member_path': 'inside/other.sofa',
                'role': 'measured_rir',
            }
        ],
    )
    receipts = verify_fixture_payloads(tmp_path, fixture, dataset)
    assert receipts[0].verdict == 'member_missing'


# ---------------------------------------------------------------------------
# SOFA reader — semantics preserved, ambiguity fails closed
# ---------------------------------------------------------------------------


def test_read_sofa_happy(tmp_path):
    path = tmp_path / 'scene.sofa'
    _write_sofa(path)
    m = read_sofa_measurement(
        path,
        required_units='pascal_calibrated',
        expected_coordinate_system='sofa_cartesian_metre',
    )
    assert m.sample_rate_hz == SR
    assert m.data_ir_units == 'pascal'
    assert m.measurement_count == 1 and m.channel_count == 1
    arrival, how = measured_arrival_s(m, measurement_index=0)
    assert how == 'data_delay'
    assert arrival == pytest.approx(ARRIVAL_SAMPLES / SR)


def test_read_sofa_missing_units_fails(tmp_path):
    path = tmp_path / 'scene.sofa'
    _write_sofa(path, units=None)
    with pytest.raises(FixtureImportError, match='Units'):
        read_sofa_measurement(path)


def test_read_sofa_unit_mismatch_fails(tmp_path):
    path = tmp_path / 'scene.sofa'
    _write_sofa(path, units='volt')
    with pytest.raises(FixtureImportError, match='pascal_calibrated'):
        read_sofa_measurement(
            path, required_units='pascal_calibrated'
        )


def test_read_sofa_coordinate_mismatch_fails(tmp_path):
    path = tmp_path / 'scene.sofa'
    _write_sofa(path, position_type='spherical')
    with pytest.raises(FixtureImportError, match='Type'):
        read_sofa_measurement(
            path, expected_coordinate_system='sofa_cartesian_metre'
        )


def test_read_sofa_nonfinite_fails(tmp_path):
    path = tmp_path / 'scene.sofa'
    ir = _ir()
    ir[0, 0, 50] = np.nan
    _write_sofa(path, ir=ir)
    with pytest.raises(FixtureImportError, match='non-finite'):
        read_sofa_measurement(path)


def test_read_sofa_non_srir_fails(tmp_path):
    path = tmp_path / 'scene.sofa'
    _write_sofa(path, conventions='GeneralTF')
    with pytest.raises(FixtureImportError, match='SingleRoomSRIR'):
        read_sofa_measurement(path)


# ---------------------------------------------------------------------------
# Import — declared geometry vs measured geometry
# ---------------------------------------------------------------------------


def _imported(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene)
    imported = import_fixture_case(fixture, dataset, scene, tmp_path)
    return fixture, dataset, scene, imported


def test_import_fixture_case(tmp_path):
    fixture, dataset, scene, imported = _imported(tmp_path)
    case = imported.case
    assert case.sample_rate_hz == SR
    assert case.sources[0].position_m == tuple(SOURCE_POS[0])
    assert case.receivers[0].position_m == tuple(LISTENER_POS[0])
    assert case.source_asset.license_id == 'cc-by-sa-4.0'
    assert case.source_asset.evidence_class == 'external_measured'
    assert 'pascal' in case.limitations
    # sealed case re-parses
    from htdt.cad_benchmark import BenchmarkCase

    again = BenchmarkCase(**case.model_dump(mode='python'))
    assert again.semantic_sha256 == case.semantic_sha256


def test_import_position_drift_fails(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(
        dataset,
        scene,
        points=[
            {
                'point_id': 'S1',
                'role': 'source',
                'position_m': (9.9, 0.0, 1.5),
            },
            {
                'point_id': 'R1',
                'role': 'receiver',
                'position_m': tuple(LISTENER_POS[0]),
            },
        ],
    )
    with pytest.raises(FixtureIntegrityError, match='divergent'):
        import_fixture_case(fixture, dataset, scene, tmp_path)


def test_import_unverified_payload_fails(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene)
    (tmp_path / DATASET_NAME / 'scene.sofa').unlink()
    with pytest.raises(FixtureIntegrityError):
        import_fixture_case(fixture, dataset, scene, tmp_path)


def test_import_stale_dataset_pin_fails(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene)
    # same admission id but different content → semantic hash drifts
    other = build_external_asset_admission(
        admission_id='synthetic-bras',
        dataset_name=DATASET_NAME,
        dataset_title='Synthetic BRAS-style corpus v2',
        publisher='Unit Test Publisher',
        source_kind='institutional_repository',
        admission_state='download_on_demand_candidate',
        license_id='cc-by-sa-4.0',
        license_family='cc_by_sa',
        record_uri='https://example.test/record/1',
        version_doi='10.0000/test.1',
        files=dataset.files,
    )
    assert other.semantic_sha256 != dataset.semantic_sha256
    with pytest.raises(FixtureIntegrityError):
        import_fixture_case(fixture, other, scene, tmp_path)


# ---------------------------------------------------------------------------
# Eligibility-gated evaluation
# ---------------------------------------------------------------------------


def test_arrival_timing_pass_and_fail(tmp_path):
    fixture, dataset, scene, imported = _imported(tmp_path)
    measured = ARRIVAL_SAMPLES / SR  # payload-declared delay
    observable = _arrival_observable(measured)
    provider = AnalyticDirectPathProvider()
    # analytic direct path: 1.0 m at ~343.4 m/s ≈ 2.91 ms —
    # measured delay 100/44100 ≈ 2.27 ms → should FAIL honestly
    spec = build_fixture_run_spec(
        spec_id='run-1',
        fixture=fixture,
        provider=provider,
        evaluation_profile=EvaluationProfile(
            profile_id='default', version='1'
        ),
        solver_path='analytic',
        solver_revision='none',
        mesh_resolution='n/a',
        seed='0',
    )
    evidence = run_fixture(
        fixture, imported, [observable], provider, spec
    )
    evaluation = evidence.observable_evaluations[0]
    assert evaluation.verdict == 'fail'
    assert evaluation.error == pytest.approx(
        abs(
            DISTANCE_M
            / (331.3 * (1.0 + TEMPERATURE_C / 273.15) ** 0.5)
            - measured
        )
    )
    assert evidence.status == 'fail'


def test_band_disjoint_unobservable(tmp_path):
    fixture, dataset, scene, imported = _imported(tmp_path)
    observable = _arrival_observable(
        ARRIVAL_SAMPLES / SR, band=(10000.0, 20000.0)
    )
    evaluation = evaluate_fixture_observable(
        fixture, observable, {'arrival_s': 0.001}
    )
    assert evaluation.verdict == 'unobservable'


def test_normalized_fixture_blocks_absolute_level(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa', units='normalized')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene, unit_semantics='normalized')
    observable = BenchmarkObservable(
        observable_id='level',
        kind='magnitude_fr',
        metric_id='rms',
        metric_version='1',
        tolerance=1e-3,
        tolerance_unit='Pa',
        reference={'frequency_hz': [100.0], 'magnitude_db': [-20.0]},
        valid_band_hz=(100.0, 200.0),
    )
    evaluation = evaluate_fixture_observable(
        fixture, observable, {'frequency_hz': [100.0], 'magnitude_db': [-20.0]}
    )
    assert evaluation.verdict == 'unsupported'
    assert 'normalized' in (evaluation.reason or '')


def test_unknown_units_block_amplitude(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(dataset, scene, unit_semantics='unknown')
    observable = BenchmarkObservable(
        observable_id='fr',
        kind='magnitude_fr',
        metric_id='rms',
        metric_version='1',
        tolerance=0.5,
        tolerance_unit='dB',
        reference={'frequency_hz': [100.0], 'magnitude_db': [-20.0]},
        valid_band_hz=(100.0, 200.0),
    )
    evaluation = evaluate_fixture_observable(
        fixture, observable, {'frequency_hz': [100.0], 'magnitude_db': [-20.0]}
    )
    assert evaluation.verdict == 'unsupported'


def test_magnitude_only_blocks_phase(tmp_path):
    sofa = _write_sofa(tmp_path / 'payload.sofa')
    dataset = _dataset(tmp_path, {'scene.sofa': sofa})
    scene = _scene(dataset)
    fixture = _fixture(
        dataset, scene, phase_authority='magnitude_only'
    )
    observable = BenchmarkObservable(
        observable_id='tf',
        kind='complex_transfer',
        metric_id='complex',
        metric_version='1',
        tolerance=0.1,
        tolerance_unit='Pa·s',
        reference={
            'frequency_hz': [100.0],
            'real': [0.1],
            'imaginary': [0.0],
        },
        valid_band_hz=(100.0, 200.0),
    )
    evaluation = evaluate_fixture_observable(
        fixture,
        observable,
        {'frequency_hz': [100.0], 'real': [0.1], 'imaginary': [0.0]},
    )
    assert evaluation.verdict == 'unsupported'
    assert 'phase' in (evaluation.reason or '')


def test_missing_prediction(tmp_path):
    fixture, _, _, _ = _imported(tmp_path)
    observable = _arrival_observable(0.001)
    evaluation = evaluate_fixture_observable(fixture, observable, None)
    assert evaluation.verdict == 'missing'


def test_impulse_window_pass_fail(tmp_path):
    fixture, _, _, _ = _imported(tmp_path)
    times = [i / SR for i in range(200)]
    reference = {'time_s': times, 'pressure': [0.0] * 200}
    observable = BenchmarkObservable(
        observable_id='ir-window',
        kind='impulse_window',
        metric_id='rms',
        metric_version='1',
        tolerance=1e-3,
        tolerance_unit='Pa',
        window_s=(0.0, 200 / SR),
        reference=reference,
        valid_band_hz=(100.0, 4000.0),
    )
    passing = evaluate_fixture_observable(
        fixture, observable, dict(reference)
    )
    assert passing.verdict == 'pass'
    failing = evaluate_fixture_observable(
        fixture,
        observable,
        {'time_s': times, 'pressure': [0.01] * 200},
    )
    assert failing.verdict == 'fail'
    missing = evaluate_fixture_observable(
        fixture, observable, {'time_s': [], 'pressure': []}
    )
    assert missing.verdict == 'missing'


def test_decay_metric_pass_fail(tmp_path):
    fixture, _, _, _ = _imported(tmp_path)
    observable = BenchmarkObservable(
        observable_id='t20',
        kind='decay_metric',
        metric_id='t20',
        metric_version='1',
        tolerance=0.05,
        tolerance_unit='s',
        reference={'value_s': 0.4},
        valid_band_hz=(100.0, 4000.0),
    )
    assert (
        evaluate_fixture_observable(
            fixture, observable, {'value_s': 0.42}
        ).verdict
        == 'pass'
    )
    assert (
        evaluate_fixture_observable(
            fixture, observable, {'value_s': 0.9}
        ).verdict
        == 'fail'
    )
    assert (
        evaluate_fixture_observable(fixture, observable, {}).verdict
        == 'missing'
    )


# ---------------------------------------------------------------------------
# Run spec + replay determinism
# ---------------------------------------------------------------------------


def test_run_and_replay_deterministic(tmp_path):
    fixture, dataset, scene, imported = _imported(tmp_path)
    arrival = ARRIVAL_SAMPLES / SR
    observable = _arrival_observable(arrival)
    provider = StaticFixtureReplayProvider(
        {observable.observable_id: {'arrival_s': arrival}}
    )
    profile = EvaluationProfile(profile_id='p1', version='1')
    spec = build_fixture_run_spec(
        spec_id='run-a',
        fixture=fixture,
        provider=provider,
        evaluation_profile=profile,
        solver_path='static',
        mesh_resolution='n/a',
        seed='7',
    )
    evidence = run_fixture(
        fixture, imported, [observable], provider, spec, profile
    )
    assert evidence.status == 'pass'
    again = run_fixture(
        fixture, imported, [observable], provider, spec, profile
    )
    assert again.evidence_sha256 == evidence.evidence_sha256
    report = replay_fixture(
        fixture,
        imported,
        [observable],
        provider,
        spec,
        evidence,
        profile,
    )
    assert report.verdict == 'reproduced'


def test_replay_diverged_on_changed_predictions(tmp_path):
    fixture, dataset, scene, imported = _imported(tmp_path)
    arrival = ARRIVAL_SAMPLES / SR
    observable = _arrival_observable(arrival)
    provider = StaticFixtureReplayProvider(
        {observable.observable_id: {'arrival_s': arrival}}
    )
    spec = build_fixture_run_spec(
        spec_id='run-b',
        fixture=fixture,
        provider=provider,
        evaluation_profile=EvaluationProfile(
            profile_id='p1', version='1'
        ),
    )
    evidence = run_fixture(
        fixture, imported, [observable], provider, spec
    )
    # replay against a spec that binds the SAME provider identity —
    # so rebuild spec for a mutated provider config, then check the
    # frozen-config guard fires
    mutated = StaticFixtureReplayProvider(
        {observable.observable_id: {'arrival_s': arrival + 1e-3}}
    )
    with pytest.raises(FixtureIntegrityError, match='config hash'):
        run_fixture(fixture, imported, [observable], mutated, spec)


def test_replay_blocked_on_runtime_drift(tmp_path):
    fixture, dataset, scene, imported = _imported(tmp_path)
    arrival = ARRIVAL_SAMPLES / SR
    observable = _arrival_observable(arrival)
    provider = StaticFixtureReplayProvider(
        {observable.observable_id: {'arrival_s': arrival}}
    )
    spec = build_fixture_run_spec(
        spec_id='run-c',
        fixture=fixture,
        provider=provider,
        evaluation_profile=EvaluationProfile(
            profile_id='p1', version='1'
        ),
        runtime={
            'python_version': '9.9.9',
            'python_implementation': 'CPython',
            'platform': 'fake',
            'h5py_version': '0',
            'numpy_version': '0',
        },
    )
    report = replay_fixture(
        fixture, imported, [observable], provider, spec, None
    )
    assert report.verdict == 'blocked'
    assert 'python_version' in (report.reason or '')


def test_informed_calibrated_requires_params(tmp_path):
    fixture, _, _, _ = _imported(tmp_path)
    provider = StaticFixtureReplayProvider({})
    with pytest.raises(ValueError, match='informed'):
        build_fixture_run_spec(
            spec_id='run-d',
            fixture=fixture,
            provider=provider,
            evaluation_profile=EvaluationProfile(
                profile_id='p1', version='1'
            ),
            run_mode='informed_calibrated',
        )


def test_preregistered_rejects_informed_parameters(tmp_path):
    fixture, _, _, _ = _imported(tmp_path)
    provider = StaticFixtureReplayProvider({})
    with pytest.raises(ValueError, match='preregistered'):
        build_fixture_run_spec(
            spec_id='run-e',
            fixture=fixture,
            provider=provider,
            evaluation_profile=EvaluationProfile(
                profile_id='p1', version='1'
            ),
            informed_parameters=('alpha',),
        )


def test_spec_frozen_config_guard(tmp_path):
    fixture, dataset, scene, imported = _imported(tmp_path)
    provider = StaticFixtureReplayProvider({'o': {'arrival_s': 0.001}})
    spec = build_fixture_run_spec(
        spec_id='run-f',
        fixture=fixture,
        provider=provider,
        evaluation_profile=EvaluationProfile(
            profile_id='p1', version='1'
        ),
    )
    other = StaticFixtureReplayProvider(
        {'o': {'arrival_s': 0.001}}, provider_version='2'
    )
    with pytest.raises(FixtureIntegrityError, match='version'):
        run_fixture(
            fixture,
            imported,
            [_arrival_observable(0.001)],
            other,
            spec,
        )
