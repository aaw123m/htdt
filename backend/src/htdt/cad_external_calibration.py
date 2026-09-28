"""External calibration artifact import (#808 ECI10).

An ``ImportedCalibrationArtifact`` preserves a third-party DSP /
calibration configuration file as immutable evidence: exact source bytes
hash, producer/format identity, normalized supported settings, retained
opaque sections and parse diagnostics. Import is never proof that
settings are active on hardware — the artifact state is always
``imported_configuration``, kept visibly separate from HTDT
``CalibrationPlan`` (requested), ``CadCalibrationExportSnapshot``
(materialized), read-back observed state and acoustic verification.

ECI10 first slice: a bounded Equalizer APO text-config grammar
(``Preamp``, ``Channel``, common ``Filter`` forms, ``Delay``, ``Device``
scope, ``Include`` with dependency closure and cycle detection). Unknown
or unsupported commands are surfaced explicitly as opaque sections —
Equalizer APO itself silently ignores unknown commands; HTDT must not
mirror that.
"""

from __future__ import annotations

import re
from hashlib import sha256
from math import isfinite
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import CadCalibrationExportSnapshot
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload


_EQUALIZER_APO_IMPORTER_ID = 'htdt-import-equalizer-apo'
_EQUALIZER_APO_IMPORTER_VERSION = '1'
_EQUALIZER_APO_FORMAT_ID = 'equalizer-apo-config-txt'

_ARTIFACT_PREFIX = 'imported-calibration:'

# Equalizer APO filter mnemonics whose exact parametric semantics are
# documented in the configuration reference. Anything else is retained
# as an opaque section instead of being guessed into a generic PEQ.
_FILTER_TYPE_MAP: dict[str, str] = {
    'PK': 'peaking',
    'LP': 'low_pass',
    'LPQ': 'low_pass',
    'HP': 'high_pass',
    'HPQ': 'high_pass',
    'LS': 'low_shelf',
    'LSC': 'low_shelf',
    'HS': 'high_shelf',
    'HSC': 'high_shelf',
    'NO': 'notch',
    'BP': 'band_pass',
    'AP': 'all_pass',
}

_NUMBER = r'-?\d+(?:\.\d+)?'






class ImportedFilterBand(BaseModel):
    """One normalized parametric filter band imported from an external
    configuration. Exact type/frequency/gain/Q (or bandwidth) semantics
    are preserved; the source text stays attached for audit."""

    model_config = ConfigDict(frozen=True)

    band_index: int = Field(ge=0)
    filter_type: Literal[
        'peaking',
        'low_pass',
        'high_pass',
        'low_shelf',
        'high_shelf',
        'notch',
        'band_pass',
        'all_pass',
    ]
    enabled: bool = True
    frequency_hz: float | None = Field(default=None, gt=0.0)
    gain_db: float | None = None
    q: float | None = Field(default=None, gt=0.0)
    bandwidth_oct: float | None = Field(default=None, gt=0.0)
    raw_text: str = Field(min_length=1)

    @model_validator(mode='after')
    def finite_band(self) -> 'ImportedFilterBand':
        for value in (self.frequency_hz, self.gain_db, self.q, self.bandwidth_oct):
            if value is not None and not isfinite(float(value)):
                raise ValueError('imported filter parameters must be finite')
        return self


class ChannelMappingEntry(BaseModel):
    """Explicit binding of one external channel token to an HTDT channel.
    An unmapped token stays unmapped — order/display position is never
    used to guess a binding."""

    model_config = ConfigDict(frozen=True)

    channel_label: str = Field(min_length=1)
    htdt_channel_id: str | None = None
    state: Literal['mapped', 'unmapped', 'ambiguous']


class ImportedChannelSettings(BaseModel):
    """Settings attributed to one external channel label."""

    model_config = ConfigDict(frozen=True)

    channel_label: str = Field(min_length=1)
    preamp_db: float | None = None
    delay_s: float | None = Field(default=None, ge=0.0)
    peq: tuple[ImportedFilterBand, ...] = ()


class OpaqueArtifactSection(BaseModel):
    """A command/section whose semantics HTDT does not interpret. It is
    retained verbatim — unknown commands are surfaced, never silently
    dropped (unlike Equalizer APO itself)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[
        'unsupported_command',
        'unsupported_filter_type',
        'malformed_line',
        'unresolved_include',
        'device_scope',
        'external_file_reference',
        'conditional_block',
    ]
    raw_text: str = Field(min_length=1)
    line_number: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1)


class IncludeDependency(BaseModel):
    """One resolved or unresolved ``Include:`` directive."""

    model_config = ConfigDict(frozen=True)

    include_path: str = Field(min_length=1)
    resolved: bool
    source_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    diagnostics: tuple[str, ...] = ()


class ImportedCalibrationArtifact(BaseModel):
    """Immutable evidence of one imported external calibration
    configuration (#808). State is always ``imported_configuration`` —
    never applied/attested/observed/verified truth."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    artifact_id: str = Field(min_length=1)
    state: Literal['imported_configuration'] = 'imported_configuration'
    producer_tool: str = Field(min_length=1)
    format_id: str = Field(min_length=1)
    format_version: str = Field(min_length=1)
    importer_id: str = Field(min_length=1)
    importer_version: str = Field(min_length=1)
    source_filename: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    imported_at_utc: str = Field(min_length=1)
    device_context: str | None = None
    global_preamp_db: float | None = None
    channel_mapping: tuple[ChannelMappingEntry, ...] = ()
    channels: tuple[ImportedChannelSettings, ...] = ()
    opaque_sections: tuple[OpaqueArtifactSection, ...] = ()
    include_dependencies: tuple[IncludeDependency, ...] = ()
    #: Other external files a configuration depends on (convolution
    #: impulse responses, etc.) — tracked like includes, never applied.
    file_dependencies: tuple[IncludeDependency, ...] = ()
    diagnostics: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_artifact(self) -> 'ImportedCalibrationArtifact':
        if not self.artifact_id.startswith(_ARTIFACT_PREFIX):
            raise ValueError(
                'artifact id must use imported-calibration: prefix'
            )
        if self.global_preamp_db is not None and not isfinite(
            float(self.global_preamp_db)
        ):
            raise ValueError('global preamp must be finite')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('imported artifact semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'artifact_id': self.artifact_id,
            'state': self.state,
            'producer_tool': self.producer_tool,
            'format_id': self.format_id,
            'format_version': self.format_version,
            'importer_id': self.importer_id,
            'importer_version': self.importer_version,
            'source_filename': self.source_filename,
            'source_sha256': self.source_sha256,
            'imported_at_utc': self.imported_at_utc,
            'device_context': self.device_context,
            'global_preamp_db': self.global_preamp_db,
            'channel_mapping': [
                item.model_dump(mode='json') for item in self.channel_mapping
            ],
            'channels': [
                item.model_dump(mode='json') for item in self.channels
            ],
            'opaque_sections': [
                item.model_dump(mode='json') for item in self.opaque_sections
            ],
            'include_dependencies': [
                item.model_dump(mode='json')
                for item in self.include_dependencies
            ],
            'file_dependencies': [
                item.model_dump(mode='json')
                for item in self.file_dependencies
            ],
            'diagnostics': list(self.diagnostics),
        }


class ImportedFieldComparison(BaseModel):
    """Field-by-field comparison between imported settings and an HTDT
    export snapshot — deliberately not a single same/different boolean."""

    model_config = ConfigDict(frozen=True)

    channel_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    state: Literal[
        'exact_match',
        'value_differs',
        'quantization_diff',
        'unsupported_external',
        'missing_in_import',
        'missing_in_plan',
        'unmapped_channel',
        'blocked_by_diagnostics',
    ]
    detail: str = ''


class ImportedVsExportComparison(BaseModel):
    """Comparison of an imported artifact against a
    ``CadCalibrationExportSnapshot`` (#808 §8)."""

    model_config = ConfigDict(frozen=True)

    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    export_settings_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    compared_at_utc: str = Field(min_length=1)
    items: tuple[ImportedFieldComparison, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_comparison(self) -> 'ImportedVsExportComparison':
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('comparison semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'artifact_sha256': self.artifact_sha256,
            'export_settings_sha256': self.export_settings_sha256,
            'compared_at_utc': self.compared_at_utc,
            'items': [item.model_dump(mode='json') for item in self.items],
        }


def _parse_number(text: str) -> float | None:
    match = re.search(_NUMBER, text)
    return float(match.group(0)) if match else None


def _parse_filter_line(body: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse one ``Filter n:`` body. Returns (fields, opaque_reason)."""
    match = re.match(r'^\s*(ON|OFF)\s+(\S+)\s*(.*)$', body, re.IGNORECASE)
    if match is None:
        return None, 'missing ON/OFF state or filter type'
    enabled = match.group(1).upper() == 'ON'
    mnemonic = match.group(2).upper()
    rest = match.group(3)
    filter_type = _FILTER_TYPE_MAP.get(mnemonic)
    if filter_type is None:
        return None, f'unsupported filter type {mnemonic}'

    frequency: float | None = None
    fc = re.search(rf'\bFc\s+({_NUMBER})\s*Hz', rest, re.IGNORECASE)
    if fc:
        frequency = float(fc.group(1))
    else:
        bare = re.match(rf'^\s*({_NUMBER})\s*Hz\b', rest, re.IGNORECASE)
        if bare:
            frequency = float(bare.group(1))

    gain = re.search(rf'\bGain\s+({_NUMBER})\s*dB', rest, re.IGNORECASE)
    q = re.search(rf'\bQ\s+({_NUMBER})', rest, re.IGNORECASE)
    bw = re.search(rf'\bBW\s+Oct\s+({_NUMBER})', rest, re.IGNORECASE)

    return {
        'enabled': enabled,
        'filter_type': filter_type,
        'frequency_hz': frequency,
        'gain_db': float(gain.group(1)) if gain else None,
        'q': float(q.group(1)) if q else None,
        'bandwidth_oct': float(bw.group(1)) if bw else None,
    }, None


def _assign_channel(
    channels: dict[str, dict[str, Any]],
    scope: tuple[str, ...],
    order: list[str],
) -> dict[str, dict[str, Any]]:
    """Return the mutable per-label buckets for the current scope."""
    labels = scope or ('ALL',)
    for label in labels:
        if label not in channels:
            channels[label] = {'preamp_db': None, 'delay_s': None, 'peq': []}
            order.append(label)
    return {label: channels[label] for label in labels}


def _parse_config_text(
    text: str,
    *,
    diagnostics: list[str],
    opaque: list[OpaqueArtifactSection],
    include_deps: list[IncludeDependency],
    include_resolver: Callable[[str], tuple[str, bytes] | None] | None,
    file_deps: list[IncludeDependency] | None = None,
    _include_stack: tuple[str, ...] = (),
    depth: int = 0,
) -> tuple[
    dict[str, dict[str, Any]],
    list[str],
    list[str],
    str | None,
    float | None,
]:
    """Parse one Equalizer APO config body. Returns
    (channel buckets, channel order, declared labels, device_context,
    global_preamp).
    ``declared`` accumulates every channel label a ``Channel:`` line
    selected, so scope declarations surface in the channel mapping even
    when no supported command lands under them.
    ``file_deps`` collects non-include file references (convolution IRs).
    Conditional blocks (``If``/``Else``/``EndIf``) block interpretation:
    every line inside one is recorded opaque — never silently applied
    under an unevaluated condition."""
    channels: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    declared: list[str] = []
    scope: tuple[str, ...] = ()
    device_context: str | None = None
    global_preamp: float | None = None
    in_conditional = False

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        command, _, argument = line.partition(':')
        command = command.strip()
        argument = argument.strip()

        if command in ('If', 'ElseIf', 'Else', 'EndIf'):
            if command == 'EndIf':
                in_conditional = False
            elif command in ('If', 'ElseIf', 'Else'):
                in_conditional = True
            opaque.append(
                OpaqueArtifactSection(
                    kind='conditional_block',
                    raw_text=line,
                    line_number=lineno,
                    reason='conditional expressions are not evaluated; the block stays opaque',
                )
            )
            diagnostics.append(
                f'conditional expression {command!r} at line {lineno}: contents not interpreted'
            )
            continue

        if in_conditional:
            opaque.append(
                OpaqueArtifactSection(
                    kind='conditional_block',
                    raw_text=line,
                    line_number=lineno,
                    reason='inside an unevaluated conditional block; recorded opaque',
                )
            )
            continue

        if command == 'Channel':
            tokens = argument.replace(',', ' ').split()
            if not tokens or tokens == ['ALL']:
                scope = ()
                if 'ALL' not in declared:
                    declared.append('ALL')
            else:
                scope = tuple(tokens)
                for token in scope:
                    if token not in declared:
                        declared.append(token)
            continue

        if command == 'Device':
            device_context = argument or None
            opaque.append(
                OpaqueArtifactSection(
                    kind='device_scope',
                    raw_text=line,
                    line_number=lineno,
                    reason='device selection recorded; output binding is not interpreted',
                )
            )
            continue

        if command == 'Stage':
            # Equalizer APO stage qualifier — the subset executes all
            # stages uniformly; scope is recorded but not modeled.
            opaque.append(
                OpaqueArtifactSection(
                    kind='device_scope',
                    raw_text=line,
                    line_number=lineno,
                    reason='processing stage scope is not modeled in the ECI10 subset',
                )
            )
            continue

        if command == 'Preamp':
            value = _parse_number(argument)
            if value is None:
                opaque.append(
                    OpaqueArtifactSection(
                        kind='malformed_line',
                        raw_text=line,
                        line_number=lineno,
                        reason='Preamp requires a dB value',
                    )
                )
                continue
            if scope:
                for bucket in _assign_channel(channels, scope, order).values():
                    bucket['preamp_db'] = value
            else:
                global_preamp = value
            continue

        if command == 'Delay':
            value = _parse_number(argument)
            if value is None:
                opaque.append(
                    OpaqueArtifactSection(
                        kind='malformed_line',
                        raw_text=line,
                        line_number=lineno,
                        reason='Delay requires a numeric value with a unit',
                    )
                )
                continue
            lowered = argument.lower()
            if 'ms' in lowered:
                delay_s = value / 1000.0
            elif 'samples' in lowered or 'smp' in lowered:
                opaque.append(
                    OpaqueArtifactSection(
                        kind='unsupported_command',
                        raw_text=line,
                        line_number=lineno,
                        reason='delay in samples needs a source sample rate; retained opaque',
                    )
                )
                continue
            elif re.search(rf'{_NUMBER}\s*s\b', lowered):
                delay_s = value
            else:
                opaque.append(
                    OpaqueArtifactSection(
                        kind='malformed_line',
                        raw_text=line,
                        line_number=lineno,
                        reason='Delay unit must be ms or s in the ECI10 subset',
                    )
                )
                continue
            for bucket in _assign_channel(channels, scope, order).values():
                bucket['delay_s'] = delay_s
            continue

        if re.match(r'^Filter\s+\d+$', command):
            fields, reason = _parse_filter_line(argument)
            if fields is None:
                opaque.append(
                    OpaqueArtifactSection(
                        kind='unsupported_filter_type'
                        if reason and 'filter type' in reason
                        else 'malformed_line',
                        raw_text=line,
                        line_number=lineno,
                        reason=reason or 'unparseable filter line',
                    )
                )
                continue
            for bucket in _assign_channel(channels, scope, order).values():
                bucket['peq'].append((line, fields))
            continue

        if command == 'Convolution':
            ir_path = argument
            if not ir_path:
                opaque.append(
                    OpaqueArtifactSection(
                        kind='malformed_line',
                        raw_text=line,
                        line_number=lineno,
                        reason='Convolution requires an impulse-response path',
                    )
                )
                continue
            resolved = include_resolver(ir_path) if include_resolver else None
            dependency = IncludeDependency(
                include_path=ir_path,
                resolved=resolved is not None,
                source_sha256=sha256(resolved[1]).hexdigest()
                if resolved is not None
                else None,
                diagnostics=()
                if resolved is not None
                else ('convolution impulse response not provided',),
            )
            if file_deps is not None:
                file_deps.append(dependency)
            else:
                include_deps.append(dependency)
            opaque.append(
                OpaqueArtifactSection(
                    kind='external_file_reference',
                    raw_text=line,
                    line_number=lineno,
                    reason='convolution impulse response recorded as a file dependency; the IR is not interpreted',
                )
            )
            if resolved is None:
                diagnostics.append(
                    f'unresolved convolution file: {ir_path}'
                )
            continue

        if command == 'Include':
            include_path = argument
            if not include_path:
                opaque.append(
                    OpaqueArtifactSection(
                        kind='malformed_line',
                        raw_text=line,
                        line_number=lineno,
                        reason='Include requires a path',
                    )
                )
                continue
            if include_path in _include_stack:
                include_deps.append(
                    IncludeDependency(
                        include_path=include_path,
                        resolved=False,
                        diagnostics=('include cycle detected',),
                    )
                )
                diagnostics.append(f'include cycle: {include_path}')
                continue
            resolved = include_resolver(include_path) if include_resolver else None
            if resolved is None:
                include_deps.append(
                    IncludeDependency(
                        include_path=include_path,
                        resolved=False,
                        diagnostics=('include target not provided',),
                    )
                )
                diagnostics.append(f'unresolved include: {include_path}')
                continue
            resolved_name, resolved_bytes = resolved
            include_deps.append(
                IncludeDependency(
                    include_path=include_path,
                    resolved=True,
                    source_sha256=sha256(resolved_bytes).hexdigest(),
                )
            )
            sub_text = resolved_bytes.decode('utf-8-sig', errors='replace')
            (
                sub_channels,
                sub_order,
                sub_declared,
                sub_device,
                sub_global,
            ) = _parse_config_text(
                sub_text,
                diagnostics=diagnostics,
                opaque=opaque,
                include_deps=include_deps,
                include_resolver=include_resolver,
                file_deps=file_deps,
                _include_stack=(*_include_stack, include_path),
                depth=depth + 1,
            )
            for label in sub_declared:
                if label not in declared:
                    declared.append(label)
            for label in sub_order:
                if label not in channels:
                    channels[label] = {'preamp_db': None, 'delay_s': None, 'peq': []}
                    order.append(label)
                bucket = channels[label]
                sub = sub_channels[label]
                if sub['preamp_db'] is not None:
                    bucket['preamp_db'] = sub['preamp_db']
                if sub['delay_s'] is not None:
                    bucket['delay_s'] = sub['delay_s']
                bucket['peq'].extend(sub['peq'])
            if sub_device and not device_context:
                device_context = sub_device
            if sub_global is not None:
                global_preamp = sub_global
            continue

        opaque.append(
            OpaqueArtifactSection(
                kind='unsupported_command',
                raw_text=line,
                line_number=lineno,
                reason=f'command {command!r} is outside the ECI10 supported subset',
            )
        )

    return channels, order, declared, device_context, global_preamp


def build_equalizer_apo_artifact(
    source_bytes: bytes,
    *,
    source_filename: str,
    imported_at_utc: str,
    artifact_id: str,
    channel_map: dict[str, str] | None = None,
    include_resolver: Callable[[str], tuple[str, bytes] | None] | None = None,
    diagnostics_extra: tuple[str, ...] = (),
) -> ImportedCalibrationArtifact:
    """Parse a documented Equalizer APO text configuration into an
    ``ImportedCalibrationArtifact`` (#808 ECI10).

    ``include_resolver`` maps an include path to ``(name, bytes)`` or
    ``None`` when the file is unavailable; unresolved includes stay
    visible as diagnostics and dependency rows.
    """
    source_sha = sha256(source_bytes).hexdigest()
    text = source_bytes.decode('utf-8-sig', errors='replace')
    diagnostics: list[str] = list(diagnostics_extra)
    opaque: list[OpaqueArtifactSection] = []
    include_deps: list[IncludeDependency] = []
    file_deps: list[IncludeDependency] = []

    channels, order, declared, device_context, global_preamp = (
        _parse_config_text(
            text,
            diagnostics=diagnostics,
            opaque=opaque,
            include_deps=include_deps,
            include_resolver=include_resolver,
            file_deps=file_deps,
            _include_stack=(),
        )
    )

    channel_map = channel_map or {}
    all_labels = order + [label for label in declared if label not in order]
    mapping: list[ChannelMappingEntry] = []
    normalized_channels: list[ImportedChannelSettings] = []
    for label in all_labels:
        bucket = channels.get(
            label, {'preamp_db': None, 'delay_s': None, 'peq': []}
        )
        mapped = channel_map.get(label)
        state: Literal['mapped', 'unmapped', 'ambiguous']
        if label == 'ALL' and mapped is None:
            state = 'ambiguous'
            diagnostics.append(
                'channel label ALL is unmapped: apply-scope comparison is blocked'
            )
        elif mapped is None:
            state = 'unmapped'
            diagnostics.append(f'channel {label} has no HTDT mapping')
        else:
            state = 'mapped'
        mapping.append(
            ChannelMappingEntry(
                channel_label=label,
                htdt_channel_id=mapped,
                state=state,
            )
        )
        normalized_channels.append(
            ImportedChannelSettings(
                channel_label=label,
                preamp_db=bucket['preamp_db'],
                delay_s=bucket['delay_s'],
                peq=tuple(
                    ImportedFilterBand(
                        band_index=index,
                        filter_type=fields['filter_type'],
                        enabled=fields['enabled'],
                        frequency_hz=fields['frequency_hz'],
                        gain_db=fields['gain_db'],
                        q=fields['q'],
                        bandwidth_oct=fields['bandwidth_oct'],
                        raw_text=raw,
                    )
                    for index, (raw, fields) in enumerate(bucket['peq'])
                ),
            )
        )

    payload: dict[str, Any] = {
        'artifact_id': artifact_id,
        'state': 'imported_configuration',
        'producer_tool': 'equalizer_apo',
        'format_id': _EQUALIZER_APO_FORMAT_ID,
        'format_version': 'unknown',
        'importer_id': _EQUALIZER_APO_IMPORTER_ID,
        'importer_version': _EQUALIZER_APO_IMPORTER_VERSION,
        'source_filename': source_filename,
        'source_sha256': source_sha,
        'imported_at_utc': imported_at_utc,
        'device_context': device_context,
        'global_preamp_db': global_preamp,
        'channel_mapping': tuple(mapping),
        'channels': tuple(normalized_channels),
        'opaque_sections': tuple(opaque),
        'include_dependencies': tuple(include_deps),
        'file_dependencies': tuple(file_deps),
        'diagnostics': tuple(diagnostics),
    }
    provisional = ImportedCalibrationArtifact.model_construct(**canonicalize_payload(ImportedCalibrationArtifact, dict(
        **payload, semantic_sha256='0' * 64
    )))
    return ImportedCalibrationArtifact.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def compare_imported_vs_exported(
    artifact: ImportedCalibrationArtifact,
    export: CadCalibrationExportSnapshot,
    *,
    compared_at_utc: str,
    gain_tolerance_db: float = 0.0,
    delay_tolerance_s: float = 0.0,
    frequency_tolerance_hz: float = 0.0,
    q_tolerance: float = 0.0,
) -> ImportedVsExportComparison:
    """Field-by-field comparison between an imported artifact and an
    exported HTDT settings snapshot (#808 §8). Tolerances distinguish
    ``exact_match`` from ``quantization_diff`` from ``value_differs``;
    unmapped channels never produce comparisons — they surface as
    ``unmapped_channel`` items instead of silently binding a wrong output.
    """
    items: list[ImportedFieldComparison] = []
    mapping = {entry.channel_label: entry for entry in artifact.channel_mapping}
    export_channels = {channel.channel_id: channel for channel in export.channels}
    artifact_fields_absent = ('polarity', 'routing', 'crossovers')

    for channel in artifact.channels:
        entry = mapping.get(channel.channel_label)
        if entry is None or entry.state != 'mapped' or not entry.htdt_channel_id:
            items.append(
                ImportedFieldComparison(
                    channel_id=channel.channel_label,
                    field='*',
                    state='unmapped_channel',
                    detail='no explicit HTDT channel mapping — comparison blocked',
                )
            )
            continue
        channel_id = entry.htdt_channel_id
        exported = export_channels.get(channel_id)
        if exported is None:
            items.append(
                ImportedFieldComparison(
                    channel_id=channel_id,
                    field='*',
                    state='missing_in_plan',
                    detail='channel absent from HTDT export snapshot',
                )
            )
            continue

        imported_gain = (
            channel.preamp_db
            if channel.preamp_db is not None
            else artifact.global_preamp_db
        )
        if imported_gain is None:
            items.append(
                ImportedFieldComparison(
                    channel_id=channel_id,
                    field='gain_db',
                    state='missing_in_import',
                    detail='artifact carries no preamp/gain for this channel',
                )
            )
        else:
            delta = abs(imported_gain - exported.gain_db)
            if delta == 0.0:
                state: Literal[
                    'exact_match', 'quantization_diff', 'value_differs'
                ] = 'exact_match'
            elif delta <= gain_tolerance_db:
                state = 'quantization_diff'
            else:
                state = 'value_differs'
            items.append(
                ImportedFieldComparison(
                    channel_id=channel_id,
                    field='gain_db',
                    state=state,
                    detail=f'import {imported_gain:g} dB vs export {exported.gain_db:g} dB',
                )
            )

        if channel.delay_s is None:
            items.append(
                ImportedFieldComparison(
                    channel_id=channel_id,
                    field='delay_s',
                    state='missing_in_import',
                    detail='artifact carries no delay for this channel',
                )
            )
        else:
            delta = abs(channel.delay_s - exported.delay_s)
            if delta == 0.0:
                state = 'exact_match'
            elif delta <= delay_tolerance_s:
                state = 'quantization_diff'
            else:
                state = 'value_differs'
            items.append(
                ImportedFieldComparison(
                    channel_id=channel_id,
                    field='delay_s',
                    state=state,
                    detail=f'import {channel.delay_s:g} s vs export {exported.delay_s:g} s',
                )
            )

        for field in artifact_fields_absent:
            items.append(
                ImportedFieldComparison(
                    channel_id=channel_id,
                    field=field,
                    state='missing_in_import',
                    detail='Equalizer APO config does not carry this field; cannot verify',
                )
            )

        peq_count = max(len(channel.peq), len(exported.peq))
        for index in range(peq_count):
            field = f'peq[{index}]'
            if index >= len(channel.peq):
                items.append(
                    ImportedFieldComparison(
                        channel_id=channel_id,
                        field=field,
                        state='missing_in_import',
                        detail='HTDT plan has a filter the import lacks',
                    )
                )
                continue
            if index >= len(exported.peq):
                items.append(
                    ImportedFieldComparison(
                        channel_id=channel_id,
                        field=field,
                        state='missing_in_plan',
                        detail='import has a filter the HTDT plan lacks',
                    )
                )
                continue
            band = channel.peq[index]
            expected = exported.peq[index]
            if band.filter_type != expected.filter_type:
                items.append(
                    ImportedFieldComparison(
                        channel_id=channel_id,
                        field=f'{field}.filter_type',
                        state='value_differs',
                        detail=(
                            f'import {band.filter_type} vs export '
                            f'{expected.filter_type} (position {index})'
                        ),
                    )
                )
                continue
            for name, imported_value, expected_value, tolerance in (
                ('frequency_hz', band.frequency_hz, expected.frequency_hz, frequency_tolerance_hz),
                ('gain_db', band.gain_db, expected.gain_db, gain_tolerance_db),
                ('q', band.q, expected.q, q_tolerance),
            ):
                if imported_value is None:
                    items.append(
                        ImportedFieldComparison(
                            channel_id=channel_id,
                            field=f'{field}.{name}',
                            state='missing_in_import',
                            detail='import band does not carry this parameter',
                        )
                    )
                    continue
                delta = abs(imported_value - expected_value)
                if delta == 0.0:
                    state = 'exact_match'
                elif delta <= tolerance:
                    state = 'quantization_diff'
                else:
                    state = 'value_differs'
                items.append(
                    ImportedFieldComparison(
                        channel_id=channel_id,
                        field=f'{field}.{name}',
                        state=state,
                        detail=f'import {imported_value:g} vs export {expected_value:g}',
                    )
                )

    for channel_id in sorted(export_channels.keys() - {
        entry.htdt_channel_id
        for entry in artifact.channel_mapping
        if entry.htdt_channel_id
    }):
        items.append(
            ImportedFieldComparison(
                channel_id=channel_id,
                field='*',
                state='missing_in_import',
                detail='HTDT export channel has no imported counterpart',
            )
        )

    payload: dict[str, Any] = {
        'artifact_sha256': artifact.semantic_sha256,
        'export_settings_sha256': export.exported_settings_semantic_sha256,
        'compared_at_utc': compared_at_utc,
        'items': tuple(items),
    }
    provisional = ImportedVsExportComparison.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ImportedVsExportComparison.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


__all__ = [
    'ChannelMappingEntry',
    'ImportedCalibrationArtifact',
    'ImportedChannelSettings',
    'ImportedFieldComparison',
    'ImportedFilterBand',
    'ImportedVsExportComparison',
    'IncludeDependency',
    'OpaqueArtifactSection',
    'build_equalizer_apo_artifact',
    'compare_imported_vs_exported',
]
