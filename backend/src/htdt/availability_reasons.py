"""Stable availability reason codes (#776).

``CommandAvailability`` previously carried only a rendered Japanese
``disabled_reason`` sentence — the human text *was* the identity, so
contextual help, localization and diagnostics could not be wired without
matching prose. This module is the semantic bridge:

* :class:`AvailabilityReason` — stable machine-readable ``code`` plus
  ``params`` (parameter detail that never becomes identity) and an optional
  non-identity ``detail`` string;
* :data:`AVAILABILITY_REASONS` — the central catalog of command-owned codes
  with localized short messages and an optional help topic;
* :func:`availability_reason` — catalog-checked factory;
* :func:`localized_reason_message` — presentation rendering for a reason.

Contract:

* codes are language independent, dotted and never built from runtime prose;
* parameters render into localized text but do not change the code;
* unknown/unbound codes degrade safely — rendering falls back to detail or
  the code itself and never raises;
* ``command.*`` codes in the catalog are the ones :class:`HelpRegistry`
  integrity checks validate (catalog-owned vocabulary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .localization import PresentationLocale


@dataclass(frozen=True, slots=True)
class AvailabilityReason:
    """Stable, language-independent identity of a disabled command state."""

    code: str
    params: Mapping[str, object] = field(default_factory=dict)
    #: Optional diagnostic detail. Never identity: translations, logging and
    #: help resolution all key off ``code``/``params`` instead.
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AvailabilityReasonSpec:
    """Catalog entry: localized short text for one stable code."""

    code: str
    message_ja: str
    message_en: str
    help_topic_id: str | None = None

    def __post_init__(self) -> None:
        if not self.code or '.' not in self.code:
            raise ValueError('reason codes must be dotted: <domain>.<reason>')
        if not self.message_ja or not self.message_en:
            raise ValueError(
                f'reason code {self.code!r} requires JA and EN short text'
            )


# ---------------------------------------------------------------------------
# Central catalog of command-owned availability reason codes.
#
# Every code emitted by the CommandRegistry and the shipped availability
# providers lives here. Domain reason families owned elsewhere (prediction
# source prerequisites, readiness checks, ...) bind their own codes; CI
# verifies that catalog-owned ``command.*`` bindings in the help registry
# refer to codes declared below.
# ---------------------------------------------------------------------------

def _spec(
    code: str,
    message_ja: str,
    message_en: str,
    *,
    help_topic_id: str | None = None,
) -> AvailabilityReasonSpec:
    return AvailabilityReasonSpec(
        code=code,
        message_ja=message_ja,
        message_en=message_en,
        help_topic_id=help_topic_id,
    )


AVAILABILITY_REASONS: dict[str, AvailabilityReasonSpec] = {
    spec.code: spec
    for spec in (
        # --- CommandRegistry-owned (freeze/unbound/context) ---------------
        _spec(
            'command.blocked.data_mutation_frozen',
            'データ処理中はデータを変更できません',
            'Data cannot be changed while a data operation is running',
            help_topic_id='trouble.command_unavailable',
        ),
        _spec(
            'command.blocked.navigation_handler_unavailable',
            '画面切替の準備が完了すると利用できます',
            'Available once workspace navigation is ready',
            help_topic_id='trouble.command_unavailable',
        ),
        _spec(
            'command.blocked.unavailable_in_context',
            'この操作は現在の画面では利用できません',
            'This action is not available in the current workspace',
            help_topic_id='trouble.command_unavailable',
        ),
        # --- Edit state ---------------------------------------------------
        _spec(
            'command.blocked.editing_not_available',
            '編集できる状態ではありません',
            'Editing is not available right now',
        ),
        _spec(
            'command.blocked.edit_in_progress',
            '編集中の操作を確定またはキャンセルしてください',
            'Commit or cancel the in-progress edit first',
        ),
        _spec(
            'command.blocked.nothing_to_save',
            '保存できる変更がないか、編集中の操作があります',
            'No changes to save, or an edit is in progress',
        ),
        _spec(
            'command.blocked.nothing_to_undo',
            '元に戻せる操作はありません',
            'There is nothing to undo',
        ),
        _spec(
            'command.blocked.nothing_to_redo',
            'やり直せる操作はありません',
            'There is nothing to redo',
        ),
        _spec(
            'command.blocked.clipboard_empty',
            '先にコピーしてください',
            'Copy something first',
        ),
        _spec(
            'command.blocked.nothing_to_cancel',
            'キャンセルする操作はありません',
            'There is nothing to cancel',
        ),
        _spec(
            'command.blocked.nothing_to_commit',
            '確定する操作はありません',
            'There is nothing to commit',
        ),
        _spec(
            'command.blocked.isolation_inactive',
            '分離中ではありません',
            'No isolation is currently active',
        ),
        # --- Room / selection prerequisites --------------------------------
        _spec(
            'command.blocked.room_required',
            '先に部屋を作成してください',
            'Create a room first',
        ),
        _spec(
            'command.blocked.selection_required',
            '項目を選択してください',
            'Select an item first',
        ),
        _spec(
            'command.blocked.min_selection',
            '{required}つ以上の項目を選択してください',
            'Select at least {required} items',
        ),
        _spec(
            'command.blocked.speaker_required',
            'スピーカーを選択してください',
            'Select a speaker first',
        ),
        _spec(
            'command.blocked.underlay_required',
            '下図をインポートしてください',
            'Import an underlay first',
        ),
        _spec(
            'room.edit.requires_editable_selection',
            '編集できる項目を選択してください',
            'Select an editable item first',
        ),
        _spec(
            'room.view.requires_selection',
            '表示する項目を選択してください',
            'Select an item to display',
        ),
        _spec(
            'room.transform.requires_active_transform',
            '移動または回転を開始してから軸を指定してください',
            'Start a move or rotate before choosing an axis',
        ),
        _spec(
            'room.draw.blocked_while_editing',
            '部屋の編集中または復旧確認中は新しい作図を開始できません',
            'Cannot start a new sketch while the room is being edited or recovered',
        ),
        _spec(
            'room.add_speaker.requires_finished_room',
            '部屋を作成し、部屋・壁編集を完了してから追加してください',
            'Create a room and finish room/wall editing before adding speakers',
        ),
        # --- Save / undo-redo ------------------------------------------------
        _spec(
            'project.save.nothing_to_save',
            '保存する変更がありません',
            'There is nothing to save',
        ),
        _spec(
            'project.save.unavailable_or_editing',
            '保存できる変更がないか、編集中の操作があります',
            'No changes can be saved, or an edit is in progress',
        ),
        _spec(
            'project.save.unavailable_or_busy',
            '保存する変更がないか、候補生成・REW読込・編集操作が実行中です',
            'Nothing to save, or candidate generation/REW import/editing is running',
        ),
        _spec(
            'edit.undo.unavailable_or_busy',
            '元に戻せる操作がないか、処理が実行中です',
            'Nothing to undo, or an operation is running',
        ),
        _spec(
            'edit.redo.unavailable_or_busy',
            'やり直せる操作がないか、処理が実行中です',
            'Nothing to redo, or an operation is running',
        ),
        # --- Prediction ----------------------------------------------------
        _spec(
            'prediction.run.running',
            '予測を実行中です',
            'A prediction is already running',
            help_topic_id='trouble.prediction_unavailable',
        ),
        _spec(
            'prediction.run.requires_saved_layout',
            '予測の前に現在の配置を保存してください',
            'Save the current layout before running a prediction',
            help_topic_id='trouble.prediction_unavailable',
        ),
        _spec(
            'prediction.run.receiver_required',
            '受音点を選択してください',
            'Select a receiver point first',
            help_topic_id='trouble.prediction_unavailable',
        ),
        _spec(
            'prediction.run.requires_saved_scene_and_receiver',
            '保存済みSceneと受音点を用意してから予測を実行してください',
            'Provide a saved scene and a receiver point before running a prediction',
            help_topic_id='trouble.prediction_unavailable',
        ),
        _spec(
            'prediction.run.workspace_unbound',
            '予測ワークスペースが接続されていません',
            'The prediction workspace is not connected',
            help_topic_id='trouble.command_unavailable',
        ),
        # --- Measurement import ---------------------------------------------
        _spec(
            'measurement.import.requires_saved_scene',
            '部屋を保存してからREWを読み込んでください',
            'Save the room before importing REW data',
        ),
        _spec(
            'measurement.import.requires_saved_scene_and_point',
            '保存済みSceneと測定点を用意してからREWを読み込んでください',
            'Provide a saved scene and a measurement point before importing REW data',
        ),
        _spec(
            'measurement.import.workspace_unbound',
            '測定ワークスペースが接続されていません',
            'The measurement workspace is not connected',
            help_topic_id='trouble.command_unavailable',
        ),
        # --- Optimization ----------------------------------------------------
        _spec(
            'optimization.compare.requires_spec_selection',
            '比較する探索仕様を選択してください',
            'Select a search spec to compare',
        ),
        _spec(
            'optimization.compare.workspace_unbound',
            '候補比較ワークスペースが接続されていません',
            'The candidate-compare workspace is not connected',
            help_topic_id='trouble.command_unavailable',
        ),
    )
}


class _SafeParams(dict):
    """``str.format_map`` mapping that leaves unknown placeholders literal."""

    def __missing__(self, key: str) -> object:
        return '{' + key + '}'


def availability_reason(
    code: str,
    *,
    params: Mapping[str, object] | None = None,
    detail: str | None = None,
) -> AvailabilityReason:
    """Build a reason for a catalog-owned code (fails closed on typos)."""

    if code not in AVAILABILITY_REASONS:
        raise KeyError(f'unknown availability reason code: {code}')
    return AvailabilityReason(code=code, params=dict(params or ()), detail=detail)


def localized_reason_message(
    reason: AvailabilityReason,
    locale: PresentationLocale,
) -> str:
    """Render a reason's localized short text; never raises."""

    spec = AVAILABILITY_REASONS.get(reason.code)
    if spec is None:
        # Unknown/unbound code — presentation falls back instead of crashing.
        return reason.detail or reason.code
    template = (
        spec.message_ja
        if locale == PresentationLocale.JAPANESE
        else spec.message_en
    )
    return template.format_map(_SafeParams(reason.params))


def reason_help_topic_id(reason: AvailabilityReason) -> str | None:
    """Help 'Why?' target for a reason: the catalog binding, if one exists."""

    spec = AVAILABILITY_REASONS.get(reason.code)
    return None if spec is None else spec.help_topic_id


__all__ = [
    'AVAILABILITY_REASONS',
    'AvailabilityReason',
    'AvailabilityReasonSpec',
    'availability_reason',
    'localized_reason_message',
    'reason_help_topic_id',
]
