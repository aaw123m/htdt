# REV44-STAGED — finding-9 dead-writer lane audit

Scope: `docs/reviews/rev43-producers.md` finding 9 — the ~25 persistence
families whose `save_*`/`record_*` writers had no production caller.
Every family below got a lane decision by evidence:

- **wired** — a real producer or an honest minimal record-entry surface
  now calls the writer, and a real consumer reads the rows.
- **file-entry** — wired via a small operator-entry surface (the
  calibration-file pattern: pick file → digest → save).
- **staged** — no runtime producer and no honest minimal record-entry;
  the authority needs a feature spec or hardware/external dependency.
  Only `native_authority_audit`/`cad_authority_registry` read these
  families, which honestly report their absence.

Audit corrections vs. rev43 (verified on this branch, not trusted):
- `cad_data_source_repository.save_decision` was listed as live — it
  has **no** production caller today; the whole family is dead.
- `cad_gain_structure_repository.save_scenario`/`save_evaluation` were
  listed as live — zero callers; whole family dead.
- `cad_layout_profile_repository.save_profile` was listed as live —
  zero callers (the live `save_profile` hits belong to standards /
  acoustic-environment repositories); whole family dead.
- `cad_measurement_pose.save_observation` was listed as live via
  producer — the producer writes `CadMeasurementQualityRepository`,
  not this repository; both pose writers are dead.
- `cad_multifidelity.save_plan` was listed as live — the repo has no
  external importer at all; all four writers dead.
- `cad_amplifier_headroom_repository.save_amplifier_capability` /
  `save_speaker_load` are **live** via `playback_chain_widgets.py`
  (declared capability/load surfaces) — family is partially live.
- `system_expansion_workflow.record_as_built` is **live** via
  `system_expansion_widgets.py` — only `save_cost_record` and the
  single-speaker proposal path are dead.
- `cad_intervention_study_repository.save_alternative` is **live** via
  `intervention_planner.py` — finding 9's "record_alternative" name
  does not exist; the applied-alternative writer works today.
- `cad_operating_preset_repository.save_preset` was itself dead but
  not listed — included below and wired.
- `measurement store.save_comparison` is live via `main.py` /
  `measurement_workflow.py` — not a dead writer.

## Wired this revision

| writer | lane | what produces it now | consumer that reads it | reason |
| --- | --- | --- | --- | --- |
| `CadOperatingPresetRepository.save_preset` | wired | `OperatingPresetRecordDialog` プリセット登録 tab (measurement page プリセットを記録…) — operator-declared preset seeded from `_head` pin | `HealthCheckDialog` preset combos (`measurement_record_surfaces.py:1132/1149`), `CadProjectActivityService.events` `operating_preset_created` | Operator declaration is genuine record-entry evidence; persisted contexts existed but there was no way to name/keep one |
| `CadOperatingPresetRepository.save_applied_state` | wired | same dialog 適用の記録 tab — confirmed device context + deviations + note via `record_applied_preset_state` | `CadProjectActivityService.events` `operating_preset_applied` (`cad_project_activity.py:784`), applied-state reads in health/evidence flows | The `operating_preset_applied` activity event consumed `list_applied_states` — permanently empty before this |
| `CadAcousticTreatmentRepository.save_source_asset` | file-entry | `TreatmentDefinitionDialog` — external source kinds (メーカー資料/測定データ/文献) require a picked 出典ファイル; bytes retained via `save_source_asset`, digest lands in `source_sha256` | `_check_managed_source_asset` (evidence save requires retained bytes when provenance claims a digest), managed-asset backup/restore | Calibration-file pattern verbatim: pick file → digest → save. Without it, provenance could claim a hash with no retained bytes |
| `CadEquipmentBindingRepository.save_binding` | wired | `InstallationRecordDialog` 機材バインド tab on the interventions panel — entity + equipment + body-geometry/acoustic-reference declaration + `EquipmentDataProvenance` | `cad_evidence_register.get_binding_for_entity`, `cad_prediction_capability.list_bindings` (degraded bindings), overview install card | Operator-declared binding evidence, same shape as existing equipment provenance |
| `CadInstallationContextRepository.save_context` | wired | `InstallationRecordDialog` 設置コンテキスト tab — mounting mode / baffle / directivity / host / measured clearances + provenance | `overview_readiness.get_context_for_entity` (R110/install gating), prediction capability reads | Operator-declared installation evidence |
| `CadInstallationDatumRepository.save_datum` | wired | `InstallationRecordDialog` 設置基準 tab — anchor pick (vertex/wall), distinct X/Y direction walls, frame labels, evidence refs | `installation_output_authority` datum resolution (`:785`, `:796`, evidence refs `:1382`), dialog status tree | Operator-declared datum; fail-closed on same-wall X/Y |

## Staged — document only (no honest producer today)

| writer family | lane | what produces it now | consumer that reads it | reason |
| --- | --- | --- | --- | --- |
| `cad_operating_preset_repository.save_measurement_binding` | staged | nothing | audit registry only | Zero readers beyond the audit graph; binding a preset to a measurement needs a product decision on what the binding authorizes |
| `cad_colorimetry_repository` (save_target_profile, save_measurement_set) | staged | nothing | audit registry only | Display-calibration epic — needs measurement hardware + spec |
| `cad_photometric_repository` (3 saves) | staged | nothing | audit registry only | Projector/screen optical epic |
| `cad_lighting_repository.save_scene` | staged | nothing | audit registry only | Lighting epic |
| `cad_site` (save_space, save_relationship) | staged | nothing | audit registry only | Multi-space epic |
| `cad_signal_path_repository.save_path` | staged | nothing (model exists: `AVSignalPath` node/port/edge graph) | `installation_output_authority` edge discovery reads paths when present | Consumer exists, but honest authoring needs a port-aware graph UI — staged epic, not a small record-entry |
| `cad_validation_corpus_repository` (save_entry, save_benchmark_spec) | staged | nothing | audit registry only | Validation corpus epic |
| `cad_visual_qa.save_verdict` | staged | nothing | audit registry only | Visual QA epic — verdicts need a capture/inspect path |
| `cad_tactile_repository` (save_actuator_definition, save_profile) | staged | nothing | audit registry only | Tactile epic — hardware-dependent |
| `cad_data_source_repository` (all 6 saves incl. save_decision) | staged | nothing | audit registry only | Import/registry epic — no producer or reader exists |
| `cad_gain_structure_repository` (save_stage, save_scenario, save_evaluation) | staged | nothing | audit registry only | Gain-staging epic; audit's "scenario/evaluation live" was wrong |
| `cad_layout_profile_repository` (save_profile, save_topology) | staged | `LayoutProfile` used as value object by expansion workflow, never persisted | audit registry only | Needs a declared-channel-vocabulary authoring surface |
| `cad_measurement_pose` (save_observation, save_delta) | staged | listener-pose selections live in `CadListenerPoseRepository` (different authority) | audit registry only | Measurement-pose capture is a producer problem, not record-entry |
| `cad_multifidelity` (save_plan, save_stage_result, save_screening, save_finalization) | staged | nothing | audit registry only | Execution epic — needs the multifidelity runner |
| `cad_equipment_instance_repository` (save_instance, save_binding, save_observation, save_replacement) | staged | nothing writes; `get_instance`/`list_instances` read-only consumers exist (installation cost, layout profile, workflow_application) | cost listing, handoff checks | Instance lifecycle (install/replace events) needs a declared epic; instance binding differs from definition binding (wired above) |
| `cad_field_evidence_repository` (store_asset, save_evidence) | staged | nothing — typed dispatch lacks the `field_evidence` kind | audit registry only | Field-session epic |
| `cad_commissioning_repository` (save_tolerance_profile, save_plan, save_run) | staged | nothing — `deliverables_catalog` only counts plans | deliverables catalog (counts), audit registry | Commissioning epic — tolerance profiles need a declared spec |
| `cad_amplifier_headroom_repository` (save_speaker_impedance, save_amplifier_limit, save_frequency_resolved_evaluation) | staged | capability + load writers are live via playback-chain widgets | audit registry only | Impedance/limit/freq-resolved declarations need per-driver evidence entry — bigger than a file pick |
| `cad_evidence_reconciliation_repository` (save_subject, save_observation, save_decision) | staged | nothing | audit registry only | Reconciliation epic |
| `cad_library_upgrade_repository` (save_upgrade, save_adoption) | staged | nothing | audit registry only | Library-upgrade epic |
| `cad_analysis_study_repository.save_study` | staged | nothing | audit registry only | Analysis-study epic |
| `system_expansion_workflow.save_cost_record` + single-speaker proposal | staged | `record_as_built` is live; cost records and `create_single_speaker_proposal` have no callers | audit registry only | Cost-record entry is plausible later; needs product decision on cost model |
| `cad_acoustic_treatment` comparison authority | staged | `save_comparison` on the measurement store is live — the treatment-side comparison surface has no producer | audit registry only | Treatment comparison epic |

## Already wired by REV44-HEALTHSYNC (earlier revision, this campaign)

- `cad_health_repository.save_plan`/`save_run` via `measurement_record_surfaces` health surfaces
- AV-sync health evidence rows

## Fail-closed + honesty notes

- Every wired surface pins `scene_revision_id`/`scene_content_hash` from
  `scene_repository.current_head(document_id)` at open — writes against
  a stale head fail closed with a JA status message.
- Provenance required on binding/context records: blank 出典名 refuses
  the write (`status_label`), never fabricates.
- Datum writer refuses identical X/Y direction walls (validator parity).
- Treatment external-source kinds refuse accept without a retained file.
- No fabricated capability: dialogs write only rows the repositories
  already model; staged families got documentation, not fake UI.

## Tests

`backend/tests/test_rev44_staged.py` — happy path + fail-closed for
each wired family, plus the treatment external-source contract and the
retained-asset byte check.
