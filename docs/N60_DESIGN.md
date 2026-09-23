# N60 native measurement workspace — design

> Tracking: Issue #61  
> Base: `main` at `160c62e77ca9e1a32510795bf93604a6e7a3fd44`  
> Acceptance: A12 / A13 in `CAD_EDITOR_ACCEPTANCE.md`

## 1. Goal

N60 connects saved and imported acoustic measurements to the native CAD editor without making the mutable current scene the meaning of old data.

A measurement is evidence about one exact saved `SceneRevision` and one explicit measurement/acoustic reference point. Moving speakers, seats, or measurement points later must never make an old measurement look as if it was captured in the new arrangement.

N60 also adds a native FR workspace, historical-placement ghost visualization, A/B comparison, and stale/cancel protection for asynchronous REW reads.

The browser UI remains frozen.

## 2. Reuse existing assets, do not reuse the legacy Context as authority

Reuse these existing modules:

- `rew_api.py`: localhost-only, read-only REW measurement discovery and immutable frequency-response snapshot read
- `rew_parser.py`: REW text export parser and source SHA-256
- `comparison.py`: deterministic FR comparison on the existing logarithmic grid
- existing quality/provenance terminology from `DATA_AND_ANALYSIS.md` / `MEASUREMENT_WORKFLOW.md`

Do **not** make legacy `database.Store.contexts` the authority for native CAD. Native placement authority is `SceneRepository.scene_revisions`.

The legacy Store remains available to the old application path and can later be migrated/imported explicitly; N60 does not create a hidden automatic migration between two history systems.

## 3. Native measurement model

Create Qt/VTK-independent immutable models in a new native measurement module.

### 3.1 Measurement binding

Each measurement binds to:

- stable HTDT `measurement_id`
- `document_id`
- exact `scene_revision_id`
- exact `scene_content_hash`
- `measurement_entity_id` (`measurement_point` or physical object with explicit acoustic reference)
- immutable `measurement_position` snapshot in HTDT metres
- optional immutable direction snapshot when known
- evidence type: `measured | derived | predicted | unknown`
- channel/input role and source speaker IDs without conflating role with physical speaker ID
- captured time if known; import time separately
- quality state/reasons/source, preserving unknown
- provenance/source kind (`rew_api`, `rew_text`, later other explicit importers)

Binding creation validates that the revision belongs to the same document and that the named measurement entity exists in that exact revision. The stored position must equal `acoustic_reference_position(entity)` for that revision at binding time.

A measurement cannot be rebound in place to a later revision. A correction creates a new record/revision rather than mutating old evidence.

### 3.2 Frequency-response dataset

Initial native dataset scope:

- original frequency points, no resampling at save
- level values
- optional phase values
- phase status (`valid | absent | unknown`; current import paths generally produce absent/unknown unless stronger evidence exists)
- level reference
- smoothing/resolution/request metadata when known
- original/raw source SHA-256
- importer/adapter version

All numeric arrays are immutable once stored.

## 4. Native persistence

Add a `CadMeasurementRepository` beside `SceneRepository`, using the same `cad-scenes.sqlite3` file so native foreign-key relationships are local and inspectable.

Recommended tables:

- `cad_measurements`
- `cad_frequency_responses`
- `cad_measurement_assets`
- `cad_measurement_asset_links`

`cad_measurements.scene_revision_id` references `scene_revisions.revision_id`.

Raw REW text/API snapshot bytes are content-addressed by SHA-256 in a sibling native asset directory, not committed to Git. Duplicate bytes may be shared by multiple import records.

The repository must refuse:

- missing revision
- revision from another document
- unknown measurement entity
- measurement entity whose stored acoustic reference does not match the captured snapshot
- array-length mismatch / non-finite numeric data
- mutation of an existing measurement/dataset payload

## 5. Import adapters

### 5.1 REW API

Use `RewApiClient.get_frequency_response_snapshot()` unchanged for the external read. It already verifies the selected REW UUID is unique and that the REW measurement summary did not change during the snapshot read.

Normalize the snapshot into the native measurement model only after binding an exact native `SceneRevision` and measurement point.

The external REW UUID remains provenance, never the HTDT primary key.

### 5.2 REW text

Use `parse_rew_frequency_response(raw)` unchanged for supported text exports. Preserve its source SHA, phase warning, original frequency grid, and parser version.

Import UI must not invent capture time, SPL calibration, microphone identity, or routing evidence from filename/header text when evidence is insufficient.

## 6. FR workspace

Add a new product-composition layer over `ConstraintEditorWindow`, tentatively `MeasurementEditorWindow`.

The native `実測` dock contains:

- saved measurement list with evidence/status labels
- selected measurement metadata (revision, point, role, source, quality)
- FR graph
- current-vs-historical scene indicator
- A/B comparison selection
- REW connection/read action
- text import action

PyQtGraph is the preferred FR plotting candidate because it is optimized for interactive numeric plots and supports PySide6. N60 should add it only if CI/package verification succeeds; do not create a custom plotting engine.

Graph requirements:

- logarithmic frequency axis
- dB level axis with unit/reference label
- distinguish A/B/difference using line style/label in addition to color
- no smoothing unless it is explicitly part of the imported dataset/request
- saved data remains viewable when REW is not running

## 7. Historical placement ghost

When the selected measurement's source revision differs from the currently open revision:

- load the immutable source `SceneRevision` from `SceneRepository`
- draw relevant measured-source speakers and measurement point as non-pickable ghost actors
- use dashed/wireframe/label semantics in addition to color/opacity
- keep current objects editable; ghost actors are never editing targets
- show source revision ID/hash and a clear `測定時配置` label

The ghost uses the exact saved revision, not reconstructed coordinates copied from the current scene.

If the same source revision is current, no misleading duplicate ghost is drawn.

## 8. Comparison

Reuse `compare_frequency_responses()`.

A comparison stores or displays:

- exact dataset IDs A/B
- source scene revision IDs for A/B
- requested and actual overlap band
- algorithm version
- difference curve and summary values

Do not auto-level unless the user explicitly requests the existing reference-band operation. Do not interpret lower RMS as an overall sound-quality score.

## 9. Async job / stale-result contract

External REW calls must not block the GUI thread once connected to the native dock.

Introduce a small job-token layer independent of Qt rendering.

Each submitted job captures:

- job ID / monotonically unique token
- document ID
- source SceneRevision ID + content hash
- measurement entity ID + position snapshot
- N50 constraint workspace content hash/snapshot if the job semantics depend on constraints
- requested REW UUID/query

Before applying a completed result to UI/current selection, verify:

1. job was not cancelled,
2. active document still matches,
3. relevant source revision/hash still matches the job target,
4. this result is still the latest applicable token for that UI operation.

Stale/cancelled results may be logged or stored only through an explicit immutable import path; they are never silently applied to the current scene.

Changing scene geometry/placement does not invalidate already-saved measurement evidence; it only makes it historical relative to the current revision.

## 10. A12

Fixture:

1. save SceneRevision A with a measurement point and speaker placement,
2. import/save a measurement bound to A,
3. move a speaker/point and save SceneRevision B,
4. select the A measurement while B is current.

Pass:

- A measurement still references A revision/hash/point snapshot exactly
- A FR is viewable with REW unavailable
- ghost shows A placement and is distinguishable from current B
- current B entities remain editable
- selecting/comparing B does not relabel A as B evidence

## 11. A13

Fixture:

1. submit a delayed REW read for revision A,
2. edit/save revision B while job is pending,
3. cancel one job,
4. switch document/project for another pending job,
5. allow delayed completion.

Pass:

- cancelled result not applied
- A result not attached to B/current document automatically
- UI remains responsive
- application exits cleanly without live worker/observer residue
- exact captured input token is available for diagnostics

## 12. Focused automated verification

Add tests only for high-cost invariants:

- binding rejects wrong document/revision/entity
- binding snapshots exact acoustic reference point
- saved measurement remains bound after later scene revision
- FR arrays/provenance round-trip exactly
- raw asset SHA/content deduplication
- REW API snapshot normalization keeps external UUID as provenance only
- text parser normalization preserves source hash and unknown semantics
- ghost source loads exact historical SceneRevision
- comparison preserves dataset/revision IDs
- cancelled/stale job cannot become current applied result

Do not add graph-pixel snapshots.

## 13. Explicit non-goals

- no automatic REW measurement capture/control in this milestone; read/import first
- no prediction engine
- no optimization/recommendation
- no migration of every legacy Store record into native history
- no browser implementation
- no generic task framework beyond what A13 needs
- no RDC coding
