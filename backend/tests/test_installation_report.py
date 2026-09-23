import csv
import io
from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    SceneEntity,
    Size3,
    make_f1_scene,
    quaternion_from_euler_deg,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    ProposalEvidenceRef,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.report import (
    InstallationCalibrationChannelSummary,
    InstallationCalibrationSummary,
    InstallationTreatmentInstanceSummary,
    InstallationTreatmentQuantitySummary,
    InstallationTreatmentSummary,
    build_installation_output,
    render_installation_csv,
    render_installation_report_html,
)


def _installation_scene():
    base = make_f1_scene()
    additions = (
        SceneEntity(
            entity_id='seat-main',
            kind='seat',
            name='Main seat',
            position=Position3(x_m=3.0, y_m=3.1, z_m=0.45),
            size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
            acoustic_reference_offset_m=Offset3(z_m=0.65),
        ),
        SceneEntity(
            entity_id='screen-main',
            kind='screen',
            name='Main screen',
            position=Position3(x_m=3.0, y_m=0.12, z_m=1.35),
            size_m=Size3(x_m=2.8, y_m=0.08, z_m=1.58),
        ),
    )
    return base.model_copy(update={'entities': base.entities + additions})


def _variant(revision):
    changed = revision.document.entity('speaker-fl').model_copy(update={
        'orientation': quaternion_from_euler_deg(
            yaw_deg=12.0,
            pitch_deg=-4.0,
            roll_deg=0.0,
        ),
    })
    return build_system_variant(
        baseline=revision,
        name='installation candidate',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
            ChannelRoleBinding(role_id='C', display_name='Center'),
            ChannelRoleBinding(role_id='FR', display_name='Front Right'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='speaker-fl-pose',
                entity=changed,
                role_binding_id='FL',
            ),
        ),
        proposal_evidence=(
            ProposalEvidenceRef(
                evidence_kind='objective',
                evidence_id='objective-1',
                evidence_sha256='a' * 64,
            ),
        ),
        created_at_utc='2026-09-19T07:00:00+00:00',
    )


def test_installation_output_is_semantic_and_generation_metadata_free(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(_installation_scene(), parent_revision_id=None)
    variant = _variant(saved.revision)

    output = build_installation_output(saved.revision, variant=variant)
    speaker = next(item for item in output.entities if item.entity_id == 'speaker-fl')
    assert speaker.body_yaw_deg == pytest.approx(12.0)
    assert speaker.body_pitch_deg == pytest.approx(-4.0)
    assert speaker.mounting_height_m == speaker.z_m
    assert speaker.mounting_height_reference == 'scene_entity_origin_z'
    assert {item.entity_kind for item in output.entities} >= {
        'speaker',
        'seat',
        'screen',
        'measurement_point',
    }
    projector = next(item for item in output.sections if item.section == 'projector_coordinates')
    assert projector.status == 'UNKNOWN'
    assert next(item for item in output.sections if item.section == 'standards_profile').status == 'UNKNOWN'
    assert len(output.dimensions) == 3
    assert {item.view for item in output.dimensions} == {'top', 'front', 'side'}
    assert output.authority.system_variant_id == variant.variant_id
    assert output.authority.system_variant_sha256 == variant.variant_sha256
    assert output.evidence[0].evidence_id == 'objective-1'

    first = render_installation_report_html(
        output,
        exported_at_utc='2026-09-19T07:01:00+00:00',
    )
    second = render_installation_report_html(
        output,
        exported_at_utc='2026-09-19T08:02:00+00:00',
    )
    assert first != second
    assert output.semantic_sha256 in first
    assert output.semantic_sha256 in second
    assert 'generation metadata is not part of semantic identity' in first
    assert '2026-09-19T07:01:00+00:00' not in output.model_dump_json()

    csv_a = render_installation_csv(output)
    csv_b = render_installation_csv(output)
    assert csv_a == csv_b
    assert 'exported_at' not in csv_a
    assert output.semantic_sha256 in csv_a
    assert 'screen-main' in csv_a
    assert 'seat-main' in csv_a


def test_installation_output_regenerates_identically_after_repository_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / 'scene.sqlite3'
    scene_repository = SceneRepository(database)
    saved = scene_repository.save(_installation_scene(), parent_revision_id=None)
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = _variant(saved.revision)
    variant_repository.save_variant(variant)

    before = build_installation_output(saved.revision, variant=variant)

    reopened_scene_repository = SceneRepository(database)
    reopened_variant_repository = CadSystemVariantRepository(reopened_scene_repository)
    reopened_revision = reopened_scene_repository.get(saved.revision.revision_id)
    reopened_variant = reopened_variant_repository.get_variant(variant.variant_id)

    assert reopened_revision is not None
    assert reopened_variant is not None
    after = build_installation_output(reopened_revision, variant=reopened_variant)

    assert after == before
    assert after.semantic_sha256 == before.semantic_sha256
    assert render_installation_csv(after) == render_installation_csv(before)


def _assert_spreadsheet_inert(cell: str) -> None:
    """A parsed cell must never re-enter spreadsheet formula evaluation."""
    candidate = cell.lstrip()
    if not candidate:
        return
    assert candidate[0] not in ('=', '+', '@'), f'unneutralized formula cell: {cell!r}'
    if candidate[0] == '-':
        # Only an inert numeric literal may keep a leading '-'.
        float(candidate)


def test_installation_csv_neutralizes_formula_prefixed_entity_cells(tmp_path: Path) -> None:
    base = _installation_scene()
    entities = list(base.entities)
    entities[0] = entities[0].model_copy(update={
        'entity_id': '=HYPERLINK("https://example.invalid","speaker")',
        'name': '  =2+1',
        'speaker_role': '@SUM(1,1)',
    })
    entities[5] = entities[5].model_copy(update={
        'entity_id': '\t=cmd|"/c calc"!A0',
        'name': '-2+3+cmd|"/c calc"!A0',
        'position': Position3(x_m=-1.5, y_m=3.1, z_m=0.45),
    })
    entities[6] = entities[6].model_copy(update={'name': '+cmd|"/c calc"!A0'})
    scene = base.model_copy(update={'entities': tuple(entities)})

    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(scene, parent_revision_id=None)
    output = build_installation_output(saved.revision)

    csv_text = render_installation_csv(output)
    rows = [row for row in csv.reader(io.StringIO(csv_text)) if row]
    for row in rows:
        for cell in row:
            _assert_spreadsheet_inert(cell)

    entity_rows = {
        row[0]: row
        for row in rows[1:]
        if len(row) == 22
    }
    speaker = entity_rows["'=HYPERLINK(\"https://example.invalid\",\"speaker\")"]
    assert speaker[2] == "'  =2+1"
    assert speaker[3] == "'@SUM(1,1)"
    seat = entity_rows["'\t=cmd|\"/c calc\"!A0"]
    assert seat[2] == "'-2+3+cmd|\"/c calc\"!A0"
    assert seat[4] == '-1.5'  # legitimate negative numeric cell is not prefixed
    assert float(seat[4]) == -1.5
    screen = entity_rows['screen-main']
    assert screen[2] == "'+cmd|\"/c calc\"!A0"
    point = entity_rows['point-mlp']
    assert point[2] == 'MLP'
    assert output.semantic_sha256 in csv_text

    # Rendering safety never mutates the semantic model or its identity.
    speaker_model = next(
        item for item in output.entities
        if item.entity_id == '=HYPERLINK("https://example.invalid","speaker")'
    )
    assert speaker_model.name == '  =2+1'
    assert speaker_model.speaker_role == '@SUM(1,1)'

    html = render_installation_report_html(
        output,
        exported_at_utc='2026-09-19T07:01:00+00:00',
    )
    assert '=HYPERLINK(&quot;https://example.invalid&quot;,&quot;speaker&quot;)' in html


def test_installation_csv_neutralizes_treatment_and_calibration_identifiers(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(_installation_scene(), parent_revision_id=None)
    output = build_installation_output(saved.revision)

    dangerous = output.model_copy(update={
        'treatment': InstallationTreatmentSummary(
            status='AVAILABLE',
            instances=(
                InstallationTreatmentInstanceSummary(
                    instance_id='=cmd|"/c calc"!A0',
                    placement_version=1,
                    placement_sha256='a' * 64,
                    lifecycle='proposed',
                    definition_id='@def',
                    definition_version='1',
                    definition_sha256='b' * 64,
                    scene_revision_id='rev-1',
                    scene_content_hash='c' * 64,
                    host_surface_id='\t=surface',
                    host_surface_authority_sha256='d' * 64,
                    host_binding_evaluation_sha256='e' * 64,
                    host_binding_state='bound',
                    host_surface_semantic_class='wall',
                    position_m=(-1.0, 2.0, 3.0),
                    orientation={'yaw_deg': 0.0},
                    coverage_width_m=1.2,
                    coverage_height_m=0.6,
                    physical_width_m=1.2,
                    physical_height_m=0.6,
                    thickness_m=0.05,
                    air_gap_m=0.0,
                    face_area_m2=0.72,
                    treatment_type='absorber',
                    uncertainty_kind='unknown',
                    wave_material_capability='UNKNOWN',
                    geometric_material_capability='UNKNOWN',
                    solver_prediction_readiness='UNKNOWN',
                ),
            ),
            quantities=(
                InstallationTreatmentQuantitySummary(
                    definition_id='@def',
                    definition_version='1',
                    definition_sha256='b' * 64,
                    lifecycle='proposed',
                    quantity=2,
                    total_face_area_m2=1.44,
                    instance_ids=('=cmd|"/c calc"!A0',),
                ),
            ),
        ),
        'calibration': InstallationCalibrationSummary(
            status='AVAILABLE',
            lifecycle_state='proposed',
            requested_channels=(
                InstallationCalibrationChannelSummary(
                    settings_source='requested',
                    channel_id='-2+3+cmd',
                    role_id='FL',
                    source_entity_id='speaker-fl',
                    physical_output_id='@out-fl',
                    sample_rate_hz=48000,
                    gain_db=-1.5,
                    delay_s=0.0,
                    polarity='normal',
                ),
            ),
        ),
    })
    assert dangerous.semantic_sha256 == output.semantic_sha256

    csv_text = render_installation_csv(dangerous)
    rows = [row for row in csv.reader(io.StringIO(csv_text)) if row]
    for row in rows:
        for cell in row:
            _assert_spreadsheet_inert(cell)

    instance_row = next(
        row for row in rows
        if row[0] == 'treatment_instance' and row[1] != 'instance_id'
    )
    assert instance_row[1] == '\'=cmd|"/c calc"!A0'
    assert instance_row[2] == "'@def@1"
    assert instance_row[6] == "'-1,2,3"
    assert instance_row[8] == "'\t=surface"
    quantity_row = next(
        row for row in rows
        if row[0] == 'treatment_quantity' and row[1] != 'definition_id'
    )
    assert quantity_row[1] == "'@def@1"
    assert quantity_row[3] == '2'
    assert quantity_row[5] == '\'=cmd|"/c calc"!A0'
    calibration_row = next(
        row for row in rows
        if row[0] == 'calibration_setting' and row[1] != 'requested_or_exported'
    )
    assert calibration_row[1] == 'requested'
    assert calibration_row[2] == "'-2+3+cmd"
    assert calibration_row[3] == "'@out-fl"
    assert calibration_row[4] == '-1.5'
    assert calibration_row[9] == 'proposed'
