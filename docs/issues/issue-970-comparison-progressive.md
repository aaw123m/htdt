# Issue #970 — 測定 A/B 比較：基本3手順の固定操作領域と「詳細条件」の段階的開示

## Scope

The 測定 → 比較 page now presents a fixed short strip for the basic
three-step flow — **比較目的を選ぶ → A/Bを選ぶ → 判定/差分を見る** — while
expert conditions (除外帯域, 参照帯域, 位相・個別スムージング) collapse
into a 「詳細条件」 block using the existing `_advanced_block` pattern
from the optimization workspace. Semantics, defaults and every authority
(#564 registration gate, `ComparabilityVerdict`, `ComparisonSemantics`,
`compare_frequency_responses`) are unchanged; this is a view-layer
reorganization plus honest, field-adjacent surfacing of what the
canonical authorities already decide.

All changes are in
`backend/src/htdt/measurement/ui/measurement_page_workspace.py` with
tests in `backend/tests/test_measurement_workspace_composition.py` and
fixture repair in `backend/tests/test_round8_measurement_journey.py`.

## Vocabulary

| Term | Meaning |
|---|---|
| `step_hint` | Fixed one-line strip at the top of the setup card: `基本3手順: ① 比較目的を選ぶ → ② A/Bを選ぶ → ③ 判定/差分を見る`. Always visible, never scrolls with the details. |
| `pair_summary_label` | At-a-glance identity of the selected pair: per-side `source · channel role · target · scene binding`, whether both sides pin the same SceneRevision, the `level_compatibility` verdict and the registration state for 実測×予測 pairs. |
| `selection_reason_label` | The **nearest resolvable reason** the current selection cannot be saved, rendered beside the fields. Empty when nothing blocks. |
| `save_guidance_label` | The concrete next operation next to the save button (`次の操作: …`). Bounded width so it wraps instead of stretching the card. |
| `comparison_state_label` | Now synced to exactly `プレビュー（未保存）` / `保存済み比較` / `stale（古い登録）` / `比較不能（…）`. Semantic states: none=normal, WARNING for advisory, STALE for stale, UNSUPPORTED for 比較不能. |
| 詳細条件 | `comparison_details_block` — `_advanced_block` wrapping レベル参照 (ref_band_check + low/high), 除外帯域 (low/high + add/remove + table) and 表示スムージング (per-side smoothing combos). Collapsed by default; toggle only calls `setVisible` on the content — no state is touched, so open/close can never alter selection, band, preview or saved evidence. |
| `phase_note_label` | Secondary note under the phase lane for partial-availability cases (one side missing phase, or phase drawn without a common timing basis). |

## Reason chain

`_update_selection_reason` runs on every selection/band change and walks
the canonical gates in order — the FIRST failing gate is the displayed
reason, so the operator always sees the closest actionable problem:

1. **Incomplete selection** → labels cleared, save disabled.
2. **実測×予測 without registration** → `比較不能（未登録）`; guidance
   points at 「登録レコードを作成」. This mirrors the #564 gate in
   `_run_comparison` before the click, not after it.
3. **Registration verdict not comparable*** → `比較不能（<state>）` with
   the verdict's own reason codes translated by
   `_comparability_reason_label` (geometry_revision_mismatch,
   receiver_position_unknown, routing_topology_mismatch,
   unsupported_frequency_domain, timing_reference_unknown,
   phase_*/level_reference_*/magnitude_domain_unavailable …).
4. **Registration freshness ≠ current** → `stale（古い登録）`; guidance
   offers re-registration or 残差確認. `_pair_registration_freshness`
   treats unreadable freshness as `unverified` — never `current`
   (#815 fail-closed).
5. **Verdict limitations** → advisory line (not blocking).
6. **Band overlap** → mirrors the pinned rule in
   `compare_frequency_responses` (`overlap_low = max(low, a.f0, b.f0)`,
   `overlap_high = min(high, a.fN, b.fN)`): when the requested band
   misses the common range the guidance names the concrete
   `A∩B` Hz span; when the datasets are truly disjoint the guidance
   honestly says pick another pair instead of promising a band that
   cannot exist.
7. **Semantic advisories** — `level_compatibility` and
   `common_time_phase` unavailability surface as warnings that do not
   block save (the canonical save path itself allows them).

Blocked → `compare_button` disabled (fail-closed: a wrong diff cannot
be saved), state label shows the blocked vocabulary, reason label
carries ERROR semantic. Unblocked → button enabled, label returns to
`プレビュー（未保存）`/`保存済み比較` per `_last_comparison`, WARNING
semantic while advisory reasons remain.

## Chart honesty

- **Phase lane never disappears silently.** `phase_compare_plot` stays
  visible; when no phase can be drawn `show_plot_state` renders
  `位相は表示できません` with the per-side ground
  (`A: 位相データがありません`), and `_bind_saved_pair`'s missing-pair
  branch states its ground instead of hiding the plot.
- **Partial availability is named.** If one side lacks phase the drawn
  side stays and `phase_note_label` says which side is missing; if the
  pair shares no common timing basis the note says the drawn phase is
  reference-only (`共通のタイミング基準なし … 表示は参考値`).
- **Eye order** is FR → A−B diff → 位相, unchanged; cursor, 0 dB line,
  legend and CSV/PNG export are untouched.
- **No repair by guessing.** Reason text is built only from
  `MeasurementView` fields, `ComparabilityVerdict` codes,
  `ComparisonSemantics` availability and the pinned overlap rule —
  measurement data is never inferred or patched.

## Evidence model

- `_preview_comparison_pair` still previews whatever is plottable —
  chart displayability and saveability stay distinct axes: a blocked
  pair keeps its preview while the save button and state label refuse
  the claim.
- `_update_pair_summary` / `_update_selection_reason` take the fetched
  `views` tuple so one selection change costs one verification pass,
  not three.
- `compare_low`/`compare_high` `valueChanged` re-run the reason chain,
  so band edits clear or raise the overlap blocker live.
- `_history_selection_changed` and `_run_comparison` reset the state
  label's semantic styling when it flips back to 保存済み比較.

## Accessibility / layout

- JA strings throughout; bounded labels (`save_guidance_label` max
  width 380, word-wrap) keep the strip inside narrow/DPI windows.
- The 詳細条件 toggle is a checkable compact button with a JA
  `accessibleName`, tooltip and WhatsThis; Tab order follows the visual
  order (preset → A → B → band → details toggle → details controls →
  compare).
- Two journey tests were updated to create the registration record the
  #564 gate already required before their `compare_button.click()` —
  they were red on `main` and are now green.

## Verification

- `test_measurement_workspace_composition.py`: 15 tests — fail-closed
  unregistered state, pair summary contents, band gate both directions
  (resolvable vs disjoint), details-toggle state preservation, phase
  lane reason, stale registration, saved-history reload.
- `test_round8_measurement_journey.py` + accessibility/terms/UX140
  suites: 42 tests green.
