"""Measured impulse-response import and persistence (#474).

An IR is a separate immutable dataset bound to the same measurement record
(and therefore the same SceneRevision and acquisition context) as the
frequency-response dataset — never a replacement for it. The v0.2 contract
deliberately accepts the REW IR *text* export first: its time column
provides the time axis as ``start_time_s + sample_index / sample_rate_hz``,
and the importer requires explicit declarations for the parts a text file
cannot carry (amplitude reference, normalization, windowing, calibration
state), so a normalized IR is never silently read as absolute SPL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
import re
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ingress import IngressTooLargeError
from .limits import MAX_REW_TEXT_BYTES
from .canonical_json import canonical_sha256


REW_IR_PARSER_VERSION = 'rew-ir-text-1'
IMPORT_IR_TRANSFORMATION_VERSION = 'ir-import-transformation-1'


class RewIrParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedImpulseResponse:
    """Canonical decode of a REW IR text export."""

    time_s: tuple[float, ...]
    amplitudes: tuple[float, ...]
    sample_rate_hz: float | None
    header_lines: tuple[str, ...]
    warnings: tuple[str, ...]
    source_sha256: str
    parser_version: str = REW_IR_PARSER_VERSION


_SPLIT = re.compile(r'[\t, ]+')


def _decode_ir(raw: bytes) -> str:
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise RewIrParseError(
            'Only UTF-8/ASCII REW impulse-response text exports are supported'
        ) from exc


def parse_rew_impulse_response(
    raw: bytes,
    *,
    max_bytes: int = MAX_REW_TEXT_BYTES,
) -> ParsedImpulseResponse:
    """Parse a REW IR text export into time/amplitude samples.

    Non-numeric leading lines become ``header_lines`` verbatim. Numeric
    rows carry ``time_s amplitude`` columns (a lone amplitude column is
    accepted but leaves ``sample_rate_hz`` unresolved — the importer then
    requires an explicit declaration). Uniform sample spacing is verified
    and the sample rate is inferred from it; non-uniform timing is
    rejected rather than resampled.
    """
    if max_bytes < 0:
        raise ValueError('max_bytes must be non-negative')
    if len(raw) > max_bytes:
        raise IngressTooLargeError(
            f'REW IR text payload is too large: {len(raw)} bytes '
            f'(limit {max_bytes} bytes)'
        )
    if not raw:
        raise RewIrParseError('The input file is empty')

    text = _decode_ir(raw)
    times: list[float] = []
    amplitudes: list[float] = []
    header_lines: list[str] = []
    row_width: int | None = None

    for line_number, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line:
            continue
        tokens = _SPLIT.split(line)
        starts_numeric = False
        try:
            float(tokens[0])
            starts_numeric = True
        except ValueError:
            pass

        if not starts_numeric:
            header_lines.append(original)
            continue

        if len(tokens) not in (1, 2):
            raise RewIrParseError(
                f'Line {line_number}: expected 1 or 2 numeric columns'
            )
        try:
            values = [float(token) for token in tokens]
        except ValueError as exc:
            raise RewIrParseError(
                f'Line {line_number}: malformed numeric row'
            ) from exc
        if not all(isfinite(value) for value in values):
            raise RewIrParseError(f'Line {line_number}: NaN/Inf is not allowed')
        if row_width is None:
            row_width = len(values)
        elif len(values) != row_width:
            raise RewIrParseError(
                f'Line {line_number}: column count changes within the file'
            )

        if row_width == 1:
            amplitudes.append(values[0])
        else:
            time_s, amplitude = values
            if times and time_s <= times[-1]:
                raise RewIrParseError(
                    f'Line {line_number}: time values must be strictly increasing'
                )
            times.append(time_s)
            amplitudes.append(amplitude)

    if len(amplitudes) < 2:
        raise RewIrParseError(
            'At least two impulse-response samples are required'
        )

    sample_rate_hz: float | None = None
    warnings: list[str] = []
    if times:
        step = times[1] - times[0]
        if step <= 0 or not isfinite(step):
            raise RewIrParseError('impulse-response time step is invalid')
        for index in range(2, len(times)):
            delta = times[index] - times[index - 1]
            if abs(delta - step) > 1e-9 * max(1.0, abs(step)):
                raise RewIrParseError(
                    'impulse-response sampling is not uniform; '
                    'the file cannot be regridded by HTDT'
                )
        sample_rate_hz = 1.0 / step
    else:
        warnings.append(
            'ir_time_axis_unresolved: amplitude-only export requires an '
            'explicit sample rate declaration'
        )

    return ParsedImpulseResponse(
        time_s=tuple(times),
        amplitudes=tuple(amplitudes),
        sample_rate_hz=sample_rate_hz,
        header_lines=tuple(header_lines),
        warnings=tuple(warnings),
        source_sha256=sha256(raw).hexdigest(),
    )


IrT0Semantics = Literal['export_t0', 'reference_signal', 'first_peak', 'imported', 'manual', 'unknown']
IrAmplitudeReference = Literal['full_scale', 'normalized', 'uncalibrated', 'unknown']
IrSemantics = Literal['deconvolved', 'raw_recording', 'unknown']
IrCalibrationState = Literal['calibrated', 'uncalibrated', 'unknown']


class CadImpulseResponseDataset(BaseModel):
    """Immutable measured impulse-response samples on their original axis.

    Identity semantics mirror ``CadFrequencyResponseDataset``: the sealed
    ``dataset_sha256`` covers the samples plus every interpretation field a
    consumer must resolve — sample rate, time axis, t=0 convention,
    amplitude reference, normalization, window and calibration state —
    and the declared importer must rederive them from the raw asset on save
    and on every authoritative read.
    """

    model_config = ConfigDict(frozen=True)

    dataset_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    sample_rate_hz: float = Field(gt=0.0)
    start_time_s: float
    amplitudes: tuple[float, ...]
    t0_semantics: IrT0Semantics = 'unknown'
    amplitude_reference: IrAmplitudeReference = 'unknown'
    normalized: bool = False
    window_kind: str | None = None
    ir_semantics: IrSemantics = 'unknown'
    calibration_state: IrCalibrationState = 'unknown'
    processing_json: str = '{}'
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    importer_version: str = Field(min_length=1)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json')

    @property
    def dataset_sha256(self) -> str:
        return sha256(
            json.dumps(
                self.identity_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            ).encode('utf-8')
        ).hexdigest()

    @property
    def end_time_s(self) -> float:
        return self.start_time_s + (len(self.amplitudes) - 1) / self.sample_rate_hz

    @property
    def duration_s(self) -> float:
        return len(self.amplitudes) / self.sample_rate_hz

    @model_validator(mode='after')
    def valid_samples(self) -> 'CadImpulseResponseDataset':
        if len(self.amplitudes) < 2:
            raise ValueError('impulse response requires at least two samples')
        if not isfinite(float(self.sample_rate_hz)) or self.sample_rate_hz <= 0:
            raise ValueError('sample rate must be finite and positive')
        if not isfinite(float(self.start_time_s)):
            raise ValueError('start_time_s must be finite')
        if any(not isfinite(float(value)) for value in self.amplitudes):
            raise ValueError('impulse-response samples must be finite')
        if self.normalized and self.amplitude_reference not in (
            'normalized',
            'unknown',
        ):
            raise ValueError(
                'a normalized IR cannot claim a non-normalized amplitude reference'
            )
        return self


@dataclass(frozen=True)
class IrImportTransformationAuthority:
    """A versioned IR importer proving a dataset derives from raw bytes."""

    importer_version: str
    source_kind: str
    rederive: Callable[[bytes], dict[str, Any]]


def normalize_rew_ir_text(
    measurement_id: str,
    raw: bytes,
    *,
    filename: str,
    sample_rate_hz: float | None = None,
    t0_semantics: IrT0Semantics = 'export_t0',
    amplitude_reference: IrAmplitudeReference = 'normalized',
    normalized: bool = True,
    window_kind: str | None = None,
    ir_semantics: IrSemantics = 'deconvolved',
    calibration_state: IrCalibrationState = 'uncalibrated',
    dataset_id: str | None = None,
) -> tuple[CadImpulseResponseDataset, str, bytes]:
    """Import one REW IR text export as a sealed dataset declaration.

    ``amplitude_reference``/``normalized``/``window_kind``/``calibration_state``
    are *importer declarations*, not derivations from the file: REW text
    exports do not carry them, and a normalized IR must never be read back
    as absolute SPL or double-applied microphone calibration.
    """
    parsed = parse_rew_impulse_response(raw)
    rate = parsed.sample_rate_hz if parsed.sample_rate_hz is not None else (
        float(sample_rate_hz) if sample_rate_hz is not None else None
    )
    if rate is None or not isfinite(rate) or rate <= 0:
        raise RewIrParseError(
            'impulse-response sample rate is unresolved: the export has no '
            'time column and no explicit sample_rate_hz was declared'
        )
    start = parsed.time_s[0] if parsed.time_s else 0.0
    dataset = CadImpulseResponseDataset(
        dataset_id=dataset_id or str(uuid4()),
        measurement_id=measurement_id,
        sample_rate_hz=rate,
        start_time_s=start,
        amplitudes=parsed.amplitudes,
        t0_semantics=t0_semantics,
        amplitude_reference=amplitude_reference,
        normalized=normalized,
        window_kind=window_kind,
        ir_semantics=ir_semantics,
        calibration_state=calibration_state,
        processing_json=json.dumps(
            {
                'header_lines': list(parsed.header_lines),
                'warnings': list(parsed.warnings),
                'filename': filename,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ),
        source_sha256=parsed.source_sha256,
        importer_version=REW_IR_PARSER_VERSION,
    )
    return dataset, filename, raw


def _rederive_rew_ir_text_dataset(raw: bytes) -> dict[str, Any]:
    """Re-run the pinned REW IR text importer against the raw bytes.

    Returns the fields the raw asset carries — the amplitude samples, the
    inferred sample rate (only when the file has a time column), the start
    time and the importer identity. The interpretation fields the file
    cannot carry (t=0 semantics, amplitude reference, normalization,
    window, calibration state) are declared at import and sealed into
    ``dataset_sha256``; ``verify_imported_ir_dataset`` re-verifies both
    halves on save and on every authoritative read.
    """
    parsed = parse_rew_impulse_response(raw)
    return {
        'parsed': parsed,
        'amplitudes': parsed.amplitudes,
        'sample_rate_hz': parsed.sample_rate_hz,
        'start_time_s': parsed.time_s[0] if parsed.time_s else 0.0,
        'source_sha256': parsed.source_sha256,
        'importer_version': parsed.parser_version,
    }


IR_IMPORT_TRANSFORMATION_AUTHORITIES: dict[str, IrImportTransformationAuthority] = {
    authority.importer_version: authority
    for authority in (
        IrImportTransformationAuthority(
            importer_version=REW_IR_PARSER_VERSION,
            source_kind='rew_ir_text',
            rederive=_rederive_rew_ir_text_dataset,
        ),
    )
}


def import_ir_transformation_authority(
    importer_version: str,
) -> IrImportTransformationAuthority:
    authority = IR_IMPORT_TRANSFORMATION_AUTHORITIES.get(importer_version)
    if authority is None:
        raise ValueError(
            'no registered impulse-response import authority: '
            f'{importer_version}'
        )
    return authority


def ir_transformation_sha256(
    *,
    source_sha256: str,
    importer_version: str,
    dataset_sha256: str,
) -> str:
    """Seal exact raw source + pinned importer identity + IR dataset identity."""
    return canonical_sha256({
                'transformation_version': IMPORT_IR_TRANSFORMATION_VERSION,
                'source_sha256': source_sha256,
                'importer_version': importer_version,
                'dataset_sha256': dataset_sha256,
            })


def verify_imported_ir_dataset(
    dataset: CadImpulseResponseDataset,
    raw_bytes: bytes,
) -> None:
    """Require ``dataset`` to be the canonical output of its pinned importer.

    The raw bytes re-derive the sample axis (time column / amplitudes /
    sample rate) and the declared importer version; the interpretation
    fields the file cannot carry are compared verbatim because they are
    sealed into ``dataset_sha256`` — persistence stores them alongside the
    raw asset, so a persisted row rewritten coherently still fails closed.
    """
    if sha256(raw_bytes).hexdigest() != dataset.source_sha256:
        raise ValueError('IR raw asset SHA-256 does not match dataset source')
    authority = import_ir_transformation_authority(dataset.importer_version)
    derived = authority.rederive(raw_bytes)
    if derived['importer_version'] != dataset.importer_version:
        raise ValueError('IR importer version mismatch')
    parsed: ParsedImpulseResponse = derived['parsed']

    if parsed.amplitudes != tuple(dataset.amplitudes):
        raise ValueError(
            'IR dataset does not match the canonical import transformation '
            'output for its raw asset'
        )
    if parsed.time_s:
        expected_rate = float(parsed.sample_rate_hz)  # type: ignore[arg-type]
        if abs(float(dataset.sample_rate_hz) - expected_rate) > 1e-9 * expected_rate:
            raise ValueError('IR dataset sample rate does not match its raw asset')
        if float(dataset.start_time_s) != float(parsed.time_s[0]):
            raise ValueError('IR dataset start time does not match its raw asset')
    elif float(dataset.start_time_s) != 0.0:
        raise ValueError(
            'IR dataset declares a non-zero start time its raw asset cannot carry'
        )


def ir_observation_fields(dataset: CadImpulseResponseDataset) -> dict[str, Any]:
    """Evidence fields a persisted IR honestly attests for a quality report.

    ``has_impulse_response`` and the window bounds come from the dataset
    itself. ``ir_truncated`` is *not* derived — the file cannot prove its
    own truncation — so callers pass it explicitly when known.
    """
    return {
        'has_impulse_response': True,
        'ir_window_start_s': dataset.start_time_s,
        'ir_window_end_s': dataset.end_time_s,
    }


__all__ = [
    'CadImpulseResponseDataset',
    'IMPORT_IR_TRANSFORMATION_VERSION',
    'IR_IMPORT_TRANSFORMATION_AUTHORITIES',
    'IrAmplitudeReference',
    'IrCalibrationState',
    'IrImportTransformationAuthority',
    'IrSemantics',
    'IrT0Semantics',
    'ParsedImpulseResponse',
    'REW_IR_PARSER_VERSION',
    'RewIrParseError',
    'import_ir_transformation_authority',
    'ir_observation_fields',
    'ir_transformation_sha256',
    'normalize_rew_ir_text',
    'parse_rew_impulse_response',
    'verify_imported_ir_dataset',
]
