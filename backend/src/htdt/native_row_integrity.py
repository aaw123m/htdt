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


def _list_count(
    table: str,
    column: str,
    list_key: str,
) -> ExtraCheck:
    """Verify a denormalized count column equals len(payload[list_key]).

    The count is row-mirrored payload state — tampering with it without
    touching the tuple drifts just like a renamed column would.
    """

    def check(
        row: RowMapping, payload_text: str, payload: Mapping[str, Any],
    ):
        entries = payload.get(list_key)
        expected = len(entries) if isinstance(entries, (list, tuple)) else None
        if expected is not None and row.get(column) != expected:
            yield RowPayloadDrift(
                table,
                str(row.get('seq')),
                column,
                row.get(column),
                expected,
            )

    return check


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
    # REV57-PROJ: #619 spatial projection-image qualification
    'cad_spatial_measurement_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('layout', 'layout'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_spatial_measurement_sets': (
        'payload_json',
        (
            _b('set_id', 'set_id'),
            _b('set_sha256', 'set_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('evidence_kind', 'evidence_kind'),
            _b('stimulus_profile', 'stimulus_profile'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_spatial_derived_maps': (
        'payload_json',
        (
            _b('map_id', 'map_id'),
            _b('map_sha256', 'map_sha256'),
            _b('document_id', 'document_id'),
            _b('source_set_ref_id', 'source_set_ref', 'ref_id'),
            _b('quantity', 'quantity'),
            _b('interpolation_algorithm', 'interpolation_algorithm'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_spatial_uniformity_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('set_ref_id', 'set_ref', 'ref_id'),
            _b('coverage_state', 'coverage_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-PROJ: #622 projection image-geometry / masking
    'cad_presentation_geometry_bindings': (
        'payload_json',
        (
            _b('binding_id', 'binding_id'),
            _b('binding_sha256', 'binding_sha256'),
            _b('document_id', 'document_id'),
            _b('projected_aspect', 'projected_aspect'),
            _b('content_aspect', 'content_aspect'),
            _b('keystone_state', 'keystone_state'),
            _b('anamorphic_state', 'anamorphic_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_image_geometry_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('binding_ref_id', 'binding_ref', 'ref_id'),
            _b('method', 'method'),
            _b('test_pattern_identity', 'test_pattern_identity'),
            _b('physical_alignment', 'physical_alignment'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_lens_memory_recalls': (
        'payload_json',
        (
            _b('recall_id', 'recall_id'),
            _b('recall_sha256', 'recall_sha256'),
            _b('document_id', 'document_id'),
            _b('memory_id', 'memory_id'),
            _b('cycle_index', 'cycle_index'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_geometry_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('binding_ref_id', 'binding_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('physical_alignment', 'physical_alignment'),
            _b('digital_correction_state', 'digital_correction_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-PROJ: #624 projector hush-box / enclosure co-design
    'cad_projector_install_constraints': (
        'payload_json',
        (
            _b('constraint_id', 'constraint_id'),
            _b('constraint_sha256', 'constraint_sha256'),
            _b('document_id', 'document_id'),
            _b('manufacturer', 'manufacturer'),
            _b('model', 'model'),
            _b('source_document', 'source_document'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_projector_enclosure_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('constraint_ref_id', 'constraint_ref', 'ref_id',
               optional=True),
            _b('remote_projection', 'remote_projection'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_enclosure_operating_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('scenario', 'scenario'),
            _b('duration_s', 'duration_s'),
            _b('projector_fan_state', 'projector_fan_state'),
            _b('protection_event', 'protection_event'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_enclosure_acoustic_observations': (
        'payload_json',
        (
            _b('acoustic_id', 'acoustic_id'),
            _b('acoustic_sha256', 'acoustic_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('comparability', 'comparability'),
            _b('pre_spl_db', 'pre_spl_db'),
            _b('post_spl_db', 'post_spl_db'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_enclosure_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('thermal_state', 'thermal_state'),
            _b('acoustic_state', 'acoustic_state'),
            _b('optical_state', 'optical_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-PROJ: #627 projector optical-radiation safety
    'cad_projector_safety_identities': (
        'payload_json',
        (
            _b('identity_id', 'identity_id'),
            _b('identity_sha256', 'identity_sha256'),
            _b('document_id', 'document_id'),
            _b('illumination_source', 'illumination_source'),
            _b('risk_group', 'risk_group'),
            _b('laser_class', 'laser_class'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_manufacturer_safety_constraints': (
        'payload_json',
        (
            _b('constraint_id', 'constraint_id'),
            _b('constraint_sha256', 'constraint_sha256'),
            _b('document_id', 'document_id'),
            _b('safety_identity_ref_id', 'safety_identity_ref', 'ref_id',
               optional=True),
            _b('source_document', 'source_document'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_projector_placements': (
        'payload_json',
        (
            _b('placement_id', 'placement_id'),
            _b('placement_sha256', 'placement_sha256'),
            _b('document_id', 'document_id'),
            _b('safety_identity_ref_id', 'safety_identity_ref', 'ref_id'),
            _b('operating_state', 'operating_state'),
            _b('throw_distance_m', 'throw_distance_m'),
            _b('viewer_position', 'viewer_position'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_optical_safety_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('placement_ref_id', 'placement_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
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
    # REV57-AUD: #621 acoustic channel-identity / polarity verification
    'cad_channel_identity_chains': (
        'payload_json',
        (
            _b('chain_id', 'chain_id'),
            _b('chain_sha256', 'chain_sha256'),
            _b('document_id', 'document_id'),
            _b('logical_channel', 'logical_channel'),
            _b('channel_class', 'channel_class'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_acoustic_endpoint_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
            _b('confidence', 'confidence'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_channel_identity_tests': (
        'payload_json',
        (
            _b('test_id', 'test_id'),
            _b('test_sha256', 'test_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_ref_id', 'chain_ref', 'ref_id'),
            _b('stimulus_class', 'stimulus', 'stimulus_class'),
            _b('tested_at_utc', 'tested_at_utc'),
        ),
        (),
    ),
    'cad_polarity_verification_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_ref_id', 'chain_ref', 'ref_id'),
            _b('physical_wiring_state', 'physical_wiring_state'),
            _b('dsp_polarity_state', 'dsp_polarity_state'),
            _b('acoustic_polarity_state', 'acoustic_polarity_state'),
            _b('policy_acceptance', 'policy_acceptance'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_channel_identity_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_ref_id', 'chain_ref', 'ref_id'),
            _b('logical_channel', 'logical_channel'),
            _b('verdict', 'verdict'),
            _b('reconciliation', 'reconciliation'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-AUD: #634 listener-area coverage / acoustic-aim
    'cad_acoustic_aim_states': (
        'payload_json',
        (
            _b('aim_id', 'aim_id'),
            _b('aim_sha256', 'aim_sha256'),
            _b('document_id', 'document_id'),
            _b('speaker_entity_id', 'speaker_entity_id'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_coverage_listener_areas': (
        'payload_json',
        (
            _b('area_id', 'area_id'),
            _b('area_sha256', 'area_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_coverage_predictions': (
        'payload_json',
        (
            _b('prediction_id', 'prediction_id'),
            _b('prediction_sha256', 'prediction_sha256'),
            _b('document_id', 'document_id'),
            _b('area_ref_id', 'area_ref', 'ref_id'),
            _b('quantity', 'quantity'),
            _b('summation_model', 'summation_model'),
            _b('predicted_at_utc', 'predicted_at_utc'),
        ),
        (),
    ),
    'cad_coverage_measurement_sets': (
        'payload_json',
        (
            _b('set_id', 'set_id'),
            _b('set_sha256', 'set_sha256'),
            _b('document_id', 'document_id'),
            _b('area_ref_id', 'area_ref', 'ref_id'),
            _b('quantity', 'quantity'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_coverage_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('area_ref_id', 'area_ref', 'ref_id'),
            _b('coverage_state', 'coverage_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-AUD: #628 installed loudspeaker instance variation
    'cad_instance_acoustic_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('instance_ref_id', 'instance_ref', 'ref_id',
               optional=True),
            _b('evidence_level', 'evidence_level'),
            _b('evidence_source', 'evidence_source'),
            _b('measurement_domain', 'measurement_domain'),
            _b('measured_at_utc', 'measured_at_utc', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_model_instance_deltas': (
        'payload_json',
        (
            _b('delta_id', 'delta_id'),
            _b('delta_sha256', 'delta_sha256'),
            _b('document_id', 'document_id'),
            _b('reference_evidence_ref_id', 'reference_evidence_ref',
               'ref_id'),
            _b('instance_evidence_ref_id', 'instance_evidence_ref',
               'ref_id'),
            _b('quantity', 'quantity'),
            _b('max_delta_db', 'max_delta_db', optional=True),
            _b('algorithm', 'algorithm'),
            _b('derived_at_utc', 'derived_at_utc'),
        ),
        (),
    ),
    'cad_matched_set_declarations': (
        'payload_json',
        (
            _b('set_id', 'set_id'),
            _b('set_sha256', 'set_sha256'),
            _b('document_id', 'document_id'),
            _b('role', 'role'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_matched_set_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('set_ref_id', 'set_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-AUD: #632 media-playback capability qualification
    'cad_playback_stack_identities': (
        'payload_json',
        (
            _b('stack_id', 'stack_id'),
            _b('stack_sha256', 'stack_sha256'),
            _b('document_id', 'document_id'),
            _b('source_class', 'source_class'),
            _b('device_identity', 'device_identity'),
            _b('app_name', 'app_name', optional=True),
            _b('app_version', 'app_version', optional=True),
            _b('os_version', 'os_version', optional=True),
            _b('firmware', 'firmware', optional=True),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_media_profile_requirements': (
        'payload_json',
        (
            _b('requirement_id', 'requirement_id'),
            _b('requirement_sha256', 'requirement_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('delivery_class', 'delivery_class'),
            _b('video_codec', 'video_codec', optional=True),
            _b('audio_codec', 'audio_codec', optional=True),
            _b('encryption_requirement', 'encryption_requirement'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_playback_capability_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('stack_ref_id', 'stack_ref', 'ref_id'),
            _b('media_ref_id', 'media_ref', 'ref_id'),
            _b('capability_class', 'capability_class'),
            _b('evidence_class', 'evidence_class'),
            _b('output_state', 'observation', 'output_state',
               optional=True),
            _b('failure_attribution', 'failure_attribution'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_playback_operation_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('run_sha256', 'run_sha256'),
            _b('document_id', 'document_id'),
            _b('stack_ref_id', 'stack_ref', 'ref_id'),
            _b('media_ref_id', 'media_ref', 'ref_id'),
            _b('scenario', 'scenario'),
            _b('duration_s', 'duration_s', optional=True),
            _b('started_at_utc', 'started_at_utc'),
        ),
        (),
    ),
    'cad_playback_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('stack_ref_id', 'stack_ref', 'ref_id'),
            _b('media_ref_id', 'media_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # ---- REV57-INST: #616 HVAC co-design ---------------------------
    'cad_hvac_ventilation_scenarios': (
        'payload_json',
        (
            _b('scenario_id', 'scenario_id'),
            _b('scenario_sha256', 'scenario_sha256'),
            _b('document_id', 'document_id'),
            _b('operating_state', 'operating_state'),
            _b('required_supply_flow_lps', 'required_supply_flow_lps',
               optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_hvac_path_declarations': (
        'payload_json',
        (
            _b('path_id', 'path_id'),
            _b('path_sha256', 'path_sha256'),
            _b('document_id', 'document_id'),
            _b('path_kind', 'path_kind'),
            _b('serves_room', 'serves_room', optional=True),
            _b('flanking_role', 'flanking_role'),
            _b('scenario_ref_id', 'scenario_ref', 'ref_id', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_hvac_component_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('component_kind', 'component_kind'),
            _b('method', 'method'),
            _b('airflow_evidence_class', 'airflow_evidence_class'),
            _b('flow_rate_lps', 'flow_rate_lps', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_hvac_field_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('path_ref_id', 'path_ref', 'ref_id'),
            _b('operating_state', 'operating_state'),
            _b('balancing_state', 'balancing_state'),
            _b('room_noise_db', 'room_noise_db', optional=True),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_hvac_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('path_ref_id', 'path_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('airflow_eligibility', 'airflow_eligibility'),
            _b('acoustic_state', 'acoustic_state'),
            _b('flanking_state', 'flanking_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # ---- REV57-INST: #618 playback reference calibration ------------
    'cad_ref_cal_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_kind', 'profile_kind'),
            _b('profile_document', 'profile_document'),
            _b('lifecycle_state', 'lifecycle_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_ref_cal_stimuli': (
        'payload_json',
        (
            _b('stimulus_id', 'stimulus_id'),
            _b('stimulus_sha256', 'stimulus_sha256'),
            _b('document_id', 'document_id'),
            _b('source_kind', 'source_kind'),
            _b('signal_class', 'signal_class'),
            _b('digital_level_dbfs', 'digital_level_dbfs', optional=True),
            _b('device_identity', 'device_identity', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_ref_cal_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('channel_role', 'channel_role'),
            _b('signal_class', 'signal_class'),
            _b('stimulus_ref_id', 'stimulus_ref', 'ref_id', optional=True),
            _b('quantity', 'quantity'),
            _b('measured_spl_db', 'measured_spl_db', optional=True),
            _b('weighting', 'weighting', optional=True),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_ref_cal_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('stimulus_state', 'stimulus_state'),
            _b('measurement_state', 'measurement_state'),
            _b('lfe_state', 'lfe_state'),
            _b('alignment_state', 'alignment_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # ---- REV57-INST: #631 as-built treatment ------------------------
    'cad_treatment_install_specs': (
        'payload_json',
        (
            _b('spec_id', 'spec_id'),
            _b('spec_sha256', 'spec_sha256'),
            _b('document_id', 'document_id'),
            _b('treatment_class', 'treatment_class'),
            _b('acoustic_role', 'acoustic_role'),
            _b('lab_evidence_class', 'lab_evidence_class'),
            _b('product_identity', 'product_identity', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_treatment_asbuilt_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('spec_ref_id', 'spec_ref', 'ref_id'),
            _b('substituted', 'substituted', optional=True),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_treatment_inspections': (
        'payload_json',
        (
            _b('inspection_id', 'inspection_id'),
            _b('inspection_sha256', 'inspection_sha256'),
            _b('document_id', 'document_id'),
            _b('operator', 'operator', optional=True),
            _b('inspected_at_utc', 'inspected_at_utc'),
        ),
        (),
    ),
    'cad_treatment_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('spec_ref_id', 'spec_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('prediction_validity', 'prediction_validity'),
            _b('before_after_result', 'before_after_result'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # ---- REV57-INST: #612 tactile / seat vibration ------------------
    'cad_tactile_vibration_paths': (
        'payload_json',
        (
            _b('path_id', 'path_id'),
            _b('path_sha256', 'path_sha256'),
            _b('document_id', 'document_id'),
            _b('label', 'label'),
            _b('seat_ref', 'seat_ref', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_tactile_vibration_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('path_ref_id', 'path_ref', 'ref_id'),
            _b('quantity', 'quantity'),
            _b('axis', 'axis'),
            _b('contact_point', 'contact_point'),
            _b('occupancy_state', 'occupancy_state'),
            _b('sensor_evidence_class', 'sensor_evidence_class'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_tactile_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_kind', 'profile_kind'),
            _b('label', 'label'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_tactile_vibration_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('path_ref_id', 'path_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('transfer_state', 'transfer_state'),
            _b('occupancy_state', 'occupancy_state'),
            _b('timing_state', 'timing_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV57-MOUNT: #620 AV mounting / structural-support evidence
    'cad_mount_assemblies': (
        'payload_json',
        (
            _b('assembly_id', 'assembly_id'),
            _b('assembly_sha256', 'assembly_sha256'),
            _b('document_id', 'document_id'),
            _b('equipment_ref_id', 'equipment_ref', 'ref_id',
               optional=True),
            _b('placement_ref_id', 'placement_ref', 'ref_id',
               optional=True),
            _b('equipment_class', 'equipment_class'),
            _b('support_method', 'support_method'),
            _b('overhead_suspension', 'overhead_suspension'),
            _b('duty_state', 'duty_state'),
            _b('interference_state', 'interference_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_mount_load_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('assembly_ref_id', 'assembly_ref', 'ref_id'),
            _b('mass_kg', 'mass_kg', optional=True),
            _b('weight_n', 'weight_n', optional=True),
            _b('duty_state', 'duty_state'),
            _b('source_class', 'source_class'),
            _b('measured_at_utc', 'measured_at_utc', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_mount_support_elements': (
        'payload_json',
        (
            _b('element_id', 'element_id'),
            _b('element_sha256', 'element_sha256'),
            _b('document_id', 'document_id'),
            _b('assembly_ref_id', 'assembly_ref', 'ref_id'),
            _b('element_class', 'element_class'),
            _b('geometry_ref_id', 'geometry_ref', 'ref_id',
               optional=True),
            _b('hidden_condition_state', 'hidden_condition_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_mount_manufacturer_requirements': (
        'payload_json',
        (
            _b('requirement_id', 'requirement_id'),
            _b('requirement_sha256', 'requirement_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('secondary_retention', 'secondary_retention'),
            _b('enclosure_suspension', 'enclosure_suspension'),
            _b('vesa_pattern', 'vesa_pattern', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_mount_structural_approvals': (
        'payload_json',
        (
            _b('approval_id', 'approval_id'),
            _b('approval_sha256', 'approval_sha256'),
            _b('document_id', 'document_id'),
            _b('assembly_ref_id', 'assembly_ref', 'ref_id',
               optional=True),
            _b('evidence_class', 'evidence_class'),
            _b('approval_scope', 'approval_scope'),
            _b('duty_coverage', 'duty_coverage'),
            _b('standard_ref_id', 'standard_ref', 'ref_id',
               optional=True),
            _b('jurisdiction', 'jurisdiction', optional=True),
            _b('issued_at_utc', 'issued_at_utc', optional=True),
            _b('expires_at_utc', 'expires_at_utc', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_mount_inspection_records': (
        'payload_json',
        (
            _b('inspection_id', 'inspection_id'),
            _b('inspection_sha256', 'inspection_sha256'),
            _b('document_id', 'document_id'),
            _b('assembly_ref_id', 'assembly_ref', 'ref_id'),
            _b('inspection_kind', 'inspection_kind'),
            _b('inspector_class', 'inspector_class'),
            _b('findings', 'findings'),
            _b('inspected_at_utc', 'inspected_at_utc'),
            _b('next_due_at_utc', 'next_due_at_utc', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_mount_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('assembly_ref_id', 'assembly_ref', 'ref_id'),
            _b('support_state', 'support_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_measchain_linearity_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_label', 'chain_label'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_measchain_overload_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_ref_id', 'chain_ref', 'ref_id'),
            _b('overload_mechanism', 'overload_mechanism'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_measchain_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_ref_id', 'chain_ref', 'ref_id'),
            _b('state', 'state'),
            _b('requested_class', 'requested_class'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_sweep_deconvolution_specs': (
        'payload_json',
        (
            _b('spec_id', 'spec_id'),
            _b('spec_sha256', 'spec_sha256'),
            _b('document_id', 'document_id'),
            _b('stimulus_ref_id', 'stimulus_ref', 'ref_id'),
            _b('sweep_law', 'sweep_law'),
            _b('algorithm', 'algorithm'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_harmonic_impulse_components': (
        'payload_json',
        (
            _b('component_id', 'component_id'),
            _b('component_sha256', 'component_sha256'),
            _b('document_id', 'document_id'),
            _b('spec_ref_id', 'spec_ref', 'ref_id'),
            _b('harmonic_order', 'harmonic_order'),
            _b('expected_offset_s', 'expected_offset_s', optional=True),
            _b('overlap_state', 'overlap_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_recovered_impulse_responses': (
        'payload_json',
        (
            _b('ir_id', 'ir_id'),
            _b('ir_sha256', 'ir_sha256'),
            _b('document_id', 'document_id'),
            _b('provenance_class', 'provenance_class'),
            _b('spec_ref_id', 'spec_ref', 'ref_id', optional=True),
            _b('raw_capture_ref_id', 'raw_capture_ref', 'ref_id',
               optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_linear_ir_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('ir_ref_id', 'ir_ref', 'ref_id'),
            _b('contamination_state', 'contamination_state'),
            _b('clock_gate', 'clock_gate'),
            _b('chain_gate', 'chain_gate'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_excitation_source_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('source_label', 'source_label'),
            _b('source_type', 'source_type'),
            _b('measurand_class', 'measurand_class'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_source_orientation_captures': (
        'payload_json',
        (
            _b('capture_id', 'capture_id'),
            _b('capture_sha256', 'capture_sha256'),
            _b('document_id', 'document_id'),
            _b('source_ref_id', 'source_ref', 'ref_id'),
            _b('aggregation_role', 'aggregation_role'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_measurement_source_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('source_ref_id', 'source_ref', 'ref_id'),
            _b('strength_g_gate', 'strength_g_gate'),
            _b('level_gate', 'level_gate'),
            _b('sim_comparison', 'sim_comparison', optional=True),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_dsp_realization_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('device_identity', 'device_identity'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_dsp_stage_records': (
        'payload_json',
        (
            _b('stage_id', 'stage_id'),
            _b('stage_sha256', 'stage_sha256'),
            _b('document_id', 'document_id'),
            _b('stage_kind', 'stage_kind'),
            _b('bank_label', 'bank_label'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_dsp_parameter_mappings': (
        'payload_json',
        (
            _b('mapping_id', 'mapping_id'),
            _b('mapping_sha256', 'mapping_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('bank_label', 'bank_label'),
            _b('state', 'state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_dsp_realization_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('state', 'state'),
            _b('transfer_verification', 'transfer_verification'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_decay_processing_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_label', 'profile_label'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_decay_noise_estimates': (
        'payload_json',
        (
            _b('estimate_id', 'estimate_id'),
            _b('estimate_sha256', 'estimate_sha256'),
            _b('document_id', 'document_id'),
            _b('rir_ref_id', 'rir_ref', 'ref_id'),
            _b('method', 'method'),
            _b('stationarity', 'stationarity'),
            _b('level_db', 'level_db'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_decay_truncation_decisions': (
        'payload_json',
        (
            _b('decision_id', 'decision_id'),
            _b('decision_sha256', 'decision_sha256'),
            _b('document_id', 'document_id'),
            _b('rir_ref_id', 'rir_ref', 'ref_id'),
            _b('truncation_time_s', 'truncation_time_s'),
            _b('reason', 'reason'),
            _b('capture_truncated', 'capture_truncated'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_decay_edc_artifacts': (
        'payload_json',
        (
            _b('artifact_id', 'artifact_id'),
            _b('artifact_sha256', 'artifact_sha256'),
            _b('document_id', 'document_id'),
            _b('rir_ref_id', 'rir_ref', 'ref_id'),
            _b('edc_kind', 'edc_kind'),
            _b('content_sha256', 'content_sha256'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_decay_fit_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('rir_ref_id', 'rir_ref', 'ref_id'),
            _b('metric', 'metric'),
            _b('value_s', 'value_s'),
            _b('eligibility', 'eligibility'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_boundary_evidence_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('evidence_label', 'evidence_label'),
            _b('boundary_class', 'boundary_class'),
            _b('passivity_class', 'passivity_class'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_boundary_rational_fits': (
        'payload_json',
        (
            _b('fit_id', 'fit_id'),
            _b('fit_sha256', 'fit_sha256'),
            _b('document_id', 'document_id'),
            _b('input_evidence_ref_id', 'input_evidence_ref', 'ref_id'),
            _b('fit_variable', 'fit_variable'),
            _b('pole_count', 'pole_count'),
            _b('algorithm', 'algorithm'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_td_impedance_realizations': (
        'payload_json',
        (
            _b('realization_id', 'realization_id'),
            _b('realization_sha256', 'realization_sha256'),
            _b('document_id', 'document_id'),
            _b('evidence_ref_id', 'evidence_ref', 'ref_id'),
            _b('solver_family', 'solver_family'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_boundary_realizability_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('evidence_ref_id', 'evidence_ref', 'ref_id'),
            _b('state', 'state'),
            _b('passivity_class', 'passivity_class'),
            _b('causality_state', 'causality_state'),
            _b('stability_state', 'stability_state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-NUMERIC: #683 wave-solver numerical fidelity
    'cad_wave_fidelity_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('solver_family', 'formulation', 'solver_family'),
            _b('solver_result_ref_id', 'solver_result_ref', 'ref_id',
               optional=True),
            _b('mesh_identity', 'discretization', 'mesh_identity'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_wave_convergence_records': (
        'payload_json',
        (
            _b('convergence_id', 'convergence_id'),
            _b('convergence_sha256', 'convergence_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('study_kind', 'study_kind'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_wave_fidelity_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('fidelity_state', 'fidelity_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-NUMERIC: #685 geometrical-acoustics numerical fidelity
    'cad_geometric_fidelity_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('algorithm_family', 'algorithm', 'family'),
            _b('solver_result_ref_id', 'solver_result_ref', 'ref_id',
               optional=True),
            _b('receiver_model', 'receiver', 'model'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_ray_sampling_convergences': (
        'payload_json',
        (
            _b('convergence_id', 'convergence_id'),
            _b('convergence_sha256', 'convergence_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_path_enumeration_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('deterministic_state', 'deterministic_state'),
            _b('named_path_evidence_class', 'named_path_evidence_class'),
            _b('max_qualified_order', 'max_qualified_order',
               optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_geometric_fidelity_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('fidelity_state', 'fidelity_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-NUMERIC: #687 wave↔geometrical hybrid handoff
    'cad_hybrid_composition_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('wave_prediction_ref_id', 'wave_component',
               'prediction_ref', 'ref_id'),
            _b('ga_prediction_ref_id', 'ga_component',
               'prediction_ref', 'ref_id'),
            _b('transition_kind', 'transition', 'kind'),
            _b('output_capability', 'output_capability'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_hybrid_transition_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('handoff_state', 'handoff_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-IDENT: #691 typed logarithmic quantity / dB reference
    'cad_log_quantities': (
        'payload_json',
        (
            _b('quantity_id', 'quantity_id'),
            _b('quantity_sha256', 'quantity_sha256'),
            _b('document_id', 'document_id'),
            _b('quantity_class', 'quantity_class'),
            _b('domain', 'domain'),
            _b('quantity', 'quantity'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_log_calibration_bridges': (
        'payload_json',
        (
            _b('bridge_id', 'bridge_id'),
            _b('bridge_sha256', 'bridge_sha256'),
            _b('document_id', 'document_id'),
            _b('bridge_label', 'bridge_label'),
            _b('from_domain', 'from_domain'),
            _b('to_domain', 'to_domain'),
            _b('status', 'status'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_log_operations': (
        'payload_json',
        (
            _b('operation_id', 'operation_id'),
            _b('operation_sha256', 'operation_sha256'),
            _b('document_id', 'document_id'),
            _b('operation', 'operation'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-IDENT: #689 calibration-parameter identifiability
    'cad_calib_parameter_records': (
        'payload_json',
        (
            _b('parameter_id', 'parameter_id'),
            _b('parameter_sha256', 'parameter_sha256'),
            _b('document_id', 'document_id'),
            _b('parameter_label', 'parameter_label'),
            _b('role', 'role'),
            _b('provenance', 'provenance'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_ident_sensitivity_evidence': (
        'payload_json',
        (
            _b('sensitivity_id', 'sensitivity_id'),
            _b('sensitivity_sha256', 'sensitivity_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
            _b('calibration_run_ref_id', 'calibration_run_ref', 'ref_id',
               optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_ident_correlation_evidence': (
        'payload_json',
        (
            _b('correlation_id', 'correlation_id'),
            _b('correlation_sha256', 'correlation_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_ident_equivalent_sets': (
        'payload_json',
        (
            _b('set_id', 'set_id'),
            _b('set_sha256', 'set_sha256'),
            _b('document_id', 'document_id'),
            _b('multimodal', 'multimodal'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_identifiability_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('parameter_ref_id', 'parameter_ref', 'ref_id'),
            _b('identifiability_class', 'identifiability_class'),
            _b('parameter_claim', 'parameter_claim'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-IDENT: #698 validation sample-dependence / leakage
    'cad_validation_statistical_designs': (
        'payload_json',
        (
            _b('design_id', 'design_id'),
            _b('design_sha256', 'design_sha256'),
            _b('document_id', 'document_id'),
            _b('design_label', 'design_label'),
            _b('generalization_claim', 'generalization_claim'),
            _b('independent_unit', 'independent_unit'),
            _b('independent_unit_count', 'independent_unit_count',
               optional=True),
            _b('raw_observation_count', 'raw_observation_count'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_dependence_models': (
        'payload_json',
        (
            _b('dependence_id', 'dependence_id'),
            _b('dependence_sha256', 'dependence_sha256'),
            _b('document_id', 'document_id'),
            _b('design_ref_id', 'design_ref', 'ref_id', optional=True),
            _b('spatial_correlation_model', 'spatial_correlation_model'),
            _b('resampling_unit', 'resampling_unit', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_dataset_role_assignments': (
        'payload_json',
        (
            _b('assignment_id', 'assignment_id'),
            _b('assignment_sha256', 'assignment_sha256'),
            _b('document_id', 'document_id'),
            _b('corpus_ref_id', 'corpus_ref', 'ref_id'),
            _b('role', 'role'),
            _b('context_label', 'context_label'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_benchmark_exposures': (
        'payload_json',
        (
            _b('exposure_id', 'exposure_id'),
            _b('exposure_sha256', 'exposure_sha256'),
            _b('document_id', 'document_id'),
            _b('corpus_ref_id', 'corpus_ref', 'ref_id'),
            _b('decision_class', 'decision_class'),
            _b('solver_version', 'solver_version', optional=True),
            _b('exposed_at_utc', 'exposed_at_utc'),
        ),
        (),
    ),
    'cad_challenge_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('design_ref_id', 'design_ref', 'ref_id'),
            _b('state', 'state'),
            _b('independent_unit', 'independent_unit'),
            _b('independent_unit_count', 'independent_unit_count',
               optional=True),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-AUDIOMODEL: #654 acoustic-reference origin / phase center
    'cad_source_origin_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('source_ref_id', 'source_ref', 'ref_id'),
            _b('capability', 'capability'),
            _b('boundary_state', 'boundary_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_source_origin_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('requested_capability', 'requested_capability'),
            _b('effective_origin_kind', 'effective_origin_kind'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-AUDIOMODEL: #655 source near/far-field applicability
    'cad_source_field_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('source_ref_id', 'source_ref', 'ref_id'),
            _b('mic_distance_m', 'measurement_geometry',
               'mic_distance_m'),
            _b('environment', 'measurement_geometry', 'environment'),
            _b('default_source_model', 'default_source_model'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_source_field_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('requested_distance_m', 'requested_distance_m',
               optional=True),
            _b('effective_regime', 'effective_regime'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-AUDIOMODEL: #656 directivity angular resolution
    'cad_directivity_sampling_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('dataset_ref_id', 'dataset_ref', 'ref_id'),
            _b('coverage_class', 'sampling', 'coverage_class'),
            _b('measured_direction_count', 'sampling',
               'measured_direction_count'),
            _b('dataset_kind', 'dataset_kind'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_directivity_interpolation_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('method', 'method'),
            _b('domain', 'domain'),
            _b('output_step_deg', 'output_grid', 'nominal_step_deg',
               optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_directivity_direction_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('azimuth_deg', 'azimuth_deg'),
            _b('elevation_deg', 'elevation_deg'),
            _b('frequency_hz', 'frequency_hz', optional=True),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-AUDIOMODEL: #690 multi-source correlation / coherence
    'cad_source_coherence_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('group_label', 'group_label'),
            _b('default_relation', 'default_relation'),
            _b('declared_combination_mode',
               'declared_combination_mode', optional=True),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_source_combination_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('requested_mode', 'requested_mode'),
            _b('effective_mode', 'effective_mode', optional=True),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-AUDIOMODEL: #684 geometric surface-scattering model
    'cad_scattering_model_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('solver_model', 'solver_model'),
            _b('implementation', 'implementation'),
            _b('directional_redirection', 'directional_redirection'),
            _b('incidence_domain', 'incidence_domain'),
            _b('early_late_applicability',
               'early_late_applicability'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_scattering_model_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('effective_model', 'effective_model', optional=True),
            _b('requires_redirection', 'requires_redirection'),
            _b('reflection_order', 'reflection_order'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-AUDIOMODEL: #681 edge diffraction model
    'cad_diffraction_model_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('model_family', 'model_family'),
            _b('implementation', 'implementation'),
            _b('domain', 'domain'),
            _b('edge_kind', 'edge_geometry', 'edge_kind'),
            _b('wedge_boundary', 'wedge_material', 'boundary'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_diffraction_benchmark_results': (
        'payload_json',
        (
            _b('result_id', 'result_id'),
            _b('result_sha256', 'result_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('fixture_id', 'fixture_id'),
            _b('fixture_kind', 'fixture_kind'),
            _b('reference_class', 'reference_class'),
            _b('result', 'result'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_diffraction_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('capability', 'capability'),
            _b('boundary_limited', 'boundary_limited'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-VALIDMETH: #675 optimizer algorithm qualification.
    'cad_optimization_problems': (
        'payload_json',
        (
            _b('problem_id', 'problem_id'),
            _b('problem_sha256', 'problem_sha256'),
            _b('document_id', 'document_id'),
            _b('problem_label', 'problem_label'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_optimizer_run_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('problem_ref_id', 'problem_ref', 'ref_id'),
            _b('algorithm_family', 'algorithm', 'family'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_optimizer_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('state', 'state'),
            _b('optimality_claim', 'optimality_claim'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_pareto_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('state', 'state'),
            _b('reference_status', 'reference_status'),
            _b('nondominated_count', 'nondominated_count',
               optional=True),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-VALIDMETH: #674 acoustic eigenmode validation.
    'cad_mode_pairings': (
        'payload_json',
        (
            _b('pairing_id', 'pairing_id'),
            _b('pairing_sha256', 'pairing_sha256'),
            _b('document_id', 'document_id'),
            _b('pairing_state', 'pairing_state'),
            _b('pairing_algorithm', 'pairing_algorithm'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_eigenmode_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('pairing_ref_id', 'pairing_ref', 'ref_id'),
            _b('state', 'state'),
            _b('frequency_agreement', 'frequency_agreement'),
            _b('shape_agreement', 'shape_agreement'),
            _b('damping_agreement', 'damping_agreement'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-VALIDMETH: #673 sound-field diffuseness applicability.
    'cad_diffuseness_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('eligibility_state', 'eligibility_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_statistical_applicability_declarations': (
        'payload_json',
        (
            _b('declaration_id', 'declaration_id'),
            _b('declaration_sha256', 'declaration_sha256'),
            _b('document_id', 'document_id'),
            _b('assessment_ref_id', 'assessment_ref', 'ref_id',
               optional=True),
            _b('state', 'state'),
            _b('basis', 'basis'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-VALIDMETH: #671 coupled-room multi-slope decay.
    'cad_multi_slope_fits': (
        'payload_json',
        (
            _b('fit_id', 'fit_id'),
            _b('fit_sha256', 'fit_sha256'),
            _b('document_id', 'document_id'),
            _b('raw_evidence_ref_id', 'raw_evidence_ref', 'ref_id'),
            _b('model_class', 'model_class'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_single_slope_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('raw_evidence_ref_id', 'raw_evidence_ref', 'ref_id'),
            _b('adequacy_state', 'adequacy_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_coupled_decay_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('behavior_state', 'behavior_state'),
            _b('state', 'state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-VALIDMETH: #677 early-reflection correspondence.
    'cad_reflection_pairings': (
        'payload_json',
        (
            _b('pairing_id', 'pairing_id'),
            _b('pairing_sha256', 'pairing_sha256'),
            _b('document_id', 'document_id'),
            _b('correspondence_state', 'correspondence_state'),
            _b('matching_algorithm', 'matching_algorithm'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_reflection_correspondence_sets': (
        'payload_json',
        (
            _b('set_id', 'set_id'),
            _b('set_sha256', 'set_sha256'),
            _b('document_id', 'document_id'),
            _b('registration_ref_id', 'registration_ref', 'ref_id'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_reflection_correspondence_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('set_ref_id', 'set_ref', 'ref_id'),
            _b('state', 'state'),
            _b('matched_pair_count', 'matched_pair_count'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-VALIDMETH: #706 time-frequency modal decay.
    'cad_modal_decay_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('raw_evidence_ref_id', 'raw_evidence_ref', 'ref_id'),
            _b('overlap_state', 'overlap_state'),
            _b('fit_model', 'fit_model'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_modal_decay_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('observation_ref_id', 'observation_ref', 'ref_id'),
            _b('state', 'state'),
            _b('decay_trustworthy', 'decay_trustworthy'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV58-DISPLAYMEAS: #682 pattern-generator fidelity
    'cad_pg_generator_instances': (
        'payload_json',
        (
            _b('generator_id', 'generator_id'),
            _b('generator_sha256', 'generator_sha256'),
            _b('document_id', 'document_id'),
            _b('generator_class', 'generator_class'),
            _b('manufacturer', 'manufacturer'),
            _b('model', 'model'),
        ),
        (),
    ),
    'cad_pg_requested_patches': (
        'payload_json',
        (
            _b('patch_id', 'patch_id'),
            _b('patch_sha256', 'patch_sha256'),
            _b('document_id', 'document_id'),
            _b('stimulus_ref_id', 'stimulus_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_pg_delivered_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('patch_ref_id', 'patch_ref', 'ref_id'),
            _b('generator_ref_id', 'generator_ref', 'ref_id'),
            _b('observation_point', 'observation_point'),
            _b('verification', 'verification'),
        ),
        (),
    ),
    'cad_pg_fidelity_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('generator_ref_id', 'generator_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # REV58-DISPLAYMEAS: #680 probe matching / spectral mismatch
    'cad_mm_match_profiles': (
        'payload_json',
        (
            _b('match_id', 'match_id'),
            _b('match_sha256', 'match_sha256'),
            _b('document_id', 'document_id'),
            _b('target_serial', 'target_instrument', 'serial'),
            _b('reference_serial', 'reference_instrument', 'serial'),
            _b('display_instance', 'display_state', 'display_instance'),
        ),
        (),
    ),
    'cad_mm_match_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('match_ref_id', 'match_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_mm_verifications': (
        'payload_json',
        (
            _b('verification_id', 'verification_id'),
            _b('verification_sha256', 'verification_sha256'),
            _b('document_id', 'document_id'),
            _b('match_ref_id', 'match_ref', 'ref_id'),
            _b('passed', 'passed'),
        ),
        (),
    ),
    'cad_mm_applicability': (
        'payload_json',
        (
            _b('applicability_id', 'applicability_id'),
            _b('applicability_sha256', 'applicability_sha256'),
            _b('document_id', 'document_id'),
            _b('match_ref_id', 'match_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # REV58-DISPLAYMEAS: #686 additivity / separation / volumetric
    'cad_da_additivity_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_ref_id', 'display_state_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_da_separation_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_ref_id', 'display_state_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_da_volumetric_characterisations': (
        'payload_json',
        (
            _b('characterisation_id', 'characterisation_id'),
            _b('characterisation_sha256', 'characterisation_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_ref_id', 'display_state_ref', 'ref_id'),
            _b('grid_size', 'grid_size'),
        ),
        (),
    ),
    'cad_da_holdout_verifications': (
        'payload_json',
        (
            _b('verification_id', 'verification_id'),
            _b('verification_sha256', 'verification_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_ref_id', 'display_state_ref', 'ref_id'),
            _b('model_family', 'model_family'),
            _b('passed', 'passed'),
        ),
        (),
    ),
    'cad_da_model_eligibility': (
        'payload_json',
        (
            _b('eligibility_id', 'eligibility_id'),
            _b('eligibility_sha256', 'eligibility_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_ref_id', 'display_state_ref', 'ref_id'),
            _b('model_family', 'model_family'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_da_characterisation_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_ref_id', 'display_state_ref', 'ref_id'),
            _b('required_capability', 'required_capability'),
        ),
        (),
    ),
    # REV58-DISPLAYMEAS: #647 temporal display fidelity
    'cad_td_states': (
        'payload_json',
        (
            _b('state_id', 'state_id'),
            _b('state_sha256', 'state_sha256'),
            _b('document_id', 'document_id'),
            _b('display_state_ref_id', 'display_state_ref', 'ref_id'),
            _b('input_frame_rate_hz', 'input_frame_rate_hz'),
            _b('refresh_rate_hz', 'refresh_rate_hz'),
        ),
        (),
    ),
    'cad_td_step_responses': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('state_ref_id', 'state_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_td_motion_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('state_ref_id', 'state_ref', 'ref_id'),
            _b('mechanism', 'mechanism'),
        ),
        (),
    ),
    'cad_td_flicker_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('state_ref_id', 'state_ref', 'ref_id'),
            _b('method', 'method'),
        ),
        (),
    ),
    'cad_td_retention_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('state_ref_id', 'state_ref', 'ref_id'),
            _b('persistence', 'persistence'),
        ),
        (),
    ),
    'cad_td_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('state_ref_id', 'state_ref', 'ref_id'),
        ),
        (),
    ),
    # REV58-DISPLAYMEAS: #666 LUT closed-loop calibration
    'cad_lut_artifacts': (
        'payload_json',
        (
            _b('artifact_id', 'artifact_id'),
            _b('artifact_sha256', 'artifact_sha256'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
        ),
        (),
    ),
    'cad_lut_generation_records': (
        'payload_json',
        (
            _b('generation_id', 'generation_id'),
            _b('generation_sha256', 'generation_sha256'),
            _b('document_id', 'document_id'),
            _b('artifact_ref_id', 'artifact_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_lut_preflight_verifications': (
        'payload_json',
        (
            _b('preflight_id', 'preflight_id'),
            _b('preflight_sha256', 'preflight_sha256'),
            _b('document_id', 'document_id'),
            _b('artifact_ref_id', 'artifact_ref', 'ref_id'),
            _b('numeric_validation_passed',
               'numeric_validation_passed'),
        ),
        (),
    ),
    'cad_lut_deployments': (
        'payload_json',
        (
            _b('deployment_id', 'deployment_id'),
            _b('deployment_sha256', 'deployment_sha256'),
            _b('document_id', 'document_id'),
            _b('artifact_ref_id', 'artifact_ref', 'ref_id'),
            _b('device_instance', 'device_instance'),
            _b('slot', 'slot'),
        ),
        (),
    ),
    'cad_lut_post_verifications': (
        'payload_json',
        (
            _b('post_verification_id', 'post_verification_id'),
            _b('post_verification_sha256', 'post_verification_sha256'),
            _b('document_id', 'document_id'),
            _b('deployment_ref_id', 'deployment_ref', 'ref_id'),
            _b('passed', 'passed'),
        ),
        (),
    ),
    'cad_lut_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('artifact_ref_id', 'artifact_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # REV58-MEASELEC: #699 / #651 / #649 / #665 / #693.
    'cad_interface_loopback_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('loopback_path_kind', 'io_path', 'loopback_path_kind'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_interface_transfer_calibrations': (
        'payload_json',
        (
            _b('calibration_id', 'calibration_id'),
            _b('calibration_sha256', 'calibration_sha256'),
            _b('document_id', 'document_id'),
            _b('calibration_kind', 'calibration_kind'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_interface_correction_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('calibration_ref_id', 'calibration_ref', 'ref_id',
               optional=True),
            _b('state', 'state'),
            _b('sample_rate_applicability', 'sample_rate_applicability'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_signal_level_references': (
        'payload_json',
        (
            _b('reference_id', 'reference_id'),
            _b('reference_sha256', 'reference_sha256'),
            _b('document_id', 'document_id'),
            _b('stage_label', 'stage', 'stage_label'),
            _b('analog_unit', 'analog_unit'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_noise_floor_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('stage_label', 'stage', 'stage_label', optional=True),
            _b('noise_class', 'noise_class'),
            _b('noise_level', 'noise_level'),
            _b('noise_unit', 'noise_unit'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_clipping_margins': (
        'payload_json',
        (
            _b('margin_id', 'margin_id'),
            _b('margin_sha256', 'margin_sha256'),
            _b('document_id', 'document_id'),
            _b('stage_label', 'stage', 'stage_label'),
            _b('clip_mechanism', 'clip_mechanism'),
            _b('load_stress', 'load_stress'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_gain_structure_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('use_case', 'use_case'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_playback_dynamics_states': (
        'payload_json',
        (
            _b('state_id', 'state_id'),
            _b('state_sha256', 'state_sha256'),
            _b('document_id', 'document_id'),
            _b('device', 'device'),
            _b('output_mode', 'output_mode'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_level_sweep_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('stimulus_ref_id', 'stimulus_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_playback_dynamics_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('dynamics_state_ref_id', 'dynamics_state_ref', 'ref_id',
               optional=True),
            _b('purpose', 'purpose'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_multiway_speaker_definitions': (
        'payload_json',
        (
            _b('definition_id', 'definition_id'),
            _b('definition_sha256', 'definition_sha256'),
            _b('document_id', 'document_id'),
            _b('speaker_instance', 'speaker_instance'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_active_crossover_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('speaker_ref_id', 'speaker_ref', 'ref_id'),
            _b('dsp_device', 'dsp_device'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_driver_alignment_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('speaker_ref_id', 'speaker_ref', 'ref_id'),
            _b('way_label', 'way_label'),
            _b('acoustic_polarity', 'acoustic_polarity'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_active_crossover_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('speaker_ref_id', 'speaker_ref', 'ref_id'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_method_procedures': (
        'payload_json',
        (
            _b('procedure_id', 'procedure_id'),
            _b('procedure_sha256', 'procedure_sha256'),
            _b('document_id', 'document_id'),
            _b('method_name', 'method_name'),
            _b('procedure_version', 'procedure_version'),
            _b('documented', 'documented'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_reproducibility_campaigns': (
        'payload_json',
        (
            _b('campaign_id', 'campaign_id'),
            _b('campaign_sha256', 'campaign_sha256'),
            _b('document_id', 'document_id'),
            _b('procedure_ref_id', 'procedure_ref', 'ref_id'),
            _b('design_class', 'design_class'),
            _b('evidence_tier', 'evidence_tier'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_method_precision_models': (
        'payload_json',
        (
            _b('model_id', 'model_id'),
            _b('model_sha256', 'model_sha256'),
            _b('document_id', 'document_id'),
            _b('campaign_ref_id', 'campaign_ref', 'ref_id'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_reproducibility_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('procedure_ref_id', 'procedure_ref', 'ref_id'),
            _b('evidence_tier', 'evidence_tier'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV59-APPLY: #723 device apply transaction / rollback
    'cad_apply_capability_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('device_ref_id', 'device_ref', 'ref_id'),
            _b('capability_evidence', 'capability_evidence'),
        ),
        (),
    ),
    'cad_apply_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('device_ref_id', 'device_ref', 'ref_id'),
            _b('capability_ref_id', 'capability_ref', 'ref_id'),
            _b('pre_state_evidence', 'pre_state_evidence'),
            _b('rollback_strategy', 'rollback_strategy'),
        ),
        (),
    ),
    'cad_apply_write_records': (
        'payload_json',
        (
            _b('write_id', 'write_id'),
            _b('write_sha256', 'write_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('sequence_index', 'sequence_index'),
            _b('write_kind', 'write_kind'),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    'cad_apply_verifications': (
        'payload_json',
        (
            _b('verification_id', 'verification_id'),
            _b('verification_sha256', 'verification_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('readback_means', 'readback_means'),
        ),
        (),
    ),
    'cad_apply_rollback_plans': (
        'payload_json',
        (
            _b('rollback_plan_id', 'rollback_plan_id'),
            _b('rollback_plan_sha256', 'rollback_plan_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('pre_state_evidence', 'pre_state_evidence'),
            _b('claim', 'claim'),
        ),
        (),
    ),
    'cad_apply_rollback_executions': (
        'payload_json',
        (
            _b('execution_id', 'execution_id'),
            _b('execution_sha256', 'execution_sha256'),
            _b('document_id', 'document_id'),
            _b('rollback_plan_ref_id', 'rollback_plan_ref', 'ref_id'),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    'cad_apply_transactions': (
        'payload_json',
        (
            _b('transaction_id', 'transaction_id'),
            _b('transaction_sha256', 'transaction_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('capability_ref_id', 'capability_ref', 'ref_id'),
            _b('state_verdict', 'state_verdict'),
        ),
        (),
    ),
    'cad_fractional_octave_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('band_kind', 'band_kind'),
            _b('frequency_standard', 'frequency_standard'),
            _b('filter_class', 'filter_class'),
        ),
        (),
    ),
    'cad_band_integrations': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_echo_density_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('estimator_kind', 'estimator_kind'),
        ),
        (),
    ),
    'cad_mixing_time_estimates': (
        'payload_json',
        (
            _b('estimate_id', 'estimate_id'),
            _b('estimate_sha256', 'estimate_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('basis', 'basis'),
        ),
        (),
    ),
    'cad_late_field_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_interpolation_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
            _b('quantity', 'quantity'),
        ),
        (),
    ),
    'cad_field_surface_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_solver_budget_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),
    'cad_compute_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),
    'cad_accuracy_cost_envelopes': (
        'payload_json',
        (
            _b('envelope_id', 'envelope_id'),
            _b('envelope_sha256', 'envelope_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),
    'cad_jitter_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('spectrum_capable', 'spectrum_capable'),
        ),
        (),
    ),
    'cad_jitter_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('jitter_kind', 'jitter_kind'),
        ),
        (),
    ),
    'cad_jitter_transfer_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_converter_jitter_susceptibility': (
        'payload_json',
        (
            _b('susceptibility_id', 'susceptibility_id'),
            _b('susceptibility_sha256', 'susceptibility_sha256'),
            _b('document_id', 'document_id'),
            _b('converter_ref_id', 'converter_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_dither_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('dither_kind', 'dither_kind'),
        ),
        (),
    ),
    'cad_digital_path_transforms': (
        'payload_json',
        (
            _b('transform_id', 'transform_id'),
            _b('transform_sha256', 'transform_sha256'),
            _b('document_id', 'document_id'),
            _b('transform_kind', 'transform_kind'),
        ),
        (),
    ),
    'cad_playback_src_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('algorithm', 'algorithm'),
        ),
        (),
    ),
    'cad_src_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('src_profile_ref_id', 'src_profile_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_clock_domain_crossings': (
        'payload_json',
        (
            _b('crossing_id', 'crossing_id'),
            _b('crossing_sha256', 'crossing_sha256'),
            _b('document_id', 'document_id'),
            _b('declared_kind', 'declared_kind'),
        ),
        (),
    ),
    'cad_interchannel_leakage_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('stage', 'stage'),
            _b('method', 'method'),
        ),
        (),
    ),
    'cad_channel_separation_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('stage', 'stage'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),    'cad_projector_light_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),
    'cad_temporal_contrast_measures': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('measurand', 'measurand'),
            _b('light_mode', 'light_mode'),
        ),
        (),
    ),
    'cad_dynamic_contrast_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('measurement_ref_id', 'measurement_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_light_measurement_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('stray_light_control', 'stray_light_control'),
        ),
        (),
    ),
    'cad_low_luminance_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('capability_ref_id', 'capability_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_display_boundary_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('transmission', 'transmission'),
        ),
        (),
    ),
    'cad_front_stage_variants': (
        'payload_json',
        (
            _b('variant_id', 'variant_id'),
            _b('variant_sha256', 'variant_sha256'),
            _b('document_id', 'document_id'),
            _b('boundary_ref_id', 'boundary_ref', 'ref_id'),
            _b('strategy', 'strategy'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_codec_chain_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('media_kind', 'media_kind'),
        ),
        (),
    ),
    'cad_quality_method_profiles': (
        'payload_json',
        (
            _b('method_id', 'method_id'),
            _b('method_sha256', 'method_sha256'),
            _b('document_id', 'document_id'),
            _b('method_kind', 'method_kind'),
        ),
        (),
    ),
    'cad_codec_fidelity_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_ref_id', 'chain_ref', 'ref_id'),
            _b('method_ref_id', 'method_ref', 'ref_id'),
        ),
        (),
    ),    'cad_power_sequencing_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('direction', 'direction'),
        ),
        (),
    ),
    'cad_power_sequence_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_ups_transition_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('ups_device_id', 'ups_device_id'),
            _b('transfer_observed', 'transfer_observed'),
        ),
        (),
    ),
    'cad_power_quality_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('circuit_id', 'circuit_id'),
        ),
        (),
    ),
    'cad_power_quality_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('circuit_id', 'circuit_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_emc_product_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),
    'cad_emc_symptom_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),    'cad_product_safety_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('listing_kind', 'listing_kind'),
        ),
        (),
    ),
    'cad_occupied_iaq_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('room_id', 'room_id'),
            _b('occupied', 'occupied'),
        ),
        (),
    ),
    'cad_occupied_iaq_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('room_id', 'room_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_voc_emission_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),    'cad_measurement_fixture_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),
    'cad_fixture_scattering_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('fixture_ref_id', 'fixture_ref', 'ref_id'),
            _b('contamination_detected', 'contamination_detected'),
        ),
        (),
    ),
    'cad_spectral_estimator_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('window_kind', 'window_kind'),
        ),
        (),
    ),
    'cad_spectral_resolution_claims': (
        'payload_json',
        (
            _b('claim_id', 'claim_id'),
            _b('claim_sha256', 'claim_sha256'),
            _b('document_id', 'document_id'),
            _b('estimator_ref_id', 'estimator_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_external_evidence_sources': (
        'payload_json',
        (
            _b('source_id', 'source_id'),
            _b('source_sha256', 'source_sha256'),
            _b('document_id', 'document_id'),
            _b('source_tier', 'source_tier'),
            _b('scope', 'scope'),
        ),
        (),
    ),
    'cad_evidence_supersession_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('resolution', 'resolution'),
        ),
        (),
    ),
    'cad_sound_strength_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
            _b('band', 'band'),
        ),
        (),
    ),
    'cad_sound_strength_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_resonant_absorber_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('absorber_kind', 'absorber_kind'),
        ),
        (),
    ),
    'cad_resonant_performance_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('derivation', 'derivation'),
        ),
        (),
    ),
    'cad_service_envelope_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),
    'cad_service_access_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('envelope_ref_id', 'envelope_ref', 'ref_id'),
        ),
        (),
    ),
    # REV59-CODEPOLICY: #746 egress / #748 lighting TLM / #722 privacy.
    'cad_life_safety_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('project_kind', 'project_kind'),
            _b('applicability_decision', 'applicability_decision'),
        ),
        (),
    ),
    'cad_circulation_routes': (
        'payload_json',
        (
            _b('route_id', 'route_id'),
            _b('route_sha256', 'route_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('furniture_state', 'furniture_state'),
        ),
        (),
    ),
    'cad_seating_accessibility_requirements': (
        'payload_json',
        (
            _b('requirement_id', 'requirement_id'),
            _b('requirement_sha256', 'requirement_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_egress_evidence_records': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('evidence_class', 'evidence_class'),
        ),
        (),
    ),
    'cad_professional_approval_refs': (
        'payload_json',
        (
            _b('approval_id', 'approval_id'),
            _b('approval_sha256', 'approval_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_dimming_temporal_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('luminaire_ref_id', 'luminaire_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_temporal_light_waveforms': (
        'payload_json',
        (
            _b('waveform_id', 'waveform_id'),
            _b('waveform_sha256', 'waveform_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('illuminance_lx', 'illuminance_lx'),
        ),
        (),
    ),
    'cad_lighting_tlm_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('waveform_ref_id', 'waveform_ref', 'ref_id'),
            _b('phenomenon', 'phenomenon'),
        ),
        (),
    ),
    'cad_lighting_tla_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('metric_id', 'metric_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_project_data_classifications': (
        'payload_json',
        (
            _b('classification_id', 'classification_id'),
            _b('classification_sha256', 'classification_sha256'),
            _b('document_id', 'document_id'),
            _b('artifact_ref_id', 'artifact_ref', 'ref_id'),
            _b('data_class', 'data_class'),
        ),
        (),
    ),
    'cad_sensitive_artifact_policies': (
        'payload_json',
        (
            _b('policy_id', 'policy_id'),
            _b('policy_sha256', 'policy_sha256'),
            _b('document_id', 'document_id'),
        ),
        (),
    ),
    'cad_export_redaction_manifests': (
        'payload_json',
        (
            _b('manifest_id', 'manifest_id'),
            _b('manifest_sha256', 'manifest_sha256'),
            _b('document_id', 'document_id'),
            _b('bundle_kind', 'bundle_kind'),
        ),
        (),
    ),
    'cad_retention_policy_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('artifact_ref_id', 'artifact_ref', 'ref_id'),
            _b('retention_class', 'retention_class'),
        ),
        (),
    ),
    # REV59-DEPS: #729 authority dependency / staleness graph.
    'cad_dependency_edge_declarations': (
        'payload_json',
        (
            _b('edge_id', 'edge_id'),
            _b('edge_sha256', 'edge_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('kind', 'kind'),
            _b('target_ref_id', 'target_ref', 'ref_id'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_dependency_change_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('changed_ref_id', 'changed_ref', 'ref_id'),
            _b('change_class', 'change_class'),
            _b('occurred_at_utc', 'occurred_at_utc'),
        ),
        (),
    ),
    'cad_dependency_rule_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('ruleset_version', 'ruleset_version'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (_list_count(
            'cad_dependency_rule_profiles', 'entry_count', 'entries',
        ),),
    ),
    'cad_staleness_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('change_event_ref_id', 'change_event_ref', 'ref_id'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (_list_count(
            'cad_staleness_assessments', 'entry_count', 'entries',
        ),),
    ),
    'cad_revalidation_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('assessment_ref_id', 'assessment_ref', 'ref_id'),
            _b('planned_at_utc', 'planned_at_utc'),
        ),
        (_list_count(
            'cad_revalidation_plans', 'action_count', 'actions',
        ),),
    ),
    # REV59-DEPS: #725 evidence attestation / trusted timestamp.
    'cad_signed_manifests': (
        'payload_json',
        (
            _b('manifest_id', 'manifest_id'),
            _b('manifest_record_sha256', 'manifest_record_sha256'),
            _b('document_id', 'document_id'),
            _b('manifest_label', 'manifest_label'),
            _b('manifest_sha256', 'manifest_sha256'),
            _b('approval_scope', 'approval_scope'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_manifest_attestations': (
        'payload_json',
        (
            _b('attestation_id', 'attestation_id'),
            _b('attestation_sha256', 'attestation_sha256'),
            _b('document_id', 'document_id'),
            _b('manifest_ref_id', 'manifest_ref', 'ref_id'),
            _b('kind', 'kind'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_attestation_verifications': (
        'payload_json',
        (
            _b('verification_id', 'verification_id'),
            _b('verification_sha256', 'verification_sha256'),
            _b('document_id', 'document_id'),
            _b('attestation_ref_id', 'attestation_ref', 'ref_id'),
            _b('manifest_ref_id', 'manifest_ref', 'ref_id'),
            _b('state', 'state'),
            _b('time_authority', 'time_authority'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV59-DEPS: #718 project archival / schema-migration authority.
    'cad_archive_snapshots': (
        'payload_json',
        (
            _b('archive_id', 'archive_id'),
            _b('archive_sha256', 'archive_sha256'),
            _b('document_id', 'document_id'),
            _b('archive_label', 'archive_label'),
            _b('schema_version', 'schema_version'),
            _b('content_hash', 'content_hash'),
            _b('preservation_scope', 'preservation_scope'),
            _b('captured_at_utc', 'captured_at_utc'),
        ),
        (),
    ),
    'cad_archive_verifications': (
        'payload_json',
        (
            _b('verification_id', 'verification_id'),
            _b('verification_sha256', 'verification_sha256'),
            _b('document_id', 'document_id'),
            _b('archive_ref_id', 'archive_ref', 'ref_id'),
            _b('status', 'status'),
            _b('verified_at_utc', 'verified_at_utc'),
        ),
        (_list_count(
            'cad_archive_verifications', 'check_count', 'checks',
        ),),
    ),
    'cad_migration_records': (
        'payload_json',
        (
            _b('migration_id', 'migration_id'),
            _b('migration_sha256', 'migration_sha256'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
            _b('source_archive_ref_id', 'source_archive_ref', 'ref_id'),
            _b('target_archive_ref_id', 'target_archive_ref', 'ref_id'),
            _b('from_schema_version', 'from_schema_version'),
            _b('to_schema_version', 'to_schema_version'),
            _b('migration_status_at_write', 'migration_status_at_write'),
            _b('migrated_at_utc', 'migrated_at_utc'),
        ),
        (),
    ),
    'cad_migration_verifications': (
        'payload_json',
        (
            _b('verification_id', 'verification_id'),
            _b('verification_sha256', 'verification_sha256'),
            _b('document_id', 'document_id'),
            _b('migration_ref_id', 'migration_ref', 'ref_id'),
            _b('status', 'status'),
            _b('verified_at_utc', 'verified_at_utc'),
        ),
        (_list_count(
            'cad_migration_verifications', 'check_count', 'checks',
        ),),
    ),    # REV59-LOUDSPK: #754/#734/#731/#732/#737/#735.
    'cad_large_signal_models': (
        'payload_json',
        (
            _b('model_id', 'model_id'),
            _b('model_sha256', 'model_sha256'),
            _b('document_id', 'document_id'),
            _b('evidence_class', 'evidence_class'),
            _b('enclosure_alignment', 'enclosure_alignment'),
            _b('level_applicability', 'level_applicability'),
        ),
        (),
    ),
    'cad_excursion_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('transducer_ref_id', 'transducer_ref', 'ref_id'),
            _b('evidence_class', 'evidence_class'),
            _b('definition_basis', 'definition_basis'),
        ),
        (),
    ),
    'cad_vent_flow_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('system_ref_id', 'system_ref', 'ref_id'),
            _b('mechanism', 'mechanism'),
        ),
        (),
    ),
    'cad_mechanical_output_limits': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('model_ref_id', 'model_ref', 'ref_id'),
            _b('limiting_mechanism', 'limiting_mechanism'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_source_normalizations': (
        'payload_json',
        (
            _b('normalization_id', 'normalization_id'),
            _b('normalization_sha256', 'normalization_sha256'),
            _b('document_id', 'document_id'),
            _b('capability', 'capability'),
            _b('normalization_method', 'normalization_method'),
        ),
        (),
    ),
    'cad_reference_drive_conditions': (
        'payload_json',
        (
            _b('condition_id', 'condition_id'),
            _b('condition_sha256', 'condition_sha256'),
            _b('document_id', 'document_id'),
            _b('quantity_kind', 'quantity_kind'),
        ),
        (),
    ),
    'cad_absolute_output_anchors': (
        'payload_json',
        (
            _b('anchor_id', 'anchor_id'),
            _b('anchor_sha256', 'anchor_sha256'),
            _b('document_id', 'document_id'),
            _b('normalization_ref_id', 'normalization_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_sustained_output_tests': (
        'payload_json',
        (
            _b('test_id', 'test_id'),
            _b('test_sha256', 'test_sha256'),
            _b('document_id', 'document_id'),
            _b('capability_class', 'capability_class'),
            _b('initial_thermal_state', 'initial_thermal_state'),
        ),
        (),
    ),
    'cad_thermal_compression_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('test_ref_id', 'test_ref', 'ref_id'),
            _b('suspected_cause', 'suspected_cause'),
        ),
        (),
    ),
    'cad_recovery_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('test_ref_id', 'test_ref', 'ref_id'),
            _b('recovery_state', 'recovery_state'),
        ),
        (),
    ),
    'cad_microphone_directional_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('calibration_field_kind', 'calibration_field_kind'),
        ),
        (),
    ),
    'cad_receiver_orientation_states': (
        'payload_json',
        (
            _b('state_id', 'state_id'),
            _b('state_sha256', 'state_sha256'),
            _b('document_id', 'document_id'),
            _b('orientation_frame', 'orientation_frame'),
        ),
        (),
    ),
    'cad_microphone_incidence_applicability': (
        'payload_json',
        (
            _b('applicability_id', 'applicability_id'),
            _b('applicability_sha256', 'applicability_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_same_channel_arrays': (
        'payload_json',
        (
            _b('array_id', 'array_id'),
            _b('array_sha256', 'array_sha256'),
            _b('document_id', 'document_id'),
            _b('logical_channel_ref_id', 'logical_channel_ref', 'ref_id'),
            _b('topology', 'topology'),
        ),
        (),
    ),
    'cad_array_reproduction_modes': (
        'payload_json',
        (
            _b('mode_id', 'mode_id'),
            _b('mode_sha256', 'mode_sha256'),
            _b('document_id', 'document_id'),
            _b('array_ref_id', 'array_ref', 'ref_id'),
            _b('render_mode', 'render_mode'),
        ),
        (),
    ),
    'cad_array_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('array_ref_id', 'array_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_loudspeaker_front_layers': (
        'payload_json',
        (
            _b('layer_id', 'layer_id'),
            _b('layer_sha256', 'layer_sha256'),
            _b('document_id', 'document_id'),
            _b('loudspeaker_ref_id', 'loudspeaker_ref', 'ref_id'),
            _b('kind', 'kind'),
        ),
        (),
    ),
    'cad_grille_transfer_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('layer_ref_id', 'layer_ref', 'ref_id'),
            _b('evidence_class', 'evidence_class'),
            _b('transfer_kind', 'transfer_kind'),
        ),
        (),
    ),
    'cad_front_layer_applicability': (
        'payload_json',
        (
            _b('applicability_id', 'applicability_id'),
            _b('applicability_sha256', 'applicability_sha256'),
            _b('document_id', 'document_id'),
            _b('layer_ref_id', 'layer_ref', 'ref_id', optional=True),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # REV59-QUALNUM: #703/#716/#717.
    'cad_numerical_repro_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('precision_kind', 'precision_kind'),
            _b('parallelism_kind', 'parallelism_kind'),
        ),
        (),
    ),
    'cad_stochastic_realizations': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('realization_kind', 'realization_kind'),
        ),
        (),
    ),
    'cad_numerical_comparisons': (
        'payload_json',
        (
            _b('comparison_id', 'comparison_id'),
            _b('comparison_sha256', 'comparison_sha256'),
            _b('document_id', 'document_id'),
            _b('domain', 'domain'),
        ),
        (),
    ),
    'cad_imaging_measurement_chains': (
        'payload_json',
        (
            _b('chain_id', 'chain_id'),
            _b('chain_sha256', 'chain_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_state', 'chain_state'),
            _b('shutter_kind', 'shutter_kind'),
        ),
        (),
    ),
    'cad_camera_calibrations': (
        'payload_json',
        (
            _b('calibration_id', 'calibration_id'),
            _b('calibration_sha256', 'calibration_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_ref_id', 'chain_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_camera_derived_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('chain_ref_id', 'chain_ref', 'ref_id'),
            _b('measurand', 'measurand'),
            _b('processing_state', 'processing_state'),
        ),
        (),
    ),
    'cad_wireless_av_links': (
        'payload_json',
        (
            _b('link_id', 'link_id'),
            _b('link_sha256', 'link_sha256'),
            _b('document_id', 'document_id'),
            _b('transport_kind', 'transport_kind'),
        ),
        (),
    ),
    'cad_wireless_transport_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('link_ref_id', 'link_ref', 'ref_id'),
            _b('dropout_events', 'dropout_events'),
        ),
        (),
    ),
    'cad_wireless_sync_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('link_ref_id', 'link_ref', 'ref_id'),
        ),
        (),
    ),    # REV59-DRAWPROF: #741/#742/#733.
    'cad_ht_video_design_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('edition', 'edition'),
        ),
        (),
    ),
    'cad_ceb23_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_drawing_symbol_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('edition', 'edition'),
            _b('rights_provenance', 'rights_provenance'),
        ),
        (),
    ),
    'cad_device_symbol_mappings': (
        'payload_json',
        (
            _b('mapping_id', 'mapping_id'),
            _b('mapping_sha256', 'mapping_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('device_kind', 'device_kind'),
        ),
        (),
    ),
    'cad_drawing_export_records': (
        'payload_json',
        (
            _b('export_id', 'export_id'),
            _b('export_sha256', 'export_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('export_format', 'export_format'),
        ),
        (),
    ),
    'cad_timed_text_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_kind', 'profile_kind'),
        ),
        (),
    ),
    'cad_caption_render_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_finite_absorber_geometries': (
        'payload_json',
        (
            _b('geometry_id', 'geometry_id'),
            _b('geometry_sha256', 'geometry_sha256'),
            _b('document_id', 'document_id'),
            _b('edge_state', 'edge_state'),
            _b('mounting_kind', 'mounting_kind'),
        ),
        (),
    ),
    'cad_finite_treatment_boundary_models': (
        'payload_json',
        (
            _b('model_id', 'model_id'),
            _b('model_sha256', 'model_sha256'),
            _b('document_id', 'document_id'),
            _b('geometry_ref_id', 'geometry_ref', 'ref_id'),
            _b('reaction_kind', 'reaction_kind'),
        ),
        (),
    ),
    'cad_precedence_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('stimulus_kind', 'stimulus_kind'),
        ),
        (),
    ),
    'cad_echo_risk_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('risk_verdict', 'risk_verdict'),
        ),
        (),
    ),
    'cad_reaction_to_fire_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('test_standard', 'test_standard'),
        ),
        (),
    ),
    'cad_finish_assembly_evidence': (
        'payload_json',
        (
            _b('assembly_id', 'assembly_id'),
            _b('assembly_sha256', 'assembly_sha256'),
            _b('document_id', 'document_id'),
            _b('installation_context', 'installation_context'),
        ),
        (),
    ),
    'cad_listening_experiment_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('method_kind', 'method_kind'),
            _b('impairment_regime', 'impairment_regime'),
        ),
        (),
    ),
    'cad_listener_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('training_completed', 'training_completed'),
        ),
        (),
    ),
    'cad_subjective_inference_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_assistive_listening_paths': (
        'payload_json',
        (
            _b('path_id', 'path_id'),
            _b('path_sha256', 'path_sha256'),
            _b('document_id', 'document_id'),
            _b('technology', 'technology'),
        ),
        (),
    ),
    'cad_als_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('path_ref_id', 'path_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_receiver_compatibility_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('path_ref_id', 'path_ref', 'ref_id'),
            _b('compatible', 'compatible'),
        ),
        (),
    ),
    'cad_dynamic_binaural_sessions': (
        'payload_json',
        (
            _b('session_id', 'session_id'),
            _b('session_sha256', 'session_sha256'),
            _b('document_id', 'document_id'),
            _b('hrtf_class', 'hrtf_class'),
        ),
        (),
    ),
    'cad_pose_tracking_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_binaural_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_receiver_reference_points': (
        'payload_json',
        (
            _b('reference_id', 'reference_id'),
            _b('reference_sha256', 'reference_sha256'),
            _b('document_id', 'document_id'),
            _b('point_kind', 'point_kind'),
        ),
        (),
    ),
    'cad_microphone_capsule_poses': (
        'payload_json',
        (
            _b('pose_id', 'pose_id'),
            _b('pose_sha256', 'pose_sha256'),
            _b('document_id', 'document_id'),
            _b('reference_ref_id', 'reference_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_measurement_fixtures': (
        'payload_json',
        (
            _b('fixture_id', 'fixture_id'),
            _b('fixture_sha256', 'fixture_sha256'),
            _b('document_id', 'document_id'),
            _b('fixture_kind', 'fixture_kind'),
        ),
        (),
    ),
    'cad_fixture_scattering_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('bound_kind', 'bound_kind'),
        ),
        (),
    ),
    'cad_discrete_reflection_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('periodicity', 'periodicity'),
        ),
        (),
    ),
    'cad_echo_diagnostics': (
        'payload_json',
        (
            _b('diagnostic_id', 'diagnostic_id'),
            _b('diagnostic_sha256', 'diagnostic_sha256'),
            _b('document_id', 'document_id'),
            _b('signal_class', 'signal_class'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_drr_method_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('receiver_kind', 'receiver_kind'),
        ),
        (),
    ),
    'cad_drr_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('method_ref_id', 'method_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_adaptive_identification_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
        ),
        (),
    ),
    'cad_arbitrary_stimulus_measurements': (
        'payload_json',
        (
            _b('stimulus_id', 'stimulus_id'),
            _b('stimulus_sha256', 'stimulus_sha256'),
            _b('document_id', 'document_id'),
            _b('defect_state', 'defect_state'),
        ),
        (),
    ),
    'cad_adaptive_transfer_estimates': (
        'payload_json',
        (
            _b('estimate_id', 'estimate_id'),
            _b('estimate_sha256', 'estimate_sha256'),
            _b('document_id', 'document_id'),
            _b('convergence_state', 'convergence_state'),
        ),
        (),
    ),
    'cad_adaptive_residual_evidence': (
        'payload_json',
        (
            _b('residual_id', 'residual_id'),
            _b('residual_sha256', 'residual_sha256'),
            _b('document_id', 'document_id'),
            _b('declared_quantity', 'declared_quantity'),
        ),
        (),
    ),
    'cad_live_tf_sessions': (
        'payload_json',
        (
            _b('session_id', 'session_id'),
            _b('session_sha256', 'session_sha256'),
            _b('document_id', 'document_id'),
            _b('reference_kind', 'reference_kind'),
        ),
        (),
    ),
    'cad_dual_channel_tf_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('capture_state', 'capture_state'),
        ),
        (),
    ),
    'cad_coherence_observations': (
        'payload_json',
        (
            _b('coherence_id', 'coherence_id'),
            _b('coherence_sha256', 'coherence_sha256'),
            _b('document_id', 'document_id'),
            _b('observation_ref_id', 'observation_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_reference_delay_tracks': (
        'payload_json',
        (
            _b('track_id', 'track_id'),
            _b('track_sha256', 'track_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
        ),
        (),
    ),
    'cad_microphone_array_geometries': (
        'payload_json',
        (
            _b('geometry_id', 'geometry_id'),
            _b('geometry_sha256', 'geometry_sha256'),
            _b('document_id', 'document_id'),
            _b('topology', 'topology'),
        ),
        (),
    ),
    'cad_spatial_sampling_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('sync_capability', 'sync_capability'),
        ),
        (),
    ),
    'cad_beamforming_transforms': (
        'payload_json',
        (
            _b('transform_id', 'transform_id'),
            _b('transform_sha256', 'transform_sha256'),
            _b('document_id', 'document_id'),
            _b('output_state', 'output_state'),
        ),
        (),
    ),
    'cad_impedance_measurement_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
        ),
        (),
    ),
    'cad_impedance_calibration_states': (
        'payload_json',
        (
            _b('calibration_id', 'calibration_id'),
            _b('calibration_sha256', 'calibration_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_measured_load_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('evidence_class', 'evidence_class'),
        ),
        (),
    ),
    'cad_thiele_small_derivations': (
        'payload_json',
        (
            _b('derivation_id', 'derivation_id'),
            _b('derivation_sha256', 'derivation_sha256'),
            _b('document_id', 'document_id'),
            _b('model_fit_state', 'model_fit_state'),
        ),
        (),
    ),
    'cad_power_sequence_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('amplifier_step', 'amplifier_step'),
            _b('amplifier_last_on_first_off', 'amplifier_last_on_first_off'),
        ),
        (),
    ),
    'cad_power_sequence_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    'cad_power_quality_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('instrument_class', 'instrument_class'),
        ),
        (),
    ),
    'cad_indoor_air_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('sensor_class', 'sensor_class'),
        ),
        (),
    ),
    'cad_material_emission_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('emission_class', 'emission_class'),
        ),
        (),
    ),
    'cad_product_safety_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('safety_standard', 'safety_standard'),
        ),
        (),
    ),
    'cad_emc_compliance_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_kind', 'profile_kind'),
        ),
        (),
    ),
    'cad_displayed_gradation_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('range_semantics', 'range_semantics'),
            _b('banding_observed', 'banding_observed'),
        ),
        (),
    ),
    'cad_colour_volume_measurements': (
        'payload_json',
        (
            _b('volume_id', 'volume_id'),
            _b('volume_sha256', 'volume_sha256'),
            _b('document_id', 'document_id'),
            _b('colour_space', 'colour_space'),
            _b('method', 'method'),
        ),
        (),
    ),
    'cad_spatial_resolution_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('method', 'method'),
        ),
        (),
    ),
    'cad_low_luminance_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('stray_light_control', 'stray_light_control'),
        ),
        (),
    ),
    'cad_dynamic_contrast_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('contrast_kind', 'contrast_kind'),
        ),
        (),
    ),
    'cad_display_wall_boundaries': (
        'payload_json',
        (
            _b('boundary_id', 'boundary_id'),
            _b('boundary_sha256', 'boundary_sha256'),
            _b('document_id', 'document_id'),
            _b('wall_kind', 'wall_kind'),
            _b('acoustic_transparency_claim', 'acoustic_transparency_claim'),
        ),
        (),
    ),
    'cad_wall_acoustic_impacts': (
        'payload_json',
        (
            _b('impact_id', 'impact_id'),
            _b('impact_sha256', 'impact_sha256'),
            _b('document_id', 'document_id'),
            _b('boundary_ref_id', 'boundary_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_panning_continuity_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('stimulus_kind', 'stimulus_kind'),
        ),
        (),
    ),
    'cad_subwoofer_localization_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('stimulus_kind', 'stimulus_kind'),
            _b('crossover_hz', 'crossover_hz'),
        ),
        (),
    ),
    'cad_groupdelay_audibility': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('stimulus_kind', 'stimulus_kind'),
            _b('peak_delay_ms', 'peak_delay_ms'),
            _b('frequency_hz', 'frequency_hz'),
        ),
        (),
    ),
    'cad_headphone_coupling_evidence': (
        'payload_json',
        (
            _b('coupling_id', 'coupling_id'),
            _b('coupling_sha256', 'coupling_sha256'),
            _b('document_id', 'document_id'),
            _b('compensation_kind', 'compensation_kind'),
            _b('fit_state', 'fit_state'),
        ),
        (),
    ),
    'cad_structureborne_paths': (
        'payload_json',
        (
            _b('path_id', 'path_id'),
            _b('path_sha256', 'path_sha256'),
            _b('document_id', 'document_id'),
            _b('source_kind', 'source_kind'),
            _b('mount_kind', 'mount_kind'),
        ),
        (),
    ),
    'cad_spatial_remapping_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('remap_mode', 'remap_mode'),
        ),
        (),
    ),
    'cad_codec_fidelity_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('media_kind', 'media_kind'),
            _b('codec_family', 'codec_family'),
        ),
        (),
    ),
    'cad_fft_spectral_estimator_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('window_kind', 'window_kind'),
            _b('enbw_bins', 'enbw_bins'),
        ),
        (),
    ),
    'cad_clock_domain_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('domain_kind', 'domain_kind'),
            _b('lock_state', 'lock_state'),
        ),
        (),
    ),
    'cad_external_fact_claims': (
        'payload_json',
        (
            _b('claim_id', 'claim_id'),
            _b('claim_sha256', 'claim_sha256'),
            _b('document_id', 'document_id'),
            _b('subject', 'subject'),
            _b('published_on', 'published_on'),
        ),
        (),
    ),
    'cad_fact_conflict_resolutions': (
        'payload_json',
        (
            _b('resolution_id', 'resolution_id'),
            _b('resolution_sha256', 'resolution_sha256'),
            _b('document_id', 'document_id'),
            _b('resolution_kind', 'resolution_kind'),
        ),
        (),
    ),
    'cad_bom_estimates': (
        'payload_json',
        (
            _b('estimate_id', 'estimate_id'),
            _b('estimate_sha256', 'estimate_sha256'),
            _b('document_id', 'document_id'),
            _b('bom_version', 'bom_version'),
        ),
        (),
    ),
    'cad_cadence_delivery_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('content_cadence_kind', 'content_cadence_kind'),
            _b('refresh_relationship', 'refresh_relationship'),
        ),
        (),
    ),
    'cad_reference_room_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('framework', 'framework'),
        ),
        (),
    ),
    'cad_verification_requirements': (
        'payload_json',
        (
            _b('requirement_id', 'requirement_id'),
            _b('requirement_sha256', 'requirement_sha256'),
            _b('document_id', 'document_id'),
            _b('issue_ref', 'issue_ref'),
            _b('evaluator_kind', 'evaluator_kind'),
        ),
        (),
    ),
    'cad_verification_closures': (
        'payload_json',
        (
            _b('closure_id', 'closure_id'),
            _b('closure_sha256', 'closure_sha256'),
            _b('document_id', 'document_id'),
            _b('evaluator_verdict', 'evaluator_verdict'),
        ),
        (),
    ),

    # REV59-CLOSEAUX: manifest-gate bridge.
    'cad_manifest_gates': (
        'payload_json',
        (
            _b('gate_id', 'gate_id'),
            _b('gate_sha256', 'gate_sha256'),
            _b('document_id', 'document_id'),
            _b('issue_ref', 'issue_ref'),
            _b('check_kind', 'check_kind'),
        ),
        (),
    ),
    'cad_gate_run_results': (
        'payload_json',
        (
            _b('result_id', 'result_id'),
            _b('result_sha256', 'result_sha256'),
            _b('document_id', 'document_id'),
            _b('gate_ref_id', 'gate_ref_id'),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    # REV59-UNITS: #728 typed physical quantity.
    'cad_typed_quantities': (
        'payload_json',
        (
            _b('quantity_id', 'quantity_id'),
            _b('quantity_sha256', 'quantity_sha256'),
            _b('document_id', 'document_id'),
            _b('quantity_kind', 'quantity_kind'),
            _b('value_kind', 'value_kind'),
            _b('canonical_value', 'canonical_value'),
            _b('canonical_unit', 'canonical_unit'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_quantity_operations': (
        'payload_json',
        (
            _b('operation_id', 'operation_id'),
            _b('operation_sha256', 'operation_sha256'),
            _b('document_id', 'document_id'),
            _b('operation', 'operation'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV59-UNITS: #730 engineering-assumption ledger.
    'cad_engineering_assumptions': (
        'payload_json',
        (
            _b('assumption_id', 'assumption_id'),
            _b('assumption_sha256', 'assumption_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('assumption_kind', 'assumption_kind'),
            _b('evidence_state', 'evidence_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_assumption_resolutions': (
        'payload_json',
        (
            _b('resolution_id', 'resolution_id'),
            _b('resolution_sha256', 'resolution_sha256'),
            _b('document_id', 'document_id'),
            _b('assumption_ref_id', 'assumption_ref', 'ref_id'),
            _b('resolution_state', 'resolution_state'),
            _b('resolved_at_utc', 'resolved_at_utc'),
        ),
        (),
    ),
    'cad_permissible_use_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('intended_use', 'intended_use'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV59-UNITS: #720 perceptual relevance / audibility.
    'cad_perceptual_model_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('model_kind', 'model_kind'),
            _b('scope_class', 'applicability', 'scope_class'),
            _b('literature_ref', 'literature_ref'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_audibility_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('difference_ref_id', 'difference_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV59-UNITS: #719 residual diagnostic-hypothesis.
    'cad_diagnostic_cases': (
        'payload_json',
        (
            _b('case_id', 'case_id'),
            _b('case_sha256', 'case_sha256'),
            _b('document_id', 'document_id'),
            _b('symptom_ref_id', 'symptom_ref', 'ref_id'),
            _b('status', 'status'),
            _b('opened_at_utc', 'opened_at_utc'),
        ),
        (),
    ),
    'cad_diagnostic_hypotheses': (
        'payload_json',
        (
            _b('hypothesis_id', 'hypothesis_id'),
            _b('hypothesis_sha256', 'hypothesis_sha256'),
            _b('document_id', 'document_id'),
            _b('case_ref_id', 'case_ref', 'ref_id'),
            _b('cause_family', 'cause_family'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_diagnostic_tests': (
        'payload_json',
        (
            _b('test_id', 'test_id'),
            _b('test_sha256', 'test_sha256'),
            _b('document_id', 'document_id'),
            _b('case_ref_id', 'case_ref', 'ref_id'),
            _b('test_kind', 'test_kind'),
            _b('verdict', 'verdict'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_diagnostic_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('case_ref_id', 'case_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV60-EDGE: #779 sub-20 Hz / infrasonic acoustics.
    'cad_ulf_acoustic_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('request_low_hz', 'request_low_hz'),
            _b('request_high_hz', 'request_high_hz'),
        ),
        (),
    ),
    'cad_infrasonic_measurement_capabilities': (
        'payload_json',
        (
            _b('capability_id', 'capability_id'),
            _b('capability_sha256', 'capability_sha256'),
            _b('document_id', 'document_id'),
            _b('capability_state', 'capability_state'),
        ),
        (),
    ),
    'cad_ulf_acoustic_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('capability_ref_id', 'capability_ref', 'ref_id'),
            _b('quantity_kind', 'quantity_kind'),
        ),
        (),
    ),
    'cad_ulf_system_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('qualification_state', 'qualification_state'),
        ),
        (),
    ),
    # REV60-EDGE: #781 external noise ingress / façade isolation.
    'cad_external_noise_ingress_scenarios': (
        'payload_json',
        (
            _b('scenario_id', 'scenario_id'),
            _b('scenario_sha256', 'scenario_sha256'),
            _b('document_id', 'document_id'),
            _b('source_kind', 'source_kind'),
        ),
        (),
    ),
    'cad_facade_transmission_models': (
        'payload_json',
        (
            _b('model_id', 'model_id'),
            _b('model_sha256', 'model_sha256'),
            _b('document_id', 'document_id'),
            _b('envelope_state', 'envelope_state'),
        ),
        (),
    ),
    'cad_external_noise_ingress_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_ref_id', 'scenario_ref', 'ref_id'),
            _b('model_ref_id', 'model_ref', 'ref_id'),
            _b('measurement_class', 'measurement_class'),
            _b('domain_state', 'domain_state'),
        ),
        (),
    ),
    'cad_indoor_noise_ingress_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_ref_id', 'scenario_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # REV60-EDGE: #782 material fire-safety evidence.
    'cad_fire_safety_evidence_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('project_class', 'project_class'),
            _b('approval_status', 'approval_status'),
        ),
        (),
    ),
    'cad_material_reaction_to_fire_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('evidence_kind', 'evidence_kind'),
            _b('specimen_applicability', 'specimen_applicability'),
        ),
        (),
    ),
    'cad_installed_material_safety_requirements': (
        'payload_json',
        (
            _b('requirement_id', 'requirement_id'),
            _b('requirement_sha256', 'requirement_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('state', 'state'),
        ),
        (),
    ),
    'cad_fire_safety_approval_refs': (
        'payload_json',
        (
            _b('approval_id', 'approval_id'),
            _b('approval_sha256', 'approval_sha256'),
            _b('document_id', 'document_id'),
            _b('requirement_ref_id', 'requirement_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # REV60-EDGE: #783 accessible media playback.
    'cad_accessible_media_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_source', 'profile_source'),
        ),
        (),
    ),
    'cad_caption_presentation_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('component_kind', 'component_kind'),
            _b('reached_stage', 'reached_stage'),
            _b('readability_state', 'readability_state'),
        ),
        (),
    ),
    'cad_audio_description_playback_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('mix_semantics', 'mix_semantics'),
            _b('output_state', 'output_state'),
        ),
        (),
    ),
    'cad_accessible_playback_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # REV60-COLLABENV: #721 collaboration/approval authority.
    'cad_collaboration_actors': (
        'payload_json',
        (
            _b('actor_id', 'actor_id'),
            _b('actor_sha256', 'actor_sha256'),
            _b('document_id', 'document_id'),
            _b('identity_basis', 'identity_basis'),
        ),
        (),
    ),
    'cad_revision_authorship': (
        'payload_json',
        (
            _b('authorship_id', 'authorship_id'),
            _b('authorship_sha256', 'authorship_sha256'),
            _b('document_id', 'document_id'),
            _b('author_ref_id', 'author_ref', 'ref_id'),
            _b('parent_revision_id', 'parent_revision_id', optional=True),
            _b('result_revision_id', 'result_revision_id'),
            _b('change_scope', 'change_scope'),
        ),
        (),
    ),
    'cad_information_states': (
        'payload_json',
        (
            _b('state_id', 'state_id'),
            _b('state_sha256', 'state_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('state', 'state'),
            _b('actor_ref_id', 'actor_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_approval_records': (
        'payload_json',
        (
            _b('approval_id', 'approval_id'),
            _b('approval_sha256', 'approval_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('scope_kind', 'scope_kind'),
            _b('approver_ref_id', 'approver_ref', 'ref_id'),
            _b('decision', 'decision'),
        ),
        (),
    ),
    'cad_review_decisions': (
        'payload_json',
        (
            _b('decision_id', 'decision_id'),
            _b('decision_sha256', 'decision_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('author_ref_id', 'author_ref', 'ref_id'),
            _b('kind', 'kind'),
            _b('status', 'status'),
        ),
        (),
    ),
    'cad_sibling_divergences': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('verdict', 'verdict'),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    'cad_conflict_resolutions': (
        'payload_json',
        (
            _b('resolution_id', 'resolution_id'),
            _b('resolution_sha256', 'resolution_sha256'),
            _b('document_id', 'document_id'),
            _b('assessment_ref_id', 'assessment_ref', 'ref_id'),
            _b('resolution_kind', 'resolution_kind'),
            _b('resolver_ref_id', 'resolver_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_branch_proposals': (
        'payload_json',
        (
            _b('proposal_id', 'proposal_id'),
            _b('proposal_sha256', 'proposal_sha256'),
            _b('document_id', 'document_id'),
            _b('proposal_kind', 'proposal_kind'),
            _b('author_ref_id', 'author_ref', 'ref_id'),
            _b('status', 'status'),
        ),
        (),
    ),
    'cad_proposal_promotions': (
        'payload_json',
        (
            _b('promotion_id', 'promotion_id'),
            _b('promotion_sha256', 'promotion_sha256'),
            _b('document_id', 'document_id'),
            _b('proposal_ref_id', 'proposal_ref', 'ref_id'),
            _b('promoted_by_ref_id', 'promoted_by_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_client_acceptances': (
        'payload_json',
        (
            _b('acceptance_id', 'acceptance_id'),
            _b('acceptance_sha256', 'acceptance_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('client_ref_id', 'client_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_collaboration_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('kind', 'kind'),
            _b('actor_ref_id', 'actor_ref', 'ref_id', optional=True),
        ),
        (),
    ),
    # REV60-COLLABENV: #776 material environmental/aging applicability.
    'cad_material_condition_states': (
        'payload_json',
        (
            _b('condition_id', 'condition_id'),
            _b('condition_sha256', 'condition_sha256'),
            _b('document_id', 'document_id'),
            _b('material_ref_id', 'material_ref', 'ref_id'),
            _b('context', 'context'),
            _b('condition_state', 'condition_state'),
        ),
        (),
    ),
    'cad_material_durability_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('material_family_ref_id', 'material_family_ref', 'ref_id'),
            _b('evidence_class', 'evidence_class'),
            _b('change_direction', 'change_direction'),
        ),
        (),
    ),
    'cad_material_evidence_applicability': (
        'payload_json',
        (
            _b('applicability_id', 'applicability_id'),
            _b('applicability_sha256', 'applicability_sha256'),
            _b('document_id', 'document_id'),
            _b('material_ref_id', 'material_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_material_reinspections': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('installed_state_ref_id', 'installed_state_ref', 'ref_id'),
            _b('trigger', 'trigger'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # #806 calibration deployment/verification loop.
    'cad_deployment_capability_declarations': (
        'payload_json',
        (
            _b('declaration_id', 'declaration_id'),
            _b('declaration_sha256', 'declaration_sha256'),
            _b('document_id', 'document_id'),
            _b('adapter_id', 'adapter_id'),
            _b('adapter_kind', 'adapter_kind'),
        ),
        (),
    ),
    'cad_calibration_deployments': (
        'payload_json',
        (
            _b('deployment_id', 'deployment_id'),
            _b('deployment_sha256', 'deployment_sha256'),
            _b('document_id', 'document_id'),
            _b('deployment_state', 'deployment_state'),
            _b('evidence_mode', 'evidence_mode'),
            _b('target_class', 'target_class'),
        ),
        (),
    ),
    'cad_deployment_effectiveness_reports': (
        'payload_json',
        (
            _b('report_id', 'report_id'),
            _b('report_sha256', 'report_sha256'),
            _b('document_id', 'document_id'),
            _b('deployment_ref_id', 'deployment_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_deployment_rollbacks': (
        'payload_json',
        (
            _b('rollback_id', 'rollback_id'),
            _b('rollback_sha256', 'rollback_sha256'),
            _b('document_id', 'document_id'),
            _b('deployment_ref_id', 'deployment_ref', 'ref_id'),
        ),
        (),
    ),
    # REV64: #838 CamillaDSP deploy/read-back/rollback evidence.
    'cad_camilladsp_deployment_sessions': (
        'payload_json',
        (
            _b('session_id', 'session_id'),
            _b('session_sha256', 'session_sha256'),
            _b('document_id', 'document_id'),
            _b('deployment_ref_id', 'deployment_ref', 'ref_id'),
            _b('binding_sha256', 'binding_sha256'),
            _b('candidate_config_sha256', 'candidate_config_sha256'),
        ),
        (),
    ),
    'cad_camilladsp_runtime_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('binding_sha256', 'binding_sha256'),
            _b('deployment_ref_id', 'deployment_ref', 'ref_id',
               optional=True),
            _b('processing_state', 'processing_state', optional=True),
        ),
        (),
    ),
    'cad_camilladsp_rollback_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('deployment_ref_id', 'deployment_ref', 'ref_id'),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    # REV67: #878 capability-negotiated deployment pipeline.
    'cad_deployment_pipeline_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('pipeline_id', 'pipeline_id'),
            _b('binding_sha256', 'binding_sha256'),
            _b('adapter_id', 'adapter_id'),
            _b('target_ref', 'target_ref'),
            _b('stage', 'stage'),
            _b('evidence_strength', 'evidence_strength'),
            _b('readback_verdict', 'readback_verdict'),
            _b('partial_write', 'partial_write'),
        ),
        (),
    ),
    'cad_deployment_operator_authorizations': (
        'payload_json',
        (
            _b('authorization_id', 'authorization_id'),
            _b('authorization_sha256', 'authorization_sha256'),
            _b('document_id', 'document_id'),
            _b('pipeline_id', 'pipeline_id'),
            _b('scope', 'scope'),
            _b('operator_id', 'operator_id'),
            _b('consumed', 'consumed'),
        ),
        (),
    ),
    'cad_assisted_instruction_manifests': (
        'payload_json',
        (
            _b('manifest_id', 'manifest_id'),
            _b('manifest_sha256', 'manifest_sha256'),
            _b('document_id', 'document_id'),
            _b('target_ref', 'target_ref'),
        ),
        (),
    ),
    'cad_assisted_deployment_attestations': (
        'payload_json',
        (
            _b('attestation_id', 'attestation_id'),
            _b('attestation_sha256', 'attestation_sha256'),
            _b('document_id', 'document_id'),
            _b('manifest_ref_id', 'manifest_ref', 'ref_id'),
            _b('pipeline_id', 'pipeline_id', optional=True),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    'cad_apo_install_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('target_path_repr', 'target_path_repr'),
            _b('verification', 'verification'),
            _b('evidence_strength', 'evidence_strength'),
        ),
        (),
    ),
    # REV69: #887 adapter SDK contract descriptors + conformance results.
    'cad_adapter_sdk_descriptors': (
        'payload_json',
        (
            _b('descriptor_id', 'descriptor_id'),
            _b('descriptor_sha256', 'descriptor_sha256'),
            _b('document_id', 'document_id'),
            _b('adapter_id', 'adapter_id'),
            _b('adapter_version', 'adapter_version'),
            _b('adapter_kind', 'adapter_kind'),
            _b('device_family', 'device_family'),
            _b('sdk_version', 'sdk_version'),
            _b('contract_status', 'contract_status'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_adapter_conformance_results': (
        'payload_json',
        (
            _b('result_id', 'result_id'),
            _b('result_sha256', 'result_sha256'),
            _b('document_id', 'document_id'),
            _b('descriptor_sha256', 'descriptor_ref', 'ref_sha256'),
            _b('adapter_id', 'adapter_id'),
            _b('adapter_version', 'adapter_version'),
            _b('suite_version', 'suite_version'),
            _b('contract_version', 'contract_version'),
            _b('verdict', 'verdict'),
            _b('issued_at_utc', 'issued_at_utc'),
        ),
        (),
    ),
    # REV68: #880 owned-Windows UX acceptance evidence bundles.
    'cad_ux_acceptance_bundle_records': (
        'payload_json',
        (
            _b('bundle_id', 'bundle_id'),
            _b('bundle_sha256', 'bundle_sha256'),
            _b('document_id', 'document_id'),
            _b('matrix_id', 'matrix_id'),
            _b('row_id', 'row_id'),
            _b('run_attempt', 'run_attempt'),
            _b('scenario', 'scenario'),
            _b('scale_factor', 'scale_factor'),
            _b('capture_mode', 'capture_mode'),
            _b('verdict', 'verdict'),
            _b('review_state', 'review_state'),
            _b('checkpoints_total', 'checkpoints_total'),
            _b('checkpoints_finding', 'checkpoints_finding'),
            _b('manifest_sha256', 'manifest_sha256', optional=True),
            _b('bundle_ref', 'bundle_ref'),
            _b('finished_at_utc', 'finished_at_utc'),
        ),
        (),
    ),
    # #790 playback-electronics / electrical audio-path authority.
    'cad_electronic_audio_path_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('path_class', 'path_class'),
        ),
        (),
    ),
    'cad_electrical_transfer_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('channel_label', 'channel_label'),
            _b('measurement_class', 'measurement_class'),
            _b('deembedding_state', 'deembedding_state'),
        ),
        (),
    ),
    'cad_electronic_linearity_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('domain', 'domain'),
            _b('observed_regime', 'observed_regime'),
        ),
        (),
    ),
    'cad_playback_electronics_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('qualification_state', 'qualification_state'),
        ),
        (),
    ),
    # REV62: #792 lifecycle/supportability authority.
    'cad_supportability_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_label', 'profile_label'),
        ),
        (),
    ),
    'cad_supportability_dependencies': (
        'payload_json',
        (
            _b('dependency_id', 'dependency_id'),
            _b('dependency_sha256', 'dependency_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id', optional=True),
            _b('function_label', 'function_label'),
            _b('dependency_kind', 'dependency_kind'),
        ),
        (),
    ),
    'cad_lifecycle_risk_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('kind', 'kind'),
            _b('support_state', 'support_state'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_offline_continuity_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('function_label', 'function_label'),
            _b('condition', 'condition'),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    'cad_replacement_readiness': (
        'payload_json',
        (
            _b('readiness_id', 'readiness_id'),
            _b('readiness_sha256', 'readiness_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('requalification_ref_id', 'requalification_scope_ref',
               'ref_id', optional=True),
        ),
        (),
    ),

    # REV62: #793 single-channel live-observation authority.
    'cad_realtime_measurement_sessions': (
        'payload_json',
        (
            _b('session_id', 'session_id'),
            _b('session_sha256', 'session_sha256'),
            _b('document_id', 'document_id'),
            _b('input_channel', 'input_channel'),
            _b('input_domain', 'input_domain'),
        ),
        (),
    ),
    'cad_live_spectrum_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('mode', 'mode'),
            _b('capture_state', 'capture_state'),
        ),
        (),
    ),
    'cad_spl_time_histories': (
        'payload_json',
        (
            _b('history_id', 'history_id'),
            _b('history_sha256', 'history_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('quantity', 'quantity'),
        ),
        (),
    ),
    'cad_captured_live_traces': (
        'payload_json',
        (
            _b('trace_id', 'trace_id'),
            _b('trace_sha256', 'trace_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('capture_kind', 'capture_kind'),
        ),
        (),
    ),
    'cad_live_event_annotations': (
        'payload_json',
        (
            _b('annotation_id', 'annotation_id'),
            _b('annotation_sha256', 'annotation_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('source', 'source'),
        ),
        (),
    ),
    # REV63: #809 benchmark-qualification authority.
    'cad_benchmark_scene_mappings': (
        'payload_json',
        (
            _b('mapping_id', 'mapping_id'),
            _b('mapping_sha256', 'mapping_sha256'),
            _b('document_id', 'document_id'),
            _b('asset_ref_id', 'asset_ref', 'ref_id'),
            _b('corpus_scene_id', 'corpus_scene_id'),
            _b('solver_path', 'solver_path'),
            _b('phenomenon_id', 'phenomenon_id'),
            _b('curvature_class', 'curvature_class'),
        ),
        (),
    ),
    'cad_benchmark_preregistrations': (
        'payload_json',
        (
            _b('preregistration_id', 'preregistration_id'),
            _b('preregistration_sha256', 'preregistration_sha256'),
            _b('document_id', 'document_id'),
            _b('mapping_ref_id', 'mapping_ref', 'ref_id'),
            _b('benchmark_sha256', 'benchmark_sha256'),
            _b('provider_id', 'provider_id'),
            _b('run_mode', 'run_mode'),
        ),
        (),
    ),
    'cad_benchmark_qualifications': (
        'payload_json',
        (
            _b('qualification_id', 'qualification_id'),
            _b('qualification_sha256', 'qualification_sha256'),
            _b('document_id', 'document_id'),
            _b('mapping_ref_id', 'mapping_ref', 'ref_id'),
            _b('preregistration_ref_id', 'preregistration_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('level_attained', 'level_attained'),
            _b('run_mode', 'run_mode'),
            _b('predictive', 'predictive'),
        ),
        (),
    ),
    # REV63: #810 uncertainty-aware validation authority.
    'cad_validation_uncertainty_protocols': (
        'payload_json',
        (
            _b('protocol_id', 'protocol_id'),
            _b('protocol_sha256', 'protocol_sha256'),
            _b('document_id', 'document_id'),
            _b('model_id', 'model_id'),
            _b('protocol_version', 'protocol_version'),
            _b('calibration_uncertainty_tuned', 'calibration_uncertainty_tuned'),
        ),
        (),
    ),
    'cad_observable_uncertainty_evaluations': (
        'payload_json',
        (
            _b('evaluation_id', 'evaluation_id'),
            _b('evaluation_sha256', 'evaluation_sha256'),
            _b('document_id', 'document_id'),
            _b('protocol_ref_id', 'protocol_ref', 'ref_id'),
            _b('candidate_id', 'candidate_id'),
            _b('split', 'split'),
            _b('observable_id', 'observable', 'observable_id'),
            _b('summary_verdict', 'summary_verdict'),
            _b(
                'dominant_uncertainty_category',
                'dominant_uncertainty_category',
            ),
        ),
        (),
    ),
    'cad_uncertainty_validation_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('protocol_ref_id', 'protocol_ref', 'ref_id'),
            _b(
                'validation_ref_id', 'validation_ref', 'ref_id',
                optional=True,
            ),
            _b('evidence_scope', 'evidence_scope'),
            _b('absolute_verdict', 'absolute_verdict'),
            _b('ranking_verdict', 'ranking_verdict'),
            _b('calibration_state', 'calibration_state'),
            _b(
                'model_form_discrepancy_suspected',
                'model_form_discrepancy_suspected',
            ),
        ),
        (),
    ),
    # REV63: #811 solver-confidence-bound authority.
    'cad_material_input_authorities': (
        'payload_json',
        (
            _b('material_id', 'material_id'),
            _b('material_sha256', 'material_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('authority_class', 'authority_class'),
            _b('quantity', 'quantity'),
            _b('method_class', 'method_class'),
        ),
        (),
    ),
    'cad_source_directivity_authorities': (
        'payload_json',
        (
            _b('directivity_id', 'directivity_id'),
            _b('directivity_sha256', 'directivity_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('provenance_class', 'provenance_class'),
            _b('format_compliance', 'format_compliance'),
            _b('coverage', 'coverage'),
        ),
        (),
    ),
    'cad_geometry_input_authorities': (
        'payload_json',
        (
            _b('geometry_id', 'geometry_id'),
            _b('geometry_sha256', 'geometry_sha256'),
            _b('document_id', 'document_id'),
            _b('fidelity_class', 'fidelity_class'),
            _b('scene_revision_ref_id', 'scene_revision_ref', 'ref_id', optional=True),
        ),
        (),
    ),
    'cad_pose_input_authorities': (
        'payload_json',
        (
            _b('pose_id', 'pose_id'),
            _b('pose_sha256', 'pose_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_kind', 'subject_kind'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('authority_class', 'authority_class'),
            _b('modal_sensitivity', 'modal_sensitivity'),
        ),
        (),
    ),
    'cad_solver_input_envelopes': (
        'payload_json',
        (
            _b('envelope_id', 'envelope_id'),
            _b('envelope_sha256', 'envelope_sha256'),
            _b('document_id', 'document_id'),
            _b('solver_request_ref_id', 'solver_request_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_claim_bound_records': (
        'payload_json',
        (
            _b('record_id', 'record_id'),
            _b('record_sha256', 'record_sha256'),
            _b('document_id', 'document_id'),
            _b('input_envelope_ref_id', 'input_envelope_ref', 'ref_id'),
            _b('evaluator_version', 'evaluator_version'),
        ),
        (),
    ),
    # REV63: #789 surge/lightning transient-protection authority.
    'cad_protected_paths': (
        'payload_json',
        (
            _b('path_id', 'path_id'),
            _b('path_sha256', 'path_sha256'),
            _b('document_id', 'document_id'),
            _b('load_ref_id', 'load_ref', 'ref_id'),
            _b('path_kind', 'path_kind'),
            _b('requires_protection', 'requires_protection'),
        ),
        (),
    ),
    'cad_transient_protection_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('jurisdiction_country', 'jurisdiction_country',
               optional=True),
            _b('code_family', 'code_family', optional=True),
            _b('code_edition', 'code_edition', optional=True),
            _b('requires_qualified_review', 'requires_qualified_review'),
        ),
        (),
    ),
    'cad_spd_evidence': (
        'payload_json',
        (
            _b('spd_id', 'spd_id'),
            _b('spd_sha256', 'spd_sha256'),
            _b('document_id', 'document_id'),
            _b('path_ref_id', 'path_ref', 'ref_id'),
            _b('domain', 'domain'),
            _b('spd_type_class', 'spd_type_class'),
            _b('standard_profile', 'standard_profile'),
            _b('install_state', 'install_state'),
            _b('evidence_basis', 'evidence_basis'),
        ),
        (),
    ),
    'cad_transient_protection_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('spd_ref_id', 'spd_ref', 'ref_id'),
            _b('status', 'status'),
            _b('status_source', 'status_source'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_transient_protection_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('event_kind', 'event_kind'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_transient_protection_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('path_ref_id', 'path_ref', 'ref_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id', optional=True),
            _b('verdict', 'verdict'),
            _b('coordination_state', 'coordination_state'),
            _b(
                'professional_review_required',
                'professional_review_required',
            ),
        ),
        (),
    ),
    # REV63: #791 operational-energy authority.
    'cad_device_power_mode_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('device_ref_id', 'device_ref', 'ref_id'),
            _b(
                'device_state_ref_id', 'device_state_ref', 'ref_id',
                optional=True,
            ),
            _b('mode', 'mode'),
            _b('evidence_class', 'evidence_class'),
            _b('standard_profile', 'standard_profile'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_networked_standby_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('device_ref_id', 'device_ref', 'ref_id'),
            _b('standard_profile', 'standard_profile'),
            _b('evidence_class', 'evidence_class'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_operational_energy_scenarios': (
        'payload_json',
        (
            _b('scenario_id', 'scenario_id'),
            _b('scenario_sha256', 'scenario_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_kind', 'scenario_kind'),
            _b('label', 'label'),
        ),
        (),
    ),
    'cad_energy_use_derivations': (
        'payload_json',
        (
            _b('derivation_id', 'derivation_id'),
            _b('derivation_sha256', 'derivation_sha256'),
            _b('document_id', 'document_id'),
            _b('scenario_ref_id', 'scenario_ref', 'ref_id'),
            _b('derivation_kind', 'derivation_kind'),
            _b('derivation_version', 'derivation_version'),
        ),
        (),
    ),
    # REV63: #813 owned-room campaign authority.
    'cad_campaign_preregistrations': (
        'payload_json',
        (
            _b('preregistration_id', 'preregistration_id'),
            _b('preregistration_sha256', 'preregistration_sha256'),
            _b('document_id', 'document_id'),
            _b('protocol_id', 'protocol_id'),
            _b('protocol_version', 'protocol_version'),
            _b('scene_ref_id', 'scene_ref', 'ref_id'),
            _b('solver_ref_id', 'solver_ref', 'ref_id'),
            _b('preregistered_at_utc', 'preregistered_at_utc'),
        ),
        (),
    ),
    'cad_campaign_measurements': (
        'payload_json',
        (
            _b('measurement_id', 'measurement_id'),
            _b('measurement_sha256', 'measurement_sha256'),
            _b('document_id', 'document_id'),
            _b('campaign_ref_id', 'campaign_ref', 'ref_id'),
            _b('role', 'role'),
            _b('condition_id', 'condition_id'),
            _b('acquired_at_utc', 'acquired_at_utc'),
        ),
        (),
    ),
    # REV63: #812 hybrid-composition validation authority.
    'cad_hybrid_composition_validation_specs': (
        'payload_json',
        (
            _b('spec_id', 'spec_id'),
            _b('spec_sha256', 'spec_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('composition_semantics', 'composition_semantics'),
            _b('late_field_composed', 'late_field_composed'),
            _b(
                'grid_reconciliation_required',
                'grid_reconciliation_required',
            ),
        ),
        (),
    ),
    'cad_hybrid_validation_evidence': (
        'payload_json',
        (
            _b('evidence_id', 'evidence_id'),
            _b('evidence_sha256', 'evidence_sha256'),
            _b('document_id', 'document_id'),
            _b('spec_ref_id', 'spec_ref', 'ref_id'),
            _b('evidence_kind', 'evidence_kind'),
            _b('outcome', 'outcome'),
            _b('domain_kind', 'domain_kind', optional=True),
            _b('solver_path', 'solver_path', optional=True),
            _b('reference_class', 'reference_class', optional=True),
        ),
        (),
    ),
    'cad_hybrid_validation_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('spec_ref_id', 'spec_ref', 'ref_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('qualification_level', 'qualification_level'),
        ),
        (),
    ),
    'cad_campaign_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('campaign_ref_id', 'campaign_ref', 'ref_id'),
            _b('promotion_outcome', 'promotion_outcome'),
            _b('concluded_at_utc', 'concluded_at_utc'),
        ),
        (),
    ),
    # REV63: #801 production-readiness gate.
    'cad_production_readiness_decisions': (
        'payload_json',
        (
            _b('decision_id', 'decision_id'),
            _b('decision_sha256', 'decision_sha256'),
            _b('document_id', 'document_id'),
            _b('scene_ref_id', 'scene_ref', 'ref_id'),
            _b('solver_version_ref_id', 'solver_version_ref', 'ref_id'),
            _b('solver_path_kind', 'solver_path_kind'),
            _b('outcome', 'outcome'),
            _b('decided_at_utc', 'decided_at_utc'),
        ),
        (),
    ),
    'cad_recommendation_surface_decisions': (
        'payload_json',
        (
            _b('surface_decision_id', 'surface_decision_id'),
            _b('surface_decision_sha256', 'surface_decision_sha256'),
            _b('document_id', 'document_id'),
            _b('decision_ref_id', 'decision_ref', 'ref_id'),
            _b('outcome_at_issue', 'outcome_at_issue'),
            _b('issued_at_utc', 'issued_at_utc'),
        ),
        (),
    ),
    # REV64: #838 delegated-provider + file-export authority.
    'cad_delegated_provider_manifests': (
        'payload_json',
        (
            _b('manifest_id', 'manifest_id'),
            _b('manifest_sha256', 'manifest_sha256'),
            _b('document_id', 'document_id'),
            _b('provider_class', 'provider_class'),
            _b('provider_id', 'provider_id'),
            _b('adapter_id', 'adapter_id'),
            _b('endpoint_kind', 'endpoint_kind'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_provider_acquisitions': (
        'payload_json',
        (
            _b('acquisition_id', 'acquisition_id'),
            _b('acquisition_sha256', 'acquisition_sha256'),
            _b('document_id', 'document_id'),
            _b('manifest_ref_id', 'manifest_ref', 'ref_id'),
            _b('capability', 'capability'),
            _b('outcome', 'outcome'),
            _b('observed_at_utc', 'observed_at_utc'),
        ),
        (),
    ),
    'cad_file_deployments': (
        'payload_json',
        (
            _b('file_deployment_id', 'file_deployment_id'),
            _b('file_deployment_sha256', 'file_deployment_sha256'),
            _b('document_id', 'document_id'),
            _b('target_class', 'target_class'),
            _b('file_state', 'file_state'),
            _b('runtime_state', 'runtime_state'),
            _b('evidence_mode', 'evidence_mode'),
            _b('roundtrip_verdict', 'roundtrip_verdict'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_commissioning_orch_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('run_sha256', 'run_sha256'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
            _b('provider_manifest_ref_id', 'provider_manifest_ref',
               'ref_id'),
            _b('device_binding_sha256', 'device_binding_sha256'),
        ),
        (),
    ),
    'cad_commissioning_orch_transitions': (
        'payload_json',
        (
            _b('transition_id', 'transition_id'),
            _b('transition_sha256', 'transition_sha256'),
            _b('document_id', 'document_id'),
            _b('run_ref_id', 'run_ref', 'ref_id'),
            _b('seq_no', 'seq'),
            _b('event_kind', 'event_kind'),
            _b('outcome', 'outcome'),
            _b('to_stage', 'to_stage'),
        ),
        (),
    ),
    'cad_commissioning_orch_authorizations': (
        'payload_json',
        (
            _b('authorization_id', 'authorization_id'),
            _b('authorization_sha256', 'authorization_sha256'),
            _b('document_id', 'document_id'),
            _b('run_ref_id', 'run_ref', 'ref_id'),
            _b('scope', 'scope'),
        ),
        (),
    ),
    'cad_commissioning_orch_rollbacks': (
        'payload_json',
        (
            _b('rollback_id', 'rollback_id'),
            _b('rollback_sha256', 'rollback_sha256'),
            _b('document_id', 'document_id'),
            _b('run_ref_id', 'run_ref', 'ref_id'),
            _b('deployment_ref_id', 'deployment_ref', 'ref_id'),
            _b('outcome', 'outcome'),
        ),
        (),
    ),
    'cad_commissioning_orch_before_after': (
        'payload_json',
        (
            _b('comparison_id', 'comparison_id'),
            _b('comparison_sha256', 'comparison_sha256'),
            _b('document_id', 'document_id'),
            _b('run_ref_id', 'run_ref', 'ref_id'),
            _b('deployment_ref_id', 'deployment_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_commissioning_orch_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('run_ref_id', 'run_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    # REV68: #885 guided troubleshooting orchestrator.
    'cad_diagnostic_sessions': (
        'payload_json',
        (
            _b('session_id', 'session_id'),
            _b('session_sha256', 'session_sha256'),
            _b('document_id', 'document_id'),
            _b('fault_tree_id', 'fault_tree_id'),
            _b('case_ref_id', 'case_ref', 'ref_id'),
            _b('symptom_ref_id', 'symptom_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_diagnostic_session_transitions': (
        'payload_json',
        (
            _b('transition_id', 'transition_id'),
            _b('transition_sha256', 'transition_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('seq_no', 'seq'),
            _b('event_kind', 'event_kind'),
            _b('outcome', 'outcome'),
            _b('to_stage', 'to_stage'),
        ),
        (),
    ),
    'cad_diagnostic_session_hypotheses': (
        'payload_json',
        (
            _b('entry_id', 'entry_id'),
            _b('entry_sha256', 'entry_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('hypothesis_key', 'hypothesis_key'),
            _b('hypothesis_ref_id', 'hypothesis_ref', 'ref_id'),
            _b('cause_family', 'cause_family'),
        ),
        (),
    ),
    'cad_diagnostic_test_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('template_id', 'template_id'),
            _b('plan_seq', 'plan_seq'),
            _b('mechanism', 'mechanism'),
            _b('safety_class', 'safety_class'),
        ),
        (),
    ),
    'cad_diagnostic_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('mechanism', 'mechanism'),
        ),
        (),
    ),
    'cad_diagnostic_evidence_updates': (
        'payload_json',
        (
            _b('update_id', 'update_id'),
            _b('update_sha256', 'update_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('observation_ref_id', 'observation_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_diagnostic_resolutions': (
        'payload_json',
        (
            _b('resolution_id', 'resolution_id'),
            _b('resolution_sha256', 'resolution_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('verdict_ref_id', 'verdict_ref', 'ref_id'),
        ),
        (),
    ),
    'cad_diagnostic_authorizations': (
        'payload_json',
        (
            _b('authorization_id', 'authorization_id'),
            _b('authorization_sha256', 'authorization_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
        ),
        (),
    ),
    # REV69: #890 credential vault — non-secret references + lifecycle
    # events (secret material never lives in the schema).
    'cad_credential_references': (
        'payload_json',
        (
            _b('reference_id', 'reference_id'),
            _b('reference_sha256', 'reference_sha256'),
            _b('credential_id', 'credential_id'),
            _b('document_id', 'document_id'),
            _b('scope_kind', 'scope_kind'),
            _b('scope_ref', 'scope_ref'),
            _b('credential_type', 'credential_type'),
            _b('vault_scope', 'vault_scope'),
            _b('vault_key', 'vault_key'),
            _b('state', 'state'),
            _b('version', 'version'),
            _b('identity_hint', 'identity_hint', optional=True),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_credential_lifecycle_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('credential_id', 'credential_id'),
            _b('event_kind', 'event_kind'),
            _b('actor', 'actor'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    # REV69: #889 safe application updater.
    'cad_update_packages': (
        'payload_json',
        (
            _b('package_id', 'package_id'),
            _b('package_sha256', 'package_sha256'),
            _b('document_id', 'document_id'),
            _b('target_version', 'target_version'),
            _b('channel', 'channel'),
            _b('signature_status', 'signature', 'status'),
            _b('declared_by', 'declared_by'),
        ),
        (),
    ),
    'cad_update_sessions': (
        'payload_json',
        (
            _b('session_id', 'session_id'),
            _b('session_sha256', 'session_sha256'),
            _b('document_id', 'document_id'),
            _b('package_ref_id', 'package_ref', 'ref_id'),
            _b('signature_policy', 'signature_policy'),
            _b('opened_by', 'opened_by'),
        ),
        (),
    ),
    'cad_update_transitions': (
        'payload_json',
        (
            _b('transition_id', 'transition_id'),
            _b('transition_sha256', 'transition_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('seq_no', 'seq'),
            _b('event_kind', 'event_kind'),
            _b('outcome', 'outcome'),
            _b('to_stage', 'to_stage'),
        ),
        (),
    ),
    'cad_update_preflight_reports': (
        'payload_json',
        (
            _b('report_id', 'report_id'),
            _b('report_sha256', 'report_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('package_ref_id', 'package_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_update_restore_points': (
        'payload_json',
        (
            _b('restore_id', 'restore_id'),
            _b('restore_sha256', 'restore_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('data_backup_kind', 'data_backup_kind'),
            _b('migration_boundary', 'migration_boundary'),
            _b('rollback_scope_capable', 'rollback_scope_capable'),
        ),
        (),
    ),
    'cad_update_health_reports': (
        'payload_json',
        (
            _b('report_id', 'report_id'),
            _b('report_sha256', 'report_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_update_authorizations': (
        'payload_json',
        (
            _b('authorization_id', 'authorization_id'),
            _b('authorization_sha256', 'authorization_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('scope', 'scope'),
            _b('authorized_by', 'authorized_by'),
        ),
        (),
    ),
    'cad_update_outcomes': (
        'payload_json',
        (
            _b('outcome_id', 'outcome_id'),
            _b('outcome_sha256', 'outcome_sha256'),
            _b('document_id', 'document_id'),
            _b('session_ref_id', 'session_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('rollback_scope', 'rollback_scope'),
        ),
        (),
    ),
    # REV69: #888 headless CLI automation authority.
    'cad_headless_run_records': (
        'payload_json',
        (
            _b('run_record_id', 'run_record_id'),
            _b('run_sha256', 'run_sha256'),
            _b('document_id', 'document_id'),
            _b('verb', 'verb'),
            _b('outcome', 'outcome'),
            _b('dry_run', 'dry_run'),
            _b('spec_sha256', 'spec_sha256'),
            _b('tool_commit_sha', 'tool_commit_sha', optional=True),
            _b('backend_id', 'backend_id', optional=True),
            _b('started_at_utc', 'started_at_utc'),
            _b('finished_at_utc', 'finished_at_utc'),
        ),
        (),
    ),
    # REV66: #866 geometry intake readiness authority.
    'cad_geometry_intake_reports': (
        'payload_json',
        (
            _b('report_id', 'report_id'),
            _b('report_sha256', 'report_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('subject_sha256', 'subject_sha256'),
            _b('defect_count', 'defect_count'),
            _b('critical_count', 'critical_count'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_geometry_repair_proposals': (
        'payload_json',
        (
            _b('proposal_id', 'proposal_id'),
            _b('proposal_sha256', 'proposal_sha256'),
            _b('document_id', 'document_id'),
            _b('report_ref_id', 'report_ref', 'ref_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('generated_by', 'generated_by'),
            _b('generated_at_utc', 'generated_at_utc'),
        ),
        (),
    ),
    'cad_geometry_repair_acceptances': (
        'payload_json',
        (
            _b('acceptance_id', 'acceptance_id'),
            _b('acceptance_sha256', 'acceptance_sha256'),
            _b('document_id', 'document_id'),
            _b('proposal_ref_id', 'proposal_ref', 'ref_id'),
            _b('decided_by', 'decided_by'),
            _b('decided_at_utc', 'decided_at_utc'),
        ),
        (),
    ),
    'cad_derived_geometry_revisions': (
        'payload_json',
        (
            _b('derived_revision_id', 'derived_revision_id'),
            _b('derived_revision_sha256', 'derived_revision_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'source_subject_ref', 'ref_id'),
            _b('acceptance_ref_id', 'acceptance_ref', 'ref_id'),
            _b('derived_geometry_sha256', 'derived_geometry_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_sweep_stimulus_definitions': (
        'payload_json',
        (
            _b('stimulus_definition_id', 'stimulus_definition_id'),
            _b('stimulus_sha256', 'stimulus_sha256'),
            _b('document_id', 'document_id'),
            _b('generator_version', 'generator_version'),
            _b('sample_rate_hz', 'sample_rate_hz'),
            _b('start_frequency_hz', 'start_frequency_hz'),
            _b('end_frequency_hz', 'end_frequency_hz'),
            _b('duration_s', 'duration_s'),
            _b('level_dbfs', 'level_dbfs'),
            _b('repetitions', 'repetitions'),
            _b('params_sha256', 'params_sha256'),
            _b('samples_sha256', 'samples_sha256'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_geometry_solver_readiness': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('geometry_sha256', 'geometry_sha256'),
            _b('adapter_ref_id', 'adapter_descriptor_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_sweep_acquisition_runs': (
        'payload_json',
        (
            _b('acquisition_id', 'acquisition_id'),
            _b('acquisition_sha256', 'acquisition_sha256'),
            _b('document_id', 'document_id'),
            _b('run_id', 'run_id'),
            _b('engine_version', 'engine_version'),
            _b('backend_id', 'backend_id'),
            _b('backend_version', 'backend_version'),
            _b('backend_is_simulated', 'backend_is_simulated'),
            _b('stimulus_ref_id', 'stimulus_ref', 'ref_id'),
            _b('playback_device_id', 'playback_device_id'),
            _b('capture_device_id', 'capture_device_id'),
            _b('requested_sample_rate_hz', 'requested_sample_rate_hz'),
            _b('actual_sample_rate_hz', 'actual_sample_rate_hz',
               optional=True),
            _b('timing_method', 'timing_method', optional=True),
            _b('timing_quality', 'timing_quality', optional=True),
            _b('raw_audio_sha256', 'raw_audio_sha256', optional=True),
            _b('ir_sha256', 'ir_sha256', optional=True),
            _b('calibration_state', 'calibration_state'),
            _b('stage', 'stage'),
            _b('outcome', 'outcome', optional=True),
            _b('quality_verdict', 'quality_verdict', optional=True),
            _b('captured_at_utc', 'captured_at_utc', optional=True),
            _b('completed_at_utc', 'completed_at_utc'),
        ),
        (),
    ),
    'cad_sweep_acquisition_stage_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('run_id', 'run_id'),
            _b('run_seq', 'run_seq'),
            _b('stage', 'stage'),
            _b('entered_at_utc', 'entered_at_utc'),
        ),
        (),
    ),
    'cad_channel_verification_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('reference_channel', 'reference_channel', optional=True),
            _b('target_count', 'target_count'),
            _b('method', 'method'),
            _b('created_at_utc', 'created_at_utc'),
        ),
        (),
    ),
    'cad_channel_excitation_results': (
        'payload_json',
        (
            _b('result_id', 'result_id'),
            _b('result_sha256', 'result_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('logical_channel', 'logical_channel'),
            _b('acquisition_run_ref_id', 'acquisition_run_ref', 'ref_id',
               optional=True),
            _b('capture_quality', 'capture_quality'),
            _b('response_detected', 'response_detected'),
            _b('measured_at_utc', 'measured_at_utc'),
        ),
        (),
    ),
    'cad_channel_operator_attestations': (
        'payload_json',
        (
            _b('attestation_id', 'attestation_id'),
            _b('attestation_sha256', 'attestation_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('logical_channel', 'logical_channel'),
            _b('attested_by', 'attested_by'),
            _b('responded', 'responded'),
            _b('attested_at_utc', 'attested_at_utc'),
        ),
        (),
    ),
    'cad_channel_verification_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_ref_id', 'plan_ref', 'ref_id'),
            _b('map_state', 'map_state'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    'cad_calibration_wizard_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('run_sha256', 'run_sha256'),
            _b('document_id', 'document_id'),
            _b('lane', 'lane'),
            _b('wizard_version', 'wizard_version'),
            _b('engine_version', 'engine_version'),
            _b('derivation_version', 'derivation_version'),
            _b('check_evaluation_version', 'check_evaluation_version'),
            _b('instrument_ref_id', 'instrument_ref', 'ref_id',
               optional=True),
            _b('calibrator_ref_id', 'calibrator_ref', 'ref_id',
               optional=True),
            _b('acceptance_profile_ref_id', 'acceptance_profile_ref',
               'ref_id', optional=True),
            _b('campaign_ref_id', 'campaign_ref', 'ref_id', optional=True),
            _b('check_plan_ref_id', 'check_plan_ref', 'ref_id',
               optional=True),
            _b('check_kind', 'check_kind', optional=True),
            _b('created_at_utc', 'created_at_utc'),
            _b('created_by', 'created_by'),
        ),
        (),
    ),
    'cad_calibration_wizard_transitions': (
        'payload_json',
        (
            _b('transition_id', 'transition_id'),
            _b('transition_sha256', 'transition_sha256'),
            _b('document_id', 'document_id'),
            _b('run_ref_id', 'run_ref', 'ref_id'),
            _b('run_seq', 'seq'),
            _b('event_kind', 'event_kind'),
            _b('outcome', 'outcome'),
            _b('actor', 'actor'),
            _b('from_stage', 'from_stage', optional=True),
            _b('to_stage', 'to_stage'),
            _b('event_succeeded', 'event_succeeded', optional=True),
            _b('result_tag', 'result_tag', optional=True),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    # REV67: #883 crash-safe session-recovery authority.
    'session_recovery_journals': (
        'payload_json',
        (
            _b('journal_id', 'journal_id'),
            _b('journal_sha256', 'journal_sha256'),
            _b('session_id', 'session_id'),
            _b('document_id', 'document_id'),
            _b('ending', 'ending'),
            _b('integrity', 'integrity'),
            _b('entry_count', 'entry_count'),
            _b('head_entry_sha256', 'head_entry_sha256', optional=True),
            _b('envelope_sha256', 'envelope_sha256', optional=True),
            _b('detected_at_utc', 'detected_at_utc'),
        ),
        (),
    ),
    'session_recovery_decisions': (
        'payload_json',
        (
            _b('decision_id', 'decision_id'),
            _b('decision_sha256', 'decision_sha256'),
            _b('session_id', 'session_id'),
            _b('document_id', 'document_id'),
            _b('scope_kind', 'scope_kind'),
            _b('scope_ref_id', 'scope_ref_id', optional=True),
            _b('action', 'action'),
            _b('actor', 'actor'),
            _b('decided_at_utc', 'decided_at_utc'),
        ),
        (),
    ),
    'session_recovery_reconciliations': (
        'payload_json',
        (
            _b('reconciliation_id', 'reconciliation_id'),
            _b('reconciliation_sha256', 'reconciliation_sha256'),
            _b('session_id', 'session_id'),
            _b('document_id', 'document_id'),
            _b('operation_kind', 'operation_kind'),
            _b('operation_ref_id', 'operation_ref_id'),
            _b('verdict', 'verdict'),
            _b('recorded_at_utc', 'recorded_at_utc'),
        ),
        (),
    ),
    'cad_spl_check_acceptance_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('name', 'name'),
            _b('reference_level_db', 'reference_level_db'),
            _b('reference_frequency_hz', 'reference_frequency_hz'),
            _b('expected_measured_level_db', 'expected_measured_level_db'),
            _b('level_tolerance_db', 'level_tolerance_db'),
            _b('min_snr_db', 'min_snr_db'),
            _b('declared_at_utc', 'declared_at_utc'),
            _b('declared_by', 'declared_by'),
        ),
        (),
    ),
    'cad_campaign_check_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('campaign_ref_id', 'campaign_ref', 'ref_id'),
            _b('blocked_on_failure', 'blocked_on_failure'),
            _b('declared_at_utc', 'declared_at_utc'),
            _b('declared_by', 'declared_by'),
        ),
        (),
    ),
    'cad_campaign_execution_plans': (
        'payload_json',
        (
            _b('plan_id', 'plan_id'),
            _b('plan_sha256', 'plan_sha256'),
            _b('document_id', 'document_id'),
            _b('authority_version', 'authority_version'),
            _b('campaign_ref_id', 'campaign_ref', 'ref_id'),
            _b('scene_ref_id', 'scene_ref', 'ref_id', optional=True),
            _b('entry_count', 'entry_count'),
            _b('generated_at_utc', 'generated_at_utc'),
        ),
        (),
    ),
    'cad_campaign_execution_events': (
        'payload_json',
        (
            _b('event_id', 'event_id'),
            _b('event_sha256', 'event_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_id', 'plan_id'),
            _b('journal_seq', 'seq'),
            _b('kind', 'kind'),
            _b('entry_key', 'entry_key', optional=True),
            _b('position_id', 'position_id', optional=True),
            _b('actor', 'actor'),
            _b('at_utc', 'at_utc'),
            _b('retry_decision', 'retry_decision', optional=True),
            _b('confirmation_method', 'confirmation_method',
               optional=True),
        ),
        (),
    ),
    'cad_campaign_execution_runs': (
        'payload_json',
        (
            _b('run_record_id', 'run_record_id'),
            _b('run_record_sha256', 'run_record_sha256'),
            _b('document_id', 'document_id'),
            _b('plan_id', 'plan_id'),
            _b('entry_key', 'entry_key'),
            _b('entry_ordinal', 'entry_ordinal'),
            _b('attempt', 'attempt'),
            _b('position_id', 'position_id'),
            _b('channel_entity_id', 'channel_entity_id'),
            _b('role', 'role'),
            _b('run_index', 'run_index'),
            _b('required', 'required'),
            _b('level_dbfs', 'level_dbfs'),
            _b('outcome', 'outcome'),
            _b('failure_kind', 'failure_kind', optional=True),
            _b('quality_verdict', 'quality_verdict', optional=True),
            _b('stimulus_ref_id', 'stimulus_ref', 'ref_id',
               optional=True),
            _b('acquisition_ref_id', 'acquisition_ref', 'ref_id',
               optional=True),
            _b('position_ref_id', 'position_ref', 'ref_id',
               optional=True),
            _b('started_at_utc', 'started_at_utc'),
            _b('completed_at_utc', 'completed_at_utc'),
        ),
        (),
    ),
    # #879 device-discovery + capability-handshake authority.
    'cad_discovery_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('run_sha256', 'run_sha256'),
            _b('document_id', 'document_id'),
            _b('backend_id', 'backend_id'),
            _b('mechanism', 'mechanism'),
            _b('outcome', 'outcome'),
            _b('device_count', 'device_count'),
        ),
        (),
    ),
    'cad_discovered_devices': (
        'payload_json',
        (
            _b('device_id', 'device_id'),
            _b('device_sha256', 'device_sha256'),
            _b('document_id', 'document_id'),
            _b('run_ref_id', 'run_ref', 'ref_id', optional=True),
            _b('endpoint', 'endpoint'),
            _b('identity_state', 'identity_state'),
            _b('ambiguity_group', 'ambiguity_group', optional=True),
        ),
        (),
    ),
    'cad_capability_probe_records': (
        'payload_json',
        (
            _b('probe_id', 'probe_id'),
            _b('probe_sha256', 'probe_sha256'),
            _b('document_id', 'document_id'),
            _b('device_ref_id', 'device_ref', 'ref_id'),
            _b('adapter_id', 'adapter_id'),
            _b('outcome', 'outcome'),
            _b('capability_snapshot_sha256', 'capability_snapshot_sha256',
               optional=True),
        ),
        (),
    ),
    'cad_trusted_device_bindings': (
        'payload_json',
        (
            _b('binding_record_id', 'binding_record_id'),
            _b('binding_record_sha256', 'binding_record_sha256'),
            _b('document_id', 'document_id'),
            _b('binding_id', 'binding_id'),
            _b('device_ref_id', 'device_ref', 'ref_id'),
            _b('adapter_id', 'adapter_id'),
            _b('endpoint', 'endpoint'),
            _b('trust_state', 'trust_state'),
            _b('identity_basis', 'identity_basis'),
        ),
        (),
    ),
    'cad_device_identity_drift_reports': (
        'payload_json',
        (
            _b('report_id', 'report_id'),
            _b('report_sha256', 'report_sha256'),
            _b('document_id', 'document_id'),
            _b('binding_ref_id', 'binding_ref', 'ref_id'),
            _b('drift_kind', 'drift_kind', optional=True),
            _b('verdict', 'verdict'),
        ),
        (),
    ),
    'cad_device_rebinding_decisions': (
        'payload_json',
        (
            _b('decision_id', 'decision_id'),
            _b('decision_sha256', 'decision_sha256'),
            _b('document_id', 'document_id'),
            _b('previous_binding_ref_id', 'previous_binding_ref', 'ref_id'),
            _b('action', 'action'),
            _b('new_binding_ref_id', 'new_binding_ref', 'ref_id',
               optional=True),
        ),
        (),
    ),
    # REV70: #892 interop corpus — sealed per-fixture run verdicts and
    # whole-corpus run records pinned to the corpus manifest.
    'cad_interop_fixture_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('run_sha256', 'run_sha256'),
            _b('document_id', 'document_id'),
            _b('fixture_id', 'fixture_ref', 'ref_id'),
            _b('fixture_sha256', 'fixture_ref', 'ref_sha256'),
            _b('manifest_sha256', 'manifest_sha256'),
            _b('corpus_version', 'corpus_version'),
            _b('harness_version', 'harness_version'),
            _b('format_family', 'format_family'),
            _b('round_trip_mode', 'round_trip_mode'),
            _b('verdict', 'verdict'),
            _b('started_at_utc', 'started_at_utc'),
            _b('finished_at_utc', 'finished_at_utc'),
        ),
        (),
    ),
    'cad_interop_corpus_runs': (
        'payload_json',
        (
            _b('corpus_run_id', 'corpus_run_id'),
            _b('corpus_run_sha256', 'corpus_run_sha256'),
            _b('document_id', 'document_id'),
            _b('manifest_id', 'manifest_ref', 'ref_id'),
            _b('manifest_sha256', 'manifest_ref', 'ref_sha256'),
            _b('corpus_version', 'corpus_version'),
            _b('harness_version', 'harness_version'),
            _b('verdict', 'verdict'),
            _b('started_at_utc', 'started_at_utc'),
            _b('finished_at_utc', 'finished_at_utc'),
        ),
        (),
    ),
    # REV70: #891 Reference Theater self-test fixture authority.
    'cad_reference_theater_runs': (
        'payload_json',
        (
            _b('run_id', 'run_id'),
            _b('run_sha256', 'run_sha256'),
            _b('document_id', 'document_id'),
            _b('fixture_version', 'fixture_version'),
            _b('outcome', 'outcome'),
            _b('verdict', 'verdict'),
            _b('manifest_sha256', 'manifest_sha256'),
            _b('deploy_is_simulated', 'deploy_is_simulated'),
            _b('deploy_evidence_strength', 'deploy_evidence_strength'),
            _b('scene_sha256', 'scene_sha256'),
            _b('started_at_utc', 'started_at_utc'),
            _b('finished_at_utc', 'finished_at_utc'),
        ),
        (),
    ),
    # REV70: #937 Decision Brief — sealed next-action recommendations.
    'cad_decision_briefs': (
        'payload_json',
        (
            _b('brief_id', 'brief_id'),
            _b('brief_sha256', 'brief_sha256'),
            _b('document_id', 'document_id'),
            _b('scene_revision_id', 'scene_revision_id'),
            _b('baseline_ref_id', 'baseline_ref', 'ref_id'),
            _b('top_tier', 'top_tier'),
            _b('action_count', 'action_count'),
            _b('ready_count', 'ready_count'),
            _b('created_at_utc', 'created_at_utc'),
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
    'cad_standards_gap_matrices',
    'cad_standards_observation_authorities',
    'cad_standards_profiles',
    'cad_standards_source_authorities',
    'cad_standards_source_matrices',
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
