"""Resonant acoustic-treatment physical-model authority (issue #704).

Helmholtz, perforated/slotted, microperforated, membrane and panel
absorbers are resonant impedance systems — NOT generic porous
absorbers and NOT adequately represented by a single octave-band
absorption coefficient or one center-frequency/Q pair. A resonant
treatment claim must pin the physical model parameters and the
performance derivation path.

Basis: issue #704 scope; Cox & D'Antonio (resonant absorber design);
Ingard (perforated facing impedance); Maa (MPP theory); #570 evidence
compatibility; #615 porous absorbers; #631 as-built; #694 finite
size.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


ResonantKind = Literal[
    'helmholtz', 'perforated_panel', 'microperforated', 'membrane',
    'panel', 'slotted', 'porous', 'unknown',
]

RESONANT_LABELS: dict[str, str] = {
    'resonant_model_declared': '共振モデルは宣言済み',
    'single_coefficient_inadequate': '単一係数では共振系を表せない',
    'porous_model_misapplied': '多孔質モデルの誤用',
    'insufficient_evidence': '証拠不足',
}


class ResonantAbsorberProfile(BaseModel):
    """Physical resonant-absorber model parameters (res- prefix)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    absorber_kind: ResonantKind
    cavity_depth_m: float | None = None
    perforation_ratio: float | None = None
    hole_diameter_m: float | None = None
    panel_areal_density_kg_m2: float | None = None
    air_gap_m: float | None = None
    target_resonance_hz: float | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ResonantAbsorberProfile':
        if self.absorber_kind == 'unknown':
            raise ValueError('absorber_kind must be declared')
        if self.absorber_kind == 'porous':
            raise ValueError(
                'porous absorbers belong to the porous authority '
                '(#615), not the resonant authority')
        resonant = {
            'helmholtz', 'perforated_panel', 'microperforated',
            'membrane', 'panel', 'slotted',
        }
        if self.absorber_kind in resonant:
            physical = (
                self.cavity_depth_m is not None
                or self.panel_areal_density_kg_m2 is not None
                or (self.perforation_ratio is not None
                    and self.hole_diameter_m is not None)
            )
            if not physical:
                raise ValueError(
                    'resonant kinds need physical model parameters '
                    '(cavity depth / panel mass / perforation geometry)')
        for v in (self.cavity_depth_m, self.hole_diameter_m,
                  self.air_gap_m):
            if v is not None and v <= 0.0:
                raise ValueError('dimensions must be positive')
        if self.perforation_ratio is not None \
                and not 0.0 < self.perforation_ratio <= 1.0:
            raise ValueError('perforation_ratio must be in (0, 1]')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ResonantAbsorberProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'res')


class ResonantPerformanceRecord(BaseModel):
    """Measured/derived resonant performance bound to a physical
    model (rpr- prefix)."""

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    derivation: Literal[
        'measured_impedance_tube', 'measured_reverberation',
        'impedance_model_computed', 'declared_only',
    ]
    absorption_curve_ref: AuthorityRef | None = None
    peak_absorption_hz: float | None = None
    q_factor: float | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ResonantPerformanceRecord':
        _require_refs(self.profile_ref)
        if self.absorption_curve_ref is not None:
            _require_refs(self.absorption_curve_ref)
        if self.derivation == 'declared_only' \
                and (self.peak_absorption_hz is not None
                     or self.q_factor is not None):
            raise ValueError(
                'declared_only cannot carry measured figures')
        if self.q_factor is not None and self.q_factor <= 0.0:
            raise ValueError('q_factor must be positive')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'ResonantPerformanceRecord':
        return _seal(cls, payload, 'record_id', 'record_sha256', 'rpr')


def evaluate_resonance_claim(
    profile: ResonantAbsorberProfile | None,
    claim_kind: str,
) -> tuple[str, str]:
    """One octave-band alpha or a single fc/Q pair never models a
    resonant system."""
    if profile is None:
        return ('insufficient_evidence', 'no_resonant_profile')
    if claim_kind == 'single_octave_alpha':
        return ('single_coefficient_inadequate',
                'resonant_system_needs_model_parameters')
    if claim_kind == 'porous_equivalent':
        return ('porous_model_misapplied',
                'resonant_kind_is_not_porous')
    return ('resonant_model_declared', 'physical_parameters_pinned')
