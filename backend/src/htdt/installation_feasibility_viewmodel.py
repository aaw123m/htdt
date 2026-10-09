"""Installation feasibility view model (Issue #1005).

"Fits in 3D" is not the same as "installable with evidence". This module
is the read-only bridge between the persisted mounting authorities and
the CAD inspection surface:

- each persisted ``SpeakerInstallationContext`` (the mounting record
  authored on the placement page) resolves to an ``InstallationRequest``
  bound to the *exact* scene entity and the *exact* construction element
  — a ``wall_id`` host binds the wall segment, ceiling/floor modes bind
  the slab, an entity host means the mount lands on furniture/equipment
  (not a construction element) and honestly evaluates not_applicable;
- every verdict comes from :func:`installation_feasibility` verbatim —
  the view never re-judges a check, it only renders what the authority
  already decided;
- substrate/cavity/framing gaps surface as UNKNOWN (灰色・不明) — never
  as a red failure — and translate to an explicit list of on-site
  construction confirmations;
- translucent preview volumes (service-clearance box, cutout recess,
  cavity extent) only appear for dimensions with declared evidence —
  the entity envelope, the bound wall segment, or an assembly cavity
  layer. Nothing invents a behind-wall position or stud pitch: without
  cavity evidence the item carries a confirmation instead of a shape.

The preview is rebuilt from the live document on every render pass, so a
scene edit, a new context record or an assembly change lapses every glyph
honestly — no stale ``supported`` survives its head revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from pydantic import BaseModel, ConfigDict

from .cad_attachment_models import AssemblyElementKind
from .cad_construction_assembly import ElementEvidence, element_evidence
from .cad_installation_context import SpeakerInstallationContext
from .cad_installation_feasibility import (
    FeasibilityCheck,
    FeasibilityVerdict,
    InstallationFeasibilityReport,
    InstallationRequest,
    MountSurfaceKind,
    installation_feasibility,
)
from .cad_orientation_constraints import entity_horizontal_footprint
from .cad_scene import (
    SceneDocument,
    SceneEntity,
    room_vertices,
    scene_content_hash,
)


#: Verdict vocabulary — Japanese display label + actor color. UNKNOWN is
#: deliberately gray: it is an evidence gap to confirm on site, NEVER a
#: red failure. Colors are explanation symbols only.
FEASIBILITY_VERDICT_VOCAB: dict[str, tuple[str, str]] = {
    'supported': ('証拠あり', '#59d98c'),
    'conflict': ('不適合', '#e05555'),
    'unknown': ('不明', '#8a93a3'),
    'not_applicable': ('対象外', '#6b7280'),
}
FEASIBILITY_VERDICTS: tuple[str, ...] = tuple(FEASIBILITY_VERDICT_VOCAB)

#: The six FeasibilityCheck ids → JA row labels.
FEASIBILITY_CHECK_LABELS: dict[str, str] = {
    'mount_surface': '取付面',
    'substrate': '基材',
    'payload': '荷重',
    'framing': '下地（間柱）',
    'cutout': '開口深さ',
    'service_clearance': '保守空間',
}

#: Item states that carry no authority report — mounting intent exists but
#: the request cannot be honestly bound/evaluated. All render with the
#: UNKNOWN gray, never red.
FEASIBILITY_ITEM_STATE_LABELS: dict[str, str] = {
    'undeclared': '設置方式が未記録',        # mounting_mode 'unknown'
    'unbound_host': '取付先が未確定',        # no/dangling wall or element host
    'missing_entity': '対象機器が不在',      # context survives a deleted entity
}

#: Header disclaimer — this is an evidence inspection aid, never a permit.
INSTALLATION_FEASIBILITY_DISCLAIMER = (
    '設置実現性の表示は施工許可・建築基準・耐荷重法規への適合認定ではありません。'
    '登録された取付情報と構造アセンブリ証拠に基づく参考検査です。'
    '「不明」は不適合ではなく、現場で確認すべき項目を意味します。'
)

_MOUNT_MODE_LABELS: dict[str, str] = {
    'free_standing': '自立（床置き）',
    'stand': 'スタンド',
    'shelf': '棚置き',
    'wall': '壁掛け',
    'ceiling': '天井吊り',
    'in_wall': '壁埋込み',
    'in_ceiling': '天井埋込み',
    'custom': '独自',
    'unknown': '未記録',
}

_ELEMENT_LABELS: dict[str, str] = {
    'wall': '壁',
    'ceiling': '天井',
    'floor': '床',
    'soffit': '軒裏',
    'riser': '立ち上がり',
}

_ELEMENT_SLAB_Z = {'ceiling': 'top', 'floor': 'bottom'}

_EPS = 1e-9


class _DerivedRequest(BaseModel):
    """Result of resolving one context into an InstallationRequest."""

    model_config = ConfigDict(frozen=True)
    state: str  # 'evaluated' | 'undeclared' | 'unbound_host' | 'missing_entity'
    request: InstallationRequest | None = None
    mount_surface: str | None = None
    element: str | None = None
    element_ref: str | None = None
    element_label: str = ''
    host_kind: str = 'none'  # 'wall' | 'slab' | 'entity' | 'none'
    payload_declared: bool = False
    recess_depth_m: float | None = None


def _mountable_payload_kg(entity: SceneEntity) -> float | None:
    """The entity's declared mounting payload, if a binding declares one.

    ``mountable.max_payload_kg`` is the only declared payload evidence an
    entity can carry; when absent the payload requirement is honestly
    flagged undeclared instead of inventing a weight.
    """

    for binding in entity.semantic_bindings or ():
        if binding.capability != 'mountable':
            continue
        value = binding.parameters.get('max_payload_kg')
        if value is None:
            continue
        payload = float(value)
        if payload > 0.0:
            return payload
    return None


def _wall_geometry(
    document: SceneDocument, wall_id: str
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]] | None:
    """(from_xy, direction_unit, inward_normal_unit) for a bound wall id."""

    topology = document.wall_topology
    if topology is None or document.room is None:
        return None
    wall = next((w for w in topology.walls if w.wall_id == wall_id), None)
    if wall is None:
        return None
    vertices = {v.vertex_id: v for v in room_vertices(document.room)}
    a = vertices.get(wall.from_vertex_id)
    b = vertices.get(wall.to_vertex_id)
    if a is None or b is None:
        return None
    ax, ay = float(a.x_m), float(a.y_m)
    bx, by = float(b.x_m), float(b.y_m)
    length = hypot(bx - ax, by - ay)
    if length <= _EPS:
        return None
    u = ((bx - ax) / length, (by - ay) / length)
    # Two perpendiculars; pick the one pointing at the room centroid —
    # the recess direction is the opposite (into the wall cavity).
    nx, ny = -u[1], u[0]
    coords = [(float(v.x_m), float(v.y_m)) for v in room_vertices(document.room)]
    cx = sum(c[0] for c in coords) / len(coords)
    cy = sum(c[1] for c in coords) / len(coords)
    mx, my = (ax + bx) * 0.5, (ay + by) * 0.5
    if nx * (cx - mx) + ny * (cy - my) < 0.0:
        nx, ny = -nx, -ny
    return (ax, ay), u, (nx, ny), length


def _footprint_spans(
    entity: SceneEntity, u: tuple[float, float], n: tuple[float, float]
) -> tuple[float, float]:
    """(span along u, span along n) of the entity's XY footprint."""

    footprint = entity_horizontal_footprint(entity)
    coords = list(footprint.exterior.coords) if hasattr(footprint, 'exterior') else [
        (float(entity.position.x_m), float(entity.position.y_m))
    ]
    us = [x * u[0] + y * u[1] for x, y, *_ in coords]
    ns = [x * n[0] + y * n[1] for x, y, *_ in coords]
    return (max(us) - min(us)) if us else 0.0, (max(ns) - min(ns)) if ns else 0.0


def _derive_request(
    document: SceneDocument,
    context: SpeakerInstallationContext,
    *,
    service_clearance_m: float | None,
) -> _DerivedRequest:
    """Map one persisted context to a bound InstallationRequest.

    Every request field traces to declared authority: the entity's
    mountable payload binding, the recorded host wall, the entity's
    declared envelope (recess depth for in-wall mounts), and the service
    envelope's clearance. Nothing else is assumed — an undeclared input
    surfaces as a confirmation item, not a fabricated number.
    """

    entities = {entity.entity_id: entity for entity in document.entities}
    entity = entities.get(context.entity_id)
    if entity is None:
        return _DerivedRequest(state='missing_entity', element_label='対象不在')

    mode = context.selected_mounting_mode
    host = context.host_entity_id

    # 'custom' declares "a mount exists" without a surface vocabulary;
    # the host decides the surface it honestly binds. 'unknown' records
    # nothing — the request itself cannot be declared.
    if mode == 'unknown':
        return _DerivedRequest(
            state='undeclared',
            element_label='設置方式 未記録',
        )

    mount_surface: MountSurfaceKind
    element: AssemblyElementKind | None = None
    element_ref: str | None = None
    element_label = ''
    host_kind = 'none'
    cutout_required = mode in ('in_wall', 'in_ceiling')
    recess_depth_m: float | None = None
    wall_info = None

    if mode in ('wall', 'in_wall') or (mode == 'custom' and host):
        if host is None:
            return _DerivedRequest(
                state='unbound_host',
                element_label='取付壁 未指定',
                host_kind='none',
            )
        wall_ids = (
            {wall.wall_id for wall in document.wall_topology.walls}
            if document.wall_topology is not None
            else set()
        )
        if host in wall_ids:
            element, element_ref, host_kind = 'wall', host, 'wall'
            element_label = f'壁 {host}'
            wall_info = _wall_geometry(document, host)
            if wall_info is None:
                return _DerivedRequest(
                    state='unbound_host',
                    element='wall',
                    element_ref=host,
                    element_label=f'壁 {host}（形状なし）',
                    host_kind='wall',
                )
        elif host in entities:
            # Mounted on furniture/equipment — not a construction element.
            mount_surface = 'furniture_top'
            element_label = f'機器・家具上（{host}）'
            host_kind = 'entity'
        else:
            return _DerivedRequest(
                state='unbound_host',
                element_label=f'取付先 {host} が不明',
                host_kind='none',
            )
        if host_kind == 'wall':
            mount_surface = 'wall'
    elif mode in ('ceiling', 'in_ceiling'):
        mount_surface, element, element_ref, host_kind = (
            'ceiling', 'ceiling', 'ceiling', 'slab'
        )
        element_label = '天井'
    elif mode == 'free_standing':
        mount_surface, element, element_ref, host_kind = (
            'floor', 'floor', 'floor', 'slab'
        )
        element_label = '床'
    elif mode in ('stand', 'shelf'):
        mount_surface = 'furniture_top'
        element_label = '家具・機器上'
        host_kind = 'entity'
    else:  # 'custom' without a host
        return _DerivedRequest(
            state='undeclared',
            element_label='独自設置（取付先未指定）',
        )

    if cutout_required and wall_info is not None and element == 'wall':
        _u, n = wall_info[1], wall_info[2]
        _span_u, span_n = _footprint_spans(entity, wall_info[1], n)
        recess_depth_m = span_n
    elif mode == 'in_ceiling':
        recess_depth_m = (
            float(entity.size_m.z_m) if entity.size_m is not None else None
        )

    if cutout_required and (
        recess_depth_m is None or recess_depth_m <= _EPS
    ):
        # The recess depth cannot be derived from declared geometry —
        # the request cannot honestly claim a cutout dimension.
        return _DerivedRequest(
            state='undeclared',
            element=element,
            element_ref=element_ref,
            element_label=f'{element_label}（掘込深さ未宣言）',
            host_kind=host_kind,
        )

    payload = _mountable_payload_kg(entity)
    request = InstallationRequest(
        mount_surface=mount_surface,
        entity_id=context.entity_id,
        element=element,
        element_ref=element_ref,
        payload_kg=payload if payload is not None else 0.0,
        cutout_required=cutout_required,
        cutout_depth_m=recess_depth_m if cutout_required else None,
        service_clearance_m=float(service_clearance_m or 0.0),
        label=_MOUNT_MODE_LABELS.get(mode, mode),
    )
    return _DerivedRequest(
        state='evaluated',
        request=request,
        mount_surface=mount_surface,
        element=element,
        element_ref=element_ref,
        element_label=element_label,
        host_kind=host_kind,
        payload_declared=payload is not None,
        recess_depth_m=recess_depth_m,
    )


@dataclass(frozen=True, slots=True)
class FeasibilityCheckRow:
    """One authority check, presentation-bound. Detail stays verbatim."""

    check: str
    label: str
    verdict: FeasibilityVerdict
    verdict_label: str
    detail: str


@dataclass(frozen=True, slots=True)
class WallMountFace:
    """Mount-face rectangle on a bound wall, domain coordinates.

    ``center`` is the entity's nearest-point projection on the wall
    segment at the entity's declared height — every number is derived
    from declared geometry, never a guessed behind-wall position.
    """

    wall_id: str
    center: tuple[float, float, float]
    axis_u: tuple[float, float, float]  # along-wall unit (domain XY, z=0)
    width_m: float                      # entity span along the wall
    height_m: float                     # entity vertical extent
    recess_direction: tuple[float, float, float]  # into the wall cavity


@dataclass(frozen=True, slots=True)
class SlabMountFace:
    """Mount face on the ceiling/floor slab — the entity XY footprint."""

    element: str  # 'ceiling' | 'floor'
    footprint: object  # shapely geometry, domain XY
    z_m: float
    anchor: tuple[float, float, float]
    recess_direction: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class DepthVolume:
    """A translucent depth prism extruded from the mount face.

    ``source_label`` states whose declared dimension this is: a request
    requirement (要求値) or assembly cavity evidence (アセンブリ証拠).
    """

    depth_m: float
    source_label: str


@dataclass(frozen=True, slots=True)
class InstallationFeasibilityItem:
    """One mounting context resolved against the current document."""

    context_id: str
    context_sha256: str
    entity_id: str
    entity_name: str | None
    mounting_label: str
    state: str  # 'evaluated' | item-state key
    state_label: str
    element: str | None
    element_ref: str | None
    element_label: str
    host_kind: str
    request: InstallationRequest | None
    report: InstallationFeasibilityReport | None
    overall: FeasibilityVerdict | None
    overall_label: str
    checks: tuple[FeasibilityCheckRow, ...]
    confirmations: tuple[str, ...]
    undeclared_inputs: frozenset[str]
    #: Glyph anchors — entity top and the bound substrate face.
    entity_anchor: tuple[float, float, float] | None
    substrate_anchor: tuple[float, float, float] | None
    wall_face: WallMountFace | None
    slab_face: SlabMountFace | None
    #: Evidence-gated translucent volumes (see module docstring).
    cutout_volume: DepthVolume | None
    service_volume: DepthVolume | None
    cavity_volume: DepthVolume | None
    evidence_source: str | None


@dataclass(frozen=True, slots=True)
class InstallationFeasibilityPreview:
    """Everything the viewport and panel render for this head."""

    document_id: str
    head_sha256: str
    items: tuple[InstallationFeasibilityItem, ...]
    summary: str
    verdict_legend: tuple[tuple[str, str], ...]
    disclaimer: str


def _check_rows(
    checks: tuple[FeasibilityCheck, ...],
) -> tuple[FeasibilityCheckRow, ...]:
    return tuple(
        FeasibilityCheckRow(
            check=check.check,
            label=FEASIBILITY_CHECK_LABELS.get(check.check, check.check),
            verdict=check.verdict,
            verdict_label=FEASIBILITY_VERDICT_VOCAB[check.verdict][0],
            detail=check.detail,
        )
        for check in checks
    )


def _confirmations(
    *,
    derived: _DerivedRequest,
    entity: SceneEntity | None,
    evidence: ElementEvidence | None,
    context: SpeakerInstallationContext,
) -> tuple[str, ...]:
    """On-site construction confirmations — the UNKNOWN guidance path."""

    items: list[str] = []
    if entity is None:
        items.append(
            '対象機器が現在の場面に存在しません — 設置記録が古い可能性があります'
        )
        return tuple(items)
    if derived.state == 'undeclared':
        if context.selected_mounting_mode == 'unknown':
            items.append('設置方式が未記録です — 取付方法を現場で確認・記録してください')
        else:
            items.append('取付に必要な寸法・取付先が宣言されていません — 現場で確認してください')
        return tuple(items)
    if derived.state == 'unbound_host':
        items.append('取付先の壁・要素が確定していません — 取付位置を指定してください')
        return tuple(items)
    if evidence is None:
        # Non-construction host (furniture_top / rack) — the authority
        # already reports not_applicable; nothing to confirm on site.
        if derived.host_kind == 'entity':
            items.append(
                '構造要素外への取付です — 取付先機器の強度・条件を確認してください'
            )
    elif evidence.assembly_id is None:
        items.append(
            '取付面の構造アセンブリが未宣言です — 層構成・基材を記録してください'
        )
    else:
        if evidence.substrate_material == 'unknown':
            items.append('取付面の基材を現場で確認してください（石膏ボード・下地等）')
        if evidence.cavity_depth_m is None and (
            (derived.request is not None and derived.request.cutout_required)
            or (derived.request is not None and derived.request.service_clearance_m > 0.0)
        ):
            items.append('壁裏・天井裏の空間寸法（キャビティ深さ）を確認してください')
        if evidence.framing_material is None and (
            derived.request is not None and derived.request.requires_framing
        ):
            items.append('下地（間柱）の有無・位置を確認してください')
    if not derived.payload_declared:
        items.append(
            '機材の質量（荷重）が未宣言です — 総合判定は未評価のままです。'
            '現地で計測・確認してください'
        )
    return tuple(items)


def _wall_mount_face(
    entity: SceneEntity,
    wall_id: str,
    wall_info,
) -> tuple[WallMountFace, tuple[float, float, float]] | None:
    """Mount face on the bound wall + substrate glyph anchor."""

    (ax, ay), u, n, length = wall_info
    ex, ey = float(entity.position.x_m), float(entity.position.y_m)
    t = ((ex - ax) * u[0] + (ey - ay) * u[1]) / max(length, _EPS)
    t = min(max(t, 0.0), 1.0)
    mx, my = ax + u[0] * length * t, ay + u[1] * length * t
    span_u, _span_n = _footprint_spans(entity, u, n)
    height = float(entity.size_m.z_m) if entity.size_m is not None else 0.0
    if span_u <= _EPS or height <= _EPS:
        return None
    ez = float(entity.position.z_m)
    face = WallMountFace(
        wall_id=wall_id,
        center=(mx, my, ez),
        axis_u=(u[0], u[1], 0.0),
        width_m=span_u,
        height_m=height,
        recess_direction=(-n[0], -n[1], 0.0),
    )
    anchor = (mx, my, ez + height * 0.5)
    return face, anchor


def _slab_mount_face(
    document: SceneDocument,
    entity: SceneEntity,
    element: str,
) -> SlabMountFace | None:
    """Mount face on ceiling/floor — the exact declared footprint."""

    if document.room is None:
        return None
    z = (
        float(document.room.height_m) if element == 'ceiling' else 0.0
    )
    footprint = entity_horizontal_footprint(entity)
    if footprint is None or footprint.is_empty:
        return None
    recess = (0.0, 0.0, 1.0) if element == 'ceiling' else (0.0, 0.0, -1.0)
    return SlabMountFace(
        element=element,
        footprint=footprint,
        z_m=z,
        anchor=(float(entity.position.x_m), float(entity.position.y_m), z),
        recess_direction=recess,
    )


def _build_item(
    document: SceneDocument,
    context: SpeakerInstallationContext,
    *,
    service_clearance_m: float | None,
) -> InstallationFeasibilityItem:
    derived = _derive_request(
        document, context, service_clearance_m=service_clearance_m
    )
    entity = next(
        (e for e in document.entities if e.entity_id == context.entity_id),
        None,
    )
    entity_anchor = None
    if entity is not None:
        top_z = float(entity.position.z_m) + (
            float(entity.size_m.z_m) * 0.5
            if entity.size_m is not None
            else 0.0
        )
        entity_anchor = (
            float(entity.position.x_m),
            float(entity.position.y_m),
            top_z,
        )

    report: InstallationFeasibilityReport | None = None
    evidence: ElementEvidence | None = None
    checks: tuple[FeasibilityCheckRow, ...] = ()
    if derived.state == 'evaluated' and derived.request is not None:
        report = installation_feasibility(document, derived.request)
        checks = _check_rows(report.checks)
        if derived.element is not None and derived.element_ref is not None:
            evidence = element_evidence(
                document, derived.element, derived.element_ref  # type: ignore[arg-type]
            )

    # Mount face + translucent volumes — every one gated on declared dims.
    wall_face: WallMountFace | None = None
    slab_face: SlabMountFace | None = None
    substrate_anchor: tuple[float, float, float] | None = None
    cutout_volume: DepthVolume | None = None
    service_volume: DepthVolume | None = None
    cavity_volume: DepthVolume | None = None
    if entity is not None and derived.state == 'evaluated':
        if derived.element == 'wall' and derived.element_ref is not None:
            wall_info = _wall_geometry(document, derived.element_ref)
            if wall_info is not None:
                resolved = _wall_mount_face(
                    entity, derived.element_ref, wall_info
                )
                if resolved is not None:
                    wall_face, substrate_anchor = resolved
        elif derived.element in _ELEMENT_SLAB_Z:
            slab_face = _slab_mount_face(document, entity, derived.element)
            if slab_face is not None:
                substrate_anchor = slab_face.anchor
        if wall_face is not None or slab_face is not None:
            request = derived.request
            if request is not None and request.cutout_required:
                cutout_volume = DepthVolume(
                    depth_m=float(request.cutout_depth_m or 0.0),
                    source_label='要求値（機器寸法）',
                )
            if (
                request is not None
                and float(request.service_clearance_m) > 0.0
            ):
                service_volume = DepthVolume(
                    depth_m=float(request.service_clearance_m),
                    source_label='要求値（保守エンベロープ）',
                )
            if evidence is not None and evidence.cavity_depth_m is not None:
                cavity_volume = DepthVolume(
                    depth_m=float(evidence.cavity_depth_m),
                    source_label='アセンブリ証拠',
                )

    # Headline honesty: the authority's checks are shown verbatim, but an
    # overall verdict only promotes when every input the verdict depends
    # on was actually declared. A mount with no declared payload must
    # never paint green 'supported' — evaluating it at 0.0 kg is exactly
    # the invented evidence this surface exists to eliminate. Non-
    # construction surfaces carry no load check, so payload undeclared
    # does not gate them.
    undeclared_inputs: frozenset[str] = frozenset(
        (() if derived.payload_declared else ('payload',))
    )
    construction_surface = (
        derived.mount_surface in ('wall', 'ceiling', 'floor')
    )
    inputs_complete = (
        derived.payload_declared or not construction_surface
    )
    overall = (
        report.overall
        if report is not None and inputs_complete
        else None
    )
    confirmations = _confirmations(
        derived=derived, entity=entity, evidence=evidence, context=context
    )
    state_label = (
        FEASIBILITY_ITEM_STATE_LABELS.get(derived.state, derived.state)
        if derived.state != 'evaluated'
        else ''
    )
    return InstallationFeasibilityItem(
        context_id=context.context_id,
        context_sha256=context.semantic_sha256,
        entity_id=context.entity_id,
        entity_name=None if entity is None else entity.name,
        mounting_label=_MOUNT_MODE_LABELS.get(
            context.selected_mounting_mode, context.selected_mounting_mode
        ),
        state=derived.state,
        state_label=state_label,
        element=derived.element,
        element_ref=derived.element_ref,
        element_label=derived.element_label,
        host_kind=derived.host_kind,
        request=derived.request,
        report=report,
        overall=overall,
        overall_label=(
            FEASIBILITY_VERDICT_VOCAB[overall][0]
            if overall is not None
            else state_label or '不明'
        ),
        checks=checks,
        confirmations=confirmations,
        undeclared_inputs=undeclared_inputs,
        entity_anchor=entity_anchor,
        substrate_anchor=substrate_anchor,
        wall_face=wall_face,
        slab_face=slab_face,
        cutout_volume=cutout_volume,
        service_volume=service_volume,
        cavity_volume=cavity_volume,
        evidence_source=(
            None if evidence is None else evidence.evidence_source
        ),
    )


def build_installation_feasibility_preview(
    *,
    document: SceneDocument,
    contexts: tuple[SpeakerInstallationContext, ...] | list[SpeakerInstallationContext],
    service_clearances: dict[str, float] | None = None,
) -> InstallationFeasibilityPreview:
    """Resolve every persisted mounting context against THIS document.

    Called on every render refresh with ``controller.document`` — verdicts,
    glyph anchors and preview volumes always derive from the same live
    head, so an edited scene or a changed assembly can never leave a stale
    'supported' on screen. ``head_sha256`` keys the whole preview to the
    exact content it was evaluated from.
    """

    clearances = dict(service_clearances or {})
    items = tuple(
        _build_item(
            document,
            context,
            service_clearance_m=clearances.get(context.entity_id),
        )
        for context in contexts
    )

    counts = {verdict: 0 for verdict in FEASIBILITY_VERDICTS}
    unevaluated = 0
    confirmation_count = 0
    for item in items:
        if item.overall is not None:
            counts[item.overall] += 1
        else:
            unevaluated += 1
        confirmation_count += len(item.confirmations)

    legend = tuple(FEASIBILITY_VERDICT_VOCAB.values())
    if unevaluated:
        legend += (('未評価', '#8a93a3'),)

    summary = (
        f'設置実現性 · 記録{len(items)}件'
        f' · 証拠あり{counts["supported"]}'
        f' · 不適合{counts["conflict"]}'
        f' · 不明{counts["unknown"]}'
        f' · 対象外{counts["not_applicable"]}'
    )
    if unevaluated:
        summary += f' · 未評価{unevaluated}'
    if confirmation_count:
        summary += f' · 要確認項目{confirmation_count}'

    return InstallationFeasibilityPreview(
        document_id=document.document_id,
        head_sha256=scene_content_hash(document),
        items=items,
        summary=summary,
        verdict_legend=legend,
        disclaimer=INSTALLATION_FEASIBILITY_DISCLAIMER,
    )
