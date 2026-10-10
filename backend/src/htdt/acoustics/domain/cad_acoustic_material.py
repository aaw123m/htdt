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

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from ...canonical_json import canonical_sha256 as _hash
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

__all__ = [
    'AcousticMaterialAuthority',
    'BoundaryMaterialState',
    '',
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
