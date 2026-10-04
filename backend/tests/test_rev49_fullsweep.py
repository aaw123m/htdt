"""REV49-FULLSWEEP regressions.

Covers:
* palette crash — CommandContext must cover every WorkspaceId (#534/#541
  added workspaces without extending the enum; ``CommandContext(value)``
  raised ValueError inside the palette's context_provider on those
  workspaces and Ctrl+K died).
* navigation commands for presentation / video / acceptance.
* HealthCheckDialog's document-wide plan listing matches the per-baseline
  listing (and keeps the fail-closed baseline agreement).
* display-length policy propagation to the room geometry / installation /
  field explorer panels via MetricSpinBox.
* family-specific help topics bound from UserFacingError reason codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_display_units import display_length_policy
from htdt.cad_repository import SceneRepository
from htdt.command_registry import (
    CommandContext,
    CommandRegistry,
    WorkspaceDeepLink,
    default_command_definitions,
    register_default_commands,
)
from htdt.field_explorer_panel import FieldExplorerPanel
from htdt.help_registry import build_help_registry
from htdt.length_spinbox import MetricSpinBox
from htdt.workflow_navigation import (
    ApplicationDestinationId,
    WorkspaceId,
)


# ---------------------------------------------------------------------------
# Command palette context coverage


def test_command_context_covers_every_workspace() -> None:
    """The palette's context_provider converts WorkspaceId → CommandContext.

    Any workspace missing from CommandContext raises ValueError here — the
    exact failure that used to break Ctrl+K on presentation/video.
    """

    for workspace in WorkspaceId:
        context = CommandContext(workspace.value)
        assert context.value == workspace.value


def test_new_workspace_navigation_commands_registered() -> None:
    definitions = {item.command_id: item for item in default_command_definitions()}

    assert definitions['navigation.presentation'].display_name == 'プレゼン'
    assert definitions['navigation.presentation'].deep_link == WorkspaceDeepLink(
        WorkspaceId.PRESENTATION
    )
    assert definitions['navigation.video'].display_name == '映像調整'
    assert definitions['navigation.video'].deep_link == WorkspaceDeepLink(
        WorkspaceId.VIDEO
    )
    assert definitions['navigation.acceptance'].display_name == '受入検証'
    assert definitions['navigation.acceptance'].deep_link == WorkspaceDeepLink(
        ApplicationDestinationId.ACCEPTANCE
    )


def test_new_navigation_commands_execute_as_deep_links() -> None:
    registry = CommandRegistry()
    register_default_commands(registry)
    navigated: list[WorkspaceDeepLink] = []
    registry.set_deep_link_handler(lambda target: navigated.append(target))

    for command_id in (
        'navigation.presentation',
        'navigation.video',
        'navigation.acceptance',
    ):
        assert registry.availability(command_id).enabled is True
        assert registry.execute(command_id) is True

    assert [link.workspace for link in navigated] == [
        WorkspaceId.PRESENTATION,
        WorkspaceId.VIDEO,
        ApplicationDestinationId.ACCEPTANCE,
    ]


# ---------------------------------------------------------------------------
# Health repository: document-wide plan listing


def test_list_plans_for_document_matches_per_baseline_listing(
    tmp_path: Path,
) -> None:
    """``list_plans_for_document`` returns the same plan set as
    ``list_plans`` iterated over the document's baselines — the dialog's
    single-query replacement for the N+1 refresh loop.
    """

    from test_cad_system_health import (  # noqa: WPS433
        NOW,
        _baseline,
        _check,
        _repositories,
    )
    from htdt.cad_system_health import build_health_check_plan

    revision, repository = _repositories(tmp_path)
    baseline_a = _baseline(revision, name='ベースラインA')
    baseline_b = _baseline(revision, name='ベースラインB')
    repository.save_baseline(baseline_a)
    repository.save_baseline(baseline_b)
    plan_a = build_health_check_plan(
        baseline_a, checks=(_check(),), created_at_utc=NOW
    )
    plan_b = build_health_check_plan(
        baseline_b, checks=(_check(check_id='check-b'),), created_at_utc=NOW
    )
    repository.save_plan(plan_a)
    repository.save_plan(plan_b)

    document_plans = repository.list_plans_for_document(revision.document_id)
    assert {plan.plan_id for plan in document_plans} == {
        plan_a.plan_id,
        plan_b.plan_id,
    }
    per_baseline = {
        plan.plan_id
        for baseline in (baseline_a, baseline_b)
        for plan in repository.list_plans(baseline.baseline_id)
    }
    assert {plan.plan_id for plan in document_plans} == per_baseline

    # A foreign document id never leaks plans: the JOIN binds baseline
    # identity, so an empty result is the fail-closed answer.
    assert repository.list_plans_for_document('other-doc') == ()


# ---------------------------------------------------------------------------
# MetricSpinBox module extraction + panel display-policy propagation


def test_metric_spinbox_module_export_and_reexport() -> None:
    from htdt import length_spinbox, room_workspace

    assert room_workspace.MetricSpinBox is length_spinbox.MetricSpinBox
    assert room_workspace.PendingTextSpinBox is (
        length_spinbox.PendingTextSpinBox
    )


def test_metric_spinbox_minimum_maximum_m_rescale_on_unit_change() -> None:
    """``set_minimum_m``/``set_maximum_m`` keep bounds in SI across units."""

    from test_room_cadux import _app  # noqa: WPS433

    _app()
    field = MetricSpinBox(minimum_m=0.0, maximum_m=4.0)
    field.set_display_unit('mm')
    assert field.minimum() == pytest.approx(0.0)
    assert field.maximum() == pytest.approx(4000.0)
    field.set_display_unit('m')
    assert field.maximum() == pytest.approx(4.0)


def test_geometry_panel_fields_follow_length_policy(tmp_path: Path) -> None:
    from test_room_cadux import _workspace  # noqa: WPS433
    from htdt.room_geometry_input import RoomGeometryInputController
    from htdt.room_geometry_panel import RoomGeometryPanel

    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(
        workspace, workspace.viewport
    )
    panel = RoomGeometryPanel(geometry)
    try:
        panel.set_length_policy(display_length_policy('mm'))
        for field in panel._length_fields():
            assert field.display_unit() == 'mm'
            assert field.suffix().endswith('mm')
    finally:
        panel.deleteLater()
        geometry.dispose()
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_installation_panel_spins_follow_length_policy(tmp_path: Path) -> None:
    from test_rev44_install_surfaces import _app  # noqa: WPS433
    from htdt.cad_equipment_repository import CadEquipmentRepository
    from htdt.cad_system_variant_repository import CadSystemVariantRepository
    from htdt.installation_panel import InstallationPanel

    app = _app()
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        CadSystemVariantRepository(scene_repository),
    )
    panel = InstallationPanel(
        scene_repository, equipment_repository, 'rev49-doc'
    )
    try:
        panel.set_length_policy(display_length_policy('cm'))
        for spin in panel.clearance_spins.values():
            assert spin.display_unit() == 'cm'
            assert spin.suffix().endswith('cm')
    finally:
        panel.deleteLater()
        app.processEvents()


def test_field_explorer_spins_follow_length_policy(tmp_path: Path) -> None:
    from test_room_cadux import _app  # noqa: WPS433
    from htdt.cad_prediction_repository import CadPredictionRepository

    app = _app()
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    prediction_repository = CadPredictionRepository(scene_repository)
    panel = FieldExplorerPanel(
        scene_repository, prediction_repository, 'rev49-doc'
    )
    try:
        panel.set_length_policy(display_length_policy('inch'))
        assert panel.stride_field.display_unit() == 'inch'
        assert panel.probe_x.display_unit() == 'inch'
        # Stride stays stored in SI metres regardless of the display unit.
        panel.stride_field.set_display_unit('m')
        panel.stride_field.set_value_m(0.1)
        panel.stride_field.set_display_unit('mm')
        assert panel.stride_field.value_m() == pytest.approx(0.1)
    finally:
        panel.deleteLater()
        app.processEvents()


# ---------------------------------------------------------------------------
# Error-help topic families


def test_error_reason_codes_resolve_to_family_topics() -> None:
    registry = build_help_registry()
    assert (
        registry.topic_for_reason('rew.unavailable').topic_id
        == 'trouble.rew_errors'
    )
    assert (
        registry.topic_for_reason('import.rew').topic_id
        == 'trouble.rew_errors'
    )
    assert (
        registry.topic_for_reason('storage.locked').topic_id
        == 'trouble.storage_errors'
    )
    assert (
        registry.topic_for_reason('migration.failed').topic_id
        == 'trouble.storage_errors'
    )
    assert (
        registry.topic_for_reason('io.address_in_use').topic_id
        == 'trouble.storage_errors'
    )
    assert (
        registry.topic_for_reason('authority.conflict').topic_id
        == 'trouble.authority_errors'
    )
    assert (
        registry.topic_for_reason('authority.stale_head').topic_id
        == 'trouble.authority_errors'
    )
    # Unclaimed codes still land on the umbrella topic.
    assert (
        registry.topic_for_reason('operation.failed').topic_id
        == 'trouble.operation_error'
    )
    assert (
        registry.topic_for_reason('project.not_found').topic_id
        == 'trouble.operation_error'
    )
