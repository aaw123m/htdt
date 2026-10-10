"""#1006 — read-only campaign spatial overlay resolution for the room viewport.

Reads the sealed `SpatialCampaignDesign` authority plus campaign-executor
runner-cell progress and produces a per-render snapshot the viewport can draw:

* one marker per `CampaignPoint` at its exact declared XYZ, never moved;
* marker shape keyed by primary role (spatial holdout can never visually merge
  with optimization — different glyph AND different fill);
* a progress ring under each marker fed ONLY by the executor's persisted
  runner-cell states and `CampaignPointBinding`s — unknown stays unknown;
* designs pinned to a stale `SceneRevision` surface as LAPSED: drawn dimmed
  wireframe with all validity/progress claims withheld;
* validity (inside-room bounds, ear-height range, zone membership) comes only
  from existing geometry/assessment on the CURRENT head; anything not
  derivable renders as unknown.

This module is Qt-free: it only resolves data. RoomViewport3D renders it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .cad_scene import Position3, acoustic_reference_position, is_listener_receiver_eligible
from .cad_spatial_campaign import (
    CampaignPoint,
    SpatialCampaignDesign,
)
from .cad_spatial_campaign_repository import CadSpatialCampaignRepository
from .cad_repository import SceneRepository
from .measurement.domain.cad_measurement_runner import (
    MeasurementRunnerPlan,
    RunnerCellStatus,
)
from .measurement.persistence.cad_measurement_runner_repository import (
    CadMeasurementRunnerRepository,
)

# Role glyphs: shape is the role channel. spatial_holdout uses a cube so it can
# never visually merge with the optimization sphere; repeatability/diagnostic
# (never coverage roles) get their own unambiguous glyphs too.
CAMPAIGN_ROLE_GLYPH: dict[str, str] = {
    'reference_alignment': 'cone',
    'optimization': 'sphere',
    'spatial_holdout': 'cube',
    'repeatability': 'disc',
    'diagnostic': 'wireframe_sphere',
    'boundary_stress': 'cylinder',
    'standards_required': 'diamond',
}

# Primary-role precedence when a point carries several roles: holdout first so
# it is always visually separable, then boundary/standards/reference.
_CAMPAIGN_ROLE_PRIORITY = (
    'spatial_holdout',
    'boundary_stress',
    'standards_required',
    'reference_alignment',
    'optimization',
    'repeatability',
    'diagnostic',
)

CAMPAIGN_ROLE_COLORS: dict[str, str] = {
    'reference_alignment': '#7ED6FF',
    'optimization': '#6BA6FF',
    'spatial_holdout': '#E58383',
    'repeatability': '#4CC5B1',
    'diagnostic': '#98A2AD',
    'boundary_stress': '#E1B15A',
    'standards_required': '#BD9CF4',
}

# ASCII legend labels — the VTK viewport cannot render CJK reliably.
CAMPAIGN_ROLE_ASCII: dict[str, str] = {
    'reference_alignment': 'REF',
    'optimization': 'OPT',
    'spatial_holdout': 'HOLDOUT',
    'repeatability': 'REP',
    'diagnostic': 'DIAG',
    'boundary_stress': 'BND',
    'standards_required': 'STD',
}

CampaignProgress = Literal[
    'measured',
    'blocked',
    'staged',
    'pending',
    'skipped',
    'unbound',
    'unknown',
]

CAMPAIGN_PROGRESS_LABELS: dict[str, str] = {
    'measured': '測定済み',
    'blocked': '要対応',
    'staged': '取り込み済',
    'pending': '未測定',
    'skipped': 'スキップ済み',
    'unbound': 'セル未結合',
    'unknown': '不明',
    'lapsed': '失効',
}

CAMPAIGN_PROGRESS_ASCII: dict[str, str] = {
    'measured': 'measured',
    'blocked': 'blocked',
    'staged': 'staged',
    'pending': 'pending',
    'skipped': 'skipped',
    'unbound': 'unbound',
    'unknown': 'unknown',
    'lapsed': 'lapsed',
}

CAMPAIGN_PROGRESS_COLORS: dict[str, str] = {
    'measured': '#68B98A',
    'blocked': '#E1B15A',
    'staged': '#4CC5B1',
    'pending': '#6BA6FF',
    'skipped': '#5D6874',
    'unbound': '#3A4858',
    'unknown': '#3A4858',
    'lapsed': '#B09BC6',
}

CellValidity = Literal[
    'inside_room',
    'outside_room',
    'zone_excluded',
    'zone_challenge',
    'height_out_of_range',
    'area_outside',
    'undeclared',
    'unknown',
]

CAMPAIGN_VALIDITY_LABELS: dict[str, str] = {
    'inside_room': '室内',
    'outside_room': '室外形',
    'zone_excluded': '除外ゾーン内',
    'zone_challenge': 'チャレンジ領域内',
    'height_out_of_range': '耳高範囲外',
    'area_outside': 'リスニングエリア外',
    'undeclared': 'エリア未宣言',
    'unknown': '不明',
}

_BLOCKED_STATUSES = frozenset({'retake_required', 'assignment_incomplete', 'quality_pending'})
_PENDING_STATUSES = frozenset({'not_started', 'quality_pending'})


def campaign_primary_role(roles: tuple[str, ...]) -> str:
    """The role that owns the marker shape/fill. Holdout always wins."""
    for role in _CAMPAIGN_ROLE_PRIORITY:
        if role in roles:
            return role
    return roles[0] if roles else 'diagnostic'


def _distance_m(a: Position3, b: Position3) -> float:
    return (
        (a.x_m - b.x_m) ** 2 + (a.y_m - b.y_m) ** 2 + (a.z_m - b.z_m) ** 2
    ) ** 0.5


@dataclass(frozen=True, slots=True)
class CampaignOverlayMarker:
    point_id: str
    position: Position3
    roles: tuple[str, ...]
    primary_role: str
    glyph: str
    progress: CampaignProgress
    progress_detail: str
    validity: CellValidity
    zone_id: str | None
    provenance: str
    focused: bool
    channel_coverage: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CampaignOverlayScene:
    revision_id: str
    revision_content_hash: str
    design_id: str | None
    declared_at_utc: str | None
    lapsed: bool
    scene_unbound: bool
    progress_source: str | None
    markers: tuple[CampaignOverlayMarker, ...]
    legend: tuple[tuple[str, str], ...]
    progress_counts: dict[str, int]
    shown_count: int
    total_count: int
    notices: tuple[str, ...]
    viewport_lines: tuple[str, ...]
    summary_ja: str


def _marker_validity(design: SpatialCampaignDesign, point: CampaignPoint, head) -> CellValidity:
    """Validity from CURRENT-head geometry + the design's declared zones only."""
    zone = design.listening_area.zone_for(point.position)
    if zone is not None:
        if zone.kind == 'excluded':
            return 'zone_excluded'
        if zone.kind == 'challenge_region':
            return 'zone_challenge'
    elif design.listening_area.non_excluded_zones():
        return 'area_outside'
    room = getattr(head.document, 'room', None)
    if room is not None:
        inside = (
            0.0 <= point.position.x_m <= float(room.width_m)
            and 0.0 <= point.position.y_m <= float(room.depth_m)
            and 0.0 <= point.position.z_m <= float(room.height_m)
        )
        if not inside:
            return 'outside_room'
    height_range = design.listening_area.head_height_range_m
    if height_range is not None:
        low, high = height_range
        if not (low <= point.position.z_m <= high):
            return 'height_out_of_range'
    return 'inside_room' if room is not None else 'unknown'


def _resolve_executor_join(
    runner_repository: CadMeasurementRunnerRepository | None,
    head,
    design: SpatialCampaignDesign,
    document_id: str,
) -> tuple[dict[str, list[RunnerCellStatus]], str | None, str | None]:
    """Map executor cell statuses onto point positions (bounded position join).

    Returns (point_id -> joined statuses, progress_source_label, focus_point_id).
    Cells whose target entity cannot resolve a position join are surfaced as
    notices by the caller; nothing is guessed.
    """
    if runner_repository is None:
        return {}, None, None
    plans = runner_repository.list_plans(document_id)
    if not plans:
        return {}, None, None
    plan: MeasurementRunnerPlan = plans[-1]
    runs = runner_repository.list_runs(plan.plan_id)
    if not runs:
        return {}, None, None
    run = runs[-1]
    states = runner_repository.cell_states(run.run_id)
    by_id = {entity.entity_id: entity for entity in head.document.entities}
    tolerance = float(
        getattr(design.expectations, 'duplicate_tolerance_m', 0.01)
    )
    # Cell target positions keyed by executor target entity.
    joined: dict[str, list[RunnerCellStatus]] = {}
    focus_point_id: str | None = None
    from .measurement.domain.cad_measurement_runner import next_incomplete_cell
    next_cell_index = next_incomplete_cell(plan, states)
    for cell in plan.cells:
        state = states.get(cell.cell_index)
        status = state.status if state is not None else 'not_started'
        entity = by_id.get(cell.target_entity_id)
        position = (
            acoustic_reference_position(entity)
            if entity is not None and is_listener_receiver_eligible(entity)
            else None
        )
        if position is None:
            continue
        for point in design.points:
            if _distance_m(position, point.position) <= tolerance:
                joined.setdefault(point.point_id, []).append(status)
        if next_cell_index is not None and cell.cell_index == next_cell_index:
            for point in design.points:
                if _distance_m(position, point.position) <= tolerance:
                    focus_point_id = point.point_id
                    break
    source = f'{plan.plan_id}/{run.run_id}'
    return joined, source, focus_point_id


def resolve_campaign_overlay(
    scene_repository: SceneRepository,
    spatial_repository: CadSpatialCampaignRepository,
    runner_repository: CadMeasurementRunnerRepository | None,
    document_id: str,
    *,
    design_id: str | None = None,
    focus_entity_id: str | None = None,
) -> CampaignOverlayScene | None:
    """Resolve the campaign overlay against the CURRENT head — per render."""
    head = scene_repository.current_head(document_id)
    if head is None:
        return None

    notices: list[str] = []
    designs = spatial_repository.list_designs(document_id)
    design: SpatialCampaignDesign | None = None
    if design_id is not None:
        for item in designs:
            if item.design_id == design_id:
                design = item
                break
        if design is None:
            notices.append(f'指定された空間計画が見つかりません: {design_id}')
            design_id = None
    if design is None and designs:
        design = designs[-1]

    # Executor progress — bounded join onto point positions.
    joined: dict[str, list[RunnerCellStatus]] = {}
    progress_source: str | None = None
    guided_point_id: str | None = None
    if design is not None and runner_repository is not None:
        try:
            joined, progress_source, guided_point_id = _resolve_executor_join(
                runner_repository, head, design, document_id
            )
        except Exception:  # error-boundary: overlay read — a progress-resolve failure shows an honest 'progress unknown' notice, never fabricated progress (noqa: BLE001)
            notices.append('測定実行の進捗を取得できません（進捗は不明として表示）')

    bindings: dict[str, int] = {}
    if design is not None:
        for binding in spatial_repository.bindings_for_design(design.design_id):
            bindings[binding.point_id] = bindings.get(binding.point_id, 0) + 1

    # Staleness: the design pinned to a non-current revision is LAPSED.
    scene_unbound = False
    lapsed = False
    if design is not None:
        if design.scene_revision_id is None or design.scene_content_hash is None:
            scene_unbound = True
            notices.append('空間計画はシーンリビジョンに束縛されていません')
        elif (
            design.scene_revision_id != head.revision_id
            or design.scene_content_hash != head.content_hash
        ):
            lapsed = True
            notices.append(
                '計画は別のシーンリビジョンで宣言されました（失効 — 現在のリビジョンに基づく主張は保留）'
            )

    # Focus join: an explicit entity id from the deep link, else the executor's
    # next incomplete cell. Both are bounded by the position join.
    focus_point_id: str | None = guided_point_id
    if focus_entity_id is not None and design is not None:
        by_id = {entity.entity_id: entity for entity in head.document.entities}
        entity = by_id.get(focus_entity_id)
        position = (
            acoustic_reference_position(entity)
            if entity is not None and is_listener_receiver_eligible(entity)
            else None
        )
        tolerance = float(getattr(design.expectations, 'duplicate_tolerance_m', 0.01))
        focus_point_id = None
        if position is not None:
            for point in design.points:
                if _distance_m(position, point.position) <= tolerance:
                    focus_point_id = point.point_id
                    break
        if focus_point_id is None:
            notices.append('選択セルの測定位置は計画点に結合しません（位置一致なし）')

    markers: list[CampaignOverlayMarker] = []
    progress_counts: dict[str, int] = {key: 0 for key in CAMPAIGN_PROGRESS_LABELS}
    if design is not None:
        for point in design.points:
            primary = campaign_primary_role(point.roles)
            if lapsed:
                progress: CampaignProgress = 'unknown'
                detail = '失効した計画 — 進捗・妥当性は保留'
                validity: CellValidity = 'unknown'
            else:
                statuses = joined.get(point.point_id, [])
                bound = point.point_id in bindings
                if bound or any(s == 'completed' for s in statuses):
                    progress = 'measured'
                elif any(s in _BLOCKED_STATUSES for s in statuses):
                    progress = 'blocked'
                elif any(s == 'staged' for s in statuses):
                    progress = 'staged'
                elif any(s in _PENDING_STATUSES for s in statuses):
                    progress = 'pending'
                elif statuses and all(s == 'skipped' for s in statuses):
                    progress = 'skipped'
                elif not statuses:
                    progress = 'unbound'
                else:
                    progress = 'unknown'
                detail_bits: list[str] = []
                if bound:
                    detail_bits.append(f'測定バインド{bindings[point.point_id]}件')
                if statuses:
                    detail_bits.append('セル:' + '+'.join(sorted(set(statuses))))
                detail = ' / '.join(detail_bits) if detail_bits else '進捗なし'
                validity = _marker_validity(design, point, head)
            zone = design.listening_area.zone_for(point.position)
            markers.append(
                CampaignOverlayMarker(
                    point_id=point.point_id,
                    position=point.position,
                    roles=tuple(point.roles),
                    primary_role=primary,
                    glyph=CAMPAIGN_ROLE_GLYPH.get(primary, 'sphere'),
                    progress=progress,
                    progress_detail=detail,
                    validity=validity,
                    zone_id=zone.zone_id if zone is not None else None,
                    provenance=point.provenance,
                    focused=point.point_id == focus_point_id,
                    channel_coverage=tuple(point.channel_coverage),
                )
            )
            progress_counts[progress if not lapsed else 'lapsed'] = (
                progress_counts.get(progress if not lapsed else 'lapsed', 0) + 1
            )

    used_roles = {marker.primary_role for marker in markers}
    legend = tuple(
        (CAMPAIGN_ROLE_ASCII[role], CAMPAIGN_ROLE_COLORS[role])
        for role in _CAMPAIGN_ROLE_PRIORITY
        if role in used_roles
    )
    shown = len(markers)
    total = len(design.points) if design is not None else 0

    viewport_lines: list[str] = []
    if design is None:
        viewport_lines.append('campaign: no spatial design for this document')
    else:
        short = design.design_id.replace('spatial-campaign:', '')[:12]
        state = 'LAPSED' if lapsed else ('unbound' if scene_unbound else 'current')
        viewport_lines.append(f'campaign: {short} {state} shown {shown}/{total}')
        progress_ascii = ' '.join(
            f'{key}:{count}'
            for key, count in progress_counts.items()
            if count
        )
        if progress_ascii:
            viewport_lines.append(f'progress: {progress_ascii}')
        if progress_source is None and not lapsed and design is not None:
            viewport_lines.append('progress: no executor run (unknown)')

    summary_bits: list[str] = []
    if design is not None:
        summary_bits.append(f'空間計画 {design.design_id.replace("spatial-campaign:", "")[:12]}')
        summary_bits.append(f'表示 {shown}/{total} 点')
        if lapsed:
            summary_bits.append('失効（古いリビジョンの計画 — 主張は保留）')
        if progress_source is not None:
            summary_bits.append(f'進捗: {progress_source}')
    else:
        summary_bits.append('空間計画なし')

    return CampaignOverlayScene(
        revision_id=head.revision_id,
        revision_content_hash=head.content_hash,
        design_id=design.design_id if design is not None else None,
        declared_at_utc=design.declared_at_utc if design is not None else None,
        lapsed=lapsed,
        scene_unbound=scene_unbound,
        progress_source=progress_source,
        markers=tuple(markers),
        legend=legend,
        progress_counts={k: v for k, v in progress_counts.items() if v},
        shown_count=shown,
        total_count=total,
        notices=tuple(notices),
        viewport_lines=tuple(viewport_lines),
        summary_ja=' / '.join(summary_bits),
    )


@dataclass(slots=True)
class _ArmedRequest:
    design_id: str | None
    focus_entity_id: str | None


class RoomCampaignOverlayController:
    """Arms and resolves the campaign overlay for a room workspace.

    Content-keyed cache identical in spirit to RoomTreatmentOverlayController:
    the key carries head revision id + content hash + design id, so a stale
    revision flips the overlay to lapsed on the next render without leaks.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        spatial_repository: CadSpatialCampaignRepository | None = None,
        runner_repository: CadMeasurementRunnerRepository | None = None,
    ) -> None:
        self._scene_repository = scene_repository
        self._document_id = document_id
        self._spatial_repository = spatial_repository or CadSpatialCampaignRepository(
            scene_repository
        )
        self._runner_repository = runner_repository
        self._request: _ArmedRequest | None = None
        self._cache_key: tuple | None = None
        self._cache: CampaignOverlayScene | None = None

    @property
    def armed(self) -> bool:
        return self._request is not None

    def arm(
        self,
        *,
        design_id: str | None = None,
        focus_entity_id: str | None = None,
    ) -> None:
        self._request = _ArmedRequest(
            design_id=design_id, focus_entity_id=focus_entity_id
        )
        self._cache_key = None
        self._cache = None

    def clear(self) -> None:
        self._request = None
        self._cache_key = None
        self._cache = None

    def resolve(self) -> CampaignOverlayScene | None:
        if self._request is None:
            return None
        head = self._scene_repository.current_head(self._document_id)
        if head is None:
            return None
        key = (
            head.revision_id,
            head.content_hash,
            self._request.design_id,
            self._request.focus_entity_id,
        )
        if self._cache_key == key:
            return self._cache
        scene = resolve_campaign_overlay(
            self._scene_repository,
            self._spatial_repository,
            self._runner_repository,
            self._document_id,
            design_id=self._request.design_id,
            focus_entity_id=self._request.focus_entity_id,
        )
        self._cache_key = key
        self._cache = scene
        return scene
