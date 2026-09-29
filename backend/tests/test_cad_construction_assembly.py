"""Issue #657 regression tests: unified construction assembly authority."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import ValidationError

from htdt.cad_attachment_models import (
    AssemblyLayer,
    ConstructionAssembly,
    EntityAttachment,
)
from htdt.cad_construction_assembly import (
    ConstructionAssemblyError,
    assembly_for_element,
    bound_construction_elements,
    default_assumed_assembly,
    element_evidence,
    finish_absorption_coefficients,
    validate_construction_assemblies,
)
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_walls import make_wall_topology


_ROOM = RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0)
_WALL_ID = make_wall_topology(_ROOM).walls[0].wall_id


def _document(**overrides) -> SceneDocument:
    payload = {
        'document_id': 'asm-fixture',
        'schema_version': 5,
        'room': _ROOM,
        'entities': (),
    }
    payload.update(overrides)
    return SceneDocument(**payload)


def _drywall_assembly() -> ConstructionAssembly:
    return ConstructionAssembly(
        assembly_id='asm-front',
        element='wall',
        element_ref=_WALL_ID,
        layers=(
            AssemblyLayer(layer_id='finish', kind='finish',
                          material='drywall', thickness_m=0.0125,
                          absorption_coefficients=(0.3, 0.2, 0.15),
                          evidence_source='capture'),
            AssemblyLayer(layer_id='sub', kind='substrate',
                          material='drywall', thickness_m=0.0125,
                          evidence_source='capture'),
            AssemblyLayer(layer_id='cav', kind='cavity',
                          material='unknown', thickness_m=0.09,
                          evidence_source='operator'),
            AssemblyLayer(layer_id='frame', kind='framing',
                          material='wood_stud', thickness_m=0.09,
                          evidence_source='operator'),
        ),
        evidence_source='operator',
    )


def test_assembly_model_validation() -> None:
    with pytest.raises(ValidationError, match='unique'):
        ConstructionAssembly(
            assembly_id='a', element='wall', element_ref='w1',
            layers=(
                AssemblyLayer(layer_id='x', kind='finish', thickness_m=0.01),
                AssemblyLayer(layer_id='x', kind='substrate',
                              material='drywall', thickness_m=0.01),
            ),
        )
    with pytest.raises(ValidationError, match='finish and one substrate'):
        ConstructionAssembly(
            assembly_id='a', element='wall', element_ref='w1',
            layers=(
                AssemblyLayer(layer_id='f1', kind='finish', thickness_m=0.01),
                AssemblyLayer(layer_id='f2', kind='finish', thickness_m=0.01),
            ),
        )
    with pytest.raises(ValidationError, match='within \\[0, 1\\]'):
        AssemblyLayer(layer_id='f', kind='finish', thickness_m=0.01,
                      absorption_coefficients=(1.5,))


def test_element_evidence_declared_vs_assumed() -> None:
    document = _document(
        wall_topology=make_wall_topology(_ROOM),
        construction_assemblies=(_drywall_assembly(),),
    )
    evidence = element_evidence(document, 'wall', _WALL_ID)
    assert evidence.substrate_material == 'drywall'
    assert evidence.cavity_depth_m == pytest.approx(0.09)
    assert evidence.framing_material == 'wood_stud'
    assert evidence.finish_absorption == (0.3, 0.2, 0.15)
    assert evidence.evidence_source == 'operator'
    assert assembly_for_element(document, 'wall', _WALL_ID).assembly_id == 'asm-front'
    # Undeclared element → honest unknowns.
    missing = element_evidence(document, 'wall', 'w9')
    assert missing.substrate_material == 'unknown'
    assert missing.cavity_depth_m is None
    assert missing.evidence_source == 'assumed'
    assert bound_construction_elements(document) == frozenset({('wall', _WALL_ID)})
    assert finish_absorption_coefficients(document, 'wall', _WALL_ID) == (0.3, 0.2, 0.15)


def _with_assemblies(
    document: SceneDocument, assemblies: tuple[ConstructionAssembly, ...]
) -> SceneDocument:
    """model_copy bypasses validators — malformed bindings are constructible
    only through the bypass, mirroring how production state must never persist."""

    return document.model_copy(update={'construction_assemblies': assemblies})


def test_validate_fails_closed_on_dangling_bindings() -> None:
    document = _with_assemblies(
        _document(),
        (
            ConstructionAssembly(
                assembly_id='a', element='wall', element_ref='ghost-wall',
                layers=(AssemblyLayer(layer_id='f', kind='finish',
                                      thickness_m=0.01),),
            ),
        ),
    )
    with pytest.raises(ConstructionAssemblyError, match='unknown wall'):
        validate_construction_assemblies(document)
    duplicate = _with_assemblies(
        _document(wall_topology=make_wall_topology(_ROOM)),
        (
            _drywall_assembly(),
            _drywall_assembly().model_copy(update={'assembly_id': 'asm-dup'}),
        ),
    )
    with pytest.raises(ConstructionAssemblyError, match='duplicate'):
        validate_construction_assemblies(duplicate)
    wrong_ref = _with_assemblies(
        _document(),
        (
            ConstructionAssembly(
                assembly_id='a', element='floor', element_ref='not-floor',
                layers=(AssemblyLayer(layer_id='f', kind='finish',
                                      thickness_m=0.01),),
            ),
        ),
    )
    with pytest.raises(ConstructionAssemblyError, match='floor'):
        validate_construction_assemblies(wrong_ref)


def test_document_rejects_dangling_assembly_bindings() -> None:
    """Schema-level check: dangling element_refs fail closed at validation."""

    with pytest.raises(ValidationError, match='unknown wall'):
        _document(
            construction_assemblies=(
                ConstructionAssembly(
                    assembly_id='a', element='wall', element_ref='ghost-wall',
                    layers=(AssemblyLayer(layer_id='f', kind='finish',
                                          thickness_m=0.01),),
                ),
            ),
        )


def test_schema_version_gate() -> None:
    with pytest.raises(ValidationError, match='schema_version >= 5'):
        _document(
            schema_version=4,
            construction_assemblies=(
                ConstructionAssembly(
                    assembly_id='a', element='floor', element_ref='floor',
                    layers=(AssemblyLayer(layer_id='f', kind='finish',
                                          thickness_m=0.01),),
                ),
            ),
        )


def test_assumed_assembly_marks_evidence() -> None:
    assembly = default_assumed_assembly('ceiling', 'ceiling')
    assert assembly.evidence_source == 'assumed'
    assert assembly.layer_of_kind('finish') is not None
    assert assembly.cavity_depth_m is None
