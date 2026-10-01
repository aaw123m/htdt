from hashlib import sha256
import json
from pathlib import Path

import pytest

import htdt.installation_handoff as installation_handoff

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
    assert '利用可能' in preview or '不明' in preview
    # The preview is the operator's last look before writing — it must
    # expose the actual package contents, not record counts.
    assert 'x=' in preview and 'y=' in preview
    assert 'speaker-fl' in preview or 'Front Left' in preview
    assert '== ' in preview
    # Internal enum vocabulary renders in Japanese on this surface; ids
    # and hashes stay verbatim.
    assert '[スピーカー]' in preview
    assert '[座席]' in preview
    assert '  上面: ' in preview
    assert '校正プラン' in preview
    assert 'calibration_plan' not in preview
    # Section verdict reasons render in Japanese on this surface too —
    # the stored reason strings stay English domain prose.
    assert 'authority is bound' not in preview
    assert '結び付けられていません' in preview


def test_handoff_package_writes_deterministic_files(tmp_path: Path) -> None:
    saved, _variant, service = _service(tmp_path)
    handoff = build_installation_handoff(
        service,
        scene_revision_id=saved.revision.revision_id,
        system_variant_id='',
        generated_at_utc='2026-09-24T01:00:00+00:00',
    )
    outputs = write_handoff_package(handoff, tmp_path / 'handoff')
    assert set(outputs) == {
        'report',
        'dimensions',
        'settings',
        'entities',
        'manifest',
    }
    for path in outputs.values():
        assert path.exists()
    regenerated = write_handoff_package(handoff, tmp_path / 'handoff2')
    for key, path in outputs.items():
        assert path.read_text(encoding='utf-8') == regenerated[
            key
        ].read_text(encoding='utf-8')


def test_handoff_manifest_ties_members_to_one_generation(
    tmp_path: Path,
) -> None:
    saved, _variant, service = _service(tmp_path)
    handoff = build_installation_handoff(
        service,
        scene_revision_id=saved.revision.revision_id,
        system_variant_id='',
        generated_at_utc='2026-09-24T01:00:00+00:00',
    )
    target = tmp_path / 'handoff-manifest'
    outputs = write_handoff_package(handoff, target)
    manifest = json.loads(
        outputs['manifest'].read_text(encoding='utf-8')
    )
    assert manifest['scene_revision_id'] == saved.revision.revision_id
    assert manifest['semantic_sha256'] == handoff.output.semantic_sha256
    assert manifest['generated_at_utc'] == '2026-09-24T01:00:00+00:00'
    # Machine-readable degraded state — completeness is never inferred
    # from file presence alone.
    assert manifest['complete'] is False
    assert 'calibration_plan' in manifest['degraded']
    digests = {entry['name']: entry for entry in manifest['files']}
    assert set(digests) == {
        'dimension_sheets.csv',
        'settings.csv',
        'installation_report.html',
        'installation_coordinates.csv',
    }
    for name, entry in digests.items():
        member = (target / name).read_bytes()
        assert entry['sha256'] == sha256(member).hexdigest()
        assert entry['size_bytes'] == len(member)


def test_handoff_package_failure_restores_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved, _variant, service = _service(tmp_path)
    first = build_installation_handoff(
        service,
        scene_revision_id=saved.revision.revision_id,
        system_variant_id='',
        generated_at_utc='2026-09-24T01:00:00+00:00',
    )
    target = tmp_path / 'handoff-atomic'
    previous = write_handoff_package(first, target)
    before = {
        name: path.read_bytes() for name, path in previous.items()
    }
    second = build_installation_handoff(
        service,
        scene_revision_id=saved.revision.revision_id,
        system_variant_id='',
        generated_at_utc='2026-09-24T02:00:00+00:00',
    )
    real_replace = installation_handoff.os.replace
    calls = {'count': 0}

    def failing_replace(src, dst):
        # Fail while promoting the third member file — after two member
        # files of the new generation were already moved into place.
        calls['count'] += 1
        if calls['count'] == 6:
            raise OSError('injected publish failure')
        real_replace(src, dst)

    monkeypatch.setattr(
        installation_handoff.os, 'replace', failing_replace
    )
    with pytest.raises(OSError, match='injected publish failure'):
        write_handoff_package(second, target)
    monkeypatch.undo()
    # The previous complete package is intact: every file — members and
    # the manifest — is back to the earlier generation's bytes.
    for name, path in previous.items():
        assert path.read_bytes() == before[name]
    leftovers = [
        path
        for path in target.iterdir()
        if path.name.startswith('.htdt-handoff-')
    ]
    assert leftovers == []


def test_handoff_package_leaves_no_staging_on_disk(tmp_path: Path) -> None:
    saved, _variant, service = _service(tmp_path)
    handoff = build_installation_handoff(
        service,
        scene_revision_id=saved.revision.revision_id,
        system_variant_id='',
        generated_at_utc='2026-09-24T01:00:00+00:00',
    )
    target = tmp_path / 'handoff-out'
    write_handoff_package(handoff, target)
    leftovers = [
        path
        for path in target.iterdir()
        if path.name.startswith('.htdt-handoff-')
    ]
    assert leftovers == []
