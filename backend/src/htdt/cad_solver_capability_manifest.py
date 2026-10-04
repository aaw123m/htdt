"""Declared per-solver-path capability manifest.

Each solver adapter descriptor declares supported observables and a valid
frequency domain, and dispatch bindings decide READY/BLOCKED/UNSUPPORTED per
request — but nothing declared the *phenomenon* coverage of a solver path
(direct sound, specular reflection, scattering, diffraction, LF modal
response, late decay, coherent phase, spatial field, portal coupling).

This manifest is that record: one typed, hash-bound, serializable authority
per adapter descriptor, with one row per phenomenon. Fail-closed rules:

* every phenomenon must be declared exactly once — a manifest cannot omit a
  phenomenon (no silent gaps);
* SUPPORTED/BOUNDED rows carry a valid frequency domain contained in the
  adapter's declared domain;
* BOUNDED rows must carry a bound description;
* UNSUPPORTED rows carry at least one reason and no valid band — the honest
  limit is recorded instead of silence.
"""

from __future__ import annotations

from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_solver_adapter import (
    AcousticSolverAdapterDescriptor,
    AcousticSolverDomain,
    _domain_contains,
)
from .cad_equipment import FrequencyDomain
from .canonical_json import canonical_sha256 as _semantic_hash

SOLVER_CAPABILITY_MANIFEST_AUTHORITY_VERSION = '1'

SolverPathPhenomenon = Literal[
    'direct_sound',
    'specular_reflection',
    'edge_diffraction',
    'scattering',
    'low_frequency_modal_response',
    'late_energy_decay',
    'coherent_phase',
    'spatial_pressure_field',
    'portal_region_coupling',
]

SOLVER_PATH_PHENOMENA: tuple[str, ...] = (
    'direct_sound',
    'specular_reflection',
    'edge_diffraction',
    'scattering',
    'low_frequency_modal_response',
    'late_energy_decay',
    'coherent_phase',
    'spatial_pressure_field',
    'portal_region_coupling',
)

SolverCapabilityState = Literal['SUPPORTED', 'BOUNDED', 'UNSUPPORTED']


class SolverCapabilityRow(BaseModel):
    """One phenomenon declaration inside a solver-path capability manifest."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    phenomenon: SolverPathPhenomenon
    state: SolverCapabilityState
    valid_frequency_domain: FrequencyDomain | None = None
    bound_description: str | None = Field(default=None, min_length=1)
    reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_row(self) -> 'SolverCapabilityRow':
        if self.state == 'UNSUPPORTED':
            if not self.reasons:
                raise ValueError(
                    'UNSUPPORTED capability row requires at least one reason'
                )
            if self.valid_frequency_domain is not None:
                raise ValueError(
                    'UNSUPPORTED capability row cannot declare a valid band'
                )
            if self.bound_description is not None:
                raise ValueError(
                    'UNSUPPORTED capability row cannot declare a bound description'
                )
        else:
            if self.valid_frequency_domain is None:
                raise ValueError(
                    'SUPPORTED/BOUNDED capability row requires a valid '
                    'frequency domain'
                )
        if self.state == 'BOUNDED' and self.bound_description is None:
            raise ValueError(
                'BOUNDED capability row requires a bound description'
            )
        if self.state == 'SUPPORTED' and self.bound_description is not None:
            raise ValueError(
                'SUPPORTED capability row cannot carry a bound description'
            )
        return self


class SolverCapabilityManifest(BaseModel):
    """Declared phenomenon coverage for one solver-adapter path."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['1'] = SOLVER_CAPABILITY_MANIFEST_AUTHORITY_VERSION
    manifest_id: str = Field(
        pattern=r'^solver-capability-manifest:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    adapter_descriptor_id: str = Field(
        pattern=r'^acoustic-solver-adapter:[0-9a-f]{64}$'
    )
    adapter_descriptor_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    model_solver_role_id: str = Field(min_length=1)
    acoustic_domain: AcousticSolverDomain
    solver_valid_frequency_domain: FrequencyDomain
    rows: tuple[SolverCapabilityRow, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_manifest(self) -> 'SolverCapabilityManifest':
        phenomena = [row.phenomenon for row in self.rows]
        if sorted(phenomena) != sorted(SOLVER_PATH_PHENOMENA):
            missing = sorted(set(SOLVER_PATH_PHENOMENA) - set(phenomena))
            duplicated = sorted(
                {item for item in phenomena if phenomena.count(item) > 1}
            )
            raise ValueError(
                'solver capability manifest must declare every phenomenon '
                'exactly once '
                f'(missing={missing}, duplicated={duplicated})'
            )
        if self.rows != tuple(
            sorted(self.rows, key=lambda row: str(row.phenomenon))
        ):
            raise ValueError(
                'solver capability manifest rows must be sorted by phenomenon'
            )
        for row in self.rows:
            if row.valid_frequency_domain is not None and not _domain_contains(
                self.solver_valid_frequency_domain,
                row.valid_frequency_domain,
            ):
                raise ValueError(
                    f'capability row {row.phenomenon} declares a band outside '
                    'the adapter valid frequency domain'
                )
        payload = self.model_dump(
            mode='json', exclude={'manifest_id', 'semantic_sha256'}
        )
        expected = _semantic_hash(payload)
        if self.semantic_sha256 != expected:
            raise ValueError('solver capability manifest hash mismatch')
        if self.manifest_id != f'solver-capability-manifest:{expected}':
            raise ValueError('solver capability manifest id mismatch')
        return self

    def row_for(self, phenomenon: SolverPathPhenomenon) -> SolverCapabilityRow:
        return next(
            row for row in self.rows if row.phenomenon == phenomenon
        )


def build_solver_capability_manifest(
    *,
    descriptor: AcousticSolverAdapterDescriptor,
    rows: Sequence[SolverCapabilityRow],
) -> SolverCapabilityManifest:
    """Declare the phenomenon manifest for a persisted solver adapter path.

    Rows are canonicalized (sorted by phenomenon) and the manifest binds the
    descriptor by exact id and semantic hash, so a stored manifest always
    names the precise adapter authority it describes.
    """

    normalized = tuple(
        sorted(
            (SolverCapabilityRow.model_validate(row) for row in rows),
            key=lambda row: str(row.phenomenon),
        )
    )
    payload = {
        'authority_version': SOLVER_CAPABILITY_MANIFEST_AUTHORITY_VERSION,
        'adapter_descriptor_id': descriptor.descriptor_id,
        'adapter_descriptor_semantic_sha256': descriptor.semantic_sha256,
        'adapter_id': descriptor.adapter_id,
        'adapter_version': descriptor.adapter_version,
        'model_solver_role_id': descriptor.model_solver_role_id,
        'acoustic_domain': descriptor.acoustic_domain,
        'solver_valid_frequency_domain': (
            descriptor.valid_frequency_domain.model_dump(mode='json')
        ),
        'rows': [row.model_dump(mode='json') for row in normalized],
    }
    digest = _semantic_hash(payload)
    return SolverCapabilityManifest(
        manifest_id=f'solver-capability-manifest:{digest}',
        semantic_sha256=digest,
        adapter_descriptor_id=descriptor.descriptor_id,
        adapter_descriptor_semantic_sha256=descriptor.semantic_sha256,
        adapter_id=descriptor.adapter_id,
        adapter_version=descriptor.adapter_version,
        model_solver_role_id=descriptor.model_solver_role_id,
        acoustic_domain=descriptor.acoustic_domain,
        solver_valid_frequency_domain=descriptor.valid_frequency_domain,
        rows=normalized,
    )
