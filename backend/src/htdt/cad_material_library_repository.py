"""Append-only persistence for the curated material library (#771)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_material_library import (
    BUILTIN_MATERIAL_LIBRARY,
    MaterialAcousticEvidence,
    MaterialDefinition,
)
from .cad_repository import SceneRepository


class MaterialLibraryConflictError(ValueError):
    """A material/evidence save violated append-only identity rules."""


class CadMaterialLibraryRepository:
    """Native storage for MaterialDefinition + MaterialAcousticEvidence.

    Definitions are append-only per ``material_id``; evidence is append-only
    per ``(evidence_id)`` and versioned per ``(material_id, version)``.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_material_definitions (
                    material_id TEXT PRIMARY KEY,
                    document_id TEXT,
                    material_sha256 TEXT NOT NULL UNIQUE,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_material_evidence (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    material_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL UNIQUE,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE (material_id, version, quantity)
                )
                """
            )

    # ------------------------------------------------------------------
    # Definitions

    def save_material(self, material: MaterialDefinition) -> None:
        if self.get_material(material.material_id) is not None:
            raise MaterialLibraryConflictError(
                'MaterialDefinition ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_material_definitions (
                    material_id, document_id, material_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    material.material_id,
                    material.document_id,
                    material.material_sha256,
                    material.created_at_utc,
                    material.model_dump_json(),
                ),
            )

    def get_material(self, material_id: str) -> MaterialDefinition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_material_definitions WHERE material_id=?',
                (material_id,),
            ).fetchone()
        if row is None:
            return None
        return MaterialDefinition.model_validate_json(row['payload_json'])

    def list_materials(
        self, document_id: str | None = None
    ) -> tuple[MaterialDefinition, ...]:
        """Shared/bundled entries plus (optionally) one project's entries."""
        with closing(self._connect()) as connection:
            if document_id is None:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM cad_material_definitions
                    WHERE document_id IS NULL ORDER BY created_at_utc, material_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM cad_material_definitions
                    WHERE document_id IS NULL OR document_id=?
                    ORDER BY created_at_utc, material_id
                    """,
                    (document_id,),
                ).fetchall()
        return tuple(
            MaterialDefinition.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Evidence

    def save_evidence(self, evidence: MaterialAcousticEvidence) -> None:
        material = self.get_material(evidence.material_id)
        if material is None:
            raise ValueError(
                'evidence references a MaterialDefinition that is not persisted'
            )
        if material.document_id is None and not evidence.redistribution_permitted:
            raise ValueError(
                'shared/bundled library evidence must carry explicit '
                'redistribution licensing (redistribution_permitted=True)'
            )
        for existing in self.list_evidence(evidence.material_id):
            if (
                existing.version == evidence.version
                and existing.quantity == evidence.quantity
            ):
                raise MaterialLibraryConflictError(
                    'MaterialAcousticEvidence (material_id, version, quantity) '
                    'is append-only'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_material_evidence (
                    evidence_id, material_id, version, quantity,
                    evidence_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.material_id,
                    evidence.version,
                    evidence.quantity,
                    evidence.evidence_sha256,
                    evidence.created_at_utc,
                    evidence.model_dump_json(),
                ),
            )

    def get_evidence(self, evidence_id: str) -> MaterialAcousticEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_material_evidence WHERE evidence_id=?',
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return MaterialAcousticEvidence.model_validate_json(row['payload_json'])

    def get_evidence_by_hash(
        self, evidence_sha256: str
    ) -> MaterialAcousticEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_material_evidence WHERE evidence_sha256=?',
                (evidence_sha256,),
            ).fetchone()
        if row is None:
            return None
        return MaterialAcousticEvidence.model_validate_json(row['payload_json'])

    def list_evidence(
        self, material_id: str, quantity: str | None = None
    ) -> tuple[MaterialAcousticEvidence, ...]:
        with closing(self._connect()) as connection:
            if quantity is None:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM cad_material_evidence
                    WHERE material_id=? ORDER BY created_at_utc, evidence_id
                    """,
                    (material_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM cad_material_evidence
                    WHERE material_id=? AND quantity=?
                    ORDER BY created_at_utc, evidence_id
                    """,
                    (material_id, quantity),
                ).fetchall()
        return tuple(
            MaterialAcousticEvidence.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Bundled library

    def install_builtin_library(self) -> int:
        """Seed bundled entries idempotently. Returns count newly added."""
        installed = 0
        for material, evidence in BUILTIN_MATERIAL_LIBRARY:
            if self.get_material(material.material_id) is None:
                self.save_material(material)
                installed += 1
            if self.get_evidence(evidence.evidence_id) is None:
                self.save_evidence(evidence)
        return installed
