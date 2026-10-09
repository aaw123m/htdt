# Issue #968 — 再測定キュー：品質不良の既存測定から再測定候補を抽出し入力条件を明示

## Scope

A sealed re-measurement queue generated from the sealed quality epoch:
the queue scans every committed measurement of the current scene head,
extracts the rows whose latest sealed `CadMeasurementQualityReport` fails
the declared thresholds (`RETAKE`), and surfaces them as a typed queue
whose per-item required inputs are resolved from the persisted
authorities — never fabricated.

Files:

- `backend/src/htdt/measurement/domain/cad_remeasure_queue.py` — sealed
  `RemeasureQueue` / `RemeasureQueueItem` / `RemeasureSoftItem` /
  `RemeasureSkippedItem` / `RemeasureQueueEvent` models and the
  `build_remeasure_queue` / `build_queue_event` /
  `evaluation_set_sha256` authorities.
- `backend/src/htdt/measurement/persistence/cad_remeasure_queue_repository.py`
  — `cad_remeasure_queues` + `cad_remeasure_queue_events` persistence with
  scene-binding re-validation on every read and terminal-once event
  semantics.
- `backend/src/htdt/measurement/services/cad_remeasure_queue_service.py`
  — generation, lapse detection, dismiss, runner-plan conversion,
  soft re-evaluation.
- `backend/src/htdt/cad_schema_ddl.py` / `cad_schema.py` — schema v117
  (`_migrate_116_to_117`) adding both tables.
- `backend/src/htdt/native_authority_audit.py` /
  `native_row_integrity.py` — probes and row bindings for the new tables.
- `backend/src/htdt/measurement/services/measurement_workflow.py` —
  controller facade (`remeasure_queue` snapshot, `generate_…`,
  `dismiss_…`, `convert_…`, `soft_reevaluate_measurement`).
- `backend/src/htdt/measurement/ui/measurement_page_workspace.py` —
  測定 → 再測定キュー context page (workspace context id `remeasure`,
  registered in `workflow_navigation.CANONICAL_WORKSPACE_CONTEXTS`).

## Vocabulary

| Term | Meaning |
|---|---|
| Evaluation row | `(measurement_id, report_id, report_sha256, report_error)` — one per committed measurement; the queue's freshness pin covers *every* row including report-less and unreadable ones. |
| `evaluation_set_sha256` | `canonical_sha256` over the sorted evaluation rows at generation time. Stored inside the sealed queue payload; any drift lapses the queue. |
| Queue item | A pending re-capture request: verbatim `failed_criteria`, `retake_reasons`, `remeasure_instructions`, `missing_evidence` from `measurement_retake_guidance(report)` plus `RemeasureRequiredInputs` resolved from the sealed context. |
| Soft item | `UNKNOWN` quality whose missing evidence is completable from stored authorities (file re-read / metadata backfill). **Not** re-measurement — routed to `soft_reevaluate` which re-runs the honest producer, never recaptures. |
| `CAPTURE_REQUIRED_EVIDENCE` | `{repeat_measurements}` — evidence that cannot be recovered without another acquisition. `UNKNOWN` verdicts needing it queue only when no same-binding sibling exists. |
| Skipped entry | Every excluded measurement lands here with a stated reason: `passed` (NOT_NEEDED), `no_current_report`, `report_unreadable`, `superseded`, `not_normally_eligible` (detail verbatim), `target_not_in_current_scene`, `undetermined`. |
| Lapse verdict | `status(queue)` → `current` | `lapsed_scene` (head revision/content hash moved) | `lapsed_evaluations` (`evaluation_set_sha256` drift). Dismiss and convert refuse a lapsed queue — re-generate first. |
| Queue event | Sealed `rqev-` row: `dismissed` (requires a non-empty reason) or `converted` (pins the runner plan). An item resolves terminal-once; events for non-pending or foreign items are rejected. |

## Evidence model

- **The queue is sealed.** `queue_sha256` / `queue_id` (`rqueue-<sha[:24]>`)
  hash the full payload — document, scene head pins, counts, every item,
  every soft item, every skipped entry. Identical inputs reproduce the
  identical queue, so regeneration is idempotent and `save_queue` is a
  no-op for the same hash.
- **Scene binding is re-validated on read.** The repository rejects a
  queue whose `document_id` / `scene_revision_id` / `scene_content_hash`
  diverges from the persisted heads — a stale queue lapses honestly
  (`lapsed_scene`) and never executes.
- **Classification never fabricates.** `RETAKE` queues with verbatim
  criteria; `UNKNOWN` becomes a soft item only when all missing evidence
  is in `SOFT_REEVALUABLE_EVIDENCE` and no capture-only evidence applies
  (or a same-binding sibling already exists); anything else is skipped
  with its reason. Unknown quality is never treated as pass.
- **Per-measurement isolation.** The service resolves each record's
  effective binding inside try/except: one unreadable report lands in
  `skipped` (`report_unreadable`, error verbatim) while the rest of the
  queue still builds — and the evaluation-set pin records the error
  string so repairing it lapses the queue.
- **Conversion preserves the exact binding.** `convert_to_runner_plan`
  emits one `RunnerCellSpec` per pending item — `channel_role`,
  `source_speaker_ids`, `target_entity_id`, `repeat_index=0`,
  `purpose='measurement'`, `notes='remeasure:<mid> report:<rid>'` —
  under the queue's pinned `scene_revision_id`/`scene_content_hash`, then
  seals per-item `converted` events. An identical scene-pinned plan cell
  set de-duplicates onto the existing plan (concurrent conversion is a
  no-op, not a fork).
- **Retake lineage stays append-only.** New measurements commit through
  the runner path; `commit_cell` already derives `retake_required` /
  `completed` / `quality_pending` from the latest sealed report and links
  supersedes — old records are never rewritten.
- **Physical confirmation stays human.** The queue and runner plan only
  *declare* the required inputs (position / channel role / speakers /
  calibration sha / sample rate). Arming, mic movement and audio output
  require the operator's explicit confirmation in the existing runner /
  acquisition surfaces — no implicit arming.

## UI surface

測定 → 再測定キュー (workspace context `remeasure`, between 品質 and
比較):

- Status card: queue state (最新 / 失効(シーン変更) / 失効(評価変更) /
  未生成), pending/soft/skipped counts, 生成/再生成 and
  キャンペーンへ変換 buttons (convert is enabled only while
  `current` and pending > 0).
- Items table: measurement id, position, channel role, sources, verbatim
  failed criteria, per-item state — with 測定を開く
  (`select_measurement_id` jump) and 理由を付けて棄却
  (`QInputDialog`; a blank reason is refused and never records).
- Soft card: recoverable-without-recapture rows + 再評価 button (runs the
  honest producer again, never queues a recapture).
- Skipped card: every excluded measurement with its stated reason —
  nothing silently dropped.
- Convert lands a notice with a キャンペーンを開く action
  (`purpose='measurement'` cells join the standard campaign plan list
  under `rqplan-<sha[:24]>`).

## Tests

`backend/tests/test_issue_968_remeasure_queue.py` (14 tests):

- Multi-fixture extraction: only the threshold-failing measurement is
  queued, criteria verbatim, inputs resolved from the sealed context.
- Determinism / idempotency; lapse on evaluation drift and on scene
  change (with `lapsed_*` gating convert/dismiss).
- Dismiss records a reason, is terminal, and rejects blank reasons and
  foreign/converted items; events for unqueued measurements are refused.
- Convert produces an exact-binding campaign plan and per-item converted
  events; second conversion is a no-op.
- `UNKNOWN` classification: soft when recoverable, queued only when
  capture is genuinely required, skipped when nothing can be stated.
- One corrupted report payload lands skipped (`report_unreadable`)
  without poisoning the rest of the queue.
- Qt offscreen: the context page lists the three sections, the dismiss
  dialog records the reason (and cancel is a no-op), convert yields a
  selectable campaign plan.

## Related

- #969 quality table (authority surface this queue reads).
- #956 native runner, #1006 campaign cell states (conversion target).
- #1030 sealed gate-acceptance run records (sealing patterns reused:
  `_seal` hash-pin, append-only events, content-addressed idempotency).
