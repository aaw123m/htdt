"""Video measurement import entry — the journey's honest on-ramp (#541).

One bounded authority decides what a dropped-in measurement file becomes:

* ``htdt_video_measurements_json`` — the documented HTDT interchange
  (``htdt-video-measurements-1``). Round-trips through
  :func:`export_video_measurements` byte-exactly, so a re-imported set is
  byte-identical to the evidence that was exported.
* ``hcfr_grayscale_csv`` / ``hcfr_primaries_csv`` — the documented
  ColorHCFR ``Advance → Export → Measures to csv file`` interchange
  (``*.GrayScaleSheet.csv`` / ``*.PrimariesSheet.csv``, ``;``-separated,
  ``X``/``Y``/``Z`` rows keyed by the ``Measure`` header row). This is the
  replacement for the proprietary ``.chc`` project file.
* ``hcfr_general_csv`` — the ``*.GeneralSheet.csv`` sidecar (document
  name, sensor, generator). Parsed for metadata only; it carries no
  tristimulus samples.
* ``hcfr_chc_binary`` — the ColorHCFR ``.chc`` project file. It is an
  undocumented MFC-serialized binary; the honest answer is
  ``unsupported`` with a pointer at the CSV interchange above. Never
  guessed, never partially decoded.

Every successful import produces a ``VideoColorMeasurementSet`` (the
existing #647 evidence type, with its ``import_*`` provenance fields) and
a persisted ``VideoMeasurementImport`` batch record so the journey can
link session → imported set without mutating the baseline.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_colorimetry import (
    ColorimeterCorrectionProfile,
    StimulusDefinition,
    TristimulusSample,
    VideoColorMeasurementSet,
    build_video_color_measurement_set,
)
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)
from .clock import utc_now_iso as _utc_now


VideoImportFormat = Literal[
    'htdt_video_measurements_json',
    'hcfr_grayscale_csv',
    'hcfr_primaries_csv',
    'hcfr_general_csv',
    'hcfr_chc_binary',
    'unknown',
]

VIDEO_IMPORT_FORMAT_LABELS: dict[str, str] = {
    'htdt_video_measurements_json': 'HTDT測定データ (JSON)',
    'hcfr_grayscale_csv': 'ColorHCFR グレースケール CSV',
    'hcfr_primaries_csv': 'ColorHCFR カラーポイント CSV',
    'hcfr_general_csv': 'ColorHCFR 測定情報 CSV',
    'hcfr_chc_binary': 'ColorHCFR プロジェクト (.chc)',
    'unknown': '不明な形式',
}

# The parser ids are part of the persisted provenance: bump them when the
# interpretation changes, never silently.
HTDT_JSON_PARSER_ID = 'htdt-video-measurements-json-1'
HCFR_GRAYSCALE_PARSER_ID = 'hcfr-grayscale-csv-1'
HCFR_PRIMARIES_PARSER_ID = 'hcfr-primaries-csv-1'
HCFR_GENERAL_PARSER_ID = 'hcfr-general-csv-1'

_CHC_INTERCHANGE_HINT = (
    '.chc は ColorHCFR の独自バイナリ形式（文書化されていないシリアル化'
    'データ）のため、取り込みには対応していません。HCFR の '
    '「Advance → Export → Measures to csv file」で書き出される '
    '*.GrayScaleSheet.csv / *.PrimariesSheet.csv をそのまま読めます。'
)


class VideoImportFailure(BaseModel):
    """Why an import did not produce evidence — never silently dropped."""

    model_config = ConfigDict(frozen=True)

    format_id: VideoImportFormat
    file_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    interchange_hint: str | None = None


class VideoMeasurementImport(BaseModel):
    """Persisted record of one import batch — append-only evidence link."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['video-measurement-import-1'] = (
        'video-measurement-import-1'
    )
    batch_id: str = Field(min_length=1)
    session_id: str | None = None
    file_name: str = Field(min_length=1)
    format_id: VideoImportFormat
    parser_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_set_id: str = Field(min_length=1)
    measurement_set_sha256: str = Field(min_length=16)
    stimulus_count: int = Field(ge=0)
    warnings: tuple[str, ...] = ()
    imported_at_utc: str = Field(min_length=1)
    batch_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'batch_sha256', 'batch_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'VideoMeasurementImport':
        if self.batch_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'video measurement import semantic hash mismatch'
            )
        if self.batch_id != 'vib-' + self.batch_sha256[:24]:
            raise ValueError(
                'video measurement import batch id/hash mismatch'
            )
        return self


class VideoImportResult(BaseModel):
    """Outcome of one import attempt — evidence or an honest failure."""

    model_config = ConfigDict(frozen=True)

    status: Literal['imported', 'unsupported', 'malformed']
    file_name: str = Field(min_length=1)
    format_id: VideoImportFormat
    measurement_set: VideoColorMeasurementSet | None = None
    batch: VideoMeasurementImport | None = None
    failure: VideoImportFailure | None = None
    warnings: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'VideoImportResult':
        if self.status == 'imported':
            if self.measurement_set is None or self.batch is None:
                raise ValueError(
                    'an imported result carries its set and batch'
                )
            if self.failure is not None:
                raise ValueError('imported result cannot carry a failure')
        else:
            if self.failure is None:
                raise ValueError(
                    'a non-imported result must explain itself'
                )
            if (
                self.measurement_set is not None
                or self.batch is not None
            ):
                raise ValueError(
                    'a failed import cannot carry measurement evidence'
                )
        return self


# ---------------------------------------------------------------------------
# Format detection — file name and content, never just the extension.
# ---------------------------------------------------------------------------

def detect_video_import_format(
    file_name: str, data: bytes
) -> VideoImportFormat:
    lower = file_name.lower()
    if lower.endswith('.chc'):
        return 'hcfr_chc_binary'
    if lower.endswith('grayscalesheet.csv'):
        return 'hcfr_grayscale_csv'
    if lower.endswith('primariessheet.csv'):
        return 'hcfr_primaries_csv'
    if lower.endswith('generalsheet.csv'):
        return 'hcfr_general_csv'
    if lower.endswith('.json'):
        try:
            payload = json.loads(data.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 'unknown'
        if (
            isinstance(payload, dict)
            and payload.get('format') == 'htdt-video-measurements-1'
        ):
            return 'htdt_video_measurements_json'
        return 'unknown'
    return 'unknown'


# ---------------------------------------------------------------------------
# HTDT interchange export — the canonical documented round-trip form.
# ---------------------------------------------------------------------------

def export_video_measurements(
    measurement_set: VideoColorMeasurementSet,
) -> str:
    """Serialize a measurement set to the ``htdt-video-measurements-1``
    interchange JSON. Re-importing the output reconstructs a byte-identical
    set (same ``measurement_set_sha256``) when the caller keeps
    ``measurement_set_id`` — provenance fields are part of the payload."""
    payload: dict = {
        'format': 'htdt-video-measurements-1',
        'measurement_set_id': measurement_set.measurement_set_id,
        'measured_at_utc': measurement_set.measured_at_utc,
        'surface_entity_id': measurement_set.surface_entity_id,
        'meter': measurement_set.meter,
        'stimulus': measurement_set.stimulus.model_dump(mode='python'),
        'samples': [
            s.model_dump(mode='python') for s in measurement_set.samples
        ],
    }
    if measurement_set.meter_correction is not None:
        payload['meter_correction'] = (
            measurement_set.meter_correction.model_dump(mode='python')
        )
    if measurement_set.import_source is not None:
        payload['import_source'] = measurement_set.import_source
        payload['import_app_version'] = measurement_set.import_app_version
        payload['import_asset_sha256'] = (
            measurement_set.import_asset_sha256
        )
        payload['import_parser_id'] = measurement_set.import_parser_id
    if measurement_set.provenance:
        payload['provenance'] = [
            p.model_dump(mode='python') for p in measurement_set.provenance
        ]
    return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'


# ---------------------------------------------------------------------------
# HCFR GrayScaleSheet parsing
# ---------------------------------------------------------------------------

def _rows_of(data: bytes) -> list[list[str]]:
    text = data.decode('utf-8-sig', errors='strict')
    return [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(text), delimiter=';')
        if any(cell for cell in row)
    ]


def _float_of(cell: str) -> float | None:
    try:
        return float(cell)
    except (TypeError, ValueError):
        return None


def _parse_hcfr_grayscale(
    data: bytes,
) -> tuple[tuple[TristimulusSample, ...], tuple[str, ...]]:
    """``Measure;0;1;…`` header, then a level row (``IRE`` or a percent
    label), ``X``, ``Y``, ``Z`` rows (HCFR also writes ``R``/``G``/``B``,
    ``ColorTemp``, ``DeltaE`` — retained in warnings, not re-interpreted)."""
    rows = _rows_of(data)
    if len(rows) < 5:
        raise ValueError('グレースケールシートの行が不足しています')
    header = rows[0]
    if header[0].lower() != 'measure' or len(header) < 2:
        raise ValueError(
            '先頭行が Measure ヘッダーではありません（HCFR の '
            'GrayScaleSheet.csv 形式ではありません）'
        )
    count = len(header) - 1

    def _row(label_options: tuple[str, ...]) -> list[str] | None:
        for row in rows[1:]:
            if row[0].lower() in label_options:
                return row
        return None

    level_row = _row(('ire', '%', 'percent', 'percentage'))
    x_row = _row(('x',))
    y_row = _row(('y',))
    z_row = _row(('z',))
    if x_row is None or y_row is None or z_row is None:
        raise ValueError('X/Y/Z 行が見つかりません')
    warnings: list[str] = []
    if level_row is None:
        warnings.append(
            'レベル行（IRE/パーセント）が見つかりません — '
            '刺激レベルは順序から推定しました'
        )
    if len(x_row) < count + 1 or len(y_row) < count + 1 or len(z_row) < count + 1:
        raise ValueError('X/Y/Z 行の列数がヘッダーと一致しません')

    raw_levels: list[float | None] = []
    if level_row is not None:
        if len(level_row) < count + 1:
            warnings.append(
                'レベル行（IRE/パーセント）の列数が不足しています — '
                '刺激レベルは順序から推定しました'
            )
        else:
            raw_levels = [_float_of(c) for c in level_row[1 : count + 1]]
            if any(v is None for v in raw_levels):
                warnings.append(
                    '一部のレベル値が読み取れません — '
                    'その点の刺激レベルは順序から推定しました'
                )
    # HCFR writes IRE (0–100) or a percent label (also 0–100); the canonical
    # stimulus_level is normalized 0–1.
    scale = 100.0
    if raw_levels and all(
        v is not None and v <= 1.5 for v in raw_levels
    ):
        scale = 1.0

    samples: list[TristimulusSample] = []
    for i in range(count):
        x = _float_of(x_row[1 + i])
        y = _float_of(y_row[1 + i])
        z = _float_of(z_row[1 + i])
        if x is None or y is None or z is None:
            warnings.append(
                f'列 {i + 1} の XYZ が読み取れませんでした — スキップ'
            )
            continue
        if raw_levels and raw_levels[i] is not None:
            level_value = raw_levels[i]
            normalized = level_value / scale
        else:
            level_value = (i / (count - 1) * 100.0) if count > 1 else 100.0
            normalized = level_value / 100.0
        samples.append(
            TristimulusSample(
                stimulus_id=f'w{level_value:g}',
                stimulus_level=min(1.0, max(0.0, normalized)),
                x=x,
                y_luminance=y,
                z=z,
            )
        )
    if not samples:
        raise ValueError('読み取れる測定点がありません')
    return tuple(samples), tuple(warnings)


# ---------------------------------------------------------------------------
# HCFR PrimariesSheet parsing
# ---------------------------------------------------------------------------

_HCFR_PRIMARY_STIMULUS_IDS = (
    'red', 'green', 'blue', 'yellow', 'cyan', 'magenta'
)


def _parse_hcfr_primaries(
    data: bytes,
) -> tuple[tuple[TristimulusSample, ...], tuple[str, ...]]:
    """``Measure;Red;Green;Blue;Yellow;Cyan;Magenta`` header, then
    ``X``/``Y``/``Z`` rows (HCFR's ``R``/``G``/``B``/``DeltaE`` rows are
    noted in warnings, not re-interpreted)."""
    rows = _rows_of(data)
    if len(rows) < 4:
        raise ValueError('カラーポイントシートの行が不足しています')
    header = rows[0]
    if header[0].lower() != 'measure' or len(header) != 7:
        raise ValueError(
            '先頭行が Measure;Red;Green;Blue;Yellow;Cyan;Magenta '
            'ではありません（HCFR の PrimariesSheet.csv 形式ではありません）'
        )

    def _row(label: str) -> list[str] | None:
        for row in rows[1:]:
            if row[0].lower() == label:
                return row
        return None

    x_row = _row('x')
    y_row = _row('y')
    z_row = _row('z')
    if x_row is None or y_row is None or z_row is None:
        raise ValueError('X/Y/Z 行が見つかりません')

    warnings: list[str] = []
    samples: list[TristimulusSample] = []
    for i, stimulus_id in enumerate(_HCFR_PRIMARY_STIMULUS_IDS):
        x = _float_of(x_row[1 + i]) if len(x_row) > 1 + i else None
        y = _float_of(y_row[1 + i]) if len(y_row) > 1 + i else None
        z = _float_of(z_row[1 + i]) if len(z_row) > 1 + i else None
        if x is None or y is None or z is None:
            warnings.append(
                f'{stimulus_id} の XYZ が読み取れませんでした — スキップ'
            )
            continue
        samples.append(
            TristimulusSample(
                stimulus_id=stimulus_id,
                stimulus_level=1.0,
                x=x,
                y_luminance=y,
                z=z,
            )
        )
    if not samples:
        raise ValueError('読み取れる測定点がありません')
    return tuple(samples), tuple(warnings)


# ---------------------------------------------------------------------------
# HCFR GeneralSheet — metadata only, no samples.
# ---------------------------------------------------------------------------

def _parse_hcfr_general(data: bytes) -> dict[str, str]:
    rows = _rows_of(data)
    if len(rows) < 2 or rows[0][0].lower() != 'num':
        raise ValueError(
            'Num;Name;… ヘッダーではありません（HCFR の '
            'GeneralSheet.csv 形式ではありません）'
        )
    labels = [c.lower() for c in rows[0]]
    values = rows[1]
    return {
        labels[i]: values[i]
        for i in range(min(len(labels), len(values)))
    }


# ---------------------------------------------------------------------------
# The import entry itself.
# ---------------------------------------------------------------------------

def import_video_measurements(
    *,
    file_name: str,
    data: bytes,
    surface_entity_id: str,
    meter: str | None = None,
    measured_at_utc: str | None = None,
    stimulus: StimulusDefinition | None = None,
    meter_correction: ColorimeterCorrectionProfile | None = None,
    session_id: str | None = None,
    measurement_set_id: str | None = None,
) -> VideoImportResult:
    """Import one measurement file into a canonical measurement set.

    ``.chc`` and anything undetected return ``unsupported`` with a JA
    pointer at the documented interchange; malformed payloads return
    ``malformed`` with the parse reason. Never raises for bad input —
    the journey surface renders the failure state directly.
    """
    format_id = detect_video_import_format(file_name, data)
    source_sha256 = _hash_bytes(data)

    if format_id in ('hcfr_chc_binary', 'unknown'):
        return VideoImportResult(
            status='unsupported',
            file_name=file_name,
            format_id=format_id,
            failure=VideoImportFailure(
                format_id=format_id,
                file_name=file_name,
                reason=(
                    _CHC_INTERCHANGE_HINT
                    if format_id == 'hcfr_chc_binary'
                    else 'このファイルの形式を識別できませんでした。'
                    '対応形式: HTDT JSON、HCFR の '
                    '*.GrayScaleSheet.csv / *.PrimariesSheet.csv'
                ),
                interchange_hint=(
                    _CHC_INTERCHANGE_HINT
                    if format_id == 'hcfr_chc_binary'
                    else None
                ),
            ),
        )

    try:
        warns: tuple[str, ...] = ()
        json_payload: dict | None = None
        if format_id == 'htdt_video_measurements_json':
            json_payload = _parse_htdt_json(data)
            samples = tuple(
                TristimulusSample.model_validate(s)
                for s in json_payload['samples']
            )
            if json_payload.get('stimulus') is not None:
                stimulus = stimulus or StimulusDefinition.model_validate(
                    json_payload['stimulus']
                )
            meter = meter or json_payload.get('meter')
            if not meter:
                raise ValueError('測定器が指定されていません')
            set_id = (
                measurement_set_id
                or json_payload.get('measurement_set_id')
                or 'vms-' + source_sha256[:16]
            )
            parser_id = HTDT_JSON_PARSER_ID
        else:
            if not meter:
                raise ValueError('測定器が指定されていません')
            if format_id == 'hcfr_grayscale_csv':
                samples, warns = _parse_hcfr_grayscale(data)
                set_id = measurement_set_id or (
                    'vms-hcfr-gray-' + source_sha256[:16]
                )
                parser_id = HCFR_GRAYSCALE_PARSER_ID
            elif format_id == 'hcfr_primaries_csv':
                samples, warns = _parse_hcfr_primaries(data)
                set_id = measurement_set_id or (
                    'vms-hcfr-prim-' + source_sha256[:16]
                )
                parser_id = HCFR_PRIMARIES_PARSER_ID
            elif format_id == 'hcfr_general_csv':
                _parse_hcfr_general(data)  # validated for shape
                raise ValueError(
                    'このファイルは測定情報のみで、測定サンプルを含みません。'
                    'GrayScaleSheet.csv / PrimariesSheet.csv '
                    'を取り込んでください'
                )
            else:  # pragma: no cover — detection is exhaustive
                raise ValueError('未対応の形式です')
    except (ValueError, UnicodeDecodeError) as exc:
        return VideoImportResult(
            status='malformed',
            file_name=file_name,
            format_id=format_id,
            failure=VideoImportFailure(
                format_id=format_id,
                file_name=file_name,
                reason=f'ファイルの解釈に失敗しました: {exc}',
                interchange_hint=(
                    _CHC_INTERCHANGE_HINT
                    if format_id.startswith('hcfr_')
                    else None
                ),
            ),
        )

    try:
        if json_payload is not None:
            # A re-imported interchange restores every original field — the
            # batch record carries THIS import's provenance instead of
            # rewriting the set's history.
            measured_at_utc = measured_at_utc or json_payload.get(
                'measured_at_utc'
            )
            if json_payload.get('meter_correction') is not None:
                meter_correction = (
                    meter_correction
                    or ColorimeterCorrectionProfile.model_validate(
                        json_payload['meter_correction']
                    )
                )
            provenance = tuple(
                EquipmentDataProvenance.model_validate(p)
                for p in json_payload.get('provenance', ())
            )
            import_source = json_payload.get('import_source')
            import_app_version = json_payload.get('import_app_version')
            import_asset_sha256 = json_payload.get('import_asset_sha256')
            import_parser_id = json_payload.get('import_parser_id')
        else:
            provenance = (
                EquipmentDataProvenance(
                    evidence_kind='measured',
                    source_name=file_name,
                    source_version=parser_id,
                    source_reference=file_name,
                    source_sha256=source_sha256,
                ),
            )
            import_source = {
                'hcfr_grayscale_csv': 'hcfr-grayscale-csv',
                'hcfr_primaries_csv': 'hcfr-primaries-csv',
            }[format_id]
            import_app_version = 'htdt'
            import_asset_sha256 = source_sha256
            import_parser_id = parser_id

        measurement_set = build_video_color_measurement_set(
            measurement_set_id=set_id,
            measured_at_utc=measured_at_utc or _utc_now(),
            surface_entity_id=surface_entity_id,
            meter=meter,
            samples=samples,
            stimulus=stimulus,
            meter_correction=meter_correction,
            import_source=import_source,
            import_app_version=import_app_version,
            import_asset_sha256=import_asset_sha256,
            import_parser_id=import_parser_id,
            provenance=provenance,
        )
    except (ValueError, UnicodeDecodeError) as exc:
        # A payload whose rows parse but cannot assemble a valid set
        # (duplicate stimulus ids, partial provenance triple, ...) is a
        # bad input too — report malformed, never raise.
        return VideoImportResult(
            status='malformed',
            file_name=file_name,
            format_id=format_id,
            failure=VideoImportFailure(
                format_id=format_id,
                file_name=file_name,
                reason=f'ファイルの解釈に失敗しました: {exc}',
                interchange_hint=(
                    _CHC_INTERCHANGE_HINT
                    if format_id.startswith('hcfr_')
                    else None
                ),
            ),
        )
    batch = _build_import_batch(
        file_name=file_name,
        format_id=format_id,
        parser_id=parser_id,
        source_sha256=source_sha256,
        measurement_set=measurement_set,
        warnings=warns,
        session_id=session_id,
    )
    return VideoImportResult(
        status='imported',
        file_name=file_name,
        format_id=format_id,
        measurement_set=measurement_set,
        batch=batch,
        warnings=warns,
    )


def _parse_htdt_json(data: bytes) -> dict:
    payload = json.loads(data.decode('utf-8'))
    if not isinstance(payload, dict) or 'samples' not in payload:
        raise ValueError('htdt-video-measurements-1 の構造ではありません')
    if (
        not isinstance(payload['samples'], list)
        or not payload['samples']
    ):
        raise ValueError('samples が空です')
    return payload


def _build_import_batch(
    *,
    file_name: str,
    format_id: VideoImportFormat,
    parser_id: str,
    source_sha256: str,
    measurement_set: VideoColorMeasurementSet,
    warnings: tuple[str, ...],
    session_id: str | None,
) -> VideoMeasurementImport:
    probe = VideoMeasurementImport.model_construct(**canonicalize_payload(
        VideoMeasurementImport, dict(
            batch_id='',
            session_id=session_id,
            file_name=file_name,
            format_id=format_id,
            parser_id=parser_id,
            source_sha256=source_sha256,
            measurement_set_id=measurement_set.measurement_set_id,
            measurement_set_sha256=measurement_set.measurement_set_sha256,
            stimulus_count=len(measurement_set.samples),
            warnings=warnings,
            imported_at_utc=_utc_now(),
            batch_sha256='',
        ),
    ))
    digest = _hash(probe.semantic_payload())
    return VideoMeasurementImport(
        **probe.model_dump(mode='python', exclude={'batch_sha256', 'batch_id'}),
        batch_sha256=digest,
        batch_id='vib-' + digest[:24],
    )


def _hash_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


__all__ = [
    'VIDEO_IMPORT_FORMAT_LABELS',
    'VideoImportFailure',
    'VideoImportFormat',
    'VideoImportResult',
    'VideoMeasurementImport',
    'detect_video_import_format',
    'export_video_measurements',
    'import_video_measurements',
]
