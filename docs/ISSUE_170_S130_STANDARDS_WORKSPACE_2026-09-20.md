# Issue #170 — S130 StandardsProfile workspace integration

Date: 2026-09-20

## Scope

This slice integrates the existing immutable/versioned StandardsProfile and StandardsEvaluation
authority into the workflow-first native UI. It does not add standards physics, inferred criteria,
a compliance score, a recommendation model, or a new workspace destination.

Starting authority: main b4843cc3b7b22d0d66032deb912f58f2ae30af85.

## UX placement

- Room > スピーカー・座席: the existing O100 placement right context now contains a
  StandardsProfile criterion panel below the system-expansion controls.
- Optimize > 比較: the existing comparison page now contains a StandardsProfile
  criterion-by-SystemVariant matrix.
- No rail item, global destination, or standalone standards workspace was introduced.
- Existing UX150 SurfaceRole / TypographyRole / ControlSize / SemanticState helpers are reused;
  no standards-specific stylesheet system was added.

## Authority reuse

- CadStandardsRepository remains the persistence authority.
- CadSystemVariantRepository and materialize_system_variant() provide exact O100 targets.
- evaluate_standards_profile() and reevaluate_standards_profile() remain the only criterion
  evaluation semantics used by S130.
- explicit_hard_constraint_gate() remains the only hard-constraint policy adapter used by S130.
- builtin_standards_profiles() is persisted idempotently so the selector exposes supported
  built-ins alongside already-persisted user-defined profiles.
- S130 adds only CadStandardsRepository.list_profiles() as a read-side enumeration helper.

## Criterion presentation

Normal UI presents:

- Japanese status labels with non-color symbols: ✓ 適合 / ✕ 不適合 / ? 判定材料不足 / — 対象外.
- observed value and unit.
- exact encoded min/max/range/equality requirement.
- predicted / measured evidence basis.
- missing input/capability reason when the evaluator returns UNKNOWN.
- profile name/version/source/applicable scope.

Raw UUID/hash values are not used as normal display labels. Advanced provenance exposes exact
SceneRevision and optional SystemVariant identity/hash, profile identity/version/hash, evaluator
and evaluation identity/timestamp/hash, criterion source/version/reference/hash, re-evaluation
lineage, and evidence identity/hash.

## Explicit hard-constraint semantics

Constraint selection is UI-local policy state and does not mutate StandardsEvaluation identity.
Only checked criterion IDs are passed to explicit_hard_constraint_gate().

| criterion state | unchecked | checked |
|---|---|---|
| PASS | advisory | allowed |
| FAIL | advisory; does not remove/block candidate | blocks |
| UNKNOWN | advisory | blocks fail-closed |
| NOT_APPLICABLE | irrelevant | non-blocking |

No criterion is converted to a Pareto objective. No pass count, total compliance score, winner,
or hidden ranking is calculated.

## Historical re-evaluation

Profile versions remain separate selector entries. If a newer version of the same profile is
evaluated for the exact same target, S130 reuses only compatible persisted observations and calls
reevaluate_standards_profile(), producing a new immutable evaluation linked by reevaluation_of_id.
The older evaluation remains readable and is never overwritten.

## Missing evidence / capability

S130 deliberately does not derive missing geometry/measurement quantities or manufacture
capabilities. When the backend authority cannot evaluate a criterion, the result remains UNKNOWN
and the UI explains missing inputs/capabilities when provided by the evaluator. It is not rendered
as FAIL.

## Automated software acceptance

backend/tests/test_standards_workspace.py covers:

1. exact persisted evaluation rendering;
2. PASS / FAIL / UNKNOWN / NOT_APPLICABLE presentation with Japanese normal labels;
3. profile version distinction;
4. explicit hard-constraint opt-in;
5. unselected FAIL remains non-blocking;
6. selected UNKNOWN is fail-closed;
7. exact SystemVariant comparison binding;
8. Japanese-first user-facing labels and no raw UNKNOWN/NOT_APPLICABLE in normal status cells;
9. preservation of historical evaluation during newer-version re-evaluation;
10. first-class placement in existing Room and Optimize contexts.

Existing workflow/headless Qt tests remain regression coverage for navigation, canonical contexts,
and UX150 software layout behavior.

Final GitHub Actions run IDs/results are recorded in the PR and implementation status after CI.

## UX160 boundary / screenshots

No Windows screenshots are produced in S130 because the requested acceptance is software-only.
Actual Windows DPI/font rasterization, mouse interaction, GPU/3D appearance, screenshots, and
first-use visual/discoverability acceptance remain explicitly pending under UX160.

## RDC

RDC usage: **0**.
All implementation, review, and software validation for this slice is performed through GitHub.
