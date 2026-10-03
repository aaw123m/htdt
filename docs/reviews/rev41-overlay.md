# REV41 — reflection-guidance viewport overlay review

Scope: PR #514 (`218aa5a7`) — `display_input.reflection_guidance_overlay`
(off/auto/on), the `guidance-` actor lane, `bind_reflection_guidance`,
preference live-apply, and the ガイド toggle suppression. Read-only review:
findings report here; no defects cleared the fix bar.

Method: (1) line-level audit of the preference definition, panel signal,
workspace binding, and viewport actor lane; (2) runtime probes against the
real `PreferenceStore` + `RoomWorkspace` composition (offscreen Qt);
(3) scoped pytest re-run of the REV40 suite.

## Checklist verdicts

| # | Item | Verdict |
|---|------|---------|
| 1 | Persisted setting round-trip + invalid-value handling | CLEAN — probed `'bogus'`, `42`, `null`, `{"x":1}`, `""`, and a corrupted prefs file: every value degrades to `'auto'` with a typed load state and JA diagnostic; store validation also rejects garbage on write. |
| 2 | Listener leak / double-binding on remount | CLEAN — probed: bind adds exactly 1 listener; `deleteLater` + `DeferredDelete` delivery → `destroyed` → `unsubscribe` → 0 listeners, 0 renders on later writes. Double-`bind_reflection_guidance` stacks listeners (see judgment #5) but is unreachable — single call site in the mount composition; each mount builds a fresh workspace. |
| 3 | `auto` scoping correctness | CLEAN — `current_context == 'acoustics'`; `set_context` always ends in `_render()`, so markers appear/disappear on context entry/exit; probed acoustics↔geometry transitions. See judgment #1 for the `overlays.acoustics` nuance. |
| 4 | Actor namespace cleanup | CLEAN — `'guidance-'` registered in `_OVERLAY_ACTOR_PREFIXES`; identical-signature render calls `_remove_overlay_actors()`; rebuild path `plotter.clear()`; empty-marker early return sits after cleanup inside `deferred_render`, so an off-toggle or data-loss pass cannot ghost old actors. |
| 5 | Replay honesty | CLEAN — every anchor derives from persisted/proven fields: `plane_point` is `specular.ordered_interaction_points[0]`; `path_points` emitted only when both source and receiver refs resolve from a geometry-matched snapshot; degraded snapshot (geometry_match=False, no source ref) projects zone-only — nothing fabricated. Corner label honestly names the data ("確定的パス権威"). |
| 6 | Performance | CLEAN with a note — markers are O(persisted artifacts) rebuilt per `_render()`, same pattern as the prediction/history overlay lanes; no unbounded accumulation (namespace wiped each pass). See judgment #4. |
| 7 | `guides_visible` + scrub lane | CLEAN — suppression is workspace-side (`_visible_guidance_markers` returns `()` when the ガイド toggle is off) so the viewport lane stays independent; scrub/history lanes untouched; tested by `test_overlay_suppressed_by_guides_toggle`. |
| 8 | Document-switch staleness | CLEAN — `_switch_project` → `dispose_data_workspaces` → `deleteLater` → `destroyed` → unsubscribe; each mount composes a fresh workspace+panel and reloads the view; no cross-document marker bleed possible. |
| 9 | Preference-listener thread-safety | CLEAN — `_commit` fires listeners synchronously after the durable write; all writers are GUI-thread (`workflow_settings` row); the listener only mutates `self._guidance_overlay_mode` and calls `_render()` — no cross-thread viewport access. |
| 10 | Freshness after artifact changes | CLEAN — `set_context('acoustics')` and `workspace._refresh()` call `acoustics_panel.refresh()` → `RoomAcousticsTabs.refresh()` refreshes every tab incl. guidance → `guidanceViewChanged` → markers reload. |
| 11 | Corner-text collision | CLEAN — `lower_right` is unoccupied; prediction/snap labels live at `lower_left`, ghost/search at `upper_*`. |

## Judgment calls — reported, not fixed

| # | Finding | Severity | Notes |
|---|---------|----------|-------|
| 1 | `auto` ignores the `overlays.acoustics` checkbox: inside the acoustics context with that overlay unchecked, guidance markers still draw. The stated semantics ("音響コンテキストを開いている間だけ") match the code, and the dedicated 3-state pref is the authoritative off switch — but a user who hides the acoustic overlay may expect all acoustic overlays hidden. | LOW consistency | If desired, gate on `overlays.acoustics` too — one-line change; deferred pending product intent. |
| 2 | Overlay always projects **all** persisted revisions (`guidance_overlay_markers` iterates `view.entries`), matching the panel's `すべてのリビジョン` default — but it does not follow the panel's revision combo: `guidanceViewChanged` only fires on full `refresh()`, and the combo narrows `_visible_entries` without re-emitting or exposing the selection. | LOW consistency | Making the overlay track the combo needs the panel to emit the filtered selection — small API change, deferred. |
| 3 | Marker labels use the raw `surface_id` (e.g. `wall-left`) while the panel resolves JA display names — cosmetic divergence; ASCII ids are also pragmatic since VTK point-label text drops CJK glyphs (pre-existing viewport limitation). | LOW cosmetic | — |
| 4 | Unbounded marker count: each persisted artifact adds up to 4 actors per render. Bounded in practice by artifact volume; no throttling/declutter exists. | LOW perf | Same shape as the prediction overlay lane; revisit only if large projects measure slow. |
| 5 | Calling `bind_reflection_guidance` twice stacks a second pref listener + second signal connection (probe-verified: 2 listeners → 2 renders per write). Unreachable today — one call site — but the bind is not idempotent. | LOW hardening | Optional: store the listener and unsubscribe-or-skip on re-bind. |
| 6 | A listener exception (`self._render()` raising inside `_on_pref`) propagates as `PreferenceNotificationError` through the settings write — the same exposure every store subscriber carries by design (#742 observer error surfacing). | — observed | Honest-failure semantics, not a defect. |

## Probe evidence (runtime, not code-reading)

- `rev41_probes.py` — corrupt values `'bogus'`/`42`/`null`/`{}`/`''` → all
  read back `'auto'` with diagnostics; garbled prefs file → `'auto'` +
  corrupt load state; write-path validation rejects non-members.
- `rev41_probe2.py` — two live writes while bound → exactly 2 renders;
  `bind` → +1 listener.
- `rev41_probe3.py` — `deleteLater` + `sendPostedEvents(DeferredDelete)` →
  `destroyed` fires → `_listeners == 0`; post-destroy write → 0 renders.
  (PySide6 note: `processEvents()` alone does **not** deliver
  `DeferredDelete` — first probe's "leak" was a harness artifact.)
- Double-bind probe: 2 listeners, 2 renders per write — theoretical only.
- Unbound workspace with `'on'` persisted → 0 markers (fail-closed default).

## Test suite

`TMPDIR=/c/t QT_QPA_PLATFORM=offscreen pytest backend/tests/test_reflection_guidance_overlay.py
backend/tests/test_reflection_guidance_ui.py backend/tests/test_room_viewport.py -q -n 4
--basetemp=C:/t/rev41-pytest` → **36 passed**.
