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
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_benchmark import (
    AcousticMaterial,
    GeometricAcousticBand,
    SpecificImpedancePoint,
)
from .r120_geometry_compiler import (
    ExactExternalAuthorityRef,
    SurfaceBoundaryAuthorityBinding,
)
from .semantic_geometry import SemanticSurface
from hashlib import sha256


_MATERIAL_PREFIX = 'acoustic-material:'

WAVE_MODEL_LABELS: dict[str, str] = {
    'rigid': '剛性（完全反射）',
    'specific_impedance_table': '比インピーダンス表',
    'unsupported': 'wave物理なし（非対応）',
}
GEOMETRIC_MODEL_LABELS: dict[str, str] = {
    'banded': 'バンド吸音/散乱',
    'unsupported': 'geometric物理なし（非対応）',
}
SURFACE_CLASS_LABELS: dict[str, str] = {
    'room_boundary': '部屋境界（壁・床・天井）',
    'object_surface': 'オブジェクト表面',
    'unknown': '不明',
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


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
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_acoustic_materials (
                    material_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_surface_material_assignments (
                    document_id TEXT NOT NULL,
                    source_surface_id TEXT NOT NULL,
                    material_id TEXT NOT NULL,
                    material_sha256 TEXT NOT NULL,
                    PRIMARY KEY (document_id, source_surface_id)
                )
                """
            )

    def save_material(self, material: AcousticMaterialAuthority) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_acoustic_materials(material_id, payload_json)'
                ' VALUES(?,?)'
                ' ON CONFLICT(material_id) DO UPDATE SET'
                ' payload_json=excluded.payload_json',
                (material.material_id, material.model_dump_json()),
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
    ) -> None:
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
    'CadAcousticMaterialRepository',
    'GEOMETRIC_MODEL_LABELS',
    'SURFACE_CLASS_LABELS',
    'WAVE_MODEL_LABELS',
    'build_acoustic_material',
    'material_capability_label',
    'surface_class_label',
]
