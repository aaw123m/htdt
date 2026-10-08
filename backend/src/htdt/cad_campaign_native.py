"""Issue #956 — native automated campaign execution bound to runner plans.

Converts a #529 :class:`MeasurementRunnerPlan` into the canonical #875
:class:`CampaignExecutionPlan`, resolves the exact queue-entry ↔
runner-cell binding, runs a read-only PRECHECK across every declared
route, and drives the :class:`MeasurementCampaignRunner` from a
UI-facing surface.

The #869 acquisition engine and the sealed campaign journal do all the
work — this module only materializes and drives. It never fabricates
evidence, never substitutes spec or saved settings for an explicit arm
approval, and never promotes a SIMULATED run to a measured verdict:
``backend_is_simulated`` from each ``mcrun-`` record is surfaced
verbatim and ambiguous entry↔cell bindings stay parked for manual
review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .cad_authority_resolver import AuthorityRef
from .cad_campaign_execution import (
    CampaignAutomationPolicy,
    CampaignChannelBinding,
    CampaignExecutionPlan,
    CampaignExecutionState,
    CampaignPositionSpec,
    PositionConfirmation,
    build_campaign_execution_plan,
    derive_campaign_state,
)
from .cad_campaign_execution_runner import (
    MeasurementCampaignRunner,
    RunnerStepResult,
)
from .cad_measurement_runner import MeasurementRunnerPlan, RunnerCellSpec
from .cad_owned_room_campaign import MeasurementRole
from .cad_repository import SceneRevision
from .cad_sweep_acquisition import (
    AcquisitionRequest,
    AudioIOBackend,
    ChannelRouting,
    LevelSafetyPolicy,
    MeasurementAcquisitionEngine,
    QualityGateThresholds,
    SweepStimulusSpec,
)
from .clock import utc_now_iso

#: Runner purposes map onto the campaign MeasurementRole vocabulary.
#: 'measurement' cells run the same condition repeatedly — repeatability
#: is the honest role; nothing claims calibration-grade evidence.
RUNNER_PURPOSE_TO_ROLE: dict[str, MeasurementRole] = {
    'measurement': 'repeatability',
    'calibration': 'calibration',
    'holdout': 'holdout',
    'diagnostic': 'screening',
}

_ROLE_ORDER: tuple[MeasurementRole, ...] = (
    'calibration', 'holdout', 'repeatability', 'perturbation', 'screening')

#: The only backend id whose capture is simulated evidence — mirrors
#: ``cad_sweep_acquisition_evidence``'s rule, kept literal here so this
#: module never reaches into a private constant.
_SIMULATED_BACKEND_ID = 'fake-audio-io'


class NativePlanConversionError(ValueError):
    """The runner plan cannot be converted to a canonical campaign plan.

    Raised instead of silently inventing queue entries or dropping
    cells — a campaign that does not exactly match the runner matrix
    fails closed before any audio output.
    """


def channel_entity_id_for(
        channel_role: str,
        source_speaker_ids: Sequence[str]) -> str:
    """Deterministic logical-channel id for a runner cell's source.

    The same speaker-role token grouping different physical radiators
    keeps them in one logical channel (#925) — the id pins exactly the
    bound speaker set so a distinct grouping is a distinct channel.
    """

    return f"{channel_role}[{'+'.join(sorted(source_speaker_ids))}]"


@dataclass(frozen=True)
class CampaignCellAssignments:
    """The queue-entry ↔ runner-cell binding for one campaign plan.

    ``entry_to_cell`` is the exact 1:1 bind used for automatic evidence
    attachment; ``ambiguous`` entries have zero or several candidate
    cells and stay parked for manual review — they are never auto-bound.
    ``unbound_cells`` are runner cells no queue entry covers (variant
    plans outside the canonical matrix).
    """

    entry_to_cell: dict[str, int]
    ambiguous: dict[str, tuple[int, ...]]
    unbound_cells: tuple[int, ...]

    @property
    def exact(self) -> bool:
        return not self.ambiguous and not self.unbound_cells

    def cell_for_entry(self, entry_key: str) -> int | None:
        return self.entry_to_cell.get(entry_key)

    def entry_for_cell(self, cell_index: int) -> str | None:
        for entry_key, cell_index_hit in self.entry_to_cell.items():
            if cell_index_hit == cell_index:
                return entry_key
        return None


def _cell_key(cell: RunnerCellSpec) -> tuple[str, str, str, int]:
    return (
        cell.target_entity_id,
        channel_entity_id_for(cell.channel_role, cell.source_speaker_ids),
        RUNNER_PURPOSE_TO_ROLE[cell.purpose],
        cell.repeat_index + 1,
    )


def resolve_cell_assignments(
        runner_plan: MeasurementRunnerPlan,
        exec_plan: CampaignExecutionPlan) -> CampaignCellAssignments:
    """Resolve each queue entry to exactly one runner cell — or park it."""

    index: dict[tuple[str, str, str, int], list[int]] = {}
    for cell in runner_plan.cells:
        index.setdefault(_cell_key(cell), []).append(cell.cell_index)
    bound: dict[str, int] = {}
    ambiguous: dict[str, tuple[int, ...]] = {}
    used: set[int] = set()
    for entry in exec_plan.queue:
        key = (entry.position_id, entry.channel_entity_id,
               entry.role, entry.run_index)
        hits = index.get(key, [])
        if len(hits) == 1:
            bound[entry.entry_key] = hits[0]
            used.add(hits[0])
        else:
            # Zero candidates means the queue invents work; several means
            # indistinguishable cells — both park for manual review.
            ambiguous[entry.entry_key] = tuple(hits)
    unbound = tuple(sorted(
        set(range(len(runner_plan.cells))) - used))
    return CampaignCellAssignments(
        entry_to_cell=bound, ambiguous=ambiguous, unbound_cells=unbound)


@dataclass(frozen=True)
class NativeCampaignMaterialization:
    """A sealed campaign plan plus its exact cell binding."""

    plan: CampaignExecutionPlan
    assignments: CampaignCellAssignments


def materialize_native_campaign_plan(
        *,
        runner_plan: MeasurementRunnerPlan,
        revision: SceneRevision,
        routings: Mapping[str, ChannelRouting],
        stimulus_template: SweepStimulusSpec,
        policy: CampaignAutomationPolicy | None = None,
        level_policy: LevelSafetyPolicy | None = None,
        requires_absolute_level: bool = False,
        declared_synchronized: bool = False,
        quality_thresholds: QualityGateThresholds | None = None,
        notes: str | None = None,
        generated_at_utc: str | None = None,
) -> NativeCampaignMaterialization:
    """Canonical #875 plan pinned to the runner plan's exact matrix.

    Fails closed: unknown positions/channels, missing routings, gapped or
    non-uniform repetition counts, and any plan whose materialized queue
    does not match the runner cells 1:1 are conversion errors — never
    silently invented or dropped runs.
    """

    entities = {
        entity.entity_id: entity for entity in revision.document.entities}
    if revision.revision_id != runner_plan.scene_revision_id:
        raise NativePlanConversionError(
            '実行計画のシーン改訂が現在の計画と一致しません')

    position_order: list[str] = []
    channel_order: list[str] = []
    channel_cells: dict[str, RunnerCellSpec] = {}
    for cell in runner_plan.cells:
        if cell.target_entity_id not in entities:
            raise NativePlanConversionError(
                f'測定位置 {cell.target_entity_id} が現在の部屋に存在しません')
        channel_id = channel_entity_id_for(
            cell.channel_role, cell.source_speaker_ids)
        if cell.target_entity_id not in position_order:
            position_order.append(cell.target_entity_id)
        if channel_id not in channel_cells:
            channel_cells[channel_id] = cell
            channel_order.append(channel_id)
        elif channel_cells[channel_id].channel_role != cell.channel_role:
            raise NativePlanConversionError(
                f'チャンネル {channel_id} の役割が一貫していません')

    bindings: list[CampaignChannelBinding] = []
    for channel_id in channel_order:
        routing = routings.get(channel_id)
        if routing is None:
            raise NativePlanConversionError(
                f'チャンネル {channel_id} にルーティングが割り当てられていません')
        if not routing.playback_device_id or not routing.capture_device_id:
            raise NativePlanConversionError(
                f'チャンネル {channel_id} のデバイスが未指定です — '
                '事前チェックで提示できるルーティングがありません')
        bindings.append(CampaignChannelBinding(
            channel_entity_id=channel_id,
            playback_device_id=routing.playback_device_id,
            playback_channel=routing.playback_channel,
            capture_device_id=routing.capture_device_id,
            capture_channel=routing.capture_channel,
            loopback_input_channel=routing.loopback_input_channel,
        ))

    # Positions carry only the roles/channels their cells actually use;
    # repetition counts must be uniform per (position, channel, role)
    # group — the canonical queue is a strict cross product.
    groups: dict[tuple[str, str, str], list[int]] = {}
    for cell in runner_plan.cells:
        key = (cell.target_entity_id,
               channel_entity_id_for(
                   cell.channel_role, cell.source_speaker_ids),
               RUNNER_PURPOSE_TO_ROLE[cell.purpose])
        groups.setdefault(key, []).append(cell.repeat_index)
    repetitions: int | None = None
    for key, repeats in groups.items():
        ordered = sorted(repeats)
        if ordered != list(range(len(ordered))):
            raise NativePlanConversionError(
                f'{key[0]} のリピート番号が連続していません — '
                '自動実行の正準行列に変換できません')
        if repetitions is None:
            repetitions = len(ordered)
        elif len(ordered) != repetitions:
            raise NativePlanConversionError(
                'リピート回数がグループ間で異なります — '
                '自動実行の正準行列に変換できません')
    if repetitions is None:
        raise NativePlanConversionError('計画にセルがありません')

    positions: list[CampaignPositionSpec] = []
    for position_id in position_order:
        cells = [c for c in runner_plan.cells
                 if c.target_entity_id == position_id]
        roles = sorted(
            {RUNNER_PURPOSE_TO_ROLE[c.purpose] for c in cells},
            key=_ROLE_ORDER.index)
        channels = [
            channel_entity_id_for(c.channel_role, c.source_speaker_ids)
            for c in cells]
        seen: list[str] = []
        for channel_id in channels:
            if channel_id not in seen:
                seen.append(channel_id)
        entity = entities[position_id]
        positions.append(CampaignPositionSpec(
            position_id=position_id,
            declared_position=entity.position,
            roles=tuple(roles),
            required=not all(c.allow_skip for c in cells),
            channel_entity_ids=tuple(seen),
        ))

    kwargs: dict = dict(
        document_id=runner_plan.document_id,
        campaign_ref=AuthorityRef(
            kind='measurement_runner_plan',
            ref_id=runner_plan.plan_id,
            ref_sha256=runner_plan.plan_sha256,
        ),
        stimulus_template=stimulus_template,
        channel_bindings=bindings,
        positions=positions,
        repetitions_per_entry=repetitions,
        policy=policy or CampaignAutomationPolicy(),
        generated_at_utc=generated_at_utc or utc_now_iso(),
        scene_ref=AuthorityRef(
            kind='scene_revision',
            ref_id=revision.revision_id,
            ref_sha256=revision.content_hash,
        ),
        requires_absolute_level=requires_absolute_level,
        declared_synchronized=declared_synchronized,
    )
    if level_policy is not None:
        kwargs['level_policy'] = level_policy
    if quality_thresholds is not None:
        kwargs['quality_thresholds'] = quality_thresholds
    if notes is not None:
        kwargs['notes'] = notes
    try:
        plan = build_campaign_execution_plan(**kwargs)
    except ValueError as exc:
        raise NativePlanConversionError(str(exc)) from exc

    # Fail closed: the materialized queue must cover exactly the runner
    # matrix — no invented or dropped work.
    expected = sorted(_cell_key(c) for c in runner_plan.cells)
    actual = sorted(
        (e.position_id, e.channel_entity_id, e.role, e.run_index)
        for e in plan.queue)
    if actual != expected:
        raise NativePlanConversionError(
            '実行キューが計画セルと一致しません — 自動実行に変換できません')

    return NativeCampaignMaterialization(
        plan=plan,
        assignments=resolve_cell_assignments(runner_plan, plan))


# ---------------------------------------------------------------------------
# Read-only preflight — nothing emits audio from here
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativePreflightReport:
    """Everything the operator approves before arming — read-only."""

    plan_id: str
    simulated_backend: bool
    playback_devices: tuple[str, ...]
    capture_devices: tuple[str, ...]
    routing_lines: tuple[str, ...]
    position_lines: tuple[str, ...]
    level_dbfs: float
    max_output_level_dbfs: float
    calibration_state: str
    blocked_reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.blocked_reasons


def build_native_preflight(
        *,
        plan: CampaignExecutionPlan,
        backend: AudioIOBackend,
        calibration_state: str = 'unknown',
        position_names: Mapping[str, str] | None = None,
) -> NativePreflightReport:
    """Enumerate devices and PRECHECK every declared route — read-only.

    Each distinct routing gets a real engine PRECHECK; a failure lands
    in ``blocked_reasons`` and the caller must refuse arming.
    """

    try:
        devices = backend.enumerate_devices()
    except Exception:
        devices = ()
    playback = tuple(d.device_id for d in devices
                     if d.direction in ('playback', 'duplex'))
    capture = tuple(d.device_id for d in devices
                    if d.direction in ('capture', 'duplex'))
    simulated = backend.backend_id == _SIMULATED_BACKEND_ID

    blocked: list[str] = []
    if not playback:
        blocked.append('再生デバイスが検出されていません')
    if not capture:
        blocked.append('キャプチャデバイスが検出されていません')

    routing_lines: list[str] = []
    seen_routings: set[tuple] = set()
    for binding in plan.channel_bindings:
        routing_lines.append(
            f'{binding.channel_entity_id}: '
            f'再生 {binding.playback_device_id} ch{binding.playback_channel} / '
            f'録音 {binding.capture_device_id} ch{binding.capture_channel}'
            + (f' (loopback ch{binding.loopback_input_channel})'
               if binding.loopback_input_channel is not None else ''))
        key = (binding.playback_device_id, binding.playback_channel,
               binding.capture_device_id, binding.capture_channel,
               binding.loopback_input_channel)
        if key in seen_routings:
            continue
        seen_routings.add(key)
        engine = MeasurementAcquisitionEngine(backend)
        try:
            report = engine.configure(AcquisitionRequest(
                stimulus=plan.stimulus_template,
                routing=ChannelRouting(
                    playback_device_id=binding.playback_device_id,
                    playback_channel=binding.playback_channel,
                    capture_device_id=binding.capture_device_id,
                    capture_channel=binding.capture_channel,
                    loopback_input_channel=(
                        binding.loopback_input_channel),
                ),
                level_policy=plan.level_policy,
                requires_absolute_level=plan.requires_absolute_level,
                calibration_state=calibration_state,
                declared_synchronized=plan.declared_synchronized,
                quality_thresholds=plan.quality_thresholds,
            ))
        except Exception as exc:  # PRECHECK fail-closed
            blocked.append(
                f'{binding.channel_entity_id}: 事前チェック失敗 — {exc}')
            continue
        for reason in report.blocked_reasons:
            blocked.append(f'{binding.channel_entity_id}: {reason}')

    names = dict(position_names or {})
    position_lines = tuple(
        f"{names.get(pos.position_id, pos.position_id)}"
        + (f' ({pos.declared_position.x_m:.2f}, '
           f'{pos.declared_position.y_m:.2f}, '
           f'{pos.declared_position.z_m:.2f}) m'
           if pos.declared_position is not None else ' — 座標未宣言')
        + '（移動時に確認が必要）'
        for pos in plan.positions)

    return NativePreflightReport(
        plan_id=plan.plan_id,
        simulated_backend=simulated,
        playback_devices=playback,
        capture_devices=capture,
        routing_lines=tuple(routing_lines),
        position_lines=position_lines,
        level_dbfs=plan.stimulus_template.level_dbfs,
        max_output_level_dbfs=plan.level_policy.max_output_level_dbfs,
        calibration_state=calibration_state,
        blocked_reasons=tuple(blocked),
    )


# ---------------------------------------------------------------------------
# Drive — sealed-journal orchestration for the UI
# ---------------------------------------------------------------------------


class NativeCampaignDrive:
    """Owns one campaign runner over the sealed journal.

    Construct via the campaign repository's ``runner_for`` — a rebuild
    from persisted events + run records IS the restart-resume path, so
    re-opening the page continues exactly where the journal left off.
    """

    def __init__(
            self,
            *,
            campaign_repository,
            plan: CampaignExecutionPlan,
            engine_factory: Callable[[], MeasurementAcquisitionEngine],
            arm_confirmation_provider,
            calibration_state_provider=None,
            position_ref_for=None,
            acquisition_sink=None,
            clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.plan = plan
        self._repository = campaign_repository
        self.runner: MeasurementCampaignRunner = (
            campaign_repository.runner_for(
                plan,
                engine_factory=engine_factory,
                arm_confirmation_provider=arm_confirmation_provider,
                calibration_state_provider=calibration_state_provider,
                position_ref_for=position_ref_for,
                acquisition_sink=acquisition_sink,
                clock=clock,
            ))

    def state(self) -> CampaignExecutionState:
        return derive_campaign_state(
            self.plan,
            self._repository.list_events(self.plan.plan_id),
            self._repository.list_run_records(self.plan.plan_id))

    def advance(self) -> RunnerStepResult:
        """Run until the campaign needs the operator or ends."""
        return self.runner.run_until_blocked()

    def confirm_position(
            self,
            position_id: str,
            *,
            method: str = 'operator_attest',
            actor: str = 'operator') -> None:
        spec = self.plan.position(position_id)
        if spec is None:
            raise ValueError(f'unknown position {position_id}')
        self.runner.confirm_position(PositionConfirmation(
            position_id=position_id,
            method=method,
            reported_position=spec.declared_position,
            at_utc=utc_now_iso(),
            actor=actor,
        ))

    def pause(self, reason: str = '') -> None:
        self.runner.pause(reason)

    def resume(self, reason: str = '') -> None:
        self.runner.resume(reason=reason)

    def cancel(self, reason: str = '') -> None:
        self.runner.cancel(reason)

    def report_routing_change(self, reason: str) -> None:
        self.runner.report_routing_change(reason)


# ---------------------------------------------------------------------------
# Cell rendering — honest per-cell native evidence labels
# ---------------------------------------------------------------------------

_OUTCOME_LABELS = {
    'completed': '完了',
    'failed': '失敗',
    'cancelled': 'キャンセル',
}

_ENTRY_STATE_LABELS = {
    'pending': '未実行',
    'awaiting_position': '位置確認待ち',
    'in_progress': '実行中',
    'interrupted': '中断',
    'blocked': 'ブロック',
    'completed': '完了',
    'failed': '失敗',
    'waived': '免除',
}


def cell_native_labels(
        *,
        runner_plan: MeasurementRunnerPlan,
        assignments: CampaignCellAssignments,
        state: CampaignExecutionState,
        run_records: Sequence,
) -> dict[int, str]:
    """Per-cell native-run label for the campaign table — never a verdict
    promotion. A SIMULATED-bound cell says so explicitly."""

    records_by_id = {r.run_record_id: r for r in run_records}
    labels: dict[int, str] = {}
    pending_review: set[int] = set()
    for hits in assignments.ambiguous.values():
        pending_review.update(hits)
    for cell in runner_plan.cells:
        entry_key = assignments.entry_for_cell(cell.cell_index)
        if entry_key is None:
            labels[cell.cell_index] = (
                '要レビュー' if cell.cell_index in pending_review else '—')
            continue
        entry_state = state.entries.get(entry_key)
        if entry_state is None:
            labels[cell.cell_index] = '—'
            continue
        record = None
        if entry_state.run_record_refs:
            record = records_by_id.get(entry_state.run_record_refs[-1])
        base = _ENTRY_STATE_LABELS.get(
            entry_state.state, entry_state.state)
        if record is not None:
            base = _OUTCOME_LABELS.get(record.outcome, record.outcome)
            if record.quality_verdict is not None:
                base += f'（品質 {record.quality_verdict}）'
            if record.backend_is_simulated:
                base += ' SIMULATED'
        labels[cell.cell_index] = base
    return labels


__all__ = [
    'RUNNER_PURPOSE_TO_ROLE',
    'CampaignCellAssignments',
    'NativeCampaignDrive',
    'NativeCampaignMaterialization',
    'NativePlanConversionError',
    'NativePreflightReport',
    'build_native_preflight',
    'cell_native_labels',
    'channel_entity_id_for',
    'materialize_native_campaign_plan',
    'resolve_cell_assignments',
]
