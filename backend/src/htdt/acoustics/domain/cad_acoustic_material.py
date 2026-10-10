"""Acoustic material/boundary library and per-surface assignment (#465).

An ``AcousticMaterialAuthority`` is a sealed, versioned library entry: it
declares wave-domain (rigid / specific-impedance table / unsupported) and
geometric-domain (banded absorption/scattering / unsupported) capability
separately — impedance is never inferred from absorption bands, and bands
are never inferred from impedance. Per-surface assignment rows bind one
material authority to a semantic surface id per document; openings keep
surface-material, portal, and boundary-termination authorities distinct
(this module only authors ``material_authority`` refs — portals and
terminations stay in their own authorities). Object surfaces and attached
``AcousticTreatment`` overlays remain separate concepts: a treatment
definition is not a base material.

Assigned materials replay into ``compile_r120_geometry`` as
``SurfaceBoundaryAuthorityBinding`` rows and from there into the
AcousticSceneSnapshot ``surface_boundary_configuration``.

Bulk applies (「全境界面へ適用」, #996) commit through
``assign_material_bulk`` — one ``BEGIN IMMEDIATE`` transaction that
re-verifies the persisted material hash and the pinned scene head at
commit time, so a mid-batch failure or concurrent drift rolls the whole
batch back with zero surfaces changed. ``preview_assign_material_bulk``
provides the read-only pre-commit snapshot and ``boundary_material_state``
the honest read side (mixed / partial / unresolved are reported, never
coerced to "assigned").
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_schema import connect_sqlite, ensure_native_schema, require_native_tables
from ...cad_dependency_impact import SceneChange
from .acoustic_benchmark import (
    AcousticMaterial,
    GeometricAcousticBand,
    SpecificImpedancePoint,
)
from ...r120_geometry_compiler import (
    ExactExternalAuthorityRef,
    SurfaceBoundaryAuthorityBinding,
)
from ...semantic_geometry import SemanticSurface
from ...canonical_json import canonical_json as _canonical, canonical_sha256 as _hash
from ...clock import utc_now_iso as _utc_now


_MATERIAL_PREFIX = 'acoustic-material:'

WAVE_MODEL_LABELS: dict[str, str] = {
    'rigid': '剛性（完全反射）',
    'specific_impedance_table': '比インピーダンス表',
    'unsupported': '波動物理なし（非対応）',
}
GEOMETRIC_MODEL_LABELS: dict[str, str] = {
    'banded': 'バンド吸音/散乱',
    'unsupported': '幾何物理なし（非対応）',
}
SURFACE_CLASS_LABELS: dict[str, str] = {
    'room_boundary': '部屋境界（壁・床・天井）',
    'object_surface': 'オブジェクト表面',
    'unknown': '不明',
}


class AcousticMaterialAuthority(BaseModel):
    """Sealed acoustic material/boundary library entry (#465)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    material_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    label: str = Field(min_length=1)
    wave_model: Literal[
        'rigid', 'specific_impedance_table', 'unsupported'
    ] = 'unsupported'
    specific_impedance: tuple[SpecificImpedancePoint, ...] = ()
    geometric_model: Literal['banded', 'unsupported'] = 'unsupported'
    geometric_bands: tuple[GeometricAcousticBand, ...] = ()
    provenance: str = Field(min_length=1)
    notes: str = ''
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_material(self) -> 'AcousticMaterialAuthority':
        try:
            parsed = datetime.fromisoformat(self.created_at_utc)
        except ValueError as exc:
            raise ValueError('material created_at_utc must be ISO-8601') from exc
        if parsed.tzinfo is None:
            raise ValueError('material created_at_utc must be timezone-aware')
        if not self.material_id.startswith(_MATERIAL_PREFIX):
            raise ValueError('material id must use acoustic-material: prefix')
        if self.wave_model == 'specific_impedance_table':
            if not self.specific_impedance:
                raise ValueError(
                    'specific_impedance_table wave model requires typed '
                    'impedance points — impedance is never inferred from bands'
                )
        elif self.specific_impedance:
            raise ValueError(
                'impedance data is only valid for the '
                'specific_impedance_table wave model'
            )
        if self.geometric_model == 'banded':
            if not self.geometric_bands:
                raise ValueError(
                    'banded geometric model requires typed bands — bands '
                    'are never inferred from impedance'
                )
        elif self.geometric_bands:
            raise ValueError(
                'geometric bands are only valid for the banded geometric model'
            )
        if self.wave_model == 'unsupported' and self.geometric_model == 'unsupported':
            raise ValueError(
                'a material with no wave or geometric capability carries no '
                'physics — use UNKNOWN surface state instead of a library entry'
            )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('material authority semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'material_id': self.material_id,
            'authority_version': self.authority_version,
            'label': self.label,
            'wave_model': self.wave_model,
            'geometric_model': self.geometric_model,
            'provenance': self.provenance,
            'notes': self.notes,
            'created_at_utc': self.created_at_utc,
        }
        if self.specific_impedance:
            payload['specific_impedance'] = [
                point.model_dump(mode='json')
                for point in self.specific_impedance
            ]
        if self.geometric_bands:
            payload['geometric_bands'] = [
                band.model_dump(mode='json')
                for band in self.geometric_bands
            ]
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.material_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

    def as_acoustic_material(self) -> AcousticMaterial:
        """The solver-facing material payload — capability semantics only."""
        return AcousticMaterial(
            material_id=self.material_id,
            provenance=self.provenance,
            version=self.authority_version,
            wave_model=self.wave_model,
            specific_impedance=self.specific_impedance,
            geometric_model=self.geometric_model,
            geometric_bands=self.geometric_bands,
        )


def material_capability_label(material: AcousticMaterialAuthority) -> str:
    wave = WAVE_MODEL_LABELS.get(material.wave_model, material.wave_model)
    geo = GEOMETRIC_MODEL_LABELS.get(
        material.geometric_model, material.geometric_model
    )
    return f'wave: {wave} · geometric: {geo}'


def surface_class_label(semantic_class: str) -> str:
    return SURFACE_CLASS_LABELS.get(semantic_class, semantic_class)


class MaterialBulkApplyError(ValueError):
    """Fail-closed bulk surface-material apply refusal (#996).

    ``reason`` names the commit-time check that refused the batch — the
    whole transaction rolls back, so a refused apply never leaves a
    partially assigned boundary set behind.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(detail)


class SurfaceMaterialPlanEntry(BaseModel):
    """One target surface's pre-commit snapshot row (#996)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    surface_id: str = Field(min_length=1)
    row_material_id: str | None
    row_material_sha256: str | None
    #: The referenced material record still exists with the pinned hash —
    #: ``False`` means the row claims a vanished authority (dangling).
    row_material_resolves: bool
    #: Row resolves to the exact material being applied.
    same_material: bool


class MaterialBulkApplyPreview(BaseModel):
    """Read-only snapshot of a bulk boundary apply before commit (#996).

    Captures the document head pin, the material identity, and every
    target surface's current row so the operator can see the overwrite /
    unassigned / kept counts before anything is written.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str = Field(min_length=1)
    scene_revision_id: str | None
    material_id: str = Field(min_length=1)
    material_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    #: Sorted by ``surface_id`` — the same deterministic order the commit
    #: writes in.
    entries: tuple[SurfaceMaterialPlanEntry, ...]
    target_count: int = Field(ge=0)
    unassigned_count: int = Field(ge=0)
    already_assigned_count: int = Field(ge=0)
    overwrite_count: int = Field(ge=0)
    #: Rows claiming a material that no longer resolves — overwriting them
    #: is still an overwrite, counted separately so it is never hidden in
    #: "unassigned".
    dangling_count: int = Field(ge=0)


class MaterialBulkApplyResult(BaseModel):
    """Committed outcome of one atomic bulk apply (#996)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str = Field(min_length=1)
    scene_revision_id: str | None
    material_id: str = Field(min_length=1)
    material_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    applied_surface_ids: tuple[str, ...]
    kept_surface_ids: tuple[str, ...]
    skipped_surface_ids: tuple[str, ...]


class SurfaceMaterialStateEntry(BaseModel):
    """Honest per-surface material state for one geometry surface (#996)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    surface_id: str = Field(min_length=1)
    surface_key: str = Field(min_length=1)
    semantic_class: str = Field(min_length=1)
    material_id: str | None
    status: Literal['assigned', 'unassigned', 'unresolved_reference']


class BoundaryMaterialState(BaseModel):
    """Honest aggregate of a document's boundary-material assignments (#996).

    A project left with mixed materials by an older partial apply reports
    ``mixed`` (or ``partial`` / ``unresolved``); it is never collapsed into
    a single "assigned" verdict. Rows referencing surfaces outside the
    supplied geometry are exposed as ``stale_surface_ids`` rather than
    dropped.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    document_id: str = Field(min_length=1)
    summary: Literal[
        'no_boundaries',
        'unassigned',
        'partial',
        'uniform',
        'mixed',
        'unresolved',
    ]
    boundary_surface_ids: tuple[str, ...]
    entries: tuple[SurfaceMaterialStateEntry, ...]
    assigned_material_ids: tuple[str, ...]
    unresolved_surface_ids: tuple[str, ...]
    stale_surface_ids: tuple[str, ...]


def material_assignment_changes(
    before_rows: tuple[tuple[str, str | None, str | None], ...],
    after_rows: tuple[tuple[str, str | None, str | None], ...],
) -> tuple['SceneChange', ...]:
    """#964-facing change events for material sidecar edits (#996).

    Material assignments are design-transaction sidecars (#915): they do
    not advance the scene head, so the document diff alone cannot see
    them. Callers composing the revalidation queue feed these through
    ``extra_changes`` — one bulk apply emits ONE ``authority_reference``
    change on the ``material_boundary`` axis (never per-surface noise),
    which marks every material-dependent analysis artifact stale via the
    declared ``material_boundary → recompute / re_evaluate`` rules.
    """
    before = {
        row[0]: (row[1], row[2]) for row in before_rows
    }
    after = {row[0]: (row[1], row[2]) for row in after_rows}
    changed = sorted(
        surface_id
        for surface_id in set(before) | set(after)
        if before.get(surface_id) != after.get(surface_id)
    )
    if not changed:
        return ()
    materials = sorted(
        {
            after[surface_id][0]
            for surface_id in changed
            if after.get(surface_id) is not None
            and after[surface_id][0] is not None
        }
    )
    return (
        SceneChange(
            kind='authority_reference',
            axes=frozenset({'material_boundary'}),
            entity_id=None,
            detail=(
                'surface material assignments changed for '
                f'{len(changed)} surface(s) -> '
                + (', '.join(materials) if materials else 'cleared')
            ),
        ),
    )


def build_acoustic_material(
    *,
    label: str,
    provenance: str,
    wave_model: str = 'unsupported',
    specific_impedance: tuple[SpecificImpedancePoint, ...] = (),
    geometric_model: str = 'unsupported',
    geometric_bands: tuple[GeometricAcousticBand, ...] = (),
    notes: str = '',
    authority_version: str = '1',
    material_id: str | None = None,
    created_at_utc: str | None = None,
) -> AcousticMaterialAuthority:
    """Assemble a sealed material authority from typed capability fields."""

    payload: dict[str, Any] = {
        'material_id': material_id or f'{_MATERIAL_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'label': label,
        'wave_model': wave_model,
        'specific_impedance': specific_impedance,
        'geometric_model': geometric_model,
        'geometric_bands': geometric_bands,
        'provenance': provenance,
        'notes': notes,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = AcousticMaterialAuthority.model_construct(
        **payload,
        semantic_sha256='0' * 64,
    )
    return AcousticMaterialAuthority.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
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
    'AcousticMaterialAuthority',
    'BoundaryMaterialState',
    'CadAcousticMaterialRepository',
    'GEOMETRIC_MODEL_LABELS',
    'MaterialBulkApplyError',
    'MaterialBulkApplyPreview',
    'MaterialBulkApplyResult',
    'SURFACE_CLASS_LABELS',
    'SurfaceMaterialPlanEntry',
    'SurfaceMaterialStateEntry',
    'WAVE_MODEL_LABELS',
    'build_acoustic_material',
    'material_assignment_changes',
    'material_capability_label',
    'surface_class_label',
]
