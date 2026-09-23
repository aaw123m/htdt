# HTDT Capture HTDTMSH1 -> RawVisualMesh adapter slice

Date: 2026-09-20

Related:
- HTDT-Capture Phase 6 / Issue #7
- HTDT Issue #167
- HTDT Issue #101 geometry authority chain

## Purpose

This slice adds the first production-backend source-specific adapter for the
validated HTDT-Capture mesh handoff.

It does not ingest an untrusted `.htdtcapture` archive directly and it does not
promote capture geometry into `SemanticAcousticGeometry`.

The bounded chain is:

```text
validated htdt.capture.ingestion-plan v1 mesh handoff
+ exact referenced .meshbin bytes
  -> HTDTMSH1 v1 raw decoder
  -> immutable RawVisualMesh
  -> CaptureRawVisualMeshBinding
```

## RawVisualMesh extension

The existing raw visual authority now recognizes one additional explicit source
format:

`htdt_meshbin_v1`

The exact original `HTDTMSH1` bytes remain embedded in the immutable
`RawVisualMesh`, with their SHA-256 and byte length validated in the same way
as OBJ/GLB source assets.

OBJ/GLB parsing remains importer version 1. The new HTDTMSH1 parser is marked as
importer version 2 so existing provenance is not silently rewritten.

The parser validates:

- exact `HTDTMSH1` magic and version 1.0;
- 32-byte header;
- UInt32 triangle indices;
- known flags only;
- zero reserved fields;
- exact payload length from declared counts;
- finite vertex/normal values;
- index bounds;
- non-degenerate triangle indices.

Normals and ARKit classification bytes remain preserved in the exact original
source payload even though the current generic `RawVisualMesh` geometry model
only exposes vertices and triangles.

## Capture lineage binding

`CaptureRawVisualMeshBinding` keeps spatial/source lineage outside the generic
raw geometry asset:

- reference-ingestor mesh handoff ID;
- bundle digest;
- exact anchor ID and anchor-record locator;
- source-evidence IDs for the anchor index and geometry payload;
- exact geometry path and SHA-256;
- capture-session ID;
- coordinate-space ID;
- `T_world_from_mesh_anchor`;
- session timestamp;
- declared vertex/face counts;
- the immutable `RawVisualMesh`.

This separation is intentional: two anchors may reference byte-identical
geometry while having different spatial placement authorities.

The binding remains explicitly `solver_ready = false`.

## Transaction boundary

`adapt_capture_mesh_handoff()` assumes its handoff came from the already
validated Capture ingestion-plan v1 boundary. It still independently checks the
exact geometry bytes against the handoff SHA-256 and validates decoded counts.

A later transaction slice must:

1. validate the complete Capture Bundle;
2. build/validate the ingestion plan;
3. stage every source-evidence record and source-specific binding;
4. commit all staged authorities atomically;
5. only then allow semantic conversion / repair / SceneRevision binding.

No partial transaction semantics are claimed here.

## Verification

Focused tests cover:

- exact HTDTMSH1 source-byte preservation;
- anchor-local geometry decode;
- importer-version separation from OBJ/GLB;
- coordinate/transform/source lineage retention;
- serialize/reopen determinism;
- hash mismatch rejection before import;
- count mismatch rejection.

No RDC or Windows host access is required for this slice.
