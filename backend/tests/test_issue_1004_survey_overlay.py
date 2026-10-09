"""#1004: as-built survey uncertainty / unverified-element overlay (REV73).

Covers the resolver + viewport + panel contract:

- one room carrying mixed evidence (traceable survey / consumer AR /
  design-only / unknown-instrument / noncurrent-stale);
- element_key resolution against the CURRENT head's stable ids — wrong
  source frame, re-numbered keys, missing ids, and non-unique keys all
  land in honest ``unmapped`` buckets with zero drawn actors;
- residual rendering honesty — registration-consumed controls are never
  presented as validation passes, tolerance-less controls show residual
  only;
- stale cleanup — a re-saved element whose live hash no longer matches
  the sealed qualification is unevaluated, and a new head revision
  re-resolves everything;
- ASCII viewport legend + aggregate counts, zoomed-out label
  suppression, narrow/DPI200 panel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_geometry_survey import (
    GeometryFrameDeclaration,
    RegistrationTransform,
    build_control_measurement,
    build_element_evidence,
    build_reconciliation,
    build_survey_campaign,
    build_survey_instrument,
    build_task_requirement,
    evaluate_geometry_qualification,
)
from htdt.cad_geometry_survey_repository import CadGeometrySurveyRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, SceneDocument
from htdt.room_survey_overlay import (
    MODE_LEGEND_ASCII,
    SURVEY_OVERLAY_MODES,
    RoomSurveyOverlayController,
    resolve_survey_overlay,
)

DOC = 'doc-issue-1004'
_TS = '2026-10-05T12:00:00+00:00'


def _document() -> SceneDocument:
    return SceneDocument(
        document_id=DOC,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(),
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_document(), parent_revision_id=None)
    survey = CadGeometrySurveyRepository(scene_repository)
    seeded = _seed_survey(survey)
    return {
        'scene_repository': scene_repository,
        'survey_repository': survey,
        'revision': revision,
        **seeded,
    }


def _seed_survey(survey: CadGeometrySurveyRepository) -> dict:
    """Mixed-authority room: traceable TLS, consumer AR, design-only,
    unknown-instrument, and a stale element — plus every unmapped cause."""

    tls = build_survey_instrument(
        kind='terrestrial_laser_scanner',
        capability_class='traceable_survey_instrument',
        manufacturer='FARO',
        model='Focus Premium',
        nominal_accuracy_mm=1.0,
        calibration_evidence_refs=('cal-cert:1',),
    )
    ar = build_survey_instrument(
        kind='mobile_lidar_device',
        capability_class='consumer_ar_capability',
        capture_app='CaptureApp',
        capture_app_version='2.1',
    )
    unknown_inst = build_survey_instrument(
        kind='unknown',
        capability_class='unknown',
    )
    for instrument in (tls, ar, unknown_inst):
        survey.save_instrument(instrument)

    campaign_tls = build_survey_campaign(
        document_id=DOC,
        label='TLS scan',
        captured_at_utc=_TS,
        instrument_ids=(tls.instrument_id,),
        registration=RegistrationTransform(method='icp', uncertainty_mm=3.0),
        frame=GeometryFrameDeclaration(frame_name='site', length_unit='m'),
    )
    campaign_ar = build_survey_campaign(
        document_id=DOC,
        label='AR walkthrough',
        captured_at_utc=_TS,
        instrument_ids=(ar.instrument_id,),
        frame=GeometryFrameDeclaration(frame_name='arkit', length_unit='m'),
    )
    campaign_unknown = build_survey_campaign(
        document_id=DOC,
        label='unspecified capture',
        captured_at_utc=_TS,
        instrument_ids=(unknown_inst.instrument_id,),
    )
    campaign_mirrored = build_survey_campaign(
        document_id=DOC,
        label='left-handed export',
        captured_at_utc=_TS,
        instrument_ids=(tls.instrument_id,),
        frame=GeometryFrameDeclaration(
            frame_name='mirror', length_unit='m', right_handed=False
        ),
    )
    for campaign in (
        campaign_tls,
        campaign_ar,
        campaign_unknown,
        campaign_mirrored,
    ):
        survey.save_campaign(campaign)

    elements = {}
    elements['wall0'] = build_element_evidence(
        document_id=DOC,
        element_key='wall:0',
        evidence_classes=('terrestrial_laser_scan',),
        observation_state='observed_surface',
        campaign_ids=(campaign_tls.campaign_id,),
    )
    elements['wall1'] = build_element_evidence(
        document_id=DOC,
        element_key='wall:1',
        evidence_classes=('mobile_lidar',),
        observation_state='observed_surface',
        campaign_ids=(campaign_ar.campaign_id,),
    )
    elements['wall2'] = build_element_evidence(
        document_id=DOC,
        element_key='wall:2',
        evidence_classes=('design_drawing',),
        observation_state='design_source_only',
    )
    elements['floor'] = build_element_evidence(
        document_id=DOC,
        element_key='floor',
        evidence_classes=('unknown',),
        observation_state='observed_surface',
        campaign_ids=(campaign_unknown.campaign_id,),
    )
    elements['ceiling'] = build_element_evidence(
        document_id=DOC,
        element_key='ceiling',
        evidence_classes=('terrestrial_laser_scan',),
        observation_state='observed_surface',
        campaign_ids=(campaign_tls.campaign_id,),
        stale_after_change=True,
    )
    # Non-unique: two sealed records carry the same element_key — neither
    # may colour the surface.
    elements['dup_a'] = build_element_evidence(
        document_id=DOC,
        element_key='wall:3',
        evidence_classes=('manual_tape_measure',),
        observation_state='observed_surface',
        campaign_ids=(campaign_unknown.campaign_id,),
    )
    elements['dup_b'] = build_element_evidence(
        document_id=DOC,
        element_key='wall:3',
        evidence_classes=('laser_distance_meter',),
        observation_state='observed_surface',
        campaign_ids=(campaign_unknown.campaign_id,),
        notes='second instrument, same key',
    )
    elements['renumbered'] = build_element_evidence(
        document_id=DOC,
        element_key='wall:9',
        evidence_classes=('terrestrial_laser_scan',),
        observation_state='observed_surface',
        campaign_ids=(campaign_tls.campaign_id,),
    )
    elements['missing'] = build_element_evidence(
        document_id=DOC,
        element_key='wall:north',
        evidence_classes=('design_drawing',),
        observation_state='design_source_only',
    )
    elements['frame'] = build_element_evidence(
        document_id=DOC,
        element_key='wall:8',
        evidence_classes=('terrestrial_laser_scan',),
        observation_state='observed_surface',
        campaign_ids=(campaign_mirrored.campaign_id,),
    )
    for element in elements.values():
        survey.save_element(element)

    controls = {}
    controls['pass'] = build_control_measurement(
        document_id=DOC,
        kind='wall_to_wall_distance',
        campaign_id=campaign_tls.campaign_id,
        element_keys=('wall:0', 'wall:2'),
        measured_value_mm=6002.0,
        declared_value_mm=6000.0,
        tolerance_mm=5.0,
    )
    controls['reg_used'] = build_control_measurement(
        document_id=DOC,
        kind='known_target',
        campaign_id=campaign_tls.campaign_id,
        element_keys=('wall:0',),
        measured_value_mm=6001.0,
        declared_value_mm=6000.0,
        tolerance_mm=5.0,
        used_for_registration=True,
    )
    controls['no_tol'] = build_control_measurement(
        document_id=DOC,
        kind='wall_to_wall_distance',
        campaign_id=campaign_ar.campaign_id,
        element_keys=('wall:1',),
        measured_value_mm=4003.0,
        declared_value_mm=4000.0,
    )
    controls['fail'] = build_control_measurement(
        document_id=DOC,
        kind='ceiling_height',
        campaign_id=campaign_ar.campaign_id,
        element_keys=('wall:1',),
        measured_value_mm=2500.0,
        declared_value_mm=2400.0,
        tolerance_mm=20.0,
    )
    controls['unmapped'] = build_control_measurement(
        document_id=DOC,
        kind='wall_to_wall_distance',
        campaign_id=campaign_tls.campaign_id,
        element_keys=('wall:9',),
        measured_value_mm=900.0,
        declared_value_mm=900.0,
        tolerance_mm=5.0,
    )
    for control in controls.values():
        survey.save_control(control)

    recon_wall0 = build_reconciliation(
        document_id=DOC,
        element_key='wall:0',
        reconciled_at_utc='2026-10-06T00:00:00+00:00',
        design_value_mm=6000.0,
        as_built_value_mm=5988.0,
        delta_mm=-12.0,
        approved_change=False,
        downstream_invalidated_refs=('artifact:pfftd-run-9',),
    )
    recon_wall2 = build_reconciliation(
        document_id=DOC,
        element_key='wall:2',
        reconciled_at_utc='2026-10-06T00:00:00+00:00',
        design_value_mm=4000.0,
        as_built_value_mm=4003.0,
        delta_mm=3.0,
        approved_change=True,
    )
    recon_floor = build_reconciliation(
        document_id=DOC,
        element_key='floor',
        reconciled_at_utc='2026-10-06T00:00:00+00:00',
        design_value_mm=4000.0,
        delta_mm=None,
    )
    for record in (recon_wall0, recon_wall2, recon_floor):
        survey.save_reconciliation(record)

    task = build_task_requirement(
        document_id=DOC,
        task_class='sbir_early_reflection',
        required_element_keys=('wall:0', 'wall:1'),
    )
    survey.save_task_requirement(task)

    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=survey.list_elements(DOC),
        campaigns=survey.list_campaigns(DOC),
        controls=survey.list_controls(DOC),
        task_requirements=survey.list_task_requirements(DOC),
        reconciliations=survey.get_reconciliation(recon_wall0.reconciliation_id)
        and tuple(
            survey.get_reconciliation(r.reconciliation_id)
            for r in (recon_wall0, recon_wall2, recon_floor)
        ),
        evaluated_at_utc='2026-10-06T01:00:00+00:00',
    )
    survey.save_qualification(qualification)
    return {
        'elements': elements,
        'controls': controls,
        'qualification': qualification,
        'task': task,
        'instruments': (tls, ar, unknown_inst),
    }


def _resolve(fixture, mode='verification'):
    return resolve_survey_overlay(
        fixture['scene_repository'],
        fixture['survey_repository'],
        DOC,
        mode,
    )


def _by_key(scene):
    return {
        item.element.element_key: item
        for item in (*scene.elements, *scene.unmapped_elements)
    }


# ---------------------------------------------------------------------------
# Resolution / mapping honesty
# ---------------------------------------------------------------------------


def test_mixed_tiers_states_and_unmapped_buckets(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    scene = _resolve(fixture)
    assert scene is not None
    by_key = _by_key(scene)

    # Mixed evidence in one room, all resolved:
    assert by_key['wall:0'].verification == 'verified'  # traceable TLS
    assert by_key['wall:0'].state_entry.state == 'field_checked'
    assert by_key['wall:1'].state_entry.state == 'control_check_failed'
    assert by_key['wall:1'].verification == 'failed'  # consumer AR, failed
    assert by_key['wall:2'].state_entry.state == 'design_only'
    assert by_key['wall:2'].verification == 'unverified'  # design-only
    assert by_key['floor'].verification in ('unverified', 'failed')
    assert by_key['ceiling'].verification == 'stale'  # noncurrent evidence

    # Wrong/mis-keyed sources never draw: each carries an honest reason.
    unmapped = {item.element.element_key: item for item in scene.unmapped_elements}
    assert unmapped['wall:9'].unmapped_reason == 'renumbered'
    assert unmapped['wall:north'].unmapped_reason == 'missing'
    assert unmapped['wall:8'].unmapped_reason == 'unsupported_frame'
    dup = [i for i in scene.unmapped_elements if i.element.element_key == 'wall:3']
    assert len(dup) == 2
    assert all(i.unmapped_reason == 'non_unique' for i in dup)


def test_tier_and_uncertainty_and_delta_modes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    scene = _resolve(fixture, 'tier')
    by_key = _by_key(scene)
    assert by_key['wall:0'].bucket == 't3'
    assert by_key['wall:1'].bucket == 't2'
    assert by_key['wall:2'].bucket == 't0'
    assert by_key['ceiling'].bucket == 't3'

    scene = _resolve(fixture, 'uncertainty_mm')
    by_key = _by_key(scene)
    assert by_key['wall:0'].bucket == 'le5'  # residual 2mm via pass control
    # The wall:0↔wall:2 span control legitimately covers wall:2 too.
    assert by_key['wall:2'].bucket == 'le5'
    # No-data elements are never 0mm — explicit unknown bucket.
    assert by_key['floor'].bucket == 'unknown'
    assert by_key['wall:1'].bucket == 'unknown'  # failed control ≠ achieved

    scene = _resolve(fixture, 'delta_mm')
    by_key = _by_key(scene)
    assert by_key['wall:0'].bucket == 'delta_le50'
    assert 'd-12mm*' in by_key['wall:0'].label_ascii  # unapproved marker
    assert by_key['wall:2'].bucket == 'delta_le10'
    assert 'd+3mm' in by_key['wall:2'].label_ascii
    assert '*' not in by_key['wall:2'].label_ascii  # approved
    assert by_key['floor'].bucket == 'delta_na'  # record without delta
    assert by_key['ceiling'].bucket == 'none'  # no record at all


def test_stale_colors_cleared_on_hash_and_revision_change(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    scene = _resolve(fixture)
    assert _by_key(scene)['wall:0'].state_entry is not None

    # Re-seal wall:0 with a note — a NEW sealed element on the same key
    # (records are append-only). Two live records for one key means the
    # authority is ambiguous: the wall shows 'non_unique' unmapped and the
    # superseded 'verified' colour never bleeds onto the new record.
    element = fixture['elements']['wall0']
    fixture['survey_repository'].save_element(
        build_element_evidence(
            document_id=DOC,
            element_key='wall:0',
            evidence_classes=element.evidence_classes,
            observation_state=element.observation_state,
            campaign_ids=element.campaign_ids,
            notes='re-check after doorway edit',
        )
    )
    scene = _resolve(fixture)
    wall0_items = [
        item
        for item in (*scene.elements, *scene.unmapped_elements)
        if item.element.element_key == 'wall:0'
    ]
    assert len(wall0_items) == 2  # superseded + re-sealed records
    assert all(
        item.unmapped_reason == 'non_unique' for item in wall0_items
    )
    # The superseded record's own sha still matches the sealed verdict —
    # but as a duplicate-key authority it is unmapped, so the 'verified'
    # colour never reaches the surface.
    assert 'wall:0' in {
        item.element.element_key for item in scene.unmapped_elements
    }
    assert not any(
        item.element.element_key == 'wall:0' for item in scene.elements
    )
    # Qualification itself is untouched — read-only overlay.
    assert (
        fixture['survey_repository']
        .latest_qualification(DOC)
        .qualification_sha256
        == fixture['qualification'].qualification_sha256
    )


def test_scene_revision_change_reresolves(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    controller = RoomSurveyOverlayController(
        fixture['scene_repository'], DOC
    )
    first = controller.resolve('verification')
    assert first is not None
    assert controller.resolve('verification') is first  # cached

    # Edit the scene (wider room → new head): resolution re-binds to the
    # new revision — a stale colour cannot survive.
    repo = fixture['scene_repository']
    document = repo.current_head(DOC).document
    repo.save(
        document.model_copy(
            update={
                'room': RoomPrism(width_m=8.0, depth_m=4.0, height_m=2.4)
            }
        ),
        parent_revision_id=first.revision_id,
    )
    second = controller.resolve('verification')
    assert second is not None
    assert second.revision_id != first.revision_id


def test_viewport_lines_ascii_counts_and_legend(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    for mode in SURVEY_OVERLAY_MODES:
        scene = _resolve(fixture, mode)
        assert all(line.isascii() for line in scene.viewport_lines)
        joined = '\n'.join(scene.viewport_lines)
        assert 'legend:' in joined
        assert 'counts:' in joined
        assert 'CURRENT' in joined
        for legend_line in MODE_LEGEND_ASCII[mode]:
            assert legend_line in scene.viewport_lines


def test_read_only_overlay_never_mutates_verdicts(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    before = fixture['survey_repository'].latest_qualification(DOC)
    for mode in SURVEY_OVERLAY_MODES:
        _resolve(fixture, mode)
    RoomSurveyOverlayController(
        fixture['scene_repository'], DOC
    ).describe_element(fixture['elements']['wall0'].element_id)
    after = fixture['survey_repository'].latest_qualification(DOC)
    assert before.qualification_sha256 == after.qualification_sha256
    assert before == after


# ---------------------------------------------------------------------------
# Controls — residual rendering honesty
# ---------------------------------------------------------------------------


def test_control_buckets_and_labels(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    scene = _resolve(fixture)
    controls = {c.control.control_id: c for c in scene.controls}
    controls.update({c.control.control_id: c for c in scene.unmapped_controls})
    by_kind = {
        item.control.control_id: item
        for item in (*scene.controls, *scene.unmapped_controls)
    }
    fixture_controls = fixture['controls']

    pass_item = by_kind[fixture_controls['pass'].control_id]
    assert pass_item.bucket == 'pass'
    assert len(pass_item.anchors) == 2
    assert 'PASS' in pass_item.label_ascii

    reg = by_kind[fixture_controls['reg_used'].control_id]
    assert reg.bucket == 'reg_used'  # never 'pass'
    assert 'REG-USED' in reg.label_ascii
    assert 'not validation' in reg.label_ascii

    no_tol = by_kind[fixture_controls['no_tol'].control_id]
    assert no_tol.bucket == 'evidence_only'
    assert 'r=3mm' in no_tol.label_ascii
    assert 'no tolerance' in no_tol.label_ascii
    assert 'PASS' not in no_tol.label_ascii

    fail = by_kind[fixture_controls['fail'].control_id]
    assert fail.bucket == 'fail'
    assert 'FAIL' in fail.label_ascii

    unmapped = by_kind[fixture_controls['unmapped'].control_id]
    assert unmapped.bucket == 'unmapped'
    assert not unmapped.anchors


# ---------------------------------------------------------------------------
# Authority detail (click target)
# ---------------------------------------------------------------------------


def test_describe_element_authority_bundle(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    controller = RoomSurveyOverlayController(
        fixture['scene_repository'], DOC
    )
    controller.resolve('verification')
    element = fixture['elements']['wall0']
    detail = controller.describe_element(element.element_id)
    assert detail is not None
    assert detail.element_sha256 == element.element_sha256
    assert detail.campaigns and detail.campaigns[0].label == 'TLS scan'
    assert detail.instruments
    instrument = detail.instruments[0]
    assert instrument.kind == 'terrestrial_laser_scanner'
    assert instrument.capability_class == 'traceable_survey_instrument'
    assert instrument.calibration_evidence_refs
    assert detail.state_entry is not None
    assert detail.reconciliations
    assert detail.invalidated_refs == ('artifact:pfftd-run-9',)
    text = '\n'.join(detail.detail_lines_ja)
    assert '位置合わせ' in text
    assert 'hash' in text


def test_describe_element_blocked_task_reasons(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    controller = RoomSurveyOverlayController(
        fixture['scene_repository'], DOC
    )
    wall1 = fixture['elements']['wall1']
    detail = controller.describe_element(wall1.element_id)
    assert detail is not None
    assert detail.blocking_task_verdicts
    task_id, verdict, reasons = detail.blocking_task_verdicts[0]
    assert task_id == fixture['task'].task_id
    assert verdict == 'control_check_failed'
    assert 'CONTROL_CHECK_FAILED' in reasons
    assert any(verdict in line or task_id in line
               for line in detail.detail_lines_ja)


# ---------------------------------------------------------------------------
# Viewport actors
# ---------------------------------------------------------------------------


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(['htdt-test'])


def test_viewport_actors_prefix_pickable_and_clear(tmp_path: Path) -> None:
    _qapp()
    from htdt.room_viewport import RoomViewport3D

    fixture = _fixture(tmp_path)
    scene = _resolve(fixture)
    viewport = RoomViewport3D()
    with viewport.deferred_render():
        viewport.render_survey_overlay(scene)
    names = [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('survey-overlay-')
    ]
    assert any('survey-overlay-item-' in n for n in names)
    assert any('survey-overlay-ctrl-' in n for n in names)
    assert 'survey-overlay-status' in names
    for name in names:
        actor = viewport.plotter.renderer.actors[name]
        if name != 'survey-overlay-status':
            assert not actor.GetPickable()
    # Unmapped elements contribute no fill actors: 5 mapped elements max.
    fills = [n for n in names if n.endswith('-fill')]
    assert len(fills) == len(scene.elements)

    viewport.clear_survey_overlay()
    assert not [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('survey-overlay-')
    ]


def test_viewport_prefix_swept_by_overlay_removal(tmp_path: Path) -> None:
    _qapp()
    from htdt.room_viewport import RoomViewport3D

    fixture = _fixture(tmp_path)
    scene = _resolve(fixture)
    viewport = RoomViewport3D()
    viewport.render_survey_overlay(scene)
    assert 'survey-overlay-' in RoomViewport3D._OVERLAY_ACTOR_PREFIXES
    viewport._remove_overlay_actors()
    assert not any(
        name.startswith('survey-overlay-')
        for name in viewport.plotter.renderer.actors
    )


def test_viewport_zoomed_out_suppresses_element_labels(
    tmp_path: Path,
) -> None:
    _qapp()
    from htdt.room_viewport import RoomViewport3D

    fixture = _fixture(tmp_path)
    scene = _resolve(fixture)
    viewport = RoomViewport3D()
    camera = viewport.plotter.camera
    # Push the visible world span far beyond the zoomed-out threshold
    # using the app's own zoom model (parallel scale): labels (thin
    # per-element detail) disappear; fills + aggregate status remain.
    # Note: camera.GetDistance() is NOT the gate — the viewport zooms via
    # SetParallelScale/camera.Zoom, so distance stays constant in-app.
    camera.SetParallelProjection(1)
    camera.SetParallelScale(50.0)
    viewport.render_survey_overlay(scene)
    names = [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('survey-overlay-')
    ]
    assert 'survey-overlay-status' in names
    assert not any('survey-overlay-labels-' in n for n in names)
    assert any(n.endswith('-fill') for n in names)


def test_viewport_near_zoom_shows_labels(tmp_path: Path) -> None:
    _qapp()
    from htdt.room_viewport import RoomViewport3D

    fixture = _fixture(tmp_path)
    scene = _resolve(fixture)
    viewport = RoomViewport3D()
    camera = viewport.plotter.camera
    # Near view under the app's parallel-projection zoom model: a small
    # parallel scale means a tight visible span, so labels appear even
    # though the camera distance itself is unchanged by wheel zoom.
    camera.SetParallelProjection(1)
    camera.SetParallelScale(2.0)
    viewport.render_survey_overlay(scene)
    assert any(
        name.startswith('survey-overlay-labels')
        for name in viewport.plotter.renderer.actors
    )

    # Perspective path: half-span = distance * tan(angle/2); a near camera
    # under perspective also reveals the labels.
    viewport.clear_survey_overlay()
    camera.SetParallelProjection(0)
    focal = camera.GetFocalPoint()
    camera.SetPosition(focal[0], focal[1] - 6.0, focal[2] + 3.0)
    viewport.render_survey_overlay(scene)
    assert any(
        name.startswith('survey-overlay-labels')
        for name in viewport.plotter.renderer.actors
    )


# ---------------------------------------------------------------------------
# Panel (Qt) — narrow / DPI200
# ---------------------------------------------------------------------------


def test_panel_lists_sections_and_detail(tmp_path: Path) -> None:
    _qapp()
    from htdt.room_survey_panel import RoomSurveyPanel

    fixture = _fixture(tmp_path)
    controller = RoomSurveyOverlayController(
        fixture['scene_repository'], DOC
    )
    panel = RoomSurveyPanel(controller)
    scene = controller.resolve('verification')
    panel.show_scene(scene)

    roots = {
        panel.elements.topLevelItem(i).text(0)
        for i in range(panel.elements.topLevelItemCount())
    }
    assert '要素' in roots
    assert 'マッピング不可' in roots
    assert '独立コントロール' in roots
    assert 'ブロック中のタスク' in roots

    # Click → authority detail in one op.
    mapped_root = panel.elements.topLevelItem(0)
    first = mapped_root.child(0)
    panel.elements.setCurrentItem(first)
    assert panel.detail.toPlainText()
    panel.deleteLater()


def test_panel_narrow_layout_and_dpi200(tmp_path: Path) -> None:
    app = _qapp()
    from htdt.room_survey_panel import RoomSurveyPanel

    fixture = _fixture(tmp_path)
    controller = RoomSurveyOverlayController(
        fixture['scene_repository'], DOC
    )
    panel = RoomSurveyPanel(controller)
    panel.show_scene(controller.resolve('verification'))
    panel.resize(260, 700)
    panel.show()
    assert panel.elements.width() <= panel.width() + 20
    font = app.font()
    font.setPointSizeF(font.pointSizeF() * 2.0)
    app.setFont(font)
    panel.resize(320, 900)
    panel.show_scene(controller.resolve('delta_mm'))
    assert panel.elements.topLevelItemCount() >= 1
    panel.close()
    panel.deleteLater()


def test_panel_mode_combo_drives_signal(tmp_path: Path) -> None:
    _qapp()
    from htdt.room_survey_panel import RoomSurveyPanel

    panel = RoomSurveyPanel()
    seen: list[str] = []
    panel.modeChanged.connect(seen.append)
    panel.mode_combo.setCurrentIndex(2)  # 'uncertainty_mm'
    assert seen == ['uncertainty_mm']
    panel.deleteLater()


# ---------------------------------------------------------------------------
# OverlayControls state
# ---------------------------------------------------------------------------


def test_overlay_controls_survey_state_and_modes() -> None:
    _qapp()
    from htdt.room_workspace import OverlayControls

    controls = OverlayControls()
    assert controls.survey_mode is None
    controls.set_survey_mode('delta_mm')
    assert controls.survey_mode == 'delta_mm'
    assert controls.state().survey_mode == 'delta_mm'
    controls.survey.setChecked(False)
    assert controls.state().survey_mode is None
    with pytest.raises(ValueError):
        controls.set_survey_mode('bogus')
    controls.deleteLater()
