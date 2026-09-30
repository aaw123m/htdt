"""R160 residual — bounded late-energy decay derivation.

The typed foundation (PR #254/#262) reserves a ``LateEnergyDecay`` component
slot. This module derives the observable's bounded decay-model artifact
from exact late-field inputs:

- ``DeterministicPathArtifact`` — the exact R150 path set. At each recorded
  specular reflection, ``scattering`` of the arriving energy leaves the
  specular channel into the late field; ``absorption`` is lost;
  ``specular_energy_factor`` continues. Per band, per path, the injected
  late energy is ``E0 * sum_k( prod_{j<k} specular_j * scattering_k )``
  with ``E0 = spreading_factor_per_m2 * source_directivity.energy_factor``.
- ``LateFieldInputAuthority`` — per-surface late-field capability
  declarations. Every surface any evaluated path reflects on must declare
  ``scattering_modeled`` (bound ``SurfaceScatteringEvidence``),
  ``specular_only_declared`` (recorded zero contribution), or
  ``diffraction_declared`` (bound evidence plus explicit declared late
  energy). Undeclared/unsupported capability fails closed: the artifact is
  UNSUPPORTED and carries no fabricated samples.
- ``LateEnergyDecayLaw`` — declared per-band decay time constants and the
  bounded evaluation time grid. The decay model is a single bounded
  exponential tail starting at the last deterministic arrival
  (``bounded_exponential_after_last_deterministic_arrival_v1``); it is a
  declared model, never a statistical inference.

The resulting ``LateEnergyDecayArtifact`` carries per-band injected late
energy, per-path audited contributions, and bounded decay samples, and is
exposed to a solver-result envelope manifest under
:data:`R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF` — which is what
``build_hybrid_acoustic_result`` promotes into the
``LateEnergyDecay(state='AVAILABLE')`` component.

Two artifact types bind the ``late_energy_decay`` observable name: this
bounded decay model and the R150 late-field contribution authority
(``cad_late_field_energy``, ``LATE_FIELD_ARTIFACT_SCHEMA_REF`` — per-band
energy upper bounds, not decay samples). Envelopes disambiguate the two
encodings by ``encoding_schema_ref``; ``build_hybrid_acoustic_result`` fails
closed when more than one ``late_energy_decay`` candidate is bound.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from math import exp, isclose, isfinite
from pathlib import Path
import sqlite3
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_solver_result import AcousticSolverObservableArtifact
from .cad_equipment import FrequencyDomain
from .cad_geometric_acoustics_adapter import (
    BoundaryMaterialContribution,
    DeterministicPathArtifact,
)
from .cad_hybrid_grid_reconciliation import (
    HybridNumericalCompositionError,
    HybridNumericalFailureCode,
)
from .cad_repository import SceneRepository
from .cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from .cad_surface_scattering import (
    SurfaceScatteringEvidence,
    ga_scatter_fraction,
    solver_scattering_representation,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _semantic_hash


R160_LATE_FIELD_INPUT_VERSION = 'r160-late-field-input-1'
R160_LATE_DECAY_LAW_VERSION = 'r160-late-energy-decay-law-1'
R160_LATE_ENERGY_ARTIFACT_VERSION = 'r160-late-energy-decay-1'

LateFieldSurfaceCapabilityKind = Literal[
    'scattering_modeled',
    'specular_only_declared',
    'diffraction_declared',
]
LateDecayProvenance = Literal[
    'declared_model',
    'measured',
    'analytic_estimate',
]
LateEnergyCapability = Literal['SUPPORTED', 'UNSUPPORTED']
LATE_ENERGY_DECAY_OBSERVABLE = 'late_energy_decay'
LATE_ENERGY_DECAY_MODEL = (
    'bounded_exponential_after_last_deterministic_arrival_v1'
)

R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF = ExactExternalAuthorityRef(
    authority_id='htdt.r160-late-energy-decay-artifact.schema',
    authority_version=R160_LATE_ENERGY_ARTIFACT_VERSION,
    semantic_hash_sha256=_semantic_hash(
        {
            'schema': 'LateEnergyDecayArtifact',
            'schema_version': 1,
            'quantity': 'late_energy_density_per_m2',
            'decay_model': LATE_ENERGY_DECAY_MODEL,
            'phase_capability': 'NOT_APPLICABLE',
        }
    ),
)


def surface_scattering_evidence_ref(
    evidence: SurfaceScatteringEvidence,
) -> ExactExternalAuthorityRef:
    """Exact external binding of one scattering evidence authority."""

    return ExactExternalAuthorityRef(
        authority_id=evidence.evidence_id,
        authority_version=evidence.authority_version,
        semantic_hash_sha256=evidence.evidence_sha256,
    )


class LateFieldBandEnergy(BaseModel):
    """Late-field energy for one band (declared or computed injection)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    center_hz: float = Field(gt=0.0)
    late_energy_per_m2: float = Field(ge=0.0)


class LateFieldSurfaceCapability(BaseModel):
    """Declared late-field capability of one boundary surface."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    surface_id: str = Field(min_length=1)
    capability: LateFieldSurfaceCapabilityKind
    evidence_ref: ExactExternalAuthorityRef | None = None
    declared_late_energy: tuple[LateFieldBandEnergy, ...] = ()
    detail: str = Field(min_length=1)

    @model_validator(mode='after')
    def contract(self) -> 'LateFieldSurfaceCapability':
        if self.capability == 'scattering_modeled':
            if self.evidence_ref is None:
                raise ValueError(
                    'scattering_modeled surfaces require exact scattering '
                    'evidence binding'
                )
            if self.declared_late_energy:
                raise ValueError(
                    'scattering_modeled surfaces derive late energy from '
                    'specular-path evidence; declared energy is double '
                    'counting'
                )
        elif self.capability == 'specular_only_declared':
            if self.evidence_ref is not None or self.declared_late_energy:
                raise ValueError(
                    'specular_only_declared surfaces carry no evidence or '
                    'declared energy'
                )
        else:  # diffraction_declared
            if self.evidence_ref is None:
                raise ValueError(
                    'diffraction_declared surfaces require exact evidence '
                    'binding'
                )
            if not self.declared_late_energy:
                raise ValueError(
                    'diffraction_declared surfaces require declared '
                    'per-band late energy'
                )
            if any(
                item.late_energy_per_m2 <= 0.0
                for item in self.declared_late_energy
            ):
                raise ValueError(
                    'diffraction_declared late energy must be positive'
                )
            centers = [item.center_hz for item in self.declared_late_energy]
            if centers != sorted(set(centers)):
                raise ValueError(
                    'declared late energy band centers must be unique/sorted'
                )
        return self


class LateFieldInputAuthority(BaseModel):
    """Versioned binding of late-field capability evidence per surface."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-late-field-input-1'
    ] = R160_LATE_FIELD_INPUT_VERSION
    input_id: str = Field(pattern=r'^r160-late-field-input:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    deterministic_path_artifact_ref: ExactExternalAuthorityRef
    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    surface_capabilities: tuple[LateFieldSurfaceCapability, ...]

    @model_validator(mode='after')
    def contract(self) -> 'LateFieldInputAuthority':
        surface_ids = [item.surface_id for item in self.surface_capabilities]
        if surface_ids != sorted(set(surface_ids)):
            raise ValueError(
                'R160 late-field capabilities must be unique/sorted by surface'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 late-field input semantic hash mismatch')
        if self.input_id != f'r160-late-field-input:{expected}':
            raise ValueError('R160 late-field input id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'input_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.input_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

    def capability_for(self, surface_id: str) -> LateFieldSurfaceCapability | None:
        for item in self.surface_capabilities:
            if item.surface_id == surface_id:
                return item
        return None


def build_late_field_input_authority(
    *,
    path_artifact: DeterministicPathArtifact,
    source_entity_id: str,
    receiver_id: str,
    surface_capabilities: Sequence[LateFieldSurfaceCapability],
) -> LateFieldInputAuthority:
    core = {
        'authority_version': R160_LATE_FIELD_INPUT_VERSION,
        'deterministic_path_artifact_ref': (
            path_artifact.as_external_ref().model_dump(mode='json')
        ),
        'source_entity_id': source_entity_id,
        'receiver_id': receiver_id,
        'surface_capabilities': [
            item.model_dump(mode='json')
            for item in sorted(
                surface_capabilities,
                key=lambda item: item.surface_id,
            )
        ],
    }
    digest = _semantic_hash(core)
    return LateFieldInputAuthority(
        input_id=f'r160-late-field-input:{digest}',
        semantic_sha256=digest,
        **core,
    )


class LateFieldBandDecay(BaseModel):
    """Declared decay time constant for one band."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    center_hz: float = Field(gt=0.0)
    decay_time_s: float = Field(gt=0.0)
    provenance: LateDecayProvenance


class LateEnergyDecayLaw(BaseModel):
    """Versioned declared decay law — a bounded exponential model."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r160-late-energy-decay-law-1'
    ] = R160_LATE_DECAY_LAW_VERSION
    law_id: str = Field(pattern=r'^r160-late-energy-decay-law:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    decay_model: Literal[
        'bounded_exponential_after_last_deterministic_arrival_v1'
    ] = LATE_ENERGY_DECAY_MODEL
    bands: tuple[LateFieldBandDecay, ...] = Field(min_length=1)
    decay_time_grid_s: tuple[float, ...] = Field(min_length=1)
    evidence_ref: ExactExternalAuthorityRef | None = None
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def contract(self) -> 'LateEnergyDecayLaw':
        centers = [item.center_hz for item in self.bands]
        if centers != sorted(set(centers)):
            raise ValueError(
                'R160 decay law band centers must be unique/sorted'
            )
        grid = tuple(float(item) for item in self.decay_time_grid_s)
        if grid != tuple(sorted(set(grid))) or any(
            not isfinite(item) or item < 0.0 for item in grid
        ):
            raise ValueError(
                'R160 decay time grid must be non-negative/unique/sorted'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 decay law semantic hash mismatch')
        if self.law_id != f'r160-late-energy-decay-law:{expected}':
            raise ValueError('R160 decay law id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'law_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.law_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

    def decay_time_for(self, center_hz: float) -> float | None:
        for band in self.bands:
            if band.center_hz == center_hz:
                return band.decay_time_s
        return None


def build_late_energy_decay_law(
    *,
    bands: Sequence[LateFieldBandDecay],
    decay_time_grid_s: Sequence[float],
    evidence_ref: ExactExternalAuthorityRef | None = None,
    rationale: str,
) -> LateEnergyDecayLaw:
    core = {
        'authority_version': R160_LATE_DECAY_LAW_VERSION,
        'decay_model': LATE_ENERGY_DECAY_MODEL,
        'bands': [
            item.model_dump(mode='json')
            for item in sorted(bands, key=lambda item: item.center_hz)
        ],
        'decay_time_grid_s': [
            float(item) for item in decay_time_grid_s
        ],
        'evidence_ref': (
            None if evidence_ref is None
            else evidence_ref.model_dump(mode='json')
        ),
        'rationale': rationale,
    }
    digest = _semantic_hash(core)
    return LateEnergyDecayLaw(
        law_id=f'r160-late-energy-decay-law:{digest}',
        semantic_sha256=digest,
        **core,
    )


class LateEnergyDecaySample(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    time_s: float = Field(ge=0.0)
    late_energy_per_m2: float = Field(ge=0.0)

    @model_validator(mode='after')
    def consistent(self) -> 'LateEnergyDecaySample':
        if not isfinite(float(self.time_s)) or not isfinite(
            float(self.late_energy_per_m2)
        ):
            raise ValueError('R160 late-energy sample must be finite')
        return self


class LatePathContribution(BaseModel):
    """Audited per-path late-energy injection record."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    deterministic_path_id: str = Field(min_length=1)
    path_type: str = Field(min_length=1)
    injected_late_energy: tuple[LateFieldBandEnergy, ...] = ()


class LateEnergyBandResult(BaseModel):
    """Computed late-energy decay for one exact band."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    center_hz: float = Field(gt=0.0)
    injected_late_energy_per_m2: float = Field(ge=0.0)
    specular_path_energy_per_m2: float = Field(ge=0.0)
    diffraction_declared_energy_per_m2: float = Field(ge=0.0)
    decay_time_s: float = Field(gt=0.0)
    decay_provenance: LateDecayProvenance
    samples: tuple[LateEnergyDecaySample, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self) -> 'LateEnergyBandResult':
        for value in (
            self.injected_late_energy_per_m2,
            self.specular_path_energy_per_m2,
            self.diffraction_declared_energy_per_m2,
            self.decay_time_s,
        ):
            if not isfinite(float(value)):
                raise ValueError('R160 late-energy band values must be finite')
        return self


class LateEnergyDecayArtifact(BaseModel):
    """Bounded late-energy decay artifact (observable ``late_energy_decay``)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    authority_version: Literal[
        'r160-late-energy-decay-1'
    ] = R160_LATE_ENERGY_ARTIFACT_VERSION
    artifact_id: str = Field(
        pattern=r'^r160-late-energy-decay:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    deterministic_path_artifact_ref: ExactExternalAuthorityRef
    late_field_input_ref: ExactExternalAuthorityRef
    late_field_input: LateFieldInputAuthority
    decay_law: LateEnergyDecayLaw
    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)

    quantity: Literal['late_energy_density_per_m2'] = (
        'late_energy_density_per_m2'
    )
    decay_model: Literal[
        'bounded_exponential_after_last_deterministic_arrival_v1'
    ] = LATE_ENERGY_DECAY_MODEL
    late_field_start_s: float = Field(gt=0.0)
    valid_frequency_domain: FrequencyDomain
    phase_capability: Literal['NOT_APPLICABLE'] = 'NOT_APPLICABLE'

    capability_state: LateEnergyCapability
    unsupported_reasons: tuple[str, ...] = ()
    bands: tuple[LateEnergyBandResult, ...] = ()
    path_contributions: tuple[LatePathContribution, ...] = ()

    @model_validator(mode='after')
    def contract(self) -> 'LateEnergyDecayArtifact':
        if self.late_field_input.as_external_ref() != self.late_field_input_ref:
            raise ValueError('R160 late-energy input binding mismatch')
        if self.late_field_input.deterministic_path_artifact_ref != (
            self.deterministic_path_artifact_ref
        ):
            raise ValueError(
                'R160 late-energy input does not pin the path artifact'
            )
        if self.capability_state == 'SUPPORTED':
            if self.unsupported_reasons:
                raise ValueError(
                    'supported late-energy artifact cannot carry reasons'
                )
            if not self.bands:
                raise ValueError('supported late-energy artifact needs bands')
        else:
            if (
                not self.unsupported_reasons
                or self.bands
                or self.path_contributions
            ):
                raise ValueError(
                    'unsupported late-energy artifact requires reasons and '
                    'no bands'
                )
        if self.bands:
            centers = [item.center_hz for item in self.bands]
            if centers != sorted(set(centers)):
                raise ValueError('R160 late-energy bands must be sorted')
            if (
                float(self.valid_frequency_domain.minimum_hz) != centers[0]
                or float(self.valid_frequency_domain.maximum_hz)
                != centers[-1]
            ):
                raise ValueError(
                    'R160 late-energy frequency domain must bound the bands'
                )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('R160 late-energy artifact semantic hash mismatch')
        if self.artifact_id != f'r160-late-energy-decay:{expected}':
            raise ValueError('R160 late-energy artifact id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'artifact_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.artifact_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )

    def as_solver_observable(
        self,
        encoding_schema_ref: ExactExternalAuthorityRef | None = None,
    ) -> AcousticSolverObservableArtifact:
        return AcousticSolverObservableArtifact(
            observable=LATE_ENERGY_DECAY_OBSERVABLE,
            artifact_authority=self.as_external_ref(),
            encoding_schema_ref=(
                encoding_schema_ref or R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF
            ),
            valid_frequency_domain=self.valid_frequency_domain,
        )


def late_energy_decay_observable_manifest(
    artifact: LateEnergyDecayArtifact,
) -> AcousticSolverObservableArtifact:
    """Expose a persisted decay artifact as the R160 late-energy observable."""
    return artifact.as_solver_observable()


def _band_materials(
    band: Any,
) -> tuple[BoundaryMaterialContribution, ...]:
    """surface-ordered material contributions of one path band."""

    materials: list[BoundaryMaterialContribution] = []
    if band.boundary_materials is not None:
        materials.extend(band.boundary_materials)
    elif band.boundary_material is not None:
        materials.append(band.boundary_material)
    return tuple(materials)


def solve_late_energy_decay(
    *,
    path_artifact: DeterministicPathArtifact,
    late_field_input: LateFieldInputAuthority,
    decay_law: LateEnergyDecayLaw,
    scattering_evidence: Mapping[str, SurfaceScatteringEvidence],
) -> LateEnergyDecayArtifact:
    """Derive the bounded late-energy decay artifact, fail-closed."""

    path_artifact = DeterministicPathArtifact.model_validate(
        path_artifact.model_dump(mode='python')
    )
    late_field_input = LateFieldInputAuthority.model_validate(
        late_field_input.model_dump(mode='python')
    )
    decay_law = LateEnergyDecayLaw.model_validate(
        decay_law.model_dump(mode='python')
    )
    evidence_map = {
        key: SurfaceScatteringEvidence.model_validate(
            item.model_dump(mode='python')
        )
        for key, item in scattering_evidence.items()
    }
    if late_field_input.deterministic_path_artifact_ref != (
        path_artifact.as_external_ref()
    ):
        raise ValueError(
            'R160 late-field input is not bound to this exact path artifact'
        )
    paths = [
        path
        for path in path_artifact.paths
        if path.source_entity_id == late_field_input.source_entity_id
        and path.receiver_id == late_field_input.receiver_id
    ]
    if not paths:
        raise ValueError(
            'R160 late-energy solve found no deterministic paths for the '
            'declared source/receiver pair'
        )
    band_sets = {
        tuple(float(band.center_hz) for band in path.bands) for path in paths
    }
    if len(band_sets) != 1:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
            'R160 late-energy solve requires one shared band set across paths',
        )
    bands_hz = tuple(sorted(next(iter(band_sets))))

    law_centers = tuple(band.center_hz for band in decay_law.bands)
    if law_centers != bands_hz:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
            'R160 decay law must declare exactly the evaluated band centers',
        )
    time_grid = tuple(float(item) for item in decay_law.decay_time_grid_s)
    late_start = max(float(path.propagation_delay_s) for path in paths)
    if time_grid[0] < late_start:
        raise HybridNumericalCompositionError(
            HybridNumericalFailureCode.INVALID_GRID,
            'R160 decay time grid must start at or after the last '
            'deterministic arrival; earlier samples would fabricate the '
            'unseparated field',
        )

    reasons: list[str] = []
    # Capability audit over every surface any evaluated path reflects on.
    touched_surfaces = sorted(
        {
            surface_id
            for path in paths
            for surface_id in path.ordered_interaction_surface_ids
        }
    )
    capability_by_surface: dict[str, LateFieldSurfaceCapability] = {}
    for surface_id in touched_surfaces:
        capability = late_field_input.capability_for(surface_id)
        if capability is None:
            reasons.append(
                f'surface {surface_id} lacks a late-field capability '
                'declaration'
            )
            continue
        capability_by_surface[surface_id] = capability
        if capability.capability == 'scattering_modeled':
            assert capability.evidence_ref is not None
            evidence = evidence_map.get(
                capability.evidence_ref.authority_id
            )
            if evidence is None:
                reasons.append(
                    f'surface {surface_id} scattering evidence '
                    f'{capability.evidence_ref.authority_id} is missing'
                )
                continue
            if evidence.surface_id != surface_id:
                reasons.append(
                    f'surface {surface_id} scattering evidence binds a '
                    'different surface'
                )
                continue
            representation = solver_scattering_representation(evidence)
            if representation != 'scalar_coefficient':
                reasons.append(
                    f'surface {surface_id} scattering representation is '
                    f'{representation}; the bounded solver consumes only '
                    'scalar scatter fractions'
                )
        elif capability.capability == 'specular_only_declared':
            pass
        else:  # diffraction_declared
            declared_centers = tuple(
                item.center_hz for item in capability.declared_late_energy
            )
            if declared_centers != bands_hz:
                reasons.append(
                    f'surface {surface_id} declared diffraction energy does '
                    'not cover the evaluated band set'
                )

    # Consistency audit: specular_only surfaces must record zero scattering
    # in the exact path artifact; scattering_modeled surfaces must record a
    # positive scatter fraction consistent with solver consumption.
    for path in paths:
        surfaces = tuple(path.ordered_interaction_surface_ids)
        for band in path.bands:
            materials = _band_materials(band)
            if len(materials) != len(surfaces):
                raise HybridNumericalCompositionError(
                    HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
                    'R160 path band material contributions do not match the '
                    'ordered reflection sequence',
                )
            for surface_id, material in zip(surfaces, materials):
                if material.source_surface_id != surface_id:
                    raise HybridNumericalCompositionError(
                        HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
                        'R160 path band material contribution surface order '
                        'is inconsistent',
                    )
                capability = capability_by_surface.get(surface_id)
                if capability is None:
                    continue
                if (
                    capability.capability == 'specular_only_declared'
                    and float(material.scattering) > 0.0
                ):
                    reasons.append(
                        f'surface {surface_id} is declared specular-only but '
                        f'path {path.path_id} records scattering '
                        f'{material.scattering} at {band.center_hz} Hz'
                    )
                if capability.capability == 'scattering_modeled':
                    evidence = evidence_map.get(
                        capability.evidence_ref.authority_id
                        if capability.evidence_ref is not None
                        else ''
                    )
                    if evidence is not None and (
                        solver_scattering_representation(evidence)
                        == 'scalar_coefficient'
                    ):
                        scalar = ga_scatter_fraction(evidence)
                        assert scalar is not None
                        if (scalar > 0.0) != (
                            float(material.scattering) > 0.0
                        ):
                            reasons.append(
                                f'surface {surface_id} scattering evidence '
                                'and the recorded path contribution disagree '
                                f'at {band.center_hz} Hz'
                            )

    path_contributions: list[dict[str, Any]] = []
    band_injected = {center: 0.0 for center in bands_hz}
    band_specular = {center: 0.0 for center in bands_hz}
    if not reasons:
        for path in paths:
            injected: list[dict[str, Any]] = []
            surfaces = tuple(path.ordered_interaction_surface_ids)
            for band in path.bands:
                center = float(band.center_hz)
                e0 = (
                    float(band.spreading_factor_per_m2)
                    * float(band.source_directivity.energy_factor)
                )
                materials = _band_materials(band)
                arriving = e0
                late = 0.0
                for material in materials:
                    late += arriving * float(material.scattering)
                    arriving *= float(material.specular_energy_factor)
                if not isclose(
                    arriving,
                    float(band.relative_energy_transport_per_m2),
                    rel_tol=1e-4,
                    abs_tol=1e-12,
                ):
                    raise HybridNumericalCompositionError(
                        HybridNumericalFailureCode.INPUT_CAPABILITY_MISMATCH,
                        'R160 path energy chain does not reproduce the '
                        'recorded receiver transport',
                    )
                band_injected[center] += late
                band_specular[center] += float(
                    band.relative_energy_transport_per_m2
                )
                injected.append(
                    {
                        'center_hz': center,
                        'late_energy_per_m2': late,
                    }
                )
            path_contributions.append(
                {
                    'deterministic_path_id': path.path_id,
                    'path_type': path.path_type,
                    'injected_late_energy': injected,
                }
            )

    base: dict[str, Any] = {
        'schema_version': 1,
        'authority_version': R160_LATE_ENERGY_ARTIFACT_VERSION,
        'deterministic_path_artifact_ref': (
            path_artifact.as_external_ref().model_dump(mode='json')
        ),
        'late_field_input_ref': (
            late_field_input.as_external_ref().model_dump(mode='json')
        ),
        'late_field_input': late_field_input.model_dump(mode='json'),
        'decay_law': decay_law.model_dump(mode='json'),
        'source_entity_id': late_field_input.source_entity_id,
        'receiver_id': late_field_input.receiver_id,
        'quantity': 'late_energy_density_per_m2',
        'decay_model': LATE_ENERGY_DECAY_MODEL,
        'late_field_start_s': late_start,
        'valid_frequency_domain': {
            'minimum_hz': bands_hz[0],
            'maximum_hz': bands_hz[-1],
        },
        'phase_capability': 'NOT_APPLICABLE',
    }

    if reasons:
        core = {
            **base,
            'capability_state': 'UNSUPPORTED',
            'unsupported_reasons': sorted(set(reasons)),
            'bands': [],
            'path_contributions': [],
        }
    else:
        diffraction_energy = {center: 0.0 for center in bands_hz}
        for capability in capability_by_surface.values():
            if capability.capability != 'diffraction_declared':
                continue
            for item in capability.declared_late_energy:
                diffraction_energy[item.center_hz] += float(
                    item.late_energy_per_m2
                )
        band_results: list[dict[str, Any]] = []
        for band_law in decay_law.bands:
            center = band_law.center_hz
            injected_total = (
                band_injected[center] + diffraction_energy[center]
            )
            tau = float(band_law.decay_time_s)
            band_results.append(
                {
                    'center_hz': center,
                    'injected_late_energy_per_m2': injected_total,
                    'specular_path_energy_per_m2': band_specular[center],
                    'diffraction_declared_energy_per_m2': (
                        diffraction_energy[center]
                    ),
                    'decay_time_s': tau,
                    'decay_provenance': band_law.provenance,
                    'samples': [
                        {
                            'time_s': time_s,
                            'late_energy_per_m2': (
                                injected_total
                                * exp(-(time_s - late_start) / tau)
                            ),
                        }
                        for time_s in time_grid
                    ],
                }
            )
        core = {
            **base,
            'capability_state': 'SUPPORTED',
            'unsupported_reasons': [],
            'bands': band_results,
            'path_contributions': path_contributions,
        }

    digest = _semantic_hash(core)
    return LateEnergyDecayArtifact(
        artifact_id=f'r160-late-energy-decay:{digest}',
        semantic_sha256=digest,
        **core,
    )


PathArtifactResolver = Callable[[str], DeterministicPathArtifact | None]
ScatteringEvidenceResolver = Callable[
    [str], SurfaceScatteringEvidence | None
]


class CadLateEnergyDecayRepository:
    """Append-only late-energy persistence with exact stale rejection."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        path_artifact_resolver: PathArtifactResolver,
        scattering_evidence_resolver: ScatteringEvidenceResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self.path_artifact_resolver = path_artifact_resolver
        self.scattering_evidence_resolver = scattering_evidence_resolver
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'r160_late_energy_decay_artifacts')

    def _rebuild(
        self,
        artifact: LateEnergyDecayArtifact,
    ) -> LateEnergyDecayArtifact:
        artifact = LateEnergyDecayArtifact.model_validate(
            artifact.model_dump(mode='python')
        )
        path_artifact = self.path_artifact_resolver(
            artifact.deterministic_path_artifact_ref.authority_id
        )
        if (
            path_artifact is None
            or path_artifact.as_external_ref()
            != artifact.deterministic_path_artifact_ref
        ):
            raise ValueError(
                'R160 late-energy deterministic path artifact is missing/stale'
            )
        evidences: dict[str, SurfaceScatteringEvidence] = {}
        for capability in artifact.late_field_input.surface_capabilities:
            if capability.evidence_ref is None:
                continue
            evidence = self.scattering_evidence_resolver(
                capability.evidence_ref.authority_id
            )
            if evidence is None or (
                surface_scattering_evidence_ref(evidence)
                != capability.evidence_ref
            ):
                raise ValueError(
                    'R160 late-energy scattering evidence is missing/stale'
                )
            evidences[capability.evidence_ref.authority_id] = evidence
        rebuilt = solve_late_energy_decay(
            path_artifact=path_artifact,
            late_field_input=artifact.late_field_input,
            decay_law=artifact.decay_law,
            scattering_evidence=evidences,
        )
        if rebuilt != artifact:
            raise ValueError(
                'R160 persisted late-energy artifact no longer reproduces exactly'
            )
        return rebuilt

    def save(self, artifact: LateEnergyDecayArtifact) -> LateEnergyDecayArtifact:
        artifact = self._rebuild(artifact)
        payload = _canonical_json(artifact.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT semantic_sha256, payload_json '
                'FROM r160_late_energy_decay_artifacts WHERE artifact_id=?',
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing['semantic_sha256'] != artifact.semantic_sha256
                    or existing['payload_json'] != payload
                ):
                    raise ValueError(
                        'R160 late-energy artifact identity collision'
                    )
                return artifact
            connection.execute(
                'INSERT INTO r160_late_energy_decay_artifacts '
                '(artifact_id, semantic_sha256, late_field_input_id, '
                'payload_json) VALUES (?, ?, ?, ?)',
                (
                    artifact.artifact_id,
                    artifact.semantic_sha256,
                    artifact.late_field_input.input_id,
                    payload,
                ),
            )
        return artifact

    def get(self, artifact_id: str) -> LateEnergyDecayArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM r160_late_energy_decay_artifacts '
                'WHERE artifact_id=?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._rebuild(
            LateEnergyDecayArtifact.model_validate_json(row['payload_json'])
        )


__all__ = [
    'CadLateEnergyDecayRepository',
    'LATE_ENERGY_DECAY_MODEL',
    'LATE_ENERGY_DECAY_OBSERVABLE',
    'R160_LATE_ENERGY_ARTIFACT_SCHEMA_REF',
    'LateEnergyBandResult',
    'LateEnergyDecayArtifact',
    'LateEnergyDecayLaw',
    'LateEnergyDecaySample',
    'LateEnergyCapability',
    'LateFieldBandDecay',
    'LateFieldBandEnergy',
    'LateFieldInputAuthority',
    'LateFieldSurfaceCapability',
    'LateFieldSurfaceCapabilityKind',
    'LatePathContribution',
    'R160_LATE_DECAY_LAW_VERSION',
    'R160_LATE_ENERGY_ARTIFACT_VERSION',
    'R160_LATE_FIELD_INPUT_VERSION',
    'build_late_energy_decay_law',
    'build_late_field_input_authority',
    'late_energy_decay_observable_manifest',
    'solve_late_energy_decay',
    'surface_scattering_evidence_ref',
]
