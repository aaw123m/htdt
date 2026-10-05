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

Runtime emit points (the manifest is no longer test-only):

* ``scripts/run_r130a_candidate_wave_execution.py`` declares the manifest for
  the PFFDTD candidate descriptor when the descriptor itself is persisted —
  every r130x lane shares that fixture;
* ``PffdtdCandidateWaveExecutor.execute`` re-derives and persists the
  manifest at result-commit with the envelope's produced observables —
  a descriptor/result divergence fails closed instead of recording an
  overclaim. The polyhedral executor routes through the same helper.
"""

from __future__ import annotations

from typing import Iterable, Literal, Sequence

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


# ---------------------------------------------------------------------------
# Honest row derivation from a persisted adapter descriptor
# ---------------------------------------------------------------------------

#: What one declared observable directly evidences, per acoustic domain:
#: ``{domain: {observable: {phenomenon: (state, bound_description|None)}}}``.
#: An observable only ever evidences phenomena the artifact it names is a
#: direct measurement/synthesis of — anything not listed here must not be
#: upgraded from UNSUPPORTED by declaration alone.
_OBSERVABLE_EVIDENCE: dict[str, dict[str, dict[str, tuple[str, str | None]]]] = {
    'wave': {
        # A complex-pressure solve carries the full coherent wave field at
        # the receivers: direct propagation, wall reflections, modal buildup
        # and phase — all inside the declared band. Diffraction and
        # scattering exist in the solution but their fidelity is bounded by
        # what the grid/compiled boundary can represent, so they record
        # BOUNDED rather than SUPPORTED.
        'complex_pressure': {
            'direct_sound': ('SUPPORTED', None),
            'specular_reflection': ('SUPPORTED', None),
            'edge_diffraction': (
                'BOUNDED',
                'resolved by the wave grid only down to its spatial '
                'discretization',
            ),
            'scattering': (
                'BOUNDED',
                'carried only by boundary/material heterogeneity present '
                'in the compiled geometry',
            ),
            'low_frequency_modal_response': ('SUPPORTED', None),
            'coherent_phase': ('SUPPORTED', None),
        },
        'impulse_response': {
            'direct_sound': ('SUPPORTED', None),
            'specular_reflection': ('SUPPORTED', None),
            'late_energy_decay': (
                'BOUNDED',
                'bounded by the finite simulated duration of the impulse '
                'response',
            ),
            'low_frequency_modal_response': ('SUPPORTED', None),
            'coherent_phase': ('SUPPORTED', None),
        },
        'phase_response': {
            'coherent_phase': ('SUPPORTED', None),
        },
        'magnitude_response': {
            'direct_sound': ('SUPPORTED', None),
        },
        'spatial_pressure_field': {
            'spatial_pressure_field': ('SUPPORTED', None),
            'direct_sound': ('SUPPORTED', None),
        },
    },
    'geometric': {
        # Deterministic path artifacts are energy/time path ensembles over
        # the compiled region/portal graph — no coherent phase, no modal
        # resolution, no diffraction synthesis on this domain.
        'deterministic_paths': {
            'direct_sound': ('SUPPORTED', None),
            'specular_reflection': (
                'BOUNDED',
                'bounded by the maximum traced interaction order',
            ),
            'portal_region_coupling': (
                'BOUNDED',
                'coupled only through the declared region/portal '
                'traversal paths',
            ),
        },
        'late_decay_estimate': {
            'late_energy_decay': (
                'BOUNDED',
                'bounded stochastic estimate over the traced path '
                'ensemble',
            ),
        },
        'late_energy_decay': {
            'late_energy_decay': (
                'BOUNDED',
                'bounded stochastic estimate over the traced path '
                'ensemble',
            ),
        },
        'impulse_response': {
            'direct_sound': ('SUPPORTED', None),
            'late_energy_decay': (
                'BOUNDED',
                'bounded by the finite synthesized impulse response '
                'duration',
            ),
        },
        'magnitude_response': {
            'direct_sound': ('SUPPORTED', None),
        },
        'spatial_pressure_field': {
            'spatial_pressure_field': ('SUPPORTED', None),
        },
    },
}

#: Phenomena a domain cannot evidence at all — recorded UNSUPPORTED with an
#: explicit domain reason instead of the generic "no evidence" one.
_DOMAIN_UNSUPPORTED_REASONS: dict[str, dict[str, str]] = {
    'geometric': {
        'coherent_phase': (
            'geometric path ensembles carry energy/time only — no coherent '
            'phase synthesis'
        ),
        'low_frequency_modal_response': (
            'geometric approximation does not resolve modal response'
        ),
        'edge_diffraction': (
            'deterministic path model does not synthesize edge diffraction'
        ),
        'scattering': (
            'no stochastic/scattering observable is declared on this path'
        ),
    },
    'wave': {
        'portal_region_coupling': (
            'this solver path does not bind region/portal coupling '
            'authorities'
        ),
    },
}


def derive_solver_capability_rows(
    descriptor: AcousticSolverAdapterDescriptor,
    *,
    produced_observables: Iterable[str] | None = None,
) -> tuple[SolverCapabilityRow, ...]:
    """Derive honest capability rows from a persisted adapter descriptor.

    SUPPORTED/BOUNDED is recorded only where the descriptor's own declared
    ``supported_observables`` provide direct artifact evidence for the
    phenomenon; every other phenomenon records UNSUPPORTED with its reason —
    never silence and never an overclaim.

    ``produced_observables`` is supplied at result-commit emit points and
    narrows the evidence basis to what the execution actually produced:
    phenomena whose only evidence is a declared-but-unproduced observable
    degrade to UNSUPPORTED with an explicit reason, and any produced
    observable the descriptor never declared is a descriptor/result
    divergence that fails closed instead of being recorded.
    """
    descriptor = AcousticSolverAdapterDescriptor.model_validate(
        descriptor.model_dump(mode='python')
    )
    if produced_observables is None:
        observables = descriptor.supported_observables
        unevidenced_reason = (
            'no declared observable on this solver path evidences '
        )
    else:
        undeclared = sorted(
            set(produced_observables) - set(descriptor.supported_observables)
        )
        if undeclared:
            raise ValueError(
                'solver result produced observables the adapter descriptor '
                f'does not declare: {undeclared}'
            )
        observables = tuple(
            observable
            for observable in descriptor.supported_observables
            if observable in set(produced_observables)
        )
        unevidenced_reason = (
            'no produced observable on this execution evidences '
        )
    domain_evidence = _OBSERVABLE_EVIDENCE.get(descriptor.acoustic_domain, {})
    domain_reasons = _DOMAIN_UNSUPPORTED_REASONS.get(
        descriptor.acoustic_domain, {}
    )
    # Strongest claim wins; a SUPPORTED evidence overrules a BOUNDED one.
    claims: dict[str, tuple[str, str | None]] = {}
    for observable in observables:
        for phenomenon, claim in domain_evidence.get(observable, {}).items():
            state, bound = claim
            existing = claims.get(phenomenon)
            if existing is None or existing[0] == 'BOUNDED':
                if state == 'SUPPORTED' or existing is None:
                    claims[phenomenon] = claim
                elif existing[0] == 'BOUNDED' and bound is not None:
                    claims[phenomenon] = (
                        'BOUNDED',
                        f'{existing[1]}; {bound}',
                    )
    rows: list[SolverCapabilityRow] = []
    for phenomenon in SOLVER_PATH_PHENOMENA:
        claim = claims.get(phenomenon)
        if claim is not None:
            state, bound = claim
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,
                    state=state,  # type: ignore[arg-type]
                    valid_frequency_domain=descriptor.valid_frequency_domain,
                    bound_description=bound,
                )
            )
            continue
        reason = domain_reasons.get(
            phenomenon,
            f'{unevidenced_reason}{phenomenon}',
        )
        rows.append(
            SolverCapabilityRow(
                phenomenon=phenomenon,  # type: ignore[arg-type]
                state='UNSUPPORTED',
                reasons=(reason,),
            )
        )
    return tuple(
        sorted(rows, key=lambda row: str(row.phenomenon))
    )
