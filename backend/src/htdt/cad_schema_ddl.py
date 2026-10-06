"""Canonical persistent schema for the HTDT native database (#302).

This module is the data half of the native schema migration authority:
every persistent table, index and column the native database may contain is
declared here exactly once, in the shape the current application requires.
``cad_schema.ensure_native_schema`` applies :data:`NATIVE_BASELINE_DDL` and
:data:`NATIVE_COLUMN_ENSURES` as the v6 migration, so fresh database creation
and upgrades are reproducible from versioned migrations alone.

Production repositories must verify an already-migrated schema
(``cad_schema.require_native_tables``) instead of evolving it themselves.
"""

from __future__ import annotations

# Ordered CREATE TABLE / CREATE INDEX statements forming the complete native
# schema contract. Statements are idempotent (IF NOT EXISTS); table statements
# precede index statements and foreign-key parents precede dependents.
NATIVE_BASELINE_DDL: tuple[str, ...] = (

    """
    CREATE TABLE IF NOT EXISTS authoring_constraint_sets ( document_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_geometry_derivations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, derivation_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, scene_revision_id TEXT NOT NULL, semantic_geometry_id TEXT NOT NULL, r120_compiled_geometry_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(r120_compiled_geometry_id) REFERENCES cad_r120_compiled_geometry(compiled_geometry_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_solver_capability_manifests ( seq INTEGER PRIMARY KEY AUTOINCREMENT, manifest_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, adapter_descriptor_id TEXT NOT NULL, adapter_id TEXT NOT NULL, acoustic_domain TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(adapter_descriptor_id) REFERENCES cad_acoustic_solver_adapters(descriptor_id) )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_geometry_derivation_compiled ON cad_acoustic_geometry_derivations( r120_compiled_geometry_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_geometry_derivation_scene ON cad_acoustic_geometry_derivations( scene_revision_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_solver_capability_manifest_descriptor ON cad_solver_capability_manifests( adapter_descriptor_id, seq ASC )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_level_calibrations ( calibration_id TEXT PRIMARY KEY, calibration_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_solver_adapters ( seq INTEGER PRIMARY KEY AUTOINCREMENT, descriptor_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, adapter_id TEXT NOT NULL, adapter_version TEXT NOT NULL, model_solver_role_id TEXT NOT NULL, acoustic_domain TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_solver_results ( seq INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, execution_id TEXT NOT NULL, dispatch_binding_id TEXT NOT NULL, prediction_request_id TEXT NOT NULL, acoustic_scene_snapshot_id TEXT NOT NULL, deterministic_solver_input_hash TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_treatment_definitions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, definition_id TEXT NOT NULL, definition_version TEXT NOT NULL, definition_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, UNIQUE(definition_id, definition_version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_wave_excitations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, excitation_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, equipment_definition_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acquisition_contexts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, acquisition_context_id TEXT NOT NULL UNIQUE, acquisition_context_sha256 TEXT NOT NULL, source_kind TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ambient_comparisons ( comparison_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, comparison_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ambient_conditions ( condition_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, condition_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ambient_criteria ( criterion_id TEXT PRIMARY KEY, criterion_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ambient_evaluations ( evaluation_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, criterion_id TEXT NOT NULL, verdict TEXT NOT NULL, evaluation_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ambient_profiles ( profile_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, condition_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_amplifier_electrical_limits ( seq INTEGER PRIMARY KEY AUTOINCREMENT, limit_id TEXT NOT NULL, version TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, amplifier_capability_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(limit_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_amplifier_output_capabilities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT NOT NULL, version TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, output_id TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(capability_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_applicability_attestations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, attestation_id TEXT NOT NULL UNIQUE, attestation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, search_spec_id TEXT NOT NULL, code TEXT NOT NULL, payload_json TEXT NOT NULL, attested_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_auralization_artifacts ( artifact_id TEXT PRIMARY KEY, artifact_semantic_sha256 TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, output_asset_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_auralization_render_specs ( spec_id TEXT PRIMARY KEY, spec_semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_auralization_capabilities ( capability_id TEXT PRIMARY KEY, capability_semantic_sha256 TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, document_id TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_auralization_listening_validations ( validation_id TEXT PRIMARY KEY, validation_semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_auralization_review_packages ( package_id TEXT PRIMARY KEY, package_semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, package_asset_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_auralization_routing_declarations ( routing_id TEXT PRIMARY KEY, routing_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_av_latency_measurements ( measurement_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, condition_id TEXT NOT NULL, status TEXT NOT NULL, measurement_sha256 TEXT NOT NULL, captured_at_utc TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_av_sync_conditions ( condition_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, condition_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_constraint_workspaces ( document_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, updated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_coverage_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL UNIQUE, scenario_sha256 TEXT NOT NULL UNIQUE, equipment_definition_sha256 TEXT NOT NULL, directivity_dataset_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_current_topologies ( seq INTEGER PRIMARY KEY AUTOINCREMENT, topology_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, layout_profile_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_data_source_registry ( source_id TEXT PRIMARY KEY, domain TEXT NOT NULL, source_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dataset_reviews ( review_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, review_sha256 TEXT NOT NULL UNIQUE, reviewed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_deterministic_ga_execution_inputs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, execution_input_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, snapshot_id TEXT NOT NULL, prediction_request_id TEXT NOT NULL, dispatch_binding_id TEXT NOT NULL, r120_compiled_geometry_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_deterministic_path_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, execution_id TEXT NOT NULL, execution_provenance_authority_id TEXT NOT NULL, execution_input_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, prediction_request_id TEXT NOT NULL, dispatch_binding_id TEXT NOT NULL, r120_compiled_geometry_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_stochastic_receiver_estimate_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, execution_id TEXT NOT NULL, execution_provenance_authority_id TEXT NOT NULL, execution_input_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, prediction_request_id TEXT NOT NULL, dispatch_binding_id TEXT NOT NULL, r120_compiled_geometry_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_direct_level_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL UNIQUE, scenario_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_direct_view_evaluations ( evaluation_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, system_variant_id TEXT, request_sha256 TEXT NOT NULL, geometry_status TEXT NOT NULL, evaluation_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_direct_view_specifications ( specification_id TEXT NOT NULL, version TEXT NOT NULL, specification_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (specification_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_directivity_datasets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, dataset_id TEXT NOT NULL, version TEXT NOT NULL, equipment_definition_sha256 TEXT NOT NULL, source_asset_sha256 TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(dataset_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_equipment_binding_semantics ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, entity_id TEXT NOT NULL, equipment_definition_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_equipment_definitions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, definition_id TEXT NOT NULL, version TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(definition_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_equipment_evidence_authorities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, equipment_definition_sha256 TEXT NOT NULL, provenance_sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL, authority_kind TEXT NOT NULL, subject_sha256 TEXT NOT NULL, field_groups_json TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(equipment_definition_sha256, provenance_sha256) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_equipment_upgrades ( seq INTEGER PRIMARY KEY AUTOINCREMENT, upgrade_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, definition_id TEXT NOT NULL, from_sha256 TEXT NOT NULL, to_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_field_explorer_sessions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, prediction_run_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_extended_model_capabilities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT NOT NULL UNIQUE, model_id TEXT NOT NULL, model_version TEXT NOT NULL, evidence_scope TEXT NOT NULL, capability_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_extended_parameter_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, parameter TEXT NOT NULL, model_id TEXT NOT NULL, model_version TEXT NOT NULL, evidence_scope TEXT NOT NULL, evidence_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_frequency_resolved_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, impedance_sha256 TEXT NOT NULL, amplifier_capability_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_gain_structure_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, scenario_id TEXT NOT NULL, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_gain_structure_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL UNIQUE, scenario_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hybrid_acoustic_results ( seq INTEGER PRIMARY KEY AUTOINCREMENT, hybrid_result_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, acoustic_scene_snapshot_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, stitching_policy_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hybrid_prediction_provider_objectives ( seq INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, provider_id TEXT NOT NULL, objective_input_id TEXT NOT NULL, evaluation_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hybrid_prediction_providers ( seq INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, base_provider_id TEXT NOT NULL, r160_artifact_id TEXT NOT NULL, r160_composition_spec_id TEXT NOT NULL, source_entity_id TEXT NOT NULL, receiver_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hybrid_prediction_provider_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, provider_id TEXT NOT NULL, consumer_kind TEXT NOT NULL, consumer_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hybrid_stitching_policies ( seq INTEGER PRIMARY KEY AUTOINCREMENT, policy_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, mode TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_importer_declarations ( importer_id TEXT PRIMARY KEY, domain TEXT NOT NULL, importer_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_installation_contexts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, context_id TEXT NOT NULL, version_key TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, entity_id TEXT NOT NULL, equipment_definition_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(context_id, version_key) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_installed_definition_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, instance_id TEXT NOT NULL, equipment_definition_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_installed_device_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, instance_id TEXT NOT NULL, observation_kind TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_installed_equipment_instances ( seq INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, equipment_class TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_installed_equipment_replacements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, replacement_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, removed_instance_id TEXT NOT NULL, installed_instance_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ir_analysis_results ( result_id TEXT PRIMARY KEY, spec_id TEXT NOT NULL, analysis_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ir_analysis_specs ( spec_id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL, spec_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_joint_optimization_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, spec_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, base_system_variant_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_late_decay_estimate_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, execution_id TEXT NOT NULL, execution_provenance_authority_id TEXT NOT NULL, execution_input_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, prediction_request_id TEXT NOT NULL, dispatch_binding_id TEXT NOT NULL, r120_compiled_geometry_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_late_field_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, execution_id TEXT NOT NULL, execution_provenance_authority_id TEXT NOT NULL, execution_input_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, prediction_request_id TEXT NOT NULL, dispatch_binding_id TEXT NOT NULL, r120_compiled_geometry_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_layout_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, version TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, name TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_line_level_stages ( seq INTEGER PRIMARY KEY AUTOINCREMENT, stage_id TEXT NOT NULL, version TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, UNIQUE(stage_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_materialized_pattern_points ( point_id TEXT PRIMARY KEY, pattern_id TEXT NOT NULL, document_id TEXT NOT NULL, measurement_point_entity_id TEXT NOT NULL, point_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_assets ( sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_runner_plans ( plan_id TEXT PRIMARY KEY, scene_revision_id TEXT NOT NULL, plan_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_target_lineages ( target_lineage_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, measurement_point_id TEXT NOT NULL, target_lineage_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_target_patterns ( pattern_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, pattern_version INTEGER NOT NULL, pattern_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multi_seat_results ( result_id TEXT PRIMARY KEY, set_id TEXT NOT NULL, analysis_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multi_seat_sets ( set_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, set_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multi_sub_candidates ( seq INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL UNIQUE, candidate_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, strategy TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multi_sub_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL, document_id TEXT NOT NULL, population TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(candidate_id) REFERENCES cad_multi_sub_candidates(candidate_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multi_sub_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, baseline_candidate_id TEXT NOT NULL, candidate_id TEXT NOT NULL, claim TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(baseline_candidate_id) REFERENCES cad_multi_sub_candidates(candidate_id), FOREIGN KEY(candidate_id) REFERENCES cad_multi_sub_candidates(candidate_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multi_sub_stage_comparisons ( seq INTEGER PRIMARY KEY AUTOINCREMENT, comparison_id TEXT NOT NULL UNIQUE, comparison_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multi_sub_deployments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT NOT NULL UNIQUE, verification_sha256 TEXT NOT NULL UNIQUE, qualification_id TEXT NOT NULL, candidate_id TEXT NOT NULL, document_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(candidate_id) REFERENCES cad_multi_sub_candidates(candidate_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multifidelity_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, domain TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_o90_robust_pareto_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, search_spec_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_decision_rule_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, decision_type TEXT NOT NULL, criterion_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_decision_verdicts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verdict_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, rule_id TEXT NOT NULL, rule_sha256 TEXT NOT NULL, decision_type TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_uncertain_input_sets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, input_set_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, model_ref TEXT, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_robust_design_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, input_set_id TEXT NOT NULL, input_set_sha256 TEXT NOT NULL, propagation_spec_id TEXT NOT NULL, propagation_spec_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_stimulus_assets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, stimulus_id TEXT NOT NULL UNIQUE, stimulus_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, origin_class TEXT NOT NULL, subtype TEXT NOT NULL, content_sha256 TEXT, registered_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_stimulus_pins ( seq INTEGER PRIMARY KEY AUTOINCREMENT, pin_id TEXT NOT NULL UNIQUE, pin_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_ref TEXT NOT NULL, stimulus_id TEXT NOT NULL, stimulus_sha256 TEXT NOT NULL, pinned_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_stimulus_eligibility ( seq INTEGER PRIMARY KEY AUTOINCREMENT, eligibility_id TEXT NOT NULL UNIQUE, eligibility_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, procedure_id TEXT NOT NULL, stimulus_id TEXT NOT NULL, stimulus_sha256 TEXT NOT NULL, verdict TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_bass_splice_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, role_id TEXT NOT NULL, sub_group_id TEXT NOT NULL, seat_id TEXT NOT NULL, seat_role TEXT NOT NULL, path TEXT NOT NULL, observed_state TEXT NOT NULL, stimulus_pin_id TEXT, measurement_dataset_sha256 TEXT, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_bass_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, lifecycle_at_evaluation TEXT NOT NULL, status TEXT NOT NULL, scope TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spatial_campaign_designs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, design_id TEXT NOT NULL UNIQUE, design_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT, scene_content_hash TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spatial_campaign_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, design_id TEXT NOT NULL, design_sha256 TEXT NOT NULL, document_id TEXT NOT NULL, state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spatial_campaign_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, binding_sha256 TEXT NOT NULL UNIQUE, design_id TEXT NOT NULL, design_sha256 TEXT NOT NULL, document_id TEXT NOT NULL, point_id TEXT NOT NULL, measurement_id TEXT NOT NULL, measurement_sha256 TEXT NOT NULL, deviation_m REAL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp32_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, publisher TEXT NOT NULL, revision TEXT NOT NULL, source_access_kind TEXT NOT NULL, clause_mapping_state TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp32_reconciliations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, reconciliation_id TEXT NOT NULL UNIQUE, reconciliation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp32_readiness ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, plan_sha256 TEXT NOT NULL, document_id TEXT NOT NULL, state TEXT NOT NULL, assessed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp32_verification_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, spatial_design_id TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp32_verification_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, plan_sha256 TEXT NOT NULL, document_id TEXT NOT NULL, readiness_assessment_id TEXT NOT NULL, overall_state TEXT NOT NULL, rp22_state TEXT NOT NULL, completed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp32_reports ( seq INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL UNIQUE, report_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, plan_id TEXT NOT NULL, record_id TEXT NOT NULL, overall_state TEXT NOT NULL, generated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_provider_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, provider_id TEXT NOT NULL, consumer_kind TEXT NOT NULL, consumer_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_provider_objectives ( seq INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, provider_id TEXT NOT NULL, provider_binding_id TEXT NOT NULL, evaluation_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_providers ( seq INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, result_envelope_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_projector_spec_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_sha256 TEXT NOT NULL UNIQUE, evidence_kind TEXT NOT NULL, source_sha256 TEXT, manufacturer TEXT, model TEXT, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_projector_specifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, specification_id TEXT NOT NULL, version TEXT NOT NULL, specification_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(specification_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_proposal_objective_result_authorities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, authority_kind TEXT NOT NULL, authority_id TEXT NOT NULL, objective_id TEXT NOT NULL, sample_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(authority_kind, authority_id, objective_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_proposal_robust_pareto_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, topology_comparison_evaluation_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_proposal_robustness_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, robustness_spec_id TEXT NOT NULL UNIQUE, robustness_spec_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, candidate_variant_id TEXT NOT NULL, topology_candidate_id TEXT NOT NULL, nominal_bundle_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_quality_calibration_files ( sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r140_execution_schedules ( seq INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r140_execution_tasks ( seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, execution_input_sha256 TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, stage_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r140_gpu_authorities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, authority_kind TEXT NOT NULL, authority_id TEXT NOT NULL, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(authority_kind, authority_id), UNIQUE(authority_kind, semantic_sha256) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r140_resource_estimates ( seq INTEGER PRIMARY KEY AUTOINCREMENT, estimate_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_raw_source_records ( record_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, record_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_robustness_specs ( robustness_spec_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, search_spec_id TEXT NOT NULL, candidate_id TEXT NOT NULL, nominal_objective_evaluation_id TEXT NOT NULL, model_id TEXT NOT NULL, model_version TEXT NOT NULL, payload_json TEXT NOT NULL, robustness_spec_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_routing_profiles ( routing_profile_id TEXT PRIMARY KEY, routing_profile_sha256 TEXT NOT NULL UNIQUE, profile_name TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_review_decisions ( decision_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, decision_sha256 TEXT NOT NULL UNIQUE, reviewed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_speaker_electrical_loads ( seq INTEGER PRIMARY KEY AUTOINCREMENT, load_id TEXT NOT NULL, version TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, equipment_definition_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(load_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_speaker_impedances ( seq INTEGER PRIMARY KEY AUTOINCREMENT, impedance_id TEXT NOT NULL, version TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, equipment_definition_sha256 TEXT NOT NULL, tier TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(impedance_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_standards_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, profile_semantic_hash TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, UNIQUE(profile_id, profile_version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_standards_source_authorities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, authority_id TEXT NOT NULL UNIQUE, authority_version TEXT NOT NULL, semantic_hash_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variant_as_built ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, application_id TEXT NOT NULL UNIQUE, variant_id TEXT NOT NULL, as_built_revision_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variant_measured ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, as_built_record_id TEXT NOT NULL, variant_id TEXT NOT NULL, as_built_revision_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_campaign_completions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, completion_id TEXT NOT NULL UNIQUE, completion_sha256 TEXT NOT NULL UNIQUE, campaign_id TEXT NOT NULL UNIQUE, measured_record_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_campaign_registrations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, registration_id TEXT NOT NULL UNIQUE, registration_sha256 TEXT NOT NULL UNIQUE, campaign_id TEXT NOT NULL UNIQUE, campaign_sha256 TEXT NOT NULL, registered_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_campaigns ( seq INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL UNIQUE, campaign_sha256 TEXT NOT NULL UNIQUE, variant_id TEXT NOT NULL, as_built_record_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_plan_completions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, completion_id TEXT NOT NULL UNIQUE, completion_sha256 TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, campaign_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, variant_id TEXT NOT NULL, as_built_record_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_timing_references ( timing_reference_id TEXT PRIMARY KEY, timing_reference_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_treatment_evidence_authorities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_version TEXT NOT NULL, source_sha256 TEXT, subject_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_upgrade_adoptions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, adoption_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, upgrade_sha256 TEXT NOT NULL, document_id TEXT NOT NULL, decision TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_upstream_version_candidates ( candidate_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, candidate_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_validation_benchmark_specs ( benchmark_spec_id TEXT PRIMARY KEY, benchmark_spec_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_validation_campaign_registrations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, registration_id TEXT NOT NULL UNIQUE, registration_sha256 TEXT NOT NULL UNIQUE, campaign_id TEXT NOT NULL UNIQUE, campaign_sha256 TEXT NOT NULL, registered_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_validation_corpus_entries ( corpus_entry_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, corpus_entry_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_geometry_workspaces ( document_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, updated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wave_excitation_evidence_authorities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, evidence_kind TEXT NOT NULL, source_sha256 TEXT NOT NULL, subject_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wiring_checks ( check_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, check_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS capture_mission_packages ( package_id TEXT PRIMARY KEY, pairing_id TEXT, descriptor_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, status TEXT NOT NULL, status_detail TEXT NOT NULL, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS capture_receiver_config ( id INTEGER PRIMARY KEY CHECK(id=1), receiver_instance_id TEXT NOT NULL, display_name TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL, enabled INTEGER NOT NULL, pinned_identity TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS capture_receiver_pairings ( pairing_id TEXT PRIMARY KEY, pairing_token TEXT NOT NULL UNIQUE, receiver_instance_id TEXT NOT NULL, project_ref TEXT, endpoint_url TEXT NOT NULL, capability_endpoint_url TEXT, missions_endpoint_url TEXT, pinned_identity TEXT NOT NULL, confirmation_code TEXT NOT NULL, state TEXT NOT NULL, created_at_utc TEXT NOT NULL, confirmed_at_utc TEXT, expires_at_utc TEXT, capture_instance_id TEXT )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS editor_camera_states ( document_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS editor_named_views ( document_id TEXT NOT NULL, view_id TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL, PRIMARY KEY (document_id, view_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS editor_view_states ( document_id TEXT PRIMARY KEY, selected_id TEXT, hidden_ids_json TEXT NOT NULL, locked_ids_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
        selected_ids_json TEXT NOT NULL DEFAULT '[]',
        snap_json TEXT
    )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS field_return_contributions ( contribution_id TEXT PRIMARY KEY, artifact_sha256 TEXT NOT NULL, validation_state TEXT NOT NULL, routing TEXT NOT NULL, matched_project_id TEXT, mission_id TEXT, plan_sha256 TEXT, manifest_json TEXT, detail TEXT, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS capture_authoring_provenances ( provenance_id TEXT PRIMARY KEY, capture_lineage_digest TEXT NOT NULL, bundle_digest TEXT NOT NULL, capture_revision_id TEXT NOT NULL, record_kind TEXT NOT NULL, record_id TEXT NOT NULL, authority_record_handoff_id TEXT NOT NULL, source_payload_sha256 TEXT NOT NULL, coordinate_space_id TEXT, applied_to_document_id TEXT NOT NULL, applied_to_scene_revision_id TEXT NOT NULL, operator_action TEXT NOT NULL, resolved_refs_json TEXT, quantity_type TEXT, quantity_value REAL, quantity_unit TEXT, created_at_utc TEXT NOT NULL, UNIQUE(capture_lineage_digest, record_kind, record_id, applied_to_document_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS floor_plan_underlays ( document_id TEXT NOT NULL, underlay_id TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL, PRIMARY KEY (document_id, underlay_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS r150_path_frequency_response_artifacts ( artifact_id TEXT PRIMARY KEY, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS r160_numerical_hybrid_responses ( artifact_id TEXT PRIMARY KEY, semantic_sha256 TEXT NOT NULL UNIQUE, composition_spec_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS r160_stitched_hybrid_responses ( artifact_id TEXT PRIMARY KEY, semantic_sha256 TEXT NOT NULL UNIQUE, composition_spec_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS r160_late_energy_decay_artifacts ( artifact_id TEXT PRIMARY KEY, semantic_sha256 TEXT NOT NULL UNIQUE, late_field_input_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS scene_revisions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, revision_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, parent_revision_id TEXT, created_at_utc TEXT NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, detached INTEGER NOT NULL DEFAULT 0, detached_reason TEXT, FOREIGN KEY(parent_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS scene_document_heads ( document_id TEXT PRIMARY KEY, head_revision_id TEXT NOT NULL, updated_at_utc TEXT NOT NULL, generation INTEGER NOT NULL, FOREIGN KEY(head_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS scene_revision_labels ( revision_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, label TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', updated_at_utc TEXT NOT NULL, FOREIGN KEY(revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS htdt_project_documents ( project_id TEXT PRIMARY KEY, document_id TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, description TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT, last_opened_at_utc TEXT, archived INTEGER NOT NULL DEFAULT 0, archived_at_utc TEXT, cloned_from_project_id TEXT, source_revision_id TEXT )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS htdt_project_imports ( import_id TEXT PRIMARY KEY, bundle_manifest_sha256 TEXT NOT NULL, source_document_id TEXT NOT NULL, imported_document_id TEXT NOT NULL, import_mode TEXT NOT NULL, imported_at_utc TEXT NOT NULL, imported_rows INTEGER NOT NULL, reused_rows INTEGER NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS htdt_legacy_imports ( legacy_project_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_id TEXT NOT NULL, migrated_at_utc TEXT NOT NULL, detail TEXT )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS htdt_storage_gc_pending ( sha256 TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, queued_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS seating_layout_specs ( document_id TEXT NOT NULL, spec_id TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL, PRIMARY KEY (document_id, spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_treatment_comparisons ( seq INTEGER PRIMARY KEY AUTOINCREMENT, comparison_id TEXT NOT NULL UNIQUE, comparison_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_directivity_source_assets ( source_asset_sha256 TEXT PRIMARY KEY REFERENCES cad_measurement_assets(sha256), filename TEXT, media_type TEXT, source_format TEXT NOT NULL, declared_schema TEXT, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_joint_candidates ( seq INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL UNIQUE, candidate_sha256 TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, physical_system_variant_id TEXT NOT NULL, calibration_plan_id TEXT, candidate_class TEXT NOT NULL, eligibility_state TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(spec_id) REFERENCES cad_joint_optimization_specs(spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, document_id TEXT NOT NULL, search_spec_id TEXT NOT NULL, candidate_id TEXT NOT NULL, applied_scene_revision_id TEXT NOT NULL, status TEXT NOT NULL, plan_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, FOREIGN KEY(applied_scene_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_runner_runs ( run_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES cad_measurement_runner_plans(plan_id), plan_sha256 TEXT NOT NULL, started_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurements ( measurement_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL REFERENCES scene_revisions(revision_id), scene_content_hash TEXT NOT NULL, measurement_entity_id TEXT NOT NULL, measurement_position_json TEXT NOT NULL, measurement_direction_json TEXT, evidence_type TEXT NOT NULL, channel_role TEXT NOT NULL, source_speaker_ids_json TEXT NOT NULL, radiation_scope TEXT NOT NULL, routing_evidence TEXT NOT NULL, captured_at TEXT, imported_at TEXT NOT NULL, source_kind TEXT NOT NULL, external_source_id TEXT, quality_status TEXT NOT NULL, quality_reasons_json TEXT NOT NULL, quality_source TEXT NOT NULL, provenance_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multifidelity_screening_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(plan_id) REFERENCES cad_multifidelity_plans(plan_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multifidelity_stage_results ( seq INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL, stage_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(plan_id) REFERENCES cad_multifidelity_plans(plan_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_perturbation_samples ( sample_id TEXT PRIMARY KEY, robustness_spec_id TEXT NOT NULL, candidate_id TEXT NOT NULL, sample_index INTEGER NOT NULL, feasible INTEGER NOT NULL, payload_json TEXT NOT NULL, sample_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, UNIQUE(robustness_spec_id, sample_index), FOREIGN KEY(robustness_spec_id) REFERENCES cad_robustness_specs(robustness_spec_id) ON DELETE RESTRICT )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_results ( seq INTEGER PRIMARY KEY AUTOINCREMENT, prediction_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, constraint_workspace_hash TEXT, model_id TEXT NOT NULL, model_version TEXT NOT NULL, result_kind TEXT NOT NULL, geometry_compatibility TEXT NOT NULL, parameters_json TEXT NOT NULL, input_snapshot_json TEXT NOT NULL, input_hash TEXT NOT NULL, submitted_at_utc TEXT NOT NULL, completed_at_utc TEXT NOT NULL, status TEXT NOT NULL, assumptions_json TEXT NOT NULL, warnings_json TEXT NOT NULL, modes_json TEXT NOT NULL, reflections_json TEXT NOT NULL, provider_response_json TEXT, result_sha256 TEXT, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_projector_spec_source_assets ( source_asset_sha256 TEXT PRIMARY KEY REFERENCES cad_measurement_assets(sha256), evidence_sha256 TEXT NOT NULL REFERENCES cad_projector_spec_evidence(evidence_sha256), filename TEXT, media_type TEXT, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_proposal_perturbation_samples ( seq INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT NOT NULL UNIQUE, sample_sha256 TEXT NOT NULL UNIQUE, robustness_spec_id TEXT NOT NULL, sample_index INTEGER NOT NULL, feasible INTEGER NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(robustness_spec_id, sample_index), FOREIGN KEY(robustness_spec_id) REFERENCES cad_proposal_robustness_specs(robustness_spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_proposal_robustness_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, robustness_spec_id TEXT NOT NULL, objective_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(robustness_spec_id, objective_id), FOREIGN KEY(robustness_spec_id) REFERENCES cad_proposal_robustness_specs(robustness_spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r120_compiled_geometry ( seq INTEGER PRIMARY KEY AUTOINCREMENT, compiled_geometry_id TEXT NOT NULL UNIQUE, compiled_hash_sha256 TEXT NOT NULL UNIQUE, scene_revision_id TEXT NOT NULL, scene_revision_content_hash TEXT NOT NULL, semantic_geometry_id TEXT NOT NULL, semantic_geometry_hash_sha256 TEXT NOT NULL, request_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r140_execution_attempts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES cad_r140_execution_tasks(task_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r140_execution_cache ( seq INTEGER PRIMARY KEY AUTOINCREMENT, cache_entry_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, execution_input_sha256 TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES cad_r140_execution_tasks(task_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r140_execution_results ( seq INTEGER PRIMARY KEY AUTOINCREMENT, execution_result_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES cad_r140_execution_tasks(task_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_robustness_evaluations ( evaluation_id TEXT PRIMARY KEY, robustness_spec_id TEXT NOT NULL, candidate_id TEXT NOT NULL, objective_id TEXT NOT NULL, payload_json TEXT NOT NULL, evaluation_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, UNIQUE(robustness_spec_id, objective_id), FOREIGN KEY(robustness_spec_id) REFERENCES cad_robustness_specs(robustness_spec_id) ON DELETE RESTRICT )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_robustness_validation_cases ( seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL UNIQUE, robustness_spec_id TEXT NOT NULL, candidate_id TEXT NOT NULL, axis_id TEXT NOT NULL, direction TEXT NOT NULL, case_sha256 TEXT NOT NULL UNIQUE, preregistration_status TEXT NOT NULL, preregistered_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(robustness_spec_id) REFERENCES cad_robustness_specs(robustness_spec_id) ON DELETE RESTRICT )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_robustness_validation_decisions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE, robustness_spec_id TEXT NOT NULL, candidate_id TEXT NOT NULL, production_gate TEXT NOT NULL, support_state TEXT NOT NULL, decision_sha256 TEXT NOT NULL UNIQUE, decided_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(robustness_spec_id) REFERENCES cad_robustness_specs(robustness_spec_id) ON DELETE RESTRICT )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_search_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, search_spec_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, constraint_workspace_hash TEXT NOT NULL, payload_json TEXT NOT NULL, search_spec_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_standards_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, system_variant_id TEXT, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, profile_semantic_hash TEXT NOT NULL, evaluation_sha256 TEXT NOT NULL UNIQUE, reevaluation_of_id TEXT, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(profile_id, profile_version) REFERENCES cad_standards_profiles(profile_id, profile_version), FOREIGN KEY(reevaluation_of_id) REFERENCES cad_standards_evaluations(evaluation_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_standards_observation_authorities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, authority_id TEXT NOT NULL UNIQUE, semantic_hash_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, system_variant_id TEXT, payload_json TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variants ( seq INTEGER PRIMARY KEY AUTOINCREMENT, variant_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, baseline_revision_id TEXT NOT NULL, baseline_content_hash TEXT NOT NULL, parent_variant_id TEXT, variant_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(baseline_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(parent_variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_comparison_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, comparison_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_spaces ( seq INTEGER PRIMARY KEY AUTOINCREMENT, topology_search_id TEXT NOT NULL UNIQUE, topology_search_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, baseline_revision_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(baseline_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_geometry_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, system_variant_id TEXT, system_variant_sha256 TEXT, projector_specification_sha256 TEXT NOT NULL, request_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(projector_specification_sha256) REFERENCES cad_projector_specifications(specification_sha256) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wave_excitation_source_assets ( source_asset_sha256 TEXT PRIMARY KEY REFERENCES cad_measurement_assets(sha256), filename TEXT, media_type TEXT, declared_schema TEXT, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wave_source_excitation_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, r110_compiled_source_sha256 TEXT NOT NULL, excitation_id TEXT NOT NULL, excitation_semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(excitation_id) REFERENCES cad_acoustic_wave_excitations(excitation_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS capture_receiver_deliveries ( delivery_key TEXT PRIMARY KEY, pairing_id TEXT NOT NULL REFERENCES capture_receiver_pairings(pairing_id), artifact_kind TEXT NOT NULL, artifact_id TEXT, artifact_digest TEXT, capture_revision_id TEXT, bundle_digest TEXT, archive_sha256 TEXT NOT NULL, archive_bytes INTEGER NOT NULL, outcome TEXT NOT NULL, staging_ref TEXT, lineage_digest TEXT, detail TEXT NOT NULL, received_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS scene_recovery_snapshots ( document_id TEXT PRIMARY KEY, source_revision_id TEXT, updated_at_utc TEXT NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(source_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_scene_snapshots ( seq INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, system_variant_id TEXT, system_variant_sha256 TEXT, r120_compiled_geometry_id TEXT NOT NULL, r120_compiled_geometry_sha256 TEXT NOT NULL, material_boundary_configuration_sha256 TEXT NOT NULL, environment_authority_sha256 TEXT, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(system_variant_id) REFERENCES cad_system_variants(variant_id), FOREIGN KEY(r120_compiled_geometry_id) REFERENCES cad_r120_compiled_geometry(compiled_geometry_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_treatment_placements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL, placement_version INTEGER NOT NULL, lifecycle TEXT NOT NULL, definition_id TEXT NOT NULL, definition_version TEXT NOT NULL, definition_sha256 TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, system_variant_id TEXT, placement_sha256 TEXT NOT NULL UNIQUE, previous_placement_sha256 TEXT, payload_json TEXT NOT NULL, UNIQUE(instance_id, placement_version), FOREIGN KEY(definition_id, definition_version) REFERENCES cad_acoustic_treatment_definitions(definition_id, definition_version), FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(system_variant_id) REFERENCES cad_system_variants(variant_id), FOREIGN KEY(previous_placement_sha256) REFERENCES cad_acoustic_treatment_placements(placement_sha256) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_adaptive_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, search_spec_id TEXT NOT NULL, validation_id TEXT NOT NULL, execution_scope TEXT NOT NULL, selected_candidate_id TEXT NOT NULL, adaptive_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_coverage_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, scenario_id TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, variant_id TEXT NOT NULL, equipment_definition_sha256 TEXT NOT NULL, directivity_dataset_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(scenario_id) REFERENCES cad_coverage_scenarios(scenario_id), FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_direct_level_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, scenario_id TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, variant_id TEXT NOT NULL, equipment_definition_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(scenario_id) REFERENCES cad_direct_level_scenarios(scenario_id), FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_extended_search_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, extended_search_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, base_search_spec_id TEXT NOT NULL, capability_id TEXT NOT NULL, extended_search_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(base_search_spec_id) REFERENCES cad_search_specs(search_spec_id), FOREIGN KEY(capability_id) REFERENCES cad_extended_model_capabilities(capability_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_frequency_responses ( dataset_id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL UNIQUE REFERENCES cad_measurements(measurement_id), frequency_blob BLOB NOT NULL, level_blob BLOB NOT NULL, phase_blob BLOB, phase_status TEXT NOT NULL, level_reference TEXT NOT NULL, smoothing TEXT, processing_json TEXT NOT NULL, source_sha256 TEXT NOT NULL REFERENCES cad_measurement_assets(sha256), importer_version TEXT NOT NULL, dataset_sha256 TEXT, transformation_sha256 TEXT )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_impulse_responses ( dataset_id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), samples_blob BLOB NOT NULL, sample_rate_hz REAL NOT NULL, start_time_s REAL NOT NULL, t0_semantics TEXT NOT NULL, amplitude_reference TEXT NOT NULL, normalized INTEGER NOT NULL, window_kind TEXT, ir_semantics TEXT NOT NULL, calibration_state TEXT NOT NULL, processing_json TEXT NOT NULL, source_sha256 TEXT NOT NULL REFERENCES cad_measurement_assets(sha256), importer_version TEXT NOT NULL, dataset_sha256 TEXT NOT NULL, transformation_sha256 TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_joint_candidate_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_binding_id TEXT NOT NULL UNIQUE, evaluation_binding_sha256 TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, candidate_id TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(spec_id) REFERENCES cad_joint_optimization_specs(spec_id), FOREIGN KEY(candidate_id) REFERENCES cad_joint_candidates(candidate_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_joint_candidate_selections ( seq INTEGER PRIMARY KEY AUTOINCREMENT, selection_id TEXT NOT NULL UNIQUE, selection_sha256 TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, candidate_id TEXT NOT NULL, evaluation_binding_id TEXT, payload_json TEXT NOT NULL, FOREIGN KEY(spec_id) REFERENCES cad_joint_optimization_specs(spec_id), FOREIGN KEY(candidate_id) REFERENCES cad_joint_candidates(candidate_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_attachments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, attachment_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), kind TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES cad_measurement_assets(sha256), size_bytes INTEGER NOT NULL, note TEXT, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_dispositions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, disposition_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), disposition TEXT NOT NULL, correction_id TEXT, disposition_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_lineage ( seq INTEGER PRIMARY KEY AUTOINCREMENT, lineage_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), supersedes_measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), selected_measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), lineage_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), observation_sha256 TEXT NOT NULL, source_kind TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_runner_events ( event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES cad_measurement_runner_runs(run_id), cell_index INTEGER NOT NULL, status TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_model_validations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, validation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, search_spec_id TEXT NOT NULL, model_id TEXT NOT NULL, model_version TEXT NOT NULL, recommendation_gate TEXT NOT NULL, validation_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multifidelity_finalizations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, finalization_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, screening_evaluation_id TEXT NOT NULL, final_comparison_evaluation_id TEXT NOT NULL, claim_state TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(screening_evaluation_id) REFERENCES cad_multifidelity_screening_evaluations(evaluation_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_objective_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, search_spec_id TEXT NOT NULL, search_spec_sha256 TEXT NOT NULL, candidate_id TEXT NOT NULL, evaluation_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, candidate_set_sha256 TEXT, input_authorities_json TEXT, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pareto_sets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, pareto_set_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, search_spec_id TEXT NOT NULL, search_spec_sha256 TEXT NOT NULL, pareto_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_chain_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL UNIQUE, scenario_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, variant_id TEXT NOT NULL, source_equipment_sha256 TEXT NOT NULL, amplifier_capability_sha256 TEXT NOT NULL, speaker_load_sha256 TEXT, payload_json TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r110_compiled_source_models ( seq INTEGER PRIMARY KEY AUTOINCREMENT, semantic_sha256 TEXT NOT NULL UNIQUE, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, system_variant_id TEXT NOT NULL, system_variant_sha256 TEXT NOT NULL, source_entity_id TEXT NOT NULL, equipment_definition_sha256 TEXT NOT NULL, directivity_dataset_sha256 TEXT, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(system_variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r120_compile_inputs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, compiled_geometry_id TEXT NOT NULL UNIQUE, compiled_hash_sha256 TEXT NOT NULL, inputs_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(compiled_geometry_id) REFERENCES cad_r120_compiled_geometry(compiled_geometry_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r120_leak_portal_diagnostics ( seq INTEGER PRIMARY KEY AUTOINCREMENT, diagnostic_result_id TEXT NOT NULL UNIQUE, diagnostic_hash_sha256 TEXT NOT NULL UNIQUE, compiled_geometry_id TEXT NOT NULL, compiled_geometry_hash_sha256 TEXT NOT NULL, request_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(compiled_geometry_id) REFERENCES cad_r120_compiled_geometry(compiled_geometry_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_roomsim_batch_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_run_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, search_spec_id TEXT NOT NULL, search_spec_sha256 TEXT NOT NULL, candidate_set_sha256 TEXT NOT NULL, batch_spec_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_system_variant_applications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT NOT NULL UNIQUE, variant_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, baseline_revision_id TEXT NOT NULL, applied_revision_id TEXT NOT NULL UNIQUE, application_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, selected_at_utc TEXT NOT NULL, FOREIGN KEY(variant_id) REFERENCES cad_system_variants(variant_id), FOREIGN KEY(baseline_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(applied_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_comparison_bundles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, bundle_id TEXT NOT NULL UNIQUE, bundle_sha256 TEXT NOT NULL UNIQUE, comparison_id TEXT NOT NULL, variant_id TEXT NOT NULL, variant_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(comparison_id) REFERENCES cad_topology_comparison_specs(comparison_id), FOREIGN KEY(variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_comparison_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, comparison_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(comparison_id) REFERENCES cad_topology_comparison_specs(comparison_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_search_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, search_id TEXT NOT NULL UNIQUE, search_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, baseline_revision_id TEXT NOT NULL, template_variant_id TEXT NOT NULL, topology_search_id TEXT NOT NULL, topology_option_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(baseline_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(template_variant_id) REFERENCES cad_system_variants(variant_id), FOREIGN KEY(topology_search_id) REFERENCES cad_topology_spaces(topology_search_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_space_options ( seq INTEGER PRIMARY KEY AUTOINCREMENT, topology_search_id TEXT NOT NULL, option_id TEXT NOT NULL, template_variant_id TEXT NOT NULL, UNIQUE(topology_search_id, option_id), FOREIGN KEY(topology_search_id) REFERENCES cad_topology_spaces(topology_search_id), FOREIGN KEY(template_variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_treatment_boundary_compositions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, composition_id TEXT NOT NULL UNIQUE, composition_hash_sha256 TEXT NOT NULL UNIQUE, scene_revision_id TEXT NOT NULL, compiled_geometry_id TEXT NOT NULL, host_surface_id TEXT NOT NULL, target_domain TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(compiled_geometry_id) REFERENCES cad_r120_compiled_geometry(compiled_geometry_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_validation_campaigns ( seq INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, search_spec_id TEXT NOT NULL, model_id TEXT NOT NULL, model_version TEXT NOT NULL, candidate_set_sha256 TEXT NOT NULL, campaign_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_prediction_requests ( seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE, request_semantic_sha256 TEXT NOT NULL UNIQUE, acoustic_scene_snapshot_id TEXT NOT NULL, acoustic_scene_snapshot_sha256 TEXT NOT NULL, deterministic_input_hash TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(acoustic_scene_snapshot_id) REFERENCES cad_acoustic_scene_snapshots(snapshot_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dataset_level_references ( level_reference_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL UNIQUE REFERENCES cad_frequency_responses(dataset_id), level_reference_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_comparisons ( comparison_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, dataset_a_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id), dataset_b_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id), scene_revision_a_id TEXT NOT NULL REFERENCES scene_revisions(revision_id), scene_revision_b_id TEXT NOT NULL REFERENCES scene_revisions(revision_id), created_at TEXT NOT NULL, result_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_corrections ( seq INTEGER PRIMARY KEY AUTOINCREMENT, correction_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), dataset_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id), dataset_sha256 TEXT NOT NULL, correction_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_quality_reports ( seq INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL UNIQUE, measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), dataset_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id), raw_asset_sha256 TEXT NOT NULL REFERENCES cad_measurement_assets(sha256), report_sha256 TEXT NOT NULL, profile_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_chain_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, scenario_id TEXT NOT NULL, variant_id TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(scenario_id) REFERENCES cad_playback_chain_scenarios(scenario_id), FOREIGN KEY(variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_r120_leak_diagnostic_inputs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, diagnostic_result_id TEXT NOT NULL UNIQUE, diagnostic_hash_sha256 TEXT NOT NULL, inputs_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(diagnostic_result_id) REFERENCES cad_r120_leak_portal_diagnostics( diagnostic_result_id ) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_roomsim_candidate_attempts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL UNIQUE, batch_run_id TEXT NOT NULL, candidate_id TEXT NOT NULL, attempt_index INTEGER NOT NULL, status TEXT NOT NULL, attempt_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, completed_at_utc TEXT NOT NULL, UNIQUE(batch_run_id, candidate_id, attempt_index), FOREIGN KEY(batch_run_id) REFERENCES cad_roomsim_batch_specs(batch_run_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_comparison_selections ( seq INTEGER PRIMARY KEY AUTOINCREMENT, selection_id TEXT NOT NULL UNIQUE, selection_sha256 TEXT NOT NULL UNIQUE, comparison_evaluation_id TEXT NOT NULL, selected_variant_id TEXT NOT NULL, payload_json TEXT NOT NULL, selected_at_utc TEXT NOT NULL, FOREIGN KEY(comparison_evaluation_id) REFERENCES cad_topology_comparison_evaluations(evaluation_id), FOREIGN KEY(selected_variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_placement_candidates ( seq INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL UNIQUE, candidate_sha256 TEXT NOT NULL UNIQUE, search_id TEXT NOT NULL, candidate_set_sha256 TEXT NOT NULL, feasible_index INTEGER NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(search_id) REFERENCES cad_topology_search_specs(search_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_treatment_boundary_overlays ( seq INTEGER PRIMARY KEY AUTOINCREMENT, overlay_id TEXT NOT NULL UNIQUE, overlay_hash_sha256 TEXT NOT NULL UNIQUE, scene_revision_id TEXT NOT NULL, compiled_geometry_id TEXT NOT NULL, treatment_definition_id TEXT NOT NULL, treatment_definition_version TEXT NOT NULL, treatment_placement_instance_id TEXT NOT NULL, treatment_placement_version INTEGER NOT NULL, surface_binding_evaluation_hash_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id), FOREIGN KEY(compiled_geometry_id) REFERENCES cad_r120_compiled_geometry(compiled_geometry_id), FOREIGN KEY(treatment_definition_id, treatment_definition_version) REFERENCES cad_acoustic_treatment_definitions( definition_id, definition_version ), FOREIGN KEY( treatment_placement_instance_id, treatment_placement_version ) REFERENCES cad_acoustic_treatment_placements( instance_id, placement_version ) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_solver_dispatch_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, prediction_request_id TEXT NOT NULL, acoustic_scene_snapshot_id TEXT NOT NULL, adapter_descriptor_id TEXT NOT NULL, deterministic_solver_input_hash TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(prediction_request_id) REFERENCES cad_acoustic_prediction_requests(request_id), FOREIGN KEY(acoustic_scene_snapshot_id) REFERENCES cad_acoustic_scene_snapshots(snapshot_id), FOREIGN KEY(adapter_descriptor_id) REFERENCES cad_acoustic_solver_adapters(descriptor_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL REFERENCES scene_revisions(revision_id), system_variant_id TEXT NOT NULL REFERENCES cad_system_variants(variant_id), source_measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id), source_dataset_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id), quality_report_id TEXT NOT NULL REFERENCES cad_measurement_quality_reports(report_id), plan_semantic_sha256 TEXT NOT NULL, support_state TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_topology_candidate_variants ( seq INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL UNIQUE, variant_id TEXT NOT NULL UNIQUE, FOREIGN KEY(candidate_id) REFERENCES cad_topology_placement_candidates(candidate_id), FOREIGN KEY(variant_id) REFERENCES cad_system_variants(variant_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_exports ( seq INTEGER PRIMARY KEY AUTOINCREMENT, export_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL REFERENCES cad_calibration_plans(plan_id), exported_settings_semantic_sha256 TEXT NOT NULL, adapter_id TEXT NOT NULL, adapter_version TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_lifecycle_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL REFERENCES cad_calibration_plans(plan_id), state TEXT NOT NULL, event_semantic_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_verification_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_plan_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL REFERENCES cad_calibration_plans(plan_id), export_id TEXT NOT NULL REFERENCES cad_calibration_exports(export_id), verification_semantic_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_verification_completions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, completion_id TEXT NOT NULL UNIQUE, completion_sha256 TEXT NOT NULL UNIQUE, verification_plan_id TEXT NOT NULL REFERENCES cad_calibration_verification_plans(verification_plan_id), result TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_verification_registrations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, registration_id TEXT NOT NULL UNIQUE, registration_sha256 TEXT NOT NULL UNIQUE, verification_plan_id TEXT NOT NULL UNIQUE REFERENCES cad_calibration_verification_plans(verification_plan_id), verification_plan_semantic_sha256 TEXT NOT NULL, registered_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_request_snapshot ON cad_acoustic_prediction_requests( acoustic_scene_snapshot_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_snapshot_scene ON cad_acoustic_scene_snapshots(scene_revision_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_solver_adapter_role_seq ON cad_acoustic_solver_adapters( model_solver_role_id, acoustic_domain, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_solver_dispatch_request_seq ON cad_acoustic_solver_dispatch_bindings( prediction_request_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_solver_result_dispatch_seq ON cad_acoustic_solver_results( dispatch_binding_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_solver_result_request_seq ON cad_acoustic_solver_results( prediction_request_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_treatment_definition_id_seq ON cad_acoustic_treatment_definitions(definition_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_treatment_placement_instance_seq ON cad_acoustic_treatment_placements(instance_id, placement_version ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_treatment_placement_scene_seq ON cad_acoustic_treatment_placements(scene_revision_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_treatment_placement_variant_seq ON cad_acoustic_treatment_placements(system_variant_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_adaptive_search_seq ON cad_adaptive_plans(search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_amplifier_electrical_limit_seq ON cad_amplifier_electrical_limits(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_amplifier_headroom_scenario_seq ON cad_playback_chain_evaluations(scenario_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_amplifier_headroom_variant_seq ON cad_playback_chain_evaluations(variant_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_amplifier_output_capability_seq ON cad_amplifier_output_capabilities(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_applicability_attestations_spec ON cad_applicability_attestations(search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_impulse_responses_measurement ON cad_impulse_responses(measurement_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_measurement_attachments_document_seq ON cad_measurement_attachments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_measurement_attachments_measurement_seq ON cad_measurement_attachments(measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_measurement_comparisons_document_created ON cad_measurement_comparisons(document_id, created_at DESC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_measurement_plans_search_seq ON cad_measurement_plans(search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_measurements_document_imported ON cad_measurements(document_id, imported_at DESC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_measurements_revision ON cad_measurements(scene_revision_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_perturbation_samples_spec ON cad_perturbation_samples( robustness_spec_id, sample_index )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_robustness_evaluations_spec ON cad_robustness_evaluations( robustness_spec_id, objective_id )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_robustness_specs_candidate ON cad_robustness_specs( document_id, scene_revision_id, candidate_id, created_at_utc )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_search_document_seq ON cad_search_specs(document_id, seq DESC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calibration_export_plan_seq ON cad_calibration_exports(plan_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calibration_lifecycle_plan_seq ON cad_calibration_lifecycle_events(plan_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calibration_plan_document_seq ON cad_calibration_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calibration_verification_completion_plan_seq ON cad_calibration_verification_completions(verification_plan_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calibration_verification_plan_seq ON cad_calibration_verification_plans(plan_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_coverage_scenario_evaluation_seq ON cad_coverage_evaluations(scenario_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_coverage_scenario_seq ON cad_coverage_scenarios(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_coverage_variant_seq ON cad_coverage_evaluations(variant_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_current_topology_document ON cad_current_topologies(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_deterministic_ga_input_request_seq ON cad_deterministic_ga_execution_inputs( prediction_request_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_deterministic_path_request_seq ON cad_deterministic_path_artifacts( prediction_request_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stochastic_receiver_estimate_request_seq ON cad_stochastic_receiver_estimate_artifacts( prediction_request_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_direct_level_scenario_evaluation_seq ON cad_direct_level_evaluations(scenario_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_direct_level_scenario_seq ON cad_direct_level_scenarios(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_direct_level_variant_seq ON cad_direct_level_evaluations(variant_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_directivity_dataset_equipment ON cad_directivity_datasets(equipment_definition_sha256, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_directivity_dataset_seq ON cad_directivity_datasets(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_equipment_binding_entity ON cad_equipment_binding_semantics( document_id, entity_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_equipment_definition_seq ON cad_equipment_definitions(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_equipment_evidence_definition_seq ON cad_equipment_evidence_authorities( equipment_definition_sha256, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_extended_capability_model_seq ON cad_extended_model_capabilities(model_id, model_version, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_extended_parameter_evidence_model_seq ON cad_extended_parameter_evidence( model_id, model_version, parameter, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_extended_search_base_seq ON cad_extended_search_specs(base_search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_frequency_resolved_eval_seq ON cad_frequency_resolved_evaluations(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gain_structure_eval_scenario ON cad_gain_structure_evaluations(scenario_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gain_structure_scenario_doc ON cad_gain_structure_scenarios(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hybrid_result_snapshot_seq ON cad_hybrid_acoustic_results( acoustic_scene_snapshot_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_installation_context_entity ON cad_installation_contexts(document_id, entity_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_installed_binding_instance ON cad_installed_definition_bindings(instance_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_installed_instance_document ON cad_installed_equipment_instances(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_installed_observation_instance ON cad_installed_device_observations(instance_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_installed_replacement_document ON cad_installed_equipment_replacements(document_id, seq ASC)
    """
    ,
    """
    -- #842: one physical instance can be replaced only once and one
    -- successor can absorb only one replacement, enforced at the storage
    -- layer so concurrent writers cannot branch the lineage even if both
    -- observed a current predecessor.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_installed_replacement_predecessor ON cad_installed_equipment_replacements(removed_instance_id)
    """
    ,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_installed_replacement_successor ON cad_installed_equipment_replacements(installed_instance_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_joint_candidate_spec_seq ON cad_joint_candidates(spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_joint_evaluation_spec_seq ON cad_joint_candidate_evaluations(spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_joint_opt_spec_document_seq ON cad_joint_optimization_specs(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_joint_selection_spec_seq ON cad_joint_candidate_selections(spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_late_decay_estimate_request_seq ON cad_late_decay_estimate_artifacts( prediction_request_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_late_field_request_seq ON cad_late_field_artifacts( prediction_request_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_line_level_stage_seq ON cad_line_level_stages(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_corrections_measurement_seq ON cad_measurement_corrections(measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_dispositions_document_seq ON cad_measurement_dispositions(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_dispositions_measurement_seq ON cad_measurement_dispositions(measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_lineage_document_seq ON cad_measurement_lineage(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_lineage_measurement ON cad_measurement_lineage(measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_lineage_supersedes ON cad_measurement_lineage(supersedes_measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_observations_measurement_seq ON cad_measurement_observations(measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_quality_dataset_seq ON cad_measurement_quality_reports(dataset_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_quality_measurement_seq ON cad_measurement_quality_reports(measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_model_validation_search_seq ON cad_model_validations(search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_multi_sub_candidates_document_seq ON cad_multi_sub_candidates(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_multi_sub_evaluations_candidate_seq ON cad_multi_sub_evaluations(candidate_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_multi_sub_evaluations_document_seq ON cad_multi_sub_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_multi_sub_qualifications_document_seq ON cad_multi_sub_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_multi_sub_stage_comparisons_document_seq ON cad_multi_sub_stage_comparisons(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_multi_sub_deployments_document_seq ON cad_multi_sub_deployments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_multifidelity_finalization_screening_seq ON cad_multifidelity_finalizations( screening_evaluation_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_multifidelity_stage_plan_seq ON cad_multifidelity_stage_results(plan_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_o90_robust_pareto_search_seq ON cad_o90_robust_pareto_evaluations( search_spec_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decision_rule_spec_document ON cad_decision_rule_specs(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decision_verdict_rule ON cad_decision_verdicts(rule_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decision_verdict_document ON cad_decision_verdicts(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_uncertain_input_set_document ON cad_uncertain_input_sets(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_robust_design_document ON cad_robust_design_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stimulus_assets_document ON cad_stimulus_assets(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stimulus_assets_content ON cad_stimulus_assets(content_sha256, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stimulus_pins_measurement ON cad_stimulus_pins(measurement_ref, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stimulus_pins_document ON cad_stimulus_pins(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stimulus_eligibility_document ON cad_stimulus_eligibility(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bass_splice_evidence_group ON cad_bass_splice_evidence(document_id, role_id, sub_group_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bass_splice_evidence_document ON cad_bass_splice_evidence(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bass_qualifications_profile ON cad_bass_qualifications(profile_sha256, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bass_qualifications_document ON cad_bass_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spatial_designs_document ON cad_spatial_campaign_designs(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spatial_evaluations_design ON cad_spatial_campaign_evaluations(design_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spatial_evaluations_document ON cad_spatial_campaign_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spatial_bindings_design ON cad_spatial_campaign_bindings(design_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spatial_bindings_measurement ON cad_spatial_campaign_bindings(measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rp32_profiles_created ON cad_rp32_profiles(created_at_utc, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rp32_reconciliations_document ON cad_rp32_reconciliations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rp32_readiness_plan ON cad_rp32_readiness(plan_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rp32_plans_profile ON cad_rp32_verification_plans(profile_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rp32_records_plan ON cad_rp32_verification_records(plan_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rp32_reports_document ON cad_rp32_reports(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_robust_design_input_set ON cad_robust_design_assessments(input_set_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_o90e_case_spec_seq ON cad_robustness_validation_cases(robustness_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_o90e_decision_spec_seq ON cad_robustness_validation_decisions(robustness_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_objective_search_seq ON cad_objective_evaluations(search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pareto_search_seq ON cad_pareto_sets(search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_playback_chain_variant_seq ON cad_playback_chain_scenarios(variant_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_document_seq ON cad_prediction_results(document_id, seq DESC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_provider_binding_consumer ON cad_prediction_provider_bindings( consumer_kind, consumer_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hybrid_provider_binding_consumer ON cad_hybrid_prediction_provider_bindings( consumer_kind, consumer_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_provider_document_seq ON cad_prediction_providers(document_id, seq ASC)
    """
    ,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_run_result_kind ON cad_prediction_results(run_id, result_kind)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_run_seq ON cad_prediction_results(run_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_projector_spec_evidence_seq ON cad_projector_spec_evidence(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_projector_specification_seq ON cad_projector_specifications(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_proposal_objective_result_sample_seq ON cad_proposal_objective_result_authorities( sample_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_proposal_robust_pareto_topology_seq ON cad_proposal_robust_pareto_evaluations( topology_comparison_evaluation_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_proposal_robustness_variant_seq ON cad_proposal_robustness_specs( candidate_variant_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_r110_source_variant_entity ON cad_r110_compiled_source_models( system_variant_id, source_entity_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_r120_compiled_scene_revision ON cad_r120_compiled_geometry(scene_revision_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_r120_leak_compiled_geometry ON cad_r120_leak_portal_diagnostics(compiled_geometry_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_r140_attempt_task_seq ON cad_r140_execution_attempts(task_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_r140_gpu_authority_kind_seq ON cad_r140_gpu_authorities(authority_kind, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_r140_result_task_seq ON cad_r140_execution_results(task_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_r140_task_plan_stage_seq ON cad_r140_execution_tasks(plan_id, stage_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_roomsim_attempt_batch_seq ON cad_roomsim_candidate_attempts(batch_run_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_roomsim_attempt_candidate_seq ON cad_roomsim_candidate_attempts(batch_run_id, candidate_id, attempt_index ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_roomsim_batch_search_seq ON cad_roomsim_batch_specs(search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_scene_revisions_document_seq ON scene_revisions(document_id, seq DESC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_speaker_electrical_load_seq ON cad_speaker_electrical_loads(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_speaker_impedance_seq ON cad_speaker_impedances(seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_standards_evaluation_document_seq ON cad_standards_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_standards_evaluation_scene_seq ON cad_standards_evaluations(scene_revision_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_standards_observation_scene_seq ON cad_standards_observation_authorities( scene_revision_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_standards_profile_id_seq ON cad_standards_profiles(profile_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_system_variant_application_document_seq ON cad_system_variant_applications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_system_variant_as_built_revision ON cad_system_variant_as_built( as_built_revision_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_system_variant_document_seq ON cad_system_variants(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_system_variant_measured_as_built_seq ON cad_system_variant_measured( as_built_record_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_target_lineages_document ON cad_measurement_target_lineages(document_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_target_lineages_point ON cad_measurement_target_lineages(measurement_point_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_topology_candidate_search_feasible ON cad_topology_placement_candidates( search_id, feasible_index ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_topology_comparison_bundle_spec_seq ON cad_topology_comparison_bundles(comparison_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_topology_comparison_evaluation_spec_seq ON cad_topology_comparison_evaluations(comparison_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_topology_comparison_selection_eval_seq ON cad_topology_comparison_selections( comparison_evaluation_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_topology_comparison_spec_document_seq ON cad_topology_comparison_specs(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_topology_search_document_seq ON cad_topology_search_specs(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_topology_space_document_seq ON cad_topology_spaces(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_treatment_comparison_scene_seq ON cad_acoustic_treatment_comparisons( scene_revision_id, seq ASC )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_treatment_comparison_outcomes ( seq INTEGER PRIMARY KEY AUTOINCREMENT, outcome_id TEXT NOT NULL UNIQUE, outcome_sha256 TEXT NOT NULL UNIQUE, comparison_id TEXT NOT NULL, document_id TEXT NOT NULL, compatibility TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(comparison_id) REFERENCES cad_acoustic_treatment_comparisons(comparison_id) )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_treatment_outcome_comparison ON cad_treatment_comparison_outcomes( comparison_id, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_treatment_composition_scene ON cad_treatment_boundary_compositions(scene_revision_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_treatment_evidence_source ON cad_treatment_evidence_authorities( source_kind, source_id, source_version, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_treatment_overlay_placement ON cad_treatment_boundary_overlays( treatment_placement_instance_id, treatment_placement_version, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_treatment_overlay_scene ON cad_treatment_boundary_overlays(scene_revision_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_upgrade_adoption_document ON cad_upgrade_adoptions(document_id, upgrade_sha256, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_validation_campaign_registrations_campaign ON cad_validation_campaign_registrations(campaign_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_validation_campaign_search_seq ON cad_validation_campaigns(search_spec_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_video_geometry_revision_seq ON cad_video_geometry_evaluations(scene_revision_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_video_geometry_variant_seq ON cad_video_geometry_evaluations(system_variant_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wave_excitation_equipment_seq ON cad_acoustic_wave_excitations( equipment_definition_sha256, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wave_excitation_evidence_source ON cad_wave_excitation_evidence_authorities( evidence_kind, source_sha256, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wave_source_binding_r110_seq ON cad_wave_source_excitation_bindings( r110_compiled_source_sha256, seq ASC )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wiring_checks_document ON cad_wiring_checks(document_id, created_at_utc)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_applied_settings ( applied_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, calibration_plan_id TEXT NOT NULL, applied_sha256 TEXT NOT NULL, applied_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_applied_settings_plan ON cad_applied_settings(calibration_plan_id)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_constraint_snapshots ( snapshot_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, constraint_sha256 TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_design_checkpoints ( checkpoint_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, checkpoint_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_checkpoint_restores ( restore_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, restore_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_design_comparison_sets ( set_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, revision INTEGER NOT NULL, supersedes_set_id TEXT, set_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_cost_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, category TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cost_records_document ON cad_cost_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_correction_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, correction_subject_id TEXT NOT NULL, correction_subject_sha256 TEXT NOT NULL, state TEXT NOT NULL, scope TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_correction_qualification_document ON cad_correction_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_correction_qualification_subject ON cad_correction_qualifications(correction_subject_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_cost_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, variant_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cost_evaluations_document ON cad_cost_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_matrix_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, spec_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id) )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_matrix_specs_document ON cad_prediction_matrix_specs(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_matrix_result_sets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, spec_semantic_sha256 TEXT NOT NULL, document_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(spec_id) REFERENCES cad_prediction_matrix_specs(spec_id) )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_matrix_result_sets_spec ON cad_prediction_matrix_result_sets(spec_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_matrix_runs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(spec_id) REFERENCES cad_prediction_matrix_specs(spec_id) )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_matrix_runs_spec ON cad_prediction_matrix_runs(spec_id, seq ASC)
    """
    ,
    # Issue #534: local/offline presentation authority — immutable
    # presentation sessions, client proposals and synchronized A/B
    # bindings. All three are append-only manifests of references; the
    # payload carries the full sealed authority.
    """
    CREATE TABLE IF NOT EXISTS cad_presentation_sessions ( session_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, session_sha256 TEXT NOT NULL UNIQUE, status_label TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_presentation_sessions_document ON cad_presentation_sessions(document_id, created_at_utc)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_presentation_proposals ( proposal_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, document_id TEXT NOT NULL, kind TEXT NOT NULL, proposal_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_presentation_proposals_session ON cad_presentation_proposals(session_id, created_at_utc)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_presentation_sync_bindings ( binding_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, binding_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_presentation_sync_bindings_document ON cad_presentation_sync_bindings(document_id, created_at_utc)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_intervention_study_specs ( spec_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_intervention_specs_doc ON cad_intervention_study_specs(document_id, scene_revision_id, created_at_utc)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_intervention_alternatives ( alternative_id TEXT PRIMARY KEY, spec_id TEXT NOT NULL, family TEXT NOT NULL, payload_json TEXT NOT NULL, alternative_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, FOREIGN KEY(spec_id) REFERENCES cad_intervention_study_specs(spec_id) ON DELETE RESTRICT )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_intervention_alts_spec ON cad_intervention_alternatives(spec_id, created_at_utc)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_operating_presets ( preset_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, category TEXT NOT NULL, preset_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_applied_preset_states ( applied_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, preset_id TEXT NOT NULL, preset_sha256 TEXT NOT NULL, confirmed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_preset_measurement_bindings ( binding_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, preset_id TEXT NOT NULL, preset_sha256 TEXT NOT NULL, bound_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_project_notes ( note_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, note_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_project_notes_document ON cad_project_notes(document_id)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_health_baselines ( baseline_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, baseline_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_health_check_plans ( plan_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, baseline_id TEXT NOT NULL, baseline_sha256 TEXT NOT NULL, plan_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_health_check_runs ( run_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, plan_id TEXT NOT NULL, run_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_seat_priority_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_seat_priority_document ON cad_seat_priority_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS htdt_project_tombstones ( tombstone_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_id TEXT NOT NULL, display_name TEXT NOT NULL, deleted_at_utc TEXT NOT NULL, removed_rows INTEGER NOT NULL, estimated_bytes INTEGER NOT NULL, authorities_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_bass_management_profiles ( document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, lifecycle TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_bass_management_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_presentation_profiles ( document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_presentation_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, screen_entity_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,

# Repository-local CREATE statements moved under baseline ownership (#767).
    """
    CREATE TABLE IF NOT EXISTS cad_environment_profiles ( authority_id TEXT PRIMARY KEY, semantic_hash_sha256 TEXT NOT NULL, label TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_environment_selections ( document_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL, semantic_hash_sha256 TEXT NOT NULL, updated_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_analysis_studies ( study_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, study_kind TEXT NOT NULL, study_sha256 TEXT NOT NULL, supersedes_study_id TEXT, duplicated_from_study_id TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_cable_runs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, version TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, kind TEXT NOT NULL, total_length_m REAL NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(run_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_tolerance_profiles ( profile_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_commissioning_plans ( plan_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, tolerance_profile_id TEXT NOT NULL, plan_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_commissioning_runs ( run_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, plan_id TEXT NOT NULL, run_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY (plan_id) REFERENCES cad_commissioning_plans (plan_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_design_briefs ( brief_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, brief_sha256 TEXT NOT NULL, supersedes_brief_id TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS design_decisions ( decision_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, decision_scope TEXT NOT NULL, selected_ref_id TEXT NOT NULL, supersedes_decision_id TEXT, created_at_utc TEXT NOT NULL, decision_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_target_bindings ( binding_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, binding_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_capability_snapshots ( snapshot_id TEXT PRIMARY KEY, binding_sha256 TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL UNIQUE, probed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_observed_device_states ( observation_id TEXT PRIMARY KEY, binding_sha256 TEXT NOT NULL, observation_sha256 TEXT NOT NULL UNIQUE, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_proposed_device_actions ( action_id TEXT PRIMARY KEY, binding_sha256 TEXT NOT NULL, action_sha256 TEXT NOT NULL UNIQUE, planned_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_action_acks ( ack_id TEXT PRIMARY KEY, action_sha256 TEXT NOT NULL, ack_sha256 TEXT NOT NULL UNIQUE, acked_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_evidence_subjects ( subject_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, target_json TEXT NOT NULL, attribute TEXT NOT NULL, subject_sha256 TEXT, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_evidence_observations ( observation_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT, captured_at_utc TEXT, observation_sha256 TEXT, payload_json TEXT NOT NULL, FOREIGN KEY (subject_id) REFERENCES cad_evidence_subjects (subject_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_reconciliation_decisions ( decision_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, document_id TEXT NOT NULL, outcome TEXT NOT NULL, decision_sha256 TEXT NOT NULL, decided_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY (subject_id) REFERENCES cad_evidence_subjects (subject_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_target_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, document_id TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, UNIQUE(profile_id, profile_version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_isolation_assemblies ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assembly_id TEXT NOT NULL, document_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_isolation_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL, document_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_isolation_estimates ( seq INTEGER PRIMARY KEY AUTOINCREMENT, estimate_id TEXT NOT NULL, scenario_id TEXT NOT NULL, document_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_isolation_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL, document_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rack_definitions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, rack_id TEXT NOT NULL, document_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rack_layouts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, layout_id TEXT NOT NULL, rack_id TEXT NOT NULL, document_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_project_boms ( seq INTEGER PRIMARY KEY AUTOINCREMENT, bom_id TEXT NOT NULL, version TEXT NOT NULL, document_id TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, UNIQUE(bom_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_drawing_set_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, spec_id TEXT NOT NULL, spec_version TEXT NOT NULL, document_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, UNIQUE(spec_id, spec_version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_installation_drawing_sets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, drawing_set_id TEXT NOT NULL, document_id TEXT, installation_output_sha256 TEXT NOT NULL, spec_sha256 TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_field_labels ( seq INTEGER PRIMARY KEY AUTOINCREMENT, label_id TEXT NOT NULL, project_id TEXT NOT NULL, target_id TEXT NOT NULL, generation INTEGER NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, UNIQUE(target_id, generation) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_field_label_sheets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, sheet_id TEXT NOT NULL, project_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_field_evidence ( evidence_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, kind TEXT NOT NULL, asset_sha256 TEXT, evidence_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_field_evidence_targets ( evidence_id TEXT NOT NULL, target_kind TEXT NOT NULL, revision_id TEXT, entity_id TEXT, ref_id TEXT, FOREIGN KEY (evidence_id) REFERENCES cad_field_evidence (evidence_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_installation_datums ( seq INTEGER PRIMARY KEY AUTOINCREMENT, datum_id TEXT NOT NULL, version TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(datum_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_material_definitions ( material_id TEXT PRIMARY KEY, document_id TEXT, material_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_material_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, material_id TEXT NOT NULL, version TEXT NOT NULL, quantity TEXT NOT NULL, evidence_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE (material_id, version, quantity) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_excitation_assets ( excitation_asset_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, sha256 TEXT NOT NULL, excitation_sha256 TEXT NOT NULL UNIQUE, byte_length INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_stimulus_profiles ( stimulus_profile_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, stimulus_profile_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, spec_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, baseline_snapshot_sha256 TEXT NOT NULL, solver_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_results ( seq INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, spec_id TEXT NOT NULL, calibrated_model_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_models ( seq INTEGER PRIMARY KEY AUTOINCREMENT, materialized_model_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, calibration_result_id TEXT NOT NULL, baseline_snapshot_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_freezes ( seq INTEGER PRIMARY KEY AUTOINCREMENT, freeze_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, calibration_result_id TEXT NOT NULL, calibrated_model_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_holdout_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, freeze_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_evidence_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, campaign_sha256 TEXT NOT NULL, consumption_kind TEXT NOT NULL, freeze_id TEXT, record_id TEXT UNIQUE, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_measurement_registrations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, registration_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, measurement_id TEXT NOT NULL, comparability_state TEXT NOT NULL, partition TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(measurement_id) REFERENCES cad_measurements(measurement_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_prediction_measurement_residual_reports ( seq INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, registration_id TEXT NOT NULL, document_id TEXT NOT NULL, partition TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, FOREIGN KEY(registration_id) REFERENCES cad_prediction_measurement_registrations(registration_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_level_conditions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, condition_id TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, condition_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE (condition_id, scene_revision_id, condition_sha256) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_reference_playback_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, version TEXT NOT NULL, document_id TEXT, semantic_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE (profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS authoring_constraint_revisions ( constraint_revision_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, supersedes_id TEXT, scene_revision_id TEXT, payload_json TEXT NOT NULL, constraint_revision_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_room_operating_states ( seq INTEGER PRIMARY KEY AUTOINCREMENT, state_id TEXT NOT NULL, version TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT NOT NULL, scene_content_hash TEXT NOT NULL, name TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(state_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_speaker_definitions ( speaker_id TEXT PRIMARY KEY, document_id TEXT, speaker_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_speaker_datasets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, dataset_id TEXT NOT NULL UNIQUE, speaker_id TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL, dataset_sha256 TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE (speaker_id, version, kind) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_target_curve_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, version TEXT NOT NULL, document_id TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, UNIQUE(profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_plan_target_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, binding_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, bound_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_external_dependencies ( dependency_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, kind TEXT NOT NULL, authority_ref TEXT NOT NULL, dependency_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dependency_resolution_events ( event_id TEXT PRIMARY KEY, dependency_id TEXT NOT NULL, document_id TEXT NOT NULL, outcome TEXT NOT NULL, resolved_sha256 TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY (dependency_id) REFERENCES cad_external_dependencies (dependency_id) )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_analysis_studies_document ON cad_analysis_studies (document_id, created_at_utc)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cable_run_doc ON cad_cable_runs(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_commissioning_runs_plan ON cad_commissioning_runs (plan_id, created_at_utc)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_design_briefs_document ON cad_design_briefs (document_id, created_at_utc)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_design_decisions_document ON design_decisions(document_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_evidence_observations_subject ON cad_evidence_observations (subject_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_reconciliation_subject ON cad_reconciliation_decisions (subject_id, decided_at_utc)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acoustic_target_profile_doc ON cad_acoustic_target_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_isolation_assembly_doc ON cad_isolation_assemblies(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_isolation_scenario_doc ON cad_isolation_scenarios(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_isolation_estimate_doc ON cad_isolation_estimates(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_isolation_measurement_doc ON cad_isolation_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rack_definition_doc ON cad_rack_definitions(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rack_layout_doc ON cad_rack_layouts(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_project_bom_doc ON cad_project_boms(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_drawing_set_spec_doc ON cad_drawing_set_specs(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_installation_drawing_set_doc ON cad_installation_drawing_sets(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_field_label_project ON cad_field_labels(project_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_field_label_sheet_project ON cad_field_label_sheets(project_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_field_evidence_targets ON cad_field_evidence_targets (target_kind, revision_id, entity_id)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_field_evidence_document ON cad_field_evidence (document_id, created_at_utc)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_installation_datum_doc ON cad_installation_datums(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_excitation_assets_document ON cad_excitation_assets(document_id, created_at_utc)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stimulus_profiles_document ON cad_stimulus_profiles(document_id, created_at_utc)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calibration_evidence_campaign ON cad_calibration_evidence_events(campaign_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acr_document_created ON authoring_constraint_revisions(document_id, created_at_utc)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_room_operating_state_doc ON cad_room_operating_states(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_target_profile_doc ON cad_target_curve_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_plan_target_binding_plan ON cad_plan_target_bindings(plan_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_plan_target_binding_profile ON cad_plan_target_bindings(profile_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_external_dependencies_ref ON cad_external_dependencies (document_id, kind, authority_ref)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cad_resolution_events_dependency ON cad_dependency_resolution_events (dependency_id, created_at_utc)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_materials ( material_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_surface_material_assignments ( document_id TEXT NOT NULL, source_surface_id TEXT NOT NULL, material_id TEXT NOT NULL, material_sha256 TEXT NOT NULL, PRIMARY KEY (document_id, source_surface_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_source_poses ( observation_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, source_entity_id TEXT NOT NULL, verdict TEXT NOT NULL, observed_at_utc TEXT NOT NULL, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_compute_benchmarks ( benchmark_id TEXT PRIMARY KEY, case_name TEXT NOT NULL, spec_digest TEXT NOT NULL, hardware_profile_id TEXT NOT NULL, backend TEXT NOT NULL, solver_identity TEXT NOT NULL, runtime_s REAL NOT NULL, peak_memory_mb REAL NOT NULL, output_size_mb REAL NOT NULL, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_field_sessions ( session_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, task_kind TEXT NOT NULL, state TEXT NOT NULL, issued_at_utc TEXT NOT NULL, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_field_evidence_records ( record_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, review_state TEXT NOT NULL, captured_at_utc TEXT NOT NULL, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_listener_poses ( pose_id TEXT PRIMARY KEY, seat_entity_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_listener_pose_selections ( document_id TEXT NOT NULL, seat_entity_id TEXT NOT NULL, pose_id TEXT NOT NULL, pose_sha256 TEXT NOT NULL, PRIMARY KEY (document_id, seat_entity_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_pose_observations ( observation_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, measurement_ref TEXT, planned_target_ref TEXT, method TEXT NOT NULL, observed_at_utc TEXT NOT NULL, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_planned_observed_deltas ( delta_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL, classification TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS project_templates ( template_id TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL, template_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (template_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS template_instantiations ( instantiation_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, template_id TEXT NOT NULL, template_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, instantiation_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_review_notes ( note_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref TEXT NOT NULL, resolution TEXT NOT NULL, created_at_utc TEXT NOT NULL, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_screen_transfers ( transfer_id TEXT PRIMARY KEY, screen_entity_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_screen_transfer_selections ( document_id TEXT NOT NULL, screen_entity_id TEXT NOT NULL, transfer_id TEXT NOT NULL, transfer_sha256 TEXT NOT NULL, PRIMARY KEY (document_id, screen_entity_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_site_spaces ( space_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, kind TEXT NOT NULL, authority_version TEXT NOT NULL, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_site_relationships ( relationship_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, kind TEXT NOT NULL, space_a_id TEXT NOT NULL, space_b_id TEXT NOT NULL, semantic_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_responses ( response_id TEXT PRIMARY KEY, equipment_definition_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_response_selections ( document_id TEXT NOT NULL, equipment_definition_id TEXT NOT NULL, response_id TEXT NOT NULL, response_sha256 TEXT NOT NULL, PRIMARY KEY (document_id, equipment_definition_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_validation_cases ( evidence_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, provider_id TEXT NOT NULL, provider_version TEXT NOT NULL, geometry_class TEXT NOT NULL, source_class TEXT NOT NULL, observable TEXT NOT NULL, evidence_level TEXT NOT NULL, verdict TEXT NOT NULL, is_holdout INTEGER NOT NULL, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_visual_qa_verdicts ( verdict_id TEXT PRIMARY KEY, fixture_id TEXT NOT NULL, passed INTEGER NOT NULL, error_count INTEGER NOT NULL, warning_count INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS project_action_items ( action_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, status TEXT NOT NULL, priority TEXT NOT NULL, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, action_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_project_action_items_document ON project_action_items(document_id)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS assumption_decisions ( decision_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, attested_classification TEXT NOT NULL, supersedes_decision_id TEXT, created_at_utc TEXT NOT NULL, decision_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_assumption_decisions_document ON assumption_decisions(document_id)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_signal_paths ( document_id TEXT NOT NULL, path_id TEXT NOT NULL, version TEXT NOT NULL, path_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, path_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_signal_path_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, path_id TEXT NOT NULL, version TEXT NOT NULL, path_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lighting_scenes ( document_id TEXT NOT NULL, scene_id TEXT NOT NULL, version TEXT NOT NULL, scene_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, scene_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lighting_scene_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, scene_id TEXT NOT NULL, version TEXT NOT NULL, scene_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_tactile_actuator_definitions ( document_id TEXT NOT NULL, definition_id TEXT NOT NULL, version TEXT NOT NULL, definition_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, definition_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_tactile_processing_profiles ( document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_tactile_profile_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_usable_output_profiles ( document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, equipment_definition_id TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_usable_output_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, equipment_definition_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_photometric_profiles ( document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_photometric_profile_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_screen_optical_profiles ( document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_screen_optical_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, screen_entity_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_color_target_profiles ( document_id TEXT NOT NULL, target_id TEXT NOT NULL, version TEXT NOT NULL, target_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, target_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_color_target_selections ( selection_seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, target_id TEXT NOT NULL, version TEXT NOT NULL, target_sha256 TEXT NOT NULL, selected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_color_measurement_sets ( document_id TEXT NOT NULL, measurement_set_id TEXT NOT NULL, surface_entity_id TEXT NOT NULL, measurement_set_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, measurement_set_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ambient_reflectance_profiles ( document_id TEXT NOT NULL, profile_id TEXT NOT NULL, version TEXT NOT NULL, profile_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_active_lf_control_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, plan_id TEXT NOT NULL, plan_sha256 TEXT NOT NULL, representation TEXT NOT NULL, lifecycle TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(document_id, plan_id, plan_sha256) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_active_lf_control_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, plan_id TEXT NOT NULL, from_plan_sha256 TEXT NOT NULL, to_plan_sha256 TEXT NOT NULL, from_lifecycle TEXT NOT NULL, to_lifecycle TEXT NOT NULL, event_kind TEXT NOT NULL, evidence_ref TEXT, actor TEXT, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS htdt_acceptance_runs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, revision INTEGER NOT NULL, gate_id TEXT NOT NULL, status TEXT NOT NULL, run_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(run_id, revision) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS htdt_acceptance_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, step_id TEXT NOT NULL, kind TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL, relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_commissioning_sessions ( document_id TEXT NOT NULL, session_id TEXT NOT NULL, surface_entity_id TEXT NOT NULL, mode TEXT NOT NULL, session_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, session_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_commissioning_status_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, session_id TEXT NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL, event_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_readiness_reports ( document_id TEXT NOT NULL, report_id TEXT NOT NULL, session_id TEXT NOT NULL, state TEXT NOT NULL, report_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, report_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_diagnoses ( document_id TEXT NOT NULL, diagnosis_id TEXT NOT NULL, session_id TEXT NOT NULL, measurement_set_id TEXT NOT NULL, overall_status TEXT NOT NULL, diagnosis_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, diagnosis_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_action_proposals ( document_id TEXT NOT NULL, proposal_id TEXT NOT NULL, diagnosis_id TEXT NOT NULL, proposal_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, proposal_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_operator_adjustments ( document_id TEXT NOT NULL, adjustment_id TEXT NOT NULL, session_id TEXT NOT NULL, iteration_index INTEGER NOT NULL, adjustment_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, adjustment_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_before_after_comparisons ( document_id TEXT NOT NULL, comparison_id TEXT NOT NULL, session_id TEXT NOT NULL, iteration_index INTEGER NOT NULL, comparison_status TEXT NOT NULL, overall_direction TEXT NOT NULL, comparison_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, comparison_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_video_import_batches ( document_id TEXT NOT NULL, batch_id TEXT NOT NULL, session_id TEXT, measurement_set_id TEXT NOT NULL, format_id TEXT NOT NULL, batch_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (document_id, batch_id) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_uncertainty_budgets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, budget_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_id TEXT, dataset_id TEXT, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mub_document ON cad_measurement_uncertainty_budgets(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mub_measurement ON cad_measurement_uncertainty_budgets(measurement_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_significance_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT, budget_id TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_msa_document ON cad_measurement_significance_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_state_policies ( seq INTEGER PRIMARY KEY AUTOINCREMENT, policy_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, name TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mspol_document ON cad_measurement_state_policies(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_state_snapshots ( seq INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_id TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mss_document ON cad_measurement_state_snapshots(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mss_measurement ON cad_measurement_state_snapshots(measurement_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_state_verdicts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verdict_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, state TEXT NOT NULL, policy_id TEXT, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_msv_document ON cad_measurement_state_verdicts(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_transforms ( seq INTEGER PRIMARY KEY AUTOINCREMENT, transform_id TEXT NOT NULL UNIQUE, semantic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mtr_document ON cad_measurement_transforms(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_backup_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, artifact_sha256 TEXT NOT NULL UNIQUE, content_sha256 TEXT NOT NULL UNIQUE, device_equipment_id TEXT NOT NULL, manufacturer TEXT, model TEXT, firmware_version TEXT, backup_format TEXT, privacy_class TEXT NOT NULL, captured_at_utc TEXT, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_config_snapshots ( seq INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL UNIQUE, snapshot_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instance_id TEXT NOT NULL, evidence_class TEXT NOT NULL, transition_kind TEXT NOT NULL, firmware_version TEXT, state_content_sha256 TEXT NOT NULL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_firmware_transitions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, transition_id TEXT NOT NULL UNIQUE, transition_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instance_id TEXT NOT NULL, from_firmware TEXT, to_firmware TEXT NOT NULL, migration_result TEXT NOT NULL, rollback_status TEXT NOT NULL, updated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_known_good_baselines ( seq INTEGER PRIMARY KEY AUTOINCREMENT, baseline_id TEXT NOT NULL UNIQUE, baseline_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instance_id TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, promoted_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_replacement_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_instance_id TEXT NOT NULL, target_instance_id TEXT NOT NULL, assessed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_restore_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, restore_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, target_instance_id TEXT NOT NULL, artifact_sha256 TEXT, source_snapshot_sha256 TEXT, result_status TEXT NOT NULL, verdict TEXT NOT NULL, restored_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_devsnap_instance ON cad_device_config_snapshots(instance_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_devsnap_document ON cad_device_config_snapshots(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_devbackup_device ON cad_device_backup_artifacts(device_equipment_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_devfw_instance ON cad_device_firmware_transitions(instance_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_devkg_instance ON cad_device_known_good_baselines(instance_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_devrpl_instances ON cad_device_replacement_assessments(source_instance_id, target_instance_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_devrst_target ON cad_device_restore_records(target_instance_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_external_standard_documents ( seq INTEGER PRIMARY KEY AUTOINCREMENT, registry_key TEXT NOT NULL UNIQUE, standard_id TEXT NOT NULL, edition TEXT NOT NULL, document_number TEXT NOT NULL, publisher TEXT NOT NULL, lifecycle TEXT NOT NULL, admission TEXT NOT NULL, rights TEXT NOT NULL, replaced_by TEXT, registered_at_utc TEXT NOT NULL, document_sha TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_standard_evaluation_pins ( seq INTEGER PRIMARY KEY AUTOINCREMENT, pin_id TEXT NOT NULL UNIQUE, pin_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, standard_id TEXT NOT NULL, edition TEXT NOT NULL, mapping_id TEXT, result TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_standard_lifecycle_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, standard_id TEXT NOT NULL, edition TEXT NOT NULL, claimed_lifecycle TEXT NOT NULL, source_tier TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_standard_profile_mappings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, mapping_id TEXT NOT NULL UNIQUE, mapping_sha256 TEXT NOT NULL UNIQUE, standard_id TEXT NOT NULL, edition TEXT NOT NULL, mapping_version TEXT NOT NULL, calculation_version TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_standard_revision_diffs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, diff_id TEXT NOT NULL UNIQUE, diff_sha256 TEXT NOT NULL UNIQUE, from_standard_id TEXT NOT NULL, from_edition TEXT NOT NULL, to_standard_id TEXT NOT NULL, to_edition TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stddoc_standard ON cad_external_standard_documents(standard_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stdobs_standard ON cad_standard_lifecycle_observations(standard_id, edition, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stdmap_standard ON cad_standard_profile_mappings(standard_id, edition, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stdpin_standard ON cad_standard_evaluation_pins(standard_id, edition, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stdpin_document ON cad_standard_evaluation_pins(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_room_noise_metric_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, metric_family TEXT NOT NULL, standard_id TEXT NOT NULL, standard_edition TEXT NOT NULL, status TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_background_noise_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, temporal_class TEXT NOT NULL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_noise_criterion_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_id TEXT NOT NULL, measurement_sha256 TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, metric_family TEXT NOT NULL, applicability TEXT NOT NULL, rating_label TEXT, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_speech_intelligibility_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, standard_id TEXT NOT NULL, standard_edition TEXT NOT NULL, method TEXT NOT NULL, voice_class TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_sti_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, method TEXT NOT NULL, noise_measurement_id TEXT NOT NULL, sti_value REAL, applicability TEXT NOT NULL, seat_ref TEXT, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_sti_predictions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, prediction_id TEXT NOT NULL UNIQUE, prediction_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, method TEXT NOT NULL, model_version TEXT NOT NULL, validation_ref TEXT, noise_measurement_id TEXT NOT NULL, sti_value REAL, predicted_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dialogue_intelligibility_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, seat_count INTEGER NOT NULL, worst_seat_label TEXT, assessed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_content_loudness_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, standard_id TEXT NOT NULL, standard_edition TEXT NOT NULL, eligibility TEXT NOT NULL, algorithm_version TEXT NOT NULL, channel_config TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_programme_loudness_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, source_class TEXT NOT NULL, channel_config TEXT NOT NULL, integrated_loudness_lufs REAL, true_peak_dbtp REAL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_normalization_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_class TEXT NOT NULL, mode TEXT NOT NULL, target_lufs REAL, applied_gain_db REAL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_gain_states ( seq INTEGER PRIMARY KEY AUTOINCREMENT, state_id TEXT NOT NULL UNIQUE, state_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, normalization_observation_id TEXT, master_volume_db REAL, measured_in_room_spl_db REAL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_loudness_matching_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, comparison_label TEXT NOT NULL, target_quantity TEXT NOT NULL, residual_mismatch_db REAL NOT NULL, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rnprof_document ON cad_room_noise_metric_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bnmeas_document ON cad_background_noise_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_nceval_measurement ON cad_noise_criterion_evaluations(measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_nceval_document ON cad_noise_criterion_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stiprof_document ON cad_speech_intelligibility_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stimeas_document ON cad_sti_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stimeas_noise ON cad_sti_measurements(noise_measurement_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_stipred_document ON cad_sti_predictions(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dia_document ON cad_dialogue_intelligibility_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ldnprof_document ON cad_content_loudness_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_plm_document ON cad_programme_loudness_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_norm_document ON cad_normalization_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pgs_document ON cad_playback_gain_states(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lmr_document ON cad_loudness_matching_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp22_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, profile_sha256 TEXT NOT NULL UNIQUE, registry_key TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, UNIQUE(profile_id, profile_version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp22_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, evaluation_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_response_targets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, version TEXT NOT NULL, document_id TEXT NOT NULL, target_sha256 TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL, UNIQUE(profile_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spectral_balance_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, evaluation_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rp22eval_profile ON cad_rp22_evaluations(profile_id, profile_version, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rp22eval_document ON cad_rp22_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rstarget_document ON cad_response_targets(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sbeval_profile ON cad_spectral_balance_evaluations(profile_id, profile_version, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sbeval_document ON cad_spectral_balance_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_electrical_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scenario_sha256 TEXT NOT NULL, equipment_sha256 TEXT NOT NULL, verdict TEXT NOT NULL, capability_class TEXT NOT NULL, qualification_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_physical_interconnects ( seq INTEGER PRIMARY KEY AUTOINCREMENT, path_id TEXT NOT NULL, version TEXT NOT NULL, document_id TEXT NOT NULL, scene_revision_id TEXT, scene_content_hash TEXT, path_class TEXT NOT NULL, evidence_state TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, UNIQUE(path_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wiring_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT NOT NULL UNIQUE, path_id TEXT NOT NULL, path_sha256 TEXT NOT NULL, test_kind TEXT NOT NULL, result TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_logical_physical_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, logical_ref_kind TEXT NOT NULL, logical_ref_id TEXT NOT NULL, path_id TEXT NOT NULL, path_sha256 TEXT NOT NULL, semantic_sha256 TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, recorded_at_utc TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_elecqual_document ON cad_electrical_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_elecqual_scenario ON cad_electrical_qualifications(scenario_sha256, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_physint_document ON cad_physical_interconnects(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_physint_path ON cad_physical_interconnects(path_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wirever_path ON cad_wiring_verifications(path_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wirebind_document ON cad_logical_physical_bindings(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wirebind_logical ON cad_logical_physical_bindings(logical_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_monitoring_declarations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, declaration_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, capability_repr TEXT NOT NULL, remote_allowed INTEGER NOT NULL, declaration_sha256 TEXT NOT NULL UNIQUE, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lifecycle_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, domain TEXT NOT NULL, kind TEXT NOT NULL, collection_mode TEXT NOT NULL, observation_sha256 TEXT NOT NULL UNIQUE, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_change_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, kind TEXT NOT NULL, event_sha256 TEXT NOT NULL UNIQUE, occurred_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_trend_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, metric_key TEXT NOT NULL, state TEXT NOT NULL, assessment_sha256 TEXT NOT NULL UNIQUE, assessed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_symptom_episodes ( seq INTEGER PRIMARY KEY AUTOINCREMENT, episode_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, reason_state TEXT NOT NULL, resolved INTEGER NOT NULL, episode_sha256 TEXT NOT NULL UNIQUE, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_drift_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, dependency_status TEXT NOT NULL, operational_severity TEXT NOT NULL, evidence_certainty TEXT NOT NULL, assessment_sha256 TEXT NOT NULL UNIQUE, assessed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_reverification_triggers ( seq INTEGER PRIMARY KEY AUTOINCREMENT, trigger_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assessment_id TEXT NOT NULL, action TEXT NOT NULL, trigger_sha256 TEXT NOT NULL UNIQUE, decided_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_restore_confirmations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, confirmation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, restore_ref_kind TEXT NOT NULL, restore_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, confirmation_sha256 TEXT NOT NULL UNIQUE, confirmed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hlobs_subject ON cad_lifecycle_observations(document_id, subject_kind, subject_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hlobs_domain ON cad_lifecycle_observations(document_id, domain, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hldrf_subject ON cad_drift_assessments(document_id, subject_kind, subject_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hlrev_assessment ON cad_reverification_triggers(assessment_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_substitution_proposals ( seq INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, original_definition_id TEXT NOT NULL, proposed_definition_id TEXT NOT NULL, reason_kind TEXT NOT NULL, evidence_class TEXT NOT NULL, proposal_sha256 TEXT NOT NULL UNIQUE, requested_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_change_impact_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, proposal_id TEXT NOT NULL, proposal_sha256 TEXT NOT NULL, technical_verdict TEXT NOT NULL, assessment_sha256 TEXT NOT NULL UNIQUE, assessed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_substitution_decisions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, proposal_id TEXT NOT NULL, state TEXT NOT NULL, commercial_state TEXT NOT NULL, decision_sha256 TEXT NOT NULL UNIQUE, decided_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_asbuilt_reconciliations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, reconciliation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, proposal_id TEXT NOT NULL, verdict TEXT NOT NULL, reconciliation_sha256 TEXT NOT NULL UNIQUE, reconciled_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_equipment_schedule_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, phase TEXT NOT NULL, supersedes_schedule_id TEXT, schedule_sha256 TEXT NOT NULL UNIQUE, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_subprop_document ON cad_substitution_proposals(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_subimp_proposal ON cad_change_impact_assessments(proposal_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_subapr_proposal ON cad_substitution_decisions(proposal_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_subab_proposal ON cad_asbuilt_reconciliations(proposal_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_subsch_document ON cad_equipment_schedule_records(document_id, phase, seq ASC)
    """
    ,
    # REV56-TRANSPORT: #582 A/V latency authority
    """
    CREATE TABLE IF NOT EXISTS cad_av_latency_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT, profile_kind TEXT NOT NULL, label TEXT NOT NULL, standard_ref TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_av_latency_paths ( seq INTEGER PRIMARY KEY AUTOINCREMENT, path_id TEXT NOT NULL, version TEXT NOT NULL, path_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, signal_path_id TEXT, signal_path_version TEXT, signal_path_sha256 TEXT, display_picture_mode TEXT, audio_route TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(path_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_av_latency_path_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_id TEXT NOT NULL, path_version TEXT NOT NULL, path_sha256 TEXT NOT NULL, method TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_avsyncmeas_path ON cad_av_latency_path_measurements(path_id, path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_av_latency_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_id TEXT NOT NULL, path_version TEXT NOT NULL, path_sha256 TEXT NOT NULL, profile_id TEXT NOT NULL, verdict TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_avsyncqual_path ON cad_av_latency_qualifications(path_id, path_version, seq ASC)
    """
    ,
    # REV56-TRANSPORT: #583 HDMI design & verification authority
    """
    CREATE TABLE IF NOT EXISTS cad_hdmi_signal_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, width_px INTEGER NOT NULL, height_px INTEGER NOT NULL, refresh_hz REAL NOT NULL, hdcp_required TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hdmi_edid_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, artifact_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, signal_path_id TEXT NOT NULL, signal_path_version TEXT NOT NULL, signal_path_sha256 TEXT NOT NULL, interception_kind TEXT NOT NULL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_edidart_path ON cad_hdmi_edid_artifacts(signal_path_id, signal_path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hdmi_hdcp_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, signal_path_id TEXT NOT NULL, signal_path_version TEXT NOT NULL, signal_path_sha256 TEXT NOT NULL, negotiated_version TEXT NOT NULL, auth_state TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hdcpobs_path ON cad_hdmi_hdcp_observations(signal_path_id, signal_path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hdmi_link_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, signal_path_id TEXT NOT NULL, signal_path_version TEXT NOT NULL, signal_path_sha256 TEXT NOT NULL, link_mode TEXT NOT NULL, negotiated_rate_gbps REAL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_linkobs_path ON cad_hdmi_link_observations(signal_path_id, signal_path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hdmi_verification_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, signal_path_id TEXT NOT NULL, signal_path_version TEXT NOT NULL, signal_path_sha256 TEXT NOT NULL, required_profile_id TEXT NOT NULL, verdict TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hdmiver_path ON cad_hdmi_verification_records(signal_path_id, signal_path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hdmi_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, signal_path_id TEXT NOT NULL, signal_path_version TEXT NOT NULL, signal_path_sha256 TEXT NOT NULL, required_profile_id TEXT NOT NULL, theoretical_status TEXT NOT NULL, verdict TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hdmiqual_path ON cad_hdmi_qualifications(signal_path_id, signal_path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rp28_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT, label TEXT NOT NULL, standard_id TEXT NOT NULL, edition TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    # REV56-TRANSPORT: #591 networked AV qualification authority
    """
    CREATE TABLE IF NOT EXISTS cad_network_av_paths ( seq INTEGER PRIMARY KEY AUTOINCREMENT, path_id TEXT NOT NULL, version TEXT NOT NULL, path_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(path_id, version) )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_network_media_flows ( seq INTEGER PRIMARY KEY AUTOINCREMENT, flow_id TEXT NOT NULL UNIQUE, flow_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_id TEXT NOT NULL, path_version TEXT NOT NULL, provider_profile TEXT NOT NULL, delivery TEXT NOT NULL, clock_requirement TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_netflow_path ON cad_network_media_flows(path_id, path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_network_transport_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_id TEXT NOT NULL, path_version TEXT NOT NULL, kind TEXT NOT NULL, flow_id TEXT, interface_ref TEXT, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_netobs_path ON cad_network_transport_observations(path_id, path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_network_timing_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_id TEXT NOT NULL, path_version TEXT NOT NULL, ptp_domain INTEGER, node_state TEXT NOT NULL, lock_state TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ptpobs_path ON cad_network_timing_observations(path_id, path_version, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_network_av_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_id TEXT NOT NULL, path_version TEXT NOT NULL, flow_id TEXT NOT NULL, flow_sha256 TEXT NOT NULL, media_state TEXT NOT NULL, verdict TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_netqual_path ON cad_network_av_qualifications(path_id, path_version, seq ASC)
    """
    ,
    # REV56-BUILDING: #576 inter-room sound-isolation qualification
    """
    CREATE TABLE IF NOT EXISTS cad_isolation_elements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, element_id TEXT NOT NULL UNIQUE, element_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, construction_class TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_interroom_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL UNIQUE, scenario_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, source_region_id TEXT NOT NULL, receiving_region_id TEXT NOT NULL, construction_state TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_interroom_field_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scenario_id TEXT NOT NULL, scenario_sha256 TEXT NOT NULL, method_profile TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_irm_scenario ON cad_interroom_field_measurements(scenario_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_isolation_calibrations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, calibration_id TEXT NOT NULL UNIQUE, calibration_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scenario_id TEXT NOT NULL, model_ref TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_isolation_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scenario_id TEXT NOT NULL, scenario_sha256 TEXT NOT NULL, lifecycle_state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_isoqual_scenario ON cad_isolation_qualifications(scenario_id, seq ASC)
    """
    ,
    # REV56-BUILDING: #589 mechanical rattle / structure-borne noise
    """
    CREATE TABLE IF NOT EXISTS cad_mechanical_noise_tests ( seq INTEGER PRIMARY KEY AUTOINCREMENT, test_id TEXT NOT NULL UNIQUE, test_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, signal_type TEXT NOT NULL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rattle_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, event_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, test_id TEXT NOT NULL, test_sha256 TEXT NOT NULL, kind TEXT NOT NULL, localization_state TEXT NOT NULL, detected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rte_test ON cad_rattle_events(test_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_remediation_actions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL UNIQUE, action_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, action_kind TEXT NOT NULL, performed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mechanical_noise_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, overall_verdict TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    # REV56-BUILDING: #590 seating / occupancy acoustic authority
    """
    CREATE TABLE IF NOT EXISTS cad_seat_acoustic_models ( seq INTEGER PRIMARY KEY AUTOINCREMENT, seat_model_id TEXT NOT NULL UNIQUE, seat_model_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, seat_entity_id TEXT NOT NULL, geometry_source TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sam_entity ON cad_seat_acoustic_models(seat_entity_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_occupancy_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, occupancy_scenario_id TEXT NOT NULL UNIQUE, occupancy_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, state TEXT NOT NULL, comparability_key TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_clearance_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, occupancy_scenario_id TEXT NOT NULL, occupancy_scenario_sha256 TEXT NOT NULL, listener_ref TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_seating_commissioning_results ( seq INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL UNIQUE, result_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, verdict TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    # REV56-OPS: networked AV security authority (#598)
    """
    CREATE TABLE IF NOT EXISTS cad_security_assets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL UNIQUE, asset_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, management_reachability TEXT NOT NULL, vendor_support_status TEXT NOT NULL, lifecycle_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_secasset_doc ON cad_security_assets(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_security_credentials ( seq INTEGER PRIMARY KEY AUTOINCREMENT, credential_id TEXT NOT NULL UNIQUE, credential_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, account_ref TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL, default_credential_state TEXT NOT NULL, state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_seccred_doc ON cad_security_credentials(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_security_surfaces ( seq INTEGER PRIMARY KEY AUTOINCREMENT, surface_id TEXT NOT NULL UNIQUE, surface_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, exposure_scope TEXT NOT NULL, authentication_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_secsurf_doc ON cad_security_surfaces(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_security_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, kind TEXT NOT NULL, outcome TEXT NOT NULL, evidence_class TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_secobs_doc ON cad_security_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_security_risks ( seq INTEGER PRIMARY KEY AUTOINCREMENT, risk_id TEXT NOT NULL UNIQUE, risk_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT, subject_ref_id TEXT, title TEXT NOT NULL, likelihood_class TEXT NOT NULL, status TEXT NOT NULL, review_at_utc TEXT, raised_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_secrisk_doc ON cad_security_risks(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_remote_service_authorizations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, authorization_id TEXT NOT NULL UNIQUE, authorization_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_ref_id TEXT NOT NULL, method TEXT NOT NULL, state TEXT NOT NULL, valid_until_utc TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_secrem_doc ON cad_remote_service_authorizations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_security_test_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, kind TEXT NOT NULL, tool_provider TEXT NOT NULL, performed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sectest_doc ON cad_security_test_evidence(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_access_reviews ( seq INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL UNIQUE, review_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, trigger TEXT NOT NULL, reviewer TEXT NOT NULL, performed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_secacc_doc ON cad_access_reviews(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_security_reviews ( seq INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL UNIQUE, review_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_secrev_doc ON cad_security_reviews(document_id, seq ASC)
    """
    ,
    # REV56-OPS: control/automation scenario qualification (#601)
    """
    CREATE TABLE IF NOT EXISTS cad_control_surfaces ( seq INTEGER PRIMARY KEY AUTOINCREMENT, surface_id TEXT NOT NULL UNIQUE, surface_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, controller_ref_kind TEXT, controller_ref_id TEXT, controller_family TEXT NOT NULL, program_identity TEXT, program_version TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ctrlsurf_doc ON cad_control_surfaces(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_control_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL UNIQUE, scenario_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, surface_ref_id TEXT, kind TEXT NOT NULL, name TEXT NOT NULL, step_count INTEGER NOT NULL, failure_notification_required INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ctrlscn_doc ON cad_control_scenarios(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_control_scenario_runs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE, run_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scenario_ref_id TEXT NOT NULL, scenario_sha256 TEXT NOT NULL, outcome TEXT NOT NULL, failure_notification_outcome TEXT NOT NULL, started_at_utc TEXT NOT NULL, finished_at_utc TEXT, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ctrlrun_scn ON cad_control_scenario_runs(scenario_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_control_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scenario_ref_id TEXT NOT NULL, state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ctrlqual_doc ON cad_control_qualifications(document_id, seq ASC)
    """
    ,
    # REV56-OPS: safe-listening / test-exposure authority (#602)
    """
    CREATE TABLE IF NOT EXISTS cad_exposure_limits ( seq INTEGER PRIMARY KEY AUTOINCREMENT, limit_id TEXT NOT NULL UNIQUE, limit_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, basis TEXT NOT NULL, criterion TEXT NOT NULL, limit_level_db REAL NOT NULL, reference_window_s INTEGER, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_explim_doc ON cad_exposure_limits(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spl_capabilities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT NOT NULL UNIQUE, capability_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scope TEXT NOT NULL, capability_source TEXT NOT NULL, source_ref_id TEXT, max_continuous_db_spl REAL, max_peak_db_spl REAL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_splcap_doc ON cad_spl_capabilities(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_test_exposure_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, planned_level_db_spl REAL, planned_duration_s INTEGER, occupancy TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_explan_doc ON cad_test_exposure_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_exposure_gates ( seq INTEGER PRIMARY KEY AUTOINCREMENT, gate_id TEXT NOT NULL UNIQUE, gate_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assessment_ref_id TEXT NOT NULL, decision TEXT NOT NULL, decided_by TEXT NOT NULL, decided_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_expgate_doc ON cad_exposure_gates(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_exposure_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, limit_ref_id TEXT, state TEXT NOT NULL, projected_dose_pct REAL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_expassess_doc ON cad_exposure_assessments(document_id, seq ASC)
    """
    ,
    # REV56-INTEROP: openBIM IFC 4.3 interoperability (#578)
    """
    CREATE TABLE IF NOT EXISTS cad_ifc_import_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, artifact_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, file_name TEXT NOT NULL, schema_identifier TEXT NOT NULL, imported_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ifc_entity_mappings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, mapping_id TEXT NOT NULL UNIQUE, mapping_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, import_artifact_id TEXT NOT NULL, ifc_global_id TEXT, ifc_type TEXT NOT NULL, htdt_role TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ifcmap_artifact ON cad_ifc_entity_mappings(import_artifact_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ifc_revision_deltas ( seq INTEGER PRIMARY KEY AUTOINCREMENT, delta_id TEXT NOT NULL UNIQUE, delta_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, prior_artifact_id TEXT NOT NULL, new_artifact_id TEXT NOT NULL, reconciliation_state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ifc_intake_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, name TEXT NOT NULL, profile_version TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ifc_intake_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, file_sha256 TEXT NOT NULL, overall_state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ifc_exports ( seq INTEGER PRIMARY KEY AUTOINCREMENT, export_id TEXT NOT NULL UNIQUE, export_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, mode TEXT NOT NULL, source_artifact_id TEXT, step_sha256 TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_performance_fact_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, publisher TEXT NOT NULL, family TEXT NOT NULL, document_reference TEXT NOT NULL, maturity_state TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_performance_fact_products ( seq INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT NOT NULL UNIQUE, product_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, manufacturer TEXT NOT NULL, model TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_performance_facts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, fact_id TEXT NOT NULL UNIQUE, fact_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, product_id TEXT NOT NULL, product_sha256 TEXT NOT NULL, quantity_kind TEXT NOT NULL, evidence_class TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pff_product ON cad_performance_facts(product_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_performance_fact_imports ( seq INTEGER PRIMARY KEY AUTOINCREMENT, import_id TEXT NOT NULL UNIQUE, import_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, extraction_state TEXT NOT NULL, imported_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_performance_fact_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, product_id TEXT NOT NULL, product_sha256 TEXT NOT NULL, verdict TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_performance_fact_rebinds ( seq INTEGER PRIMARY KEY AUTOINCREMENT, rebind_id TEXT NOT NULL UNIQUE, rebind_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, from_profile_id TEXT NOT NULL, to_profile_id TEXT NOT NULL, decided_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rack_enclosures ( seq INTEGER PRIMARY KEY AUTOINCREMENT, rack_id TEXT NOT NULL UNIQUE, rack_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, enclosure_kind TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rackenv_doc ON cad_rack_enclosures(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rack_devices ( seq INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL UNIQUE, device_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, rack_ref_id TEXT NOT NULL, role TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rackdev_doc ON cad_rack_devices(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_branch_circuits ( seq INTEGER PRIMARY KEY AUTOINCREMENT, circuit_id TEXT NOT NULL UNIQUE, circuit_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, nominal_voltage_v REAL, breaker_rating_a REAL, continuous_load_policy TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_circuit_doc ON cad_branch_circuits(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_power_protection_devices ( seq INTEGER PRIMARY KEY AUTOINCREMENT, protection_id TEXT NOT NULL UNIQUE, protection_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_protdev_doc ON cad_power_protection_devices(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_poe_budgets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, poe_id TEXT NOT NULL UNIQUE, poe_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, standard TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_poe_doc ON cad_poe_budgets(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_infrastructure_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL UNIQUE, scenario_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_infscn_doc ON cad_infrastructure_scenarios(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_rack_thermal_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, rack_ref_id TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rmeas_doc ON cad_rack_thermal_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_infrastructure_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, scenario_ref_id TEXT NOT NULL, overall_state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rqual_scn ON cad_infrastructure_qualifications(scenario_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_immersive_contents ( seq INTEGER PRIMARY KEY AUTOINCREMENT, content_id TEXT NOT NULL UNIQUE, content_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, metadata_class TEXT NOT NULL, format_label TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_icont_doc ON cad_immersive_contents(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_renderer_capabilities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT NOT NULL UNIQUE, capability_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, model_label TEXT NOT NULL, capability_source TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rcap_doc ON cad_renderer_capabilities(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_speaker_layouts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, layout_id TEXT NOT NULL UNIQUE, layout_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT, evidence TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_slay_doc ON cad_speaker_layouts(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_render_sessions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL UNIQUE, session_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, content_ref_id TEXT NOT NULL, decoder_mode TEXT NOT NULL, upmixer_state TEXT NOT NULL, started_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rsess_doc ON cad_render_sessions(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_render_output_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, session_ref_id TEXT NOT NULL, capture_method TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_robs_sess ON cad_render_output_observations(session_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_render_path_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, session_ref_id TEXT NOT NULL, overall_state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rpqual_sess ON cad_render_path_qualifications(session_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_electrical_noise_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, symptom TEXT NOT NULL, instrument TEXT NOT NULL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_enobs_doc ON cad_electrical_noise_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_audio_interconnects ( seq INTEGER PRIMARY KEY AUTOINCREMENT, interconnect_id TEXT NOT NULL UNIQUE, interconnect_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, interface_class TEXT NOT NULL, shield_termination TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_icnx_doc ON cad_audio_interconnects(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_noise_isolation_tests ( seq INTEGER PRIMARY KEY AUTOINCREMENT, test_id TEXT NOT NULL UNIQUE, test_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, observation_ref_id TEXT NOT NULL, performed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_itest_obs ON cad_noise_isolation_tests(observation_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_humbuzz_diagnostics ( seq INTEGER PRIMARY KEY AUTOINCREMENT, diagnostic_id TEXT NOT NULL UNIQUE, diagnostic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, classification TEXT NOT NULL, hypothesis_state TEXT NOT NULL, recommendation TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hdiag_doc ON cad_humbuzz_diagnostics(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_noise_mitigations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL UNIQUE, attempt_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, diagnostic_ref TEXT NOT NULL, kind TEXT NOT NULL, outcome TEXT NOT NULL, performed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hmit_diag ON cad_noise_mitigations(diagnostic_ref, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_humbuzz_verdicts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verdict_id TEXT NOT NULL UNIQUE, verdict_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, diagnostic_ref TEXT NOT NULL, state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hverd_diag ON cad_humbuzz_verdicts(diagnostic_ref, seq ASC)
    """
    ,
    # REV57-METRO: measurement timebase / clock authority (#609)
    """
    CREATE TABLE IF NOT EXISTS cad_timebase_clock_domains ( seq INTEGER PRIMARY KEY AUTOINCREMENT, clock_domain_id TEXT NOT NULL UNIQUE, clock_domain_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, domain_kind TEXT NOT NULL, device_identity TEXT, nominal_sample_rate_hz REAL, effective_sample_rate_hz REAL, common_clock_group TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_clkdom_doc ON cad_timebase_clock_domains(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_timebases ( seq INTEGER PRIMARY KEY AUTOINCREMENT, timebase_id TEXT NOT NULL UNIQUE, timebase_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, topology TEXT NOT NULL, topology_evidence TEXT NOT NULL, sequential_anchor TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mtbase_doc ON cad_measurement_timebases(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_timebase_capability_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, timebase_ref_id TEXT NOT NULL, timing_uncertainty_s REAL, drift_material INTEGER, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tbcap_tb ON cad_timebase_capability_assessments(timebase_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tbcap_doc ON cad_timebase_capability_assessments(document_id, seq ASC)
    """
    ,
    # REV57-METRO: reproducible evidence bundle / integrity manifest (#610)
    """
    CREATE TABLE IF NOT EXISTS cad_evidence_bundles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, bundle_id TEXT NOT NULL UNIQUE, bundle_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL, completeness_profile TEXT NOT NULL, reproducibility_level TEXT NOT NULL, producer_software TEXT NOT NULL, producer_version TEXT NOT NULL, manifest_root_sha256 TEXT, created_at_utc TEXT NOT NULL, finalized_at_utc TEXT, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_evbun_doc ON cad_evidence_bundles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_evidence_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, artifact_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, bundle_id TEXT NOT NULL, logical_role TEXT NOT NULL, artifact_class TEXT NOT NULL, inclusion TEXT NOT NULL, package_path TEXT, required INTEGER NOT NULL, rights_sensitivity TEXT NOT NULL, digest TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_evart_bundle ON cad_evidence_artifacts(bundle_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_evart_doc ON cad_evidence_artifacts(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_evidence_derivation_edges ( seq INTEGER PRIMARY KEY AUTOINCREMENT, edge_id TEXT NOT NULL UNIQUE, edge_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, bundle_id TEXT NOT NULL, operation TEXT NOT NULL, software_identity TEXT NOT NULL, output_artifact_id TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_evedge_bundle ON cad_evidence_derivation_edges(bundle_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_evidence_attestations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, attestation_id TEXT NOT NULL UNIQUE, attestation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, bundle_ref_id TEXT NOT NULL, signer_identity TEXT NOT NULL, role TEXT NOT NULL, signed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_evatt_bundle ON cad_evidence_attestations(bundle_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_evidence_bundle_validations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verdict_id TEXT NOT NULL UNIQUE, verdict_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, bundle_ref_id TEXT NOT NULL, profile TEXT NOT NULL, state TEXT NOT NULL, validation_version TEXT NOT NULL, validated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_evval_bundle ON cad_evidence_bundle_validations(bundle_ref_id, seq ASC)
    """
    ,
    # REV57-METRO: instrument calibration lifecycle (#611)
    """
    CREATE TABLE IF NOT EXISTS cad_instrument_instances ( seq INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id TEXT NOT NULL UNIQUE, instrument_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, category TEXT NOT NULL, serial_or_instance_id TEXT NOT NULL, service_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calinst_doc ON cad_instrument_instances(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, calibration_id TEXT NOT NULL UNIQUE, calibration_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instrument_ref_id TEXT NOT NULL, event_kind TEXT NOT NULL, performed_at_utc TEXT NOT NULL, provider_or_lab TEXT, traceability_class TEXT NOT NULL, valid_until_utc TEXT, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calevt_inst ON cad_calibration_events(instrument_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calevt_doc ON cad_calibration_events(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_calibration_interval_policies ( seq INTEGER PRIMARY KEY AUTOINCREMENT, policy_id TEXT NOT NULL UNIQUE, policy_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instrument_ref_id TEXT, instrument_category TEXT, basis TEXT NOT NULL, nominal_interval_days INTEGER, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calpol_doc ON cad_calibration_interval_policies(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_instrument_verification_checks ( seq INTEGER PRIMARY KEY AUTOINCREMENT, check_id TEXT NOT NULL UNIQUE, check_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instrument_ref_id TEXT NOT NULL, kind TEXT NOT NULL, outcome TEXT NOT NULL, campaign_id TEXT, performed_at_utc TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calchk_inst ON cad_instrument_verification_checks(instrument_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_instrument_service_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, event_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instrument_ref_id TEXT NOT NULL, kind TEXT NOT NULL, occurred_at_utc TEXT NOT NULL, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calsvc_inst ON cad_instrument_service_events(instrument_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_instrument_fitness_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instrument_ref_id TEXT NOT NULL, at_utc TEXT NOT NULL, state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calfit_inst ON cad_instrument_fitness_assessments(instrument_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_out_of_tolerance_reviews ( seq INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL UNIQUE, review_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instrument_ref_id TEXT NOT NULL, triggering_ref_id TEXT NOT NULL, last_known_valid_at_utc TEXT, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_caloot_inst ON cad_out_of_tolerance_reviews(instrument_ref_id, seq ASC)
    """
    ,
    # REV57-PHYS: #613 geometry survey, #614 installed-source boundary,
    # #615 porous absorber authorities.
    """
    CREATE TABLE IF NOT EXISTS cad_geo_survey_instruments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id TEXT NOT NULL UNIQUE, instrument_sha256 TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, capability_class TEXT NOT NULL, manufacturer TEXT, model TEXT, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_geo_survey_campaigns ( seq INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL UNIQUE, campaign_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gsurvey_doc ON cad_geo_survey_campaigns(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_geo_element_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, element_id TEXT NOT NULL UNIQUE, element_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, element_key TEXT NOT NULL, observation_state TEXT NOT NULL, derivation_stage TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gev_doc ON cad_geo_element_evidence(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_geo_control_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, control_id TEXT NOT NULL UNIQUE, control_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, kind TEXT NOT NULL, campaign_id TEXT, instrument_id TEXT, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gctrl_doc ON cad_geo_control_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_geo_reconciliations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, reconciliation_id TEXT NOT NULL UNIQUE, reconciliation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, element_key TEXT NOT NULL, approved_change INTEGER NOT NULL DEFAULT 0, reconciled_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_grec_doc ON cad_geo_reconciliations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_geo_task_requirements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL UNIQUE, task_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, task_class TEXT NOT NULL, tolerance_mm REAL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_geo_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, element_count INTEGER NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gqual_doc ON cad_geo_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_src_meas_conditions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, condition_id TEXT NOT NULL UNIQUE, condition_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_dataset_id TEXT NOT NULL, environment TEXT NOT NULL, evidence_class TEXT NOT NULL, includes_installed_boundary INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_smc_dataset ON cad_src_meas_conditions(source_dataset_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_src_mounting_conditions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, mounting_id TEXT NOT NULL UNIQUE, mounting_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_ref TEXT NOT NULL, kind TEXT NOT NULL, rear_cavity TEXT NOT NULL, declared_by TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_smnt_doc ON cad_src_mounting_conditions(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_src_boundary_corrections ( seq INTEGER PRIMARY KEY AUTOINCREMENT, correction_id TEXT NOT NULL UNIQUE, correction_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL, model_identity TEXT NOT NULL, model_version TEXT NOT NULL, domain TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_src_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, mounting_id TEXT NOT NULL, mounting_sha256 TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_smeas_mount ON cad_src_measurements(mounting_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_src_boundary_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_dataset_id TEXT NOT NULL, mounting_id TEXT NOT NULL, mounting_sha256 TEXT NOT NULL, state TEXT NOT NULL, achieved_capability TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sbqual_src ON cad_src_boundary_qualifications(source_dataset_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sbqual_mount ON cad_src_boundary_qualifications(mounting_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pam_parameter_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, material_ref TEXT NOT NULL, quantity TEXT NOT NULL, evidence_class TEXT NOT NULL, method TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pamparam_mat ON cad_pam_parameter_evidence(material_ref, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pam_material_models ( seq INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL UNIQUE, model_sha256 TEXT NOT NULL UNIQUE, family TEXT NOT NULL, label TEXT NOT NULL, version TEXT NOT NULL, compute_capable INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pam_buildups ( seq INTEGER PRIMARY KEY AUTOINCREMENT, buildup_id TEXT NOT NULL UNIQUE, buildup_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT, backing TEXT NOT NULL, anisotropy TEXT NOT NULL, layer_count INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pambu_doc ON cad_pam_buildups(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pam_predictions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, prediction_id TEXT NOT NULL UNIQUE, prediction_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, model_id TEXT NOT NULL, model_sha256 TEXT NOT NULL, buildup_id TEXT NOT NULL, buildup_sha256 TEXT NOT NULL, eligibility TEXT NOT NULL, evidence_class TEXT NOT NULL, computed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pampred_bu ON cad_pam_predictions(buildup_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pam_fit_comparisons ( seq INTEGER PRIMARY KEY AUTOINCREMENT, comparison_id TEXT NOT NULL UNIQUE, comparison_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, prediction_id TEXT, prediction_sha256 TEXT, measured_evidence_ref TEXT NOT NULL, verdict TEXT NOT NULL, compared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pamfit_doc ON cad_pam_fit_comparisons(document_id, seq ASC)
    """
    ,
    # REV57-PROJ: spatial projection-image qualification (#619)
    """
    CREATE TABLE IF NOT EXISTS cad_spatial_measurement_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, layout TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spplan_doc ON cad_spatial_measurement_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spatial_measurement_sets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, set_id TEXT NOT NULL UNIQUE, set_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, evidence_kind TEXT NOT NULL, stimulus_profile TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spset_plan ON cad_spatial_measurement_sets(plan_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spset_doc ON cad_spatial_measurement_sets(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spatial_derived_maps ( seq INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL UNIQUE, map_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_set_ref_id TEXT NOT NULL, quantity TEXT NOT NULL, interpolation_algorithm TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_spmap_set ON cad_spatial_derived_maps(source_set_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spatial_uniformity_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, set_ref_id TEXT NOT NULL, coverage_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_speval_set ON cad_spatial_uniformity_evaluations(set_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_speval_doc ON cad_spatial_uniformity_evaluations(document_id, seq ASC)
    """
    ,
    # REV57-PROJ: projection image-geometry / masking (#622)
    """
    CREATE TABLE IF NOT EXISTS cad_presentation_geometry_bindings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL UNIQUE, binding_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, projected_aspect REAL, content_aspect REAL, keystone_state TEXT NOT NULL, anamorphic_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_geobind_doc ON cad_presentation_geometry_bindings(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_image_geometry_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, binding_ref_id TEXT NOT NULL, method TEXT NOT NULL, test_pattern_identity TEXT NOT NULL, physical_alignment TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_geomeas_bind ON cad_image_geometry_measurements(binding_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_geomeas_doc ON cad_image_geometry_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lens_memory_recalls ( seq INTEGER PRIMARY KEY AUTOINCREMENT, recall_id TEXT NOT NULL UNIQUE, recall_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, memory_id TEXT NOT NULL, cycle_index INTEGER NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lensrec_doc ON cad_lens_memory_recalls(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_geometry_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, binding_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, physical_alignment TEXT NOT NULL, digital_correction_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_geoeval_bind ON cad_geometry_evaluations(binding_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_geoeval_doc ON cad_geometry_evaluations(document_id, seq ASC)
    """
    ,
    # REV57-PROJ: projector hush-box / enclosure co-design (#624)
    """
    CREATE TABLE IF NOT EXISTS cad_projector_install_constraints ( seq INTEGER PRIMARY KEY AUTOINCREMENT, constraint_id TEXT NOT NULL UNIQUE, constraint_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, manufacturer TEXT, model TEXT, source_document TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pjcons_doc ON cad_projector_install_constraints(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_projector_enclosure_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, constraint_ref_id TEXT, remote_projection INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hushplan_doc ON cad_projector_enclosure_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_enclosure_operating_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, scenario TEXT NOT NULL, duration_s REAL NOT NULL, projector_fan_state TEXT NOT NULL, protection_event TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hushobs_plan ON cad_enclosure_operating_observations(plan_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hushobs_doc ON cad_enclosure_operating_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_enclosure_acoustic_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, acoustic_id TEXT NOT NULL UNIQUE, acoustic_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, comparability TEXT NOT NULL, pre_spl_db REAL, post_spl_db REAL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hushac_plan ON cad_enclosure_acoustic_observations(plan_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_enclosure_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, thermal_state TEXT NOT NULL, acoustic_state TEXT NOT NULL, optical_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hushqual_plan ON cad_enclosure_qualifications(plan_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hushqual_doc ON cad_enclosure_qualifications(document_id, seq ASC)
    """
    ,
    # REV57-PROJ: projector optical-radiation safety (#627)
    """
    CREATE TABLE IF NOT EXISTS cad_projector_safety_identities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, identity_id TEXT NOT NULL UNIQUE, identity_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, illumination_source TEXT NOT NULL, risk_group TEXT NOT NULL, laser_class TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pjsafe_doc ON cad_projector_safety_identities(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_manufacturer_safety_constraints ( seq INTEGER PRIMARY KEY AUTOINCREMENT, constraint_id TEXT NOT NULL UNIQUE, constraint_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, safety_identity_ref_id TEXT, source_document TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pjscons_ident ON cad_manufacturer_safety_constraints(safety_identity_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pjscons_doc ON cad_manufacturer_safety_constraints(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_projector_placements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, placement_id TEXT NOT NULL UNIQUE, placement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, safety_identity_ref_id TEXT NOT NULL, operating_state TEXT NOT NULL, throw_distance_m REAL, viewer_position TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pjplace_ident ON cad_projector_placements(safety_identity_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pjplace_doc ON cad_projector_placements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_optical_safety_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, placement_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pjseval_place ON cad_optical_safety_evaluations(placement_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pjseval_doc ON cad_optical_safety_evaluations(document_id, seq ASC)
    """
    ,
    # REV57-DISP: #625 direct-view display, #626 observer metamerism,
    # #633 viewing environment authorities.
    """
    CREATE TABLE IF NOT EXISTS cad_dv_display_states ( seq INTEGER PRIMARY KEY AUTOINCREMENT, display_state_id TEXT NOT NULL UNIQUE, display_state_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, panel_technology TEXT NOT NULL, content_mode TEXT NOT NULL, local_dimming TEXT NOT NULL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dvstate_doc ON cad_dv_display_states(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dv_stimulus_contexts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, stimulus_context_id TEXT NOT NULL UNIQUE, stimulus_context_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stimulus_ref TEXT, field_kind TEXT NOT NULL, window_size_percent REAL, apl_percent REAL, content_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dvstim_doc ON cad_dv_stimulus_contexts(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dv_photometric_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_id TEXT NOT NULL, display_state_sha256 TEXT NOT NULL, stimulus_context_id TEXT NOT NULL, stimulus_context_sha256 TEXT NOT NULL, quantity TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dvmeas_state ON cad_dv_photometric_measurements(display_state_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dv_temporal_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_id TEXT NOT NULL, display_state_sha256 TEXT NOT NULL, state TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dvobs_state ON cad_dv_temporal_observations(display_state_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dv_spatial_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, spatial_id TEXT NOT NULL UNIQUE, spatial_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_id TEXT NOT NULL, display_state_sha256 TEXT NOT NULL, stimulus_context_id TEXT NOT NULL, stimulus_context_sha256 TEXT NOT NULL, observable TEXT NOT NULL, point_count INTEGER NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dvspatial_state ON cad_dv_spatial_measurements(display_state_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dv_angle_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, angle_id TEXT NOT NULL UNIQUE, angle_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_id TEXT NOT NULL, display_state_sha256 TEXT NOT NULL, stimulus_context_id TEXT NOT NULL, stimulus_context_sha256 TEXT NOT NULL, horizontal_angle_deg REAL NOT NULL, vertical_angle_deg REAL NOT NULL, seat_ref TEXT, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dvangle_state ON cad_dv_angle_measurements(display_state_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dv_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_id TEXT NOT NULL, display_state_sha256 TEXT NOT NULL, state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dvqual_state ON cad_dv_qualifications(display_state_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_om_spectral_states ( seq INTEGER PRIMARY KEY AUTOINCREMENT, spectral_state_id TEXT NOT NULL UNIQUE, spectral_state_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_ref TEXT NOT NULL, system_kind TEXT NOT NULL, evidence_class TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_omstate_doc ON cad_om_spectral_states(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_om_observer_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL, revision TEXT, observer_set TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_om_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, reference_state_id TEXT NOT NULL, reference_state_sha256 TEXT NOT NULL, dut_state_id TEXT NOT NULL, dut_state_sha256 TEXT NOT NULL, profile_id TEXT NOT NULL, profile_sha256 TEXT NOT NULL, result_class TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_omeval_doc ON cad_om_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_om_perceptual_matches ( seq INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL UNIQUE, match_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, reference_state_id TEXT NOT NULL, dut_state_id TEXT NOT NULL, observer_identity_class TEXT NOT NULL, observer_count INTEGER NOT NULL, recorded_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ommatch_doc ON cad_om_perceptual_matches(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_om_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, goal TEXT NOT NULL, verdict TEXT NOT NULL, reference_state_id TEXT NOT NULL, dut_state_id TEXT NOT NULL, evaluation_id TEXT, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_omqual_doc ON cad_om_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ve_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, room_ref TEXT, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_veobs_doc ON cad_ve_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ve_geometry_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, geometry_id TEXT NOT NULL UNIQUE, geometry_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, observation_id TEXT, observation_sha256 TEXT, seat_ref TEXT, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vegeo_doc ON cad_ve_geometry_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ve_lighting_scenes ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT NOT NULL UNIQUE, scene_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, bound_observation_id TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vescene_doc ON cad_ve_lighting_scenes(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ve_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, observation_id TEXT NOT NULL, observation_sha256 TEXT NOT NULL, profile_kind TEXT NOT NULL, profile_scope TEXT NOT NULL, state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vequal_doc ON cad_ve_qualifications(document_id, seq ASC)
    """
    ,
    # REV57-AUD: acoustic channel-identity / polarity verification (#621)
    """
    CREATE TABLE IF NOT EXISTS cad_channel_identity_chains ( seq INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT NOT NULL UNIQUE, chain_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, logical_channel TEXT NOT NULL, channel_class TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_chchain_doc ON cad_channel_identity_chains(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_endpoint_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, method TEXT NOT NULL, confidence TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_chiobs_doc ON cad_acoustic_endpoint_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_channel_identity_tests ( seq INTEGER PRIMARY KEY AUTOINCREMENT, test_id TEXT NOT NULL UNIQUE, test_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_ref_id TEXT NOT NULL, stimulus_class TEXT NOT NULL, tested_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_chitest_chain ON cad_channel_identity_tests(chain_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_chitest_doc ON cad_channel_identity_tests(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_polarity_verification_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_ref_id TEXT NOT NULL, physical_wiring_state TEXT NOT NULL, dsp_polarity_state TEXT NOT NULL, acoustic_polarity_state TEXT NOT NULL, policy_acceptance TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_chipol_chain ON cad_polarity_verification_records(chain_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_chipol_doc ON cad_polarity_verification_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_channel_identity_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_ref_id TEXT NOT NULL, logical_channel TEXT NOT NULL, verdict TEXT NOT NULL, reconciliation TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_chieval_chain ON cad_channel_identity_evaluations(chain_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_chieval_doc ON cad_channel_identity_evaluations(document_id, seq ASC)
    """
    ,
    # REV57-AUD: listener-area coverage / acoustic-aim qualification (#634)
    """
    CREATE TABLE IF NOT EXISTS cad_acoustic_aim_states ( seq INTEGER PRIMARY KEY AUTOINCREMENT, aim_id TEXT NOT NULL UNIQUE, aim_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, speaker_entity_id TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_aim_doc ON cad_acoustic_aim_states(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_coverage_listener_areas ( seq INTEGER PRIMARY KEY AUTOINCREMENT, area_id TEXT NOT NULL UNIQUE, area_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, position_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_covarea_doc ON cad_coverage_listener_areas(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_coverage_predictions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, prediction_id TEXT NOT NULL UNIQUE, prediction_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, area_ref_id TEXT NOT NULL, quantity TEXT NOT NULL, summation_model TEXT NOT NULL, path_count INTEGER NOT NULL, predicted_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_covpred_area ON cad_coverage_predictions(area_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_covpred_doc ON cad_coverage_predictions(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_coverage_measurement_sets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, set_id TEXT NOT NULL UNIQUE, set_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, area_ref_id TEXT NOT NULL, quantity TEXT NOT NULL, observation_count INTEGER NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_covmeas_area ON cad_coverage_measurement_sets(area_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_covmeas_doc ON cad_coverage_measurement_sets(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_coverage_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, area_ref_id TEXT NOT NULL, coverage_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_coveval_area ON cad_coverage_qualifications(area_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_coveval_doc ON cad_coverage_qualifications(document_id, seq ASC)
    """
    ,
    # REV57-AUD: installed loudspeaker instance variation (#628)
    """
    CREATE TABLE IF NOT EXISTS cad_instance_acoustic_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, instance_ref_id TEXT, evidence_level TEXT NOT NULL, evidence_source TEXT NOT NULL, measurement_domain TEXT NOT NULL, measured_at_utc TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_instev_instance ON cad_instance_acoustic_evidence(instance_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_instev_doc ON cad_instance_acoustic_evidence(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_model_instance_deltas ( seq INTEGER PRIMARY KEY AUTOINCREMENT, delta_id TEXT NOT NULL UNIQUE, delta_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, reference_evidence_ref_id TEXT NOT NULL, instance_evidence_ref_id TEXT NOT NULL, quantity TEXT NOT NULL, max_delta_db REAL, algorithm TEXT NOT NULL, derived_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_instdelta_inst ON cad_model_instance_deltas(instance_evidence_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_instdelta_doc ON cad_model_instance_deltas(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_matched_set_declarations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, set_id TEXT NOT NULL UNIQUE, set_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, role TEXT NOT NULL, member_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_matchset_doc ON cad_matched_set_declarations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_matched_set_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, set_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_setqual_set ON cad_matched_set_qualifications(set_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_setqual_doc ON cad_matched_set_qualifications(document_id, seq ASC)
    """
    ,
    # REV57-AUD: media-playback capability qualification (#632)
    """
    CREATE TABLE IF NOT EXISTS cad_playback_stack_identities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, stack_id TEXT NOT NULL UNIQUE, stack_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_class TEXT NOT NULL, device_identity TEXT NOT NULL, app_name TEXT, app_version TEXT, os_version TEXT, firmware TEXT, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pbstack_doc ON cad_playback_stack_identities(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_media_profile_requirements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, requirement_id TEXT NOT NULL UNIQUE, requirement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, delivery_class TEXT NOT NULL, video_codec TEXT, audio_codec TEXT, encryption_requirement TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pbmedia_doc ON cad_media_profile_requirements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_capability_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stack_ref_id TEXT NOT NULL, media_ref_id TEXT NOT NULL, capability_class TEXT NOT NULL, evidence_class TEXT NOT NULL, output_state TEXT, failure_attribution TEXT NOT NULL, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pbcap_stack ON cad_playback_capability_records(stack_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pbcap_doc ON cad_playback_capability_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_operation_runs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE, run_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stack_ref_id TEXT NOT NULL, media_ref_id TEXT NOT NULL, scenario TEXT NOT NULL, duration_s REAL, operation_count INTEGER NOT NULL, started_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pbrun_stack ON cad_playback_operation_runs(stack_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pbrun_doc ON cad_playback_operation_runs(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stack_ref_id TEXT NOT NULL, media_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pbqual_stack ON cad_playback_qualifications(stack_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pbqual_doc ON cad_playback_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hvac_ventilation_scenarios ( seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL UNIQUE, scenario_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, operating_state TEXT NOT NULL, required_supply_flow_lps REAL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hvacscn_doc ON cad_hvac_ventilation_scenarios(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hvac_path_declarations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, path_id TEXT NOT NULL UNIQUE, path_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_kind TEXT NOT NULL, serves_room TEXT, flanking_role TEXT NOT NULL, scenario_ref_id TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hvacpath_doc ON cad_hvac_path_declarations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hvac_component_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, component_kind TEXT NOT NULL, method TEXT NOT NULL, airflow_evidence_class TEXT NOT NULL, flow_rate_lps REAL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hvaccomp_doc ON cad_hvac_component_evidence(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hvac_field_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_ref_id TEXT NOT NULL, operating_state TEXT NOT NULL, balancing_state TEXT NOT NULL, room_noise_db REAL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hvacobs_doc ON cad_hvac_field_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hvac_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, airflow_eligibility TEXT NOT NULL, acoustic_state TEXT NOT NULL, flanking_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hvacqual_doc ON cad_hvac_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ref_cal_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_kind TEXT NOT NULL, profile_document TEXT NOT NULL, lifecycle_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_refprof_doc ON cad_ref_cal_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ref_cal_stimuli ( seq INTEGER PRIMARY KEY AUTOINCREMENT, stimulus_id TEXT NOT NULL UNIQUE, stimulus_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_kind TEXT NOT NULL, signal_class TEXT NOT NULL, digital_level_dbfs REAL, device_identity TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_refstim_doc ON cad_ref_cal_stimuli(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ref_cal_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, channel_role TEXT NOT NULL, signal_class TEXT NOT NULL, stimulus_ref_id TEXT, quantity TEXT NOT NULL, measured_spl_db REAL, weighting TEXT, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_refobs_doc ON cad_ref_cal_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ref_cal_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, stimulus_state TEXT NOT NULL, measurement_state TEXT NOT NULL, lfe_state TEXT NOT NULL, alignment_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_refqual_doc ON cad_ref_cal_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_treatment_install_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, spec_id TEXT NOT NULL UNIQUE, spec_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, treatment_class TEXT NOT NULL, acoustic_role TEXT NOT NULL, lab_evidence_class TEXT NOT NULL, product_identity TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_taispec_doc ON cad_treatment_install_specs(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_treatment_asbuilt_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, spec_ref_id TEXT NOT NULL, substituted INTEGER, observed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_taiobs_doc ON cad_treatment_asbuilt_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_treatment_inspections ( seq INTEGER PRIMARY KEY AUTOINCREMENT, inspection_id TEXT NOT NULL UNIQUE, inspection_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, operator TEXT, inspected_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_taiinsp_doc ON cad_treatment_inspections(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_treatment_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, spec_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, prediction_validity TEXT NOT NULL, before_after_result TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_taieval_doc ON cad_treatment_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_tactile_vibration_paths ( seq INTEGER PRIMARY KEY AUTOINCREMENT, path_id TEXT NOT NULL UNIQUE, path_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, label TEXT NOT NULL, seat_ref TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tvpath_doc ON cad_tactile_vibration_paths(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_tactile_vibration_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_ref_id TEXT NOT NULL, quantity TEXT NOT NULL, axis TEXT NOT NULL, contact_point TEXT NOT NULL, occupancy_state TEXT NOT NULL, sensor_evidence_class TEXT NOT NULL, measured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tvmeas_doc ON cad_tactile_vibration_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_tactile_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_kind TEXT NOT NULL, label TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tvprof_doc ON cad_tactile_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_tactile_vibration_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, path_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, transfer_state TEXT NOT NULL, occupancy_state TEXT NOT NULL, timing_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tvqual_doc ON cad_tactile_vibration_qualifications(document_id, seq ASC)
    """
    ,
    # REV57-MOUNT: AV mounting / structural-support authority (#620)
    """
    CREATE TABLE IF NOT EXISTS cad_mount_assemblies ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assembly_id TEXT NOT NULL UNIQUE, assembly_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, equipment_ref_id TEXT, placement_ref_id TEXT, equipment_class TEXT NOT NULL, support_method TEXT NOT NULL, overhead_suspension INTEGER NOT NULL, duty_state TEXT NOT NULL, interference_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntassy_doc ON cad_mount_assemblies(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntassy_equipment ON cad_mount_assemblies(equipment_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mount_load_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assembly_ref_id TEXT NOT NULL, mass_kg REAL, weight_n REAL, duty_state TEXT NOT NULL, source_class TEXT NOT NULL, measured_at_utc TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntload_assy ON cad_mount_load_evidence(assembly_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntload_doc ON cad_mount_load_evidence(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mount_support_elements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, element_id TEXT NOT NULL UNIQUE, element_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assembly_ref_id TEXT NOT NULL, element_class TEXT NOT NULL, geometry_ref_id TEXT, hidden_condition_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntsup_assy ON cad_mount_support_elements(assembly_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntsup_doc ON cad_mount_support_elements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mount_manufacturer_requirements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, requirement_id TEXT NOT NULL UNIQUE, requirement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_ref_id TEXT NOT NULL, secondary_retention TEXT NOT NULL, enclosure_suspension TEXT NOT NULL, vesa_pattern TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntreq_subject ON cad_mount_manufacturer_requirements(subject_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntreq_doc ON cad_mount_manufacturer_requirements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mount_structural_approvals ( seq INTEGER PRIMARY KEY AUTOINCREMENT, approval_id TEXT NOT NULL UNIQUE, approval_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assembly_ref_id TEXT, evidence_class TEXT NOT NULL, approval_scope TEXT NOT NULL, duty_coverage TEXT NOT NULL, standard_ref_id TEXT, jurisdiction TEXT, issued_at_utc TEXT, expires_at_utc TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntappr_assy ON cad_mount_structural_approvals(assembly_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntappr_doc ON cad_mount_structural_approvals(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mount_inspection_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, inspection_id TEXT NOT NULL UNIQUE, inspection_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assembly_ref_id TEXT NOT NULL, inspection_kind TEXT NOT NULL, inspector_class TEXT NOT NULL, findings TEXT NOT NULL, inspected_at_utc TEXT NOT NULL, next_due_at_utc TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntinsp_assy ON cad_mount_inspection_records(assembly_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntinsp_doc ON cad_mount_inspection_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mount_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assembly_ref_id TEXT NOT NULL, support_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntqual_assy ON cad_mount_qualifications(assembly_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mntqual_doc ON cad_mount_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measchain_linearity_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_label TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mclpro_doc ON cad_measchain_linearity_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measchain_overload_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_ref_id TEXT NOT NULL, overload_mechanism TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mclobs_chain ON cad_measchain_overload_observations(chain_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mclobs_doc ON cad_measchain_overload_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measchain_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_ref_id TEXT NOT NULL, state TEXT NOT NULL, requested_class TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mclqual_chain ON cad_measchain_qualifications(chain_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mclqual_doc ON cad_measchain_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_sweep_deconvolution_specs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, spec_id TEXT NOT NULL UNIQUE, spec_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stimulus_ref_id TEXT NOT NULL, sweep_law TEXT NOT NULL, algorithm TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_swspec_doc ON cad_sweep_deconvolution_specs(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_harmonic_impulse_components ( seq INTEGER PRIMARY KEY AUTOINCREMENT, component_id TEXT NOT NULL UNIQUE, component_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, spec_ref_id TEXT NOT NULL, harmonic_order INTEGER NOT NULL, expected_offset_s REAL, overlap_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_harmn_spec ON cad_harmonic_impulse_components(spec_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_harmn_doc ON cad_harmonic_impulse_components(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_recovered_impulse_responses ( seq INTEGER PRIMARY KEY AUTOINCREMENT, ir_id TEXT NOT NULL UNIQUE, ir_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, provenance_class TEXT NOT NULL, spec_ref_id TEXT, raw_capture_ref_id TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_recir_doc ON cad_recovered_impulse_responses(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_linear_ir_capabilities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT NOT NULL UNIQUE, capability_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, ir_ref_id TEXT NOT NULL, contamination_state TEXT NOT NULL, clock_gate TEXT NOT NULL, chain_gate TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lircap_ir ON cad_linear_ir_capabilities(ir_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lircap_doc ON cad_linear_ir_capabilities(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_excitation_source_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_label TEXT NOT NULL, source_type TEXT NOT NULL, measurand_class TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_srcpro_doc ON cad_excitation_source_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_orientation_captures ( seq INTEGER PRIMARY KEY AUTOINCREMENT, capture_id TEXT NOT NULL UNIQUE, capture_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_ref_id TEXT NOT NULL, aggregation_role TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_srcori_source ON cad_source_orientation_captures(source_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_srcori_doc ON cad_source_orientation_captures(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_source_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_ref_id TEXT NOT NULL, strength_g_gate TEXT NOT NULL, level_gate TEXT NOT NULL, sim_comparison TEXT, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_srcqual_source ON cad_measurement_source_qualifications(source_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_srcqual_doc ON cad_measurement_source_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dsp_realization_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, device_identity TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dsppro_doc ON cad_dsp_realization_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dsp_stage_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, stage_id TEXT NOT NULL UNIQUE, stage_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stage_kind TEXT NOT NULL, bank_label TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dspstg_doc ON cad_dsp_stage_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dsp_parameter_mappings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, mapping_id TEXT NOT NULL UNIQUE, mapping_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, bank_label TEXT NOT NULL, state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dspmap_profile ON cad_dsp_parameter_mappings(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dspmap_doc ON cad_dsp_parameter_mappings(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dsp_realization_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, state TEXT NOT NULL, transfer_verification TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dspqual_profile ON cad_dsp_realization_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dspqual_doc ON cad_dsp_realization_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_decay_processing_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_label TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decpro_doc ON cad_decay_processing_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_decay_noise_estimates ( seq INTEGER PRIMARY KEY AUTOINCREMENT, estimate_id TEXT NOT NULL UNIQUE, estimate_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, rir_ref_id TEXT NOT NULL, method TEXT NOT NULL, stationarity TEXT NOT NULL, level_db REAL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decnse_rir ON cad_decay_noise_estimates(rir_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decnse_doc ON cad_decay_noise_estimates(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_decay_truncation_decisions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE, decision_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, rir_ref_id TEXT NOT NULL, truncation_time_s REAL NOT NULL, reason TEXT NOT NULL, capture_truncated INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dectrn_rir ON cad_decay_truncation_decisions(rir_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dectrn_doc ON cad_decay_truncation_decisions(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_decay_edc_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, artifact_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, rir_ref_id TEXT NOT NULL, edc_kind TEXT NOT NULL, content_sha256 TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decedc_rir ON cad_decay_edc_artifacts(rir_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decedc_doc ON cad_decay_edc_artifacts(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_decay_fit_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, rir_ref_id TEXT NOT NULL, metric TEXT NOT NULL, value_s REAL, eligibility TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decfit_rir ON cad_decay_fit_records(rir_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_decfit_doc ON cad_decay_fit_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_boundary_evidence_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, evidence_label TEXT NOT NULL, boundary_class TEXT NOT NULL, passivity_class TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bdevi_doc ON cad_boundary_evidence_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_boundary_rational_fits ( seq INTEGER PRIMARY KEY AUTOINCREMENT, fit_id TEXT NOT NULL UNIQUE, fit_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, input_evidence_ref_id TEXT NOT NULL, fit_variable TEXT NOT NULL, pole_count INTEGER NOT NULL, algorithm TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bdrat_evi ON cad_boundary_rational_fits(input_evidence_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bdrat_doc ON cad_boundary_rational_fits(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_td_impedance_realizations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, realization_id TEXT NOT NULL UNIQUE, realization_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, evidence_ref_id TEXT NOT NULL, solver_family TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bdtim_evi ON cad_td_impedance_realizations(evidence_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bdtim_doc ON cad_td_impedance_realizations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_boundary_realizability_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, evidence_ref_id TEXT NOT NULL, state TEXT NOT NULL, passivity_class TEXT NOT NULL, causality_state TEXT NOT NULL, stability_state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bdass_evi ON cad_boundary_realizability_assessments(evidence_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bdass_doc ON cad_boundary_realizability_assessments(document_id, seq ASC)
    """
    ,
    # REV58-NUMERIC: wave-solver numerical-fidelity authority (#683)
    """
    CREATE TABLE IF NOT EXISTS cad_wave_fidelity_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, solver_family TEXT NOT NULL, solver_result_ref_id TEXT, mesh_identity TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wnfprof_doc ON cad_wave_fidelity_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wnfprof_result ON cad_wave_fidelity_profiles(solver_result_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wave_convergence_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, convergence_id TEXT NOT NULL UNIQUE, convergence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, study_kind TEXT NOT NULL, level_count INTEGER NOT NULL, fixture_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wnvconv_prof ON cad_wave_convergence_records(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wnvconv_doc ON cad_wave_convergence_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wave_fidelity_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, fidelity_state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wnfqual_prof ON cad_wave_fidelity_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wnfqual_doc ON cad_wave_fidelity_qualifications(document_id, seq ASC)
    """
    ,
    # REV58-NUMERIC: geometrical-acoustics numerical-fidelity authority (#685)
    """
    CREATE TABLE IF NOT EXISTS cad_geometric_fidelity_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, algorithm_family TEXT NOT NULL, solver_result_ref_id TEXT, receiver_model TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gnfprof_doc ON cad_geometric_fidelity_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gnfprof_result ON cad_geometric_fidelity_profiles(solver_result_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ray_sampling_convergences ( seq INTEGER PRIMARY KEY AUTOINCREMENT, convergence_id TEXT NOT NULL UNIQUE, convergence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, evidence_count INTEGER NOT NULL, fixture_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_raysconv_prof ON cad_ray_sampling_convergences(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_raysconv_doc ON cad_ray_sampling_convergences(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_path_enumeration_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, deterministic_state TEXT NOT NULL, named_path_evidence_class TEXT NOT NULL, max_qualified_order INTEGER, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pathqual_prof ON cad_path_enumeration_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pathqual_doc ON cad_path_enumeration_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_geometric_fidelity_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, fidelity_state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gnfqual_prof ON cad_geometric_fidelity_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gnfqual_doc ON cad_geometric_fidelity_qualifications(document_id, seq ASC)
    """
    ,
    # REV58-NUMERIC: wave↔geometrical hybrid-handoff authority (#687)
    """
    CREATE TABLE IF NOT EXISTS cad_hybrid_composition_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, wave_prediction_ref_id TEXT NOT NULL, ga_prediction_ref_id TEXT NOT NULL, transition_kind TEXT NOT NULL, output_capability TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hybprof_doc ON cad_hybrid_composition_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hybprof_wave ON cad_hybrid_composition_profiles(wave_prediction_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hybprof_ga ON cad_hybrid_composition_profiles(ga_prediction_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_hybrid_transition_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, handoff_state TEXT NOT NULL, gap_low_hz REAL, gap_high_hz REAL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hybqual_prof ON cad_hybrid_transition_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_hybqual_doc ON cad_hybrid_transition_qualifications(document_id, seq ASC)
    """
    ,
    # REV58-AUDIOMODEL: acoustic-model authority tables
    # (#654/#655/#656/#690/#684/#681)
    """
    CREATE TABLE IF NOT EXISTS cad_source_origin_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_ref_id TEXT NOT NULL, capability TEXT NOT NULL, boundary_state TEXT NOT NULL, estimate_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sorprof_doc ON cad_source_origin_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sorprof_src ON cad_source_origin_profiles(source_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_origin_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, requested_capability TEXT NOT NULL, effective_origin_kind TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sorqual_prof ON cad_source_origin_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sorqual_doc ON cad_source_origin_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_field_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_ref_id TEXT NOT NULL, mic_distance_m REAL NOT NULL, environment TEXT NOT NULL, default_source_model TEXT NOT NULL, band_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sfldprof_doc ON cad_source_field_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sfldprof_src ON cad_source_field_profiles(source_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_field_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, requested_distance_m REAL, effective_regime TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sfldqual_prof ON cad_source_field_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sfldqual_doc ON cad_source_field_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_directivity_sampling_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, dataset_ref_id TEXT NOT NULL, coverage_class TEXT NOT NULL, measured_direction_count INTEGER NOT NULL, dataset_kind TEXT NOT NULL, has_interpolation INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_drsprof_doc ON cad_directivity_sampling_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_drsprof_ds ON cad_directivity_sampling_profiles(dataset_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_directivity_interpolation_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, method TEXT NOT NULL, domain TEXT NOT NULL, output_step_deg REAL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dinterp_prof ON cad_directivity_interpolation_records(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dinterp_doc ON cad_directivity_interpolation_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_directivity_direction_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, azimuth_deg REAL NOT NULL, elevation_deg REAL NOT NULL, frequency_hz REAL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_drqual_prof ON cad_directivity_direction_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_drqual_doc ON cad_directivity_direction_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_coherence_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, group_label TEXT NOT NULL, member_count INTEGER NOT NULL, relation_count INTEGER NOT NULL, default_relation TEXT NOT NULL, declared_combination_mode TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mscprof_doc ON cad_source_coherence_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_source_combination_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, requested_mode TEXT NOT NULL, effective_mode TEXT, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mscqual_prof ON cad_source_combination_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mscqual_doc ON cad_source_combination_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_scattering_model_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, solver_model TEXT NOT NULL, implementation TEXT NOT NULL, directional_redirection INTEGER NOT NULL, incidence_domain TEXT NOT NULL, early_late_applicability TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_scatprof_doc ON cad_scattering_model_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_scattering_model_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, effective_model TEXT, requires_redirection INTEGER NOT NULL, reflection_order TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_scatqual_prof ON cad_scattering_model_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_scatqual_doc ON cad_scattering_model_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_diffraction_model_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, model_family TEXT NOT NULL, implementation TEXT NOT NULL, domain TEXT NOT NULL, edge_kind TEXT NOT NULL, wedge_boundary TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_edfprof_doc ON cad_diffraction_model_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_diffraction_benchmark_results ( seq INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL UNIQUE, result_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, fixture_id TEXT NOT NULL, fixture_kind TEXT NOT NULL, reference_class TEXT NOT NULL, result TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_difbench_prof ON cad_diffraction_benchmark_results(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_difbench_doc ON cad_diffraction_benchmark_results(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_diffraction_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, capability TEXT NOT NULL, boundary_limited INTEGER NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_difqual_prof ON cad_diffraction_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_difqual_doc ON cad_diffraction_qualifications(document_id, seq ASC)
    """
    ,
    # REV58-IDENT: typed logarithmic quantity / dB-reference authority (#691)
    """
    CREATE TABLE IF NOT EXISTS cad_log_quantities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, quantity_id TEXT NOT NULL UNIQUE, quantity_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, quantity_class TEXT NOT NULL, domain TEXT NOT NULL, quantity TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_logqty_doc ON cad_log_quantities(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_log_calibration_bridges ( seq INTEGER PRIMARY KEY AUTOINCREMENT, bridge_id TEXT NOT NULL UNIQUE, bridge_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, bridge_label TEXT NOT NULL, from_domain TEXT NOT NULL, to_domain TEXT NOT NULL, status TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_logbrg_doc ON cad_log_calibration_bridges(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_log_operations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE, operation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, operation TEXT NOT NULL, state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_logop_doc ON cad_log_operations(document_id, seq ASC)
    """
    ,
    # REV58-IDENT: calibration-parameter identifiability authority (#689)
    """
    CREATE TABLE IF NOT EXISTS cad_calib_parameter_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, parameter_id TEXT NOT NULL UNIQUE, parameter_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, parameter_label TEXT NOT NULL, role TEXT NOT NULL, provenance TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_calprm_doc ON cad_calib_parameter_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ident_sensitivity_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, sensitivity_id TEXT NOT NULL UNIQUE, sensitivity_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, method TEXT NOT NULL, calibration_run_ref_id TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_idsens_doc ON cad_ident_sensitivity_evidence(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_idsens_run ON cad_ident_sensitivity_evidence(calibration_run_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ident_correlation_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT NOT NULL UNIQUE, correlation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, method TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_idcorr_doc ON cad_ident_correlation_evidence(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ident_equivalent_sets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, set_id TEXT NOT NULL UNIQUE, set_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, member_count INTEGER NOT NULL, multimodal INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ideqset_doc ON cad_ident_equivalent_sets(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_identifiability_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, parameter_ref_id TEXT NOT NULL, identifiability_class TEXT NOT NULL, parameter_claim TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_idassess_doc ON cad_identifiability_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_idassess_param ON cad_identifiability_assessments(parameter_ref_id, seq ASC)
    """
    ,
    # REV58-IDENT: validation sample-dependence / benchmark-leakage authority (#698)
    """
    CREATE TABLE IF NOT EXISTS cad_validation_statistical_designs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, design_id TEXT NOT NULL UNIQUE, design_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, design_label TEXT NOT NULL, generalization_claim TEXT NOT NULL, independent_unit TEXT NOT NULL, independent_unit_count INTEGER, raw_observation_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vsdes_doc ON cad_validation_statistical_designs(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dependence_models ( seq INTEGER PRIMARY KEY AUTOINCREMENT, dependence_id TEXT NOT NULL UNIQUE, dependence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, design_ref_id TEXT, spatial_correlation_model TEXT NOT NULL, resampling_unit TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vsdep_doc ON cad_dependence_models(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dataset_role_assignments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL UNIQUE, assignment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, corpus_ref_id TEXT NOT NULL, role TEXT NOT NULL, context_label TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vsrole_doc ON cad_dataset_role_assignments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vsrole_corpus ON cad_dataset_role_assignments(corpus_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_benchmark_exposures ( seq INTEGER PRIMARY KEY AUTOINCREMENT, exposure_id TEXT NOT NULL UNIQUE, exposure_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, corpus_ref_id TEXT NOT NULL, decision_class TEXT NOT NULL, solver_version TEXT, exposed_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vsexp_doc ON cad_benchmark_exposures(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vsexp_corpus ON cad_benchmark_exposures(corpus_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_challenge_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, design_ref_id TEXT NOT NULL, state TEXT NOT NULL, independent_unit TEXT NOT NULL, independent_unit_count INTEGER, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vsqual_doc ON cad_challenge_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_vsqual_design ON cad_challenge_qualifications(design_ref_id, seq ASC)
    """
    ,
    # REV58-VALIDMETH: #675 optimizer algorithm qualification —
    # problems, run profiles, qualifications, Pareto assessments.
    """
    CREATE TABLE IF NOT EXISTS cad_optimization_problems ( seq INTEGER PRIMARY KEY AUTOINCREMENT, problem_id TEXT NOT NULL UNIQUE, problem_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, problem_label TEXT NOT NULL, objective_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_optimizer_run_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, problem_ref_id TEXT NOT NULL, algorithm_family TEXT NOT NULL, run_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_optimizer_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, state TEXT NOT NULL, optimality_claim TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pareto_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, state TEXT NOT NULL, reference_status TEXT NOT NULL, nondominated_count INTEGER, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_optprob_doc ON cad_optimization_problems(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_optprof_doc ON cad_optimizer_run_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_optprof_problem ON cad_optimizer_run_profiles(problem_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_optqual_doc ON cad_optimizer_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_optqual_profile ON cad_optimizer_qualifications(profile_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_parassess_doc ON cad_pareto_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_parassess_profile ON cad_pareto_assessments(profile_ref_id, seq ASC)
    """
    ,
    # REV58-VALIDMETH: #674 acoustic eigenmode / mode-shape validation
    # — mode pairings, validation verdicts.
    """
    CREATE TABLE IF NOT EXISTS cad_mode_pairings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, pairing_id TEXT NOT NULL UNIQUE, pairing_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, pairing_state TEXT NOT NULL, pairing_algorithm TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_eigenmode_verdicts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verdict_id TEXT NOT NULL UNIQUE, verdict_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, pairing_ref_id TEXT NOT NULL, state TEXT NOT NULL, frequency_agreement TEXT NOT NULL, shape_agreement TEXT NOT NULL, damping_agreement TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_modpair_doc ON cad_mode_pairings(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_eigverd_doc ON cad_eigenmode_verdicts(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_eigverd_pairing ON cad_eigenmode_verdicts(pairing_ref_id, seq ASC)
    """
    ,
    # REV58-VALIDMETH: #673 sound-field diffuseness / statistical-model
    # applicability — assessments, declarations.
    """
    CREATE TABLE IF NOT EXISTS cad_diffuseness_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, eligibility_state TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_statistical_applicability_declarations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, declaration_id TEXT NOT NULL UNIQUE, declaration_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assessment_ref_id TEXT, state TEXT NOT NULL, basis TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dffassess_doc ON cad_diffuseness_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dffdec_doc ON cad_statistical_applicability_declarations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dffdec_assess ON cad_statistical_applicability_declarations(assessment_ref_id, seq ASC)
    """
    ,
    # REV58-VALIDMETH: #671 coupled-room multi-slope decay —
    # multi-slope fits, single-slope adequacy gates, qualifications.
    """
    CREATE TABLE IF NOT EXISTS cad_multi_slope_fits ( seq INTEGER PRIMARY KEY AUTOINCREMENT, fit_id TEXT NOT NULL UNIQUE, fit_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, raw_evidence_ref_id TEXT NOT NULL, model_class TEXT NOT NULL, component_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_single_slope_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, raw_evidence_ref_id TEXT NOT NULL, adequacy_state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_coupled_decay_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, behavior_state TEXT NOT NULL, state TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cplfit_doc ON cad_multi_slope_fits(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cplfit_evidence ON cad_multi_slope_fits(raw_evidence_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cplgate_doc ON cad_single_slope_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cplgate_evidence ON cad_single_slope_assessments(raw_evidence_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cplqual_doc ON cad_coupled_decay_qualifications(document_id, seq ASC)
    """
    ,
    # REV58-VALIDMETH: #677 predicted↔measured early-reflection
    # correspondence — pairings, sets, verdicts.
    """
    CREATE TABLE IF NOT EXISTS cad_reflection_pairings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, pairing_id TEXT NOT NULL UNIQUE, pairing_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, correspondence_state TEXT NOT NULL, matching_algorithm TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_reflection_correspondence_sets ( seq INTEGER PRIMARY KEY AUTOINCREMENT, set_id TEXT NOT NULL UNIQUE, set_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, registration_ref_id TEXT NOT NULL, pairing_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_reflection_correspondence_verdicts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verdict_id TEXT NOT NULL UNIQUE, verdict_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, set_ref_id TEXT NOT NULL, state TEXT NOT NULL, matched_pair_count INTEGER NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rfxpair_doc ON cad_reflection_pairings(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rfxset_doc ON cad_reflection_correspondence_sets(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rfxverd_doc ON cad_reflection_correspondence_verdicts(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rfxverd_set ON cad_reflection_correspondence_verdicts(set_ref_id, seq ASC)
    """
    ,
    # REV58-VALIDMETH: #706 time-frequency modal-decay authority —
    # observations, qualifications.
    """
    CREATE TABLE IF NOT EXISTS cad_modal_decay_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, raw_evidence_ref_id TEXT NOT NULL, overlap_state TEXT NOT NULL, fit_model TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_modal_decay_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, observation_ref_id TEXT NOT NULL, state TEXT NOT NULL, decay_trustworthy INTEGER NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mdtobs_doc ON cad_modal_decay_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mdtobs_evidence ON cad_modal_decay_observations(raw_evidence_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mdtqual_doc ON cad_modal_decay_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mdtqual_obs ON cad_modal_decay_qualifications(observation_ref_id, seq ASC)
    """
    ,
    # REV59-DEPS: #729 authority dependency / staleness graph —
    # typed edge declarations, semantic change events, invalidation
    # rulesets, staleness assessments, revalidation plans.
    """
    CREATE TABLE IF NOT EXISTS cad_dependency_edge_declarations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, edge_id TEXT NOT NULL UNIQUE, edge_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, subject_ref_id TEXT NOT NULL, kind TEXT NOT NULL, target_ref_id TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dependency_change_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, event_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, changed_ref_id TEXT NOT NULL, change_class TEXT NOT NULL, occurred_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dependency_rule_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, ruleset_version TEXT NOT NULL, entry_count INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_staleness_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, change_event_ref_id TEXT NOT NULL, entry_count INTEGER NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_revalidation_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, assessment_ref_id TEXT NOT NULL, action_count INTEGER NOT NULL, planned_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_depedge_doc ON cad_dependency_edge_declarations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_depedge_subject ON cad_dependency_edge_declarations(subject_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_depedge_target ON cad_dependency_edge_declarations(target_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_depevt_doc ON cad_dependency_change_events(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_depevt_changed ON cad_dependency_change_events(changed_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_deprule_doc ON cad_dependency_rule_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_staleassess_doc ON cad_staleness_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_staleassess_event ON cad_staleness_assessments(change_event_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_revplan_doc ON cad_revalidation_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_revplan_assess ON cad_revalidation_plans(assessment_ref_id, seq ASC)
    """
    ,
    # REV59-DEPS: #725 evidence attestation / trusted timestamp
    # authority — signed manifests, attestations, verifications.
    """
    CREATE TABLE IF NOT EXISTS cad_signed_manifests ( seq INTEGER PRIMARY KEY AUTOINCREMENT, manifest_id TEXT NOT NULL UNIQUE, manifest_record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, manifest_label TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, approval_scope TEXT NOT NULL, created_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_manifest_attestations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, attestation_id TEXT NOT NULL UNIQUE, attestation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, manifest_ref_id TEXT NOT NULL, kind TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_attestation_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT NOT NULL UNIQUE, verification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, attestation_ref_id TEXT NOT NULL, manifest_ref_id TEXT NOT NULL, state TEXT NOT NULL, time_authority TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sigman_doc ON cad_signed_manifests(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_evatt_doc ON cad_manifest_attestations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_evatt_manifest ON cad_manifest_attestations(manifest_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_attver_doc ON cad_attestation_verifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_attver_att ON cad_attestation_verifications(attestation_ref_id, seq ASC)
    """
    ,
    # REV59-DEPS: #718 project archival / schema-migration authority —
    # archive snapshots, archive verifications, migration records,
    # migration verifications.
    """
    CREATE TABLE IF NOT EXISTS cad_archive_snapshots ( seq INTEGER PRIMARY KEY AUTOINCREMENT, archive_id TEXT NOT NULL UNIQUE, archive_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, archive_label TEXT NOT NULL, schema_version TEXT NOT NULL, content_hash TEXT NOT NULL, preservation_scope TEXT NOT NULL, captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_archive_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT NOT NULL UNIQUE, verification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, archive_ref_id TEXT NOT NULL, status TEXT NOT NULL, check_count INTEGER NOT NULL, verified_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_migration_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, migration_id TEXT NOT NULL UNIQUE, migration_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, kind TEXT NOT NULL, source_archive_ref_id TEXT NOT NULL, target_archive_ref_id TEXT NOT NULL, from_schema_version TEXT NOT NULL, to_schema_version TEXT NOT NULL, migration_status_at_write TEXT NOT NULL, migrated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_migration_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT NOT NULL UNIQUE, verification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, migration_ref_id TEXT NOT NULL, status TEXT NOT NULL, check_count INTEGER NOT NULL, verified_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_arcsnap_doc ON cad_archive_snapshots(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_arcver_doc ON cad_archive_verifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_arcver_archive ON cad_archive_verifications(archive_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_migrec_doc ON cad_migration_records(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_migrec_target ON cad_migration_records(target_archive_ref_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_migver_doc ON cad_migration_verifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_migver_mig ON cad_migration_verifications(migration_ref_id, seq ASC)
    """
    ,
    # REV58-DISPLAYMEAS: #682 / #680 / #686 / #647 / #666.
    """
    CREATE TABLE IF NOT EXISTS cad_pg_generator_instances ( seq INTEGER PRIMARY KEY AUTOINCREMENT, generator_id TEXT NOT NULL UNIQUE, generator_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, generator_class TEXT NOT NULL, manufacturer TEXT NOT NULL, model TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pggen_doc ON cad_pg_generator_instances(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pg_requested_patches ( seq INTEGER PRIMARY KEY AUTOINCREMENT, patch_id TEXT NOT NULL UNIQUE, patch_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stimulus_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pgpatch_doc ON cad_pg_requested_patches(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pg_delivered_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, patch_ref_id TEXT NOT NULL, generator_ref_id TEXT NOT NULL, observation_point TEXT NOT NULL, verification TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pgobs_doc ON cad_pg_delivered_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pgobs_patch ON cad_pg_delivered_observations(patch_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_pg_fidelity_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, generator_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pgqual_doc ON cad_pg_fidelity_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mm_match_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL UNIQUE, match_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, target_serial TEXT NOT NULL, reference_serial TEXT NOT NULL, display_instance TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mmprof_doc ON cad_mm_match_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mm_match_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, match_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mmobs_doc ON cad_mm_match_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mm_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT NOT NULL UNIQUE, verification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, match_ref_id TEXT NOT NULL, passed INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mmver_doc ON cad_mm_verifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mm_applicability ( seq INTEGER PRIMARY KEY AUTOINCREMENT, applicability_id TEXT NOT NULL UNIQUE, applicability_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, match_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mmappl_doc ON cad_mm_applicability(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_da_additivity_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_daobs_doc ON cad_da_additivity_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_da_separation_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dasep_doc ON cad_da_separation_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_da_volumetric_characterisations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, characterisation_id TEXT NOT NULL UNIQUE, characterisation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_ref_id TEXT NOT NULL, grid_size INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_davol_doc ON cad_da_volumetric_characterisations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_da_holdout_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT NOT NULL UNIQUE, verification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_ref_id TEXT NOT NULL, model_family TEXT NOT NULL, passed INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dahold_doc ON cad_da_holdout_verifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_da_model_eligibility ( seq INTEGER PRIMARY KEY AUTOINCREMENT, eligibility_id TEXT NOT NULL UNIQUE, eligibility_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_ref_id TEXT NOT NULL, model_family TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_daelig_doc ON cad_da_model_eligibility(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_da_characterisation_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_ref_id TEXT NOT NULL, required_capability TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_daplan_doc ON cad_da_characterisation_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_td_states ( seq INTEGER PRIMARY KEY AUTOINCREMENT, state_id TEXT NOT NULL UNIQUE, state_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, display_state_ref_id TEXT NOT NULL, input_frame_rate_hz REAL NOT NULL, refresh_rate_hz REAL NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tdstate_doc ON cad_td_states(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_td_step_responses ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, state_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tdstep_doc ON cad_td_step_responses(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_td_motion_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, state_ref_id TEXT NOT NULL, mechanism TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tdmot_doc ON cad_td_motion_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_td_flicker_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, state_ref_id TEXT NOT NULL, method TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tdflick_doc ON cad_td_flicker_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_td_retention_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, state_ref_id TEXT NOT NULL, persistence TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tdret_doc ON cad_td_retention_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_td_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, state_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tdq_doc ON cad_td_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lut_artifacts ( seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL UNIQUE, artifact_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lutart_doc ON cad_lut_artifacts(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lut_generation_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, generation_id TEXT NOT NULL UNIQUE, generation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, artifact_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lutgen_doc ON cad_lut_generation_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lut_preflight_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, preflight_id TEXT NOT NULL UNIQUE, preflight_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, artifact_ref_id TEXT NOT NULL, numeric_validation_passed INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lutpre_doc ON cad_lut_preflight_verifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lut_deployments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id TEXT NOT NULL UNIQUE, deployment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, artifact_ref_id TEXT NOT NULL, device_instance TEXT NOT NULL, slot TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lutdep_doc ON cad_lut_deployments(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lut_post_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, post_verification_id TEXT NOT NULL UNIQUE, post_verification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, deployment_ref_id TEXT NOT NULL, passed INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lutpost_doc ON cad_lut_post_verifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lut_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, artifact_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lutq_doc ON cad_lut_qualifications(document_id, seq ASC)
    """
    ,
    # REV58-MEASELEC: #699 / #651 / #649 / #665 / #693.
    """
    CREATE TABLE IF NOT EXISTS cad_interface_loopback_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, loopback_path_kind TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ifcobs_doc ON cad_interface_loopback_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_interface_transfer_calibrations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, calibration_id TEXT NOT NULL UNIQUE, calibration_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, calibration_kind TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ifccal_doc ON cad_interface_transfer_calibrations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_interface_correction_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, calibration_ref_id TEXT, state TEXT NOT NULL, sample_rate_applicability TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ifcqual_doc ON cad_interface_correction_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ifcqual_cal ON cad_interface_correction_qualifications(calibration_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_signal_level_references ( seq INTEGER PRIMARY KEY AUTOINCREMENT, reference_id TEXT NOT NULL UNIQUE, reference_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stage_label TEXT NOT NULL, analog_unit TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lvlref_doc ON cad_signal_level_references(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_noise_floor_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stage_label TEXT, noise_class TEXT NOT NULL, noise_level REAL NOT NULL, noise_unit TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gnobs_doc ON cad_noise_floor_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_clipping_margins ( seq INTEGER PRIMARY KEY AUTOINCREMENT, margin_id TEXT NOT NULL UNIQUE, margin_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stage_label TEXT NOT NULL, clip_mechanism TEXT NOT NULL, load_stress TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_clipm_doc ON cad_clipping_margins(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_gain_structure_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, use_case TEXT NOT NULL, state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gnqual_doc ON cad_gain_structure_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_dynamics_states ( seq INTEGER PRIMARY KEY AUTOINCREMENT, state_id TEXT NOT NULL UNIQUE, state_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, device TEXT, output_mode TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dynstate_doc ON cad_playback_dynamics_states(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_level_sweep_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stimulus_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dynobs_doc ON cad_level_sweep_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_dynamics_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, dynamics_state_ref_id TEXT, purpose TEXT NOT NULL, state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dynqual_doc ON cad_playback_dynamics_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dynqual_state ON cad_playback_dynamics_qualifications(dynamics_state_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_multiway_speaker_definitions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, definition_id TEXT NOT NULL UNIQUE, definition_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, speaker_instance TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_axospk_doc ON cad_multiway_speaker_definitions(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_active_crossover_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, speaker_ref_id TEXT NOT NULL, dsp_device TEXT, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_axoplan_doc ON cad_active_crossover_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_axoplan_spk ON cad_active_crossover_plans(speaker_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_driver_alignment_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, speaker_ref_id TEXT NOT NULL, way_label TEXT NOT NULL, acoustic_polarity TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_axomeas_doc ON cad_driver_alignment_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_axomeas_spk ON cad_driver_alignment_measurements(speaker_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_active_crossover_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, speaker_ref_id TEXT NOT NULL, state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_axoqual_doc ON cad_active_crossover_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_axoqual_spk ON cad_active_crossover_qualifications(speaker_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_method_procedures ( seq INTEGER PRIMARY KEY AUTOINCREMENT, procedure_id TEXT NOT NULL UNIQUE, procedure_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, method_name TEXT NOT NULL, procedure_version TEXT NOT NULL, documented INTEGER NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_repproc_doc ON cad_method_procedures(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_reproducibility_campaigns ( seq INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL UNIQUE, campaign_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, procedure_ref_id TEXT NOT NULL, design_class TEXT NOT NULL, evidence_tier TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_repcamp_doc ON cad_reproducibility_campaigns(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_repcamp_proc ON cad_reproducibility_campaigns(procedure_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_method_precision_models ( seq INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL UNIQUE, model_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, campaign_ref_id TEXT NOT NULL, declared_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_repmod_doc ON cad_method_precision_models(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_repmod_camp ON cad_method_precision_models(campaign_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_reproducibility_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, procedure_ref_id TEXT NOT NULL, evidence_tier TEXT NOT NULL, state TEXT NOT NULL, evaluation_version TEXT NOT NULL, evaluated_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_repqual_doc ON cad_reproducibility_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_repqual_proc ON cad_reproducibility_qualifications(procedure_ref_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_apply_capability_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, device_ref_id TEXT NOT NULL, capability_evidence TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_apcap_doc ON cad_apply_capability_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_apply_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, device_ref_id TEXT NOT NULL, capability_ref_id TEXT NOT NULL, pre_state_evidence TEXT NOT NULL, rollback_strategy TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_applan_doc ON cad_apply_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_apply_write_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, write_id TEXT NOT NULL UNIQUE, write_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, sequence_index INTEGER NOT NULL, write_kind TEXT NOT NULL, outcome TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_apwrite_doc ON cad_apply_write_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_apply_verifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT NOT NULL UNIQUE, verification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, readback_means TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_apver_doc ON cad_apply_verifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_apply_rollback_plans ( seq INTEGER PRIMARY KEY AUTOINCREMENT, rollback_plan_id TEXT NOT NULL UNIQUE, rollback_plan_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, pre_state_evidence TEXT NOT NULL, claim TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rbplan_doc ON cad_apply_rollback_plans(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_apply_rollback_executions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL UNIQUE, execution_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, rollback_plan_ref_id TEXT NOT NULL, outcome TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rbexec_doc ON cad_apply_rollback_executions(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_apply_transactions ( seq INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL UNIQUE, transaction_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, plan_ref_id TEXT NOT NULL, capability_ref_id TEXT NOT NULL, state_verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_aptxn_doc ON cad_apply_transactions(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_fractional_octave_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, band_kind TEXT NOT NULL, frequency_standard TEXT NOT NULL, filter_class TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_foctp_doc ON cad_fractional_octave_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_band_integrations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_bandi_doc ON cad_band_integrations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_echo_density_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, estimator_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_edp_doc ON cad_echo_density_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_mixing_time_estimates ( seq INTEGER PRIMARY KEY AUTOINCREMENT, estimate_id TEXT NOT NULL UNIQUE, estimate_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, basis TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_mixt_doc ON cad_mixing_time_estimates(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_late_field_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lft_doc ON cad_late_field_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_interpolation_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, method TEXT NOT NULL, quantity TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_fint_doc ON cad_interpolation_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_field_surface_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_fsurf_doc ON cad_field_surface_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_solver_budget_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cbp_doc ON cad_solver_budget_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_compute_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cobs_doc ON cad_compute_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_accuracy_cost_envelopes ( seq INTEGER PRIMARY KEY AUTOINCREMENT, envelope_id TEXT NOT NULL UNIQUE, envelope_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cenv_doc ON cad_accuracy_cost_envelopes(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_jitter_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, spectrum_capable INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_jmp_doc ON cad_jitter_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_jitter_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, jitter_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_job_doc ON cad_jitter_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_jitter_transfer_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_jtf_doc ON cad_jitter_transfer_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_converter_jitter_susceptibility ( seq INTEGER PRIMARY KEY AUTOINCREMENT, susceptibility_id TEXT NOT NULL UNIQUE, susceptibility_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, converter_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cjs_doc ON cad_converter_jitter_susceptibility(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dither_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, dither_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dns_doc ON cad_dither_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_digital_path_transforms ( seq INTEGER PRIMARY KEY AUTOINCREMENT, transform_id TEXT NOT NULL UNIQUE, transform_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, transform_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dpt_doc ON cad_digital_path_transforms(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_playback_src_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, algorithm TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_srcp_doc ON cad_playback_src_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_src_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, src_profile_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_srcq_doc ON cad_src_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_clock_domain_crossings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, crossing_id TEXT NOT NULL UNIQUE, crossing_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, declared_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cdc_doc ON cad_clock_domain_crossings(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_interchannel_leakage_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stage TEXT NOT NULL, method TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_xtk_doc ON cad_interchannel_leakage_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_channel_separation_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stage TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_csep_doc ON cad_channel_separation_qualifications(document_id, seq ASC)
    """
    ,
"""
    CREATE TABLE IF NOT EXISTS cad_projector_light_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pdl_doc ON cad_projector_light_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_temporal_contrast_measures ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurand TEXT NOT NULL, light_mode TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tcnt_doc ON cad_temporal_contrast_measures(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dynamic_contrast_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, measurement_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dcq_doc ON cad_dynamic_contrast_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_light_measurement_capabilities ( seq INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT NOT NULL UNIQUE, capability_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, stray_light_control TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lmc_doc ON cad_light_measurement_capabilities(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_low_luminance_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, capability_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_llo_doc ON cad_low_luminance_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_display_boundary_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, transmission TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dab_doc ON cad_display_boundary_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_front_stage_variants ( seq INTEGER PRIMARY KEY AUTOINCREMENT, variant_id TEXT NOT NULL UNIQUE, variant_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, boundary_ref_id TEXT NOT NULL, strategy TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_fsv_doc ON cad_front_stage_variants(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_codec_chain_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, media_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cfp_doc ON cad_codec_chain_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_quality_method_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, method_id TEXT NOT NULL UNIQUE, method_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, method_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_qmp_doc ON cad_quality_method_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_codec_fidelity_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_ref_id TEXT NOT NULL, method_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cfo_doc ON cad_codec_fidelity_observations(document_id, seq ASC)
    """
    ,
    # REV59-POWEREV: #736 power sequencing, #738 AC power quality,
    # #752 EMC evidence — seven append-only authorities.
    """
    CREATE TABLE IF NOT EXISTS cad_power_sequencing_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, direction TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_psq_doc ON cad_power_sequencing_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_power_sequence_events ( seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, event_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_psev_doc ON cad_power_sequence_events(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ups_transition_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, ups_device_id TEXT NOT NULL, transfer_observed INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_upst_doc ON cad_ups_transition_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_power_quality_measurements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT NOT NULL UNIQUE, measurement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, circuit_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pqm_doc ON cad_power_quality_measurements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_power_quality_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, circuit_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pqq_doc ON cad_power_quality_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_emc_product_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_emc_doc ON cad_emc_product_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_emc_symptom_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_emcs_doc ON cad_emc_symptom_records(document_id, seq ASC)
    """
    ,
    # REV59-BUILDENV: #751 product safety, #740 occupied IAQ,
    # #750 VOC emissions — four append-only authorities.
    """
    CREATE TABLE IF NOT EXISTS cad_product_safety_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, listing_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_psf_doc ON cad_product_safety_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_occupied_iaq_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, room_id TEXT NOT NULL, occupied INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_iaq_doc ON cad_occupied_iaq_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_occupied_iaq_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, room_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_iaqq_doc ON cad_occupied_iaq_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_voc_emission_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_voc_doc ON cad_voc_emission_profiles(document_id, seq ASC)
    """
    ,
    # REV59-ACOUST2: #743 fixture scattering, #749 spectral estimator,
    # #765 evidence supersession — six append-only authorities.
    """
    CREATE TABLE IF NOT EXISTS cad_measurement_fixture_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_fxp_doc ON cad_measurement_fixture_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_fixture_scattering_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, fixture_ref_id TEXT NOT NULL, contamination_detected INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_fxo_doc ON cad_fixture_scattering_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spectral_estimator_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, window_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sep_doc ON cad_spectral_estimator_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_spectral_resolution_claims ( seq INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT NOT NULL UNIQUE, claim_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, estimator_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_src2_doc ON cad_spectral_resolution_claims(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_external_evidence_sources ( seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL UNIQUE, source_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, source_tier TEXT NOT NULL, scope TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ees_doc ON cad_external_evidence_sources(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_evidence_supersession_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, resolution TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ess_doc ON cad_evidence_supersession_records(document_id, seq ASC)
    """
    ,
    # REV59-ROOMQ: #761 sound strength G, #704 resonant treatment,
    # #707 serviceability — six append-only authorities.
    """
    CREATE TABLE IF NOT EXISTS cad_sound_strength_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, method TEXT NOT NULL, band TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gobs_doc ON cad_sound_strength_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_sound_strength_qualifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, qualification_id TEXT NOT NULL UNIQUE, qualification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_gqual_doc ON cad_sound_strength_qualifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_resonant_absorber_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, absorber_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_res_doc ON cad_resonant_absorber_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_resonant_performance_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, derivation TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rpr_doc ON cad_resonant_performance_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_service_envelope_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_svc_doc ON cad_service_envelope_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_service_access_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, envelope_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_svo_doc ON cad_service_access_observations(document_id, seq ASC)
    """
    ,
    # REV59-QUALNUM: #703 numerical reproducibility, #716 imaging
    # measurement chain, #717 wireless AV transport.
    """
    CREATE TABLE IF NOT EXISTS cad_numerical_repro_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, precision_kind TEXT NOT NULL, parallelism_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_nrep_doc ON cad_numerical_repro_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_stochastic_realizations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, realization_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_srez_doc ON cad_stochastic_realizations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_numerical_comparisons ( seq INTEGER PRIMARY KEY AUTOINCREMENT, comparison_id TEXT NOT NULL UNIQUE, comparison_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, domain TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_nxcmp_doc ON cad_numerical_comparisons(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_imaging_measurement_chains ( seq INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT NOT NULL UNIQUE, chain_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_state TEXT NOT NULL, shutter_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_imc_doc ON cad_imaging_measurement_chains(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_camera_calibrations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, calibration_id TEXT NOT NULL UNIQUE, calibration_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_camcal_doc ON cad_camera_calibrations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_camera_derived_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, chain_ref_id TEXT NOT NULL, measurand TEXT NOT NULL, processing_state TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cdo_doc ON cad_camera_derived_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wireless_av_links ( seq INTEGER PRIMARY KEY AUTOINCREMENT, link_id TEXT NOT NULL UNIQUE, link_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, transport_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wav_doc ON cad_wireless_av_links(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wireless_transport_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, link_ref_id TEXT NOT NULL, dropout_events INTEGER NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wto_doc ON cad_wireless_transport_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_wireless_sync_evidence ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, link_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_wsync_doc ON cad_wireless_sync_evidence(document_id, seq ASC)
    """
    ,
    # REV59-DRAWPROF: #741 CEB23-B video profile, #742 J-STD-710
    # drawing symbols, #733 timed-text presentation.
    """
    CREATE TABLE IF NOT EXISTS cad_ht_video_design_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, edition TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_htvdp_doc ON cad_ht_video_design_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_ceb23_evaluations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evaluation_id TEXT NOT NULL UNIQUE, evaluation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ceb23_doc ON cad_ceb23_evaluations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_drawing_symbol_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, edition TEXT NOT NULL, rights_provenance TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ads_doc ON cad_drawing_symbol_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_device_symbol_mappings ( seq INTEGER PRIMARY KEY AUTOINCREMENT, mapping_id TEXT NOT NULL UNIQUE, mapping_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, device_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dsm_doc ON cad_device_symbol_mappings(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_drawing_export_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, export_id TEXT NOT NULL UNIQUE, export_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, export_format TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dexp_doc ON cad_drawing_export_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_timed_text_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_ttp_doc ON cad_timed_text_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_caption_render_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_cro_doc ON cad_caption_render_observations(document_id, seq ASC)
    """
    ,
    # REV59-CODEPOLICY: #746 egress/accessibility, #748 lighting TLM/TLA,
    # #722 project data privacy/sharing.
    """
    CREATE TABLE IF NOT EXISTS cad_life_safety_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, project_kind TEXT NOT NULL, applicability_decision TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_lsp_doc ON cad_life_safety_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_circulation_routes ( seq INTEGER PRIMARY KEY AUTOINCREMENT, route_id TEXT NOT NULL UNIQUE, route_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, furniture_state TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rte_doc ON cad_circulation_routes(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_seating_accessibility_requirements ( seq INTEGER PRIMARY KEY AUTOINCREMENT, requirement_id TEXT NOT NULL UNIQUE, requirement_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_acr_doc ON cad_seating_accessibility_requirements(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_egress_evidence_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE, evidence_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, evidence_class TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_egx_doc ON cad_egress_evidence_records(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_professional_approval_refs ( seq INTEGER PRIMARY KEY AUTOINCREMENT, approval_id TEXT NOT NULL UNIQUE, approval_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_appr_doc ON cad_professional_approval_refs(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_dimming_temporal_profiles ( seq INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL UNIQUE, profile_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, luminaire_ref_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_dtp_doc ON cad_dimming_temporal_profiles(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_temporal_light_waveforms ( seq INTEGER PRIMARY KEY AUTOINCREMENT, waveform_id TEXT NOT NULL UNIQUE, waveform_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, profile_ref_id TEXT NOT NULL, illuminance_lx REAL NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tlw_doc ON cad_temporal_light_waveforms(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lighting_tlm_observations ( seq INTEGER PRIMARY KEY AUTOINCREMENT, observation_id TEXT NOT NULL UNIQUE, observation_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, waveform_ref_id TEXT NOT NULL, phenomenon TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tlmo_doc ON cad_lighting_tlm_observations(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_lighting_tla_assessments ( seq INTEGER PRIMARY KEY AUTOINCREMENT, assessment_id TEXT NOT NULL UNIQUE, assessment_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, metric_id TEXT NOT NULL, verdict TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_tlaa_doc ON cad_lighting_tla_assessments(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_project_data_classifications ( seq INTEGER PRIMARY KEY AUTOINCREMENT, classification_id TEXT NOT NULL UNIQUE, classification_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, artifact_ref_id TEXT NOT NULL, data_class TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_pdc_doc ON cad_project_data_classifications(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_sensitive_artifact_policies ( seq INTEGER PRIMARY KEY AUTOINCREMENT, policy_id TEXT NOT NULL UNIQUE, policy_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_sap_doc ON cad_sensitive_artifact_policies(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_export_redaction_manifests ( seq INTEGER PRIMARY KEY AUTOINCREMENT, manifest_id TEXT NOT NULL UNIQUE, manifest_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, bundle_kind TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_erm_doc ON cad_export_redaction_manifests(document_id, seq ASC)
    """
    ,
    """
    CREATE TABLE IF NOT EXISTS cad_retention_policy_records ( seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE, record_sha256 TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL, artifact_ref_id TEXT NOT NULL, retention_class TEXT NOT NULL, payload_json TEXT NOT NULL )
    """
    ,
    """
    CREATE INDEX IF NOT EXISTS idx_rtn_doc ON cad_retention_policy_records(document_id, seq ASC)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_finite_absorber_geometries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    geometry_id TEXT NOT NULL UNIQUE,
    geometry_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    edge_state TEXT,
    mounting_kind TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_finite_treatment_boundary_models (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL UNIQUE,
    model_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    geometry_ref_id TEXT NOT NULL,
    reaction_kind TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_precedence_profiles (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL UNIQUE,
    profile_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    stimulus_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_echo_risk_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL UNIQUE,
    observation_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    profile_ref_id TEXT NOT NULL,
    risk_verdict TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_reaction_to_fire_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    test_standard TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_finish_assembly_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    assembly_id TEXT NOT NULL UNIQUE,
    assembly_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    installation_context TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_finite_absorber_geometries_document_idx ON cad_finite_absorber_geometries (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_finite_treatment_boundary_models_document_idx ON cad_finite_treatment_boundary_models (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_precedence_profiles_document_idx ON cad_precedence_profiles (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_echo_risk_observations_document_idx ON cad_echo_risk_observations (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_reaction_to_fire_evidence_document_idx ON cad_reaction_to_fire_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_finish_assembly_evidence_document_idx ON cad_finish_assembly_evidence (document_id)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_listening_experiment_plans (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL UNIQUE,
    plan_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    method_kind TEXT NOT NULL,
    impairment_regime TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_listener_qualifications (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    qualification_id TEXT NOT NULL UNIQUE,
    qualification_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    training_completed INTEGER,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_subjective_inference_records (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL UNIQUE,
    record_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    plan_ref_id TEXT NOT NULL,
    verdict TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_assistive_listening_paths (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    path_id TEXT NOT NULL UNIQUE,
    path_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    technology TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_als_qualifications (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    qualification_id TEXT NOT NULL UNIQUE,
    qualification_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    path_ref_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_receiver_compatibility_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    path_ref_id TEXT NOT NULL,
    compatible INTEGER,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_dynamic_binaural_sessions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    session_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    hrtf_class TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_pose_tracking_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_ref_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_binaural_qualifications (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    qualification_id TEXT NOT NULL UNIQUE,
    qualification_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_ref_id TEXT NOT NULL,
    verdict TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_listening_experiment_plans_document_idx ON cad_listening_experiment_plans (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_listener_qualifications_document_idx ON cad_listener_qualifications (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_subjective_inference_records_document_idx ON cad_subjective_inference_records (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_assistive_listening_paths_document_idx ON cad_assistive_listening_paths (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_als_qualifications_document_idx ON cad_als_qualifications (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_receiver_compatibility_evidence_document_idx ON cad_receiver_compatibility_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_dynamic_binaural_sessions_document_idx ON cad_dynamic_binaural_sessions (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_pose_tracking_evidence_document_idx ON cad_pose_tracking_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_binaural_qualifications_document_idx ON cad_binaural_qualifications (document_id)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_receiver_reference_points (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id TEXT NOT NULL UNIQUE,
    reference_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    point_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_microphone_capsule_poses (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    pose_id TEXT NOT NULL UNIQUE,
    pose_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    reference_ref_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_measurement_fixtures (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id TEXT NOT NULL UNIQUE,
    fixture_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    fixture_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_fixture_scattering_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    bound_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_discrete_reflection_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    periodicity TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_echo_diagnostics (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    diagnostic_id TEXT NOT NULL UNIQUE,
    diagnostic_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    signal_class TEXT NOT NULL,
    verdict TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_drr_method_profiles (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL UNIQUE,
    profile_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    receiver_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_drr_measurements (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id TEXT NOT NULL UNIQUE,
    measurement_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    method_ref_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_receiver_reference_points_document_idx ON cad_receiver_reference_points (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_microphone_capsule_poses_document_idx ON cad_microphone_capsule_poses (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_measurement_fixtures_document_idx ON cad_measurement_fixtures (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_fixture_scattering_evidence_document_idx ON cad_fixture_scattering_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_discrete_reflection_events_document_idx ON cad_discrete_reflection_events (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_echo_diagnostics_document_idx ON cad_echo_diagnostics (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_drr_method_profiles_document_idx ON cad_drr_method_profiles (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_drr_measurements_document_idx ON cad_drr_measurements (document_id)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_power_sequence_plans (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL UNIQUE,
    plan_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    amplifier_step TEXT,
    amplifier_last_on_first_off INTEGER,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_power_sequence_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    plan_ref_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_power_quality_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL UNIQUE,
    observation_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    instrument_class TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_indoor_air_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL UNIQUE,
    observation_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    sensor_class TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_material_emission_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    emission_class TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_product_safety_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    safety_standard TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_emc_compliance_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    profile_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_power_sequence_plans_document_idx ON cad_power_sequence_plans (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_power_sequence_evidence_document_idx ON cad_power_sequence_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_power_quality_observations_document_idx ON cad_power_quality_observations (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_indoor_air_observations_document_idx ON cad_indoor_air_observations (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_material_emission_evidence_document_idx ON cad_material_emission_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_product_safety_evidence_document_idx ON cad_product_safety_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_emc_compliance_evidence_document_idx ON cad_emc_compliance_evidence (document_id)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_displayed_gradation_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL UNIQUE,
    observation_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    range_semantics TEXT NOT NULL,
    banding_observed INTEGER,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_colour_volume_measurements (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id TEXT NOT NULL UNIQUE,
    volume_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    colour_space TEXT NOT NULL,
    method TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_spatial_resolution_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    method TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_low_luminance_capabilities (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    capability_id TEXT NOT NULL UNIQUE,
    capability_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    stray_light_control TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_dynamic_contrast_measurements (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id TEXT NOT NULL UNIQUE,
    measurement_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    contrast_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_display_wall_boundaries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    boundary_id TEXT NOT NULL UNIQUE,
    boundary_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    wall_kind TEXT NOT NULL,
    acoustic_transparency_claim TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_wall_acoustic_impacts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    impact_id TEXT NOT NULL UNIQUE,
    impact_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    boundary_ref_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_displayed_gradation_observations_document_idx ON cad_displayed_gradation_observations (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_colour_volume_measurements_document_idx ON cad_colour_volume_measurements (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_spatial_resolution_evidence_document_idx ON cad_spatial_resolution_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_low_luminance_capabilities_document_idx ON cad_low_luminance_capabilities (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_dynamic_contrast_measurements_document_idx ON cad_dynamic_contrast_measurements (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_display_wall_boundaries_document_idx ON cad_display_wall_boundaries (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_wall_acoustic_impacts_document_idx ON cad_wall_acoustic_impacts (document_id)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_panning_continuity_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    stimulus_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_subwoofer_localization_profiles (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL UNIQUE,
    profile_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    stimulus_kind TEXT NOT NULL,
    crossover_hz REAL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_groupdelay_audibility (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id TEXT NOT NULL UNIQUE,
    verdict_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    stimulus_kind TEXT NOT NULL,
    peak_delay_ms REAL,
    frequency_hz REAL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_headphone_coupling_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    coupling_id TEXT NOT NULL UNIQUE,
    coupling_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    compensation_kind TEXT NOT NULL,
    fit_state TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_structureborne_paths (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    path_id TEXT NOT NULL UNIQUE,
    path_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    mount_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_spatial_remapping_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    remap_mode TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_panning_continuity_evidence_document_idx ON cad_panning_continuity_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_subwoofer_localization_profiles_document_idx ON cad_subwoofer_localization_profiles (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_groupdelay_audibility_document_idx ON cad_groupdelay_audibility (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_headphone_coupling_evidence_document_idx ON cad_headphone_coupling_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_structureborne_paths_document_idx ON cad_structureborne_paths (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_spatial_remapping_evidence_document_idx ON cad_spatial_remapping_evidence (document_id)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_codec_fidelity_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    codec_family TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_fft_spectral_estimator_profiles (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL UNIQUE,
    profile_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    window_kind TEXT NOT NULL,
    enbw_bins REAL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_clock_domain_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL UNIQUE,
    observation_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    domain_kind TEXT NOT NULL,
    lock_state TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_external_fact_claims (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL UNIQUE,
    claim_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    published_on TEXT,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_fact_conflict_resolutions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution_id TEXT NOT NULL UNIQUE,
    resolution_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    resolution_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE TABLE IF NOT EXISTS cad_bom_estimates (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id TEXT NOT NULL UNIQUE,
    estimate_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL,
    bom_version TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_codec_fidelity_evidence_document_idx ON cad_codec_fidelity_evidence (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_fft_spectral_estimator_profiles_document_idx ON cad_fft_spectral_estimator_profiles (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_clock_domain_observations_document_idx ON cad_clock_domain_observations (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_external_fact_claims_document_idx ON cad_external_fact_claims (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_fact_conflict_resolutions_document_idx ON cad_fact_conflict_resolutions (document_id)
    """
    ,
    """CREATE INDEX IF NOT EXISTS cad_bom_estimates_document_idx ON cad_bom_estimates (document_id)
    """
    ,
)




# Columns historically appended by lazy repository-local ALTER TABLE.
# Fresh databases receive them through the canonical CREATE statements above;
# databases whose tables predate the column are converged by the migration.
# (table, column, column definition)
NATIVE_COLUMN_ENSURES: tuple[tuple[str, str, str], ...] = (
    ('editor_view_states', 'selected_ids_json', "selected_ids_json TEXT NOT NULL DEFAULT '[]'"),
    ('editor_view_states', 'snap_json', 'snap_json TEXT'),
    ('cad_frequency_responses', 'dataset_sha256', 'dataset_sha256 TEXT'),
    ('cad_frequency_responses', 'transformation_sha256', 'transformation_sha256 TEXT'),
    ('cad_objective_evaluations', 'candidate_set_sha256', 'candidate_set_sha256 TEXT'),
    ('cad_objective_evaluations', 'input_authorities_json', 'input_authorities_json TEXT'),
    ('cad_prediction_results', 'result_sha256', 'result_sha256 TEXT'),
    ('cad_prediction_results', 'provider_response_json', 'provider_response_json TEXT'),
    ('htdt_project_documents', 'updated_at_utc', 'updated_at_utc TEXT'),
    ('htdt_project_documents', 'archived_at_utc', 'archived_at_utc TEXT'),
    ('cad_evidence_subjects', 'subject_sha256', 'subject_sha256 TEXT'),
    ('cad_evidence_observations', 'observation_sha256', 'observation_sha256 TEXT'),
)

# Every persistent table the migration authority owns. Used by the schema
# invariant tests and by diagnostics that must enumerate the contract.
NATIVE_SCHEMA_TABLES: tuple[str, ...] = (
    'assumption_decisions',
    'authoring_constraint_revisions',
    'authoring_constraint_sets',
    'cad_acoustic_geometry_derivations',
    'cad_acoustic_level_calibrations',
    'cad_acoustic_materials',
    'cad_acoustic_prediction_requests',
    'cad_acoustic_scene_snapshots',
    'cad_acoustic_solver_adapters',
    'cad_acoustic_solver_dispatch_bindings',
    'cad_acoustic_solver_results',
    'cad_acoustic_source_poses',
    'cad_acoustic_target_profiles',
    'cad_acoustic_treatment_comparisons',
    'cad_acoustic_treatment_definitions',
    'cad_acoustic_treatment_placements',
    'cad_acoustic_wave_excitations',
    'cad_acquisition_contexts',
    'cad_active_lf_control_events',
    'cad_active_lf_control_plans',
    'cad_adaptive_extended_observations',
    'cad_adaptive_extended_plans',
    'cad_adaptive_plans',
    'cad_ambient_comparisons',
    'cad_ambient_conditions',
    'cad_ambient_criteria',
    'cad_ambient_evaluations',
    'cad_ambient_profiles',
    'cad_amplifier_electrical_limits',
    'cad_amplifier_output_capabilities',
    'cad_analysis_studies',
    'cad_applicability_attestations',
    'cad_applied_preset_states',
    'cad_applied_settings',
    'cad_asbuilt_reconciliations',
    'cad_auralization_artifacts',
    'cad_auralization_capabilities',
    'cad_auralization_listening_validations',
    'cad_auralization_render_specs',
    'cad_auralization_review_packages',
    'cad_auralization_routing_declarations',
    'cad_av_latency_measurements',
    'cad_av_sync_conditions',
    'cad_background_noise_measurements',
    'cad_bass_management_profiles',
    'cad_bass_management_selections',
    'cad_bass_qualifications',
    'cad_bass_splice_evidence',
    'cad_cable_runs',
    'cad_calibration_evidence_events',
    'cad_calibration_exports',
    'cad_calibration_freezes',
    'cad_calibration_holdout_records',
    'cad_calibration_lifecycle_events',
    'cad_calibration_models',
    'cad_calibration_plans',
    'cad_calibration_results',
    'cad_calibration_specs',
    'cad_calibration_verification_completions',
    'cad_calibration_verification_plans',
    'cad_calibration_verification_registrations',
    'cad_checkpoint_restores',
    'cad_change_events',
    'cad_change_impact_assessments',
    'cad_commissioning_plans',
    'cad_commissioning_runs',
    'cad_compute_benchmarks',
    'cad_constraint_snapshots',
    'cad_constraint_workspaces',
    'cad_content_loudness_profiles',
    'cad_correction_qualifications',
    'cad_cost_evaluations',
    'cad_cost_records',
    'cad_coverage_evaluations',
    'cad_coverage_scenarios',
    'cad_current_topologies',
    'cad_data_source_registry',
    'cad_dataset_level_references',
    'cad_dataset_reviews',
    'cad_decision_rule_specs',
    'cad_decision_verdicts',
    'cad_dependency_resolution_events',
    'cad_design_briefs',
    'cad_design_checkpoints',
    'cad_design_comparison_sets',
    'cad_deterministic_ga_execution_inputs',
    'cad_deterministic_path_artifacts',
    'cad_device_action_acks',
    'cad_device_backup_artifacts',
    'cad_device_capability_snapshots',
    'cad_device_config_snapshots',
    'cad_device_firmware_transitions',
    'cad_device_known_good_baselines',
    'cad_device_replacement_assessments',
    'cad_device_restore_records',
    'cad_device_target_bindings',
    'cad_dialogue_intelligibility_assessments',
    'cad_direct_level_evaluations',
    'cad_direct_level_scenarios',
    'cad_direct_view_evaluations',
    'cad_direct_view_specifications',
    'cad_directivity_datasets',
    'cad_directivity_source_assets',
    'cad_drawing_set_specs',
    'cad_drift_assessments',
    'cad_environment_profiles',
    'cad_environment_selections',
    'cad_equipment_binding_semantics',
    'cad_electrical_qualifications',
    'cad_equipment_definitions',
    'cad_equipment_evidence_authorities',
    'cad_equipment_schedule_records',
    'cad_equipment_upgrades',
    'cad_evidence_observations',
    'cad_evidence_subjects',
    'cad_excitation_assets',
    'cad_extended_model_capabilities',
    'cad_extended_parameter_evidence',
    'cad_extended_search_specs',
    'cad_external_dependencies',
    'cad_external_standard_documents',
    'cad_field_evidence',
    'cad_field_evidence_records',
    'cad_field_evidence_targets',
    'cad_field_explorer_sessions',
    'cad_field_label_sheets',
    'cad_field_labels',
    'cad_field_sessions',
    'cad_frequency_resolved_evaluations',
    'cad_frequency_responses',
    'cad_gain_structure_evaluations',
    'cad_gain_structure_scenarios',
    'cad_health_baselines',
    'cad_health_check_plans',
    'cad_health_check_runs',
    'cad_hybrid_acoustic_results',
    'cad_hybrid_prediction_provider_bindings',
    'cad_hybrid_prediction_provider_objectives',
    'cad_hybrid_prediction_providers',
    'cad_hybrid_stitching_policies',
    'cad_importer_declarations',
    'cad_impulse_responses',
    'cad_installation_contexts',
    'cad_installation_datums',
    'cad_installation_drawing_sets',
    'cad_installed_definition_bindings',
    'cad_installed_device_observations',
    'cad_installed_equipment_instances',
    'cad_installed_equipment_replacements',
    'cad_intervention_alternatives',
    'cad_intervention_study_specs',
    'cad_ir_analysis_results',
    'cad_ir_analysis_specs',
    'cad_isolation_assemblies',
    'cad_isolation_estimates',
    'cad_isolation_measurements',
    'cad_isolation_scenarios',
    'cad_joint_candidate_evaluations',
    'cad_joint_candidate_selections',
    'cad_joint_candidates',
    'cad_joint_optimization_specs',
    'cad_late_decay_estimate_artifacts',
    'cad_late_field_artifacts',
    'cad_layout_profiles',
    'cad_lifecycle_observations',
    'cad_line_level_stages',
    'cad_listener_pose_selections',
    'cad_listener_poses',
    'cad_logical_physical_bindings',
    'cad_loudness_matching_records',
    'cad_material_definitions',
    'cad_material_evidence',
    'cad_materialized_pattern_points',
    'cad_measurement_assets',
    'cad_measurement_attachments',
    'cad_measurement_comparisons',
    'cad_measurement_corrections',
    'cad_measurement_dispositions',
    'cad_measurement_lineage',
    'cad_measurement_observations',
    'cad_measurement_plans',
    'cad_measurement_pose_observations',
    'cad_measurement_quality_reports',
    'cad_measurement_runner_events',
    'cad_measurement_runner_plans',
    'cad_measurement_runner_runs',
    'cad_measurement_significance_assessments',
    'cad_measurement_state_policies',
    'cad_measurement_state_snapshots',
    'cad_measurement_state_verdicts',
    'cad_measurement_target_lineages',
    'cad_measurement_target_patterns',
    'cad_measurement_transforms',
    'cad_measurement_uncertainty_budgets',
    'cad_measurements',
    'cad_model_validations',
    'cad_monitoring_declarations',
    'cad_multi_seat_results',
    'cad_multi_seat_sets',
    'cad_multi_sub_candidates',
    'cad_multi_sub_deployments',
    'cad_multi_sub_evaluations',
    'cad_multi_sub_qualifications',
    'cad_multi_sub_stage_comparisons',
    'cad_multifidelity_finalizations',
    'cad_multifidelity_plans',
    'cad_multifidelity_screening_evaluations',
    'cad_multifidelity_stage_results',
    'cad_noise_criterion_evaluations',
    'cad_normalization_observations',
    'cad_o90_robust_pareto_evaluations',
    'cad_objective_evaluations',
    'cad_observed_device_states',
    'cad_operating_presets',
    'cad_pareto_sets',
    'cad_perturbation_samples',
    'cad_physical_interconnects',
    'cad_plan_target_bindings',
    'cad_planned_observed_deltas',
    'cad_playback_chain_evaluations',
    'cad_playback_chain_scenarios',
    'cad_playback_gain_states',
    'cad_playback_level_conditions',
    'cad_prediction_measurement_registrations',
    'cad_prediction_measurement_residual_reports',
    'cad_prediction_matrix_result_sets',
    'cad_prediction_matrix_runs',
    'cad_prediction_matrix_specs',
    'cad_prediction_provider_bindings',
    'cad_prediction_provider_objectives',
    'cad_prediction_providers',
    'cad_prediction_results',
    'cad_presentation_proposals',
    'cad_presentation_sessions',
    'cad_presentation_sync_bindings',
    'cad_preset_measurement_bindings',
    'cad_project_boms',
    'cad_project_notes',
    'cad_projector_spec_evidence',
    'cad_projector_spec_source_assets',
    'cad_projector_specifications',
    'cad_programme_loudness_measurements',
    'cad_proposal_objective_result_authorities',
    'cad_proposal_perturbation_samples',
    'cad_proposal_robust_pareto_evaluations',
    'cad_proposal_robustness_evaluations',
    'cad_proposal_robustness_specs',
    'cad_proposed_device_actions',
    'cad_quality_calibration_files',
    'cad_r110_compiled_source_models',
    'cad_r120_compile_inputs',
    'cad_r120_compiled_geometry',
    'cad_r120_leak_diagnostic_inputs',
    'cad_r120_leak_portal_diagnostics',
    'cad_r140_execution_attempts',
    'cad_r140_execution_cache',
    'cad_r140_execution_results',
    'cad_r140_execution_schedules',
    'cad_r140_execution_tasks',
    'cad_r140_gpu_authorities',
    'cad_r140_resource_estimates',
    'cad_rack_definitions',
    'cad_rack_layouts',
    'cad_raw_mesh_repair_bundles',
    'cad_raw_source_records',
    'cad_reconciliation_decisions',
    'cad_reference_playback_profiles',
    'cad_response_targets',
    'cad_restore_confirmations',
    'cad_reverification_triggers',
    'cad_review_notes',
    'cad_robust_design_assessments',
    'cad_robustness_evaluations',
    'cad_robustness_specs',
    'cad_robustness_validation_cases',
    'cad_robustness_validation_decisions',
    'cad_room_operating_states',
    'cad_room_noise_metric_profiles',
    'cad_roomsim_batch_specs',
    'cad_roomsim_candidate_attempts',
    'cad_routing_profiles',
    'cad_rp22_evaluations',
    'cad_rp22_profiles',
    'cad_rp32_profiles',
    'cad_rp32_readiness',
    'cad_rp32_reconciliations',
    'cad_rp32_reports',
    'cad_rp32_verification_plans',
    'cad_rp32_verification_records',
    'cad_screen_transfer_selections',
    'cad_screen_transfers',
    'cad_search_specs',
    'cad_seat_priority_profiles',
    'cad_site_relationships',
    'cad_site_spaces',
    'cad_solver_capability_manifests',
    'cad_source_response_selections',
    'cad_source_responses',
    'cad_source_review_decisions',
    'cad_spatial_campaign_bindings',
    'cad_spatial_campaign_designs',
    'cad_spatial_campaign_evaluations',
    'cad_speaker_datasets',
    'cad_speaker_definitions',
    'cad_speaker_electrical_loads',
    'cad_speaker_impedances',
    'cad_spectral_balance_evaluations',
    'cad_speech_intelligibility_profiles',
    'cad_standard_evaluation_pins',
    'cad_standard_lifecycle_observations',
    'cad_standard_profile_mappings',
    'cad_standard_revision_diffs',
    'cad_standards_evaluations',
    'cad_standards_observation_authorities',
    'cad_standards_profiles',
    'cad_standards_source_authorities',
    'cad_sti_measurements',
    'cad_sti_predictions',
    'cad_stimulus_assets',
    'cad_stimulus_eligibility',
    'cad_stimulus_pins',
    'cad_stimulus_profiles',
    'cad_stochastic_receiver_estimate_artifacts',
    'cad_substitution_decisions',
    'cad_substitution_proposals',
    'cad_surface_material_assignments',
    'cad_symptom_episodes',
    'cad_system_variant_applications',
    'cad_system_variant_as_built',
    'cad_system_variant_measured',
    'cad_system_variant_measurement_campaign_completions',
    'cad_system_variant_measurement_campaign_registrations',
    'cad_system_variant_measurement_campaigns',
    'cad_system_variant_measurement_plan_completions',
    'cad_system_variant_measurement_plans',
    'cad_system_variants',
    'cad_target_curve_profiles',
    'cad_timing_references',
    'cad_tolerance_profiles',
    'cad_topology_candidate_variants',
    'cad_topology_comparison_bundles',
    'cad_topology_comparison_evaluations',
    'cad_topology_comparison_selections',
    'cad_topology_comparison_specs',
    'cad_topology_placement_candidates',
    'cad_topology_search_specs',
    'cad_topology_space_options',
    'cad_topology_spaces',
    'cad_treatment_boundary_compositions',
    'cad_treatment_boundary_overlays',
    'cad_treatment_comparison_outcomes',
    'cad_treatment_evidence_authorities',
    'cad_trend_assessments',
    'cad_uncertain_input_sets',
    'cad_upgrade_adoptions',
    'cad_upstream_version_candidates',
    'cad_validation_benchmark_specs',
    'cad_validation_campaign_registrations',
    'cad_validation_campaigns',
    'cad_validation_cases',
    'cad_validation_corpus_entries',
    'cad_video_action_proposals',
    'cad_video_before_after_comparisons',
    'cad_video_commissioning_sessions',
    'cad_video_commissioning_status_events',
    'cad_video_diagnoses',
    'cad_video_geometry_evaluations',
    'cad_video_geometry_workspaces',
    'cad_video_import_batches',
    'cad_video_operator_adjustments',
    'cad_video_presentation_profiles',
    'cad_video_presentation_selections',
    'cad_video_readiness_reports',
    'cad_visual_qa_verdicts',
    'cad_wave_excitation_evidence_authorities',
    'cad_wave_excitation_source_assets',
    'cad_wave_source_excitation_bindings',
    'cad_wiring_checks',
    'cad_wiring_verifications',
    'capture_authoring_provenances',
    'capture_authority_records',
    'capture_bundles',
    'capture_connected_space_documents',
    'capture_coordinate_authorities',
    'capture_disposition_transitions',
    'capture_inbox_items',
    'capture_inbox_promotions',
    'capture_inbox_registrations',
    'capture_inbox_supersessions',
    'capture_ingestion_authority_links',
    'capture_ingestion_lineages',
    'capture_ingestion_mesh_links',
    'capture_ingestion_runs',
    'capture_ingestion_source_links',
    'capture_mesh_compositions',
    'capture_mission_packages',
    'capture_raw_visual_mesh_bindings',
    'capture_receiver_config',
    'capture_receiver_deliveries',
    'capture_receiver_pairings',
    'capture_revision_conflicts',
    'capture_revisions',
    'capture_roomplan_records',
    'capture_semantic_promotions',
    'capture_source_evidence',
    'design_decisions',
    'editor_camera_states',
    'editor_named_views',
    'editor_view_states',
    'field_return_contributions',
    'floor_plan_underlays',
    'htdt_acceptance_evidence',
    'htdt_acceptance_runs',
    'htdt_content_blobs',
    'htdt_legacy_imports',
    'htdt_project_documents',
    'htdt_project_imports',
    'htdt_project_tombstones',
    'htdt_storage_gc_pending',
    'native_schema_metadata',
    'native_schema_migrations',
    'physical_space_models',
    'project_action_items',
    'project_templates',
    'r150_path_frequency_response_artifacts',
    'r160_late_energy_decay_artifacts',
    'r160_numerical_hybrid_responses',
    'r160_stitched_hybrid_responses',
    'scene_document_heads',
    'scene_recovery_snapshots',
    'scene_revisions',
    'scene_revision_labels',
    'seating_layout_specs',
    'cad_signal_paths',
    'cad_signal_path_selections',
    'cad_lighting_scenes',
    'cad_lighting_scene_selections',
    'cad_tactile_actuator_definitions',
    'cad_tactile_processing_profiles',
    'cad_tactile_profile_selections',
    'cad_usable_output_profiles',
    'cad_usable_output_selections',
    'cad_photometric_profiles',
    'cad_photometric_profile_selections',
    'cad_screen_optical_profiles',
    'cad_screen_optical_selections',
    'cad_color_target_profiles',
    'cad_color_target_selections',
    'cad_color_measurement_sets',
    'cad_ambient_reflectance_profiles',
    'template_instantiations',
    'cad_av_latency_profiles',
    'cad_av_latency_paths',
    'cad_av_latency_path_measurements',
    'cad_av_latency_qualifications',
    'cad_hdmi_signal_profiles',
    'cad_hdmi_edid_artifacts',
    'cad_hdmi_hdcp_observations',
    'cad_hdmi_link_observations',
    'cad_hdmi_verification_records',
    'cad_hdmi_qualifications',
    'cad_rp28_profiles',
    'cad_network_av_paths',
    'cad_network_media_flows',
    'cad_network_transport_observations',
    'cad_network_timing_observations',
    'cad_network_av_qualifications',
    'cad_isolation_elements',
    'cad_interroom_scenarios',
    'cad_interroom_field_measurements',
    'cad_isolation_calibrations',
    'cad_isolation_qualifications',
    'cad_mechanical_noise_tests',
    'cad_rattle_events',
    'cad_remediation_actions',
    'cad_mechanical_noise_qualifications',
    'cad_seat_acoustic_models',
    'cad_occupancy_scenarios',
    'cad_clearance_evaluations',
    'cad_seating_commissioning_results',
    'cad_security_assets',
    'cad_security_credentials',
    'cad_security_surfaces',
    'cad_security_observations',
    'cad_security_risks',
    'cad_remote_service_authorizations',
    'cad_security_test_evidence',
    'cad_access_reviews',
    'cad_security_reviews',
    'cad_control_surfaces',
    'cad_control_scenarios',
    'cad_control_scenario_runs',
    'cad_control_qualifications',
    'cad_exposure_limits',
    'cad_spl_capabilities',
    'cad_test_exposure_plans',
    'cad_exposure_gates',
    'cad_exposure_assessments',
    'cad_ifc_import_artifacts',
    'cad_ifc_entity_mappings',
    'cad_ifc_revision_deltas',
    'cad_ifc_intake_profiles',
    'cad_ifc_intake_evaluations',
    'cad_ifc_exports',
    'cad_performance_fact_profiles',
    'cad_performance_fact_products',
    'cad_performance_facts',
    'cad_performance_fact_imports',
    'cad_performance_fact_evaluations',
    'cad_performance_fact_rebinds',
    'cad_rack_enclosures',
    'cad_rack_devices',
    'cad_branch_circuits',
    'cad_power_protection_devices',
    'cad_poe_budgets',
    'cad_infrastructure_scenarios',
    'cad_rack_thermal_measurements',
    'cad_infrastructure_qualifications',
    'cad_immersive_contents',
    'cad_renderer_capabilities',
    'cad_speaker_layouts',
    'cad_render_sessions',
    'cad_render_output_observations',
    'cad_render_path_qualifications',
    'cad_electrical_noise_observations',
    'cad_audio_interconnects',
    'cad_noise_isolation_tests',
    'cad_humbuzz_diagnostics',
    'cad_noise_mitigations',
    'cad_humbuzz_verdicts',
    'cad_timebase_clock_domains',
    'cad_measurement_timebases',
    'cad_timebase_capability_assessments',
    'cad_evidence_bundles',
    'cad_evidence_artifacts',
    'cad_evidence_derivation_edges',
    'cad_evidence_attestations',
    'cad_evidence_bundle_validations',
    'cad_instrument_instances',
    'cad_calibration_events',
    'cad_calibration_interval_policies',
    'cad_instrument_verification_checks',
    'cad_instrument_service_events',
    'cad_instrument_fitness_assessments',
    'cad_out_of_tolerance_reviews',
    # REV57-PHYS: #613/#614/#615.
    'cad_geo_survey_instruments',
    'cad_geo_survey_campaigns',
    'cad_geo_element_evidence',
    'cad_geo_control_measurements',
    'cad_geo_reconciliations',
    'cad_geo_task_requirements',
    'cad_geo_qualifications',
    'cad_src_meas_conditions',
    'cad_src_mounting_conditions',
    'cad_src_boundary_corrections',
    'cad_src_measurements',
    'cad_src_boundary_qualifications',
    'cad_pam_parameter_evidence',
    'cad_pam_material_models',
    'cad_pam_buildups',
    'cad_pam_predictions',
    'cad_pam_fit_comparisons',
    # REV57-PROJ: #619/#622/#624/#627.
    'cad_spatial_measurement_plans',
    'cad_spatial_measurement_sets',
    'cad_spatial_derived_maps',
    'cad_spatial_uniformity_evaluations',
    'cad_presentation_geometry_bindings',
    'cad_image_geometry_measurements',
    'cad_lens_memory_recalls',
    'cad_geometry_evaluations',
    'cad_projector_install_constraints',
    'cad_projector_enclosure_plans',
    'cad_enclosure_operating_observations',
    'cad_enclosure_acoustic_observations',
    'cad_enclosure_qualifications',
    'cad_projector_safety_identities',
    'cad_manufacturer_safety_constraints',
    'cad_projector_placements',
    'cad_optical_safety_evaluations',
    # REV57-DISP: #625/#626/#633.
    'cad_dv_display_states',
    'cad_dv_stimulus_contexts',
    'cad_dv_photometric_measurements',
    'cad_dv_temporal_observations',
    'cad_dv_spatial_measurements',
    'cad_dv_angle_measurements',
    'cad_dv_qualifications',
    'cad_om_spectral_states',
    'cad_om_observer_profiles',
    'cad_om_evaluations',
    'cad_om_perceptual_matches',
    'cad_om_qualifications',
    'cad_ve_observations',
    'cad_ve_geometry_observations',
    'cad_ve_lighting_scenes',
    'cad_ve_qualifications',
    # REV57-AUD: #621/#634/#628/#632.
    'cad_channel_identity_chains',
    'cad_acoustic_endpoint_observations',
    'cad_channel_identity_tests',
    'cad_polarity_verification_records',
    'cad_channel_identity_evaluations',
    'cad_acoustic_aim_states',
    'cad_coverage_listener_areas',
    'cad_coverage_predictions',
    'cad_coverage_measurement_sets',
    'cad_coverage_qualifications',
    'cad_instance_acoustic_evidence',
    'cad_model_instance_deltas',
    'cad_matched_set_declarations',
    'cad_matched_set_qualifications',
    'cad_playback_stack_identities',
    'cad_media_profile_requirements',
    'cad_playback_capability_records',
    'cad_playback_operation_runs',
    'cad_playback_qualifications',
    # REV57-INST: #616/#618/#631/#612.
    'cad_hvac_ventilation_scenarios',
    'cad_hvac_path_declarations',
    'cad_hvac_component_evidence',
    'cad_hvac_field_observations',
    'cad_hvac_qualifications',
    'cad_ref_cal_profiles',
    'cad_ref_cal_stimuli',
    'cad_ref_cal_observations',
    'cad_ref_cal_qualifications',
    'cad_treatment_install_specs',
    'cad_treatment_asbuilt_observations',
    'cad_treatment_inspections',
    'cad_treatment_qualifications',
    'cad_tactile_vibration_paths',
    'cad_tactile_vibration_measurements',
    'cad_tactile_profiles',
    'cad_tactile_vibration_qualifications',
    # REV57-MOUNT: #620.
    'cad_mount_assemblies',
    'cad_mount_load_evidence',
    'cad_mount_support_elements',
    'cad_mount_manufacturer_requirements',
    'cad_mount_structural_approvals',
    'cad_mount_inspection_records',
    'cad_mount_qualifications',
    'cad_measchain_linearity_profiles',
    'cad_measchain_overload_observations',
    'cad_measchain_qualifications',
    'cad_sweep_deconvolution_specs',
    'cad_harmonic_impulse_components',
    'cad_recovered_impulse_responses',
    'cad_linear_ir_capabilities',
    'cad_excitation_source_profiles',
    'cad_source_orientation_captures',
    'cad_measurement_source_qualifications',
    'cad_dsp_realization_profiles',
    'cad_dsp_stage_records',
    'cad_dsp_parameter_mappings',
    'cad_dsp_realization_qualifications',
    'cad_decay_processing_profiles',
    'cad_decay_noise_estimates',
    'cad_decay_truncation_decisions',
    'cad_decay_edc_artifacts',
    'cad_decay_fit_records',
    'cad_boundary_evidence_records',
    'cad_boundary_rational_fits',
    'cad_td_impedance_realizations',
    'cad_boundary_realizability_assessments',
    # REV58-NUMERIC: #683 / #685 / #687.
    'cad_wave_fidelity_profiles',
    'cad_wave_convergence_records',
    'cad_wave_fidelity_qualifications',
    'cad_geometric_fidelity_profiles',
    'cad_ray_sampling_convergences',
    'cad_path_enumeration_qualifications',
    'cad_geometric_fidelity_qualifications',
    'cad_hybrid_composition_profiles',
    'cad_hybrid_transition_qualifications',
    # REV58-AUDIOMODEL: #654 / #655 / #656 / #690 / #684 / #681.
    'cad_source_origin_profiles',
    'cad_source_origin_qualifications',
    'cad_source_field_profiles',
    'cad_source_field_qualifications',
    'cad_directivity_sampling_profiles',
    'cad_directivity_interpolation_records',
    'cad_directivity_direction_qualifications',
    'cad_source_coherence_profiles',
    'cad_source_combination_qualifications',
    'cad_scattering_model_profiles',
    'cad_scattering_model_qualifications',
    'cad_diffraction_model_profiles',
    'cad_diffraction_benchmark_results',
    'cad_diffraction_qualifications',
    # REV58-IDENT: #689 / #691 / #698.
    'cad_log_quantities',
    'cad_log_calibration_bridges',
    'cad_log_operations',
    'cad_calib_parameter_records',
    'cad_ident_sensitivity_evidence',
    'cad_ident_correlation_evidence',
    'cad_ident_equivalent_sets',
    'cad_identifiability_assessments',
    'cad_validation_statistical_designs',
    'cad_dependence_models',
    'cad_dataset_role_assignments',
    'cad_benchmark_exposures',
    'cad_challenge_qualifications',
    # REV58-VALIDMETH: #675 / #674 / #673 / #671 / #677 / #706.
    'cad_optimization_problems',
    'cad_optimizer_run_profiles',
    'cad_optimizer_qualifications',
    'cad_pareto_assessments',
    'cad_mode_pairings',
    'cad_eigenmode_verdicts',
    'cad_diffuseness_assessments',
    'cad_statistical_applicability_declarations',
    'cad_multi_slope_fits',
    'cad_single_slope_assessments',
    'cad_coupled_decay_qualifications',
    'cad_reflection_pairings',
    'cad_reflection_correspondence_sets',
    'cad_reflection_correspondence_verdicts',
    'cad_modal_decay_observations',
    'cad_modal_decay_qualifications',
    # REV58-DISPLAYMEAS: #682 / #680 / #686 / #647 / #666.
    'cad_pg_generator_instances',
    'cad_pg_requested_patches',
    'cad_pg_delivered_observations',
    'cad_pg_fidelity_qualifications',
    'cad_mm_match_profiles',
    'cad_mm_match_observations',
    'cad_mm_verifications',
    'cad_mm_applicability',
    'cad_da_additivity_observations',
    'cad_da_separation_assessments',
    'cad_da_volumetric_characterisations',
    'cad_da_holdout_verifications',
    'cad_da_model_eligibility',
    'cad_da_characterisation_plans',
    'cad_td_states',
    'cad_td_step_responses',
    'cad_td_motion_measurements',
    'cad_td_flicker_measurements',
    'cad_td_retention_observations',
    'cad_td_qualifications',
    'cad_lut_artifacts',
    'cad_lut_generation_records',
    'cad_lut_preflight_verifications',
    'cad_lut_deployments',
    'cad_lut_post_verifications',
    'cad_lut_qualifications',
    # REV58-MEASELEC: #699 / #651 / #649 / #665 / #693.
    'cad_interface_loopback_observations',
    'cad_interface_transfer_calibrations',
    'cad_interface_correction_qualifications',
    'cad_signal_level_references',
    'cad_noise_floor_observations',
    'cad_clipping_margins',
    'cad_gain_structure_qualifications',
    'cad_playback_dynamics_states',
    'cad_level_sweep_observations',
    'cad_playback_dynamics_qualifications',
    'cad_multiway_speaker_definitions',
    'cad_active_crossover_plans',
    'cad_driver_alignment_measurements',
    'cad_active_crossover_qualifications',
    'cad_method_procedures',
    'cad_reproducibility_campaigns',
    'cad_method_precision_models',
    'cad_reproducibility_qualifications',
    # REV59-APPLY: #723 device apply transaction / rollback.
    'cad_apply_capability_profiles',
    'cad_apply_plans',
    'cad_apply_write_records',
    'cad_apply_verifications',
    'cad_apply_rollback_plans',
    'cad_apply_rollback_executions',
    'cad_apply_transactions',
    'cad_fractional_octave_profiles',
    'cad_band_integrations',
    'cad_echo_density_profiles',
    'cad_mixing_time_estimates',
    'cad_late_field_assessments',
    'cad_interpolation_profiles',
    'cad_field_surface_records',
    'cad_solver_budget_profiles',
    'cad_compute_observations',
    'cad_accuracy_cost_envelopes',
    'cad_jitter_profiles',
    'cad_jitter_observations',
    'cad_jitter_transfer_measurements',
    'cad_converter_jitter_susceptibility',
    'cad_dither_profiles',
    'cad_digital_path_transforms',
    'cad_playback_src_profiles',
    'cad_src_qualifications',
    'cad_clock_domain_crossings',
    'cad_interchannel_leakage_measurements',
    'cad_channel_separation_qualifications',
    'cad_projector_light_profiles',
    'cad_temporal_contrast_measures',
    'cad_dynamic_contrast_qualifications',
    'cad_light_measurement_capabilities',
    'cad_low_luminance_observations',
    'cad_display_boundary_profiles',
    'cad_front_stage_variants',
    'cad_codec_chain_profiles',
    'cad_quality_method_profiles',
    'cad_codec_fidelity_observations',
    # REV59-POWEREV: #736 / #738 / #752.
    'cad_power_sequencing_profiles',
    'cad_power_sequence_events',
    'cad_ups_transition_records',
    'cad_power_quality_measurements',
    'cad_power_quality_qualifications',
    'cad_emc_product_profiles',
    'cad_emc_symptom_records',
    # REV59-BUILDENV: #751 / #740 / #750.
    'cad_product_safety_profiles',
    'cad_occupied_iaq_observations',
    'cad_occupied_iaq_qualifications',
    'cad_voc_emission_profiles',
    # REV59-ACOUST2: #743 / #749 / #765.
    'cad_measurement_fixture_profiles',
    'cad_fixture_scattering_observations',
    'cad_spectral_estimator_profiles',
    'cad_spectral_resolution_claims',
    'cad_external_evidence_sources',
    'cad_evidence_supersession_records',
    # REV59-DEPS: #729 / #725 / #718.
    'cad_dependency_edge_declarations',
    'cad_dependency_change_events',
    'cad_dependency_rule_profiles',
    'cad_staleness_assessments',
    'cad_revalidation_plans',
    'cad_signed_manifests',
    'cad_manifest_attestations',
    'cad_attestation_verifications',
    'cad_archive_snapshots',
    'cad_archive_verifications',
    'cad_migration_records',
    'cad_migration_verifications',
    # REV59-ROOMQ: #761 / #704 / #707.
    'cad_sound_strength_observations',
    'cad_sound_strength_qualifications',
    'cad_resonant_absorber_profiles',
    'cad_resonant_performance_records',
    'cad_service_envelope_profiles',
    'cad_service_access_observations',
    # REV59-QUALNUM: #703 / #716 / #717.
    'cad_numerical_repro_profiles',
    'cad_stochastic_realizations',
    'cad_numerical_comparisons',
    'cad_imaging_measurement_chains',
    'cad_camera_calibrations',
    'cad_camera_derived_observations',
    'cad_wireless_av_links',
    'cad_wireless_transport_observations',
    'cad_wireless_sync_evidence',
    # REV59-DRAWPROF: #741 / #742 / #733.
    'cad_ht_video_design_profiles',
    'cad_ceb23_evaluations',
    'cad_drawing_symbol_profiles',
    'cad_device_symbol_mappings',
    'cad_drawing_export_records',
    'cad_timed_text_profiles',
    'cad_caption_render_observations',
    # REV59-CODEPOLICY: #746 / #748 / #722.
    'cad_life_safety_profiles',
    'cad_circulation_routes',
    'cad_seating_accessibility_requirements',
    'cad_egress_evidence_records',
    'cad_professional_approval_refs',
    'cad_dimming_temporal_profiles',
    'cad_temporal_light_waveforms',
    'cad_lighting_tlm_observations',
    'cad_lighting_tla_assessments',
    'cad_project_data_classifications',
    'cad_sensitive_artifact_policies',
    'cad_export_redaction_manifests',
    'cad_retention_policy_records',
    # REV59-ACOUST3: #694 / #646 / #648.
    'cad_finite_absorber_geometries',
    'cad_finite_treatment_boundary_models',
    'cad_precedence_profiles',
    'cad_echo_risk_observations',
    'cad_reaction_to_fire_evidence',
    'cad_finish_assembly_evidence',
    'cad_listening_experiment_plans',
    'cad_listener_qualifications',
    'cad_subjective_inference_records',
    'cad_assistive_listening_paths',
    'cad_als_qualifications',
    'cad_receiver_compatibility_evidence',
    'cad_dynamic_binaural_sessions',
    'cad_pose_tracking_evidence',
    'cad_binaural_qualifications',
    'cad_receiver_reference_points',
    'cad_microphone_capsule_poses',
    'cad_measurement_fixtures',
    'cad_fixture_scattering_evidence',
    'cad_discrete_reflection_events',
    'cad_echo_diagnostics',
    'cad_drr_method_profiles',
    'cad_drr_measurements',
    'cad_power_sequence_plans',
    'cad_power_sequence_evidence',
    'cad_power_quality_observations',
    'cad_indoor_air_observations',
    'cad_material_emission_evidence',
    'cad_product_safety_evidence',
    'cad_emc_compliance_evidence',
    'cad_displayed_gradation_observations',
    'cad_colour_volume_measurements',
    'cad_spatial_resolution_evidence',
    'cad_low_luminance_capabilities',
    'cad_dynamic_contrast_measurements',
    'cad_display_wall_boundaries',
    'cad_wall_acoustic_impacts',
    'cad_panning_continuity_evidence',
    'cad_subwoofer_localization_profiles',
    'cad_groupdelay_audibility',
    'cad_headphone_coupling_evidence',
    'cad_structureborne_paths',
    'cad_spatial_remapping_evidence',
    'cad_codec_fidelity_evidence',
    'cad_fft_spectral_estimator_profiles',
    'cad_clock_domain_observations',
    'cad_external_fact_claims',
    'cad_fact_conflict_resolutions',
    'cad_bom_estimates',
)
