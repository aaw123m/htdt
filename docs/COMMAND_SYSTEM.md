# Command Registry / Command Palette

> Issue #118 / UX110 command system foundation.  
> 正本はこの文書と `backend/src/htdt/command_registry.py` のcommand metadata。

## 目的

HTDTの操作入口をwidgetごとの文字列・shortcut・callback定義から分離し、`Ctrl+K` の単一検索入口からnavigation commandとscene/task commandを扱う。

command layerが保持するのは次だけ。

- stable command ID
- 日本語表示名
- shortcut metadata
- context
- availability / disabled reason
- workspace deep-link metadata
- 既存authorityを呼ぶcallback

Scene保存、Undo/Redo、測定、予測、Pareto計算などのdomain authorityはcommand layerへ移さない。

## 実装

- `backend/src/htdt/workflow_navigation.py`
  - shell / command / Overview共有の `WorkspaceId` / `WorkspaceDeepLink`
- `backend/src/htdt/command_registry.py`
  - Qt非依存のcentral registry
  - command metadata / search / availability / execution
  - text input focus時のshortcut policy
- `backend/src/htdt/command_palette.py`
  - `Ctrl+K` palette
  - disabled reason表示
  - `CommandShortcutBinder`
- `backend/src/htdt/native_command_adapter.py`
  - 現行native windowの既存method/actionへdelegateするadapter
  - save / undo / redo / room draw / speaker add / REW import / prediction / candidate compare

## 初期command

| command ID | 表示 | shortcut | context | deep-link |
|---|---|---|---|---|
| `navigation.overview` | 概要 | — | global / overview | `overview` |
| `navigation.room` | 部屋 | — | global / room | `room` |
| `navigation.measurements` | 測定 | — | global / measurement | `measurement` |
| `navigation.optimization` | 最適化 | — | global / optimization | `optimization` |
| `project.save` | 保存 | Ctrl+S | global | — |
| `edit.undo` | 元に戻す | Ctrl+Z | global / room | — |
| `edit.redo` | やり直す | Ctrl+Y / Ctrl+Shift+Z | global / room | — |
| `room.draw` | 部屋作図 | — | room | `room/geometry` |
| `room.add_speaker` | スピーカー追加 | — | room | `room/placement` |
| `measurements.import_rew` | REW読み込み | — | measurement | `measurement/import` |
| `prediction.run` | 予測実行 | — | room | `room/acoustics` |
| `optimization.compare_candidates` | 候補比較 | — | optimization | `optimization/candidates` |

## Shell integration

canonical shell ID/contextは `workflow_navigation.py` を唯一の正本とする。

- workspace: `overview / room / measurement / optimization`
- Room context: `geometry / objects / placement / acoustics`
- Measurement context: `import / assignment / quality / comparison`
- Optimization context: `setup / candidates / objectives / measurement-plan / validation`

`native_cad.py` のcomposition rootでpaletteをshellのchildとして1回だけ生成し、
`WorkflowShellWindow.handle_deep_link()` をregistryへ接続する。

~~~python
registry = CommandRegistry()
register_default_commands(registry)
registry.set_deep_link_handler(shell.handle_deep_link)

controller = CommandPaletteController(
    shell,
    registry,
    context_provider=lambda: CommandContext(shell.current_workspace_id.value),
)
~~~

`register_default_commands()` はexecutor未接続でも全metadataを先に登録できる。
lazy workspaceがmountされた時点で、workspace側の既存authorityを `bind()` する。

~~~python
room = RoomEditorWindow(repository, document_id)
registry.bind(
    'room.draw',
    execute=room.start_room_sketch,
    availability=lambda: command_availability_from(room.draw_room_action),
)
~~~

deep-link付きtask commandを実行すると、registryはまずshell navigationを行う。
navigation中のlazy mountで `bind()` されたexecutorがあれば、その後に同じcommandを実行する。
したがってshellがdomain/service authorityを持つ必要はない。

`project.save` / `edit.undo` / `edit.redo` のようなdeep-linkを持たないcommandは、
active workspace変更時に対応する既存action/methodへbindする。未bind時は
「この操作は現在の画面では利用できません」と理由付きdisabledになる。

deep-link handler自体が未接続の場合、navigation commandはpaletteに残るがdisabledになり、
「画面切替の準備が完了すると利用できます」と表示する。

## command追加方法

1. `default_command_definitions()` にstable ID、日本語表示名、context、keywords、必要ならshortcut/deep-linkを追加する。
2. metadataを先に登録し、実行処理が存在するworkspaceのmount/activate時に `registry.bind()` で既存authorityへ接続する。legacy単一windowでは `native_command_adapter.py` の `bindings` を利用できる。
3. availabilityは既存actionの `isEnabled()`、既存precondition method、repositoryのread-only state等を参照する。domain判定をcommand側へ再実装しない。
4. disabled時は短い日本語理由を返す。
5. scene/document shortcutを追加する場合は `ShortcutBehavior.FOCUS_SAFE` を使う。
6. shell navigationだけのcommandはexecutorを作らず `WorkspaceDeepLink` を指定する。task commandもdeep-linkを持たせると、shell navigation後のlazy bindingを利用できる。
7. registry contractを変える場合だけ `backend/tests/test_command_registry.py` を更新する。単なるmetadata追加のために不要なGUI testを増やさない。

例:

~~~python
CommandDefinition(
    command_id='room.add_seat',
    display_name='座席追加',
    contexts=frozenset({CommandContext.ROOM}),
    keywords=('座席を追加', 'add seat'),
    deep_link=WorkspaceDeepLink(WorkspaceId.ROOM, 'objects'),
)
~~~

binding:

~~~python
registry.bind(
    'room.add_seat',
    execute=lambda: room_workspace.add_object('seat'),
    availability=lambda: existing_availability_check(room_workspace),
)
~~~

## shortcut / focus contract

text、numeric、search fieldにfocusがある間、scene/document editing shortcutを奪わない。

- `ShortcutBehavior.GLOBAL`
  - text入力中も有効にしてよいもの。例: `Ctrl+K`、保存。
- `ShortcutBehavior.FOCUS_SAFE`
  - text/numeric/search入力中は無効化するもの。例: Undo/Redo、将来の `M` / `R` 等scene shortcut。

`CommandShortcutBinder` は `QLineEdit`、`QAbstractSpinBox`、`QTextEdit`、
`QPlainTextEdit`、editable `QComboBox` をtext inputとして扱い、focus change時にshortcut自体をdisableする。
trigger後に無視する方式ではないため、入力widget側の通常shortcutを先に奪わない。

現行legacy toolbarのshortcut全面移行はこのPRのscope外。Agent D等のCAD shortcut実装時は、
新しいshortcutをregistry metadataへ集約し、widgetごとの重複定義を増やさない。

## authority boundary

command adapterは以下を直接実装しない。

- SceneRevision保存 semantics
- Undo/Redo stack
- room geometry validation
- measurement revision binding / raw asset保存
- prediction job guard / stale判定
- SearchSpec / objective / Pareto authority

それぞれ現行window/service/repository methodへdelegateする。
command availabilityはUIの入口を説明するためのread-only判定であり、
最終的なdomain validationを置き換えない。

## 現在の残件

- PR #127でpalette controller / deep-link handler / lazy workspace bindingをshellへ統合済み。旧PR #122はsuperseded。
- entity search、設定、ヘルプはIssue #118の後続scope。registry APIを拡張せず追加可能。
- legacy toolbar/actionのshortcutを一括削除・置換する作業はこのPRでは行わない。新shell側はregistry shortcut metadataを使用する。
- visual token適用はAgent Bのtheme foundationへ委譲する。paletteはQt palette roleだけを使用し、独自色を持たない。
