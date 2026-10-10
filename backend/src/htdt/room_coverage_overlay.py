"""Per-seat speaker coverage markers on the room 3D view (#1001 / REV73).

Resolves a sealed :class:`CoverageEvaluation` — read ONLY through
:class:`CadCoverageRepository`, which re-resolves the scenario, SceneRevision,
SystemVariant, EquipmentDefinition and DirectivityDataset authorities and
replays the pinned evaluator on every read — into a per-render draw scene:

* one discrete marker per ``SeatCoverageResult`` at the EXACT sealed
  ``receiver_reference_position_m`` — ear-position evidence points, never a
  whole-room heatmap and never interpolation between seats (#755);
* marker colour = the selected quantity on a FIXED discrete dB band scale, so
  two renders of different evaluations stay comparable; unsupported /
  undecided seats draw grey hatched — ``coverage_pass=None`` is never painted
  red or read as 0 dB;
* speaker→seat direction lines draw ONLY when the evaluation's
  ``source_acoustic_axis`` is evidence-determined, and are labelled geometric
  direction — never a claimed measured energy path; occlusion is reported as
  not evaluated;
* scene revision/content hash, variant SHA, equipment/directivity evidence or
  listener identity mismatches surface as HISTORICAL (dimmed, claims
  withheld) or block drawing entirely;
* A/B Δ is per-seat only, gated on identical ear position + frequency grid +
  quantity + the same baseline-revision comparison authority; nothing claims
  unchanged ear positions or an all-seat mean as a uniformity improvement;
* required-vs-diagnostic seat-priority membership (``priority_aggregates``)
  stays in the legend — priority seats never stand for all seats.

This module is Qt-free: it only resolves data. ``RoomViewport3D`` renders the
scene and the Qt panel renders the table/detail from the same scene object.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import acos, degrees, isfinite
from typing import Literal

from .cad_coverage import CoverageEvaluation, SeatCoverageResult
from .cad_coverage_aim_authority import CadAcousticAimState
from .cad_coverage_aim_repository import CadCoverageAimRepository
from .cad_coverage_repository import CadCoverageRepository
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_system_variant_repository import CadSystemVariantRepository


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

CoverageQuantity = Literal['relative_level', 'off_axis_loss', 'coverage_gate']
COVERAGE_QUANTITY_ORDER: tuple[CoverageQuantity, ...] = (
    'relative_level',
    'off_axis_loss',
    'coverage_gate',
)

#: Panel labels (JA) for the comparison quantity.
COVERAGE_QUANTITY_LABELS: dict[str, str] = {
    'relative_level': '相対指向性レベル (dB)',
    'off_axis_loss': 'オフアキシス損失 (dB)',
    'coverage_gate': '適合判定 (閾値)',
}

#: ASCII-only forms for the VTK viewport (CJK glyphs do not render there).
COVERAGE_QUANTITY_ASCII: dict[str, str] = {
    'relative_level': 'rel directivity level',
    'off_axis_loss': 'off-axis loss',
    'coverage_gate': 'eligibility gate',
}

COVERAGE_STATE_LABELS: dict[str, str] = {
    'available': '評価済み',
    'unsupported': '非対応',
    'missing': 'データなし',
    'no_position': '位置なし',
    'identity_mismatch': '席情報不一致',
}

COVERAGE_PRIORITY_LABELS: dict[str, str] = {
    'required': '必須席',
    'weighted': '加重席',
    'diagnostic': '診断席',
    'standard': '—',
}

COVERAGE_GATE_LABELS: dict[str, str] = {
    'pass': 'PASS',
    'fail': 'FAIL',
    'undecided': '未決定',
}

#: FIXED discrete colour bands (dB of loss magnitude) — identical across
#: scenes, evaluations and A/B sides so a colour never depends on the
#: currently shown subset. Index 0 is best, 4 is worst.
COVERAGE_BAND_COLORS: tuple[str, ...] = (
    '#5BBF8A',  # |loss| <= 3 dB
    '#8FC97E',  # <= 6 dB
    '#E1C75A',  # <= 9 dB
    '#E08A5A',  # <= 12 dB
    '#D96C6C',  # > 12 dB
)
COVERAGE_BAND_ASCII: tuple[str, ...] = (
    '<=3dB',
    '3-6dB',
    '6-9dB',
    '9-12dB',
    '>12dB',
)
COVERAGE_UNKNOWN_COLOR = '#8A93A3'   # grey — unknown/unsupported, never red
COVERAGE_UNDECIDED_COLOR = '#8A93A3'
COVERAGE_STALE_COLOR = '#B09BC6'     # historical/lapsed violet (claim withheld)
COVERAGE_PASS_COLOR = '#5BBF8A'
COVERAGE_FAIL_COLOR = '#D96C6C'
COVERAGE_FOCUS_COLOR = '#F2F5F8'
COVERAGE_RAY_COLOR = '#7ED6FF'
COVERAGE_AXIS_COLOR = '#E58383'

COVERAGE_DISCLAIMER_JA = (
    'マーカーは封じられた評価の耳位置証跡点のみを示します — '
    '席間の補間・全席平均の均一性主張・測定エネルギー経路の表示は行いません'
)
COVERAGE_DISCLAIMER_ASCII = (
    'markers = sealed ear-position evidence only (no interpolation, '
    'no measured-energy path)'
)

#: Per-seat-priority glyph channel: the marker SHAPE carries membership so a
#: diagnostic seat can never stand for the whole population.
COVERAGE_ROLE_GLYPH: dict[str, str] = {
    'required': 'sphere',      # + hard-floor ring drawn by the viewport
    'weighted': 'sphere',
    'diagnostic': 'cube',      # excluded from every aggregate (#975)
    'standard': 'sphere',
}
COVERAGE_ROLE_ASCII: dict[str, str] = {
    'required': 'REQ',
    'weighted': 'WGT',
    'diagnostic': 'DIAG',
    'standard': 'SEAT',
}

_REASON_REMEDIATION: tuple[tuple[str, str], ...] = (
    (
        'frequency is outside dataset domain',
        'データセットの周波数域を拡張するか、要求周波数をデータセット域内に設定してください',
    ),
    (
        'horizontal/azimuth angle is outside dataset domain',
        'スピーカーのエイムを調整するか、該当方向の指向性測定データを追加してください',
    ),
    (
        'vertical/elevation angle is outside dataset domain',
        'スピーカーのエイムを調整するか、該当仰角の指向性測定データを追加してください',
    ),
    (
        'dataset forbids interpolation',
        'グリッド上の周波数を選ぶか、補間を許可したデータセットを使用してください',
    ),
    (
        'no explicit acoustic aim_xyz',
        'スピーカーに明示的な音響エイム (aim_xyz) を設定してください',
    ),
    (
        'no explicit scene acoustic reference position',
        '座席に音響基準位置（耳位置）を設定してください',
    ),
    (
        'missing from SystemVariant',
        '座席エンティティを対象バリアントのシーンに含めてください',
    ),
    (
        'not a seat entity',
        '受聽者人口のメンバーを座席エンティティにしてください',
    ),
    (
        'on-axis reference is unsupported',
        'オンアキシス (0°,0°) のリファレンス評価がデータセットで必要です',
    ),
)


def remediation_for_reason(reason: str | None) -> str | None:
    """Map an evaluation-supplied failure reason to next-step guidance (JA).

    The reason text is authority output; the remediation is static guidance —
    the UI never auto-fixes or approves the model on the user's behalf.
    """

    if not reason:
        return None
    for needle, remediation in _REASON_REMEDIATION:
        if needle in reason:
            return remediation
    return '原因を確認し、証跡（シーン・バリアント・データセット）を更新してください'


# ---------------------------------------------------------------------------
# Resolved draw / table scene
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageFrequencyDetail:
    """One requested-grid frequency row for the panel detail block."""

    frequency_hz: float
    support_state: str            # 'SUPPORTED' | 'UNSUPPORTED'
    horizontal_angle_deg: float | None
    vertical_angle_deg: float | None
    relative_level_db: float | None
    reference_level_db: float | None
    off_axis_loss_db: float | None
    reason: str | None
    remediation: str | None


@dataclass(frozen=True, slots=True)
class CoverageSeatMarker:
    """One drawable ear-position marker (domain coordinates)."""

    seat_entity_id: str
    position: Position3
    seat_state: str
    priority_role: str
    glyph: str
    color: str
    wireframe: bool
    value: float | None
    pass_state: str | None        # 'pass' | 'fail' | 'undecided' | None
    label: str                    # ASCII value/gate text
    delta_label: str | None       # ASCII A/B per-seat delta, e.g. 'd+3.0'
    focused: bool


@dataclass(frozen=True, slots=True)
class CoverageRay:
    """Speaker→seat geometric direction line — never an energy path."""

    seat_entity_id: str
    start: Position3
    end: Position3
    color: str


@dataclass(frozen=True, slots=True)
class CoverageSeatRow:
    """One seat row for the Qt panel table (JA display strings + identity)."""

    seat_entity_id: str
    seat_name: str
    has_marker: bool
    value_text: str
    gate_text: str
    state_text: str
    priority_text: str
    delta_text: str
    reason: str | None
    remediation: str | None
    position: Position3 | None
    frequencies: tuple[CoverageFrequencyDetail, ...]


@dataclass(frozen=True, slots=True)
class CoverageProvenance:
    """Authority identity shown in the detail block — verbatim, no recompute."""

    evaluation_id: str
    evaluation_sha256: str
    scenario_id: str
    scenario_sha256: str
    authority_version: str
    algorithm_id: str
    algorithm_version: str
    document_id: str
    scene_revision_id: str
    scene_content_hash: str
    variant_id: str
    variant_sha256: str
    variant_name: str
    equipment_definition_id: str
    equipment_definition_version: str
    equipment_definition_sha256: str
    directivity_dataset_id: str
    directivity_dataset_version: str
    directivity_dataset_sha256: str
    source_entity_id: str
    channel_role_id: str
    coverage_criterion: str
    coverage_threshold_db: float
    frequency_aggregation_semantics: str
    evaluation_frequencies_hz: tuple[float, ...]
    source_acoustic_axis_determined: bool
    seat_weighting: str
    priority_profile_id: str | None


@dataclass(frozen=True, slots=True)
class CoverageOverlayScene:
    """Per-render resolution consumed by the viewport AND the Qt panel."""

    options: tuple['CoverageEvaluationOption', ...]
    state: str  # 'current' | 'historical' | 'blocked' | 'empty'
    blocked_reason: str | None
    evaluation_id: str | None
    variant_id: str | None
    variant_name: str | None
    source_entity_id: str | None
    scene_revision_id: str | None
    scene_content_hash: str | None
    quantity: str
    quantity_label: str
    frequency_hz: float | None
    frequency_mode: str           # 'aggregate' | 'frequency' | 'gate'
    aggregation_semantics: str | None
    threshold_db: float | None
    axis_determined: bool
    axis_reason: str | None
    source_position: Position3 | None
    source_axis: tuple[float, float, float] | None
    markers: tuple[CoverageSeatMarker, ...]
    rays: tuple[CoverageRay, ...]
    seat_rows: tuple[CoverageSeatRow, ...]
    legend: tuple[tuple[str, str], ...]
    viewport_lines: tuple[str, ...]
    notices: tuple[str, ...]
    delta_active: bool
    delta_counts: dict[str, int]
    delta_semantics: str | None
    baseline_label: str | None
    provenance: CoverageProvenance | None
    summary_ja: str


@dataclass(frozen=True, slots=True)
class CoverageEvaluationOption:
    """One selectable evaluation for the panel combo."""

    evaluation_id: str
    variant_id: str
    variant_name: str
    source_entity_id: str
    scene_revision_id: str
    current: bool
    frequencies_hz: tuple[float, ...]

    @property
    def label(self) -> str:
        state = '' if self.current else ' [履歴]'
        return (
            f'{self.variant_name} / {self.source_entity_id} '
            f'({self.evaluation_id.rsplit("-", 1)[-1][:10]}){state}'
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def coverage_band_for_value(quantity: str, value: float) -> tuple[int, str]:
    """Fixed discrete band for a valued quantity (never relative to the scene).

    ``relative_level`` is ≤ 0 dB (0 = on-axis reference); ``off_axis_loss``
    is ≥ 0 dB. Both map onto the same five loss-magnitude bands.
    """

    loss = -float(value) if quantity == 'relative_level' else float(value)
    edges = (3.0, 6.0, 9.0, 12.0)
    index = 0
    for edge in edges:
        if loss <= edge:
            break
        index += 1
    return index, COVERAGE_BAND_COLORS[index]


def _seat_state(seat: SeatCoverageResult, position: Position3 | None) -> str:
    if seat.state == 'available':
        return 'available'
    if position is None:
        return 'no_position'
    return seat.state


def _frequency_index(
    evaluation: CoverageEvaluation, frequency_hz: float | None
) -> int | None:
    if frequency_hz is None:
        return None
    grid = evaluation.scenario.evaluation_frequencies_hz
    for index, value in enumerate(grid):
        if float(value) == float(frequency_hz):
            return index
    return None


def _seat_value(
    seat: SeatCoverageResult,
    *,
    quantity: str,
    frequency_index: int | None,
) -> tuple[float | None, str | None, str, str | None]:
    """(value, pass_state, state, reason) — authority fields verbatim only.

    Per-frequency display reads that grid frequency's own sealed result:
    a seat unsupported overall can still carry a SUPPORTED frequency
    value, and that value is real evaluated evidence. Gate and aggregate
    display is governed by the seat-level state instead.
    """

    if quantity == 'coverage_gate':
        if seat.state != 'available':
            return None, None, seat.state, seat.reason
        if seat.coverage_pass is None:
            return None, 'undecided', 'available', None
        return (
            None,
            'pass' if seat.coverage_pass else 'fail',
            'available',
            None,
        )
    if frequency_index is not None:
        result = seat.frequency_results[frequency_index]
        if result.support_state != 'SUPPORTED':
            return (
                None,
                None,
                'unsupported',
                result.reason or seat.reason,
            )
        value = (
            result.relative_level_db
            if quantity == 'relative_level'
            else result.off_axis_loss_db
        )
        if value is None:
            return (
                None,
                None,
                'missing',
                'frequency result lacks the quantity',
            )
        return float(value), None, 'available', None
    if seat.state != 'available':
        return None, None, seat.state, seat.reason
    scalar = (
        seat.aggregated_relative_directivity_level
        if quantity == 'relative_level'
        else seat.aggregated_off_axis_loss
    )
    if scalar.state != 'available':
        return None, None, scalar.state, scalar.reason
    return float(scalar.value), None, 'available', None


def _priority_role(
    seat_entity_id: str,
    evaluation: CoverageEvaluation,
    profiles: tuple,
) -> str:
    """Seat role channel for the marker + legend.

    Primary source: the evaluation's own ``priority_aggregates`` (exact
    sealed membership). Fallback: a SeatPriorityProfile bound to the
    EVAL's scene revision — declared room membership for that exact
    scene, read through its validating repository. Neither is ever
    presented as an aggregate claim over the whole population.
    """

    aggregates = evaluation.priority_aggregates
    if aggregates is not None:
        if seat_entity_id in aggregates.required_seat_entity_ids:
            return 'required'
        if seat_entity_id in aggregates.normalized_weights:
            return 'weighted'
        return 'diagnostic'
    for profile in profiles:
        if (
            profile.scene_revision_id == evaluation.scene_revision_id
            and profile.scene_content_hash == evaluation.scene_content_hash
        ):
            for member in profile.members:
                if member.seat_entity_id == seat_entity_id:
                    if member.required:
                        return 'required'
                    if member.seat_role == 'diagnostic':
                        return 'diagnostic'
                    return 'weighted'
    return 'standard'


def _axis_angle_deg(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> float | None:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    dot = min(1.0, max(-1.0, dot))
    if not isfinite(dot):
        return None
    return degrees(acos(dot))


def _aim_mismatch_notice(
    aim_repository: CadCoverageAimRepository | None,
    document_id: str,
    source_entity_id: str,
) -> str | None:
    """Design-vs-observed aim divergence on the SAME declared aim record.

    Both axes live in one :class:`CadAcousticAimState`, so the comparison
    never mixes frames with the scene's world-space ``aim_xyz``. Unknown or
    absent axes produce no claim at all.
    """

    if aim_repository is None:
        return None
    try:
        states = aim_repository.list_aim_states(document_id)
    except Exception:  # error-boundary: overlay read — an aim-store failure returns an honest 'comparison unknown' notice, never a fabricated mismatch (noqa: BLE001)
        return '音響エイム権威を読み取れません（設計/実測比較は不明）'
    aim: CadAcousticAimState | None = None
    for state in states:
        if state.speaker_entity_id == source_entity_id:
            aim = state
    if aim is None:
        return None
    design = aim.axis('design_aim_target')
    observed = aim.axis('as_built_observed_aim')
    if (
        design is None
        or observed is None
        or design.vector is None
        or observed.vector is None
    ):
        return None
    angle = _axis_angle_deg(tuple(design.vector), tuple(observed.vector))
    if angle is None or angle <= 0.5:
        return None
    return (
        f'設計エイムと実測エイムが {angle:.1f}° 乖離しています'
        f'（aim {aim.aim_id.rsplit("-", 1)[-1][:10]}）'
    )


def list_coverage_evaluations(
    variant_repository: CadSystemVariantRepository,
    coverage_repository: CadCoverageRepository,
    document_id: str,
) -> tuple[tuple[CoverageEvaluation, ...], dict[str, str], tuple[str, ...]]:
    """Every persisted evaluation for the document, repository-verified.

    A variant whose evaluations fail re-verification contributes a notice —
    its rows are dropped rather than trusted.
    """

    evaluations: list[CoverageEvaluation] = []
    variant_names: dict[str, str] = {}
    notices: list[str] = []
    for variant in variant_repository.list_variants(document_id):
        variant_names[variant.variant_id] = variant.name
        try:
            found = coverage_repository.list_evaluations_for_variant(
                variant.variant_id
            )
        except ValueError as exc:
            notices.append(
                f'バリアント {variant.name} のカバレッジ評価を再検証'
                f'できませんでした: {exc}'
            )
            continue
        evaluations.extend(found)
    return tuple(evaluations), variant_names, tuple(notices)


def _select_evaluation(
    evaluations: tuple[CoverageEvaluation, ...],
    evaluation_id: str | None,
    head_revision_id: str,
) -> tuple[CoverageEvaluation | None, str | None]:
    """Pick the displayed evaluation — the explicit id wins; otherwise prefer
    an evaluation bound to the CURRENT head, then the latest listed."""

    if not evaluations:
        return None, None
    if evaluation_id is not None:
        for evaluation in evaluations:
            if evaluation.evaluation_id == evaluation_id:
                return evaluation, None
        return (
            None,
            f'選択されたカバレッジ評価が見つかりません: {evaluation_id}',
        )
    for evaluation in reversed(evaluations):
        if evaluation.scene_revision_id == head_revision_id:
            return evaluation, None
    return evaluations[-1], None


def _gate_text(pass_state: str | None) -> str:
    if pass_state is None:
        return '—'
    return COVERAGE_GATE_LABELS[pass_state]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_coverage_overlay(
    *,
    scene_repository: SceneRepository,
    variant_repository: CadSystemVariantRepository,
    coverage_repository: CadCoverageRepository,
    aim_repository: CadCoverageAimRepository | None,
    document_id: str,
    priority_repository=None,
    evaluation_id: str | None = None,
    frequency_hz: float | None = None,
    quantity: CoverageQuantity = 'relative_level',
    baseline_evaluation_id: str | None = None,
    focus_seat_entity_id: str | None = None,
) -> CoverageOverlayScene:
    """Resolve the armed coverage display against the CURRENT head.

    Every evaluation read goes through ``CadCoverageRepository`` (which
    replays the pinned evaluator and requires exact equality) — the overlay
    never trusts a cached payload.
    """

    notices: list[str] = []
    head = scene_repository.current_head(document_id)
    if head is None:
        return CoverageOverlayScene(
            options=(),
            state='blocked',
            blocked_reason='シーンの現行リビジョンがありません',
            evaluation_id=None,
            variant_id=None,
            variant_name=None,
            source_entity_id=None,
            scene_revision_id=None,
            scene_content_hash=None,
            quantity=quantity,
            quantity_label=COVERAGE_QUANTITY_LABELS[quantity],
            frequency_hz=frequency_hz,
            frequency_mode='aggregate',
            aggregation_semantics=None,
            threshold_db=None,
            axis_determined=False,
            axis_reason='シーンなし',
            source_position=None,
            source_axis=None,
            markers=(),
            rays=(),
            seat_rows=(),
            legend=(),
            viewport_lines=('coverage: no scene head',),
            notices=(),
            delta_active=False,
            delta_counts={},
            delta_semantics=None,
            baseline_label=None,
            provenance=None,
            summary_ja='シーンがありません',
        )

    evaluations, variant_names, list_notices = list_coverage_evaluations(
        variant_repository, coverage_repository, document_id
    )
    notices.extend(list_notices)

    if not evaluations:
        return CoverageOverlayScene(
            options=(),
            state='empty',
            blocked_reason=None,
            evaluation_id=None,
            variant_id=None,
            variant_name=None,
            source_entity_id=None,
            scene_revision_id=head.revision_id,
            scene_content_hash=head.content_hash,
            quantity=quantity,
            quantity_label=COVERAGE_QUANTITY_LABELS[quantity],
            frequency_hz=frequency_hz,
            frequency_mode='aggregate',
            aggregation_semantics=None,
            threshold_db=None,
            axis_determined=False,
            axis_reason=None,
            source_position=None,
            source_axis=None,
            markers=(),
            rays=(),
            seat_rows=(),
            legend=(),
            viewport_lines=('coverage: no persisted evaluations',),
            notices=tuple(notices),
            delta_active=False,
            delta_counts={},
            delta_semantics=None,
            baseline_label=None,
            provenance=None,
            summary_ja=(
                'カバレッジ評価がありません（比較実行で生成・保存されます）'
            ),
        )

    evaluation, select_error = _select_evaluation(
        evaluations, evaluation_id, head.revision_id
    )
    if evaluation is None:
        return _blocked_scene(
            head_revision_id=head.revision_id,
            head_content_hash=head.content_hash,
            quantity=quantity,
            frequency_hz=frequency_hz,
            reason=select_error or 'カバレッジ評価を選択できません',
            notices=notices,
            options=_options_for(evaluations, variant_names, head),
        )
    # Re-read the selected row through the trusted get — a stale payload or
    # any authority mismatch (scene/variant/equipment/directivity) raises and
    # blocks drawing rather than drawing unverified numbers.
    try:
        evaluation = coverage_repository.get_evaluation(
            evaluation.evaluation_id
        )
    except ValueError as exc:
        return _blocked_scene(
            head_revision_id=head.revision_id,
            head_content_hash=head.content_hash,
            quantity=quantity,
            frequency_hz=frequency_hz,
            reason=f'カバレッジ評価の権威再検証に失敗しました: {exc}',
            notices=notices,
            options=_options_for(evaluations, variant_names, head),
        )
    if evaluation is None:  # pragma: no cover - listed row cannot vanish
        return _blocked_scene(
            head_revision_id=head.revision_id,
            head_content_hash=head.content_hash,
            quantity=quantity,
            frequency_hz=frequency_hz,
            reason='カバレッジ評価が削除されました',
            notices=notices,
            options=_options_for(evaluations, variant_names, head),
        )

    scenario = evaluation.scenario
    variant_name = variant_names.get(evaluation.variant_id)
    variant = variant_repository.get_variant(evaluation.variant_id)
    if variant is None or variant.variant_sha256 != evaluation.variant_sha256:
        return _blocked_scene(
            head_revision_id=head.revision_id,
            head_content_hash=head.content_hash,
            quantity=quantity,
            frequency_hz=frequency_hz,
            reason='カバレッジ評価の SystemVariant 権威が一致しません',
            notices=notices,
            options=_options_for(evaluations, variant_names, head),
        )
    variant_name = variant.name

    historical = (
        evaluation.scene_revision_id != head.revision_id
        or evaluation.scene_content_hash != head.content_hash
    )
    if historical:
        notices.append(
            '評価は別のシーンリビジョンに対するものです — 履歴表示'
            '（数値・判定の主張は保留）'
        )

    # Frequency selection: exact requested grid only — an off-grid request is
    # blocked, never interpolated (#755).
    frequency_index = _frequency_index(evaluation, frequency_hz)
    if frequency_hz is not None and frequency_index is None:
        return _blocked_scene(
            head_revision_id=head.revision_id,
            head_content_hash=head.content_hash,
            quantity=quantity,
            frequency_hz=frequency_hz,
            reason=(
                f'周波数 {frequency_hz:g} Hz は評価の要求グリッドにありません'
                f'（要求グリッド: '
                + ', '.join(f'{f:g}' for f in scenario.evaluation_frequencies_hz)
                + ' Hz — 補間は行いません）'
            ),
            notices=notices,
            options=_options_for(evaluations, variant_names, head),
        )
    # The eligibility gate is a seat-level aggregate decision — it has no
    # per-frequency meaning, so the gate always reads the aggregate.
    if quantity == 'coverage_gate':
        frequency_mode = 'gate'
        frequency_index = None
    elif frequency_index is None:
        frequency_mode = 'aggregate'
    else:
        frequency_mode = 'frequency'

    # ---- A/B resolution ----------------------------------------------------
    baseline: CoverageEvaluation | None = None
    baseline_by_seat: dict[str, SeatCoverageResult] = {}
    delta_active = False
    delta_counts = {'improved': 0, 'same': 0, 'worse': 0, 'undecided': 0}
    delta_semantics: str | None = None
    baseline_label: str | None = None
    if baseline_evaluation_id is not None:
        baseline_label = baseline_evaluation_id
        try:
            baseline = coverage_repository.get_evaluation(
                baseline_evaluation_id
            )
        except ValueError as exc:
            notices.append(
                f'比較基準の評価を再検証できません（A/Bなし）: {exc}'
            )
        if baseline is None:
            notices.append('比較基準の評価が見つかりません（A/Bなし）')
        elif (
            baseline.scene_revision_id != evaluation.scene_revision_id
            or baseline.scene_content_hash != evaluation.scene_content_hash
        ):
            notices.append(
                'A/B 比較は同一ベースラインリビジョンの評価同士でのみ有効です'
            )
            baseline = None
        elif (
            quantity != 'coverage_gate'
            and frequency_mode == 'aggregate'
            and (
                baseline.scenario.evaluation_frequencies_hz
                != scenario.evaluation_frequencies_hz
                or baseline.scenario.frequency_aggregation_semantics
                != scenario.frequency_aggregation_semantics
            )
        ):
            notices.append(
                '集約Δは同一周波数グリッド＋同一集約意味でのみ有効です'
            )
            baseline = None
        elif (
            quantity != 'coverage_gate'
            and frequency_mode == 'frequency'
            and _frequency_index(baseline, frequency_hz) is None
        ):
            notices.append(
                f'周波数 {frequency_hz:g} Hz は基準評価のグリッドにありません'
                '（Δなし）'
            )
            baseline = None
        if baseline is not None:
            baseline_label = variant_names.get(
                baseline.variant_id, baseline.variant_id
            )
            baseline_by_seat = {
                seat.seat_entity_id: seat for seat in baseline.seat_results
            }
            # The baseline's own grid slot for the displayed frequency —
            # same Hz, potentially a different index.
            baseline_frequency_index = (
                _frequency_index(baseline, frequency_hz)
                if frequency_mode == 'frequency'
                else None
            )
            delta_active = True
            delta_semantics = (
                'Δ = 表示値 − 基準値（相対レベル: 正=改善 / 損失: 負=改善）'
                if quantity != 'coverage_gate'
                else 'Δ = 適合判定の変化（PASS/FAIL遷移のみ）'
            )

    # ---- per-seat resolution ------------------------------------------------
    axis_determined = evaluation.source_acoustic_axis is not None
    axis_reason: str | None = None
    if not axis_determined:
        axis_reason = (
            'スピーカーの音響エイム (aim_xyz) が未宣言です — '
            '方向レイは表示しません'
        )
    aim_notice = _aim_mismatch_notice(
        aim_repository, document_id, scenario.source_entity_id
    )
    if aim_notice is not None:
        notices.append(aim_notice)

    # Seat-priority membership: the evaluation's sealed aggregates win
    # when present; otherwise a profile bound to the EVAL's revision is
    # authoritative for that scene — never a claimed evaluation weight.
    profiles: tuple = ()
    if priority_repository is not None:
        try:
            profiles = priority_repository.list_for_document(document_id)
        except Exception:  # error-boundary: overlay read — a profile-read failure shows an honest 'unreadable' notice, never a fabricated profile (noqa: BLE001)
            notices.append('座席優先度プロファイルを読み取れませんでした')
    if axis_determined:
        notices.append(
            'スピーカー→座席の線は幾何方向のみを示します — '
            '遮蔽は未評価、測定されたエネルギー経路ではありません'
        )

    entity_names = {
        entity.entity_id: entity.name for entity in head.document.entities
    }
    markers: list[CoverageSeatMarker] = []
    rays: list[CoverageRay] = []
    seat_rows: list[CoverageSeatRow] = []
    gate_counts = {'pass': 0, 'fail': 0, 'undecided': 0}

    for seat in evaluation.seat_results:
        position = seat.receiver_reference_position_m
        state = _seat_state(seat, position)
        # Listener identity: a CURRENT evaluation's seat must still be an
        # entity on the head document — otherwise the id no longer names the
        # listener the evaluation meant and the marker cannot claim identity.
        if (
            not historical
            and position is not None
            and seat.seat_entity_id not in entity_names
        ):
            state = 'identity_mismatch'
            notices.append(
                f'座席 {seat.seat_entity_id} が現行シーンに存在しません — '
                '受聽者IDの一致を確認してください'
            )
        value, pass_state, value_state, value_reason = _seat_value(
            seat, quantity=quantity, frequency_index=frequency_index
        )
        if state == 'available':
            state = value_state

        delta_label: str | None = None
        delta_text = ''
        baseline_seat = baseline_by_seat.get(seat.seat_entity_id)
        if delta_active and not historical:
            if baseline_seat is None:
                delta_counts['undecided'] += 1
                delta_text = '基準になし'
            elif (
                baseline_seat.receiver_reference_position_m is None
                or position is None
                or baseline_seat.receiver_reference_position_m != position
            ):
                delta_counts['undecided'] += 1
                delta_text = '耳位置不一致 — Δ保留'
                notices.append(
                    f'座席 {seat.seat_entity_id}: 耳位置が評価間で一致しません'
                    ' — Δは保留'
                )
            elif quantity == 'coverage_gate':
                base_value, base_pass, base_state, _r = _seat_value(
                    baseline_seat,
                    quantity=quantity,
                    frequency_index=baseline_frequency_index,
                )
                if pass_state is None or base_pass is None:
                    delta_counts['undecided'] += 1
                    delta_text = 'Δ未決定'
                elif pass_state == base_pass:
                    delta_counts['same'] += 1
                    delta_text = f'{pass_state} 維持'
                    delta_label = 'same'
                else:
                    improved = pass_state == 'pass' and base_pass == 'fail'
                    delta_counts['improved' if improved else 'worse'] += 1
                    delta_text = (
                        f'{_gate_text(base_pass)}→{_gate_text(pass_state)}'
                    )
                    delta_label = delta_text
            else:
                base_value, _bp, base_state, _r = _seat_value(
                    baseline_seat,
                    quantity=quantity,
                    frequency_index=baseline_frequency_index,
                )
                if value is None or base_value is None:
                    delta_counts['undecided'] += 1
                    delta_text = 'Δ不可'
                else:
                    delta = value - base_value
                    if abs(delta) < 1e-9:
                        delta_counts['same'] += 1
                        delta_label = f'd{delta:+.1f}'
                    elif (quantity == 'relative_level') == (delta > 0):
                        delta_counts['improved'] += 1
                        delta_label = f'd{delta:+.1f}'
                    else:
                        delta_counts['worse'] += 1
                        delta_label = f'd{delta:+.1f}'
                    delta_text = f'Δ {delta:+.2f} dB'

        # Draw state.
        if historical:
            color = COVERAGE_STALE_COLOR
            wireframe = True
            label = ''
        elif quantity == 'coverage_gate':
            if pass_state == 'pass':
                color = COVERAGE_PASS_COLOR
            elif pass_state == 'fail':
                color = COVERAGE_FAIL_COLOR
            else:
                color = COVERAGE_UNDECIDED_COLOR
            wireframe = state != 'available'
            label = (
                'PASS'
                if pass_state == 'pass'
                else ('FAIL' if pass_state == 'fail' else 'n/a')
            )
        elif value is None:
            color = COVERAGE_UNKNOWN_COLOR
            wireframe = True
            label = 'n/a'
        else:
            _band, color = coverage_band_for_value(quantity, value)
            wireframe = False
            label = f'{value:+.1f}'

        role = _priority_role(seat.seat_entity_id, evaluation, profiles)
        glyph = COVERAGE_ROLE_GLYPH[role]
        if position is not None:
            markers.append(
                CoverageSeatMarker(
                    seat_entity_id=seat.seat_entity_id,
                    position=position,
                    seat_state=state,
                    priority_role=role,
                    glyph=glyph,
                    color=color,
                    wireframe=wireframe,
                    value=value,
                    pass_state=pass_state,
                    label=label,
                    delta_label=delta_label,
                    focused=seat.seat_entity_id == focus_seat_entity_id,
                )
            )
            if axis_determined and not historical:
                rays.append(
                    CoverageRay(
                        seat_entity_id=seat.seat_entity_id,
                        start=evaluation.source_reference_position_m,
                        end=position,
                        color=(
                            COVERAGE_RAY_COLOR
                            if state == 'available'
                            else COVERAGE_UNKNOWN_COLOR
                        ),
                    )
                )

        if pass_state in gate_counts:
            gate_counts[pass_state] += 1

        reason = value_reason or seat.reason
        seat_rows.append(
            CoverageSeatRow(
                seat_entity_id=seat.seat_entity_id,
                seat_name=entity_names.get(
                    seat.seat_entity_id, seat.seat_entity_id
                ),
                has_marker=position is not None,
                value_text=(
                    ''
                    if historical
                    else (
                        f'{value:+.1f} dB'
                        if value is not None
                        else '—'
                    )
                ),
                gate_text='' if historical else _gate_text(pass_state),
                state_text=COVERAGE_STATE_LABELS.get(state, state),
                priority_text=COVERAGE_PRIORITY_LABELS[role],
                delta_text=delta_text,
                reason=reason,
                remediation=remediation_for_reason(reason),
                position=position,
                frequencies=tuple(
                    CoverageFrequencyDetail(
                        frequency_hz=item.requested_frequency_hz,
                        support_state=item.support_state,
                        horizontal_angle_deg=(
                            None
                            if item.source_relative_angles is None
                            else item.source_relative_angles.horizontal_angle_deg
                        ),
                        vertical_angle_deg=(
                            None
                            if item.source_relative_angles is None
                            else item.source_relative_angles.vertical_angle_deg
                        ),
                        relative_level_db=item.relative_level_db,
                        reference_level_db=item.reference_level_db,
                        off_axis_loss_db=item.off_axis_loss_db,
                        reason=item.reason,
                        remediation=remediation_for_reason(item.reason),
                    )
                    for item in seat.frequency_results
                ),
            )
        )

    # ---- legend / viewport text ----------------------------------------------
    legend: list[tuple[str, str]] = []
    if quantity == 'coverage_gate':
        legend.append(('PASS', COVERAGE_PASS_COLOR))
        legend.append(('FAIL', COVERAGE_FAIL_COLOR))
        legend.append(('undecided', COVERAGE_UNDECIDED_COLOR))
    else:
        for index, color in enumerate(COVERAGE_BAND_COLORS):
            legend.append((COVERAGE_BAND_ASCII[index], color))
    used_roles = {marker.priority_role for marker in markers}
    for role in ('required', 'weighted', 'diagnostic'):
        if role in used_roles:
            legend.append((COVERAGE_ROLE_ASCII[role], COVERAGE_FOCUS_COLOR))

    viewport_lines: list[str] = []
    eval_short = evaluation.evaluation_id.rsplit('-', 1)[-1][:10]
    rev_short = evaluation.scene_revision_id[:8]
    state_tag = 'HISTORICAL' if historical else 'current'
    variant_tag = (
        variant_name
        if variant_name is not None and variant_name.isascii()
        else evaluation.variant_id[:12]
    )
    viewport_lines.append(
        f'coverage: {eval_short} {variant_tag} rev:{rev_short} {state_tag}'
    )
    viewport_lines.append(
        f'src:{scenario.source_entity_id} axis:'
        f'{"yes" if axis_determined else "none"}'
    )
    if quantity == 'coverage_gate':
        freq_tag = 'gate (seat aggregate)'
    elif frequency_mode == 'aggregate':
        agg = (
            'worst'
            if scenario.frequency_aggregation_semantics
            == 'worst_over_requested_frequencies'
            else 'mean'
        )
        freq_tag = f'agg-{agg}'
    else:
        freq_tag = f'{frequency_hz:g}Hz'
    viewport_lines.append(
        f'qty:{COVERAGE_QUANTITY_ASCII[quantity]} {freq_tag} unit:dB'
    )
    if historical:
        viewport_lines.append(
            'HISTORICAL: sealed revision shown — claims withheld'
        )
    else:
        viewport_lines.append(
            f'seats:{len(markers)}/{len(evaluation.seat_results)} '
            f'pass:{gate_counts["pass"]} fail:{gate_counts["fail"]} '
            f'n/a:{gate_counts["undecided"]}'
        )
    if delta_active:
        baseline_tag = (
            baseline_label
            if baseline_label is not None and baseline_label.isascii()
            else 'baseline'
        )
        viewport_lines.append(
            f'delta vs {baseline_tag}: '
            f'+{delta_counts["improved"]} ={delta_counts["same"]} '
            f'-{delta_counts["worse"]} ?{delta_counts["undecided"]}'
        )
    if axis_determined and not historical:
        viewport_lines.append(
            'rays = geometric direction only (occlusion not evaluated)'
        )
    viewport_lines.append(COVERAGE_DISCLAIMER_ASCII)

    state = 'historical' if historical else 'current'
    summary_bits: list[str] = [
        f'評価 {eval_short} — {variant_name or evaluation.variant_id}',
        f'表示 {len(markers)}/{len(evaluation.seat_results)} 席',
        COVERAGE_QUANTITY_LABELS[quantity],
    ]
    if historical:
        summary_bits.append('履歴（別リビジョンの評価 — 主張は保留）')
    if delta_active:
        summary_bits.append(
            f'Δ vs {baseline_label}: 改善 {delta_counts["improved"]} · '
            f'同一 {delta_counts["same"]} · 悪化 {delta_counts["worse"]} · '
            f'未決定 {delta_counts["undecided"]}'
        )

    provenance = CoverageProvenance(
        evaluation_id=evaluation.evaluation_id,
        evaluation_sha256=evaluation.evaluation_sha256,
        scenario_id=scenario.scenario_id,
        scenario_sha256=scenario.scenario_sha256,
        authority_version=evaluation.authority_version,
        algorithm_id=scenario.algorithm_id,
        algorithm_version=scenario.algorithm_version,
        document_id=evaluation.document_id,
        scene_revision_id=evaluation.scene_revision_id,
        scene_content_hash=evaluation.scene_content_hash,
        variant_id=evaluation.variant_id,
        variant_sha256=evaluation.variant_sha256,
        variant_name=variant_name or evaluation.variant_id,
        equipment_definition_id=evaluation.equipment_definition_id,
        equipment_definition_version=evaluation.equipment_definition_version,
        equipment_definition_sha256=evaluation.equipment_definition_sha256,
        directivity_dataset_id=evaluation.directivity_dataset_id,
        directivity_dataset_version=evaluation.directivity_dataset_version,
        directivity_dataset_sha256=evaluation.directivity_dataset_sha256,
        source_entity_id=scenario.source_entity_id,
        channel_role_id=scenario.channel_role_id,
        coverage_criterion=scenario.coverage_criterion,
        coverage_threshold_db=scenario.coverage_threshold_db,
        frequency_aggregation_semantics=scenario.frequency_aggregation_semantics,
        evaluation_frequencies_hz=scenario.evaluation_frequencies_hz,
        source_acoustic_axis_determined=axis_determined,
        seat_weighting=scenario.seat_weighting_semantics,
        priority_profile_id=scenario.receiver_population.priority_profile_id,
    )

    return CoverageOverlayScene(
        options=_options_for(evaluations, variant_names, head),
        state=state,
        blocked_reason=None,
        evaluation_id=evaluation.evaluation_id,
        variant_id=evaluation.variant_id,
        variant_name=variant_name,
        source_entity_id=scenario.source_entity_id,
        scene_revision_id=evaluation.scene_revision_id,
        scene_content_hash=evaluation.scene_content_hash,
        quantity=quantity,
        quantity_label=COVERAGE_QUANTITY_LABELS[quantity],
        frequency_hz=(
            frequency_hz if frequency_mode == 'frequency' else None
        ),
        frequency_mode=frequency_mode,
        aggregation_semantics=scenario.frequency_aggregation_semantics,
        threshold_db=scenario.coverage_threshold_db,
        axis_determined=axis_determined,
        axis_reason=axis_reason,
        source_position=evaluation.source_reference_position_m,
        source_axis=(
            None
            if evaluation.source_acoustic_axis is None
            else (
                float(evaluation.source_acoustic_axis.x),
                float(evaluation.source_acoustic_axis.y),
                float(evaluation.source_acoustic_axis.z),
            )
        ),
        markers=tuple(markers),
        rays=tuple(rays),
        seat_rows=tuple(seat_rows),
        legend=tuple(legend),
        viewport_lines=tuple(viewport_lines),
        notices=tuple(notices),
        delta_active=delta_active and not historical,
        delta_counts=delta_counts if delta_active and not historical else {},
        delta_semantics=delta_semantics,
        baseline_label=baseline_label,
        provenance=provenance,
        summary_ja=' / '.join(summary_bits),
    )


def _options_for(
    evaluations: tuple[CoverageEvaluation, ...],
    variant_names: dict[str, str],
    head,
) -> tuple[CoverageEvaluationOption, ...]:
    """Selectable evaluations built from the same verified listing."""

    return tuple(
        CoverageEvaluationOption(
            evaluation_id=item.evaluation_id,
            variant_id=item.variant_id,
            variant_name=variant_names.get(item.variant_id, item.variant_id),
            source_entity_id=item.scenario.source_entity_id,
            scene_revision_id=item.scene_revision_id,
            current=(
                item.scene_revision_id == head.revision_id
                and item.scene_content_hash == head.content_hash
            ),
            frequencies_hz=item.scenario.evaluation_frequencies_hz,
        )
        for item in evaluations
    )


def _blocked_scene(
    *,
    head_revision_id: str,
    head_content_hash: str,
    quantity: str,
    frequency_hz: float | None,
    reason: str,
    notices: list[str],
    options: tuple[CoverageEvaluationOption, ...] = (),
) -> CoverageOverlayScene:
    return CoverageOverlayScene(
        options=options,
        state='blocked',
        blocked_reason=reason,
        evaluation_id=None,
        variant_id=None,
        variant_name=None,
        source_entity_id=None,
        scene_revision_id=head_revision_id,
        scene_content_hash=head_content_hash,
        quantity=quantity,
        quantity_label=COVERAGE_QUANTITY_LABELS[quantity],
        frequency_hz=frequency_hz,
        frequency_mode='aggregate',
        aggregation_semantics=None,
        threshold_db=None,
        axis_determined=False,
        axis_reason=None,
        source_position=None,
        source_axis=None,
        markers=(),
        rays=(),
        seat_rows=(),
        legend=(),
        viewport_lines=(f'coverage blocked: {reason[:80]}',),
        notices=tuple(notices) + (reason,),
        delta_active=False,
        delta_counts={},
        delta_semantics=None,
        baseline_label=None,
        provenance=None,
        summary_ja=reason,
    )


# ---------------------------------------------------------------------------
# Controller — armed request + per-render resolve with head-keyed caching
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageOverlayRequest:
    """Panel-agnostic display request (the カバレッジ表示 state)."""

    evaluation_id: str | None = None
    frequency_hz: float | None = None
    quantity: CoverageQuantity = 'relative_level'
    baseline_evaluation_id: str | None = None


class RoomSeatCoverageOverlayController:
    """Arms and resolves the seat-coverage overlay for a room workspace.

    Same discipline as the campaign/treatment controllers: resolve()
    re-reads ``current_head`` plus the sealed evaluation store on every
    call — deliberately uncached so a scene edit, a new comparison run or
    a selector change can never leave a superseded marker on screen.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository,
        coverage_repository: CadCoverageRepository,
        document_id: str,
        aim_repository: CadCoverageAimRepository | None = None,
        priority_repository=None,
    ) -> None:
        self._scene_repository = scene_repository
        self._variant_repository = variant_repository
        self._coverage_repository = coverage_repository
        self._aim_repository = aim_repository
        self._priority_repository = priority_repository
        self._document_id = document_id
        self._request = CoverageOverlayRequest()
        self._enabled = False

    @property
    def armed(self) -> bool:
        return self._enabled

    @property
    def request(self) -> CoverageOverlayRequest:
        return self._request

    def arm(self, request: CoverageOverlayRequest) -> None:
        self._request = request
        self._enabled = True

    def update(self, request: CoverageOverlayRequest) -> None:
        """Change the display parameters without toggling the overlay."""
        if request != self._request:
            self._request = request

    def clear(self) -> None:
        self._enabled = False
        self._request = CoverageOverlayRequest()

    def list_options(
        self,
    ) -> tuple[tuple[CoverageEvaluationOption, ...], tuple[str, ...]]:
        """Selectable evaluations for the panel combo (repository-verified)."""

        head = self._scene_repository.current_head(self._document_id)
        evaluations, _names, notices = list_coverage_evaluations(
            self._variant_repository,
            self._coverage_repository,
            self._document_id,
        )
        options = tuple(
            CoverageEvaluationOption(
                evaluation_id=evaluation.evaluation_id,
                variant_id=evaluation.variant_id,
                variant_name=_names.get(
                    evaluation.variant_id, evaluation.variant_id
                ),
                source_entity_id=evaluation.scenario.source_entity_id,
                scene_revision_id=evaluation.scene_revision_id,
                current=(
                    head is not None
                    and evaluation.scene_revision_id == head.revision_id
                    and evaluation.scene_content_hash == head.content_hash
                ),
                frequencies_hz=evaluation.scenario.evaluation_frequencies_hz,
            )
            for evaluation in evaluations
        )
        return options, notices

    def resolve(
        self, *, focus_seat_entity_id: str | None = None
    ) -> CoverageOverlayScene | None:
        """Fresh resolution every call — deliberately uncached.

        A cross-render cache keyed by head cannot see evaluations written
        mid-session under an unchanged head (e.g. a fresh comparison run),
        so resolving would serve a stale scene. The repository re-read is
        the honesty contract; the cost is bounded evaluator replay.
        """
        if not self._enabled:
            return None
        if self._scene_repository.current_head(self._document_id) is None:
            return None
        request = self._request
        return resolve_coverage_overlay(
            scene_repository=self._scene_repository,
            variant_repository=self._variant_repository,
            coverage_repository=self._coverage_repository,
            aim_repository=self._aim_repository,
            document_id=self._document_id,
            priority_repository=self._priority_repository,
            evaluation_id=request.evaluation_id,
            frequency_hz=request.frequency_hz,
            quantity=request.quantity,
            baseline_evaluation_id=request.baseline_evaluation_id,
            focus_seat_entity_id=focus_seat_entity_id,
        )
