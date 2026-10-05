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
