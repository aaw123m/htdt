"""Lighting-scene 3D preview presentation model (#1013).

Read-only derivation of *display symbols* from a sealed
:class:`LightingScene` plus its fixture/zone inventory and commissioning
records. The model keeps the four setpoint stages — ``desired`` /
``commanded`` / ``read_back`` / ``measured`` — as separate presentation
channels that are never merged into a single "actual" state:

- a pin is drawn ONLY when a scene-referenced fixture's ``entity_id``
  resolves exactly to an entity in the current :class:`SceneDocument`;
- a control-only fixture (``entity_id=None``) or a dangling binding stays
  in the ``3D未配置`` list — it is never pinned and never guessed;
- an absent commissioning stage renders as ``unknown`` (``level_percent``
  stays ``None``), never as 0 % and never as the desired value;
- ``commanded`` proves a request was sent, not an applied state;
  ``read_back`` is the device's own report — it is not photometric
  evidence, and neither is ever promoted into ``measured``;
- commissioning records participate ONLY when bound to the exact scene
  identity (``scene_id`` + ``version`` + ``scene_sha256``); records
  belonging to another scene identity are ignored and counted.

Nothing here converts ``level_percent`` into lux, beam spread, or room
illumination; marker colors are explanation symbols of configured state.
This module performs no I/O — device apply/read-back stays on the
approved ``cad_hue_lighting`` adapter path, never on a preview toggle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from .cad_lighting import (
    ChannelState,
    LightingCommissioningRecord,
    LightingFixture,
    LightingScene,
    LightingZone,
    SetpointStage,
)
from .cad_scene import SceneDocument


#: All four setpoint stages, in display order.
LIGHTING_STAGES: tuple[SetpointStage, ...] = (
    'desired',
    'commanded',
    'read_back',
    'measured',
)

#: Stage vocabulary — Japanese legend label + bead color. The colors are
#: explanation symbols of configured state; they never encode photometry
#: and a colored bead never means "the room is lit".
LIGHTING_STAGE_VOCAB: dict[SetpointStage, tuple[str, str]] = {
    'desired': ('目標 desired', '#4da3ff'),
    'commanded': ('送信 commanded', '#ffb340'),
    'read_back': ('応答 read-back', '#59d98c'),
    'measured': ('実測 measured', '#e06ee0'),
}

#: The fifth legend entry: a commissioning stage that was never recorded.
LIGHTING_UNKNOWN_STAGE_LABEL = '不明 unknown'
LIGHTING_UNKNOWN_STAGE_COLOR = '#4c5361'

#: Zone-role vocabulary for pin anchors — color-codes which functional
#: zone a fixture serves (screen bias vs path vs general …).
LIGHTING_ZONE_VOCAB: dict[str, tuple[str, str]] = {
    'general': ('一般ゾーン', '#8ab4ff'),
    'bias': ('バイアス照明', '#c792ea'),
    'accent': ('アクセント', '#ffd166'),
    'path': ('通路・足下', '#7bd88f'),
    'other': ('その他', '#9aa3b2'),
}

#: Pin anchor color when a fixture carries only a direct scene state
#: (no zone membership) or no assignable role.
LIGHTING_NEUTRAL_ANCHOR_COLOR = '#9aa3b2'

#: The honesty contract rendered next to every legend: markers are state
#: symbols, not a lighting simulation.
LIGHTING_PREVIEW_DISCLAIMER = (
    '照明シーン・プレビュー: 色・マーカーは設定状態の説明記号です。'
    '照度(lux)・ビーム広がり・部屋の明るさの物理表現ではありません。'
    'read-backは機器の応答報告であり実測照度ではありません。'
    'プレビュー操作は機器へ一切送信しません。'
)

#: ``LightingUnplacedFixture.reason`` vocabulary.
UNPLACED_REASON_LABELS: dict[str, str] = {
    'control_only': '制御専用（entityバインドなし）',
    'entity_unresolved': 'バインド先の部屋物体が未解決',
}


@dataclass(frozen=True, slots=True)
class LightingStagePresentation:
    """One setpoint stage as displayed.

    ``level_percent=None`` means the stage is unknown — the renderer must
    draw an *unknown* glyph, never substitute 0 % or the desired value.
    ``stale`` marks a recorded stage that carries no observation
    timestamp/source — present evidence of unknown freshness.
    """

    stage: SetpointStage
    level_percent: float | None = None
    cct_k: int | None = None
    color_rgb: tuple[int, int, int] | None = None
    observed_at_utc: str | None = None
    source: str | None = None
    stale: bool = False
    #: Declared state the fixture cannot physically produce, e.g. a CCT
    #: set on a non-CCT fixture ('cct', 'color', 'level').
    unsupported: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.level_percent is not None


@dataclass(frozen=True, slots=True)
class LightingScenePin:
    """A scene-referenced fixture exact-bound to a document entity."""

    fixture_id: str
    fixture_label: str | None
    fixture_type: str
    entity_id: str
    entity_name: str
    #: Domain-space entity position (pin anchor).
    position: tuple[float, float, float]
    #: Top of the entity body (z), where the stage-bead row floats.
    top_z_m: float
    zone_roles: tuple[str, ...]
    #: Scene refs driving this fixture's state, e.g. ('fixture:fx-1',
    #: 'zone:z-bias'). More than one means the fixture carries several
    #: assignments — flagged, never merged.
    assigning_refs: tuple[str, ...]
    multi_assigned: bool
    #: All four stages in ``LIGHTING_STAGES`` order.
    states: tuple[LightingStagePresentation, ...]
    #: When an assigning zone is ``bias``, the resolved position of the
    #: display/screen entity it serves (None when unresolved/absent).
    bias_target_entity_id: str | None = None
    bias_target_position: tuple[float, float, float] | None = None

    def stage(self, stage: SetpointStage) -> LightingStagePresentation:
        for state in self.states:
            if state.stage == stage:
                return state
        raise KeyError(stage)


@dataclass(frozen=True, slots=True)
class LightingUnplacedFixture:
    """A scene-referenced fixture with no 3-D placement.

    ``control_only`` — ``entity_id`` was never bound; the fixture is
    reachable only through its ``control_ref``. ``entity_unresolved`` —
    an ``entity_id`` was recorded but no longer resolves in the current
    document (renamed/deleted entity). Neither is ever drawn as a pin.
    """

    fixture_id: str
    fixture_label: str | None
    fixture_type: str
    reason: Literal['control_only', 'entity_unresolved']
    detail: str
    zone_roles: tuple[str, ...]
    states: tuple[LightingStagePresentation, ...]

    def stage(self, stage: SetpointStage) -> LightingStagePresentation:
        for state in self.states:
            if state.stage == stage:
                return state
        raise KeyError(stage)


@dataclass(frozen=True, slots=True)
class LightingZoneRow:
    """A scene-referenced zone row for the preview's list surface.

    Zone-level commissioning evidence is kept at zone level — it is never
    fanned out into member fixtures' per-device stages.
    """

    zone_id: str
    zone_label: str | None
    role: str
    member_fixture_ids: tuple[str, ...]
    states: tuple[LightingStagePresentation, ...]


@dataclass(frozen=True, slots=True)
class LightingScenePreview:
    """Everything the viewport/panel need to draw one scene's preview."""

    scene_id: str
    scene_version: str
    scene_sha256: str
    scene_label: str
    pins: tuple[LightingScenePin, ...]
    unplaced: tuple[LightingUnplacedFixture, ...]
    zone_rows: tuple[LightingZoneRow, ...]
    #: Scene refs that resolve to nothing ('fixture:x', 'zone:z',
    #: 'zone:z/member:f'), plus zones with no member fixtures.
    missing_refs: tuple[str, ...]
    #: Commissioning records dropped for being bound to another scene.
    ignored_records: int
    stage_legend: tuple[tuple[str, str], ...]
    zone_legend: tuple[tuple[str, str], ...]
    disclaimer: str


def _stage_presentation(
    stage: SetpointStage,
    level_percent: float | None,
    cct_k: int | None,
    color_rgb: tuple[int, int, int] | None,
    observed_at_utc: str | None,
    source: str | None,
    fixture: LightingFixture,
) -> LightingStagePresentation:
    unsupported: list[str] = []
    if level_percent is not None:
        if not fixture.controllable_level:
            unsupported.append('level')
        if cct_k is not None and fixture.cct_min_k is None and fixture.cct_max_k is None:
            unsupported.append('cct')
        if color_rgb is not None and not fixture.color_capable:
            unsupported.append('color')
    stale = (
        stage != 'desired'
        and level_percent is not None
        and observed_at_utc is None
    )
    return LightingStagePresentation(
        stage=stage,
        level_percent=level_percent,
        cct_k=cct_k,
        color_rgb=color_rgb,
        observed_at_utc=observed_at_utc,
        source=source,
        stale=stale,
        unsupported=tuple(unsupported),
    )


def _fixture_states(
    fixture: LightingFixture,
    desired_state: ChannelState | None,
    record: LightingCommissioningRecord | None,
) -> tuple[LightingStagePresentation, ...]:
    """The four stage presentations for one fixture — never merged.

    Desired mirrors ``cad_lighting._record_desired_level``: an exact-bound
    record's own ``desired`` snapshot wins over the scene assignment.
    """
    desired_level: float | None = None
    desired_cct: int | None = None
    desired_rgb: tuple[int, int, int] | None = None
    if record is not None and record.desired is not None:
        desired_level = record.desired.level_percent
        desired_cct = record.desired.cct_k
    elif desired_state is not None:
        desired_level = desired_state.level_percent
        desired_cct = desired_state.cct_k
        desired_rgb = desired_state.color_rgb
    out = [
        _stage_presentation(
            'desired',
            desired_level,
            desired_cct,
            desired_rgb,
            None,
            'scene',
            fixture,
        )
    ]
    for stage in ('commanded', 'read_back', 'measured'):
        slot = getattr(record, stage) if record is not None else None
        out.append(
            _stage_presentation(
                stage,
                slot.level_percent if slot is not None else None,
                slot.cct_k if slot is not None else None,
                None,
                slot.observed_at_utc if slot is not None else None,
                slot.source if slot is not None else None,
                fixture,
            )
        )
    return tuple(out)


def _zone_states(
    desired_state: ChannelState | None,
    record: LightingCommissioningRecord | None,
) -> tuple[LightingStagePresentation, ...]:
    """Zone-level stage presentations — no fixture capability checks."""

    class _ZoneUnit:
        controllable_level = True
        cct_min_k = 0
        cct_max_k = 1
        color_capable = True

    unit = _ZoneUnit()
    desired_level = desired_state.level_percent if desired_state else None
    desired_cct = desired_state.cct_k if desired_state else None
    out = [
        _stage_presentation(
            'desired', desired_level, desired_cct,
            desired_state.color_rgb if desired_state else None,
            None, 'scene', unit,
        )
    ]
    for stage in ('commanded', 'read_back', 'measured'):
        slot = getattr(record, stage) if record is not None else None
        out.append(
            _stage_presentation(
                stage,
                slot.level_percent if slot is not None else None,
                slot.cct_k if slot is not None else None,
                None,
                slot.observed_at_utc if slot is not None else None,
                slot.source if slot is not None else None,
                unit,
            )
        )
    return tuple(out)


def build_lighting_scene_preview(
    *,
    document: SceneDocument,
    scene: LightingScene,
    fixtures: Iterable[LightingFixture] = (),
    zones: Iterable[LightingZone] = (),
    commissioning_records: Iterable[LightingCommissioningRecord] = (),
) -> LightingScenePreview:
    """Derive the read-only preview model for ``scene`` on ``document``.

    Scene refs drive the preview: a fixture participates when named by a
    ``fixture`` state or as a member of a named zone. Pinning additionally
    requires the fixture's ``entity_id`` to resolve to a document entity.
    """

    entities = {entity.entity_id: entity for entity in document.entities}
    fixtures_by_id = {f.fixture_id: f for f in fixtures}
    zones_by_id = {z.zone_id: z for z in zones}

    # Only records bound to THIS scene identity participate — a record
    # carrying another scene's hash is foreign evidence, never a state.
    records: dict[tuple[str, str], LightingCommissioningRecord] = {}
    ignored_records = 0
    for record in commissioning_records:
        if (
            record.scene_id != scene.scene_id
            or record.scene_version != scene.version
            or record.scene_sha256 != scene.scene_sha256
        ):
            ignored_records += 1
            continue
        records[(record.ref_kind, record.ref_id)] = record

    scene_states = {(s.ref_kind, s.ref_id): s for s in scene.states}
    # fixture_id -> ordered [(ChannelState, assigning ref label)]
    assignments: dict[str, list[tuple[ChannelState, str]]] = {}
    fixture_zone_roles: dict[str, list[str]] = {}
    fixture_bias_targets: dict[str, str] = {}
    zone_rows: list[LightingZoneRow] = []
    missing_refs: list[str] = []

    for state in scene.states:
        if state.ref_kind == 'fixture':
            if state.ref_id in fixtures_by_id:
                assignments.setdefault(state.ref_id, []).append(
                    (state, f'fixture:{state.ref_id}')
                )
            else:
                missing_refs.append(f'fixture:{state.ref_id}')
            continue
        zone = zones_by_id.get(state.ref_id)
        if zone is None:
            missing_refs.append(f'zone:{state.ref_id}')
            continue
        zone_rows.append(
            LightingZoneRow(
                zone_id=zone.zone_id,
                zone_label=zone.label,
                role=zone.role,
                member_fixture_ids=zone.member_fixture_ids,
                states=_zone_states(state, records.get(('zone', zone.zone_id))),
            )
        )
        if not zone.member_fixture_ids:
            missing_refs.append(f'zone:{zone.zone_id}(メンバーなし)')
        for member_id in zone.member_fixture_ids:
            if member_id not in fixtures_by_id:
                missing_refs.append(f'zone:{zone.zone_id}/member:{member_id}')
                continue
            assignments.setdefault(member_id, []).append(
                (state, f'zone:{zone.zone_id}')
            )
            roles = fixture_zone_roles.setdefault(member_id, [])
            if zone.role not in roles:
                roles.append(zone.role)
            if zone.role == 'bias' and zone.bias_target_entity_id is not None:
                fixture_bias_targets[member_id] = zone.bias_target_entity_id

    pins: list[LightingScenePin] = []
    unplaced: list[LightingUnplacedFixture] = []
    used_roles: list[str] = []
    for fixture_id, entries in assignments.items():
        fixture = fixtures_by_id[fixture_id]
        direct = next(
            (state for state, ref in entries if ref == f'fixture:{fixture_id}'),
            None,
        )
        # Direct fixture state wins for desired; zone states otherwise
        # apply in scene order (first assignment), never merged.
        desired_state = direct if direct is not None else entries[0][0]
        record = records.get(('fixture', fixture_id))
        states = _fixture_states(fixture, desired_state, record)
        roles = tuple(fixture_zone_roles.get(fixture_id, ()))
        for role in roles:
            if role not in used_roles:
                used_roles.append(role)
        assigning_refs = tuple(dict.fromkeys(ref for _s, ref in entries))

        entity = entities.get(fixture.entity_id) if fixture.entity_id else None
        if fixture.entity_id is None:
            unplaced.append(
                LightingUnplacedFixture(
                    fixture_id=fixture_id,
                    fixture_label=fixture.label,
                    fixture_type=fixture.fixture_type,
                    reason='control_only',
                    detail=UNPLACED_REASON_LABELS['control_only'],
                    zone_roles=roles,
                    states=states,
                )
            )
            continue
        if entity is None:
            unplaced.append(
                LightingUnplacedFixture(
                    fixture_id=fixture_id,
                    fixture_label=fixture.label,
                    fixture_type=fixture.fixture_type,
                    reason='entity_unresolved',
                    detail=(
                        f"{UNPLACED_REASON_LABELS['entity_unresolved']}: "
                        f"{fixture.entity_id}"
                    ),
                    zone_roles=roles,
                    states=states,
                )
            )
            continue
        position = (
            entity.position.x_m,
            entity.position.y_m,
            entity.position.z_m,
        )
        top_z = position[2] + (
            float(entity.size_m.z_m) / 2.0 if entity.size_m is not None else 0.0
        )
        bias_target_id = fixture_bias_targets.get(fixture_id)
        bias_target = (
            entities.get(bias_target_id) if bias_target_id is not None else None
        )
        pins.append(
            LightingScenePin(
                fixture_id=fixture_id,
                fixture_label=fixture.label,
                fixture_type=fixture.fixture_type,
                entity_id=entity.entity_id,
                entity_name=entity.name,
                position=position,
                top_z_m=top_z,
                zone_roles=roles,
                assigning_refs=assigning_refs,
                multi_assigned=len(assigning_refs) > 1,
                states=states,
                bias_target_entity_id=bias_target_id,
                bias_target_position=(
                    (
                        bias_target.position.x_m,
                        bias_target.position.y_m,
                        bias_target.position.z_m,
                    )
                    if bias_target is not None
                    else None
                ),
            )
        )

    stage_legend = tuple(
        (LIGHTING_STAGE_VOCAB[stage][0], LIGHTING_STAGE_VOCAB[stage][1])
        for stage in LIGHTING_STAGES
    ) + ((LIGHTING_UNKNOWN_STAGE_LABEL, LIGHTING_UNKNOWN_STAGE_COLOR),)
    zone_legend = tuple(
        LIGHTING_ZONE_VOCAB[role]
        for role in LIGHTING_ZONE_VOCAB
        if role in used_roles
    )
    return LightingScenePreview(
        scene_id=scene.scene_id,
        scene_version=scene.version,
        scene_sha256=scene.scene_sha256,
        scene_label=scene.label,
        pins=tuple(pins),
        unplaced=tuple(unplaced),
        zone_rows=tuple(zone_rows),
        missing_refs=tuple(missing_refs),
        ignored_records=ignored_records,
        stage_legend=stage_legend,
        zone_legend=zone_legend,
        disclaimer=LIGHTING_PREVIEW_DISCLAIMER,
    )


def stage_bead_label(pin: LightingScenePin | LightingUnplacedFixture) -> str:
    """Compact per-stage text: '目30% 送– 応0% 測–'.

    Every stage is spelled out — a dash marks unknown so a missing stage
    can never be mistaken for 0 % or for the desired value.
    """

    parts: list[str] = []
    for state in pin.states:
        tag = LIGHTING_STAGE_VOCAB[state.stage][0].split(' ', 1)[0]
        if state.known:
            text = f'{state.level_percent:g}%'
        else:
            text = '–'
        parts.append(f'{tag}{text}')
    return ' '.join(parts)
