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

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Mapping

from .cad_schema_ddl import NATIVE_BASELINE_DDL, NATIVE_SCHEMA_TABLES
from .canonical_json import canonical_sha256 as _canonical_sha256


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


def _connected_document_identity(
    row: RowMapping, payload_text: str, payload: Mapping[str, Any],
):
    # connected_document_id re-derives from the stored payload — the
    # canonical model dump is what the promotion path hashes (#763-style
    # identity binding for the connected-space table). Deferred import:
    # capture_connected_space does not load this module, but the domain
    # constant stays owned there.
    from .capture_connected_space import CONNECTED_DOC_DOMAIN

    expected = 'capture-connected-space:' + _canonical_sha256(
        {'domain': CONNECTED_DOC_DOMAIN, 'payload': payload}
    )
    if row.get('connected_document_id') != expected:
        yield RowPayloadDrift(
            'capture_connected_space_documents',
            str(row.get('connected_document_id')),
            'connected_document_id',
            row.get('connected_document_id'),
            expected,
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
    'cad_correction_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('correction_subject_id', 'subject', 'subject_id'),
            _b('correction_subject_sha256', 'subject', 'subject_sha256'),
            _b('state', 'state'),
            _b('scope', 'scope'),
        ),
        (),
    ),
    'cad_measurement_uncertainty_budgets': (
        'payload_json',
        (
            _b('budget_id', 'budget_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('measurement_id', 'measurement_id', optional=True),
            _b('dataset_id', 'dataset_id', optional=True),
        ),
        (),
    ),
    'cad_measurement_significance_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_kind'),
            _b('subject_ref_id', 'subject_ref_id', optional=True),
            _b('budget_id', 'budget_id'),
        ),
        (),
    ),
    'cad_measurement_state_policies': (
        'payload_json',
        (
            _b('policy_id', 'policy_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('name', 'name'),
        ),
        (),
    ),
    'cad_measurement_state_snapshots': (
        'payload_json',
        (
            _b('snapshot_id', 'snapshot_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('measurement_id', 'measurement_id'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_measurement_state_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_kind'),
            _b('state', 'state'),
            _b('policy_id', 'policy_id', optional=True),
        ),
        (),
    ),
    'cad_measurement_transforms': (
        'payload_json',
        (
            _b('transform_id', 'transform_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
        ),
        (),
    ),
    'cad_decision_rule_specs': (
        'payload_json',
        (
            _b('rule_id', 'rule_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('decision_type', 'decision_type'),
            _b('criterion_id', 'criterion_id'),
        ),
        (),
    ),
    'cad_decision_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('rule_id', 'rule_id'),
            _b('rule_sha256', 'rule_sha256'),
            _b('decision_type', 'decision_type'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_uncertain_input_sets': (
        'payload_json',
        (
            _b('input_set_id', 'input_set_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
            _b('scene_content_hash', 'scene_content_hash'),
            _b('model_ref', 'model_ref'),
        ),
        (),
    ),
    'cad_robust_design_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('document_id', 'document_id'),
            _b('input_set_id', 'input_set_id'),
            _b('input_set_sha256', 'input_set_sha256'),
            _b('propagation_spec_id', 'propagation_spec_id'),
            _b('propagation_spec_sha256', 'propagation_spec_sha256'),
        ),
        (),
    ),
    # #608: the stimulus registry rows duplicate sealed payload state.
    'cad_stimulus_assets': (
        'payload_json',
        (
            _b('stimulus_id', 'stimulus_id'),
            _b('stimulus_sha256', 'stimulus_sha256'),
            _b('document_id', 'document_id'),
            _b('origin_class', 'origin_class'),
            _b('subtype', 'subtype'),
            _b('content_sha256', 'content_sha256', optional=True),
            _b('registered_at_utc', 'registered_at_utc'),
        ),
        (),
    ),
    'cad_stimulus_pins': (
        'payload_json',
        (
            _b('pin_id', 'pin_id'),
            _b('pin_sha256', 'pin_sha256'),
            _b('document_id', 'document_id'),
            _b('measurement_ref', 'measurement_ref'),
            _b('stimulus_id', 'stimulus_id'),
            _b('stimulus_sha256', 'stimulus_sha256'),
            _b('pinned_at_utc', 'pinned_at_utc'),
        ),
        (),
    ),
    'cad_stimulus_eligibility': (
        'payload_json',
        (
            _b('eligibility_id', 'eligibility_id'),
            _b('eligibility_sha256', 'eligibility_sha256'),
            _b('document_id', 'document_id'),
            _b('procedure_id', 'procedure_id'),
            _b('stimulus_id', 'stimulus_id'),
            _b('stimulus_sha256', 'stimulus_sha256'),
            _b('verdict', 'verdict'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # #574: bass splice evidence + qualification verdict rows duplicate
    # sealed payload state.
    'cad_bass_splice_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('role_id', 'role_id'),
            _b('sub_group_id', 'sub_group_id'),
            _b('seat_id', 'seat_id'),
            _b('seat_role', 'seat_role'),
            _b('path', 'path'),
            _b('observed_state', 'observed_state'),
            _b('stimulus_pin_id', 'stimulus_pin_id', optional=True),
            _b('measurement_dataset_sha256',
               'measurement_dataset_sha256',
               optional=True),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_bass_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('lifecycle_at_evaluation', 'lifecycle_at_evaluation'),
            _b('status', 'status'),
            _b('scope', 'scope'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # #592: device snapshot/restore authority rows duplicate sealed
    # payload state.
    'cad_device_backup_artifacts': (
        'payload_json',
        (
            _b('artifact_id', 'artifact_id'),
            _b('artifact_sha256', 'artifact_sha256'),
            _b('content_sha256', 'content_sha256'),
            _b('device_equipment_id', 'device_equipment_id'),
            _b('manufacturer', 'manufacturer', optional=True),
            _b('model', 'model', optional=True),
            _b('firmware_version', 'firmware_version', optional=True),
            _b('backup_format', 'backup_format', optional=True),
            _b('privacy_class', 'privacy_class'),
            _b('captured_at_utc', 'captured_at_utc', optional=True),
        ),
        (),
    ),
    'cad_device_config_snapshots': (
        'payload_json',
        (
            _b('snapshot_id', 'snapshot_id'),
            _b('snapshot_sha256', 'snapshot_sha256'),
            _b('document_id', 'document_id'),
            _b('instance_id', 'instance_id'),
            _b('evidence_class', 'evidence_class'),
            _b('transition_kind', 'transition_kind'),
            _b('firmware_version', 'firmware_version', optional=True),
            _b('state_content_sha256', 'state_content_sha256'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_device_firmware_transitions': (
        'payload_json',
        (
            _b('transition_id', 'transition_id'),
            _b('transition_sha256', 'transition_sha256'),
            _b('document_id', 'document_id'),
            _b('instance_id', 'instance_id'),
            _b('from_firmware', 'from_firmware', optional=True),
            _b('to_firmware', 'to_firmware'),
            _b('migration_result', 'migration_result'),
            _b('rollback_status', 'rollback_status'),
            _b('updated_at_utc', 'updated_at_utc'),
        ),
        (),
    ),
    'cad_device_known_good_baselines': (
        'payload_json',
        (
            _b('baseline_id', 'baseline_id'),
            _b('baseline_sha256', 'baseline_sha256'),
            _b('document_id', 'document_id'),
            _b('instance_id', 'instance_id'),
            _b('snapshot_sha256', 'snapshot_sha256'),
            _b('promoted_at_utc', 'promoted_at_utc'),
        ),
        (),
    ),
    'cad_device_replacement_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('source_instance_id', 'source_instance_id'),
            _b('target_instance_id', 'target_instance_id'),
            _b('assessed_at_utc', 'assessed_at_utc'),
        ),
        (),
    ),
    'cad_device_restore_records': (
        'payload_json',
        (
            _b('restore_id', 'restore_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('target_instance_id', 'target_instance_id'),
            _b('artifact_sha256', 'artifact_sha256', optional=True),
            _b('source_snapshot_sha256',
               'source_snapshot_sha256',
               optional=True),
            _b('result_status', 'result_status'),
            _b('verdict', 'verdict'),
            _b('restored_at_utc', 'restored_at_utc'),
        ),
        (),
    ),
    # #599: external standards registry rows duplicate sealed payload
    # state. ``registry_key`` is a derived property (standard_id@edition),
    # not a payload field, so it is not bound.
    'cad_external_standard_documents': (
        'payload_json',
        (
            _b('standard_id', 'standard_id'),
            _b('edition', 'edition'),
            _b('document_number', 'document_number'),
            _b('publisher', 'publisher'),
            _b('lifecycle', 'lifecycle'),
            _b('admission', 'admission'),
            _b('rights', 'rights'),
            _b('replaced_by', 'replaced_by', optional=True),
            _b('registered_at_utc', 'registered_at_utc'),
            _b('document_sha', 'document_sha'),
        ),
        (),
    ),
    'cad_standard_evaluation_pins': (
        'payload_json',
        (
            _b('pin_id', 'pin_id'),
            _b('pin_sha256', 'pin_sha256'),
            _b('document_id', 'document_id'),
            _b('standard_id', 'standard_id'),
            _b('edition', 'edition'),
            _b('mapping_id', 'mapping_id', optional=True),
            _b('result', 'result'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_standard_lifecycle_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('standard_id', 'standard_id'),
            _b('edition', 'edition'),
            _b('claimed_lifecycle', 'claimed_lifecycle'),
            _b('source_tier', 'source_tier'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_standard_profile_mappings': (
        'payload_json',
        (
            _b('mapping_id', 'mapping_id'),
            _b('mapping_sha256', 'mapping_sha256'),
            _b('standard_id', 'standard_id'),
            _b('edition', 'edition'),
            _b('mapping_version', 'mapping_version'),
            _b('calculation_version', 'calculation_version'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_standard_revision_diffs': (
        'payload_json',
        (
            _b('diff_id', 'diff_id'),
            _b('diff_sha256', 'diff_sha256'),
            _b('from_standard_id', 'from_standard_id'),
            _b('from_edition', 'from_edition'),
            _b('to_standard_id', 'to_standard_id'),
            _b('to_edition', 'to_edition'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    # #581: spatial campaign designs/evaluations/capture bindings
    # duplicate sealed payload state.
    'cad_spatial_campaign_designs': (
        'payload_json',
        (
            _b('design_id', 'design_id'),
            _b('design_sha256', 'design_sha256'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id', optional=True),
            _b('scene_content_hash', 'scene_content_hash', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_spatial_campaign_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('design_id', 'design_id'),
            _b('design_sha256', 'design_sha256'),
            _b('document_id', 'document_id'),
            _b('state', 'state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_spatial_campaign_bindings': (
        'payload_json',
        (
            _b('binding_id', 'binding_id'),
            _b('binding_sha256', 'binding_sha256'),
            _b('design_id', 'design_id'),
            _b('design_sha256', 'design_sha256'),
            _b('document_id', 'document_id'),
            _b('point_id', 'point_id'),
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('deviation_m', 'deviation_m', optional=True),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    # #585: RP32 commissioning profile chain rows duplicate sealed payload
    # state.
    'cad_rp32_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document', 'document_id'),
            _b('publisher', 'document', 'publisher'),
            _b('revision', 'document', 'revision'),
            _b('source_access_kind',
               'document', 'source_access_kind'),
            _b('clause_mapping_state', 'clause_mapping_state'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_rp32_reconciliations': (
        'payload_json',
        (
            _b('reconciliation_id', 'reconciliation_id'),
            _b('reconciliation_sha256', 'reconciliation_sha256'),
            _b('document_id', 'document_id'),
            _b('state', 'state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_rp32_readiness': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('state', 'state'),
            _b('assessed_at_utc', 'assessed_at_utc'),
        ),
        (),
    ),
    'cad_rp32_verification_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('spatial_design_id', 'spatial_design_id', optional=True),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_rp32_verification_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('readiness_assessment_id', 'readiness_assessment_id'),
            _b('overall_state', 'overall_state'),
            _b('rp22_state', 'rp22_state'),
            _b('completed_at_utc', 'completed_at_utc'),
        ),
        (),
    ),
    'cad_rp32_reports': (
        'payload_json',
        (
            _b('report_id', 'report_id'),
            _b('report_sha256', 'report_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('plan_id', 'plan_id'),
            _b('record_id', 'record_id'),
            _b('overall_state', 'overall_state'),
            _b('generated_at_utc', 'generated_at_utc'),
        ),
        (),
    ),
    # #579/#588: RP22 profile and response-target rows duplicate sealed
    # payload state. ``profile_version`` on cad_rp22_evaluations is derived
    # from the pinned profile's edition (not an evaluation payload field),
    # so it is not bound.
    'cad_rp22_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_version', 'edition'),
            _b('profile_sha256', 'profile_sha256'),
            _b('registry_key', 'registry_key'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_rp22_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_response_targets': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('version', 'version'),
            _b('document_id', 'document_id'),
            _b('target_sha256', 'target_sha256'),
            _b('kind', 'kind'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_spectral_balance_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('profile_version', 'profile_version'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
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
    # #533: every duplicated column of the active-LF control rows is a
    # payload field duplicate — the repository stores the full sealed
    # plan/event documents in ``payload_json``.
    'cad_active_lf_control_plans': (
        'payload_json',
        (
            _b('document_id', 'document_id'),
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('representation', 'representation'),
            _b('lifecycle', 'lifecycle'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_active_lf_control_events': (
        'payload_json',
        (
            _b('document_id', 'document_id'),
            _b('plan_id', 'plan_id'),
            _b('from_plan_sha256', 'from_plan_sha256'),
            _b('to_plan_sha256', 'to_plan_sha256'),
            _b('from_lifecycle', 'from_lifecycle'),
            _b('to_lifecycle', 'to_lifecycle'),
            _b('event_kind', 'event_kind'),
            _b('evidence_ref', 'evidence_ref', optional=True),
            _b('actor', 'actor', optional=True),
            _b('recorded_at_utc', 'recorded_at_utc'),
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
    # Convergence/migration-installed canonical-payload tables — declared
    # by domain schema convergence (v6 chain) or an inline migration step,
    # so ``canonical_payload_tables`` (baseline DDL only) does not see
    # them; ``installed_payload_tables`` covers them.
    #
    # Every stored column of the adaptive-extended rows is a payload
    # duplicate — the repository read path already raises on any column
    # that disagrees with the payload it parses.
    'cad_adaptive_extended_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('extended_search_id', 'extended_search_id'),
            _b('candidate_id', 'candidate_id'),
            _b('objective_id', 'objective_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b(
                'supersedes_observation_sha256',
                'supersedes_observation_sha256',
            ),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_adaptive_extended_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('document_id', 'document_id'),
            _b('extended_search_id', 'extended_search_id'),
            _b('validation_id', 'validation_id'),
            _b('execution_scope', 'execution_scope'),
            _b('selected_candidate_id', 'selected_candidate_id'),
            _b('adaptive_extended_sha256', 'adaptive_extended_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    # kind/source_evidence_id are genuine payload duplicates; the
    # ingestion_run_id column is run scope the record model does not
    # carry (same convention as document_id on profile rows).
    'capture_roomplan_records': (
        'payload_json',
        (
            _b('kind', 'kind'),
            _b('source_evidence_id', 'source_evidence_id'),
        ),
        (),
    ),
    # lineage_digest, document_sha256 (digest of the originally submitted
    # bundle bytes) and staged_at_utc are storage-scope columns with no
    # payload counterpart; the primary key re-derives from the payload.
    'capture_connected_space_documents': (
        'payload_json',
        (),
        (_connected_document_identity,),
    ),
    'physical_space_models': (
        'payload_json',
        (
            _b('physical_space_model_id', 'physical_space_model_id'),
            _b('document_id', 'document_id'),
            _b('revision', 'revision'),
            _b('parent_model_id', 'parent_model_id'),
            _b('source_connected_document_id', 'source_connected_document_id'),
            _b('world_to_scene_authority_id', 'world_to_scene_authority_id'),
            _b('created_at_utc', 'created_at_utc'),
            _b('reason', 'reason'),
        ),
        (),
    ),
    # Repair-bundle rows flatten nested bundle members for search. The
    # persisted payload nests the same values (``source_raw_mesh`` is a
    # compact reference in newer rows, an embedded mesh in legacy ones —
    # both carry ``mesh_id``). ``raw_mesh_semantic_hash`` and
    # ``created_at_utc`` are insert-only derivatives with no payload path.
    'cad_raw_mesh_repair_bundles': (
        'payload_json',
        (
            _b('repaired_mesh_id', 'repaired_mesh', 'repaired_mesh_id'),
            _b(
                'repaired_mesh_semantic_hash',
                'repaired_mesh', 'semantic_hash_sha256',
            ),
            _b('raw_mesh_id', 'source_raw_mesh', 'mesh_id'),
            _b('repair_plan_id', 'repair_plan', 'plan_id'),
            _b(
                'repair_plan_semantic_hash',
                'repair_plan', 'semantic_hash_sha256',
            ),
            _b(
                'post_diagnostic_id',
                'post_repair_diagnostic', 'diagnostic_id',
            ),
            _b(
                'post_diagnostic_semantic_hash',
                'post_repair_diagnostic', 'semantic_hash_sha256',
            ),
        ),
        (),
    ),
    # Calibration and measurement convergence families — several columns
    # deliberately differ from the payload field name (noted per row).
    'cad_calibration_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
            _b('system_variant_id', 'system_variant_id'),
            _b('source_measurement_id', 'source_measurement_id'),
            _b('source_dataset_id', 'source_dataset_id'),
            # Row column keeps the short name; the payload field is
            # measurement_quality_report_id.
            _b('quality_report_id', 'measurement_quality_report_id'),
            _b('plan_semantic_sha256', 'plan_semantic_sha256'),
            _b('support_state', 'support_state'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_calibration_exports': (
        'payload_json',
        (
            _b('export_id', 'export_id'),
            _b('plan_id', 'calibration_plan_id'),
            _b(
                'exported_settings_semantic_sha256',
                'exported_settings_semantic_sha256',
            ),
            _b('adapter_id', 'adapter_id'),
            _b('adapter_version', 'adapter_version'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    # created_at_utc on this row is the *registration* timestamp written
    # by save_verification_plan, not the contract's own created_at_utc —
    # it has no payload counterpart and stays unbound.
    'cad_calibration_verification_plans': (
        'payload_json',
        (
            _b('verification_plan_id', 'verification_plan_id'),
            _b('plan_id', 'calibration_plan_id'),
            _b('export_id', 'exported_settings_id'),
            _b(
                'verification_semantic_sha256',
                'verification_semantic_sha256',
            ),
        ),
        (),
    ),
    'cad_calibration_verification_registrations': (
        'payload_json',
        (
            _b('registration_id', 'registration_id'),
            _b('registration_sha256', 'registration_sha256'),
            _b('verification_plan_id', 'verification_plan_id'),
            _b(
                'verification_plan_semantic_sha256',
                'verification_plan_semantic_sha256',
            ),
            _b('registered_at_utc', 'registered_at_utc'),
        ),
        (),
    ),
    # Column created_at_utc stores the completion's completed_at_utc.
    'cad_calibration_verification_completions': (
        'payload_json',
        (
            _b('completion_id', 'completion_id'),
            _b('completion_sha256', 'completion_sha256'),
            _b('verification_plan_id', 'verification_plan_id'),
            _b('result', 'result'),
            _b('created_at_utc', 'completed_at_utc'),
        ),
        (),
    ),
    'cad_calibration_lifecycle_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('plan_id', 'calibration_plan_id'),
            _b('state', 'state'),
            _b('event_semantic_sha256', 'event_semantic_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_measurement_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('measurement_id', 'measurement_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('source_kind', 'source_kind'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    # profile_sha256 lives on the nested CadMeasurementQualityProfile.
    'cad_measurement_quality_reports': (
        'payload_json',
        (
            _b('report_id', 'report_id'),
            _b('measurement_id', 'measurement_id'),
            _b('dataset_id', 'dataset_id'),
            _b('raw_asset_sha256', 'raw_asset_sha256'),
            _b('report_sha256', 'report_sha256'),
            _b('profile_sha256', 'profile', 'profile_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_measurement_lineage': (
        'payload_json',
        (
            _b('lineage_id', 'lineage_id'),
            _b('document_id', 'document_id'),
            _b('measurement_id', 'measurement_id'),
            _b(
                'supersedes_measurement_id',
                'supersedes_measurement_id',
            ),
            _b('selected_measurement_id', 'selected_measurement_id'),
            _b('lineage_sha256', 'lineage_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_dataset_level_references': (
        'payload_json',
        (
            _b('level_reference_id', 'level_reference_id'),
            _b('dataset_id', 'dataset_id'),
            _b('level_reference_sha256', 'level_reference_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_measurement_dispositions': (
        'payload_json',
        (
            _b('disposition_id', 'disposition_id'),
            _b('document_id', 'document_id'),
            _b('measurement_id', 'measurement_id'),
            _b('disposition', 'disposition'),
            _b('correction_id', 'correction_id'),
            _b('disposition_sha256', 'disposition_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_measurement_corrections': (
        'payload_json',
        (
            _b('correction_id', 'correction_id'),
            _b('document_id', 'document_id'),
            _b('measurement_id', 'measurement_id'),
            _b('dataset_id', 'dataset_id'),
            _b('dataset_sha256', 'dataset_sha256'),
            _b('correction_sha256', 'correction_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    # Runner timestamps keep different names in the payload:
    # started_at_utc -> started_at, created_at_utc -> created_at.
    'cad_measurement_runner_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('started_at_utc', 'started_at'),
        ),
        (),
    ),
    'cad_measurement_runner_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('run_id', 'run_id'),
            _b('cell_index', 'cell_index'),
            _b('status', 'status'),
            _b('created_at_utc', 'created_at'),
        ),
        (),
    ),
    # REV48 guided acceptance runs: index columns mirror the revision
    # payload exactly — the bindings make a divergence itself drift.
    'htdt_acceptance_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('revision', 'revision'),
            _b('gate_id', 'gate_id'),
            _b('status', 'status'),
            _b('run_sha256', 'run_sha256'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    # REV56-METRICS: #580 noise metric profiles/measurements/evaluations,
    # #605 STI profiles/measurements/predictions/assessments and #607
    # loudness profiles/measurements/observations/gain-states/matching —
    # every duplicated column is a payload field duplicate. The
    # row-only ``seat_count`` on dialogue assessments is intentionally
    # unbound (it counts payload entries, not payload state).
    'cad_room_noise_metric_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('metric_family', 'metric_family'),
            _b('standard_id', 'standard_id'),
            _b('standard_edition', 'standard_edition'),
            _b('status', 'status'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_background_noise_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('temporal_class', 'temporal_class'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_noise_criterion_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('metric_family', 'metric_family'),
            _b('applicability', 'applicability'),
            _b('rating_label', 'rating_label'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_speech_intelligibility_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('standard_id', 'standard_id'),
            _b('standard_edition', 'standard_edition'),
            _b('method', 'method'),
            _b('voice_class', 'voice_class'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_sti_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('method', 'method'),
            _b('noise_measurement_id', 'noise_measurement_id'),
            _b('sti_value', 'sti_value'),
            _b('applicability', 'applicability'),
            _b('seat_ref', 'path', 'seat_ref'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_sti_predictions': (
        'payload_json',
        (
            _b('prediction_id', 'prediction_id'),
            _b('prediction_sha256', 'prediction_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('method', 'method'),
            _b('model_version', 'model_version'),
            _b('validation_ref', 'validation_ref'),
            _b('noise_measurement_id', 'noise_measurement_id'),
            _b('sti_value', 'sti_value'),
            _b('predicted_at_utc', 'predicted_at_utc'),
        ),
        (),
    ),
    'cad_dialogue_intelligibility_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('worst_seat_label', 'worst_seat_label'),
            _b('assessed_at_utc', 'assessed_at_utc'),
        ),
        (),
    ),
    'cad_content_loudness_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('standard_id', 'standard_id'),
            _b('standard_edition', 'standard_edition'),
            _b('eligibility', 'eligibility'),
            _b('algorithm_version', 'algorithm_version'),
            _b('channel_config', 'channel_config'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_programme_loudness_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('source_class', 'source_class'),
            _b('channel_config', 'channel_config'),
            _b('integrated_loudness_lufs', 'integrated_loudness_lufs'),
            _b('true_peak_dbtp', 'true_peak_dbtp'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_normalization_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('source_class', 'source_class'),
            _b('mode', 'mode'),
            _b('target_lufs', 'target_lufs'),
            _b('applied_gain_db', 'applied_gain_db'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_playback_gain_states': (
        'payload_json',
        (
            _b('state_id', 'state_id'),
            _b('state_sha256', 'state_sha256'),
            _b('document_id', 'document_id'),
            _b('normalization_observation_id',
               'normalization_observation_id'),
            _b('master_volume_db', 'master_volume_db'),
            _b('measured_in_room_spl_db', 'measured_in_room_spl_db'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_loudness_matching_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('comparison_label', 'comparison_label'),
            _b('target_quantity', 'target_quantity'),
            _b('residual_mismatch_db', 'residual_mismatch_db'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    # REV56-ELEC (#593 electrical qualification, #597 as-built wiring
    # traceability): index columns mirror the sealed payload fields.
    'cad_electrical_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('document_id', 'document_id'),
            _b('scenario_sha256', 'scenario', 'scenario_sha256'),
            _b('equipment_sha256', 'scenario', 'equipment', 'semantic_sha256'),
            _b('verdict', 'verdict'),
            _b('capability_class', 'capability_class'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_physical_interconnects': (
        'payload_json',
        (
            _b('path_id', 'path_id'),
            _b('version', 'version'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id', optional=True),
            _b('scene_content_hash', 'scene_content_hash', optional=True),
            _b('path_class', 'path_class'),
            _b('evidence_state', 'evidence_state'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('recorded_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_wiring_verifications': (
        'payload_json',
        (
            _b('verification_id', 'verification_id'),
            _b('path_id', 'path_id'),
            _b('path_sha256', 'path_sha256'),
            _b('test_kind', 'test_kind'),
            _b('result', 'result'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('recorded_at_utc', 'verified_at_utc'),
        ),
        (),
    ),
    'cad_logical_physical_bindings': (
        'payload_json',
        (
            _b('binding_id', 'binding_id'),
            _b('document_id', 'document_id'),
            _b('logical_ref_kind', 'logical_ref_kind'),
            _b('logical_ref_id', 'logical_ref_id'),
            _b('path_id', 'path_id'),
            _b('path_sha256', 'path_sha256'),
            _b('semantic_sha256', 'semantic_sha256'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    # REV56-LIFECYCLE (#595 health/drift monitoring, #596 substitution
    # impact): index columns mirror the sealed payload fields.
    'cad_monitoring_declarations': (
        'payload_json',
        (
            _b('declaration_id', 'declaration_id'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('remote_allowed', 'authorization', 'remote_collection_allowed', optional=True),
            _b('declaration_sha256', 'declaration_sha256'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_lifecycle_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('domain', 'domain'),
            _b('kind', 'kind'),
            _b('collection_mode', 'collection_mode'),
            _b('observation_sha256', 'observation_sha256'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    # REV56-TRANSPORT: #582/#583/#591 rows duplicate sealed payload state.
    'cad_av_latency_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id', optional=True),
            _b('profile_kind', 'profile_kind'),
            _b('label', 'label'),
            _b('standard_ref', 'standard_ref', optional=True),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_av_latency_paths': (
        'payload_json',
        (
            _b('path_id', 'path_id'),
            _b('version', 'version'),
            _b('document_id', 'document_id'),
            _b('path_sha256', 'path_sha256'),
            _b('signal_path_id', 'signal_path_id', optional=True),
            _b('signal_path_version', 'signal_path_version',
               optional=True),
            _b('signal_path_sha256', 'signal_path_sha256',
               optional=True),
            _b('display_picture_mode', 'display_picture_mode',
               optional=True),
            _b('audio_route', 'audio_route', optional=True),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_av_latency_path_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('path_id', 'path_id'),
            _b('path_version', 'path_version'),
            _b('path_sha256', 'path_sha256'),
            _b('method', 'method'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_av_latency_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('path_id', 'path_id'),
            _b('path_version', 'path_version'),
            _b('path_sha256', 'path_sha256'),
            _b('profile_id', 'profile_id'),
            _b('verdict', 'verdict'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_hdmi_signal_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('width_px', 'width_px'),
            _b('height_px', 'height_px'),
            _b('refresh_hz', 'refresh_hz'),
            _b('hdcp_required', 'hdcp_required'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_hdmi_edid_artifacts': (
        'payload_json',
        (
            _b('artifact_id', 'artifact_id'),
            _b('artifact_sha256', 'artifact_sha256'),
            _b('document_id', 'document_id'),
            _b('signal_path_id', 'signal_path_id'),
            _b('signal_path_version', 'signal_path_version'),
            _b('signal_path_sha256', 'signal_path_sha256'),
            _b('interception_kind', 'interception_kind'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_hdmi_hdcp_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('signal_path_id', 'signal_path_id'),
            _b('signal_path_version', 'signal_path_version'),
            _b('signal_path_sha256', 'signal_path_sha256'),
            _b('negotiated_version', 'negotiated_version'),
            _b('auth_state', 'auth_state'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_change_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
            _b('event_sha256', 'event_sha256'),
            _b('occurred_at_utc', 'occurred_at_utc'),
        ),
        (),
    ),
    'cad_trend_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('metric_key', 'spec', 'metric_key'),
            _b('state', 'state'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('assessed_at_utc', 'assessed_at_utc'),
        ),
        (),
    ),
    'cad_symptom_episodes': (
        'payload_json',
        (
            _b('episode_id', 'episode_id'),
            _b('document_id', 'document_id'),
            _b('reason_state', 'reason_state'),
            _b('resolved', 'resolved'),
            _b('episode_sha256', 'episode_sha256'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    'cad_drift_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('dependency_status', 'dependency_status'),
            _b('operational_severity', 'operational_severity'),
            _b('evidence_certainty', 'evidence_certainty'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('assessed_at_utc', 'assessed_at_utc'),
        ),
        (),
    ),
    'cad_reverification_triggers': (
        'payload_json',
        (
            _b('trigger_id', 'trigger_id'),
            _b('document_id', 'document_id'),
            _b('assessment_id', 'assessment_id'),
            _b('action', 'action'),
            _b('trigger_sha256', 'trigger_sha256'),
            _b('decided_at_utc', 'decided_at_utc'),
        ),
        (),
    ),
    'cad_restore_confirmations': (
        'payload_json',
        (
            _b('confirmation_id', 'confirmation_id'),
            _b('document_id', 'document_id'),
            _b('restore_ref_kind', 'restore_ref', 'kind'),
            _b('restore_ref_id', 'restore_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('confirmation_sha256', 'confirmation_sha256'),
            _b('confirmed_at_utc', 'confirmed_at_utc'),
        ),
        (),
    ),
    'cad_substitution_proposals': (
        'payload_json',
        (
            _b('proposal_id', 'proposal_id'),
            _b('document_id', 'document_id'),
            _b('original_definition_id', 'original', 'definition_id'),
            _b('proposed_definition_id', 'proposed', 'definition_id'),
            _b('reason_kind', 'reason_kind'),
            _b('evidence_class', 'evidence_class'),
            _b('proposal_sha256', 'proposal_sha256'),
            _b('requested_at_utc', 'requested_at_utc'),
        ),
        (),
    ),
    'cad_change_impact_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('document_id', 'document_id'),
            _b('proposal_id', 'proposal_id'),
            _b('proposal_sha256', 'proposal_sha256'),
            _b('technical_verdict', 'technical_verdict'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('assessed_at_utc', 'assessed_at_utc'),
        ),
        (),
    ),
    'cad_substitution_decisions': (
        'payload_json',
        (
            _b('decision_id', 'decision_id'),
            _b('document_id', 'document_id'),
            _b('proposal_id', 'proposal_id'),
            _b('state', 'state'),
            _b('commercial_state', 'commercial_state'),
            _b('decision_sha256', 'decision_sha256'),
            _b('decided_at_utc', 'decided_at_utc'),
        ),
        (),
    ),
    'cad_asbuilt_reconciliations': (
        'payload_json',
        (
            _b('reconciliation_id', 'reconciliation_id'),
            _b('document_id', 'document_id'),
            _b('proposal_id', 'proposal_id'),
            _b('verdict', 'verdict'),
            _b('reconciliation_sha256', 'reconciliation_sha256'),
            _b('reconciled_at_utc', 'reconciled_at_utc'),
        ),
        (),
    ),
    'cad_equipment_schedule_records': (
        'payload_json',
        (
            _b('schedule_id', 'schedule_id'),
            _b('document_id', 'document_id'),
            _b('phase', 'phase'),
            _b(
                'supersedes_schedule_id',
                'supersedes_schedule_id',
                optional=True,
            ),
            _b('schedule_sha256', 'schedule_sha256'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    'cad_hdmi_link_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('signal_path_id', 'signal_path_id'),
            _b('signal_path_version', 'signal_path_version'),
            _b('signal_path_sha256', 'signal_path_sha256'),
            _b('link_mode', 'link_mode'),
            _b('negotiated_rate_gbps', 'negotiated_rate_gbps',
               optional=True),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_hdmi_verification_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('signal_path_id', 'signal_path_id'),
            _b('signal_path_version', 'signal_path_version'),
            _b('signal_path_sha256', 'signal_path_sha256'),
            _b('required_profile_id', 'required_profile_id'),
            _b('verdict', 'verdict'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_hdmi_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('signal_path_id', 'signal_path_id'),
            _b('signal_path_version', 'signal_path_version'),
            _b('signal_path_sha256', 'signal_path_sha256'),
            _b('required_profile_id', 'required_profile_id'),
            _b('theoretical_status', 'theoretical_status'),
            _b('verdict', 'verdict'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_rp28_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id', optional=True),
            _b('label', 'label'),
            _b('standard_id', 'standard_id'),
            _b('edition', 'edition', optional=True),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_network_av_paths': (
        'payload_json',
        (
            _b('path_id', 'path_id'),
            _b('version', 'version'),
            _b('path_sha256', 'path_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label', optional=True),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_network_media_flows': (
        'payload_json',
        (
            _b('flow_id', 'flow_id'),
            _b('flow_sha256', 'flow_sha256'),
            _b('document_id', 'document_id'),
            _b('path_id', 'path_id'),
            _b('path_version', 'path_version'),
            _b('provider_profile', 'provider_profile'),
            _b('delivery', 'delivery'),
            _b('clock_requirement', 'clock_requirement'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_network_transport_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('path_id', 'path_id'),
            _b('path_version', 'path_version'),
            _b('kind', 'kind'),
            _b('flow_id', 'flow_id', optional=True),
            _b('interface_ref', 'interface_ref', optional=True),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_network_timing_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('path_id', 'path_id'),
            _b('path_version', 'path_version'),
            _b('ptp_domain', 'ptp_domain', optional=True),
            _b('node_state', 'node_state'),
            _b('lock_state', 'lock_state'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_network_av_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('path_id', 'path_id'),
            _b('path_version', 'path_version'),
            _b('flow_id', 'flow_id'),
            _b('flow_sha256', 'flow_sha256'),
            _b('media_state', 'media_state'),
            _b('verdict', 'verdict'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV56-BUILDING: #576 inter-room isolation authorities, #589
    # mechanical-noise qualification, #590 seating/occupancy — every
    # mirrored column is a payload field duplicate.
    'cad_isolation_elements': (
        'payload_json',
        (
            _b('element_id', 'element_id'),
            _b('element_sha256', 'element_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('construction_class', 'construction_class'),
        ),
        (),
    ),
    'cad_interroom_scenarios': (
        'payload_json',
        (
            _b('scenario_id', 'scenario_id'),
            _b('scenario_sha256', 'scenario_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('source_region_id', 'source_region_id'),
            _b('receiving_region_id', 'receiving_region_id'),
            _b('construction_state', 'construction_state'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_interroom_field_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_id', 'scenario_id'),
            _b('scenario_sha256', 'scenario_sha256'),
            _b('method_profile', 'method_profile'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_isolation_calibrations': (
        'payload_json',
        (
            _b('calibration_id', 'calibration_id'),
            _b('calibration_sha256', 'calibration_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_id', 'scenario_id'),
            _b('model_ref', 'model_ref'),
        ),
        (),
    ),
    'cad_isolation_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_id', 'scenario_id'),
            _b('scenario_sha256', 'scenario_sha256'),
            _b('lifecycle_state', 'lifecycle_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_mechanical_noise_tests': (
        'payload_json',
        (
            _b('test_id', 'test_id'),
            _b('test_sha256', 'test_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('signal_type', 'stimulus', 'signal_type'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_rattle_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('test_id', 'test_id'),
            _b('test_sha256', 'test_sha256'),
            _b('kind', 'kind'),
            _b('localization_state', 'localization_state'),
            _b('detected_at_utc', 'detected_at_utc'),
        ),
        (),
    ),
    'cad_remediation_actions': (
        'payload_json',
        (
            _b('action_id', 'action_id'),
            _b('action_sha256', 'action_sha256'),
            _b('document_id', 'document_id'),
            _b('action_kind', 'action_kind'),
            _b('performed_at_utc', 'performed_at_utc'),
        ),
        (),
    ),
    'cad_mechanical_noise_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('overall_verdict', 'overall_verdict'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_seat_acoustic_models': (
        'payload_json',
        (
            _b('seat_model_id', 'seat_model_id'),
            _b('seat_model_sha256', 'seat_model_sha256'),
            _b('document_id', 'document_id'),
            _b('seat_entity_id', 'seat_entity_id'),
            _b('geometry_source', 'geometry', 'source'),
        ),
        (),
    ),
    'cad_occupancy_scenarios': (
        'payload_json',
        (
            _b('occupancy_scenario_id', 'occupancy_scenario_id'),
            _b('occupancy_sha256', 'occupancy_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('state', 'state'),
            _b('comparability_key', 'comparability_key'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_clearance_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('occupancy_scenario_id', 'occupancy_scenario_id'),
            _b('occupancy_scenario_sha256', 'occupancy_scenario_sha256'),
            _b('listener_ref', 'listener_ref'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_seating_commissioning_results': (
        'payload_json',
        (
            _b('result_id', 'result_id'),
            _b('result_sha256', 'result_sha256'),
            _b('document_id', 'document_id'),
            _b('verdict', 'verdict'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    # REV56-OPS: #598 security authority, #601 control-scenario
    # qualification and #602 safe-listening/test-exposure — every
    # duplicated column is a payload field duplicate. Row-only derived
    # columns (step_count on scenarios) are intentionally unbound.
    'cad_security_assets': (
        'payload_json',
        (
            _b('asset_id', 'asset_id'),
            _b('asset_sha256', 'asset_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('management_reachability', 'management_reachability'),
            _b('vendor_support_status', 'vendor_support_status'),
            _b('lifecycle_state', 'lifecycle_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_security_credentials': (
        'payload_json',
        (
            _b('credential_id', 'credential_id'),
            _b('credential_sha256', 'credential_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('account_ref', 'account_ref'),
            _b('kind', 'kind'),
            _b('scope', 'scope'),
            _b('default_credential_state', 'default_credential_state'),
            _b('state', 'state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_security_surfaces': (
        'payload_json',
        (
            _b('surface_id', 'surface_id'),
            _b('surface_sha256', 'surface_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('kind', 'kind'),
            _b('state', 'state'),
            _b('exposure_scope', 'exposure_scope'),
            _b('authentication_state', 'authentication_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_security_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('kind', 'kind'),
            _b('outcome', 'outcome'),
            _b('evidence_class', 'evidence_class'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_security_risks': (
        'payload_json',
        (
            _b('risk_id', 'risk_id'),
            _b('risk_sha256', 'risk_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind', optional=True),
            _b('subject_ref_id', 'subject_ref', 'ref_id', optional=True),
            _b('title', 'title'),
            _b('likelihood_class', 'likelihood_class'),
            _b('status', 'status'),
            _b('review_at_utc', 'review_at_utc', optional=True),
            _b('raised_at_utc', 'raised_at_utc'),
        ),
        (),
    ),
    'cad_remote_service_authorizations': (
        'payload_json',
        (
            _b('authorization_id', 'authorization_id'),
            _b('authorization_sha256', 'authorization_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_ref', 'kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('method', 'method'),
            _b('state', 'state'),
            _b('valid_until_utc', 'valid_until_utc', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_security_test_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
            _b('tool_provider', 'tool_provider'),
            _b('performed_at_utc', 'performed_at_utc'),
        ),
        (),
    ),
    'cad_access_reviews': (
        'payload_json',
        (
            _b('review_id', 'review_id'),
            _b('review_sha256', 'review_sha256'),
            _b('document_id', 'document_id'),
            _b('trigger', 'trigger'),
            _b('reviewer', 'reviewer'),
            _b('performed_at_utc', 'performed_at_utc'),
        ),
        (),
    ),
    'cad_security_reviews': (
        'payload_json',
        (
            _b('review_id', 'review_id'),
            _b('review_sha256', 'review_sha256'),
            _b('document_id', 'document_id'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_control_surfaces': (
        'payload_json',
        (
            _b('surface_id', 'surface_id'),
            _b('surface_sha256', 'surface_sha256'),
            _b('document_id', 'document_id'),
            _b('controller_ref_kind', 'controller_ref', 'kind', optional=True),
            _b('controller_ref_id', 'controller_ref', 'ref_id', optional=True),
            _b('controller_family', 'controller_family'),
            _b('program_identity', 'program_identity', optional=True),
            _b('program_version', 'program_version', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_control_scenarios': (
        'payload_json',
        (
            _b('scenario_id', 'scenario_id'),
            _b('scenario_sha256', 'scenario_sha256'),
            _b('document_id', 'document_id'),
            _b('surface_ref_id', 'surface_ref', 'ref_id', optional=True),
            _b('kind', 'kind'),
            _b('name', 'name'),
            _b(
                'failure_notification_required',
                'failure_notification_required',
            ),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_control_scenario_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('run_sha256', 'run_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_ref_id', 'scenario_ref', 'ref_id'),
            _b('scenario_sha256', 'scenario_ref', 'ref_sha256', optional=True),
            _b('outcome', 'outcome'),
            _b(
                'failure_notification_outcome',
                'failure_notification_outcome',
            ),
            _b('started_at_utc', 'started_at_utc'),
            _b('finished_at_utc', 'finished_at_utc', optional=True),
        ),
        (),
    ),
    'cad_control_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_ref_id', 'scenario_ref', 'ref_id'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_exposure_limits': (
        'payload_json',
        (
            _b('limit_id', 'limit_id'),
            _b('limit_sha256', 'limit_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('basis', 'basis'),
            _b('criterion', 'criterion'),
            _b('limit_level_db', 'limit_level_db'),
            _b('reference_window_s', 'reference_window_s', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_spl_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('scope', 'scope'),
            _b('capability_source', 'capability_source'),
            _b('source_ref_id', 'source_ref', 'ref_id', optional=True),
            _b(
                'max_continuous_db_spl', 'max_continuous_db_spl',
                optional=True,
            ),
            _b('max_peak_db_spl', 'max_peak_db_spl', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_test_exposure_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b(
                'planned_level_db_spl', 'planned_level_db_spl',
                optional=True,
            ),
            _b('planned_duration_s', 'planned_duration_s', optional=True),
            _b('occupancy', 'occupancy'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_exposure_gates': (
        'payload_json',
        (
            _b('gate_id', 'gate_id'),
            _b('gate_sha256', 'gate_sha256'),
            _b('document_id', 'document_id'),
            _b('assessment_ref_id', 'assessment_ref', 'ref_id'),
            _b('decision', 'decision'),
            _b('decided_by', 'decided_by'),
            _b('decided_at_utc', 'decided_at_utc'),
        ),
        (),
    ),
    'cad_exposure_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('limit_ref_id', 'limit_ref', 'ref_id', optional=True),
            _b('state', 'state'),
            _b('projected_dose_pct', 'projected_dose_pct', optional=True),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_ifc_import_artifacts': (
        'payload_json',
        (
            _b('artifact_id', 'artifact_id'),
            _b('artifact_sha256', 'artifact_sha256'),
            _b('document_id', 'document_id'),
            _b('file_name', 'file_name'),
            _b('schema_identifier', 'schema_identifier'),
            _b('imported_at_utc', 'imported_at_utc'),
        ),
        (),
    ),
    'cad_ifc_entity_mappings': (
        'payload_json',
        (
            _b('mapping_id', 'mapping_id'),
            _b('mapping_sha256', 'mapping_sha256'),
            _b('document_id', 'document_id'),
            _b('import_artifact_id', 'import_artifact_id'),
            _b('ifc_global_id', 'ifc_global_id'),
            _b('ifc_type', 'ifc_type'),
            _b('htdt_role', 'htdt_role'),
        ),
        (),
    ),
    'cad_ifc_revision_deltas': (
        'payload_json',
        (
            _b('delta_id', 'delta_id'),
            _b('delta_sha256', 'delta_sha256'),
            _b('document_id', 'document_id'),
            _b('prior_artifact_id', 'prior_artifact_id'),
            _b('new_artifact_id', 'new_artifact_id'),
            _b('reconciliation_state', 'reconciliation_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_ifc_intake_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('name', 'name'),
            _b('profile_version', 'profile_version'),
        ),
        (),
    ),
    'cad_ifc_intake_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('file_sha256', 'file_sha256'),
            _b('overall_state', 'overall_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_ifc_exports': (
        'payload_json',
        (
            _b('export_id', 'export_id'),
            _b('export_sha256', 'export_sha256'),
            _b('document_id', 'document_id'),
            _b('mode', 'mode'),
            _b('source_artifact_id', 'source_artifact_id'),
            _b('step_sha256', 'step_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_performance_fact_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('publisher', 'publisher'),
            _b('family', 'family'),
            _b('document_reference', 'document_reference'),
            _b('maturity_state', 'maturity_state'),
        ),
        (),
    ),
    'cad_performance_fact_products': (
        'payload_json',
        (
            _b('product_id', 'product_id'),
            _b('product_sha256', 'product_sha256'),
            _b('document_id', 'document_id'),
            _b('manufacturer', 'manufacturer'),
            _b('model', 'model'),
        ),
        (),
    ),
    'cad_performance_facts': (
        'payload_json',
        (
            _b('fact_id', 'fact_id'),
            _b('fact_sha256', 'fact_sha256'),
            _b('document_id', 'document_id'),
            _b('product_id', 'product_id'),
            _b('product_sha256', 'product_sha256'),
            _b('quantity_kind', 'quantity_kind'),
            _b('evidence_class', 'evidence_class'),
        ),
        (),
    ),
    'cad_performance_fact_imports': (
        'payload_json',
        (
            _b('import_id', 'import_id'),
            _b('import_sha256', 'import_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('extraction_state', 'extraction_state'),
            _b('imported_at_utc', 'imported_at_utc'),
        ),
        (),
    ),
    'cad_performance_fact_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('product_id', 'product_id'),
            _b('product_sha256', 'product_sha256'),
            _b('verdict', 'verdict'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_performance_fact_rebinds': (
        'payload_json',
        (
            _b('rebind_id', 'rebind_id'),
            _b('rebind_sha256', 'rebind_sha256'),
            _b('document_id', 'document_id'),
            _b('from_profile_id', 'from_profile_id'),
            _b('to_profile_id', 'to_profile_id'),
            _b('decided_at_utc', 'decided_at_utc'),
        ),
        (),
    ),
    # REV56-INFRA: #587 rack/power/thermal qualification
    'cad_rack_enclosures': (
        'payload_json',
        (
            _b('rack_id', 'rack_id'),
            _b('rack_sha256', 'rack_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('enclosure_kind', 'enclosure_kind'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_rack_devices': (
        'payload_json',
        (
            _b('device_id', 'device_id'),
            _b('device_sha256', 'device_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('rack_ref_id', 'rack_ref', 'ref_id'),
            _b('role', 'role'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_branch_circuits': (
        'payload_json',
        (
            _b('circuit_id', 'circuit_id'),
            _b('circuit_sha256', 'circuit_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('nominal_voltage_v', 'nominal_voltage_v', optional=True),
            _b('breaker_rating_a', 'breaker_rating_a', optional=True),
            _b('continuous_load_policy', 'continuous_load_policy'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_power_protection_devices': (
        'payload_json',
        (
            _b('protection_id', 'protection_id'),
            _b('protection_sha256', 'protection_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_poe_budgets': (
        'payload_json',
        (
            _b('poe_id', 'poe_id'),
            _b('poe_sha256', 'poe_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('standard', 'standard'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_infrastructure_scenarios': (
        'payload_json',
        (
            _b('scenario_id', 'scenario_id'),
            _b('scenario_sha256', 'scenario_sha256'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
            _b('name', 'name'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_rack_thermal_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('rack_ref_id', 'rack_ref', 'ref_id'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_infrastructure_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_ref_id', 'scenario_ref', 'ref_id'),
            _b('overall_state', 'overall_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV56-INFRA: #603 immersive render-path qualification
    'cad_immersive_contents': (
        'payload_json',
        (
            _b('content_id', 'content_id'),
            _b('content_sha256', 'content_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('metadata_class', 'metadata_class'),
            _b('format_label', 'format_label', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_renderer_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('model_label', 'model_label'),
            _b('capability_source', 'capability_source'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_speaker_layouts': (
        'payload_json',
        (
            _b('layout_id', 'layout_id'),
            _b('layout_sha256', 'layout_sha256'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
            _b('label', 'label', optional=True),
            _b('evidence', 'evidence'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_render_sessions': (
        'payload_json',
        (
            _b('session_id', 'session_id'),
            _b('session_sha256', 'session_sha256'),
            _b('document_id', 'document_id'),
            _b('content_ref_id', 'content_ref', 'ref_id'),
            _b('decoder_mode', 'decoder_mode'),
            _b('upmixer_state', 'upmixer_state'),
            _b('started_at_utc', 'started_at_utc'),
        ),
        (),
    ),
    'cad_render_output_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('capture_method', 'capture_method'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_render_path_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('overall_state', 'overall_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV56-INFRA: #606 hum/buzz grounding-EMC diagnosis
    'cad_electrical_noise_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('symptom', 'symptom'),
            _b('instrument', 'instrument'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_audio_interconnects': (
        'payload_json',
        (
            _b('interconnect_id', 'interconnect_id'),
            _b('interconnect_sha256', 'interconnect_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('interface_class', 'interface_class'),
            _b('shield_termination', 'shield_termination'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_noise_isolation_tests': (
        'payload_json',
        (
            _b('test_id', 'test_id'),
            _b('test_sha256', 'test_sha256'),
            _b('document_id', 'document_id'),
            _b('observation_ref_id', 'observation_ref', 'ref_id'),
            _b('performed_at_utc', 'performed_at_utc'),
        ),
        (),
    ),
    'cad_humbuzz_diagnostics': (
        'payload_json',
        (
            _b('diagnostic_id', 'diagnostic_id'),
            _b('diagnostic_sha256', 'diagnostic_sha256'),
            _b('document_id', 'document_id'),
            _b('classification', 'classification'),
            _b('hypothesis_state', 'hypothesis_state'),
            _b('recommendation', 'recommendation'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_noise_mitigations': (
        'payload_json',
        (
            _b('attempt_id', 'attempt_id'),
            _b('attempt_sha256', 'attempt_sha256'),
            _b('document_id', 'document_id'),
            _b('diagnostic_ref', 'diagnostic_ref'),
            _b('kind', 'kind'),
            _b('outcome', 'outcome'),
            _b('performed_at_utc', 'performed_at_utc'),
        ),
        (),
    ),
    'cad_humbuzz_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('diagnostic_ref', 'diagnostic_ref'),
            _b('state', 'state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-METRO: #609 timebase / clock authority
    'cad_timebase_clock_domains': (
        'payload_json',
        (
            _b('clock_domain_id', 'clock_domain_id'),
            _b('clock_domain_sha256', 'clock_domain_sha256'),
            _b('document_id', 'document_id'),
            _b('domain_kind', 'domain_kind'),
            _b('device_identity', 'device_identity', optional=True),
            _b('nominal_sample_rate_hz', 'nominal_sample_rate_hz',
               optional=True),
            _b('effective_sample_rate_hz', 'effective_sample_rate_hz',
               optional=True),
            _b('common_clock_group', 'common_clock_group', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_measurement_timebases': (
        'payload_json',
        (
            _b('timebase_id', 'timebase_id'),
            _b('timebase_sha256', 'timebase_sha256'),
            _b('document_id', 'document_id'),
            _b('topology', 'topology'),
            _b('topology_evidence', 'topology_evidence'),
            _b('sequential_anchor', 'sequential_anchor'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_timebase_capability_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('timebase_ref_id', 'timebase_ref', 'ref_id'),
            _b('timing_uncertainty_s', 'timing_uncertainty_s',
               optional=True),
            _b('drift_material', 'drift_material', optional=True),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-METRO: #610 evidence bundle / integrity manifest
    'cad_evidence_bundles': (
        'payload_json',
        (
            _b('bundle_id', 'bundle_id'),
            _b('bundle_sha256', 'bundle_sha256'),
            _b('document_id', 'document_id'),
            _b('purpose', 'purpose'),
            _b('status', 'status'),
            _b('completeness_profile', 'completeness_profile'),
            _b('reproducibility_level', 'reproducibility_level'),
            _b('producer_software', 'producer_software'),
            _b('producer_version', 'producer_version'),
            _b('manifest_root_sha256', 'manifest_root_sha256',
               optional=True),
            _b('created_at_utc', 'created_at_utc'),
            _b('finalized_at_utc', 'finalized_at_utc', optional=True),
        ),
        (),
    ),
    'cad_evidence_artifacts': (
        'payload_json',
        (
            _b('artifact_id', 'artifact_id'),
            _b('artifact_sha256', 'artifact_sha256'),
            _b('document_id', 'document_id'),
            _b('bundle_id', 'bundle_id'),
            _b('logical_role', 'logical_role'),
            _b('artifact_class', 'artifact_class'),
            _b('inclusion', 'inclusion'),
            _b('package_path', 'package_path', optional=True),
            _b('required', 'required'),
            _b('rights_sensitivity', 'rights_sensitivity'),
            _b('digest', 'digest', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_evidence_derivation_edges': (
        'payload_json',
        (
            _b('edge_id', 'edge_id'),
            _b('edge_sha256', 'edge_sha256'),
            _b('document_id', 'document_id'),
            _b('bundle_id', 'bundle_id'),
            _b('operation', 'operation'),
            _b('software_identity', 'software_identity'),
            _b('output_artifact_id', 'output_artifact_id'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_evidence_attestations': (
        'payload_json',
        (
            _b('attestation_id', 'attestation_id'),
            _b('attestation_sha256', 'attestation_sha256'),
            _b('document_id', 'document_id'),
            _b('bundle_ref_id', 'bundle_ref', 'ref_id'),
            _b('signer_identity', 'signer_identity'),
            _b('role', 'role'),
            _b('signed_at_utc', 'signed_at_utc'),
        ),
        (),
    ),
    'cad_evidence_bundle_validations': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('bundle_ref_id', 'bundle_ref', 'ref_id'),
            _b('profile', 'profile'),
            _b('state', 'state'),
            _b('validation_version', 'validation_version'),
            _b('validated_at_utc', 'validated_at_utc'),
        ),
        (),
    ),
    # REV57-METRO: #611 instrument calibration lifecycle
    'cad_instrument_instances': (
        'payload_json',
        (
            _b('instrument_id', 'instrument_id'),
            _b('instrument_sha256', 'instrument_sha256'),
            _b('document_id', 'document_id'),
            _b('category', 'category'),
            _b('serial_or_instance_id', 'serial_or_instance_id'),
            _b('service_state', 'service_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_calibration_events': (
        'payload_json',
        (
            _b('calibration_id', 'calibration_id'),
            _b('calibration_sha256', 'calibration_sha256'),
            _b('document_id', 'document_id'),
            _b('instrument_ref_id', 'instrument_ref', 'ref_id'),
            _b('event_kind', 'event_kind'),
            _b('performed_at_utc', 'performed_at_utc'),
            _b('provider_or_lab', 'provider_or_lab', optional=True),
            _b('traceability_class', 'traceability_class'),
            _b('valid_until_utc', 'valid_until_utc', optional=True),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    'cad_calibration_interval_policies': (
        'payload_json',
        (
            _b('policy_id', 'policy_id'),
            _b('policy_sha256', 'policy_sha256'),
            _b('document_id', 'document_id'),
            _b('instrument_ref_id', 'instrument_ref', 'ref_id',
               optional=True),
            _b('instrument_category', 'instrument_category',
               optional=True),
            _b('basis', 'basis'),
            _b('nominal_interval_days', 'nominal_interval_days',
               optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_instrument_verification_checks': (
        'payload_json',
        (
            _b('check_id', 'check_id'),
            _b('check_sha256', 'check_sha256'),
            _b('document_id', 'document_id'),
            _b('instrument_ref_id', 'instrument_ref', 'ref_id'),
            _b('kind', 'kind'),
            _b('outcome', 'outcome'),
            _b('campaign_id', 'campaign_id', optional=True),
            _b('performed_at_utc', 'performed_at_utc'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    'cad_instrument_service_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('instrument_ref_id', 'instrument_ref', 'ref_id'),
            _b('kind', 'kind'),
            _b('occurred_at_utc', 'occurred_at_utc'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    'cad_instrument_fitness_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('instrument_ref_id', 'instrument_ref', 'ref_id'),
            _b('at_utc', 'at_utc'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_out_of_tolerance_reviews': (
        'payload_json',
        (
            _b('review_id', 'review_id'),
            _b('review_sha256', 'review_sha256'),
            _b('document_id', 'document_id'),
            _b('instrument_ref_id', 'instrument_ref', 'ref_id'),
            _b('triggering_ref_id', 'triggering_ref', 'ref_id'),
            _b('last_known_valid_at_utc', 'last_known_valid_at_utc',
               optional=True),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    # ---- REV57-PHYS: #613 geometry survey --------------------------
    'cad_geo_survey_instruments': (
        'payload_json',
        (
            _b('instrument_id', 'instrument_id'),
            _b('instrument_sha256', 'instrument_sha256'),
            _b('kind', 'kind'),
            _b('capability_class', 'capability_class'),
            _b('manufacturer', 'manufacturer', optional=True),
            _b('model', 'model', optional=True),
        ),
        (),
    ),
    'cad_geo_survey_campaigns': (
        'payload_json',
        (
            _b('campaign_id', 'campaign_id'),
            _b('campaign_sha256', 'campaign_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_geo_element_evidence': (
        'payload_json',
        (
            _b('element_id', 'element_id'),
            _b('element_sha256', 'element_sha256'),
            _b('document_id', 'document_id'),
            _b('element_key', 'element_key'),
            _b('observation_state', 'observation_state'),
            _b('derivation_stage', 'derivation_stage'),
        ),
        (),
    ),
    'cad_geo_control_measurements': (
        'payload_json',
        (
            _b('control_id', 'control_id'),
            _b('control_sha256', 'control_sha256'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
            _b('campaign_id', 'campaign_id', optional=True),
            _b('instrument_id', 'instrument_id', optional=True),
        ),
        (),
    ),
    'cad_geo_reconciliations': (
        'payload_json',
        (
            _b('reconciliation_id', 'reconciliation_id'),
            _b('reconciliation_sha256', 'reconciliation_sha256'),
            _b('document_id', 'document_id'),
            _b('element_key', 'element_key'),
            _b('approved_change', 'approved_change'),
            _b('reconciled_at_utc', 'reconciled_at_utc'),
        ),
        (),
    ),
    'cad_geo_task_requirements': (
        'payload_json',
        (
            _b('task_id', 'task_id'),
            _b('task_sha256', 'task_sha256'),
            _b('document_id', 'document_id'),
            _b('task_class', 'task_class'),
            _b('tolerance_mm', 'tolerance_mm', optional=True),
        ),
        (),
    ),
    'cad_geo_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('element_count', 'element_count'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # ---- REV57-PHYS: #614 installed-source boundary ----------------
    'cad_src_meas_conditions': (
        'payload_json',
        (
            _b('condition_id', 'condition_id'),
            _b('condition_sha256', 'condition_sha256'),
            _b('document_id', 'document_id'),
            _b('source_dataset_id', 'source_dataset_id'),
            _b('environment', 'environment'),
            _b('evidence_class', 'evidence_class'),
            _b('includes_installed_boundary', 'includes_installed_boundary'),
        ),
        (),
    ),
    'cad_src_mounting_conditions': (
        'payload_json',
        (
            _b('mounting_id', 'mounting_id'),
            _b('mounting_sha256', 'mounting_sha256'),
            _b('document_id', 'document_id'),
            _b('source_ref', 'source_ref'),
            _b('kind', 'kind'),
            _b('rear_cavity', 'rear_cavity'),
            _b('declared_by', 'declared_by'),
        ),
        (),
    ),
    'cad_src_boundary_corrections': (
        'payload_json',
        (
            _b('correction_id', 'correction_id'),
            _b('correction_sha256', 'correction_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('kind', 'kind'),
            _b('model_identity', 'model_identity'),
            _b('model_version', 'model_version'),
            _b('domain', 'domain'),
        ),
        (),
    ),
    'cad_src_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('mounting_id', 'mounting_id'),
            _b('mounting_sha256', 'mounting_sha256'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_src_boundary_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('source_dataset_id', 'source_dataset_id'),
            _b('mounting_id', 'mounting_id'),
            _b('mounting_sha256', 'mounting_sha256'),
            _b('state', 'state'),
            _b('achieved_capability', 'achieved_capability'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # ---- REV57-PHYS: #615 porous absorber --------------------------
    'cad_pam_parameter_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('material_ref', 'material_ref'),
            _b('quantity', 'quantity'),
            _b('evidence_class', 'evidence_class'),
            _b('method', 'method'),
        ),
        (),
    ),
    'cad_pam_material_models': (
        'payload_json',
        (
            _b('model_id', 'model_id'),
            _b('model_sha256', 'model_sha256'),
            _b('family', 'family'),
            _b('label', 'label'),
            _b('version', 'version'),
            _b('compute_capable', 'compute_capable'),
        ),
        (),
    ),
    'cad_pam_buildups': (
        'payload_json',
        (
            _b('buildup_id', 'buildup_id'),
            _b('buildup_sha256', 'buildup_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label', optional=True),
            _b('backing', 'backing'),
            _b('anisotropy', 'anisotropy'),
        ),
        (),
    ),
    'cad_pam_predictions': (
        'payload_json',
        (
            _b('prediction_id', 'prediction_id'),
            _b('prediction_sha256', 'prediction_sha256'),
            _b('document_id', 'document_id'),
            _b('model_id', 'model_id'),
            _b('model_sha256', 'model_sha256'),
            _b('buildup_id', 'buildup_id'),
            _b('buildup_sha256', 'buildup_sha256'),
            _b('eligibility', 'eligibility'),
            _b('evidence_class', 'evidence_class'),
            _b('computed_at_utc', 'computed_at_utc'),
        ),
        (),
    ),
    'cad_pam_fit_comparisons': (
        'payload_json',
        (
            _b('comparison_id', 'comparison_id'),
            _b('comparison_sha256', 'comparison_sha256'),
            _b('document_id', 'document_id'),
            _b('prediction_id', 'prediction_id', optional=True),
            _b('prediction_sha256', 'prediction_sha256', optional=True),
            _b('measured_evidence_ref', 'measured_evidence_ref'),
            _b('verdict', 'verdict'),
            _b('compared_at_utc', 'compared_at_utc'),
        ),
        (),
    ),
    # ---- REV57-DISP: #625 direct-view display ----------------------
    'cad_dv_display_states': (
        'payload_json',
        (
            _b('display_state_id', 'display_state_id'),
            _b('display_state_sha256', 'display_state_sha256'),
            _b('document_id', 'document_id'),
            _b('panel_technology', 'panel_technology'),
            _b('content_mode', 'content_mode'),
            _b('local_dimming', 'local_dimming'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_dv_stimulus_contexts': (
        'payload_json',
        (
            _b('stimulus_context_id', 'stimulus_context_id'),
            _b('stimulus_context_sha256', 'stimulus_context_sha256'),
            _b('document_id', 'document_id'),
            _b('stimulus_ref', 'stimulus_ref', optional=True),
            _b('field_kind', 'field_kind'),
            _b('window_size_percent', 'window_size_percent', optional=True),
            _b('apl_percent', 'apl_percent', optional=True),
            _b('content_kind', 'content_kind'),
        ),
        (),
    ),
    'cad_dv_photometric_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_id', 'display_state_id'),
            _b('display_state_sha256', 'display_state_sha256'),
            _b('stimulus_context_id', 'stimulus_context_id'),
            _b('stimulus_context_sha256', 'stimulus_context_sha256'),
            _b('quantity', 'quantity'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_dv_temporal_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_id', 'display_state_id'),
            _b('display_state_sha256', 'display_state_sha256'),
            _b('state', 'state'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_dv_spatial_measurements': (
        'payload_json',
        (
            _b('spatial_id', 'spatial_id'),
            _b('spatial_sha256', 'spatial_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_id', 'display_state_id'),
            _b('display_state_sha256', 'display_state_sha256'),
            _b('stimulus_context_id', 'stimulus_context_id'),
            _b('stimulus_context_sha256', 'stimulus_context_sha256'),
            _b('observable', 'observable'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_dv_angle_measurements': (
        'payload_json',
        (
            _b('angle_id', 'angle_id'),
            _b('angle_sha256', 'angle_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_id', 'display_state_id'),
            _b('display_state_sha256', 'display_state_sha256'),
            _b('stimulus_context_id', 'stimulus_context_id'),
            _b('stimulus_context_sha256', 'stimulus_context_sha256'),
            _b('horizontal_angle_deg', 'horizontal_angle_deg'),
            _b('vertical_angle_deg', 'vertical_angle_deg'),
            _b('seat_ref', 'seat_ref', optional=True),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_dv_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_id', 'display_state_id'),
            _b('display_state_sha256', 'display_state_sha256'),
            _b('state', 'state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # ---- REV57-DISP: #626 observer metamerism ----------------------
    'cad_om_spectral_states': (
        'payload_json',
        (
            _b('spectral_state_id', 'spectral_state_id'),
            _b('spectral_state_sha256', 'spectral_state_sha256'),
            _b('document_id', 'document_id'),
            _b('display_ref', 'display_ref'),
            _b('system_kind', 'system_kind'),
            _b('evidence_class', 'evidence_class'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_om_observer_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('kind', 'kind'),
            _b('revision', 'revision', optional=True),
            _b('observer_set', 'observer_set'),
        ),
        (),
    ),
    'cad_om_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('reference_state_id', 'reference_state_id'),
            _b('reference_state_sha256', 'reference_state_sha256'),
            _b('dut_state_id', 'dut_state_id'),
            _b('dut_state_sha256', 'dut_state_sha256'),
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('result_class', 'result_class'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_om_perceptual_matches': (
        'payload_json',
        (
            _b('match_id', 'match_id'),
            _b('match_sha256', 'match_sha256'),
            _b('document_id', 'document_id'),
            _b('reference_state_id', 'reference_state_id'),
            _b('dut_state_id', 'dut_state_id'),
            _b('observer_identity_class', 'observer_identity_class'),
            _b('observer_count', 'observer_count'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    'cad_om_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('goal', 'goal'),
            _b('verdict', 'verdict'),
            _b('reference_state_id', 'reference_state_id'),
            _b('dut_state_id', 'dut_state_id'),
            _b('evaluation_id', 'evaluation_id', optional=True),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # ---- REV57-DISP: #633 viewing environment ----------------------
    'cad_ve_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('room_ref', 'room_ref', optional=True),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_ve_geometry_observations': (
        'payload_json',
        (
            _b('geometry_id', 'geometry_id'),
            _b('geometry_sha256', 'geometry_sha256'),
            _b('document_id', 'document_id'),
            _b('observation_id', 'observation_id', optional=True),
            _b('observation_sha256', 'observation_sha256', optional=True),
            _b('seat_ref', 'seat_ref', optional=True),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_ve_lighting_scenes': (
        'payload_json',
        (
            _b('scene_id', 'scene_id'),
            _b('scene_sha256', 'scene_sha256'),
            _b('document_id', 'document_id'),
            _b('name', 'name'),
            _b('kind', 'kind'),
            _b('bound_observation_id', 'bound_observation_id',
               optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_ve_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('profile_kind', 'profile_kind'),
            _b('profile_scope', 'profile_scope'),
            _b('state', 'state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
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


_LIVE_PAYLOAD_RE = re.compile(
    r'\b(' + '|'.join(PAYLOAD_COLUMN_NAMES) + r')\b'
)


def _live_payload_tables(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    """``table -> canonical payload column`` from the live schema's DDL.

    Ground truth for what the database actually contains: tables the
    versioned migration chain installs outside ``NATIVE_BASELINE_DDL``
    (domain schema convergence, inline migration steps) show up here even
    though :func:`canonical_payload_tables` cannot parse them.
    """

    found: dict[str, str] = {}
    for name, sql in connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'"
    ):
        if not sql:
            continue
        match = _LIVE_PAYLOAD_RE.search(sql)
        if match is not None:
            found[str(name)] = match.group(1)
    return found


_INSTALLED_PAYLOAD_TABLES: dict[str, str] | None = None


def installed_payload_tables() -> dict[str, str]:
    """Every canonical-payload table the migration authority installs.

    ``canonical_payload_tables`` parses the baseline DDL only; the
    versioned chain also installs payload tables through the domain
    convergence functions (``capture_*``, ``cad_adaptive_extended_*``,
    ``physical_space_models``) and inline migration steps
    (``cad_raw_mesh_repair_bundles``). Running ``ensure_native_schema`` on
    a scratch database enumerates the union without duplicating their
    DDL. The result is cached — the installed set is constant per build.
    """

    global _INSTALLED_PAYLOAD_TABLES
    if _INSTALLED_PAYLOAD_TABLES is None:
        # Deferred: cad_schema pulls in native_backup, which imports this
        # module at top level — the call site runs long after that.
        from .cad_schema import connect_sqlite, ensure_native_schema

        with tempfile.TemporaryDirectory(
            prefix='htdt-integrity-schema-'
        ) as scratch:
            database = Path(scratch) / 'cad.sqlite3'
            ensure_native_schema(database)
            with closing(connect_sqlite(database)) as connection:
                _INSTALLED_PAYLOAD_TABLES = _live_payload_tables(connection)
    return _INSTALLED_PAYLOAD_TABLES


# Audited tables whose duplicated columns are not bound yet — either
# append-only rows whose columns are insert-only derivatives, or families
# not yet bound. Presence here is an explicit decision, not silence: a
# canonical-payload table absent from both registries fails the
# completeness invariant. Membership is inventory, not verification —
# ``scan_native_row_integrity`` still parses every row's canonical
# payload in these tables and reports unparseable or non-object payloads
# as drift; only duplicated-column comparisons are waived.
_UNBOUND_PAYLOAD_TABLES: tuple[str, ...] = (
    'assumption_decisions',
    'authoring_constraint_revisions',
    'authoring_constraint_sets',
    'cad_acoustic_geometry_derivations',
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
    'cad_adaptive_plans',
    'cad_ambient_comparisons',
    'cad_ambient_conditions',
    'cad_ambient_criteria',
    'cad_ambient_evaluations',
    'cad_ambient_profiles',
    'cad_ambient_reflectance_profiles',
    'cad_amplifier_electrical_limits',
    'cad_amplifier_output_capabilities',
    'cad_analysis_studies',
    'cad_applicability_attestations',
    'cad_auralization_artifacts',
    'cad_auralization_capabilities',
    'cad_auralization_listening_validations',
    'cad_auralization_render_specs',
    'cad_auralization_review_packages',
    'cad_auralization_routing_declarations',
    'cad_av_latency_measurements',
    'cad_av_sync_conditions',
    'cad_cable_runs',
    'cad_calibration_freezes',
    'cad_calibration_holdout_records',
    'cad_calibration_models',
    'cad_calibration_results',
    'cad_calibration_specs',
    'cad_commissioning_plans',
    'cad_commissioning_runs',
    'cad_color_measurement_sets',
    'cad_color_target_profiles',
    'cad_color_target_selections',
    'cad_compute_benchmarks',
    'cad_constraint_workspaces',
    'cad_coverage_evaluations',
    'cad_coverage_scenarios',
    'cad_current_topologies',
    'cad_data_source_registry',
    'cad_dataset_reviews',
    'cad_dependency_resolution_events',
    'cad_design_briefs',
    'cad_deterministic_ga_execution_inputs',
    'cad_deterministic_path_artifacts',
    'cad_device_action_acks',
    'cad_device_capability_snapshots',
    'cad_device_target_bindings',
    'cad_direct_level_evaluations',
    'cad_direct_level_scenarios',
    'cad_direct_view_evaluations',
    'cad_direct_view_specifications',
    'cad_directivity_datasets',
    'cad_drawing_set_specs',
    'cad_environment_profiles',
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
    'cad_field_evidence',
    'cad_field_evidence_records',
    'cad_field_explorer_sessions',
    'cad_field_label_sheets',
    'cad_field_labels',
    'cad_field_sessions',
    'cad_frequency_resolved_evaluations',
    'cad_gain_structure_evaluations',
    'cad_gain_structure_scenarios',
    'cad_hybrid_acoustic_results',
    'cad_hybrid_prediction_provider_bindings',
    'cad_hybrid_prediction_provider_objectives',
    'cad_hybrid_prediction_providers',
    'cad_hybrid_stitching_policies',
    'cad_importer_declarations',
    'cad_installation_contexts',
    'cad_installation_datums',
    'cad_installation_drawing_sets',
    'cad_installed_definition_bindings',
    'cad_installed_device_observations',
    'cad_installed_equipment_instances',
    'cad_installed_equipment_replacements',
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
    'cad_lighting_scene_selections',
    'cad_lighting_scenes',
    'cad_listener_poses',
    'cad_material_definitions',
    'cad_material_evidence',
    'cad_materialized_pattern_points',
    'cad_measurement_plans',
    'cad_measurement_pose_observations',
    'cad_measurement_runner_plans',
    'cad_measurement_target_lineages',
    'cad_measurement_target_patterns',
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
    'cad_observed_device_states',
    'cad_perturbation_samples',
    'cad_photometric_profile_selections',
    'cad_photometric_profiles',
    'cad_plan_target_bindings',
    'cad_planned_observed_deltas',
    'cad_playback_chain_evaluations',
    'cad_playback_chain_scenarios',
    'cad_playback_level_conditions',
    'cad_prediction_matrix_result_sets',
    'cad_prediction_matrix_runs',
    'cad_prediction_measurement_registrations',
    'cad_prediction_measurement_residual_reports',
    'cad_prediction_matrix_specs',
    'cad_prediction_provider_bindings',
    'cad_prediction_provider_objectives',
    'cad_prediction_providers',
    'cad_presentation_proposals',
    'cad_presentation_sessions',
    'cad_presentation_sync_bindings',
    'cad_project_boms',
    'cad_projector_spec_evidence',
    'cad_projector_specifications',
    'cad_proposal_objective_result_authorities',
    'cad_proposal_perturbation_samples',
    'cad_proposal_robust_pareto_evaluations',
    'cad_proposal_robustness_evaluations',
    'cad_proposal_robustness_specs',
    'cad_proposed_device_actions',
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
    'cad_rack_definitions',
    'cad_rack_layouts',
    'cad_raw_source_records',
    'cad_reconciliation_decisions',
    'cad_reference_playback_profiles',
    'cad_review_notes',
    'cad_robustness_evaluations',
    'cad_robustness_specs',
    'cad_robustness_validation_cases',
    'cad_robustness_validation_decisions',
    'cad_room_operating_states',
    'cad_roomsim_batch_specs',
    'cad_roomsim_candidate_attempts',
    'cad_routing_profiles',
    'cad_screen_optical_profiles',
    'cad_screen_optical_selections',
    'cad_screen_transfers',
    'cad_site_relationships',
    'cad_site_spaces',
    'cad_signal_path_selections',
    'cad_signal_paths',
    'cad_solver_capability_manifests',
    'cad_source_responses',
    'cad_source_review_decisions',
    'cad_speaker_datasets',
    'cad_speaker_definitions',
    'cad_speaker_electrical_loads',
    'cad_speaker_impedances',
    'cad_standards_evaluations',
    'cad_standards_observation_authorities',
    'cad_standards_profiles',
    'cad_standards_source_authorities',
    'cad_stimulus_profiles',
    'cad_stochastic_receiver_estimate_artifacts',
    'cad_system_variant_as_built',
    'cad_system_variant_measured',
    'cad_system_variant_measurement_campaign_completions',
    'cad_system_variant_measurement_campaign_registrations',
    'cad_system_variant_measurement_campaigns',
    'cad_system_variant_measurement_plan_completions',
    'cad_system_variant_measurement_plans',
    'cad_tactile_actuator_definitions',
    'cad_tactile_processing_profiles',
    'cad_tactile_profile_selections',
    'cad_target_curve_profiles',
    'cad_timing_references',
    'cad_tolerance_profiles',
    'cad_topology_comparison_bundles',
    'cad_topology_comparison_evaluations',
    'cad_topology_comparison_selections',
    'cad_topology_comparison_specs',
    'cad_topology_placement_candidates',
    'cad_topology_search_specs',
    'cad_topology_spaces',
    'cad_treatment_boundary_compositions',
    'cad_treatment_boundary_overlays',
    'cad_treatment_comparison_outcomes',
    'cad_treatment_evidence_authorities',
    'cad_usable_output_profiles',
    'cad_usable_output_selections',
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
    'cad_video_readiness_reports',
    'cad_visual_qa_verdicts',
    'cad_wave_excitation_evidence_authorities',
    'cad_wave_source_excitation_bindings',
    'cad_wiring_checks',
    'design_decisions',
    'editor_camera_states',
    'editor_named_views',
    'floor_plan_underlays',
    'project_action_items',
    'project_templates',
    'r150_path_frequency_response_artifacts',
    'r160_late_energy_decay_artifacts',
    'r160_numerical_hybrid_responses',
    'r160_stitched_hybrid_responses',
    'scene_recovery_snapshots',
    'seating_layout_specs',
    'template_instantiations',
)


def assert_row_integrity_registry_complete() -> None:
    """Raise when a canonical-payload table escapes both registries.

    The invariant: ``_ROW_BINDINGS ∪ _UNBOUND_PAYLOAD_TABLES`` covers
    exactly the tables the migration authority installs with a canonical
    payload column — no additions unaccounted for, no stale entries. The
    declared set is the migrated schema itself, so tables installed by
    domain convergence or inline migration DDL — not only the baseline —
    are held to the same rule.
    """

    declared = canonical_payload_tables() | installed_payload_tables()
    bound = set(_ROW_BINDINGS)
    unbound = set(_UNBOUND_PAYLOAD_TABLES)
    problems: list[str] = []
    overlap = bound & unbound
    if overlap:
        problems.append(
            'tables registered as both bound and unbound: '
            + ', '.join(sorted(overlap))
        )
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
        # The payload column comes from the live schema so ledger entries
        # for tables the baseline DDL does not declare (convergence- or
        # migration-installed) are still scanned.
        live = _live_payload_tables(connection)
        canonical = canonical_payload_tables()
        for table in _UNBOUND_PAYLOAD_TABLES:
            if table not in tables:
                continue
            payload_column = live.get(table) or canonical.get(table)
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
        # Registry completeness over the live schema: a declared htdt
        # table carrying a canonical payload column that is in neither
        # registry escapes row integrity entirely — the gap itself is
        # drift, reported like any other (#763 invariant, extended past
        # baseline-declared tables). Foreign tables (test probes, tool
        # markers) stay the authority audit's problem — its
        # unclassified-table path already fails closed on them.
        for table in sorted(
            set(live) & set(NATIVE_SCHEMA_TABLES)
            - set(_ROW_BINDINGS)
            - set(_UNBOUND_PAYLOAD_TABLES)
        ):
            drifts.append(
                RowPayloadDrift(
                    table,
                    '*',
                    '<registry coverage>',
                    '<canonical-payload table in neither registry>',
                    None,
                )
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
