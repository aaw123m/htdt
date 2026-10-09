"""Room-viewport acoustic-treatment coverage overlay (#1009 / REV73).

Resolves persisted AcousticTreatmentPlacements against the EXACT current
SceneRevision — placement scene_revision_id/content hash, the semantic host
surface, and the placement_sha256 all re-check against live authority on every
resolve — and derives each panel's clipped effective patch
(:class:`TreatmentFootprint.patch_uv_polygons`) the same way the solver does.

Honesty contract:
- Only the clipped patch is drawable. A placement rectangle that extends
  past its host surface is never painted as fully treated; the effective/
  rectangle ratio is surfaced instead.
- 提案された配置 (proposed) and 設置記録 (installed) draw as distinct states.
- A placement whose host binding no longer evaluates 'exact' against the
  current head — stale SceneRevision, removed/changed surface, legacy
  unverified binding — is reported as lapsed/undrawable, never drawn.
- Derived patches that overlap on one host, and rectangles whose patch is
  clipped to nothing (範囲外), are explicit warnings, not silent states.

No Qt imports here — the workspace owns the widgets; this module owns the
resolution so a late render cannot resurrect a superseded footprint.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Polygon

from .cad_acoustic_treatment import AcousticTreatmentPlacement
from .cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from .cad_repository import SceneRepository
from .cad_scene import Position3, domain_to_render
from .treatment_boundary_overlay import (
    FOOTPRINT_COVERAGE_MISMATCH_TOLERANCE,
    FOOTPRINT_OVERLAP_TOLERANCE_M2,
    TreatmentFootprint,
    derive_treatment_footprint,
)


#: Operator-facing JA vocabulary for placement lifecycle (#1009).
LIFECYCLE_LABELS = {
    'proposed': '提案された配置',
    'installed': '設置記録',
}

#: JA vocabulary for why a placement contributes no drawn patch.
NOTICE_KIND_LABELS = {
    'lapsed': 'ホストバインド失効',
    'unbound': 'ホスト面未設定',
    'out_of_bounds': '範囲外 (実効パッチなし)',
    'underivable': '実効パッチを導出できません',
    'unreadable': '配置権威を再検証できません',
}

#: JA labels for TreatmentSurfaceBindingEvaluation.binding_state — kept
#: parallel to room_acoustics_panel._BINDING_STATE_LABELS.
BINDING_STATE_LABELS = {
    'exact': 'EXACT (バインド済み)',
    'unbound': '未バインド',
    'legacy_unverified': '旧式・未検証',
    'scene_revision_missing': 'シーンリビジョンなし',
    'wrong_scene_revision': '別リビジョン',
    'scene_authority_mismatch': 'シーン権威不一致',
    'semantic_geometry_missing': 'セマンティックジオメトリなし',
    'surface_removed': '面が削除済み',
    'surface_authority_mismatch': '面権威不一致',
    'stale_scene_revision': '古い (リビジョン更新)',
    'stale_semantic_geometry': '古い (ジオメトリ更新)',
}

#: Viewport header text is ASCII-only — VTK viewport text drops CJK glyphs
#: entirely (#999), so honesty lines must be readable in ASCII.
_VIEWPORT_LIFECYCLE = {'proposed': 'PROPOSED', 'installed': 'INSTALLED'}
_VIEWPORT_NOTICE = {
    'lapsed': 'Lapsed binding (stale scene revision)',
    'unbound': 'No host surface bound',
    'out_of_bounds': 'Out of bounds (empty clipped patch)',
    'underivable': 'Patch not derivable on host plane',
    'unreadable': 'Placement authority unreadable',
}


@dataclass(frozen=True, slots=True)
class TreatmentOverlayPatch:
    """One drawable clipped patch resolved against the exact head (#1009)."""

    placement: AcousticTreatmentPlacement
    footprint: TreatmentFootprint
    lifecycle: str
    #: Clipped patch rings in VTK render coordinates (x, -y, z applied).
    render_polygons: tuple[tuple[tuple[float, float, float], ...], ...]
    patch_area_m2: float
    rectangle_area_m2: float
    #: patch_area / rectangle area — how much of the authored panel actually
    #: covers the wall. <1 means the rectangle extends past the surface.
    effective_ratio: float
    clipped: bool
    covers_entire_surface: bool
    #: instance_ids of same-host placements whose derived patches overlap.
    overlap_with: tuple[str, ...]

    @property
    def warning(self) -> bool:
        return bool(self.overlap_with) or self.clipped


@dataclass(frozen=True, slots=True)
class TreatmentOverlayNotice:
    """A placement that contributes no drawn patch — surfaced, never silent."""

    instance_id: str
    lifecycle: str | None
    kind: str  # key of NOTICE_KIND_LABELS
    binding_state: str | None
    message: str
    viewport_line: str


@dataclass(frozen=True, slots=True)
class TreatmentOverlayScene:
    """Everything one render pass draws/announces for the current head."""

    revision_id: str
    revision_content_hash: str
    patches: tuple[TreatmentOverlayPatch, ...]
    notices: tuple[TreatmentOverlayNotice, ...]
    #: ASCII-only lines for VTK viewport text.
    viewport_lines: tuple[str, ...]
    #: JA lines for Qt surfaces (panel coverage list / status).
    summary_ja: tuple[str, ...]


def _binding_label(binding_state: str | None) -> str:
    return BINDING_STATE_LABELS.get(binding_state or '', binding_state or '不明')


def footprint_render_polygons(
    footprint: TreatmentFootprint,
) -> tuple[tuple[tuple[float, float, float], ...], ...]:
    """Host-plane UV rings -> world domain -> VTK render space (x, -y, z).

    The patch is reconstructed on the surface's own plane frame, then run
    through the same :func:`domain_to_render` flip the field overlay uses,
    so patch orientation on screen matches every other surface render.
    """

    origin = footprint.plane_origin_m
    u_axis = footprint.plane_u_axis
    v_axis = footprint.plane_v_axis
    polygons: list[tuple[tuple[float, float, float], ...]] = []
    for ring in footprint.patch_uv_polygons:
        points: list[tuple[float, float, float]] = []
        for u, v in ring:
            domain = Position3(
                x_m=origin[0] + u * u_axis[0] + v * v_axis[0],
                y_m=origin[1] + u * u_axis[1] + v * v_axis[1],
                z_m=origin[2] + u * u_axis[2] + v * v_axis[2],
            )
            points.append(domain_to_render(domain))
        polygons.append(tuple(points))
    return tuple(polygons)


def _overlapping_peers(
    footprints: dict[str, TreatmentFootprint],
) -> dict[str, tuple[str, ...]]:
    """instance_id -> overlapping same-host instance_ids (#1009).

    Footprints on one host share that host's UV frame, so patch rings are
    directly comparable. Overlap beyond tolerance is flagged — the same
    conservatism the solver applies (BLOCKED_OVERLAP) — but the patches
    still draw so the conflict stays visible instead of hidden.
    """

    overlap: dict[str, set[str]] = {key: set() for key in footprints}
    by_host: dict[str, list[str]] = {}
    for instance_id, footprint in footprints.items():
        by_host.setdefault(footprint.host_surface_id, []).append(instance_id)
    for ids in by_host.values():
        polygons = {
            instance_id: [
                Polygon(ring)
                for ring in footprints[instance_id].patch_uv_polygons
                if len(ring) >= 3
            ]
            for instance_id in ids
        }
        for index, first in enumerate(ids):
            for second in ids[index + 1 :]:
                intersects = any(
                    a.intersects(b)
                    and a.intersection(b).area > FOOTPRINT_OVERLAP_TOLERANCE_M2
                    for a in polygons[first]
                    for b in polygons[second]
                )
                if intersects:
                    overlap[first].add(second)
                    overlap[second].add(first)
    return {key: tuple(sorted(value)) for key, value in overlap.items()}


def resolve_treatment_overlay(
    scene_repository: SceneRepository,
    treatment_repository: CadAcousticTreatmentRepository,
    document_id: str,
) -> TreatmentOverlayScene | None:
    """Resolve the coverage overlay for the document's CURRENT head.

    Returns ``None`` when the document has no head revision. Every placement
    is re-evaluated against the live head — never a revision captured at
    arm time — so a scene edit leaves no stale patch drawn (#999 lesson).
    """

    head = scene_repository.current_head(document_id)
    if head is None:
        return None
    try:
        latest = treatment_repository.latest_placements_for_document(document_id)
    except ValueError as exc:
        notice = TreatmentOverlayNotice(
            instance_id='(配置一覧)',
            lifecycle=None,
            kind='unreadable',
            binding_state=None,
            message=f'配置一覧を再検証できません: {exc}',
            viewport_line='placements unreadable: authority revalidation failed',
        )
        return TreatmentOverlayScene(
            revision_id=head.revision_id,
            revision_content_hash=head.content_hash,
            patches=(),
            notices=(notice,),
            viewport_lines=(
                'treatments: placement list unreadable — nothing drawn',
                f'rev {head.revision_id[:8]} CURRENT',
            ),
            summary_ja=(notice.message,),
        )

    geometry = head.document.r120_semantic_geometry
    footprints: dict[str, TreatmentFootprint] = {}
    clipped_pending: list[tuple[AcousticTreatmentPlacement, TreatmentFootprint]] = []
    notices: list[TreatmentOverlayNotice] = []

    for placement in latest:
        bound_to_head = placement.scene_revision_id == head.revision_id
        try:
            evaluation = treatment_repository.evaluate_placement_surface_binding(
                placement,
                scene_revision_id=head.revision_id,
            )
        except ValueError as exc:
            notices.append(
                TreatmentOverlayNotice(
                    instance_id=placement.instance_id,
                    lifecycle=placement.lifecycle,
                    kind='unreadable',
                    binding_state=None,
                    message=(
                        f'{placement.instance_id}: 配置権威を再検証できません ({exc})'
                    ),
                    viewport_line=(
                        f'{placement.instance_id}: authority revalidation failed'
                    ),
                )
            )
            continue
        state = evaluation.binding_state
        if not bound_to_head or state != 'exact':
            kind = 'unbound' if state == 'unbound' else 'lapsed'
            notices.append(
                TreatmentOverlayNotice(
                    instance_id=placement.instance_id,
                    lifecycle=placement.lifecycle,
                    kind=kind,
                    binding_state=state,
                    message=(
                        f'{placement.instance_id}: {NOTICE_KIND_LABELS[kind]}'
                        f' — {_binding_label(state)}'
                    ),
                    viewport_line=(
                        f'{placement.instance_id}: '
                        f'{_VIEWPORT_NOTICE[kind]} [{state}]'
                    ),
                )
            )
            continue
        footprint = (
            None if geometry is None else derive_treatment_footprint(placement, geometry)
        )
        if footprint is None:
            notices.append(
                TreatmentOverlayNotice(
                    instance_id=placement.instance_id,
                    lifecycle=placement.lifecycle,
                    kind='underivable',
                    binding_state=state,
                    message=(
                        f'{placement.instance_id}: 実効パッチを導出できません'
                        ' — ホスト面が平面として解けません'
                    ),
                    viewport_line=(
                        f'{placement.instance_id}: '
                        f'{_VIEWPORT_NOTICE["underivable"]}'
                    ),
                )
            )
            continue
        if footprint.patch_area_m2 <= FOOTPRINT_OVERLAP_TOLERANCE_M2:
            notices.append(
                TreatmentOverlayNotice(
                    instance_id=placement.instance_id,
                    lifecycle=placement.lifecycle,
                    kind='out_of_bounds',
                    binding_state=state,
                    message=(
                        f'{placement.instance_id}: 範囲外 — '
                        '配置矩形はホスト面上に実効パッチを持ちません'
                    ),
                    viewport_line=(
                        f'{placement.instance_id}: '
                        f'{_VIEWPORT_NOTICE["out_of_bounds"]}'
                    ),
                )
            )
            continue
        footprints[placement.instance_id] = footprint
        clipped_pending.append((placement, footprint))

    overlap = _overlapping_peers(footprints)
    patches: list[TreatmentOverlayPatch] = []
    for placement, footprint in clipped_pending:
        rectangle_area = float(placement.coverage.width_m) * float(
            placement.coverage.height_m
        )
        ratio = min(
            1.0,
            footprint.patch_area_m2 / rectangle_area if rectangle_area else 0.0,
        )
        patches.append(
            TreatmentOverlayPatch(
                placement=placement,
                footprint=footprint,
                lifecycle=placement.lifecycle,
                render_polygons=footprint_render_polygons(footprint),
                patch_area_m2=footprint.patch_area_m2,
                rectangle_area_m2=rectangle_area,
                effective_ratio=ratio,
                clipped=(
                    footprint.patch_area_m2
                    < rectangle_area - FOOTPRINT_COVERAGE_MISMATCH_TOLERANCE
                ),
                covers_entire_surface=footprint.covers_entire_surface,
                overlap_with=overlap.get(placement.instance_id, ()),
            )
        )

    proposed = sum(1 for p in patches if p.lifecycle == 'proposed')
    installed = sum(1 for p in patches if p.lifecycle == 'installed')
    warned = sum(1 for p in patches if p.warning)
    lines = [
        f'treatments: {len(patches)} drawn '
        f'({proposed} proposed, {installed} installed) | '
        f'rev {head.revision_id[:8]} | CURRENT',
    ]
    if notices:
        lines.append(
            f'not drawn: {len(notices)} - '
            + '; '.join(notice.viewport_line for notice in notices[:4])
            + ('; ...' if len(notices) > 4 else '')
        )
    for patch in patches[:6]:
        extras = []
        if patch.clipped:
            extras.append('clipped')
        if patch.overlap_with:
            extras.append('OVERLAP')
        lines.append(
            f'{patch.placement.instance_id}: '
            f'{_VIEWPORT_LIFECYCLE.get(patch.lifecycle, patch.lifecycle)} '
            f'eff {patch.patch_area_m2:.2f}/{patch.rectangle_area_m2:.2f} m2 '
            f'({patch.effective_ratio:.0%})'
            + (f' [{" ".join(extras)}]' if extras else '')
        )
    if len(patches) > 6:
        lines.append(f'... {len(patches) - 6} more')

    summary: list[str] = []
    for patch in patches:
        extras = []
        if patch.clipped:
            extras.append('矩形が面をはみ出し — クリップ済み部分のみ描画')
        if patch.overlap_with:
            extras.append(
                '重複警告: ' + ' / '.join(patch.overlap_with)
            )
        summary.append(
            f'{patch.placement.instance_id}: '
            f'{LIFECYCLE_LABELS.get(patch.lifecycle, patch.lifecycle)} · '
            f'実効 {patch.patch_area_m2:.2f} m² / '
            f'矩形 {patch.rectangle_area_m2:.2f} m² '
            f'({patch.effective_ratio:.0%})'
            + (f' · {"; ".join(extras)}' if extras else '')
        )
    summary.extend(notice.message for notice in notices)
    return TreatmentOverlayScene(
        revision_id=head.revision_id,
        revision_content_hash=head.content_hash,
        patches=tuple(patches),
        notices=tuple(notices),
        viewport_lines=tuple(lines),
        summary_ja=tuple(summary),
    )


class RoomTreatmentOverlayController:
    """Per-render resolve() with a content-keyed cache (#1009).

    The cache key is (head revision id + content hash + latest placement
    hashes), so a scene edit — which mints a new head — never replays the
    previous revision's footprints, and an unchanged scene resolves for free.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        treatment_repository: CadAcousticTreatmentRepository,
        document_id: str,
    ) -> None:
        self.scene_repository = scene_repository
        self.treatment_repository = treatment_repository
        self.document_id = document_id
        self._cache_key: tuple | None = None
        self._cache: TreatmentOverlayScene | None = None

    def invalidate(self) -> None:
        self._cache_key = None
        self._cache = None

    def resolve(self) -> TreatmentOverlayScene | None:
        head = self.scene_repository.current_head(self.document_id)
        if head is None:
            self._cache_key = None
            self._cache = None
            return None
        try:
            latest = self.treatment_repository.latest_placements_for_document(
                self.document_id
            )
        except ValueError:
            latest = ()
        key = (
            head.revision_id,
            head.content_hash,
            tuple(
                (p.instance_id, p.placement_sha256) for p in latest
            ),
        )
        if key != self._cache_key:
            self._cache = resolve_treatment_overlay(
                self.scene_repository,
                self.treatment_repository,
                self.document_id,
            )
            self._cache_key = key
        return self._cache
