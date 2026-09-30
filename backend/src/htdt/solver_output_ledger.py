"""Solver-output ledger projection for the diagnostics surface (REV24-SURFACE).

Read-only projection over the acoustic solver stack's persisted rows. Each
row is classified by the same bound/unbound ledger
:func:`native_authority_audit.audit_table_modes` reports: ``bound`` rows have
a canonical replay (or evidence-bytes) path, ``unbound`` rows live in the
structural-only payload ledger whose strongest verification today is schema
+ payload parse.

Nothing here mutates the database and nothing fabricates: a row whose
``payload_json`` fails to parse is still listed with
``payload_state='unreadable'`` and no payload-derived fields; a row whose
declared linkage resolves to no revision of the inspected document is
``link_state='unresolved'`` (the ledger's orphaned payloads), and rows in
linkage-free-by-design tables (solver adapters, stitching policies) resolve
through the rows that reference them — a policy/adapter no scene row
references is likewise ``unresolved``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .cad_schema import connect_sqlite
from .native_authority_audit import audit_table_modes

LinkState = Literal['resolved', 'unresolved']
BindingState = Literal['bound', 'unbound']
PayloadState = Literal['ok', 'unreadable']


@dataclass(frozen=True, slots=True)
class SolverArtifactEntry:
    """One persisted solver-stack row projected for diagnostics."""

    kind: str
    table: str
    artifact_id: str
    #: Revisions the row's declared linkage resolves to. An artifact shared
    #: by several revisions (e.g. an adapter referenced by bindings on more
    #: than one scene) lists all of them; an empty tuple means the linkage
    #: resolved to nothing persisted in this document.
    scene_revision_ids: tuple[str, ...]
    link_state: LinkState
    #: ``bound`` — a canonical replay/evidence path exists;
    #: ``unbound`` — payload-ledger row, replay path pending.
    binding: BindingState
    observables: tuple[str, ...]
    produced_by: str | None
    provenance_ref: str | None
    capability: str | None
    payload_state: PayloadState
    #: Count of the artifact's primary item list (paths, bands, samples,
    #: manifests) when the payload declares one.
    item_count: int | None
    recorded_at_utc: str | None
    semantic_sha256: str | None


@dataclass(frozen=True, slots=True)
class SolverOutputLedger:
    """Whole-document solver-output ledger."""

    document_id: str
    entries: tuple[SolverArtifactEntry, ...]

    def for_revision(self, revision_id: str) -> tuple[SolverArtifactEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if revision_id in entry.scene_revision_ids
        )

    @property
    def unresolved(self) -> tuple[SolverArtifactEntry, ...]:
        return tuple(
            entry for entry in self.entries if entry.link_state == 'unresolved'
        )

    @property
    def bound_count(self) -> int:
        return sum(1 for entry in self.entries if entry.binding == 'bound')

    @property
    def unbound_count(self) -> int:
        return len(self.entries) - self.bound_count


@dataclass(frozen=True, slots=True)
class _TableSpec:
    table: str
    kind: str
    id_column: str
    #: Linkage resolution strategy — see ``_resolve`` in the builder.
    link: str


#: The acoustic solver stack: prediction requests → dispatch → results →
#: deterministic/late-field/hybrid artifacts, plus the compiled-geometry and
#: treatment-boundary authorities they consume. Order is the display order.
_SPECS: tuple[_TableSpec, ...] = (
    _TableSpec(
        'cad_acoustic_solver_adapters',
        'solver_adapter',
        'descriptor_id',
        'adapter',
    ),
    _TableSpec(
        'cad_acoustic_scene_snapshots',
        'scene_snapshot',
        'snapshot_id',
        'scene',
    ),
    _TableSpec(
        'cad_r120_compiled_geometry',
        'compiled_geometry',
        'compiled_geometry_id',
        'scene',
    ),
    _TableSpec(
        'cad_r120_leak_portal_diagnostics',
        'leak_portal',
        'diagnostic_result_id',
        'compiled',
    ),
    _TableSpec(
        'cad_acoustic_prediction_requests',
        'prediction_request',
        'request_id',
        'snapshot:acoustic_scene_snapshot_id',
    ),
    _TableSpec(
        'cad_acoustic_solver_dispatch_bindings',
        'dispatch_binding',
        'binding_id',
        'snapshot:acoustic_scene_snapshot_id',
    ),
    _TableSpec(
        'cad_deterministic_ga_execution_inputs',
        'execution_input',
        'execution_input_id',
        'snapshot:snapshot_id',
    ),
    _TableSpec(
        'cad_acoustic_solver_results',
        'solver_result',
        'result_id',
        'snapshot:acoustic_scene_snapshot_id',
    ),
    _TableSpec(
        'cad_deterministic_path_artifacts',
        'path_artifact',
        'artifact_id',
        'snapshot:snapshot_id',
    ),
    _TableSpec(
        'cad_late_field_artifacts',
        'late_field',
        'artifact_id',
        'snapshot:snapshot_id',
    ),
    _TableSpec(
        'r150_path_frequency_response_artifacts',
        'path_frequency_response',
        'artifact_id',
        'payload_path_id',
    ),
    _TableSpec(
        'r160_late_energy_decay_artifacts',
        'late_energy_decay',
        'artifact_id',
        'payload_path_ref',
    ),
    _TableSpec(
        'r160_stitched_hybrid_responses',
        'stitched_response',
        'artifact_id',
        'payload_result_ref',
    ),
    _TableSpec(
        'r160_numerical_hybrid_responses',
        'numerical_hybrid_response',
        'artifact_id',
        'payload_result_ref',
    ),
    _TableSpec(
        'cad_hybrid_acoustic_results',
        'hybrid_result',
        'hybrid_result_id',
        'scene',
    ),
    _TableSpec(
        'cad_hybrid_stitching_policies',
        'stitching_policy',
        'policy_id',
        'policy',
    ),
    _TableSpec(
        'cad_hybrid_prediction_providers',
        'prediction_provider',
        'provider_id',
        'provider_artifact',
    ),
    _TableSpec(
        'cad_hybrid_prediction_provider_bindings',
        'provider_binding',
        'binding_id',
        'provider',
    ),
    _TableSpec(
        'cad_hybrid_prediction_provider_objectives',
        'provider_objective',
        'connection_id',
        'provider',
    ),
    _TableSpec(
        'cad_treatment_boundary_overlays',
        'boundary_overlay',
        'overlay_id',
        'scene',
    ),
    _TableSpec(
        'cad_treatment_boundary_compositions',
        'boundary_composition',
        'composition_id',
        'scene',
    ),
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]


def _rows(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    columns = _table_columns(connection, table)
    order = ' ORDER BY seq' if 'seq' in columns else ' ORDER BY rowid'
    return list(connection.execute(f'SELECT * FROM "{table}"{order}'))


def _payload(row: sqlite3.Row) -> dict[str, Any] | None:
    text = row['payload_json']
    if not isinstance(text, str):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _at(payload: dict[str, Any], *path: str) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _observables(kind: str, payload: dict[str, Any]) -> tuple[str, ...]:
    if kind == 'solver_result':
        artifacts = payload.get('artifacts')
        if isinstance(artifacts, (list, tuple)):
            return tuple(
                item['observable']
                for item in artifacts
                if isinstance(item, dict) and isinstance(item.get('observable'), str)
            )
        return ()
    if kind == 'prediction_request':
        return _strings(payload.get('requested_observables'))
    if kind in ('path_artifact', 'late_field'):
        return _strings(payload.get('path_scope'))
    if kind == 'late_energy_decay':
        return _strings(payload.get('quantity')) + ('late_energy_decay',)
    if kind in ('path_frequency_response', 'numerical_hybrid_response'):
        return _strings(payload.get('quantity'))
    if kind == 'hybrid_result':
        return _strings(_at(payload, 'composition_spec', 'observable'))
    if kind == 'prediction_provider':
        return _strings(payload.get('response_quantity'))
    if kind == 'boundary_composition':
        return _strings(payload.get('target_domain'))
    return ()


def _produced_by(kind: str, row: sqlite3.Row, payload: dict[str, Any]) -> str | None:
    def _col(name: str) -> str | None:
        try:
            value = row[name]
        except (IndexError, KeyError):
            return None
        return value if isinstance(value, str) else None

    if kind in ('solver_result', 'path_artifact', 'late_field', 'execution_input'):
        return _col('execution_id') or _col('execution_input_id')
    if kind == 'solver_adapter':
        adapter_id, version = _col('adapter_id'), _col('adapter_version')
        return f'{adapter_id} v{version}' if adapter_id else None
    if kind == 'prediction_provider':
        adapter_id = payload.get('adapter_id')
        version = payload.get('adapter_version')
        return f'{adapter_id} v{version}' if adapter_id else None
    if kind == 'stitched_response':
        identity = _at(payload, 'stitch_plan', 'algorithm_identity')
        version = _at(payload, 'stitch_plan', 'algorithm_version')
        return f'{identity} v{version}' if isinstance(identity, str) else None
    if kind == 'numerical_hybrid_response':
        value = _at(payload, 'composition_spec', 'composition_spec_id')
        return value if isinstance(value, str) else None
    if kind == 'path_frequency_response':
        value = _at(payload, 'execution_input_ref', 'authority_id')
        return value if isinstance(value, str) else None
    if kind == 'late_energy_decay':
        value = payload.get('decay_model')
        return value if isinstance(value, str) else None
    if kind == 'stitching_policy':
        value = payload.get('authority_version')
        return value if isinstance(value, str) else None
    if kind == 'boundary_overlay':
        return _col('treatment_placement_instance_id')
    if kind == 'boundary_composition':
        return _col('host_surface_id')
    if kind in ('compiled_geometry', 'leak_portal'):
        return _col('request_id')
    if kind == 'provider_objective':
        return _col('evaluation_id')
    return None


def _provenance_ref(kind: str, row: sqlite3.Row, payload: dict[str, Any]) -> str | None:
    def _col(name: str) -> str | None:
        try:
            value = row[name]
        except (IndexError, KeyError):
            return None
        return value if isinstance(value, str) else None

    if kind == 'solver_result':
        value = _at(payload, 'execution_provenance_ref', 'authority_id')
        return value if isinstance(value, str) else None
    if kind in ('path_artifact', 'late_field'):
        return _col('execution_provenance_authority_id')
    if kind == 'late_energy_decay':
        value = _at(payload, 'deterministic_path_artifact_ref', 'authority_id')
        return value if isinstance(value, str) else None
    if kind in ('stitched_response', 'numerical_hybrid_response'):
        value = _at(payload, 'exact_r130_result', 'result_id')
        return value if isinstance(value, str) else None
    if kind == 'path_frequency_response':
        value = payload.get('deterministic_path_artifact_id')
        return value if isinstance(value, str) else None
    if kind == 'hybrid_result':
        return _col('stitching_policy_id')
    if kind == 'dispatch_binding':
        return _col('prediction_request_id')
    if kind == 'execution_input':
        return _col('prediction_request_id')
    if kind == 'prediction_provider':
        return _col('r160_artifact_id')
    if kind in ('provider_binding', 'provider_objective'):
        return _col('provider_id')
    if kind == 'boundary_overlay':
        definition, version = (
            _col('treatment_definition_id'),
            _col('treatment_definition_version'),
        )
        return f'{definition} v{version}' if definition else None
    if kind == 'boundary_composition':
        return _col('compiled_geometry_id')
    if kind == 'compiled_geometry':
        return _col('semantic_geometry_id')
    if kind == 'leak_portal':
        return _col('compiled_geometry_id')
    if kind == 'scene_snapshot':
        return _col('r120_compiled_geometry_id')
    return None


def _capability(kind: str, row: sqlite3.Row, payload: dict[str, Any]) -> str | None:
    def _col(name: str) -> str | None:
        try:
            value = row[name]
        except (IndexError, KeyError):
            return None
        return value if isinstance(value, str) else None

    if kind == 'late_field':
        value = _at(payload, 'capability_record', 'energy_semantics')
        return value if isinstance(value, str) else None
    if kind == 'late_energy_decay':
        value = payload.get('capability_state')
        return value if isinstance(value, str) else None
    if kind == 'stitched_response':
        value = _at(payload, 'stitch_plan', 'stitch_state')
        return value if isinstance(value, str) else None
    if kind == 'numerical_hybrid_response':
        value = payload.get('capability_state')
        return value if isinstance(value, str) else None
    if kind == 'path_frequency_response':
        value = payload.get('capability')
        return value if isinstance(value, str) else None
    if kind == 'stitching_policy':
        value = payload.get('mode')
        return value if isinstance(value, str) else None
    if kind == 'solver_result':
        value = payload.get('result_state')
        return value if isinstance(value, str) else None
    if kind == 'dispatch_binding':
        return _col('state')
    if kind == 'solver_adapter':
        return _col('acoustic_domain')
    if kind == 'hybrid_result':
        value = payload.get('evidence_state')
        return value if isinstance(value, str) else None
    return None


_ITEM_LIST_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    'solver_result': (('artifacts',),),
    'path_artifact': (('paths',),),
    'late_field': (('path_contributions',),),
    'late_energy_decay': (('bands',),),
    'stitched_response': (('samples',), ('responses',)),
    'numerical_hybrid_response': (('samples',),),
    'path_frequency_response': (('samples',),),
    'hybrid_result': (('bands',), ('samples',)),
}


def _item_count(kind: str, payload: dict[str, Any]) -> int | None:
    for path in _ITEM_LIST_KEYS.get(kind, ()):
        node: Any = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, (list, tuple)):
            return len(node)
    return None


def build_solver_output_ledger(
    connection: sqlite3.Connection,
    document_id: str,
) -> SolverOutputLedger:
    """Project the document's solver-stack rows into ledger entries.

    ``connection`` must be a row-factory connection over the live native
    database; the function only reads. Rows that fail payload parse are kept
    with ``payload_state='unreadable'`` — the surface shows the honest state
    instead of fabricating fields.
    """
    modes = audit_table_modes()
    known = {
        spec.table: spec
        for spec in _SPECS
        if _table_exists(connection, spec.table)
    }

    document_revisions = {
        row['revision_id']
        for row in connection.execute(
            'SELECT revision_id FROM scene_revisions WHERE document_id=?',
            (document_id,),
        )
    }

    snapshot_rev: dict[str, str] = {}
    if 'cad_acoustic_scene_snapshots' in known:
        for row in _rows(connection, 'cad_acoustic_scene_snapshots'):
            snapshot_rev[row['snapshot_id']] = row['scene_revision_id']
    compiled_rev: dict[str, str] = {}
    if 'cad_r120_compiled_geometry' in known:
        for row in _rows(connection, 'cad_r120_compiled_geometry'):
            compiled_rev[row['compiled_geometry_id']] = row['scene_revision_id']
    path_snap: dict[str, str] = {}
    if 'cad_deterministic_path_artifacts' in known:
        for row in _rows(connection, 'cad_deterministic_path_artifacts'):
            path_snap[row['artifact_id']] = row['snapshot_id']
    result_snap: dict[str, str] = {}
    if 'cad_acoustic_solver_results' in known:
        for row in _rows(connection, 'cad_acoustic_solver_results'):
            result_snap[row['result_id']] = row['acoustic_scene_snapshot_id']
    adapter_rev: dict[str, set[str]] = {}
    if 'cad_acoustic_solver_dispatch_bindings' in known:
        for row in _rows(connection, 'cad_acoustic_solver_dispatch_bindings'):
            revision = snapshot_rev.get(row['acoustic_scene_snapshot_id'])
            if revision is not None:
                adapter_rev.setdefault(row['adapter_descriptor_id'], set()).add(
                    revision
                )
    policy_rev: dict[str, set[str]] = {}
    if 'cad_hybrid_acoustic_results' in known:
        for row in _rows(connection, 'cad_hybrid_acoustic_results'):
            policy_rev.setdefault(row['stitching_policy_id'], set()).add(
                row['scene_revision_id']
            )

    # r160 artifacts resolve through their payload's declared refs:
    # decay artifacts pin a deterministic path artifact, stitched responses
    # pin the R130 solver result they re-express.
    r160_rev: dict[str, set[str]] = {}
    for table, ref_path, lookup in (
        (
            'r160_late_energy_decay_artifacts',
            ('deterministic_path_artifact_ref', 'authority_id'),
            path_snap,
        ),
        (
            'r160_stitched_hybrid_responses',
            ('exact_r130_result', 'result_id'),
            result_snap,
        ),
        (
            'r160_numerical_hybrid_responses',
            ('exact_r130_result', 'result_id'),
            result_snap,
        ),
    ):
        if table not in known:
            continue
        for row in _rows(connection, table):
            payload = _payload(row)
            ref = _at(payload, *ref_path) if payload is not None else None
            snapshot = lookup.get(ref) if isinstance(ref, str) else None
            revision = snapshot_rev.get(snapshot) if snapshot is not None else None
            if revision is not None:
                r160_rev.setdefault(row['artifact_id'], set()).add(revision)

    provider_rev: dict[str, set[str]] = {}
    if 'cad_hybrid_prediction_providers' in known:
        for row in _rows(connection, 'cad_hybrid_prediction_providers'):
            for revision in r160_rev.get(row['r160_artifact_id'], ()):
                provider_rev.setdefault(row['provider_id'], set()).add(revision)

    def _resolve(spec: _TableSpec, row: sqlite3.Row, payload) -> tuple[str, ...]:
        revisions: set[str] = set()
        link = spec.link
        if link == 'scene':
            value = row['scene_revision_id']
            if isinstance(value, str):
                revisions.add(value)
        elif link == 'compiled':
            value = compiled_rev.get(row['compiled_geometry_id'])
            if value is not None:
                revisions.add(value)
        elif link.startswith('snapshot:'):
            value = snapshot_rev.get(row[link.split(':', 1)[1]])
            if value is not None:
                revisions.add(value)
        elif link == 'payload_path_ref':
            ref = _at(payload, 'deterministic_path_artifact_ref', 'authority_id') if payload else None
            snapshot = path_snap.get(ref) if isinstance(ref, str) else None
            value = snapshot_rev.get(snapshot) if snapshot else None
            if value is not None:
                revisions.add(value)
        elif link == 'payload_result_ref':
            ref = _at(payload, 'exact_r130_result', 'result_id') if payload else None
            snapshot = result_snap.get(ref) if isinstance(ref, str) else None
            value = snapshot_rev.get(snapshot) if snapshot else None
            if value is not None:
                revisions.add(value)
        elif link == 'payload_path_id':
            ref = payload.get('deterministic_path_artifact_id') if payload else None
            snapshot = path_snap.get(ref) if isinstance(ref, str) else None
            value = snapshot_rev.get(snapshot) if snapshot else None
            if value is not None:
                revisions.add(value)
        elif link == 'adapter':
            revisions.update(adapter_rev.get(row['descriptor_id'], ()))
        elif link == 'policy':
            revisions.update(policy_rev.get(row['policy_id'], ()))
        elif link == 'provider_artifact':
            revisions.update(r160_rev.get(row['r160_artifact_id'], ()))
        elif link == 'provider':
            revisions.update(provider_rev.get(row['provider_id'], ()))
        return tuple(
            sorted(revision for revision in revisions if revision in document_revisions)
        )

    entries: list[SolverArtifactEntry] = []
    for spec in _SPECS:
        if spec.table not in known:
            continue
        binding: BindingState = (
            'bound'
            if modes.get(spec.table) in ('replay_canonical', 'evidence_bytes')
            else 'unbound'
        )
        for row in _rows(connection, spec.table):
            payload = _payload(row)
            revisions = _resolve(spec, row, payload)
            entries.append(
                SolverArtifactEntry(
                    kind=spec.kind,
                    table=spec.table,
                    artifact_id=str(row[spec.id_column]),
                    scene_revision_ids=revisions,
                    link_state='resolved' if revisions else 'unresolved',
                    binding=binding,
                    observables=(
                        _observables(spec.kind, payload)
                        if payload is not None
                        else ()
                    ),
                    produced_by=(
                        _produced_by(spec.kind, row, payload)
                        if payload is not None
                        else None
                    ),
                    provenance_ref=(
                        _provenance_ref(spec.kind, row, payload)
                        if payload is not None
                        else None
                    ),
                    capability=(
                        _capability(spec.kind, row, payload)
                        if payload is not None
                        else None
                    ),
                    payload_state='ok' if payload is not None else 'unreadable',
                    item_count=(
                        _item_count(spec.kind, payload)
                        if payload is not None
                        else None
                    ),
                    recorded_at_utc=(
                        row['recorded_at_utc']
                        if 'recorded_at_utc' in row.keys()
                        else None
                    ),
                    semantic_sha256=(
                        row['semantic_sha256']
                        if 'semantic_sha256' in row.keys()
                        else None
                    ),
                )
            )

    entries.sort(
        key=lambda entry: (
            entry.recorded_at_utc or '',
            spec_order(entry.kind),
            entry.artifact_id,
        )
    )
    return SolverOutputLedger(document_id=document_id, entries=tuple(entries))


_KIND_ORDER = {spec.kind: index for index, spec in enumerate(_SPECS)}


def spec_order(kind: str) -> int:
    return _KIND_ORDER.get(kind, len(_KIND_ORDER))


def open_solver_output_ledger(
    db_path: Path, document_id: str
) -> SolverOutputLedger:
    """Convenience opener used by the shell — read-only connection."""
    with closing(connect_sqlite(db_path)) as connection:
        return build_solver_output_ledger(connection, document_id)


__all__ = [
    'BindingState',
    'LinkState',
    'PayloadState',
    'SolverArtifactEntry',
    'SolverOutputLedger',
    'build_solver_output_ledger',
    'open_solver_output_ledger',
]
