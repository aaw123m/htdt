"""Theater operating presets (#567).

A theater usually has several valid operating configurations — Movie, Music,
Game, Night — that differ in routing, calibration, target, video
presentation, A/V sync and room state *without* changing physical geometry.
None of those are new ``SceneRevision``\\ s, and none should be free-form
notes: a :class:`TheaterOperatingPreset` is an immutable composition over
existing exact authorities, keyed by preset id.

Contract properties:

- one physical theater persists many named presets without cloning the
  SceneRevision — the preset pins the exact ``scene_revision_id`` +
  ``content_hash`` it operates on;
- every composed authority is an exact reference (``PresetComponentRef``),
  never a copy of its values;
- the *desired* preset, the *user-confirmed applied device state*
  (:class:`AppliedPresetState`) and *measured evidence* under that preset
  (:class:`PresetMeasurementBinding`) are separate append-only authorities —
  selecting "Movie" never proves the AVR/projector actually switched;
- declared program/listening modes record what the user configured; decoder
  or upmixer internals that are not separately validated stay UNKNOWN and
  are never simulated;
- freshness evaluation (:func:`evaluate_preset_freshness`) reports
  per-component CURRENT / STALE / MISSING so changing one referenced
  authority marks dependent presets stale without rewriting history;
- there is deliberately no global "best preset" score.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload


PRESET_SCHEMA_VERSION = 1
PRESET_AUTHORITY_VERSION = 'theater-operating-preset-1'

OperatingPresetCategory = Literal['movie', 'music', 'game', 'night', 'custom']
OPERATING_PRESET_CATEGORIES: frozenset[str] = frozenset(
    {'movie', 'music', 'game', 'night', 'custom'}
)

DeclaredProgramInput = Literal[
    'stereo_pcm',
    'channel_5_1',
    'channel_7_1',
    'object_audio',
    'game_low_latency',
    'unknown',
]

# Component kinds a preset may pin. The list is deliberately open ('other')
# because new versioned authorities keep landing; semantics stay the same.
PresetComponentKind = Literal[
    'system_topology',
    'system_variant',
    'excitation_scenario',
    'routing_profile',
    'calibration_plan',
    'calibration_export',
    'applied_settings',
    'target_curve',
    'presentation_profile',
    'photometric_state',
    'av_sync_condition',
    'room_operating_state',
    'playback_level_condition',
    'playback_chain',
    'listening_population',
    'other',
]

PresetFreshnessState = Literal['current', 'stale', 'missing']

#: Summary used when a project has no presets at all — a valid state, not an
#: error (existing projects remain NOT_CONFIGURED).
OPERATING_PRESET_NOT_CONFIGURED = 'not_configured'






class PresetComponentRef(BaseModel):
    """Exact reference to one authority a preset composes.

    ``ref_sha256`` pins the semantic hash when the referenced authority has
    one; authorities keyed by bare ids (e.g. an operating-state row that has
    no semantic hash) may leave it unset, in which case staleness is decided
    purely by whether the id still resolves.

    ``external_dependency`` marks a component that is *deliberately* not a
    resolvable local authority — e.g. a service-side or external snapshot
    the project cannot enumerate. It is the explicit contract for an
    unresolved pin: without it, ``save_preset`` rejects refs that do not
    resolve to a persisted authority.
    """

    model_config = ConfigDict(frozen=True)

    kind: PresetComponentKind
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)
    label: str | None = Field(default=None, min_length=1)
    external_dependency: bool = False


class PresetProvenanceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class TheaterOperatingPreset(BaseModel):
    """Immutable composition naming one exact operational configuration."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PRESET_SCHEMA_VERSION
    authority_version: Literal['theater-operating-preset-1'] = (
        PRESET_AUTHORITY_VERSION
    )
    preset_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: OperatingPresetCategory = 'custom'
    purpose_note: str | None = None
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    declared_input: DeclaredProgramInput = 'unknown'
    #: Declared listening/DSP mode string (e.g. device preset name). This is
    #: user-declared state, never a simulated decoder/upmixer model.
    declared_device_mode: str | None = Field(default=None, min_length=1)
    nominal_playback_level_db: float | None = None
    component_refs: tuple[PresetComponentRef, ...] = ()
    provenance: tuple[PresetProvenanceItem, ...] = ()
    created_at_utc: str = Field(min_length=1)
    preset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_preset(self) -> 'TheaterOperatingPreset':
        if self.nominal_playback_level_db is not None and not isfinite(
            self.nominal_playback_level_db
        ):
            raise ValueError('nominal playback level must be finite')
        keys = [(item.kind, item.ref_id) for item in self.component_refs]
        if len(keys) != len(set(keys)):
            raise ValueError('preset component refs must be unique per kind/ref')
        if any(item.kind == 'scene_revision' for item in self.component_refs):
            raise ValueError('the Scene component is pinned by dedicated fields only')
        provenance_keys = [item.key for item in self.provenance]
        if len(provenance_keys) != len(set(provenance_keys)):
            raise ValueError('preset provenance keys must be unique')
        if self.preset_sha256 != _hash(self.semantic_payload()):
            raise ValueError('TheaterOperatingPreset hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'preset_id': self.preset_id,
            'document_id': self.document_id,
            'name': self.name,
            'category': self.category,
            'purpose_note': self.purpose_note,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'declared_input': self.declared_input,
            'declared_device_mode': self.declared_device_mode,
            'nominal_playback_level_db': self.nominal_playback_level_db,
            'component_refs': [
                item.model_dump(mode='json') for item in self.component_refs
            ],
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }

    def component(self, kind: str, ref_id: str) -> PresetComponentRef | None:
        for item in self.component_refs:
            if item.kind == kind and item.ref_id == ref_id:
                return item
        return None


class AppliedPresetState(BaseModel):
    """User-confirmed record that devices were actually set to a preset.

    Distinct from the desired :class:`TheaterOperatingPreset`: confirmation is
    explicit, records the device context, and can carry structured deviations
    the user made relative to the exported/desired configuration.
    """

    model_config = ConfigDict(frozen=True)

    applied_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    preset_id: str = Field(min_length=1)
    preset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed_at_utc: str = Field(min_length=1)
    device_context: str | None = Field(default=None, min_length=1)
    deviations: tuple[PresetProvenanceItem, ...] = ()
    note: str | None = None
    applied_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_applied(self) -> 'AppliedPresetState':
        keys = [item.key for item in self.deviations]
        if len(keys) != len(set(keys)):
            raise ValueError('applied deviation keys must be unique')
        if self.applied_sha256 != _hash(self.semantic_payload()):
            raise ValueError('AppliedPresetState hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'applied_id': self.applied_id,
            'document_id': self.document_id,
            'preset_id': self.preset_id,
            'preset_sha256': self.preset_sha256,
            'confirmed_at_utc': self.confirmed_at_utc,
            'device_context': self.device_context,
            'deviations': [item.model_dump(mode='json') for item in self.deviations],
            'note': self.note,
        }


class PresetMeasurementBinding(BaseModel):
    """Binds measurement evidence to the exact preset it was captured under.

    Repeated measurements are only semantically comparable when they name the
    same operating preset, so the binding is persisted as its own append-only
    record rather than inferred from timestamps.

    ``measurement_sha256s`` optionally pins each measurement's semantic hash
    (same order as ``measurement_ids``) so the binding references exact
    measurement versions, not bare ids.

    ``historical_attestation`` is the explicit allowance for binding a
    measurement captured *before* the preset itself was saved — the recorded
    claim that this earlier measurement genuinely belongs to this operating
    configuration.
    """

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    preset_id: str = Field(min_length=1)
    preset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_ids: tuple[str, ...] = Field(min_length=1)
    measurement_sha256s: tuple[str, ...] | None = None
    bound_at_utc: str = Field(min_length=1)
    historical_attestation: bool = False
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_binding(self) -> 'PresetMeasurementBinding':
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError('binding measurement ids must be unique')
        if self.measurement_sha256s is not None and len(
            self.measurement_sha256s
        ) != len(self.measurement_ids):
            raise ValueError(
                'measurement_sha256s must align one-to-one with measurement_ids'
            )
        if self.binding_sha256 != _hash(self.semantic_payload()):
            raise ValueError('PresetMeasurementBinding hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'binding_id': self.binding_id,
            'document_id': self.document_id,
            'preset_id': self.preset_id,
            'preset_sha256': self.preset_sha256,
            'measurement_ids': list(self.measurement_ids),
            'measurement_sha256s': (
                list(self.measurement_sha256s)
                if self.measurement_sha256s is not None
                else None
            ),
            'bound_at_utc': self.bound_at_utc,
            'historical_attestation': self.historical_attestation,
        }


class PresetComponentStatus(BaseModel):
    """Freshness of one composed authority reference."""

    model_config = ConfigDict(frozen=True)

    kind: str
    ref_id: str
    state: PresetFreshnessState
    reason: str


class PresetFreshness(BaseModel):
    """Per-component freshness evaluation; never collapsed into one score."""

    model_config = ConfigDict(frozen=True)

    preset_id: str
    components: tuple[PresetComponentStatus, ...]

    @property
    def all_current(self) -> bool:
        return all(item.state == 'current' for item in self.components)


class OperatingModeSummary(BaseModel):
    """Human-readable operating-mode summary for reports/Overview."""

    model_config = ConfigDict(frozen=True)

    preset_id: str
    name: str
    category: str
    lines: tuple[str, ...]
    unknowns: tuple[str, ...]


def build_operating_preset(
    *,
    document_id: str,
    name: str,
    scene_revision_id: str,
    scene_content_hash: str,
    category: OperatingPresetCategory = 'custom',
    purpose_note: str | None = None,
    declared_input: DeclaredProgramInput = 'unknown',
    declared_device_mode: str | None = None,
    nominal_playback_level_db: float | None = None,
    component_refs: tuple[PresetComponentRef, ...] = (),
    provenance: tuple[PresetProvenanceItem, ...] = (),
    created_at_utc: str,
    preset_id: str | None = None,
) -> TheaterOperatingPreset:
    payload: dict[str, Any] = {
        'preset_id': preset_id or str(uuid4()),
        'document_id': document_id,
        'name': name,
        'category': category,
        'purpose_note': purpose_note,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'declared_input': declared_input,
        'declared_device_mode': declared_device_mode,
        'nominal_playback_level_db': nominal_playback_level_db,
        'component_refs': tuple(component_refs),
        'provenance': tuple(provenance),
        'created_at_utc': created_at_utc,
    }
    provisional = TheaterOperatingPreset.model_construct(**canonicalize_payload(TheaterOperatingPreset, dict(
        **payload, preset_sha256='0' * 64
    )))
    return TheaterOperatingPreset(
        **payload,
        preset_sha256=_hash(provisional.semantic_payload()),
    )


def record_applied_preset_state(
    preset: TheaterOperatingPreset,
    *,
    confirmed_at_utc: str,
    device_context: str | None = None,
    deviations: tuple[PresetProvenanceItem, ...] = (),
    note: str | None = None,
    applied_id: str | None = None,
) -> AppliedPresetState:
    payload: dict[str, Any] = {
        'applied_id': applied_id or str(uuid4()),
        'document_id': preset.document_id,
        'preset_id': preset.preset_id,
        'preset_sha256': preset.preset_sha256,
        'confirmed_at_utc': confirmed_at_utc,
        'device_context': device_context,
        'deviations': tuple(deviations),
        'note': note,
    }
    provisional = AppliedPresetState.model_construct(
        **payload, applied_sha256='0' * 64
    )
    return AppliedPresetState(
        **payload,
        applied_sha256=_hash(provisional.semantic_payload()),
    )


def bind_preset_measurements(
    preset: TheaterOperatingPreset,
    *,
    measurement_ids: tuple[str, ...],
    bound_at_utc: str,
    measurement_sha256s: tuple[str, ...] | None = None,
    historical_attestation: bool = False,
    binding_id: str | None = None,
) -> PresetMeasurementBinding:
    payload: dict[str, Any] = {
        'binding_id': binding_id or str(uuid4()),
        'document_id': preset.document_id,
        'preset_id': preset.preset_id,
        'preset_sha256': preset.preset_sha256,
        'measurement_ids': tuple(measurement_ids),
        'measurement_sha256s': (
            tuple(measurement_sha256s) if measurement_sha256s is not None else None
        ),
        'bound_at_utc': bound_at_utc,
        'historical_attestation': historical_attestation,
    }
    provisional = PresetMeasurementBinding.model_construct(**canonicalize_payload(PresetMeasurementBinding, dict(
        **payload, binding_sha256='0' * 64
    )))
    return PresetMeasurementBinding(
        **payload,
        binding_sha256=_hash(provisional.semantic_payload()),
    )


def evaluate_preset_freshness(
    preset: TheaterOperatingPreset,
    *,
    resolve_sha256: 'Mapping[tuple[str, str], str | None]',
) -> PresetFreshness:
    """Per-component freshness against current authority state.

    ``resolve_sha256`` maps ``(kind, ref_id)`` to the referenced authority's
    *current* semantic hash, or ``None`` when the id no longer resolves. A pin
    without a recorded hash is CURRENT when the id resolves and STALE is not
    derivable — the preset itself is never rewritten by this evaluation.
    """

    components: list[PresetComponentStatus] = []
    for ref in preset.component_refs:
        current = resolve_sha256.get((ref.kind, ref.ref_id))
        if current is None:
            components.append(
                PresetComponentStatus(
                    kind=ref.kind,
                    ref_id=ref.ref_id,
                    state='missing',
                    reason='referenced authority no longer resolves',
                )
            )
        elif ref.ref_sha256 is not None and ref.ref_sha256 != current:
            components.append(
                PresetComponentStatus(
                    kind=ref.kind,
                    ref_id=ref.ref_id,
                    state='stale',
                    reason='referenced authority changed since the preset was saved',
                )
            )
        else:
            components.append(
                PresetComponentStatus(
                    kind=ref.kind,
                    ref_id=ref.ref_id,
                    state='current',
                    reason='reference unchanged',
                )
            )
    return PresetFreshness(preset_id=preset.preset_id, components=tuple(components))


_COMPONENT_LABELS: dict[str, str] = {
    'system_topology': 'システム構成',
    'system_variant': 'システムバリアント',
    'excitation_scenario': '再生/励起シナリオ',
    'routing_profile': 'ルーティング',
    'calibration_plan': '校正プラン',
    'calibration_export': '校正出力',
    'applied_settings': '適用済み設定',
    'target_curve': 'ターゲットカーブ',
    'presentation_profile': '映像プレゼンテーション',
    'photometric_state': '光出力状態',
    'av_sync_condition': 'AV同期条件',
    'room_operating_state': '部屋の状態',
    'playback_level_condition': '再生レベル条件',
    'playback_chain': '再生チェーン',
    'listening_population': 'リスニング範囲',
    'other': 'その他',
}

_INPUT_LABELS: dict[str, str] = {
    'stereo_pcm': 'ステレオPCM',
    'channel_5_1': '5.1ch',
    'channel_7_1': '7.1ch',
    'object_audio': 'オブジェクトオーディオ',
    'game_low_latency': '低遅延ゲーム',
    'unknown': '不明',
}


def operating_mode_summary(preset: TheaterOperatingPreset) -> OperatingModeSummary:
    """Concise human-readable summary of one preset's composition.

    Referenced authorities are named, not re-stated; components without an
    available semantic pin appear under ``unknowns`` instead of being
    silently trusted.
    """

    lines: list[str] = [
        f'入力: {_INPUT_LABELS.get(preset.declared_input, preset.declared_input)}',
    ]
    if preset.declared_device_mode is not None:
        lines.append(f'デバイスモード: {preset.declared_device_mode}')
    if preset.nominal_playback_level_db is not None:
        lines.append(f'再生レベル: {preset.nominal_playback_level_db} dB')
    unknowns: list[str] = []
    for ref in preset.component_refs:
        label = _COMPONENT_LABELS.get(ref.kind, ref.kind)
        display = ref.label or ref.ref_id
        if ref.ref_sha256 is None:
            unknowns.append(f'{label}: {display}（ピンなし）')
        else:
            lines.append(f'{label}: {display}')
    return OperatingModeSummary(
        preset_id=preset.preset_id,
        name=preset.name,
        category=preset.category,
        lines=tuple(lines),
        unknowns=tuple(unknowns),
    )
