# N70 design — prediction / visualization

Tracking: Issue #63  
Branch: `feat/n70-prediction-visualization`  
Base: `86354dd58200738b33ae27261a5308e3551bcbac`

## 1. Goal

N70 adds a native prediction/visualization layer on top of the accepted N60 CAD + measurement workspace. The layer must preserve evidence semantics: measured data remains measured, model output remains predicted, and geometry-only candidates are not promoted to an SPL/FR prediction.

The first implementation slice is N70a: immutable prediction authority plus native visualization of the existing rectangular room-mode and first-order-reflection geometry. N70b adds a model-gated scalar-field pipeline only after a predictor can supply a real field with documented applicability.

## 2. Existing assets and what they mean

`backend/src/htdt/acoustics.py` already provides:

- `rectangular_room_modes()` — mode frequencies/classes for a rectangular box;
- `first_order_reflections()` — geometric image-source reflection points/path lengths for six box surfaces;
- `analyze_rectangular_context()` — legacy-context adapter classified as `predicted_geometry_candidate` with explicit assumptions.

Its current algorithm version is `rect-room-geometry-1`. It does **not** model reflection amplitude, phase, absorption, speaker directivity, modal amplitude, damping, or a spatial SPL field. N70a therefore reuses the pure geometry functions but does not label their output as a predicted FR or heatmap.

## 3. Prediction authority

Introduce native immutable prediction records bound directly to native SceneRevision rather than legacy Context.

Minimum run identity:

- HTDT prediction run ID;
- document ID;
- exact source SceneRevision ID and scene content hash;
- optional N50 constraint workspace hash when the computation depends on constraints;
- model ID and model version;
- result kind;
- geometry compatibility classification;
- canonical parameter JSON;
- canonical model-input snapshot/hash;
- submitted/completed timestamps;
- state: completed / cancelled / failed only as immutable run outcome metadata;
- assumptions/warnings.

Result kinds begin with:

- `geometry_modes`;
- `geometry_reflections`;
- later `scalar_field`.

Geometry compatibility is explicit:

- `exact_for_model_geometry`;
- `rectangular_approximation`;
- `unsupported`.

No silent conversion from polygon room to rectangular input is allowed.

## 4. Native rectangular geometry adapter

Create a Qt-free adapter from native `SceneRevision` into the existing pure `acoustics.py` functions.

For N70a:

- accept only a true rectangular native room as exact input;
- obtain receiver from the selected measurement/acoustic-reference entity;
- obtain speaker source points from each speaker acoustic reference;
- preserve speaker entity ID and role;
- map surfaces to stable native wall/top/bottom identities where possible;
- record sound speed and maximum mode frequency in parameters;
- preserve `rect-room-geometry-1` as the underlying geometry algorithm version.

For non-rectangular rooms the exact adapter returns an explicit unsupported result/reason. A future approximation path must require an explicit reference-box rule and save that rule/box as part of model input.

## 5. Storage

Use the same native `cad-scenes.sqlite3` authority as SceneRevision/N60 measurements.

N70a stores compact geometry results in normalized immutable prediction tables or canonical JSON payloads with foreign-key binding to the source revision. Do not store derived results back into SceneDocument.

N70b scalar fields should use metadata in SQLite and a content-addressed binary artifact for large arrays. The first field fixture is 64^3; do not inflate the scene DB with repeated JSON float grids.

## 6. Async / stale semantics

Prediction computation must run outside the GUI thread when it can become non-trivial.

Do not modify the accepted N60 `MeasurementJobGuard` contract merely to make it generic. Add a prediction-specific token/guard that captures:

- document/revision/content hash;
- constraint workspace hash when relevant;
- model ID/version;
- canonical parameters/input hash.

A completion can be applied to current UI state only when the active document/revision/hash and all other captured mutable authorities still match. Cancelled, superseded, stale or document-switched results are retained only if deliberately persisted as historical completed evidence; they are never rebound to the current scene.

## 7. Native UI

Add `PredictionWorkspaceWindow` above `MeasurementWorkspaceWindow`; do not duplicate earlier CAD layers.

A `予測` right-side context dock participates in the N60 single tab stack. N70a UI exposes:

- model/result type and algorithm version;
- exact / approximation / unsupported geometry state;
- source revision and stale/current state;
- assumptions/warnings;
- room-mode frequency/class list;
- reflection list with speaker, surface, direct/reflected/excess path, delay and geometry-only destructive-frequency candidate;
- layer toggles for reflection points and paths.

Viewport overlays:

- reflection point marker;
- direct source→receiver segment;
- reflected source→surface→receiver polyline;
- non-pickable analysis actors;
- label/pattern/shape semantics in addition to color.

Room-mode data has no spatial amplitude in the current model; do not fake a heatmap from mode frequency alone.

## 8. Scalar field gate (N70b)

Heatmap/slice/volume controls remain disabled unless a persisted result really contains a scalar grid with:

- axes/origin/spacing or explicit coordinates;
- units/reference;
- frequency/band;
- model ID/version;
- source revision/input hash;
- geometry compatibility;
- assumptions and validity domain.

REW Room Simulator may be used only as a rectangular baseline/approximation after S01 confirms a stable input/output contract. A polygon predictor such as pyroomacoustics requires S03-equivalent Windows install, coordinate, boundary, low-frequency accuracy, performance and reproducibility evidence before adoption.

## 9. Acceptance

### A13
Prediction job start → edit → cancel → document switch → delayed completion must not apply a stale result. GUI remains responsive; close leaves no worker.

### A14
Selecting a rectangular-only model for a non-rectangular room must visibly produce unsupported or an explicitly requested/saved rectangular approximation. The polygon room remains authoritative.

### F5
Benchmark 10,000 analysis markers without one actor per marker. When scalar-field support exists, benchmark a 64^3 field for storage size, load time, memory and interaction frame time, then set a measured budget.

## 10. Non-goals

- N80 search/Pareto/recommendation;
- automatic placement claims;
- treating mode frequencies as measured peaks/nulls;
- inferring reflection severity without amplitude/phase/material/directivity;
- exact polygon acoustics before a model gate;
- browser/FastAPI duplicate UI;
- a generic plugin framework before a second validated prediction model actually requires one.
