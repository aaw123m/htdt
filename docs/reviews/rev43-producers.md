# REV43 — producers/derivers audit + remaining manual debt

Scope: every `build_*`/`derive_*`/`save_*`/producer-style definition in
`backend/src/htdt` whose only call sites are tests (the PR #521
quality-report pattern), honesty of producers that DO run, and the
manual-debt recheck for the file-watch lanes (watch-dir edge cases,
seen-set eviction vs re-drop, baseline semantics on preference change).
Branch `devin/1791084720-rev43-producers`, rebased onto `844ec0cd`
(post-REV43-SEAMS #522 — that sibling landed the bounded route-requeue
and per-directory sentinel on the capture lane; this review's remaining
capture findings are the pieces it missed). Verification is Qt-offscreen
(`QT_QPA_PLATFORM=offscreen`) under Python 3.12.10 plus scoped pytest
(93 tests green; two `test_round8_measurement_journey` failures
reproduce on clean `origin/main` on this box — managed-asset size
mismatch, pre-existing fixture/environment rot, not from this branch).
The new 「ファイルを選択…」 affordance was exercised end-to-end on the
real GUI: pick → bytes persisted (`cad_quality_calibration_files` row +
content-addressed asset) → filename + digest fields filled.

## Method

Name-reference audit across `backend/src` + `scripts/` + `benchmarks`
(excluding def lines): 748 producer/write-style defs, ~230 with zero
production references. Each candidate was then re-checked at the
**artifact** level (does any write path — including internal transaction
helpers — persist the artifact, and does any production surface read it),
because a dead public `save_*` wrapper is benign when an internal
`_save_*_in_transaction` is the real path (e.g. `cad_r140_executor`).

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Mic calibration bytes could never be attached: `save_calibration_file` was dead, the onboarding checklist instructs "バイト列を添付してください" with no affordance, and any typed SHA-256 could never resolve via `validate_calibration_file` — the quality producer's calibration claim could only ever be UNKNOWN | High | FIXED — 「ファイルを選択…」 button on the 割り当て acquisition card retains bytes via `save_calibration_file` and fills filename+SHA-256 |
| 2 | Capture watch-folder gaps REV43-SEAMS (#522) did not cover: a failed first listing still claimed the baseline (next poll mass-stages every pre-existing file); `seen` markers were never evicted (unbounded map + identical-signature re-drop silently ignored); a watch-path preference change kept cumulative markers from another directory | Med data-loss | FIXED — failed listing claims nothing, seen-eviction mirrors `scan_rew_watch_dir`, and the runner re-baselines on path change |
| 3 | REW watch lane had the converse baseline bug: one global `_SCANNED_SENTINEL` meant switching `integrations.rew_watch_dir` to a populated folder staged every pre-existing file there | Med | FIXED — per-directory sentinel + workspace re-baselines its marker maps on path change |
| 4 | Measurement-quality authority families beyond calibration files are never produced: `save_timing_reference`, `save_excitation_asset`, `save_stimulus_profile`, `save_level_calibration`, `save_dataset_level_reference` + their `build_*` constructors are all dead; the producer reads level-reference/calibration and honestly degrades to UNKNOWN, but the SPL-readiness onboarding step can never leave 'manual' and the routing-profile combo is permanently 「（未選択）」-only for real documents | Med | REPORT — needs a registration surface (which authority types get UI, which stay scripted is a product call) |
| 5 | Prediction matrix never creatable: `prediction_matrix_service.create_matrix`/`run_matrix` dead, `cad_prediction_provider.save_provider` dead; the 行列 UI permanently shows 「行列なし」 | Med | REPORT — needs create/run surfaces + provider registration lane |
| 6 | Manual applicability gate unreachable: `build_applicability_attestation`/`save_attestation` dead while `optimization_validation_controller` offers a 手動 mode that demands a "登録済み証明 ID" no production path can mint — selecting 手動 can only fail | Med | REPORT — needs an attestation-registration surface with actor/evidence semantics |
| 7 | `cad_project_activity` advertises event kinds that can never occur: `av_sync_recorded` (both `CadAVSyncRepository` saves dead), `health_baseline_created` (`save_baseline` dead) — permanent-zero feed lanes with live deep-link targets | Low | REPORT — wire the writers or drop the kinds; product call |
| 8 | `workflow_application` collects `list_templates()` for the new-project wizard and `list_measurements` for install-context readers while `save_template`/`save_context`/`save_datum` are dead — permanently empty pickers | Low | REPORT — staged lanes vs missing writers |
| 9 | ~25 further persistence families have dead writers whose only readers are `native_authority_audit`/`cad_authority_registry` (colorimetry, photometric, lighting, site, signal_path, validation_corpus, visual_qa, tactile actuators, data-source source/importer/raw/upstream/review (only `save_decision` is live), gain-stage, layout topology, measurement-pose delta, multifidelity screening/stage/finalization (only `save_plan` is live), operating-preset applied-state/binding, equipment replacement, field-evidence `store_asset`, intervention `record_alternative`, expansion single-speaker/cost-record, acoustic-treatment source-asset + comparison/definition, commissioning tolerance-profile, amplifier limit/impedance/frequency-resolved, evidence-reconciliation subject, library-upgrade, analysis-study, installation-context/datum, AV-sync) | — | STAGED — the audit graph honestly reports their absence; each needs its own lane decision, not a drive-by |
| 10 | Public-wrapper dead code with live internal paths (benign): `cad_r140_executor.save_result` (`_save_result_in_transaction` is the real path), `room_workspace.save_constraints` and ~30 dead workspace setters, `runtime_instance.write_runtime_info` (installed-build marker; native_cad comment documents external producer), `measurement_target_service.create_pattern` | Low | REPORT — cleanup candidates; no data gap |
| 11 | `test_round8_measurement_journey` fails 2 tests on clean `origin/main` on this box (`managed_assets` size mismatch — CRLF/fixture rot) | — | PRE-EXISTING — noted, not caused here |

## Derivation-honesty audit (producers that DO run)

- `cad_measurement_quality_producer` — VERIFIED honest: every catch is
  fail-closed (`unresolved` results recorded, warnings logged); unprovable
  fields stay `None` → honest `UNKNOWN`/`NOT_EVALUATED`; dedupe reuses
  identical observations; `_same_report_epoch` makes re-derivation
  idempotent. The docstring's per-field provenance table matches the code.
- `authority_revalidation` — VERIFIED honest: every rebuild/re-sign failure
  returns `_kept(diagnostic, reason)` with the JA explanation; a re-sealed
  row that fails the canonical read is rolled back byte-for-byte.
- `cad_comparison_semantics`, `room_prediction`, `cad_project_activity` —
  VERIFIED honest: parse failures degrade to empty metadata (never
  fabricated), run errors emit error states, absent families yield no
  events rather than invented ones.

No fabricated PASSes and no silent degradation that converts a failure
into a claimed success were found. The systemic gap is upstream:
authorities the producers consume are never persisted.

## Fixed

### 1 — calibration-file attach affordance (measurement page)

`cad_measurement_quality_repository.save_calibration_file` is the only
writer of `cad_quality_calibration_files`, and `validate_calibration_file`
— the resolver the producer's `_resolve_calibration` calls on the
context-declared hash — fails closed without a retained row. The 割り当て
form collected 校正ファイル名/SHA-256 as free text and the onboarding
checklist told the user to "記録し、バイト列を添付してください" — but
nothing in production could attach bytes, so no declared hash could ever
resolve (the producer's calibration claim was permanently UNKNOWN) and
the instruction named an affordance that did not exist.

`measurement_page_workspace` now has 「ファイルを選択…」 beside the field:
it picks via `file_dialog_memory`, reads through `read_file_bounded`
(`MAX_ATTACHMENT_BYTES`), calls `save_calibration_file`, and fills both
fields with the real filename and the returned digest. Regression tests
in `test_bounded_ingress.py` cover pick-fills-fields + resolvable digest
and cancel-leaves-fields.

### 2/3 — watch-folder contract gaps on both lanes

The capture scanner still claimed its per-root sentinel before reading
the directory and converted `OSError` to an empty scan — a watch dir
that's transiently unreadable on the first poll (network mount not yet
up, removable drive) claimed a baseline it never read, and the next
successful poll treated every pre-existing file as a new drop — exactly
the bulk import the contract forbids. An unreadable listing now returns
early with no mutation.

`seen` markers were never evicted (REV43-SEAMS covered the requeue
cap but not the map growth): an identical-signature re-drop — same
preserved mtime+size, normal for copied exports — silently matched its
stale marker and was never delivered. Eviction now mirrors
`scan_rew_watch_dir`, skipping `\x00scanned:` sentinel keys.

Neither lane re-baselined on a preference-path change. The capture
runner now tracks `_watched_root`: a changed path is a new watch epoch
— maps clear, the new folder baselines, nothing pre-existing stages;
clearing the pref disables, and a configured-but-missing directory keeps
the epoch (drops during the outage still read as new on recovery). The
REW workspace poll applies the same epoch rule to its `_rew_watch_*`
maps, and `scan_rew_watch_dir`'s global `_SCANNED_SENTINEL` became
per-directory (`\x00scanned:{root}`) — previously, switching
`integrations.rew_watch_dir` to a populated folder skipped that
folder's baseline and staged every pre-existing file.

Regression coverage: `test_capture_watch_runner.py` gained
failed-listing/no-baseline, identical-signature re-drop, and
path-change re-baseline tests; `test_rew_auto.py` gained the
per-directory-baseline test; `test_rev42_fullreview.py` was updated for
the per-dir sentinel key.

## Report-first notes

- The quality-authority families (finding 4) have honest degradations
  everywhere — producers report UNKNOWN, onboarding marks steps manual,
  the audit graph shows absence. Nothing is broken, but the SPL-readiness
  step and routing-profile combo are permanently unreachable for real
  users. The right fix is a registration surface per authority type
  (calibration upload done here is the pattern: retain bytes → persist
  authority → reference by hash).
- `measurement_workflow.attach_to_batch_item` writes to
  `cad_measurement_attachments`, a different store — batch attachments
  do NOT satisfy `validate_calibration_file`; the onboarding copy's
  "添付" now means the new pick-button path specifically.
- `runtime_instance.write_runtime_info` is intentionally dead on dev
  builds (installed single-instance forwarder writes it; the
  `previous_session_unexpected_end` reader tolerates absence) — staged,
  not a defect.
