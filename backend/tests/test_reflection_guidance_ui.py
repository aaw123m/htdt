"""REV36-GUIDANCE-PROD: the Room acoustics dock hosts the reflection
guidance surface — ranked ``ReflectionGuidanceReport`` items replayed from
persisted R150 deterministic path artifacts, with honest empty/issue
states and JA labels on every row.
"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_geometric_acoustics_adapter import (
    BoundaryMaterialContribution,
    DeterministicAcousticPath,
    DeterministicPathArtifact,
    DeterministicPathBandQuantity,
    SourceDirectivityContribution,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    F1_DOCUMENT_ID,
    Position3,
    make_empty_scene,
    make_f1_scene,
)
from htdt.cad_schema import connect_sqlite
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef
from htdt.reflection_guidance_presentation import (
    load_reflection_guidance_view,
)
from htdt.reflection_guidance_ui import ReflectionGuidancePanel
from htdt.room_workspace import RoomWorkspaceController


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


SHA = 'a' * 64
NOW = '2026-10-03T00:00:00+00:00'


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(payload: object) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _authority(label: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test:{label}',
        authority_version='1',
        semantic_hash_sha256=_hash(label),
    )


SOLVER_REF = _authority('solver')


def _band(surface_id: str | None = None) -> DeterministicPathBandQuantity:
    return DeterministicPathBandQuantity(
        center_hz=100.0,
        spreading_factor_per_m2=1.0,
        source_directivity=SourceDirectivityContribution(
            dataset_id='dataset-1',
            dataset_version='1',
            dataset_semantic_sha256=_hash('dataset'),
            evaluation_semantic_sha256=_hash('eval'),
            frequency_hz=100.0,
            horizontal_angle_deg=0.0,
            vertical_angle_deg=0.0,
            magnitude_db=0.0,
            magnitude_linear=1.0,
            energy_factor=1.0,
        ),
        boundary_material=(
            None
            if surface_id is None
            else BoundaryMaterialContribution(
                source_surface_id=surface_id,
                material_authority=_authority('material'),
                frequency_hz=100.0,
                absorption=0.1,
                scattering=0.0,
                specular_energy_factor=0.9,
            )
        ),
        relative_energy_transport_per_m2=1.0,
    )


def _path(
    *,
    path_type: str,
    length_m: float,
    delay_s: float,
    surface_id: str | None = None,
    point: Position3 | None = None,
    source_entity_id: str = 'speaker-fl',
    receiver_id: str = 'seat-1',
    receiver_entity_id: str = 'point-mlp',
) -> DeterministicAcousticPath:
    kwargs = dict(
        source_entity_id=source_entity_id,
        receiver_id=receiver_id,
        receiver_entity_id=receiver_entity_id,
        path_type=path_type,
        ordered_interaction_surface_ids=(
            () if surface_id is None else (surface_id,)
        ),
        ordered_interaction_points=(
            () if point is None else (point,)
        ),
        geometric_path_length_m=length_m,
        propagation_delay_s=delay_s,
        departure_direction=Direction3(x=1.0, y=0.0, z=0.0),
        arrival_direction=Direction3(x=-1.0, y=0.0, z=0.0),
        bands=(_band(surface_id),),
        solver_implementation_ref=SOLVER_REF,
    )
    probe = DeterministicAcousticPath.model_construct(
        path_id='deterministic-acoustic-path:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return DeterministicAcousticPath(
        path_id=f'deterministic-acoustic-path:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


def _artifact(
    paths: tuple[DeterministicAcousticPath, ...],
    *,
    snapshot_id: str,
    snapshot_sha256: str,
    salt: str = 'a',
) -> DeterministicPathArtifact:
    kwargs = dict(
        execution_id='r150-ga-execution:' + _hash(f'execution-{salt}'),
        execution_input_id='ga-input-' + salt,
        execution_input_sha256=_hash(f'execution-input-{salt}'),
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        prediction_request_id='req-' + salt,
        prediction_request_sha256=_hash(f'request-{salt}'),
        dispatch_binding_id='dispatch-' + salt,
        dispatch_binding_sha256=_hash(f'dispatch-{salt}'),
        adapter_descriptor_id='adapter-' + salt,
        adapter_descriptor_sha256=_hash(f'adapter-{salt}'),
        solver_implementation_ref=SOLVER_REF,
        solver_configuration_ref=_authority(f'configuration-{salt}'),
        r120_compiled_geometry_id='compiled-' + salt,
        r120_compiled_geometry_sha256=_hash(f'compiled-{salt}'),
        topology_identity_sha256=_hash(f'topology-{salt}'),
        engine_id='fixture-engine',
        engine_version='1',
        numeric_comparison_tolerance_m=1.0e-9,
        identity_decimal_places=9,
        frequency_domain=FrequencyDomain(minimum_hz=20.0, maximum_hz=500.0),
        paths=paths,
        rejected_candidates=(),
    )
    probe = DeterministicPathArtifact.model_construct(
        artifact_id='deterministic-path-artifact:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return DeterministicPathArtifact(
        artifact_id=f'deterministic-path-artifact:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


def _snapshot_payload(speed: float | None = 343.0) -> str:
    environment = (
        None
        if speed is None
        else {
            'authority': {
                'authority_id': 'test:environment',
                'authority_version': '1',
                'semantic_hash_sha256': _hash('environment'),
            },
            'sound_speed_m_s': speed,
            'sound_speed_source_authority': {
                'authority_id': 'test:sound-speed',
                'authority_version': '1',
                'semantic_hash_sha256': _hash('sound-speed'),
            },
        }
    )
    return json.dumps(
        {'environment': environment},
        ensure_ascii=False,
        sort_keys=True,
    )


def _seed_document(tmp_path: Path) -> tuple[SceneRepository, str]:
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    revision = repository.save(make_f1_scene(), parent_revision_id=None)
    return repository, revision.revision.revision_id


def _insert_compiled_geometry(connection, *, revision_id: str) -> None:
    """Parent row the snapshot table's FK requires."""
    connection.execute(
        'INSERT INTO cad_r120_compiled_geometry (compiled_geometry_id, '
        'compiled_hash_sha256, scene_revision_id, '
        'scene_revision_content_hash, semantic_geometry_id, '
        'semantic_geometry_hash_sha256, request_id, payload_json, '
        'recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            f'r120-compiled-geometry:{SHA}',
            SHA,
            revision_id,
            SHA,
            f'semantic-geometry:{SHA}',
            SHA,
            'req-1',
            json.dumps({'authority_version': 'r120-compiled-geometry-1'}),
            NOW,
        ),
    )


def _insert_snapshot(
    connection,
    *,
    snapshot_id: str,
    snapshot_sha256: str,
    document_id: str,
    revision_id: str,
    speed: float | None = 343.0,
) -> None:
    _insert_compiled_geometry(
        connection, revision_id=revision_id
    )
    connection.execute(
        'INSERT INTO cad_acoustic_scene_snapshots (snapshot_id, '
        'semantic_sha256, document_id, scene_revision_id, '
        'scene_content_hash, r120_compiled_geometry_id, '
        'r120_compiled_geometry_sha256, '
        'material_boundary_configuration_sha256, payload_json, '
        'recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            snapshot_id,
            snapshot_sha256,
            document_id,
            revision_id,
            _hash(f'scene-{snapshot_id}'),
            f'r120-compiled-geometry:{SHA}',
            SHA,
            SHA,
            _snapshot_payload(speed),
            NOW,
        ),
    )


def _insert_artifact(connection, artifact: DeterministicPathArtifact) -> None:
    connection.execute(
        'INSERT INTO cad_deterministic_path_artifacts (artifact_id, '
        'semantic_sha256, execution_id, '
        'execution_provenance_authority_id, execution_input_id, '
        'snapshot_id, prediction_request_id, dispatch_binding_id, '
        'r120_compiled_geometry_id, payload_json, recorded_at_utc) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            artifact.artifact_id,
            artifact.semantic_sha256,
            artifact.execution_id,
            'r150-ga-execution-provenance:' + _hash('provenance'),
            artifact.execution_input_id,
            artifact.snapshot_id,
            artifact.prediction_request_id,
            artifact.dispatch_binding_id,
            artifact.r120_compiled_geometry_id,
            artifact.model_dump_json(),
            NOW,
        ),
    )


def _seed_guidance(
    connection,
    *,
    document_id: str,
    revision_id: str,
    surfaces: tuple[str, ...] = ('wall-left', 'floor'),
    speed: float | None = 343.0,
) -> tuple[str, str]:
    snapshot_id = f'acoustic-scene-snapshot:{_hash("snap" + revision_id)}'
    snapshot_sha256 = _hash(f'snapshot-sha-{snapshot_id}')
    _insert_snapshot(
        connection,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        document_id=document_id,
        revision_id=revision_id,
        speed=speed,
    )
    direct = _path(path_type='direct', length_m=5.0, delay_s=5.0 / 343.0)
    # DeterministicPathArtifact validates canonical path ordering: direct
    # first, then speculars sorted by surface tuple then path id.
    speculars = tuple(
        sorted(
            (
                _path(
                    path_type='specular_reflection',
                    length_m=7.0 + index,
                    delay_s=(7.0 + index) / 343.0,
                    surface_id=surface,
                    point=Position3(x_m=float(index), y_m=2.0, z_m=1.0),
                )
                for index, surface in enumerate(surfaces)
            ),
            key=lambda path: (
                path.ordered_interaction_surface_ids,
                path.path_id,
            ),
        )
    )
    artifact = _artifact(
        (direct, *speculars),
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
    )
    _insert_artifact(connection, artifact)
    return snapshot_id, artifact.artifact_id


def _load(repository: SceneRepository, document_id: str):
    with connect_sqlite(repository.path) as connection:
        return load_reflection_guidance_view(connection, document_id)


def test_guidance_view_empty_without_artifacts(tmp_path) -> None:
    repository, _revision_id = _seed_document(tmp_path)
    view = _load(repository, F1_DOCUMENT_ID)
    assert view.entries == ()
    assert view.issues == ()


def test_guidance_view_replays_persisted_pairs(tmp_path) -> None:
    repository, revision_id = _seed_document(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision_id,
        )
    view = _load(repository, F1_DOCUMENT_ID)
    assert view.issues == ()
    assert len(view.entries) == 2
    surfaces = {entry.surface_id for entry in view.entries}
    assert surfaces == {'wall-left', 'floor'}
    for entry in view.entries:
        assert entry.scene_revision_id == revision_id
        assert entry.environment_state == 'bound'
        assert entry.speed_of_sound_m_s == pytest.approx(343.0)
        report = entry.report
        assert report.status == 'actionable'
        assert report.request_semantic_sha256
        kinds = [item.kind for item in report.items]
        assert kinds == [
            'treat_reflection_zone',
            'reposition_source',
            'verify_with_measurement',
        ]
        # No measured ETC lane exists — confidence stays honest.
        assert all(
            item.confidence == 'unverified_hypothesis'
            for item in report.items
        )
        assert entry.hypothesis is not None
        assert entry.excess_delay_s > 0.0
    # Revision filter returns exactly the seeded entries.
    assert view.for_revision(revision_id) == view.entries
    assert view.for_revision('other-revision') == ()


def test_guidance_view_reports_unreadable_artifact(tmp_path) -> None:
    repository, revision_id = _seed_document(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        snapshot_id, _artifact_id = _seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision_id,
        )
        connection.execute(
            'UPDATE cad_deterministic_path_artifacts '
            'SET payload_json=? WHERE snapshot_id=?',
            ('{not json', snapshot_id),
        )
    view = _load(repository, F1_DOCUMENT_ID)
    assert view.entries == ()
    assert len(view.issues) == 1
    assert view.issues[0].kind == 'unreadable_artifact'


def test_guidance_view_reports_snapshot_binding_mismatch(tmp_path) -> None:
    repository, revision_id = _seed_document(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        snapshot_id, _artifact_id = _seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision_id,
        )
        connection.execute(
            'UPDATE cad_deterministic_path_artifacts '
            'SET payload_json=? WHERE snapshot_id=?',
            (
                # Rebuild the artifact bound to a different snapshot hash.
                _artifact(
                    (
                        _path(
                            path_type='direct',
                            length_m=5.0,
                            delay_s=5.0 / 343.0,
                        ),
                        _path(
                            path_type='specular_reflection',
                            length_m=7.0,
                            delay_s=7.0 / 343.0,
                            surface_id='wall-left',
                            point=Position3(x_m=0.0, y_m=2.0, z_m=1.0),
                        ),
                    ),
                    snapshot_id=snapshot_id,
                    snapshot_sha256=_hash('tampered-snapshot'),
                    salt='tampered',
                ).model_dump_json(),
                snapshot_id,
            ),
        )
    view = _load(repository, F1_DOCUMENT_ID)
    assert view.entries == ()
    assert [issue.kind for issue in view.issues] == ['binding_mismatch']


def test_guidance_view_ignores_other_documents(tmp_path) -> None:
    repository, _revision_id = _seed_document(tmp_path)
    other = repository.save(
        make_empty_scene('other-document'), parent_revision_id=None
    ).revision
    with connect_sqlite(repository.path) as connection, connection:
        _seed_guidance(
            connection,
            document_id='other-document',
            revision_id=other.revision_id,
        )
    view = _load(repository, F1_DOCUMENT_ID)
    assert view.entries == ()
    assert view.issues == ()
    assert view.document_id == F1_DOCUMENT_ID


def _panel(tmp_path) -> ReflectionGuidancePanel:
    _app()
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    room = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    return ReflectionGuidancePanel(room)


def test_guidance_panel_lists_ranked_items(tmp_path) -> None:
    repository, revision_id = _seed_document(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        _seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision_id,
        )
    panel = _panel(tmp_path)
    assert panel.empty_label.text() == ''
    assert 'ガイダンス' in panel.summary_label.text()
    # Combo rows resolve scene entity names, never raw ids alone.
    labels = [
        panel.entry_combo.itemText(index)
        for index in range(panel.entry_combo.count())
    ]
    assert len(labels) == 2
    assert all('Front Left → MLP' in label for label in labels)
    assert any('wall-left' in label for label in labels)
    assert any('floor' in label for label in labels)
    rows = [
        panel.item_list.item(index).text()
        for index in range(panel.item_list.count())
    ]
    assert rows == [
        '1. 一次反射ゾーンの処理 — 未検証の仮説',
        '2. 音源の再配置 — 未検証の仮説',
        '3. 測定による検証 — 未検証の仮説',
    ]
    assert 'deterministic-path-artifact:' in panel.authority_label.text()
    assert panel.rationale_label.text() != '-'
    assert panel.action_label.text() != '-'


def test_guidance_panel_honest_empty_state(tmp_path) -> None:
    _seed_document(tmp_path)
    panel = _panel(tmp_path)
    assert '確定的パス成果物がまだありません' in panel.empty_label.text()
    assert panel.item_list.count() == 0
    assert panel.surface_label.text() == '-'


def test_guidance_panel_surfaces_unreadable_rows(tmp_path) -> None:
    repository, revision_id = _seed_document(tmp_path)
    with connect_sqlite(repository.path) as connection, connection:
        snapshot_id, _artifact_id = _seed_guidance(
            connection,
            document_id=F1_DOCUMENT_ID,
            revision_id=revision_id,
        )
        connection.execute(
            'UPDATE cad_deterministic_path_artifacts '
            'SET payload_json=? WHERE snapshot_id=?',
            ('{not json', snapshot_id),
        )
    panel = _panel(tmp_path)
    assert panel.item_list.count() == 0
    assert '読み取り不可' in panel.issue_label.text()
    assert '読み飛ばし' in panel.summary_label.text()
