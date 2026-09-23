# UX120 CAD input / shortcut authority

Issue #118 / UX120 の CAD 操作基盤は、keyboard command と viewport pointer gesture を分離する。

## Canonical input map

| 操作 | 入力 | authority |
| --- | --- | --- |
| 画面移動 | 中ボタン drag | `cad_input.CadInputController` |
| 視点回転 | Shift + 中ボタン drag | `cad_input.CadInputController` |
| zoom | wheel | `cad_input.CadInputController` |
| context menu | 右クリック | `cad_input.CadInputController` |
| 移動 | M | `command_registry.default_command_definitions()` |
| 回転 | R | `command_registry.default_command_definitions()` |
| 選択範囲に合わせる | F | `command_registry.default_command_definitions()` |
| 全体表示 | Home | `command_registry.default_command_definitions()` |
| 取消 / 確定 | Esc / Enter | `command_registry.default_command_definitions()` |
| 元に戻す / やり直す | Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z | existing command registry |
| 保存 | Ctrl+S | existing command registry |
| 複製 | Ctrl+D | `command_registry.default_command_definitions()` |
| 軸拘束 | X / Y / Z | `command_registry.default_command_definitions()` |

keyboard shortcut文字列を Room widget や toolbar に再定義しない。menu / tooltip も
`CommandDefinition.display_name` と `shortcut` を表示元として使う。

## Agent A integration interface

Room workspace は concrete renderer に合わせて `CadViewportInputPort` を実装する。

- `begin_pan / pan_by / end_pan`
- `begin_orbit / orbit_by / end_orbit`
- `zoom_by`
- `open_context_menu`

camera操作の尺度、pivot、projection、selection-aware context menu は Room workspace /
renderer adapter 側の責務である。`CadInputController` は Qt input を canonical gesture に
変換するだけで、Scene / WorkingDocument / repository を変更しない。

edit command は `CadCommandBindings` で既存の workspace/domain callback を接続する。

```python
self.cad_input = CadInputController(
    shortcut_parent=self,
    viewport=self.viewport_widget,
    registry=command_registry,
    viewport_port=self.viewport_input_adapter,
    command_bindings=CadCommandBindings(
        move=self.start_move,
        rotate=self.start_rotate,
        fit_selection=self.fit_selection,
        fit_all=self.fit_all,
        cancel=self.cancel_current_edit,
        commit=self.commit_current_edit,
        duplicate=self.duplicate_selection,
        constrain_axis=self.constrain_axis,
        availability={
            "room.transform.move": self.move_availability,
            "room.transform.rotate": self.rotate_availability,
            "room.view.fit_selection": self.fit_selection_availability,
            "room.edit.duplicate": self.duplicate_availability,
            "room.transform.axis_x": self.axis_availability,
            "room.transform.axis_y": self.axis_availability,
            "room.transform.axis_z": self.axis_availability,
        },
    ),
)
```

`project.save` / `edit.undo` / `edit.redo` の callback authority は既存 application /
WorkingDocument composition に残す。`CadInputController` はそれらを再実装せず、Room
workspace内でshortcutを受け取れるようにするだけである。

workspaceを破棄・差し替える場合は、古い callback を registry に残さないよう
`CadInputController.dispose()` を先に呼ぶ。

## Focus policy

scene command はすべて `ShortcutBehavior.FOCUS_SAFE` とする。

`CommandShortcutBinder` は `QLineEdit`, `QAbstractSpinBox`, `QTextEdit`,
`QPlainTextEdit`, editable `QComboBox` に focus がある間、scene shortcutを無効化する。
そのため search、数値入力、Inspector text editing 中の M/R/F/X/Y/Z/Esc/Enter/Ctrl+D や
Ctrl+Z/Ctrl+Y は sceneへ誤発火しない。

`Ctrl+S` は既存の global document command として維持する。

QShortcutはfocusと現在のcommand availabilityの両方でgateする。selection、transform、
preview等の状態をprogrammaticに変更した後は Room workspace から
`CadInputController.refresh_shortcuts()` を呼び、shortcut enablementを即時同期する。
実行時にも `CommandRegistry.execute()` がavailabilityを再評価するため、input layerが
domain stateの第二authorityにはならない。

## Axis constraint lifecycle

X / Y / Z は transform開始そのものではない。Room workspace は transform中だけ
`room.transform.axis_x/y/z` をavailableにし、それ以外では短い disabled reason を返す。

constraintの解釈（world/local axis、gizmo state、snapとの組み合わせ）は Room edit authority
の責務であり、input controllerに保存しない。

## Adding a shortcut

1. `command_registry.default_command_definitions()` に command ID、表示名、context、
   shortcut、keywordsを一度だけ追加する。
2. scene command は原則 `ShortcutBehavior.FOCUS_SAFE` にする。
3. Room固有commandなら `CAD_SCENE_COMMAND_IDS` / `CadCommandBindings` に接続点を追加する。
4. widget側で別の `QAction.setShortcut()` や `keyPressEvent()` を追加しない。
5. availabilityは対象workspace/domain stateから導出し、input layerへ新しいtruthを作らない。
6. command palette、toolbar、context menuは同じcommand IDを参照する。

## Authority boundary

- **CommandRegistry**: command ID / 日本語表示名 / keyboard shortcut / context / availability
  metadata の正本。
- **CadInputController**: Qt pointer gesture変換とRoom-scoped shortcut binding。
- **Room workspace / renderer adapter**: camera movement、selection、context menu、fit。
- **WorkingDocument / Scene / repository**: edit、undo/redo、save、revision authority。
- **Gizmo / snap / transform service**: move/rotate/axis constraintのdomain/edit semantics。

このsliceでは `native_cad.py` と `workflow_shell.py` を変更しない。Room UI本体も実装しない。
