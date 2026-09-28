# Round 13 — Feature Discoverability & Navigation Completeness

Scope: can a real user find and reach every shipped capability? Feature inventory, reachability map, orphans, menu/keyboard parity, palette truth, empty states, workflow dead-ends. Verified in code and with offscreen Qt (`WorkflowApplicationComposition` instantiated; rail clicks, palette searches, and `registry.execute` exercised for real).

## Reachability map

The app has one shell (`WorkflowShellWindow`): a left **rail** (destinations), a **top context bar** (project chip + per-workspace context tabs + palette button), a **menubar** (プロジェクト only), and the **command palette** (Ctrl+K). Every surface rolls up to one of: a rail destination, a workspace context tab, a tool strip / context menu, a dialog launched from those, or the palette.

| Surface / feature | Reachable via |
|---|---|
| 4 workspaces (概要/部屋/測定/最適化) | Rail · palette `navigation.*` · htdt://workspace links · Alt+←/→ history |
| 5 app destinations (プロジェクト/取り込み/アクティビティ/ライブラリ/サポート) | Rail · **palette `navigation.*` (new this round)** |
| Room contexts (geometry/objects/placement/acoustics/history) | Context-bar tabs when 部屋 mounted · tool strip buttons · scene context menu (undo/redo + 9 command ids) |
| Measurement contexts (import/assignment/campaign/quality/comparison/calibration) | Context-bar tabs when 測定 mounted |
| Optimization contexts (setup/candidates/comparison/interventions/robustness/validation) | Context-bar tabs when 最適化 mounted |
| Project ops (new/open/rename/duplicate/export/import/deliverables/archive/restore) | プロジェクト menu · palette (`project.*`) |
| Settings (general/units/capture/display) | Rail 設定 button · palette `settings:*` |
| Help topics | Palette `help-topics:*` · **F1 shortcuts dialog (new)** |
| Palette itself | Ctrl+K · **検索・操作 top-bar button (new)** |
| Legacy windows | `--legacy-ui` flag (intentional; composition switch in `native_cad.py`) |

## Orphans

**Zero orphan modules**: every panel/dialog rolls up to the shell or a `--legacy-ui` window; legacy windows are intentionally reachable via the flag only.

Orphans *found and fixed* this round:

1. **5 app destinations unreachable by keyboard/search** — rail was the only route. Added `navigation.projects|inbox|activity|library|support` commands (deep-link backed, read-only, GLOBAL, JP+EN keywords); they appear in palette suggestions and search and navigate for real (verified: `execute()` switches `current_workspace_id`).
2. **Palette itself undiscoverable** — Ctrl+K was the only entry point. Added the 検索・操作 button to the top context bar (`paletteRequested` → `command_palette.open`).
3. **`settings.capture` dead palette entry** — surfaced unconditionally even when the capture receiver failed to init (entry was a dead link). Provider now filters it unless `capture_receiver` is live.
4. **No help affordance** — no F1, no menu Help. Added shell `F1` → `helpRequested` → `HelpDialog.shortcuts(registry)` (real handler `help.shortcuts`).

## Menu & keyboard parity

- Menubar is プロジェクト-only *by design*: work-area actions live in per-context tool strips + the room scene context menu; the palette is the primary action surface (documented IA, not a gap).
- `Ctrl+S`/`Ctrl+Z`/`Ctrl+Shift+Z` bind per-workspace where document-save makes sense (room, optimization); measurement persists through its controller so no save binding is needed there (verified `_WORKSPACE_COMMAND_IDS` unbind on switch).
- `Esc`: palette clears-then-closes internally; QDialogs close natively; `room.edit.cancel` binds `Esc` while 部屋 is mounted (the `Esc` WindowShortcut observed on the shell is that binding).
- Shortcuts-reference completeness: `HelpDialog.shortcuts` now prepends the shell-level QShortcuts (Ctrl+K, Alt+←/→, F1) which live outside the registry — previously the list omitted them entirely.

## Palette truth

- Every palette entry resolves: command results → registry (executors or deep links), settings/help/navigation items → real handlers. The one dead entry (`settings.capture` with no receiver) is now filtered.
- Suggested() before this round listed only the 4 workspaces + action links; now lists all 9 destinations (verified output).
- Search quality: JP labels hit on JP queries; EN keywords added for the 5 destinations (`inbox`/`library`/`support`/`projects`/`activity` all hit). Existing providers already alias scene entities JP+EN.

## Empty states & workflow dead-ends

- Measurement workspace already had designed empty states (「まだ読み込まれていません」「測定がありません」「…先にREWを読み込み」).
- **Fixed**: REW-import success notice dead-ended — added a `notice_action` button 「割り当てへ進む」 that jumps to the assignment context (both single-file and batch import notices).
- **Fixed**: Reference library (ライブラリ destination) rendered an empty table when no definitions exist — added an empty-state label pointing at 「機材ライブラリを管理…」.
- Deliverables dialog already carries per-deliverable 書き出し/開く next-step buttons; optimization setup leads with preset+preview + guidance text.

## Deferred / intentionally not changed

- Workspace *sections* (e.g. room/objects) are not palette-navigable — palette links target actions and top-level destinations; sections are context tabs inside a mounted workspace. Extending deep links per-section is possible but larger than this round's scope.
- No menubar entries for work-area actions (palette-first IA — see above).

## Files changed

- `workflow_shell.py`: palette button, `paletteRequested`/`helpRequested` signals, F1 shortcut.
- `workflow_application.py`: wire palette/help signals; filter `settings.capture` when no receiver.
- `command_registry.py`: 5 `navigation.*` app-destination commands.
- `workflow_help.py`: shell-level shortcuts prepended to the shortcuts dialog.
- `measurement_page_workspace.py`: notice action button + 「割り当てへ進む」 next-step.
- `application_pages.py`: library empty state.
- `docs/COMMAND_SYSTEM.md`: 5 new command rows.
- Tests: `test_command_registry.py` (nav commands + read-only set), `test_workflow_shell.py` (palette button + F1), `test_workflow_help.py` (shell shortcuts), `test_measurement_workflow_ux130.py` (notice action).
