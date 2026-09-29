# Round 15 review — wire / serialization contract truth

Branch: `devin/rev15-wire`. Scope: every surface where bytes leave an
in-memory model — `SceneRevision` payloads in `cad_repository.py`, the
generic `_save_payload`/`_payloads` dict stores, `capture_inbox.py`
(30-column item rows), `capture_receiver.py` (pairing / mission-package /
mission-receipt rows + the HTTP wire contract), `capture_bundle.py` +
`capture_contract/support-matrix.json` (39 families → documents →
emitted/read version sets), `project_bundle.py` (`.htdtbundle` manifest +
`manifest_sha256`), `application_preferences.py` (settings store),
`launch_intents.py` (drop-file queue), `runtime_instance.py`
(`runtime.json`), `authority_revalidation.py` (stored-vs-rebuilt
comparison), and the DDL-to-SQL column contract across `NATIVE_SCHEMA_VERSION = 10`
(343 tables cross-checked programmatically). Method: payloads were
written and read back in both directions with a live `HTDTStore` —
round-trip byte-equality, v1-shaped payloads, tampered payloads,
unknown-key payloads, and every malformed receipt shape the handler can
see — plus an AST/regex inventory of write-key vs read-key sets per
surface.

## Fixed in this branch

| # | Severity | Location | Finding | Fix |
|---|----------|----------|---------|-----|
| 1 | high | `capture_receiver.py` `handle_mission_receipt` | **`schema_version` was declared but never enforced.** `MISSION_RECEIPT_SCHEMA_VERSION = '1.0.0'` exists as the wire pin, yet the handler accepted a receipt with the key absent *and* with `"9.9.9"` alike — both verified live to return 200. A v2-shaped receipt would be silently consumed with v1 semantics and recorded in the audit trail; version drift between sender and receiver is exactly what the field exists to detect. | `receipt.get('schema_version') != MISSION_RECEIPT_SCHEMA_VERSION` → 400 `unsupported receipt schema version`, matching the strict-equality convention every other versioned document in the module uses. |
| 2 | high | `capture_receiver.py` `handle_mission_receipt` | **`paired_destination_id` / `receiver_instance_id` were read but never checked.** A receipt carrying a different pairing's identifiers was verified live to be accepted (200) and persisted against *this* pairing's package — a wrong-but-plausible claim silently mixing one endpoint's audit rows into another's. | Both fields are now enforced when present: `paired_destination_id` must equal `pairing.pairing_id` and `receiver_instance_id` must equal `pairing.receiver_instance_id`, else 400. Absence stays tolerated — the bearer token already authenticates the reporting endpoint; a present-but-wrong identity is the malformed claim. |
| 3 | medium | `capture_receiver.py` `handle_mission_receipt` | **Non-string verdict fields crashed with `sqlite3.ProgrammingError` → dropped connection.** `receipt.get('detail') or result` and `receipt.get('validation_result', '')` were bound into `status_detail` untyped: a dict `detail` or list `validation_result` raised `Error binding parameter` inside the transaction — verified live — and nothing in `do_POST` catches it, so the request died as a 500 mid-write. A non-dict JSON body (`[1,2]`) likewise crashed on `receipt.get`. | Non-dict bodies → 400 `receipt is not a JSON object`; `validation_result` must be a string and `detail` string-or-None, else 400 `receipt verdict fields must be strings` — malformed claims get a contract error instead of a storage-layer crash. |
| 4 | medium | `capture_receiver.py` `capabilities_document` | **The advertised contract said the endpoint accepts 1 schema × 1 version and 0 authority families; the implementation accepts 37 schemas and promotes 8 kinds.** `accepted_payload_schemas` was hard-mirrored to `[connected-spaces 1.0.0]` while `support-matrix.json` pins 37 versioned families the ingestion pipeline actually validates, and `supported_authority_families` was `[]` while `PROMOTION_AUTHORITY_KINDS` is 8 members. Any sender negotiating via the capabilities document — the document's sole purpose — learns the endpoint supports almost nothing. | Both fields now derive from their single authorities: `accepted_payload_schemas` is built from `_load_support_matrix()` (schema_id + sorted `read` set per family, numeric-version ordered), and `supported_authority_families` is `sorted(PROMOTION_AUTHORITY_KINDS)`. Hand-mirroring constants across modules was the drift mechanism; the fix removes the mirror. |
| 5 | low | `capture_bundle.py` `_schema_document_for_version` | **`supported=` diagnostic listed versions lexically** (`1.10.0` before `1.2.0`) in the negotiation-failure message — the only place the read-set ordering leaked into a user-visible string. | Same numeric-component sort key used everywhere else in the module. |

## Verified honest (checked, no change needed)

- **SceneRevision payload contract** — write `canonical_scene_json`
  (sorted keys, ASCII-off, `None`-valued pre-era optionals popped for
  hash continuity) + `sha256` column; `_row_to_revision` re-canonicalizes
  and hash-verifies every read. Live results: save→get→resave byte-equal;
  a v1-shaped payload parses, hash-verifies, and re-saves into canonical
  form; an unknown extra key passes the hash gate leniently (hash covers
  the *canonical* form) and is dropped inertly on read — a consistent
  lenient-on-extra/strict-on-content policy, not ambiguity; a tampered
  semantic field is rejected by the hash check. `schema_version` gates
  (`wall_topology` ≥3, `r120_semantic_geometry` ≥4, attachments/
  construction assemblies ≥5) hold: v1 payloads read fine and stay v1.
- **`_save_payload`/`_payloads` dict stores** — corrupt rows are purged
  fail-soft, not crashed on; column sets written match column sets read.
- **`capture_inbox`** — 30-column `INSERT` ↔ 30-field `_item_from_row`
  verified 1:1; every `Literal[...]` status/stage/authority-kind enum is
  written and read through the same constants (`PROMOTION_AUTHORITY_KINDS`
  frozenset = the 8 kinds); `stage()` requires the persisted ingestion
  transaction first (ordering contract enforced, not assumed).
- **`project_bundle.py` manifest** — `import_events` is written-but-never-
  read, **deliberately kept**: it is inside `identity_payload()` →
  `manifest_sha256`, so removing it would change the hash identity of
  existing bundles. Dead on the read path, live in the seal — the correct
  treatment for sealed contract surface.
- **`application_preferences.py`** — unknown keys in `values` round-trip
  verbatim through `_opaque_values` (forward-compat honored); values the
  canonical writer can't serialize are dropped *and reported*; a file
  with a newer `schema_version` returns `INCOMPATIBLE_NEWER_SCHEMA` and
  refuses to write — honest rejection, no silent downgrade.
- **`launch_intents.py`** — `schema_version: Literal[1]` strict; malformed
  or stale intents move to `dead/` instead of replaying — the at-least-once
  queue's honest-failure policy.
- **`runtime_instance.py`** — `runtime.json` round-trips; the reader
  validates `app_id`, int `pid`/`port`, port range, and the exact
  `http://127.0.0.1:{port}/` URL — a marker that parses but fails shape
  is treated as unreadable, never probed field-by-field.
- **`authority_revalidation.py`** — `_semantic_drift` compares
  stored→rebuilt one-directionally by design (a rebuild *adding* fields
  is intentionally sealed, not flagged); `_wiring_check_kwargs` maps
  kwargs explicitly rather than serializing blindly.
- **`MissionPackage.descriptor()`** — `issued_at_utc`→`issued_at` is the
  only wire rename in the module and is symmetric: written by
  `descriptor()`, read back by `_mission_from_row` for the same surface.
- **`ReceiverPairing.state`** — all 4 `Literal` states
  (`offered/active/revoked/expired`) are written by the lifecycle; no
  write-side/read-side enum drift anywhere (wire strings are the same
  constants both directions; support-matrix `emitted ⊆ read` holds for
  all versioned families).
- **DDL ↔ SQL column contract** — all 343 `CREATE TABLE` statements in
  `NATIVE_BASELINE_DDL` were cross-checked programmatically against every
  `INSERT`/`SELECT` column list in the codebase: zero orphaned or
  misspelled columns.

## Deferred

- None. The one structurally-weak surface (`handle_mission_receipt` input
  typing) and the one drifted advertised contract (`capabilities_document`)
  were both small-diff fixes completed here.
