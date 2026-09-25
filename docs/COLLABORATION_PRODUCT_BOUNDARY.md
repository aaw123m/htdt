# HTDT Collaboration Product Boundary — Issue #730

> 制定: 2026-09-24 / 対象: project exchange・review・field return・future collaboration feature scope 判断

## Decision

For the current product generation:

**Support bounded exchange and review. Defer real-time/cloud multi-user editing.**

HTDT is a Windows/personal/local-first product (see
`docs/PRODUCT_DOMAIN_BOUNDARY_CHARTER.md`). Project exchange, review
notes, and field return are supported through explicit artifacts and
exact provenance — never through accounts, cloud sync, or server-side
collaboration.

## Supported collaboration surface

### Project exchange
- export/import one exact `.htdtproject` bundle;
- preserve dependency closure and source build/version identity;
- import as an independent copy when needed;
- never silently merge conflicting authorities.

### Human-readable review
- reports, design comparison summaries, commissioning reports,
  installation sheets, diagnostic/support packages.

### Review notes / decisions (`cad_review_note`)
- bounded immutable annotations pinned to exact authority:
  SceneRevision, SystemVariant, commissioning deviation, design
  alternative, installation evidence, or project document;
- optional `subject_sha256` pins the exact authority content;
- `author_label` is a human label — no accounts, mentions, or identity
  service required;
- notes remain strictly separate from physical/measured truth.

### Field return
- field/Capture evidence returns through explicit staged import/pairing
  (`.htdtfieldreturn`, `cad_field_session`) with exact source identity;
- a stale session never silently writes into a different current design.

## Explicitly deferred

- real-time co-editing;
- cloud project database / account-login service;
- presence indicators, shared cursors;
- server/CRDT conflict-free editing;
- comments requiring cloud identities;
- SaaS permission/RBAC systems;
- online-only project access.

These are product-scope changes, not incremental UI features. Any future
issue assuming them requires an explicit scope decision first.

## Merge semantics

Divergent project copies are never "synced latest". Supported behavior:

1. import as an independent copy; or
2. explicit bounded reconciliation for known authority types.

Any future merge engine must preserve immutable historical authorities,
exact provenance, and conflicting alternatives — no silent winner
selection.

## Revisit criteria

Cloud/multi-user collaboration is reconsidered only with concrete product
evidence of recurring needs unmet by portable bundles, reports, field
companion, and staged review/import — via a separate architecture/product
decision.
