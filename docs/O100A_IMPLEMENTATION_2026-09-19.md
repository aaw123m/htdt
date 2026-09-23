# O100A implementation record — Proposed system variant authority

Date: 2026-09-19  
Tracking: Issue #142 / PR #144

## Implemented scope

O100A introduces immutable proposal authority without changing the baseline `SceneRevision`.

Implemented domain objects:

- `SystemVariant`
- `ProposedEntitySpec`
- `ChannelRoleBinding`
- `EntityLifecycleBinding`
- `VariantEntityDiff`
- `ProposalEvidenceRef`
- `SystemVariantApplication`
- `SystemVariantComparisonRef`

The variant is bound to an exact baseline by both `baseline_revision_id` and `baseline_content_hash`. Its semantic payload is content-hashed as `variant_sha256`.

## Exact topology diff

O100A represents speaker topology changes explicitly as:

- `add`
- `remove`
- `replace`

Each diff stores the exact before/after `SceneEntity` payload and the baseline/final insertion index required to reproduce the variant deterministically.

A proposed 5.0.2 system can therefore be represented from a current 3.0.2 revision by adding SL/SR while keeping the baseline revision unchanged.

## Lifecycle separation

Persisted lifecycle states are:

- `current`
- `proposed`
- `as_built`
- `measured`

`ProposedEntitySpec` is always `proposed`.

Measurement IDs are accepted only by a lifecycle binding whose state is `measured`. A `proposed`, `current`, or `as_built` binding cannot carry measurement IDs. This prevents absent/proposed speakers from receiving fake measurement evidence.

This slice only defines the lifecycle authority. The installation/promotion workflow that creates an As-built state and the later measurement campaign remain O100G work.

## Baseline immutability and explicit apply

`materialize_system_variant()` derives a new `SceneDocument` from one immutable baseline revision. It never mutates the baseline object or baseline database row.

`CadSystemVariantRepository.apply_variant()` is the explicit selection/apply operation:

1. load the persisted variant;
2. re-validate the exact baseline revision/hash;
3. deterministically materialize the proposed document;
4. reject a no-op variant;
5. save one new `SceneRevision` with the baseline as parent;
6. persist `SystemVariantApplication` linking the proposal to the applied revision.

Repeated apply of the same persisted variant returns its existing application rather than creating duplicate revisions.

## Proposal evidence lineage

A `SystemVariant` can keep typed references to proposal-side evidence:

- prediction
- objective
- robustness
- comparison
- explicit user decision

These are references only. O100A does not redefine the corresponding O30/O40/O90/R-series authority.

Physical measurement is intentionally not a `ProposalEvidenceKind`. Applied proposal lineage is preserved as:

`SystemVariant -> SystemVariantApplication -> SceneRevision`

The original proposal remains immutable after apply.

## Persistence

`CadSystemVariantRepository` stores immutable records in the native CAD SQLite database:

- `cad_system_variants`
- `cad_system_variant_applications`

Both tables bind back to existing `scene_revisions` by foreign key. No alternative SceneRevision store or proposal-only scene database was introduced.

## Undo and comparison integration points

`WorkingDocument.replace_document()` applies an exact same-document replacement as one command-history entry. `apply_system_variant_to_working_document()` uses it only when the WorkingDocument still matches the exact variant baseline, so a topology change is one Undo/Redo unit.

`CadSystemVariantRepository.comparison_ref()` exposes stable baseline/proposed/applied revision hashes and IDs. It is deliberately a reference interface; comparison scoring and Pareto semantics stay with existing authorities.

## Explicit non-scope / remaining work

Not implemented in O100A:

- O100B virtual-channel placement search
- allowed/exclusion-region search or linked SL/SR search
- O10/O80 candidate generation for proposed entities
- EquipmentDefinition or source/directivity capability
- O100D objective/topology comparison logic
- O100E multi-fidelity search
- new O90 robustness semantics
- GUI / ghost rendering / workflow-shell integration
- installation completion / As-built promotion workflow
- REW measurement campaign binding for newly installed systems

O100B and later slices must consume this variant authority rather than recreating topology/lifecycle state.

## Targeted verification

`backend/tests/test_cad_system_variant.py` covers:

- current 3.0.2 -> proposed 5.0.2 without baseline mutation;
- explicit SL/SR proposed lifecycle;
- exact add/remove/replace diff representation;
- one-step WorkingDocument Undo for a topology replacement;
- SQLite persistence and explicit apply -> new SceneRevision;
- proposal evidence lineage after apply;
- stable comparison references before/after apply;
- idempotent repeat apply;
- rejection of measurement evidence on proposed lifecycle state.

CI / package workflow results are recorded in PR #144.
