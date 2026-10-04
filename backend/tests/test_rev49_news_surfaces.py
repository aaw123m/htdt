"""REV49: deep-review regression tests for recently merged surfaces.

Each test pins a defect found in the REV49 re-review — they fail against
the pre-fix implementation.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_acoustic_geometry_derivation import _scene  # noqa: E402
from test_cad_auralization_review import _comparison_row, _hash  # noqa: E402
from test_cad_video_commissioning import (  # noqa: E402
    _improved_samples,
    _measurement,
    _sample,
    _session,
    _target,
)

from htdt.acceptance_checks import (  # noqa: E402
    CheckContext,
    check_persistence_probe,
)
from htdt.cad_acceptance_repository import AcceptanceRunRepository  # noqa: E402
from htdt.cad_active_lf_control import build_control_plan  # noqa: E402
from htdt.cad_active_lf_design import (  # noqa: E402
    DspResourceEnvelope,
    evaluate_dsp_feasibility,
    lf_control_evaluation_spec,
    lf_control_objectives,
)
from htdt.cad_auralization_review import (  # noqa: E402
    build_review_package,
    verify_review_package,
)
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_video_commissioning import (  # noqa: E402
    compare_video_measurements,
    diagnose_video_measurement,
    evaluate_video_readiness,
    propose_video_actions,
)
from htdt.cad_video_commissioning_repository import (  # noqa: E402
    CadVideoCommissioningRepository,
    VideoCommissioningIntegrityError,
)
from htdt.cad_video_measure_import import import_video_measurements  # noqa: E402
from htdt.canonical_json import canonical_sha256  # noqa: E402
from htdt.capture_authoring import CaptureAuthoringAnnotation  # noqa: E402
from htdt.capture_entity_promotion import (  # noqa: E402
    CaptureEntityPromotionService,
    _matmul4,
    _uniform_scale,
)
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionRepository,
)
from htdt.comparison import FrequencyResponse  # noqa: E402
from htdt.cad_acoustic_geometry_derivation import (  # noqa: E402
    build_acoustic_geometry_derivation,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef  # noqa: E402


# ---------------------------------------------------------------------------
# 受入検証: persistence probe must bind to an actual restart
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, repo, *, boot_id=None) -> CheckContext:
    return CheckContext(
        data_dir=tmp_path,
        db_path=tmp_path / 'cad-scenes.sqlite3',
        rew_base_url='http://127.0.0.1:4735',
        run_id='ac-test',
        step_id='step-1',
        repository=repo,
        boot_id=boot_id,
    )


def test_persistence_probe_requires_a_real_restart(tmp_path: Path) -> None:
    db = tmp_path / 'cad-scenes.sqlite3'
    repo = AcceptanceRunRepository(db)
    ctx = _ctx(tmp_path, repo)
    assert check_persistence_probe(ctx, '').verdict == 'deferred'
    # Re-running in the SAME process must not satisfy the restart claim —
    # the marker was written by this very boot.
    same_boot = _ctx(
        tmp_path, AcceptanceRunRepository(db)
    )
    result = check_persistence_probe(same_boot, '')
    assert result.verdict == 'deferred'
    # A different boot id — i.e. the post-restart process — verifies.
    restarted = _ctx(
        tmp_path, AcceptanceRunRepository(db), boot_id='other-boot'
    )
    result = check_persistence_probe(restarted, '')
    assert result.verdict == 'pass'
    assert result.evidence['verified_after_restart'] is True


def test_persistence_probe_rejects_bootless_marker(tmp_path: Path) -> None:
    db = tmp_path / 'cad-scenes.sqlite3'
    repo = AcceptanceRunRepository(db)
    ctx = _ctx(tmp_path, repo)
    # Simulate a marker written before boot binding existed (no boot_id)
    # — it cannot prove a restart, so it is re-issued, never trusted.
    repo.attach_evidence(
        'ac-test',
        'step-1',
        kind='persistence_probe',
        filename='persistence-probe.json',
        payload=json.dumps(
            {'run_id': 'ac-test', 'step_id': 'step-1'}
        ).encode('utf-8'),
    )
    restarted = _ctx(
        tmp_path, AcceptanceRunRepository(db), boot_id='other-boot'
    )
    assert check_persistence_probe(restarted, '').verdict == 'deferred'


# ---------------------------------------------------------------------------
# 映像調整: comparison honesty — achromatic ramps only name white/black,
# missing comparability axes now flagged
# ---------------------------------------------------------------------------


def test_comparison_luminance_extrema_use_achromatic_only() -> None:
    session, target = _session(), _target()
    before = _measurement()
    after = _measurement(set_id='vms-after', samples=_improved_samples())
    comparison = compare_video_measurements(
        session=session,
        before_set=before,
        after_set=after,
        target=target,
        iteration_index=1,
        compared_at_utc='2026-10-01T06:00:00+00:00',
    )
    assert comparison.status == 'comparable'
    rows = {row.metric: row for row in comparison.rows}
    # The blue primary (6.0) must never be reported as the black floor —
    # the achromatic ramp's lowest point (gray_20 = 2.4) is.
    assert rows['black_floor_cd_m2'].before == pytest.approx(2.4)
    # Peak white is w100 (120), not the green primary (80) when
    # achromatic anchors exist.
    assert rows['peak_white_cd_m2'].before == pytest.approx(120.0)


def test_comparison_primaries_only_set_reports_unknown_luminance() -> None:
    session, target = _session(), _target()
    primaries = (
        _sample('r', 0.640, 0.330, 25.0),
        _sample('g', 0.300, 0.600, 80.0),
        _sample('b', 0.150, 0.060, 6.0),
    )
    before = _measurement(set_id='vms-p1', samples=primaries)
    after = _measurement(
        set_id='vms-p2',
        samples=(
            _sample('r', 0.640, 0.330, 26.0),
            _sample('g', 0.300, 0.600, 82.0),
            _sample('b', 0.150, 0.060, 6.5),
        ),
    )
    comparison = compare_video_measurements(
        session=session,
        before_set=before,
        after_set=after,
        target=target,
        iteration_index=1,
        compared_at_utc='2026-10-01T06:00:00+00:00',
    )
    rows = {row.metric: row for row in comparison.rows}
    # No achromatic ramp at all → the luminance extrema are UNKNOWN,
    # never the brightest/dimmest primary.
    assert rows['peak_white_cd_m2'].direction == 'unknown'
    assert rows['peak_white_cd_m2'].before is None
    assert rows['black_floor_cd_m2'].direction == 'unknown'


def test_comparison_flags_stimulus_differences_as_incomparable() -> None:
    from htdt.cad_colorimetry import StimulusDefinition

    session, target = _session(), _target()
    before = _measurement()
    after = _measurement(
        set_id='vms-after',
        samples=_improved_samples(),
        stimulus=StimulusDefinition(
            encoding='rgb_limited',
            bit_depth=10,
            patch_size_percent=25.0,  # was 10 — a different patch
            pattern_generator='murideo-g7',
        ),
    )
    comparison = compare_video_measurements(
        session=session,
        before_set=before,
        after_set=after,
        target=target,
        iteration_index=1,
        compared_at_utc='2026-10-01T06:00:00+00:00',
    )
    assert comparison.status == 'incomparable'
    assert any('パッチサイズ' in r for r in comparison.incompatibility_reasons)


def test_comparison_peak_white_change_is_not_a_regression() -> None:
    session, target = _session(), _target()
    before = _measurement()
    brighter = (
        _sample('w100', 0.3127, 0.3290, 150.0, stimulus_level=1.0),
        *_improved_samples()[1:],
    )
    after = _measurement(set_id='vms-after', samples=brighter)
    comparison = compare_video_measurements(
        session=session,
        before_set=before,
        after_set=after,
        target=target,
        iteration_index=1,
        compared_at_utc='2026-10-01T06:00:00+00:00',
    )
    rows = {row.metric: row for row in comparison.rows}
    # Raw luminance capability has no intrinsic better/worse — a change
    # is a measured change, never 'regressed'.
    assert rows['peak_white_cd_m2'].direction == 'inconclusive'
    assert rows['peak_white_cd_m2'].delta == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# 映像調整: repository — insertion-order "latest", proposal FK integrity
# ---------------------------------------------------------------------------


def _video_repository(tmp_path: Path) -> CadVideoCommissioningRepository:
    return CadVideoCommissioningRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )


def test_latest_readiness_report_uses_insertion_order(tmp_path: Path) -> None:
    repository = _video_repository(tmp_path)
    session, target = _session(), _target()
    repository.save_session(session)
    first = evaluate_video_readiness(
        session,
        target=target,
        correction_compatibility=None,
        measurement_sets=(),
        evaluated_at_utc='2026-10-01T02:00:00+00:00',
    )
    second = evaluate_video_readiness(
        session,
        target=target,
        correction_compatibility=None,
        measurement_sets=(),
        evaluated_at_utc='2026-10-01T03:00:00+00:00',
    )
    repository.save_readiness_report(first, 'doc-1')
    repository.save_readiness_report(second, 'doc-1')
    # Rewrite the second row's timestamp older than the first's — text
    # ordering on created_at_utc would now return the WRONG report; the
    # authoritative order is insertion (rowid DESC).
    import sqlite3

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'UPDATE cad_video_readiness_reports SET created_at_utc=? '
            'WHERE report_id=?',
            ('1999-01-01T00:00:00+00:00', second.report_id),
        )
        connection.commit()
    latest = repository.latest_readiness_report('doc-1', session.session_id)
    assert latest is not None
    assert latest.report_id == second.report_id


def test_save_proposal_requires_persisted_diagnosis(tmp_path: Path) -> None:
    repository = _video_repository(tmp_path)
    session, target = _session(), _target()
    ms = _measurement()
    repository.save_session(session)
    diagnosis = diagnose_video_measurement(
        session=session,
        measurement_set=ms,
        target=target,
        diagnosed_at_utc='2026-10-01T03:00:00+00:00',
    )
    proposal = propose_video_actions(
        diagnosis,
        session=session,
        measurement_set=ms,
        target=target,
        proposed_at_utc='2026-10-01T04:00:00+00:00',
    )
    # The diagnosis row was never persisted — the proposal must not be
    # allowed to reference air.
    with pytest.raises(VideoCommissioningIntegrityError):
        repository.save_proposal(proposal, 'doc-1')
    repository.save_diagnosis(diagnosis, 'doc-1')
    repository.save_proposal(proposal, 'doc-1')


# ---------------------------------------------------------------------------
# 映像調整: import honesty — assembly failures are malformed, not crashes;
# partial level rows surface a warning
# ---------------------------------------------------------------------------


def test_import_htdt_json_bad_meter_correction_is_malformed() -> None:
    payload = {
        'format': 'htdt-video-measurements-1',
        'meter': 'klein-k10',
        'meter_correction': {'correction_id': ''},  # fails validation
        'samples': [
            {
                'stimulus_id': 'w100',
                'stimulus_level': 1.0,
                'x': 34.0,
                'y_luminance': 120.0,
                'z': 35.0,
            }
        ],
    }
    result = import_video_measurements(
        file_name='set.json',
        data=json.dumps(payload).encode('utf-8'),
        surface_entity_id='screen-main',
    )
    assert result.status == 'malformed'
    assert result.failure is not None


def test_import_hcfr_truncated_level_row_warns() -> None:
    # Level row has fewer columns than the Measure header — levels are
    # then inferred by ordinal and the user must be told.
    rows = [
        'Measure;0;1;2;3',
        'IRE;0;50',
        'X;0.0;25.0;60.0;95.0',
        'Y;0.0;25.0;60.0;95.0',
        'Z;0.0;25.0;60.0;95.0',
    ]
    result = import_video_measurements(
        file_name='GrayScaleSheet.csv',
        data='\r\n'.join(rows).encode('utf-8'),
        surface_entity_id='screen-main',
        meter='klein-k10',
    )
    assert result.status == 'imported'
    assert any('レベル' in w and '推定' in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Auralization: verify_review_package cross-checks entry pins to payloads
# ---------------------------------------------------------------------------


def _repack_with_manifest(data: bytes, mutate) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(
        buffer, 'w', compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            raw = source.read(name)
            if name == 'manifest.json':
                raw = mutate(raw)
            target.writestr(name, raw)
    return buffer.getvalue()


def test_verify_review_package_rejects_unlinked_capability() -> None:
    _package, data = build_review_package(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        comparisons=(_comparison_row('a', label='A', seed=41),),
        created_at_utc='2026-10-04T00:00:00+00:00',
    )

    def drop_capabilities(raw: bytes) -> bytes:
        manifest = json.loads(raw.decode('utf-8'))
        manifest['capability_payloads'] = []
        return json.dumps(manifest).encode('utf-8')

    repacked = _repack_with_manifest(data, drop_capabilities)
    with pytest.raises(ValueError, match='capability'):
        verify_review_package(repacked)


def test_verify_review_package_rejects_repointed_entry() -> None:
    _package, data = build_review_package(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        comparisons=(_comparison_row('a', label='A', seed=41),),
        created_at_utc='2026-10-04T00:00:00+00:00',
    )

    def repoint(raw: bytes) -> bytes:
        manifest = json.loads(raw.decode('utf-8'))
        # Claim the comparison pins a capability that is not the embedded
        # one — member bytes still hash correctly, only linkage is wrong.
        manifest['comparisons'][0]['capability_semantic_sha256'] = 'f' * 64
        return json.dumps(manifest).encode('utf-8')

    repacked = _repack_with_manifest(data, repoint)
    with pytest.raises(ValueError, match='capability digest mismatch'):
        verify_review_package(repacked)


# ---------------------------------------------------------------------------
# Active LF: silent latency demand is UNKNOWN, not PASS; eval spec pins the
# resolved seat identities
# ---------------------------------------------------------------------------


def _plan(**overrides):
    kwargs = dict(
        plan_id='alfc-rev49',
        schema_version='alfc_v1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash='e' * 64,
        logical_input_groups=('LFE',),
        physical_output_groups=('SUB1',),
        control_band_hz=(20.0, 150.0),
        representation='transfer_matrix',
        matrix=(
            dict(
                input_group='LFE',
                output_group='SUB1',
                gain_db=-4.0,
                delay_s=0.005,
                filter_kind='fir',
                filter_ref='fir-1',
                valid_band_hz=(20.0, 120.0),
                latency_s=0.012,
            ),
        ),
        objectives=('decay_control',),
        total_latency_s=0.018,
        lifecycle='proposed',
        design_producer='htdt',
        design_version='1',
        created_at_utc='2026-10-01T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_control_plan(**kwargs)


def test_latency_budget_unknown_when_plan_declares_none() -> None:
    plan = _plan(total_latency_s=None)
    envelope = DspResourceEnvelope(
        envelope_id='env-1',
        latency_budget_s=0.020,
    )
    report = evaluate_dsp_feasibility(plan, envelope)
    checks = {c.check: c for c in report.checks}
    # The envelope limits a value the plan never declares — certifying
    # PASS here would fabricate a verification.
    assert checks['latency_budget'].status == 'UNKNOWN'
    assert report.verdict != 'compatible'


def test_lf_evaluation_spec_pins_resolved_seat_ids() -> None:
    plan = _plan()
    responses = (
        FrequencyResponse((40.0, 63.0, 80.0), (-3.0, -2.0, -1.0)),
        FrequencyResponse((40.0, 63.0, 80.0), (-4.0, -3.0, -2.0)),
    )
    vector = lf_control_objectives(plan, 'cand-1', responses)
    # No seat ids supplied → metrics fall back to seat-N — the bound spec
    # must pin exactly those identities, not an empty list.
    expected = canonical_sha256(
        lf_control_evaluation_spec(plan, ['seat-0', 'seat-1'])
    )
    assert vector.metrics
    assert all(
        metric.definition.comparison_model_version == expected
        for metric in vector.metrics
    )


# ---------------------------------------------------------------------------
# Solver authority: a bound authority ref cannot be omitted silently
# ---------------------------------------------------------------------------


def test_derivation_rejects_omitted_bound_portal_authority(
    tmp_path: Path,
) -> None:
    fx = _scene(tmp_path)
    ref = ExactExternalAuthorityRef(
        authority_id='portal-authority-1',
        authority_version='1',
        semantic_hash_sha256='f' * 64,
    )
    compiled = fx['compiled'].model_copy(
        update={'portal_authority_ref': ref}
    )
    with pytest.raises(ValueError, match='must.*be supplied'):
        build_acoustic_geometry_derivation(
            semantic_geometry=fx['geometry'],
            compiled_geometry=compiled,
        )


# ---------------------------------------------------------------------------
# Promotion: per-space world→scene authority + composed scale
# ---------------------------------------------------------------------------


def _scale4(s: float) -> tuple[tuple[float, ...], ...]:
    return (
        (s, 0.0, 0.0, 0.0),
        (0.0, s, 0.0, 0.0),
        (0.0, 0.0, s, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _annotation(document: dict, space: str = 'space-a'):
    return CaptureAuthoringAnnotation(
        record_id='rec-1',
        entity_type='speaker',
        label='sub',
        coordinate_space_id=space,
        authority_record_handoff_id='h-1',
        source_payload_sha256='a' * 64,
        document=document,
        evidence_refs=(),
        endpoint_refs=(),
        resolved_evidence=(),
        resolved_endpoints=(),
        equipment_ref=None,
        suggestion_only=False,
        conflicts=(),
    )


class _StubPromotionRepository:
    def __init__(self, rows):
        self._rows = rows  # (record, request) pairs

    def list_promotions(self):
        return [record for record, _req in self._rows]

    def promotion_request(self, promotion_id):
        for record, request in self._rows:
            if record.promotion_id == promotion_id:
                return request
        return None


def test_world_to_scene_authority_is_per_coordinate_space(
    tmp_path: Path,
) -> None:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    rows = [
        (
            SimpleNamespace(promotion_id='promo-a'),
            SimpleNamespace(
                target_document_id='doc-1',
                world_to_scene_authority=SimpleNamespace(
                    coordinate_space_id='space-a',
                    transform=SimpleNamespace(
                        matrix_source_to_scene_m=_scale4(2.0)
                    ),
                ),
            ),
        ),
    ]
    service = CaptureEntityPromotionService(
        ingestion,
        scene,
        semantic_promotion_repository=_StubPromotionRepository(rows),
    )
    plan = SimpleNamespace(
        bundle=SimpleNamespace(
            coordinate_space_ids=('space-a', 'space-b')
        )
    )
    by_space = service._world_to_scene_by_space(plan, 'doc-1')
    assert by_space == {'space-a': _scale4(2.0)}


def test_materialize_applies_composed_scene_scale(tmp_path: Path) -> None:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    service = CaptureEntityPromotionService(ingestion, scene)
    # column-major 4x4: uniform scale 3 in the annotation's own transform.
    values = [
        3.0, 0.0, 0.0, 0.0,
        0.0, 3.0, 0.0, 0.0,
        0.0, 0.0, 3.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    annotation = _annotation(
        {
            'T_world_from_annotation': {'values': values},
            'physical_envelope': {
                'width_m': 1.0,
                'height_m': 0.5,
                'depth_m': 0.2,
            },
            'channel_role': 'center',
        }
    )
    entity, reason = service._materialize(
        annotation,
        entity_id='e-1',
        world_to_scene=_scale4(2.0),
        candidates={},
        existing_entities=(),
    )
    assert entity is not None, reason
    # Composed scale: 3 (annotation→world) × 2 (world→scene) = 6.
    assert entity.size_m is not None
    assert entity.size_m.x_m == pytest.approx(6.0)
    assert _uniform_scale(_matmul4(_scale4(2.0), _scale4(3.0))) == (
        pytest.approx(6.0)
    )
