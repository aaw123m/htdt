# Round 14 — Equipment / Spec-Library Data Truth

Scope: every shipped device/spec catalog, the loader, the UI pickers that list
them, the `equipment_catalog` export, and the add/edit/delete lifecycle for
user entries — verified against real catalog contents and real repository
flows (`backend/tests/test_round14_equip.py`, plus the targeted modules below).

## Enumeration

| Catalog / path | Store | Loader / seed | UI picker(s) | Scope semantics |
|---|---|---|---|---|
| `BUILTIN_SPEAKER_LIBRARY` (bookshelf + ported sub, 9-band SPL-declared datasets each) | `cad_speaker_definitions` / `cad_speaker_datasets` (`document_id IS NULL` = bundled) | `CadSpeakerLibraryRepository.install_builtin_library()` (idempotent) | Reference hub `機材・スピーカー定義` | `BUILTIN_SPEAKER_IDS` → 同梱 |
| `BUILTIN_MATERIAL_LIBRARY` (gypsum/concrete/mineral-wool-100/curtain, octave α 125–4k) | `cad_material_definitions` / `cad_material_evidence` | `CadMaterialLibraryRepository.install_builtin_library()` | Reference hub `音響材料` | `BUILTIN_MATERIAL_IDS` → 同梱 |
| `PROJECTOR_REFERENCE_PACKS` (JVC NZ500/NZ700, Epson LS12000, Sony XW6100ES/XW8100ES; asserted + honestly-`unknown_fields`) | in-memory, self-hash-verified | import-time | Reference hub (equipment section) | 同梱 |
| `TACTILE_REFERENCE_PACK` (ButtKicker LFE, Dayton BST-300EX, TT25-16) | in-memory | import-time | Reference hub | 同梱 |
| `PROJECTION_SCREEN_EVIDENCE_REGISTRY` (Harmony G3, MicroPerf X2, Center Stage XD; 15 records / 5 sources) | in-memory | import-time | Reference hub (material section) | 同梱 |
| `EquipmentDefinition` user entries (identity, dims, sensitivity, SPL, directivity) | `cad_equipment_definitions` + evidence authorities | `EquipmentLibraryService.create_user_definition` / `create_next_version` | `EquipmentLibraryDialog`, system-expansion + playback-chain combos, `ReferenceLibraryPage` | `user_defined` → ユーザーライブラリ |
| `AmplifierOutputCapability` / `SpeakerElectricalLoadAuthority` | headroom repository | `PlaybackChainService` | `PlaybackChainDialog` tabs + scenario combos | user-defined |
| `AcousticMaterialAuthority` + per-surface assignment | acoustic-material repository | `MaterialDialog` / `SurfaceMaterialPanel` | material combo per boundary | user entries |
| `equipment_catalog` export | canonical JSON snapshot | `export_equipment_catalog_snapshot` (atomic write) | consumed by HTDT-Capture | exact identity refs only |

Pickers bind by exact `semantic_sha256` (`addItem(label, sha)` + `findData`),
so a chosen row is the exact definition version applied — verified fail-closed
at resolution (`get_definition_by_hash` → JP error, never a silent substitute).

## Findings & fixes

| # | Surface | Verified failure | Fix |
|---|---|---|---|
| 1 | Shipped catalogs reachability | `install_builtin_library()` had zero non-test callers — the curated speaker/material catalogs never reached the app's library hub or any picker; projector/tactile/screen reference packs were likewise invisible. "Ships catalogs of devices" was untrue at runtime. | `build_reference_library_index` now seeds the bundled libraries (idempotent) and registers hub providers for the projector, tactile, and screen evidence packs so all shipped catalogs are discoverable as 同梱 entries. `reference_library_sources.py` |
| 2 | Scope labeling | `_speaker_entry`/`_material_entry` labeled every `document_id IS NULL` row "ユーザーライブラリ" — bundled entries were mislabeled as user-authored. | Scope now `BUILTIN` for `BUILTIN_SPEAKER_IDS`/`BUILTIN_MATERIAL_IDS`, `USER_LIBRARY` for other shared rows, `PROJECT_LOCAL` when scoped to a document. |
| 3 | `SensitivityReference.level_db_spl` | Any finite float accepted — 0.087 (×1000 unit error), −5, 400 all stored as authority data. | Bounded `(10, 200]` dB SPL — catches the documented unit-error class and absurd values while permitting every plausible declared spec. `cad_equipment.py` |
| 4 | `SplCapability` level fields | `continuous_db_spl`/`peak_db_spl`/`headroom_reference_level_db_spl` unbounded; `headroom_reference` could exceed the declared max SPL (self-contradicting headroom). | Same `(10, 200]` bound on all level fields + `headroom_reference_level_db_spl ≤ max(continuous, peak)` when levels are declared. |
| 5 | `AmplifierOutputCapability` | `peak_capability < continuous_capability` in the same electrical quantity was accepted — a physically impossible output pair. | Model validator rejects same-quantity peak < continuous; different quantities remain un-ordered (no invented conversion). `cad_amplifier_headroom.py` |
| 6 | `MaterialAcousticEvidence` | Absorption/scattering coefficients and transmission loss were unbounded (α=2.5, TL=−3 stored as evidence); `incidence_angle_deg` was accepted for `incidence='unknown'` despite the model's own message. | `*_absorption_coefficient` + `scattering_coefficient` values bounded to [0,1]; `transmission_loss` ≥ 0; angle now requires `incidence='oblique'` (matching `GeometricAcousticBand`'s rule). `cad_material_library.py` |
| 7 | `equipment_choices()` picker labels | Label dropped `version` — two immutable versions of one definition rendered as identical rows in the system-expansion picker (playback-chain picker already printed `(v…)`). | Label now `"{name} (v{version})"`, matching `source_equipment_choices`. `system_expansion_workflow.py` |
| 8 | `_import_directivity` | Successful import published a new version but neither refreshed the list nor emitted `definitionsChanged` — the new version stayed invisible until the dialog was closed. | Refresh + signal emitted on success. `equipment_library.py` |
| 9 | `create_user_definition` / `create_next_version` | Whitespace-only labels/sources passed `min_length` and were persisted verbatim. | Service strips inputs and rejects blank 機材ラベル/出典 fields with JP messages; manufacturer/model blanks normalize to `None`. |
| 10 | `SurfaceMaterialPanel._new_material` parsers | CSV rows with wrong column counts were silently misparsed: a 2-column band line became `scattering=0.0`; 4+ column rows silently dropped trailing data. | Impedance rows must have exactly 3 columns; band rows 2–3 (scattering still optional); violations surface the field-count error instead of corrupt data. `room_acoustics_panel.py` |

## Verified honest (no change needed)

- **Shipped values sane**: every bundled entry audited — speaker SPL bands ordered with plausible levels (−9…+0.5 dB rel), material α ∈ [0.01, 0.95] over ordered octaves, projector packs match published specs (NZ500 2000 lm/40000:1, LS12000 2700 lm 3LCD, XW 2700/3400 lm SXRD), tactile refs match datasheets (resonant freq inside response band, `Re < Z_nom`), screen records consistent (gain 0.7–1.2, cone/min-throw bounds). No dangling references — registry source_ids are validator-enforced, datasets require persisted parents.
- **Referential integrity**: `save_dataset`/`save_evidence` require the persisted definition + redistribution licensing for shared rows; assignments and scenario refs re-resolve by exact semantic hash at read/eval time and fail closed with JP messages.
- **User-entry lifecycle**: add → evidence-gated persist → restart persistence verified; "edit" is an immutable new version sharing `definition_id` (bindings pinned by hash keep pointing at their exact version — renames never orphan references). Same device added twice yields honest distinct identities — no destructive dedupe; name collisions coexist by contract (`resolve_import` COEXIST).
- **Export truth**: `catalog_snapshot` canonical bytes are deterministic; snapshot validator rejects duplicate id/version and duplicate hashes; `model_validate_json` round-trips the emitted file; declared `schema='htdt.equipment.catalog-snapshot'`, `schema_version=1` — the exact-reference format HTDT-Capture consumes.
- **Missing-data honesty**: `capability_preview` renders 未入力 lines for undeclared SPL/directivity instead of presenting gaps as data; projector packs carry explicit `unknown_fields`; `missing_unsupported_fields` is a declared tuple on amp capabilities.
- **Validation depth elsewhere**: `AmplifierLoadDomain` enforces min ≤ ref ≤ max; multi-channel counts require shared-supply evidence; gain requires a reference input; `GeometricAcousticBand` already bounds absorption/scattering to [0,1]; `AcousticMaterialAuthority` rejects double-unsupported capability/data pairs.

## Deferred / noted (not fixed this round)

- **No delete path exists for any catalog entry** (equipment, materials, amps): `LibraryMetaStore` has archive flags and `assert_deletable` gates on reachability, but no UI wires them — entries can only accumulate or be archived. Honest-but-incomplete capability; archive UI is the designed answer (#501), not a quick add.
- **`MaterialDefinition` library has no authoring UI**: the shipped material catalog is readable in the hub, but users cannot add `MaterialDefinition` entries from the app (the live "new material" dialog authors the separate `AcousticMaterialAuthority` model). No silent corruption — the authority store simply can't receive user rows.
- **Picker search**: `ReferenceLibraryIndex.search` exists but no UI field binds it; family tables are flat lists. Not broken — absence, not misbehavior.
- **Screen registry emits one hub entry per `screen_model`** with a hash derived from that model's record digests — presentation-level identity only; the registry itself remains the authority.

## Tests

`backend/tests/test_round14_equip.py` — builtin seed/scope + idempotency,
bundled-pack discoverability, material-evidence bounds (α/scatter [0,1],
TL ≥ 0, oblique-only angle), sensitivity/SPL bounds + headroom consistency,
amplifier peak-vs-continuous, blank-label/source rejection,
version-disambiguated picker labels. `test_review_round8_reference_library.py`
updated: scope column verified per-row (同梱 vs ユーザーライブラリ) and archive
hides only the archived row.
