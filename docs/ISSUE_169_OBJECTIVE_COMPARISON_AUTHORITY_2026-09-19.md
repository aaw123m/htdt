# Issue #169 prerequisite — objective direction / comparison authority

Status: implemented on the Issue #169 prerequisite branch. This slice changes comparison
authority only; it does not implement coverage, SPL, or headroom physics.

## Scope

The optimization objective contract now has an explicit, immutable
`ObjectiveDefinition` for new objectives. A definition carries:

- objective ID and physical quantity;
- unit;
- direction: `minimize` or `maximize`;
- declared valid domain;
- comparison-model ID and version;
- an explicit identity physical-value transform version;
- a deterministic semantic hash / definition ID.

The authoritative value remains the displayed physical value. Pareto and O90 compare that
value directly according to direction. There is no hidden loss value and no implicit sign
flip.

`ObjectiveMetric.state` is `available`, `missing`, or `unsupported`. Missing and
unsupported metrics carry no numeric value and are not comparison-eligible; they are never
substituted with 0, infinity, or another favorable sentinel.

## Comparison eligibility

Two candidates may compare an objective only when the selected metrics resolve to the same
objective-definition identity. Therefore quantity, unit, direction, valid domain,
comparison-model identity/version, and transform version must match exactly.

A mismatch fails closed before dominance is evaluated. Pareto dominance is evaluated per
axis:

- minimize: lower is better;
- maximize: higher is better.

No aggregate score is introduced.

The comparison-model fields describe the semantics of the objective value itself. They are
not a speaker/source capability model and do not replace or pre-implement Issue #168
`EquipmentDefinition`.

## O90 semantics

O90 continues to retain nominal, sampled-envelope, infeasible, failed, percentile, and
probability semantics separately.

For `sampled_worst`:

- minimize objective -> highest scored physical value is adverse;
- maximize objective -> lowest scored physical value is adverse.

Infeasible and failed samples remain explicit evidence and are not converted into scored
values. A missing/unsupported perturbed metric can remain explicit in its sample vector and
is not used as a numeric sampled value.

Nominal/robust Pareto reconstruction preserves the original objective direction and exact
definition authority. In particular, `robust.sampled_worst::*` is no longer forced to
`minimize`.

## Migration and backward compatibility

Existing minimize-only `ObjectiveMetric` records without an explicit definition remain
valid. They resolve to a deterministic legacy comparison definition
(`legacy-objective-metric`, version 1, finite-real domain) for comparison purposes.

Legacy semantic identity is preserved deliberately:

- legacy `ObjectiveMetric` identity retains the historical four fields
  (`objective_id`, `value`, `unit`, `direction`);
- `ObjectiveVector.identity_payload()` preserves the historical vector hash shape;
- CAD objective evaluation hashes use that semantic payload;
- O90 perturbation sample hashes use that semantic payload;
- old `RobustnessEvaluation` identities omit the new optional definition field.

As a result, previously persisted minimize-only objective/O90 evidence can reopen under the
new code without changing its semantic hash. New explicit definitions become part of the
semantic identity.

Pareto results use `pareto-front-1` for pure legacy minimize-only vectors and
`pareto-front-2` when formal direction-aware authority is used.

## Focused verification

The focused fixtures cover:

- legacy minimize-only Pareto regression;
- mixed minimize/maximize nominal Pareto trade-off;
- fail-closed unit/direction/comparison-model mismatch;
- missing/unsupported non-numeric state;
- deterministic definition identity and valid-domain enforcement;
- explicit-definition persistence/reopen;
- O90 maximize sampled-worst selecting the low side;
- O90 persistence/reopen with the explicit definition;
- mixed-direction nominal + robust sampled-worst Pareto.

## Deferred to the main Issue #169 slice

This prerequisite intentionally does not calculate any of the following:

- coverage;
- SPL;
- continuous/peak headroom;
- worst-seat coverage/SPL/headroom;
- source/equipment capability.

The main Issue #169 implementation must consume the approved EquipmentDefinition/source
authority rather than creating a second capability model. It must define the physical
calculation/evidence contracts for coverage/SPL/headroom, bind them to exact
SceneRevision/SystemVariant/source/model evidence, and emit `missing`/`unsupported`
when the required capability is absent. The objective values produced there can then use
the comparison authority implemented by this prerequisite.
