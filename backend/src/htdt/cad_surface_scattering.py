"""Surface scattering / diffusion authority (#1032).

Room-acoustic standards distinguish quantities that one scalar
``scattering`` field cannot express:

- ISO 17497-1 *random-incidence scattering coefficient* — the fraction of
  reflected sound energy that deviates from specular reflection. This is the
  only quantity a geometric-acoustics (GA) solver may consume as a scalar
  scatter fraction.
- ISO 17497-2 *directional diffusion coefficient* — spatial uniformity of
  the reflected polar distribution. Per ISO, it is *not* suitable as a
  direct input to GA scattering algorithms; it is a display/documentation
  quantity here and is never silently fed to the solver.
- Directional scattering distributions / kernels and explicit diffuser
  geometry — stronger representations that preserve orientation and must
  not be double-counted against a scalar coefficient.

Every quantity kind is explicit and fail-closed: asking for the GA scatter
fraction of diffusion/geometry-only evidence yields ``None`` (UNKNOWN
solver input) rather than a fabricated coefficient.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurements import canonical_json
from .canonical_json import canonical_sha256 as _hash


# The seven quantity kinds the issue enumerates — never one field named
# "scattering" for all of them.
ScatteringQuantityKind = Literal[
    'random_incidence_scattering_coefficient',
    'directional_scattering_distribution',
    'directional_diffusion_coefficient',
    'random_incidence_diffusion_coefficient',
    'model_derived_scattering',
    'explicit_geometry_only',
    'unknown',
]

# Kinds that legitimately carry a scalar GA scatter fraction. ISO 17497-2
# diffusion coefficients are deliberately absent — the standard itself says
# the coefficient is not a GA solver input.
_GA_SCALAR_KINDS = frozenset(
    {
        'random_incidence_scattering_coefficient',
        'model_derived_scattering',
    }
)

ScatteringIncidenceSemantics = Literal[
    'random_or_diffuse_incidence',
    'angle_specific',
    'normal_incidence',
    'unknown_incidence',
]

ScatteringStandard = Literal[
    'iso_17497_1',
    'iso_17497_2',
    'iso_354',
    'declared_no_standard',
    'unknown',
]




class SurfaceScatteringEvidence(BaseModel):
    """Typed authority for one surface's scattering/diffusion quantity.

    The ``quantity_kind`` decides what a consumer may do with ``values``:
    only random-incidence/model-derived scattering coefficients may feed a
    GA scatter fraction; diffusion coefficients and directional or
    geometry-only kinds are preserved for documentation, reduction, or
    wave solving but never silently converted.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    evidence_id: str = Field(min_length=1)
    authority_version: Literal[
        'surface_scattering_evidence_v1'
    ] = 'surface_scattering_evidence_v1'
    # Exact binding to what was measured/modelled.
    surface_id: str | None = None
    material_id: str | None = None
    treatment_id: str | None = None
    quantity_kind: ScatteringQuantityKind
    # Banded scalar values (coefficients) or a declared placeholder for
    # directional data held by ``DirectionalScatteringKernel``.
    band_center_hz: tuple[float, ...] = ()
    values: tuple[float, ...] = ()
    incidence_semantics: ScatteringIncidenceSemantics = 'unknown_incidence'
    incidence_angle_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    standard: ScatteringStandard = 'unknown'
    method: str = ''
    source: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    uncertainty: float | None = Field(default=None, ge=0.0)
    valid_frequency_range_hz: tuple[float, float] | None = None
    # Specimen/mounting context when the evidence is a measured artifact.
    mounting: str | None = None
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evidence(self) -> 'SurfaceScatteringEvidence':
        if not any(
            item is not None
            for item in (
                self.surface_id,
                self.material_id,
                self.treatment_id,
            )
        ):
            raise ValueError(
                'scattering evidence must bind at least one of '
                'surface/material/treatment'
            )
        if len(self.band_center_hz) != len(self.values):
            raise ValueError(
                'band_center_hz and values must align element-wise'
            )
        if list(self.band_center_hz) != sorted(self.band_center_hz):
            raise ValueError('band centers must be sorted')
        for center, value in zip(self.band_center_hz, self.values):
            if not isfinite(center) or center <= 0.0:
                raise ValueError('band centers must be positive and finite')
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(
                    'scattering/diffusion coefficients must be in [0, 1]'
                )
        if self.incidence_semantics == 'angle_specific':
            if self.incidence_angle_deg is None:
                raise ValueError(
                    'angle_specific scattering evidence requires '
                    'incidence_angle_deg'
                )
        elif self.incidence_angle_deg is not None:
            raise ValueError(
                'incidence_angle_deg is only meaningful for '
                'angle_specific evidence'
            )
        if (
            self.quantity_kind == 'directional_diffusion_coefficient'
            and self.standard not in ('iso_17497_2', 'declared_no_standard')
        ):
            raise ValueError(
                'directional diffusion coefficients must declare '
                'iso_17497_2 or honestly decline a standard'
            )
        if (
            self.quantity_kind == 'random_incidence_scattering_coefficient'
            and self.standard == 'iso_17497_2'
        ):
            # ISO 17497-2 measures diffusion, not the random-incidence
            # scatter fraction — the pairing is a semantics error.
            raise ValueError(
                'ISO 17497-2 does not produce a random-incidence '
                'scattering coefficient'
            )
        if self.valid_frequency_range_hz is not None:
            lo, hi = self.valid_frequency_range_hz
            if not (isfinite(lo) and isfinite(hi)) or not 0.0 < lo <= hi:
                raise ValueError(
                    'valid_frequency_range_hz must be positive and ordered'
                )
        if self.evidence_sha256 != _hash(self.identity_payload()):
            raise ValueError('surface scattering evidence hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'evidence_id': self.evidence_id,
            'surface_id': self.surface_id,
            'material_id': self.material_id,
            'treatment_id': self.treatment_id,
            'quantity_kind': self.quantity_kind,
            'band_center_hz': list(self.band_center_hz),
            'values': list(self.values),
            'incidence_semantics': self.incidence_semantics,
            'incidence_angle_deg': self.incidence_angle_deg,
            'standard': self.standard,
            'method': self.method,
            'source': self.source,
            'provenance': self.provenance,
            'uncertainty': self.uncertainty,
            'valid_frequency_range_hz': (
                None
                if self.valid_frequency_range_hz is None
                else list(self.valid_frequency_range_hz)
            ),
            'mounting': self.mounting,
        }


class DirectionalScatteringKernel(BaseModel):
    """Outgoing angular redistribution for one incidence direction (#1032).

    Preserves what a scalar coefficient cannot: the reflected polar
    distribution as (azimuth, elevation, weight) samples, its
    normalization, and the surface-local frame orientation. Rotating a
    diffuser changes ``orientation_deg`` — the kernel is not
    orientation-invariant.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kernel_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    frequency_hz: float = Field(gt=0.0)
    incidence_azimuth_deg: float = Field(ge=-360.0, le=360.0)
    incidence_elevation_deg: float = Field(ge=-90.0, le=90.0)
    # Degrees the diffuser/sample is rotated about the surface normal.
    orientation_deg: float = Field(default=0.0, ge=-360.0, le=360.0)
    # (azimuth_deg, elevation_deg, weight) outgoing samples.
    outgoing_samples: tuple[tuple[float, float, float], ...]
    normalization: Literal[
        'unit_energy', 'peak_normalized', 'declared_only'
    ] = 'unit_energy'
    interpolation: Literal['nearest', 'linear', 'none'] = 'none'
    source: str = Field(min_length=1)
    valid_angular_domain: str = ''
    kernel_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_kernel(self) -> 'DirectionalScatteringKernel':
        if not self.outgoing_samples:
            raise ValueError('directional kernel requires outgoing samples')
        for azimuth, elevation, weight in self.outgoing_samples:
            if not all(
                isfinite(value) for value in (azimuth, elevation, weight)
            ):
                raise ValueError('kernel samples must be finite')
            if weight < 0.0:
                raise ValueError('kernel sample weights must be >= 0')
            if not -90.0 <= elevation <= 90.0:
                raise ValueError('kernel elevation must be in [-90, 90]')
        if self.normalization == 'unit_energy':
            total = sum(sample[2] for sample in self.outgoing_samples)
            if not total > 0.0:
                raise ValueError('unit_energy kernels require positive mass')
        if self.kernel_sha256 != _hash(self.identity_payload()):
            raise ValueError('directional scattering kernel hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'kernel_id': self.kernel_id,
            'evidence_id': self.evidence_id,
            'frequency_hz': self.frequency_hz,
            'incidence_azimuth_deg': self.incidence_azimuth_deg,
            'incidence_elevation_deg': self.incidence_elevation_deg,
            'orientation_deg': self.orientation_deg,
            'outgoing_samples': [list(s) for s in self.outgoing_samples],
            'normalization': self.normalization,
            'interpolation': self.interpolation,
            'source': self.source,
            'valid_angular_domain': self.valid_angular_domain,
        }


StochasticDistribution = Literal[
    'lambertian',
    'cosine_power',
    'measured_polar_kernel',
    'provider_declared',
]


class StochasticScatteringModel(BaseModel):
    """Reproducible GA stochastic scattering specification (#1032 §12-13).

    The outgoing distribution family, seed, sample count and version are
    all pinned — two runs on the same semantic input reproduce identically
    and the model spec participates in result identity.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    model_version: Literal['stochastic_scattering_v1'] = (
        'stochastic_scattering_v1'
    )
    distribution: StochasticDistribution
    # Explicit deterministic seed — never left implicit.
    seed: int = Field(ge=0)
    sample_count: int = Field(gt=0)
    normalization: Literal['unit_energy', 'declared_only'] = 'unit_energy'
    kernel_id: str | None = None

    @model_validator(mode='after')
    def valid_model(self) -> 'StochasticScatteringModel':
        if self.distribution == 'measured_polar_kernel' and (
            self.kernel_id is None
        ):
            raise ValueError(
                'measured_polar_kernel scattering requires the kernel_id '
                'it samples from'
            )
        if self.distribution != 'measured_polar_kernel' and (
            self.kernel_id is not None
        ):
            raise ValueError(
                'kernel_id is only meaningful for measured_polar_kernel'
            )
        return self


SolverScatteringRepresentation = Literal[
    'scalar_coefficient',
    'directional_kernel',
    'explicit_geometry',
    'display_only',
    'unknown',
]


def ga_scatter_fraction(
    evidence: SurfaceScatteringEvidence,
) -> float | None:
    """Scalar GA scatter fraction for ``evidence``, or ``None``.

    Only quantities that actually are scatter fractions qualify. An ISO
    17497-2 directional diffusion coefficient — or geometry-only evidence —
    returns ``None`` so the solver reports UNKNOWN rather than consuming a
    quantity that was never a fraction of redirected energy (#1032 §2, §11).
    """

    if evidence.quantity_kind not in _GA_SCALAR_KINDS:
        return None
    if not evidence.values:
        return None
    # Deterministic reduction to one scalar: band-center-weighted mean is a
    # declared reduced model, not a hidden conversion.
    return sum(evidence.values) / len(evidence.values)


def solver_scattering_representation(
    evidence: SurfaceScatteringEvidence | None,
    *,
    explicit_geometry: bool = False,
    kernel: DirectionalScatteringKernel | None = None,
) -> SolverScatteringRepresentation:
    """What the solver may consume for a surface (#1032 §6 double-count).

    Explicit diffuser geometry wins: when exact geometry is traced or
    wave-solved, scalar/kernel reductions of the same surface are
    documentation, not additional scattering — they cannot double-count.
    A directional kernel is usable only when its evidence is directional.
    Scalar input comes only from ``ga_scatter_fraction``.
    """

    if explicit_geometry:
        return 'explicit_geometry'
    if kernel is not None:
        if evidence is not None and evidence.quantity_kind != (
            'directional_scattering_distribution'
        ):
            raise ValueError(
                'a directional kernel requires '
                'directional_scattering_distribution evidence'
            )
        if (
            evidence is not None
            and kernel.evidence_id != evidence.evidence_id
        ):
            raise ValueError(
                'directional kernel must belong to the supplied evidence'
            )
        return 'directional_kernel'
    if evidence is None:
        return 'unknown'
    if evidence.quantity_kind == 'explicit_geometry_only':
        # Geometry evidence declared but no explicit solve requested —
        # never downgraded to a fabricated scalar.
        return 'unknown'
    if ga_scatter_fraction(evidence) is not None:
        return 'scalar_coefficient'
    # Diffusion / unknown evidence is display/documentation only.
    return 'display_only'


def build_surface_scattering_evidence(
    *,
    quantity_kind: ScatteringQuantityKind,
    source: str,
    provenance: str,
    surface_id: str | None = None,
    material_id: str | None = None,
    treatment_id: str | None = None,
    band_center_hz: tuple[float, ...] = (),
    values: tuple[float, ...] = (),
    incidence_semantics: ScatteringIncidenceSemantics = 'unknown_incidence',
    incidence_angle_deg: float | None = None,
    standard: ScatteringStandard = 'unknown',
    method: str = '',
    uncertainty: float | None = None,
    valid_frequency_range_hz: tuple[float, float] | None = None,
    mounting: str | None = None,
    evidence_id: str | None = None,
) -> SurfaceScatteringEvidence:
    payload: dict[str, Any] = {
        'evidence_id': evidence_id or str(uuid4()),
        'surface_id': surface_id,
        'material_id': material_id,
        'treatment_id': treatment_id,
        'quantity_kind': quantity_kind,
        'band_center_hz': tuple(band_center_hz),
        'values': tuple(values),
        'incidence_semantics': incidence_semantics,
        'incidence_angle_deg': incidence_angle_deg,
        'standard': standard,
        'method': method,
        'source': source,
        'provenance': provenance,
        'uncertainty': uncertainty,
        'valid_frequency_range_hz': valid_frequency_range_hz,
        'mounting': mounting,
    }
    provisional = SurfaceScatteringEvidence.model_construct(
        **payload, evidence_sha256='0' * 64
    )
    return SurfaceScatteringEvidence(
        **payload, evidence_sha256=_hash(provisional.identity_payload())
    )


def build_directional_scattering_kernel(
    *,
    evidence_id: str,
    frequency_hz: float,
    incidence_azimuth_deg: float,
    incidence_elevation_deg: float,
    outgoing_samples: tuple[tuple[float, float, float], ...],
    source: str,
    orientation_deg: float = 0.0,
    normalization: Literal[
        'unit_energy', 'peak_normalized', 'declared_only'
    ] = 'unit_energy',
    interpolation: Literal['nearest', 'linear', 'none'] = 'none',
    valid_angular_domain: str = '',
    kernel_id: str | None = None,
) -> DirectionalScatteringKernel:
    payload: dict[str, Any] = {
        'kernel_id': kernel_id or str(uuid4()),
        'evidence_id': evidence_id,
        'frequency_hz': frequency_hz,
        'incidence_azimuth_deg': incidence_azimuth_deg,
        'incidence_elevation_deg': incidence_elevation_deg,
        'orientation_deg': orientation_deg,
        'outgoing_samples': tuple(tuple(s) for s in outgoing_samples),
        'normalization': normalization,
        'interpolation': interpolation,
        'source': source,
        'valid_angular_domain': valid_angular_domain,
    }
    provisional = DirectionalScatteringKernel.model_construct(
        **payload, kernel_sha256='0' * 64
    )
    return DirectionalScatteringKernel(
        **payload, kernel_sha256=_hash(provisional.identity_payload())
    )
