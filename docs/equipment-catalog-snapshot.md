# Equipment Catalog Snapshot

The backend can export a deterministic, portable picker surface for
HTDT-Capture without copying the full EquipmentDefinition authority into the
capture app.

Command:

```text
python scripts/export_equipment_catalog_snapshot.py <cad.sqlite3> <catalog.json>
```

The output schema is `htdt.equipment.catalog-snapshot` v1. Each entry carries
only:

- exact `definition_id`;
- exact `version`;
- exact `semantic_sha256`;
- identity kind and human-readable manufacturer/model or user label.

The snapshot is a selection/index surface, **not** a substitute authority.
HTDT-Capture must bind the selected exact ID/version/hash tuple. The backend
remains authoritative for resolving that tuple back to the immutable
EquipmentDefinition.
