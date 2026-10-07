"""Generic DSP export fallbacks (#838 slice A).

Issue #838 action 6: every correction surface must always offer generic
PEQ / biquad / FIR / manual-entry export paths for targets without a
qualified adapter. These renderers produce deterministic, documented
text formats plus the exact byte hash the operator installs — file truth
only, never a runtime claim (see ``cad_file_export_deployment`` for the
evidence chain each export feeds).

* PEQ export — parametric spec (type/Fc/gain/Q|BW) as portable text;
  the always-available path.
* Biquad export — normalized a0 biquad coefficient rows produced through
  the #679-family ``build_biquad_filter`` convention (RBJ cookbook,
  pole-validated). Bounded to the types that convention defines;
  everything else fails closed and falls back to PEQ/manual.
* FIR export — raw tap lists, explicitly tagged with their sample rate.
* Manual handoff — an operator instruction sheet for AVR-style manual
  entry; it asserts nothing about the device state.

Every renderer is pure text: deterministic bytes → deterministic hash →
sealable evidence.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import build_biquad_filter

GENERIC_PEQ_FORMAT_ID = 'htdt-generic-peq-txt'
GENERIC_BIQUAD_FORMAT_ID = 'htdt-generic-biquad-txt'
GENERIC_FIR_FORMAT_ID = 'htdt-generic-fir-txt'
MANUAL_HANDOFF_FORMAT_ID = 'htdt-manual-settings-txt'
GENERIC_EXPORTER_ID = 'htdt-export-generic'
GENERIC_EXPORTER_VERSION = '1'

MAX_FIR_TAPS = 65536

ExportFilterType = Literal[
    'peaking', 'low_shelf', 'high_shelf', 'low_pass', 'high_pass',
    'notch', 'band_pass', 'all_pass',
]

#: Filter types the normalized-biquad convention (#679) defines.
_BIQUAD_SUPPORTED: tuple[str, ...] = (
    'peaking', 'low_pass', 'high_pass', 'all_pass')


class GenericExportUnsupportedError(ValueError):
    """The request cannot be rendered in the chosen generic format —
    every unsupported item is listed; nothing is silently dropped."""

    def __init__(self, unsupported_items: tuple[str, ...]) -> None:
        self.unsupported_items = tuple(unsupported_items)
        super().__init__(
            'unsupported generic export parameters: '
            + '; '.join(self.unsupported_items))


class ExportFilterBand(BaseModel):
    """Target-agnostic normalized parametric band — the shared export
    vocabulary for APO, generic PEQ, biquad, and manual paths."""

    model_config = ConfigDict(frozen=True)

    filter_type: ExportFilterType
    frequency_hz: float = Field(gt=0.0)
    gain_db: float | None = None
    q: float | None = Field(default=None, gt=0.0)
    bandwidth_oct: float | None = Field(default=None, gt=0.0)
    enabled: bool = True

    @model_validator(mode='after')
    def _validate(self) -> 'ExportFilterBand':
        for value in (self.frequency_hz, self.gain_db, self.q,
                      self.bandwidth_oct):
            if value is not None and not isfinite(float(value)):
                raise ValueError('band parameters must be finite')
        if self.q is not None and self.bandwidth_oct is not None:
            raise ValueError(
                'a band pins either q or bandwidth_oct, never both')
        return self


class GenericExportChannel(BaseModel):
    """One channel in a generic export. ``channel_label`` is an opaque
    operator-facing name ('Front L', 'Sub 1', 'ALL')."""

    model_config = ConfigDict(frozen=True)

    channel_label: str = Field(min_length=1)
    preamp_db: float | None = None
    delay_s: float | None = Field(default=None, ge=0.0)
    bands: tuple[ExportFilterBand, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'GenericExportChannel':
        if not self.channel_label.strip() or '\n' in self.channel_label:
            raise ValueError('channel_label must be a single line')
        for value in (self.preamp_db, self.delay_s):
            if value is not None and not isfinite(float(value)):
                raise ValueError('channel values must be finite')
        return self


class GenericFirChannel(BaseModel):
    """One channel of raw FIR taps for the generic FIR export."""

    model_config = ConfigDict(frozen=True)

    channel_label: str = Field(min_length=1)
    sample_rate_hz: int = Field(gt=0)
    taps: tuple[float, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'GenericFirChannel':
        if not self.channel_label.strip() or '\n' in self.channel_label:
            raise ValueError('channel_label must be a single line')
        if len(self.taps) > MAX_FIR_TAPS:
            raise ValueError(f'fir taps exceed {MAX_FIR_TAPS}')
        if any(not isfinite(float(t)) for t in self.taps):
            raise ValueError('fir taps must be finite')
        return self


def _fmt(value: float) -> str:
    """Deterministic ASCII decimal for coefficient/parameter text."""
    text = f'{float(value):.12g}'
    if 'e' in text.lower() or text in ('inf', '-inf', 'nan'):
        raise ValueError(f'value {value!r} cannot be rendered')
    return text


def _header(format_id: str, purpose: str) -> list[str]:
    return [
        f'# HTDT generic export — {format_id}',
        f'# exporter={GENERIC_EXPORTER_ID} v{GENERIC_EXPORTER_VERSION}',
        f'# {purpose}',
        '# File truth is not runtime truth: installing this file does not',
        '# prove the target applies it. Re-measure to verify.',
    ]


def render_generic_peq_export(
    *,
    channels: tuple[GenericExportChannel, ...],
) -> str:
    """Portable parametric-EQ spec. One row per (channel, band); the
    column layout is documented in the header."""
    lines = _header(
        GENERIC_PEQ_FORMAT_ID,
        'parametric equalizer specification for manual/bulk entry')
    lines.append(
        '# columns: channel,preamp_db,delay_ms,band,type,'
        'frequency_hz,gain_db,q,bandwidth_oct,enabled')
    for channel in channels:
        if not channel.bands and channel.preamp_db is None \
                and channel.delay_s is None:
            lines.append(f'{channel.channel_label},,,,,,,,')
            continue
        if not channel.bands:
            lines.append(
                f'{channel.channel_label},'
                f'{"" if channel.preamp_db is None else _fmt(channel.preamp_db)},'
                f'{"" if channel.delay_s is None else _fmt(channel.delay_s * 1000.0)},'
                ',,,,,')
            continue
        for index, band in enumerate(channel.bands):
            if index == 0:
                prefix = (
                    f'{channel.channel_label},'
                    f'{"" if channel.preamp_db is None else _fmt(channel.preamp_db)},'
                    f'{"" if channel.delay_s is None else _fmt(channel.delay_s * 1000.0)},')
            else:
                prefix = f'{channel.channel_label},,,'
            lines.append(
                f'{prefix}{index},'
                f'{band.filter_type},'
                f'{_fmt(band.frequency_hz)},'
                f'{"" if band.gain_db is None else _fmt(band.gain_db)},'
                f'{"" if band.q is None else _fmt(band.q)},'
                f'{"" if band.bandwidth_oct is None else _fmt(band.bandwidth_oct)},'
                f'{"on" if band.enabled else "off"}')
    return '\n'.join(lines) + '\n'


def biquad_export_support_problems(
    channels: tuple[GenericExportChannel, ...],
    sample_rate_hz: int,
) -> tuple[str, ...]:
    """Every reason these channels cannot render as biquads — empty
    when the export is fully supported."""
    problems: list[str] = []
    if sample_rate_hz <= 0:
        problems.append(f'invalid sample rate {sample_rate_hz}')
        return tuple(problems)
    for channel in channels:
        for index, band in enumerate(channel.bands):
            where = f'channel {channel.channel_label} band[{index}]'
            if band.filter_type not in _BIQUAD_SUPPORTED:
                problems.append(
                    f'{where} {band.filter_type}: the normalized '
                    'biquad convention does not define this type — use '
                    'the PEQ or manual export')
                continue
            if band.q is None:
                # bandwidth_oct-pinned bands land here too (the model
                # forbids pinning both); the message covers both cases.
                problems.append(
                    f'{where} requires q for biquad export; '
                    'bandwidth_oct must be converted to q first')
                continue
            if band.frequency_hz >= sample_rate_hz / 2:
                problems.append(
                    f'{where} {band.frequency_hz} Hz is not below '
                    f'Nyquist ({sample_rate_hz / 2:g} Hz)')
            if band.filter_type != 'peaking' \
                    and band.gain_db is not None \
                    and abs(band.gain_db) > 1e-12:
                problems.append(
                    f'{where} {band.filter_type} carries a non-zero '
                    'gain; the biquad convention allows gain only on '
                    'peaking')
    return tuple(problems)


def render_generic_biquad_export(
    *,
    channels: tuple[GenericExportChannel, ...],
    sample_rate_hz: int,
) -> str:
    """Normalized a0 biquad coefficient rows (b0,b1,b2,a1,a2 with
    denominator 1+a1 z^-1+a2 z^-2) per channel band, via the validated
    #679 biquad convention. Fails closed on anything outside it."""
    problems = biquad_export_support_problems(channels, sample_rate_hz)
    if problems:
        raise GenericExportUnsupportedError(problems)
    lines = _header(
        GENERIC_BIQUAD_FORMAT_ID,
        f'normalized biquad coefficients, Fs={sample_rate_hz} Hz')
    lines.append(
        '# columns: channel,band,type,frequency_hz,gain_db,q,enabled,'
        'b0,b1,b2,a1,a2')
    lines.append(
        '# convention: a0_normalized; ordering b0,b1,b2,a1,a2; '
        'denominator=1+a1*z^-1+a2*z^-2')
    for channel in channels:
        for index, band in enumerate(channel.bands):
            biquad = build_biquad_filter(
                filter_id=f'{channel.channel_label}:{index}',
                filter_type=band.filter_type,  # type: ignore[arg-type]
                frequency_hz=band.frequency_hz,
                q=float(band.q),
                gain_db=band.gain_db or 0.0,
                sample_rate_hz=sample_rate_hz,
            )
            coeffs = ','.join(_fmt(c) for c in biquad.coefficients)
            lines.append(
                f'{channel.channel_label},{index},'
                f'{band.filter_type},{_fmt(band.frequency_hz)},'
                f'{_fmt(band.gain_db or 0.0)},{_fmt(band.q or 0.0)},'
                f'{"on" if band.enabled else "off"},{coeffs}')
    return '\n'.join(lines) + '\n'


def render_generic_fir_export(
    *,
    channels: tuple[GenericFirChannel, ...],
) -> str:
    """Raw FIR tap lists with an explicit per-channel sample rate."""
    lines = _header(
        GENERIC_FIR_FORMAT_ID,
        'raw FIR taps; column: channel,tap_index,coefficient')
    for channel in channels:
        lines.append(
            f'# channel={channel.channel_label} '
            f'Fs={channel.sample_rate_hz} taps={len(channel.taps)}')
        for index, tap in enumerate(channel.taps):
            lines.append(
                f'{channel.channel_label},{index},{_fmt(tap)}')
    return '\n'.join(lines) + '\n'


def render_manual_settings_handoff(
    *,
    target_name: str,
    channels: tuple[GenericExportChannel, ...],
    notes: str | None = None,
) -> str:
    """Operator instruction sheet for manual entry — AVR menus and
    similar. Asserts nothing about device state; verification still
    requires re-measurement."""
    lines = _header(
        MANUAL_HANDOFF_FORMAT_ID,
        f'manual settings entry sheet for {target_name}')
    lines.append(
        '# Enter these values on the target device by hand, then run a')
    lines.append(
        '# post-deployment measurement — manual entry is unverified by')
    lines.append('# construction.')
    for channel in channels:
        lines.append('')
        lines.append(f'[{channel.channel_label}]')
        if channel.preamp_db is not None:
            lines.append(
                f'  level/trim: {_fmt(channel.preamp_db)} dB')
        if channel.delay_s is not None:
            lines.append(
                f'  delay: {_fmt(channel.delay_s * 1000.0)} ms')
        for index, band in enumerate(channel.bands):
            state = '' if band.enabled else ' (OFF)'
            width = (
                f'Q {_fmt(band.q)}' if band.q is not None
                else (f'BW {_fmt(band.bandwidth_oct)} oct'
                      if band.bandwidth_oct is not None else ''))
            gain = (f' gain {_fmt(band.gain_db)} dB'
                    if band.gain_db is not None else '')
            lines.append(
                f'  EQ {index}: {band.filter_type} '
                f'{_fmt(band.frequency_hz)} Hz{gain} {width}'
                f'{state}'.rstrip())
    if notes:
        lines.append('')
        lines.append(f'Notes: {notes}')
    return '\n'.join(lines) + '\n'


__all__ = [
    'ExportFilterBand',
    'ExportFilterType',
    'GENERIC_BIQUAD_FORMAT_ID',
    'GENERIC_EXPORTER_ID',
    'GENERIC_EXPORTER_VERSION',
    'GENERIC_FIR_FORMAT_ID',
    'GENERIC_PEQ_FORMAT_ID',
    'GenericExportChannel',
    'GenericExportUnsupportedError',
    'GenericFirChannel',
    'MANUAL_HANDOFF_FORMAT_ID',
    'MAX_FIR_TAPS',
    'biquad_export_support_problems',
    'render_generic_biquad_export',
    'render_generic_fir_export',
    'render_generic_peq_export',
    'render_manual_settings_handoff',
]
