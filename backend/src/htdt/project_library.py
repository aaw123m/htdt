"""Native project/document library records (#450).

One data directory may hold several independent HTDT projects. The
``htdt_project_documents`` table is the user-facing library authority:
``project_id`` (UUIDv4) is the stable semantic identity, ``document_id``
binds the project to its document-scoped data, and ``display_name`` is
presentation only — never identity. Provenance fields
(``cloned_from_project_id`` / ``source_revision_id``) record explicit
duplication lineage.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
PROJECT_LIBRARY_SCHEMA = 'htdt.project-library-entry'
PROJECT_LIBRARY_SCHEMA_VERSION = '1.0.0'


class ProjectLibraryError(ValueError):
    """Project library operation could not be completed safely."""


class ProjectNotFoundError(ProjectLibraryError):
    """Referenced project is not in the library."""


class ProjectArchivedError(ProjectLibraryError):
    """Operation requires a project that is not archived."""


class ProjectLibraryEntry(BaseModel):
    """One user-facing project bound to one ``document_id``."""

    model_config = ConfigDict(frozen=True, extra='forbid', serialize_by_alias=True)

    schema_: Literal['htdt.project-library-entry'] = Field(default=PROJECT_LIBRARY_SCHEMA, alias='schema')
    schema_version: Literal['1.0.0'] = PROJECT_LIBRARY_SCHEMA_VERSION
    project_id: str
    document_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str | None = None
    created_at_utc: str = Field(min_length=1)
    last_opened_at_utc: str | None = None
    archived: bool = False
    cloned_from_project_id: str | None = None
    source_revision_id: str | None = None

    @field_validator('project_id', 'cloned_from_project_id')
    @classmethod
    def validate_uuid4(cls, value: str | None) -> str | None:
        if value is not None and not UUID4_RE.fullmatch(value):
            raise ValueError('project identity must be a lowercase UUIDv4')
        return value
