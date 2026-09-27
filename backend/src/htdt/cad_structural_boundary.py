"""Compliant room-boundary / vibroacoustic coupling authority (#1008).

Room boundaries are not all the same physics. A perfectly rigid wall, a
locally-reacting surface impedance, and a flexible gypsum partition that
resonates and re-radiates are three different capability classes — and
treating them interchangeably silently loses structural resonance, the
very thing that dominates low-frequency room response.

This module keeps the classes separate:

- :class:`BoundaryCapabilityClass` distinguishes ``rigid``,
  ``locally_reacting_impedance`` and ``structurally_compliant``
  boundaries. A structural model only exists for the compliant class.
- :class:`StructuralBoundaryModel` records one compliant boundary: its
  structural kind (thin plate, shell, lumped modal panel, measured
  mobility, measured effective boundary, external FE, opaque), areal
  mass, stiffness and damping, edge condition, cavity/backing, which
  sides couple, and its valid frequency domain. Constants are evidence —
  a ``ConstructionAssembly`` (materials + layers) does *not* auto-imply
  them.
- :class:`DerivedEffectiveImpedance` records a structural→impedance
  derivation *with its structural lineage* — the boundary model, provider
  and parameters it was computed from — so downstream consumers can tell
  a real compliant-boundary result from an asserted number.
- :func:`evaluate_boundary_capability` reports whether a provider can
  honour a boundary's declared class — a compliant boundary without
  structural coupling support is reported, never silently downgraded to
  rigid.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .cad_video_geometry import EvaluationStatus
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


STRUCTURAL_BOUNDARY_AUTHORITY_VERSION = 'structural-boundary-1'

#: The physics class a boundary may take. These are never interchangeable:
#: a compliant boundary downgraded to 'rigid' loses its resonances, and an
#: impedance boundary pretending to be compliant invents them.
BoundaryCapabilityClass = Literal[
    'rigid',
    'locally_reacting_impedance',
    'structurally_compliant',
]

#: Structural idealization kinds for a compliant boundary.
StructuralBoundaryKind = Literal[
    'thin_plate',
    'shell',
    'lumped_modal_panel',
    'measured_mobility',
    'measured_effective_boundary',
    'external_FE',
    'opaque',
]

#: Edge support of a structural panel.
BoundaryEdgeCondition = Literal[
    'clamped', 'simply_supported', 'free', 'elastic', 'mixed', 'unknown'
]

#: What is behind the panel — sealed air cavity, open back, etc.
BoundaryCavityBacking = Literal[
    'sealed_air_cavity',
    'open_backed',
    'double_leaf',
    'free_field',
    'unknown',
]

#: Which faces of the boundary exchange energy with the room(s).
BoundaryCouplingSides = Literal['front', 'back', 'both', 'unknown']

#: Provider-declared coupling capabilities. A provider reports exactly
#: what it can compute; a structural request against a provider without
#: structural coupling is reported, never downgraded.
ProviderBoundaryCapability = Literal[
    'rigid_boundary',
    'local_impedance_boundary',
    'causal_local_boundary',
    'structural_plate_coupling',
    'structural_shell_coupling',
    'external_FSI',
]

#: Structural kinds a provider must support for a given coupling.
_KIND_CAPABILITIES: dict[str, ProviderBoundaryCapability] = {
    'thin_plate': 'structural_plate_coupling',
    'shell': 'structural_shell_coupling',
    'lumped_modal_panel': 'structural_plate_coupling',
    'measured_mobility': 'structural_plate_coupling',
    'measured_effective_boundary': 'local_impedance_boundary',
    'external_FE': 'external_FSI',
    'opaque': 'rigid_boundary',
}






def _finite(value: object, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class StructuralBoundaryModel(BaseModel):
    """One compliant boundary's structural record.

    Only ``structurally_compliant`` boundaries carry a structural model;
    ``rigid`` and ``locally_reacting_impedance`` boundaries must not
    instantiate this record (a rigid wall with a stiffness is a
    contradiction). Constants that were never measured stay ``None`` —
    construction metadata does not imply them.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'structural-boundary-1'
    ] = STRUCTURAL_BOUNDARY_AUTHORITY_VERSION
    boundary_model_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    #: The surface/wall/boundary this model describes.
    boundary_ref: str = Field(min_length=1)
    capability_class: Literal['structurally_compliant'] = (
        'structurally_compliant'
    )
    kind: StructuralBoundaryKind = 'opaque'
    areal_mass_kg_m2: float | None = Field(default=None, gt=0.0)
    bending_stiffness_nm: float | None = Field(default=None, ge=0.0)
    damping_loss_factor: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    edge_condition: BoundaryEdgeCondition = 'unknown'
    cavity_backing: BoundaryCavityBacking = 'unknown'
    cavity_depth_m: float | None = Field(default=None, gt=0.0)
    coupling_sides: BoundaryCouplingSides = 'unknown'
    valid_frequency_domain: FrequencyDomain | None = None
    #: Binding to the construction assembly this boundary is built from —
    #: recorded lineage, not a claim that the assembly implies the
    #: constants.
    construction_assembly_ref: str | None = Field(
        default=None, min_length=1
    )
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_model(self) -> 'StructuralBoundaryModel':
        for name in (
            'areal_mass_kg_m2',
            'bending_stiffness_nm',
            'damping_loss_factor',
            'cavity_depth_m',
        ):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        if self.kind in ('thin_plate', 'shell', 'lumped_modal_panel') and (
            self.areal_mass_kg_m2 is None
            and self.bending_stiffness_nm is None
        ):
            raise ValueError(
                f'{self.kind} requires at least one structural constant '
                '(areal mass or bending stiffness)'
            )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'StructuralBoundaryModel semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'boundary_model_id': self.boundary_model_id,
            'version': self.version,
            'boundary_ref': self.boundary_ref,
            'capability_class': self.capability_class,
            'kind': self.kind,
            'areal_mass_kg_m2': self.areal_mass_kg_m2,
            'bending_stiffness_nm': self.bending_stiffness_nm,
            'damping_loss_factor': self.damping_loss_factor,
            'edge_condition': self.edge_condition,
            'cavity_backing': self.cavity_backing,
            'cavity_depth_m': self.cavity_depth_m,
            'coupling_sides': self.coupling_sides,
            'valid_frequency_domain': (
                self.valid_frequency_domain.model_dump(mode='json')
                if self.valid_frequency_domain is not None
                else None
            ),
            'construction_assembly_ref': self.construction_assembly_ref,
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }

    @property
    def required_provider_capability(self) -> ProviderBoundaryCapability:
        return _KIND_CAPABILITIES[self.kind]


class DerivedEffectiveImpedance(BaseModel):
    """An impedance record derived *from* a structural model — the
    derivation keeps its structural lineage (which boundary model, which
    provider, which derivation parameters) so it can never masquerade as
    a directly measured surface impedance."""

    model_config = ConfigDict(frozen=True)

    derivation_id: str = Field(min_length=1)
    structural_model_id: str = Field(min_length=1)
    structural_model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_id: str = Field(min_length=1)
    parameters: dict[str, Any] = {}
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_derivation(self) -> 'DerivedEffectiveImpedance':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DerivedEffectiveImpedance hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': STRUCTURAL_BOUNDARY_AUTHORITY_VERSION,
            'derivation_id': self.derivation_id,
            'structural_model_id': self.structural_model_id,
            'structural_model_sha256': self.structural_model_sha256,
            'provider_id': self.provider_id,
            'parameters': self.parameters,
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


def build_structural_boundary_model(
    *,
    boundary_model_id: str | None = None,
    version: str = '1',
    boundary_ref: str,
    kind: StructuralBoundaryKind = 'opaque',
    areal_mass_kg_m2: float | None = None,
    bending_stiffness_nm: float | None = None,
    damping_loss_factor: float | None = None,
    edge_condition: BoundaryEdgeCondition = 'unknown',
    cavity_backing: BoundaryCavityBacking = 'unknown',
    cavity_depth_m: float | None = None,
    coupling_sides: BoundaryCouplingSides = 'unknown',
    valid_frequency_domain: FrequencyDomain | None = None,
    construction_assembly_ref: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> StructuralBoundaryModel:
    payload: dict[str, Any] = {
        'authority_version': STRUCTURAL_BOUNDARY_AUTHORITY_VERSION,
        'boundary_model_id': boundary_model_id or str(uuid4()),
        'version': version,
        'boundary_ref': boundary_ref,
        'capability_class': 'structurally_compliant',
        'kind': kind,
        'areal_mass_kg_m2': areal_mass_kg_m2,
        'bending_stiffness_nm': bending_stiffness_nm,
        'damping_loss_factor': damping_loss_factor,
        'edge_condition': edge_condition,
        'cavity_backing': cavity_backing,
        'cavity_depth_m': cavity_depth_m,
        'coupling_sides': coupling_sides,
        'valid_frequency_domain': valid_frequency_domain,
        'construction_assembly_ref': construction_assembly_ref,
        'provenance': provenance,
    }
    provisional = StructuralBoundaryModel.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return StructuralBoundaryModel(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


def build_derived_effective_impedance(
    *,
    derivation_id: str | None = None,
    structural_model: StructuralBoundaryModel,
    provider_id: str,
    parameters: dict[str, Any] | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> DerivedEffectiveImpedance:
    payload: dict[str, Any] = {
        'derivation_id': derivation_id or str(uuid4()),
        'structural_model_id': structural_model.boundary_model_id,
        'structural_model_sha256': structural_model.semantic_sha256,
        'provider_id': provider_id,
        'parameters': parameters or {},
        'provenance': provenance,
    }
    provisional = DerivedEffectiveImpedance.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return DerivedEffectiveImpedance(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class BoundaryCapabilityCheck(BaseModel):
    """One check on whether a boundary's declared class is supportable."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


class BoundaryCapabilityReport(BaseModel):
    """Capability report for one boundary against a provider's declared
    capabilities — a compliant boundary without structural coupling
    support is reported, never silently downgraded."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    boundary_ref: str
    boundary_model_sha256: str | None = None
    checks: tuple[BoundaryCapabilityCheck, ...]
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_report(self) -> 'BoundaryCapabilityReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('boundary capability report hash mismatch')
        expected = 'bcr-' + self.report_sha256[:24]
        if self.report_id != expected:
            raise ValueError('boundary capability report id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': STRUCTURAL_BOUNDARY_AUTHORITY_VERSION,
            'boundary_ref': self.boundary_ref,
            'boundary_model_sha256': self.boundary_model_sha256,
            'checks': [
                check.model_dump(mode='json') for check in self.checks
            ],
        }


def evaluate_boundary_capability(
    *,
    boundary_ref: str,
    capability_class: BoundaryCapabilityClass,
    structural_model: StructuralBoundaryModel | None = None,
    provider_capabilities: tuple[ProviderBoundaryCapability, ...] = (),
) -> BoundaryCapabilityReport:
    """Report whether a boundary's declared class is supportable.

    - ``capability_class_consistent``: a structural model only exists for
      ``structurally_compliant``; a compliant boundary requires one;
    - ``structural_constants_declared``: compliant models must carry
      their structural constants or an explicit ``opaque`` kind;
    - ``provider_support``: the declared class maps to provider
      capabilities — structural plates need
      ``structural_plate_coupling``, shells ``structural_shell_coupling``,
      external FE ``external_FSI``. Missing support is FAIL (reported —
      the boundary keeps its class; nothing is silently downgraded to
      rigid).
    """

    checks: list[BoundaryCapabilityCheck] = []

    if capability_class == 'structurally_compliant':
        if structural_model is None:
            checks.append(
                BoundaryCapabilityCheck(
                    check='capability_class_consistent',
                    status='UNKNOWN',
                    reason='structurally compliant boundary has no '
                    'structural model recorded',
                )
            )
        elif structural_model.boundary_ref != boundary_ref:
            checks.append(
                BoundaryCapabilityCheck(
                    check='capability_class_consistent',
                    status='FAIL',
                    reason=f'structural model binds '
                    f"'{structural_model.boundary_ref}', not "
                    f"'{boundary_ref}'",
                )
            )
        else:
            checks.append(
                BoundaryCapabilityCheck(
                    check='capability_class_consistent',
                    status='PASS',
                    reason='structural model bound to compliant boundary',
                )
            )
    else:
        checks.append(
            BoundaryCapabilityCheck(
                check='capability_class_consistent',
                status='PASS' if structural_model is None else 'FAIL',
                reason=(
                    f'class {capability_class} carries no structural '
                    'constants'
                    if structural_model is None
                    else f'{capability_class} boundary cannot carry a '
                    'structural model — separate classes are not '
                    'interchangeable'
                ),
            )
        )

    if structural_model is not None:
        structural = structural_model
        if structural.kind == 'opaque':
            constants_status: EvaluationStatus = 'UNKNOWN'
            constants_reason = 'opaque structural kind carries no constants'
        elif structural.areal_mass_kg_m2 is None and (
            structural.bending_stiffness_nm is None
        ):
            constants_status = 'UNKNOWN'
            constants_reason = 'no structural constants recorded'
        else:
            constants_status = 'PASS'
            constants_reason = 'structural constants recorded'
        checks.append(
            BoundaryCapabilityCheck(
                check='structural_constants_declared',
                status=constants_status,
                reason=constants_reason,
            )
        )

    if capability_class == 'structurally_compliant' and (
        structural_model is not None
    ):
        needed = structural_model.required_provider_capability
        if not provider_capabilities:
            provider_status: EvaluationStatus = 'UNKNOWN'
            provider_reason = 'no provider capabilities declared'
        elif needed in provider_capabilities:
            provider_status = 'PASS'
            provider_reason = f'provider supports {needed}'
        else:
            provider_status = 'FAIL'
            provider_reason = (
                f'provider lacks {needed} — boundary keeps its class; '
                'no downgrade to rigid performed'
            )
        checks.append(
            BoundaryCapabilityCheck(
                check='provider_support',
                status=provider_status,
                reason=provider_reason,
            )
        )
    elif capability_class == 'locally_reacting_impedance':
        if not provider_capabilities:
            provider_status = 'UNKNOWN'
            provider_reason = 'no provider capabilities declared'
        elif 'local_impedance_boundary' in provider_capabilities or (
            'causal_local_boundary' in provider_capabilities
        ):
            provider_status = 'PASS'
            provider_reason = 'provider supports local impedance boundary'
        else:
            provider_status = 'FAIL'
            provider_reason = (
                'provider lacks local_impedance_boundary — no downgrade '
                'performed'
            )
        checks.append(
            BoundaryCapabilityCheck(
                check='provider_support',
                status=provider_status,
                reason=provider_reason,
            )
        )
    else:
        checks.append(
            BoundaryCapabilityCheck(
                check='provider_support',
                status='NOT_APPLICABLE',
                reason='rigid boundaries need no coupling capability',
            )
        )

    probe = BoundaryCapabilityReport.model_construct(
        report_id='',
        boundary_ref=boundary_ref,
        boundary_model_sha256=(
            structural_model.semantic_sha256
            if structural_model is not None
            else None
        ),
        checks=tuple(checks),
        report_sha256='',
    )
    digest = _hash(probe.identity_payload())
    return BoundaryCapabilityReport(
        **probe.model_dump(
            mode='python',
            exclude={'report_sha256', 'report_id'},
        ),
        report_id='bcr-' + digest[:24],
        report_sha256=digest,
    )


__all__ = [
    'BoundaryCapabilityCheck',
    'BoundaryCapabilityClass',
    'BoundaryCapabilityReport',
    'BoundaryCavityBacking',
    'BoundaryCouplingSides',
    'BoundaryEdgeCondition',
    'DerivedEffectiveImpedance',
    'ProviderBoundaryCapability',
    'STRUCTURAL_BOUNDARY_AUTHORITY_VERSION',
    'StructuralBoundaryKind',
    'StructuralBoundaryModel',
    'build_derived_effective_impedance',
    'build_structural_boundary_model',
    'evaluate_boundary_capability',
]
