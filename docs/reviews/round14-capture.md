# Round 14 review — capture → inbox → measurement pipeline depth

Scope: the physical-capture lane end to end — paired-receiver delivery,
file import, `CaptureIngestionRepository.ingest`, Capture Inbox staging /
dispositions / granular promotion / supersession, semantic promotion into
scene revisions, and the retention purge. Drove real `.htdtcapture`
payload bytes through every hop (production `_default_bundle_reader`,
real pairing + `handle_delivery`, real `promote()` calls writing scene
revisions, real purges) in `tests/test_review_round14_capture_pipeline.py`
(15 tests) and asserted identity, payload hashes, labels and timestamps
at each hop.

## What the pipeline actually does, verified

| Hop | Verified |
|---|---|
| Wire → receipt | archive SHA-256/byte-count headers verified against body; revision/bundle-digest headers must agree with bundle identity; receipt echoes `capture_revision_id`, `bundle_digest`, `staging_ref` |
| Receipt → ingestion run | `bundle_digest`/`capture_revision_id`/`lineage_digest` all match; `recorded_at_utc` stamped at ingest, not at capture time |
| Run → inbox item | same lineage/digest; `arrival_source='paired_receiver'`; scope from pairing `project_ref` (`CAPTURE_INBOX_UNASSIGNED_SCOPE` when absent); `disposition='pending'`; `first_arrived_at_utc` is arrival time, not manifest `created_at` |
| Manifest → evidence rows | every `capture_source_evidence` row's `payload_sha256`/`bytes`/`media_type`/`provenance_class` equal the manifest's declared values; retained bytes re-hash to the declared sha |
| Item → promotion records | granular per-authority-kind records; idempotent on (kind, authority); `partially_promoted`/`promoted` derived honestly |
| Promotion → scene revision | `replace_exact` policy fails closed unless caller names the exact prior geometry id + semantic hash; `superseded_geometry_id` recorded on the promotion row |
| Purge → payloads | run/link/evidence/binding/authority rows + unreferenced content blobs actually deleted; post-purge `foreign_key_check` enforced in-transaction |

## Findings and fixes this round

### 1. Purged revisions left ghost inbox rows — stranded and uninspectable

`CaptureRetentionService._execute_plan` deleted ingestion/evidence rows
but never the `capture_inbox_*` bookkeeping. A staged item survived its
payload: `list_items()` kept counting it (UI badges/lists honest-looking
but dead), `inspect()` raised *"inbox item survives without its
ingestion run; the store is inconsistent"*, and a pending-review item
could not even block the purge (inbox items are not dependents).

Fix: the purge transaction now deletes the inbox item, its promotion
records, and any supersessions/registrations that name a purged lineage
on either side. An item flipped `superseded` solely by records that were
just deleted reverts to its promoted-derived disposition — the
replacement claim no longer exists. Inbox cleanup is conditional on the
tables existing (stores that never opened the inbox keep purging).
(`test_purge_removes_review_bookkeeping_and_payload`,
`test_purging_superseding_revision_revives_superseded_item`)

### 2. Disposition operations had no guards — incoherent states reachable

`defer()`/`reject()` required only a non-empty reason. Via repository
calls (or a future UI bug) a fully `promoted` or `superseded` item could
be flipped to `rejected`/`deferred` while its live promotion records
persisted — and `rejected` → `resume()` → `pending` → promotable again,
silently undoing a supersession. `record_promotion`/`record_blocked`
also ran on `deferred` items (the UI disables all promote affordances
for deferred items; the repository did not).

Fix: `defer` is only legal from `pending`; `reject` from
`pending`/`deferred` — exactly the affordances the inbox page already
shows. `_check_promotable` now also blocks `deferred` ("resume it
before promoting" — the existing honest verb). No message change needed
in the UI; the JP labels already map these dispositions.
(`test_promoted_item_cannot_be_rejected_or_deferred`,
`test_deferred_item_cannot_record_promotion`,
`test_superseded_item_cannot_be_resurrected_by_disposition_ops`)

### 3. A superseded kind could silently regain a live promotion

`record_promotion` did not consult supersession records: after
`supersede(A, B, 'semantic_geometry')`, re-recording a `promoted`
outcome for `semantic_geometry` on A succeeded — the item then carried
both a live promotion and a supersession claim for the same kind, and
`promotability` still read `complete`.

Fix: `_record_outcome` rejects a `promoted` outcome for a kind already
covered by a supersession on that item. (`test_superseded_kind_cannot_be_recorded_promoted`)

### 4. Receiver delivered no manifest evidence — and lied about it

Two wire-path bugs in `CaptureReceiverService.handle_delivery`:

- **Manifest not retained.** `_default_bundle_reader` validated and
  discarded `manifest_bytes`; `ingest()` was called without `manifest`,
  so `capture_bundles` stayed empty for every receiver-delivered capture
  — the canonical manifest (#338) was only retained on the file-import
  lane, silently. The reader contract now allows a third return value
  (manifest bytes) and the delivery forwards it to `ingest`; older
  two-part readers keep working.
  (`test_real_archive_delivery_preserves_identity_at_every_hop` asserts
  the retained manifest re-hashes to the bundle digest)

- **False claim in rejections.** A same-revision/different-digest
  delivery was rejected with *"conflict staged in the inbox"* — nothing
  was ever staged (the bundle legitimately cannot ingest, so no inbox
  item exists; the conflict lives only on the delivery ledger). The
  detail now says what happened: rejected and recorded on the delivery
  ledger. (`test_revision_conflict_receipt_tells_the_truth`)

### 5. A rejected delivery was a permanent dead letter

The delivery ledger deduped on `pairing_id:delivery_id_or_sha` and
replayed the stored receipt for ANY prior row — including
`outcome='rejected'`. A bundle that failed mid-pipeline (e.g. ingest
succeeded, inbox staging crashed) was then un-stagable forever: every
retry replayed the old rejection without re-running the pipeline, while
the ingested run sat in the library orphaned from the inbox.

Fix: same-byte replays of a `rejected` delivery re-execute the pipeline;
`_record_delivery` rewrites the rejected row in place (keeping the
original receipt time). Different bytes under a replayed id still get
the 409 conflict. (`test_failed_staging_keeps_run_visible_and_redelivery_works`)

### 6. Quality state resolved against an arbitrary run

`_resolve_persisted_quality` and `get_capture_quality_state` selected
`WHERE lineage_digest=?` with no `ORDER BY` — for multi-run lineages
(#413: same lineage ingested by two ingestor versions) the picked row
was whichever SQLite returned first. A stale pre-gate `unresolved` row
on the older run could shadow the latest run's `validated` state and
wrongly block promotion.

Fix: both queries now order `recorded_at_utc DESC, ingestion_run_id
DESC` — the same latest-run rule `get_ingestion` already applies to
plans. (`test_multi_run_lineage_quality_reads_latest_run`)

## Verified-true behaviors worth calling out

- **Duplicate ingest is deduped, not double-promoted.** Same archive
  re-delivered → `already_staged` receipt replay, one inbox item,
  `arrival_count` tracks distinct deliveries (second pairing bumps it;
  scope/arrival_source keep the first arrival's routing — documented).
- **Failed ingest is atomic.** A corrupted payload fails the contract
  check and leaves zero rows; the same valid bundle then ingests
  cleanly. (`test_failed_ingest_leaves_no_rows_and_recovers`)
- **Inbox supersession never mutates downstream scene truth.** The
  promoted scene revision keeps its geometry (the document's honest
  current authority); replacing it still requires an explicit
  `replace_exact` promotion naming the prior geometry identity.
  (`test_inbox_supersession_does_not_mutate_scene_geometry`)
- **Listing truth.** `list_items()` counts and per-disposition filters
  match persisted rows; promotability labels (`promotable` /
  `partially_promotable` / `complete` / `blocked`) derive from real
  records. (`test_inbox_counts_and_filters_match_persisted_rows`)
- **Purge stays reference-safe.** A live `capture_semantic_promotions`
  dependent blocks the plan (`status='blocked'`, named dependents); the
  inbox promotion record alone does not — it is removed with the item.
  (`test_purge_blocked_by_live_semantic_promotion`)

## Deferred / noted gaps (not fixed — design-level)

- **Supersession is a claim, not a verified replacement.** `supersede`
  flips the old item once every promoted kind is *declared* covered by
  the new item — the superseding item need not have staged or promoted
  that kind (existing contract; tests rely on record-first ordering).
  Risk: A can read `superseded` while nothing actually replaced it.
  A verified-coverage gate (require the successor's promotion record for
  the kind before flipping) is a follow-up.
- **No partial-superseded label.** An item with one kind superseded and
  others still promoted reports `disposition='promoted'` and
  `promotability='complete'` — the superseded kind still counts as
  promoted in inspection. A `partially_superseded` facet would be
  honest; deferred to a schema/facet change.
- **`identity_digest_conflict` inbox classification is unreachable**
  through the real pipeline: ingestion fails closed before staging, so
  no inbox item for the conflicting variant can exist (the conflict is
  on `capture_revision_conflicts` / the receiver ledger instead). The
  classification path is test-only coverage; consider routing a real
  conflict row into the inbox, or drop the unreachable branch.
- **`arrival_source`/scope freeze at first arrival.** Later deliveries
  via another lane bump `arrival_count` but keep the first source —
  accurate for "first arrival", but there is no per-arrival ledger
  inside the inbox item. (Receiver-side deliveries are logged.)
- **Retention widget** never exposes that purging also removes inbox
  review entries — now true by deletion. A plan field surfacing the
  inbox-row count in the dry-run would be a nice follow-up.

## Files

- `backend/src/htdt/capture_receiver.py` — manifest retention, honest
  conflict detail, retryable rejections (ledger row rewritten in place).
- `backend/src/htdt/capture_inbox.py` — disposition guards on
  `defer`/`reject`, `deferred` blocked from promotion outcomes,
  superseded-kind promotion guard.
- `backend/src/htdt/capture_retention.py` — purge removes inbox
  bookkeeping and revives orphaned `superseded` dispositions.
- `backend/src/htdt/capture_ingestion_transaction.py` — deterministic
  latest-run quality resolution.
- `backend/tests/test_review_round14_capture_pipeline.py` — 15
  end-to-end probes (new).
- `docs/reviews/round14-capture.md` — this file.
