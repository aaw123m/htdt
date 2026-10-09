"""Speaker directivity-balloon overlay resolver (Issue #1000).

Renders each speaker's measured directivity as a 3D balloon in the room
viewport, keyed to the speaker's *installed* aim/transform in the current
SceneDocument and bound ONLY to sealed directivity authorities:

- the equipment binding comes from ``SpeakerInstallationContext``
  (``cad_installation_context_repository``) — never a guessed speaker model;
- the dataset comes from ``CadDirectivityRepository`` — every read re-verifies
  the bound managed source asset and replays the recorded import adapter;
- orientation follows the *same* installed source-frame convention as the
  coverage evaluator (``explicit-aim-body-up-source-frame-1``):
  ``aim_xyz`` is the acoustic reference axis in world and the body +Z
  supplies the roll reference — body orientation never substitutes for an
  unknown aim;
- when a :class:`CadAcousticAimState` exists for the speaker its declared
  axes are drawn as separate arrows (cabinet pose / design aim / as-built
  observed aim) so aim disagreement is visible, never merged silently.

Honesty contract (never violated by this module):

- no sealed dataset → honest ``unknown`` marker (gray wireframe), never an
  idealized or synthesized lobe;
- frequency selection is the dataset's exact measured grid only — an
  off-grid request is blocked, never interpolated;
- balloon vertices sit at *declared* grid angles only: each vertex's
  direction is round-tripped through the evaluator's own angular
  semantics, and any declared cell the evaluator cannot reach is reported
  as an unrepresentable gap instead of being drawn;
- faces are drawn only across fully-measured quads — wrap seams, poles and
  unmeasured cells are never bridged by invented surface;
- the radius is a display mapping of the *relative* level in dB — it is
  never a physical distance, an SPL, or an in-room propagated level;
- every resolve re-reads ``current_head``: a scene edit re-mints the head
  and the next render re-derives every vertex, so a superseded balloon
  cannot stay painted (the cache key pins revision + content hash and the
  authority snapshot, including payload hashes and asset presence).

Qt-free: the Qt panel binds the request and the viewport draws the
resolved scene; this module carries both in plain dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, isfinite, radians, sin
from typing import Literal, Sequence

from .cad_coverage import _source_frame as _installed_source_frame
from .cad_coverage_aim_authority import (
    CadAcousticAimState,
    CadAimAxis,
)
from .cad_coverage_aim_repository import CadCoverageAimRepository
from .cad_directivity import (
    DirectivityDataset,
    classify_directivity_grid,
    direction_to_directivity_angles,
    evaluate_directivity,
)
from .cad_directivity_repository import CadDirectivityRepository
from .cad_equipment_repository import CadEquipmentRepository
from .cad_installation_context_repository import CadInstallationContextRepository
from .cad_repository import SceneRepository
from .cad_scene import (
    Position3,
    SceneEntity,
    acoustic_reference_position,
)
from .cad_schema import connect_sqlite


DIRECTIVITY_OVERLAY_AUTHORITY = 'issue1000-directivity-balloon-1'
"""Authority tag recorded on the resolved scene — display only."""

#: Display-radius mapping bounds (metres). The radius maps *relative dB*
#: onto screen distance — it is never a physical quantity.
BALLOON_RADIUS_SCALE = 1.5
BALLOON_RADIUS_MIN_FRACTION = 0.06
BALLOON_RADIUS_MIN_M = 0.30
BALLOON_RADIUS_MAX_M = 1.40
DEFAULT_FLOOR_DB = -25.0

#: Bounded vertex budget (LOD): grids denser than this are decimated by an
#: index stride — only declared measured samples are shown, never
#: interpolated ones; the scene records ``decimated`` + a notice.
MAX_BALLOON_VERTICES = 6000

#: Round-trip tolerance for the declared-angle → direction → evaluated-angle
#: check. A cell whose declared angles do not reproduce under the dataset's
#: own angle semantics is *unrepresentable* — a real coverage gap.
ANGLE_ROUNDTRIP_TOL_DEG = 1e-6

DIRECTIVITY_DISCLAIMER_JA = (
    '指向性バルーンは実測データセットの相対レベル(dB)の可視化です。'
    '半径は表示用マッピングであり物理距離ではありません。'
    'SPL・室内伝搬・反射を含む音場ではなく、カバレッジ適合判定でもありません。'
    '未測定の方向は面を張らず「未測定」として扱います。'
)

BalloonState = Literal['mesh', 'points', 'unknown', 'blocked']

#: 5-stop cool→warm gradient for the relative-level colour scale.
BALLOON_COLOR_STOPS = (
    (0.00, (59, 76, 192)),
    (0.30, (116, 169, 247)),
    (0.55, (221, 221, 221)),
    (0.80, (244, 140, 97)),
    (1.00, (180, 4, 38)),
)
DIRECTIVITY_UNKNOWN_COLOR = '#8a8f98'
DIRECTIVITY_BLOCKED_COLOR = '#b96a6a'
DIRECTIVITY_AIM_COLORS = {
    'aim': '#7fd4ff',
    'design': '#e0b34f',
    'as-built': '#e58383',
    'cabinet': '#9aa4ff',
}

_BALLOON_GAP_LABELS = {
    'unreachable_under_declared_semantics': '宣言格子が角度意味で未到達',
    'evaluation_unsupported': '評価不能の格子点',
    'decimated': '表示LOD間引き',
    'seam_unbridged': 'ラップ継ぎ目未接続',
}


def color_for_t(t: float) -> tuple[int, int, int]:
    """Display-only colour for normalized level ``t`` in [0, 1]."""
    t = min(1.0, max(0.0, float(t)))
    stops = BALLOON_COLOR_STOPS
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for index in range(len(stops) - 1):
        t0, c0 = stops[index]
        t1, c1 = stops[index + 1]
        if t0 <= t <= t1:
            w = (t - t0) / (t1 - t0)
            return tuple(
                int(round(c0[c] + (c1[c] - c0[c]) * w)) for c in range(3)
            )
    return stops[-1][1]  # pragma: no cover


def _normalize(
    vector: Sequence[float], *, label: str
) -> tuple[float, float, float]:
    if len(vector) != 3:
        raise ValueError(f'{label} must be a 3-vector')
    norm = sum(c * c for c in vector) ** 0.5
    if norm <= 1e-9 or not isfinite(norm):
        raise ValueError(f'{label} must be a non-zero finite vector')
    return (
        float(vector[0]) / norm,
        float(vector[1]) / norm,
        float(vector[2]) / norm,
    )


def _axis_angle_deg(left, right) -> float | None:
    from math import acos, degrees

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    dot = min(1.0, max(-1.0, dot))
    if not isfinite(dot):
        return None
    return degrees(acos(dot))


def _grid_direction(
    horizontal_deg: float,
    vertical_deg: float,
    *,
    semantics: str,
) -> tuple[float, float, float]:
    """Declared (h, v) angles → dataset source-local unit direction.

    ``(forward, left, up)`` frame. The inverse of the evaluator's own
    angle map (``direction_to_directivity_angles``):

    - ``horizontal_vertical``: v is measured against *forward* —
      h = atan2(l, f), v = atan2(u, f) → direction ∝ (1, tan h, tan v),
      i.e. ``(cos h·cos v, sin h·cos v, cos h·sin v)``;
    - ``spherical_azimuth_elevation``: v is elevation off the forward-left
      plane → ``(cos v·cos h, cos v·sin h, sin v)``.

    Reachability is *proven* per cell by the round-trip check in the
    resolver, so any declared cell the evaluator cannot reach (e.g. the
    |v| = 90° h ≠ 0 corners under horizontal_vertical) is reported as a
    gap instead of being drawn at a wrong position.
    """
    h = radians(horizontal_deg)
    v = radians(vertical_deg)
    if semantics == 'spherical_azimuth_elevation':
        cos_v = cos(v)
        direction = (cos_v * cos(h), cos_v * sin(h), sin(v))
    else:  # horizontal_vertical
        direction = (
            cos(h) * cos(v),
            sin(h) * cos(v),
            cos(h) * sin(v),
        )
    return _normalize(direction, label='grid direction')


def _seam_is_bridged(dataset: DirectivityDataset) -> bool:
    """Whether the azimuth wrap seam may carry faces (#981 rule).

    Only a ``signed_180`` convention with a grid-like seam gap (<= 2× the
    largest interior step — the same bound ``_periodic_bracket`` uses) is
    bridged; a wider gap is a real coverage hole, never hidden.
    """
    if dataset.coordinate_convention.horizontal_wrap != 'signed_180':
        return False
    axis = dataset.horizontal_angles_deg
    if len(axis) < 2:
        return False
    interior = [axis[i + 1] - axis[i] for i in range(len(axis) - 1)]
    seam_gap = (axis[0] + 360.0) - axis[-1]
    return 0.0 < seam_gap <= 2.0 * max(interior)


@dataclass(frozen=True, slots=True)
class DirectivityOverlayRequest:
    """Panel-agnostic display request (the 指向性バルーン表示 state)."""

    speaker_entity_id: str | None = None
    """``None`` draws every speaker that resolves; an id scopes to one."""
    dataset_sha256: str | None = None
    """Pin to one sealed dataset; ``None`` selects the newest listed."""
    frequency_hz: float | None = None
    """Exact measured grid frequency — off-grid blocks, never interpolates."""
    floor_db: float = DEFAULT_FLOOR_DB


@dataclass(frozen=True, slots=True)
class DirectivityBalloonVertex:
    """One measured grid vertex in domain space."""

    position: Position3
    direction_domain: tuple[float, float, float]
    horizontal_angle_deg: float
    vertical_angle_deg: float
    magnitude_db: float
    level_t: float
    color_rgb: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class DirectivityBalloonGap:
    reason: str
    count: int
    label_ja: str


@dataclass(frozen=True, slots=True)
class DirectivityAimArrow:
    """One declared aim axis drawn as a world-frame arrow."""

    kind: str
    direction_domain: tuple[float, float, float] | None
    label: str
    color: str


@dataclass(frozen=True, slots=True)
class DirectivityBalloonViewModel:
    """Derived, read-only render state for one speaker (issue §1)."""

    speaker_entity_id: str
    speaker_name: str
    speaker_role: str | None
    state: BalloonState
    origin: Position3 | None
    frequency_hz: float | None
    frequency_grid_hz: tuple[float, ...]
    grid_classification: str | None
    display_scale: str
    reference_level_db: float | None
    floor_db: float
    radius_max_m: float
    vertices: tuple[DirectivityBalloonVertex, ...]
    faces: tuple[tuple[int, int, int], ...]
    gaps: tuple[DirectivityBalloonGap, ...]
    arrows: tuple[DirectivityAimArrow, ...]
    decimated: bool
    reasons: tuple[str, ...]
    dataset_id: str | None
    dataset_version: str | None
    dataset_semantic_sha256: str | None
    equipment_definition_sha256: str | None
    context_sha256: str | None
    aim_sha256: str | None


@dataclass(frozen=True, slots=True)
class DirectivitySpeakerOption:
    """One selectable speaker row for the Qt panel."""

    speaker_entity_id: str
    label: str
    datasets: tuple[tuple[str, str], ...]
    """(dataset semantic sha256, 'id vN') pairs, newest last."""
    frequencies_hz: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class DirectivityOverlayProvenance:
    authority: str
    scene_revision_id: str
    scene_content_hash: str
    dataset_refs: tuple[str, ...]
    equipment_refs: tuple[str, ...]
    aim_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DirectivityOverlayScene:
    """Everything the viewport draws / the panel binds this render."""

    state: Literal['rendered', 'empty', 'blocked']
    balloons: tuple[DirectivityBalloonViewModel, ...]
    options: tuple[DirectivitySpeakerOption, ...]
    legend: tuple[tuple[str, str], ...]
    viewport_lines: tuple[str, ...]
    notices: tuple[str, ...]
    summary_ja: str
    blocked_reason: str | None = None
    provenance: DirectivityOverlayProvenance | None = None
    frequency_hz: float | None = None
    floor_db: float = DEFAULT_FLOOR_DB
    scene_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class _SpeakerSnapshot:
    """Cheap per-speaker authority snapshot (cache key + options)."""

    entity: SceneEntity
    context_sha256: str | None
    equipment_sha256: str | None
    datasets: tuple[tuple[str, str, str, str, bool], ...]
    """(dataset_id, version, semantic_sha256, source_sha256, asset_present)."""
    aim_sha256: str | None


def _empty_scene(
    *,
    state: str,
    reason: str | None,
    request: DirectivityOverlayRequest,
    options: tuple[DirectivitySpeakerOption, ...] = (),
    revision_id: str | None = None,
) -> DirectivityOverlayScene:
    return DirectivityOverlayScene(
        state=state,
        balloons=(),
        options=options,
        legend=(),
        viewport_lines=(
            (f'directivity: {state} — {reason[:80]}' if reason else state),
        ),
        notices=((reason,) if reason else ()),
        summary_ja=reason or '—',
        blocked_reason=reason if state == 'blocked' else None,
        provenance=None,
        floor_db=request.floor_db,
        scene_revision_id=revision_id,
    )


def _snapshot(
    *,
    scene_repository: SceneRepository,
    installation_repository: CadInstallationContextRepository,
    aim_repository: CadCoverageAimRepository | None,
    document_id: str,
    entities: tuple[SceneEntity, ...],
) -> tuple[_SpeakerSnapshot, ...]:
    """One indexed read of every per-speaker authority binding.

    The listing is deliberately cheap (ids + hashes + asset presence only):
    it feeds the staleness cache key and the panel options. The authoritative
    payloads themselves are re-read through the verified repositories only
    when the key actually changes.
    """
    try:
        contexts = installation_repository.latest_contexts_for_document(
            document_id
        )
    except ValueError:
        contexts = {}
    aims: dict[str, CadAcousticAimState] = {}
    if aim_repository is not None:
        try:
            aims = {
                aim.speaker_entity_id: aim
                for aim in aim_repository.list_aim_states(document_id)
            }
        except ValueError:
            aims = {}
    snapshots: list[_SpeakerSnapshot] = []
    for entity in entities:
        context = contexts.get(entity.entity_id)
        datasets: tuple[tuple[str, str, str, str, bool], ...] = ()
        if context is not None:
            datasets = _cheap_dataset_listing(
                scene_repository, context.equipment.equipment_definition_sha256
            )
        aim = aims.get(entity.entity_id)
        snapshots.append(
            _SpeakerSnapshot(
                entity=entity,
                context_sha256=(
                    context.semantic_sha256 if context is not None else None
                ),
                equipment_sha256=(
                    context.equipment.equipment_definition_sha256
                    if context is not None
                    else None
                ),
                datasets=datasets,
                aim_sha256=aim.aim_sha256 if aim is not None else None,
            )
        )
    return tuple(snapshots)


def _cheap_dataset_listing(
    scene_repository: SceneRepository,
    equipment_sha256: str,
) -> tuple[tuple[str, str, str, str, bool], ...]:
    """(id, version, sha, source_sha, asset_row_present) per dataset.

    Reads the index columns plus a hash of the stored payload, so tampering
    with the row body also shifts the key and forces a verified re-read.
    """
    path = scene_repository.path
    rows_out: list[tuple[str, str, str, str, bool]] = []
    try:
        with connect_sqlite(path) as connection:
            rows = connection.execute(
                """
                SELECT dataset_id, version, semantic_sha256,
                       source_asset_sha256, payload_json
                FROM cad_directivity_datasets
                WHERE equipment_definition_sha256=?
                ORDER BY seq ASC
                """,
                (equipment_sha256,),
            ).fetchall()
            for row in rows:
                present = connection.execute(
                    'SELECT 1 FROM cad_measurement_assets WHERE sha256=?',
                    (row['source_asset_sha256'],),
                ).fetchone() is not None
                from hashlib import sha256 as _sha

                payload_hash = _sha(row['payload_json'].encode('utf-8')).hexdigest()
                rows_out.append(
                    (
                        row['dataset_id'],
                        row['version'],
                        row['semantic_sha256'] + ':' + payload_hash[:16],
                        row['source_asset_sha256'],
                        present,
                    )
                )
    except Exception:
        return ()
    return tuple(rows_out)


def _options(
    snapshots: tuple[_SpeakerSnapshot, ...],
) -> tuple[DirectivitySpeakerOption, ...]:
    options: list[DirectivitySpeakerOption] = []
    for snap in snapshots:
        entity = snap.entity
        datasets = tuple(
            (meta[2].split(':', 1)[0], f'{meta[0]} v{meta[1]}')
            for meta in snap.datasets
        )
        options.append(
            DirectivitySpeakerOption(
                speaker_entity_id=entity.entity_id,
                label=(
                    f'{entity.name}（{entity.speaker_role or entity.entity_id}）'
                ),
                datasets=datasets,
                frequencies_hz=(),
            )
        )
    return tuple(options)


def _resolve_balloon(
    *,
    snap: _SpeakerSnapshot,
    request: DirectivityOverlayRequest,
    equipment_repository: CadEquipmentRepository,
    directivity_repository: CadDirectivityRepository,
    aim_repository: CadCoverageAimRepository | None,
    document_id: str,
) -> DirectivityBalloonViewModel:
    entity = snap.entity
    base = dict(
        speaker_entity_id=entity.entity_id,
        speaker_name=entity.name,
        speaker_role=entity.speaker_role,
        origin=None,
        frequency_hz=request.frequency_hz,
        frequency_grid_hz=(),
        grid_classification=None,
        display_scale='unknown',
        reference_level_db=None,
        floor_db=request.floor_db,
        radius_max_m=0.0,
        vertices=(),
        faces=(),
        gaps=(),
        arrows=(),
        decimated=False,
        dataset_id=None,
        dataset_version=None,
        dataset_semantic_sha256=None,
        equipment_definition_sha256=snap.equipment_sha256,
        context_sha256=snap.context_sha256,
        aim_sha256=snap.aim_sha256,
    )

    def finish(state: BalloonState, reasons: Sequence[str], **extra):
        merged = dict(base)
        merged.update(extra)
        return DirectivityBalloonViewModel(
            state=state, reasons=tuple(reasons), **merged
        )

    # 1. equipment binding via the sealed installation context.
    if snap.context_sha256 is None or snap.equipment_sha256 is None:
        return finish('unknown', ('設備割り当てがありません（取付コンテキスト未登録）',))
    definition = equipment_repository.get_definition_by_hash(
        snap.equipment_sha256
    )
    if definition is None:
        return finish('blocked', ('機材定義の権威を再検証できません',))

    # 2. sealed dataset listing — every row is re-verified + replayed.
    if not snap.datasets:
        return finish(
            'unknown',
            ('封緘された指向性データセットがありません（UNKNOWN — 推定モデルは描画しません）',),
        )
    if any(not meta[4] for meta in snap.datasets):
        return finish('blocked', ('指向性ソース資産が欠落しています（フェイルクローズ）',))
    try:
        datasets = directivity_repository.list_datasets_for_definition(
            snap.equipment_sha256
        )
    except ValueError as exc:
        return finish('blocked', (f'データセット権威の再検証に失敗: {exc}',))
    if not datasets:
        return finish('unknown', ('指向性データセットがありません',))
    dataset: DirectivityDataset
    if request.dataset_sha256 is not None:
        pinned = [
            item
            for item in datasets
            if item.semantic_sha256 == request.dataset_sha256
        ]
        if not pinned:
            return finish('blocked', ('指定されたデータセットがこの機材にありません',))
        dataset = pinned[0]
    else:
        dataset = datasets[-1]  # append-only listing → newest wins
    base.update(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_semantic_sha256=dataset.semantic_sha256,
        frequency_grid_hz=dataset.frequencies_hz,
        grid_classification=classify_directivity_grid(dataset),
        display_scale=(
            'normalized_on_axis'
            if dataset.normalization.reference == 'on_axis_per_frequency'
            else 'normalized_explicit_reference'
        ),
        reference_level_db=dataset.normalization.reference_level_db,
    )

    # 3. frequency must be an exact measured grid point.
    frequency = request.frequency_hz
    if frequency is None:
        frequency = dataset.frequencies_hz[0]
        base['frequency_hz'] = frequency
    elif frequency not in dataset.frequencies_hz:
        return finish(
            'blocked',
            (
                f'周波数 {frequency:g} Hz は測定グリッドにありません'
                '（グリッド外は補間せずブロック）',
            ),
        )

    # 4. installed aim/transform from the SceneDocument — never guessed.
    origin = acoustic_reference_position(entity) or entity.position
    base['origin'] = origin
    if entity.aim_xyz is None:
        return finish(
            'unknown',
            ('設置エイムが未設定です（aim_xyz なし — 胴体姿勢から推定しません）',),
        )
    try:
        forward, left, up = _installed_source_frame(entity)
    except ValueError as exc:
        return finish('blocked', (f'設置フレームを解決できません: {exc}',))

    arrows: list[DirectivityAimArrow] = [
        DirectivityAimArrow(
            kind='aim',
            direction_domain=(entity.aim_xyz.x, entity.aim_xyz.y, entity.aim_xyz.z),
            label='aim',
            color=DIRECTIVITY_AIM_COLORS['aim'],
        )
    ]
    reasons: list[str] = []
    aim_state: CadAcousticAimState | None = None
    if aim_repository is not None:
        try:
            aim_state = next(
                (
                    state
                    for state in aim_repository.list_aim_states(document_id)
                    if state.speaker_entity_id == entity.entity_id
                ),
                None,
            )
        except ValueError:
            reasons.append('エイム権威の読み取りに失敗しました')
    aim_axis_kinds: tuple[tuple[str, str], ...] = (
        ('design_aim_target', 'design'),
        ('as_built_observed_aim', 'as-built'),
        ('cabinet_pose', 'cabinet'),
    )
    if aim_state is not None:
        for axis_kind, label in aim_axis_kinds:
            axis = aim_state.axis(axis_kind)
            if axis is None or axis.vector is None:
                continue
            direction = _normalize(axis.vector, label=f'aim axis {axis_kind}')
            arrows.append(
                DirectivityAimArrow(
                    kind=label,
                    direction_domain=direction,
                    label=label,
                    color=DIRECTIVITY_AIM_COLORS[label],
                )
            )
        design = aim_state.axis('design_aim_target')
        observed = aim_state.axis('as_built_observed_aim')
        if (
            design is not None
            and observed is not None
            and design.vector is not None
            and observed.vector is not None
        ):
            angle = _axis_angle_deg(
                tuple(design.vector), tuple(observed.vector)
            )
            if angle is not None and angle > 0.5:
                reasons.append(
                    f'設計エイムと実測エイムが {angle:.1f}° 乖離しています'
                )
        for axis_kind in ('as_built_observed_aim', 'design_aim_target'):
            axis = aim_state.axis(axis_kind)
            if axis is None or axis.vector is None:
                continue
            angle = _axis_angle_deg(
                tuple(axis.vector),
                (entity.aim_xyz.x, entity.aim_xyz.y, entity.aim_xyz.z),
            )
            if angle is not None and angle > 0.5:
                label = '実測' if axis_kind == 'as_built_observed_aim' else '設計'
                reasons.append(
                    f'{label}エイム宣言とシーン aim_xyz が {angle:.1f}° 乖離'
                )
                break
    else:
        reasons.append('エイム権威レコードなし（設計/実測の矢印は非表示）')

    # 5. vertices — declared grid only, round-trip reachability, exact eval.
    h_axis = dataset.horizontal_angles_deg
    v_axis = dataset.vertical_angles_deg
    stride = 1
    total = len(h_axis) * len(v_axis)
    decimated = False
    if total > MAX_BALLOON_VERTICES:
        decimated = True
        stride = max(1, int((total / MAX_BALLOON_VERTICES) ** 0.5) + 1)
        reasons.append(
            f'表示LOD: 測定格子を {stride} 間隔で間引き表示'
            '（格子内補間はしません）'
        )
    h_indices = _kept_indices(len(h_axis), stride)
    v_indices = _kept_indices(len(v_axis), stride)

    size = entity.size_m
    radius_max = (
        max(size.x_m, size.y_m, size.z_m) * BALLOON_RADIUS_SCALE
        if size is not None
        else 0.55
    )
    radius_max = min(BALLOON_RADIUS_MAX_M, max(BALLOON_RADIUS_MIN_M, radius_max))
    radius_min = radius_max * BALLOON_RADIUS_MIN_FRACTION
    floor_db = request.floor_db
    if floor_db >= 0.0:
        floor_db = DEFAULT_FLOOR_DB

    semantics = dataset.coordinate_convention.angle_semantics
    vertex_index: dict[tuple[int, int], int] = {}
    vertices: list[DirectivityBalloonVertex] = []
    unreachable = 0
    unevaluated = 0
    for i in h_indices:
        for j in v_indices:
            h_deg = h_axis[i]
            v_deg = v_axis[j]
            try:
                local = _grid_direction(h_deg, v_deg, semantics=semantics)
            except ValueError:
                unreachable += 1
                continue
            # Reachability proof: the evaluator's own angle map must
            # reproduce the declared cell — a mismatch is a real gap in
            # what the dataset can claim, never drawn.
            back_h, back_v = direction_to_directivity_angles(
                local, semantics=semantics
            )
            if (
                abs(back_h - h_deg) > ANGLE_ROUNDTRIP_TOL_DEG
                or abs(back_v - v_deg) > ANGLE_ROUNDTRIP_TOL_DEG
            ):
                unreachable += 1
                continue
            result = evaluate_directivity(
                dataset,
                frequency_hz=frequency,
                horizontal_angle_deg=h_deg,
                vertical_angle_deg=v_deg,
            )
            if (
                result.decision != 'SUPPORTED'
                or result.magnitude_db is None
                or result.interpolation_applied
            ):
                unevaluated += 1
                continue
            world = (
                forward[0] * local[0] + left[0] * local[1] + up[0] * local[2],
                forward[1] * local[0] + left[1] * local[1] + up[1] * local[2],
                forward[2] * local[0] + left[2] * local[1] + up[2] * local[2],
            )
            t = min(
                1.0,
                max(0.0, (result.magnitude_db - floor_db) / (0.0 - floor_db)),
            )
            radius = radius_min + (radius_max - radius_min) * t
            vertices.append(
                DirectivityBalloonVertex(
                    position=Position3(
                        x_m=origin.x_m + world[0] * radius,
                        y_m=origin.y_m + world[1] * radius,
                        z_m=origin.z_m + world[2] * radius,
                    ),
                    direction_domain=world,
                    horizontal_angle_deg=h_deg,
                    vertical_angle_deg=v_deg,
                    magnitude_db=result.magnitude_db,
                    level_t=t,
                    color_rgb=color_for_t(t),
                )
            )
            vertex_index[(i, j)] = len(vertices) - 1

    # 6. faces — only across fully-measured quads; seams/poles never
    # fabricate surface. Wrap seam is bridged only under the #981 rule.
    h_kept = list(h_indices)
    faces: list[tuple[int, int, int]] = []
    v_kept = list(v_indices)
    seam_bridged = _seam_is_bridged(dataset)
    column_pairs: list[tuple[int, int]] = list(zip(h_kept, h_kept[1:]))
    if seam_bridged and stride == 1:
        column_pairs.append((h_kept[-1], h_kept[0]))
    elif seam_bridged:
        # a decimated seam would connect non-adjacent measured samples —
        # never bridge a gap the LOD itself created.
        pass
    positions = [v.position for v in vertices]

    def _tri_area(a: int, b: int, c: int) -> float:
        pa, pb, pc = positions[a], positions[b], positions[c]
        ab = (pb.x_m - pa.x_m, pb.y_m - pa.y_m, pb.z_m - pa.z_m)
        ac = (pc.x_m - pa.x_m, pc.y_m - pa.y_m, pc.z_m - pa.z_m)
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        return 0.5 * sum(x * x for x in cross) ** 0.5

    for i_left, i_right in column_pairs:
        for j_low, j_high in zip(v_kept, v_kept[1:]):
            corners = [
                vertex_index.get((i_left, j_low)),
                vertex_index.get((i_right, j_low)),
                vertex_index.get((i_right, j_high)),
                vertex_index.get((i_left, j_high)),
            ]
            if any(index is None for index in corners):
                continue  # unmeasured cell — no invented surface
            a, b, c, d = corners
            for tri in ((a, b, c), (a, c, d)):
                if len(set(tri)) < 3:
                    continue  # pole collapse — degenerate, skip
                if _tri_area(*tri) <= 1e-12:
                    continue
                faces.append(tri)

    gaps: list[DirectivityBalloonGap] = []
    if unreachable:
        gaps.append(
            DirectivityBalloonGap(
                reason='unreachable_under_declared_semantics',
                count=unreachable,
                label_ja=_BALLOON_GAP_LABELS[
                    'unreachable_under_declared_semantics'
                ],
            )
        )
    if unevaluated:
        gaps.append(
            DirectivityBalloonGap(
                reason='evaluation_unsupported',
                count=unevaluated,
                label_ja=_BALLOON_GAP_LABELS['evaluation_unsupported'],
            )
        )
    if decimated:
        gaps.append(
            DirectivityBalloonGap(
                reason='decimated',
                count=total - len(vertices),
                label_ja=_BALLOON_GAP_LABELS['decimated'],
            )
        )
    if dataset.coordinate_convention.horizontal_wrap == 'signed_180' and (
        not seam_bridged
    ):
        gaps.append(
            DirectivityBalloonGap(
                reason='seam_unbridged',
                count=0,
                label_ja=_BALLOON_GAP_LABELS['seam_unbridged'],
            )
        )

    if not vertices:
        return finish(
            'unknown',
            reasons
            + [
                'この周波数で描画可能な測定方向がありません'
                '（グリッドは存在しますが到達可能な方向を主張できません）'
            ],
            dataset_id=base['dataset_id'],
            dataset_version=base['dataset_version'],
            dataset_semantic_sha256=base['dataset_semantic_sha256'],
            frequency_grid_hz=base['frequency_grid_hz'],
            grid_classification=base['grid_classification'],
            display_scale=base['display_scale'],
            reference_level_db=base['reference_level_db'],
            origin=origin,
            radius_max_m=radius_max,
            arrows=tuple(arrows),
            gaps=tuple(gaps),
            decimated=decimated,
        )

    classification = base['grid_classification']
    if classification == 'hv_cuts_suspect':
        state: BalloonState = 'points'
        reasons.append(
            '格子分類 hv_cuts_suspect — 水平/垂直カット列のみのため'
            '面は張らず測定点群で表示します'
        )
    elif faces:
        state = 'mesh'
    else:
        state = 'points'
        reasons.append(
            '測定格子が閉じた面を構成しません — 測定点群のみ表示'
        )

    return finish(
        state,
        reasons,
        frequency_hz=frequency,
        vertices=tuple(vertices),
        faces=tuple(faces),
        arrows=tuple(arrows),
        gaps=tuple(gaps),
        radius_max_m=radius_max,
        decimated=decimated,
        dataset_id=base['dataset_id'],
        dataset_version=base['dataset_version'],
        dataset_semantic_sha256=base['dataset_semantic_sha256'],
        frequency_grid_hz=base['frequency_grid_hz'],
        grid_classification=classification,
        display_scale=base['display_scale'],
        reference_level_db=base['reference_level_db'],
        origin=origin,
    )


def _kept_indices(length: int, stride: int) -> tuple[int, ...]:
    if stride <= 1:
        return tuple(range(length))
    kept = list(range(0, length, stride))
    if kept[-1] != length - 1:
        kept.append(length - 1)
    return tuple(kept)


def resolve_directivity_overlay(
    *,
    scene_repository: SceneRepository,
    equipment_repository: CadEquipmentRepository,
    directivity_repository: CadDirectivityRepository,
    installation_repository: CadInstallationContextRepository,
    aim_repository: CadCoverageAimRepository | None,
    document_id: str,
    request: DirectivityOverlayRequest,
) -> DirectivityOverlayScene:
    """Resolve the armed balloon display against the CURRENT head.

    Every authority row is re-read through its sealed repository — the
    overlay never trusts a cached payload when the cache key changes.
    """
    head = scene_repository.current_head(document_id)
    if head is None:
        return _empty_scene(
            state='blocked',
            reason='シーンの現行リビジョンがありません',
            request=request,
        )
    document = head.document
    speakers = tuple(
        entity for entity in document.entities if entity.kind == 'speaker'
    )
    if request.speaker_entity_id is not None:
        speakers = tuple(
            entity
            for entity in speakers
            if entity.entity_id == request.speaker_entity_id
        )
        if not speakers:
            return _empty_scene(
                state='blocked',
                reason=f'選択スピーカーがシーンにありません: {request.speaker_entity_id}',
                request=request,
                revision_id=head.revision_id,
            )
    if not speakers:
        return _empty_scene(
            state='empty',
            reason='シーンにスピーカーがありません',
            request=request,
            revision_id=head.revision_id,
        )

    snapshots = _snapshot(
        scene_repository=scene_repository,
        installation_repository=installation_repository,
        aim_repository=aim_repository,
        document_id=document_id,
        entities=speakers,
    )
    balloons = tuple(
        _resolve_balloon(
            snap=snap,
            request=request,
            equipment_repository=equipment_repository,
            directivity_repository=directivity_repository,
            aim_repository=aim_repository,
            document_id=document_id,
        )
        for snap in snapshots
    )

    rendered = sum(1 for b in balloons if b.state in ('mesh', 'points'))
    unknown = sum(1 for b in balloons if b.state == 'unknown')
    blocked_n = sum(1 for b in balloons if b.state == 'blocked')
    frequency = next(
        (b.frequency_hz for b in balloons if b.frequency_hz is not None),
        request.frequency_hz,
    )

    legend: list[tuple[str, str]] = []
    floor_db = request.floor_db if request.floor_db < 0 else DEFAULT_FLOOR_DB
    for boundary_db in (0.0, -6.0, -12.0, -18.0):
        if boundary_db <= floor_db:
            break
        mid = (boundary_db + max(floor_db, boundary_db - 6.0)) / 2.0
        t = (mid - floor_db) / (0.0 - floor_db)
        rgb = color_for_t(t)
        legend.append(
            (
                f'{max(floor_db, boundary_db - 6.0):g}..{boundary_db:g} dB',
                '#%02x%02x%02x' % rgb,
            )
        )
    legend.append(('UNMEASURED', DIRECTIVITY_UNKNOWN_COLOR))
    legend.extend(
        (label.upper(), DIRECTIVITY_AIM_COLORS[label])
        for label in ('aim', 'design', 'as-built', 'cabinet')
    )

    scale_label = next(
        (b.display_scale for b in balloons if b.state in ('mesh', 'points')),
        'none',
    )
    lines = [
        'directivity: f={} Hz  rel dB (display radius only — not SPL)'.format(
            f'{frequency:g}' if frequency is not None else '—'
        ),
        'scale: {} floor {} dB | mesh {} pts {} unknown {} blocked {}'.format(
            scale_label, f'{floor_db:g}', rendered,
            sum(1 for b in balloons if b.state == 'points'),
            unknown, blocked_n,
        ),
    ]

    notices: list[str] = []
    for balloon in balloons:
        notices.extend(
            f'{balloon.speaker_name}: {reason}' for reason in balloon.reasons
        )
    summary = (
        f'指向性バルーン: {rendered} スピーカー描画'
        f'（f={frequency:g} Hz, 相対dB）' if frequency is not None
        else f'指向性バルーン: {rendered} スピーカー描画'
    )
    if unknown:
        summary += f' / UNKNOWN {unknown}'
    if blocked_n:
        summary += f' / ブロック {blocked_n}'

    provenance = DirectivityOverlayProvenance(
        authority=DIRECTIVITY_OVERLAY_AUTHORITY,
        scene_revision_id=head.revision_id,
        scene_content_hash=head.content_hash,
        dataset_refs=tuple(
            f'{b.dataset_id} v{b.dataset_version}'
            f'#{b.dataset_semantic_sha256[:12]}'
            for b in balloons
            if b.dataset_semantic_sha256 is not None
        ),
        equipment_refs=tuple(
            sorted(
                {
                    b.equipment_definition_sha256[:12]
                    for b in balloons
                    if b.equipment_definition_sha256
                }
            )
        ),
        aim_refs=tuple(
            sorted({b.aim_sha256[:12] for b in balloons if b.aim_sha256})
        ),
    )
    return DirectivityOverlayScene(
        state='rendered',
        balloons=balloons,
        options=_options(snapshots),
        legend=tuple(legend),
        viewport_lines=tuple(lines),
        notices=tuple(notices),
        summary_ja=summary,
        provenance=provenance,
        frequency_hz=frequency,
        floor_db=floor_db,
        scene_revision_id=head.revision_id,
    )


class RoomDirectivityOverlayController:
    """Arms and resolves the directivity-balloon overlay.

    Same discipline as the coverage/survey controllers: ``resolve()`` is
    keyed on ``current_head`` + the authority snapshot — deliberately never
    trusting a stale payload — so a scene edit, a new dataset, or a removed
    asset always lapses the drawn balloons on the next render.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        equipment_repository: CadEquipmentRepository,
        directivity_repository: CadDirectivityRepository,
        installation_repository: CadInstallationContextRepository,
        document_id: str,
        aim_repository: CadCoverageAimRepository | None = None,
    ) -> None:
        self._scene_repository = scene_repository
        self._equipment_repository = equipment_repository
        self._directivity_repository = directivity_repository
        self._installation_repository = installation_repository
        self._aim_repository = aim_repository
        self._document_id = document_id
        self._request = DirectivityOverlayRequest()
        self._enabled = False
        self._cache_key: tuple | None = None
        self._cache: DirectivityOverlayScene | None = None

    @property
    def armed(self) -> bool:
        return self._enabled

    @property
    def request(self) -> DirectivityOverlayRequest:
        return self._request

    def arm(self, request: DirectivityOverlayRequest) -> None:
        self._request = request
        self._enabled = True

    def update(self, request: DirectivityOverlayRequest) -> None:
        if request != self._request:
            self._request = request

    def clear(self) -> None:
        self._enabled = False
        self._request = DirectivityOverlayRequest()
        self._cache_key = None
        self._cache = None

    def _key(self, head, snapshots) -> tuple:
        return (
            self._document_id,
            head.revision_id,
            head.content_hash,
            self._request,
            tuple(
                (
                    snap.entity.entity_id,
                    snap.context_sha256,
                    snap.equipment_sha256,
                    snap.datasets,
                    snap.aim_sha256,
                )
                for snap in snapshots
            ),
        )

    def list_options(
        self,
    ) -> tuple[tuple[DirectivitySpeakerOption, ...], tuple[str, ...]]:
        """Selectable speakers for the panel combo (snapshot-verified)."""
        head = self._scene_repository.current_head(self._document_id)
        if head is None:
            return (), ()
        speakers = tuple(
            entity
            for entity in head.document.entities
            if entity.kind == 'speaker'
        )
        snapshots = _snapshot(
            scene_repository=self._scene_repository,
            installation_repository=self._installation_repository,
            aim_repository=self._aim_repository,
            document_id=self._document_id,
            entities=speakers,
        )
        return _options(snapshots), ()

    def resolve(self) -> DirectivityOverlayScene | None:
        """Fresh resolution keyed on head + authority snapshot.

        The key pins the head revision/content hash and every bound
        authority identity (context sha, dataset ids+payload hash+asset
        presence, aim sha); the payload-bearing verified reads run only
        when the key changes, and any tamper that shifts a hash forces the
        repository's fail-closed re-verification.
        """
        if not self._enabled:
            return None
        head = self._scene_repository.current_head(self._document_id)
        if head is None:
            self._cache_key = None
            self._cache = None
            return _empty_scene(
                state='blocked',
                reason='シーンの現行リビジョンがありません',
                request=self._request,
            )
        speakers = tuple(
            entity
            for entity in head.document.entities
            if entity.kind == 'speaker'
        )
        if self._request.speaker_entity_id is not None:
            speakers = tuple(
                entity
                for entity in speakers
                if entity.entity_id == self._request.speaker_entity_id
            )
        snapshots = _snapshot(
            scene_repository=self._scene_repository,
            installation_repository=self._installation_repository,
            aim_repository=self._aim_repository,
            document_id=self._document_id,
            entities=speakers,
        )
        key = self._key(head, snapshots)
        if key == self._cache_key:
            return self._cache
        scene = resolve_directivity_overlay(
            scene_repository=self._scene_repository,
            equipment_repository=self._equipment_repository,
            directivity_repository=self._directivity_repository,
            installation_repository=self._installation_repository,
            aim_repository=self._aim_repository,
            document_id=self._document_id,
            request=self._request,
        )
        self._cache_key = key
        self._cache = scene
        return scene
