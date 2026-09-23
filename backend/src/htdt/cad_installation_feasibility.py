"""Installation feasibility authority (Issue #652).

Whether a mount actually works depends on the wall behind it: substrate
material, framing/cavity depth, cutout size, service clearance. This module
evaluates an ``InstallationRequest`` against unified
``ElementEvidence`` (Issue #657's construction assemblies) and reports a
per-check verdict — SUPPORTED, CONFLICT, UNKNOWN, NOT_APPLICABLE — rather
than silently assuming drywall.

The overall verdict is conservative: any CONFLICT → conflict; otherwise any
UNKNOWN → unknown; every check NOT_APPLICABLE → not_applicable; else
supported. Feasibility is advisory authority — it reports what the evidence
supports and never fabricates a substrate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_attachment_models import (
    AssemblyElementKind,
    ConstructionAssembly,
    SubstrateMaterial,
)
from .cad_construction_assembly import ElementEvidence, element_evidence
from .cad_scene import SceneDocument


INSTALLATION_FEASIBILITY_VERSION = '1'

FeasibilityVerdict = Literal['supported', 'conflict', 'unknown', 'not_applicable']

MountSurfaceKind = Literal[
    'wall', 'ceiling', 'floor', 'rack_rail', 'furniture_top', 'pole'
]

# Conservative static payload limits per substrate (kg). 'unknown' has no
# supported payload — any load check against it is UNKNOWN, never supported.
_SUBSTRATE_PAYLOAD_KG: dict[SubstrateMaterial, float] = {
    'drywall': 15.0,
    'plywood': 45.0,
    'osb': 45.0,
    'masonry': 200.0,
    'concrete': 200.0,
    'metal_stud': 25.0,
    'wood_stud': 80.0,
    'glass': 0.0,
    'acoustic_panel': 2.0,
    'unknown': 0.0,
}

# Mounts that require solid framing members rather than board substrate.
_FRAMING_SUBSTRATES: frozenset[str] = frozenset({'wood_stud', 'metal_stud'})


class InstallationRequest(BaseModel):
    """What an entity needs from a mounting element to install safely."""

    model_config = ConfigDict(frozen=True)
    mount_surface: MountSurfaceKind
    entity_id: str = Field(min_length=1)
    # Wall/ceiling/floor/… element and its reference (wall_id etc.).
    element: AssemblyElementKind | None = None
    element_ref: str | None = None
    payload_kg: float = Field(ge=0.0)
    requires_solid_substrate: bool = False   # needs structural substrate, not just finish
    requires_framing: bool = False           # must land on framing members
    cutout_required: bool = False            # recessed/in-wall install
    cutout_depth_m: float | None = Field(default=None, gt=0)
    service_clearance_m: float = Field(default=0.0, ge=0.0)  # behind-mount access
    label: str | None = None

    @model_validator(mode='after')
    def valid_request(self) -> 'InstallationRequest':
        if self.cutout_required and self.cutout_depth_m is None:
            raise ValueError('cutout_required installs must declare cutout_depth_m')
        if self.element is not None and self.element_ref is None:
            raise ValueError('element installs must declare element_ref')
        return self


class FeasibilityCheck(BaseModel):
    """One evaluated constraint."""

    model_config = ConfigDict(frozen=True)
    check: Literal[
        'mount_surface', 'substrate', 'payload', 'framing', 'cutout',
        'service_clearance',
    ]
    verdict: FeasibilityVerdict
    detail: str


class InstallationFeasibilityReport(BaseModel):
    """Per-check verdicts plus the conservative overall verdict."""

    model_config = ConfigDict(frozen=True)
    entity_id: str
    mount_surface: MountSurfaceKind
    checks: tuple[FeasibilityCheck, ...]
    overall: FeasibilityVerdict


def _check_substrate(
    request: InstallationRequest, evidence: ElementEvidence
) -> FeasibilityCheck:
    material = evidence.substrate_material
    if material == 'unknown':
        return FeasibilityCheck(
            check='substrate', verdict='unknown',
            detail='substrate material has no declared evidence',
        )
    if request.requires_solid_substrate and material in ('drywall', 'acoustic_panel'):
        return FeasibilityCheck(
            check='substrate', verdict='conflict',
            detail=f'{material} is not a structural substrate for this mount',
        )
    return FeasibilityCheck(
        check='substrate', verdict='supported',
        detail=f'{material} substrate (evidence: {evidence.evidence_source})',
    )


def _check_payload(
    request: InstallationRequest, evidence: ElementEvidence
) -> FeasibilityCheck:
    material = evidence.substrate_material
    if material == 'unknown':
        return FeasibilityCheck(
            check='payload', verdict='unknown',
            detail='payload capacity unknown without substrate evidence',
        )
    limit = _SUBSTRATE_PAYLOAD_KG[material]
    if limit <= 0.0:
        return FeasibilityCheck(
            check='payload', verdict='conflict',
            detail=f'{material} cannot carry mounted payloads',
        )
    if float(request.payload_kg) > limit:
        return FeasibilityCheck(
            check='payload', verdict='conflict',
            detail=f'{request.payload_kg:.1f}kg exceeds {material} limit {limit:.0f}kg',
        )
    return FeasibilityCheck(
        check='payload', verdict='supported',
        detail=f'{request.payload_kg:.1f}kg within {material} limit {limit:.0f}kg',
    )


def _check_framing(
    request: InstallationRequest, evidence: ElementEvidence
) -> FeasibilityCheck:
    if not request.requires_framing:
        return FeasibilityCheck(
            check='framing', verdict='not_applicable',
            detail='mount does not require framing members',
        )
    if evidence.framing_material is None:
        return FeasibilityCheck(
            check='framing', verdict='unknown',
            detail='no framing evidence behind the substrate',
        )
    if evidence.framing_material in _FRAMING_SUBSTRATES:
        return FeasibilityCheck(
            check='framing', verdict='supported',
            detail=f'{evidence.framing_material} framing present',
        )
    return FeasibilityCheck(
        check='framing', verdict='conflict',
        detail=f'{evidence.framing_material} cannot receive framing anchors',
    )


def _check_cutout(
    request: InstallationRequest, evidence: ElementEvidence
) -> FeasibilityCheck:
    if not request.cutout_required:
        return FeasibilityCheck(
            check='cutout', verdict='not_applicable',
            detail='surface mount — no cutout required',
        )
    if evidence.cavity_depth_m is None:
        return FeasibilityCheck(
            check='cutout', verdict='unknown',
            detail='cavity depth unknown — cannot verify recessed install',
        )
    assert request.cutout_depth_m is not None
    if float(request.cutout_depth_m) > float(evidence.cavity_depth_m):
        return FeasibilityCheck(
            check='cutout', verdict='conflict',
            detail=(
                f'cutout depth {request.cutout_depth_m:.3f}m exceeds cavity '
                f'{float(evidence.cavity_depth_m):.3f}m'
            ),
        )
    return FeasibilityCheck(
        check='cutout', verdict='supported',
        detail='cavity depth accommodates the cutout',
    )


def _check_service_clearance(
    request: InstallationRequest, evidence: ElementEvidence
) -> FeasibilityCheck:
    if float(request.service_clearance_m) <= 0.0:
        return FeasibilityCheck(
            check='service_clearance', verdict='not_applicable',
            detail='no behind-mount service clearance required',
        )
    if evidence.cavity_depth_m is None:
        return FeasibilityCheck(
            check='service_clearance', verdict='unknown',
            detail='service clearance cannot be verified without cavity evidence',
        )
    if float(request.service_clearance_m) > float(evidence.cavity_depth_m):
        return FeasibilityCheck(
            check='service_clearance', verdict='conflict',
            detail='required service clearance exceeds cavity depth',
        )
    return FeasibilityCheck(
        check='service_clearance', verdict='supported',
        detail='cavity provides the required service clearance',
    )


def installation_feasibility(
    document: SceneDocument, request: InstallationRequest
) -> InstallationFeasibilityReport:
    """Evaluate one installation request against construction evidence."""

    # Non-building surfaces carry their own authority — no element evidence.
    if request.mount_surface in ('rack_rail', 'furniture_top', 'pole'):
        checks = (
            FeasibilityCheck(
                check='mount_surface', verdict='not_applicable',
                detail=f'{request.mount_surface} is not a construction element',
            ),
        )
        return InstallationFeasibilityReport(
            entity_id=request.entity_id,
            mount_surface=request.mount_surface,
            checks=checks,
            overall='not_applicable',
        )

    element = request.element
    if element is None:
        element = {'wall': 'wall', 'ceiling': 'ceiling', 'floor': 'floor'}[
            request.mount_surface
        ]
    element_ref = request.element_ref or element
    evidence = element_evidence(document, element, element_ref)
    checks = (
        _check_substrate(request, evidence),
        _check_payload(request, evidence),
        _check_framing(request, evidence),
        _check_cutout(request, evidence),
        _check_service_clearance(request, evidence),
    )
    verdicts = {check.verdict for check in checks}
    if 'conflict' in verdicts:
        overall = 'conflict'
    elif 'unknown' in verdicts:
        overall = 'unknown'
    elif verdicts == {'not_applicable'}:
        overall = 'not_applicable'
    else:
        overall = 'supported'
    return InstallationFeasibilityReport(
        entity_id=request.entity_id,
        mount_surface=request.mount_surface,
        checks=checks,
        overall=overall,
    )
