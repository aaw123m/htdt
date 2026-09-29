# Round 19 — convergence sweep 2 (fresh eyes)

Scope: a second, independent convergence pass over `main` (4f5135c4) after
the r18 sweep merged. The instruction was deliberately adversarial to r18:
approach as a *different* reviewer, prefer surfaces the codebase-history
docs suggest are least-covered, and hunt for THIRD instances of patterns
that r18 (and earlier rounds) fixed only at individual sites — sibling
exception types escaping `except ValueError` contracts, sibling
`_disposed`/`_closed` launch guards, sibling `ORDER BY`
non-determinism, sibling stale-snapshot windows — plus re-verifying the
r18 deferred list for anything that became quick-fixable. Verified locally
(Windows, `QT_QPA_PLATFORM=offscreen`, `pytest backend/tests -q -n 4`,
Python 3.12.10 NuGet). Branch `devin/rev19-sweep2`.

**Verdict: CONVERGED after this diff.** One residual family was still
partially open (timestamp-only `ORDER BY` stragglers in surfaces r18 did
not reach), plus one more unclosed-handle sibling and the r18-deferred
Windows path-separator item — which turned out to be quick-fixable once
its stored contract was read correctly. No other defensible findings:
every audited surface is either already uniform or documents a deliberate
design-level deferral.

## Sweep ledger

| Sweep | Result |
|-------|--------|
| `ORDER BY` audit, codebase-wide | Scripted audit of every `ORDER BY` clause: flagged each clause whose key list contains no unique column, then hand-triaged. The r18 sweep fixed 3 sites in `capture_receiver`/`cad_ambient_noise`/`cad_project_template_repository`; this pass found **14 more stragglers** — the entire legacy dev store (`htdt/database.py`, 9 listings: `list_projects`, `list_sessions`, `list_constraint_sets` ×2, `list_search_specs` ×2, `list_measurements`, `list_attachments`, `list_comparisons`), the capture-ingestion registry (`capture_ingestion_transaction.py`, 4 sites: migration-time registry rebuild, `list_registered_revisions`, `list_revision_conflicts`, `list_capture_bundles`), the tombstone read-back in `project_lifecycle.py`, and the keyed editor-payload stores in `cad_repository.py._payloads`. Each now ends in a unique-key tiebreak (`id`/PK/`rowid`), matching the convention everywhere else. Clauses left alone all resolve on a UNIQUE-constrained key (`contexts.revision_number`, `sample_index`, `placement_version`, `revision`) or sort singleton rows |
| `except ValueError` contract escapes — beyond `OverflowError` | r18 closed the `10.0 **` `OverflowError` family codebase-wide. The same hole exists for any *non-*`ValueError` raised on data paths: audited `math.exp`/`log10`/`log` on finite-only inputs (all either guarded `<= 0`/`energy <= 0`/`Field(gt=0)` or clamped), `ZeroDivisionError` sites (all denominators `Field(gt=0)`-validated or `if x:`-guarded), `StopIteration` on `next()` (defaults or guards present), `KeyError`/`IndexError` on parsed payloads (all in `except (KeyError, ...)`-type mappers or inside validation that converts to `ValueError`). No escapes found — the family is converged |
| `_disposed`/`_closed` launch guards — third sweep | Every `pool.start`/`worker.start`/`timer.start` call site audited; all remaining `.start()` paths either carry the guard r17/r18 established or are constructor/setup-time (not dispose-reachable). Converged |
| Unclosed resource handles — third instance | `capture_bundle.validate_bundle` was the third capture-bundle open path that built a `ZipSource` and never closed its `ZipFile` — `ZipSource.__init__` itself and `capture_reference` already closed explicitly. On CPython the handle outlives the call until refcount GC; on a `CaptureBundleError` mid-validation the traceback pins the frame and the handle survives to the caller — on Windows the archive then cannot be moved/deleted. Fixed: `try/finally` close of `source.zf` when the source is a `ZipSource` |
| Stale-snapshot / TOCTOU windows | Re-sampled the r17-concurrency surface: `job_guard.can_apply` token binding, `accept_results` hash binding, deletion-plan fingerprints re-checked under the write lock, supersede-time predecessor ordering via parsed `_instant()` (not string) comparisons, concurrency checks on conflict retry. No unclosed windows |
| Determinism miscellany | No builtin `hash()` calls in `src`; filesystem enumerations are aggregate-only or `sorted()`; `QTimer.singleShot` calls all carry receiver context; `datetime` comparisons parse through `fromisoformat`/`_instant`, never raw-string compares; f-string SQL interpolations restricted to the `_PAYLOAD_TABLES` whitelist; `dict.get` chains only (no `requests.get` HTTP behind the suspicious name) |
| r18 deferred list re-verification | **Resolved (quick-fixable):** the "Windows path-separator normalization" item was deferred as needing "a deliberate normalization decision" — but the decision is already made: `safe_managed_relative_path` requires `PurePosixPath` on read and every write site stores `.as_posix()`. Only three *test expectations* disagreed (`str(Path('measurement-assets') / digest)` → backslashes on Windows): `test_persisted_dataset_reopens_exact_source_bytes`, `test_concurrent_same_digest_distinct_measurements_share_single_asset`, `test_imported_excitation_persists_reopens_and_replays` — all confirmed RED on clean `main`, now assert `.as_posix()`. **Still deferred (correctly, design-level):** ActivityCenter `request_cancel` dead capability, scene-edit granularity on the activity center, `matrix_presentation()` snapshot blind spot, `retry()` pin inheritance, true-async import/cancel affordance, unbounded `sample_rate_hz`/venue-dims provenance fields, MAX_PATH host limit, junction overcount (conservative), `progress_sink` lifecycle, `localization.format_datetime` dead code |

## Fixed this sweep

| File | Change |
|------|--------|
| `database.py` | 9 listings gain `id DESC` last key: `list_projects`, `list_sessions` (after `COALESCE(started_at, created_at), created_at`), `list_constraint_sets` (both variants), `list_search_specs` (both), `list_measurements` (`m.id`), `list_attachments` (`l.id`), `list_comparisons` |
| `capture_ingestion_transaction.py` | `_migrate_revision_registry` `ORDER BY recorded_at_utc, lineage_digest` (the *legacy* runs table's PK — it predates the `ingestion_run_id` rebuild, which runs after this step); `list_registered_revisions` `..., capture_revision_id`; `list_revision_conflicts` `..., rowid` (composite PK `(revision_id, detail)` — rowid is insertion chronology, the contract the callers want); `list_capture_bundles` `..., bundle_digest` |
| `project_lifecycle.py` | `delete_project` tombstone read-back `ORDER BY deleted_at_utc DESC, tombstone_id DESC LIMIT 1` — matches the retention prune's ordering; a same-instant earlier tombstone for the project can no longer shadow the just-written row |
| `cad_repository.py` | `_payloads` keyed stores `ORDER BY updated_at_utc, {key_column}` — `named_views`/`underlays`/`seating_specs` stable on write-timestamp ties |
| `capture_bundle.py` | `validate_bundle` closes the `ZipSource` archive in `finally` |
| `test_cad_directivity_source_assets.py`, `test_cad_measurement_asset_atomicity.py`, `test_cad_wave_excitation_evidence.py` | Stored-path expectations `str(Path(...) / digest)` → `(Path(...) / digest).as_posix()` — POSIX is the stored contract; the tests were the only Windows-broken part |
| `backend/tests/test_review_round19_convergence.py` | 13 regression tests: 7 dev-store listings, 3 capture-registry listings, 1 keyed payload store, 1 tombstone read-back (same-instant stale-tombstone shadowing via `_utc_now` pin), 1 archive-handle close; all verified green on branch |

## Still deferred (correctly — design-level, none small)

- ActivityCenter `request_cancel` unwired (dead capability, no UI lie);
  scene-edit granularity on the activity center; `matrix_presentation()`
  snapshot blind spot; `retry()` pin inheritance — all need product/
  wiring decisions.
- True-async import with cancel affordance — no cancellation seam; moves
  the dialog flow off-thread.
- Unbounded provenance fields (`sample_rate_hz`, venue dims) — authority
  records what the operator claims by design.
- MAX_PATH (>260) — host-level; junction overcount in
  `native_upgrade._directory_size` — benign over-estimate.
- `progress_sink` ownership lifecycle; `localization.format_datetime`
  dead code — both documented API-decision items.

## Suite result

Scoped run on touched modules' suites + the new regression file:
13/13 new tests pass; the three `.as_posix()` expectation fixes are green
(RED on clean `main`); ordering tests insert the lexically-smaller key
first so insertion order disagrees with the asserted key order (RED on
the pre-tiebreak SQL). Full-suite background run on the same tree shows
only preexisting failures documented in earlier rounds.
