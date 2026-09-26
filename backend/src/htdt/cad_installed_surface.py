"""In-situ installed-surface acoustic evidence (#1033).

``InstalledSurfaceAcousticMeasurement`` captures a *local* measurement of an
already-installed wall/treatment/surface — a different evidence class from
library data (#771), construction-physics derivation (#790), or whole-room
inverse calibration (#522). It answers "what did this installed surface
exhibit under this exact local method at this exact patch?" — never "what
universal material truth explains the room".

First implementation is import-only: methods like impedance-tube imports
(ISO 10534-2), two-microphone in-situ, or external reports arrive as
already-derived quantities with their method identity intact. No native
estimator exists yet and none is implied.

Promotion produces a ``MaterialAcousticEvidence`` with provenance
``user_in_situ_measured`` — bound to a caller-chosen (project-local)
material_id, preserving method/incidence/patch semantics. Promotion never
rewrites a shared ``MaterialDefinition`` and never converts incidence or
quantity semantics (normal-incidence tube data stays normal-incidence).
"""

from __future__ import annotations

from hashlib import sha256
from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_material_library import (
    IncidenceCondition,
    MaterialAcousticEvidence,
    MaterialQuantity,
    build_material_evidence,
)
from .cad_measurements import canonical_json
from .cad_scene import Position3


def _hash(payload: Any) -> str:
    return sha256(canonical_json(payload).encode('utf-8')).hexdigest()


#: Local surface-measurement method families (#1033 §3). Method identity is
#: mandatory — these are never combined under a generic "measured" flag.
InstalledSurfaceMethod = Literal[
    'impedance_tube_import',
    'two_microphone_in_situ',
    'p_u_probe',
    'double_layer_array_sonah',
    'ir_reflection_separation',
    'ensemble_averaging',
    'external_report_import',
    'custom_validated',
    'unknown',
]

#: Incidence semantics of the local measurement — preserved, never silently
#: converted (#1033 §6).
InSituIncidence = Literal[
    'normal',
    'exact_angle',
    'field_or_diffuse_like',
    'method_derived_effective',
    'unknown',
]

#: Where on the surface the patch sits — multiple patches of one surface
#: stay separate evidence until an explicit aggregation policy exists.
PatchLocation = Literal['center', 'edge', 'seam', 'bay', 'other']

#: Method-specific validity of the patch measurement. Nearby-reflection and
#: finite-patch contamination can only ever downgrade a result.
InSituQuality = Literal['pass', 'limited', 'invalid', 'unknown']

#: Quantities an in-situ method may legitimately report. Phase is only
#: carried by complex-capable quantities.
InSituQuantity = Literal[
    'surface_impedance',
    'surface_admittance',
    'complex_reflection_coefficient',
    'normal_incidence_absorption_coefficient',
    'oblique_absorption_coefficient',
]

_COMPLEX_QUANTITIES = frozenset(
    {'surface_impedance', 'surface_admittance', 'complex_reflection_coefficient'}
)


class MeasuredSurfaceQuantity(BaseModel):
    """One method-derived quantity of the installed patch."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    quantity: InSituQuantity
    frequency_hz: tuple[float, ...] = Field(min_length=1)
    values: tuple[float, ...] = Field(min_length=1)
    unit_label: str = Field(min_length=1)
    phase_deg: tuple[float, ...] | None = None

    @model_validator(mode='after')
    def valid_quantity(self) -> 'MeasuredSurfaceQuantity':
        if len(self.values) != len(self.frequency_hz):
            raise ValueError('values must align one-to-one with frequency_hz')
        if any(hz <= 0.0 or not isfinite(hz) for hz in self.frequency_hz):
            raise ValueError('frequencies must be positive and finite')
        if any(not isfinite(value) for value in self.values):
            raise ValueError('values must be finite')
        if self.phase_deg is not None:
            if self.quantity not in _COMPLEX_QUANTITIES:
                raise ValueError(
                    'phase_deg is only valid for complex-capable quantities'
                )
            if len(self.phase_deg) != len(self.frequency_hz):
                raise ValueError('phase_deg must align with frequency_hz')
            if any(not isfinite(deg) for deg in self.phase_deg):
                raise ValueError('phase values must be finite')
        return self


class InstalledSurfaceAcousticMeasurement(BaseModel):
    """Local in-situ evidence bound to an exact surface patch (#1033).

    Identity pins the exact SceneRevision, the surface/treatment instance,
    the patch location/extent, the method kind+version, the installed state
    (air gap, backing, mounting), and the measured quantities. ``quality``
    may only ever downgrade applicability — a contaminated or finite-patch
    result is ``limited``/``invalid``, never silently promoted.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_id: str = Field(min_length=1)
    authority_version: Literal['installed_surface_v1'] = 'installed_surface_v1'
    # Exact scene binding.
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    # Exact installed subject + patch.
    surface_id: str = Field(min_length=1)
    treatment_id: str | None = None
    patch_center: Position3 | None = None
    patch_extent_m: tuple[float, float] | None = None
    patch_location: PatchLocation = 'other'
    # Method identity is part of the measurement identity.
    method: InstalledSurfaceMethod
    method_version: str = Field(min_length=1)
    calibration_state: Literal[
        'calibrated', 'uncalibrated', 'unknown'
    ] = 'unknown'
    incidence: InSituIncidence = 'unknown'
    incidence_angle_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    # Installed state is the whole point — retained, not detached.
    air_gap_mm: float | None = Field(default=None, ge=0.0)
    backing: str | None = None
    mounting_detail: str | None = None
    orientation_deg: float | None = Field(default=None, ge=-360.0, le=360.0)
    # Source/receiver geometry as free-form method metadata plus a required
    # machine-readable summary string.
    source_geometry: str = ''
    receiver_geometry: str = ''
    quantities: tuple[MeasuredSurfaceQuantity, ...] = ()
    raw_asset_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    valid_frequency_range_hz: tuple[float, float] | None = None
    quality: InSituQuality = 'unknown'
    contamination_note: str | None = None
    uncertainty_note: str | None = None
    limitations: tuple[str, ...] = ()
    provenance: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_measurement(self) -> 'InstalledSurfaceAcousticMeasurement':
        if self.incidence == 'exact_angle':
            if self.incidence_angle_deg is None:
                raise ValueError(
                    'exact_angle incidence requires incidence_angle_deg'
                )
        elif self.incidence_angle_deg is not None:
            raise ValueError(
                'incidence_angle_deg is only meaningful for exact_angle'
            )
        if self.patch_extent_m is not None:
            width, height = self.patch_extent_m
            if not all(
                isfinite(value) and value > 0.0
                for value in (width, height)
            ):
                raise ValueError('patch extent must be positive and finite')
        if self.valid_frequency_range_hz is not None:
            lo, hi = self.valid_frequency_range_hz
            if not (isfinite(lo) and isfinite(hi)) or not 0.0 < lo <= hi:
                raise ValueError(
                    'valid_frequency_range_hz must be positive and ordered'
                )
        if self.quality == 'invalid' and self.quantities:
            raise ValueError(
                'an invalid measurement must not carry derived quantities'
            )
        if self.quality != 'pass' and not (
            self.contamination_note or self.limitations
        ):
            raise ValueError(
                'non-pass quality requires a contamination note or '
                'explicit limitations'
            )
        if self.measurement_sha256 != _hash(self.identity_payload()):
            raise ValueError('installed surface measurement hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'measurement_id': self.measurement_id,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'surface_id': self.surface_id,
            'treatment_id': self.treatment_id,
            'patch_center': (
                None
                if self.patch_center is None
                else self.patch_center.model_dump(mode='json')
            ),
            'patch_extent_m': (
                None
                if self.patch_extent_m is None
                else list(self.patch_extent_m)
            ),
            'patch_location': self.patch_location,
            'method': self.method,
            'method_version': self.method_version,
            'calibration_state': self.calibration_state,
            'incidence': self.incidence,
            'incidence_angle_deg': self.incidence_angle_deg,
            'air_gap_mm': self.air_gap_mm,
            'backing': self.backing,
            'mounting_detail': self.mounting_detail,
            'orientation_deg': self.orientation_deg,
            'source_geometry': self.source_geometry,
            'receiver_geometry': self.receiver_geometry,
            'quantities': [
                item.model_dump(mode='json') for item in self.quantities
            ],
            'raw_asset_sha256': self.raw_asset_sha256,
            'valid_frequency_range_hz': (
                None
                if self.valid_frequency_range_hz is None
                else list(self.valid_frequency_range_hz)
            ),
            'quality': self.quality,
            'contamination_note': self.contamination_note,
            'uncertainty_note': self.uncertainty_note,
            'limitations': list(self.limitations),
            'provenance': self.provenance,
            'created_at_utc': self.created_at_utc,
        }


def build_installed_surface_measurement(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    surface_id: str,
    method: InstalledSurfaceMethod,
    method_version: str,
    provenance: str,
    created_at_utc: str,
    treatment_id: str | None = None,
    patch_center: Position3 | None = None,
    patch_extent_m: tuple[float, float] | None = None,
    patch_location: PatchLocation = 'other',
    calibration_state: Literal[
        'calibrated', 'uncalibrated', 'unknown'
    ] = 'unknown',
    incidence: InSituIncidence = 'unknown',
    incidence_angle_deg: float | None = None,
    air_gap_mm: float | None = None,
    backing: str | None = None,
    mounting_detail: str | None = None,
    orientation_deg: float | None = None,
    source_geometry: str = '',
    receiver_geometry: str = '',
    quantities: tuple[MeasuredSurfaceQuantity, ...] = (),
    raw_asset_sha256: str | None = None,
    valid_frequency_range_hz: tuple[float, float] | None = None,
    quality: InSituQuality = 'unknown',
    contamination_note: str | None = None,
    uncertainty_note: str | None = None,
    limitations: tuple[str, ...] = (),
    measurement_id: str | None = None,
) -> InstalledSurfaceAcousticMeasurement:
    payload: dict[str, Any] = {
        'measurement_id': measurement_id or str(uuid4()),
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'surface_id': surface_id,
        'treatment_id': treatment_id,
        'patch_center': patch_center,
        'patch_extent_m': patch_extent_m,
        'patch_location': patch_location,
        'method': method,
        'method_version': method_version,
        'calibration_state': calibration_state,
        'incidence': incidence,
        'incidence_angle_deg': incidence_angle_deg,
        'air_gap_mm': air_gap_mm,
        'backing': backing,
        'mounting_detail': mounting_detail,
        'orientation_deg': orientation_deg,
        'source_geometry': source_geometry,
        'receiver_geometry': receiver_geometry,
        'quantities': tuple(quantities),
        'raw_asset_sha256': raw_asset_sha256,
        'valid_frequency_range_hz': valid_frequency_range_hz,
        'quality': quality,
        'contamination_note': contamination_note,
        'uncertainty_note': uncertainty_note,
        'limitations': tuple(limitations),
        'provenance': provenance,
        'created_at_utc': created_at_utc,
    }
    provisional = InstalledSurfaceAcousticMeasurement.model_construct(
        **payload, measurement_sha256='0' * 64
    )
    return InstalledSurfaceAcousticMeasurement(
        **payload, measurement_sha256=_hash(provisional.identity_payload())
    )


#: How each in-situ incidence maps onto the material library's incidence
#: vocabulary. 'field_or_diffuse_like' is NOT random incidence — it stays
#: unknown there rather than being upgraded.
_INCIDENCE_MAP: dict[str, tuple[IncidenceCondition, bool]] = {
    'normal': ('normal', False),
    'exact_angle': ('oblique', True),
    'field_or_diffuse_like': ('unknown', False),
    'method_derived_effective': ('unknown', False),
    'unknown': ('unknown', False),
}

#: In-situ quantities -> MaterialQuantity. There is deliberately no entry
#: for 'oblique_absorption_coefficient': the material vocabulary cannot
#: express oblique incidence in the quantity name and relabelling it
#: 'random_incidence' or 'normal_incidence' would be a silent conversion —
#: promotion of that quantity raises instead (#1033 §2/§6).
_QUANTITY_MAP: dict[str, MaterialQuantity] = {
    'surface_impedance': 'surface_impedance',
    'surface_admittance': 'surface_admittance',
    'complex_reflection_coefficient': 'complex_reflection_coefficient',
    'normal_incidence_absorption_coefficient': (
        'normal_incidence_absorption_coefficient'
    ),
}


def promote_installed_measurement(
    measurement: InstalledSurfaceAcousticMeasurement,
    quantity: MeasuredSurfaceQuantity,
    *,
    material_id: str,
    created_at_utc: str,
    version: str = '1',
    evidence_id: str | None = None,
) -> MaterialAcousticEvidence:
    """Promote one patch quantity to ``MaterialAcousticEvidence``.

    The evidence gets provenance ``user_in_situ_measured`` and carries the
    exact method/incidence/quality of the local measurement — bound to the
    caller's ``material_id`` (a project-local variant). It never rewrites a
    shared ``MaterialDefinition``: this function only *returns* an evidence
    row and performs no repository writeback (#1033 §11-12).

    Only ``pass``/``limited`` measurements with the quantity actually
    present may promote; ``invalid``/``unknown`` raise. Incidence semantics
    are preserved — normal-incidence stays normal, a diffuse-like field
    estimate stays unknown-incidence, never relabelled random.
    """

    if measurement.quality == 'invalid':
        raise ValueError('invalid measurements cannot promote to evidence')
    if measurement.quality == 'unknown':
        raise ValueError(
            'unknown-quality measurements cannot promote to evidence'
        )
    if quantity not in measurement.quantities:
        raise ValueError('promoted quantity must come from the measurement')
    if quantity.quantity not in _QUANTITY_MAP:
        raise ValueError(
            f'quantity {quantity.quantity!r} cannot promote: the material '
            'vocabulary has no non-silent mapping for it'
        )
    incidence, has_angle = _INCIDENCE_MAP[measurement.incidence]
    limitations = list(measurement.limitations)
    if measurement.quality == 'limited':
        limitations.append('limited by in-situ contamination assessment')
    if measurement.patch_location != 'other' or measurement.patch_extent_m:
        limitations.append(
            f'local patch evidence: {measurement.patch_location}'
            + (
                f' {measurement.patch_extent_m[0]}x'
                f'{measurement.patch_extent_m[1]} m'
                if measurement.patch_extent_m
                else ''
            )
        )
    if measurement.method == 'impedance_tube_import':
        limitations.append(
            'ISO 10534-2 normal-incidence tube semantics — not comparable '
            'with random-incidence ISO 354 data'
        )
    return build_material_evidence(
        material_id=material_id,
        quantity=_QUANTITY_MAP[quantity.quantity],
        frequency_hz=quantity.frequency_hz,
        values=quantity.values,
        unit_label=quantity.unit_label,
        phase_deg=quantity.phase_deg,
        incidence=incidence,
        incidence_angle_deg=(
            measurement.incidence_angle_deg if has_angle else None
        ),
        method=f'{measurement.method}:{measurement.method_version}',
        source_label=f'in-situ:{measurement.measurement_id}',
        provenance_class='user_in_situ_measured',
        uncertainty_note=measurement.uncertainty_note,
        limitations=tuple(limitations),
        created_at_utc=created_at_utc,
        version=version,
        evidence_id=evidence_id,
    )
