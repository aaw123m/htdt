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
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.installation_handoff import (
    build_installation_handoff,
    handoff_preview_text,
    render_dimension_sheets_csv,
    render_handoff_report_html,
    render_settings_csv,
    review_installation_output,
    write_handoff_package,
)
from htdt.installation_output_authority import InstallationReportService


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
    )
    return base.model_copy(update={'entities': base.entities + additions})


def _variant(revision):
    changed = revision.document.entity('speaker-fl').model_copy(
        update={
            'orientation': quaternion_from_euler_deg(
                yaw_deg=12.0, pitch_deg=-4.0, roll_deg=0.0
            )
        }
    )
    return build_system_variant(
        baseline=revision,
        name='install variant',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='speaker-fl-pose',
                entity=changed,
                role_binding_id='FL',
            ),
        ),
        created_at_utc='2026-09-24T00:00:00+00:00',
    )


def _service(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(
        _installation_scene(), parent_revision_id=None
    )
    variant = _variant(saved.revision)
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant_repository.save_variant(variant)
    service = InstallationReportService(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
    )
    return saved, variant, service


def test_handoff_requires_explicit_revision(tmp_path: Path) -> None:
    _saved, _variant, service = _service(tmp_path)
    with pytest.raises(Exception):
        service.build_installation_output_from_authorities(
            scene_revision_id='no-such-revision'
        )


def test_handoff_builds_review_and_deterministic_exports(
    tmp_path: Path,
) -> None:
    saved, variant, service = _service(tmp_path)
    handoff = build_installation_handoff(
        service,
        scene_revision_id=saved.revision.revision_id,
        system_variant_id=variant.variant_id,
        generated_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert handoff.output.authority.system_variant_id == variant.variant_id
    assert handoff.review.complete is False
    assert 'calibration_plan' in handoff.review.degraded

    review = review_installation_output(handoff.output)
    assert review.sections == handoff.output.sections

    dimensions_first = render_dimension_sheets_csv(handoff)
    dimensions_second = render_dimension_sheets_csv(handoff)
    assert dimensions_first == dimensions_second
    assert 'top' in dimensions_first
    assert 'speaker-fl' in dimensions_first

    settings_first = render_settings_csv(handoff)
    assert render_settings_csv(handoff) == settings_first
    assert 'settings_source' in settings_first

    report = render_handoff_report_html(handoff)
    assert 'installation' in report.lower() or '<html' in report.lower()
    assert handoff.output.semantic_sha256 in report

    preview = handoff_preview_text(handoff)
    assert saved.revision.revision_id in preview
    assert 'AVAILABLE' in preview or 'UNKNOWN' in preview


def test_handoff_package_writes_deterministic_files(tmp_path: Path) -> None:
    saved, _variant, service = _service(tmp_path)
    handoff = build_installation_handoff(
        service,
        scene_revision_id=saved.revision.revision_id,
        system_variant_id='',
        generated_at_utc='2026-09-24T01:00:00+00:00',
    )
    outputs = write_handoff_package(handoff, tmp_path / 'handoff')
    assert set(outputs) == {'report', 'dimensions', 'settings', 'entities'}
    for path in outputs.values():
        assert path.exists()
    regenerated = write_handoff_package(handoff, tmp_path / 'handoff2')
    for key, path in outputs.items():
        assert path.read_text(encoding='utf-8') == regenerated[
            key
        ].read_text(encoding='utf-8')
