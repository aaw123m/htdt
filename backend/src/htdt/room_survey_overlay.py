"""Room-viewport as-built survey overlay (#1004 / REV73).

Draws the as-built *survey authority* (#613) onto the current room geometry:
per-element evidence tier, achieved uncertainty, verification state, and
design-vs-as-built reconciliation deltas — resolved against the EXACT
current ``SceneRevision`` head on every render.

Honesty contract:

- ``element_key`` resolves ONLY against the current head's stable IDs —
  compiled room-authoring surface keys (``wall:<i>``, ``wall:<i>:opening``,
  ``floor``, ``ceiling``, ``riser:<id>:top``, …), scene ``entity_id``s, and
  R120 ``semantic-surface:<sha>`` ids. A key that is absent, re-numbered
  away, non-unique across id spaces, or carried only by campaigns whose
  source frame cannot be verified renders as ``unmapped`` — the overlay
  never guess-attaches evidence to a nearby wall.
- No-data elements are explicit ``unknown`` styling — never drawn as 0 mm
  or safe green.
- Independent control measurements render as point/measure-line anchors
  on the surfaces they check; controls consumed by registration are never
  presented as validation passes, and tolerance-less controls show their
  residual only.
- ``delta_mm`` reconciliation values render as a label + number on the
  surface — the authority records no displacement direction, so the
  overlay never invents displacement arrows.
- Qualification verdicts come from the sealed authority and are bound by
  ``element_sha256``: an element whose live record no longer matches the
  evaluated hash shows as unevaluated (unknown), never the stale colour.
- Read-only: nothing here mutates documents, evidence, or verdicts.

No Qt imports — the workspace owns the widgets and the viewport owns the
actors; this module owns the resolution so a late render cannot resurrect
a superseded survey state.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .cad_geometry_survey import (
    AsBuiltGeometryQualification,
    AsBuiltReconciliation,
    GeometricElementEvidence,
    GeometricElementStateEntry,
    GeometryControlMeasurement,
    GeometrySurveyInstrument,
    SurveyCampaign,
    _EVIDENCE_TIER,
)
from .cad_geometry_survey_repository import CadGeometrySurveyRepository
from .cad_repository import SceneRepository
from .cad_room_authoring import compile_room_authoring_to_r120
from .cad_scene import (
    RoomAuthoringModel,
    SceneDocument,
    SceneEntity,
    room_vertices,
)
from .r120_polyhedral_geometry import (
    PlanarPolygonSurface,
    R120PolyhedralSemanticGeometry,
)


#: Selectable display modes (#1004). ``off`` is a panel-side choice that
#: never reaches the resolver.
SURVEY_OVERLAY_MODES: tuple[str, ...] = (
    'tier',
    'uncertainty_mm',
    'verification',
    'delta_mm',
)

#: JA labels for the mode selector / legend headers.
MODE_LABELS: dict[str, str] = {
    'tier': '証拠階層',
    'uncertainty_mm': '達成不確かさ (mm)',
    'verification': '検証状態',
    'delta_mm': '設計–現況差分 (mm)',
}

#: Verification buckets (issue #1004 vocabulary: verified / unverified /
#: stale / unknown — plus a distinct failed bucket for control_check_failed,
#: which must never collapse into either verified or a quiet unverified).
VERIFICATION_BUCKETS: tuple[str, ...] = (
    'verified',
    'unverified',
    'stale',
    'failed',
    'unknown',
)

VERIFICATION_LABELS: dict[str, str] = {
    'verified': '検証済み',
    'unverified': '未検証',
    'stale': '陳腐化',
    'failed': '検査不合格',
    'unknown': '不明 (評価なし)',
}

#: ASCII-only viewport legend lines per mode — VTK drops CJK glyphs, so the
#: in-scene legend must stay readable in ASCII (#999 lesson).
MODE_LEGEND_ASCII: dict[str, tuple[str, ...]] = {
    'tier': (
        'legend: T0 design/user | T1 derived | T2 field | T3 survey | T4 qualified',
    ),
    'uncertainty_mm': (
        'legend: <=5mm | <=25mm | <=50mm | >50mm | grey = unknown (no data)',
    ),
    'verification': (
        'legend: green verified | amber unverified | orange stale '
        '| red failed | grey unknown',
    ),
    'delta_mm': (
        'legend: |d|<=2mm | <=10mm | <=50mm | >50mm | grey = no record '
        '| * = unapproved change',
    ),
}

#: Why an element_key contributes no drawn surface (#1004 unmapped states).
UNMAPPED_REASON_LABELS: dict[str, str] = {
    'missing': '現行リビジョンに該当要素なし',
    'renumbered': '再採番済み — 旧キーは現行面に存在しません',
    'non_unique': 'キーが一意に解決しません',
    'unsupported_frame': '測量ソースの座標系を検証できません',
    'unreadable': '測量権威を再検証できません',
}

_UNMAPPED_VIEWPORT = {
    'missing': 'no such stable id on current head',
    'renumbered': 're-numbered key absent on current head',
    'non_unique': 'key resolves to more than one target',
    'unsupported_frame': 'survey source frame unverifiable',
    'unreadable': 'survey authority unreadable',
}

#: element_key families that look like compiled stable ids — a match on the
#: family but not the head means the element was re-numbered/re-authored
#: rather than a foreign key space.
_STABLE_ID_FAMILY = re.compile(
    r'^(floor|ceiling|wall:\d+(?::(?:flank-a|flank-b|lintel|opening))?|'
    r'riser:[^:]+:(?:top|side:\d+)|soffit:[^:]+:(?:bottom|side:\d+)|'
    r'partial-wall:[^:]+:(?:top|face:\d+)|region:[^:]+:(?:floor|ceiling|wall:\d+)|'
    r'semantic-surface:[0-9a-f]{64})$'
)

_VERIFIED_STATES = frozenset(
    {'field_checked', 'fit_for_declared_task', 'fit_with_limitations'}
)
_UNVERIFIED_STATES = frozenset(
    {'design_only', 'observed_unqualified', 'registration_limited'}
)


@dataclass(frozen=True, slots=True)
class SurveyElementTarget:
    """One resolved render target for an element_key on the current head."""

    kind: str  # 'surface' | 'entity' | 'semantic_surface'
    key: str
    #: Domain-space centroid (x, y, z metres) — the label/anchor position.
    centroid_domain: tuple[float, float, float]
    #: Compiled-authority handle for 'surface' (consumed by the viewport's
    #: ``_planar_polygon_mesh``); None for entity/semantic targets.
    compiled_geometry: 'R120PolyhedralSemanticGeometry | None' = None
    surface: 'PlanarPolygonSurface | None' = None
    entity: SceneEntity | None = None
    #: (geometry, surface) pair for 'semantic_surface' targets — the
    #: triangle set the viewport triangulates.
    semantic_geometry: object | None = None
    semantic_surface: object | None = None


@dataclass(frozen=True, slots=True)
class SurveyOverlayElement:
    """One survey element resolved + classified for the active mode."""

    element: GeometricElementEvidence
    #: None when the key did not resolve — see ``unmapped_reason``.
    target: SurveyElementTarget | None
    unmapped_reason: str | None  # key of UNMAPPED_REASON_LABELS
    #: The sealed evaluation entry when the LIVE element_sha256 matches the
    #: qualification's pinned hash; None = unevaluated (never colour it
    #: with a stale verdict).
    state_entry: GeometricElementStateEntry | None
    verification: str  # key of VERIFICATION_BUCKETS
    tier: int | None  # derived evidence tier (0-4); None when unknowable
    achieved_uncertainty_mm: float | None
    reconciliation: AsBuiltReconciliation | None
    #: ASCII label for the surface (near zoom), e.g. 'wall:0  T2 +/-8mm'.
    label_ascii: str
    #: Mode bucket key this element falls in for the legend/counts.
    bucket: str


@dataclass(frozen=True, slots=True)
class SurveyOverlayControl:
    """One independent control anchored to the surfaces it checks."""

    control: GeometryControlMeasurement
    #: Domain-space anchor points: a single marker point for one-surface
    #: controls, or (a, b) span endpoints for multi-surface checks.
    anchors: tuple[tuple[float, float, float], ...]
    unmapped_reason: str | None
    used_for_registration: bool
    #: 'pass' | 'fail' | 'evidence_only' | 'unmapped' — never 'pass' for
    #: registration-consumed controls.
    bucket: str
    label_ascii: str


@dataclass(frozen=True, slots=True)
class SurveyOverlayScene:
    """Everything one render pass draws/announces for the current head."""

    document_id: str
    revision_id: str
    revision_content_hash: str
    mode: str
    qualification_id: str | None
    elements: tuple[SurveyOverlayElement, ...]
    controls: tuple[SurveyOverlayControl, ...]
    unmapped_elements: tuple[SurveyOverlayElement, ...]
    unmapped_controls: tuple[SurveyOverlayControl, ...]
    #: Aggregate bucket counts for the active mode — the zoomed-out truth.
    bucket_counts: dict[str, int]
    scene_diagonal_m: float
    viewport_lines: tuple[str, ...]
    summary_ja: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SurveyElementDetail:
    """Click-target authority bundle for one element (Qt panel text)."""

    element_id: str
    element_key: str
    element_sha256: str
    campaigns: tuple[SurveyCampaign, ...]
    instruments: tuple[GeometrySurveyInstrument, ...]
    state_entry: GeometricElementStateEntry | None
    qualification_id: str | None
    blocking_task_verdicts: tuple[tuple[str, str, tuple[str, ...]], ...]
    reconciliations: tuple[AsBuiltReconciliation, ...]
    recheck_element_ids: tuple[str, ...]
    invalidated_refs: tuple[str, ...]
    detail_lines_ja: tuple[str, ...]


def _compiled_geometry(document: SceneDocument):
    """Compiled surfaces of the CURRENT document, or None when not
    compilable/absent. Plain rooms compile through RoomAuthoringModel so
    ``wall:<i>``/floor/ceiling keys resolve on every room."""

    if document.room is None:
        return None
    model = document.room_authoring
    if model is None:
        model = RoomAuthoringModel(room=document.room)
    try:
        return compile_room_authoring_to_r120(model)
    except ValueError:
        return None


def _surface_centroid(geometry, surface) -> tuple[float, float, float]:
    points = [geometry.vertices[i].point() for i in surface.outer_vertex_indices]
    n = float(len(points))
    return (
        sum(p[0] for p in points) / n,
        sum(p[1] for p in points) / n,
        sum(p[2] for p in points) / n,
    )


def _semantic_surface_centroid(geometry, surface) -> tuple[float, float, float]:
    tri_ids = set(surface.triangle_ids)
    verts = [
        v
        for tri in geometry.triangles
        if tri.triangle_id in tri_ids
        for v in (
            geometry.vertices[tri.a],
            geometry.vertices[tri.b],
            geometry.vertices[tri.c],
        )
    ]
    if not verts:
        return (0.0, 0.0, 0.0)
    n = float(len(verts))
    return (
        sum(v.x_m for v in verts) / n,
        sum(v.y_m for v in verts) / n,
        sum(v.z_m for v in verts) / n,
    )


def resolve_element_target(
    document: SceneDocument,
    element_key: str,
    *,
    compiled=None,
) -> SurveyElementTarget | str:
    """Resolve an element_key to exactly one current-head target.

    Returns a :class:`SurveyElementTarget` on success, else the unmapped
    reason key — never a nearest-match guess.
    """

    if compiled is None:
        compiled = _compiled_geometry(document)
    surface_by_key = (
        {s.surface_key: s for s in compiled.surfaces}
        if compiled is not None
        else {}
    )
    entity_ids = {e.entity_id: e for e in document.entities}
    semantic_by_id = {}
    if document.r120_semantic_geometry is not None:
        semantic_by_id = {
            s.surface_id: s for s in document.r120_semantic_geometry.surfaces
        }

    candidates: list[SurveyElementTarget] = []
    if element_key in surface_by_key:
        surface = surface_by_key[element_key]
        candidates.append(
            SurveyElementTarget(
                kind='surface',
                key=element_key,
                centroid_domain=_surface_centroid(compiled, surface),
                compiled_geometry=compiled,
                surface=surface,
            )
        )
    if element_key in entity_ids:
        entity = entity_ids[element_key]
        candidates.append(
            SurveyElementTarget(
                kind='entity',
                key=element_key,
                centroid_domain=(
                    float(entity.position.x_m),
                    float(entity.position.y_m),
                    float(entity.position.z_m),
                ),
                entity=entity,
            )
        )
    if element_key in semantic_by_id:
        surface = semantic_by_id[element_key]
        candidates.append(
            SurveyElementTarget(
                kind='semantic_surface',
                key=element_key,
                centroid_domain=_semantic_surface_centroid(
                    document.r120_semantic_geometry, surface
                ),
                semantic_geometry=document.r120_semantic_geometry,
                semantic_surface=surface,
            )
        )

    if len(candidates) > 1:
        return 'non_unique'
    if not candidates:
        return 'renumbered' if _STABLE_ID_FAMILY.match(element_key) else 'missing'
    return candidates[0]


def _frame_support(
    element: GeometricElementEvidence,
    campaign_map: dict[str, SurveyCampaign],
) -> bool:
    """True when at least one referenced campaign declares a source frame
    we can honestly place: metric units (mm/cm/m), right-handed, present in
    the repository. Elements with no campaigns (design evidence) always
    support their own design key."""

    if not element.campaign_ids:
        return True
    for campaign_id in element.campaign_ids:
        campaign = campaign_map.get(campaign_id)
        if campaign is None:
            continue
        frame = campaign.frame
        if frame is not None and not frame.right_handed:
            continue
        if frame is not None:
            return True
        # No frame declaration: the campaign carried no coordinate
        # statement — treat as supportable only when it also declares a
        # registration into the document frame.
        if campaign.registration is not None:
            return True
    return False


def _derived_tier(element: GeometricElementEvidence) -> int | None:
    if not element.evidence_classes:
        return None
    tiers = [_EVIDENCE_TIER.get(cls, 0) for cls in element.evidence_classes]
    return max(tiers, default=0) if tiers else None


def _verification_bucket(
    element: GeometricElementEvidence,
    state_entry: GeometricElementStateEntry | None,
) -> str:
    if state_entry is not None:
        state = state_entry.state
        if state in _VERIFIED_STATES:
            return 'verified'
        if state in _UNVERIFIED_STATES:
            return 'unverified'
        if state == 'stale_after_change':
            return 'stale'
        if state == 'control_check_failed':
            return 'failed'
        return 'unknown'
    # Unevaluated: the record still honestly carries design-only / stale /
    # hidden states; everything observed-but-unevaluated is unverified.
    if element.stale_after_change:
        return 'stale'
    if element.observation_state == 'hidden_unknown':
        return 'unknown'
    if element.observation_state == 'design_source_only' or not element.campaign_ids:
        return 'unverified'
    return 'unverified'


_UNCERTAINTY_BUCKETS = ((5.0, 'le5'), (25.0, 'le25'), (50.0, 'le50'))
_DELTA_BUCKETS = ((2.0, 'delta_le2'), (10.0, 'delta_le10'), (50.0, 'delta_le50'))


def _bucket_for_mode(
    mode: str,
    element: GeometricElementEvidence,
    state_entry: GeometricElementStateEntry | None,
    verification: str,
    tier: int | None,
    reconciliation: AsBuiltReconciliation | None,
) -> str:
    if mode == 'tier':
        return 'unknown' if tier is None else f't{tier}'
    if mode == 'uncertainty_mm':
        value = (
            state_entry.achieved_uncertainty_mm
            if state_entry is not None
            else None
        )
        if value is None:
            return 'unknown'
        for limit, bucket in _UNCERTAINTY_BUCKETS:
            if value <= limit:
                return bucket
        return 'gt50'
    if mode == 'delta_mm':
        if reconciliation is None:
            return 'none'
        if reconciliation.delta_mm is None:
            return 'delta_na'
        magnitude = abs(float(reconciliation.delta_mm))
        for limit, bucket in _DELTA_BUCKETS:
            if magnitude <= limit:
                return bucket
        return 'delta_gt50'
    return verification


def _element_label_ascii(
    mode: str,
    element: GeometricElementEvidence,
    state_entry: GeometricElementStateEntry | None,
    verification: str,
    tier: int | None,
    reconciliation: AsBuiltReconciliation | None,
) -> str:
    key = element.element_key
    if mode == 'tier':
        return f'{key}  T{tier}' if tier is not None else f'{key}  T?'
    if mode == 'uncertainty_mm':
        value = (
            state_entry.achieved_uncertainty_mm
            if state_entry is not None
            else None
        )
        if value is None:
            return f'{key}  +/- unknown'
        return f'{key}  +/-{value:.0f}mm'
    if mode == 'delta_mm':
        if reconciliation is None:
            return f'{key}  no reconciliation'
        if reconciliation.delta_mm is None:
            return f'{key}  delta n/a'
        mark = '*' if not reconciliation.approved_change else ''
        return f'{key}  d{reconciliation.delta_mm:+.0f}mm{mark}'
    label = verification.upper()
    return f'{key}  {label}'


def _control_label_ascii(control: GeometryControlMeasurement) -> str:
    short = control.control_id.split(':', 1)[-1][:8]
    prefix = f'CTRL {short}'
    if control.used_for_registration:
        return f'{prefix} REG-USED (not validation)'
    residual = control.residual_mm
    if residual is None:
        return f'{prefix} measured-only (no declared value)'
    if control.tolerance_mm is None:
        return f'{prefix} r={residual:.0f}mm (no tolerance)'
    verdict = 'PASS' if control.passed else 'FAIL'
    return f'{prefix} r={residual:.0f}mm tol={control.tolerance_mm:.0f}mm {verdict}'


def resolve_survey_overlay(
    scene_repository: SceneRepository,
    survey_repository: CadGeometrySurveyRepository,
    document_id: str,
    mode: str = 'verification',
) -> SurveyOverlayScene | None:
    """Resolve the survey overlay for the document's CURRENT head.

    Every authority record is re-read and re-bound per call — never a
    revision captured earlier — so a scene edit or survey change cannot
    leave stale colours drawn.
    """

    if mode not in SURVEY_OVERLAY_MODES:
        raise ValueError(f'unknown survey overlay mode {mode!r}')
    head = scene_repository.current_head(document_id)
    if head is None:
        return None
    document = head.document
    compiled = _compiled_geometry(document)
    diagonal = _scene_diagonal(document)

    try:
        elements = survey_repository.list_elements(document_id)
        campaigns = survey_repository.list_campaigns(document_id)
        controls = survey_repository.list_controls(document_id)
        reconciliations = _list_reconciliations(
            survey_repository, document_id
        )
        qualification = survey_repository.latest_qualification(document_id)
    except ValueError as exc:
        line = f'survey authority unreadable: {exc}'
        return SurveyOverlayScene(
            document_id=document_id,
            revision_id=head.revision_id,
            revision_content_hash=head.content_hash,
            mode=mode,
            qualification_id=None,
            elements=(),
            controls=(),
            unmapped_elements=(),
            unmapped_controls=(),
            bucket_counts={},
            scene_diagonal_m=diagonal,
            viewport_lines=(
                f'survey: authority unreadable — nothing drawn',
                f'rev {head.revision_id[:8]} CURRENT',
            ),
            summary_ja=(f'測量権威を再検証できません: {exc}',),
        )

    campaign_map = {c.campaign_id: c for c in campaigns}
    state_by_element: dict[str, GeometricElementStateEntry] = {}
    if qualification is not None:
        for entry in qualification.element_states:
            state_by_element[entry.element_id] = entry

    recon_by_key: dict[str, AsBuiltReconciliation] = {}
    for record in sorted(
        reconciliations,
        key=lambda r: (r.reconciled_at_utc, r.reconciliation_id),
    ):
        recon_by_key[record.element_key] = record

    duplicate_keys = {
        e.element_key
        for e in elements
        if sum(1 for other in elements if other.element_key == e.element_key)
        > 1
    }

    resolved: list[SurveyOverlayElement] = []
    unmapped: list[SurveyOverlayElement] = []
    for element in elements:
        if element.element_key in duplicate_keys:
            target: SurveyElementTarget | str = 'non_unique'
        elif not _frame_support(element, campaign_map):
            target = 'unsupported_frame'
        else:
            target = resolve_element_target(
                document, element.element_key, compiled=compiled
            )
        state_entry = state_by_element.get(element.element_id)
        if (
            state_entry is not None
            and state_entry.element_sha256 != element.element_sha256
        ):
            # The sealed verdict evaluated a DIFFERENT version of this
            # element — the identity mismatch must clear the stale colour.
            state_entry = None
        verification = _verification_bucket(element, state_entry)
        tier = _derived_tier(element)
        reconciliation = recon_by_key.get(element.element_key)
        bucket = _bucket_for_mode(
            mode,
            element,
            state_entry,
            verification,
            tier,
            reconciliation,
        )
        item = SurveyOverlayElement(
            element=element,
            target=target if isinstance(target, SurveyElementTarget) else None,
            unmapped_reason=None if isinstance(target, SurveyElementTarget) else target,
            state_entry=state_entry,
            verification=verification,
            tier=tier,
            achieved_uncertainty_mm=(
                state_entry.achieved_uncertainty_mm if state_entry else None
            ),
            reconciliation=reconciliation,
            label_ascii=_element_label_ascii(
                mode,
                element,
                state_entry,
                verification,
                tier,
                reconciliation,
            ),
            bucket=bucket,
        )
        (resolved if item.target is not None else unmapped).append(item)

    resolved_controls: list[SurveyOverlayControl] = []
    unmapped_controls: list[SurveyOverlayControl] = []
    for control in controls:
        anchors: list[tuple[float, float, float]] = []
        seen: set[str] = set()
        control_reason: str | None = None
        for key in control.element_keys:
            target = resolve_element_target(document, key, compiled=compiled)
            if isinstance(target, SurveyElementTarget):
                if target.key not in seen:
                    anchors.append(target.centroid_domain)
                    seen.add(target.key)
            elif control_reason is None:
                control_reason = target
        if not control.element_keys:
            control_reason = 'missing'
        elif not anchors and control_reason is None:
            control_reason = 'missing'
        passed = control.passed
        if control_reason is not None:
            bucket = 'unmapped'
        elif control.used_for_registration:
            bucket = 'reg_used'
        elif passed is None:
            bucket = 'evidence_only'
        else:
            bucket = 'pass' if passed else 'fail'
        item = SurveyOverlayControl(
            control=control,
            anchors=tuple(anchors),
            unmapped_reason=control_reason,
            used_for_registration=control.used_for_registration,
            bucket=bucket,
            label_ascii=_control_label_ascii(control),
        )
        (
            unmapped_controls if item.bucket == 'unmapped' else resolved_controls
        ).append(item)

    bucket_counts: dict[str, int] = {}
    for item in resolved:
        bucket_counts[item.bucket] = bucket_counts.get(item.bucket, 0) + 1
    if unmapped:
        bucket_counts['unmapped'] = len(unmapped)

    lines = [
        f'survey [{mode}]: {len(resolved)} mapped, {len(unmapped)} unmapped'
        f' | rev {head.revision_id[:8]} CURRENT',
        'counts: ' + (
            ' | '.join(f'{k} {v}' for k, v in sorted(bucket_counts.items()))
            or 'none'
        ),
    ]
    lines.extend(MODE_LEGEND_ASCII[mode])
    if unmapped:
        lines.append(
            'unmapped: '
            + '; '.join(
                f'{item.element.element_key} '
                f'({_UNMAPPED_VIEWPORT[item.unmapped_reason]})'
                for item in unmapped[:4]
            )
            + (f'; +{len(unmapped) - 4} more' if len(unmapped) > 4 else '')
        )
    for line in lines:
        assert line.isascii()

    summary: list[str] = []
    for item in resolved:
        entry = item.state_entry
        extras = []
        if item.reconciliation is not None:
            delta = item.reconciliation.delta_mm
            delta_text = 'n/a' if delta is None else f'{delta:+.0f}mm'
            extras.append(
                f'差分 {delta_text}'
                + ('' if item.reconciliation.approved_change else ' (未承認)')
            )
        if entry is not None and entry.reasons:
            extras.append('理由: ' + '/'.join(entry.reasons))
        tier_text = 'T?' if item.tier is None else f'T{item.tier}'
        unc = (
            'n/a'
            if item.achieved_uncertainty_mm is None
            else f'±{item.achieved_uncertainty_mm:.0f}mm'
        )
        summary.append(
            f'{item.element.element_key} ({item.element.element_id}): '
            f'{VERIFICATION_LABELS.get(item.verification, item.verification)} '
            f'· {tier_text} · {unc}'
            + (f' · {"; ".join(extras)}' if extras else '')
        )
    for item in unmapped:
        summary.append(
            f'{item.element.element_key} ({item.element.element_id}): '
            f'マッピング不可 — '
            f'{UNMAPPED_REASON_LABELS.get(item.unmapped_reason, item.unmapped_reason)}'
        )
    for item in unmapped_controls:
        summary.append(
            f'{item.control.control_id}: コントロール未配置 — '
            f'{UNMAPPED_REASON_LABELS.get(item.unmapped_reason, item.unmapped_reason)}'
        )
    return SurveyOverlayScene(
        document_id=document_id,
        revision_id=head.revision_id,
        revision_content_hash=head.content_hash,
        mode=mode,
        qualification_id=(
            qualification.qualification_id if qualification is not None else None
        ),
        elements=tuple(resolved),
        controls=tuple(resolved_controls),
        unmapped_elements=tuple(unmapped),
        unmapped_controls=tuple(unmapped_controls),
        bucket_counts=bucket_counts,
        scene_diagonal_m=diagonal,
        viewport_lines=tuple(lines),
        summary_ja=tuple(summary),
    )


def _scene_diagonal(document: SceneDocument) -> float:
    if document.room is None:
        xs = [float(e.position.x_m) for e in document.entities] or [0.0]
        ys = [float(e.position.y_m) for e in document.entities] or [0.0]
        zs = [float(e.position.z_m) for e in document.entities] or [0.0]
        dx = max(xs) - min(xs)
        dy = max(ys) - min(ys)
        dz = max(zs) - min(zs)
        return max((dx * dx + dy * dy + dz * dz) ** 0.5, 1.0)
    min_x, min_y, max_x, max_y = document.room.bounds_m
    dx = max_x - min_x
    dy = max_y - min_y
    dz = float(document.room.height_m)
    return max((dx * dx + dy * dy + dz * dz) ** 0.5, 1.0)


def _list_reconciliations(
    survey_repository: CadGeometrySurveyRepository,
    document_id: str,
) -> tuple[AsBuiltReconciliation, ...]:
    """All reconciliations for the document — the repository has no
    document listing, so scan the table once per resolve."""

    from .cad_schema import connect_sqlite

    with connect_sqlite(survey_repository.path) as connection:
        rows = connection.execute(
            'SELECT payload_json FROM cad_geo_reconciliations '
            'WHERE document_id=? ORDER BY reconciled_at_utc, reconciliation_id',
            (document_id,),
        ).fetchall()
    return tuple(
        AsBuiltReconciliation.model_validate_json(row['payload_json'])
        for row in rows
    )


class RoomSurveyOverlayController:
    """Per-render resolve() with a content-keyed cache (#1004).

    The cache key pins document, head revision + content hash, the latest
    qualification id, every live authority hash, and the mode — so a scene
    edit, a new survey record, or a project/head/survey identity mismatch
    always re-resolves (stale colours are cleared, never replayed).
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
        survey_repository: CadGeometrySurveyRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.survey_repository = survey_repository or CadGeometrySurveyRepository(
            scene_repository
        )
        self._cache_key: tuple | None = None
        self._cache: SurveyOverlayScene | None = None

    def invalidate(self) -> None:
        self._cache_key = None
        self._cache = None

    def resolve(self, mode: str = 'verification') -> SurveyOverlayScene | None:
        head = self.scene_repository.current_head(self.document_id)
        if head is None:
            self._cache_key = None
            self._cache = None
            return None
        try:
            elements = self.survey_repository.list_elements(self.document_id)
            campaigns = self.survey_repository.list_campaigns(self.document_id)
            controls = self.survey_repository.list_controls(self.document_id)
            reconciliations = _list_reconciliations(
                self.survey_repository, self.document_id
            )
            qualification = self.survey_repository.latest_qualification(
                self.document_id
            )
        except ValueError:
            elements, campaigns, controls, reconciliations, qualification = (
                (),
                (),
                (),
                (),
                None,
            )
        key = (
            self.document_id,
            head.revision_id,
            head.content_hash,
            None if qualification is None else qualification.qualification_sha256,
            tuple((e.element_id, e.element_sha256) for e in elements),
            tuple((c.campaign_id, c.campaign_sha256) for c in campaigns),
            tuple((c.control_id, c.control_sha256) for c in controls),
            tuple(
                (r.reconciliation_id, r.reconciliation_sha256)
                for r in reconciliations
            ),
            mode,
        )
        if key != self._cache_key:
            self._cache = resolve_survey_overlay(
                self.scene_repository,
                self.survey_repository,
                self.document_id,
                mode,
            )
            self._cache_key = key
        return self._cache

    def describe_element(self, element_id: str) -> SurveyElementDetail | None:
        """Authority bundle behind one element — the click target (#1004).

        Links the element to its campaigns, instruments (kind / capability
        / app+version / calibration refs), the source hashes, the sealed
        evaluation entry, task verdicts it blocks, reconciliations, and the
        sibling elements whose shared campaigns make them re-check
        candidates. Pure read — verdicts are never touched.
        """

        element = self.survey_repository.get_element(element_id)
        if element is None or element.document_id != self.document_id:
            return None
        campaigns = tuple(
            c
            for c in self.survey_repository.list_campaigns(self.document_id)
            if c.campaign_id in element.campaign_ids
        )
        instrument_ids = {
            iid for campaign in campaigns for iid in campaign.instrument_ids
        }
        instruments = tuple(
            instrument
            for iid in sorted(instrument_ids)
            for instrument in (self.survey_repository.get_instrument(iid),)
            if instrument is not None
        )
        qualification = self.survey_repository.latest_qualification(
            self.document_id
        )
        state_entry = None
        blocking: list[tuple[str, str, tuple[str, ...]]] = []
        if qualification is not None:
            for entry in qualification.element_states:
                if (
                    entry.element_id == element_id
                    and entry.element_sha256 == element.element_sha256
                ):
                    state_entry = entry
            for verdict in qualification.task_verdicts:
                if element_id in verdict.blocking_element_ids:
                    blocking.append(
                        (verdict.task_id, verdict.verdict, verdict.reasons)
                    )
        reconciliations = tuple(
            r
            for r in _list_reconciliations(
                self.survey_repository, self.document_id
            )
            if r.element_key == element.element_key
        )
        siblings = {
            other.element_id
            for other in self.survey_repository.list_elements(self.document_id)
            if other.element_id != element_id
            and (
                set(other.campaign_ids) & set(element.campaign_ids)
                or other.element_key == element.element_key
                or other.stale_after_change
            )
        }
        invalidated = tuple(
            dict.fromkeys(
                ref
                for r in reconciliations
                for ref in r.downstream_invalidated_refs
            )
        )
        lines = [f'{element.element_key} — {element.element_id}']
        lines.append(f'原本hash: {element.element_sha256[:16]}…')
        lines.append(
            '証拠クラス: '
            + ', '.join(element.evidence_classes)
            + f' · 観測状態 {element.observation_state}'
            + f' · 意味確度 {element.semantic_confidence}'
        )
        if element.uncertainty:
            parts = [
                f'{item.kind}={"n/a" if item.value_mm is None else f"{item.value_mm:.1f}mm"}'
                for item in element.uncertainty
            ]
            lines.append('不確かさ内訳: ' + ', '.join(parts))
        for campaign in campaigns:
            reg = (
                '位置合わせ未宣言'
                if campaign.registration is None
                else (
                    '位置合わせ不確かさ未計量'
                    if campaign.registration.uncertainty_mm is None
                    else f'位置合わせ ±{campaign.registration.uncertainty_mm:.1f}mm'
                )
            )
            lines.append(
                f'キャンペーン {campaign.campaign_id} — {campaign.label}'
                f' · {campaign.captured_at_utc} · {reg}'
                f' · hash {campaign.campaign_sha256[:12]}…'
            )
        for instrument in instruments:
            app = (
                ' / '.join(
                    part
                    for part in (
                        instrument.capture_app,
                        instrument.capture_app_version,
                    )
                    if part
                )
                or 'アプリ情報なし'
            )
            lines.append(
                f'機器 {instrument.instrument_id} — {instrument.kind}'
                f' ({instrument.capability_class})'
                f' · {instrument.manufacturer or "?" }'
                f' {instrument.model or ""}'
                f' · {app}'
                + (
                    f' · 校正 {len(instrument.calibration_evidence_refs)} 件'
                    if instrument.calibration_evidence_refs
                    else ' · 校正証跡なし'
                )
            )
        if state_entry is not None:
            lines.append(
                f'評価: {state_entry.state} · 階層 T{state_entry.evidence_tier}'
                + (
                    f' · 達成 ±{state_entry.achieved_uncertainty_mm:.1f}mm'
                    if state_entry.achieved_uncertainty_mm is not None
                    else ''
                )
            )
        else:
            lines.append('評価: 現行版に一致する封緘評価なし (unevaluated)')
        for task_id, verdict, reasons in blocking:
            lines.append(
                f'タスク {task_id}: {verdict} — ' + ' / '.join(reasons)
            )
        for record in reconciliations:
            delta = (
                'n/a' if record.delta_mm is None else f'{record.delta_mm:+.1f}mm'
            )
            lines.append(
                f'差分 {record.reconciliation_id}: {delta} '
                f'({"承認済み" if record.approved_change else "未承認"})'
            )
        if invalidated:
            lines.append('下流の失効成果物: ' + ', '.join(invalidated))
        if siblings:
            lines.append('再確認候補: ' + ', '.join(sorted(siblings)))
        return SurveyElementDetail(
            element_id=element.element_id,
            element_key=element.element_key,
            element_sha256=element.element_sha256,
            campaigns=campaigns,
            instruments=instruments,
            state_entry=state_entry,
            qualification_id=(
                qualification.qualification_id
                if qualification is not None
                else None
            ),
            blocking_task_verdicts=tuple(blocking),
            reconciliations=reconciliations,
            recheck_element_ids=tuple(sorted(siblings)),
            invalidated_refs=invalidated,
            detail_lines_ja=tuple(lines),
        )


__all__ = [
    'MODE_LABELS',
    'MODE_LEGEND_ASCII',
    'RoomSurveyOverlayController',
    'SURVEY_OVERLAY_MODES',
    'SurveyElementDetail',
    'SurveyElementTarget',
    'SurveyOverlayControl',
    'SurveyOverlayElement',
    'SurveyOverlayScene',
    'UNMAPPED_REASON_LABELS',
    'VERIFICATION_BUCKETS',
    'VERIFICATION_LABELS',
    'resolve_element_target',
    'resolve_survey_overlay',
]
