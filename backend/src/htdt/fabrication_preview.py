"""Read-only 3D fabrication preview for issued packages (#1008).

A :class:`TreatmentFabricationPackage` is the sealed, hash-pinned authority
this preview draws — never a recomputed or guessed assembly. The resolver
turns an *issued* package into drawable part boxes (multilayer panel
stacks with air gap, 1D QRD backing/fins/well-depth table), an optional
mount-reference overlay when a canonical ``AcousticTreatmentPlacement``
binds the same definition to the current head, and a deterministic
cut-list/parts report that shares the preview's numbering.

Honesty contract:
- Only sealed packages render: the preview pins ``package_sha256`` plus
  the source definition/spec identity in every header line and export.
- The QRD well order/depth is drawn from ``well_table`` 1:1 — well ``i``
  sits at index ``i`` with exactly ``row.depth_m``. Kerf/tolerances show
  only where the package grounds them; unspecified adhesion/fixing/
  material fields render as ``unspecified``, never an invented default.
- Mounting orientation/clearance draws as REFERENCE geometry only while
  a placement evaluates 'exact' against the current head — and nothing
  here feeds factory dimensions back into the acoustic boundary model.
- Unsupported fabrication families (2D QRD, freeform) never fabricate a
  3D scene: the resolver reports ``supported=False`` so the panel can
  fall back to the table/card view.
- Fabrication output is 設計値由来 (design values): absorption and
  scattering performance live under a different authority.

No Qt imports — the dialog/panel owns widgets; this module owns
resolution so a late render cannot resurrect a superseded package.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cad_acoustic_treatment import AcousticTreatmentPlacement
from .cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from .cad_repository import SceneRepository
from .cad_scene import quaternion_to_matrix3
from .cad_treatment_fabrication import TreatmentFabricationPackage
from .treatment_boundary_overlay import (
    FOOTPRINT_OVERLAP_TOLERANCE_M2,
    derive_treatment_footprint,
)


#: Fabrication families this preview can lay out in 3D. Anything else —
#: e.g. a 2D QRD or freeform family from a newer renderer — falls back
#: to the table/card surface instead of a forced 3D guess.
SUPPORTED_FAMILIES = frozenset({'rectangular_panel', 'qrd_1d'})

#: Shown verbatim wherever the package carries no value (material_ref,
#: tolerances, adhesion/fixing methods) — the preview never invents one.
UNSPECIFIED = 'unspecified'

#: JA vocabulary for part kinds (Qt surfaces; VTK lines stay ASCII).
PART_KIND_LABELS = {
    'absorber_layer': '吸音層',
    'backing_panel': '背板',
    'well_fin': '井戸フィン',
    'frame_member': '枠部材',
    'facing': '表面材',
    'spacer': 'スペーサー',
}

_FAMILY_LABELS = {
    'rectangular_panel': '矩形多層パネル',
    'qrd_1d': '1D QRD 拡散体',
}

#: Neutral-anchor geometry: assembly stands in front of the room's -Y
#: edge when no canonical placement exists.
_NEUTRAL_MARGIN_M = 0.6
#: Per-index exploded separation, as a fraction of overall depth.
_EXPLODE_STEP_RATIO = 0.45
#: Cap on drawn well-depth labels — the table lists every row regardless.
_MAX_WELL_LABELS = 24


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(v, s):
    return (v[0] * s, v[1] * s, v[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _fmt_m(value: float) -> str:
    return f'{value:.3f}'


def _fmt_mm(value: float | None) -> str:
    return UNSPECIFIED if value is None else f'{value:g}'


def _material_label(material_ref: str | None) -> str:
    return material_ref if material_ref else UNSPECIFIED


@dataclass(frozen=True, slots=True)
class PreviewAnchor:
    """Assembly frame in domain coordinates: parts build along
    ``stack_axis`` from ``origin`` (the wall-side plane); ``width_axis``
    x ``height_axis`` span the face rectangle."""

    origin: tuple[float, float, float]
    width_axis: tuple[float, float, float]
    height_axis: tuple[float, float, float]
    stack_axis: tuple[float, float, float]
    #: 'placement' (canonical AcousticTreatmentPlacement) or 'neutral'.
    source: str
    placement_instance_id: str | None
    placement_lifecycle: str | None


@dataclass(frozen=True, slots=True)
class PreviewPartBox:
    """One drawable solid resolved from the package (or a derived air
    gap). ``index`` is the 1-based ``package.parts`` number the export
    report shares — a fin instance repeats its part's P-number."""

    index: int
    part_id: str
    part_kind: str
    cut_group: str
    description: str
    #: Box centre in domain coordinates (exploded offset applied).
    center: tuple[float, float, float]
    #: Extents along (width_axis, height_axis, stack_axis).
    dims_whs: tuple[float, float, float]
    material_label: str
    quantity: int
    selected: bool
    #: Derived volumes that are not cut parts (air gap) render honestly.
    derived: bool
    label: str


@dataclass(frozen=True, slots=True)
class PreviewWellLabel:
    """A 1:1 ``well_table`` row pinned to its drawn position."""

    well_index: int
    depth_m: float
    position: tuple[float, float, float]
    label: str


@dataclass(frozen=True, slots=True)
class PreviewReference:
    """Mount-orientation/clearance reference geometry — drawn only while
    a canonical placement binds the definition to the current head."""

    placement_instance_id: str
    lifecycle: str
    #: Projected authored mount rectangle (domain coords, closed ring).
    mount_ring: tuple[tuple[float, float, float], ...]
    #: Clearance envelope: rect extruded overall_depth along stack axis.
    clearance_center: tuple[float, float, float]
    clearance_dims: tuple[float, float, float]
    clipped_to_surface: bool


@dataclass(frozen=True, slots=True)
class FabricationPreviewScene:
    """Everything one render pass draws/announces for an armed package."""

    package: TreatmentFabricationPackage
    family: str
    supported: bool
    fallback_reason: str | None
    anchor: PreviewAnchor
    parts: tuple[PreviewPartBox, ...]
    well_labels: tuple[PreviewWellLabel, ...]
    reference: PreviewReference | None
    section_fraction: float | None
    section_position: tuple[float, float, float] | None
    exploded_fraction: float
    selected_key: str | None
    selected_detail: tuple[str, ...]
    notices: tuple[str, ...]
    viewport_lines: tuple[str, ...]
    summary_ja: tuple[str, ...]


def _resolve_anchor(
    scene_repository: SceneRepository,
    treatment_repository: CadAcousticTreatmentRepository,
    document_id: str,
    package: TreatmentFabricationPackage,
    placement_instance_id: str | None,
    force_neutral: bool = False,
) -> tuple[PreviewAnchor, PreviewReference | None, tuple[str, ...]]:
    """Resolve the assembly anchor against the CURRENT head.

    A canonical placement (binding 'exact' + derivable footprint on the
    live revision) anchors the stack at the authored mount pose and adds
    the mount/clearance reference geometry. Anything else — no
    placement, a lapsed binding, an underivable patch — falls back to a
    neutral stand in front of the room with an explicit notice.
    """

    head = scene_repository.current_head(document_id)
    notices: list[str] = []
    placement: AcousticTreatmentPlacement | None = None
    if force_neutral:
        notices.append(
            'neutral anchor requested - no mount reference even when a '
            'canonical placement exists'
        )
    elif head is not None:
        try:
            candidates = tuple(
                p
                for p in treatment_repository.latest_placements_for_document(
                    document_id
                )
                if p.definition_id == package.definition_id
            )
        except ValueError:
            candidates = ()
            notices.append(
                'placement list unreadable - neutral anchor, no mount reference'
            )
        if placement_instance_id is not None:
            pinned = [
                p for p in candidates if p.instance_id == placement_instance_id
            ]
            if pinned:
                placement = pinned[0]
            else:
                notices.append(
                    f'pinned placement {placement_instance_id} is not bound '
                    f'to definition {package.definition_id} - neutral anchor'
                )
        elif candidates:
            # Auto-pick the deterministically-first EXACT binding; a
            # non-exact candidate still reports its state, never draws.
            evaluation_states: list[tuple[AcousticTreatmentPlacement, str]] = []
            for candidate in candidates:
                try:
                    evaluation = (
                        treatment_repository.evaluate_placement_surface_binding(
                            candidate, scene_revision_id=head.revision_id
                        )
                    )
                    state = evaluation.binding_state
                except ValueError:
                    state = 'unreadable'
                evaluation_states.append((candidate, state))
            exact = [
                p
                for p, state in evaluation_states
                if p.scene_revision_id == head.revision_id and state == 'exact'
            ]
            pool = exact if exact else candidates
            placement = sorted(pool, key=lambda p: p.instance_id)[0]

    reference: PreviewReference | None = None
    anchor: PreviewAnchor | None = None
    if head is not None and placement is not None:
        try:
            evaluation = treatment_repository.evaluate_placement_surface_binding(
                placement, scene_revision_id=head.revision_id
            )
            binding_state = evaluation.binding_state
        except ValueError:
            binding_state = 'unreadable'
        geometry = head.document.r120_semantic_geometry
        footprint = (
            None
            if geometry is None
            else derive_treatment_footprint(placement, geometry)
        )
        canonical = (
            placement.scene_revision_id == head.revision_id
            and binding_state == 'exact'
            and footprint is not None
            and footprint.patch_area_m2 > FOOTPRINT_OVERLAP_TOLERANCE_M2
        )
        if canonical:
            matrix = quaternion_to_matrix3(placement.orientation)
            width_axis = (matrix[0][0], matrix[1][0], matrix[2][0])
            height_axis = (matrix[0][1], matrix[1][1], matrix[2][1])
            stack_axis = (matrix[0][2], matrix[1][2], matrix[2][2])
            origin = (
                float(placement.position.x_m),
                float(placement.position.y_m),
                float(placement.position.z_m),
            )
            # Project the authored mount rectangle onto the host plane —
            # the same orthonormal frame the footprint resolves in.
            normal = footprint.plane_normal
            plane_origin = footprint.plane_origin_m
            half_w = float(placement.coverage.width_m) / 2.0
            half_h = float(placement.coverage.height_m) / 2.0
            ring: list[tuple[float, float, float]] = []
            for sx, sy in ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)):
                point = _add(
                    origin,
                    _add(
                        _scale(width_axis, sx * half_w),
                        _scale(height_axis, sy * half_h),
                    ),
                )
                ring.append(
                    _sub(
                        point,
                        _scale(
                            normal, _dot(_sub(point, plane_origin), normal)
                        ),
                    )
                )
            rectangle_area = (
                float(placement.coverage.width_m)
                * float(placement.coverage.height_m)
            )
            reference = PreviewReference(
                placement_instance_id=placement.instance_id,
                lifecycle=placement.lifecycle,
                mount_ring=tuple(ring),
                clearance_center=_add(
                    origin,
                    _scale(stack_axis, package.overall_depth_m / 2.0),
                ),
                clearance_dims=(
                    float(placement.coverage.width_m),
                    float(placement.coverage.height_m),
                    package.overall_depth_m,
                ),
                clipped_to_surface=(
                    footprint.patch_area_m2
                    < rectangle_area - FOOTPRINT_OVERLAP_TOLERANCE_M2
                ),
            )
            anchor = PreviewAnchor(
                origin=origin,
                width_axis=width_axis,
                height_axis=height_axis,
                stack_axis=stack_axis,
                source='placement',
                placement_instance_id=placement.instance_id,
                placement_lifecycle=placement.lifecycle,
            )
        else:
            reason = (
                'underivable footprint'
                if footprint is None
                else f'binding {binding_state}'
            )
            notices.append(
                f'placement {placement.instance_id} is not canonical '
                f'({reason}) - neutral anchor, no mount reference'
            )

    if anchor is None:
        anchor = _neutral_anchor(head, package)
    return anchor, reference, tuple(notices)


def _neutral_anchor(head, package: TreatmentFabricationPackage) -> PreviewAnchor:
    """Stand the assembly just off the room's -Y edge, upright.

    Deterministic even with no semantic geometry (documents with
    ``room=None`` still preview — anchor at the origin floor plane).
    Parts build toward -Y so the front face lands nearest the viewer."""

    origin = (0.0, -_NEUTRAL_MARGIN_M, package.overall_height_m / 2.0)
    if head is not None:
        room = getattr(head.document, 'room', None)
        if room is not None:
            min_x, min_y, max_x, _max_y = room.bounds_m
            origin = (
                (min_x + max_x) / 2.0,
                min_y - _NEUTRAL_MARGIN_M - package.overall_depth_m,
                package.overall_height_m / 2.0,
            )
        else:
            origin = (
                0.0,
                -_NEUTRAL_MARGIN_M - package.overall_depth_m,
                package.overall_height_m / 2.0,
            )
    return PreviewAnchor(
        origin=origin,
        width_axis=(1.0, 0.0, 0.0),
        height_axis=(0.0, 0.0, 1.0),
        stack_axis=(0.0, -1.0, 0.0),
        source='neutral',
        placement_instance_id=None,
        placement_lifecycle=None,
    )


def _qrd_layout(
    package: TreatmentFabricationPackage,
) -> tuple[tuple[int, float, float], tuple[float, ...], float] | None:
    """Well layout recovered 1:1 from ``well_table``.

    Returns ``(well x-centres, well depths, fin_thickness)`` — or ``None``
    when the package cannot ground a layout (empty/out-of-order well
    table, degenerate pitch). Fin thickness comes from the fin parts'
    finished width, the only place the package stores it."""

    wells = package.well_table
    if not wells:
        return None
    count = len(wells)
    fin_parts = [p for p in package.parts if p.part_kind == 'well_fin']
    fin_thickness = (
        float(fin_parts[0].finished_width_m) if fin_parts else 0.0
    )
    if any(
        abs(float(p.finished_width_m) - fin_thickness) > 1e-9
        for p in fin_parts
    ):
        return None
    remaining = package.overall_width_m - (count + 1) * fin_thickness
    if remaining <= 0.0:
        return None
    well_width = remaining / count
    centres: list[float] = []
    depths: list[float] = []
    for index, row in enumerate(wells):
        if row.well_index != index:
            # The table itself is out of order — surface it rather than
            # silently re-sequencing.
            return None
        centres.append(
            -package.overall_width_m / 2.0
            + (index + 1) * fin_thickness
            + (index + 0.5) * well_width
        )
        depths.append(float(row.depth_m))
    return tuple(centres), tuple(depths), fin_thickness


def _qrd_fin_instances(
    package: TreatmentFabricationPackage,
    depths: tuple[float, ...],
    fin_thickness: float,
) -> tuple[tuple[int, int, float], ...]:
    """Fin boundary positions mapped to their package part index.

    Fin ``j`` sits between wells ``j-1``/``j`` at the generator's rule
    (depth = max adjacent well depth), matched to the ``well_fin`` part
    whose ``finished_depth_m`` carries that exact value. Returns
    ``(part_index_1based, fin_position_j, x_center)`` tuples — skips a
    position whose computed depth has no matching part rather than
    guessing."""

    fin_parts = [
        (index, part)
        for index, part in enumerate(package.parts, start=1)
        if part.part_kind == 'well_fin'
    ]
    count = len(depths)
    if not fin_parts or fin_thickness <= 0.0 or count <= 0:
        return ()
    well_width = (
        package.overall_width_m - (count + 1) * fin_thickness
    ) / count
    instances: list[tuple[int, int, float]] = []
    for j in range(count + 1):
        left = depths[j - 1] if j > 0 else depths[0]
        right = depths[j] if j < count else depths[-1]
        depth = max(left, right)
        match = next(
            (
                (index, part)
                for index, part in fin_parts
                if abs(float(part.finished_depth_m) - depth) <= 1e-9
            ),
            None,
        )
        if match is None:
            continue
        part_index, _part = match
        x_center = (
            -package.overall_width_m / 2.0
            + j * (well_width + fin_thickness)
            + fin_thickness / 2.0
        )
        instances.append((part_index, j, x_center))
    return tuple(instances)


def build_fabrication_report(package: TreatmentFabricationPackage) -> str:
    """Deterministic parts/drawing report for the issued package.

    Part numbering is 1-based in ``package.parts`` order — the SAME order
    the preview numbers its boxes — so exported lists and the 3D view can
    never disagree on counts or dimensions."""

    tol = package.tolerances
    lines = [
        '# Fabrication package report (read-only preview export)',
        '',
        '## Identity (pinned authorities)',
        f'- package_id: {package.package_id}',
        f'- package_version: {package.package_version}',
        f'- package_sha256: {package.package_sha256}',
        f'- definition_id: {package.definition_id}',
        f'- definition_version: {package.definition_version}',
        f'- definition_sha256: {package.definition_sha256}',
        f'- treatment_type: {package.treatment_type}',
        f'- fabrication_family: {package.fabrication_family}',
        f'- spec_version: {package.spec_version}',
        f'- renderer: {package.renderer_id} v{package.renderer_version}',
        f'- created_at_utc: {package.created_at_utc}',
        '- supersedes_package_sha256: '
        f'{package.supersedes_package_sha256 or "none"}',
        '',
        '## Assembly',
        f'- overall W×H×D: {_fmt_m(package.overall_width_m)} × '
        f'{_fmt_m(package.overall_height_m)} × '
        f'{_fmt_m(package.overall_depth_m)} m',
        f'- panel_count: {package.panel_count}',
        f'- kerf_mm: {package.kerf_mm:g}',
        f'- assembly_orientation: '
        f'{package.assembly_orientation or UNSPECIFIED}',
        f'- install_reference: {package.install_reference or UNSPECIFIED}',
        '- tolerances (mm): '
        f'overall={_fmt_mm(tol.overall_dimension_mm)} '
        f'well_depth={_fmt_mm(tol.well_depth_mm)} '
        f'fin_thickness={_fmt_mm(tol.fin_thickness_mm)} '
        f'spacing={_fmt_mm(tol.spacing_mm)} '
        f'air_gap={_fmt_mm(tol.air_gap_mm)}',
        '',
        '## Parts (preview numbering)',
        '| # | part_id | kind | W m | H m | D m | qty | material | '
        'cut_group | notes |',
        '|---|---|---|---|---|---|---|---|---|---|',
    ]
    for index, part in enumerate(package.parts, start=1):
        lines.append(
            f'| P{index} | {part.part_id} | {part.part_kind} | '
            f'{_fmt_m(part.finished_width_m)} | '
            f'{_fmt_m(part.finished_height_m)} | '
            f'{_fmt_m(part.finished_depth_m)} | {part.quantity} | '
            f'{_material_label(part.material_ref)} | {part.cut_group} | '
            f'{part.notes or ""} |'
        )
    lines += [
        '',
        '## Cut list',
        '| cut_group | description | W m | H m | D m | qty | material | '
        'tolerance_mm |',
        '|---|---|---|---|---|---|---|---|',
    ]
    for entry in package.cut_list:
        w, h, d = entry.dimensions_m
        lines.append(
            f'| {entry.cut_group} | {entry.description} | '
            f'{_fmt_m(w)} | {_fmt_m(h)} | {_fmt_m(d)} | {entry.quantity} | '
            f'{_material_label(entry.material_ref)} | '
            f'{_fmt_mm(entry.tolerance_mm)} |'
        )
    if package.well_table:
        lines += [
            '',
            '## Well table (1:1 with preview labels)',
            '| well_index | period | residue | depth_m | depth_mm |',
            '|---|---|---|---|---|',
        ]
        for row in package.well_table:
            lines.append(
                f'| {row.well_index} | {row.period_index} | '
                f'{row.residue} | {_fmt_m(row.depth_m)} | '
                f'{row.depth_m * 1000.0:g} |'
            )
    if package.bom_fragments:
        lines += ['', '## BOM fragments']
        for fragment in package.bom_fragments:
            lines.append(
                f'- [{fragment.line_kind}] {fragment.description}: '
                f'{fragment.quantity:g} {fragment.unit}'
                + (' (derived)' if fragment.derived else '')
            )
    if package.drawing_sheets:
        lines += [
            '',
            '## Drawing sheets',
            ', '.join(package.drawing_sheets),
        ]
    if package.warnings:
        lines += ['', '## Warnings']
        lines += [f'- {warning}' for warning in package.warnings]
    lines += [
        '',
        '---',
        'Fabrication values are design-derived (設計値由来): absorption/'
        'scattering performance and as-built installation are evaluated '
        'under separate authorities. This report is read-only output of '
        'the sealed package above; it does not certify machining safety, '
        'nesting yield, or structural adequacy.',
    ]
    return '\n'.join(lines) + '\n'


def _selection_detail(
    package: TreatmentFabricationPackage, key: str
) -> tuple[str, ...]:
    """Cut-list grounded detail for one part_id/cut_group click."""

    rows: list[str] = []
    matched = [
        part
        for part in package.parts
        if part.part_id == key or part.cut_group == key
    ]
    for part in matched:
        entry = next(
            (
                item
                for item in package.cut_list
                if item.cut_group == part.cut_group
            ),
            None,
        )
        rows.append(
            f'{part.part_id}: {part.description} — '
            f'{_fmt_m(part.finished_width_m)}×'
            f'{_fmt_m(part.finished_height_m)}×'
            f'{_fmt_m(part.finished_depth_m)} m ×{part.quantity} · '
            f'material {_material_label(part.material_ref)} · '
            f'cut group {part.cut_group}'
        )
        if entry is not None:
            w, h, d = entry.dimensions_m
            rows.append(
                f'  cut list: {entry.cut_group} {_fmt_m(w)}×{_fmt_m(h)}×'
                f'{_fmt_m(d)} m ×{entry.quantity} · material '
                f'{_material_label(entry.material_ref)} · tolerance '
                f'{_fmt_mm(entry.tolerance_mm)} mm'
            )
    if not rows:
        rows.append(f'no part or cut group matches {key!r}')
    return tuple(rows)


class FabricationPreviewController:
    """Armed-package resolve() with a content-keyed cache (#1008).

    Mirrors #1009: resolve re-pins the head revision and the placement
    binding on every render, so a scene edit or a new issued package can
    never leave stale geometry drawn. The package object itself is the
    issued authority — arm() takes only sealed instances."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        treatment_repository: CadAcousticTreatmentRepository,
        document_id: str,
    ) -> None:
        self.scene_repository = scene_repository
        self.treatment_repository = treatment_repository
        self.document_id = document_id
        self._package: TreatmentFabricationPackage | None = None
        self._placement_instance_id: str | None = None
        self._section_fraction: float | None = None
        self._exploded_fraction = 0.0
        self._selected_key: str | None = None
        self._force_neutral = False
        self._cache_key: tuple | None = None
        self._cache: FabricationPreviewScene | None = None

    @property
    def armed(self) -> bool:
        return self._package is not None

    @property
    def package(self) -> TreatmentFabricationPackage | None:
        return self._package

    def arm(
        self,
        package: TreatmentFabricationPackage,
        *,
        placement_instance_id: str | None = None,
        force_neutral: bool = False,
    ) -> None:
        self._package = package
        self._placement_instance_id = placement_instance_id
        self._force_neutral = force_neutral
        self._selected_key = None
        self.invalidate()

    def disarm(self) -> None:
        self._package = None
        self._placement_instance_id = None
        self._force_neutral = False
        self._selected_key = None
        self.invalidate()

    def set_view(
        self,
        *,
        section_fraction: float | None,
        exploded_fraction: float,
    ) -> None:
        if section_fraction is not None:
            section_fraction = min(1.0, max(0.0, float(section_fraction)))
        self._section_fraction = section_fraction
        self._exploded_fraction = min(
            1.0, max(0.0, float(exploded_fraction))
        )
        self.invalidate()

    def select(self, key: str | None) -> None:
        self._selected_key = key or None
        self.invalidate()

    def invalidate(self) -> None:
        self._cache_key = None
        self._cache = None

    def resolve(self) -> FabricationPreviewScene | None:
        package = self._package
        if package is None:
            self._cache_key = None
            self._cache = None
            return None
        head = self.scene_repository.current_head(self.document_id)
        key = (
            None if head is None else head.revision_id,
            None if head is None else head.content_hash,
            package.package_sha256,
            self._placement_instance_id,
            self._force_neutral,
            self._section_fraction,
            self._exploded_fraction,
            self._selected_key,
        )
        if key != self._cache_key:
            self._cache = self._build(head, package)
            self._cache_key = key
        return self._cache

    # ------------------------------------------------------------------

    def _build(
        self, head, package: TreatmentFabricationPackage
    ) -> FabricationPreviewScene:
        family = str(getattr(package, 'fabrication_family', ''))
        anchor, reference, notices = _resolve_anchor(
            self.scene_repository,
            self.treatment_repository,
            self.document_id,
            package,
            self._placement_instance_id,
            self._force_neutral,
        )
        if family not in SUPPORTED_FAMILIES:
            return FabricationPreviewScene(
                package=package,
                family=family or 'unknown',
                supported=False,
                fallback_reason=(
                    f'fabrication family {family or "unknown"!r} has no '
                    '3D layout — table/card preview only'
                ),
                anchor=anchor,
                parts=(),
                well_labels=(),
                reference=None,
                section_fraction=None,
                section_position=None,
                exploded_fraction=self._exploded_fraction,
                selected_key=None,
                selected_detail=(),
                notices=tuple(notices)
                + (
                    f'unsupported family {family or "unknown"} - '
                    'no 3D drawn',
                ),
                viewport_lines=(
                    'fab preview: UNSUPPORTED family '
                    f'{family or "unknown"} - table view only',
                    f'pkg {package.package_sha256[:12]} def '
                    f'{package.definition_id} v{package.definition_version}',
                ),
                summary_ja=(
                    f'未対応の製作ファミリー {family or "unknown"} — '
                    '3Dプレビューは行わず表形式のみ表示します。',
                ),
            )

        explode_step = package.overall_depth_m * _EXPLODE_STEP_RATIO
        exploded = self._exploded_fraction

        parts: list[PreviewPartBox] = []
        well_labels: list[PreviewWellLabel] = []
        if family == 'rectangular_panel':
            parts.extend(
                self._panel_boxes(package, anchor, explode_step, exploded)
            )
        else:  # qrd_1d
            extra_parts, extra_labels, extra_notices = self._qrd_boxes(
                package, anchor, explode_step, exploded
            )
            parts.extend(extra_parts)
            well_labels.extend(extra_labels)
            notices = tuple(notices) + tuple(extra_notices)

        selected_detail = (
            _selection_detail(package, self._selected_key)
            if self._selected_key is not None
            else ()
        )
        if self._selected_key is not None and not any(
            p.selected for p in parts
        ):
            notices = tuple(notices) + (
                f'selection {self._selected_key} matches no drawn part',
            )

        tol = package.tolerances
        header = (
            f'fab {family} pkg {package.package_sha256[:12]} '
            f'def {package.definition_id} v{package.definition_version} '
            f'(sha {package.definition_sha256[:12]}) spec v'
            f'{package.spec_version} issued {package.created_at_utc}'
        )
        pin = (
            'supersedes '
            + (
                package.supersedes_package_sha256[:12]
                if package.supersedes_package_sha256
                else 'none'
            )
            + f' | panels x{package.panel_count} | kerf '
            f'{package.kerf_mm:g}mm | tol overall='
            f'{_fmt_mm(tol.overall_dimension_mm)} well='
            f'{_fmt_mm(tol.well_depth_mm)} fin='
            f'{_fmt_mm(tol.fin_thickness_mm)} mm'
        )
        view_line = (
            'section '
            + (
                'off'
                if self._section_fraction is None
                else f'{self._section_fraction:.0%}'
            )
            + f' exploded {self._exploded_fraction:.0%} | '
            + (
                f'anchor placement {anchor.placement_instance_id} '
                f'[{anchor.placement_lifecycle}] REF mount+clearance'
                if anchor.source == 'placement'
                else 'anchor NEUTRAL - no canonical placement'
            )
        )
        honesty = (
            'DESIGN VALUES ONLY - absorption/scattering performance is a '
            'separate authority; no nesting/strength certification'
        )
        viewport_lines = [header, pin, view_line, honesty]
        viewport_lines.extend(notices[:3])
        if package.warnings:
            viewport_lines.append(
                f'warnings: {len(package.warnings)} - see panel/report'
            )

        family_label = _FAMILY_LABELS.get(family, family)
        summary: list[str] = [
            f'製作パッケージ {package.package_sha256[:12]} — '
            f'{family_label} · {len(package.parts)}部材 · '
            f'パネル×{package.panel_count}',
            f'定義 {package.definition_id} v{package.definition_version} '
            f'(sha {package.definition_sha256[:12]}) · '
            f'仕様 v{package.spec_version}',
            (
                f'参考配置 {reference.placement_instance_id} '
                f'({reference.lifecycle}) — 取付向き・クリアランスを参考表示'
                if reference is not None
                else 'canonicalな配置がありません — '
                '中立アンカーで部材のみ表示'
            ),
            '設計値由来のプレビュー — 吸音率/散乱性能は別権威で評価します',
        ]
        summary.extend(selected_detail)
        return FabricationPreviewScene(
            package=package,
            family=family,
            supported=True,
            fallback_reason=None,
            anchor=anchor,
            parts=tuple(parts),
            well_labels=tuple(well_labels),
            reference=reference,
            section_fraction=self._section_fraction,
            section_position=(
                None
                if self._section_fraction is None
                else _add(
                    anchor.origin,
                    _scale(
                        anchor.width_axis,
                        (self._section_fraction - 0.5)
                        * package.overall_width_m,
                    ),
                )
            ),
            exploded_fraction=exploded,
            selected_key=self._selected_key,
            selected_detail=selected_detail,
            notices=tuple(notices),
            viewport_lines=tuple(viewport_lines),
            summary_ja=tuple(summary),
        )

    def _panel_boxes(
        self,
        package: TreatmentFabricationPackage,
        anchor: PreviewAnchor,
        explode_step: float,
        exploded: float,
    ) -> list[PreviewPartBox]:
        """FAB10 slabs: derived air gap at the wall, then each declared
        layer in package order. The air gap is ``overall_depth − Σ part
        depth`` — package-grounded, marked derived, never a cut part."""

        boxes: list[PreviewPartBox] = []
        part_depth = sum(
            float(part.finished_depth_m) for part in package.parts
        )
        gap = package.overall_depth_m - part_depth
        offset = 0.0
        order = 0
        if gap > 1e-9:
            order += 1
            boxes.append(
                PreviewPartBox(
                    index=0,
                    part_id='air-gap',
                    part_kind='air_gap',
                    cut_group='air-gap',
                    description=(
                        f'air gap {gap * 1000.0:g} mm — installation '
                        'space, not a cut part'
                    ),
                    center=_add(
                        anchor.origin,
                        _scale(
                            anchor.stack_axis,
                            gap / 2.0
                            + exploded * explode_step * order,
                        ),
                    ),
                    dims_whs=(
                        package.overall_width_m,
                        package.overall_height_m,
                        gap,
                    ),
                    material_label=UNSPECIFIED,
                    quantity=package.panel_count,
                    selected=False,
                    derived=True,
                    label=f'air gap {gap * 1000.0:g}mm (not a cut part)',
                )
            )
            offset += gap
        for index, part in enumerate(package.parts, start=1):
            order += 1
            depth = float(part.finished_depth_m)
            boxes.append(
                PreviewPartBox(
                    index=index,
                    part_id=part.part_id,
                    part_kind=part.part_kind,
                    cut_group=part.cut_group,
                    description=part.description,
                    center=_add(
                        anchor.origin,
                        _scale(
                            anchor.stack_axis,
                            offset
                            + depth / 2.0
                            + exploded * explode_step * order,
                        ),
                    ),
                    dims_whs=(
                        float(part.finished_width_m),
                        float(part.finished_height_m),
                        depth,
                    ),
                    material_label=_material_label(part.material_ref),
                    quantity=part.quantity,
                    selected=(
                        self._selected_key is not None
                        and self._selected_key
                        in (part.part_id, part.cut_group)
                    ),
                    derived=False,
                    label=(
                        f'P{index} {part.part_id} '
                        f'{_fmt_m(part.finished_width_m)}x'
                        f'{_fmt_m(part.finished_height_m)}x'
                        f'{_fmt_m(depth)}m x{part.quantity}'
                    ),
                )
            )
            offset += depth
        return boxes

    def _qrd_boxes(
        self,
        package: TreatmentFabricationPackage,
        anchor: PreviewAnchor,
        explode_step: float,
        exploded: float,
    ) -> tuple[list[PreviewPartBox], list[PreviewWellLabel], list[str]]:
        """FAB20 layout: backing slab, then one fin instance per well
        boundary placed/depth-ed exactly as ``well_table`` declares."""

        notices: list[str] = []
        boxes: list[PreviewPartBox] = []
        labels: list[PreviewWellLabel] = []
        layout = _qrd_layout(package)
        backing = next(
            (
                (index, part)
                for index, part in enumerate(package.parts, start=1)
                if part.part_kind == 'backing_panel'
            ),
            None,
        )
        back_depth = (
            float(backing[1].finished_depth_m) if backing else 0.0
        )
        if backing is not None:
            index, part = backing
            boxes.append(
                PreviewPartBox(
                    index=index,
                    part_id=part.part_id,
                    part_kind=part.part_kind,
                    cut_group=part.cut_group,
                    description=part.description,
                    center=_add(
                        anchor.origin,
                        _scale(anchor.stack_axis, back_depth / 2.0),
                    ),
                    dims_whs=(
                        float(part.finished_width_m),
                        float(part.finished_height_m),
                        back_depth,
                    ),
                    material_label=_material_label(part.material_ref),
                    quantity=part.quantity,
                    selected=(
                        self._selected_key is not None
                        and self._selected_key
                        in (part.part_id, part.cut_group)
                    ),
                    derived=False,
                    label=(
                        f'P{index} {part.part_id} '
                        f'{_fmt_m(part.finished_width_m)}x'
                        f'{_fmt_m(part.finished_height_m)}x'
                        f'{_fmt_m(back_depth)}m x{part.quantity}'
                    ),
                )
            )
        if layout is None:
            notices.append(
                'well_table missing/out-of-order or degenerate pitch - '
                'fins/wells not drawn; the table holds every row'
            )
            return boxes, labels, notices
        centres, depths, fin_thickness = layout
        max_depth = max(depths) if depths else 0.0
        instances = _qrd_fin_instances(package, depths, fin_thickness)
        for part_index, fin_j, x_center in instances:
            part = package.parts[part_index - 1]
            depth = float(part.finished_depth_m)
            # Exploded view: fins travel outward in proportion to depth —
            # the stepped QRD profile stays legible instead of collapsing.
            travel = exploded * explode_step * (
                depth / max_depth if max_depth > 0.0 else 0.0
            )
            boxes.append(
                PreviewPartBox(
                    index=part_index,
                    part_id=f'{part.part_id}@{fin_j}',
                    part_kind=part.part_kind,
                    cut_group=part.cut_group,
                    description=(
                        f'{part.description} (fin position {fin_j})'
                    ),
                    center=_add(
                        _add(
                            anchor.origin,
                            _scale(anchor.width_axis, x_center),
                        ),
                        _scale(
                            anchor.stack_axis,
                            back_depth + depth / 2.0 + travel,
                        ),
                    ),
                    dims_whs=(
                        fin_thickness,
                        float(part.finished_height_m),
                        depth,
                    ),
                    material_label=_material_label(part.material_ref),
                    quantity=part.quantity,
                    selected=(
                        self._selected_key is not None
                        and self._selected_key
                        in (part.part_id, part.cut_group)
                    ),
                    derived=False,
                    label=(
                        f'P{part_index} fin{fin_j} '
                        f'd={_fmt_m(depth)}m'
                    ),
                )
            )
        for index, row in enumerate(package.well_table):
            if index >= _MAX_WELL_LABELS:
                break
            labels.append(
                PreviewWellLabel(
                    well_index=row.well_index,
                    depth_m=float(row.depth_m),
                    position=_add(
                        _add(
                            anchor.origin,
                            _scale(anchor.width_axis, centres[index]),
                        ),
                        _add(
                            _scale(
                                anchor.stack_axis,
                                back_depth + float(row.depth_m),
                            ),
                            _scale(
                                anchor.height_axis,
                                package.overall_height_m / 2.0 + 0.02,
                            ),
                        ),
                    ),
                    label=(
                        f'W{row.well_index} '
                        f'd={float(row.depth_m) * 1000.0:g}mm'
                    ),
                )
            )
        if len(package.well_table) > _MAX_WELL_LABELS:
            notices.append(
                f'{len(package.well_table)} wells - first '
                f'{_MAX_WELL_LABELS} labelled; the table lists all'
            )
        return boxes, labels, notices


__all__ = [
    'FabricationPreviewController',
    'FabricationPreviewScene',
    'PART_KIND_LABELS',
    'PreviewAnchor',
    'PreviewPartBox',
    'PreviewReference',
    'PreviewWellLabel',
    'SUPPORTED_FAMILIES',
    'UNSPECIFIED',
    'build_fabrication_report',
]
