"""Issue #1011 — cable wiring inspection + route-geometry authority.

M1: listing of persisted CableRuns (endpoint bindings, signal-path edge
ref, per-segment path kinds / declared lengths / service loop, lineage,
freshness) and the honest display contract — only exactly-bound
endpoints carry a position, and geometry-absent routes are labelled
``経路形状未登録`` rather than drawn. M2: the optional sealed
``CableRunGeometry`` waypoint authority — build/hash, per-segment
divergence, freshness, repository round-trip under the v116 table, and
legacy length-only runs staying valid.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from htdt.cad_cable_run import (
    CableRunEndpoint,
    CableRunSegment,
    build_cable_run,
    evaluate_cable_run_freshness,
)
from htdt.cad_cable_run_geometry import (
    CableRunSegmentGeometry,
    build_cable_run_geometry,
    evaluate_cable_run_geometry_divergence,
    evaluate_cable_run_geometry_freshness,
)
from htdt.cad_cable_run_geometry_repository import (
    CadCableRunGeometryRepository,
)
from htdt.cad_cable_run_inspection import (
    CableEndpointBinding,
    CableRunInspection,
    inspect_cable_runs,
    scene_surface_ids,
)
from htdt.cad_cable_run_repository import CadCableRunRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    connect_sqlite,
    ensure_native_schema,
)
from htdt.cad_schema_ddl import NATIVE_SCHEMA_TABLES
from htdt.cad_signal_path_repository import CadSignalPathRepository
from htdt.cable_run_panel import (
    CableRunPanel,
    overlay_item_for_inspection,
)
from htdt.native_authority_audit import audit_table_modes
from htdt.native_row_integrity import (
    assert_row_integrity_registry_complete,
    scan_native_row_integrity,
)

NOW = '2026-10-09T00:00:00+00:00'


def _scene(document_id: str = 'doc-1') -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='rack-avr',
                kind='av_equipment',
                name='AVR rack',
                position=Position3(x_m=5.5, y_m=0.4, z_m=0.9),
                size_m=Size3(x_m=0.5, y_m=0.4, z_m=0.8),
            ),
        ),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _scene(), parent_revision_id=None
    ).revision
    return (
        scene_repository,
        revision,
        CadCableRunRepository(scene_repository),
    )


def _run(revision, **overrides) -> 'object':
    payload = {
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'label': 'FL speaker cable',
        'kind': 'speaker_signal',
        'from_endpoint': CableRunEndpoint(
            label='AVR FL output', entity_id='rack-avr'
        ),
        'to_endpoint': CableRunEndpoint(
            label='FL speaker terminals', entity_id='speaker-fl'
        ),
        'segments': (
            CableRunSegment(
                sequence=0,
                path_kind='conduit',
                length_m=1.5,
                description='rack to floor box',
            ),
            CableRunSegment(
                sequence=1, path_kind='under_floor', length_m=3.2
            ),
            CableRunSegment(
                sequence=2, path_kind='in_wall', length_m=1.1
            ),
        ),
        'service_loop_m': 0.5,
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_cable_run(**payload)


def _waypoints(*points):
    return tuple(
        Position3(x_m=x, y_m=y, z_m=z) for x, y, z in points
    )


def _geometry_for(run, **kwargs):
    payload = {
        'run': run,
        'segment_geometries': (
            CableRunSegmentGeometry(
                segment_sequence=0,
                waypoints=_waypoints((0, 0, 0), (1.5, 0, 0)),
                concealment_kind='conduit',
            ),
        ),
        'created_at_utc': NOW,
    }
    payload.update(kwargs)
    return build_cable_run_geometry(**payload)


def _inspection_setup(tmp_path: Path):
    scene_repo, revision, run_repo = _repositories(tmp_path)
    run = _run(revision)
    run_repo.save_run(run)
    geometry_repo = CadCableRunGeometryRepository(
        scene_repo, run_repo
    )
    signal_repo = CadSignalPathRepository(scene_repo)
    return scene_repo, revision, run_repo, run, geometry_repo, signal_repo


def _inspect(scene_repo, revision, run_repo, geometry_repo, signal_repo):
    return inspect_cable_runs(
        scene_repository=scene_repo,
        document_id=revision.document_id,
        cable_run_repository=run_repo,
        geometry_repository=geometry_repo,
        signal_path_repository=signal_repo,
    )


class CableRunGeometryAuthorityTests(unittest.TestCase):
    def test_build_seals_hash_and_stores_waypoints(self):
        revision = type(
            'R', (), {'document_id': 'doc-1', 'revision_id': 'rev-1',
                      'content_hash': 'a' * 64}
        )()
        run = _run(revision)
        geometry = _geometry_for(
            run,
            segment_geometries=(
                CableRunSegmentGeometry(
                    segment_sequence=0,
                    waypoints=_waypoints(
                        (0, 0, 0), (3, 0, 0), (3, 4, 0)
                    ),
                    concealment_kind='conduit',
                    traversed_surface_ids=('wall-a', 'wall-b'),
                    source='authored',
                    record_kind='design',
                ),
            ),
        )
        # Sealed: a payload round-trip re-validates the recorded hash.
        from htdt.cad_cable_run_geometry import CableRunGeometry
        self.assertEqual(len(geometry.semantic_sha256), 64)
        CableRunGeometry.model_validate(
            geometry.model_dump(mode='json')
        )
        self.assertAlmostEqual(geometry.geometric_length_m, 7.0)
        segment = geometry.segment_geometries[0]
        self.assertEqual(
            segment.traversed_surface_ids, ('wall-a', 'wall-b')
        )
        self.assertAlmostEqual(segment.polyline_length_m(), 7.0)
        self.assertEqual(geometry.run_semantic_sha256, run.semantic_sha256)

    def test_undeclared_sequence_rejected(self):
        revision = type(
            'R', (), {'document_id': 'doc-1', 'revision_id': 'rev-1',
                      'content_hash': 'a' * 64}
        )()
        run = _run(revision)
        with self.assertRaises(ValueError):
            _geometry_for(
                run,
                segment_geometries=(
                    CableRunSegmentGeometry(
                        segment_sequence=9,
                        waypoints=_waypoints((0, 0, 0), (1, 0, 0)),
                        concealment_kind='conduit',
                    ),
                ),
            )

    def test_divergence_keeps_numbers_separate(self):
        revision = type(
            'R', (), {'document_id': 'doc-1', 'revision_id': 'rev-1',
                      'content_hash': 'a' * 64}
        )()
        run = _run(revision)
        geometry = _geometry_for(
            run,
            segment_geometries=(
                # Recorded 5.0 m route vs declared 1.5 m conduit segment;
                # a different concealment kind than the run declared.
                CableRunSegmentGeometry(
                    segment_sequence=0,
                    waypoints=_waypoints(
                        (0, 0, 0), (3.0, 0, 0), (3.0, 2.0, 0)
                    ),
                    concealment_kind='in_wall',
                    source='as_built_survey',
                    record_kind='as_built',
                ),
            ),
        )
        divergence = evaluate_cable_run_geometry_divergence(run, geometry)
        self.assertTrue(divergence.divergent)
        self.assertEqual(divergence.unregistered_sequences, (1, 2))
        self.assertTrue(divergence.segments[0].concealment_mismatch)
        self.assertAlmostEqual(divergence.geometric_length_m, 5.0)
        # Declared totals keep service loop separate — never overwritten.
        self.assertAlmostEqual(divergence.declared_segment_total_m, 5.8)
        self.assertAlmostEqual(divergence.service_loop_m, 0.5)
        self.assertAlmostEqual(divergence.declared_total_length_m, 6.3)
        self.assertAlmostEqual(divergence.total_delta_m, -0.8)

    def test_freshness_vocabulary(self):
        revision = type(
            'R', (), {'document_id': 'doc-1', 'revision_id': 'rev-1',
                      'content_hash': 'a' * 64}
        )()
        run = _run(revision)
        geometry = _geometry_for(
            run,
            segment_geometries=(
                CableRunSegmentGeometry(
                    segment_sequence=0,
                    waypoints=_waypoints((0, 0, 0), (1, 0, 0)),
                    concealment_kind='conduit',
                    traversed_surface_ids=('wall-a',),
                ),
            ),
        )
        current = evaluate_cable_run_geometry_freshness(
            geometry,
            run=run,
            scene_content_hash=run.scene_content_hash,
            present_surface_ids={'wall-a'},
        )
        self.assertEqual(current.status, 'current')
        moved_run = run.model_copy(
            update={'semantic_sha256': 'b' * 64}
        )
        stale = evaluate_cable_run_geometry_freshness(
            geometry,
            run=moved_run,
            scene_content_hash=run.scene_content_hash,
            present_surface_ids={'wall-a'},
        )
        self.assertEqual(stale.status, 'stale')
        drifted = evaluate_cable_run_geometry_freshness(
            geometry,
            run=run,
            scene_content_hash='c' * 64,
            present_surface_ids={'wall-a'},
        )
        self.assertEqual(drifted.status, 'stale')
        missing = evaluate_cable_run_geometry_freshness(
            geometry,
            run=run,
            scene_content_hash=run.scene_content_hash,
            present_surface_ids=set(),
        )
        self.assertEqual(missing.status, 'missing')
        gone = evaluate_cable_run_geometry_freshness(
            geometry,
            run=None,
            scene_content_hash=run.scene_content_hash,
            present_surface_ids={'wall-a'},
        )
        self.assertEqual(gone.status, 'missing')


class InspectionTests(unittest.TestCase):
    def test_listing_endpoints_segments_lineage_freshness(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                signal_repo,
            ) = _inspection_setup(tmp)
            self.addCleanup(scene_repo.close)
            rows = _inspect(
                scene_repo, revision, run_repo, geometry_repo, signal_repo
            )
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.run_id, run.run_id)
            self.assertEqual(row.label, 'FL speaker cable')
            self.assertEqual(row.freshness_status, 'current')
            self.assertEqual(row.route_state, 'unregistered')
            self.assertEqual(row.from_endpoint.status, 'bound')
            self.assertAlmostEqual(
                row.from_endpoint.position.x_m, 5.5
            )
            self.assertEqual(row.to_endpoint.status, 'bound')
            self.assertIsNone(row.signal_path_edge_id)
            self.assertIsNone(row.signal_path_edge_present)
            self.assertEqual(
                [s.path_kind for s in row.segments],
                ['conduit', 'under_floor', 'in_wall'],
            )
            self.assertEqual(
                [s.declared_length_m for s in row.segments],
                [1.5, 3.2, 1.1],
            )
            self.assertEqual(row.service_loop_m, 0.5)
            self.assertAlmostEqual(row.total_length_m, 6.3)
            self.assertEqual(len(row.versions), 1)
            self.assertEqual(row.versions[0].version, '1')

    def test_unbound_and_missing_endpoints(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            scene_repo, revision, run_repo = _repositories(tmp)
            self.addCleanup(scene_repo.close)
            run_repo.save_run(
                _run(
                    revision,
                    from_endpoint=CableRunEndpoint(label='パネル裏'),
                    to_endpoint=CableRunEndpoint(
                        label='撤去済み機器', entity_id='gone-entity'
                    ),
                    signal_path_edge_id='edge-fl-speaker',
                )
            )
            rows = _inspect(
                scene_repo,
                revision,
                run_repo,
                CadCableRunGeometryRepository(scene_repo, run_repo),
                CadSignalPathRepository(scene_repo),
            )
            row = rows[0]
            self.assertEqual(row.from_endpoint.status, 'unbound')
            self.assertIsNone(row.from_endpoint.position)
            self.assertEqual(row.to_endpoint.status, 'missing')
            self.assertIsNone(row.to_endpoint.position)
            # Edge ref is listed but cannot resolve to a saved path.
            self.assertEqual(row.signal_path_edge_id, 'edge-fl-speaker')
            self.assertFalse(row.signal_path_edge_present)
            self.assertEqual(row.freshness_status, 'missing')

    def test_stale_after_scene_drift(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                _run_obj,
                geometry_repo,
                signal_repo,
            ) = _inspection_setup(tmp)
            self.addCleanup(scene_repo.close)
            # New head revision — the run's pinned content hash drifts.
            # Widen the room — same entities, different content hash.
            moved = _scene()
            moved = moved.model_copy(
                update={
                    'room': moved.room.model_copy(
                        update={'width_m': 6.5}
                    ),
                }
            )
            scene_repo.save(
                moved,
                parent_revision_id=revision.revision_id,
            )
            rows = _inspect(
                scene_repo, revision, run_repo, geometry_repo, signal_repo
            )
            self.assertEqual(rows[0].freshness_status, 'stale')

    def test_overlay_item_only_binds_exact_endpoints(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                _run_obj,
                geometry_repo,
                signal_repo,
            ) = _inspection_setup(tmp)
            self.addCleanup(scene_repo.close)
            rows = _inspect(
                scene_repo, revision, run_repo, geometry_repo, signal_repo
            )
            item = overlay_item_for_inspection(rows[0])
            self.assertEqual(item.route_state, 'unregistered')
            self.assertIsNotNone(item.from_endpoint.position)
            self.assertIsNotNone(item.to_endpoint.position)
            # A missing/unbound endpoint projects to no position at all —
            # the viewport can never place it.
            fake = CableRunInspection(
                run_id='r',
                version='1',
                label='x',
                kind='speaker_signal',
                medium='copper',
                gauge=None,
                from_endpoint=CableEndpointBinding(
                    label='a',
                    entity_id='e',
                    status='missing',
                    position=None,
                ),
                to_endpoint=CableEndpointBinding(
                    label='b',
                    entity_id=None,
                    status='unbound',
                    position=None,
                ),
                signal_path_edge_id=None,
                signal_path_edge_present=None,
                freshness_status='current',
                freshness_reasons=(),
                segments=(),
                service_loop_m=0.0,
                total_length_m=0.0,
                route_state='unregistered',
                geometry=None,
                versions=(),
                scene_revision_id='rev-1',
                scene_content_hash='a' * 64,
            )
            projected = overlay_item_for_inspection(fake)
            self.assertIsNone(projected.from_endpoint.position)
            self.assertIsNone(projected.to_endpoint.position)


class GeometryRepositoryTests(unittest.TestCase):
    def test_round_trip_and_latest_for_run(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                _signal,
            ) = _inspection_setup(tmp)
            self.addCleanup(scene_repo.close)
            record = _geometry_for(
                    run,
                    segment_geometries=(
                        CableRunSegmentGeometry(
                            segment_sequence=0,
                            waypoints=_waypoints(
                                (5.5, 0.4, 0.9),
                                (3.0, 0.6, 1.8),
                                (1.2, 0.8, 1.0),
                            ),
                            concealment_kind='conduit',
                            traversed_surface_ids=('wall-partition',),
                            source='authored',
                            record_kind='design',
                        ),
                    ),
                )
            geometry_repo.save_geometry(record)
            loaded = geometry_repo.get_geometry(
                record.geometry_id, record.version
            )
            self.assertIsNotNone(loaded)
            self.assertEqual(
                loaded.semantic_sha256, record.semantic_sha256
            )
            self.assertEqual(
                geometry_repo.get_geometry_by_hash(
                    record.semantic_sha256
                ).geometry_id,
                record.geometry_id,
            )
            self.assertEqual(
                geometry_repo.latest_geometry_for_run(
                    run.run_id, run.version
                ).geometry_id,
                record.geometry_id,
            )
            self.assertEqual(
                len(geometry_repo.list_geometries(revision.document_id)),
                1,
            )
            # Row integrity: duplicated columns stay bound to payload.
            with connect_sqlite(scene_repo.path) as connection:
                self.assertEqual(
                    scan_native_row_integrity(connection), ()
                )
            self.assertEqual(
                audit_table_modes()['cad_cable_run_geometries'],
                'replay_canonical',
            )

    def test_save_rejects_unpinned_run(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                _signal,
            ) = _inspection_setup(tmp)
            self.addCleanup(scene_repo.close)
            ghost = _run(
                revision, run_id='run-never-saved', version='1'
            )
            with self.assertRaises(ValueError):
                geometry_repo.save_geometry(_geometry_for(ghost))

    def test_legacy_length_only_run_stays_valid(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                signal_repo,
            ) = _inspection_setup(tmp)
            self.addCleanup(scene_repo.close)
            rows = _inspect(
                scene_repo, revision, run_repo, geometry_repo, signal_repo
            )
            self.assertEqual(rows[0].freshness_status, 'current')
            self.assertEqual(rows[0].route_state, 'unregistered')
            self.assertIsNone(rows[0].geometry)
            result = evaluate_cable_run_freshness(
                run,
                scene_content_hash=run.scene_content_hash,
                present_entity_ids={'speaker-fl', 'rack-avr'},
                present_signal_path_edge_ids=set(),
            )
            self.assertEqual(result.status, 'current')


class SchemaMigrationTests(unittest.TestCase):
    def test_v116_table_declared_and_registry_complete(self):
        self.assertIn('cad_cable_run_geometries', NATIVE_SCHEMA_TABLES)
        self.assertEqual(NATIVE_SCHEMA_VERSION, 116)
        assert_row_integrity_registry_complete()

    def test_v115_to_v116_migration_preserves_runs(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                run,
                _geometry_repo,
                _signal,
            ) = _inspection_setup(tmp)
            self.addCleanup(scene_repo.close)
            db_path = scene_repo.path
            # Simulate a pre-v116 database: drop the geometry table and
            # roll the stamped version + migration ledger back to 115.
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    'DROP TABLE IF EXISTS cad_cable_run_geometries'
                )
                connection.execute(
                    'UPDATE native_schema_metadata SET schema_version = 115'
                )
                connection.execute(
                    'DELETE FROM native_schema_migrations '
                    'WHERE schema_version >= 116'
                )
            ensure_native_schema(db_path)
            with connect_sqlite(db_path) as connection:
                version = connection.execute(
                    'SELECT schema_version FROM native_schema_metadata'
                ).fetchone()[0]
                self.assertEqual(version, 116)
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn('cad_cable_run_geometries', tables)
            self.assertEqual(
                len(run_repo.list_runs(revision.document_id)), 1
            )


class CableRunPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, tmp_path: Path, seeded: bool = True):
        scene_repo, revision, run_repo = _repositories(tmp_path)
        self.addCleanup(scene_repo.close)
        run = None
        if seeded:
            run = _run(revision)
            run_repo.save_run(run)
        geometry_repo = CadCableRunGeometryRepository(
            scene_repo, run_repo
        )
        panel = CableRunPanel(
            scene_repo,
            revision.document_id,
            cable_run_repository=run_repo,
            geometry_repository=geometry_repo,
            signal_path_repository=CadSignalPathRepository(scene_repo),
        )
        return scene_repo, revision, run_repo, run, geometry_repo, panel

    def test_lists_runs_and_emits_overlay_item(self):
        from PySide6.QtCore import QCoreApplication

        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                panel,
            ) = self._panel(tmp)
            panel.refresh()
            self.assertEqual(panel.run_list.count(), 1)
            self.assertIn('FL speaker cable', panel.run_list.item(0).text())
            self.assertIn('最新', panel.run_list.item(0).text())
            emitted = []
            panel.routeSelectionChanged.connect(emitted.append)
            panel.run_list.setCurrentRow(0)
            QCoreApplication.processEvents()
            self.assertEqual(len(emitted), 1)
            item = emitted[0]
            self.assertEqual(item.route_state, 'unregistered')
            self.assertIsNotNone(item.from_endpoint.position)
            self.assertIsNotNone(item.to_endpoint.position)
            self.assertIn('経路形状未登録', panel.detail.text())
            panel.deleteLater()

    def test_detail_shows_declared_and_divergence(self):
        from PySide6.QtCore import QCoreApplication

        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                panel,
            ) = self._panel(tmp)
            geometry_repo.save_geometry(
                _geometry_for(
                    run,
                    segment_geometries=(
                        CableRunSegmentGeometry(
                            segment_sequence=0,
                            waypoints=_waypoints(
                                (5.5, 0.4, 0.9), (2.5, 0.4, 0.9)
                            ),
                            concealment_kind='conduit',
                            source='as_built_survey',
                            record_kind='as_built',
                        ),
                    ),
                )
            )
            panel.refresh()
            panel.run_list.setCurrentRow(0)
            QCoreApplication.processEvents()
            text = panel.detail.text()
            self.assertIn('1.5', text)
            self.assertIn('幾何長', text)
            self.assertIn('経路登録済', text)
            self.assertIn('施工済', text)
            self.assertIn('長さの差', text)
            panel.deleteLater()

    def test_narrow_width_and_uia_names(self):
        with tempfile.TemporaryDirectory(dir='C:/t', ignore_cleanup_errors=True) as _tmp:
            tmp = Path(_tmp)
            (
                _sr,
                _rev,
                _rr,
                _run,
                _gr,
                panel,
            ) = self._panel(tmp)
            panel.setFixedWidth(220)
            panel.refresh()
            panel.show()
            self.assertTrue(panel.run_list.accessibleName())
            self.assertTrue(panel.detail.accessibleName())
            self.assertTrue(panel.run_list.isEnabled())
            panel.hide()
            panel.deleteLater()


if __name__ == '__main__':
    unittest.main()
