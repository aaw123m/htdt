"""Curated acoustic material library product layer (#771).

Sits on top of the physics schema authority (#465 ``AcousticMaterialAuthority``):
a *definition* (what the material is — category, construction, mounting
variant) plus separately-versioned *evidence* datasets (what we actually know —
quantity type, frequency axis, values, incidence, method, provenance class,
license).

Contract properties (per the issue):

- material identity and acoustic evidence are separate rows — one
  ``MaterialDefinition`` may have many ``MaterialAcousticEvidence`` sets, and
  each evidence set declares exactly one ``MaterialQuantity``;
- quantity capability is explicit — random-incidence absorption does not
  imply complex reflection phase, and no conversion between quantity types
  happens anywhere in this layer;
- mounting/construction context is part of the *definition*: "mineral wool
  100 mm rigid backing" and "mineral wool 100 mm + 100 mm air gap" are
  different exact variants, not one generic curve;
- every evidence row carries a provenance class and, for shared/bundled
  entries (``document_id=None``), explicit redistribution licensing — the
  library never ships data HTDT may not redistribute;
- unsupported physics stays UNKNOWN/absent — nothing is inferred to fill
  gaps.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


MATERIAL_LIBRARY_SCHEMA_VERSION = 1
MATERIAL_LIBRARY_AUTHORITY_VERSION = 'acoustic-material-library-1'






MaterialProvenanceClass = Literal[
    'manufacturer_measured',
    'laboratory_measured',
    'independent_published',
    'user_measured',
    # #1033: local measurement of an already-installed surface/treatment —
    # stronger than a generic preset for that exact surface but never a
    # global MaterialDefinition rewrite.
    'user_in_situ_measured',
    'analytic_model',
    'inferred_estimated',
    'generic_reference_preset',
]

#: The quantity an evidence set carries. Sets are never converted between
#: these types — consumers must read the declared quantity exactly.
MaterialQuantity = Literal[
    'random_incidence_absorption_coefficient',
    'normal_incidence_absorption_coefficient',
    'complex_reflection_coefficient',
    'surface_impedance',
    'surface_admittance',
    'scattering_coefficient',
    'transmission_loss',
    'porous_model_parameters',
]

#: Quantities whose values are complex-capable (phase information allowed).
_COMPLEX_CAPABLE_QUANTITIES: frozenset[str] = frozenset(
    {
        'complex_reflection_coefficient',
        'surface_impedance',
        'surface_admittance',
    }
)

MaterialCategory = Literal[
    'wall',
    'floor',
    'ceiling',
    'glazing',
    'seating',
    'curtain',
    'porous_absorber',
    'panel_absorber',
    'membrane_absorber',
    'diffuser',
    'masonry',
    'other',
]

#: Mounting/construction variant — part of the material identity.
MaterialMounting = Literal[
    'rigid_backing',
    'air_gap',
    'free_standing',
    'pleated',
    'integrated',
    'other',
    'unknown',
]

IncidenceCondition = Literal['random', 'normal', 'oblique', 'unknown']


class MaterialDefinition(BaseModel):
    """Human/product description of one material variant.

    ``mounting`` + ``mounting_detail`` make construction context part of the
    identity — a rigid-backed 100 mm absorber and the same absorber over an
    air gap are different definitions, not parameters of one entry.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = MATERIAL_LIBRARY_SCHEMA_VERSION
    authority_version: Literal['acoustic-material-library-1'] = (
        MATERIAL_LIBRARY_AUTHORITY_VERSION
    )
    material_id: str = Field(min_length=1)
    category: MaterialCategory
    name: str = Field(min_length=1)
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    thickness_mm: float | None = None
    density_kg_m3: float | None = None
    mounting: MaterialMounting = 'unknown'
    mounting_detail: str | None = Field(default=None, min_length=1)
    #: ``None`` = shared/bundled library entry; a project id scopes the entry
    #: to that project's library.
    document_id: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    material_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_material(self) -> 'MaterialDefinition':
        for value in (self.thickness_mm, self.density_kg_m3):
            if value is not None and not (isfinite(value) and value > 0):
                raise ValueError('physical dimensions must be positive')
        if self.material_sha256 != _hash(self.semantic_payload()):
            raise ValueError('MaterialDefinition hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'material_id': self.material_id,
            'category': self.category,
            'name': self.name,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'thickness_mm': self.thickness_mm,
            'density_kg_m3': self.density_kg_m3,
            'mounting': self.mounting,
            'mounting_detail': self.mounting_detail,
            'document_id': self.document_id,
            'created_at_utc': self.created_at_utc,
        }


class MaterialAcousticEvidence(BaseModel):
    """One versioned measured/derived dataset for a material.

    Carries exactly one quantity. ``phase_deg`` is only meaningful for
    complex-capable quantities and is rejected otherwise — magnitude bands
    are never upgraded to complex data by convention.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = MATERIAL_LIBRARY_SCHEMA_VERSION
    authority_version: Literal['acoustic-material-library-1'] = (
        MATERIAL_LIBRARY_AUTHORITY_VERSION
    )
    evidence_id: str = Field(min_length=1)
    material_id: str = Field(min_length=1)
    version: str = Field(min_length=1, default='1')
    quantity: MaterialQuantity
    frequency_hz: tuple[float, ...] = Field(min_length=1)
    values: tuple[float, ...] = Field(min_length=1)
    unit_label: str = Field(min_length=1)
    phase_deg: tuple[float, ...] | None = None
    incidence: IncidenceCondition = 'unknown'
    incidence_angle_deg: float | None = None
    method: str | None = Field(default=None, min_length=1)
    source_label: str | None = Field(default=None, min_length=1)
    provenance_class: MaterialProvenanceClass
    license_name: str | None = Field(default=None, min_length=1)
    license_url: str | None = Field(default=None, min_length=1)
    #: Whether HTDT may redistribute this dataset. Required True for entries
    #: in the shared/bundled library; enforced by the repository.
    redistribution_permitted: bool | None = None
    uncertainty_note: str | None = None
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evidence(self) -> 'MaterialAcousticEvidence':
        if len(self.values) != len(self.frequency_hz):
            raise ValueError(
                'evidence values must align one-to-one with frequency_hz'
            )
        for value in (*self.frequency_hz, *self.values):
            if not isfinite(value):
                raise ValueError('evidence frequency/value entries must be finite')
        if any(hz <= 0 for hz in self.frequency_hz):
            raise ValueError('evidence frequencies must be positive')
        if self.phase_deg is not None:
            if self.quantity not in _COMPLEX_CAPABLE_QUANTITIES:
                raise ValueError(
                    'phase_deg is only valid for complex-capable quantities '
                    '(complex reflection / impedance / admittance)'
                )
            if len(self.phase_deg) != len(self.frequency_hz):
                raise ValueError(
                    'phase_deg must align one-to-one with frequency_hz'
                )
            if any(not isfinite(deg) for deg in self.phase_deg):
                raise ValueError('phase entries must be finite')
        if self.incidence_angle_deg is not None:
            if not isfinite(self.incidence_angle_deg):
                raise ValueError('incidence angle must be finite')
            if self.incidence not in {'oblique', 'unknown'}:
                raise ValueError(
                    'incidence_angle_deg is only meaningful for oblique '
                    'incidence'
                )
        if self.evidence_sha256 != _hash(self.semantic_payload()):
            raise ValueError('MaterialAcousticEvidence hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'evidence_id': self.evidence_id,
            'material_id': self.material_id,
            'version': self.version,
            'quantity': self.quantity,
            'frequency_hz': list(self.frequency_hz),
            'values': list(self.values),
            'unit_label': self.unit_label,
            'phase_deg': (
                list(self.phase_deg) if self.phase_deg is not None else None
            ),
            'incidence': self.incidence,
            'incidence_angle_deg': self.incidence_angle_deg,
            'method': self.method,
            'source_label': self.source_label,
            'provenance_class': self.provenance_class,
            'license_name': self.license_name,
            'license_url': self.license_url,
            'redistribution_permitted': self.redistribution_permitted,
            'uncertainty_note': self.uncertainty_note,
            'limitations': list(self.limitations),
            'created_at_utc': self.created_at_utc,
        }


def build_material_definition(
    *,
    category: MaterialCategory,
    name: str,
    created_at_utc: str,
    manufacturer: str | None = None,
    model: str | None = None,
    thickness_mm: float | None = None,
    density_kg_m3: float | None = None,
    mounting: MaterialMounting = 'unknown',
    mounting_detail: str | None = None,
    document_id: str | None = None,
    material_id: str | None = None,
) -> MaterialDefinition:
    payload: dict[str, Any] = {
        'material_id': material_id or str(uuid4()),
        'category': category,
        'name': name,
        'manufacturer': manufacturer,
        'model': model,
        'thickness_mm': thickness_mm,
        'density_kg_m3': density_kg_m3,
        'mounting': mounting,
        'mounting_detail': mounting_detail,
        'document_id': document_id,
        'created_at_utc': created_at_utc,
    }
    provisional = MaterialDefinition.model_construct(
        **payload, material_sha256='0' * 64
    )
    return MaterialDefinition(
        **payload, material_sha256=_hash(provisional.semantic_payload())
    )


def build_material_evidence(
    *,
    material_id: str,
    quantity: MaterialQuantity,
    frequency_hz: tuple[float, ...],
    values: tuple[float, ...],
    unit_label: str,
    provenance_class: MaterialProvenanceClass,
    created_at_utc: str,
    version: str = '1',
    phase_deg: tuple[float, ...] | None = None,
    incidence: IncidenceCondition = 'unknown',
    incidence_angle_deg: float | None = None,
    method: str | None = None,
    source_label: str | None = None,
    license_name: str | None = None,
    license_url: str | None = None,
    redistribution_permitted: bool | None = None,
    uncertainty_note: str | None = None,
    limitations: tuple[str, ...] = (),
    evidence_id: str | None = None,
) -> MaterialAcousticEvidence:
    payload: dict[str, Any] = {
        'evidence_id': evidence_id or str(uuid4()),
        'material_id': material_id,
        'version': version,
        'quantity': quantity,
        'frequency_hz': tuple(frequency_hz),
        'values': tuple(values),
        'unit_label': unit_label,
        'phase_deg': tuple(phase_deg) if phase_deg is not None else None,
        'incidence': incidence,
        'incidence_angle_deg': incidence_angle_deg,
        'method': method,
        'source_label': source_label,
        'provenance_class': provenance_class,
        'license_name': license_name,
        'license_url': license_url,
        'redistribution_permitted': redistribution_permitted,
        'uncertainty_note': uncertainty_note,
        'limitations': tuple(limitations),
        'created_at_utc': created_at_utc,
    }
    provisional = MaterialAcousticEvidence.model_construct(
        **payload, evidence_sha256='0' * 64
    )
    return MaterialAcousticEvidence(
        **payload, evidence_sha256=_hash(provisional.semantic_payload())
    )


# ----------------------------------------------------------------------
# Bundled reference library
#
# These entries are deliberately small and *honestly generic*: every one is
# provenance class ``generic_reference_preset`` or ``analytic_model`` with
# an HTDT-authored license (redistributable) and explicit limitations. They
# exist so a normal user can model a common room without external data —
# they are never presented as measured authority for a specific installed
# construction.

_BUILTIN_CREATED = '2026-09-24T00:00:00+00:00'
_BUILTIN_LICENSE = 'HTDT generic reference (redistributable)'
_BUILTIN_LIMITATION = (
    'Generic/analytic reference values — not a measured description of any '
    'specific installed construction.'
)

_OCTAVE_BANDS = (125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0)


def _builtin_absorber(
    *,
    material_id: str,
    evidence_id: str,
    category: MaterialCategory,
    name: str,
    mounting: MaterialMounting,
    mounting_detail: str | None,
    values: tuple[float, ...],
    provenance_class: MaterialProvenanceClass,
    method: str,
    uncertainty_note: str | None = None,
) -> tuple[MaterialDefinition, MaterialAcousticEvidence]:
    definition = build_material_definition(
        material_id=material_id,
        category=category,
        name=name,
        mounting=mounting,
        mounting_detail=mounting_detail,
        created_at_utc=_BUILTIN_CREATED,
    )
    evidence = build_material_evidence(
        evidence_id=evidence_id,
        material_id=material_id,
        quantity='random_incidence_absorption_coefficient',
        frequency_hz=_OCTAVE_BANDS,
        values=values,
        unit_label='alpha (0-1)',
        incidence='random',
        method=method,
        provenance_class=provenance_class,
        license_name=_BUILTIN_LICENSE,
        redistribution_permitted=True,
        uncertainty_note=uncertainty_note,
        limitations=(_BUILTIN_LIMITATION,),
        created_at_utc=_BUILTIN_CREATED,
    )
    return definition, evidence


#: (definition, primary evidence) pairs shipped with the application.
BUILTIN_MATERIAL_LIBRARY: tuple[
    tuple[MaterialDefinition, MaterialAcousticEvidence], ...
] = (
    _builtin_absorber(
        material_id='builtin-gypsum-rigid',
        evidence_id='builtin-gypsum-rigid-random-alpha',
        category='wall',
        name='Painted gypsum board (rigid backing)',
        mounting='rigid_backing',
        mounting_detail='12.5 mm board on studs, painted',
        values=(0.29, 0.10, 0.05, 0.04, 0.07, 0.09),
        provenance_class='generic_reference_preset',
        method='generic textbook reference values',
        uncertainty_note='±0.1 per band; actual cavities vary strongly',
    ),
    _builtin_absorber(
        material_id='builtin-concrete-rigid',
        evidence_id='builtin-concrete-rigid-random-alpha',
        category='masonry',
        name='Concrete / masonry (unpainted)',
        mounting='integrated',
        mounting_detail='solid concrete or block, unpainted',
        values=(0.01, 0.01, 0.02, 0.02, 0.02, 0.03),
        provenance_class='generic_reference_preset',
        method='generic textbook reference values',
    ),
    _builtin_absorber(
        material_id='builtin-mineral-wool-100-rigid',
        evidence_id='builtin-mineral-wool-100-rigid-random-alpha',
        category='porous_absorber',
        name='Mineral wool 100 mm (rigid backing)',
        mounting='rigid_backing',
        mounting_detail='100 mm porous absorber directly on rigid wall',
        values=(0.30, 0.70, 0.90, 0.95, 0.95, 0.95),
        provenance_class='analytic_model',
        method='analytic porous-absorber estimate, rigid backing',
        uncertainty_note='analytic estimate; flow-resistivity dependent',
    ),
    _builtin_absorber(
        material_id='builtin-curtain-pleated-airgap',
        evidence_id='builtin-curtain-pleated-airgap-random-alpha',
        category='curtain',
        name='Heavy curtain (pleated, air gap)',
        mounting='pleated',
        mounting_detail='heavy fabric, ~50% fullness, off wall',
        values=(0.14, 0.35, 0.55, 0.70, 0.70, 0.65),
        provenance_class='generic_reference_preset',
        method='generic textbook reference values',
        uncertainty_note='fullness and gap change values substantially',
    ),
)

BUILTIN_MATERIAL_IDS: frozenset[str] = frozenset(
    material.material_id for material, _ in BUILTIN_MATERIAL_LIBRARY
)


__all__ = [
    'BUILTIN_MATERIAL_IDS',
    'BUILTIN_MATERIAL_LIBRARY',
    'IncidenceCondition',
    'MATERIAL_LIBRARY_AUTHORITY_VERSION',
    'MATERIAL_LIBRARY_SCHEMA_VERSION',
    'MaterialAcousticEvidence',
    'MaterialCategory',
    'MaterialDefinition',
    'MaterialMounting',
    'MaterialProvenanceClass',
    'MaterialQuantity',
    'build_material_definition',
    'build_material_evidence',
]
