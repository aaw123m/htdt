# O100G — exact measured lifecycle over as-built authority

Date: 2026-09-20

## Scope

This slice establishes the backend transition from explicit O100G as-built authority to explicit measurement evidence.

It reuses the existing N60 / Issue #172 measurement and quality authorities.

The chain is:

```text
immutable SystemVariant
→ SystemVariantApplication
→ SystemVariantAsBuiltRecord
→ exact CadMeasurementRecord / CadFrequencyResponseDataset
→ exact CadMeasurementQualityReport
→ SystemVariantMeasuredRecord
```

The original proposal and as-built record remain immutable.

## Configuration-level measured evidence vs entity-level measured state

A `SystemVariantMeasuredRecord` means the exact as-built configuration has one or more exact measurement evidence bindings.

It does **not** mean every proposed entity was independently measured.

Entity lifecycle is derived conservatively:

- proposed speaker appears explicitly in a bound measurement `source_speaker_ids`
  → that proposed entity receives `state='measured'` with the exact measurement IDs;
- proposed entity is not an explicit source in any bound measurement
  → it remains `state='as_built'`.

Therefore a measurement of baseline FL on a 5.x proposal can provide configuration-level measurement evidence without falsely promoting proposed SL/SR to measured.

Non-speaker proposed entities remain as-built unless a future domain-specific measurement authority can explicitly support a measured lifecycle claim.

## Exact as-built revision

Every bound measurement must reference the exact:

- document id;
- as-built SceneRevision id;
- as-built SceneRevision content hash.

A measurement from a later descendant revision is not silently attached to an earlier as-built record, even when the scene content happens to be identical.

A new as-built authority or explicit lifecycle record is required when the physical configuration revision changes.

## Measurement authority reuse

Each measurement evidence binding records exact:

- measurement id and canonical measurement SHA-256;
- dataset id and canonical dataset SHA-256;
- raw asset SHA-256;
- MeasurementQualityReport id/hash;
- measurement entity id;
- channel role;
- source speaker ids;
- radiation scope.

The implementation does not duplicate measurement arrays or quality algorithms.

The existing repositories remain authoritative for:

- raw asset content addressing;
- measurement-to-SceneRevision binding;
- dataset samples;
- acquisition context;
- clipping/SNR/band/timing/polarity/IR/calibration/repeatability checks;
- capability matrix;
- retake lineage.

## Quality semantics

The existence of a measured lifecycle record does not imply that every quality capability is ALLOWED.

For example:

- a valid FR-only measurement may allow magnitude response;
- common timing, arrival time, decay, calibrated response or repeatability may remain UNKNOWN/BLOCKED;
- retake recommendation may remain UNKNOWN.

The exact quality report is preserved so downstream claims can gate on the actual capability they require.

## Measured evidence type

Only `CadMeasurementRecord.evidence_type='measured'` can be bound.

Derived, predicted or unknown records cannot promote measured lifecycle evidence.

## Persistence / reopen

`CadSystemVariantMeasuredLifecycleRepository` stores append-only measured records.

Save/reopen re-resolves:

- exact SystemVariantAsBuiltRecord;
- exact as-built SceneRevision;
- every measurement;
- every dataset;
- every MeasurementQualityReport;
- all measurement/dataset/raw/report hashes;
- exact source-speaker-driven entity lifecycle.

A retake does not rewrite an earlier measured record.

If a later selected measurement should represent the system, create a new measured record bound to that exact evidence. Existing `CadMeasurementLineageRecord` remains the authority for retake/selection relationships.

## Focused verification

`backend/tests/test_cad_system_variant_measured_lifecycle.py` verifies:

1. exact proposal → apply → as-built → measurement chain;
2. only explicitly measured proposed source speaker is promoted to measured;
3. unmeasured proposed speaker remains as-built;
4. quality UNKNOWN is not converted into validation success;
5. save/reopen re-resolves exact measurement and quality authorities;
6. measurement from a different SceneRevision is rejected;
7. derived evidence cannot become measured lifecycle evidence;
8. baseline-only measurement can bind configuration evidence without promoting proposed entities.

## Deferred

- measurement-plan generation specialized for an as-built SystemVariant;
- retake selection UI;
- measured badges / 3D ghost styling;
- validated prediction-vs-measured acceptance for the selected variant;
- Windows visual acceptance (#118).

RDC usage: 0.
