"""Parametric acoustic construction builder (#790 MAB10).

An ``AcousticConstructionDefinition`` pins an explicit layer stack —
air layers, rigid-frame porous layers with *explicit* airflow
resistivity, and a rigid backing — plus air-state parameters. A thin,
narrow transfer-matrix implementation derives
``DerivedMaterialAcousticEvidence``: normal-incidence surface impedance,
complex reflection and normal-incidence absorption on an explicit
frequency grid, each point carrying a ``VALID`` /
``OUTSIDE_MODEL_VALIDITY`` flag.

Deliberate first-slice scope:

- the only porous model is Miki (1990), chosen after the reuse-vs-
  implement bakeoff recorded in ``docs/ISSUE_790_MATERIAL_BUILDER_REUSE_ADR.md``;
- airflow resistivity is never inferred from density or wool category;
- only normal incidence is produced — diffuse/random-incidence output
  would be a different, separately versioned derivation;
- the result is *modelled* evidence, never measured material truth;
- a construction-derived curve is not automatically a solver-ready
  boundary: R130C-style causal/vector fitting remains a separate step.
"""

from __future__ import annotations

import cmath
from math import isfinite, pi
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_benchmark import SpecificImpedancePoint
from .cad_acoustic_material import AcousticMaterialAuthority
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


_CONSTRUCTION_PREFIX = 'acoustic-construction:'
_EVIDENCE_PREFIX = 'derived-material-evidence:'

# Published applicability envelope of the Delany-Bazley/Miki family:
# the models were fitted for 0.01 <= f/sigma <= 1.0 over fibrous
# resistivities of roughly 1e3..1e5 Pa.s/m^2.
_MIKI_MIN_F_OVER_SIGMA = 0.01
_MIKI_MAX_F_OVER_SIGMA = 1.0
_MIKI_SIGMA_RANGE = (1.0e3, 1.0e5)

_MODEL_ID = 'miki_1990'
_PROVIDER_ID = 'htdt-tmm-1'






class ConstructionLayer(BaseModel):
    """One layer of an explicit acoustic construction, ordered from the
    incident side toward the backing. Every physical parameter stays
    explicit — nothing is inferred."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['air', 'porous', 'rigid_backing']
    thickness_m: float | None = Field(default=None, gt=0.0)
    airflow_resistivity_pa_s_m2: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def valid_layer(self) -> 'ConstructionLayer':
        if self.kind == 'rigid_backing':
            if self.thickness_m is not None:
                raise ValueError('rigid backing carries no thickness')
            if self.airflow_resistivity_pa_s_m2 is not None:
                raise ValueError('rigid backing carries no resistivity')
            return self
        if self.thickness_m is None:
            raise ValueError(f'{self.kind} layer requires thickness_m')
        if self.kind == 'porous' and self.airflow_resistivity_pa_s_m2 is None:
            raise ValueError(
                'porous layer requires explicit airflow_resistivity_pa_s_m2 '
                '— resistivity is never inferred from density or category'
            )
        if self.kind == 'air' and self.airflow_resistivity_pa_s_m2 is not None:
            raise ValueError('air layer carries no airflow resistivity')
        for value in (self.thickness_m, self.airflow_resistivity_pa_s_m2):
            if value is not None and not isfinite(float(value)):
                raise ValueError('layer parameters must be finite')
        return self


class AcousticConstructionDefinition(BaseModel):
    """Sealed, versioned construction: layer stack + air state + model
    selection policy (#790). Editing a construction creates a new
    version; the definition is hashed over semantic content."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    construction_id: str = Field(min_length=1)
    construction_version: str = Field(min_length=1)
    label: str = Field(min_length=1)
    layers: tuple[ConstructionLayer, ...]
    porous_model: Literal['miki_1990'] = _MODEL_ID
    air_density_kg_m3: float = Field(default=1.204, gt=0.0)
    sound_speed_m_s: float = Field(default=343.0, gt=0.0)
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_construction(self) -> 'AcousticConstructionDefinition':
        if not self.construction_id.startswith(_CONSTRUCTION_PREFIX):
            raise ValueError(
                'construction id must use acoustic-construction: prefix'
            )
        if not self.layers:
            raise ValueError('construction requires at least one layer')
        if self.layers[-1].kind != 'rigid_backing':
            raise ValueError(
                'MAB10 constructions must terminate in a rigid_backing layer'
            )
        if any(layer.kind == 'rigid_backing' for layer in self.layers[:-1]):
            raise ValueError('rigid_backing must be the last layer')
        if not any(layer.kind == 'porous' for layer in self.layers):
            raise ValueError(
                'MAB10 constructions require at least one porous layer'
            )
        if not isfinite(self.air_density_kg_m3) or not isfinite(
            self.sound_speed_m_s
        ):
            raise ValueError('air state must be finite')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('construction semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'construction_id': self.construction_id,
            'construction_version': self.construction_version,
            'label': self.label,
            'layers': [
                layer.model_dump(mode='json') for layer in self.layers
            ],
            'porous_model': self.porous_model,
            'air_density_kg_m3': self.air_density_kg_m3,
            'sound_speed_m_s': self.sound_speed_m_s,
            'created_at_utc': self.created_at_utc,
        }

    def parameter_diagnostics(self) -> tuple[str, ...]:
        """Construction-level model-validity diagnostics for the Miki
        envelope (resistivity range). Per-frequency validity is computed
        on the derived evidence instead."""
        diagnostics: list[str] = []
        for index, layer in enumerate(self.layers):
            if layer.kind != 'porous':
                continue
            sigma = layer.airflow_resistivity_pa_s_m2
            assert sigma is not None
            if not (_MIKI_SIGMA_RANGE[0] <= sigma <= _MIKI_SIGMA_RANGE[1]):
                diagnostics.append(
                    f'layer {index}: airflow resistivity {sigma:g} Pa.s/m^2 '
                    f'is outside the Miki envelope '
                    f'[{_MIKI_SIGMA_RANGE[0]:g}, {_MIKI_SIGMA_RANGE[1]:g}]'
                )
        return tuple(diagnostics)


class DerivedImpedancePoint(BaseModel):
    """One frequency point of modelled surface response."""

    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    impedance_re_pa_s_m: float
    impedance_im_pa_s_m: float
    reflection_re: float
    reflection_im: float
    absorption: float
    validity: Literal['VALID', 'OUTSIDE_MODEL_VALIDITY']

    @model_validator(mode='after')
    def finite_point(self) -> 'DerivedImpedancePoint':
        values = (
            self.frequency_hz,
            self.impedance_re_pa_s_m,
            self.impedance_im_pa_s_m,
            self.reflection_re,
            self.reflection_im,
            self.absorption,
        )
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('derived point values must be finite')
        return self


class DerivedMaterialAcousticEvidence(BaseModel):
    """Versioned derived acoustic evidence bound to an exact construction
    hash, model/provider and frequency grid (#790). This is *modelled*
    evidence — never measured truth."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    evidence_id: str = Field(min_length=1)
    evidence_kind: Literal['modelled'] = 'modelled'
    construction_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    construction_version: str = Field(min_length=1)
    model: Literal['miki_1990'] = _MODEL_ID
    provider: Literal['htdt-tmm-1'] = _PROVIDER_ID
    derivation_version: str = Field(min_length=1)
    incidence: Literal['normal'] = 'normal'
    points: tuple[DerivedImpedancePoint, ...]
    valid_frequency_range_hz: tuple[float, float] | None
    limitations: tuple[str, ...]
    diagnostics: tuple[str, ...]
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evidence(self) -> 'DerivedMaterialAcousticEvidence':
        if not self.evidence_id.startswith(_EVIDENCE_PREFIX):
            raise ValueError(
                'evidence id must use derived-material-evidence: prefix'
            )
        frequencies = [point.frequency_hz for point in self.points]
        if frequencies != sorted(frequencies) or len(frequencies) != len(
            set(frequencies)
        ):
            raise ValueError('evidence frequencies must be unique and sorted')
        if self.valid_frequency_range_hz is not None:
            low, high = self.valid_frequency_range_hz
            valid_freqs = [
                p.frequency_hz for p in self.points if p.validity == 'VALID'
            ]
            if valid_freqs and (low != min(valid_freqs) or high != max(valid_freqs)):
                raise ValueError(
                    'valid_frequency_range_hz must bound the VALID points'
                )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('derived evidence semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'evidence_id': self.evidence_id,
            'evidence_kind': self.evidence_kind,
            'construction_sha256': self.construction_sha256,
            'construction_version': self.construction_version,
            'model': self.model,
            'provider': self.provider,
            'derivation_version': self.derivation_version,
            'incidence': self.incidence,
            'points': [point.model_dump(mode='json') for point in self.points],
            'valid_frequency_range_hz': self.valid_frequency_range_hz,
            'limitations': list(self.limitations),
            'diagnostics': list(self.diagnostics),
            'created_at_utc': self.created_at_utc,
        }


class ModelledVsMeasuredResidual(BaseModel):
    """Quantity-compatible comparison of modelled evidence against a
    measured specific-impedance material (#790 §9). A fit does not prove
    physical parameters uniquely."""

    model_config = ConfigDict(frozen=True)

    modelled_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measured_material_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    common_frequency_range_hz: tuple[float, float] | None
    impedance_residual: tuple[
        tuple[float, float, float], ...
    ] = ()  # (frequency_hz, delta_re, delta_im)
    notes: tuple[str, ...] = ()


def miki_layer_medium(
    airflow_resistivity_pa_s_m2: float,
    frequency_hz: float,
    *,
    air_density_kg_m3: float = 1.204,
    sound_speed_m_s: float = 343.0,
) -> tuple[complex, complex]:
    """Characteristic impedance ``Zc`` and wavenumber ``k`` of a rigid-
    frame porous material per Miki (1990), with ``X = f / sigma``.

    ``Zc = rho*c * [1 + 0.0700 X^-0.632 - j 0.1070 X^-0.632]``
    ``k  = (omega/c) * [1 + 0.1090 X^-0.618 - j 0.1600 X^-0.618]``

    The negative imaginary wavenumber (e^{+j*omega*t} convention) yields
    attenuation through the layer and keeps the model passive.
    """
    if frequency_hz <= 0.0 or airflow_resistivity_pa_s_m2 <= 0.0:
        raise ValueError('miki medium requires positive f and resistivity')
    x = frequency_hz / airflow_resistivity_pa_s_m2
    rho_c = air_density_kg_m3 * sound_speed_m_s
    zc = rho_c * complex(
        1.0 + 0.0700 * x**-0.632,
        -0.1070 * x**-0.632,
    )
    omega = 2.0 * pi * frequency_hz
    k = (omega / sound_speed_m_s) * complex(
        1.0 + 0.1090 * x**-0.618,
        -0.1600 * x**-0.618,
    )
    return zc, k


def _cot(z: complex) -> complex:
    s = cmath.sin(z)
    if s == 0.0:
        raise ZeroDivisionError('cotangent undefined at kd = n*pi')
    return cmath.cos(z) / s


def surface_impedance(
    construction: AcousticConstructionDefinition,
    frequency_hz: float,
) -> complex:
    """Normal-incidence surface impedance of the layer stack via the
    transfer-matrix impedance recurrence, terminated by the rigid
    backing (``Z_in = -j*Zc*cot(k*d)`` for a rigidly backed layer;
    composed layer-by-layer toward the incident surface)."""
    rho_c = construction.air_density_kg_m3 * construction.sound_speed_m_s
    omega = 2.0 * pi * frequency_hz

    impedance: complex | None = None  # None = rigid termination
    for layer in reversed(construction.layers):
        if layer.kind == 'rigid_backing':
            impedance = None
            continue
        assert layer.thickness_m is not None
        if layer.kind == 'air':
            zc = complex(rho_c, 0.0)
            k = complex(omega / construction.sound_speed_m_s, 0.0)
        else:
            assert layer.airflow_resistivity_pa_s_m2 is not None
            zc, k = miki_layer_medium(
                layer.airflow_resistivity_pa_s_m2,
                frequency_hz,
                air_density_kg_m3=construction.air_density_kg_m3,
                sound_speed_m_s=construction.sound_speed_m_s,
            )
        kd = k * layer.thickness_m
        if impedance is None:
            # rigidly backed: Z_in = -j * Zc * cot(k*d)
            impedance = complex(0.0, -1.0) * zc * _cot(kd)
        else:
            # Z_in = Zc (Z_L + j Zc tan(kd)) / (Zc + j Z_L tan(kd))
            t = cmath.tan(kd)
            impedance = zc * (impedance + 1j * zc * t) / (
                zc + 1j * impedance * t
            )
    assert impedance is not None
    return impedance


def build_acoustic_construction(
    *,
    label: str,
    layers: tuple[ConstructionLayer, ...],
    construction_version: str = '1',
    construction_id: str | None = None,
    created_at_utc: str,
    air_density_kg_m3: float = 1.204,
    sound_speed_m_s: float = 343.0,
) -> AcousticConstructionDefinition:
    """Assemble a sealed construction definition from typed layer fields."""
    payload: dict[str, Any] = {
        'construction_id': construction_id or f'{_CONSTRUCTION_PREFIX}{uuid4()}',
        'construction_version': construction_version,
        'label': label,
        'layers': layers,
        'porous_model': _MODEL_ID,
        'air_density_kg_m3': air_density_kg_m3,
        'sound_speed_m_s': sound_speed_m_s,
        'created_at_utc': created_at_utc,
    }
    provisional = AcousticConstructionDefinition.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return AcousticConstructionDefinition.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def derive_material_evidence(
    construction: AcousticConstructionDefinition,
    frequencies_hz: tuple[float, ...],
    *,
    created_at_utc: str,
    evidence_id: str | None = None,
    derivation_version: str = '1',
    allow_outside_validity: bool = False,
) -> DerivedMaterialAcousticEvidence:
    """Derive modelled impedance/reflection/absorption evidence from an
    explicit construction (#790 MAB10).

    Per-frequency validity follows the documented Miki envelope
    (``f/sigma`` in [0.01, 1.0]); out-of-envelope points are only
    produced when ``allow_outside_validity`` is explicitly set, and are
    always flagged ``OUTSIDE_MODEL_VALIDITY`` — exploratory output is
    never production-ready.
    """
    if not frequencies_hz:
        raise ValueError('a frequency grid is required')
    freqs = sorted(set(float(f) for f in frequencies_hz))
    if any(not isfinite(f) or f <= 0.0 for f in freqs):
        raise ValueError('frequencies must be finite and positive')

    diagnostics = list(construction.parameter_diagnostics())
    if diagnostics and not allow_outside_validity:
        raise ValueError(
            'construction parameters fall outside the Miki model '
            'envelope: ' + '; '.join(diagnostics)
        )

    porous = [
        layer for layer in construction.layers if layer.kind == 'porous'
    ]
    rho_c = construction.air_density_kg_m3 * construction.sound_speed_m_s

    points: list[DerivedImpedancePoint] = []
    outside: list[str] = []
    for frequency in freqs:
        in_envelope = all(
            _MIKI_MIN_F_OVER_SIGMA
            <= frequency / layer.airflow_resistivity_pa_s_m2
            <= _MIKI_MAX_F_OVER_SIGMA
            for layer in porous
            if layer.airflow_resistivity_pa_s_m2 is not None
        )
        if not in_envelope and not allow_outside_validity:
            raise ValueError(
                f'frequency {frequency:g} Hz falls outside the Miki '
                'f/sigma envelope for this construction — pass '
                'allow_outside_validity=True for exploratory output'
            )
        if not in_envelope:
            outside.append(f'{frequency:g}')
        z = surface_impedance(construction, frequency)
        if z.real < -1e-9:
            raise ValueError(
                f'model produced a non-passive surface impedance at '
                f'{frequency:g} Hz'
            )
        reflection = (z - rho_c) / (z + rho_c)
        absorption = 1.0 - abs(reflection) ** 2
        points.append(
            DerivedImpedancePoint(
                frequency_hz=frequency,
                impedance_re_pa_s_m=z.real,
                impedance_im_pa_s_m=z.imag,
                reflection_re=reflection.real,
                reflection_im=reflection.imag,
                absorption=absorption,
                validity='VALID' if in_envelope else 'OUTSIDE_MODEL_VALIDITY',
            )
        )
    if outside:
        diagnostics.append(
            'frequencies outside the Miki f/sigma envelope: '
            + ', '.join(outside)
        )

    valid_freqs = [p.frequency_hz for p in points if p.validity == 'VALID']
    if evidence_id is None:
        # content-addressed id: identical construction + grid + model
        # always derive the same evidence identity (#790 determinism).
        content_hash = _hash(
            {
                'construction_sha256': construction.semantic_sha256,
                'model': _MODEL_ID,
                'provider': _PROVIDER_ID,
                'derivation_version': derivation_version,
                'incidence': 'normal',
                'frequencies_hz': freqs,
            }
        )
        evidence_id = f'{_EVIDENCE_PREFIX}{content_hash[:32]}'
    payload: dict[str, Any] = {
        'evidence_id': evidence_id,
        'evidence_kind': 'modelled',
        'construction_sha256': construction.semantic_sha256,
        'construction_version': construction.construction_version,
        'model': _MODEL_ID,
        'provider': _PROVIDER_ID,
        'derivation_version': derivation_version,
        'incidence': 'normal',
        'points': tuple(points),
        'valid_frequency_range_hz': (
            (min(valid_freqs), max(valid_freqs)) if valid_freqs else None
        ),
        'limitations': (
            'modelled, not measured',
            'normal incidence only — not diffuse/random-incidence absorption',
            'rigid-frame fibrous model; frame motion and facings unsupported',
            'not a solver-ready causal boundary — R130C fitting is separate',
            'repeated periodic constructions are not validated by this model',
        ),
        'diagnostics': tuple(diagnostics),
        'created_at_utc': created_at_utc,
    }
    provisional = DerivedMaterialAcousticEvidence.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return DerivedMaterialAcousticEvidence.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def evidence_as_specific_impedance(
    evidence: DerivedMaterialAcousticEvidence,
) -> tuple[SpecificImpedancePoint, ...]:
    """Project modelled evidence into ``SpecificImpedancePoint`` rows so
    it can seed an ``AcousticMaterialAuthority`` with the
    ``specific_impedance_table`` wave model — modelled provenance stays
    on the material authority, never relabelled as measured."""
    return tuple(
        SpecificImpedancePoint(
            frequency_hz=point.frequency_hz,
            resistance_pa_s_m=point.impedance_re_pa_s_m,
            reactance_pa_s_m=point.impedance_im_pa_s_m,
        )
        for point in evidence.points
        if point.validity == 'VALID'
    )


def compare_modelled_vs_measured(
    evidence: DerivedMaterialAcousticEvidence,
    measured: AcousticMaterialAuthority,
) -> ModelledVsMeasuredResidual:
    """Compare modelled evidence against measured specific-impedance
    material data where quantity-compatible (#790 §9)."""
    if measured.wave_model != 'specific_impedance_table':
        return ModelledVsMeasuredResidual(
            modelled_sha256=evidence.semantic_sha256,
            measured_material_sha256=measured.semantic_sha256,
            common_frequency_range_hz=None,
            notes=(
                'measured material is not a specific-impedance table; '
                'quantities are not comparable',
            ),
        )
    measured_by_freq = {
        point.frequency_hz: point for point in measured.specific_impedance
    }
    residuals: list[tuple[float, float, float]] = []
    for point in evidence.points:
        measured_point = measured_by_freq.get(point.frequency_hz)
        if measured_point is None:
            continue
        residuals.append(
            (
                point.frequency_hz,
                point.impedance_re_pa_s_m - measured_point.resistance_pa_s_m,
                point.impedance_im_pa_s_m - measured_point.reactance_pa_s_m,
            )
        )
    if not residuals:
        return ModelledVsMeasuredResidual(
            modelled_sha256=evidence.semantic_sha256,
            measured_material_sha256=measured.semantic_sha256,
            common_frequency_range_hz=None,
            notes=('no overlapping frequency grid',),
        )
    frequencies = [entry[0] for entry in residuals]
    return ModelledVsMeasuredResidual(
        modelled_sha256=evidence.semantic_sha256,
        measured_material_sha256=measured.semantic_sha256,
        common_frequency_range_hz=(min(frequencies), max(frequencies)),
        impedance_residual=tuple(residuals),
        notes=(
            'impedance residual on the common grid only; a close fit does '
            'not prove physical parameters uniquely',
            'mounting and incidence conditions of the measured data are '
            'not re-derived here',
        ),
    )


__all__ = [
    'AcousticConstructionDefinition',
    'ConstructionLayer',
    'DerivedImpedancePoint',
    'DerivedMaterialAcousticEvidence',
    'ModelledVsMeasuredResidual',
    'build_acoustic_construction',
    'compare_modelled_vs_measured',
    'derive_material_evidence',
    'evidence_as_specific_impedance',
    'miki_layer_medium',
    'surface_impedance',
]
