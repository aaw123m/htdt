# Round 22 — full-suite verification + targeted residual hunt

Scope: REV22-SWEEP3. Two jobs: (a) run the **complete** backend suite end-to-end
on current main (`34e6d8ff`, post REV21-EXPORT) and honestly classify every
failure; (b) audit the surfaces least touched by rounds 1–21 — computed by
counting per-file mentions across all `docs/reviews/round*.md` (218 of the
`htdt/` source files have **zero** prior review mentions; 223 have exactly
one) — plus a cross-check of merged PRs #397–#420. Branch
`devin/rev22-sweep3`. All verification local: Python 3.12.10 (NuGet, at
`C:/devin/python`), `QT_QPA_PLATFORM=offscreen`, `TMPDIR=/c/t`,
`PYTHONIOENCODING=utf-8`, `pytest -p pydantic -n 4 -p no:warnings`.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `decode_mission_package` revalidated `package_sha256` + dependency `payload_sha256`s but never re-derived `plan_sha256`/`mission_sha256` from embedded content nor any content-addressed id (`plan_id`, `mission_id`, `package_id`, `dependency_id`) — tampered plan/baseline/lineage/ids decoded as valid | MED integrity (fail-open decode) | FIXED — full chain revalidated; shared canonical projections extracted |
| 2 | `capture_authoring.authoring_inputs` declares `batch_conflicts` but never appends — `CaptureAuthoringBatch.conflicts` is always `()` | LOW | NOTED — per-record conflicts are populated correctly; the batch-level field is honest-empty (no batch-level conflict detector exists). Product call whether to populate or drop |
| 3 | `scripts/pytest_shard.py`, `select_affected_tests.py`, `test_durations.json` | — | ALREADY ADJUDICATED round3 — orphaned CI tooling, keep-or-delete product decision. Noted again only to confirm no new defect: the sharder emits `backend/tests/...` nodeids while a `pytest backend/tests` run uses `tests/...` nodeids (rootdir=`backend/`), so its `deselect:` lines would not match — moot while nothing invokes it |
| 4 | Packaging/installer internals (`installer/HTDT.iss`, `build-installer.ps1`, `Get-HtdtVersion.ps1`, `prepare_branding_assets.py`, `build-native.ps1`) | — | VERIFIED OK — AppId/version derivation consistent, no silent failures, branding assets validated |
| 5 | `scripts/` validation fleet (`validate_*_windows.py`, `golden_path_preflight.py`, `check_dependency_lock.py`) | — | VERIFIED OK — `validate_update_windows.py` documents the non-scriptable Inno subset instead of silently skipping; `check_dependency_lock.py` run live → lock consistent and hash-pinned |
| 6 | `benchmarks/` (28 JSON evidence/manifest files) | — | VERIFIED OK — all parse; data-only surface |
| 7 | CLI entrypoints (`__main__.py`, `scripts/native_entry.py`) | — | VERIFIED OK — `InstanceLock` byte-0 + JSON metadata, `probe_htdt` bounded read, honest errors |
| 8 | Secondary services sample: `capture_import`, `capture_receiver` mission endpoints, `cad_roomsim_batch_runner`, `native_accessibility`, `measurement_target_service`, `capture_compatibility`, `cad_measurement_runner`, `optimization_search_domain`, `cad_model_validation_service`, `analysis_markers`, `project_performance`, `overview_readiness`, `cad_hybrid_acoustic_result`, `capture_authoring` | — | VERIFIED OK — stale-snapshot guards, honest errors, bounded reads all present; every `except` site sampled is legitimately best-effort (instrumentation) or re-raises |
| 9 | Merged PRs #397–#420 (23 merge diffs, `eefdcdcf`..`34e6d8ff`) | — | VERIFIED OK — diffs uniformly defensive (honest-error raises, path-safety guards); two contract drifts investigated and resolved as non-issues (`cell_transfers` 4-tuple = test-only caller; `merge_history_since` epoch = all callers migrated) |

## 1 — Mission package decode: missing inner hash links (fixed)

`decode_mission_package` previously verified only the outer envelope:
`package_sha256` over `{mission_sha256, dependency descriptors}` and each
dependency's `payload_sha256` over its embedded bytes. The two inner links
of the integrity chain were trusted as recorded:

* `plan_sha256` — sha256 of the canonical `{project, room_name, purpose,
  tasks}` plan projection — never recomputed;
* `mission_sha256` — sha256 of `{plan_sha256, baseline, issued_from,
  supersedes_mission_id}` — never recomputed;
* and the content-addressed identities (`plan_id`, `mission_id`,
  `package_id`, `dependency_id`) — never re-derived.

Consequence: a mission package whose `mission.plan` tasks, `baseline`
(which pins `scene_content_sha256` — the issuing design authority the field
work is judged against), `issued_from`, `supersedes_mission_id`, or any id
was altered while the recorded hashes stayed stale decoded cleanly —
fail-open where the function's docstring promises "malformed input fails
closed".

Fix: decode now recomputes both hashes and re-derives all four
content-addressed identities, raising `CaptureMissionError` with a precise
message per link. The three canonical projections were extracted into
shared helpers (`_task_plan_projection`, `_mission_projection`,
`_package_projection`) used by all three producers and the decoder — the
inline duplicates were themselves a future drift risk. Honest packages
(including repair missions) round-trip unchanged.

Regression: `test_decode_revalidates_plan_and_mission_hashes` (7 tamper
vectors: plan fields, plan hash, baseline fields, issued_from,
supersedes_mission_id) and `test_decode_revalidates_derived_identities`
(4 identity vectors) in `backend/tests/test_capture_mission.py`.

## Full-suite result

Two complete runs of `pytest -p pydantic backend/tests -q -n 4 -p
no:warnings` under `TMPDIR=/c/t PYTHONIOENCODING=utf-8
QT_QPA_PLATFORM=offscreen` were executed end-to-end (the second with
`-rs` for skip classification). Both exited 0.

**6881 tests collected across 568 files: 6686 passed, 195 skipped,
0 failed.** No `lastfailed` cache entries — zero real failures to
classify.

Skip classification (195 — none are masked failures):

| Count | Site | Class |
|-------|------|-------|
| 1 | `test_cad_geometric_acoustics_adapter.py:2282` | env — `pyroomacoustics` not installed |
| 1 | `test_built_frontend.py:15` | env — `frontend/dist` not built on this mirror |
| 1 | `test_dependency_lock.py:122` | env — no `.github/workflows` on this mirror (expected: NO GITHUB ACTIONS) |
| 1 | `test_import_truth_matrix.py:1285` | env — Windows refused >260-char path creation |
| 191 | `test_round13_hash_sweep.py` (143 "builder parameters not auto-synthesizable", 6 "builder not resolvable", 42 targeted unsolved-field / validation-rejection skips) | by-design — hypothesis-style model sweep; skipped builders are ones the sweep cannot honestly auto-synthesize, reported rather than forced |

Honest note on provenance: run 1 exercised the working tree at `34e6d8ff`;
the finding-1 fix landed mid-run, so `capture_mission` was tested in its
post-fix shape (the change is additive validation — it can only convert a
previously-accepted malformed input into a `CaptureMissionError`). Run 2
covered the fully post-fix tree.

## Residual-hunt method

Per-file mention counts across all `docs/reviews/round*.md` were computed;
the audit concentrated on `htdt/` files with 0–1 mentions plus the
non-`htdt/` surfaces called out in the task (scripts/, benchmarks/,
packaging, CLI entrypoints). The defect shapes hunted were the ones
earlier rounds kept finding: dead code paths, unhandled exceptions,
stale-snapshot windows, honest-error gaps, contract drift. Beyond finding
1, the sweep surfaced only the two observations above — the codebase reads
as genuinely converged, not just untouched: guards are uniform, error
paths honest, and even orphaned tooling was already adjudicated.

## Deferred

* `CaptureAuthoringBatch.conflicts` always empty (finding 2) — populate
  or drop; product decision.
* Orphaned CI tooling (finding 3) — round3 keep-or-delete decision still
  open; if kept, the sharder's nodeid prefix (`backend/tests/` vs the
  `tests/` pytest emits under rootdir `backend/`) should be realigned.
* Stray `verify_rev21.py`/`verify_rev21b.py` at repo root — ad-hoc
  verification harnesses already acknowledged in round21 docs.
