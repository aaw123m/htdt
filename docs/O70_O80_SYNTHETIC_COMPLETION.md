# O70/O80 Synthetic Software Completion Lane

Tracking: Issue #90

## Purpose

Physical owned-room measurement is not required to finish the software implementation.
HTDT therefore has an explicit synthetic development lane for O60/O70/O80.

This lane is **not** a substitute for real validation. Synthetic records remain
`synthetic_fixture`, never acquire an owned-room campaign ID, and never make
`production_owned_room` recommendation eligible.

## O70 Adaptive Planner

O70 persists an immutable Adaptive Plan bound to:

- exact SearchSpec ID/SHA;
- exact candidate-set SHA;
- exact O60 ValidationRecord ID/SHA;
- model ID/version;
- calibration candidate IDs;
- measured/excluded candidate IDs;
- objective IDs;
- GP length scale;
- acquisition function and algorithm version;
- every proposal and its corrected objective estimate / residual uncertainty.

Two scopes are intentionally separate:

- `development_synthetic`: accepts only a synthetic O60 record for which every
  technical gate passes and the **only** stop reason is missing owned-room
  evidence.
- `production_owned_room`: accepts only the current persisted campaign-backed
  `eligible` O60 record.

O70 uses per-objective residual correction and uncertainty. It does not collapse
independent objectives into a single sound-quality score.

O80A adds a separate immutable **Adaptive Extended Plan** rather than changing
the accepted O70 plan schema. Its feature vector combines base O10 XYZ axes and
the exact O80 extended axes. Each feature is normalized by its immutable axis
span before the GP kernel is evaluated, so metres and degrees are never treated
as the same raw unit. The plan binds the exact Extended SearchSpec/capability/
candidate-set SHA and excludes extended candidates whose current observation
contains measured evidence.

## O80 Extended Search

The existing O10 SearchSpec already supports multiple entities and X/Y/Z axes,
including speaker/listener height. O80 therefore does not duplicate that engine.

O80 layers additional model-dependent parameters on the already feasible O10
candidate set. Two yaw parameters are implemented:

- speaker horizontal **acoustic aim** (`aim_yaw_deg`), which rotates only
  `aim_xyz`;
- **physical cabinet toe-in** (`body_yaw_deg`), which rotates the body
  quaternion and rotates `aim_xyz` by the same yaw delta so the body/aim
  relationship is preserved.

New O10 SearchSpecs persist the exact source-orientation cabinet XY footprint
instead of reducing the cabinet to a circular envelope. O80P body-yaw candidates
then recompute the oriented cabinet footprint and re-run affected room,
allowed/exclusion, wall-clearance, and envelope pair-distance hard constraints.
Existing stored radius-only ConstraintSets/SearchSpecs remain replayable.

An extended candidate contains:

- immutable base SearchSpec ID/SHA;
- immutable base candidate-set SHA;
- exact base candidate ID and XYZ payload;
- explicit extended model-capability ID/SHA;
- exact acoustic-aim and/or body-yaw values;
- deterministic extended candidate ID and set SHA.

Preview does not alter Scene/Undo history. Explicit apply updates position,
body orientation, and aim in one command, so one Undo restores the complete
candidate pose.

### Model capability gate

Extended parameters are unavailable unless a persisted model capability explicitly
declares support.

REW Room Simulator is rectangular position-based and does not model speaker
direction/toe-in. HTDT therefore rejects any attempt to declare
`aim_yaw_deg` or `body_yaw_deg` support for REW Room Simulator.

The synthetic software lane uses
`synthetic-directional-fixture/1`. It exists only to exercise the full software
path. A future owned-room directional model must have its own O60 evidence and
eligible ValidationRecord before a production extended capability can be saved.

## Real-repository synthetic demo

The demo is deliberately stored through normal product repositories rather than
through fake test repositories.

Run:

```powershell
python -m htdt.native_cad --data-dir <demo-data-dir> --seed-synthetic-demo
```

The command exits before QApplication creation and persists a separate document:

`htdt-synthetic-o70-o80-demo-v1`

It writes:

1. SceneRevision;
2. O10 SearchSpec and deterministic candidate set;
3. O20-style synthetic prediction batch/attempts;
4. candidate-applied SceneRevisions and O50 Measurement Plans;
5. N60 measurement records explicitly marked `synthetic_fixture` and
   `physical_measurement=false`;
6. predicted/measured O30 objective evaluations;
7. a full O60 ValidationRecord whose residual/trend/sensitivity/repeatability/
   separation/applicability gates pass, while recommendation remains disabled
   solely because evidence is not owned-room;
8. an O70 `development_synthetic` Adaptive Plan;
9. an O80 synthetic directional capability and Extended SearchSpec;
10. immutable extended-candidate objective observations and an O80A
    `development_synthetic` Adaptive Extended Plan over base X + acoustic aim yaw.

The demo refuses a duplicate seed in the same data directory.

To inspect it in the native application, launch that same data directory with:

```powershell
python -m htdt.native_cad --data-dir <demo-data-dir> --document-id htdt-synthetic-o70-o80-demo-v1
```

## Acceptance result

Software completion was accepted on 2026-09-18:

- O70 core: PR #92;
- O70 native UI: PR #93;
- O80 + real-repository synthetic completion: PR #94;
- PR #94 merge: `6faf554bcf3670f64ff13c530fa4fc79ab1881b8`;
- CI #548 / run `35313405578`: **PASS**;
- Windows Release Artifact #93 / run `35313405629`: **PASS**;
- packaged `--seed-synthetic-demo`: **PASS**;
- per-user installer build and install/uninstall data-retention smoke: **PASS**;
- RDC: **not used** for this completion slice.

These results establish software-path completeness and packaging integrity. They do
not establish real-room acoustic validity; that remains Issue #83.

## Production boundary

None of the following is permitted:

- relabel synthetic measurement as owned-room;
- attach a synthetic Validation Campaign;
- use a synthetic O60 record for `production_owned_room`;
- declare REW Room Simulator to support toe-in;
- use synthetic O80 capability as owned-room model evidence;
- use synthetic Adaptive Extended observations or plans to unlock
  `production_owned_room`.

The later physical campaign still uses O60E preregistration and O60R audit. The
real O60R runner freezes the final measured software authority only when the
physical campaign is actually performed.
