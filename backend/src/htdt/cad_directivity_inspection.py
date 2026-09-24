"""Directivity inspector view-model authority (#495).

A persisted :class:`DirectivityDataset` already carries an exact polar grid
with frozen coordinate conventions, normalization, valid domain and source
provenance. This module turns that authority into an *inspectable* semantic
view so the native Equipment/Source Library can validate the imported
physical meaning — axis orientation, angle-sign conventions, valid bands,
raw-vs-interpolated values, phase capability — before the dataset is bound
to prediction, coverage or optimization.

Contract properties:

- slices at an *exact* dataset grid frequency return raw samples;
  off-grid frequencies or an off-grid reference plane are marked
  ``interpolated=True`` and per-point ``on_grid`` distinguishes raw samples
  from evaluator-interpolated values — interpolated data is never dressed up
  as raw data;
- the canonical polar planes are the on-axis planes: horizontal at
  ``vertical=0``, vertical at ``horizontal=0``; when that plane is off-grid
  the caller must opt into interpolation explicitly;
- magnitude-only datasets report ``phase_available=False`` and never gain a
  phase view — coherent-phase capability stays explicit;
- inspection never mutates the dataset: every function is a pure read over
  the immutable authority;
- :class:`DirectivityInspectionConfirmation` is the explicit *semantic
  confirmation* record (front axis reviewed / conventions reviewed), a
  user-provenance fact bound to the exact dataset hash — it asserts review
  happened, not numerical correctness of manufacturer data;
- no dataset rotation/reinterpretation is offered here: the dataset's own
  declared ``reference_axis`` and sign conventions are surfaced as markers
  for visual comparison against the EquipmentDefinition acoustic reference.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_directivity import DirectivityDataset, evaluate_directivity
from .cad_equipment import EquipmentDefinition


INSPECTION_CONFIRMATION_AUTHORITY_VERSION = 'directivity-inspection-confirmation-1'

DirectivitySlicePlane = Literal['horizontal', 'vertical']
InspectionStatus = Literal['AVAILABLE', 'UNKNOWN']


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


class DirectivityAxisMarker(BaseModel):
    """One named semantic direction for overlay on polar/heatmap views."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    horizontal_angle_deg: float
    vertical_angle_deg: float
    in_domain: bool


class DirectivityAxisMarkers(BaseModel):
    """The declared orientation semantics of one dataset as view markers.

    ``front`` is the 0°/0° reference axis; ``left``/``right``/``up``/``down``
    follow the dataset's own declared sign conventions so a left/right or
    up/down sign mistake in the source import is visually obvious.
    """

    model_config = ConfigDict(frozen=True)

    angle_semantics: str
    reference_axis: str
    azimuth_positive: str
    elevation_positive: str
    horizontal_wrap: str
    markers: tuple[DirectivityAxisMarker, ...]


def directivity_axis_markers(dataset: DirectivityDataset) -> DirectivityAxisMarkers:
    """Named front/back/left/right/up/down markers from declared semantics."""

    convention = dataset.coordinate_convention
    domain = dataset.valid_domain

    def marker(name: str, horizontal: float, vertical: float) -> DirectivityAxisMarker:
        return DirectivityAxisMarker(
            name=name,
            horizontal_angle_deg=horizontal,
            vertical_angle_deg=vertical,
            in_domain=(
                domain.horizontal.contains(horizontal)
                and domain.vertical.contains(vertical)
            ),
        )

    left = 90.0 if convention.azimuth_positive == 'left' else -90.0
    up = 90.0 if convention.elevation_positive == 'up' else -90.0
    return DirectivityAxisMarkers(
        angle_semantics=convention.angle_semantics,
        reference_axis=convention.reference_axis,
        azimuth_positive=convention.azimuth_positive,
        elevation_positive=convention.elevation_positive,
        horizontal_wrap=convention.horizontal_wrap,
        markers=(
            marker('front', 0.0, 0.0),
            marker('back', 180.0, 0.0),
            marker('left', left, 0.0),
            marker('right', -left, 0.0),
            marker('up', 0.0, up),
            marker('down', 0.0, -up),
        ),
    )


class DirectivityPolarPoint(BaseModel):
    """One point of a polar slice.

    ``on_grid`` distinguishes a raw dataset sample from an evaluator
    interpolation — the inspector must never render them identically.
    """

    model_config = ConfigDict(frozen=True)

    angle_deg: float
    magnitude_db: float
    phase_deg: float | None = None
    on_grid: bool


class DirectivityPolarSlice(BaseModel):
    """A polar cut at one frequency on one on-axis plane."""

    model_config = ConfigDict(frozen=True)

    plane: DirectivitySlicePlane
    requested_frequency_hz: float
    orthogonal_angle_deg: float
    interpolated: bool
    interpolation_method: str | None = None
    phase_available: bool
    points: tuple[DirectivityPolarPoint, ...] = ()
    unsupported_angles_deg: tuple[float, ...] = ()
    status: InspectionStatus
    reason: str = Field(min_length=1)


def directivity_polar_slice(
    dataset: DirectivityDataset,
    plane: DirectivitySlicePlane,
    frequency_hz: float,
    *,
    allow_interpolation: bool = False,
) -> DirectivityPolarSlice:
    """Slice the dataset at one frequency on the requested on-axis plane.

    Exact mode (``allow_interpolation=False``) requires the frequency and the
    orthogonal 0° reference to be on the dataset grid — the returned points
    are raw samples with ``on_grid=True``. Interpolated preview evaluates
    each point through :func:`evaluate_directivity`, flags the slice
    ``interpolated`` when any point was interpolated, and names the dataset's
    declared interpolation authority.
    """

    frequency = float(frequency_hz)
    if not isfinite(frequency):
        raise ValueError('slice frequency must be finite')
    phase_available = dataset.kind == 'complex'
    request = 'complex' if phase_available else 'magnitude'
    on_grid_frequency = frequency in dataset.frequencies_hz

    if plane == 'horizontal':
        axis_angles = dataset.horizontal_angles_deg
        orthogonal = 0.0
        orthogonal_on_grid = orthogonal in dataset.vertical_angles_deg
    else:
        axis_angles = dataset.vertical_angles_deg
        orthogonal = 0.0
        orthogonal_on_grid = orthogonal in dataset.horizontal_angles_deg

    exact_available = on_grid_frequency and orthogonal_on_grid
    if not exact_available and not allow_interpolation:
        reasons: list[str] = []
        if not on_grid_frequency:
            reasons.append('requested frequency is not on the dataset grid')
        if not orthogonal_on_grid:
            reasons.append(
                'the on-axis reference plane is not on the dataset grid'
            )
        return DirectivityPolarSlice(
            plane=plane,
            requested_frequency_hz=frequency,
            orthogonal_angle_deg=orthogonal,
            interpolated=False,
            interpolation_method=dataset.interpolation.method,
            phase_available=phase_available,
            status='UNKNOWN',
            reason='; '.join(reasons),
        )

    samples_by_key = {
        (
            sample.frequency_hz,
            sample.horizontal_angle_deg,
            sample.vertical_angle_deg,
        ): sample
        for sample in dataset.samples
    }
    points: list[DirectivityPolarPoint] = []
    unsupported: list[float] = []
    interpolated = False
    for angle in axis_angles:
        if plane == 'horizontal':
            horizontal, vertical = angle, orthogonal
        else:
            horizontal, vertical = orthogonal, angle
        raw = samples_by_key.get((frequency, horizontal, vertical))
        if raw is not None:
            points.append(
                DirectivityPolarPoint(
                    angle_deg=angle,
                    magnitude_db=raw.magnitude_db,
                    phase_deg=raw.phase_deg,
                    on_grid=True,
                )
            )
            continue
        result = evaluate_directivity(
            dataset,
            frequency_hz=frequency,
            horizontal_angle_deg=horizontal,
            vertical_angle_deg=vertical,
            request=request,
        )
        if result.decision != 'SUPPORTED' or result.magnitude_db is None:
            unsupported.append(angle)
            continue
        interpolated = interpolated or result.interpolation_applied
        points.append(
            DirectivityPolarPoint(
                angle_deg=angle,
                magnitude_db=result.magnitude_db,
                phase_deg=result.phase_deg,
                on_grid=not result.interpolation_applied,
            )
        )

    if not points:
        return DirectivityPolarSlice(
            plane=plane,
            requested_frequency_hz=frequency,
            orthogonal_angle_deg=orthogonal,
            interpolated=interpolated,
            interpolation_method=dataset.interpolation.method,
            phase_available=phase_available,
            unsupported_angles_deg=tuple(unsupported),
            status='UNKNOWN',
            reason='no slice points resolve inside the dataset valid domain',
        )
    return DirectivityPolarSlice(
        plane=plane,
        requested_frequency_hz=frequency,
        orthogonal_angle_deg=orthogonal,
        interpolated=interpolated,
        interpolation_method=dataset.interpolation.method,
        phase_available=phase_available,
        points=tuple(points),
        unsupported_angles_deg=tuple(unsupported),
        status='AVAILABLE',
        reason=(
            'exact on-grid slice'
            if not interpolated
            else 'preview contains interpolated values'
        ),
    )


class DirectivitySliceHeatmap(BaseModel):
    """Compact magnitude matrix for one polar plane over the frequency axis.

    Exact-grid only — an interpolated heatmap would hide precisely the
    discontinuities and axis mistakes the inspector exists to expose.
    """

    model_config = ConfigDict(frozen=True)

    plane: DirectivitySlicePlane
    orthogonal_angle_deg: float
    frequencies_hz: tuple[float, ...] = ()
    angles_deg: tuple[float, ...] = ()
    #: rows parallel ``frequencies_hz``; ``None`` marks a cell that is
    #: structurally absent (impossible on a valid dataset, kept explicit).
    magnitudes_db: tuple[tuple[float | None, ...], ...] = ()
    status: InspectionStatus
    reason: str = Field(min_length=1)


def directivity_slice_heatmap(
    dataset: DirectivityDataset,
    plane: DirectivitySlicePlane,
) -> DirectivitySliceHeatmap:
    """Frequency × angle magnitude heatmap on the exact on-axis grid."""

    if plane == 'horizontal':
        axis = dataset.horizontal_angles_deg
        orthogonal_on_grid = 0.0 in dataset.vertical_angles_deg
    else:
        axis = dataset.vertical_angles_deg
        orthogonal_on_grid = 0.0 in dataset.horizontal_angles_deg
    if not orthogonal_on_grid:
        return DirectivitySliceHeatmap(
            plane=plane,
            orthogonal_angle_deg=0.0,
            status='UNKNOWN',
            reason='the on-axis reference plane is not on the dataset grid',
        )
    samples_by_key = {
        (
            sample.frequency_hz,
            sample.horizontal_angle_deg,
            sample.vertical_angle_deg,
        ): sample
        for sample in dataset.samples
    }
    rows: list[tuple[float | None, ...]] = []
    for frequency in dataset.frequencies_hz:
        row: list[float | None] = []
        for angle in axis:
            if plane == 'horizontal':
                key = (frequency, angle, 0.0)
            else:
                key = (frequency, 0.0, angle)
            sample = samples_by_key.get(key)
            row.append(None if sample is None else sample.magnitude_db)
        rows.append(tuple(row))
    return DirectivitySliceHeatmap(
        plane=plane,
        orthogonal_angle_deg=0.0,
        frequencies_hz=dataset.frequencies_hz,
        angles_deg=axis,
        magnitudes_db=tuple(rows),
        status='AVAILABLE',
        reason='exact on-grid heatmap',
    )


class DirectivityInspectionSummary(BaseModel):
    """Inspectable semantic view of one persisted DirectivityDataset.

    ``provenance`` carries the normal-view fields; ``advanced`` carries the
    hashes and implementation details an advanced panel may expose.
    """

    model_config = ConfigDict(frozen=True)

    dataset_id: str
    version: str
    dataset_semantic_sha256: str
    equipment_definition_id: str
    equipment_definition_version: str
    equipment_definition_sha256: str
    kind: str
    phase_available: bool
    #: Human-readable explicit statement when coherent phase is absent —
    #: "magnitude-only; coherent phase unavailable".
    phase_statement: str
    frequency_count: int
    horizontal_angle_count: int
    vertical_angle_count: int
    valid_frequency_band_hz: tuple[float, float]
    valid_horizontal_domain_deg: tuple[float, float]
    valid_vertical_domain_deg: tuple[float, float]
    grid_frequencies_hz: tuple[float, ...]
    markers: DirectivityAxisMarkers
    #: Normal view: source format, evidence kind, valid band, coverage, tier.
    provenance: dict[str, Any]
    #: Advanced view: hashes, interpolation implementation, phase reference.
    advanced: dict[str, Any]
    #: Acoustic reference context, present when the bound EquipmentDefinition
    #: is supplied — the cabinet/acoustic reference the directivity
    #: orientation is declared against. Never re-derived geometry.
    acoustic_reference_point_m: tuple[float, float, float] | None = None
    cabinet_envelope_m: tuple[float, float, float] | None = None


def inspect_directivity_dataset(
    dataset: DirectivityDataset,
    *,
    definition: EquipmentDefinition | None = None,
) -> DirectivityInspectionSummary:
    """Build the inspector view-model for one saved dataset.

    When ``definition`` is supplied it must be the exact authority the
    dataset is bound to (matching id/version/hash) — a mismatched definition
    fails closed rather than presenting unrelated reference geometry.
    """

    if definition is not None:
        if (
            definition.definition_id != dataset.equipment_definition_id
            or definition.version != dataset.equipment_definition_version
            or definition.semantic_sha256 != dataset.equipment_definition_sha256
        ):
            raise ValueError(
                'inspection definition must be the exact bound authority'
            )

    phase_available = dataset.kind == 'complex'
    convention = dataset.coordinate_convention
    normalization = dataset.normalization
    interpolation = dataset.interpolation
    domain = dataset.valid_domain
    provenance = dataset.source_provenance

    summary_provenance = {
        'source_format': dataset.source_format,
        'container_format': dataset.container_format,
        'evidence_kind': dataset.evidence_kind,
        'dataset_tier': dataset.kind,
        'source_name': provenance.source_name,
        'source_version': provenance.source_version,
        'source_reference': provenance.source_reference,
        'valid_frequency_band_hz': list(
            [
                domain.frequency.minimum_hz,
                domain.frequency.maximum_hz,
            ]
        ),
        'angular_coverage_deg': {
            'horizontal': [
                domain.horizontal.minimum_deg,
                domain.horizontal.maximum_deg,
            ],
            'vertical': [
                domain.vertical.minimum_deg,
                domain.vertical.maximum_deg,
            ],
        },
        'sample_count': len(dataset.samples),
    }
    advanced = {
        'source_asset_sha256': dataset.source_asset_sha256,
        'parser_id': dataset.parser_id,
        'parser_version': dataset.parser_version,
        'adapter_id': dataset.adapter_id,
        'adapter_version': dataset.adapter_version,
        'interpolation_method': interpolation.method,
        'interpolation_implementation': interpolation.implementation,
        'interpolation_version': interpolation.implementation_version,
        'interpolation_provenance': interpolation.provenance.model_dump(
            mode='json'
        ),
        'normalization': normalization.model_dump(mode='json'),
        'phase_reference': dataset.phase_reference,
        'grid_sha256': dataset.grid_sha256,
        'sample_sha256': dataset.sample_sha256,
        'semantic_sha256': dataset.semantic_sha256,
        'angle_semantics': convention.angle_semantics,
        'horizontal_wrap': convention.horizontal_wrap,
    }

    acoustic_reference_point = None
    cabinet_envelope = None
    if definition is not None:
        point = definition.acoustic_reference_point_m
        acoustic_reference_point = (
            float(point.x_m), float(point.y_m), float(point.z_m)
        )
        envelope = definition.cabinet_envelope_m
        cabinet_envelope = (
            float(envelope.x_m), float(envelope.y_m), float(envelope.z_m)
        )

    return DirectivityInspectionSummary(
        dataset_id=dataset.dataset_id,
        version=dataset.version,
        dataset_semantic_sha256=dataset.semantic_sha256,
        equipment_definition_id=dataset.equipment_definition_id,
        equipment_definition_version=dataset.equipment_definition_version,
        equipment_definition_sha256=dataset.equipment_definition_sha256,
        kind=dataset.kind,
        phase_available=phase_available,
        phase_statement=(
            'complex dataset; coherent phase available'
            if phase_available
            else 'magnitude-only; coherent phase unavailable'
        ),
        frequency_count=len(dataset.frequencies_hz),
        horizontal_angle_count=len(dataset.horizontal_angles_deg),
        vertical_angle_count=len(dataset.vertical_angles_deg),
        valid_frequency_band_hz=(
            domain.frequency.minimum_hz,
            domain.frequency.maximum_hz,
        ),
        valid_horizontal_domain_deg=(
            domain.horizontal.minimum_deg,
            domain.horizontal.maximum_deg,
        ),
        valid_vertical_domain_deg=(
            domain.vertical.minimum_deg,
            domain.vertical.maximum_deg,
        ),
        grid_frequencies_hz=dataset.frequencies_hz,
        markers=directivity_axis_markers(dataset),
        provenance=summary_provenance,
        advanced=advanced,
        acoustic_reference_point_m=acoustic_reference_point,
        cabinet_envelope_m=cabinet_envelope,
    )


class DirectivityInspectionConfirmation(BaseModel):
    """Append-only user attestation that an import was semantically reviewed.

    This is *user confirmation provenance*: it records that the front/reference
    axis and the angle-sign conventions were visually reviewed for this exact
    dataset hash. It asserts review, not numerical correctness of the source
    data; a failed review is still recorded so the state is never implicit.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'directivity-inspection-confirmation-1'
    ] = INSPECTION_CONFIRMATION_AUTHORITY_VERSION
    confirmation_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    dataset_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    front_axis_reviewed: bool
    conventions_reviewed: bool
    note: str | None = None
    confirmed_at_utc: str = Field(min_length=1)
    confirmation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_confirmation(self) -> 'DirectivityInspectionConfirmation':
        if self.confirmation_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DirectivityInspectionConfirmation hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'confirmation_id': self.confirmation_id,
            'dataset_id': self.dataset_id,
            'dataset_version': self.dataset_version,
            'dataset_semantic_sha256': self.dataset_semantic_sha256,
            'front_axis_reviewed': self.front_axis_reviewed,
            'conventions_reviewed': self.conventions_reviewed,
            'note': self.note,
            'confirmed_at_utc': self.confirmed_at_utc,
        }


def confirm_directivity_inspection(
    dataset: DirectivityDataset,
    *,
    front_axis_reviewed: bool,
    conventions_reviewed: bool,
    confirmed_at_utc: str,
    note: str | None = None,
    confirmation_id: str | None = None,
) -> DirectivityInspectionConfirmation:
    payload: dict[str, Any] = {
        'authority_version': INSPECTION_CONFIRMATION_AUTHORITY_VERSION,
        'confirmation_id': confirmation_id or str(uuid4()),
        'dataset_id': dataset.dataset_id,
        'dataset_version': dataset.version,
        'dataset_semantic_sha256': dataset.semantic_sha256,
        'front_axis_reviewed': bool(front_axis_reviewed),
        'conventions_reviewed': bool(conventions_reviewed),
        'note': note,
        'confirmed_at_utc': confirmed_at_utc,
    }
    provisional = DirectivityInspectionConfirmation.model_construct(
        **payload, confirmation_sha256='0' * 64
    )
    return DirectivityInspectionConfirmation(
        **payload,
        confirmation_sha256=_hash(provisional.semantic_payload()),
    )


__all__ = [
    'DirectivityAxisMarker',
    'DirectivityAxisMarkers',
    'DirectivityInspectionConfirmation',
    'DirectivityInspectionSummary',
    'DirectivityPolarPoint',
    'DirectivityPolarSlice',
    'DirectivitySliceHeatmap',
    'DirectivitySlicePlane',
    'INSPECTION_CONFIRMATION_AUTHORITY_VERSION',
    'InspectionStatus',
    'confirm_directivity_inspection',
    'directivity_axis_markers',
    'directivity_polar_slice',
    'directivity_slice_heatmap',
    'inspect_directivity_dataset',
]
