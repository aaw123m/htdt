"""#836 Action 3 — BRAS v3 metric-manifest tests."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import numpy as np
import pytest

h5py = pytest.importorskip('h5py', reason='h5py required for SOFA payloads')

from htdt.cad_bras_v3_importer import BrasImportError  # noqa: E402
from htdt.cad_bras_v3_metrics import (  # noqa: E402
    bras_arrival_timing_reference,
    bras_decay_metric_reference,
    bras_impulse_window_reference,
    bras_magnitude_fr_reference,
    build_bras_v3_metric_manifest,
)


def _write_generalfir(
    path: Path, *, m: int = 2, n: int = 4000, fs: float = 8000.0,
    seed: int = 1,
) -> Path:
    rng = np.random.RandomState(seed)
    t = np.arange(n) / fs
    # exponentially decaying noise → well-defined Schroeder T20
    decay_s = 0.15
    ir = rng.randn(m, 1, n) * np.exp(-t / decay_s)[None, None, :]
    with h5py.File(path, 'w') as f:
        f.attrs['SOFAConventions'] = 'GeneralFIR'
        data = f.create_dataset('Data.IR', data=ir)
        data.attrs['Units'] = 'Pascal'
        f.create_dataset('Data.SamplingRate', data=np.array([fs]))
        for name, arr in (
            ('SourcePosition', np.zeros((1, 3))),
            ('ReceiverPosition',
             (np.arange(m * 3) / 10.0).reshape(1, 3, m)),
        ):
            node = f.create_dataset(name, data=arr)
            node.attrs['Units'] = 'metre'
            node.attrs['Type'] = 'cartesian'
    return path


def _scene_zip(tmp_path: Path) -> Path:
    sofa = _write_generalfir(tmp_path / 'inner.sofa')
    archive = tmp_path / '1_scene_descriptions-RSX.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.write(sofa, 'a/b/inner.sofa')
    return archive


def test_impulse_window(tmp_path: Path):
    archive = _scene_zip(tmp_path)
    ref = bras_impulse_window_reference(
        archive, member_path='a/b/inner.sofa',
        measurement_index=0, window_s=(0.0, 0.05))
    assert ref['pressure'] and len(ref['time_s']) == len(ref['pressure'])
    assert max(ref['time_s']) <= 0.05 + 1e-9


def test_magnitude_fr(tmp_path: Path):
    archive = _scene_zip(tmp_path)
    ref = bras_magnitude_fr_reference(
        archive, member_path='a/b/inner.sofa',
        measurement_index=0, frequencies_hz=[100.0, 500.0, 1000.0])
    assert ref['units'] == 'Pascal'
    assert len(ref['magnitude']) == 3


def test_magnitude_fr_rejects_out_of_band(tmp_path: Path):
    archive = _scene_zip(tmp_path)
    with pytest.raises(BrasImportError, match='exceeds'):
        bras_magnitude_fr_reference(
            archive, member_path='a/b/inner.sofa',
            measurement_index=0, frequencies_hz=[1e9])


def test_arrival_timing(tmp_path: Path):
    archive = _scene_zip(tmp_path)
    ref = bras_arrival_timing_reference(
        archive, member_path='a/b/inner.sofa',
        measurement_index=0, onset_threshold=0.1)
    assert 0.0 <= ref['arrival_s'] < 0.5
    assert ref['onset_threshold'] == 0.1


def test_decay_t20_plausible(tmp_path: Path):
    archive = _scene_zip(tmp_path)
    ref = bras_decay_metric_reference(
        archive, member_path='a/b/inner.sofa',
        measurement_index=0, band_hz=(500.0, 2000.0))
    # positive finite T20 of a seeded decaying IR — deterministic value
    assert 0.1 < ref['value_s'] < 5.0


def test_axis_bounds_fail_closed(tmp_path: Path):
    archive = _scene_zip(tmp_path)
    with pytest.raises(BrasImportError, match='outside'):
        bras_impulse_window_reference(
            archive, member_path='a/b/inner.sofa',
            measurement_index=99)


def test_manifest_requires_indexes(tmp_path: Path):
    pytest.importorskip('htdt.cad_external_corpus_manifest')
    from htdt.cad_external_corpus_manifest import (
        BRAS_V3_ADMISSION, corpus_scenes)
    scenes = {s.scene_id: s for s in corpus_scenes()}
    with pytest.raises(BrasImportError, match='indexes'):
        build_bras_v3_metric_manifest(
            BRAS_V3_ADMISSION, scenes['RS4'], tmp_path,
            member_path='x.sofa', measurement_indexes=())


CORPUS_ROOT = os.environ.get('HTDT_BRAS_CORPUS_ROOT')
corpus_required = pytest.mark.skipif(
    not CORPUS_ROOT, reason='HTDT_BRAS_CORPUS_ROOT not set')


@corpus_required
def test_real_manifest_rs4():
    from htdt.cad_external_corpus_manifest import (
        BRAS_V3_ADMISSION, corpus_scenes)
    scenes = {s.scene_id: s for s in corpus_scenes()}
    zf = zipfile.ZipFile(
        Path(CORPUS_ROOT) / 'bras-v3' / '1_scene_descriptions-RS4.zip')
    member = [n for n in zf.namelist()
              if n.endswith('.sofa') and 'onCenter' in n][0]
    mm = build_bras_v3_metric_manifest(
        BRAS_V3_ADMISSION, scenes['RS4'], CORPUS_ROOT,
        member_path=member, measurement_indexes=(0, 4, 8),
        bands_hz=((100, 125), (1000, 1250), (2000, 2500)))
    assert mm.manifest_id.startswith('bmm-')
    arrivals = [o for o in mm.observables if o.kind == 'arrival_timing']
    t20s = [o for o in mm.observables if o.kind == 'decay_metric']
    assert len(arrivals) == 3 and len(t20s) == 9
    assert all(0.0 < a.reference['arrival_s'] < 1.0 for a in arrivals)
    assert all(0.05 < t.reference['value_s'] < 10.0 for t in t20s)
