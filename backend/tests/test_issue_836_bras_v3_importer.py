"""#836 Action 2 — BRAS v3 GeneralFIR importer tests.

Synthetic SOFA payloads are built with h5py (the same library the reader
uses); the real corpus zip path is exercised only when
``HTDT_BRAS_CORPUS_ROOT`` points at a verified local copy.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest

h5py = pytest.importorskip('h5py', reason='h5py required for SOFA payloads')

from htdt.cad_bras_v3_importer import (  # noqa: E402
    BrasImportError,
    import_bras_v3_scene,
    read_bras_material_csv,
    read_sofa_generalfir,
)


def _write_generalfir(
    path: Path,
    *,
    conventions: str = 'GeneralFIR',
    m: int = 3,
    r: int = 1,
    n: int = 8,
    fs: float = 48000.0,
    ir_units: str | None = 'Pascal',
    position_units: str = 'metre',
    emitter_per_m: bool = True,
    comment: str | None = None,
) -> Path:
    with h5py.File(path, 'w') as f:
        f.attrs['SOFAConventions'] = conventions
        if comment is not None:
            f.attrs['Comment'] = comment
        f.attrs['Conventions'] = 'SOFA'
        data = f.create_dataset('Data.IR', data=np.random.RandomState(0)
                                .rand(m, r, n))
        if ir_units is not None:
            data.attrs['Units'] = ir_units
        f.create_dataset('Data.SamplingRate', data=np.array([fs]))
        f.create_dataset('Data.Delay', data=np.zeros((r, 2)))

        def _pos(name: str, arr, *, axis=None):
            node = f.create_dataset(name, data=arr)
            node.attrs['Units'] = position_units
            node.attrs['Type'] = 'cartesian'
            if axis is not None:
                node.attrs['Dimension'] = axis
            return node

        _pos('SourcePosition', np.zeros((1, 3)))
        _pos('ListenerPosition', np.zeros((1, 3)))
        emitter = (np.arange(m * 3) / 10.0).reshape(1, 3, m) \
            if emitter_per_m else np.array([[0.0, 0.0, 1.5]])
        _pos('EmitterPosition', emitter)
        _pos('ReceiverPosition',
             (np.arange(m * 3) / 7.0).reshape(1, 3, m))
    return path


def _write_zip_with_members(root: Path, name: str, members: dict[str, Path]):
    archive = root / name
    with zipfile.ZipFile(archive, 'w') as zf:
        for member_name, src in members.items():
            zf.write(src, member_name)
    return archive


def test_generalfir_reader_parses_verbatim(tmp_path: Path):
    sofa = _write_generalfir(tmp_path / 'ok.sofa', m=4)
    m = read_sofa_generalfir(sofa)
    assert m.sofa_conventions == 'GeneralFIR'
    assert m.data_ir_units == 'Pascal'
    assert m.sample_rate_hz == 48000.0
    assert m.measurement_count == 4
    assert m.ir_length_samples == 8
    assert len(m.receiver_positions_m) == 4
    assert len(m.emitter_positions_m) == 4
    assert m.measurement_sha256 and len(m.measurement_sha256) == 64


def test_generalfir_reader_rejects_wrong_conventions(tmp_path: Path):
    sofa = _write_generalfir(tmp_path / 'bad.sofa',
                             conventions='SingleRoomSRIR')
    with pytest.raises(BrasImportError, match='GeneralFIR'):
        read_sofa_generalfir(sofa)


def test_generalfir_reader_requires_units(tmp_path: Path):
    sofa = _write_generalfir(tmp_path / 'nouint.sofa', ir_units=None)
    with pytest.raises(BrasImportError, match='Units'):
        read_sofa_generalfir(sofa)


def test_generalfir_reader_units_from_comment(tmp_path: Path):
    sofa = _write_generalfir(
        tmp_path / 'cmt.sofa', ir_units=None,
        comment='The unit of Data.IR is Pascal. other text',
    )
    m = read_sofa_generalfir(sofa)
    assert m.data_ir_units == 'Pascal'


def test_generalfir_reader_rejects_non_metre_positions(tmp_path: Path):
    sofa = _write_generalfir(tmp_path / 'deg.sofa',
                             position_units='degree')
    with pytest.raises(BrasImportError, match='metres'):
        read_sofa_generalfir(sofa)


def test_generalfir_reader_rejects_count_mismatch(tmp_path: Path):
    sofa = tmp_path / 'mismatch.sofa'
    with h5py.File(sofa, 'w') as f:
        f.attrs['SOFAConventions'] = 'GeneralFIR'
        data = f.create_dataset('Data.IR',
                                data=np.zeros((3, 1, 8)))
        data.attrs['Units'] = 'Pascal'
        f.create_dataset('Data.SamplingRate', data=np.array([48000.0]))
        sp = f.create_dataset('SourcePosition', data=np.zeros((1, 3)))
        sp.attrs['Units'] = 'metre'
        rp = f.create_dataset('ReceiverPosition',
                              data=np.zeros((1, 3, 2)))  # M=3 vs 2
        rp.attrs['Units'] = 'metre'
    with pytest.raises(BrasImportError, match='incompatible'):
        read_sofa_generalfir(sofa)


def test_generalfir_reader_zip_member(tmp_path: Path):
    sofa = _write_generalfir(tmp_path / 'inner.sofa')
    archive = _write_zip_with_members(
        tmp_path, 'scene.zip', {'a/b/inner.sofa': sofa})
    m = read_sofa_generalfir(archive, member_path='a/b/inner.sofa')
    assert m.measurement_count == 3
    with pytest.raises(BrasImportError, match='absent'):
        read_sofa_generalfir(archive, member_path='nope.sofa')


_MATERIAL_CSV = (
    '20, 25, 40, 50, 63\n'
    '0.10, 0.20, 0.30, 0.40, 0.50\n'
    '0.05, 0.05, 0.06, 0.07, 0.08\n'
)


def test_material_csv_parse(tmp_path: Path):
    csv_path = tmp_path / 'mat_x.csv'
    csv_path.write_text(_MATERIAL_CSV, encoding='utf-8')
    t = read_bras_material_csv(csv_path, estimate_kind='initial_estimates')
    assert t.band_hz == (20.0, 25.0, 40.0, 50.0, 63.0)
    assert t.absorption[-1] == 0.50
    assert t.scattering[0] == 0.05
    assert t.material_id == 'mat_x'


def test_material_csv_kind_from_path(tmp_path: Path):
    csv_path = tmp_path / 'initial_estimates' / 'mat_x.csv'
    csv_path.parent.mkdir()
    csv_path.write_text(_MATERIAL_CSV, encoding='utf-8')
    t = read_bras_material_csv(csv_path)
    assert t.estimate_kind == 'initial_estimates'


def test_material_csv_kind_unresolvable_fails(tmp_path: Path):
    csv_path = tmp_path / 'mat_x.csv'
    csv_path.write_text(_MATERIAL_CSV, encoding='utf-8')
    with pytest.raises(BrasImportError, match='estimate kind'):
        read_bras_material_csv(csv_path)


def test_material_csv_rejects_ragged(tmp_path: Path):
    csv_path = tmp_path / 'mat_bad.csv'
    csv_path.write_text(
        '20, 25\n0.1, 0.2\n0.1\n', encoding='utf-8')
    with pytest.raises(BrasImportError):
        read_bras_material_csv(csv_path, estimate_kind='initial_estimates')


def test_material_csv_rejects_out_of_range(tmp_path: Path):
    csv_path = tmp_path / 'mat_oob.csv'
    csv_path.write_text(
        '20, 25\n0.1, 1.5\n0.1, 0.2\n', encoding='utf-8')
    with pytest.raises(BrasImportError):
        read_bras_material_csv(csv_path, estimate_kind='initial_estimates')


def test_material_csv_rejects_non_numeric(tmp_path: Path):
    csv_path = tmp_path / 'mat_nan.csv'
    csv_path.write_text(
        '20, 25\n0.1, bad\n0.1, 0.2\n', encoding='utf-8')
    with pytest.raises(BrasImportError, match='non-numeric'):
        read_bras_material_csv(csv_path, estimate_kind='initial_estimates')


def test_material_csv_descending_bands_fail(tmp_path: Path):
    csv_path = tmp_path / 'mat_desc.csv'
    csv_path.write_text(
        '50, 20\n0.1, 0.2\n0.1, 0.2\n', encoding='utf-8')
    with pytest.raises(BrasImportError):
        read_bras_material_csv(csv_path, estimate_kind='initial_estimates')


# --- real-corpus integration (opt-in via env var) ---------------------------

CORPUS_ROOT = os.environ.get('HTDT_BRAS_CORPUS_ROOT')
corpus_required = pytest.mark.skipif(
    not CORPUS_ROOT, reason='HTDT_BRAS_CORPUS_ROOT not set')


@corpus_required
def test_real_scene_import_rs4():
    from htdt.cad_external_corpus_manifest import (
        BRAS_V3_ADMISSION, corpus_scenes)
    scenes = {s.scene_id: s for s in corpus_scenes()}
    imp = import_bras_v3_scene(
        BRAS_V3_ADMISSION, scenes['RS4'], CORPUS_ROOT)
    assert imp.receipts[0].verdict == 'verified'
    assert len(imp.case.sources) == 9
    assert len(imp.case.receivers) == 9
    assert all(
        m.disposition == 'imported' for m in imp.sofa_members
    )


@corpus_required
def test_real_scene_import_rs1_skips_brirs():
    from htdt.cad_external_corpus_manifest import (
        BRAS_V3_ADMISSION, corpus_scenes)
    scenes = {s.scene_id: s for s in corpus_scenes()}
    imp = import_bras_v3_scene(
        BRAS_V3_ADMISSION, scenes['RS1'], CORPUS_ROOT)
    imported = [m for m in imp.sofa_members if m.disposition == 'imported']
    skipped = [m for m in imp.sofa_members if m.disposition == 'skipped']
    assert len(imported) == 3
    assert len(skipped) == 3
    assert all(m.skip_reason for m in skipped)


@corpus_required
def test_real_material_csvs():
    root = Path(CORPUS_ROOT)
    archive = root / 'bras-v3' / '3_surface_descriptions.zip'
    with zipfile.ZipFile(archive) as zf:
        names = [
            n for n in zf.namelist()
            if n.endswith('.csv') and '_csv/' in n
        ]
    assert names
    for name in names:
        t = read_bras_material_csv(archive, member_path=name)
        assert t.estimate_kind in (
            'initial_estimates', 'fitted_estimates')
        assert len(t.band_hz) == 31
