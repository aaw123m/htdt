"""Bounded Equalizer APO config renderer + file-level readback (#838).

Issue #838 §3: Equalizer APO's own documentation states unsupported
command names or nonconforming lines may be silently ignored — a written
file is never a verified deployment. This module therefore renders only a
bounded, documented subset (``Device``, ``Channel``, ``Preamp``,
``Delay``, ``Filter n:`` peaking/shelves/passes/notch/band/all-pass) and
*round-trips* the rendered text through the #808 importer
(``build_equalizer_apo_artifact``) to prove the file parses back to the
requested semantics. Even a matched round-trip is file-level evidence
only — the runtime ceiling stays ``runtime_not_attested`` until
post-deployment measurement binds.

Unsupported parameters fail closed: the renderer collects every
unsupported item and raises ``ApoExportUnsupportedError`` rather than
silently dropping a filter the way Equalizer APO would.
"""

from __future__ import annotations

from math import isfinite, isclose
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_external_calibration import build_equalizer_apo_artifact
from .cad_generic_dsp_export import ExportFilterBand

APO_EXPORTER_ID = 'htdt-export-equalizer-apo'
APO_EXPORTER_VERSION = '1'
APO_EXPORT_FORMAT_ID = 'equalizer-apo-config-txt'

# Renderer-side filter mnemonics, restricted to the exact parametric
# forms the importer normalizes (#808 _FILTER_TYPE_MAP). The parametrized
# mnemonic is always rendered (LPQ/LSC/HSC carry Q; LP/LS/HS without Q
# normalize to the same imported fields).
_MNEMONIC: dict[str, str] = {
    'peaking': 'PK',
    'low_shelf': 'LSC',
    'high_shelf': 'HSC',
    'low_pass': 'LPQ',
    'high_pass': 'HPQ',
    'notch': 'NO',
    'band_pass': 'BP',
    'all_pass': 'AP',
}

# Conservative value bounds — outside these the rendered APO semantics
# are not trustworthy (APO itself would clamp or ignore).
_MAX_ABS_GAIN_DB = 40.0
_MIN_Q = 0.05
_MAX_Q = 200.0
_MIN_FREQ_HZ = 1.0
_MAX_FREQ_HZ = 99999.0
_MAX_ABS_PREAMP_DB = 60.0
_MAX_DELAY_S = 1.0


class ApoExportUnsupportedError(ValueError):
    """The request contains settings the bounded renderer cannot honor
    — surfaced in full, never silently dropped."""

    def __init__(self, unsupported_items: tuple[str, ...]) -> None:
        self.unsupported_items = tuple(unsupported_items)
        super().__init__(
            'unsupported Equalizer APO export parameters: '
            + '; '.join(self.unsupported_items))


ApoFilterType = Literal[
    'peaking', 'low_shelf', 'high_shelf', 'low_pass', 'high_pass',
    'notch', 'band_pass', 'all_pass',
]

# The normalized band vocabulary lives in cad_generic_dsp_export;
# ApoExportBand is the same shape under the APO-facing name.
ApoExportBand = ExportFilterBand


def _fmt(value: float) -> str:
    """Deterministic ASCII decimal the APO grammar accepts. Rejects
    exponent forms — '1e+05' is not part of the supported subset."""
    text = f'{float(value):.10g}'
    if 'e' in text.lower() or text in ('inf', '-inf', 'nan'):
        raise ValueError(f'value {value!r} cannot be rendered')
    return text


def apo_band_support_problems(band: ExportFilterBand) -> tuple[str, ...]:
    """Why a band cannot render in the bounded APO subset — empty when
    supported."""
    problems: list[str] = []
    if not (_MIN_FREQ_HZ <= band.frequency_hz <= _MAX_FREQ_HZ):
        problems.append(
            f'frequency {band.frequency_hz} Hz outside '
            f'[{_MIN_FREQ_HZ},{_MAX_FREQ_HZ}]')
    if band.gain_db is not None \
            and abs(band.gain_db) > _MAX_ABS_GAIN_DB:
        problems.append(
            f'gain {band.gain_db} dB exceeds ±{_MAX_ABS_GAIN_DB} dB')
    if band.q is not None and not (_MIN_Q <= band.q <= _MAX_Q):
        problems.append(f'Q {band.q} outside [{_MIN_Q},{_MAX_Q}]')

    if band.filter_type == 'peaking':
        if band.gain_db is None:
            problems.append('peaking requires gain_db')
        if band.q is None and band.bandwidth_oct is None:
            problems.append('peaking requires q or bandwidth_oct')
    elif band.filter_type in ('low_shelf', 'high_shelf'):
        if band.gain_db is None:
            problems.append(f'{band.filter_type} requires gain_db')
        if band.bandwidth_oct is not None:
            problems.append(
                f'{band.filter_type} supports q, not bandwidth_oct')
    elif band.filter_type in ('low_pass', 'high_pass'):
        if band.gain_db is not None:
            problems.append(
                f'{band.filter_type} takes no gain_db in the '
                'bounded subset')
        if band.bandwidth_oct is not None:
            problems.append(
                f'{band.filter_type} supports q, not bandwidth_oct')
    elif band.filter_type == 'notch':
        if band.gain_db is None:
            problems.append('notch takes no gain_db')
        if band.q is None and band.bandwidth_oct is None:
            problems.append('notch requires q or bandwidth_oct')
    elif band.filter_type == 'band_pass':
        if band.gain_db is None:
            problems.append('band_pass takes no gain_db')
        if band.q is None and band.bandwidth_oct is None:
            problems.append('band_pass requires q or bandwidth_oct')
    elif band.filter_type == 'all_pass':
        if band.gain_db is not None:
            problems.append('all_pass takes no gain_db')
        if band.q is None:
            problems.append('all_pass requires q')
        if band.bandwidth_oct is not None:
            problems.append('all_pass supports q, not bandwidth_oct')
    return tuple(problems)


class ApoExportChannel(BaseModel):
    """Settings for one APO ``Channel:`` scope. ``channel_labels`` are
    raw APO labels ('L', 'R', 'SUB', ...); an empty tuple renders the
    global ``Channel: ALL`` scope."""

    model_config = ConfigDict(frozen=True)

    channel_labels: tuple[str, ...] = ()
    preamp_db: float | None = None
    delay_s: float | None = Field(default=None, ge=0.0)
    bands: tuple[ApoExportBand, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'ApoExportChannel':
        for label in self.channel_labels:
            if not label or not label.strip() or ':' in label \
                    or '\n' in label:
                raise ValueError('channel labels must be plain APO '
                                 'labels')
        if self.preamp_db is not None and not isfinite(self.preamp_db):
            raise ValueError('channel preamp must be finite')
        if self.delay_s is not None and not isfinite(self.delay_s):
            raise ValueError('channel delay must be finite')
        return self


def render_equalizer_apo_config(
    *,
    channels: tuple[ApoExportChannel, ...],
    global_preamp_db: float | None = None,
    device: str | None = None,
) -> str:
    """Render the bounded APO subset. Raises
    :class:`ApoExportUnsupportedError` listing every unsupported item —
    never renders a partial truth."""
    unsupported: list[str] = []
    if global_preamp_db is not None:
        if not isfinite(global_preamp_db) \
                or abs(global_preamp_db) > _MAX_ABS_PREAMP_DB:
            unsupported.append(
                f'global preamp {global_preamp_db} dB outside '
                f'±{_MAX_ABS_PREAMP_DB} dB')
    if device is not None and (':' in device or '\n' in device
                               or not device.strip()):
        unsupported.append('device name must be a single plain line')
    for index, channel in enumerate(channels):
        scope = ','.join(channel.channel_labels) or 'ALL'
        if channel.preamp_db is not None \
                and abs(channel.preamp_db) > _MAX_ABS_PREAMP_DB:
            unsupported.append(
                f'channel[{index}] {scope} preamp outside '
                f'±{_MAX_ABS_PREAMP_DB} dB')
        if channel.delay_s is not None \
                and channel.delay_s > _MAX_DELAY_S:
            unsupported.append(
                f'channel[{index}] {scope} delay {channel.delay_s} s '
                f'exceeds {_MAX_DELAY_S} s')
        for band_index, band in enumerate(channel.bands):
            for problem in apo_band_support_problems(band):
                unsupported.append(
                    f'channel[{index}] {scope} band[{band_index}] '
                    f'{band.filter_type}: {problem}')
    if unsupported:
        raise ApoExportUnsupportedError(tuple(unsupported))

    lines = [
        '# Generated by HTDT — bounded Equalizer APO subset.',
        f'# renderer={APO_EXPORTER_ID} v{APO_EXPORTER_VERSION};',
        '# file truth != runtime truth — verify by re-measurement.',
    ]
    if device is not None:
        lines.append(f'Device: {device.strip()}')
    if global_preamp_db is not None:
        lines.append(f'Preamp: {_fmt(global_preamp_db)} dB')

    filter_index = 0
    for channel in channels:
        labels = channel.channel_labels
        lines.append('Channel: ' + (' '.join(labels) if labels else 'ALL'))
        if channel.preamp_db is not None:
            lines.append(f'Preamp: {_fmt(channel.preamp_db)} dB')
        if channel.delay_s is not None:
            lines.append(f'Delay: {_fmt(channel.delay_s * 1000.0)} ms')
        for band in channel.bands:
            filter_index += 1
            parts = [
                f'Filter {filter_index}:',
                'ON' if band.enabled else 'OFF',
                _MNEMONIC[band.filter_type],
                f'Fc {_fmt(band.frequency_hz)} Hz',
            ]
            if band.gain_db is not None:
                parts.append(f'Gain {_fmt(band.gain_db)} dB')
            if band.q is not None:
                parts.append(f'Q {_fmt(band.q)}')
            if band.bandwidth_oct is not None:
                parts.append(f'BW Oct {_fmt(band.bandwidth_oct)}')
            lines.append(' '.join(parts))
    return '\n'.join(lines) + '\n'


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)


def verify_exported_apo_config(
    rendered_text: str,
    *,
    channels: tuple[ApoExportChannel, ...],
    global_preamp_db: float | None = None,
    device: str | None = None,
    imported_at_utc: str = '2026-01-01T00:00:00Z',
) -> tuple[Literal['matched', 'mismatch'], tuple[str, ...]]:
    """File-level readback: re-parse the rendered text through the #808
    importer and compare normalized semantics against the request.

    ``matched`` proves only that the file *parses back* to the requested
    settings — never that the runtime applies them. A mismatch reports
    every divergent item.
    """
    artifact = build_equalizer_apo_artifact(
        rendered_text.encode('utf-8'),
        source_filename='htdt-rendered-apo.txt',
        imported_at_utc=imported_at_utc,
        artifact_id='imported-calibration:apo-export-readback',
        channel_map={label: label for channel in channels
                     for label in channel.channel_labels}
        | ({'ALL': 'ALL'} if any(not c.channel_labels for c in channels)
           else {}),
    )
    problems: list[str] = []
    for section in artifact.opaque_sections:
        if section.kind == 'device_scope':
            continue  # Device:/Stage: lines are recorded opaque by design
        problems.append(
            f'opaque section line {section.line_number}: '
            f'{section.reason}')
    if (device or None) != (artifact.device_context or None):
        problems.append(
            f'device context {artifact.device_context!r} != '
            f'requested {device!r}')
    # A Preamp under `Channel: ALL` (empty scope) accumulates into the
    # imported *global* preamp — Equalizer APO semantics — so the expected
    # global is the explicit global plus every ALL-scope channel preamp.
    expected_global = global_preamp_db or 0.0
    for channel in channels:
        if not channel.channel_labels and channel.preamp_db is not None:
            expected_global += channel.preamp_db
    if global_preamp_db is None \
            and all(c.preamp_db is None for c in channels
                    if not c.channel_labels):
        expected_global = None  # nothing global was requested
    if not _close(expected_global, artifact.global_preamp_db):
        problems.append(
            f'global preamp {artifact.global_preamp_db} != '
            f'requested {expected_global}')

    imported = {c.channel_label: c for c in artifact.channels}
    expected_labels = [
        label for channel in channels
        for label in (channel.channel_labels or ('ALL',))]
    for label in expected_labels:
        imported_channel = imported.get(label)
        expected_channel = next(
            c for c in channels
            if label in (c.channel_labels or ('ALL',)))
        if imported_channel is None:
            problems.append(f'channel {label} missing from parsed file')
            continue
        # For the ALL scope the preamp lands in global_preamp (compared
        # above), not in the channel bucket — skip it here.
        if expected_channel.channel_labels \
                and not _close(expected_channel.preamp_db,
                               imported_channel.preamp_db):
            problems.append(
                f'channel {label} preamp {imported_channel.preamp_db} '
                f'!= {expected_channel.preamp_db}')
        if not _close(expected_channel.delay_s,
                      imported_channel.delay_s):
            problems.append(
                f'channel {label} delay {imported_channel.delay_s} '
                f'!= {expected_channel.delay_s}')
        if len(imported_channel.peq) != len(expected_channel.bands):
            problems.append(
                f'channel {label} band count '
                f'{len(imported_channel.peq)} != '
                f'{len(expected_channel.bands)}')
            continue
        for band_index, (expected_band, imported_band) in enumerate(
                zip(expected_channel.bands, imported_channel.peq)):
            if imported_band.filter_type != expected_band.filter_type:
                problems.append(
                    f'channel {label} band[{band_index}] type '
                    f'{imported_band.filter_type} != '
                    f'{expected_band.filter_type}')
            if imported_band.enabled != expected_band.enabled:
                problems.append(
                    f'channel {label} band[{band_index}] enabled '
                    f'{imported_band.enabled} != '
                    f'{expected_band.enabled}')
            for field in ('frequency_hz', 'gain_db', 'q',
                          'bandwidth_oct'):
                expected_v = getattr(expected_band, field)
                imported_v = getattr(imported_band, field)
                if not _close(expected_v, imported_v):
                    problems.append(
                        f'channel {label} band[{band_index}] {field} '
                        f'{imported_v} != {expected_v}')
    extra = set(imported) - set(expected_labels)
    if extra:
        problems.append(f'parsed file has unexpected channels: '
                        + ','.join(sorted(extra)))
    return (('mismatch', tuple(problems)) if problems
            else ('matched', ()))


__all__ = [
    'APO_EXPORTER_ID',
    'APO_EXPORTER_VERSION',
    'APO_EXPORT_FORMAT_ID',
    'ApoExportBand',
    'ApoExportChannel',
    'ApoExportUnsupportedError',
    'ApoFilterType',
    'apo_band_support_problems',
    'render_equalizer_apo_config',
    'verify_exported_apo_config',
]
