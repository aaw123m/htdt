"""REV20-LOCALE regression tests: Japanese locale & encoding depth.

- Imported numeric fields parse the ASCII decimal dialect only — full-width
  digits ('５０'), underscores ('1_0') and lexical inf/nan are rejected
  instead of silently coerced (``strict_ascii_number``; the REW parser's
  ``_REW_NUMBER`` already enforced the same dialect).
- A leading UTF-8 BOM is tolerated on the polar-table and FIR-tap ingress
  paths — Excel's "CSV UTF-8" writes one — while genuinely non-UTF-8 bytes
  still fail closed.
- Search surfaces fold NFKC+casefold on both sides (half-width kana,
  full-width digits), matching the palette/command-registry convention.
- ``format_datetime`` labels the instant ' UTC': persisted timestamps are
  UTC and an unlabeled stamp reads as host-local wall time.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from htdt.cad_camilladsp import _import_filter
from htdt.cad_directivity_import import (
    POLAR_TABLE_ADAPTER_ID,
    _finite_number,
    _parse_metadata_and_rows,
)
from htdt.cad_external_calibration import _parse_filter_line
from htdt.cad_fir_filter import _parse_tap_text
from htdt.cad_foam_material_batch import parse_jcal_params_csv
from htdt.cad_loudspeaker_interchange import qualify_clf
from htdt.cad_meter_correction import CgatsParseError, import_meter_correction
from htdt.cad_wave_excitation import (
    WAVE_EXCITATION_TABLE_SCHEMA,
    VolumeVelocityTableConverter,
)
from htdt.equipment_library import _capability_from_source
from htdt.help_registry import build_help_registry
from htdt.ingress import strict_ascii_number
from htdt.localization import PresentationLocale, format_datetime
from htdt.raw_mesh import RawMeshImportError, import_raw_visual_mesh
from htdt.reference_libraries import (
    LibraryEntry,
    LibraryFamily,
    LibraryScope,
    ReferenceLibraryIndex,
)


def _polar_table_bytes(*, frequency: str = '50.0') -> bytes:
    metadata = [
        '# schema=htdt.polar-table.v1',
        '# delimiter=csv',
        '# dataset_id=locale-probe',
        '# dataset_version=1',
        '# capability=magnitude_only',
        '# angle_semantics=horizontal_vertical',
        '# horizontal_wrap=none',
        '# reference_axis=equipment_acoustic_reference_axis',
        '# azimuth_positive=left',
        '# elevation_positive=up',
        '# frequency_unit=Hz',
        '# angle_unit=degree',
        '# magnitude_unit=db',
        '# normalization_reference=on_axis_per_frequency',
        '# interpolation_method=linear',
        '# interpolation_implementation=htdt-grid-linear',
        '# interpolation_version=1',
        '# evidence_kind=user_defined',
        '# source_name=REV20 locale probe',
        '# source_version=1',
        '# source_reference=focused-test',
    ]
    body = [
        'frequency_hz,horizontal_angle_deg,vertical_angle_deg,magnitude,magnitude_unit',
        f'{frequency},0,0,-6.0,db',
    ]
    return ('\n'.join(metadata + body) + '\n').encode('utf-8')


def test_strict_ascii_number_dialect() -> None:
    assert strict_ascii_number('50', field_name='x') == 50.0
    assert strict_ascii_number(' -3.25 ', field_name='x') == -3.25
    assert strict_ascii_number('1e3', field_name='x') == 1000.0
    for token in ('５０', '１,０００', '1_0', 'nan', 'inf', 'abc', '', '５０％'):
        with pytest.raises(ValueError, match='must be numeric'):
            strict_ascii_number(token, field_name='x')
    with pytest.raises(ValueError, match='must be finite'):
        strict_ascii_number('1e999', field_name='x')


def test_polar_table_ingress_tolerates_bom_and_rejects_wide_digits() -> None:
    # Excel's "CSV UTF-8" prepends a BOM; it must not read as a missing
    # metadata header.
    metadata, rows = _parse_metadata_and_rows(b'\xef\xbb\xbf' + _polar_table_bytes())
    assert metadata['dataset_id'] == 'locale-probe'
    assert rows and rows[0].startswith('frequency_hz')

    with pytest.raises(ValueError, match='must be numeric'):
        _capability_from_source(
            _polar_table_bytes(frequency='５０'),
            adapter_id=POLAR_TABLE_ADAPTER_ID,
        )
    with pytest.raises(ValueError, match='must be numeric'):
        _finite_number('５０', field_name='frequency_hz')


def test_fir_tap_ingress_tolerates_bom_and_rejects_wide_digits() -> None:
    assert _parse_tap_text(b'\xef\xbb\xbf0.1 0.5 0.1\n') == (0.1, 0.5, 0.1)
    with pytest.raises(ValueError, match='must be numeric'):
        _parse_tap_text('0.1 １０ 0.3'.encode('utf-8'))


def test_jcal_params_reject_wide_digits() -> None:
    csv_text = (
        'parameter,value\n'
        'material,basotect\n'
        'airflow_resistivity,１２０００\n'
        'porosity,0.99\n'
    )
    with pytest.raises(ValueError, match='not numeric'):
        parse_jcal_params_csv(csv_text, admission_id='a')
    with pytest.raises(ValueError, match='not numeric'):
        parse_jcal_params_csv(
            csv_text.replace('１２０００', 'nan'), admission_id='a'
        )


def test_mesh_vertex_ingress_rejects_wide_digits() -> None:
    obj = b'v 0 0 0\nv 1 0 0\nv \xef\xbc\x91 1 0\nf 1 2 3\n'
    with pytest.raises(RawMeshImportError, match='invalid vertex'):
        import_raw_visual_mesh(
            obj, source_name='probe.obj', format_hint='obj'
        )
    stl = (
        b'solid s\nfacet normal 0 0 1\nouter loop\n'
        b'vertex 0 0 0\nvertex 1 0 0\nvertex \xef\xbc\x91 1 0\n'
        b'endloop\nendfacet\nendsolid s\n'
    )
    with pytest.raises(RawMeshImportError, match='not numeric'):
        import_raw_visual_mesh(
            stl, source_name='probe.stl', format_hint='stl'
        )


_CLF1_BOM = (
    '[HEADER]\n'
    'Manufacturer: Acme Audio\n'
    'Model: S-10\n'
    'CLF format version: 1\n'
    '[FREQUENCY]\n'
    '63 0 -3.0 0.0\n'
    '125 0 -2.0 0.0\n'
    '250 0 -1.0 0.0\n'
    '500 0 0.0 0.0\n'
    '1000 0 0.0 0.0\n'
    '2000 0 -1.5 0.0\n'
    '4000 0 -4.0 0.0\n'
    '8000 0 -8.0 0.0\n'
    '１２０ 0 -9.9 0.0\n'
    '[POLAR]\n'
    'R(0)\n'
)


def test_clf_ingress_tolerates_bom_and_ignores_wide_digit_rows() -> None:
    q = qualify_clf(b'\xef\xbb\xbf' + _CLF1_BOM.encode('utf-8'))
    assert q.verdict == 'qualified'
    assert q.detected_version == 'clf1'
    # The full-width-digit line is not a numeric polar row.
    assert q.frequency_rows == 8


_CCMX = (
    b'CCMX\n'
    b'DESCRIPTOR "test"\n'
    b'COLOR_REP "XYZ"\n'
    b'NUMBER_OF_FIELDS 3\n'
    b'BEGIN_DATA_FORMAT\n'
    b'XYZ_X XYZ_Y XYZ_Z\n'
    b'END_DATA_FORMAT\n'
    b'NUMBER_OF_SETS 3\n'
    b'BEGIN_DATA\n'
    b'1.0 0.0 0.0\n'
    b'0.0 1.0 0.0\n'
    b'0.0 0.0 1.0\n'
    b'END_DATA\n'
)


def test_cgats_ingress_tolerates_bom_and_rejects_wide_digits() -> None:
    artifact = import_meter_correction(
        file_name='probe.ccmx', data=b'\xef\xbb\xbf' + _CCMX
    )
    assert artifact.format_id == 'CCMX'

    wide = _CCMX.replace(b'1.0 0.0 0.0', '\xef\xbc\x91.0 0.0 0.0'.encode('utf-8'))
    with pytest.raises(CgatsParseError, match='non-numeric data cell'):
        import_meter_correction(file_name='wide.ccmx', data=wide)
    wide_fields = _CCMX.replace(
        b'NUMBER_OF_FIELDS 3', 'NUMBER_OF_FIELDS ３'.encode('utf-8')
    )
    with pytest.raises(CgatsParseError, match='not an integer'):
        import_meter_correction(file_name='wide2.ccmx', data=wide_fields)


def test_wave_excitation_ingress_tolerates_bom() -> None:
    source = json.dumps(
        {
            'schema': WAVE_EXCITATION_TABLE_SCHEMA,
            'rows': [{'frequency': 100.0, 'real': 1.0e-4, 'imag': 0.0}],
        }
    ).encode('utf-8')
    samples = VolumeVelocityTableConverter().convert(
        b'\xef\xbb\xbf' + source,
        {'frequency_unit': 'Hz', 'value_unit': 'm3_s', 'value_form': 'rectangular'},
    )
    assert [s.frequency_hz for s in samples] == [100.0]


def test_external_calibration_rejects_wide_digits() -> None:
    fields, reason = _parse_filter_line(
        'ON PK Fc \uff15\uff10 Hz Gain -3.0 dB Q 2.0'
    )
    assert fields is None
    assert reason == 'Fc value is not parseable'


def test_camilladsp_rejects_wide_digits() -> None:
    opaque: list = []
    diagnostics: list[str] = []
    category, _, _, _ = _import_filter(
        'g',
        {'type': 'Gain', 'parameters': {'gain': '１０'}},
        opaque=opaque,
        file_deps=[],
        diagnostics=diagnostics,
    )
    assert category == 'opaque'
    assert 'non-numeric gain' in opaque[-1].reason
    category, _, _, delay_s = _import_filter(
        'd',
        {'type': 'Delay', 'parameters': {'delay': '５', 'unit': 'ms'}},
        opaque=opaque,
        file_deps=[],
        diagnostics=diagnostics,
    )
    assert category == 'opaque'
    assert delay_s is None


def test_format_datetime_marks_utc() -> None:
    moment = datetime(2026, 9, 23, 22, 17, tzinfo=timezone.utc)
    assert format_datetime(moment, PresentationLocale.JAPANESE).endswith(' UTC')
    assert format_datetime(moment, PresentationLocale.ENGLISH).endswith(' UTC')


def test_candidate_filter_folds_half_width_query() -> None:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QTreeWidgetItem

    from htdt.optimization_search_controller import _candidate_matches_filter

    item = QTreeWidgetItem(['候補 1', '番号 1', 'スピーカー 構成'])
    assert _candidate_matches_filter(item, '１')
    assert _candidate_matches_filter(item, 'ｽﾋﾟｰｶｰ')
    assert not _candidate_matches_filter(item, '２')


def test_help_search_folds_half_width_query() -> None:
    registry = build_help_registry()
    assert registry.search('ｽﾋﾟｰｶｰ')
    assert registry.search('ＲＥＷ')


def test_reference_library_search_folds_half_width_query() -> None:
    class _Provider:
        family = LibraryFamily.EQUIPMENT

        def list_entries(self):
            return (
                LibraryEntry(
                    identity='sp-1',
                    family=LibraryFamily.EQUIPMENT,
                    scope=LibraryScope.USER_LIBRARY,
                    display_name='スピーカー SP-1',
                    version='v1',
                    authority_hash='hash-a',
                    description='ブックシェルフ型 ２ｗａｙ',
                ),
            )

        def open_editor_hint(self, entry):
            return 'editor://x'

    index = ReferenceLibraryIndex(meta=None)
    index.register_provider(_Provider())
    assert [e.identity for e in index.search('ｽﾋﾟｰｶｰ')] == ['sp-1']
    assert [e.identity for e in index.search('２ｗａｙ')] == ['sp-1']
    assert index.search('ウーファー') == ()
