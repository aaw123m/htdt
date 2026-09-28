"""Treatment fabrication packages (#791 FAB10 + FAB20).

A ``TreatmentFabricationPackage`` derives *buildable* output — cut
parts, grouped cut lists, well-depth tables, tolerances and drawing
sheet descriptors — from an exact ``AcousticTreatmentDefinition`` plus
an explicit ``FabricationSpec``. Fabrication output is derived
geometry: it is not the acoustic model and it is not measured finished
performance. Packages are immutable; a changed treatment or spec
produces a new package (with an optional ``supersedes`` link), never a
rewritten issued plan.

Supported first slices:

- FAB10 — rectangular porous panels/traps: exact overall dimensions,
  per-layer cut parts, air gap note, panel count, grouped cut list.
- FAB20 — explicit 1D Schroeder QRD: odd-prime sequence, exact
  well-depth table from design wavelength, fins/backing cut groups,
  grouped cut quantities and tolerance callouts.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_treatment import AcousticTreatmentDefinition
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload


_PACKAGE_PREFIX = 'treatment-fabrication:'
_RENDERER_ID = 'htdt-fabrication'
_RENDERER_VERSION = '1'

_FAB10_TYPES = frozenset(
    {'porous_absorber', 'absorber_with_air_gap', 'bass_trap'}
)
_FAB20_TYPES = frozenset({'diffuser_scattering_element'})






def _is_prime(value: int) -> bool:
    if value < 2:
        return False
    for divisor in range(2, int(value**0.5) + 1):
        if value % divisor == 0:
            return False
    return True


class FabricationToleranceProfile(BaseModel):
    """Bounded per-dimension tolerances (mm). ``None`` means UNKNOWN —
    tolerances are never invented globally."""

    model_config = ConfigDict(frozen=True)

    overall_dimension_mm: float | None = Field(default=None, ge=0.0)
    well_depth_mm: float | None = Field(default=None, ge=0.0)
    fin_thickness_mm: float | None = Field(default=None, ge=0.0)
    spacing_mm: float | None = Field(default=None, ge=0.0)
    air_gap_mm: float | None = Field(default=None, ge=0.0)


class FabricationSpec(BaseModel):
    """Explicit fabrication inputs: panel count, kerf, tolerances, stock
    assumption — every value the package depends on is pinned here."""

    model_config = ConfigDict(frozen=True)

    spec_version: str = Field(min_length=1)
    panel_count: int = Field(default=1, ge=1)
    kerf_mm: float = Field(default=0.0, ge=0.0)
    tolerances: FabricationToleranceProfile = Field(
        default_factory=FabricationToleranceProfile
    )
    stock_sheet_width_m: float | None = Field(default=None, gt=0.0)
    stock_sheet_height_m: float | None = Field(default=None, gt=0.0)
    material_ref: str | None = None
    supersedes_package_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )


class FabricationPart(BaseModel):
    """One part kind with finished dimensions and exact quantity."""

    model_config = ConfigDict(frozen=True)

    part_id: str = Field(min_length=1)
    part_kind: Literal[
        'absorber_layer',
        'backing_panel',
        'well_fin',
        'frame_member',
        'facing',
        'spacer',
    ]
    description: str = Field(min_length=1)
    finished_width_m: float = Field(gt=0.0)
    finished_height_m: float = Field(gt=0.0)
    finished_depth_m: float = Field(gt=0.0)
    quantity: int = Field(ge=1)
    material_ref: str | None = None
    cut_group: str = Field(min_length=1)
    notes: str = ''

    @model_validator(mode='after')
    def finite_part(self) -> 'FabricationPart':
        for value in (
            self.finished_width_m,
            self.finished_height_m,
            self.finished_depth_m,
        ):
            if not isfinite(float(value)):
                raise ValueError('part dimensions must be finite')
        return self


class CutListEntry(BaseModel):
    """Grouped cut quantity — identical parts share a ``cut_group`` so
    lists stay exact and readable (#791 §6/§7)."""

    model_config = ConfigDict(frozen=True)

    cut_group: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dimensions_m: tuple[float, float, float]
    quantity: int = Field(ge=1)
    material_ref: str | None = None
    tolerance_mm: float | None = Field(default=None, ge=0.0)


class QrdWellRow(BaseModel):
    """One well of a numbered QRD depth table."""

    model_config = ConfigDict(frozen=True)

    well_index: int = Field(ge=0)
    period_index: int = Field(ge=0)
    residue: int = Field(ge=0)
    depth_m: float = Field(ge=0.0)


class BomFragment(BaseModel):
    """A deterministic material takeoff line for #639 — raw stock,
    fabricated part or installed assembly. Stock estimates are marked
    derived; they are never installed truth."""

    model_config = ConfigDict(frozen=True)

    line_kind: Literal['stock_estimate', 'cut_part', 'assembly']
    description: str = Field(min_length=1)
    quantity: float = Field(gt=0.0)
    unit: str = Field(min_length=1)
    derived: bool = True


class TreatmentFabricationPackage(BaseModel):
    """Immutable fabrication package bound to an exact treatment
    definition + fabrication spec (#791). Package hash covers semantic
    content; issued packages supersede, never rewrite."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    package_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    treatment_type: str = Field(min_length=1)
    fabrication_family: Literal['rectangular_panel', 'qrd_1d']
    spec_version: str = Field(min_length=1)
    renderer_id: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)
    kerf_mm: float = Field(ge=0.0)
    tolerances: FabricationToleranceProfile
    panel_count: int = Field(ge=1)
    assembly_orientation: str = ''
    install_reference: str = ''
    parts: tuple[FabricationPart, ...]
    cut_list: tuple[CutListEntry, ...]
    bom_fragments: tuple[BomFragment, ...]
    well_table: tuple[QrdWellRow, ...] = ()
    overall_width_m: float = Field(gt=0.0)
    overall_height_m: float = Field(gt=0.0)
    overall_depth_m: float = Field(gt=0.0)
    drawing_sheets: tuple[str, ...]
    warnings: tuple[str, ...]
    supersedes_package_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    created_at_utc: str = Field(min_length=1)
    package_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_package(self) -> 'TreatmentFabricationPackage':
        if not self.package_id.startswith(_PACKAGE_PREFIX):
            raise ValueError(
                'package id must use treatment-fabrication: prefix'
            )
        if not self.parts:
            raise ValueError('package requires at least one part')
        if not self.cut_list:
            raise ValueError('package requires a non-empty cut list')
        if self.package_sha256 != _hash(self.identity_payload()):
            raise ValueError('fabrication package hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'package_id': self.package_id,
            'package_version': self.package_version,
            'definition_id': self.definition_id,
            'definition_version': self.definition_version,
            'definition_sha256': self.definition_sha256,
            'treatment_type': self.treatment_type,
            'fabrication_family': self.fabrication_family,
            'spec_version': self.spec_version,
            'renderer_id': self.renderer_id,
            'renderer_version': self.renderer_version,
            'kerf_mm': self.kerf_mm,
            'tolerances': self.tolerances.model_dump(mode='json'),
            'panel_count': self.panel_count,
            'assembly_orientation': self.assembly_orientation,
            'install_reference': self.install_reference,
            'parts': [part.model_dump(mode='json') for part in self.parts],
            'cut_list': [item.model_dump(mode='json') for item in self.cut_list],
            'bom_fragments': [
                item.model_dump(mode='json') for item in self.bom_fragments
            ],
            'well_table': [row.model_dump(mode='json') for row in self.well_table],
            'overall_width_m': self.overall_width_m,
            'overall_height_m': self.overall_height_m,
            'overall_depth_m': self.overall_depth_m,
            'drawing_sheets': list(self.drawing_sheets),
            'warnings': list(self.warnings),
            'supersedes_package_sha256': self.supersedes_package_sha256,
            'created_at_utc': self.created_at_utc,
        }


def _cut_list(parts: tuple[FabricationPart, ...]) -> tuple[CutListEntry, ...]:
    grouped: dict[str, CutListEntry] = {}
    for part in parts:
        existing = grouped.get(part.cut_group)
        if existing is None:
            grouped[part.cut_group] = CutListEntry(
                cut_group=part.cut_group,
                description=part.description,
                dimensions_m=(
                    part.finished_width_m,
                    part.finished_height_m,
                    part.finished_depth_m,
                ),
                quantity=part.quantity,
                material_ref=part.material_ref,
            )
        else:
            same_dims = existing.dimensions_m == (
                part.finished_width_m,
                part.finished_height_m,
                part.finished_depth_m,
            )
            if not same_dims:
                raise ValueError(
                    f'cut group {part.cut_group} mixes different '
                    'finished dimensions — pick distinct cut_group ids'
                )
            grouped[part.cut_group] = existing.model_copy(
                update={'quantity': existing.quantity + part.quantity}
            )
    return tuple(grouped[k] for k in sorted(grouped))


def _seal_package(
    payload: dict[str, Any],
) -> TreatmentFabricationPackage:
    provisional = TreatmentFabricationPackage.model_construct(**canonicalize_payload(TreatmentFabricationPackage, dict(
        **payload, package_sha256='0' * 64
    )))
    return TreatmentFabricationPackage.model_validate(
        {
            **payload,
            'package_sha256': _hash(provisional.identity_payload()),
        }
    )


def generate_panel_fabrication(
    definition: AcousticTreatmentDefinition,
    spec: FabricationSpec,
    *,
    created_at_utc: str,
    package_version: str = '1',
    assembly_orientation: str = 'wall-mounted',
    install_reference: str = '',
) -> TreatmentFabricationPackage:
    """FAB10: rectangular porous panel/trap fabrication from an exact
    treatment definition — absorber layer parts, air-gap note, panel
    count and a grouped cut list (#791 §2)."""
    if definition.treatment_type not in _FAB10_TYPES:
        raise ValueError(
            f'FAB10 panel fabrication does not apply to treatment_type '
            f'{definition.treatment_type!r} — only {sorted(_FAB10_TYPES)}'
        )
    if not definition.layers:
        raise ValueError('panel fabrication requires treatment layers')

    dims = definition.dimensions
    warnings: list[str] = []
    parts: list[FabricationPart] = []
    bom: list[BomFragment] = []

    for layer in definition.layers:
        parts.append(
            FabricationPart(
                part_id=f'layer-{layer.layer_id}',
                part_kind='absorber_layer',
                description=(
                    f'absorber layer {layer.layer_id}: '
                    f'{layer.material_name}'
                ),
                finished_width_m=dims.width_m,
                finished_height_m=dims.height_m,
                finished_depth_m=layer.thickness_m,
                quantity=spec.panel_count,
                material_ref=layer.material_name,
                cut_group=f'absorber-{layer.layer_id}',
                notes=f'{spec.panel_count} panel(s)',
            )
        )
        bom.append(
            BomFragment(
                line_kind='cut_part',
                description=(
                    f'{layer.material_name} '
                    f'{dims.width_m:.3f}x{dims.height_m:.3f}x'
                    f'{layer.thickness_m:.3f} m'
                ),
                quantity=float(spec.panel_count),
                unit='panel',
            )
        )
    layer_total = sum(layer.thickness_m for layer in definition.layers)
    if abs(layer_total - dims.thickness_m) > 1e-6:
        warnings.append(
            f'layer thickness sum {layer_total:.4f} m differs from '
            f'overall thickness {dims.thickness_m:.4f} m — check '
            'definition intent before cutting'
        )
    if definition.air_gap_m > 0.0:
        warnings.append(
            f'assembly requires a {definition.air_gap_m:.3f} m air gap '
            'behind the absorber — spacing is an installation note, not '
            'a cut part'
        )
    bom.append(
        BomFragment(
            line_kind='assembly',
            description=f'treatment assembly {definition.definition_id}',
            quantity=float(spec.panel_count),
            unit='installed item',
        )
    )

    return _seal_package(
        {
            'package_id': f'{_PACKAGE_PREFIX}{uuid4()}',
            'package_version': package_version,
            'definition_id': definition.definition_id,
            'definition_version': definition.version,
            'definition_sha256': definition.definition_sha256,
            'treatment_type': definition.treatment_type,
            'fabrication_family': 'rectangular_panel',
            'spec_version': spec.spec_version,
            'renderer_id': _RENDERER_ID,
            'renderer_version': _RENDERER_VERSION,
            'kerf_mm': spec.kerf_mm,
            'tolerances': spec.tolerances,
            'panel_count': spec.panel_count,
            'assembly_orientation': assembly_orientation,
            'install_reference': install_reference,
            'parts': tuple(parts),
            'cut_list': _cut_list(tuple(parts)),
            'bom_fragments': tuple(bom),
            'well_table': (),
            'overall_width_m': dims.width_m,
            'overall_height_m': dims.height_m,
            'overall_depth_m': dims.thickness_m + definition.air_gap_m,
            'drawing_sheets': ('front', 'section'),
            'warnings': tuple(warnings),
            'supersedes_package_sha256': spec.supersedes_package_sha256,
            'created_at_utc': created_at_utc,
        }
    )


def qrd_depth_table(
    prime: int,
    design_frequency_hz: float,
    speed_of_sound_m_s: float,
    periods: int,
) -> tuple[QrdWellRow, ...]:
    """Explicit 1D Schroeder QRD well-depth table (#791 §3): sequence
    ``s_i = i^2 mod N`` over an odd prime ``N``, well depth
    ``d_i = s_i * lambda / (2N)`` with ``lambda = c / f_design``.
    Textbook design frequency is not a guaranteed in-room diffusion
    bandwidth."""
    if not _is_prime(prime) or prime % 2 == 0:
        raise ValueError('QRD prime N must be an odd prime')
    if design_frequency_hz <= 0.0 or speed_of_sound_m_s <= 0.0:
        raise ValueError('design frequency and speed of sound must be positive')
    if periods < 1:
        raise ValueError('periods must be >= 1')
    wavelength = speed_of_sound_m_s / design_frequency_hz
    rows: list[QrdWellRow] = []
    for period in range(periods):
        for i in range(prime):
            residue = (i * i) % prime
            rows.append(
                QrdWellRow(
                    well_index=period * prime + i,
                    period_index=period,
                    residue=residue,
                    depth_m=residue * wavelength / (2.0 * prime),
                )
            )
    return tuple(rows)


def generate_qrd_fabrication(
    definition: AcousticTreatmentDefinition,
    spec: FabricationSpec,
    *,
    qrd_prime: int,
    design_frequency_hz: float,
    speed_of_sound_m_s: float = 343.0,
    well_width_m: float,
    fin_thickness_m: float,
    periods: int = 1,
    back_thickness_m: float,
    created_at_utc: str,
    package_version: str = '1',
    assembly_orientation: str = 'wall-mounted',
    install_reference: str = '',
) -> TreatmentFabricationPackage:
    """FAB20: deterministic 1D QRD fabrication — numbered well-depth
    table, fins, backing, grouped cut list and tolerance callouts."""
    if definition.treatment_type not in _FAB20_TYPES:
        raise ValueError(
            f'FAB20 QRD fabrication does not apply to treatment_type '
            f'{definition.treatment_type!r} — only {sorted(_FAB20_TYPES)}'
        )
    if well_width_m <= 0.0 or fin_thickness_m < 0.0:
        raise ValueError('well_width_m must be positive, fin_thickness >= 0')
    if back_thickness_m <= 0.0:
        raise ValueError('back_thickness_m must be positive')

    wells = qrd_depth_table(
        qrd_prime, design_frequency_hz, speed_of_sound_m_s, periods
    )
    well_count = len(wells)
    depth_by_well = [row.depth_m for row in wells]
    max_well_depth = max(depth_by_well)
    overall_width = well_count * well_width_m + (
        well_count + 1
    ) * fin_thickness_m
    overall_depth = max_well_depth + back_thickness_m
    panel_height = definition.dimensions.height_m

    warnings: list[str] = [
        'buildable geometry does not claim ISO-grade diffusion '
        'performance — repeated periodic QRD can lob; finite-size and '
        'edge diffraction are not modelled here',
    ]
    if abs(overall_width - definition.dimensions.width_m) > 1e-6:
        warnings.append(
            f'computed panel width {overall_width:.4f} m differs from '
            f'definition width {definition.dimensions.width_m:.4f} m'
        )

    parts: list[FabricationPart] = [
        FabricationPart(
            part_id='backing',
            part_kind='backing_panel',
            description='QRD backing board',
            finished_width_m=overall_width,
            finished_height_m=panel_height,
            finished_depth_m=back_thickness_m,
            quantity=spec.panel_count,
            material_ref=spec.material_ref,
            cut_group='qrd-backing',
        )
    ]
    # Fin between wells j-1 and j takes the deeper adjacent well depth;
    # edge fins take the adjacent well depth.
    fin_groups: dict[float, int] = {}
    for j in range(well_count + 1):
        left = depth_by_well[j - 1] if j > 0 else depth_by_well[0]
        right = depth_by_well[j] if j < well_count else depth_by_well[-1]
        depth = max(left, right)
        fin_groups[depth] = fin_groups.get(depth, 0) + 1
    if fin_thickness_m > 0.0:
        for index, (depth, count) in enumerate(sorted(fin_groups.items())):
            if depth <= 0.0:
                continue
            parts.append(
                FabricationPart(
                    part_id=f'fin-{index}',
                    part_kind='well_fin',
                    description=f'well divider fin depth {depth:.4f} m',
                    finished_width_m=fin_thickness_m,
                    finished_height_m=panel_height,
                    finished_depth_m=depth,
                    quantity=count * spec.panel_count,
                    material_ref=spec.material_ref,
                    cut_group=f'fin-{depth:.4f}',
                    notes='dividers run full panel height between wells',
                )
            )
    else:
        warnings.append(
            'fin thickness is zero — wells have no physical separators; '
            'this is a theoretical pattern, not a buildable package'
        )

    cut_list = _cut_list(tuple(parts))
    cut_list = tuple(
        entry.model_copy(
            update={
                'tolerance_mm': (
                    spec.tolerances.well_depth_mm
                    if entry.cut_group.startswith('fin-')
                    else spec.tolerances.overall_dimension_mm
                )
            }
        )
        for entry in cut_list
    )

    bom = [
        BomFragment(
            line_kind='cut_part',
            description='QRD divider strips',
            quantity=float(sum(
                p.quantity for p in parts if p.part_kind == 'well_fin'
            )),
            unit='exact cut parts',
        ),
        BomFragment(
            line_kind='cut_part',
            description='QRD backing board',
            quantity=float(spec.panel_count),
            unit='panel',
        ),
        BomFragment(
            line_kind='assembly',
            description=f'treatment assembly {definition.definition_id}',
            quantity=float(spec.panel_count),
            unit='installed item',
        ),
    ]

    return _seal_package(
        {
            'package_id': f'{_PACKAGE_PREFIX}{uuid4()}',
            'package_version': package_version,
            'definition_id': definition.definition_id,
            'definition_version': definition.version,
            'definition_sha256': definition.definition_sha256,
            'treatment_type': definition.treatment_type,
            'fabrication_family': 'qrd_1d',
            'spec_version': spec.spec_version,
            'renderer_id': _RENDERER_ID,
            'renderer_version': _RENDERER_VERSION,
            'kerf_mm': spec.kerf_mm,
            'tolerances': spec.tolerances,
            'panel_count': spec.panel_count,
            'assembly_orientation': assembly_orientation,
            'install_reference': install_reference,
            'parts': tuple(parts),
            'cut_list': cut_list,
            'bom_fragments': tuple(bom),
            'well_table': wells,
            'overall_width_m': overall_width,
            'overall_height_m': panel_height,
            'overall_depth_m': overall_depth,
            'drawing_sheets': ('front', 'section', 'well_table'),
            'warnings': tuple(warnings),
            'supersedes_package_sha256': spec.supersedes_package_sha256,
            'created_at_utc': created_at_utc,
        }
    )


__all__ = [
    'BomFragment',
    'CutListEntry',
    'FabricationPart',
    'FabricationSpec',
    'FabricationToleranceProfile',
    'QrdWellRow',
    'TreatmentFabricationPackage',
    'generate_panel_fabrication',
    'generate_qrd_fabrication',
    'qrd_depth_table',
]
