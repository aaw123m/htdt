"""Round 18 (REV18-SWEEP): convergence-sweep regressions.

Three remaining defect families surfaced by the sweep, each a sibling of a
pattern earlier rounds closed at individual sites:

- ``10.0 ** (db / k)`` on finite-but-unbounded inputs raises
  ``OverflowError`` — which is NOT a ``ValueError`` subclass, so it escaped
  every caller's ``except ValueError`` contract. Data-processing
  summations now saturate the exponent at +-300 (the r16 smoothing
  convention — bit-identical on every previously-working input), while
  operator-declared gain authorities refuse honestly with ``ValueError``.
- ``_commit_batch_assignment`` and ``AutomaticBackupRunner.start`` were the
  last ``pool.start`` launch paths missing the ``_disposed``/``_closed``
  guard their siblings gained in r17 — ``NativeWorkerPool.start`` on a
  shut-down pool raises ``RuntimeError`` out of the slot.
- Three ``ORDER BY created_at_utc`` listings lacked a key tiebreak (the
  r15-time sweep closed the rest): rows sharing one timestamp ordered by
  SQLite rowid — insertion order — instead of a stable key.
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest

from htdt.cad_ambient_noise import (
    CadAmbientNoiseRepository,
    ambient_overall_level_db,
    build_ambient_noise_profile,
    build_ambient_operating_condition,
)
from htdt.cad_calibration import calculate_biquad_coefficients
from htdt.cad_correction_design_policy import aggregate_position_magnitudes
from htdt.cad_directivity import (
    DirectivitySample,
    evaluate_directivity,
)
from htdt.cad_equipment_self_noise import combine_noise_sources_energy
from htdt.cad_gain_structure import dbu_to_vrms, dbv_to_vrms
from htdt.cad_phase_time_analysis import compute_minimum_phase_deg
from htdt.cad_project_template import (
    ProjectTemplateInstantiation,
    create_project_from_template,
    theater_5_1_4_template,
)
from htdt.cad_project_template_repository import CadProjectTemplateRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.cad_standards_profiles import dolby_atmos_home_5_1_2_profile
from htdt.canonical_json import canonical_sha256
from htdt.capture_receiver import (
    CaptureReceiverService,
    ReceiverPairing,
)
from htdt.capture_inbox import CaptureInboxRepository
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.cad_schema import connect_sqlite
from htdt.project_lifecycle import ProjectLibrary

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.automatic_backup_runner import AutomaticBackupRunner
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.scientific_plot_style import PlotCursor

from test_cad_auralization import _spec as _auralization_spec
from test_cad_correction_design_policy import _policy, _sample, _target
from test_cad_directivity import _imported_dataset
from test_cad_multi_channel_excitation import (
    _complex_transfer,
    _participant,
    _scenario,
)
from test_cad_project_template import _StandardsSource
from htdt.cad_auralization import render_auralization
from htdt.cad_fir_filter import build_fir_filter_artifact, evaluate_fir_artifact
from htdt.cad_multi_channel_excitation import compose_coherent_system_response

OCTAVE_BANDS = (63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


# -- 10.0 ** exponent saturation on finite-but-extreme data --------------------


def test_extreme_levels_saturate_instead_of_overflowing() -> None:
    """A finite level above ~3080 dB used to raise OverflowError, which no
    ``except ValueError`` caller catches. Statistical/data paths now clamp
    the power exponent to the representable +-300 bound."""
    phases = compute_minimum_phase_deg(
        (0.0, 1000.0, 2000.0, 3000.0, 4000.0),
        (7000.0, -7000.0, 100.0, 40.0, 60.0),
    )
    assert len(phases) == 5
    assert all(math.isfinite(v) for v in phases)

    # 3500 dB saturates to a 3000 dB power level; the band evidence carries
    # the saturated residual instead of crashing the aggregation.
    policy = _policy(
        smoothing='fractional_octave',
        smoothing_fraction_octaves=1.0,
        aggregation='linear_power_mean',
    )
    evidence = aggregate_position_magnitudes(
        (
            _sample('seat-a', 3500.0, 3500.0),
            _sample('seat-b', 3500.0, 3500.0),
        ),
        _target(),
        policy,
    )
    assert evidence.bands[0].aggregated_error_db == pytest.approx(3000.0)

    assert combine_noise_sources_energy((3500.0, 60.0)) == pytest.approx(
        3000.0, abs=0.01
    )
    # Ordinary data is unchanged by the clamp.
    assert combine_noise_sources_energy((60.0, 60.0)) == pytest.approx(
        63.0103, abs=0.001
    )
    assert combine_noise_sources_energy(()) is None


def test_ambient_overall_level_saturates_on_extreme_bands() -> None:
    condition = build_ambient_operating_condition(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash='a' * 64,
        hvac_state='off',
        projector_state='off',
        created_at='2026-09-23T00:00:00+00:00',
    )
    profile = build_ambient_noise_profile(
        condition,
        microphone_position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
        method='measured',
        level_semantics='absolute_spl',
        calibration_authority_id='cal-1',
        weighting='Z',
        band_spec='octave',
        band_center_hz=OCTAVE_BANDS,
        band_level_db=(3500.0,) * 8,
        integration_duration_s=10.0,
        captured_at='2026-09-23T01:00:00+00:00',
    )
    level = ambient_overall_level_db(profile)
    assert level is not None and math.isfinite(level)
    assert level == pytest.approx(3009.0, abs=0.1)


def _extreme_dataset(dataset, *, phase: float | None):
    samples = tuple(
        DirectivitySample(
            frequency_hz=frequency,
            horizontal_angle_deg=horizontal,
            vertical_angle_deg=vertical,
            magnitude_db=7000.0,
            phase_deg=phase,
        )
        for frequency, horizontal, vertical in product(
            dataset.frequencies_hz,
            dataset.horizontal_angles_deg,
            dataset.vertical_angles_deg,
        )
    )
    # model_copy skips validators by design; the swapped grid is otherwise
    # identical so every digest field still describes the same balloon.
    return dataset.model_copy(update={'samples': samples})


def test_directivity_evaluation_saturates_on_extreme_magnitudes() -> None:
    """Imported balloons are finite-only validated, so a 7000 dB sample
    reached ``10.0 ** 350`` and crashed evaluation with OverflowError."""
    _, _, dataset = _imported_dataset()
    hot = _extreme_dataset(dataset, phase=None)

    exact = evaluate_directivity(
        hot,
        frequency_hz=dataset.frequencies_hz[0],
        horizontal_angle_deg=dataset.horizontal_angles_deg[0],
        vertical_angle_deg=dataset.vertical_angles_deg[0],
    )
    assert exact.decision == 'SUPPORTED'
    assert exact.magnitude_linear == pytest.approx(1e300)

    interpolated = evaluate_directivity(
        hot,
        frequency_hz=(
            dataset.frequencies_hz[0] + dataset.frequencies_hz[1]
        )
        / 2.0,
        horizontal_angle_deg=dataset.horizontal_angles_deg[0],
        vertical_angle_deg=dataset.vertical_angles_deg[0],
    )
    assert interpolated.decision == 'SUPPORTED'
    assert interpolated.magnitude_linear == pytest.approx(1e300)

    _, _, complex_dataset = _imported_dataset(
        kind='complex', source_magnitude_unit='linear'
    )
    hot_complex = _extreme_dataset(complex_dataset, phase=0.0)
    composed = evaluate_directivity(
        hot_complex,
        frequency_hz=(
            complex_dataset.frequencies_hz[0]
            + complex_dataset.frequencies_hz[1]
        )
        / 2.0,
        horizontal_angle_deg=complex_dataset.horizontal_angles_deg[0],
        vertical_angle_deg=complex_dataset.vertical_angles_deg[0],
    )
    assert composed.decision == 'SUPPORTED'
    assert math.isfinite(composed.magnitude_linear)


# -- operator-declared gains refuse honestly with ValueError -------------------


def test_spec_gains_reject_unrepresentable_exponents() -> None:
    """A declared gain outside the representable range must fail with the
    module's ValueError contract — a saturated gain would persist a
    wrong-but-valid authority."""
    with pytest.raises(ValueError, match='representable'):
        calculate_biquad_coefficients(
            filter_type='peaking',
            frequency_hz=1000.0,
            q=1.0,
            gain_db=20000.0,
            sample_rate_hz=48000,
        )
    # Sane gain unchanged.
    biquad = calculate_biquad_coefficients(
        filter_type='peaking',
        frequency_hz=1000.0,
        q=1.0,
        gain_db=6.0,
        sample_rate_hz=48000,
    )
    assert all(math.isfinite(v) for v in biquad)

    with pytest.raises(ValueError, match='representable'):
        dbv_to_vrms(7000.0)
    with pytest.raises(ValueError, match='representable'):
        dbu_to_vrms(7000.0)
    assert dbv_to_vrms(0.0) == pytest.approx(1.0)


def test_auralization_rms_target_rejects_unrepresentable_gain() -> None:
    spec, dry, impulse = _auralization_spec(
        gain_policy='level_matched_rms', rms_target_dbfs=8000.0
    )
    with pytest.raises(ValueError, match='representable'):
        render_auralization(
            spec,
            dry_samples=dry,
            impulse_samples=impulse,
            dry_asset_sha256=spec.dry_source.asset_sha256,
            impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
        )
    # A sane target still renders.
    spec_ok, dry_ok, impulse_ok = _auralization_spec(
        gain_policy='level_matched_rms', rms_target_dbfs=-20.0
    )
    pcm = render_auralization(
        spec_ok,
        dry_samples=dry_ok,
        impulse_samples=impulse_ok,
        dry_asset_sha256=spec_ok.dry_source.asset_sha256,
        impulse_artifact_sha256=spec_ok.impulse_authority.artifact_sha256,
    )
    assert len(pcm.samples) > 0


def test_fir_artifact_gain_rejects_unrepresentable_exponent() -> None:
    artifact = build_fir_filter_artifact(
        filter_class='arbitrary_fir',
        sample_rate_hz=48000.0,
        taps=(1.0, 0.5, 0.25),
        tap_format='float64',
        channel_id='FL',
        gain_db=8000.0,
        time_reference_sample=0,
        latency_s=0.0,
        source_producer='test',
        source_version='1',
    )
    with pytest.raises(ValueError, match='representable'):
        evaluate_fir_artifact(artifact, (1000.0,))


def test_excitation_drive_gain_rejects_unrepresentable_exponent() -> None:
    scenario = _scenario(
        participants=(
            _participant('speaker-fl', 'FL', gain_db=8000.0),
        )
    )
    with pytest.raises(ValueError, match='representable'):
        compose_coherent_system_response(
            scenario,
            (_complex_transfer('speaker-fl', (1.0 + 0j,)),),
        )


def test_plot_cursor_log_readout_saturates_on_extreme_coordinate() -> None:
    """A reference line parked at an extreme log-mode coordinate used to
    raise OverflowError inside the hover readout."""
    view_box = SimpleNamespace(state={'logMode': [True]})
    plot_item = SimpleNamespace(getViewBox=lambda: view_box)
    cursor = SimpleNamespace(
        line=SimpleNamespace(value=lambda: 400.0),
        plot=SimpleNamespace(getPlotItem=lambda: plot_item),
    )
    assert PlotCursor._display_value(cursor) == pytest.approx(1e300)
    cursor.line = SimpleNamespace(value=lambda: -400.0)
    assert PlotCursor._display_value(cursor) == pytest.approx(1e-300)
    cursor.line = SimpleNamespace(value=lambda: 3.0)
    assert PlotCursor._display_value(cursor) == pytest.approx(1000.0)


# -- post-dispose launch guards (r17 sibling class) ---------------------------


def test_batch_commit_after_dispose_is_swallowed() -> None:
    """``_commit_batch_assignment`` calls ``_job_pool.start`` directly —
    without the ``_disposed`` guard every sibling launch path honours, a
    late signal raises RuntimeError out of the slot."""
    stub = SimpleNamespace(_disposed=True)
    MeasurementPageWorkspace._commit_batch_assignment(
        stub, 'batch_all', None, None
    )


def test_backup_runner_start_after_shutdown_returns_false(tmp_path: Path) -> None:
    """``start()`` checked ``_attempted`` but not ``_closed`` — a post-
    ``shutdown()`` start reached ``self._pool.start`` -> RuntimeError."""
    _app()
    runner = AutomaticBackupRunner(tmp_path)
    runner.shutdown()
    assert runner.start() is False


# -- ORDER BY tiebreaks (r15 sibling class) ------------------------------------


def test_ambient_profile_listing_is_deterministic_on_timestamp_ties(
    tmp_path: Path,
) -> None:
    """``list_profiles`` ordered by created_at_utc alone: same-second rows
    ordered by SQLite rowid. The primary key is now the tiebreak."""
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    repository = CadAmbientNoiseRepository(scene_repository)
    condition = build_ambient_operating_condition(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        hvac_state='off',
        projector_state='off',
        created_at='2026-09-23T00:00:00+00:00',
    )
    repository.save_condition(condition)
    profile = build_ambient_noise_profile(
        condition,
        microphone_position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
        method='measured',
        level_semantics='absolute_spl',
        calibration_authority_id='cal-1',
        weighting='Z',
        band_spec='octave',
        band_center_hz=OCTAVE_BANDS,
        band_level_db=(30.0,) * 8,
        integration_duration_s=10.0,
        captured_at='2026-09-23T01:00:00+00:00',
    )
    # profile_id is not part of profile_sha256's identity payload, so a
    # copied id still validates — this stands in for two profiles persisted
    # inside one timestamp resolution.
    high = profile.model_copy(update={'profile_id': 'zzz-tie'})
    low = profile.model_copy(update={'profile_id': 'aaa-tie'})
    repository.save_profile(high)
    repository.save_profile(low)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            "UPDATE cad_ambient_profiles "
            "SET created_at_utc='2026-01-01T00:00:00+00:00'"
        )
    assert tuple(
        p.profile_id for p in repository.list_profiles(revision.document_id)
    ) == ('aaa-tie', 'zzz-tie')


def test_pairing_listing_is_deterministic_on_timestamp_ties(
    tmp_path: Path,
) -> None:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    service = CaptureReceiverService(
        scene, inbox, ingestion, data_dir=tmp_path / 'receiver'
    )
    stamp = '2026-01-01T00:00:00+00:00'
    # Insert the higher id first so insertion order disagrees with key order.
    with closing(service._connect()) as connection, connection:
        for pairing_id in ('zzz-tie', 'aaa-tie'):
            connection.execute(
                'INSERT INTO capture_receiver_pairings('
                'pairing_id, pairing_token, receiver_instance_id, '
                'project_ref, endpoint_url, capability_endpoint_url, '
                'missions_endpoint_url, pinned_identity, confirmation_code, '
                'state, created_at_utc, confirmed_at_utc, expires_at_utc, '
                'capture_instance_id'
                ") VALUES (?, ?, 'rx-1', NULL, 'https://rx/deliveries', "
                "NULL, NULL, 'pin', '000000', 'offered', ?, NULL, NULL, NULL)",
                (pairing_id, f'{pairing_id}-token', stamp),
            )
    assert tuple(p.pairing_id for p in service.list_pairings()) == (
        'aaa-tie',
        'zzz-tie',
    )


def test_instantiation_lookup_is_deterministic_on_timestamp_ties(
    tmp_path: Path,
) -> None:
    """``instantiation_for_document`` picked LIMIT 1 on created_at_utc alone;
    a same-instant duplicate (e.g. a retried write) resolved to whichever
    rowid came first. The instantiation id is now the DESC tiebreak."""
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    library = ProjectLibrary(tmp_path / 'cad.sqlite3')
    repository = CadProjectTemplateRepository(scene_repository)
    document_id, instantiation = create_project_from_template(
        scene_repository,
        theater_5_1_4_template(),
        library=library,
        display_name='Tie',
        created_at_utc='2026-01-01T00:00:00+00:00',
        instantiation_repository=repository,
        standards_repository=_StandardsSource(
            (dolby_atmos_home_5_1_2_profile(),)
        ),
    )
    payload = dict(
        instantiation_id='zzzzzzzz-0000-4000-8000-000000000000',
        document_id=instantiation.document_id,
        project_id=instantiation.project_id,
        template_id=instantiation.template_id,
        template_version=instantiation.template_version,
        template_sha256=instantiation.template_sha256,
        unresolved_refs=instantiation.unresolved_refs,
        pending_measurement_spec=instantiation.pending_measurement_spec,
        created_at_utc=instantiation.created_at_utc,
    )
    provisional = ProjectTemplateInstantiation.model_construct(
        **payload, instantiation_sha256='0' * 64
    )
    twin = ProjectTemplateInstantiation(
        **payload,
        instantiation_sha256=canonical_sha256(
            provisional.semantic_payload()
        ),
    )
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'INSERT INTO template_instantiations ('
            'instantiation_id, document_id, template_id, template_sha256, '
            'created_at_utc, instantiation_sha256, payload_json'
            ') VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                twin.instantiation_id,
                twin.document_id,
                twin.template_id,
                twin.template_sha256,
                twin.created_at_utc,
                twin.instantiation_sha256,
                twin.model_dump_json(),
            ),
        )
    resolved = repository.instantiation_for_document(document_id)
    assert resolved is not None
    assert resolved.instantiation_id == twin.instantiation_id
