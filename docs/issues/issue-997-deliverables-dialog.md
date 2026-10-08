# Issue #997 — デリバラブルセンターのスクロール・検索・状態絞り込み

## Scope

`DeliverablesDialog` added every catalog category's `QGroupBox` and its
entry rows straight into the dialog's `QVBoxLayout` at a fixed 640x520 —
no `QScrollArea`. With many candidates, or at 125–200% DPI / short
windows, the lower categories, the 書き出し buttons and the Close row were
unreachable. The existing `DeliverablesCatalogService.catalog()` and the
readiness evaluation are untouched: this is presentation-surface work
only, no reimplementation of the #146 catalog or the export authorities.

Lives in `backend/src/htdt/deliverables_dialog.py`; tests in
`backend/tests/test_issue_997_deliverables_dialog.py`.

## Layout shape (#997)

```
QDialog
├── pinned header ─ プロジェクト: <document_id>   (SECTION_TITLE)
│                  ─ 現在のリビジョン: <scene_revision pin | 未保存>
│                  ─ [検索 QLineEdit] [状態フィルタ QComboBox] [件数]
├── QScrollArea (widgetResizable, NoFrame)
│   └── per-category QGroupBox (only shown when it has visible rows)
│       └── _EntryRow: バッジ | タイトル+要約 | [詳細を表示] | アクション
├── pinned notice label (warning state, hidden until a re-check fails)
└── pinned QDialogButtonBox Close
```

Heading, search, filter and counts never scroll; the candidate groups
scroll; Close never scrolls. Minimum size 420x320; every action stays
reachable at 640x480 / 800x600 / 1280x720 and at a doubled point size
(the 200%-DPI proxy used offscreen).

## Filter / search vocabulary

- 状態フィルタ: `すべて / 出力可能 / 制限付き / 要入力 / 古い` →
  `available` / `available_degraded` / `blocked+not_applicable` /
  `stale_review`.
- Search matches title, reason and `deliverable_id`, case-insensitive.
- The row badge now mirrors the filter vocabulary: `available_degraded`
  renders 制限付き and `stale_review` renders 古い（要レビュー） — never
  conflated with plain 出力可能.
- Filtering only hides widgets; each action button stays bound to the
  same `deliverable_id` it was rendered with, so filter/search/reorder
  cannot cross-select another entry's output.

## Progressive disclosure

Each row shows the primary state (badge) plus a one-line next-input
summary (first sentence of the reason, capped at 60 chars). The full
reason, the referenced source-authority pins and the expected formats
sit behind a 「詳細を表示」 `QToolButton` per row — expanding never
rebinds the action, it acts on the same entry.

## Click-time re-validation

Rows are rendered from a catalog snapshot. `書き出し` does **not** call
the command directly: `_execute_output` re-reads `catalog()` first and
requires the same `deliverable_id` to still carry the same `command_id`,
a still-outputable availability (`available` / `available_degraded` /
`stale_review`) and identical `source_authorities` pins. On any drift —
availability fell out of the outputable set, pins changed, the entry
disappeared, or the whole catalog is now a different project's — the
output is refused, the pinned notice explains why, and the list is
rebuilt from live authority. A stale display can therefore never emit
stale or unauthorized data, and a project switch under the open dialog
cannot leak doc-A's snapshot into doc-B's pins.

## Tests

`backend/tests/test_issue_997_deliverables_dialog.py` — 26 tests:

- **Layout:** pinned chrome lives outside the `QScrollArea`; every
  `QGroupBox` lives inside it; Close pinned.
- **Count matrix:** 0 / 1 / 10 / 30 candidates render the right row
  count and `表示 N / N 件` counts.
- **Size/DPI matrix:** 640x480, 800x600, 1280x720 with 30 candidates →
  positive scrollbar range, last row reachable via
  `ensureWidgetVisible`, Close still visible; doubled point size at
  640x480 likewise.
- **Search/filter:** text match narrows rows + counts; each filter
  choice maps to the right availabilities; the surviving button still
  emits its own entry's command.
- **Disclosure:** detail hidden → 「詳細を表示」 expands full reason +
  authority pins → collapses again.
- **Readiness re-check:** availability drift (available→blocked),
  source-pin drift (`rev-1`→`rev-2`), and a project-switch pin swap all
  refuse output with the notice shown and rows rebuilt; matching state
  still runs; blocked/not_applicable entries expose no output button.
- **Smoke:** real `DeliverablesCatalogService` roundtrip against a saved
  F1 scene.

Result: 26 passed (also re-ran `test_qt_smoke_round11`,
`test_deliverables_catalog`, `test_round9_output_surfaces` — green).

## Known gaps

- Real-DPI coverage (125–200% Windows scaling) is verified on the real
  GUI, not offscreen — the offscreen run uses the doubled-font proxy.
- The filter maps `not_applicable` under 要入力 (both mean "cannot output
  now"); a dedicated 対象外 choice can be added later if operators want
  to isolate it.
