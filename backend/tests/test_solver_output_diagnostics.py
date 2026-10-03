"""REV24-SURFACE regression tests for the solver-output diagnostics surface.

The surface is a read-only projection over the solver stack's bound/unbound
payload ledger: every row renders honest state (Japanese labels only, never
raw enum/table names), unresolved linkage is reported as such, and an
unreadable payload is listed as unreadable rather than reconstructed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from htdt.application_pages import SupportPage
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.cad_schema import connect_sqlite
from htdt.solver_output_diagnostics_ui import (
    SolverOutputDiagnosticsDialog,
    _BINDING_LABELS,
    _KIND_LABELS,
    _LINK_LABELS,
    _OBSERVABLE_LABELS,
    _PAYLOAD_STATE_LABELS,
)
from htdt.solver_output_ledger import (
    _SPECS,
    SolverOutputLedger,
    build_solver_output_ledger,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


SHA = 'a' * 64
NOW = '2026-09-30T00:00:00+00:00'


def _payload(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def _seed_repository(tmp_path: Path) -> tuple[SceneRepository, str, str]:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-test'), parent_revision_id=None)
    head = repository.current_head('doc-test')
    assert head is not None
    return repository, 'doc-test', head.revision_id


def _insert(connection, sql: str, *params) -> None:
    connection.execute(sql, params)


def _seed_solver_stack(connection, revision_id: str) -> None:
    """One honest row per ledger kind, linked to ``revision_id``.

    Direct SQL keeps the fixture minimal — these are payload-ledger tables
    whose strongest verification is schema + parse, so a well-formed
    ``payload_json`` is the honest persisted state.
    """
    _insert(
        connection,
        'INSERT INTO cad_r120_compiled_geometry (compiled_geometry_id, '
        'compiled_hash_sha256, scene_revision_id, scene_revision_content_hash, '
        'semantic_geometry_id, semantic_geometry_hash_sha256, request_id, '
        'payload_json, recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        f'r120-compiled-geometry:{SHA}',
        SHA,
        revision_id,
        SHA,
        f'semantic-geometry:{SHA}',
        SHA,
        'req-1',
        _payload(authority_version='r120-compiled-geometry-1'),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_acoustic_scene_snapshots (snapshot_id, '
        'semantic_sha256, document_id, scene_revision_id, scene_content_hash, '
        'r120_compiled_geometry_id, r120_compiled_geometry_sha256, '
        'material_boundary_configuration_sha256, payload_json, '
        'recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        f'acoustic-scene-snapshot:{SHA}',
        SHA,
        'doc-test',
        revision_id,
        SHA,
        f'r120-compiled-geometry:{SHA}',
        SHA,
        SHA,
        _payload(authority_version='acoustic-scene-snapshot-1'),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_r120_leak_portal_diagnostics (diagnostic_result_id, '
        'diagnostic_hash_sha256, compiled_geometry_id, '
        'compiled_geometry_hash_sha256, request_id, payload_json, '
        'recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)',
        f'r120-leak-portal-diagnostic:{SHA}',
        SHA,
        f'r120-compiled-geometry:{SHA}',
        SHA,
        'req-1',
        _payload(portals=[]),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_acoustic_prediction_requests (request_id, '
        'request_semantic_sha256, acoustic_scene_snapshot_id, '
        'acoustic_scene_snapshot_sha256, deterministic_input_hash, '
        'payload_json, recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)',
        f'acoustic-prediction-request:{SHA}',
        SHA,
        f'acoustic-scene-snapshot:{SHA}',
        SHA,
        SHA,
        _payload(requested_observables=['complex_pressure']),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_acoustic_solver_adapters (descriptor_id, '
        'semantic_sha256, adapter_id, adapter_version, model_solver_role_id, '
        'acoustic_domain, payload_json, recorded_at_utc) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        f'acoustic-solver-adapter:{SHA}',
        SHA,
        'fixture-adapter',
        '1',
        'fixture-role',
        'acoustic',
        _payload(),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_acoustic_solver_dispatch_bindings (binding_id, '
        'semantic_sha256, prediction_request_id, acoustic_scene_snapshot_id, '
        'adapter_descriptor_id, deterministic_solver_input_hash, state, '
        'payload_json, recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        f'acoustic-solver-dispatch:{SHA}',
        SHA,
        f'acoustic-prediction-request:{SHA}',
        f'acoustic-scene-snapshot:{SHA}',
        f'acoustic-solver-adapter:{SHA}',
        SHA,
        'READY',
        _payload(),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_deterministic_ga_execution_inputs '
        '(execution_input_id, semantic_sha256, snapshot_id, '
        'prediction_request_id, dispatch_binding_id, '
        'r120_compiled_geometry_id, payload_json, recorded_at_utc) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        f'deterministic-ga-execution-input:{SHA}',
        SHA,
        f'acoustic-scene-snapshot:{SHA}',
        f'acoustic-prediction-request:{SHA}',
        f'acoustic-solver-dispatch:{SHA}',
        f'r120-compiled-geometry:{SHA}',
        _payload(),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_acoustic_solver_results (result_id, semantic_sha256, '
        'execution_id, dispatch_binding_id, prediction_request_id, '
        'acoustic_scene_snapshot_id, deterministic_solver_input_hash, '
        'payload_json, recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        f'acoustic-solver-result:{SHA}',
        SHA,
        'r150-ga-execution:' + 'b' * 16,
        f'acoustic-solver-dispatch:{SHA}',
        f'acoustic-prediction-request:{SHA}',
        f'acoustic-scene-snapshot:{SHA}',
        SHA,
        _payload(
            result_state='COMPLETED',
            execution_provenance_ref={
                'authority_id': 'r150-execution-provenance:' + 'c' * 16,
            },
            artifacts=[
                {'observable': 'complex_pressure'},
                {'observable': 'late_energy_decay'},
            ],
        ),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_deterministic_path_artifacts (artifact_id, '
        'semantic_sha256, execution_id, execution_provenance_authority_id, '
        'execution_input_id, snapshot_id, prediction_request_id, '
        'dispatch_binding_id, r120_compiled_geometry_id, payload_json, '
        'recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        f'deterministic-path-artifact:{SHA}',
        SHA,
        'r150-ga-execution:' + 'b' * 16,
        'r150-execution-provenance:' + 'c' * 16,
        f'deterministic-ga-execution-input:{SHA}',
        f'acoustic-scene-snapshot:{SHA}',
        f'acoustic-prediction-request:{SHA}',
        f'acoustic-solver-dispatch:{SHA}',
        f'r120-compiled-geometry:{SHA}',
        _payload(
            path_scope='multi_portal_second_order_specular',
            paths=[{'path_id': 'p1'}, {'path_id': 'p2'}],
        ),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_late_field_artifacts (artifact_id, semantic_sha256, '
        'execution_id, execution_provenance_authority_id, execution_input_id, '
        'snapshot_id, prediction_request_id, dispatch_binding_id, '
        'r120_compiled_geometry_id, payload_json, recorded_at_utc) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        f'late-field-energy-artifact:{SHA}',
        SHA,
        'r150-ga-execution:' + 'b' * 16,
        'r150-execution-provenance:' + 'c' * 16,
        f'deterministic-ga-execution-input:{SHA}',
        f'acoustic-scene-snapshot:{SHA}',
        f'acoustic-prediction-request:{SHA}',
        f'acoustic-solver-dispatch:{SHA}',
        f'r120-compiled-geometry:{SHA}',
        _payload(
            path_scope='single_region_bounded_late_field_energy_v1',
            capability_record={
                'energy_semantics': 'upper_bound_not_point_estimate',
            },
            path_contributions=[{'path_id': 'p1'}],
        ),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_stochastic_receiver_estimate_artifacts (artifact_id, '
        'semantic_sha256, execution_id, execution_provenance_authority_id, '
        'execution_input_id, snapshot_id, prediction_request_id, '
        'dispatch_binding_id, r120_compiled_geometry_id, payload_json, '
        'recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        f'stochastic-receiver-estimate-artifact:{SHA}',
        SHA,
        'r150-stochastic-ray-execution:' + 'b' * 16,
        'r150-stochastic-ray-execution-provenance:' + 'c' * 16,
        f'deterministic-ga-execution-input:{SHA}',
        f'acoustic-scene-snapshot:{SHA}',
        f'acoustic-prediction-request:{SHA}',
        f'acoustic-solver-dispatch:{SHA}',
        f'r120-compiled-geometry:{SHA}',
        _payload(
            estimation_scope='bounded_stochastic_ray_receiver_estimate_v1',
            capability_record={
                'energy_semantics': 'monte_carlo_point_estimate_not_upper_bound',
            },
            estimates=[{'estimate_id': 'e1'}],
        ),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO r160_late_energy_decay_artifacts (artifact_id, '
        'semantic_sha256, late_field_input_id, payload_json) '
        'VALUES (?, ?, ?, ?)',
        'r160-late-energy-decay:' + SHA,
        SHA,
        'late-field-input-1',
        _payload(
            deterministic_path_artifact_ref={
                'authority_id': f'deterministic-path-artifact:{SHA}',
            },
            quantity='late_energy_density_per_m2',
            capability_state='SUPPORTED',
            decay_model='bounded_exponential_after_last_deterministic_arrival_v1',
            bands=[{'center_hz': 125.0}, {'center_hz': 250.0}],
        ),
    )
    _insert(
        connection,
        'INSERT INTO r150_path_frequency_response_artifacts (artifact_id, '
        'semantic_sha256, payload_json) VALUES (?, ?, ?)',
        'r150-path-frequency-response:' + SHA,
        SHA,
        _payload(
            deterministic_path_artifact_id=(
                f'deterministic-path-artifact:{SHA}'
            ),
            execution_input_ref={
                'authority_id': f'deterministic-ga-execution-input:{SHA}',
            },
            quantity='complex_acoustic_pressure_per_volume_velocity',
            capability='COMPLEX_SUPPORTED',
            samples=[{'frequency_hz': 125.0}, {'frequency_hz': 250.0}],
        ),
    )
    _insert(
        connection,
        'INSERT INTO r160_stitched_hybrid_responses (artifact_id, '
        'semantic_sha256, composition_spec_id, payload_json) '
        'VALUES (?, ?, ?, ?)',
        'r160-stitched-hybrid-response:' + SHA,
        SHA,
        'r160-stitched-composition-spec:' + SHA,
        _payload(
            exact_r130_result={'result_id': f'acoustic-solver-result:{SHA}'},
            stitch_plan={
                'algorithm_identity': 'htdt.r160.band-stitch',
                'algorithm_version': '1',
                'stitch_state': 'GAP_PRESERVED',
            },
        ),
    )
    _insert(
        connection,
        'INSERT INTO r160_numerical_hybrid_responses (artifact_id, '
        'semantic_sha256, composition_spec_id, payload_json) '
        'VALUES (?, ?, ?, ?)',
        'r160-numerical-hybrid-response:' + SHA,
        SHA,
        'r160-numerical-composition-spec:' + SHA,
        _payload(
            exact_r130_result={'result_id': f'acoustic-solver-result:{SHA}'},
            composition_spec={
                'composition_spec_id': 'r160-numerical-composition-spec:' + SHA,
            },
            quantity='complex_acoustic_pressure_per_volume_velocity',
            capability_state='COMPLEX_SUPPORTED',
            samples=[{'frequency_hz': 125.0}],
        ),
    )
    _insert(
        connection,
        'INSERT INTO cad_hybrid_stitching_policies (policy_id, '
        'semantic_sha256, mode, payload_json, recorded_at_utc) '
        'VALUES (?, ?, ?, ?, ?)',
        f'hybrid-stitching-policy:{SHA}',
        SHA,
        'frequency_partition_no_blend',
        _payload(mode='frequency_partition_no_blend'),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_hybrid_acoustic_results (hybrid_result_id, '
        'semantic_sha256, acoustic_scene_snapshot_id, scene_revision_id, '
        'stitching_policy_id, payload_json, recorded_at_utc) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        f'hybrid-acoustic-result:{SHA}',
        SHA,
        f'acoustic-scene-snapshot:{SHA}',
        revision_id,
        f'hybrid-stitching-policy:{SHA}',
        _payload(
            composition_spec={'observable': 'late_decay'},
            evidence_state='EXECUTED_UNVALIDATED',
        ),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_hybrid_prediction_providers (provider_id, '
        'semantic_sha256, base_provider_id, r160_artifact_id, '
        'r160_composition_spec_id, source_entity_id, receiver_id, '
        'payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        'r170b-hybrid-provider:' + SHA,
        SHA,
        'base-provider-1',
        'r160-stitched-hybrid-response:' + SHA,
        'r160-stitched-composition-spec:' + SHA,
        'src-1',
        'rcv-1',
        _payload(
            adapter_id='r170b-hybrid-provider',
            adapter_version='1',
            response_quantity='complex_pressure',
        ),
    )
    _insert(
        connection,
        'INSERT INTO cad_hybrid_prediction_provider_bindings (binding_id, '
        'semantic_sha256, provider_id, consumer_kind, consumer_id, '
        'payload_json) VALUES (?, ?, ?, ?, ?, ?)',
        'provider-binding-1',
        SHA,
        'r170b-hybrid-provider:' + SHA,
        'prediction_request',
        f'acoustic-prediction-request:{SHA}',
        _payload(),
    )
    _insert(
        connection,
        'INSERT INTO cad_hybrid_prediction_provider_objectives '
        '(connection_id, semantic_sha256, provider_id, objective_input_id, '
        'evaluation_id, payload_json) VALUES (?, ?, ?, ?, ?, ?)',
        'provider-objective-1',
        SHA,
        'r170b-hybrid-provider:' + SHA,
        'objective-input-1',
        'evaluation-1',
        _payload(),
    )
    _insert(
        connection,
        'INSERT INTO cad_acoustic_treatment_definitions (definition_id, '
        'definition_version, definition_sha256, payload_json) '
        'VALUES (?, ?, ?, ?)',
        'treatment-definition-1',
        'v1',
        SHA,
        _payload(),
    )
    _insert(
        connection,
        'INSERT INTO cad_acoustic_treatment_placements (instance_id, '
        'placement_version, lifecycle, definition_id, definition_version, '
        'definition_sha256, document_id, scene_revision_id, '
        'placement_sha256, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        'treatment-placement-1',
        1,
        'active',
        'treatment-definition-1',
        'v1',
        SHA,
        'doc-test',
        revision_id,
        SHA,
        _payload(),
    )
    _insert(
        connection,
        'INSERT INTO cad_treatment_boundary_overlays (overlay_id, '
        'overlay_hash_sha256, scene_revision_id, compiled_geometry_id, '
        'treatment_definition_id, treatment_definition_version, '
        'treatment_placement_instance_id, treatment_placement_version, '
        'surface_binding_evaluation_hash_sha256, payload_json, '
        'recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        'treatment-boundary-overlay:' + SHA,
        SHA,
        revision_id,
        f'r120-compiled-geometry:{SHA}',
        'treatment-definition-1',
        'v1',
        'treatment-placement-1',
        1,
        SHA,
        _payload(),
        NOW,
    )
    _insert(
        connection,
        'INSERT INTO cad_treatment_boundary_compositions (composition_id, '
        'composition_hash_sha256, scene_revision_id, compiled_geometry_id, '
        'host_surface_id, target_domain, payload_json, recorded_at_utc) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        'treatment-boundary-composition:' + SHA,
        SHA,
        revision_id,
        f'r120-compiled-geometry:{SHA}',
        'wall-east',
        'acoustic',
        _payload(target_domain='acoustic'),
        NOW,
    )


def _ledger(repository: SceneRepository) -> SolverOutputLedger:
    with connect_sqlite(repository.path) as connection:
        return build_solver_output_ledger(connection, 'doc-test')


def test_label_maps_cover_every_ledger_kind() -> None:
    """A new solver-ledger kind must ship its Japanese labels."""
    kinds = {spec.kind for spec in _SPECS}
    assert kinds <= set(_KIND_LABELS)
    for binding in ('bound', 'unbound'):
        assert binding in _BINDING_LABELS
    for state in ('resolved', 'unresolved'):
        assert state in _LINK_LABELS
    for state in ('ok', 'unreadable'):
        assert state in _PAYLOAD_STATE_LABELS


def test_ledger_empty_document_is_honest(tmp_path: Path) -> None:
    repository, _, _revision_id = _seed_repository(tmp_path)
    ledger = _ledger(repository)
    assert ledger.entries == ()
    assert ledger.bound_count == 0
    assert ledger.unresolved == ()


def test_ledger_resolves_full_solver_stack(tmp_path: Path) -> None:
    repository, _, revision_id = _seed_repository(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _seed_solver_stack(connection, revision_id)
    ledger = _ledger(repository)
    assert len(ledger.entries) == len(_SPECS)
    for entry in ledger.entries:
        assert entry.link_state == 'resolved'
        assert entry.scene_revision_ids == (revision_id,)
        assert entry.payload_state == 'ok'
    # The bound side: snapshot/geometry/leak-portal/boundary kinds carry a
    # canonical replay probe; the solver payload tables stay unbound.
    by_kind = {entry.kind: entry for entry in ledger.entries}
    for kind in (
        'scene_snapshot',
        'compiled_geometry',
        'leak_portal',
        'prediction_request',
        'boundary_overlay',
        'boundary_composition',
    ):
        assert by_kind[kind].binding == 'bound', kind
    for kind in (
        'solver_result',
        'path_artifact',
        'late_field',
        'stochastic_ray_estimate',
        'late_energy_decay',
        'stitched_response',
        'numerical_hybrid_response',
        'path_frequency_response',
        'hybrid_result',
        'stitching_policy',
        'prediction_provider',
        'provider_binding',
        'provider_objective',
        'solver_adapter',
        'dispatch_binding',
        'execution_input',
    ):
        assert by_kind[kind].binding == 'unbound', kind
    assert ledger.bound_count == 6


def test_ledger_extracts_payload_fields(tmp_path: Path) -> None:
    repository, _, revision_id = _seed_repository(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _seed_solver_stack(connection, revision_id)
    by_kind = {entry.kind: entry for entry in _ledger(repository).entries}
    solver_result = by_kind['solver_result']
    assert solver_result.observables == ('complex_pressure', 'late_energy_decay')
    assert solver_result.produced_by == 'r150-ga-execution:' + 'b' * 16
    assert solver_result.provenance_ref == (
        'r150-execution-provenance:' + 'c' * 16
    )
    assert solver_result.capability == 'COMPLETED'
    path_artifact = by_kind['path_artifact']
    assert path_artifact.observables == ('multi_portal_second_order_specular',)
    assert path_artifact.item_count == 2
    late_field = by_kind['late_field']
    assert late_field.capability == 'upper_bound_not_point_estimate'
    decay = by_kind['late_energy_decay']
    assert decay.capability == 'SUPPORTED'
    assert decay.item_count == 2
    stitched = by_kind['stitched_response']
    assert stitched.produced_by == 'htdt.r160.band-stitch v1'
    assert stitched.capability == 'GAP_PRESERVED'
    assert stitched.provenance_ref == f'acoustic-solver-result:{SHA}'
    numerical = by_kind['numerical_hybrid_response']
    assert numerical.observables == (
        'complex_acoustic_pressure_per_volume_velocity',
    )
    assert numerical.produced_by == 'r160-numerical-composition-spec:' + SHA
    assert numerical.capability == 'COMPLEX_SUPPORTED'
    assert numerical.provenance_ref == f'acoustic-solver-result:{SHA}'
    assert numerical.item_count == 1
    response = by_kind['path_frequency_response']
    assert response.observables == (
        'complex_acoustic_pressure_per_volume_velocity',
    )
    assert response.produced_by == f'deterministic-ga-execution-input:{SHA}'
    assert response.capability == 'COMPLEX_SUPPORTED'
    assert response.provenance_ref == f'deterministic-path-artifact:{SHA}'
    assert response.item_count == 2


def test_ledger_marks_orphaned_linkage(tmp_path: Path) -> None:
    """A row whose declared snapshot link resolves to nothing is unresolved."""
    repository, _, _revision_id = _seed_repository(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _insert(
            connection,
            'INSERT INTO cad_acoustic_solver_results (result_id, '
            'semantic_sha256, execution_id, dispatch_binding_id, '
            'prediction_request_id, acoustic_scene_snapshot_id, '
            'deterministic_solver_input_hash, payload_json, recorded_at_utc) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            f'acoustic-solver-result:{SHA}',
            SHA,
            'exec-orphan',
            f'acoustic-solver-dispatch:{SHA}',
            f'acoustic-prediction-request:{SHA}',
            'acoustic-scene-snapshot:missing',
            SHA,
            _payload(artifacts=[{'observable': 'complex_pressure'}]),
            NOW,
        )
    ledger = _ledger(repository)
    assert len(ledger.entries) == 1
    entry = ledger.entries[0]
    assert entry.link_state == 'unresolved'
    assert entry.scene_revision_ids == ()
    assert ledger.unresolved == (entry,)


def test_ledger_marks_unreadable_payload(tmp_path: Path) -> None:
    """A corrupt payload is listed as unreadable — never reconstructed."""
    repository, _, revision_id = _seed_repository(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _insert(
            connection,
            'INSERT INTO cad_hybrid_acoustic_results (hybrid_result_id, '
            'semantic_sha256, acoustic_scene_snapshot_id, scene_revision_id, '
            'stitching_policy_id, payload_json, recorded_at_utc) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            f'hybrid-acoustic-result:{SHA}',
            SHA,
            'snapshot-missing',
            revision_id,
            'policy-missing',
            '{ not json',
            NOW,
        )
    ledger = _ledger(repository)
    (entry,) = ledger.entries
    assert entry.payload_state == 'unreadable'
    assert entry.observables == ()
    assert entry.provenance_ref is None
    # Column-resolved linkage still works — only payload fields are empty.
    assert entry.link_state == 'resolved'
    assert entry.scene_revision_ids == (revision_id,)


def test_dialog_renders_japanese_labels(tmp_path: Path) -> None:
    _app()
    repository, _, revision_id = _seed_repository(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _seed_solver_stack(connection, revision_id)
    ledger = _ledger(repository)
    revisions = repository.list_revision_summaries('doc-test')
    dialog = SolverOutputDiagnosticsDialog(ledger, revisions)
    try:
        assert dialog.table.rowCount() == len(_SPECS)
        texts = [
            dialog.table.item(row, 0).text()
            for row in range(dialog.table.rowCount())
        ]
        assert '確定的パス成果物' in texts
        assert '遅延エネルギー減衰成果物' in texts
        assert '処理境界構成' in texts
        # Raw identifiers never reach the user.
        for row in range(dialog.table.rowCount()):
            for column in range(dialog.table.columnCount()):
                text = dialog.table.item(row, column).text()
                assert 'cad_' not in text
                assert 'r160_' not in text
                assert 'STRUCTURAL_ONLY' not in text
        observables = [
            dialog.table.item(row, 1).text()
            for row in range(dialog.table.rowCount())
        ]
        assert 'マルチポータル2次鏡面' in observables
        assert '遅延エネルギー密度、遅延エネルギー減衰' in observables
        assert '検証済み' in {
            dialog.table.item(row, 3).text()
            for row in range(dialog.table.rowCount())
        }
        assert '台帳のみ' in {
            dialog.table.item(row, 3).text()
            for row in range(dialog.table.rowCount())
        }
        assert dialog.empty_label.isHidden()
        assert f'{len(_SPECS)} 件' in dialog.summary_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_dialog_revision_filter_and_empty_state(tmp_path: Path) -> None:
    _app()
    repository, _, revision_id = _seed_repository(tmp_path)
    ledger = _ledger(repository)
    revisions = repository.list_revision_summaries('doc-test')
    dialog = SolverOutputDiagnosticsDialog(ledger, revisions)
    try:
        combo = dialog.revision_combo
        assert combo.count() == 2  # 「すべて」+ one revision
        combo.setCurrentIndex(1)
        assert combo.currentData() == revision_id
        assert dialog.table.rowCount() == 0
        assert not dialog.empty_label.isHidden()
        assert 'ありません' in dialog.empty_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_dialog_detail_shows_honest_states(tmp_path: Path) -> None:
    _app()
    repository, _, revision_id = _seed_repository(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _insert(
            connection,
            'INSERT INTO cad_acoustic_solver_results (result_id, '
            'semantic_sha256, execution_id, dispatch_binding_id, '
            'prediction_request_id, acoustic_scene_snapshot_id, '
            'deterministic_solver_input_hash, payload_json, recorded_at_utc) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            f'acoustic-solver-result:{SHA}',
            SHA,
            'exec-orphan',
            'binding-missing',
            'request-missing',
            'snapshot-missing',
            SHA,
            '{ unreadable',
            NOW,
        )
    ledger = _ledger(repository)
    revisions = repository.list_revision_summaries('doc-test')
    dialog = SolverOutputDiagnosticsDialog(
        ledger,
        revisions,
        open_authority_graph=lambda parent, revision: None,
    )
    try:
        combo = dialog.revision_combo
        # Orphaned row surfaces under the honest「未解決」bucket.
        unresolved_index = combo.count() - 1
        assert combo.itemText(unresolved_index) == '（リビジョン未解決）'
        combo.setCurrentIndex(unresolved_index)
        assert dialog.table.rowCount() == 1
        dialog.table.selectRow(0)
        assert '未解決' in dialog.payload_state_label.text()
        assert '読み取り不可' in dialog.payload_state_label.text()
        assert 'リンク未解決' in dialog.summary_label.text()
        assert '読み取り不可' in dialog.summary_label.text()
        # An orphaned row has no authority node to open on.
        assert not dialog.authority_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_dialog_opens_authority_graph_on_resolved_revision(
    tmp_path: Path,
) -> None:
    _app()
    repository, _, revision_id = _seed_repository(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _seed_solver_stack(connection, revision_id)
    ledger = _ledger(repository)
    revisions = repository.list_revision_summaries('doc-test')
    opened: list[tuple] = []
    dialog = SolverOutputDiagnosticsDialog(
        ledger,
        revisions,
        open_authority_graph=lambda parent, revision: opened.append(
            (parent, revision)
        ),
    )
    try:
        assert not dialog.authority_button.isEnabled()
        dialog.table.selectRow(0)
        assert dialog.authority_button.isEnabled()
        dialog.authority_button.click()
        assert opened == [(dialog, revision_id)]
    finally:
        dialog.close()
        dialog.deleteLater()


def test_support_page_solver_diagnostics_button(tmp_path: Path) -> None:
    _app()
    opened: list[object] = []
    page = SupportPage(
        tmp_path,
        open_solver_diagnostics=lambda parent: opened.append(parent),
    )
    try:
        assert page.solver_button is not None
        page.solver_button.click()
        assert opened == [page]
    finally:
        page.close()
        page.deleteLater()
