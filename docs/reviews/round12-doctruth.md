# Round 12 — User-facing documentation & onboarding truth

Scope: do the shipped docs, READMEs, installer flow, in-app help and
first-run journey match the built behavior? A new user following the docs
should never hit a lie or a dead end. Branch `devin/rev12-docs`. No GitHub
Actions — every claim below was verified locally against the shipped code
(and the CLI flags were executed live).

## Method

- Read `README.md`, every non-`reviews/` top-level doc (`docs/*.md`),
  `installer/HTDT.iss`, `scripts/*.ps1`, `backend/pyproject.toml`
  entry points and the in-app help registry.
- Ran every CLI surface for real: `python -m htdt.native_cad --help`,
  `--version`, `--backup`, `--restore`, `--automatic-backup`,
  `--seed-synthetic-demo`, `--migrate-legacy-data`,
  `python -m htdt --help`, `python -m htdt.capture_import --help`.
- Enumerated the shipped command registry, workspace/context canonical
  lists, palette providers, rail contents and help registry programmatically
  and diffed them against the docs' claims.
- First-run journey traced in code: splash → upgrade →
  `project_library.resolve_startup_document` → workflow shell Overview →
  `next_action` guided path → Room → 機器の準備 checklist.

## Claim → verified table

| Doc claim | Where | Verified | Result |
|---|---|---|---|
| program root `%LOCALAPPDATA%\Programs\Home Theater Digital Twin` | README / HTDT.iss `DefaultDirName` | HTDT.iss:17 | OK |
| user data root `%LOCALAPPDATA%\HomeTheaterDigitalTwin` | README | `native_editor.py` data root + HTDT.iss lowpriv | OK |
| 3 file associations → `HTDT.exe "%1"` | HTDT.iss | Registry section lines 55-66 | OK |
| `.htdt-backup` opens preview only | HTDT.iss comment | `preview_restore` path in native_cad | OK |
| `--version` → `<version>+g<sha>[.dirty]` | README / RELEASING.md | ran it — `0.2.0.dev0+g06a7d0dc[.dirty]` | OK |
| `--backup/--restore/--automatic-backup/--seed-synthetic-demo/--migrate-legacy-data` | code | all ran live (backup schema=1 files=6; pre-restore backup created; auto-backup `htdt-backup-automatic_periodic-*`; duplicate seed refused exit 2; `{"state":"nothing_to_migrate"}`) | OK |
| maintenance options mutually exclusive | code | argparse mutually exclusive group | OK |
| `python -m htdt` = dev-only launcher, requires `--data-dir`/`--allow-default-data-root` | `__main__.py` help | ran `--help` | OK |
| `GET /api/backup`, `POST /api/restore` → `410 Gone` | README | `main.py:810-814` | OK |
| `.instance.lock` byte-range lock | README | `native_instance_lock.py` | OK |
| `htdt-native`, `htdt-capture-import` entry points | pyproject | `[project.scripts]` resolve, `--help` works | OK |
| `.\scripts\run-native.ps1` launches native | README | script passes `--document-id fixture-f1` (now documented) | OK* |
| workflow shell default; `--legacy-ui` rollback | IMPLEMENTATION_STATUS | argparse `--legacy-ui` + workflow-shell default | OK |
| MMB pan / Shift+MMB orbit / wheel zoom / RMB context menu | UI_DESIGN / UX120_CAD_INPUT | `cad_input.py` gesture map | OK |
| Ctrl+K command palette | UI_DESIGN | `command_palette.py`, registered | OK |
| Esc cancel / Enter commit | UI_DESIGN | `room.edit.cancel`/`room.edit.commit` in registry | OK |
| Ctrl+左click additive select | UI_DESIGN | `room_viewport.py` entityPicked Ctrl=additive | OK |
| Overview next-action guided path | UI_DESIGN §5 | `overview_readiness._next_action` chain | OK |
| responsive room panel 300/280/260 px | UX150 doc | `room_workspace.py:6233` | OK |
| shell compact threshold <1120 px | UX150 doc | `workflow_shell.py:712` | OK |
| TopContextBar compact hides title | UX150 doc | `workflow_shell.py:554` | OK |
| design tokens (`surface.canvas` #0F141A etc.) | DESIGN_SYSTEM | `ui_theme.py` SURFACES/ACCENT/SEMANTIC | OK |
| `settings.*` destinations + `help.*` palette entries | code | `palette_search.py` providers | OK |
| 21→19 help topics, all links/commands resolve | in-app help | `HelpRegistry.validate()` clean | OK |
| 機器の準備 checklist (UMIK-1 serial/cal file/48 kHz/SPL/REW plan) | measurement calibration ctx | `measurement_instrument_onboarding.py` | OK |
| activity links `htdt://nav/v1/...` | activity page | `navigation_target_from_uri` | OK |
| RELEASING.md version chain (`__init__.py` → build_info → AppVersion → filename) | RELEASING.md | `Get-HtdtVersion.ps1`, `HTDT.iss` fallback `0.2.0.dev0` | OK |
| `Ctrl+O / Ctrl+N` 開く/新規project | UI_DESIGN shortcut table | **nothing registered** — no `Key_O`/`Key_N`, no file.open command | **LIE → fixed** |
| `D` = Room sketch 寸法tool | UI_DESIGN | **nothing registered** | **LIE → fixed** |
| `I` = 計測tool | UI_DESIGN | shipped as **`T`** (`room.measure` 計測ツール) | **stale → fixed** |
| `Shift` = transform中のprecision modifier | UI_DESIGN | actual: **snap一時無効** (`room_transform_input._snap_enabled` returns False w/ Shift; status hint「Shiftでスナップ一時解除」) | **stale → fixed** |
| `Alt` = snap一時反転 | UI_DESIGN | exists only in legacy `native_editor.py` (AltModifier bypass); workflow shell uses Shift | **stale → fixed** |
| Shift+MMB開始位置 = orbit pivot候補 | UI_DESIGN | `begin_orbit` ignores position; pivot is camera focal point (fit target) | **LIE → fixed** |
| 「下端にヘルプ / 設定」 | UI_DESIGN §4 | rail bottom has 設定 only; help is palette-only (Ctrl+K → `help`) | **stale → fixed** |
| IA tree `└─ ヘルプ / 設定` + sub-context lists | UI_DESIGN §3 | shipped: 取り込み/アクティビティ/ライブラリ/サポート destinations; room adds 履歴; measurement adds キャンペーン/機器の準備; optimization = 探索設定/候補/比較/介入計画/ばらつき耐性/測定・検証 | **stale → fixed** |
| context examples (部屋=形状/物体/スピーカー/音響等) | UI_DESIGN §4 | updated to shipped `CANONICAL_WORKSPACE_CONTEXTS` | **stale → fixed** |
| `optimization.compare_candidates` → `optimization/candidates` | COMMAND_SYSTEM | actual deep-link `optimization/comparison` | **stale → fixed** |
| context lists (Room 4 / Measurement 4 / Optimization objectives+measurement-plan) | COMMAND_SYSTEM | canonical now room5 / measurement6 / optimization6; `objectives`/`measurement-plan` are normalize aliases | **stale → fixed** |
| compact rail 112 px, labels retained | UX150 polish doc | shipped `COMPACT_WIDTH=72`, labels collapse to first glyph+tooltip (changed in IA v2 `9c0511cb`) | **stale → fixed** |
| shortcut table missing Ctrl+A/Ctrl+I/H/L/Backspace | UI_DESIGN | all shipped in registry — added rows | **gap → fixed** |
| README launch options incomplete | README | FILE positional/`--data-dir`/`--document-id`/`--legacy-ui`/`--safe-mode`/`--automatic-backup`/`--migrate-legacy-data` undocumented | **gap → fixed** |
| `run-native.ps1` behavior | README | opens `fixture-f1` project by default — undocumented gotcha | **gap → fixed** |

## Onboarding / first-run findings

Traced path: `python -m htdt.native_cad` → splash → DB upgrade →
`resolve_startup_document` auto-creates `My Home Theater` on empty library →
workflow shell Overview → bottom primary button shows the next action
(`部屋を作成` → `部屋を完成させる` → `スピーカーを追加` → … → `最適化を始める`)
→ Room contexts → 測定「機器の準備」UMIK-1 checklist. No functional dead end.

Real discoverability/onboarding gaps found and fixed in docs:

- **Help has no visible entry point.** No rail button, no menu, no F1 —
  the 19-topic help surface (shortcuts, glossary, workflows, troubleshooting)
  is reachable *only* by typing `help`/`ショートカット` in the Ctrl+K palette
  or via contextual "why" reasons. Now documented in README + UI_DESIGN.
- **`My Home Theater` initial project name is English** in a JP-first app.
  Pinned by `test_project_library.py`; product decision — deferred, not a
  doc lie (README now states it factually).
- **`htdt://nav` deep links are internal-only** — activity-page entries and
  help deep links resolve them; no OS protocol registration and no user input
  surface, so no user-facing doc is required. Verified internal.

## Fixes applied (small diffs)

- `README.md`: first-run journey note (auto-created project, guided path),
  help-entry note (Ctrl+K → `help`), full launch-option list (`FILE`,
  `--data-dir`, `--document-id`, `--legacy-ui`, `--safe-mode`), maintenance
  options (`--automatic-backup`, `--migrate-legacy-data`, mutual
  exclusivity), `run-native.ps1` fixture-f1 gotcha.
- `docs/UI_DESIGN.md`: IA tree last line + sub-contexts → shipped;
  help-entry note; rail-bottom claim; context-bar examples → shipped
  canonical names; shortcut table — Ctrl+O/N and D marked 未実装, `I`→`T`,
  Shift→snap一時無効, Alt→legacy UI only, orbit-pivot row marked 未実装,
  added shipped Ctrl+A / Ctrl+I / H / L / Backspace rows; focus-safe key
  list `M/R/D/I` → `M/R/T`.
- `docs/COMMAND_SYSTEM.md`: `compare_candidates` deep-link
  `candidates`→`comparison`; canonical context lists → shipped
  (room+history, measurement+campaign+calibration, optimization 6 with
  `objectives`/`measurement-plan` alias note).
- `docs/ISSUE_118_UX150_SOFTWARE_POLISH.md`: compact rail 112→72 px and
  label-glyph collapse, annotated as the IA v2 change so the dated record
  stays honest.

## Deferred

- EN default project name `My Home Theater` in JP-first UI (test-pinned
  constant; product decision, not doc truth).
- Help discoverability beyond docs (e.g. a rail/menu entry) — product
  decision; docs now state the real entry point.
- Dated integration/acceptance records (UX120_140, N90, etc.) left as
  historical; only the stale *current-state* claim in UX150 was corrected.
