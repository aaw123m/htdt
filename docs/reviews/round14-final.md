# Round 14 — convergence sweep (final)

Scope: fresh-eyes audit of `main` after ~24 deep-review dimensions landed in
round 14 (~300 fixes across 27 round-14 docs plus 9 round-13 docs). Not another
niche dimension — this sweep asks two questions: (1) did the round leave the
codebase in a worse state, and (2) does anything *safely actionable* remain.
Verified locally only (Windows, `QT_QPA_PLATFORM=offscreen`,
`pytest backend/tests -q -n 4`, Python 3.12.10). Branch `devin/rev14-final`.

**Verdict: CONVERGED** — three real but small inconsistencies found and fixed
(canonicalize gap in `cad_field_session`, one tautological test assertion, the
round-13-workers D5 deferred `_disposed` guards). Everything else sampled clean;
all remaining deferreds are documented design-level items needing their own
follow-up rounds.

## Sweep ledger

| Sweep | Result |
|-------|--------|
| Review-doc ledger (r13 + r14) | 36 docs read; deferred items triaged — most are design-level (need contracts/tables/product decisions), one (workers D5) was a small diff whose prerequisite convention already landed → fixed |
| Debt markers (TODO/FIXME/XXX/HACK, skip/xfail/expectedFailure) | Skip-guard count 34 → 40; **all +6 are file-availability guards in `test_packaging_surface.py`** (test skips when packaged layout absent — legitimate). Zero xfail/expectedFailure. No new code TODO/FIXME introduced by the round |
| Test integrity (~20 r14 tests sampled) | One tautology: `test_ordered_samples_cache_and_interpolation` compared `_interpolated_magnitude` output to itself → **fixed** with a real reference implementation mirroring the complex-scalar twin test. Other sampled tests assert real behavior |
| Convention consistency — canonical-json hashing | ~70 unwrapped `model_construct` sites scanned; an AST sweep of float args inlined into payloads found **one genuinely exposed site**: `record_field_evidence` builds `FieldEvidenceRecord` via raw `model_construct` and `identity_payload()` inlines `self.value_numeric` — `value_numeric=5` (int) hashed differently than the validated `5.0` → `ValidationError('field evidence semantic hash mismatch')`. **Fixed** via `canonicalize_payload` + regression test. Sites hashing via `model_dump(mode='json')` are safe (dump coerces) |
| Convention consistency — JP error sentences / fail-closed | Sampled error mappers and boundary raise sites adopt the sentence-case JP + refuse-loudly convention; no stragglers found |
| Deferred feasibility re-check | **round13-workers D5 fixed** (see below). Still deferred (design-level, not small diffs): D1 wedged-worker veto, D2 uncancellable data-ops, D3 flag-dropping jobs, D6 deliberate lingering threads; r14: per-`Device:` bucketing (calib), capture-aware import-as-copy (bundle), `capture_disposition_transitions` table (audit), `response_model`/pagination (api), settings-dialog lazification (mem), head-read FK check (stor), `_REVALIDATORS` lane growth (update), report legend overlap (print), BackupError subclass (errmsg), two preexisting flakes (errmsg doc, env/toolchain) |
| Dead code after refactors | Vulture scan: hits are pre-existing unused params and alias/re-export import conventions — no residue from superseded approaches |
| Golden journey (fresh eyes) | launch → all 4 workspaces + 5 app destinations mount offscreen; 75 commands registered, none unavailable-without-reason |

## Fixed this sweep

| File | Change |
|------|--------|
| `backend/src/htdt/cad_field_session.py` | `record_field_evidence` now `canonicalize_payload`s before the provisional `model_construct`, so int-for-float payloads hash identically to validated models (sibling `canonicalize → hash → validate` contract) |
| `backend/src/htdt/joint_optimization_panel.py` | `_disposed` flag: init `False`, set `True` in `dispose()` before pool shutdown; guards on `_on_execution_completed` + `_on_execution_progress` (round13-workers D5) |
| `backend/src/htdt/system_expansion_widgets.py` | Same contract on `SystemExpansionRoomPanel` (`_proposal_completed`, `_refresh_run_state` on_finished path) and `SystemExpansionOptimizePanel` (`_evaluation_completed`, `_refresh_run_state`) |
| `backend/src/htdt/robustness_authoring_panel.py` | Same contract: `dispose()` sets flag first; `_run_completed` + `_refresh_run_state` guarded |
| `backend/tests/test_round14_cpu.py` | Replaced self-comparison with independent `scalar_magnitude` reference (parity with `scalar_complex` sibling) |
| `backend/tests/test_cad_field_session.py` | `test_evidence_numeric_accepts_int_for_float_field` — int `value_numeric=5` validates to `5.0` (was raising semantic-hash mismatch) |
| `backend/tests/test_joint_optimization_panel.py` | `test_completion_after_dispose_is_a_no_op` — post-dispose completion/progress don't touch widgets |
| `backend/tests/test_qt_smoke_round11.py` | `test_system_expansion_panels_swallow_post_dispose_callbacks` — both panels |
| `backend/tests/test_round10_convergence.py` | `test_authoring_panel_swallows_post_dispose_callbacks` |

## Still deferred (carried forward — design-level, none small)

- **workers D1/D2/D3/D6**: wedged-worker permanent veto, uncancellable
  data-management ops, flag-dropping generic jobs, deliberate lingering
  threads — all UI-contract or plumbing changes, not convergence diffs.
- **`capture_disposition_transitions` table** (r14-audit): rejected→resumed
  items lose rejection history — needs schema migration.
- **Capture-aware import-as-copy** (r14-bundle): fails closed today; honest
  refuse, needs remap semantics.
- **Per-`Device:` bucketing in E-APO calibration** (r14-calib): needs a
  device-keyed bucket model.
- **API `response_model`/pagination/timestamp validation** (r14-api): contract
  additions, same deferral as round 4.
- **Settings-dialog lazy composition** (r14-mem): lifecycle change.
- **`scene_document_heads` FK check on read path** (r14-stor): wider blast
  radius than diff budget.
- **`BackupError` subclass** (r14-errmsg): ~30 mechanical raise-site swaps;
  standalone follow-up.
- **Two preexisting flakes** (r14-errmsg): `test_native_diagnostics` import-
  order flake and `test_cad_hybrid_prediction_provider` xdist fixture race —
  both reproduce on clean `main`, both env/harness-level.

## Suite result

Full suite on this branch (`pytest backend/tests -q -n 4`, offscreen):
**6317 passed, 195 skipped, 0 failed, 0 errors** (exit 0). Skips are the
pre-existing file-availability/platform guards counted in the debt sweep —
none new. The two r14-errmsg preexisting flakes did not fire on this run
(nondeterministic; documented above, unchanged by this diff).
