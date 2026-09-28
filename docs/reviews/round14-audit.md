# Round 14 — audit-trail & authority-record truth

Scope: every mechanism that records *what happened* — `ActivityCenter` operation entries +
`activity_history.json`, `native_authority_audit` replay probes, `evidence_state` /
`evidence_provenance` / `evidence_transitions` rows, `scene_revisions`/`supersessions` chain,
`revision_explanations`, capture-inbox disposition + promotion + supersession records and their
projection into `CadProjectActivityService.events()`, `CommandHistory` undo labels, launch-intent
queue files, REW import provenance, report verdict persistence, and the R130/R170 evidence-promotion
paths. For each: write a trail, read it back, assert on CONTENT — right actor, right inputs, right
outcome, honest order, no silent gaps. Verification is Qt-offscreen (`QT_QPA_PLATFORM=offscreen`)
under Python 3.12 + pytest. Branch `devin/rev14-audit`.

## The trail-writer contract (as verified)

The codebase's audit model splits into three layers:

1. **Durable rows** — `evidence_state` (append-only by `evidence_id`), `evidence_provenance`
   (per ref revision), `evidence_transitions`, `scene_revisions` + `supersessions`,
   `capture_inbox_items`/`capture_inbox_promotions`/`capture_inbox_supersessions`,
   `activity_history.json`, launch-intent queue files.
2. **Hash-verified reads** — `CadRepository._row_to_revision` recomputes `scene_content_hash`
   on every load and rejects a hand-edited `content_json`; evidence rows round-trip through
   `model_validate`; `dataset_row_sha256` is derived from the actual stored blobs.
3. **Projected views** — `CadProjectActivityService.events()` (timeline), `ActivityCenter`
   history load, `native_authority_audit` coverage probes. Projections are where this round
   found the lies: rows were stored fine, then re-ordered or omitted on the way to the screen.

## Trail writer → verified

| Writer | What it records | Read-back check | Verdict |
|---|---|---|---|
| `ActivityCenter.submit`/`complete`/`fail`/`block` + `persist_history`/`load_history` | Operation lifecycle rows in `activity_history.json` | Persist → `load_history` → assert state/result/actor fields | **Fixed**: one corrupt JSON row raised `ValidationError` and bricked the entire history load — `workflow_application.py` builds the diagnostics `operation_failures` map from it, so one bad row killed all failure reporting. Now `_load_operations` validates per row, logs `dropping invalid activity record`, and keeps the good rows. |
| `CadProjectActivityService.events()` ordering | Timeline sort of heterogeneous events | Insert events whose `occurred_at_utc` mixes `...Z` and `...+00:00` forms | **Fixed**: sort was raw-lexical, so `'...T12:00:00Z'` sorts *before* `'...T12:00:00+00:00'` even when the Z row is later — a dishonest chronology under mixed writers (`activity_center` emits `Z`, most others emit `+00:00`). Now sorted by a normalized UTC key; unparseable timestamps sink to the end instead of corrupting order. |
| `CadProjectActivityService._capture_events` | Capture lifecycle on the timeline | Stage → promote → supersede → defer → read event kinds/times | **Fixed**: only `capture_staged`/`capture_rejected` (+ a *current-state* promoted event derived from disposition) were projected. Supersession and deferral — real, persisted records — never appeared. Now emits `capture_promoted` per promotion record (at `promoted_at_utc`), `capture_superseded` per supersession record (at `created_at_utc`, so the replacement stays visible after the item's disposition moves on), and `capture_deferred` at `disposition_at_utc`. |
| `capture_inbox.resume` / `_record_outcome` | `disposition_at_utc` on pending/deferred/rejected rows | Resume a deferred item; re-dispose a pending one; read the stamp | **Fixed**: `resume()` stamped `disposition_at_utc = now` on an item whose disposition was `pending` — claiming a disposal that never happened. `_record_outcome` stamped `now` even when the disposition didn't change. Both now keep `disposition_at_utc` null/unset until a real disposal transition occurs. |
| `promote_hybrid_provider_evidence` (R170B) | `validation_authority_ref` / `production_adoption_authority_ref` on `cad_hybrid_prediction_providers` | Promote with fabricated refs (shape-valid, hash-random) | **Fixed**: R130's builder resolves every ref through `_external_payload` (raises on unavailable/mismatched authority); R170B validated *shape* but never resolved the payload — a provider could be promoted on authority refs pointing at nothing. `promote_hybrid_provider_evidence` and `CadHybridPredictionProviderRepository` now take a required `external_payload_resolver`, resolve refs after `model_validate` (so existing structural rejects keep their messages), and re-resolve on every `_validate` — a row whose refs stop resolving is rejected on reopen. |
| `DataManagementController._complete_success` | `result_summary` + `operation_failed`/`completed` signals on restore/backup | Restore whose lifecycle `reload`/`return_to_editor` throws | **Fixed**: a lifecycle error was caught, the op marked `COMPLETED`, and the recorded summary claimed `復元が完了しました` / backup complete — while the user still saw the failure dialog. Now the op completes honestly: summary becomes `データは復元されましたが、画面の再読み込みに失敗しました` (or the backup equivalent) and `operation_failed` emits the same message. `COMPLETED` kept because the data *was* restored — failing it would lie the other way. |
| `CommandHistory.undo_label`/`entries()` | Undo stack labels | Push/edit/undo → read labels | Honest — labels are computed live via `describe_command` from the actual command objects; `push` applies *before* recording and `undo` reverts *before* decrementing the index, so a failed apply/revert is never recorded as applied. |
| `launch_intents.forward/drain/complete_queued_intent` | One queue file per intent, `incoming/`→`done/`/`failed/`/`dead/` | Drop two intents, drain, retire | Honest — the file *is* the record (nothing separate to disagree with); filename is `time_ns`-prefixed + per-process sequence so cross-process FIFO order is true; a dispatch that never completes stays in `incoming/` for redelivery; malformed/stale drops go to `dead/` rather than vanishing. |
| `database.import_rew_api_snapshot` provenance | `metadata_json` (adapter_version, requested-vs-returned params, `raw_asset_sha256`, warnings) + `dataset_row_sha256` over the actual blobs | Import → read metadata → compare against the raw asset | Honest — records requested *and* returned parameters (catches adapter lies), the real stored-asset hash, and per-row warnings (`phase_all_zero_unverified`). |
| `scene_revisions` + `supersessions` + `revision_explanations` | Immutable revision chain with `scene_content_hash` + `authority_descriptor` | Save → hand-edit `content_json` → load | Honest — every row is hash-verified on read (`_row_to_revision` recomputes vs stored hash); supersession is a real table joined both directions (`superseded_by`/`supersedes`); explanations carry per-edit authority. Tamper detection is strong here. |
| `evidence_state` / `evidence_provenance` / `evidence_transitions` | Append-only evidence lifecycle keyed by `evidence_id` | Promote candidate→validated→production → query rows | Honest — transitions are additive rows, not in-place edits; latest state derived by max `transitioned_at_utc` per `evidence_id`; provenance rows carry `revision` + `recorded_at_utc` so each ref bump is its own record. No silent backwards path exists in the writer. |
| `evaluate_operation_support` / report verdict persistence | `VERDICT_*` + `statement_status` + `requires_updated_truth` fields | Run report with a missing artifact | Honest — fail-closed: missing inputs yield `unsupported` + `requires_updated_truth`, never a silently regenerated verdict. |
| `native_authority_audit` replay probes | Coverage report: `REPLAYED`/`EVIDENCE_BYTES`/`STRUCTURAL_ONLY` per table | — (verifier, not a writer) | Honest, with a caveat: `cad_hybrid_prediction_providers` is listed `STRUCTURAL_ONLY` ("canonical replay path pending") — the audit itself acknowledged there was no way to re-verify rows. This round's resolver fix makes real replay possible; wiring the probe is follow-up. |
| `ActivityCenter` retention (`_append_to_history`, limit 200) | Bounded in-memory + JSON history | — | Honest — trim only drops the oldest *terminal* records and bounds the file; no silent mid-life edits. |

## What "dishonest" looked like, concretely

- **Ordering lie**: `_capture_staged_event` stamps `staged_at_utc` (a `+00:00` form) while
  `_ActiveOperation`/`activity_center` stamps `Z`. Two capture events one second apart could
  render reversed purely from format choice — the timeline's job is sequence truth and it failed it.
- **Completeness lie**: a superseded capture kept a single "promoted" timeline entry frozen at
  promotion time while its true state (replaced by newer delivery) sat in
  `capture_inbox_supersessions`, invisible.
- **Attribution lie**: R170B promotion wrote a `production` row citing authority refs that had
  never resolved — indistinguishable on disk from a row backed by real authority.
- **Outcome lie**: restore + failed reload recorded `COMPLETED: 復元が完了しました`.

## Fixes (small diffs)

| # | File | Change |
|---|---|---|
| F1 | `cad_hybrid_prediction_provider.py` + `test_cad_hybrid_prediction_provider.py` | `external_payload_resolver` required on `promote_hybrid_provider_evidence` and the repository (resolved post-`model_validate`, re-resolved on load); tests mint real refs via an `_authority_ref` helper + payload map. |
| F2 | `cad_project_activity.py` + `capture_inbox.py` | New `capture_superseded`/`capture_deferred` event kinds; `_capture_events` emits per-record events; public `supersessions_for(lineage_digest)` added alongside `promotions_for`. |
| F3 | `cad_project_activity.py` | `_event_sort_key` normalizes `Z`/`+00:00`/naive to a single UTC sort key; unparseable stamps sort last. |
| F4 | `data_management.py` | Lifecycle error after a successful backup/restore records an honest result_summary and emits it on `operation_failed`. |
| F5 | `activity_center.py` | `load_history`/`load_active_operations` drop + warn on invalid rows instead of failing the whole load. |
| F6 | `capture_inbox.py` | `resume()` leaves `disposition_at_utc` null; `_record_outcome` only stamps when the disposition actually changes. |

All six were first reproduced as failing tests in `test_round14_audit_trail.py`
(timeline order, disposition/supersession projection, unresolving R170B refs, restore outcome,
corrupt-row history load) — each failed for the stated reason before the fix.

## Deferred / known gaps

- **Rejection-history loss**: a rejected → resumed → promoted item shows staged/promoted but its
  rejection vanishes from the timeline — `capture_inbox` stores only the *current* disposition, no
  transition table. Needs a `capture_disposition_transitions` table; too large for this round.
- **Blocked promotion records**: `capture_inbox_promotions` rows with `outcome='blocked'`
  (`record_blocked` — requires a reason) persist but never project to the timeline; the
  projection deliberately skips them (`record.outcome != 'promoted' → continue`) rather than
  mislabeling them as promotions. Rows are honest; visibility is the gap.
- **`promoted_from_provider_ref` chain**: the lineage link between a promoted provider and its
  source ref is not re-verified on reopen — the row's other refs now resolve, but the link itself
  is asserted at write time only.
- **Audit coverage upgrade**: `cad_hybrid_prediction_providers` can now be `REPLAYED` (resolver
  exists); `native_authority_audit` needs a deployment-time resolver it doesn't currently have.
- **Undo labels are presentation-computed**, not persisted — honest by construction but means the
  "trail" lives only for the session (no durable undo log). Accepted design, noted.

## Test evidence

- New: `backend/tests/test_round14_audit_trail.py` — 7 tests, all failing pre-fix, all passing post.
- Touched: `backend/tests/test_cad_hybrid_prediction_provider.py` — `_authority_ref` helper,
  `external_payloads` dict in `_build_bundle`, all promote call sites updated (required kwarg).
- Regression: `test_cad_project_activity.py`, `test_capture_inbox.py`, `test_activity_center.py`,
  `test_data_management.py`, `test_data_management_ui.py`, `test_review_round6_features.py`,
  `test_native_upgrade.py`, `test_authority_audit_coverage.py`,
  `test_authority_lifecycle_integrity.py` — green under `-n` parallelism.
