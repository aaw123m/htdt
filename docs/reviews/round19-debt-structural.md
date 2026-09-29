# Round 19 — structural deferreds

Scope: implement the larger design-level items accumulated across rounds
1–18 (each verified deferred in `docs/reviews/round*.md`, now implemented
properly): progress-sink lifecycle, `capture_disposition_transitions`
schema migration, `scene_document_heads` FK check, API
`response_model`/pagination, per-`Device:` bucketing / settings
lazification, append-only growth policy, import-as-copy remap +
PARSER_VERSION. Verified locally (Windows, `QT_QPA_PLATFORM=offscreen`,
`pytest`, Python 3.12.10). Branch `devin/rev19-debt2`.

## Implemented

### 1. `progress_sink` lifecycle — explicit lease contract
(`cad_r140_executor.py`, `cad_prediction_execution.py`)

The defect: `PredictionExecutionController` installed its progress sink
by *mutating* `executor.progress_sink` when it happened to be `None`.
Nothing ever released it — a disposed controller kept receiving task
events, and a successor controller silently starved when the stale
binding remained set.

Contract implemented:
- `BoundedR140Executor.acquire_progress_sink(sink) -> ProgressSinkLease`.
  Leases **stack**: the newest held lease is the effective sink, so a
  successor controller reports immediately instead of starving behind a
  predecessor's binding.
- `lease.release()` unwinds to the previous lease (or the base sink);
  releasing twice is a no-op, and `executor.close()` clears outstanding
  leases — controllers released after executor close stay safe.
- `executor.progress_sink` remains a read/write attribute: reads return
  the effective sink (top lease, else base); writes set the base sink.
  Existing constructor wiring and test stubs that assign
  `progress_sink = ...` are untouched.
- `PredictionExecutionController` now *holds* a lease for its lifetime
  instead of mutating the attribute, and gains `close()` +
  context-manager support to return it deterministically.

### 2. `capture_disposition_transitions` — schema v10 → v11
(`capture_inbox.py`, `cad_schema.py`, `cad_schema_ddl.py`,
`native_authority_audit.py`, `capture_retention.py`)

The finding every prior round flagged: the inbox mutates `disposition`
in place, so the audit trail (who changed an item's review state, when,
why) was unrecorded — only the current state survived.

- `capture_disposition_transitions(transition_id PK, lineage_digest →
  capture_inbox_items, from_disposition, to_disposition, reason, actor,
  changed_at_utc)` created inside the inbox domain's canonical
  convergence script — the same authority that materializes the other
  inbox tables when v6 lands — so fresh databases and upgraded ones
  converge identically.
- `NATIVE_SCHEMA_VERSION = 11`; `_migrate_10_to_11` reruns the inbox
  convergence on an already-current schema. **No backfill** — a v10 item
  never had a ledger; its trail starts with the first post-upgrade
  change, recorded honestly rather than retro-fabricated.
- `record_disposition_transition()` is a module-level helper that runs
  inside the *caller's* write transaction, so every audit row commits or
  rolls back with the disposition update it describes. `transition_id`
  is content-derived (`domain + lineage + from/to + per-item sequence`),
  never wall-clock-unique.
- Write-path hooks: `stage` (None→pending), `_update_facets` whenever
  `disposition` changes (actors `defer`/`resume`/`reject`), promotion
  outcomes (`promotion_outcome`, only when the derived disposition
  actually changes), `supersede` flip (`supersede`), retention-purge
  reverts (`retention_purge`).
- Read API `CaptureInboxRepository.disposition_transitions(digest)`
  returns the trail oldest-first.
- Registered everywhere the schema authority requires: `NATIVE_SCHEMA_
  TABLES`, `_initialize`'s `require_native_tables`, `native_authority_
  audit._TABLE_POLICY` (OPERATIONAL_METADATA — append-only intake
  bookkeeping). Retention purge deletes an item's ledger with the item
  (FK child); project-delete's ownership chase and bundle export/import
  follow the existing lineage-digest identity automatically.

### 3. `scene_document_heads` FK check — fail closed on dangling head
(`cad_repository.py`)

`_head_revision_row` answered a corrupt store by returning `None` —
"document has no head" — which lets the next `save` fork a second root
lineage. Now: heads row present + JOIN miss = `SceneDocumentHead-
IntegrityError`; no heads row = legitimately head-less. Write and delete
paths keep `scene_document_heads`/`scene_revisions` consistent in one
transaction, so a dangling pointer can only be corruption (FK-off
rebuilds, hand-edited DBs) — reading it as head-less would fork the
lineage authority.

### 4. API `response_model` + bounded pagination (`main.py`)

- Per-resource row models (`ProjectRow`, `SessionRow`, `ContextRow`,
  `ConstraintSetRow`, `SearchSpecRow`, `MeasurementRow`,
  `AttachmentRow`, `ComparisonRow`) sharing `_StoreRow` with
  `extra='allow'`: declared fields are the guaranteed NOT-NULL contract,
  everything else passes through byte-for-byte — **wire shapes
  identical**, OpenAPI now names them.
- `response_model=list[X]` on all eight project-scoped list endpoints
  (plus `response_model=X` on the three single-row GETs sharing those
  shapes). `/api/rew/*` untouched — that contract is proxied from
  upstream REW API, not owned here.
- `offset`/`limit` paging on the same list endpoints:
  `offset >= 0`, `1 <= limit <= MAX_LIST_PAGE_SIZE (10_000)`. Default
  `limit` equals the bound, so unparameterized responses are unchanged;
  invalid bounds 422 at the validation layer like the rest of the API.

## Judged not implementable as "minimal diff" — and exactly why

### 5a. Per-`Device:` bucketing (ECI10 importer)
Not a parsing fix — a semantic-data-model change. Today's model keys
`channels` by bare label and collapses `Device:` blocks into one opaque
`device_scope` string. Real bucketing needs (device,label)-keyed buckets
through `_parse_config_text`, the Include-merge boundary
(`_merge_include`'s flat-label merge loses device provenance at the
seam), `ImportedChannelSettings.device_label`, and the comparator's
per-channel `ALL` composition *per device* instead of globally. The
blocking product question is semantics: is `Channel: ALL` inside a
`Device:` block device-scoped or cross-device? The file format doesn't
say, and guessing wrong silently mis-profiles a multi-device config.
That's a product/format-semantics decision plus an importer-version
question — not a diff size problem, a correctness-definition problem.

### 5b. Settings-dialog lazification
Implementable (~40-line `_ensure_settings_dialog` wrapper) but **not
verifiable as minimal** inside this round's contract. The deferred
danger isn't construction — it's lifecycle: `data_management_component`
is read by `_can_close_application` (workflow_application.py:3702),
`capture_receiver.delivery_staged` is wired at init, and
`_preferences_panel.release()` runs on shutdown. A lazy path must keep
"never-opened ⇒ close unconditionally" semantics, decide whether the
component is late-bound or always-built, and re-wire the staged-delivery
signal ordering — every edge reachable only in a running Qt app, for a
startup-latency win no budget calls for. Deferred on the same grounds as
round 14: lifecycle change, no forcing requirement.

### 6. Append-only table growth — **documented policy** (no pruning)
The task allows "bounded retention or documented policy"; the honest
choice is the latter, now written down:

| Table family | Policy |
|--------------|--------|
| `native_schema_migrations`, `htdt_project_imports` | Tiny by construction (≤ schema versions, ≤ project imports) — no bound needed |
| `capture_receiver_deliveries` | Machine-local delivery ledger — bounded by pairing churn, already has peer lifecycle |
| `cad_measurement_runner_events`, `cad_calibration_lifecycle_events`, `cad_calibration_evidence_events`, `cad_dependency_resolution_events` | Per-run/per-event audit rows — the project DB is an **audit trail**, not a cache; growth is linear in real use |
| `capture_disposition_transitions` (new, this round) | One row per disposition change — bounded by operator actions on items that already have item-level lifecycle (retention purge deletes with the item) |

**Trigger to revisit**: a stated size budget (e.g. project-file size cap
or retention SLO) — absent that, auto-pruning an audit ledger destroys
the evidence it's for. Compaction stays deferred until a product size
budget lands.

### 7a. Import-as-copy remap for capture lineage
Copying a project remaps `*_digest` columns via `value_map`, but capture
inbox items carry lineage_digest/bundle_digest identities **shared by
design** — the digest *is* the evidence address, shared with every other
project copy. Remapping only the item's scope while keeping evidence
shared requires deciding whether a copy sees the original's intake queue
(a product-visibility question), and breaking the shared lineage_digest
would detach the copy from supersession/registration records pointing at
the same lineages. Not a mechanical bug — the shared-identity semantics
are deliberate.

### 7b. PARSER_VERSION pin
Intentionally *not* bumped — per round 15's own note, bumping it
invalidates every prior dataset's parse-identity. Correct as-is.

## Regression coverage

`backend/tests/test_round19_debt_structural.py` — 15 tests: dangling-head
fail-closed, lease stack/unwind + successor takeover + closed-executor
refusal + close-safety, v10→v11 upgrade preserving items, per-actor
transition sequences (including the no-change-no-row case), supersede
flip, retention purge dropping the ledger + recording the revert,
pagination bounds/wire-identity, response_model declarations. Plus
`test_cad_schema.py` migrations-ledger extended to v11 and
`test_authority_revalidation.py` upgrade target un-pinned to
`NATIVE_SCHEMA_VERSION`.

Scoped suite: ~230 tests green across the touched areas
(capture inbox/lifecycle/ingestion, schema + DDL contract, prediction
execution, API, authority audit/revalidation, repository, retention UI,
round-14/17 review suites).
