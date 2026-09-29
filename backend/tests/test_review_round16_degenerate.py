"""Round-16 regression tests: degenerate real-data shapes.

Verified issues and fixes:
- ``smoothed_level_trace`` crashed (``log10(0)``) when every sample in a
  smoothing window had level <= ~-3080 dB: float64 powers underflow to 0,
  collapsing the window mean. Exponents are now clamped to [-300, 300] so
  the power mean saturates at a finite +-3000 dB floor/ceiling instead.
- ``phase_trace(unwrap=True)`` ran a while-loop once per 360 degrees of
  delta — a stored phase of e.g. 1e12 deg took ~3e9 iterations (UI hang).
  The fold is now one ``math.remainder`` call.
- ``_volume_axes`` divided by the grid stride before validating its sign:
  ``stride_m=0`` raised ``ZeroDivisionError`` (escaping the caller's
  ``except ValueError``) and a negative stride surfaced as a cryptic
  ``RegularGridAxis`` ValidationError. Both now raise ``ValueError``.
- ``SceneEntity.name`` accepted whitespace-only strings, which render as
  blank rows in pickers and labels; it now rejects blank names like
  ``ProjectCreate`` does.

Also asserted as already-honest behavior: unicode/long entity names are
accepted verbatim, smoothing of ordinary data is unchanged.
"""

from __future__ import annotations

import math
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_field_explorer import build_mode_field_explorer_session
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    make_polygon_room,
)
from htdt.measurement_analysis import phase_trace, smoothed_level_trace


def _dataset(
    levels: tuple[float, ...],
    phases: tuple[float, ...] | None = None,
) -> CadFrequencyResponseDataset:
    count = len(levels)
    return CadFrequencyResponseDataset(
        dataset_id='dataset-degenerate',
        measurement_id='measurement-degenerate',
        frequency_hz=tuple(20.0 + 10.0 * index for index in range(count)),
        level_db=levels,
        phase_deg=None if phases is None else phases,
        phase_status='absent' if phases is None else 'valid',
        level_reference='unknown',
        processing_json='{}',
        source_sha256=sha256(b'round16').hexdigest(),
        importer_version='round16-probe',
    )


# --- smoothed_level_trace: EXTREME level shapes -----------------------------


def test_smoothed_trace_deep_null_window_stays_finite() -> None:
    """All samples far below float64 power range -> finite floor, no crash."""
    trace = smoothed_level_trace(_dataset((-4000.0,) * 8), 3)
    assert len(trace.level_db) == 8
    assert all(math.isfinite(value) for value in trace.level_db)
    assert all(value == pytest.approx(-3000.0) for value in trace.level_db)


def test_smoothed_trace_edge_deep_null_in_mixed_window() -> None:
    """A single deep-null point must not poison its smoothing window."""
    levels = (-4000.0, 80.0, 80.0, -4000.0, -4000.0, 80.0, -4000.0, 80.0)
    trace = smoothed_level_trace(_dataset(levels), 3)
    assert all(math.isfinite(value) for value in trace.level_db)
    # Points whose window holds only the 80 dB cluster still land near 80.
    assert trace.level_db[1] == pytest.approx(80.0, abs=1.0)


def test_smoothed_trace_extreme_positive_level_stays_finite() -> None:
    trace = smoothed_level_trace(_dataset((4000.0,) * 8), 3)
    assert all(math.isfinite(value) for value in trace.level_db)
    assert all(value == pytest.approx(3000.0) for value in trace.level_db)


def test_smoothed_trace_ordinary_levels_unchanged() -> None:
    levels = (70.0, 80.0, 90.0, 80.0, 70.0, 80.0, 90.0, 80.0)
    trace = smoothed_level_trace(_dataset(levels), 3)
    assert all(math.isfinite(value) for value in trace.level_db)
    flat = smoothed_level_trace(_dataset((80.0,) * 8), 3)
    assert flat.level_db == (80.0,) * 8


# --- phase_trace: EXTREME phase magnitudes -----------------------------------


def test_phase_unwrap_huge_phase_is_constant_time() -> None:
    """1e12 deg used to take ~3e9 loop iterations — now a single fold."""
    phases = (0.0, 1e12, 5e7, -3e8)
    trace = phase_trace(_dataset((0.0,) * 4, phases), unwrap=True)
    assert trace is not None
    assert len(trace.phase_deg) == 4
    for previous, current in zip(trace.phase_deg, trace.phase_deg[1:]):
        assert -180.0 < current - previous <= 180.0


@pytest.mark.parametrize(
    'phases',
    [
        (0.0, -180.0, -540.0),
        (0.0, 180.0, 540.7),
        (0.0, 180.0000001, -180.5),
        (0.0, 900.0, -900.0, 1260.0),
        (0.0, -360.0, 360.0),
        (45.0, 45.0, 45.0),
    ],
)
def test_phase_unwrap_boundary_fold_semantics(phases: tuple[float, ...]) -> None:
    """Deltas fold into (-180, 180]; the -180 tie lands on +180."""
    trace = phase_trace(
        _dataset((0.0,) * len(phases), phases), unwrap=True
    )
    assert trace is not None
    assert trace.phase_deg[0] == phases[0]
    for previous, current in zip(trace.phase_deg, trace.phase_deg[1:]):
        assert -180.0 < current - previous <= 180.0
    # Exactly-180-degree ties must land on the +180 side.
    tied = phase_trace(
        _dataset((0.0, 0.0), (0.0, -180.0)), unwrap=True
    )
    assert tied is not None
    assert tied.phase_deg[-1] == pytest.approx(180.0)


def test_phase_unwrap_sane_magnitudes_exact() -> None:
    """Ordinary wrapped phase unwraps exactly as before."""
    phases = (0.0, 170.0, -170.0, -160.0, 150.0)
    trace = phase_trace(_dataset((0.0,) * 5, phases), unwrap=True)
    assert trace is not None
    assert trace.phase_deg == pytest.approx(
        (0.0, 170.0, 190.0, 200.0, 150.0)
    )


# --- _volume_axes: BOUNDARY grid stride -------------------------------------


def _session_fixture(tmp_path: Path):
    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=1.0, y_m=2.0),
            RoomVertex(vertex_id='b', x_m=5.0, y_m=2.0),
            RoomVertex(vertex_id='c', x_m=5.0, y_m=5.0),
            RoomVertex(vertex_id='d', x_m=1.0, y_m=5.0),
        ),
        height_m=2.5,
    )
    document = SceneDocument(
        document_id='degenerate-stride',
        schema_version=2,
        room=room,
        entities=(
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=4.0, z_m=1.1),
            ),
        ),
    )
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(document, parent_revision_id=None).revision
    modes, _ = analyze_native_rectangular_geometry(
        revision, 'point-mlp', max_mode_hz=150.0
    )
    return revision, modes


@pytest.mark.parametrize('stride', [0.0, -0.5])
def test_field_explorer_nonpositive_stride_is_value_error(
    tmp_path: Path, stride: float
) -> None:
    """Zero stride must not escape as ZeroDivisionError; the UI catches
    ValueError and needs one honest rejection message for both cases."""
    revision, modes = _session_fixture(tmp_path)
    mode = modes.modes[0]
    with pytest.raises(ValueError, match='grid stride must be positive'):
        build_mode_field_explorer_session(
            revision=revision,
            modes_result=modes,
            mode_indices=(mode.n_x, mode.n_y, mode.n_z),
            stride_m=stride,
        )


# --- SceneEntity: NAMES -------------------------------------------------------


def _point(name: str) -> SceneEntity:
    return SceneEntity(
        entity_id='point-1',
        kind='measurement_point',
        name=name,
        position=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
    )


@pytest.mark.parametrize('name', ['   ', '\t\n', ' \u3000 '])
def test_scene_entity_rejects_blank_name(name: str) -> None:
    with pytest.raises(ValueError, match='name must not be blank'):
        _point(name)


def test_scene_entity_accepts_unicode_and_long_names() -> None:
    emoji = _point('Seat \U0001f3a7 row A')
    assert emoji.name == 'Seat \U0001f3a7 row A'
    long_name = _point('x' * 500)
    assert long_name.name == 'x' * 500
    spaced = _point('  seat  ')
    assert spaced.name == '  seat  '
