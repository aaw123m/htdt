# Issue #142 / O100G SystemVariant MeasurementPlan / Campaign — 2026-09-20

## Scope

This slice adds preregistered SystemVariant-specific measurement planning/campaign authority after explicit application and AsBuilt completion. It reuses existing N60 Measurement/Dataset, MeasurementQualityReport/AcquisitionContext, and SystemVariantMeasuredRecord authorities. It does not replace O60 model validation.

## Exact chain

```text
SystemVariant
→ SystemVariantApplication / applied SceneRevision
→ SystemVariantAsBuiltRecord / actual SceneRevision
→ SystemVariantMeasurementPlan
→ SystemVariantMeasurementCampaign
→ exact CadMeasurementRecord + Dataset
→ exact MeasurementQualityReport + AcquisitionContext
→ plan/campaign completion
→ existing SystemVariantMeasuredRecord
```

Plan creation freezes the exact variant/hash, application/hash, applied revision/hash, AsBuilt record/hash, actual AsBuilt revision/hash, measurement-point entity/position, source entities, channel role, requested observable/capability, acquisition requirement, expected count, repeatability requirement and optional required band.

Campaign creation freezes an exact plan set and preregistration time. Target changes require a new plan/campaign.

## Evidence matching

Completion fails closed unless evidence matches the exact AsBuilt revision, measurement point/position, source entity set, channel role, explicit capture timestamp after campaign preregistration, Dataset identity, current exact quality report, AcquisitionContext and requested capability/band. A generic imported measurement is never automatically promoted into a SystemVariant campaign.

Campaign completion delegates lifecycle materialization to the existing `build_system_variant_measured_record`; measured state therefore remains distinct from model validation, recommendation eligibility and O60 owned-room PASS.

## Persistence

Plans, campaigns, plan completions and campaign completions are append-only native SQLite authorities. Reopen validation re-resolves SystemVariant/Application/AsBuilt/Measurement/Dataset/Quality/Measured authorities and fails closed on missing or stale identities.

## Completion status

O100G backend MeasurementPlan/Campaign authority is implemented in this slice. O100G remains partial overall: Room/Optimize UX, proposed ghost/badge, measured-comparison UX and UX160 owned-Windows visual acceptance remain separate work.

RDC was not used.
