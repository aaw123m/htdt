"""Persistence for :class:`ProjectTemplate` and instantiations (#614).

Shares the SceneRepository SQLite path. Templates are immutable by
``(template_id, version)`` — a changed template is a new version, never an
edit; projects created earlier keep their instantiation record pointing at
the version they were built from.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .cad_schema import connect_sqlite, require_native_tables
from .cad_project_template import (
    ProjectTemplate,
    ProjectTemplateInstantiation,
    _hash,
    builtin_project_templates,
)


class ProjectTemplateConflictError(ValueError):
    """A template id+version already exists with different content."""


class CadProjectTemplateRepository:
    """SQLite store for user templates; built-ins live in code."""

    def __init__(self, scene_repository) -> None:
        self.path = Path(scene_repository.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'project_templates',
                'template_instantiations',
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_template(self, template: ProjectTemplate) -> ProjectTemplate:
        if template.kind == 'builtin':
            raise ValueError('built-in templates are read-only')
        # A forged ``model_copy`` keeps a template_sha256 its payload never
        # earned — re-verify the seal here or the row only fails reads.
        if template.template_sha256 != _hash(template.semantic_payload()):
            raise ValueError(
                'template payload does not match its sealed sha256'
            )
        with closing(self._connect()) as connection:
            existing = connection.execute(
                """
                SELECT template_sha256 FROM project_templates
                WHERE template_id = ? AND version = ?
                """,
                (template.template_id, template.version),
            ).fetchone()
            if existing is not None:
                if existing['template_sha256'] != template.template_sha256:
                    raise ProjectTemplateConflictError(
                        f'template {template.template_id} version '
                        f'{template.version} already exists with different content'
                    )
                return template
            with connection:
                connection.execute(
                    """
                    INSERT INTO project_templates (
                        template_id, version, kind, template_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        template.template_id,
                        template.version,
                        template.kind,
                        template.template_sha256,
                        json.dumps(
                            template.model_dump(mode='json'), ensure_ascii=False,
                            allow_nan=False,
                        ),
                    ),
                )
        return template

    def get_template(
        self, template_id: str, version: str
    ) -> ProjectTemplate | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM project_templates
                WHERE template_id = ? AND version = ?
                """,
                (template_id, version),
            ).fetchone()
        if row is not None:
            return ProjectTemplate.model_validate_json(row['payload_json'])
        for template in builtin_project_templates():
            if template.template_id == template_id and template.version == version:
                return template
        return None

    def list_templates(self) -> tuple[ProjectTemplate, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM project_templates
                ORDER BY template_id ASC, version ASC
                """
            ).fetchall()
        return tuple(builtin_project_templates()) + tuple(
            ProjectTemplate.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_instantiation(
        self,
        instantiation: ProjectTemplateInstantiation,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ProjectTemplateInstantiation:
        """Persist an instantiation record only when its claims revalidate.

        ``connection`` (#864): when supplied, the write joins the caller's
        transaction — project creation commits the Project Library row, the
        initial SceneRevision and this provenance record as one unit.
        """

        if connection is not None:
            # #767: verify the migrated contract inside the
            # caller transaction, never converge it.
            require_native_tables(
                connection,
                'project_templates',
                'template_instantiations',
            )
            return self._save_instantiation_in_transaction(
                connection, instantiation
            )
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                result = self._save_instantiation_in_transaction(
                    connection, instantiation
                )
            except BaseException:  # error-boundary: save atomicity — rollback must run on ANY failure (including KeyboardInterrupt) before the original error re-raises (noqa: BLE001)
                connection.rollback()
                raise
            connection.commit()
        return result

    def _save_instantiation_in_transaction(
        self,
        connection: sqlite3.Connection,
        instantiation: ProjectTemplateInstantiation,
    ) -> ProjectTemplateInstantiation:
        existing = connection.execute(
            """
            SELECT instantiation_sha256 FROM template_instantiations
            WHERE instantiation_id = ?
            """,
            (instantiation.instantiation_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing['instantiation_sha256']
                != instantiation.instantiation_sha256
            ):
                raise ValueError(
                    'instantiation id already persisted with different content'
                )
            return instantiation
        self._validate_instantiation(connection, instantiation)
        connection.execute(
            """
            INSERT INTO template_instantiations (
                instantiation_id, document_id, template_id,
                template_sha256, created_at_utc, instantiation_sha256,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instantiation.instantiation_id,
                instantiation.document_id,
                instantiation.template_id,
                instantiation.template_sha256,
                instantiation.created_at_utc,
                instantiation.instantiation_sha256,
                json.dumps(
                    instantiation.model_dump(mode='json'),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            ),
        )
        return instantiation

    def _validate_instantiation(
        self,
        connection: sqlite3.Connection,
        instantiation: ProjectTemplateInstantiation,
    ) -> None:
        """#864: a self-consistent payload is not proof — every claim must
        resolve against the persisted/built-in authorities sharing this
        store."""

        if instantiation.instantiation_sha256 != _hash(
            instantiation.semantic_payload()
        ):
            raise ValueError(
                'instantiation_sha256 does not match the payload semantics'
            )
        template = self._lookup_template(
            connection,
            instantiation.template_id,
            instantiation.template_version,
        )
        if template is None:
            raise ValueError(
                f'unknown template {instantiation.template_id}'
                f' version {instantiation.template_version}'
            )
        if template.template_sha256 != instantiation.template_sha256:
            raise ValueError(
                'instantiation template_sha256 does not match the exact '
                'template version it claims'
            )
        if instantiation.project_id is None:
            raise ValueError(
                'instantiation lacks the Project Library project binding'
            )
        binding = connection.execute(
            'SELECT document_id FROM htdt_project_documents WHERE project_id=?',
            (instantiation.project_id,),
        ).fetchone()
        if binding is None:
            raise ValueError(
                f'project {instantiation.project_id} is not registered'
            )
        if binding['document_id'] != instantiation.document_id:
            raise ValueError(
                f'project {instantiation.project_id} is bound to document '
                f'{binding["document_id"]}, not {instantiation.document_id}'
            )
        if connection.execute(
            'SELECT 1 FROM scene_revisions WHERE document_id=? LIMIT 1',
            (instantiation.document_id,),
        ).fetchone() is None:
            raise ValueError(
                f'document {instantiation.document_id} has no initial SceneRevision'
            )
        for ref in instantiation.unresolved_refs:
            if ref not in template.target_refs:
                raise ValueError(
                    'unresolved ref '
                    f'{ref.kind}:{ref.ref_id}@{ref.version} is not a '
                    'declared target ref of the template'
                )

    @staticmethod
    def _lookup_template(
        connection: sqlite3.Connection,
        template_id: str,
        version: str,
    ) -> ProjectTemplate | None:
        row = connection.execute(
            """
            SELECT payload_json FROM project_templates
            WHERE template_id = ? AND version = ?
            """,
            (template_id, version),
        ).fetchone()
        if row is not None:
            return ProjectTemplate.model_validate_json(row['payload_json'])
        for template in builtin_project_templates():
            if (
                template.template_id == template_id
                and template.version == version
            ):
                return template
        return None

    def instantiation_for_document(
        self, document_id: str
    ) -> ProjectTemplateInstantiation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM template_instantiations
                WHERE document_id = ? ORDER BY created_at_utc DESC, instantiation_id DESC LIMIT 1
                """,
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            instantiation = ProjectTemplateInstantiation.model_validate_json(
                row['payload_json']
            )
            # Reads revalidate the same bindings (#864): a tampered row must
            # not surface as valid provenance.
            self._validate_instantiation(connection, instantiation)
        return instantiation


__all__ = [
    'CadProjectTemplateRepository',
    'ProjectTemplateConflictError',
]
