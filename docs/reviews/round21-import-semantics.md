# Round 21 — Import-into-existing semantics

Scope: the merge/collision semantics when imported data meets existing
state — not file-format parsing (covered by the IMPORT/IO rounds).
Seven dimensions verified with constructed datasets driven through the
real import paths:

1. **Duplicates** — import the same measurement/attachment/IR twice.
2. **ID collisions** — imported ids already present in the target.
3. **Partial import** — a batch/commit that fails halfway.
4. **References** — payloads referencing entities absent everywhere.
5. **Overwrite** — does import silently clobber user-modified state.
6. **Merge semantics** — same id, diverged content: which wins.
7. **Larger-than-preview** — does commit land more than staging showed.

Import surfaces exercised: the measurement batch queue
(`stage_rew_text_files` / `stage_rew_snapshots` / `commit_batch` in
`measurement_workflow.py`), direct measurement saves
(`CadMeasurementRepository.save` / `save_attachment` /
`save_ir_dataset`), project-bundle export/import
(`project_bundle.py` frontier walk + collision contract), capture
ingestion (`capture_ingestion_transaction.py` dedup,
`capture_semantic_promotion.py` conflict policies), and the
migration/schema guard.

Branch `devin/rev21-import`. Tests:
`backend/tests/test_review_round21_import_semantics.py` (8 cases).

## Verdict table

| # | Dimension / finding | Severity | Verdict |
|---|--------------------|----------|---------|
| 1 | **DUPLICATES** — intra-batch sibling duplicates invisible: two identical sources staged before either commits both classified `new` and registered byte-identical measurements (same `source_sha256`, different `measurement_id`) the UI cannot tell apart | **HIGH** — silent dup records | FIXED — `_staged_duplicate_sources` cross-checks the uncommitted queue; second item flags `exact_duplicate`/`same_acquisition` of the sibling and resolves `reuse_existing` to the sibling's committed id |
| 2 | **DUPLICATES** — `save_attachment` inserted a second indistinguishable row for an exact re-attach (same measurement, kind, filename, bytes, note) | MED — dup attachment rows | FIXED — exact-dup returns the existing row inside `BEGIN IMMEDIATE` (`IS ?` treats NULL notes as equal) |
| 3 | **DUPLICATES** — `save_ir_dataset` re-imported identical IR semantics (same raw + same declared interpretation) as a second dataset under a fresh `dataset_id` | MED — dup datasets | FIXED — semantic-payload compare (`_ir_semantic_payload`: model dump minus `dataset_id` minus provenance `filename` in `processing_json`) returns the existing row |
| 4 | **DUPLICATES** — a `reuse_existing` batch item whose duplicate target never committed would insert anyway (reuse silently degrading to insert) | MED — dishonest merge | FIXED — unsatisfied reuse target resolves through sibling-commit and persisted-evidence lookups; if still unsatisfied the item reports an honest skip (`再利用先の測定がまだ保存されていません`) and stays retryable |
| 5 | **ID COLLISIONS** — bundle import of entity/spec ids already in the target | — | VERIFIED OK — natural-key collision contract in `project_bundle.py`: same identity + same content dedupes, same identity + diverged content is a reported conflict, never silent overwrite |
| 6 | **PARTIAL IMPORT** — batch commit interrupted mid-run | — | VERIFIED OK — per-item outcomes (`committed`/`reused`/`failed`/`skipped`), `already_committed` items never re-register on retry, normalized `commit_payload` pinned across attempts; resume accepts the identical row, mismatch fails closed |
| 7 | **REFERENCES** — imported objects referencing entities/measurements not in the payload nor the target | — | VERIFIED OK — measurement records bind to `SceneRevision` + assignment targets that must exist at commit (unknown entity fails); bundle frontier walk follows `_is_identity_value` refs and the manifest/table/asset hash chain rejects dangling payloads |
| 8 | **OVERWRITE** — import clobbering user-modified local state | — | VERIFIED OK — measurements are append-only (no delete/replace API); bundle import refuses to overwrite diverged rows without an explicit conflict path; `_LOCAL_ONLY_TABLES` never leave the machine |
| 9 | **MERGE SEMANTICS** — same id, diverged content | — | VERIFIED OK — `scene_document_heads` natural-key collision surfaces as conflict; staged-scene revision/content-hash check at commit (`読み込み後に部屋の保存状態が変更されています`) refuses to commit against a scene that moved after staging |
| 10 | **LARGER-THAN-PREVIEW** — commit landing more items than staging showed | — | VERIFIED OK — commit iterates exactly the staged `_BatchEntry` set; the only new identities created are committed measurement rows (1:1 with staged items); `already_committed` guards retry inflation |

## Fixed

### F1 — Intra-batch sibling duplicates (HIGH)

`_classify_duplicate` consulted only persisted evidence
(`measurement_id_by_source_sha256` / `measurement_id_by_external_source`),
so two identical files staged in one batch — or in two stage calls before
either committed — both classified `new` and committed two identical
measurements. Verified repro: staging `seat-a.txt` and
`copy-of-seat-a.txt` (same bytes) produced 2 measurements with the same
`source_sha256`.

Fix in `measurement_workflow.py`:

- `_staged_duplicate_sources()` maps the uncommitted queue by
  `sha256(raw_bytes)` (text) and `rew_snapshot.decoded.measurement_id`
  (snapshots); `setdefault` keeps the earliest sibling canonical.
- `stage_rew_text_files` / `stage_rew_snapshots` consult the staged maps
  after the persisted check: kind `new` + pending + sibling hit →
  `exact_duplicate` (text) / `same_acquisition` (snapshot) with
  `duplicate_of_item_id` pointing at the sibling's `item_id`.
- `_BatchEntry` carries `duplicate_of_item_id`; `_batch_item_view`
  resolves `duplicate_of_name` from the sibling's `source_label` so the
  "重複: <name>" UI line names the in-batch original honestly.
- `commit_batch` reuse resolution chain: persisted
  `duplicate_of_measurement_id` → sibling's `committed_measurement_id`
  → re-run `_classify_duplicate` (catches a sibling committed since
  staging). A reuse target that resolves to nothing — or to a
  measurement no longer persisted — is an honest `skipped` outcome
  (`再利用先の測定がまだ保存されていません`), never a silent insert.
  `import_as_new` remains the explicit escape hatch.

### F2 — Exact re-attach of a source attachment (MED)

`save_attachment` now checks for an identical persisted row
(measurement, kind, filename, sha256, note — `note IS ?` so NULL notes
compare equal) inside the `BEGIN IMMEDIATE` transaction and returns it
instead of inserting an indistinguishable second row. A different note
on the same artifact still registers a distinct row.

### F3 — Identical IR re-import (MED)

`save_ir_dataset` returns the already-persisted dataset when the new
dataset is a semantic twin — same `source_sha256` and every declared
interpretation field — via `_ir_semantic_payload` (model dump minus the
fresh `dataset_id` and the incidental `filename` inside
`processing_json`). `import_ir_for_measurement` returns the persisted
dataset so callers (and the UI) see the reuse. A different declared
interpretation (`window_kind`, t0 semantics, …) still inserts a new
dataset — never deduplicated away.

## Verified-OK evidence

- **Diverged bundle reimport**: same natural key + different content →
  collision conflict on `scene_document_heads` (honest, not silent).
- **Crafted dangling payload refs**: not decidable by design — bundle
  values that look like ids (UUIDv4/sha256) include non-row hashes;
  the hash-verified manifest chain is the integrity boundary.
- **Retry inflation**: `commit_batch` skips `committed` entries and the
  resume path in `_save_measurement_for_commit` accepts the identical
  row only — a mid-run failure leaves a precise partial state, not a
  doubled one.
- **Scene drift**: staged entries pin `(scene_revision_id,
  scene_content_hash)`; commit fails closed if the scene moved.

## Deferred

| # | Item | Why deferred |
|---|------|--------------|
| D1 | `stage_rew_text_files` preview still counts an intra-batch second file as its own row before commit (the `exact_duplicate` flag carries the truth, not the count) | Preview N vs committed N−1 is now *honest* (the dup is labeled) rather than silent; merging the count would need UI contract changes |
| D2 | Copy-import display-name collision (a reused measurement keeps the original name) | Cosmetic by design — the measurement id is the identity, the label is provenance |

## Files

- `backend/src/htdt/measurement_workflow.py` — staged-sibling duplicate
  detection + honest reuse resolution.
- `backend/src/htdt/cad_measurement_repository.py` — attachment dedup +
  IR semantic dedup (`_ir_semantic_payload`).
- `backend/tests/test_review_round21_import_semantics.py` — 8 cases.
