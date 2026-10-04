"""Guided video commissioning — measurement import (#541).

The import entry never raises for bad input: documented formats parse,
undocumented ones return an honest ``unsupported`` state with a JA
pointer at the interchange, malformed payloads return ``malformed``.
"""

from __future__ import annotations

from htdt.cad_colorimetry import (
    ColorimeterCorrectionProfile,
    StimulusDefinition,
    TristimulusSample,
    build_video_color_measurement_set,
)
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_video_measure_import import (
    VIDEO_IMPORT_FORMAT_LABELS,
    detect_video_import_format,
    export_video_measurements,
    import_video_measurements,
)


def _gray_csv() -> bytes:
    # HCFR `Advance → Export → Measures to csv file` GrayScaleSheet layout
    # (verified against the open-source Export.cpp): `Measure;0;1;…`
    # header, an IRE level row, then X / Y / Z rows. The R/G/B, ColorTemp
    # and DeltaE rows are carried as warnings, never re-interpreted.
    return (
        'Measure;0;1;2;3;4\n'
        'IRE;0;25;50;75;100\n'
        'X;0.01;0.30;0.95;2.40;39.0\n'
        'Y;0.00;0.32;1.00;2.50;41.0\n'
        'Z;0.02;0.35;1.10;2.60;44.0\n'
        'R;0.00;0.01;0.02;0.05;0.40\n'
        'G;0.00;0.01;0.02;0.05;0.41\n'
        'B;0.00;0.01;0.03;0.06;0.44\n'
        'ColorTemp;0;6500;6500;6500;6500\n'
        'DeltaE;0.0;1.1;0.9;0.7;0.4\n'
    ).encode('utf-8')


def _prim_csv() -> bytes:
    # PrimariesSheet layout: `Measure;Red;Green;Blue;Yellow;Cyan;Magenta`
    # then X / Y / Z rows (R/G/B and DeltaE retained as warnings).
    return (
        'Measure;Red;Green;Blue;Yellow;Cyan;Magenta\n'
        'X;26.0;20.0;6.0;30.0;15.0;21.0\n'
        'Y;14.0;40.0;4.0;38.0;32.0;12.0\n'
        'Z;0.5;8.0;21.0;1.0;25.0;26.0\n'
        'R;0.64;0.30;0.15;0.42;0.22;0.32\n'
        'G;0.33;0.60;0.06;0.50;0.49;0.16\n'
        'B;0.03;0.10;0.79;0.08;0.29;0.52\n'
        'DeltaE;1.0;0.8;1.2;1.1;0.9;1.3\n'
    ).encode('utf-8')


def _general_csv() -> bytes:
    return (
        'Num;Name;Export Date;Sensor;Generator;Infos\n'
        '1;demo;2026-10-01;i1DisplayPro;HTPC;notes\n'
    ).encode('utf-8')


# ---- detection -----------------------------------------------------------


def test_detect_hcfr_sheets_and_chc() -> None:
    assert (
        detect_video_import_format('a.GrayScaleSheet.csv', _gray_csv())
        == 'hcfr_grayscale_csv'
    )
    assert (
        detect_video_import_format('a.PrimariesSheet.csv', _prim_csv())
        == 'hcfr_primaries_csv'
    )
    assert (
        detect_video_import_format('a.GeneralSheet.csv', _general_csv())
        == 'hcfr_general_csv'
    )
    assert (
        detect_video_import_format('proj.chc', b'\x00' * 64)
        == 'hcfr_chc_binary'
    )
    assert detect_video_import_format('data.bin', b'\x89PNG') == 'unknown'
    assert (
        detect_video_import_format('data.json', b'{"format": "other"}')
        == 'unknown'
    )


# ---- HCFR CSV imports ----------------------------------------------------


def test_hcfr_grayscale_import_maps_ire_to_normalized_levels() -> None:
    result = import_video_measurements(
        file_name='demo.GrayScaleSheet.csv',
        data=_gray_csv(),
        surface_entity_id='screen-main',
        meter='klein-k10',
        session_id='vcs-abc',
    )
    assert result.status == 'imported'
    assert result.format_id == 'hcfr_grayscale_csv'
    ms = result.measurement_set
    assert ms is not None
    assert ms.surface_entity_id == 'screen-main'
    assert ms.meter == 'klein-k10'
    assert len(ms.samples) == 5
    levels = [s.stimulus_level for s in ms.samples]
    assert levels == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert ms.samples[0].stimulus_id == 'w0'
    assert ms.samples[-1].stimulus_id == 'w100'
    # Provenance records the file + parser — evidence_kind stays 'measured'.
    prov = ms.provenance[0]
    assert prov.evidence_kind == 'measured'
    assert prov.source_name == 'demo.GrayScaleSheet.csv'
    assert ms.import_parser_id is not None
    assert 'hcfr' in ms.import_parser_id
    # The batch links this session to the set.
    assert result.batch is not None
    assert result.batch.session_id == 'vcs-abc'
    assert result.batch.measurement_set_id == ms.measurement_set_id


def test_hcfr_grayscale_without_level_row_warns_and_estimates() -> None:
    data = (
        'Measure;0;1;2\n'
        'X;0.30;0.95;39.0\n'
        'Y;0.32;1.00;41.0\n'
        'Z;0.35;1.10;44.0\n'
        'R;0.01;0.02;0.40\n'
    ).encode('utf-8')
    result = import_video_measurements(
        file_name='demo.GrayScaleSheet.csv',
        data=data,
        surface_entity_id='screen-main',
        meter='klein-k10',
    )
    assert result.status == 'imported'
    assert result.warnings
    assert any('推定' in w for w in result.warnings)


def test_hcfr_primaries_import_maps_six_points() -> None:
    result = import_video_measurements(
        file_name='demo.PrimariesSheet.csv',
        data=_prim_csv(),
        surface_entity_id='screen-main',
        meter='klein-k10',
    )
    assert result.status == 'imported'
    ms = result.measurement_set
    assert ms is not None
    assert [s.stimulus_id for s in ms.samples] == [
        'red', 'green', 'blue', 'yellow', 'cyan', 'magenta'
    ]
    assert all(s.stimulus_level == 1.0 for s in ms.samples)


def test_hcfr_general_sheet_is_malformed_for_import() -> None:
    # GeneralSheet carries metadata only — honest malformed, not a
    # fabricated empty measurement set.
    result = import_video_measurements(
        file_name='demo.GeneralSheet.csv',
        data=_general_csv(),
        surface_entity_id='screen-main',
        meter='klein-k10',
    )
    assert result.status == 'malformed'
    assert result.failure is not None
    assert '測定サンプル' in result.failure.reason


def test_hcfr_malformed_rows_report_reason() -> None:
    result = import_video_measurements(
        file_name='demo.GrayScaleSheet.csv',
        data=b'Measure;0;1\nX;bad\n',
        surface_entity_id='screen-main',
        meter='klein-k10',
    )
    assert result.status == 'malformed'
    assert result.failure is not None
    assert result.failure.reason


# ---- honest unsupported --------------------------------------------------


def test_chc_binary_is_unsupported_with_interchange_hint() -> None:
    result = import_video_measurements(
        file_name='project.chc',
        data=b'\x00' * 128,
        surface_entity_id='screen-main',
        meter='klein-k10',
    )
    assert result.status == 'unsupported'
    assert result.failure is not None
    assert result.failure.interchange_hint
    # The hint names the documented CSV export path — JA text.
    assert 'GrayScaleSheet' in result.failure.interchange_hint
    assert 'csv' in result.failure.interchange_hint.lower()


def test_unknown_format_is_unsupported() -> None:
    result = import_video_measurements(
        file_name='screenshot.png',
        data=b'\x89PNG\r\n\x1a\n' + b'0' * 32,
        surface_entity_id='screen-main',
        meter='klein-k10',
    )
    assert result.status == 'unsupported'
    assert result.format_id == 'unknown'


# ---- HTDT JSON interchange round-trip ------------------------------------


def _source_set():
    return build_video_color_measurement_set(
        measurement_set_id='vms-round-trip',
        measured_at_utc='2026-10-01T12:00:00+00:00',
        surface_entity_id='screen-main',
        meter='klein-k10',
        meter_correction=ColorimeterCorrectionProfile(
            correction_id='k10-oled-1',
            version='1',
            base_meter='klein-k10',
            correction_kind='four_color_matrix',
            applies_to_display_class='oled',
        ),
        stimulus=StimulusDefinition(
            encoding='rgb_limited',
            bit_depth=10,
            patch_size_percent=10.0,
            pattern_generator='murideo-g7',
        ),
        samples=(
            TristimulusSample(
                stimulus_id='w100', stimulus_level=1.0,
                x=39.0, y_luminance=41.0, z=44.0,
            ),
            TristimulusSample(
                stimulus_id='gray_50', stimulus_level=0.5,
                x=0.95, y_luminance=1.0, z=1.1,
            ),
        ),
        import_source='hcfr-grayscale-csv',
        import_app_version='htdt',
        import_asset_sha256='a' * 64,
        import_parser_id='hcfr-grayscale-1',
        provenance=(
            EquipmentDataProvenance(
                evidence_kind='measured',
                source_name='demo.GrayScaleSheet.csv',
                source_version='hcfr-grayscale-1',
                source_reference='demo.GrayScaleSheet.csv',
                source_sha256='a' * 64,
            ),
        ),
    )


def test_htdt_json_export_reimports_byte_identical() -> None:
    source = _source_set()
    exported = export_video_measurements(source)
    result = import_video_measurements(
        file_name='roundtrip.json',
        data=exported.encode('utf-8'),
        surface_entity_id='screen-main',
    )
    assert result.status == 'imported'
    assert result.format_id == 'htdt_video_measurements_json'
    ms = result.measurement_set
    assert ms is not None
    assert ms.measurement_set_sha256 == source.measurement_set_sha256
    assert ms == source


def test_htdt_json_invalid_payload_is_malformed() -> None:
    result = import_video_measurements(
        file_name='bad.json',
        data=b'{"format": "htdt-video-measurements-1", "samples": "no"}',
        surface_entity_id='screen-main',
        meter='klein-k10',
    )
    assert result.status == 'malformed'


# ---- JA labels ------------------------------------------------------------


def test_every_format_has_a_japanese_label() -> None:
    formats = {
        'htdt_video_measurements_json',
        'hcfr_grayscale_csv',
        'hcfr_primaries_csv',
        'hcfr_general_csv',
        'hcfr_chc_binary',
        'unknown',
    }
    assert set(VIDEO_IMPORT_FORMAT_LABELS) == formats
    for label in VIDEO_IMPORT_FORMAT_LABELS.values():
        assert label and label.strip()
