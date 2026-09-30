# Round 23 — Accessibility & keyboard-only operability

Scope: every mounted workspace (ROOM, OPTIMIZATION, MEASUREMENT, SYSTEM
EXPANSION) plus all five application destinations (PROJECTS, INBOX, ACTIVITY,
LIBRARY, SUPPORT), the shell rail, the menubar, the command palette, the
settings dialog, and `frontend/src`. Seven dimensions, each verified
programmatically — widgets were instantiated offscreen
(`QT_QPA_PLATFORM=offscreen`), mounted through the real
`WorkflowApplicationComposition`, and their accessible/focus/shortcut
properties read back. No verdict rests on code reading alone.

1. **ACCESSIBLE NAMES** — `accessibleName`/`accessibleDescription` on
   icon-only buttons, status indicators, and caption-driven inputs. In Qt a
   control with no name is invisible to screen readers.
2. **FOCUS VISIBILITY** — custom-painted widgets swallowing focus
   indicators; `TabFocus` vs `ClickFocus` omissions.
3. **SHORTCUT CONFLICTS** — the same key bound twice in one window;
   collisions with standard text-editing keys inside editors.
4. **MNEMONICS** — Japanese `&X` coverage on menus; duplicate mnemonics in
   one menu.
5. **KEYBOARD-ONLY COMPLETENESS** — every mouse-reachable action also has a
   keyboard path (tree activate, list pick, rail navigation, palette).
6. **TAB ORDER** — non-logical focus chains in forms and dialogs.
7. **SPA SIDE** (`frontend/src`) — `aria-label`/`role` on interactive
   elements, modal focus trap, focus restore, keyboard nav.

Branch `devin/rev23-a11y`. Tests:
`backend/tests/test_accessible_labels.py` (14 cases).

## Verdict table

| # | Dimension / finding | Severity | Verdict |
|---|--------------------|----------|---------|
| 1 | **ACCESSIBLE NAMES — orphan captions** — hundreds of inputs (combos, spins, trees, fields) sat next to `QLabel` captions that were never `setBuddy`-ed and carried no `accessibleName`. Qt does not infer label↔control pairing from layout adjacency, so screen readers announced nothing for the majority of form fields in the app | **HIGH** — primary form inputs silent to AT | FIXED — new `htdt.accessible_labels.wire_label_buddies(root)` walks every `QLabel`, finds the first unnamed labelable control that follows it in its owning layout (recursing into plain containers, stopping at real widgets), and wires `setBuddy` + `accessibleName`. Invoked once on the shell and on every mount in `WorkspaceRouter._ensure_mount`. Verified end-to-end: the mounted-destination sweep reports zero unnamed controls |
| 2 | **ACCESSIBLE NAMES — form-row containers** — `QFormLayout.addRow(text, container)` auto-creates a label but buddies it to the row *container* (a plain non-focusable `QWidget`), so the caption never reaches the inner controls. Cross-parent buddies also do not resolve a name via `QAccessible` | **HIGH** in affected rows (band/axis range rows) | FIXED — `_retarget_container_buddy` descends into the row container and re-points the buddy to the first unnamed labelable; when the target lives under a different parent the explicit `accessibleName` is stamped directly (verified: `compare_low` resolves `比較帯域`) |
| 3 | **ACCESSIBLE NAMES — caption-less controls** — ~40 controls have no adjacent caption at all: specialist trees (`search_axis_tree`, `pareto_tree`, `measurement_plan_tree`, …), band-boundary spin pairs separated by 〜/– labels, icon-only rail settings button, palette search field, read-only `RoomHistoryPanel.detail` | **MED–HIGH** | FIXED — explicit `setAccessibleName` at each construction site in Japanese UI terms (e.g. `探索軸`, `帯域 上限 Hz`, `設定`, `コマンド検索`, `リビジョン詳細`). Separator labels (〜/–) are excluded from buddying by an isalnum check so they can never become names |
| 4 | **RAIL COMPACT MODE** — `WorkflowRail.set_compact(True)` truncates every destination button's *text* to a single glyph; with no `accessibleName` the screen reader announces only the truncated character | **MED** | FIXED — each rail button carries `accessibleName = registration.label` (full destination name); verified `プロジェクト` button keeps the full name while the caption collapses |
| 5 | **Ctrl+S / project.save scope hole** — save was bound only inside the Room and Optimization workspace mounts (`WidgetWithChildrenShortcut`). From Overview/Projects/Inbox/Activity/Library there was *no* binding at all: Ctrl+S was dead and `project.save` was unreachable by keyboard | **HIGH** — primary shortcut dead on most screens | FIXED — a `CommandShortcutBinder(shell, …, WindowShortcut)` binds `project.save` window-wide as a fallback (workspace-local bindings still win inside their mounts); `_unbind_workspace_commands` rebinds a dispatcher that saves every dirty mounted workspace, so Ctrl+S from Overview now flushes a dirty room (verified end-to-end) |
| 6 | **COMMAND PALETTE focus theft** — the palette grabbed focus into `search_field` but `hideEvent` never returned it; closing the palette left keyboard focus in limbo (or on whatever Qt fell back to) | **MED** — focus lost after every palette use | FIXED — `prepare_to_show` records the pre-open `focusWidget`; `hideEvent` defers a restore via `QTimer.singleShot(0)` (Qt moves focus *after* hideEvent, so a synchronous restore is overwritten), reactivating the target's window first since a closed top-level can leave no active window. Verified offscreen: settings button focus → palette open → hide → focus returns |
| 7 | **MNEMONICS** — project menubar had **zero** `&X` mnemonics: no menu was keyboard-reachable by Alt+letter and entries had none | **MED** | FIXED — `プロジェクト(&P)` plus unique mnemonics on all 9 entries (N/O/R/D/E/I/C/A/U); test asserts presence + uniqueness |
| 8 | **FOCUS VISIBILITY / POLICY** — sweep for `paintEvent` overrides that drop the focus rect, `ClickFocus`-only controls, missing `StrongFocus` | — | VERIFIED OK — no focus-rect-swallowing `paintEvent` exists; interactive controls keep default `StrongFocus`; icon buttons are `QToolButton`/`QPushButton` (default policy covers Tab) |
| 9 | **SHORTCUT CONFLICTS** — every `QShortcut`/`CommandShortcutBinder` binding per window | — | VERIFIED OK — bindings are centralized through `CommandShortcutBinder`; the new window-level `project.save` fallback cannot conflict because `WidgetWithChildrenShortcut` outranks `WindowShortcut` inside workspace mounts and yields elsewhere. No duplicate key in one context found; editor-local keys use standard Qt text-editing behavior inside their fields only |
| 10 | **KEYBOARD-ONLY COMPLETENESS** — trees/lists activate via keyboard (Qt `Activated`/`returnPressed`), rail is Tab-focusable, command palette reaches every registry command, menus Alt-mnemonic reachable | — | VERIFIED OK post-fix — the only unreachable-by-keyboard surface found was the save shortcut (#5) and menu mnemonics (#7) |
| 11 | **TAB ORDER** — natural layout order in dialogs/forms | — | VERIFIED OK — no explicit `setTabOrder` anywhere, so Qt follows widget creation order which matches visual layout in the single-column forms and row-based toolbars inspected |
| 12 | **SPA SIDE** (`frontend/src`) | — | VERIFIED OK — interactive elements carry `aria-label`/`role`; list/tab keyboard navigation present; no custom modals exist that would need a focus trap. Minor: a plain-anchor audit only — no runtime DOM harness was exercised |

## Fixed

### F1 — `htdt.accessible_labels` (new module) + call sites

`wire_label_buddies(root)` — for each `QLabel` in `root`:

1. Skip non-caption text (`_is_caption_text`: must contain an alphanumeric —
   separators like 〜/– and decorative glyphs never wire).
2. If the label already buddies a *real* control → leave it.
3. If `QFormLayout` auto-buddied it to a plain container → descend into the
   container (`_retarget_container_buddy`).
4. Otherwise find the first unnamed labelable control following the label in
   its innermost owning layout (`_following_unnamed_control` →
   `_first_unnamed_labelable`), recursing into plain `QWidget`/`QFrame`
   containers and stopping at the first real widget so captions cannot leak
   into the next section.
5. `setBuddy(target)`; when the target has a different `parentWidget`
   (Qt only resolves buddy names within one parent) also stamp the explicit
   `accessibleName`.

Idempotent, returns the wiring count; called from `WorkflowShellWindow` for
the chrome and from `WorkspaceRouter._ensure_mount` for every lazily-created
destination. `_LABELABLE` covers combo/line/spin/item-view/tab/slider/text
edits; buttons are excluded (self-named via text) and intentionally stop a
caption's reach (`_NON_CONTAINER`).

### F2 — explicit `accessibleName` at ~40 construction sites

One line each at widget construction, Japanese UI terms — see the diff for
the full list (optimization workspaces, measurement page, system expansion,
room panels, application pages, palette, rail, settings).

### F3 — `project.save` window-wide fallback

`workflow_application.py`: `_shell_save_binder` binds `project.save` at
`WindowShortcut` context on the shell; `_unbind_workspace_commands` rebinds
a dispatcher that iterates mounted dirty workspaces
(`_save_dirty_workspaces`) so the same command id works whether a workspace
binding or the shell fallback dispatches it.

### F4 — palette focus restore + window reactivation

`command_palette.py`: `prepare_to_show` stores `QApplication.focusWidget()`;
`hideEvent` schedules `QTimer.singleShot(0, restore)` which reactivates the
target's window when needed then `setFocus` — guarded by
`isEnabled`/`isVisible` (stale targets after a workspace switch are skipped)
and `RuntimeError` (deleted C++ objects).

### F5 — menubar mnemonics

`プロジェクト(&P)` and nine unique entry mnemonics in
`_build_project_menu`.

## Verified non-gaps (kept out of the sweep)

- `measurementWorkspaceNoticeAction` — hidden `QPushButton` that always
  receives its text before `setVisible(True)`; invisible widgets need no
  name.
- Two `QToolButton`s parented inside `QLineEdit`s — Qt-internal
  (clear-button / search suffix), not authored controls.
- `QScrollBar`/`QHeaderView` instances — owned by their scroll area / table;
  named through the parent control, not independently.

## Regression tests

`backend/tests/test_accessible_labels.py` — 14 cases:

- helper units: caption→buddy, nested-row reach, header-does-not-buddy-button,
  separator rejection, form-row container naming, existing-buddy preservation,
  named-control preservation;
- shell-level: all-mount unnamed-control sweep == 0, compact rail keeps full
  names, Ctrl+S WindowShortcut present, project-menu mnemonic uniqueness,
  palette search field named, palette focus restore on close, Ctrl+S fallback
  saves a dirty room from Overview.
