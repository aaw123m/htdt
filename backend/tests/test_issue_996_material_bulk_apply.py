"""#996: 「全境界面へ適用」は単一の密封トランザクションとして確定する.

Before this change the apply loop committed one INSERT per surface, so a
mid-loop failure left a PARTIAL assignment while the UI still read
"applied". The bulk authority commits every target inside one
``BEGIN IMMEDIATE`` transaction — all or none — after a read-only preview
that pins the material hash and the document head (drift at commit time
rolls the batch back). The read side reports mixed / partial / unresolved
state honestly, and one Undo step restores the exact pre-apply rows.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from htdt.acoustic_benchmark import GeometricAcousticBand
from htdt.cad_acoustic_material import (
    CadAcousticMaterialRepository,
    MaterialBulkApplyError,
    build_acoustic_material,
)
from htdt.cad_document import CommandPresentation, CompositeEditCommand
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    SceneDocument,
    SceneEntity,
)
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.room_workspace import RoomWorkspaceController
from htdt.semantic_geometry import (
    SemanticSurface,
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)

NOW = '2026-01-01T00:00:00Z'


def _obj_with_triangles(count: int) -> bytes:
    """OBJ fixture: ``count`` disjoint triangles, one per surface."""
    lines: list[str] = []
    for index in range(count):
        base = index * 3
        lines += [
            f'v {index}.0 0 0',
            f'v {index}.0 1 0',
            f'v {index}.0 0 1',
            f'f {base + 1}// {base + 2}// {base + 3}//',
        ]
    return ('\n'.join(lines) + '\n').encode()


def _material(
    label: str = '石膏ボード',
    material_id: str | None = None,
):
    return build_acoustic_material(
        label=label,
        provenance='fixture',
        wave_model='rigid',
        geometric_model='banded',
        geometric_bands=(
            GeometricAcousticBand(
                center_hz=500.0, absorption=0.1, scattering=0.05
            ),
        ),
        created_at_utc=NOW,
        material_id=material_id,
    )


def _surface(
    index: int,
    *,
    semantic_class: str = 'room_boundary',
) -> SemanticSurface:
    return SemanticSurface(
        surface_id=f'semantic-surface:{index:064x}',
        surface_key=f'wall-{index}',
        semantic_class=semantic_class,
        triangle_ids=(f't{index}',),
        assignment_provenance='explicit',
    )


def _surfaces(count: int) -> tuple[SemanticSurface, ...]:
    return tuple(_surface(i) for i in range(1, count + 1))


def _materials(tmp_path: Path) -> CadAcousticMaterialRepository:
    return CadAcousticMaterialRepository(tmp_path / 'materials.sqlite3')


def _rows(repository: CadAcousticMaterialRepository) -> dict[str, str]:
    with closing(sqlite3.connect(repository.path)) as connection:
        fetched = connection.execute(
            'SELECT source_surface_id, material_id'
            ' FROM cad_surface_material_assignments'
        ).fetchall()
    return {row[0]: row[1] for row in fetched}


def _ids(surfaces: tuple[SemanticSurface, ...]) -> list[str]:
    return [surface.surface_id for surface in surfaces]


class _FailAfterNStatements:
    """Connection proxy that raises on the Nth ``execute`` — the injected
    mid-transaction failure from the #996 DoD (disk-full / locked-DB
    equivalent)."""

    def __init__(self, inner: sqlite3.Connection, fail_on: int) -> None:
        self._inner = inner
        self._fail_on = fail_on
        self.executed = 0

    def execute(self, sql, parameters=()):
        self.executed += 1
        if self.executed == self._fail_on:
            raise sqlite3.OperationalError(
                f'injected failure at statement {self.executed}'
            )
        return self._inner.execute(sql, parameters)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._inner.__exit__(exc_type, exc, tb)

    def close(self) -> None:
        self._inner.close()


def _fail_after(
    repository: CadAcousticMaterialRepository,
    monkeypatch: pytest.MonkeyPatch,
    fail_on: int,
) -> None:
    real_connect = repository._connect

    def _connect() -> _FailAfterNStatements:
        return _FailAfterNStatements(real_connect(), fail_on)

    monkeypatch.setattr(repository, '_connect', _connect)


def _geometry_for(surfaces: tuple[SemanticSurface, ...]):
    """One semantic surface per fixture surface, one disjoint triangle
    each (conversion rejects overlapping assignments)."""
    mesh = import_raw_visual_mesh(
        _obj_with_triangles(len(surfaces)),
        source_name='issue-996-fixture.obj',
    )
    triangles = raw_triangle_ids(mesh)
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=None,
        source_to_scene_transform=(
            explicit_identity_source_to_scene_transform(
                reason='fixture OBJ coordinates are explicit HTDT metres'
            )
        ),
        surface_assignments=tuple(
            SurfaceSemanticAssignment(
                surface_key=surface.surface_key,
                triangle_ids=(triangles[index],),
                semantic_class=surface.semantic_class,
            )
            for index, surface in enumerate(surfaces)
        ),
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)


def _scene_controller(
    tmp_path: Path, surfaces: tuple[SemanticSurface, ...]
) -> tuple[SceneRepository, RoomWorkspaceController]:
    """Persist a document whose semantic geometry has one boundary
    surface per fixture surface (one disjoint triangle each)."""
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    document = SceneDocument(
        document_id=F1_DOCUMENT_ID,
        schema_version=4,
        room=None,
        entities=(),
        r120_semantic_geometry=_geometry_for(surfaces),
    )
    repository.save(document, parent_revision_id=None)
    return repository, RoomWorkspaceController(repository, F1_DOCUMENT_ID)


def _document_surfaces(controller) -> tuple[SemanticSurface, ...]:
    head = controller.repository.current_head(F1_DOCUMENT_ID)
    geometry = head.document.r120_semantic_geometry
    return geometry.surfaces


# ---------------------------------------------------------------------------
# Bulk apply authority — atomicity / ordering / drift
# ---------------------------------------------------------------------------


def test_bulk_apply_commits_every_boundary_in_one_revision(
    tmp_path: Path,
) -> None:
    repository = _materials(tmp_path)
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces(3)

    result = repository.assign_material_bulk(
        'doc-1', _ids(surfaces), material, surfaces=surfaces
    )

    assert result.applied_surface_ids == tuple(_ids(surfaces))
    assert result.kept_surface_ids == ()
    rows = _rows(repository)
    assert rows == {
        surface.surface_id: material.material_id for surface in surfaces
    }

    # Re-applying the same material keeps the rows — nothing rewritten.
    again = repository.assign_material_bulk(
        'doc-1', _ids(surfaces), material, surfaces=surfaces
    )
    assert again.applied_surface_ids == ()
    assert again.kept_surface_ids == tuple(_ids(surfaces))


@pytest.mark.parametrize('count', (2, 100, 1000))
def test_bulk_apply_rolls_back_everything_under_mid_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    repository = _materials(tmp_path)
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces(count)
    # Statements: BEGIN, material check, head pin, row read, then upserts —
    # fail halfway through the upsert sweep.
    _fail_after(repository, monkeypatch, fail_on=4 + max(1, count // 2))

    with pytest.raises(sqlite3.OperationalError, match='injected failure'):
        repository.assign_material_bulk(
            'doc-1', _ids(surfaces), material, surfaces=surfaces
        )

    # Never "some applied": the whole batch rolled back.
    assert _rows(repository) == {}


def test_bulk_apply_failure_on_first_write_also_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _materials(tmp_path)
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces(4)
    _fail_after(repository, monkeypatch, fail_on=5)

    with pytest.raises(sqlite3.OperationalError):
        repository.assign_material_bulk(
            'doc-1', _ids(surfaces), material, surfaces=surfaces
        )
    assert _rows(repository) == {}


def test_bulk_apply_deterministic_sorted_ordering(
    tmp_path: Path,
) -> None:
    repository = _materials(tmp_path)
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces(5)
    shuffled = tuple(reversed(_ids(surfaces))) + (
        surfaces[0].surface_id,  # duplicate input must dedupe
    )

    preview = repository.preview_assign_material_bulk(
        'doc-1', shuffled, material, surfaces=surfaces
    )
    assert tuple(entry.surface_id for entry in preview.entries) == tuple(
        sorted(_ids(surfaces))
    )
    assert preview.target_count == 5
    assert preview.unassigned_count == 5

    result = repository.assign_material_bulk(
        'doc-1', shuffled, material, surfaces=surfaces
    )
    assert result.applied_surface_ids == tuple(sorted(_ids(surfaces)))


def test_bulk_apply_refuses_unpersisted_material(tmp_path: Path) -> None:
    repository = _materials(tmp_path)
    surfaces = _surfaces(3)

    with pytest.raises(MaterialBulkApplyError) as caught:
        repository.assign_material_bulk(
            'doc-1', _ids(surfaces), _material(), surfaces=surfaces
        )
    assert caught.value.reason == 'material_not_persisted'
    assert _rows(repository) == {}


def test_bulk_apply_refuses_material_hash_drift(tmp_path: Path) -> None:
    repository = _materials(tmp_path)
    persisted = _material(label='persisted')
    repository.save_material(persisted)
    # Same identity carrying a different payload hash — a re-authored
    # authority is never bound silently.
    drifted = _material(
        label='re-authored', material_id=persisted.material_id
    )
    assert drifted.semantic_sha256 != persisted.semantic_sha256

    surfaces = _surfaces(2)
    with pytest.raises(MaterialBulkApplyError) as caught:
        repository.assign_material_bulk(
            'doc-1', _ids(surfaces), drifted, surfaces=surfaces
        )
    assert caught.value.reason == 'material_sha_drift'
    assert _rows(repository) == {}


def test_bulk_apply_refuses_surfaces_outside_geometry(
    tmp_path: Path,
) -> None:
    repository = _materials(tmp_path)
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces(2)
    foreign = _surface(99)

    with pytest.raises(MaterialBulkApplyError) as caught:
        repository.assign_material_bulk(
            'doc-1',
            _ids(surfaces) + [foreign.surface_id],
            material,
            surfaces=surfaces,
        )
    assert caught.value.reason == 'surface_missing'
    assert _rows(repository) == {}


def test_bulk_apply_scene_head_drift_rolls_back(tmp_path: Path) -> None:
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CadAcousticMaterialRepository(scenes.path)
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces(3)
    document = SceneDocument(
        document_id='doc-drift',
        schema_version=4,
        room=None,
        entities=(),
        r120_semantic_geometry=_geometry_for(surfaces),
    )
    first = scenes.save(document, parent_revision_id=None).revision
    head = repository.preview_assign_material_bulk(
        'doc-drift', _ids(surfaces), material, surfaces=surfaces
    ).scene_revision_id
    assert head == first.revision_id

    # A concurrent save advances the head before the bulk apply commits.
    changed = document.model_copy(
        update={
            'entities': (
                SceneEntity(
                    entity_id='point-mlp',
                    kind='measurement_point',
                    name='MLP',
                    position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                ),
            ),
        }
    )
    second = scenes.save(
        changed, parent_revision_id=first.revision_id
    ).revision
    assert second.revision_id != first.revision_id
    with pytest.raises(MaterialBulkApplyError) as caught:
        repository.assign_material_bulk(
            'doc-drift',
            _ids(surfaces),
            material,
            surfaces=surfaces,
            expected_scene_revision_id=head,
        )
    assert caught.value.reason == 'scene_revision_drift'
    assert _rows(repository) == {}

    # With the current pin the same apply commits against the new head.
    result = repository.assign_material_bulk(
        'doc-drift',
        _ids(surfaces),
        material,
        surfaces=surfaces,
        expected_scene_revision_id=second.revision_id,
    )
    assert result.scene_revision_id == second.revision_id
    assert len(result.applied_surface_ids) == 3


def test_bulk_apply_only_unassigned_keeps_existing_rows(
    tmp_path: Path,
) -> None:
    repository = _materials(tmp_path)
    first_material = _material(label='木質パネル')
    second_material = _material(label='吸音ボード')
    repository.save_material(first_material)
    repository.save_material(second_material)
    surfaces = _surfaces(3)
    repository.assign_material(
        'doc-1', surfaces[0].surface_id, first_material
    )

    result = repository.assign_material_bulk(
        'doc-1',
        _ids(surfaces),
        second_material,
        surfaces=surfaces,
        only_unassigned=True,
    )

    rows = _rows(repository)
    assert rows[surfaces[0].surface_id] == first_material.material_id
    assert rows[surfaces[1].surface_id] == second_material.material_id
    assert rows[surfaces[2].surface_id] == second_material.material_id
    assert result.skipped_surface_ids == (surfaces[0].surface_id,)
    assert set(result.applied_surface_ids) == {
        surfaces[1].surface_id,
        surfaces[2].surface_id,
    }


def test_bulk_preview_reports_overwrite_and_unassigned_counts(
    tmp_path: Path,
) -> None:
    repository = _materials(tmp_path)
    kept = _material(label='kept')
    replaced = _material(label='replaced')
    repository.save_material(kept)
    repository.save_material(replaced)
    surfaces = _surfaces(3)
    repository.assign_material(
        'doc-1', surfaces[0].surface_id, kept
    )
    repository.assign_material(
        'doc-1', surfaces[1].surface_id, replaced
    )

    preview = repository.preview_assign_material_bulk(
        'doc-1', _ids(surfaces), kept, surfaces=surfaces
    )
    assert preview.target_count == 3
    assert preview.already_assigned_count == 1
    assert preview.overwrite_count == 1
    assert preview.unassigned_count == 1
    by_surface = {e.surface_id: e for e in preview.entries}
    assert by_surface[surfaces[0].surface_id].same_material
    assert not by_surface[surfaces[1].surface_id].same_material
    assert by_surface[surfaces[2].surface_id].row_material_id is None


# ---------------------------------------------------------------------------
# Honest read side — mixed / partial / unresolved
# ---------------------------------------------------------------------------


def test_boundary_state_reports_uniform_partial_and_mixed(
    tmp_path: Path,
) -> None:
    repository = _materials(tmp_path)
    material_a = _material(label='A')
    material_b = _material(label='B')
    repository.save_material(material_a)
    repository.save_material(material_b)
    surfaces = _surfaces(3)

    state = repository.boundary_material_state('doc-1', surfaces)
    assert state.summary == 'unassigned'
    assert all(
        entry.status == 'unassigned' for entry in state.entries
    )

    repository.assign_material_bulk(
        'doc-1', _ids(surfaces), material_a, surfaces=surfaces
    )
    state = repository.boundary_material_state('doc-1', surfaces)
    assert state.summary == 'uniform'
    assert state.assigned_material_ids == (material_a.material_id,)

    # A legacy partial apply: one boundary switched to a different
    # material plus one boundary left unassigned.
    repository.assign_material(
        'doc-1', surfaces[1].surface_id, material_b
    )
    repository.clear_assignment('doc-1', surfaces[2].surface_id)
    state = repository.boundary_material_state('doc-1', surfaces)
    assert state.summary == 'partial'

    repository.assign_material(
        'doc-1', surfaces[2].surface_id, material_b
    )
    state = repository.boundary_material_state('doc-1', surfaces)
    # Mixed is reported as mixed — never coerced to "assigned".
    assert state.summary == 'mixed'
    assert set(state.assigned_material_ids) == {
        material_a.material_id,
        material_b.material_id,
    }


def test_boundary_state_reports_dangling_reference_not_unassigned(
    tmp_path: Path,
) -> None:
    repository = _materials(tmp_path)
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces(2)
    repository.assign_material_bulk(
        'doc-1', _ids(surfaces), material, surfaces=surfaces
    )

    # Operator-side corruption: the material record vanishes while the
    # assignment rows remain.
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute(
            'DELETE FROM cad_acoustic_materials WHERE material_id=?',
            (material.material_id,),
        )
        connection.commit()

    state = repository.boundary_material_state('doc-1', surfaces)
    assert state.summary == 'unresolved'
    assert set(state.unresolved_surface_ids) == set(_ids(surfaces))
    assert all(
        entry.status == 'unresolved_reference' for entry in state.entries
    )
    assert all(
        entry.material_id == material.material_id
        for entry in state.entries
    )


def test_boundary_state_flags_rows_for_surfaces_outside_geometry(
    tmp_path: Path,
) -> None:
    repository = _materials(tmp_path)
    material = _material()
    repository.save_material(material)
    surfaces = _surfaces(2)
    stale = _surface(77)
    repository.assign_material_bulk(
        'doc-1',
        _ids(surfaces) + [stale.surface_id],
        material,
    )

    state = repository.boundary_material_state('doc-1', surfaces)
    assert state.stale_surface_ids == (stale.surface_id,)
    assert state.summary == 'unresolved'


def test_boundary_state_no_boundaries(tmp_path: Path) -> None:
    repository = _materials(tmp_path)
    object_only = (_surface(1, semantic_class='object_surface'),)
    state = repository.boundary_material_state('doc-1', object_only)
    assert state.summary == 'no_boundaries'
    assert state.boundary_surface_ids == ()


# ---------------------------------------------------------------------------
# Undo — one step restores the exact prior rows
# ---------------------------------------------------------------------------


def test_bulk_apply_undo_restores_prior_rows_in_one_step(
    tmp_path: Path,
) -> None:
    repository, controller = _scene_controller(tmp_path, _surfaces(3))
    materials = controller.material_repository
    material_a = _material(label='A')
    material_b = _material(label='B')
    materials.save_material(material_a)
    materials.save_material(material_b)
    surfaces = _document_surfaces(controller)
    # Pre-existing mixed state on one boundary.
    materials.assign_material(
        F1_DOCUMENT_ID, surfaces[0].surface_id, material_b
    )
    prior_rows = materials.assignment_rows_for(
        F1_DOCUMENT_ID, _ids(surfaces)
    )

    committed: dict[str, object] = {}
    head = materials.preview_assign_material_bulk(
        F1_DOCUMENT_ID, _ids(surfaces), material_a, surfaces=surfaces
    ).scene_revision_id

    def _apply() -> None:
        committed['result'] = materials.assign_material_bulk(
            F1_DOCUMENT_ID,
            _ids(surfaces),
            material_a,
            surfaces=surfaces,
            expected_scene_revision_id=head,
        )

    def _revert() -> None:
        materials.restore_assignment_rows(F1_DOCUMENT_ID, prior_rows)

    command = CompositeEditCommand(
        inner=None,
        apply_side=_apply,
        revert_side=_revert,
        presentation=CommandPresentation(
            action='assign_material_bulk',
            label='全境界面へ「A」を適用',
        ),
    )
    assert controller.working.push_command(command)
    rows = _rows(materials)
    assert rows == {
        surface.surface_id: material_a.material_id for surface in surfaces
    }

    # ONE undo undoes the whole bulk apply, restoring the mixed state
    # byte-for-byte — including the surviving pre-apply row.
    controller.undo()
    rows = _rows(materials)
    assert rows[surfaces[0].surface_id] == material_b.material_id
    assert surfaces[1].surface_id not in rows
    assert surfaces[2].surface_id not in rows

    # Redo re-applies the whole batch as one step.
    controller.redo()
    rows = _rows(materials)
    assert rows == {
        surface.surface_id: material_a.material_id for surface in surfaces
    }


def test_bulk_apply_emits_one_material_boundary_change_event() -> None:
    from htdt.cad_acoustic_material import material_assignment_changes

    before = (
        ('semantic-surface:' + '1' * 64, 'mat-a', 'h-a'),
        ('semantic-surface:' + '2' * 64, None, None),
    )
    after = (
        ('semantic-surface:' + '1' * 64, 'mat-b', 'h-b'),
        ('semantic-surface:' + '2' * 64, 'mat-b', 'h-b'),
    )
    events = material_assignment_changes(before, after)
    # One bulk apply -> ONE event on the material_boundary axis (#964
    # revalidation channel), never per-surface noise.
    assert len(events) == 1
    event = events[0]
    assert event.kind == 'authority_reference'
    assert event.axes == frozenset({'material_boundary'})
    assert '2 surface(s)' in event.detail

    # Identical rows -> no change event at all.
    assert material_assignment_changes(before, before) == ()


# ---------------------------------------------------------------------------
# Panel wiring — confirm, apply, honest status, one-step undo
# ---------------------------------------------------------------------------


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _panel(controller) -> 'object':
    from htdt.room_acoustics_panel import SurfaceMaterialPanel

    panel = SurfaceMaterialPanel(controller)
    return panel


def _select_material(panel, material_id: str) -> None:
    index = panel.materials.findData(material_id)
    assert index >= 0
    panel.materials.setCurrentIndex(index)


def test_panel_bulk_apply_requires_no_confirmation_when_nothing_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    _repository, controller = _scene_controller(tmp_path, _surfaces(3))
    material = _material()
    controller.material_repository.save_material(material)
    panel = _panel(controller)
    _select_material(panel, material.material_id)

    def _unexpected_dialog(*_args, **_kwargs):
        raise AssertionError('overwrite confirmation must not appear')

    monkeypatch.setattr(
        panel, '_confirm_bulk_apply', _unexpected_dialog
    )
    panel._apply_to_all_boundaries()

    rows = _rows(controller.material_repository)
    assert set(rows) == set(_ids(_document_surfaces(controller)))
    assert all(
        value == material.material_id for value in rows.values()
    )
    assert '1操作で取り消し可能' in panel.readiness.text()
    panel.deleteLater()


def test_panel_bulk_apply_one_undo_restores_prior_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    _repository, controller = _scene_controller(tmp_path, _surfaces(3))
    materials = controller.material_repository
    material_a = _material(label='A')
    material_b = _material(label='B')
    materials.save_material(material_a)
    materials.save_material(material_b)
    surfaces = _document_surfaces(controller)
    materials.assign_material(
        F1_DOCUMENT_ID, surfaces[0].surface_id, material_b
    )
    panel = _panel(controller)
    _select_material(panel, material_a.material_id)

    # Operator chooses overwrite-all at the confirmation.
    monkeypatch.setattr(
        panel, '_confirm_bulk_apply', lambda *_a, **_k: 'all'
    )
    panel._apply_to_all_boundaries()
    rows = _rows(materials)
    assert set(rows.values()) == {material_a.material_id}

    controller.undo()
    rows = _rows(materials)
    assert rows[surfaces[0].surface_id] == material_b.material_id
    assert len(rows) == 1
    panel.deleteLater()


def test_panel_bulk_apply_unassigned_only_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    _repository, controller = _scene_controller(tmp_path, _surfaces(3))
    materials = controller.material_repository
    material_a = _material(label='A')
    material_b = _material(label='B')
    materials.save_material(material_a)
    materials.save_material(material_b)
    surfaces = _document_surfaces(controller)
    materials.assign_material(
        F1_DOCUMENT_ID, surfaces[0].surface_id, material_b
    )
    panel = _panel(controller)
    _select_material(panel, material_a.material_id)
    monkeypatch.setattr(
        panel, '_confirm_bulk_apply', lambda *_a, **_k: 'unassigned'
    )
    panel._apply_to_all_boundaries()

    rows = _rows(materials)
    assert rows[surfaces[0].surface_id] == material_b.material_id
    assert rows[surfaces[1].surface_id] == material_a.material_id
    assert '保持 1面' in panel.readiness.text()
    panel.deleteLater()


def test_panel_bulk_apply_cancel_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    _repository, controller = _scene_controller(tmp_path, _surfaces(2))
    materials = controller.material_repository
    material_a = _material(label='A')
    material_b = _material(label='B')
    materials.save_material(material_a)
    materials.save_material(material_b)
    surfaces = _document_surfaces(controller)
    materials.assign_material(
        F1_DOCUMENT_ID, surfaces[0].surface_id, material_b
    )
    panel = _panel(controller)
    _select_material(panel, material_a.material_id)
    monkeypatch.setattr(
        panel, '_confirm_bulk_apply', lambda *_a, **_k: 'cancel'
    )
    panel._apply_to_all_boundaries()

    rows = _rows(materials)
    assert rows == {surfaces[0].surface_id: material_b.material_id}
    assert '変更された面: 0' in panel.readiness.text()
    panel.deleteLater()


def test_panel_bulk_apply_failure_names_reason_and_zero_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    _repository, controller = _scene_controller(tmp_path, _surfaces(3))
    materials = controller.material_repository
    material = _material()
    materials.save_material(material)
    panel = _panel(controller)
    _select_material(panel, material.material_id)
    _fail_after(materials, monkeypatch, fail_on=5)

    panel._apply_to_all_boundaries()

    assert _rows(materials) == {}
    assert '変更された面: 0' in panel.readiness.text()
    assert '一括適用に失敗' in panel.readiness.text()
    panel.deleteLater()


def test_panel_readiness_reports_mixed_state_honestly(
    tmp_path: Path,
) -> None:
    _app()
    _repository, controller = _scene_controller(tmp_path, _surfaces(2))
    materials = controller.material_repository
    material_a = _material(label='A')
    material_b = _material(label='B')
    materials.save_material(material_a)
    materials.save_material(material_b)
    surfaces = _document_surfaces(controller)
    materials.assign_material(
        F1_DOCUMENT_ID, surfaces[0].surface_id, material_a
    )
    materials.assign_material(
        F1_DOCUMENT_ID, surfaces[1].surface_id, material_b
    )

    panel = _panel(controller)
    try:
        assert '混在' in panel.readiness.text()
    finally:
        panel.deleteLater()


def test_panel_readiness_reports_unresolved_reference(
    tmp_path: Path,
) -> None:
    _app()
    _repository, controller = _scene_controller(tmp_path, _surfaces(1))
    materials = controller.material_repository
    material = _material()
    materials.save_material(material)
    surfaces = _document_surfaces(controller)
    materials.assign_material(
        F1_DOCUMENT_ID, surfaces[0].surface_id, material
    )
    with closing(sqlite3.connect(materials.path)) as connection:
        connection.execute(
            'DELETE FROM cad_acoustic_materials WHERE material_id=?',
            (material.material_id,),
        )
        connection.commit()

    panel = _panel(controller)
    try:
        assert '解決不能' in panel.readiness.text()
        combo = panel.surface_tree.itemWidget(
            panel.surface_tree.topLevelItem(0), 1
        )
        assert combo is not None
        assert '解決不能' in combo.currentText()
    finally:
        panel.deleteLater()
