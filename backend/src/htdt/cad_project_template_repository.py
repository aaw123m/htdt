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

from .cad_project_template import (
    ProjectTemplate,
    ProjectTemplateInstantiation,
    builtin_project_templates,
)


class ProjectTemplateConflictError(ValueError):
    """A template id+version already exists with different content."""


class CadProjectTemplateRepository:
    """SQLite store for user templates; built-ins live in code."""

    def __init__(self, scene_repository) -> None:
        self.path = Path(scene_repository.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_templates (
                template_id TEXT NOT NULL,
                version TEXT NOT NULL,
                kind TEXT NOT NULL,
                template_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (template_id, version)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS template_instantiations (
                instantiation_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                template_id TEXT NOT NULL,
                template_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                instantiation_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        return connection

    def save_template(self, template: ProjectTemplate) -> ProjectTemplate:
        if template.kind == 'builtin':
            raise ValueError('built-in templates are read-only')
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
                            template.model_dump(mode='json'), ensure_ascii=False
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
        self, instantiation: ProjectTemplateInstantiation
    ) -> ProjectTemplateInstantiation:
        with closing(self._connect()) as connection:
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
            with connection:
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
                        ),
                    ),
                )
        return instantiation

    def instantiation_for_document(
        self, document_id: str
    ) -> ProjectTemplateInstantiation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM template_instantiations
                WHERE document_id = ? ORDER BY created_at_utc DESC LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return ProjectTemplateInstantiation.model_validate_json(row['payload_json'])


__all__ = [
    'CadProjectTemplateRepository',
    'ProjectTemplateConflictError',
]
