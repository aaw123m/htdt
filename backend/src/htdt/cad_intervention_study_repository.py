"""Append-only SQLite persistence for intervention-study authority (#519)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Literal

from .cad_intervention_study import (
    InterventionAlternative,
    InterventionStudySpec,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema, require_native_tables
from .cad_system_variant_repository import CadSystemVariantRepository


class CadInterventionStudyRepository:
    """Persist ``InterventionStudySpec`` + ``InterventionAlternative`` rows.

    Writes and reads re-establish the authority chain: a spec must bind a
    resolvable SceneRevision and a SystemVariant bound to that revision; an
    alternative must bind a persisted spec row. Persisted payloads are
    re-validated through the immutable models on every authoritative read, so
    tampered rows fail closed instead of being trusted.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.db_path = Path(scene_repository.path)
        if Path(self.variant_repository.path) != self.db_path:
            raise ValueError(
                'intervention study and variant repositories must share '
                'one native CAD database'
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.db_path)
        self._initialize()

    @property
    def path(self) -> Path:
        return self.db_path

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.db_path)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_intervention_study_specs',
                'cad_intervention_alternatives',
            )


    def _require_scene_revision(
        self, spec: InterventionStudySpec
    ) -> None:
        revision = self.scene_repository.get(spec.scene_revision_id)
        if (
            revision is None
            or revision.document_id != spec.document_id
            or revision.content_hash != spec.scene_content_hash
        ):
            raise ValueError(
                'intervention study SceneRevision is not registered '
                'for replay'
            )
        variant = self.variant_repository.get_variant(
            spec.base_system_variant_id
        )
        if (
            variant is None
            or variant.variant_sha256 != spec.base_system_variant_sha256
            or variant.baseline_revision_id != spec.scene_revision_id
            or variant.baseline_content_hash != spec.scene_content_hash
        ):
            raise ValueError(
                'intervention study base SystemVariant is not registered '
                'for replay'
            )

    def save_spec(self, spec: InterventionStudySpec) -> InterventionStudySpec:
        self._require_scene_revision(spec)
        payload = spec.model_dump(mode='json')
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_intervention_study_specs '
                'WHERE spec_id = ?',
                (spec.spec_id,),
            ).fetchone()
            if existing is not None:
                if existing['payload_json'] != payload and (
                    InterventionStudySpec.model_validate_json(
                        existing['payload_json']
                    )
                    != spec
                ):
                    raise ValueError(
                        'conflicting intervention study spec for this ID'
                    )
                return spec
            connection.execute(
                'INSERT INTO cad_intervention_study_specs ('
                '    spec_id, document_id, scene_revision_id, '
                '    scene_content_hash, payload_json, spec_sha256, '
                '    created_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    spec.spec_id,
                    spec.document_id,
                    spec.scene_revision_id,
                    spec.scene_content_hash,
                    spec.model_dump_json(),
                    spec.spec_sha256,
                    spec.created_at_utc,
                ),
            )
        return spec

    def get_spec(self, spec_id: str) -> InterventionStudySpec | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_intervention_study_specs WHERE spec_id = ?',
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validated_spec_row(row)

    def list_specs(
        self,
        document_id: str,
        *,
        scene_revision_id: str | None = None,
    ) -> list[InterventionStudySpec]:
        sql = (
            'SELECT * FROM cad_intervention_study_specs WHERE document_id = ?'
        )
        args: list[str] = [document_id]
        if scene_revision_id is not None:
            sql += ' AND scene_revision_id = ?'
            args.append(scene_revision_id)
        sql += ' ORDER BY created_at_utc, spec_id'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._validated_spec_row(row) for row in rows]

    def _validated_spec_row(self, row: sqlite3.Row) -> InterventionStudySpec:
        payload = row['payload_json']
        spec = InterventionStudySpec.model_validate_json(payload)
        if (
            spec.spec_id != row['spec_id']
            or spec.spec_sha256 != row['spec_sha256']
            or spec.document_id != row['document_id']
            or spec.scene_revision_id != row['scene_revision_id']
            or spec.scene_content_hash != row['scene_content_hash']
            or spec.created_at_utc != row['created_at_utc']
        ):
            raise ValueError(
                'persisted intervention study spec authority mismatch'
            )
        self._require_scene_revision(spec)
        return spec

    def save_alternative(
        self, alternative: InterventionAlternative
    ) -> InterventionAlternative:
        spec = self.get_spec(alternative.study_spec_id)
        if spec is None or spec.spec_sha256 != alternative.study_spec_sha256:
            raise ValueError(
                'intervention alternative study spec is not registered '
                'for replay'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_intervention_alternatives '
                'WHERE alternative_id = ?',
                (alternative.alternative_id,),
            ).fetchone()
            if existing is not None:
                if existing['payload_json'] != alternative.model_dump_json() and (
                    InterventionAlternative.model_validate_json(
                        existing['payload_json']
                    )
                    != alternative
                ):
                    raise ValueError(
                        'conflicting intervention alternative for this ID'
                    )
                return alternative
            connection.execute(
                'INSERT INTO cad_intervention_alternatives ('
                '    alternative_id, spec_id, family, payload_json, '
                '    alternative_sha256, created_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?)',
                (
                    alternative.alternative_id,
                    spec.spec_id,
                    alternative.family,
                    alternative.model_dump_json(),
                    alternative.alternative_sha256,
                    spec.created_at_utc,
                ),
            )
        return alternative

    def list_alternatives(
        self, spec_id: str
    ) -> list[InterventionAlternative]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_intervention_alternatives '
                'WHERE spec_id = ? ORDER BY created_at_utc, alternative_id',
                (spec_id,),
            ).fetchall()
        results: list[InterventionAlternative] = []
        for row in rows:
            alternative = InterventionAlternative.model_validate_json(
                row['payload_json']
            )
            if (
                alternative.alternative_id != row['alternative_id']
                or alternative.alternative_sha256 != row['alternative_sha256']
                or alternative.study_spec_id != row['spec_id']
                or alternative.family != row['family']
            ):
                raise ValueError(
                    'persisted intervention alternative authority mismatch'
                )
            results.append(alternative)
        return results


ReviewState = Literal['pending', 'reviewed', 'applied', 'discarded']

__all__ = [
    'CadInterventionStudyRepository',
    'ReviewState',
]
