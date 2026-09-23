from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
import unicodedata



from .workflow_navigation import WorkspaceDeepLink, WorkspaceId

class CommandContext(StrEnum):
    GLOBAL = 'global'
    OVERVIEW = 'overview'
    ROOM = 'room'
    MEASUREMENT = 'measurement'
    OPTIMIZATION = 'optimization'


class ShortcutBehavior(StrEnum):
    GLOBAL = 'global'
    FOCUS_SAFE = 'focus_safe'


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    command_id: str
    display_name: str
    contexts: frozenset[CommandContext] = field(
        default_factory=lambda: frozenset({CommandContext.GLOBAL})
    )
    shortcut: str | None = None
    shortcut_aliases: tuple[str, ...] = ()
    shortcut_behavior: ShortcutBehavior = ShortcutBehavior.FOCUS_SAFE
    keywords: tuple[str, ...] = ()
    deep_link: WorkspaceDeepLink | None = None
    # Fail closed: commands are treated as mutating managed data unless they are
    # explicitly declared read-only (navigation, camera fit). The data-management
    # freeze gate blocks every mutating command regardless of local availability.
    mutates_managed_data: bool = True

    def __post_init__(self) -> None:
        if not self.command_id or self.command_id.strip() != self.command_id:
            raise ValueError('command_id must be non-empty and trimmed')
        if not self.display_name.strip():
            raise ValueError('display_name must be non-empty')
        if not self.contexts:
            raise ValueError('contexts must not be empty')


@dataclass(frozen=True, slots=True)
class CommandAvailability:
    enabled: bool
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        if self.enabled and self.disabled_reason:
            raise ValueError('enabled command cannot have disabled_reason')
        if not self.enabled and not self.disabled_reason:
            raise ValueError('disabled command requires disabled_reason')

    @classmethod
    def available(cls) -> 'CommandAvailability':
        return cls(enabled=True)

    @classmethod
    def unavailable(cls, reason: str) -> 'CommandAvailability':
        return cls(enabled=False, disabled_reason=reason)


@dataclass(frozen=True, slots=True)
class CommandSearchResult:
    definition: CommandDefinition
    availability: CommandAvailability
    score: int


AvailabilityProvider = Callable[[], CommandAvailability]
CommandExecutor = Callable[[], None]
DeepLinkHandler = Callable[[WorkspaceDeepLink], bool | None]


@dataclass(slots=True)
class _RegisteredCommand:
    definition: CommandDefinition
    execute: CommandExecutor | None
    availability: AvailabilityProvider | None
    order: int


def _normalized(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold().strip()


def command_shortcut_allowed(
    definition: CommandDefinition,
    *,
    text_input_focused: bool,
) -> bool:
    if not text_input_focused:
        return True
    return definition.shortcut_behavior == ShortcutBehavior.GLOBAL


DATA_MUTATIONS_FROZEN_REASON = 'データ処理中はデータを変更できません'


class CommandRegistry:
    def __init__(self, *, deep_link_handler: DeepLinkHandler | None = None) -> None:
        self._commands: dict[str, _RegisteredCommand] = {}
        self._deep_link_handler = deep_link_handler
        self._data_mutations_frozen = False

    @property
    def data_mutations_frozen(self) -> bool:
        return self._data_mutations_frozen

    def freeze_data_mutations(self) -> None:
        """Fail closed for mutation-class commands until thaw_data_mutations().

        Workspace-local availability providers keep their own semantics; while a
        data-management operation (backup/restore) is in progress the freeze gate
        wins so shortcuts, palette entries and menus cannot reach executors.
        """
        self._data_mutations_frozen = True

    def thaw_data_mutations(self) -> None:
        self._data_mutations_frozen = False

    def set_deep_link_handler(self, handler: DeepLinkHandler | None) -> None:
        self._deep_link_handler = handler

    def register(
        self,
        definition: CommandDefinition,
        *,
        execute: CommandExecutor | None = None,
        availability: AvailabilityProvider | None = None,
    ) -> None:
        if definition.command_id in self._commands:
            raise ValueError(f'duplicate command id: {definition.command_id}')
        self._commands[definition.command_id] = _RegisteredCommand(
            definition=definition,
            execute=execute,
            availability=availability,
            order=len(self._commands),
        )

    def bind(
        self,
        command_id: str,
        *,
        execute: CommandExecutor,
        availability: AvailabilityProvider | None = None,
    ) -> None:
        command = self._commands[command_id]
        command.execute = execute
        command.availability = availability

    def unbind(self, command_id: str) -> None:
        command = self._commands[command_id]
        command.execute = None
        command.availability = None

    def definition(self, command_id: str) -> CommandDefinition:
        return self._commands[command_id].definition

    def definitions(self) -> tuple[CommandDefinition, ...]:
        return tuple(item.definition for item in self._commands.values())

    def availability(self, command_id: str) -> CommandAvailability:
        command = self._commands[command_id]
        if self._data_mutations_frozen and command.definition.mutates_managed_data:
            return CommandAvailability.unavailable(DATA_MUTATIONS_FROZEN_REASON)
        if command.availability is not None:
            result = command.availability()
            if not result.enabled:
                return result
        if command.execute is None:
            if command.definition.deep_link is not None:
                if self._deep_link_handler is not None:
                    return CommandAvailability.available()
                return CommandAvailability.unavailable(
                    '画面切替の準備が完了すると利用できます'
                )
            return CommandAvailability.unavailable(
                'この操作は現在の画面では利用できません'
            )
        return CommandAvailability.available()

    def execute(self, command_id: str) -> bool:
        command = self._commands[command_id]
        if not self.availability(command_id).enabled:
            return False

        deep_link = command.definition.deep_link
        navigated = False
        if deep_link is not None and self._deep_link_handler is not None:
            navigation_result = self._deep_link_handler(deep_link)
            if navigation_result is False:
                return False
            navigated = True
            command = self._commands[command_id]
            if not self.availability(command_id).enabled:
                return False

        if command.execute is not None:
            command.execute()
            return True
        return navigated

    def search(
        self,
        query: str = '',
        *,
        context: CommandContext | None = None,
    ) -> tuple[CommandSearchResult, ...]:
        normalized_query = _normalized(query)
        tokens = tuple(token for token in normalized_query.split() if token)
        matches: list[CommandSearchResult] = []
        for command in self._commands.values():
            definition = command.definition
            score = self._score(definition, normalized_query, tokens, context)
            if score is None:
                continue
            matches.append(
                CommandSearchResult(
                    definition=definition,
                    availability=self.availability(definition.command_id),
                    score=score,
                )
            )
        matches.sort(
            key=lambda item: (
                not item.availability.enabled,
                -item.score,
                self._commands[item.definition.command_id].order,
            )
        )
        return tuple(matches)

    @staticmethod
    def _score(
        definition: CommandDefinition,
        query: str,
        tokens: tuple[str, ...],
        context: CommandContext | None,
    ) -> int | None:
        haystacks = (
            _normalized(definition.display_name),
            _normalized(definition.command_id),
            *(_normalized(keyword) for keyword in definition.keywords),
            *(_normalized(shortcut) for shortcut in (
                (definition.shortcut,) + definition.shortcut_aliases
                if definition.shortcut is not None
                else definition.shortcut_aliases
            )),
        )
        if tokens and not all(any(token in value for value in haystacks) for token in tokens):
            return None

        score = 10
        if context is not None and context in definition.contexts:
            score += 20
        if not query:
            return score

        display = haystacks[0]
        command_id = haystacks[1]
        keyword_values = haystacks[2:2 + len(definition.keywords)]
        if query == display:
            score += 100
        elif display.startswith(query):
            score += 85
        elif query in display:
            score += 70

        if query == command_id:
            score += 65
        elif command_id.startswith(query):
            score += 50
        elif query in command_id:
            score += 35

        for keyword in keyword_values:
            if query == keyword:
                score += 55
            elif keyword.startswith(query):
                score += 40
            elif query in keyword:
                score += 25
        return score


def default_command_definitions() -> tuple[CommandDefinition, ...]:
    return (
        CommandDefinition(
            command_id='navigation.overview',
            display_name='概要',
            contexts=frozenset({CommandContext.GLOBAL, CommandContext.OVERVIEW}),
            keywords=('overview', 'ホーム', 'ダッシュボード'),
            deep_link=WorkspaceDeepLink(WorkspaceId.OVERVIEW),
            shortcut_behavior=ShortcutBehavior.GLOBAL,
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='navigation.room',
            display_name='部屋',
            contexts=frozenset({CommandContext.GLOBAL, CommandContext.ROOM}),
            keywords=('room', '3D', 'CAD'),
            deep_link=WorkspaceDeepLink(WorkspaceId.ROOM),
            shortcut_behavior=ShortcutBehavior.GLOBAL,
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='navigation.measurements',
            display_name='測定',
            contexts=frozenset({CommandContext.GLOBAL, CommandContext.MEASUREMENT}),
            keywords=('measurement', 'REW', '実測'),
            deep_link=WorkspaceDeepLink(WorkspaceId.MEASUREMENT),
            shortcut_behavior=ShortcutBehavior.GLOBAL,
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='navigation.optimization',
            display_name='最適化',
            contexts=frozenset({CommandContext.GLOBAL, CommandContext.OPTIMIZATION}),
            keywords=('optimize', 'optimization', '候補'),
            deep_link=WorkspaceDeepLink(WorkspaceId.OPTIMIZATION),
            shortcut_behavior=ShortcutBehavior.GLOBAL,
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='project.save',
            display_name='保存',
            contexts=frozenset({CommandContext.GLOBAL}),
            shortcut='Ctrl+S',
            shortcut_behavior=ShortcutBehavior.GLOBAL,
            keywords=('save', '保存する'),
        ),
        CommandDefinition(
            command_id='edit.undo',
            display_name='元に戻す',
            contexts=frozenset({CommandContext.GLOBAL, CommandContext.ROOM}),
            shortcut='Ctrl+Z',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('undo',),
        ),
        CommandDefinition(
            command_id='edit.redo',
            display_name='やり直す',
            contexts=frozenset({CommandContext.GLOBAL, CommandContext.ROOM}),
            shortcut='Ctrl+Y',
            shortcut_aliases=('Ctrl+Shift+Z',),
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('redo',),
        ),
        CommandDefinition(
            command_id='room.transform.move',
            display_name='移動',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='M',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('move', '移動モード', 'transform'),
        ),
        CommandDefinition(
            command_id='room.transform.rotate',
            display_name='回転',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='R',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('rotate', '回転モード', 'transform'),
        ),
        CommandDefinition(
            command_id='room.view.fit_selection',
            display_name='選択範囲に合わせる',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='F',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('fit selection', 'frame selection', '選択へ移動'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.fit_all',
            display_name='全体表示',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='Home',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('fit all', 'frame all', '全体に合わせる'),
            mutates_managed_data=False,
        ),
        # --- Standard orthographic editing views (#545) ---------------------
        CommandDefinition(
            command_id='room.view.perspective',
            display_name='透視ビュー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('perspective', '透視', '3D'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.top',
            display_name='上面ビュー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('top', '上面', '平面', 'plan'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.front',
            display_name='正面ビュー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('front', '正面', 'elevation'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.rear',
            display_name='背面ビュー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('rear', '背面', 'back'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.left',
            display_name='左側面ビュー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('left', '左側面', 'side'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.right',
            display_name='右側面ビュー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('right', '右側面', 'side'),
            mutates_managed_data=False,
        ),
        # --- Saved views & viewport workspaces (#629) ------------------------
        CommandDefinition(
            command_id='room.view.isolate_selection',
            display_name='選択のみ表示',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('isolate', '分離', '選択のみ'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.isolate_kind',
            display_name='同じ種類のみ表示',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('isolate kind', '種類のみ', 'category'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.isolate_clear',
            display_name='分離解除',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('unisolate', '分離解除', 'show all'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.section_toggle',
            display_name='水平断面を切替',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('section', '断面', 'clip'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.save_named',
            display_name='ビューを保存…',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('save view', 'ビューを保存', 'named view'),
        ),
        # --- Floor-plan underlay (#534) ---------------------------------------
        CommandDefinition(
            command_id='room.underlay.import',
            display_name='下図をインポート…',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('underlay', '下図', 'floor plan', '図面', 'import'),
        ),
        CommandDefinition(
            command_id='room.underlay.calibrate',
            display_name='下図をスケール合わせ…',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('calibrate', 'スケール', 'scale', '下図'),
        ),
        # --- Room CAD layout tools (#613) -------------------------------------
        CommandDefinition(
            command_id='room.layout.copy',
            display_name='コピー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('copy', 'コピー', 'clipboard'),
        ),
        CommandDefinition(
            command_id='room.layout.paste',
            display_name='貼り付け',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('paste', '貼り付け', 'clipboard'),
        ),
        CommandDefinition(
            command_id='room.layout.mirror_x',
            display_name='左右ミラー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('mirror', 'ミラー', '左右', 'flip'),
        ),
        CommandDefinition(
            command_id='room.layout.mirror_y',
            display_name='前後ミラー',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('mirror', 'ミラー', '前後', 'flip'),
        ),
        CommandDefinition(
            command_id='room.layout.pair_speaker',
            display_name='スピーカーをペア複製',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('pair', 'ペア', 'FL', 'FR', 'スピーカー'),
        ),
        CommandDefinition(
            command_id='room.layout.align_min_x',
            display_name='左端を揃える',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('align', '揃える', 'left'),
        ),
        CommandDefinition(
            command_id='room.layout.align_max_x',
            display_name='右端を揃える',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('align', '揃える', 'right'),
        ),
        CommandDefinition(
            command_id='room.layout.align_min_y',
            display_name='前端を揃える',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('align', '揃える', 'front'),
        ),
        CommandDefinition(
            command_id='room.layout.align_max_y',
            display_name='後端を揃える',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('align', '揃える', 'rear'),
        ),
        CommandDefinition(
            command_id='room.layout.align_center_x',
            display_name='左右中央を揃える',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('align', '揃える', 'center', '中央'),
        ),
        CommandDefinition(
            command_id='room.layout.align_center_y',
            display_name='前後中央を揃える',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('align', '揃える', 'center', '中央'),
        ),
        CommandDefinition(
            command_id='room.layout.distribute_x',
            display_name='左右に等間隔で配置',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('distribute', '等間隔', 'horizontal'),
        ),
        CommandDefinition(
            command_id='room.layout.distribute_y',
            display_name='前後に等間隔で配置',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('distribute', '等間隔', 'depth'),
        ),
        CommandDefinition(
            command_id='room.layout.seat_row',
            display_name='座席ロウを生成…',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('seat row', '座席', 'row', 'array'),
        ),
        # --- Seating layout authoring (#546) ----------------------------------
        CommandDefinition(
            command_id='room.seating.layout',
            display_name='座席レイアウト…',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('seating', '座席レイアウト', 'rows', 'aisle', 'riser'),
        ),
        # --- Dimensional constraints & guides (#618) --------------------------
        CommandDefinition(
            command_id='room.constraint.centerline_x',
            display_name='左右中心線に拘束',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('centerline', '中心線', 'constraint'),
        ),
        CommandDefinition(
            command_id='room.constraint.centerline_y',
            display_name='前後中心線に拘束',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('centerline', '中心線', 'constraint'),
        ),
        CommandDefinition(
            command_id='room.constraint.symmetric',
            display_name='対称ペアを拘束',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('symmetric', '対称', 'constraint', 'mirror'),
        ),
        CommandDefinition(
            command_id='room.constraint.equal_spacing',
            display_name='等間隔を維持',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('equal spacing', '等間隔', 'constraint', 'distribute'),
        ),
        CommandDefinition(
            command_id='room.constraint.fixed_distance',
            display_name='距離を固定',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('fixed distance', '距離固定', 'constraint', 'dimension'),
        ),
        CommandDefinition(
            command_id='room.constraint.remove',
            display_name='選択の拘束を解除',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('remove constraint', '拘束解除', 'unlock'),
        ),
        CommandDefinition(
            command_id='room.constraint.guides_toggle',
            display_name='ガイド表示切替',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('guides', 'ガイド', 'construction'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.edit.cancel',
            display_name='操作を取り消す',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='Esc',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('cancel', 'escape', 'キャンセル'),
        ),
        CommandDefinition(
            command_id='room.edit.commit',
            display_name='操作を確定',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='Enter',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('commit', 'confirm', '確定'),
        ),
        CommandDefinition(
            command_id='room.edit.duplicate',
            display_name='複製',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='Ctrl+D',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('duplicate', 'copy', '複製する'),
        ),
        CommandDefinition(
            command_id='room.transform.axis_x',
            display_name='X軸に拘束',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='X',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('axis x', 'x constraint', '軸拘束'),
        ),
        CommandDefinition(
            command_id='room.transform.axis_y',
            display_name='Y軸に拘束',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='Y',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('axis y', 'y constraint', '軸拘束'),
        ),
        CommandDefinition(
            command_id='room.transform.axis_z',
            display_name='Z軸に拘束',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='Z',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('axis z', 'z constraint', '軸拘束'),
        ),
        CommandDefinition(
            command_id='room.edit.delete',
            display_name='選択項目を削除',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='Delete',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('delete', 'remove', '削除'),
        ),
        CommandDefinition(
            command_id='room.edit.toggle_hide',
            display_name='表示/非表示を切替',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='H',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('hide', 'show', '非表示', '隠す'),
        ),
        CommandDefinition(
            command_id='room.edit.toggle_lock',
            display_name='ロックを切替',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='L',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('lock', 'unlock', 'ロック'),
        ),
        CommandDefinition(
            command_id='room.measure',
            display_name='計測ツール',
            contexts=frozenset({CommandContext.ROOM}),
            shortcut='T',
            shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
            keywords=('measure', 'ruler', '距離', '角度', '計測'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.view.history',
            display_name='履歴',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('history', 'revision', '履歴', 'リビジョン'),
            deep_link=WorkspaceDeepLink(WorkspaceId.ROOM, 'history'),
            mutates_managed_data=False,
        ),
        CommandDefinition(
            command_id='room.draw',
            display_name='部屋作図',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('部屋を作図', 'draw room', 'room geometry', '形状'),
            deep_link=WorkspaceDeepLink(WorkspaceId.ROOM, 'geometry'),
        ),
        CommandDefinition(
            command_id='room.add_speaker',
            display_name='スピーカー追加',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('スピーカーを追加', 'add speaker', 'speaker'),
            deep_link=WorkspaceDeepLink(WorkspaceId.ROOM, 'placement'),
        ),
        CommandDefinition(
            command_id='measurements.import_rew',
            display_name='REW読み込み',
            contexts=frozenset({CommandContext.MEASUREMENT}),
            keywords=('REWを読み込む', 'import REW', '測定取込', '測定読み込み'),
            deep_link=WorkspaceDeepLink(WorkspaceId.MEASUREMENT, 'import'),
        ),
        CommandDefinition(
            command_id='prediction.run',
            display_name='予測実行',
            contexts=frozenset({CommandContext.ROOM}),
            keywords=('予測を実行', 'run prediction', 'acoustics', '音響'),
            deep_link=WorkspaceDeepLink(WorkspaceId.ROOM, 'acoustics'),
        ),
        CommandDefinition(
            command_id='optimization.compare_candidates',
            display_name='候補比較',
            contexts=frozenset({CommandContext.OPTIMIZATION}),
            keywords=('候補を比較', 'candidate compare', 'Pareto', 'パレート'),
            deep_link=WorkspaceDeepLink(WorkspaceId.OPTIMIZATION, 'comparison'),
        ),
    )


def register_default_commands(
    registry: CommandRegistry,
    *,
    bindings: dict[
        str,
        tuple[CommandExecutor | None, AvailabilityProvider | None],
    ] | None = None,
) -> None:
    bindings = bindings or {}
    for definition in default_command_definitions():
        execute, availability = bindings.get(definition.command_id, (None, None))
        registry.register(
            definition,
            execute=execute,
            availability=availability,
        )
