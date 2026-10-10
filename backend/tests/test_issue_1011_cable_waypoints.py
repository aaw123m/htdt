"""Issue #1011 M3 — 3D waypoint authoring for cable-run geometry.

The recording flow: a dedicated edit mode arms room-surface picking on
the viewport, the in-progress point list shows as a non-persistent
``cable-route-draft-`` overlay, staged segments bind only to declared
``segment_sequence`` values, undo/redo rewrites nothing outside the
point list, and commit goes exclusively through
``CadCableRunGeometryRepository.save_geometry`` — the exact-pin
revalidation that makes a stale pin fail closed rather than clobber the
newer run.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from htdt.cad_cable_run import (
    CableRunEndpoint,
    CableRunSegment,
    build_cable_run,
)
from htdt.cad_cable_run_geometry import (
    evaluate_cable_run_geometry_freshness,
)
from htdt.cad_cable_run_repository import CadCableRunRepository
from htdt.cable_run_waypoint import CableRunWaypointController
from htdt.room_viewport import (
    CableRouteSegmentRouteItem,
    _ray_room_boundary_domain,
)

from test_issue_1011_cable_routes import (
    NOW,
    _geometry_for,
    _inspection_setup,
    _inspect,
    _scene,
)


class _FakeViewport:
    """Recording double for the M3 waypoint viewport contract."""

    def __init__(self) -> None:
        self.armed = False
        self.drafts: list[tuple] = []
        self.pick_calls: list = []
        self.pick_result = None

    def set_waypoint_pick_armed(self, armed: bool) -> None:
        self.armed = bool(armed)

    def render_cable_route_draft(self, points, staged=()) -> None:
        self.drafts.append((tuple(points), tuple(staged)))

    def pick_waypoint_domain(self, position):
        self.pick_calls.append(position)
        return self.pick_result


class _FakeWorkspace:
    """Minimal workspace surface the controller drives."""

    def __init__(self, document) -> None:
        self.status: list[tuple[str, bool]] = []
        self.refresh_calls = 0
        self.controller = SimpleNamespace(document=document)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status.append((text, error))

    def refresh(self, **_kwargs) -> None:
        self.refresh_calls += 1


def _controller_setup(tmp_path: Path):
    """Real repositories + inspection over the seeded #1011 scene."""

    (
        scene_repo,
        revision,
        run_repo,
        run,
        geometry_repo,
        signal_repo,
    ) = _inspection_setup(tmp_path)
    document = _scene()

    def provider(run_id: str):
        for inspection in _inspect(
            scene_repo, revision, run_repo, geometry_repo, signal_repo
        ):
            if inspection.run_id == run_id:
                return inspection
        return None

    workspace = _FakeWorkspace(document)
    viewport = _FakeViewport()
    controller = CableRunWaypointController(
        workspace,
        viewport,
        cable_run_repository=run_repo,
        geometry_repository=geometry_repo,
        inspection_provider=provider,
    )
    return (
        scene_repo,
        revision,
        run_repo,
        run,
        geometry_repo,
        workspace,
        viewport,
        controller,
    )


class WaypointControllerTests(unittest.TestCase):
    def test_begin_arms_viewport_and_binds_inspection(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _rev,
                _run_repo,
                run,
                _geo,
                _ws,
                viewport,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            self.assertTrue(controller.is_active)
            self.assertTrue(viewport.armed)
            self.assertEqual(controller.run_id, run.run_id)
            self.assertEqual(controller.segment_sequence, 0)
            # 既定の concealment は選択中区間の宣言 path_kind。
            self.assertEqual(controller.concealment_kind, 'conduit')
            self.assertEqual(
                [segment.sequence for segment in controller.declared_segments()],
                [0, 1, 2],
            )
            # begin() announces recording guidance, not an error.
            self.assertTrue(controller.workspace.status)
            self.assertFalse(controller.workspace.status[-1][1])

    def test_begin_fails_closed_when_run_absent(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                _rr,
                _run,
                _g,
                workspace,
                viewport,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertFalse(controller.begin('no-such-run'))
            self.assertFalse(controller.is_active)
            self.assertFalse(viewport.armed)
            self.assertTrue(workspace.status[-1][1])

    def test_record_stage_commit_persists_sealed_geometry(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                _workspace,
                viewport,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            committed = []
            controller.committed.connect(committed.append)
            self.assertTrue(controller.begin(run.run_id))

            self.assertTrue(controller.set_segment_sequence(1))
            controller.set_concealment_kind('under_floor')
            controller.set_source('as_built_survey')
            controller.set_record_kind('as_built')
            controller.set_note('床下の実測経路')
            for point in ((5.5, 0.4, 0.1), (3.0, 2.0, -0.2), (1.2, 0.8, 0.05)):
                self.assertTrue(controller.record_domain_point(point))
            self.assertTrue(controller.stage_segment())
            self.assertEqual(controller.points, ())
            self.assertEqual(len(controller.staged), 1)

            self.assertTrue(controller.commit())
            self.assertFalse(controller.is_active)
            self.assertFalse(viewport.armed)
            self.assertEqual(len(committed), 1)

            stored = geometry_repo.list_geometries(revision.document_id)
            self.assertEqual(len(stored), 1)
            geometry = stored[0]
            self.assertEqual(geometry.run_id, run.run_id)
            self.assertEqual(geometry.run_version, run.version)
            self.assertEqual(
                geometry.run_semantic_sha256, run.semantic_sha256
            )
            self.assertEqual(
                geometry.scene_revision_id, run.scene_revision_id
            )
            segment = geometry.segment_geometries[0]
            self.assertEqual(segment.segment_sequence, 1)
            self.assertEqual(segment.concealment_kind, 'under_floor')
            self.assertEqual(segment.source, 'as_built_survey')
            self.assertEqual(segment.record_kind, 'as_built')
            self.assertEqual(segment.note, '床下の実測経路')
            self.assertEqual(
                [
                    (w.x_m, w.y_m, w.z_m)
                    for w in segment.waypoints
                ],
                [(5.5, 0.4, 0.1), (3.0, 2.0, -0.2), (1.2, 0.8, 0.05)],
            )
            freshness = evaluate_cable_run_geometry_freshness(
                geometry,
                run=run,
                scene_content_hash=run.scene_content_hash,
                present_surface_ids=(),
            )
            self.assertEqual(freshness.status, 'current')
            # The committed session cleared the draft overlay.
            self.assertEqual(viewport.drafts[-1], ((), ()))
            # The committed geometry resolves to 'registered' on re-inspect.
            from htdt.cad_signal_path_repository import (
                CadSignalPathRepository,
            )

            rows = _inspect(
                scene_repo,
                revision,
                run_repo,
                geometry_repo,
                CadSignalPathRepository(scene_repo),
            )
            self.assertEqual(rows[0].route_state, 'registered')

    def test_commit_auto_stages_pending_points(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                revision,
                _rr,
                run,
                geometry_repo,
                _w,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            controller.set_segment_sequence(0)
            for point in ((0.5, 0.5, 0.0), (2.0, 0.5, 0.0)):
                controller.record_domain_point(point)
            self.assertTrue(controller.commit())
            stored = geometry_repo.list_geometries(revision.document_id)
            self.assertEqual(len(stored), 1)
            self.assertEqual(
                stored[0].segment_geometries[0].segment_sequence, 0
            )

    def test_commit_refuses_single_dangling_point(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                revision,
                _rr,
                run,
                geometry_repo,
                workspace,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            controller.record_domain_point((1.0, 1.0, 0.0))
            self.assertFalse(controller.commit())
            self.assertTrue(controller.is_active)
            self.assertEqual(len(controller.points), 1)
            self.assertTrue(workspace.status[-1][1])
            self.assertEqual(
                geometry_repo.list_geometries(revision.document_id), ()
            )

    def test_segment_binding_stays_declared(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                _rr,
                run,
                _g,
                _w,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            self.assertFalse(controller.set_segment_sequence(9))
            self.assertTrue(controller.set_segment_sequence(2))
            # The declared kind follows the target segment.
            self.assertEqual(controller.concealment_kind, 'in_wall')
            controller.record_domain_point((0, 0, 0))
            controller.record_domain_point((0, 1, 0))
            self.assertTrue(controller.stage_segment())
            self.assertEqual(
                controller.staged[0].segment_sequence, 2
            )

    def test_stage_replaces_prior_entry_for_same_sequence(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                _rr,
                run,
                _g,
                _w,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            controller.record_domain_point((0, 0, 0))
            controller.record_domain_point((1, 0, 0))
            self.assertTrue(controller.stage_segment())
            controller.record_domain_point((0, 0, 0))
            controller.record_domain_point((0, 0, 2))
            controller.record_domain_point((0, 1, 2))
            self.assertTrue(controller.stage_segment())
            self.assertEqual(len(controller.staged), 1)
            self.assertEqual(len(controller.staged[0].waypoints), 3)

    def test_undo_redo_rewrites_only_the_point_list(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                _rr,
                run,
                _g,
                workspace,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            document = workspace.controller.document
            from htdt.cad_scene import canonical_scene_json

            before_hash = canonical_scene_json(document)
            self.assertTrue(controller.begin(run.run_id))
            for point in ((0, 0, 0), (1, 0, 0), (2, 0, 0)):
                controller.record_domain_point(point)
            self.assertTrue(controller.undo())
            self.assertEqual(len(controller.points), 2)
            self.assertTrue(controller.can_redo)
            self.assertTrue(controller.redo())
            self.assertEqual(len(controller.points), 3)
            self.assertTrue(controller.undo())
            self.assertTrue(controller.undo())
            self.assertTrue(controller.undo())
            self.assertFalse(controller.can_undo)
            self.assertFalse(controller.undo())
            # A new record wipes the redo arm — no phantom branch.
            controller.record_domain_point((5, 5, 0))
            self.assertFalse(controller.can_redo)
            self.assertEqual(
                len(controller.points), 1
            )
            # Undo is honest: the document never changed.
            self.assertIs(workspace.controller.document, document)
            self.assertEqual(
                canonical_scene_json(document), before_hash
            )
            self.assertIsNone(controller.inspection.geometry)

    def test_surfaces_referenced_must_exist(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                _rr,
                run,
                _g,
                workspace,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            # The seeded scene declares no wall/authoring surfaces.
            self.assertEqual(controller.scene_surface_ids(), ())
            self.assertFalse(
                controller.set_traversed_surface_ids(('ghost-wall',))
            )
            self.assertEqual(controller.traversed_surface_ids, ())
            self.assertTrue(workspace.status[-1][1])
            self.assertTrue(controller.set_traversed_surface_ids(()))

    def test_commit_repins_to_latest_run_version(self):
        """Stale-pin guard: a mid-session run update never gets clobbered."""
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                _w,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            # A newer run version lands while the session is open.
            run_v2 = build_cable_run(
                document_id=run.document_id,
                scene_revision_id=run.scene_revision_id,
                scene_content_hash=run.scene_content_hash,
                label='FL speaker cable (revised)',
                kind=run.kind,
                from_endpoint=run.from_endpoint,
                to_endpoint=run.to_endpoint,
                segments=run.segments,
                service_loop_m=run.service_loop_m,
                created_at_utc=NOW,
                run_id=run.run_id,
                version='2',
            )
            run_repo.save_run(run_v2)
            controller.record_domain_point((0, 0, 0))
            controller.record_domain_point((2, 0, 0))
            self.assertTrue(controller.stage_segment())
            self.assertTrue(controller.commit())
            stored = geometry_repo.list_geometries(revision.document_id)
            self.assertEqual(len(stored), 1)
            geometry = stored[0]
            # The commit pinned the CURRENT run — never the stale v1.
            self.assertEqual(geometry.run_version, '2')
            self.assertEqual(
                geometry.run_semantic_sha256, run_v2.semantic_sha256
            )

    def test_save_rejects_geometry_pinned_to_forged_run(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                run_repo,
                run,
                geometry_repo,
                _w,
                _v,
                _c,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            forged = _geometry_for(run).model_copy(
                update={'run_semantic_sha256': 'f' * 64}
            )
            with self.assertRaises(Exception):
                geometry_repo.save_geometry(forged)

    def test_recommit_appends_new_geometry_version(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                _w,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            for point in ((0, 0, 0), (1, 0, 0)):
                controller.record_domain_point(point)
            self.assertTrue(controller.stage_segment())
            self.assertTrue(controller.commit())

            self.assertTrue(controller.begin(run.run_id))
            for point in ((0, 0, 0), (1, 0, 0), (1, 1, 0)):
                controller.record_domain_point(point)
            self.assertTrue(controller.stage_segment())
            self.assertTrue(controller.commit())

            stored = geometry_repo.list_geometries(revision.document_id)
            self.assertEqual(len(stored), 2)
            self.assertEqual(
                {record.version for record in stored}, {'1', '2'}
            )
            self.assertEqual(
                len({record.geometry_id for record in stored}), 1
            )
            latest = geometry_repo.latest_geometry_for_run(
                run.run_id, run.version
            )
            self.assertEqual(latest.version, '2')
            self.assertEqual(
                len(latest.segment_geometries[0].waypoints), 3
            )
            # The earlier version was appended, never clobbered.
            first = geometry_repo.get_geometry(
                stored[0].geometry_id, '1'
            )
            self.assertEqual(
                len(first.segment_geometries[0].waypoints), 2
            )

    def test_commit_merges_prior_committed_segments(self):
        """A later session's snapshot keeps earlier committed segments —
        recording segment N+1 must not unregister segments 0..N."""
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                revision,
                run_repo,
                run,
                geometry_repo,
                _w,
                _v,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            controller.set_segment_sequence(0)
            for point in ((0, 0, 0), (1, 0, 0)):
                controller.record_domain_point(point)
            self.assertTrue(controller.stage_segment())
            self.assertTrue(controller.commit())

            self.assertTrue(controller.begin(run.run_id))
            self.assertTrue(controller.set_segment_sequence(1))
            for point in ((2, 0, 0), (3, 0, 0)):
                controller.record_domain_point(point)
            self.assertTrue(controller.stage_segment())
            self.assertTrue(controller.commit())

            latest = geometry_repo.latest_geometry_for_run(
                run.run_id, run.version
            )
            self.assertEqual(latest.version, '2')
            sequences = {
                segment.segment_sequence
                for segment in latest.segment_geometries
            }
            self.assertEqual(sequences, {0, 1})
            seg0 = next(
                segment
                for segment in latest.segment_geometries
                if segment.segment_sequence == 0
            )
            self.assertEqual(
                [(w.x_m, w.y_m, w.z_m) for w in seg0.waypoints],
                [(0, 0, 0), (1, 0, 0)],
            )

    def test_refresh_run_missing_cancels_session(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                _rr,
                run,
                _g,
                workspace,
                viewport,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            controller._inspection_provider = lambda _run_id: None
            controller.refresh_run()
            self.assertFalse(controller.is_active)
            self.assertFalse(viewport.armed)
            self.assertTrue(workspace.status[-1][1])

    def test_record_at_resolves_room_point(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                _rr,
                run,
                _g,
                workspace,
                viewport,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            self.assertTrue(controller.begin(run.run_id))
            viewport.pick_result = (2.0, 1.0, 0.0)
            self.assertTrue(controller.record_at(SimpleNamespace(x=1, y=2)))
            self.assertEqual(controller.points, ((2.0, 1.0, 0.0),))
            # An unresolvable ray records nothing and complains honestly.
            viewport.pick_result = None
            controller.record_at(SimpleNamespace(x=3, y=4))
            self.assertEqual(len(controller.points), 1)
            self.assertTrue(workspace.status[-1][1])

    def test_draft_overlay_is_distinct_and_transient(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            (
                scene_repo,
                _r,
                _rr,
                run,
                _g,
                _w,
                viewport,
                controller,
            ) = _controller_setup(Path(_tmp))
            self.addCleanup(scene_repo.close)
            controller.begin(run.run_id)
            controller.record_domain_point((0, 0, 0))
            controller.record_domain_point((1, 0, 0))
            points, staged = viewport.drafts[-1]
            self.assertEqual(points, ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)))
            self.assertEqual(staged, ())
            controller.stage_segment()
            points, staged = viewport.drafts[-1]
            self.assertEqual(points, ())
            self.assertEqual(len(staged), 1)
            self.assertEqual(staged[0].segment_sequence, 0)
            self.assertEqual(len(staged[0].points), 2)


class RayBoundaryTests(unittest.TestCase):
    """``_ray_room_boundary_domain`` — the empty-space fallback pick."""

    def test_downward_ray_hits_floor(self):
        document = _scene()
        # A ray from inside the room pointing down meets the floor.
        hit = _ray_room_boundary_domain(
            (3.0, 2.0, 2.0), (3.0, 2.0, -1.0), document
        )
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit[2], 0.0)
        self.assertAlmostEqual(hit[0], 3.0)
        self.assertAlmostEqual(hit[1], 2.0)
        # From above the envelope, the ceiling is the honest first hit.
        above = _ray_room_boundary_domain(
            (3.0, 2.0, 5.0), (3.0, 2.0, -1.0), document
        )
        self.assertIsNotNone(above)
        self.assertAlmostEqual(above[2], 2.4)

    def test_sideways_ray_hits_footprint_wall(self):
        document = _scene()
        # Ray parallel to the floor toward +x wall (width 6.0 → x=6.0).
        hit = _ray_room_boundary_domain(
            (3.0, 2.0, 1.0), (10.0, 2.0, 1.0), document
        )
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit[0], 6.0)
        self.assertAlmostEqual(hit[2], 1.0)

    def test_upward_ray_hits_ceiling_plane(self):
        document = _scene()
        hit = _ray_room_boundary_domain(
            (3.0, 2.0, 0.5), (3.0, 2.0, 9.0), document
        )
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit[2], 2.4)

    def test_miss_returns_none(self):
        document = _scene()
        # Ray pointing away from the room entirely.
        self.assertIsNone(
            _ray_room_boundary_domain(
                (30.0, 30.0, 5.0), (40.0, 40.0, 9.0), document
            )
        )
        self.assertIsNone(
            _ray_room_boundary_domain((0, 0, 0), (1, 1, 1), None)
        )


class WaypointDraftOverlayTests(unittest.TestCase):
    """``render_cable_route_draft`` — non-persistent preview contract."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _viewport(self):
        from htdt.room_viewport import RoomViewport3D

        viewport = RoomViewport3D()
        viewport.plotter = _RecordingPlotter()
        return viewport

    def test_draft_actors_use_dedicated_prefix_and_stay_unpickable(self):
        viewport = self._viewport()
        points = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0))
        staged = (
            CableRouteSegmentRouteItem(
                segment_sequence=1,
                points=((5.0, 0.4, 0.1), (3.0, 2.0, 0.1)),
            ),
        )
        viewport.render_cable_route_draft(points, staged)
        names = [
            kwargs['name']
            for _args, kwargs in viewport.plotter.meshes
        ]
        self.assertTrue(all(name.startswith('cable-route-draft-') for name in names))
        # 3 point spheres + 2 legs + 1 staged leg.
        self.assertEqual(len(names), 6)
        for _args, kwargs in viewport.plotter.meshes:
            self.assertFalse(kwargs['pickable'])
            self.assertFalse(kwargs['render'])
        labels = [k['name'] for _a, k in viewport.plotter.label_calls]
        self.assertTrue(
            all(name.startswith('cable-route-draft-') for name in labels)
        )
        self.assertEqual(len(labels), 3)
        self.assertGreaterEqual(viewport.plotter.render_calls, 1)

    def test_empty_draft_clears_without_actor(self):
        viewport = self._viewport()
        viewport.render_cable_route_draft((), ())
        self.assertEqual(viewport.plotter.meshes, [])
        self.assertEqual(viewport.plotter.label_calls, [])
        self.assertEqual(viewport.plotter.render_calls, 1)

    def test_rendered_polyline_uses_domain_to_render_axis_flip(self):
        """Draft points land at (x, -y, z) — the render-space contract."""
        viewport = self._viewport()
        viewport.render_cable_route_draft(((0.5, 2.0, 1.0),), ())
        sphere = viewport.plotter.meshes[0][0][0]
        bounds = sphere.bounds
        center = (
            (bounds[0] + bounds[1]) / 2,
            (bounds[2] + bounds[3]) / 2,
            (bounds[4] + bounds[5]) / 2,
        )
        self.assertAlmostEqual(center[0], 0.5, places=5)
        self.assertAlmostEqual(center[1], -2.0, places=5)
        self.assertAlmostEqual(center[2], 1.0, places=5)

    def test_release_probe_pick_does_not_double_record(self):
        """One armed click records once — the release-time probe pick is
        read-only and must not dispatch a second ``waypointPicked``."""
        from PySide6.QtCore import QPointF

        viewport = self._viewport()
        plotter = viewport.plotter
        plotter.iren = SimpleNamespace(
            picker=SimpleNamespace(
                GetPickPosition=lambda: (3.0, -2.0, 0.0),
                Pick=lambda *_a: 1,
                GetActor=lambda: object(),
            ),
            get_poked_renderer=lambda: None,
        )
        viewport._waypoint_pick_armed = True
        received = []
        viewport.waypointPicked.connect(received.append)

        actor = object()
        # The press pick dispatches once and records the surface point.
        viewport._picked_actor(actor)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], (3.0, 2.0, 0.0))

        # The release path's pick_actor_at probe re-picks to READ the
        # result; its dispatch must not record the same point again.
        flags = []

        def pick_spy(*_args):
            flags.append(viewport._in_pick_probe)
            viewport._picked_actor(actor)  # the probe's dispatch
            return 1

        plotter.iren.picker.Pick = pick_spy
        viewport.pick_actor_at(QPointF(10, 10))
        self.assertEqual(flags, [True])
        self.assertEqual(len(received), 1)


class _RecordingPlotter:
    """Captures add_mesh/add_point_labels/render — no GL needed."""

    def __init__(self) -> None:
        self.meshes: list[tuple] = []
        self.label_calls: list[tuple] = []
        self.render_calls = 0

    def add_mesh(self, *args, **kwargs):
        self.meshes.append((args, kwargs))
        return None

    def add_point_labels(self, *args, **kwargs):
        self.label_calls.append((args, kwargs))
        return None

    def render(self) -> None:
        self.render_calls += 1

    def remove_actor(self, *_args, **_kwargs) -> None:
        pass


class WaypointWorkspaceTests(unittest.TestCase):
    """Workspace routing: picks, Esc chain, panel mount, refresh."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _workspace(self, tmp_path: Path):
        from test_room_cadux import FakeRoomViewport, _f1_repository
        from htdt.room_workspace import RoomWorkspace
        from htdt.cad_scene import F1_DOCUMENT_ID

        repository = _f1_repository(tmp_path)
        workspace = RoomWorkspace(
            repository,
            F1_DOCUMENT_ID,
            viewport_factory=lambda parent: _WaypointViewport(parent),
        )
        return workspace, repository

    def _seed_run(self, repository):
        from htdt.cad_scene import F1_DOCUMENT_ID

        revision = repository.latest(F1_DOCUMENT_ID)
        run_repo = CadCableRunRepository(repository)
        run = build_cable_run(
            document_id=F1_DOCUMENT_ID,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            label='FL feed',
            kind='speaker_signal',
            from_endpoint=CableRunEndpoint(
                label='C out', entity_id='speaker-c'
            ),
            to_endpoint=CableRunEndpoint(
                label='FL in', entity_id='speaker-fl'
            ),
            segments=(
                CableRunSegment(
                    sequence=0, path_kind='conduit', length_m=2.0
                ),
            ),
            service_loop_m=0.0,
            created_at_utc=NOW,
            run_id='run-ws',
            version='1',
        )
        run_repo.save_run(run)
        return run

    def test_workspace_routes_picks_and_esc_cancels(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            workspace, repository = self._workspace(Path(_tmp))
            self.addCleanup(repository.close)
            run = self._seed_run(repository)
            workspace.cable_run_panel.refresh()
            controller = workspace.cable_waypoint_controller
            self.assertFalse(controller.is_active)
            self.assertTrue(
                workspace.cable_waypoint_panel.isHidden()
            )
            # Panel entry point: the listing's record button asks the
            # workspace to arm the session for the selected run.
            workspace._begin_cable_waypoint_recording(run.run_id)
            self.assertTrue(controller.is_active)
            self.assertTrue(workspace.viewport.armed)
            self.assertFalse(
                workspace.cable_waypoint_panel.isHidden()
            )
            # Viewport pick → domain point lands on the session list.
            workspace.viewport.waypointPicked.emit((1.0, 1.0, 0.0))
            self.assertEqual(
                controller.points, ((1.0, 1.0, 0.0),)
            )
            # Empty click resolves through the boundary fallback.
            from PySide6.QtCore import QPointF

            workspace.viewport.waypoint_pick = (2.0, 0.5, 0.0)
            workspace._empty_clicked(QPointF(4, 4))
            self.assertEqual(len(controller.points), 2)
            # Esc chain: recording cancels before selection clears.
            self.assertTrue(workspace.cancel_active_operation())
            self.assertFalse(controller.is_active)
            self.assertFalse(workspace.viewport.armed)

    def test_panel_mounts_under_cable_run_panel_with_a11y(self):
        with tempfile.TemporaryDirectory(
            dir='C:/t', ignore_cleanup_errors=True
        ) as _tmp:
            workspace, repository = self._workspace(Path(_tmp))
            self.addCleanup(repository.close)
            panel = workspace.cable_waypoint_panel
            self.assertIsNotNone(panel)
            for widget_name in (
                'segment_combo',
                'concealment_combo',
                'record_kind_combo',
                'source_combo',
                'note_edit',
                'surface_list',
                'undo_button',
                'redo_button',
                'clear_button',
                'stage_button',
                'unstage_button',
                'staged_list',
                'commit_button',
                'cancel_button',
            ):
                widget = getattr(panel, widget_name)
                self.assertTrue(
                    widget.accessibleName(),
                    f'{widget_name} lacks accessibleName',
                )
            # Record button lives on the listing and emits run_id.
            self.assertTrue(
                workspace.cable_run_panel.record_button.accessibleName()
            )


class _WaypointViewport(__import__('PySide6.QtWidgets', fromlist=['QFrame']).QFrame):
    """FakeRoomViewport shape + the M3 waypoint surface."""

    from PySide6.QtCore import Signal

    waypointPicked = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.interactor = self
        from test_room_cadux import _FakePlotter

        self.plotter = _FakePlotter()
        self.render_calls: list = []
        self.armed = False
        self.drafts: list = []
        self.waypoint_pick = (1.0, 0.0, 0.5)

    def render_document(self, document, **kwargs) -> None:
        self.render_calls.append(kwargs)

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, entity_id: str) -> None:
        pass

    def focus_entities(self, entity_ids) -> None:
        pass

    def set_waypoint_pick_armed(self, armed: bool) -> None:
        self.armed = bool(armed)

    def render_cable_route_draft(self, points, staged=()) -> None:
        self.drafts.append((tuple(points), tuple(staged)))

    def pick_waypoint_domain(self, position):
        return self.waypoint_pick


if __name__ == '__main__':
    unittest.main()
