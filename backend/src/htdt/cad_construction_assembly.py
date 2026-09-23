"""Construction Assembly Authority (Issue #657).

Wall, floor, and ceiling physical layers were previously described in three
partial, disagreeing places: what Capture observed, what acoustics assumed,
and what installation reasoning guessed. ``SceneDocument.
construction_assemblies`` unifies them into one ordered layer stack per
element — finish / substrate / cavity / framing / insulation — and this
module is the lookup authority every consumer reads instead of maintaining
its own shadow description.

- ``assembly_for_element`` binds an element ('wall' + wall_id, 'floor',
  'ceiling', 'soffit', 'riser') to its declared assembly, if any.
- ``element_evidence`` returns the unified evidence view: substrate material,
  cavity depth, framing presence, finish absorption — each with provenance.
- ``validate_construction_assemblies`` fails closed on dangling element
  references and duplicate bindings.
- Elements without a declared assembly return ``assumed`` evidence — the
  honest "we don't know" state downstream authorities must surface as
  UNKNOWN, never as a fabricated material.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .cad_attachment_models import (
    AssemblyElementKind,
    AssemblyLayer,
    ConstructionAssembly,
    SubstrateMaterial,
)
from .cad_scene import SceneDocument


CONSTRUCTION_ASSEMBLY_VERSION = '1'


class ConstructionAssemblyError(ValueError):
    """Assembly bindings in a document are malformed."""


class ElementEvidence(BaseModel):
    """Unified physical evidence view for one room element."""

    model_config = ConfigDict(frozen=True)
    element: AssemblyElementKind
    element_ref: str
    assembly_id: str | None  # None when the element has no declared assembly
    substrate_material: SubstrateMaterial  # 'unknown' when not established
    cavity_depth_m: float | None
    framing_material: SubstrateMaterial | None
    finish_absorption: tuple[float, ...] | None
    total_thickness_m: float | None
    evidence_source: str  # capture/operator/specification/assumed


def assembly_for_element(
    document: SceneDocument,
    element: AssemblyElementKind,
    element_ref: str,
) -> ConstructionAssembly | None:
    """The declared assembly bound to an element, or None."""

    for assembly in document.construction_assemblies or ():
        if assembly.element == element and assembly.element_ref == element_ref:
            return assembly
    return None


def validate_construction_assemblies(document: SceneDocument) -> None:
    """Fail closed on duplicate/dangling assembly bindings in a document."""

    assemblies = document.construction_assemblies or ()
    seen: set[tuple[str, str]] = set()
    known_walls = {
        wall.wall_id
        for wall in (document.wall_topology.walls if document.wall_topology else ())
    }
    for assembly in assemblies:
        key = (assembly.element, assembly.element_ref)
        if key in seen:
            raise ConstructionAssemblyError(
                f'duplicate assembly for {assembly.element}:{assembly.element_ref}'
            )
        seen.add(key)
        if assembly.element == 'wall' and assembly.element_ref not in known_walls:
            raise ConstructionAssemblyError(
                f'wall assembly binds unknown wall: {assembly.element_ref}'
            )
        if assembly.element == 'floor' and assembly.element_ref != 'floor':
            raise ConstructionAssemblyError('floor assemblies bind element_ref="floor"')
        if assembly.element == 'ceiling' and assembly.element_ref != 'ceiling':
            raise ConstructionAssemblyError('ceiling assemblies bind element_ref="ceiling"')


def element_evidence(
    document: SceneDocument,
    element: AssemblyElementKind,
    element_ref: str,
) -> ElementEvidence:
    """Unified evidence for one element; ``assumed`` when nothing is declared."""

    assembly = assembly_for_element(document, element, element_ref)
    if assembly is None:
        return ElementEvidence(
            element=element,
            element_ref=element_ref,
            assembly_id=None,
            substrate_material='unknown',
            cavity_depth_m=None,
            framing_material=None,
            finish_absorption=None,
            total_thickness_m=None,
            evidence_source='assumed',
        )
    substrate = assembly.layer_of_kind('substrate')
    framing = assembly.layer_of_kind('framing')
    finish = assembly.layer_of_kind('finish')
    return ElementEvidence(
        element=element,
        element_ref=element_ref,
        assembly_id=assembly.assembly_id,
        substrate_material=(substrate.material if substrate else 'unknown'),
        cavity_depth_m=assembly.cavity_depth_m,
        framing_material=(framing.material if framing else None),
        finish_absorption=(finish.absorption_coefficients if finish else None),
        total_thickness_m=assembly.total_thickness_m,
        evidence_source=assembly.evidence_source,
    )


def bound_construction_elements(document: SceneDocument) -> frozenset[tuple[str, str]]:
    """All (element, element_ref) pairs carrying a declared assembly."""

    return frozenset(
        (assembly.element, assembly.element_ref)
        for assembly in document.construction_assemblies or ()
    )


def finish_absorption_coefficients(
    document: SceneDocument,
    element: AssemblyElementKind,
    element_ref: str,
) -> tuple[float, ...] | None:
    """Room-facing acoustic absorption of an element's finish layer."""

    return element_evidence(document, element, element_ref).finish_absorption


def default_assumed_assembly(
    element: AssemblyElementKind,
    element_ref: str,
) -> ConstructionAssembly:
    """A conservative all-``assumed`` placeholder assembly.

    Exists so surfaces that genuinely have no evidence produce an explicit
    ``assumed`` assembly rather than nothing — downstream consumers must
    report UNKNOWN for any decision that depends on it.
    """

    return ConstructionAssembly(
        assembly_id=f'assumed:{element}:{element_ref}',
        element=element,
        element_ref=element_ref,
        layers=(
            AssemblyLayer(
                layer_id='finish', kind='finish', thickness_m=0.0125,
                material='unknown', evidence_source='assumed',
            ),
        ),
        evidence_source='assumed',
    )
