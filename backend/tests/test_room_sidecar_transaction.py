"""#915 — the Room design transaction covers scene AND sidecars.

Sidecar stores (material assignments, environment/listener-pose/
screen-transfer selections, the video workspace, proposed treatment
placements) persist at edit time but join the same design transaction:
they mark the workspace dirty, Discard restores them, Keep Draft
acknowledges them, and Save commits them. Library records and installed
placement facts are never part of the transaction.
"""

from __future__ import annotations

import pytest

from htdt.cad_acoustic_environment import build_acoustic_environment_profile
from htdt.cad_acoustic_material import (
    SpecificImpedancePoint,
    build_acoustic_material,
)
from htdt.cad_acoustic_treatment import (
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceSubject,
    TreatmentLayer,
    TreatmentPhysicalParameters,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
    revise_treatment_placement,
)
from htdt.cad_listener_pose import build_listener_pose
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_screen_transfer import TransferSample, build_screen_transfer
from htdt.cad_video_workspace import (
    VideoGeometryWorkspace,
    default_seat_binding,
    default_screen_binding,
)
from htdt.room_workspace import RoomWorkspaceController


NOW = '2026-01-01T00:00:00Z'
DOC = 'doc-1'


def _controller(tmp_path):
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    return repository, RoomWorkspaceController(repository, DOC)


def _material():
    return build_acoustic_material(
        label='melamine 50',
        provenance='datasheet',
        wave_model='specific_impedance_table',
        specific_impedance=(
            SpecificImpedancePoint(
                frequency_hz=500.0,
                resistance_pa_s_m=800.0,
                reactance_pa_s_m=-200.0,
            ),
        ),
    )


def _environment(label: str = 'room standard'):
    return build_acoustic_environment_profile(
        label=label,
        sound_speed_source_kind='nominal_assumption',
        sound_speed_m_s=343.0,
        provenance='fixture',
        created_at_utc=NOW,
    )


def _treatment_definition(controller):
    dimensions = TreatmentDimensions(width_m=0.6, height_m=1.2, thickness_m=0.1)
    layers = (
        TreatmentLayer(
            layer_id='core',
            material_name='porous core',
            thickness_m=0.1,
            density_kg_m3=48.0,
        ),
    )
    evidence = build_treatment_evidence_authority(
        source_kind='user_defined',
        source_id='sidecar-test',
        source_version='1',
        extraction_id='manual-declaration',
        extraction_version='1',
        subject=TreatmentEvidenceSubject(
            definition_id='sidecar-panel',
            definition_version='1',
            treatment_type='porous_absorber',
            dimensions=dimensions,
            air_gap_m=0.0,
            layers=layers,
            parameters=TreatmentPhysicalParameters(),
        ),
    )
    definition = build_acoustic_treatment_definition(
        definition_id='sidecar-panel',
        version='1',
        name='Sidecar panel',
        treatment_type='porous_absorber',
        provenance=evidence.as_provenance(),
        dimensions=dimensions,
        layers=layers,
    )
    controller.treatment_repository.save_evidence(evidence)
    controller.treatment_repository.save_definition(definition)
    return definition


def test_material_assignment_marks_dirty_and_discard_restores(tmp_path):
    _, controller = _controller(tmp_path)
    assert not controller.is_dirty

    material = _material()
    controller.material_repository.save_material(material)
    controller.material_repository.assign_material(DOC, 'surface-1', material)
    assert controller.is_dirty
    assert controller.dirty_state() == 'dirty_recoverable'

    controller.discard_unsaved_changes()
    assert not controller.is_dirty
    assert controller.material_repository.assignments_for_document(DOC) == {}


def test_environment_selection_joins_transaction(tmp_path):
    _, controller = _controller(tmp_path)
    profile = _environment()
    controller.environment_repository.save_profile(profile)
    assert not controller.is_dirty

    controller.environment_repository.select_profile(DOC, profile)
    assert controller.is_dirty

    controller.discard_unsaved_changes()
    assert not controller.is_dirty
    assert controller.environment_repository.selected_profile(DOC) is None


def test_environment_selection_survives_save(tmp_path):
    _, controller = _controller(tmp_path)
    profile = _environment()
    controller.environment_repository.save_profile(profile)
    controller.environment_repository.select_profile(DOC, profile)

    assert controller.save() is True
    assert not controller.is_dirty
    assert (
        controller.environment_repository.selected_profile(DOC).authority_id
        == profile.authority_id
    )


def test_video_workspace_save_marks_dirty_and_discard_restores(tmp_path):
    _, controller = _controller(tmp_path)
    baseline_workspace = controller.video_workspace_repository.load(DOC)

    edited = baseline_workspace.model_copy(
        update={
            'screen_bindings': {
                'screen-x': default_screen_binding(
                    'screen-x', width_m=2.2, height_m=1.2
                ),
            },
            'seat_bindings': {
                'seat-x': default_seat_binding('seat-x'),
            },
        }
    )
    controller.save_video_workspace(edited)
    assert controller.is_dirty

    controller.discard_unsaved_changes()
    assert not controller.is_dirty
    assert (
        controller.video_workspace_repository.load(DOC).model_dump(mode='json')
        == baseline_workspace.model_dump(mode='json')
    )


def test_keep_draft_acknowledges_sidecar_edit_then_reblocks(tmp_path):
    _, controller = _controller(tmp_path)
    profile = _environment()
    controller.environment_repository.save_profile(profile)
    controller.environment_repository.select_profile(DOC, profile)

    controller.keep_draft()
    assert controller.is_dirty  # edits still exist — acknowledged, not reverted
    assert controller.dirty_state() == 'clean'
    allowed, message = controller.before_deactivate()
    assert allowed

    # A further sidecar edit breaks the release: the prompt must return.
    other = _environment(label='other')
    controller.environment_repository.save_profile(other)
    controller.environment_repository.select_profile(DOC, other)
    assert controller.dirty_state() == 'dirty_recoverable'
    allowed, message = controller.before_deactivate()
    assert not allowed


def test_scene_only_save_reports_no_change_but_sidecar_save_does(tmp_path):
    _, controller = _controller(tmp_path)
    # Nothing dirty — a no-op save reports no change.
    assert controller.save() is False
    # Library creation alone is not a design edit.
    profile = _environment()
    controller.environment_repository.save_profile(profile)
    assert not controller.is_dirty
    assert controller.save() is False


def test_proposed_placement_rolls_back_on_discard(tmp_path):
    _, controller = _controller(tmp_path)
    definition = _treatment_definition(controller)
    head = controller.repository.current_head(DOC)

    placement = build_treatment_placement(
        definition=definition,
        revision=head,
        instance_id='panel-a',
        position=Position3(x_m=0.1, y_m=1.0, z_m=1.2),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
    )
    controller.treatment_repository.save_placement(placement)
    assert controller.is_dirty
    assert controller.treatment_repository.proposed_placement_ids(DOC) == (
        'panel-a',
    )

    controller.discard_unsaved_changes()
    assert not controller.is_dirty
    assert controller.treatment_repository.proposed_placement_ids(DOC) == ()
    assert controller.treatment_repository.latest_placement('panel-a') is None


def test_installed_placement_survives_discard(tmp_path):
    _, controller = _controller(tmp_path)
    definition = _treatment_definition(controller)
    head = controller.repository.current_head(DOC)

    placement = build_treatment_placement(
        definition=definition,
        revision=head,
        instance_id='panel-installed',
        position=Position3(x_m=0.1, y_m=1.0, z_m=1.2),
        coverage=TreatmentCoverage(width_m=0.6, height_m=1.2),
    )
    controller.treatment_repository.save_placement(placement)
    installed = revise_treatment_placement(
        placement, revision=head, lifecycle='installed'
    )
    controller.treatment_repository.save_placement(installed)
    # An installed fact is physical/as-built — not a design edit.
    assert not controller.is_dirty
    assert controller.treatment_repository.proposed_placement_ids(DOC) == ()

    # And it is never deletable through the proposed-rollback path.
    with pytest.raises(ValueError, match='not proposed'):
        controller.treatment_repository.delete_placement('panel-installed')

    controller.discard_unsaved_changes()
    latest = controller.treatment_repository.latest_placement('panel-installed')
    assert latest is not None
    assert latest.lifecycle == 'installed'


def test_listener_pose_and_screen_transfer_selections_roll_back(tmp_path):
    _, controller = _controller(tmp_path)
    # Selection validation requires the bound entities in the scene head.
    controller.repository.save(
        SceneDocument(
            document_id=DOC,
            schema_version=2,
            room=None,
            entities=(
                SceneEntity(
                    entity_id='seat-1',
                    kind='seat',
                    name='MLP seat',
                    position=Position3(x_m=2.0, y_m=3.0, z_m=0.0),
                    size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
                    acoustic_reference_offset_m=Offset3(
                        x_m=0.0, y_m=0.0, z_m=1.1
                    ),
                ),
                SceneEntity(
                    entity_id='screen-1',
                    kind='screen',
                    name='Screen',
                    position=Position3(x_m=0.0, y_m=4.0, z_m=1.0),
                    size_m=Size3(x_m=2.2, y_m=0.05, z_m=1.2),
                ),
            ),
        ),
        parent_revision_id=controller.repository.current_head(DOC).revision_id,
    )
    pose = build_listener_pose(
        seat_entity_id='seat-1',
        label='upright',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.2),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.15),
        acoustic_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
        provenance='fixture',
        created_at_utc=NOW,
    )
    controller.listener_pose_repository.save_pose(pose)
    controller.listener_pose_repository.select_pose(DOC, pose)

    transfer = build_screen_transfer(
        screen_entity_id='screen-1',
        label='woven AT screen',
        capability_tier='MAGNITUDE_NORMAL_INCIDENCE',
        provenance='fixture',
        transfer_samples=(
            TransferSample(frequency_hz=1000.0, magnitude=0.9),
        ),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=200.0, maximum_hz=8000.0
        ),
        created_at_utc=NOW,
    )
    controller.screen_transfer_repository.save_transfer(transfer)
    controller.screen_transfer_repository.select_transfer(DOC, transfer)

    assert controller.is_dirty
    controller.discard_unsaved_changes()
    assert not controller.is_dirty
    assert controller.listener_pose_repository.selected_pose(DOC, 'seat-1') is None
    assert (
        controller.screen_transfer_repository.selected_transfer(DOC, 'screen-1')
        is None
    )
