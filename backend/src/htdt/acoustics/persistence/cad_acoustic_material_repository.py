
import sqlite3
from contextlib import (
    closing,
)
from pathlib import (
    Path,
)
from typing import (
    Any,
    Literal,
)
from ...cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from ...r120_geometry_compiler import (
    SurfaceBoundaryAuthorityBinding,
)
from ...semantic_geometry import (
    SemanticSurface,
)
from ..domain.cad_acoustic_material import (
    AcousticMaterialAuthority,
    BoundaryMaterialState,
    MaterialBulkApplyError,
    MaterialBulkApplyPreview,
    MaterialBulkApplyResult,
    SurfaceMaterialPlanEntry,
    SurfaceMaterialStateEntry,
)

class CadAcousticMaterialRepository:
    """SQLite persistence for the material library and the per-document
    surface-material assignment authority (#465)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_acoustic_materials',
                'cad_surface_material_assignments',
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_material(self, material: AcousticMaterialAuthority) -> None:
        """Persist an immutable material authority.

        The same ``material_id`` with a byte-identical payload is an
        idempotent no-op; a different payload under an existing id is a
        collision and rejected — a new semantic revision must carry a new
        authority identity, never an in-place update.
        """
        payload_json = material.model_dump_json()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_acoustic_materials'
                ' WHERE material_id=?',
                (material.material_id,),
            ).fetchone()
            if row is not None:
                if row['payload_json'] == payload_json:
                    return
                raise ValueError(
                    f'material authority id collision with different payload: '
                    f'{material.material_id}'
                )
            connection.execute(
                'INSERT INTO cad_acoustic_materials(material_id, payload_json)'
                ' VALUES(?,?)',
                (material.material_id, payload_json),
            )

    def get_material(
        self,
        material_id: str,
    ) -> AcousticMaterialAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_acoustic_materials'
                ' WHERE material_id=?',
                (material_id,),
            ).fetchone()
        if row is None:
            return None
        return AcousticMaterialAuthority.model_validate_json(
            row['payload_json']
        )

    def get_material_by_sha256(
        self,
        semantic_sha256: str,
    ) -> AcousticMaterialAuthority | None:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_acoustic_materials'
            ).fetchall()
        for row in rows:
            material = AcousticMaterialAuthority.model_validate_json(
                row['payload_json']
            )
            if material.semantic_sha256 == semantic_sha256:
                return material
        return None

    def list_materials(self) -> tuple[AcousticMaterialAuthority, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_acoustic_materials'
                ' ORDER BY material_id ASC'
            ).fetchall()
        return tuple(
            AcousticMaterialAuthority.model_validate_json(row['payload_json'])
            for row in rows
        )

    def assign_material(
        self,
        document_id: str,
        source_surface_id: str,
        material: AcousticMaterialAuthority,
        surfaces: tuple[SemanticSurface, ...] | None = None,
    ) -> None:
        """Bind a persisted material authority to a document surface.

        The material must already be persisted with an identical semantic
        hash — assignment never trusts caller-authored content. When
        ``surfaces`` (the document's semantic geometry) is supplied the
        target surface must exist in it; without it the target is recorded
        but unverified.
        """
        persisted = self.get_material(material.material_id)
        if persisted is None:
            raise ValueError(
                f'material authority is not persisted: {material.material_id}'
            )
        if persisted.semantic_sha256 != material.semantic_sha256:
            raise ValueError(
                f'material authority hash does not match the persisted '
                f'record: {material.material_id}'
            )
        if surfaces is not None and all(
            surface.surface_id != source_surface_id for surface in surfaces
        ):
            raise ValueError(
                f'surface {source_surface_id} is not present in the '
                'document geometry authority'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_surface_material_assignments'
                '(document_id, source_surface_id, material_id,'
                ' material_sha256) VALUES(?,?,?,?)'
                ' ON CONFLICT(document_id, source_surface_id)'
                ' DO UPDATE SET material_id=excluded.material_id,'
                ' material_sha256=excluded.material_sha256',
                (
                    document_id,
                    source_surface_id,
                    material.material_id,
                    material.semantic_sha256,
                ),
            )

    def clear_assignment(
        self,
        document_id: str,
        source_surface_id: str,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'DELETE FROM cad_surface_material_assignments'
                ' WHERE document_id=? AND source_surface_id=?',
                (document_id, source_surface_id),
            )

    # --- Bulk apply (#996) --------------------------------------------------
    #
    # The 「全境界面へ適用」 flow assigns one material to every room-boundary
    # surface. That must commit as ONE sealed transaction — all targets in a
    # single revision or none — never a half-applied boundary set.

    @staticmethod
    def _normalized_targets(
        source_surface_ids: tuple[str, ...] | list[str],
    ) -> tuple[str, ...]:
        """Deterministic commit order: deduplicated, sorted by surface id."""
        return tuple(sorted(dict.fromkeys(source_surface_ids)))

    @staticmethod
    def _require_persisted_material(
        connection: sqlite3.Connection,
        material: AcousticMaterialAuthority,
    ) -> None:
        """Commit-time material check, run inside the write transaction.

        A material deleted or re-authored between the operator's preview
        and the commit fails the whole batch — the bulk apply never binds
        an authority other than the exact persisted record.
        """
        row = connection.execute(
            'SELECT payload_json FROM cad_acoustic_materials'
            ' WHERE material_id=?',
            (material.material_id,),
        ).fetchone()
        if row is None:
            raise MaterialBulkApplyError(
                'material_not_persisted',
                f'material authority is not persisted: {material.material_id}',
            )
        persisted = AcousticMaterialAuthority.model_validate_json(
            row['payload_json']
        )
        if persisted.semantic_sha256 != material.semantic_sha256:
            raise MaterialBulkApplyError(
                'material_sha_drift',
                'material authority hash does not match the persisted '
                f'record: {material.material_id}',
            )

    @staticmethod
    def _require_head_revision(
        connection: sqlite3.Connection,
        document_id: str,
        expected_scene_revision_id: str | None,
    ) -> str | None:
        """Commit-time scene-head pin check, inside the write transaction.

        When the caller previewed against ``expected_scene_revision_id``
        and the document head has since advanced (concurrent save), the
        whole batch rolls back — the surfaces the operator confirmed are
        the ones the commit would bind, never a drifted set.
        """
        row = connection.execute(
            'SELECT head_revision_id FROM scene_document_heads'
            ' WHERE document_id=?',
            (document_id,),
        ).fetchone()
        actual = None if row is None else row['head_revision_id']
        if (
            expected_scene_revision_id is not None
            and actual != expected_scene_revision_id
        ):
            raise MaterialBulkApplyError(
                'scene_revision_drift',
                'document head drifted before commit: expected '
                f'{expected_scene_revision_id}, found {actual}',
            )
        return actual

    @staticmethod
    def _row_material(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> AcousticMaterialAuthority | None:
        """Resolve one assignment row's material inside ``connection``.

        A vanished material record returns ``None`` (dangling reference);
        a hash mismatch is authority corruption and fails closed, matching
        ``assignments_for_document``.
        """
        material_row = connection.execute(
            'SELECT payload_json FROM cad_acoustic_materials'
            ' WHERE material_id=?',
            (row['material_id'],),
        ).fetchone()
        if material_row is None:
            return None
        material = AcousticMaterialAuthority.model_validate_json(
            material_row['payload_json']
        )
        if material.semantic_sha256 != row['material_sha256']:
            raise ValueError(
                f'surface assignment for {row["source_surface_id"]} '
                'references a different material authority hash'
            )
        return material

    def preview_assign_material_bulk(
        self,
        document_id: str,
        source_surface_ids: tuple[str, ...] | list[str],
        material: AcousticMaterialAuthority,
        *,
        surfaces: tuple[SemanticSurface, ...] | None = None,
    ) -> MaterialBulkApplyPreview:
        """Read-only pre-commit snapshot of a bulk boundary apply (#996).

        Captures the document head pin, the exact material identity, and
        every target's current assignment row so the operator sees target /
        overwrite / unassigned / kept counts before anything is written.
        """
        targets = self._normalized_targets(source_surface_ids)
        if surfaces is not None:
            known = {surface.surface_id for surface in surfaces}
            missing = [sid for sid in targets if sid not in known]
            if missing:
                raise MaterialBulkApplyError(
                    'surface_missing',
                    'surfaces not present in the document geometry '
                    f'authority: {", ".join(missing)}',
                )
        with closing(self._connect()) as connection, connection:
            self._require_persisted_material(connection, material)
            head = self._require_head_revision(connection, document_id, None)
            rows = connection.execute(
                'SELECT source_surface_id, material_id, material_sha256'
                ' FROM cad_surface_material_assignments'
                ' WHERE document_id=?',
                (document_id,),
            ).fetchall()
            rows_by_surface = {row['source_surface_id']: row for row in rows}
            entries: list[SurfaceMaterialPlanEntry] = []
            for surface_id in targets:
                row = rows_by_surface.get(surface_id)
                if row is None:
                    entries.append(
                        SurfaceMaterialPlanEntry(
                            surface_id=surface_id,
                            row_material_id=None,
                            row_material_sha256=None,
                            row_material_resolves=False,
                            same_material=False,
                        )
                    )
                    continue
                resolved = self._row_material(connection, row)
                entries.append(
                    SurfaceMaterialPlanEntry(
                        surface_id=surface_id,
                        row_material_id=row['material_id'],
                        row_material_sha256=row['material_sha256'],
                        row_material_resolves=resolved is not None,
                        same_material=(
                            resolved is not None
                            and resolved.material_id == material.material_id
                            and resolved.semantic_sha256
                            == material.semantic_sha256
                        ),
                    )
                )
        return MaterialBulkApplyPreview(
            document_id=document_id,
            scene_revision_id=head,
            material_id=material.material_id,
            material_sha256=material.semantic_sha256,
            entries=tuple(entries),
            target_count=len(targets),
            unassigned_count=sum(
                1 for entry in entries if entry.row_material_id is None
            ),
            already_assigned_count=sum(
                1 for entry in entries if entry.same_material
            ),
            overwrite_count=sum(
                1
                for entry in entries
                if entry.row_material_id is not None
                and entry.row_material_resolves
                and not entry.same_material
            ),
            dangling_count=sum(
                1
                for entry in entries
                if entry.row_material_id is not None
                and not entry.row_material_resolves
            ),
        )

    def assign_material_bulk(
        self,
        document_id: str,
        source_surface_ids: tuple[str, ...] | list[str],
        material: AcousticMaterialAuthority,
        *,
        surfaces: tuple[SemanticSurface, ...] | None = None,
        only_unassigned: bool = False,
        expected_scene_revision_id: str | None = None,
    ) -> MaterialBulkApplyResult:
        """Bind one material to every listed surface in ONE transaction (#996).

        All commits share a single ``BEGIN IMMEDIATE`` write transaction:
        the persisted-material check, the optional scene-head pin, and
        every upsert. Any failure — mid-write SQL error, vanished material,
        hash drift, concurrent head advance — rolls the whole batch back;
        the authority never observes a half-applied boundary set.

        Targets are deduplicated and sorted by surface id so identical
        calls commit identical writes. ``only_unassigned`` keeps existing
        rows (including dangling claims) untouched instead of overwriting.
        """
        targets = self._normalized_targets(source_surface_ids)
        if surfaces is not None:
            known = {surface.surface_id for surface in surfaces}
            missing = [sid for sid in targets if sid not in known]
            if missing:
                raise MaterialBulkApplyError(
                    'surface_missing',
                    'surfaces not present in the document geometry '
                    f'authority: {", ".join(missing)}',
                )
        applied: list[str] = []
        kept: list[str] = []
        skipped: list[str] = []
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self._require_persisted_material(connection, material)
            head = self._require_head_revision(
                connection, document_id, expected_scene_revision_id
            )
            placeholders = ','.join('?' for _ in targets)
            rows = (
                connection.execute(
                    'SELECT source_surface_id, material_id, material_sha256'
                    ' FROM cad_surface_material_assignments'
                    f' WHERE document_id=? AND source_surface_id IN ({placeholders})',
                    (document_id, *targets),
                ).fetchall()
                if targets
                else ()
            )
            current = {row['source_surface_id']: row for row in rows}
            for surface_id in targets:
                row = current.get(surface_id)
                if (
                    row is not None
                    and row['material_id'] == material.material_id
                    and row['material_sha256'] == material.semantic_sha256
                ):
                    kept.append(surface_id)
                    continue
                if only_unassigned and row is not None:
                    skipped.append(surface_id)
                    continue
                connection.execute(
                    'INSERT INTO cad_surface_material_assignments'
                    '(document_id, source_surface_id, material_id,'
                    ' material_sha256) VALUES(?,?,?,?)'
                    ' ON CONFLICT(document_id, source_surface_id)'
                    ' DO UPDATE SET material_id=excluded.material_id,'
                    ' material_sha256=excluded.material_sha256',
                    (
                        document_id,
                        surface_id,
                        material.material_id,
                        material.semantic_sha256,
                    ),
                )
                applied.append(surface_id)
        return MaterialBulkApplyResult(
            document_id=document_id,
            scene_revision_id=head,
            material_id=material.material_id,
            material_sha256=material.semantic_sha256,
            applied_surface_ids=tuple(applied),
            kept_surface_ids=tuple(kept),
            skipped_surface_ids=tuple(skipped),
        )

    def restore_assignment_rows(
        self,
        document_id: str,
        rows: tuple[tuple[str, str | None, str | None], ...],
        *,
        expected_scene_revision_id: str | None = None,
    ) -> None:
        """Verbatim restore of captured assignment rows in one transaction.

        The revert leg of a bulk-apply Undo step must be as atomic as the
        apply it undoes. ``(surface_id, None, None)`` removes the row;
        ``(surface_id, material_id, material_sha256)`` restores the exact
        previously committed reference — including rows whose material can
        no longer resolve, which a fresh ``assign_material`` would refuse.
        """
        restored = sorted(dict.fromkeys(row[0] for row in rows))
        by_surface = {row[0]: row for row in rows}
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self._require_head_revision(
                connection, document_id, expected_scene_revision_id
            )
            for surface_id in restored:
                _, material_id, material_sha256 = by_surface[surface_id]
                if material_id is None:
                    connection.execute(
                        'DELETE FROM cad_surface_material_assignments'
                        ' WHERE document_id=? AND source_surface_id=?',
                        (document_id, surface_id),
                    )
                    continue
                connection.execute(
                    'INSERT INTO cad_surface_material_assignments'
                    '(document_id, source_surface_id, material_id,'
                    ' material_sha256) VALUES(?,?,?,?)'
                    ' ON CONFLICT(document_id, source_surface_id)'
                    ' DO UPDATE SET material_id=excluded.material_id,'
                    ' material_sha256=excluded.material_sha256',
                    (
                        document_id,
                        surface_id,
                        material_id,
                        material_sha256,
                    ),
                )

    def assignment_rows_for(
        self,
        document_id: str,
        source_surface_ids: tuple[str, ...] | list[str],
    ) -> tuple[tuple[str, str | None, str | None], ...]:
        """Raw ``(surface_id, material_id, material_sha256)`` rows — the
        exact snapshot a bulk apply captures for its revert leg (#996)."""
        targets = self._normalized_targets(source_surface_ids)
        if not targets:
            return ()
        with closing(self._connect()) as connection, connection:
            placeholders = ','.join('?' for _ in targets)
            rows = connection.execute(
                'SELECT source_surface_id, material_id, material_sha256'
                ' FROM cad_surface_material_assignments'
                f' WHERE document_id=? AND source_surface_id IN ({placeholders})',
                (document_id, *targets),
            ).fetchall()
        found = {row['source_surface_id']: row for row in rows}
        return tuple(
            (
                surface_id,
                (
                    found[surface_id]['material_id']
                    if surface_id in found
                    else None
                ),
                (
                    found[surface_id]['material_sha256']
                    if surface_id in found
                    else None
                ),
            )
            for surface_id in targets
        )

    def boundary_material_state(
        self,
        document_id: str,
        surfaces: tuple[SemanticSurface, ...],
    ) -> BoundaryMaterialState:
        """Honest aggregate of a document's surface assignments (#996).

        ``entries`` covers every supplied surface — each reports
        ``assigned`` / ``unassigned`` / ``unresolved_reference`` (a stored
        row whose material record is gone or fails its hash check is
        reported, never silently coerced to unassigned). ``stale_surface_ids``
        lists rows referencing surfaces outside the supplied geometry.

        The summary describes the ``room_boundary`` subset only:
        ``uniform`` when all boundaries share one material, ``mixed`` when
        a partial apply left several distinct ones, ``partial`` while any
        boundary is unassigned, and ``unresolved`` when any boundary row
        fails to resolve — never coerced to "assigned".
        """
        ordered = sorted(surfaces, key=lambda surface: surface.surface_id)
        boundary = [
            surface
            for surface in ordered
            if surface.semantic_class == 'room_boundary'
        ]
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT source_surface_id, material_id, material_sha256'
                ' FROM cad_surface_material_assignments'
                ' WHERE document_id=?',
                (document_id,),
            ).fetchall()
            rows_by_surface = {
                row['source_surface_id']: row for row in rows
            }
            resolved: dict[str, AcousticMaterialAuthority | None] = {}
            for row in rows:
                resolved[row['source_surface_id']] = self._row_material(
                    connection, row
                )
        surface_ids = {surface.surface_id for surface in ordered}
        stale = sorted(
            surface_id
            for surface_id in resolved
            if surface_id not in surface_ids
        )
        entries: list[SurfaceMaterialStateEntry] = []
        for surface in ordered:
            row = rows_by_surface.get(surface.surface_id)
            if row is None:
                status: Literal[
                    'assigned', 'unassigned', 'unresolved_reference'
                ] = 'unassigned'
                material_id = None
            else:
                material_id = row['material_id']
                status = (
                    'assigned'
                    if resolved[surface.surface_id] is not None
                    else 'unresolved_reference'
                )
            entries.append(
                SurfaceMaterialStateEntry(
                    surface_id=surface.surface_id,
                    surface_key=surface.surface_key,
                    semantic_class=surface.semantic_class,
                    material_id=material_id,
                    status=status,
                )
            )
        boundary_entries = {
            surface.surface_id for surface in boundary
        }
        unresolved = tuple(
            entry.surface_id
            for entry in entries
            if entry.surface_id in boundary_entries
            and entry.status == 'unresolved_reference'
        )
        assigned_ids = sorted(
            {
                entry.material_id
                for entry in entries
                if entry.surface_id in boundary_entries
                and entry.status == 'assigned'
                and entry.material_id is not None
            }
        )
        boundary_statuses = [
            entry.status
            for entry in entries
            if entry.surface_id in boundary_entries
        ]
        if not boundary:
            summary: Literal[
                'no_boundaries', 'unassigned', 'partial',
                'uniform', 'mixed', 'unresolved',
            ] = 'no_boundaries'
        elif unresolved or stale:
            summary = 'unresolved'
        elif all(status == 'unassigned' for status in boundary_statuses):
            summary = 'unassigned'
        elif any(status == 'unassigned' for status in boundary_statuses):
            summary = 'partial'
        elif len(assigned_ids) == 1:
            summary = 'uniform'
        else:
            summary = 'mixed'
        return BoundaryMaterialState(
            document_id=document_id,
            summary=summary,
            boundary_surface_ids=tuple(
                surface.surface_id for surface in boundary
            ),
            entries=tuple(entries),
            assigned_material_ids=tuple(assigned_ids),
            unresolved_surface_ids=unresolved,
            stale_surface_ids=tuple(stale),
        )

    def assignment_for(
        self,
        document_id: str,
        source_surface_id: str,
    ) -> AcousticMaterialAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT material_id, material_sha256'
                ' FROM cad_surface_material_assignments'
                ' WHERE document_id=? AND source_surface_id=?',
                (document_id, source_surface_id),
            ).fetchone()
        if row is None:
            return None
        material = self.get_material(row['material_id'])
        if material is None:
            return None
        if material.semantic_sha256 != row['material_sha256']:
            raise ValueError(
                f'surface assignment for {source_surface_id} references a '
                'different material authority hash — refusing to resolve'
            )
        return material

    def assignments_for_document(
        self,
        document_id: str,
    ) -> dict[str, AcousticMaterialAuthority]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT source_surface_id, material_id, material_sha256'
                ' FROM cad_surface_material_assignments'
                ' WHERE document_id=? ORDER BY source_surface_id ASC',
                (document_id,),
            ).fetchall()
        result: dict[str, AcousticMaterialAuthority] = {}
        for row in rows:
            material = self.get_material(row['material_id'])
            if material is None:
                continue
            if material.semantic_sha256 != row['material_sha256']:
                raise ValueError(
                    f'surface assignment for {row["source_surface_id"]} '
                    'references a different material authority hash'
                )
            result[row['source_surface_id']] = material
        return result

    def boundary_bindings(
        self,
        document_id: str,
        surfaces: tuple[SemanticSurface, ...],
    ) -> tuple[SurfaceBoundaryAuthorityBinding, ...]:
        """Material authority bindings for ``compile_r120_geometry`` —
        only surfaces with an explicit assignment produce a binding;
        unassigned surfaces stay UNSUPPORTED downstream, never guessed."""
        assignments = self.assignments_for_document(document_id)
        surface_ids = {surface.surface_id for surface in surfaces}
        stale = sorted(
            source_surface_id
            for source_surface_id in assignments
            if source_surface_id not in surface_ids
        )
        if stale:
            raise ValueError(
                'surface material assignments reference surfaces missing '
                f'from the document geometry: {", ".join(stale)}'
            )
        bindings: list[SurfaceBoundaryAuthorityBinding] = []
        for surface in surfaces:
            material = assignments.get(surface.surface_id)
            if material is None:
                continue
            bindings.append(
                SurfaceBoundaryAuthorityBinding(
                    source_surface_id=surface.surface_id,
                    material_authority=material.authority_ref(),
                )
            )
        return tuple(bindings)

__all__ = [
    'CadAcousticMaterialRepository',
]
