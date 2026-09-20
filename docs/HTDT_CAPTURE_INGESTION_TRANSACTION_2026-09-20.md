# HTDT-Capture production ingestion transaction slice

Date: 2026-09-20

Related:
- HTDT-Capture Issue #7
- HTDT raw visual mesh authority / Issue #167
- PR #247 HTDTMSH1 -> RawVisualMesh adapter

## Purpose

This slice adds the production-side atomic commit boundary for a validated
`htdt.capture.ingestion-plan` v1 and the exact payload bytes named by that
plan.

It intentionally still stops before semantic acoustic conversion.

```text
validated Capture Bundle
  -> deterministic ingestion plan v1
  -> backend revalidation of deterministic lineage identities
  -> exact payload length/SHA verification
  -> stage HTDTMSH1 RawVisualMesh bindings
  -> BEGIN IMMEDIATE
       source evidence bytes + metadata
       RoomPlan records
       RawVisualMesh bindings
       annotation/measurement authority handoffs
     COMMIT
```

No partial source authority survives a failed transaction.

## Backend-side plan validation

The backend does not blindly trust a syntactically valid plan. It recomputes:

- every source-evidence ID;
- every raw-mesh handoff ID;
- every annotation/measurement authority handoff ID;
- the overall lineage digest.

It also checks capture revision, session and coordinate references, exact
source-evidence links, and mesh geometry path/hash linkage.

## Exact source evidence

Every source-evidence record is persisted with its exact immutable payload bytes
and manifest-derived metadata. Reopen verifies the stored byte count and SHA-256
before returning the evidence.

This initial native implementation uses SQLite BLOB storage. That is a bounded
authority implementation, not a claim that SQLite BLOBs are the final
large-capture storage architecture. A later content-addressed blob backend may
replace the physical storage while preserving this logical repository contract.

## RawVisualMesh

Each mesh handoff is adapted with the PR #247 `HTDTMSH1` adapter before any
database transaction is committed. The resulting immutable
`CaptureRawVisualMeshBinding` remains explicitly `solver_ready=false`.

No fusion, repair, semantic conversion or SceneRevision promotion occurs here.

## Idempotency

Re-ingesting the same validated plan is deterministic and returns the existing
ingestion authority without duplicating source evidence.

A lineage digest collision with different canonical plan semantics fails closed.

## Verification

Focused tests cover:

- complete atomic source-authority commit;
- exact payload reopen;
- RawVisualMesh exact source-byte preservation;
- annotation authority handoff persistence;
- deterministic re-ingestion;
- payload tamper/length mismatch before commit;
- zero partial source records after failure;
- backend-side deterministic identity rejection.

No RDC is required.


## Backend hardening on rebase

The production boundary independently pins reference ingestor v1.0.0 and its
strict configuration digest. A plan produced by another ingestor version or
configuration is unsupported until this backend contract is explicitly
versioned.

The backend also fails closed on:

- non-NFC, absolute, backslash, dot-segment, empty-segment, or case-colliding
  source logical paths;
- unresolved manifest-style `sha256:` and `path:` source references;
- RoomPlan kind/path/provenance disagreement;
- mesh handoffs whose anchor-index or geometry source is not the exact ARKit
  reconstruction authority;
- mesh anchor-record locator disagreement;
- annotation/measurement handoffs whose source payload path or record locator
  is inconsistent with the handoff kind.
