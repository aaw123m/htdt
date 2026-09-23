"""Issue #652 regression tests: installation feasibility authority."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import ValidationError

from htdt.cad_attachment_models import AssemblyLayer, ConstructionAssembly
from htdt.cad_installation_feasibility import (
    InstallationRequest,
    installation_feasibility,
)
from htdt.cad_scene import RoomPrism, SceneDocument
from htdt.cad_walls import make_wall_topology


_ROOM = RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0)
_WALL_ID = make_wall_topology(_ROOM).walls[0].wall_id


def _document(assembly: ConstructionAssembly | None = None) -> SceneDocument:
    return SceneDocument(
        document_id='feas-fixture',
        schema_version=5,
        room=_ROOM,
        wall_topology=make_wall_topology(_ROOM),
        entities=(),
        construction_assemblies=(assembly,) if assembly else None,
    )


def _stud_wall() -> ConstructionAssembly:
    return ConstructionAssembly(
        assembly_id='asm-w1', element='wall', element_ref=_WALL_ID,
        layers=(
            AssemblyLayer(layer_id='fin', kind='finish', material='drywall',
                          thickness_m=0.0125, evidence_source='capture'),
            AssemblyLayer(layer_id='sub', kind='substrate', material='drywall',
                          thickness_m=0.0125, evidence_source='capture'),
            AssemblyLayer(layer_id='cav', kind='cavity', material='unknown',
                          thickness_m=0.10, evidence_source='operator'),
            AssemblyLayer(layer_id='frm', kind='framing', material='wood_stud',
                          thickness_m=0.089, evidence_source='operator'),
        ),
        evidence_source='operator',
    )


def _masonry_wall() -> ConstructionAssembly:
    return ConstructionAssembly(
        assembly_id='asm-w1', element='wall', element_ref=_WALL_ID,
        layers=(
            AssemblyLayer(layer_id='fin', kind='finish', material='drywall',
                          thickness_m=0.0125, evidence_source='capture'),
            AssemblyLayer(layer_id='sub', kind='substrate', material='masonry',
                          thickness_m=0.20, evidence_source='capture'),
        ),
        evidence_source='capture',
    )


def test_supported_mount_on_stud_wall() -> None:
    report = installation_feasibility(
        _document(_stud_wall()),
        InstallationRequest(
            mount_surface='wall', entity_id='display',
            element='wall', element_ref=_WALL_ID,
            payload_kg=12.0, requires_framing=True,
        ),
    )
    verdicts = {c.check: c.verdict for c in report.checks}
    assert verdicts['framing'] == 'supported'
    assert verdicts['payload'] == 'supported'
    assert verdicts['substrate'] == 'supported'
    assert report.overall == 'supported'


def test_unknown_when_no_evidence() -> None:
    report = installation_feasibility(
        _document(),  # no assemblies → assumed evidence
        InstallationRequest(
            mount_surface='wall', entity_id='display',
            element='wall', element_ref=_WALL_ID, payload_kg=12.0,
        ),
    )
    assert report.overall == 'unknown'
    verdicts = {c.check: c.verdict for c in report.checks}
    assert verdicts['substrate'] == 'unknown'
    assert verdicts['payload'] == 'unknown'


def test_conflict_overweight_drywall() -> None:
    report = installation_feasibility(
        _document(_stud_wall()),
        InstallationRequest(
            mount_surface='wall', entity_id='sub',
            element='wall', element_ref=_WALL_ID,
            payload_kg=40.0,  # exceeds drywall 15kg, substrate is drywall
        ),
    )
    verdicts = {c.check: c.verdict for c in report.checks}
    assert verdicts['payload'] == 'conflict'
    assert report.overall == 'conflict'


def test_recessed_install_cutout_depth() -> None:
    fits = installation_feasibility(
        _document(_stud_wall()),
        InstallationRequest(
            mount_surface='wall', entity_id='amp',
            element='wall', element_ref=_WALL_ID,
            payload_kg=8.0, cutout_required=True, cutout_depth_m=0.08,
        ),
    )
    assert {c.check: c.verdict for c in fits.checks}['cutout'] == 'supported'
    too_deep = installation_feasibility(
        _document(_stud_wall()),
        InstallationRequest(
            mount_surface='wall', entity_id='amp',
            element='wall', element_ref=_WALL_ID,
            payload_kg=8.0, cutout_required=True, cutout_depth_m=0.15,
        ),
    )
    assert {c.check: c.verdict for c in too_deep.checks}['cutout'] == 'conflict'
    assert too_deep.overall == 'conflict'
    # Masonry wall has no cavity → recessed install is UNKNOWN, not invented.
    no_cavity = installation_feasibility(
        _document(_masonry_wall()),
        InstallationRequest(
            mount_surface='wall', entity_id='amp',
            element='wall', element_ref=_WALL_ID,
            payload_kg=8.0, cutout_required=True, cutout_depth_m=0.05,
        ),
    )
    assert {c.check: c.verdict for c in no_cavity.checks}['cutout'] == 'unknown'


def test_non_building_surface_not_applicable() -> None:
    report = installation_feasibility(
        _document(),
        InstallationRequest(
            mount_surface='rack_rail', entity_id='receiver', payload_kg=9.0,
        ),
    )
    assert report.overall == 'not_applicable'


def test_request_validation() -> None:
    with pytest.raises(ValidationError, match='cutout_depth_m'):
        InstallationRequest(
            mount_surface='wall', entity_id='x', element='wall',
            element_ref='w1', payload_kg=1.0, cutout_required=True,
        )
    with pytest.raises(ValidationError, match='element_ref'):
        InstallationRequest(
            mount_surface='wall', entity_id='x', element='wall',
            payload_kg=1.0,
        )
