"""Row/payload semantic integrity for duplicated native columns (#313).

Native tables persist a canonical ``payload_json`` (or ``plan_json`` /
``request_json``) next to duplicated searchable columns — ids, digests,
revision bindings, status gates. ``PRAGMA integrity_check`` and
``foreign_key_check`` cannot detect a row whose indexed column was changed
independently of the payload that remains its semantic authority. This
module is the shared invariant boundary: repositories verify the columns
they selected against the parsed payload on read (fail closed), and
:func:`scan_native_row_integrity` lets backup/restore validation and
diagnostics audit every registered row.

The duplicated column is *not* authoritative — the payload is. A mismatch
is therefore always reported as drift of the row column, never silently
reconciled.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from .cad_schema_ddl import NATIVE_BASELINE_DDL


class NativeRowIntegrityError(ValueError):
    """Persisted row columns disagree with the canonical payload."""


@dataclass(frozen=True)
class RowPayloadDrift:
    """One row column that does not equal the canonical payload value."""

    table: str
    identity: str
    column: str
    row_value: Any
    payload_value: Any

    def describe(self) -> str:
        return (
            f'{self.table}({self.identity}): row {self.column}='
            f'{self.row_value!r} but payload carries {self.payload_value!r}'
        )


@dataclass(frozen=True)
class RowBinding:
    """One duplicated column bound to a JSON path inside the payload.

    ``optional`` tolerates payloads written before the key existed (legacy
    capture requests): the check is skipped only when the key is absent.
    ``row_json`` marks columns stored as JSON text that must be decoded
    before comparison.
    """

    column: str
    path: tuple[str, ...]
    optional: bool = False
    row_json: bool = False


RowMapping = Mapping[str, Any]
ExtraCheck = Callable[[RowMapping, str, Mapping[str, Any]], Iterable[RowPayloadDrift]]


def _row_mapping(row: sqlite3.Row | RowMapping) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _payload_at(payload: Mapping[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    node: Any = payload
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return False, None
        node = node[key]
    return True, node


def row_payload_drifts(
    table: str,
    row: sqlite3.Row | RowMapping,
    payload: Mapping[str, Any],
    bindings: Iterable[RowBinding],
) -> list[RowPayloadDrift]:
    """Compare one row's duplicated columns against its parsed payload.

    Shared by repository read paths (which raise their own error type on
    any returned drift) and by :func:`scan_native_row_integrity`.
    """

    row_map = _row_mapping(row)
    identity = str(
        row_map.get('seq')
        or next(
            (row_map[b.column] for b in bindings if b.column.endswith('_id')),
            '?',
        )
    )
    drifts: list[RowPayloadDrift] = []
    for binding in bindings:
        found, payload_value = _payload_at(payload, binding.path)
        if not found:
            if binding.optional:
                continue
            drifts.append(
                RowPayloadDrift(
                    table, identity, binding.column,
                    row_map.get(binding.column), '<payload key missing>',
                )
            )
            continue
        row_value = row_map.get(binding.column)
        if binding.row_json and row_value is not None:
            try:
                row_value = json.loads(row_value)
            except (TypeError, ValueError):
                pass
        if payload_value is not None and isinstance(payload_value, (list, tuple)):
            payload_value = list(payload_value)
        if row_value != payload_value:
            drifts.append(
                RowPayloadDrift(
                    table, identity, binding.column, row_value, payload_value,
                )
            )
    return drifts


def _scene_revision_content_hash(
    row: RowMapping, payload_text: str, payload: Mapping[str, Any],
):
    actual = sha256(payload_text.encode('utf-8')).hexdigest()
    if row.get('content_hash') != actual:
        yield RowPayloadDrift(
            'scene_revisions',
            str(row.get('revision_id')),
            'content_hash',
            row.get('content_hash'),
            actual,
        )


def _capture_run_plan_sha(
    row: RowMapping, payload_text: str, payload: Mapping[str, Any],
):
    actual = sha256(payload_text.encode('utf-8')).hexdigest()
    if row.get('plan_sha256') != actual:
        yield RowPayloadDrift(
            'capture_ingestion_runs',
            str(row.get('ingestion_run_id')),
            'plan_sha256',
            row.get('plan_sha256'),
            actual,
        )


def _promotion_request_scope(
    row: RowMapping, payload_text: str, payload: Mapping[str, Any],
):
    # The same legacy-scope gate the repository read path uses: requests
    # predating run-scoped identity are rebound on read and legitimately
    # diverge from row columns.
    authority = payload.get('world_to_scene_authority')
    if 'ingestion_run_id' not in payload or (
        isinstance(authority, Mapping)
        and 'coordinate_authority_id' not in authority
    ):
        return
    for column in (
        'promotion_id',
        'ingestion_run_id',
        'raw_mesh_binding_id',
        'mesh_composition_id',
        'source_scene_revision_id',
    ):
        if row.get(column) != payload.get(column):
            yield RowPayloadDrift(
                'capture_semantic_promotions',
                str(row.get('promotion_id')),
                column,
                row.get(column),
                payload.get(column),
            )


def _b(column: str, *path: str, optional: bool = False, row_json: bool = False) -> RowBinding:
    return RowBinding(column, path, optional=optional, row_json=row_json)


# table -> (payload column, bindings, extra checks). Only columns that are
# genuine duplicates of payload state are bound; row-only result columns
# (a promotion's produced scene_revision_id, run identity digests not
# carried by the plan) are intentionally not listed.
_ROW_BINDINGS: dict[str, tuple[str, tuple[RowBinding, ...], tuple[ExtraCheck, ...]]] = {
    'scene_revisions': (
        'payload_json',
        (_b('document_id', 'document_id'),),
        (_scene_revision_content_hash,),
    ),
    'cad_search_specs': (
        'payload_json',
        (
            _b('search_spec_id', 'search_spec_id'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
            _b('scene_content_hash', 'scene_content_hash'),
            _b('constraint_workspace_hash', 'constraint_workspace_hash'),
            _b('search_spec_sha256', 'search_spec_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_model_validations': (
        'payload_json',
        (
            _b('validation_id', 'validation_id'),
            _b('document_id', 'document_id'),
            _b('search_spec_id', 'search_spec_id'),
            _b('model_id', 'model_id'),
            _b('model_version', 'model_version'),
            _b('recommendation_gate', 'recommendation_gate'),
            _b('validation_sha256', 'validation_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_objective_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
            _b('scene_content_hash', 'scene_content_hash'),
            _b('search_spec_id', 'search_spec_id'),
            _b('search_spec_sha256', 'search_spec_sha256'),
            _b('candidate_id', 'candidate_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_pareto_sets': (
        'payload_json',
        (
            _b('pareto_set_id', 'pareto_set_id'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
            _b('scene_content_hash', 'scene_content_hash'),
            _b('search_spec_id', 'search_spec_id'),
            _b('search_spec_sha256', 'search_spec_sha256'),
            _b('pareto_sha256', 'pareto_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_system_variants': (
        'payload_json',
        (
            _b('variant_id', 'variant_id'),
            _b('document_id', 'document_id'),
            _b('baseline_revision_id', 'baseline_revision_id'),
            _b('baseline_content_hash', 'baseline_content_hash'),
            _b('parent_variant_id', 'parent_variant_id', optional=True),
            _b('variant_sha256', 'variant_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_system_variant_applications': (
        'payload_json',
        (
            _b('application_id', 'application_id'),
            _b('variant_id', 'variant_id'),
            _b('document_id', 'document_id'),
            _b('baseline_revision_id', 'baseline_revision_id'),
            _b('applied_revision_id', 'applied_revision_id'),
            _b('application_sha256', 'application_sha256'),
            _b('selected_at_utc', 'selected_at_utc'),
        ),
        (),
    ),
    'capture_ingestion_runs': (
        'plan_json',
        (
            _b('lineage_digest', 'lineage_digest'),
            _b('bundle_digest', 'bundle', 'bundle_digest'),
            _b('capture_revision_id', 'bundle', 'capture_revision_id'),
            _b('ingestor_name', 'ingestor', 'name'),
            _b('ingestor_version', 'ingestor', 'version'),
            _b('configuration_digest', 'ingestor', 'configuration_digest'),
        ),
        (_capture_run_plan_sha,),
    ),
    'capture_raw_visual_mesh_bindings': (
        'payload_json',
        (
            _b('binding_id', 'binding_id'),
            _b('handoff_id', 'handoff', 'raw_visual_mesh_handoff_id'),
        ),
        (),
    ),
    'capture_authority_records': (
        'payload_json',
        (
            _b('authority_record_handoff_id', 'authority_record_handoff_id'),
            _b('source_evidence_id', 'source_evidence_id'),
        ),
        (),
    ),
    # Legacy promotion/composition requests predate run-scoped identity:
    # every binding is optional so migrated payloads are checked only on
    # the keys they actually carry.
    'capture_semantic_promotions': (
        'request_json',
        (),
        (_promotion_request_scope,),
    ),
    'capture_mesh_compositions': (
        'request_json',
        (
            _b('composition_id', 'composition_id', optional=True),
            _b('ingestion_run_id', 'ingestion_run_id', optional=True),
            _b('coordinate_authority_id', 'coordinate_authority_id', optional=True),
            _b('overlap_policy', 'overlap_policy', optional=True),
            _b(
                'binding_ids_json', 'raw_mesh_binding_ids',
                optional=True, row_json=True,
            ),
        ),
        (),
    ),
    # #763: family bindings — each column is a genuine duplicate of the
    # payload's own field (payloads are model_dump_json of the record).
    'cad_health_baselines': (
        'payload_json',
        (
            _b('baseline_id', 'baseline_id'),
            _b('document_id', 'document_id'),
            _b('baseline_sha256', 'baseline_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_health_check_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('document_id', 'document_id'),
            _b('baseline_id', 'baseline_id'),
            _b('baseline_sha256', 'baseline_sha256'),
            _b('plan_sha256', 'plan_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_health_check_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('document_id', 'document_id'),
            _b('plan_id', 'plan_id'),
            _b('run_sha256', 'run_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_constraint_snapshots': (
        'payload_json',
        (
            _b('snapshot_id', 'snapshot_id'),
            _b('document_id', 'document_id'),
            _b('constraint_sha256', 'constraint_sha256'),
            _b('snapshot_sha256', 'snapshot_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_design_checkpoints': (
        'payload_json',
        (
            _b('checkpoint_id', 'checkpoint_id'),
            _b('document_id', 'document_id'),
            _b('checkpoint_sha256', 'checkpoint_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_checkpoint_restores': (
        'payload_json',
        (
            _b('restore_id', 'restore_id'),
            _b('document_id', 'document_id'),
            _b('checkpoint_id', 'checkpoint_id'),
            _b('restore_sha256', 'restore_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_design_comparison_sets': (
        'payload_json',
        (
            _b('set_id', 'set_id'),
            _b('document_id', 'document_id'),
            _b('revision', 'revision'),
            _b('supersedes_set_id', 'supersedes_set_id'),
            _b('set_sha256', 'set_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_operating_presets': (
        'payload_json',
        (
            _b('preset_id', 'preset_id'),
            _b('document_id', 'document_id'),
            _b('category', 'category'),
            _b('preset_sha256', 'preset_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_applied_preset_states': (
        'payload_json',
        (
            _b('applied_id', 'applied_id'),
            _b('document_id', 'document_id'),
            _b('preset_id', 'preset_id'),
            _b('preset_sha256', 'preset_sha256'),
            _b('confirmed_at_utc', 'confirmed_at_utc'),
        ),
        (),
    ),
    'cad_preset_measurement_bindings': (
        'payload_json',
        (
            _b('binding_id', 'binding_id'),
            _b('document_id', 'document_id'),
            _b('preset_id', 'preset_id'),
            _b('preset_sha256', 'preset_sha256'),
            _b('bound_at_utc', 'bound_at_utc'),
        ),
        (),
    ),
    'cad_project_notes': (
        'payload_json',
        (
            _b('note_id', 'note_id'),
            _b('document_id', 'document_id'),
            _b('note_sha256', 'note_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_acoustic_level_calibrations': (
        'payload_json',
        (
            _b('calibration_id', 'calibration_id'),
            _b('calibration_sha256', 'calibration_sha256'),
            # The row's created_at_utc column carries the model's
            # calibrated_at_utc — same instant, different field name.
            _b('created_at_utc', 'calibrated_at_utc'),
        ),
        (),
    ),
    'cad_seat_priority_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
        ),
        (),
    ),
    'cad_applied_settings': (
        'payload_json',
        (
            _b('applied_id', 'applied_id'),
            _b('document_id', 'document_id'),
            _b('calibration_plan_id', 'calibration_plan_id'),
            _b('applied_sha256', 'applied_sha256'),
            _b('applied_at_utc', 'applied_at_utc'),
        ),
        (),
    ),
    'cad_cost_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('category', 'category'),
        ),
        (),
    ),
    'cad_cost_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('variant_id', 'variant_id'),
        ),
        (),
    ),
    'cad_intervention_study_specs': (
        'payload_json',
        (
            _b('spec_id', 'spec_id'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
            _b('scene_content_hash', 'scene_content_hash'),
            _b('spec_sha256', 'spec_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_intervention_alternatives': (
        'payload_json',
        (
            _b('alternative_id', 'alternative_id'),
            _b('spec_id', 'study_spec_id'),
            _b('family', 'family'),
            _b('alternative_sha256', 'alternative_sha256'),
        ),
        (),
    ),
    # #817/#818: profile authorities — document_id on profile rows is row
    # scope (the model has no document field), so it is not bound; every
    # other duplicated column is a genuine payload field duplicate.
    'cad_bass_management_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('version', 'version'),
            _b('profile_sha256', 'profile_sha256'),
            _b('lifecycle', 'lifecycle'),
        ),
        (),
    ),
    'cad_bass_management_selections': (
        'payload_json',
        (
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('version', 'version'),
            _b('profile_sha256', 'profile_sha256'),
            _b('selected_at_utc', 'selected_at_utc'),
        ),
        (),
    ),
    'cad_video_presentation_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('version', 'version'),
            _b('profile_sha256', 'profile_sha256'),
        ),
        (),
    ),
    'cad_video_presentation_selections': (
        'payload_json',
        (
            _b('document_id', 'document_id'),
            _b('screen_entity_id', 'screen_entity_id'),
            _b('profile_id', 'profile_id'),
            _b('version', 'version'),
            _b('profile_sha256', 'profile_sha256'),
            _b('selected_at_utc', 'selected_at_utc'),
        ),
        (),
    ),
}


# ---------------------------------------------------------------------------
# Registry completeness (#763)
#
# Every table whose DDL carries a canonical payload column must be either
# bound in ``_ROW_BINDINGS`` or audited into ``_UNBOUND_PAYLOAD_TABLES``.
# ``assert_row_integrity_registry_complete`` is the CI-enforced invariant:
# a new canonical-payload table fails it until a maintainer either writes
# its bindings or audits it into the unbound ledger.

PAYLOAD_COLUMN_NAMES = ('payload_json', 'plan_json', 'request_json')

_CANONICAL_PAYLOAD_RE = re.compile(
    r'CREATE TABLE IF NOT EXISTS (\w+) \([^)]*?\b('
    + '|'.join(PAYLOAD_COLUMN_NAMES)
    + r')\b[^)]*\)'
)


def canonical_payload_tables() -> dict[str, str]:
    """``table -> canonical payload column`` parsed from the versioned DDL.

    Auxiliary ``*_json`` columns (``parameters_json``, ``inputs_json``,
    ``authorities_json`` …) are not canonical payloads — only the three
    names above claim to carry the row's authoritative document.
    """

    return {
        match.group(1): match.group(2)
        for match in (
            _CANONICAL_PAYLOAD_RE.search(stmt) for stmt in NATIVE_BASELINE_DDL
        )
        if match is not None
    }


# Audited tables whose duplicated columns are not bound yet — either
# append-only rows whose columns are insert-only derivatives, or families
# not yet bound. Presence here is an explicit decision, not silence: a
# canonical-payload table absent from both registries fails the
# completeness invariant. Membership is inventory, not verification —
# ``scan_native_row_integrity`` still parses every row's canonical
# payload in these tables and reports unparseable or non-object payloads
# as drift; only duplicated-column comparisons are waived.
_UNBOUND_PAYLOAD_TABLES: tuple[str, ...] = (
    'authoring_constraint_sets',
    'cad_acoustic_prediction_requests',
    'cad_acoustic_scene_snapshots',
    'cad_acoustic_solver_adapters',
    'cad_acoustic_solver_dispatch_bindings',
    'cad_acoustic_solver_results',
    'cad_acoustic_treatment_comparisons',
    'cad_acoustic_treatment_definitions',
    'cad_acoustic_treatment_placements',
    'cad_acoustic_wave_excitations',
    'cad_acquisition_contexts',
    'cad_adaptive_plans',
    'cad_ambient_comparisons',
    'cad_ambient_conditions',
    'cad_ambient_criteria',
    'cad_ambient_evaluations',
    'cad_ambient_profiles',
    'cad_amplifier_electrical_limits',
    'cad_amplifier_output_capabilities',
    'cad_applicability_attestations',
    'cad_av_latency_measurements',
    'cad_av_sync_conditions',
    'cad_constraint_workspaces',
    'cad_coverage_evaluations',
    'cad_coverage_scenarios',
    'cad_current_topologies',
    'cad_deterministic_ga_execution_inputs',
    'cad_deterministic_path_artifacts',
    'cad_direct_level_evaluations',
    'cad_direct_level_scenarios',
    'cad_direct_view_evaluations',
    'cad_direct_view_specifications',
    'cad_directivity_datasets',
    'cad_equipment_binding_semantics',
    'cad_equipment_definitions',
    'cad_equipment_evidence_authorities',
    'cad_equipment_upgrades',
    'cad_extended_model_capabilities',
    'cad_extended_parameter_evidence',
    'cad_extended_search_specs',
    'cad_frequency_resolved_evaluations',
    'cad_gain_structure_evaluations',
    'cad_gain_structure_scenarios',
    'cad_hybrid_acoustic_results',
    'cad_hybrid_prediction_provider_bindings',
    'cad_hybrid_prediction_provider_objectives',
    'cad_hybrid_prediction_providers',
    'cad_hybrid_stitching_policies',
    'cad_installation_contexts',
    'cad_installed_definition_bindings',
    'cad_installed_device_observations',
    'cad_installed_equipment_instances',
    'cad_installed_equipment_replacements',
    'cad_ir_analysis_results',
    'cad_ir_analysis_specs',
    'cad_joint_candidate_evaluations',
    'cad_joint_candidate_selections',
    'cad_joint_candidates',
    'cad_joint_optimization_specs',
    'cad_layout_profiles',
    'cad_line_level_stages',
    'cad_materialized_pattern_points',
    'cad_measurement_plans',
    'cad_measurement_runner_plans',
    'cad_measurement_target_lineages',
    'cad_measurement_target_patterns',
    'cad_multi_seat_results',
    'cad_multi_seat_sets',
    'cad_multifidelity_finalizations',
    'cad_multifidelity_plans',
    'cad_multifidelity_screening_evaluations',
    'cad_multifidelity_stage_results',
    'cad_o90_robust_pareto_evaluations',
    'cad_perturbation_samples',
    'cad_playback_chain_evaluations',
    'cad_playback_chain_scenarios',
    'cad_prediction_provider_bindings',
    'cad_prediction_provider_objectives',
    'cad_prediction_providers',
    'cad_projector_spec_evidence',
    'cad_projector_specifications',
    'cad_proposal_objective_result_authorities',
    'cad_proposal_perturbation_samples',
    'cad_proposal_robust_pareto_evaluations',
    'cad_proposal_robustness_evaluations',
    'cad_proposal_robustness_specs',
    'cad_r110_compiled_source_models',
    'cad_r120_compiled_geometry',
    'cad_r120_leak_portal_diagnostics',
    'cad_r140_execution_attempts',
    'cad_r140_execution_cache',
    'cad_r140_execution_results',
    'cad_r140_execution_schedules',
    'cad_r140_execution_tasks',
    'cad_r140_gpu_authorities',
    'cad_r140_resource_estimates',
    'cad_robustness_evaluations',
    'cad_robustness_specs',
    'cad_robustness_validation_cases',
    'cad_robustness_validation_decisions',
    'cad_roomsim_batch_specs',
    'cad_roomsim_candidate_attempts',
    'cad_routing_profiles',
    'cad_speaker_electrical_loads',
    'cad_speaker_impedances',
    'cad_standards_evaluations',
    'cad_standards_observation_authorities',
    'cad_standards_profiles',
    'cad_standards_source_authorities',
    'cad_system_variant_as_built',
    'cad_system_variant_measured',
    'cad_system_variant_measurement_campaign_completions',
    'cad_system_variant_measurement_campaign_registrations',
    'cad_system_variant_measurement_campaigns',
    'cad_system_variant_measurement_plan_completions',
    'cad_system_variant_measurement_plans',
    'cad_timing_references',
    'cad_topology_comparison_bundles',
    'cad_topology_comparison_evaluations',
    'cad_topology_comparison_selections',
    'cad_topology_comparison_specs',
    'cad_topology_placement_candidates',
    'cad_topology_search_specs',
    'cad_topology_spaces',
    'cad_treatment_boundary_compositions',
    'cad_treatment_boundary_overlays',
    'cad_treatment_evidence_authorities',
    'cad_upgrade_adoptions',
    'cad_validation_campaign_registrations',
    'cad_validation_campaigns',
    'cad_video_geometry_evaluations',
    'cad_video_geometry_workspaces',
    'cad_wave_excitation_evidence_authorities',
    'cad_wave_source_excitation_bindings',
    'cad_wiring_checks',
    'editor_camera_states',
    'editor_named_views',
    'floor_plan_underlays',
    'r150_path_frequency_response_artifacts',
    'r160_numerical_hybrid_responses',
    'scene_recovery_snapshots',
    'seating_layout_specs',
)


def assert_row_integrity_registry_complete() -> None:
    """Raise when a canonical-payload table escapes both registries.

    The invariant: ``_ROW_BINDINGS ∪ _UNBOUND_PAYLOAD_TABLES`` covers
    exactly the tables the versioned DDL declares with a canonical
    payload column — no additions unaccounted for, no stale entries.
    """

    declared = canonical_payload_tables()
    bound = set(_ROW_BINDINGS)
    unbound = set(_UNBOUND_PAYLOAD_TABLES)
    problems: list[str] = []
    overlap = bound & unbound
    if overlap:
        problems.append(
            'tables registered as both bound and unbound: '
            + ', '.join(sorted(overlap))
        )
    # Bound tables may be declared by runtime schema-convergence
    # functions rather than NATIVE_BASELINE_DDL (capture_* families), so
    # membership in ``declared`` is required only of the unbound ledger.
    unknown_unbound = unbound - set(declared)
    if unknown_unbound:
        problems.append(
            'unbound-ledger tables with no canonical payload column: '
            + ', '.join(sorted(unknown_unbound))
        )
    missing = set(declared) - bound - unbound
    if missing:
        problems.append(
            'canonical-payload tables in neither registry: '
            + ', '.join(sorted(missing))
        )
    if problems:
        raise NativeRowIntegrityError(
            'row-integrity registry is incomplete: ' + '; '.join(problems)
        )


def scan_native_row_integrity(
    connection: sqlite3.Connection,
) -> tuple[RowPayloadDrift, ...]:
    """Audit every registered table's duplicated columns against payloads.

    Read-only. Tables absent from the database are skipped so the scan is
    safe on partially-migrated or minimal databases. A payload that is not
    valid JSON, or not a JSON object, is itself drift. Tables on the
    unbound ledger waive duplicated-column checks only — their canonical
    payload rows are still parsed, so ledger membership stays inventory
    rather than unverified coverage (#313).
    """

    previous_factory = connection.row_factory
    if previous_factory is not sqlite3.Row:
        connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        drifts: list[RowPayloadDrift] = []
        for table, (payload_column, bindings, extras) in _ROW_BINDINGS.items():
            if table not in tables:
                continue
            rows = connection.execute(
                f'SELECT * FROM {table} ORDER BY rowid ASC'
            )
            for row in rows:
                drifts.extend(
                    _scan_row(table, payload_column, bindings, extras, row)
                )
        # Ledger-declared tables waive duplicated-column checks, not
        # payload verification: every canonical payload row must still
        # parse as a JSON object or the row is reported as drift (#313).
        canonical = canonical_payload_tables()
        for table in _UNBOUND_PAYLOAD_TABLES:
            if table not in tables:
                continue
            payload_column = canonical.get(table)
            if payload_column is None:
                # Membership without a declared canonical column is the
                # completeness invariant's problem, not the scan's.
                continue
            rows = connection.execute(
                f'SELECT * FROM {table} ORDER BY rowid ASC'
            )
            for row in rows:
                drifts.extend(
                    _scan_row(table, payload_column, (), (), row)
                )
    finally:
        connection.row_factory = previous_factory
    return tuple(drifts)


def _scan_row(
    table: str,
    payload_column: str,
    bindings: tuple[RowBinding, ...],
    extras: tuple[ExtraCheck, ...],
    row: sqlite3.Row,
) -> list[RowPayloadDrift]:
    row_map = _row_mapping(row)
    drifts: list[RowPayloadDrift] = []
    identity = str(
        row_map.get('revision_id')
        or row_map.get('ingestion_run_id')
        or next(
            (
                row_map.get(key)
                for key in row_map
                if key.endswith('_id')
            ),
            row_map.get('seq', '?'),
        )
    )
    payload_text = row_map.get(payload_column)
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        drifts.append(
            RowPayloadDrift(
                table, identity, payload_column,
                '<unparseable payload>', None,
            )
        )
        return drifts
    if not isinstance(payload, Mapping):
        drifts.append(
            RowPayloadDrift(
                table, identity, payload_column,
                '<payload is not a JSON object>', None,
            )
        )
        return drifts
    for drift in row_payload_drifts(table, row_map, payload, bindings):
        drifts.append(
            RowPayloadDrift(
                table, identity, drift.column,
                drift.row_value, drift.payload_value,
            )
        )
    for check in extras:
        for drift in check(row_map, payload_text, payload):
            drifts.append(
                RowPayloadDrift(
                    table, identity, drift.column,
                    drift.row_value, drift.payload_value,
                )
            )
    return drifts


def verify_native_row_integrity(connection: sqlite3.Connection) -> None:
    """Fail closed when any registered row drifts from its payload."""

    drifts = scan_native_row_integrity(connection)
    if drifts:
        shown = '; '.join(drift.describe() for drift in drifts[:8])
        remaining = len(drifts) - min(len(drifts), 8)
        suffix = f'; …and {remaining} more' if remaining else ''
        raise NativeRowIntegrityError(
            f'native semantic integrity check failed: {shown}{suffix}'
        )
