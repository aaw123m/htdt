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
    'cad_auralization_artifacts',
    'cad_auralization_capabilities',
    'cad_auralization_listening_validations',
    'cad_auralization_render_specs',
    'cad_auralization_review_packages',
    'cad_auralization_routing_declarations',
    'cad_av_latency_measurements',
    'cad_av_sync_conditions',
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
    'cad_commissioning_plans',
    'cad_commissioning_runs',
    'cad_compute_benchmarks',
    'cad_constraint_snapshots',
    'cad_constraint_workspaces',
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
    'cad_direct_level_evaluations',
    'cad_direct_level_scenarios',
    'cad_direct_view_evaluations',
    'cad_direct_view_specifications',
    'cad_directivity_datasets',
    'cad_directivity_source_assets',
    'cad_drawing_set_specs',
    'cad_environment_profiles',
    'cad_environment_selections',
    'cad_equipment_binding_semantics',
    'cad_equipment_definitions',
    'cad_equipment_evidence_authorities',
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
    'cad_line_level_stages',
    'cad_listener_pose_selections',
    'cad_listener_poses',
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
    'cad_o90_robust_pareto_evaluations',
    'cad_objective_evaluations',
    'cad_observed_device_states',
    'cad_operating_presets',
    'cad_pareto_sets',
    'cad_perturbation_samples',
    'cad_plan_target_bindings',
    'cad_planned_observed_deltas',
    'cad_playback_chain_evaluations',
    'cad_playback_chain_scenarios',
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
    'cad_review_notes',
    'cad_robust_design_assessments',
    'cad_robustness_evaluations',
    'cad_robustness_specs',
    'cad_robustness_validation_cases',
    'cad_robustness_validation_decisions',
    'cad_room_operating_states',
    'cad_roomsim_batch_specs',
    'cad_roomsim_candidate_attempts',
    'cad_routing_profiles',
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
    'cad_standard_evaluation_pins',
    'cad_standard_lifecycle_observations',
    'cad_standard_profile_mappings',
    'cad_standard_revision_diffs',
    'cad_standards_evaluations',
    'cad_standards_observation_authorities',
    'cad_standards_profiles',
    'cad_standards_source_authorities',
    'cad_stimulus_assets',
    'cad_stimulus_eligibility',
    'cad_stimulus_pins',
    'cad_stimulus_profiles',
    'cad_stochastic_receiver_estimate_artifacts',
    'cad_surface_material_assignments',
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
)
