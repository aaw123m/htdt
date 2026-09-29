# Round 14 — Project Bundle Export→Import Round-Trip Completeness

Scope: `backend/src/htdt/project_bundle.py` — the `.htdtproject` zip (manifest.json + `db/*.jsonl` + `assets/<sha256>`) is how users move projects between machines. Built a **fat project** in a real store (3 scene revisions + head + recovery snapshot + label, 2 REW measurements + attachment + comparison, equipment definition/evidence/binding, level calibration, notes, evidence subjects/observations/reconciliation/field evidence+targets/labels, design-decision and comparison-set supersession chains, review notes/action items/applied settings, 3 staged captures — 2 assigned with promote/supersede/cross-revision alignment, 1 unassigned), exported, imported into a **fresh store**, then diffed every table, payload, and authority ref. Adversarial battery: 34 checks.

## Baseline fidelity

- **100 rows / 38 tables / 3 assets** exported on the fat project; import → **byte-identical** row-for-row across every exported table (per-cell diff, zero drift).
- **Documented omissions** (manifest `omissions`): application preferences, paired-device/REW endpoint state, diagnostics/logs, UI state + rebuildable caches — verified absent.
- **Manifest closure**: every `db/*.jsonl` member is manifest-declared; `assets/*` are hash+size-verified; unmanifested members rejected.
- **Authority refs**: all 7 checked joins resolve post-import (evidence→definition, field-target→evidence, supersession→inbox items, label→revision, document→head, attachment→measurement, comparison→datasets).
- **Re-export** of an imported store is deterministic; it gains exactly one table — `htdt_project_imports` (the import-provenance row — correct: provenance travels forward).

## Findings — fixed this round

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | **Machine-local receiver lane leaked into bundles** — `capture_receiver_pairings` (incl. the live `pairing_token`, LAN endpoints, `pinned_identity`), `capture_receiver_deliveries`, `capture_mission_packages`, `htdt_storage_gc_pending` were pulled in by identity edges (project_ref = project uuid, lineage/bundle digests). The manifest *declared* device state omitted while shipping a live bearer credential. | **High — spec-level data leak** | `_LOCAL_ONLY_TABLES` denylist; these tables can never enter the export walk |
| 2 | **Keyless tables duplicated on re-import** — `cad_field_evidence_targets` + ~10 `cad_*_selections` tables have no PK and no usable UNIQUE index, so re-importing the same bundle INSERTed every row again. | High — silent duplication | Whole-row `IS ?` dedupe when no usable natural key exists (identical row → reuse; different → insert) |
| 3 | **Copy import dead-ended on semantic-hash rows** — any bound `*_json` payload embedding `document_id` next to a hash column (`cad_applied_settings.applied_sha256`, `design_decisions.decision_sha256`, `cad_review_notes.semantic_sha256`, …) was refused because hashes "only the owning authority model can re-derive". The models derive hash = `canonical_sha256(payload minus hash field)` — self-verifying, so a copy *can* re-verify + recompute. | Medium — feature unusable | Recompute self-verifying semantic hashes after remap (canonical-minus-key, or text-sha256), chain them through `value_map`; embedded non-derivable hashes still fail closed |
| 4 | **Missing required column → misleading error** — a bundle predating a NOT NULL column stalled the insert loop and surfaced as 'unresolved dependency order' (and triggered the dead-end copy-retry UX). | Medium | Up-front check: NOT NULL columns with no default and no PK must be present → same 'different schema generation' `BundleManifestInvalidError`, listing `missing required columns` |
| 5 | **Malformed member escaped as `JSONDecodeError`** — a corrupt `db/*.jsonl` bypassed the manifest-invalid error class entirely. | Low | JSONL parse + row-shape validation → `BundleManifestInvalidError` |

## Verified honest behaviors (no change needed)

- Re-import of the same bundle: `import_mode='reimport'`, 0 inserted / all reused, store grows by exactly the `htdt_project_imports` provenance row.
- Partial bundle: manifest-declared member missing → `manifest lists missing table payload`; unmanifested member → rejected; **zero rows** on rollback.
- Lying manifest: `row_count` and `rows_sha256` lies both caught; lying payload → `NativeRowIntegrityError` pre-commit with clean rollback.
- Tampered asset bytes → `asset payload hash/size mismatch`.
- Version skew both directions (`2.0.0`, `0.9.9`) → `unsupported bundle schema version`, clean rollback.

## Deferred / known limitation

- **Import-as-copy on capture-linked projects fails closed.** Shared capture material (`capture_source_evidence` keyed `(bundle_digest, logical_path)`, revision graphs, content-addressed registrations) cannot be identity-remapped under plain row-copy semantics: the inbox item's scope remap cascades lineage/authority remaps while bundle-digest-keyed evidence must stay shared → unique-key conflict → `BundleImportConflictError` + full rollback. Honest refuse, never corruption — but the UX copy-retry is a dead end for capture-heavy projects. Needs capture-aware copy semantics (reuse shared capture material, remap only project-owned rows).
- Non-UUID document ids (e.g. 'doc-a' test ids) never pull the capture lane — `scope` only draws an identity edge for UUID/SHA values. Consistent with the identity model, documented here for clarity.

## Files

- `backend/src/htdt/project_bundle.py` — `_LOCAL_ONLY_TABLES` export exclusion; whole-row dedupe for keyless tables; self-verifying semantic-hash recompute on copy; required-column schema check; JSONL parse → manifest-invalid.
- `backend/tests/test_round14_bundle.py` — 6 pins covering all five fixes plus the capture-copy fail-closed contract.

## Harness

`C:\t\bundle14\fat.py` (fat fixture + per-cell diff) and `adv.py` (34-check adversarial battery) — all green.
