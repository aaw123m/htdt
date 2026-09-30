# Round 21 — Attachment/file lifecycle truth

Scope: every user-managed file attachment — measurement source files, source
attachments (mdat/notes/evidence), impulse-response datasets, equipment and
directivity/treatment/video-geometry source assets, auralization WAVs,
floor-plan underlays, capture-evidence blobs, bundle members — audited
across eight dimensions with real file operations through the actual APIs:

1. **ADD** — dedupe of identical content, honest size/type limits, storage
   in the right authority, references pointing at the stored copy.
2. **REPLACE** — replacing a file: references update; the old asset is
   orphaned honestly or cleaned.
3. **REMOVE** — deleting an attachment still referenced by rows/UI.
4. **RENAME** — rename updates every display/storage path.
5. **MISSING** — file deleted externally: honest report vs crash vs
   silent empty.
6. **MOVE/RELOCATE** — attachment paths survive a project/data-dir move.
7. **SIZE** — multi-GB attachments: streaming or memory blow-up.
8. **ORPHAN SWEEP** — cleanup deletes only truly-unreferenced assets.

Method: code audit plus a live probe (`C:\t\rev21_probe.py`) that built a
real data dir (`cad-scenes.sqlite3` + `measurement-assets/` + content
blobs), attached files via `save_attachment`/`save`/`import_underlay`,
deleted bytes externally, exported/imported bundles under `tracemalloc`,
and drove `scan_storage` — all through the production repositories.
Branch `devin/rev21-attach`. Regression tests:
`backend/tests/test_round21_attachment_lifecycle.py` (8 cases).

## Findings

| # | Finding | Dimension | Severity | Verdict |
|---|---------|-----------|----------|---------|
| 1 | Both measurement attachment pickers (`_attach_to_selected_batch_item`, `_attach_to_measurement`) bounded reads with `MAX_NATIVE_REW_TEXT_FILE_BYTES` (32 MiB) while the authority limit is `MAX_ATTACHMENT_BYTES` (256 MiB) — a legal 100 MiB mdat was refused with a misleading message; the equipment picker already used the right constant | ADD / limits | MED | FIXED — pickers now bound by `MAX_ATTACHMENT_BYTES` |
| 2 | `CadMeasurementRepository.read_attachment` returned `None` for an externally deleted file (probe-verified) against its `-> bytes` contract — every sibling authority read (`get_dataset`, `read_artifact_wav`, `_check_managed_source_asset`) fails closed; a silent `None` lets callers mistake missing evidence for empty content | MISSING | MED | FIXED — raises `ManagedAssetError` naming the digest |
| 3 | `export_project_bundle` materialized **every** asset payload in a dict before writing (probe: 78 MiB peak for 24 MiB of assets → unbounded growth for multi-GB projects); `import_project_bundle` likewise accumulated all `assets/` members in `asset_bytes` before install | SIZE | HIGH for large projects | FIXED — export streams each file chunk-wise into the zip (hash/size verified as written); import streams each member straight into the atomic store install |
| 4 | Import `_validate_bundle_members` rejected members with compression ratio >100 — a legitimate highly-compressible asset (silent audio, sparse log) produced a bundle the importer refused (probe-verified); `_read_member_bounded`/`install_stream` already cap real decompressed output, so the ratio check added nothing | ADD / honest limits | MED | FIXED — ratio check removed |
| 5 | A floor-plan underlay whose blob row vanished rendered silently: menu listed it normally, viewport drew nothing (probe `underlay_silent_empty: true`) | MISSING | MED | FIXED — `missing_source` flag on render items + `（データ欠落）` marker in the underlay menu via a cheap `has_blob` probe |
| 6 | `htdt_content_blobs` (underlay/mesh/capture payloads) has no GC — deleting an underlay record leaves its blob forever; shared digests make refcounting unsafe to bolt on | ORPHAN SWEEP | LOW | DEFERRED — conservative never-clean is the documented safe side; reclaim path is backup/restore |
| 7 | `db/*.jsonl` table payloads still materialize in memory on export/import (a >256 MiB member, e.g. a blob-table-heavy project, hits the honest member bound rather than streaming) | SIZE | LOW | DEFERRED — bound is enforced honestly; streaming JSONL parse is a deeper change |
| 8 | `save_source_asset` (treatment) keeps its own ad-hoc CAS instead of `ManagedAssetStore` | — | LOW | VERIFIED OK — same digest contract; cosmetic divergence |

## Dimension notes

- **ADD**: dedupe verified live — two measurements/`save_attachment` calls
  with identical bytes yield one on-disk file (`files_on_disk == 1`,
  `attachments_same_digest == true`); installs are atomic
  temp+fsync+verify+`os.replace`; every reference is the content digest,
  never the source path. Type/size limits: underlay 64 MiB + format
  allow-list, capture ingest 2/8 GiB, attachments 256 MiB — all enforced
  with honest Japanese errors.
- **REPLACE**: attachments are immutable by design (no replace API);
  correction/disposition is append-only overlay. Replacement uploads land
  as new content digests; superseded bytes become GC-eligible, never
  silently mutated.
- **REMOVE**: no user-facing attachment delete (immutable evidence);
  `delete_project` re-checks the plan fingerprint under `BEGIN
  IMMEDIATE`, runs `foreign_key_check`, tombstones the project and leaves
  local digests GC-eligible — shared assets explicitly retained
  (test-covered).
- **RENAME**: project rename is display-only (identity rows untouched);
  underlay name is an editable field on the record. No file path embeds a
  display name — nothing to drift.
- **MISSING**: dataset/authority reads raise `ManagedAssetError` per row
  (measurement views degrade honestly via the `errors` map); storage scan
  reports `missing_referenced` as an integrity failure, never a cleanup
  candidate; backup and bundle export fail closed naming the digest.
  Attachments and underlays were the two dishonest stragglers (#2, #5).
- **MOVE/RELOCATE**: all paths are data-dir-relative; journaled relocation
  re-verifies every registry asset's size+SHA-256 on the staged copy
  before cutover.
- **SIZE**: backup streams members chunk-wise already; bundles now match
  (#3). `install_stream` keeps the atomic install contract — a stream
  exceeding the bound, the declared size, or the digest fails closed and
  leaves no file.
- **ORPHAN SWEEP**: storage GC re-proves unreachability inside `BEGIN
  IMMEDIATE`, content-verifies each candidate, keeps the
  interrupted-delete ledger crash-recoverable, and classifies
  unregistered digest-named files (e.g. auralization WAVs) as
  `unmanaged` — reported, never deleted. `htdt_content_blobs` rows are
  the one never-clean corner (#6).

## Deferred

- `htdt_content_blobs` sweep needs a reference-safety story before any
  deletion (digests are shared across underlay/mesh/capture payloads);
  conservative retention is the current documented choice.
- Blob-table-heavy bundles (>256 MiB single table payload) hit the honest
  member bound rather than streaming.
- Unregistered asset files (auralization WAVs) have no registry row —
  GC reports them as `unmanaged` when unreferenced rather than sweeping.
