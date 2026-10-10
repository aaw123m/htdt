"""#836 Actions 4–6 — BRAS v3 qualification binding/evaluation/report."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import numpy as np
import pytest

h5py = pytest.importorskip('h5py', reason='h5py required for SOFA payloads')

from htdt.cad_benchmark import BenchmarkCase, BenchmarkSourceAsset  # noqa: E402
from htdt.cad_bras_v3_importer import (  # noqa: E402
    BrasV3SceneImport,
    BrasV3SofaMember,
    read_sofa_generalfir,
)
from htdt.cad_bras_v3_metrics import build_bras_v3_metric_manifest  # noqa: E402
from htdt.cad_bras_v3_qualification import (  # noqa: E402
    BrasV3QualificationError,
    bind_bras_v3_solver_lanes,
    build_bras_v3_qualification_config,
    build_bras_v3_qualification_report,
    render_bras_v3_report_markdown,
    run_bras_v3_qualification,
)
from htdt.cad_equipment import EquipmentDataProvenance  # noqa: E402
from htdt.canonical_json import canonical_sha256 as _hash  # noqa: E402

C = 343.0


def _write_generalfir(
    path: Path,
    *,
    m: int = 4,
    n: int = 4000,
    fs: float = 8000.0,
    distances_m: tuple[float, ...] = (1.0, 2.0, 3.0, 4.0),
    onset_threshold: float = 0.1,
    emitter_ids=(4, 4, 4, 5),
    receiver_ids=(4, 5, 6, 5),
    units: str = 'Pascal',
) -> Path:
    """GeneralFIR whose per-measurement (1,3,m) positions and onset spike
    are geometrically consistent: measurement i puts the emitter at the
    origin and the receiver ``distances_m[i]`` away, with the IR onset
    spike at ``round(d/c*fs)`` — the analytic lane should pass."""
    rng = np.random.RandomState(7)
    t = np.arange(n) / fs
    ir = np.zeros((m, 1, n))
    for i in range(m):
        k = int(round(distances_m[i] / C * fs))
        decay = rng.randn(n) * np.exp(-t / 0.15) * 0.01
        decay[:k] = 0.0
        decay[k] = 1.0
        ir[i, 0, :] = decay
    with h5py.File(path, 'w') as f:
        f.attrs['SOFAConventions'] = 'GeneralFIR'
        data = f.create_dataset('Data.IR', data=ir)
        data.attrs['Units'] = units
        f.create_dataset('Data.SamplingRate', data=np.array([fs]))
        f.create_dataset('Data.Delay', data=np.zeros((m, 1)))

        def _pos(name: str, arr):
            node = f.create_dataset(name, data=arr)
            node.attrs['Units'] = 'metre'
            node.attrs['Type'] = 'cartesian'

        _pos('SourcePosition', np.zeros((1, 3)))
        _pos('ListenerPosition', np.zeros((1, 3)))
        # BRAS v3 layout: (1, 3, M) — columns are positions.
        emitters = np.zeros((1, 3, m))
        receivers = np.zeros((1, 3, m))
        for i in range(m):
            receivers[0, 0, i] = distances_m[i]
        _pos('EmitterPosition', emitters)
        _pos('ReceiverPosition', receivers)
        f.create_dataset('EmitterID',
                         data=np.asarray(emitter_ids).reshape(m, 1))
        f.create_dataset('ReceiverID',
                         data=np.asarray(receiver_ids).reshape(m, 1))
    return path


def _scene_zip(tmp_path: Path, sofa: Path,
               name: str = '1_scene_descriptions-RS4.zip',
               member: str = 'a/b/inner.sofa') -> Path:
    archive = tmp_path / 'bras-v3' / name
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.write(sofa, member)
    return archive


def _synthetic_case() -> BenchmarkCase:
    provenance = EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='test',
        source_version='1',
        source_reference='test://corpus',
        source_sha256='0' * 64,
    )
    asset_probe = BenchmarkSourceAsset.model_construct(
        asset_id='test-asset',
        dataset_name='bras-v3',
        dataset_version='1',
        origin_uri=None,
        content_sha256=None,
        license_id=None,
        admission_ref=None,
        evidence_class='external_measured',
        provenance=(provenance,),
        semantic_sha256='0' * 64,
    )
    asset = BenchmarkSourceAsset(
        **{
            **asset_probe.model_dump(
                mode='python', exclude={'semantic_sha256'}
            ),
            'semantic_sha256': _hash(asset_probe.semantic_payload()),
        }
    )
    case_probe = BenchmarkCase.model_construct(
        schema_version=1,
        benchmark_id='test-bras-case',
        version='1',
        title='synthetic',
        source_asset=asset,
        coordinate_convention='cartesian',
        geometry=None,
        sources=(),
        receivers=(),
        materials=None,
        environment={'data_ir_units': 'Pascal'},
        sample_rate_hz=8000.0,
        frequency_grid_hz=None,
        time_origin_s=0.0,
        preprocessing='synthetic',
        limitations='test',
        observables=(),
        importer_id='test-importer',
        importer_version='1',
        semantic_sha256='0' * 64,
    )
    return BenchmarkCase(
        **{
            **case_probe.model_dump(
                mode='python', exclude={'semantic_sha256'}
            ),
            'semantic_sha256': _hash(case_probe.semantic_payload()),
        }
    )


def _scene_import(
    measurement, member_path: str, case: BenchmarkCase | None = None,
) -> BrasV3SceneImport:
    return BrasV3SceneImport(
        case=case or _synthetic_case(),
        scene_id='RS4',
        sofa_members=(
            BrasV3SofaMember(
                member_path=member_path,
                role='rir',
                variant='inner',
                disposition='imported',
                measurement_sha256=measurement.measurement_sha256,
            ),
        ),
        measurements=(measurement,),
        receipts=(),
    )


def _manifest(tmp_path: Path, member: str, indexes=(0, 1, 2),
              bands=((500, 2000),)):
    from htdt.cad_external_corpus_manifest import (
        BRAS_V3_ADMISSION, corpus_scenes)
    scenes = {s.scene_id: s for s in corpus_scenes()}
    return build_bras_v3_metric_manifest(
        BRAS_V3_ADMISSION, scenes['RS4'], tmp_path,
        member_path=member, measurement_indexes=indexes,
        bands_hz=bands)


# ---------------------------------------------------------------------------
# Importer position-axis fix (regression — the (1,3,M) layout)
# ---------------------------------------------------------------------------


def test_positions_column_layout(tmp_path: Path):
    # (1,3,3) is inherently ambiguous with plain 3x3 — m != 3 here
    sofa = _write_generalfir(tmp_path / 'p.sofa', m=4,
                             distances_m=(1.0, 2.0, 3.0, 4.0))
    measurement = read_sofa_generalfir(sofa)
    assert measurement.emitter_positions_m == ((0.0, 0.0, 0.0),) * 4
    assert measurement.receiver_positions_m == (
        (1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (4.0, 0.0, 0.0)
    )
    assert measurement.emitter_ids == (4, 4, 4, 5)
    assert measurement.receiver_ids == (4, 5, 6, 5)


# ---------------------------------------------------------------------------
# Solver-lane binding — the fail-closed record
# ---------------------------------------------------------------------------


def test_solver_binding_records_zero_observables():
    case = _synthetic_case()
    binding = bind_bras_v3_solver_lanes(
        case, member_path='a/b/inner.sofa',
        measurement_sha256='a' * 64)
    assert binding.produced_observables == ()
    assert len(binding.lanes) == 3
    for lane in binding.lanes:
        assert lane.verdict == 'blocked'
        assert lane.produced_observables == ()
        assert lane.missing_prerequisites
    geometric = next(l for l in binding.lanes if l.lane == 'geometric_r150')
    assert any('DirectivityDataset' in p
               for p in geometric.missing_prerequisites)
    assert any('geometry' in p for p in geometric.missing_prerequisites)
    assert binding.capability_gap


# ---------------------------------------------------------------------------
# Synthetic end-to-end run
# ---------------------------------------------------------------------------


def _qualify(tmp_path: Path, *, distances=(1.0, 2.0, 3.0, 4.0),
             indexes=(0, 1, 2), bands=((500, 2000),),
             arrival_tolerance_s=0.002, onset_threshold=0.1):
    member = 'a/b/inner.sofa'
    sofa = _write_generalfir(
        tmp_path / 'inner.sofa', m=len(distances),
        distances_m=distances)
    archive = _scene_zip(tmp_path, sofa, member=member)
    measurement = read_sofa_generalfir(archive, member_path=member)
    manifest = _manifest(tmp_path, member, indexes=indexes, bands=bands)
    config = build_bras_v3_qualification_config(
        member_path=member, measurement_indexes=indexes,
        onset_threshold=onset_threshold,
        arrival_tolerance_s=arrival_tolerance_s)
    scene_import = _scene_import(measurement, member)
    evidence, binding = run_bras_v3_qualification(
        scene_import, manifest, config)
    return evidence, binding, manifest, scene_import, measurement


def test_run_synthetic_arrival_pass(tmp_path: Path):
    evidence, binding, *_ = _qualify(tmp_path)
    arrivals = [r for r in evidence.results
                if r.kind == 'arrival_timing']
    assert arrivals and all(r.verdict == 'pass' for r in arrivals)
    assert all(r.error < 0.001 for r in arrivals)
    assert all(r.lane == 'analytic-direct-path' for r in arrivals)
    decays = [r for r in evidence.results if r.kind == 'decay_metric']
    assert decays and all(r.verdict == 'unsupported' for r in decays)
    assert all('solver lanes are blocked' in r.reason for r in decays)
    assert evidence.status == 'incomplete'
    assert binding.produced_observables == ()
    # run case sha differs from the imported case — both recorded
    assert evidence.run_case_sha256 != evidence.case_ref.ref_sha256


def test_run_records_fail_honestly(tmp_path: Path):
    # onset spike at a place unrelated to d/c → numeric FAIL verdict,
    # never hidden
    member = 'a/b/inner.sofa'
    sofa = tmp_path / 'bad.sofa'
    rng = np.random.RandomState(3)
    n, fs, m = 4000, 8000.0, 1
    t = np.arange(n) / fs
    ir = rng.randn(m, 1, n) * np.exp(-t / 0.15) * 0.01
    ir[0, 0, 200] = 1.0  # onset at 25 ms
    with h5py.File(sofa, 'w') as f:
        f.attrs['SOFAConventions'] = 'GeneralFIR'
        data = f.create_dataset('Data.IR', data=ir)
        data.attrs['Units'] = 'Pascal'
        f.create_dataset('Data.SamplingRate', data=np.array([fs]))

        def _pos(name, arr):
            node = f.create_dataset(name, data=arr)
            node.attrs['Units'] = 'metre'
            node.attrs['Type'] = 'cartesian'

        _pos('EmitterPosition', np.zeros((1, 3, m)))
        _pos('ReceiverPosition',
             np.array([[[0.5]], [[0.0]], [[0.0]]]))  # d = 0.5 m
    archive = _scene_zip(tmp_path, sofa)
    measurement = read_sofa_generalfir(archive, member_path=member)
    manifest = _manifest(tmp_path, member, indexes=(0,), bands=())
    config = build_bras_v3_qualification_config(
        member_path=member, measurement_indexes=(0,),
        arrival_tolerance_s=0.002)
    evidence, _ = run_bras_v3_qualification(
        _scene_import(measurement, member), manifest, config)
    assert evidence.results[0].verdict == 'fail'
    assert evidence.results[0].error > 0.02
    assert evidence.status == 'fail'


def test_config_manifest_mismatch_fails_closed(tmp_path: Path):
    member = 'a/b/inner.sofa'
    sofa = _write_generalfir(tmp_path / 'inner.sofa')
    archive = _scene_zip(tmp_path, sofa, member=member)
    measurement = read_sofa_generalfir(archive, member_path=member)
    manifest = _manifest(tmp_path, member)
    scene_import = _scene_import(measurement, member)

    wrong_member = build_bras_v3_qualification_config(
        member_path='a/b/other.sofa', measurement_indexes=(0, 1, 2))
    with pytest.raises(BrasV3QualificationError, match='member_path'):
        run_bras_v3_qualification(scene_import, manifest, wrong_member)

    wrong_indexes = build_bras_v3_qualification_config(
        member_path=member, measurement_indexes=(0, 1))
    with pytest.raises(BrasV3QualificationError,
                       match='measurement_indexes'):
        run_bras_v3_qualification(scene_import, manifest, wrong_indexes)

    wrong_onset = build_bras_v3_qualification_config(
        member_path=member, measurement_indexes=(0, 1, 2),
        onset_threshold=0.2)
    with pytest.raises(BrasV3QualificationError, match='onset'):
        run_bras_v3_qualification(scene_import, manifest, wrong_onset)


def test_report_surfaces(tmp_path: Path):
    evidence, binding, manifest, scene_import, _ = _qualify(tmp_path)
    report = build_bras_v3_qualification_report(
        scene_import=scene_import, manifest=manifest, binding=binding,
        evidences=(evidence,))
    assert report.report_id.startswith('bqr-')
    assert 'no solver-qualification claim' in report.claim_ceiling
    md = render_bras_v3_report_markdown(report)
    assert 'Solver lanes (Action 4 binding)' in md
    assert 'Observable verdicts' in md
    assert 'arrival-m0' in md and 'unsupported' in md
    assert 'Capability gap' in md


# ---------------------------------------------------------------------------
# Corpus-gated runs — real payloads, real verdicts
# ---------------------------------------------------------------------------


CORPUS_ROOT = os.environ.get('HTDT_BRAS_CORPUS_ROOT')
corpus_required = pytest.mark.skipif(
    not CORPUS_ROOT, reason='HTDT_BRAS_CORPUS_ROOT not set')


def _real_run(scene_id: str, member_pick, indexes, bands):
    from htdt.cad_external_corpus_manifest import (
        BRAS_V3_ADMISSION, corpus_scenes)
    from htdt.cad_bras_v3_importer import import_bras_v3_scene
    root = Path(CORPUS_ROOT)
    scenes = {s.scene_id: s for s in corpus_scenes()}
    imp = import_bras_v3_scene(BRAS_V3_ADMISSION, scenes[scene_id], root)
    member = member_pick(imp)
    manifest = build_bras_v3_metric_manifest(
        BRAS_V3_ADMISSION, scenes[scene_id], root,
        member_path=member, measurement_indexes=indexes,
        bands_hz=bands)
    config = build_bras_v3_qualification_config(
        member_path=member, measurement_indexes=indexes)
    evidence, binding = run_bras_v3_qualification(imp, manifest, config)
    return imp, manifest, evidence, binding


@corpus_required
def test_real_qualification_rs4():
    imp, manifest, evidence, binding = _real_run(
        'RS4',
        lambda i: [m.member_path for m in i.sofa_members
                   if 'onCenter' in m.member_path][0],
        (0, 4, 8), ((500, 2000),))
    arrivals = [r for r in evidence.results if r.kind == 'arrival_timing']
    assert len(arrivals) == 3
    # measured onset vs measured-geometry d/c — sub-ms latency residual
    assert all(r.verdict == 'pass' for r in arrivals)
    assert all(r.error < 0.002 for r in arrivals)
    decays = [r for r in evidence.results if r.kind == 'decay_metric']
    assert len(decays) == 3
    assert all(r.verdict == 'unsupported' for r in decays)
    assert binding.produced_observables == ()
    report = build_bras_v3_qualification_report(
        scene_import=imp, manifest=manifest, binding=binding,
        evidences=(evidence,))
    assert report.verdict_counts == {'pass': 3, 'unsupported': 3}


@corpus_required
def test_real_qualification_rs1():
    imp, manifest, evidence, binding = _real_run(
        'RS1',
        lambda i: [m.member_path for m in i.sofa_members
                   if m.disposition == 'imported'
                   and 'Rigid' in m.member_path][0],
        (0, 4, 8), ((1000, 2000),))
    arrivals = [r for r in evidence.results if r.kind == 'arrival_timing']
    assert len(arrivals) == 3
    assert all(r.verdict == 'pass' for r in arrivals)
    assert all(r.error < 0.002 for r in arrivals)
    assert all(
        r.verdict == 'unsupported'
        for r in evidence.results if r.kind == 'decay_metric'
    )
