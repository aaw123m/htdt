# Round 10 — Fresh-eyes final sweep (convergence round)

Scope: a from-scratch senior-reviewer pass over `main` looking only for
big-picture gaps that survived rounds 6–9 (~90 fixes). No edge-case or style
hunting. Method: trace the flagship user journeys through the *wired* code
path (not tests), cross-check the legacy SPA against the backend route table,
and sweep for spec-level seams (units, axes, versioning keys, unreachable
production paths). Prior `docs/reviews/round6-*.md` … `round9-*.md` were read
first; their findings are not re-reported. Branch `devin/rev10-fresh`.

## Verdict: CONVERGED (with one cleanup fix + named deferred class)

The flagship journey is end-to-end wired on the native surface, the legacy
SPA's every API call resolves to a real route, module-boundary invariants
are consistent, and every "looks dead" trail checked this round turned out
to be deliberately handled (see below). The only remaining fresh finding is
a small dead-code deletion already earmarked in round 6.

## Journey-by-journey trace (wired path, not tests)

| Journey step | Reachable? | Honest output? | Notes |
|---|---|---|---|
| First launch | Yes — `htdt-native` → `_run_gui` → `decide_launch` (recovery/safe-mode) → `record_launch` → upgrade plan | Yes — unreadable store and legacy `htdt.sqlite3` produce explicit Japanese warnings, not silent fallback | `resolve_startup_document` opens explicit doc / most-recent / auto-creates DEFAULT_PROJECT when library empty; archived projects refused |
| Project create / switch | Yes — Projects page + `projectSwitchRequested` → `NavigationTarget(PROJECT)` | Yes — library listing is real `cad-scenes` records | No lifecycle UI (archive/restore) — already deferred in prior rounds |
| Room model | Yes — Room workspace contexts (geometry/objects/placement/acoustics/history) all in `CANONICAL_WORKSPACE_CONTEXTS` | Yes — revisions sealed, authority chain verified on read (r8) | — |
| Measurement import (REW) | Yes — Measurement import context: file pick → pending staging → batch save | Yes — pending rows labelled "保存前の一時データ"; pinned importer + replay-verify | — |
| Prediction / solve | Yes — Room acoustics context → `RoomPredictionController` → `NativeWorkerPool` + `PredictionJobGuard` | Yes — result surface driven by `interpret_prediction_results` (findings/reliability/capability-first, provider+hash provenance under Advanced); stale results bind exact stored authority (#938); `before_deactivate` blocks switching mid-run; `dispose` announces "遅延結果は保存・適用しません" | Two *parallel* solver-neutral surfaces (`cad_prediction_capability` #727, `cad_prediction_execution` #478) remain unbound — see Deferred |
| Optimize | Yes — Optimization workspace pages {setup, candidates, comparison, interventions, robustness, validation}; context aliases (objectives→comparison etc.) consistent between `workflow_navigation` and `normalize_optimization_page` | Yes — candidate/intervention/robustness/validation pages render real authority data | O90 RobustnessSpec authoring dead-end — prior-standing product decision |
| Compare | Yes — measurement comparison context + optimization comparison page | Yes — eligibility-gated verdicts (r9-parity) | — |
| Report / deliverables export | Yes — Deliverables dialog → `deliverables_catalog` | Yes — catalog honestly marks unavailable generators (commissioning.report blocked; BOM/labels only inside handoff package); generators dict covers every command_id emitted | `generators.get(command_id, lambda: None)()` — every catalog id verified present |
| Deep links (`htdt://nav/v1/...`) | Yes — `NavigationResolver` walks preferred-destination + route chain | Yes — unrouteable kinds (OPERATING_PRESET, HEALTH_*, PROJECT_NOTE) resolve to the ACTIVITY destination whose `focus_kinds` explicitly includes them; "unsupported" is honest, never substituted | Was suspected dead-end; verified resolvable end-to-end |
| App destinations (Inbox/Activity/Library/Support) | Yes — all five `ApplicationDestinationId`s have mounted pages | Yes — Activity rows deep-link via `activity_focus` with honest "該当の記録がありません" fallback | — |
| Legacy SPA | Yes — `python -m htdt` (dev-gated, `HTDT_LEGACY_API=1`, loopback) | Yes — `/api/backup`,`/api/restore` → explicit 410 | All ~49 `api()` call sites map to registered FastAPI routes; zero orphaned endpoints; zero UI promises without backend |

## Module-boundary spec sweep

- Units/axes/versioning: display-units/angle conventions centralized in
  `cad_display_units`; URI scheme versioned (`htdt://nav/v1`); store versions
  upgraded via `plan/execute_native_upgrade`. No surviving boundary skew.
- `TODO`/`FIXME`/`NotImplementedError`/`unreachable` in production paths:
  none reachable — the hits that exist are inside test scaffolding or
  documented contract stubs already catalogued in round 6.
- Dead code census: only `workflow_legacy_bridge` was production-dead and
  *not yet removed* — removed this round (below).

## Fixes made

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | `workflow_legacy_bridge.py` (122 lines) was 100% dead production code — zero callers in `backend/src` since the legacy embed path was retired; round 6 explicitly earmarked it "candidate for deletion in a cleanup round", yet round 9 still spent a fix on its `_CONTEXT_DOCK_TITLES` (wasted maintenance on unreachable code) | LOW (cleanup) | DELETED the module + its two test-only call sites (`test_workflow_integration.py` bridge tests, `test_review_round9_i18n.py` dock-title test). Verified zero references in src/, scripts/, installer/, docs/packaging |

## Deferred (named this round, consistent with standing deferrals)

| Item | State | Why deferred |
|---|---|---|
| `cad_prediction_capability` (#727) read model — provider capability report over sealed authorities | Complete module (293 lines) + tests, no production caller; Room prediction UI consumes the *different* `prediction_interpretation` (#469) read model instead | Contract layer staged ahead of its surface; binding it is a product/UI decision, not a wiring bug. Named here because round 6's catch-all ("~120 test-only modules") never listed it at module granularity and it's large enough to matter |
| `cad_prediction_execution` (#478) solver-neutral execution surface (preflight/admission/progress/cache views) | Complete module (501 lines) + tests, no production caller; Room prediction runs on `NativeWorkerPool`/`PredictionJobGuard` directly | Same class: parallel authority staged for "batch/optimization consumers later". Same product decision as #727 |
| `native_accessibility` (#731) contract | Contract-only (round 6 table) | Dedicated widget sweep still a separate pass |
| Standing deferrals (O90 RobustnessSpec authoring, quality-report/calibration severed legs, ~30 Qt modules untested, UI-thread-sync journeys) | Unchanged | Prior rounds' dispositions still hold |

## Tests

`backend/tests/test_workflow_integration.py` (2 remaining navigation tests),
`test_review_round9_i18n.py` (remaining i18n tests), `test_workflow_shell.py`
— 18 passed, `QT_QPA_PLATFORM=offscreen`. Import smoke:
`htdt.workflow_application`, `htdt.native_cad`, `htdt.workflow_shell` import
clean with the module removed.
`scripts/golden_path_preflight.py` run end-to-end on a fresh temp data root:
all stages PASS (project create → room author → equipment bind → candidate
search → development prediction → proposal/revision → fixture measurement →
authority audit → persistence checkpoint → backup/restore/reopen → blocker
paths → final non-claims).
