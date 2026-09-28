"""Measured modal identification authority (#972) — round 2 coverage.

This module owns a sealed evidence authority: specs and models are
hash-pinned, measured modes are never silently equal to theoretical room
modes, and common-pole/reconstruction helpers are deterministic. The tests
below pin the fail-closed validators and the association/clustering math.
"""

from __future__ import annotations

import pytest
from math import isfinite

from htdt.cad_measured_modal_analysis import (
    MeasuredMode,
    MeasuredModeResidue,
    ReconstructedModeShape,
    aggregate_common_poles,
    associate_predicted_mode,
    build_measured_modal_model,
    build_modal_analysis_spec,
    reconstruct_mode_shape,
)
from htdt.cad_scene import Position3


def _residue(position_id: str, amplitude: float, phase: float | None = None) -> MeasuredModeResidue:
    return MeasuredModeResidue(
        position_id=position_id, amplitude=amplitude, phase_rad=phase
    )


def _mode(
    mode_id: str,
    freq: float,
    *,
    residues: tuple[MeasuredModeResidue, ...] = (),
    **kwargs,
) -> MeasuredMode:
    return MeasuredMode(
        mode_id=mode_id,
        center_frequency_hz=freq,
        residues=residues,
        **kwargs,
    )


def _spec_kwargs(**overrides):
    payload = dict(
        spec_id='spec-1',
        schema_version='1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash='a' * 64,
        measurement_ids=('m-1', 'm-2'),
        ir_capability='measured_ir',
        algorithm='matrix_pencil',
        algorithm_version='1.0',
        created_at_utc='2026-09-26T00:00:00Z',
    )
    payload.update(overrides)
    return payload


# --- MeasuredModeResidue -----------------------------------------------------

def test_residue_rejects_negative_and_nonfinite_amplitude() -> None:
    with pytest.raises(ValueError):
        _residue('p1', -0.5)
    # NaN fails the ge=0 bound before the finite check; inf reaches it.
    with pytest.raises(ValueError):
        _residue('p1', float('nan'))
    with pytest.raises(ValueError, match='finite'):
        _residue('p1', float('inf'))


def test_residue_rejects_nonfinite_phase() -> None:
    with pytest.raises(ValueError, match='phase must be finite'):
        _residue('p1', 1.0, phase=float('nan'))


# --- MeasuredMode ------------------------------------------------------------

def test_mode_rejects_nonpositive_or_nonfinite_frequency() -> None:
    with pytest.raises(ValueError):
        _mode('m', 0.0)
    with pytest.raises(ValueError):
        _mode('m', -10.0)
    # NaN fails the gt=0 bound before the finite check.
    with pytest.raises(ValueError):
        _mode('m', float('nan'))


def test_mode_decay_rate_must_agree_with_t60_convention() -> None:
    # rate = ln(10^3)/T60 = 6.907755278982137 / T60.
    ok = _mode('m', 50.0, decay_time_s=0.691, decay_rate_nepers_per_s=10.0)
    assert ok.pole_frequency_hz == 50.0
    with pytest.raises(ValueError, match='T60 convention'):
        _mode('m', 50.0, decay_time_s=1.0, decay_rate_nepers_per_s=1.0)


def test_mode_rejects_duplicate_residue_position_ids() -> None:
    with pytest.raises(ValueError, match='unique'):
        _mode('m', 50.0, residues=(_residue('p1', 1.0), _residue('p1', 0.5)))


def test_mode_rejects_nonfinite_snr() -> None:
    with pytest.raises(ValueError, match='finite'):
        _mode('m', 50.0, snr_db=float('inf'))


# --- MeasuredModalAnalysisSpec ------------------------------------------------

def test_spec_seals_hash_and_round_trips() -> None:
    spec = build_modal_analysis_spec(**_spec_kwargs())
    assert len(spec.spec_sha256) == 64
    # Rebuilding from identical inputs reproduces the identical seal.
    again = build_modal_analysis_spec(**_spec_kwargs())
    assert again.spec_sha256 == spec.spec_sha256


def test_spec_rejects_tampered_hash() -> None:
    spec = build_modal_analysis_spec(**_spec_kwargs())
    payload = spec.model_dump(mode='python')
    payload['spec_sha256'] = 'b' * 64
    from htdt.cad_measured_modal_analysis import MeasuredModalAnalysisSpec

    with pytest.raises(ValueError, match='hash mismatch'):
        MeasuredModalAnalysisSpec(**payload)


def test_spec_rejects_duplicate_measurement_ids() -> None:
    with pytest.raises(ValueError, match='unique'):
        build_modal_analysis_spec(**_spec_kwargs(measurement_ids=('m-1', 'm-1')))


def test_spec_requires_positions_align_with_measurements() -> None:
    positions = (Position3(x_m=1, y_m=1, z_m=1),)
    with pytest.raises(ValueError, match='1:1'):
        build_modal_analysis_spec(
            **_spec_kwargs(measurement_positions=positions)
        )


def test_spec_requires_dataset_hashes_align_with_measurements() -> None:
    with pytest.raises(ValueError, match='1:1'):
        build_modal_analysis_spec(
            **_spec_kwargs(dataset_sha256s=('d' * 64,))
        )


def test_spec_rejects_unknown_algorithm_and_ir_capability() -> None:
    with pytest.raises(ValueError, match='explicit algorithm'):
        build_modal_analysis_spec(**_spec_kwargs(algorithm='unknown'))
    with pytest.raises(ValueError, match='magnitude-only'):
        build_modal_analysis_spec(**_spec_kwargs(ir_capability='unknown'))


def test_spec_rejects_inverted_band_and_window() -> None:
    with pytest.raises(ValueError, match='0 < low < high'):
        build_modal_analysis_spec(**_spec_kwargs(analysis_band_hz=(300.0, 5.0)))
    with pytest.raises(ValueError, match='after start'):
        build_modal_analysis_spec(
            **_spec_kwargs(ir_window_s=(0.5, 0.5))
        )


# --- ReconstructedModeShape / MeasuredModalModel ------------------------------

def test_shape_requires_aligned_arrays_and_bounded_values() -> None:
    with pytest.raises(ValueError, match='align'):
        ReconstructedModeShape(
            mode_id='m',
            evaluation_positions=(Position3(x_m=0, y_m=0, z_m=0),),
            normalized_values=(1.0, 0.5),
        )
    with pytest.raises(ValueError, match='must not exceed 1'):
        ReconstructedModeShape(
            mode_id='m',
            evaluation_positions=(
                Position3(x_m=0, y_m=0, z_m=0),
                Position3(x_m=1, y_m=0, z_m=0),
            ),
            normalized_values=(1.0, 1.5),
        )
    with pytest.raises(ValueError, match='finite'):
        ReconstructedModeShape(
            mode_id='m',
            evaluation_positions=(Position3(x_m=0, y_m=0, z_m=0),),
            normalized_values=(float('nan'),),
        )


def test_model_seals_hash_and_requires_shape_references() -> None:
    spec = build_modal_analysis_spec(**_spec_kwargs())
    mode = _mode('mode-1', 50.0)
    shape = ReconstructedModeShape(
        mode_id='mode-1',
        evaluation_positions=(Position3(x_m=0, y_m=0, z_m=0),),
        normalized_values=(1.0,),
    )
    model = build_measured_modal_model(
        model_id='model-1',
        spec=spec,
        modes=(mode,),
        mode_shapes=(shape,),
        created_at_utc='2026-09-26T00:00:00Z',
    )
    assert model.spec_sha256 == spec.spec_sha256
    assert len(model.model_sha256) == 64

    orphan = ReconstructedModeShape(
        mode_id='ghost',
        evaluation_positions=(Position3(x_m=0, y_m=0, z_m=0),),
        normalized_values=(1.0,),
    )
    with pytest.raises(ValueError, match='declared mode'):
        build_measured_modal_model(
            model_id='model-2',
            spec=spec,
            modes=(mode,),
            mode_shapes=(orphan,),
            created_at_utc='2026-09-26T00:00:00Z',
        )


def test_model_rejects_duplicate_mode_ids() -> None:
    spec = build_modal_analysis_spec(**_spec_kwargs())
    with pytest.raises(ValueError, match='unique'):
        build_measured_modal_model(
            model_id='model-1',
            spec=spec,
            modes=(_mode('dup', 50.0), _mode('dup', 51.0)),
            created_at_utc='2026-09-26T00:00:00Z',
        )


# --- aggregate_common_poles ---------------------------------------------------

def test_common_poles_cluster_across_positions() -> None:
    per_position = {
        'seat-a': (_mode('a1', 50.1), _mode('a2', 120.0)),
        'seat-b': (_mode('b1', 49.9), _mode('b2', 210.0)),
    }
    groups = aggregate_common_poles(per_position, frequency_tolerance_hz=0.5)
    # Sorted by pole frequency: b1 (49.9) seeds the group, a1 (50.1) joins.
    assert set(groups) == {('b1', 'a1'), ('a2',), ('b2',)}


def test_common_poles_respects_tolerance_boundary() -> None:
    per_position = {
        'p1': (_mode('m1', 50.0),),
        'p2': (_mode('m2', 50.5),),   # exactly at tolerance
        'p3': (_mode('m3', 50.6),),   # just outside the running center
    }
    groups = aggregate_common_poles(per_position, frequency_tolerance_hz=0.5)
    # m2 lands at the boundary of m1's group; the running center then moves
    # to 50.25, so m3 (0.35 away) joins the same cluster.
    assert set(groups) == {('m1', 'm2', 'm3')}


def test_common_poles_rejects_invalid_tolerance_and_empty_position() -> None:
    with pytest.raises(ValueError, match='positive'):
        aggregate_common_poles({}, frequency_tolerance_hz=0.0)
    with pytest.raises(ValueError, match='positive'):
        aggregate_common_poles({}, frequency_tolerance_hz=float('nan'))
    with pytest.raises(ValueError, match='non-empty'):
        aggregate_common_poles({'': (_mode('m', 50.0),)}, frequency_tolerance_hz=1.0)


# --- reconstruct_mode_shape ----------------------------------------------------

def test_reconstruct_mode_shape_normalizes_by_peak() -> None:
    mode = _mode(
        'm',
        50.0,
        residues=(_residue('p1', 2.0), _residue('p2', 1.0), _residue('p3', 0.5)),
    )
    positions = (
        Position3(x_m=0, y_m=0, z_m=0),
        Position3(x_m=1, y_m=0, z_m=0),
        Position3(x_m=2, y_m=0, z_m=0),
    )
    shape = reconstruct_mode_shape(
        mode,
        evaluation_positions=positions,
        evaluation_position_ids=('p1', 'p2', 'p3'),
    )
    assert shape.kind == 'reconstructed'
    assert shape.normalization == 'max_amplitude'
    assert shape.normalized_values == (1.0, 0.5, 0.25)
    assert shape.evaluation_positions == positions


def test_reconstruct_mode_shape_fail_closed_inputs() -> None:
    no_residues = _mode('m', 50.0)
    with pytest.raises(ValueError, match='per-position residues'):
        reconstruct_mode_shape(no_residues)
    zero = _mode('m', 50.0, residues=(_residue('p1', 0.0),))
    with pytest.raises(ValueError, match='zero-amplitude'):
        reconstruct_mode_shape(
            zero, evaluation_positions=(Position3(x_m=0, y_m=0, z_m=0),)
        )
    with pytest.raises(ValueError, match='explicit'):
        reconstruct_mode_shape(_mode('m', 50.0, residues=(_residue('p1', 1.0),)))


# --- associate_predicted_mode --------------------------------------------------

def test_association_ladder() -> None:
    measured = _mode('m', 50.0)
    assert (
        associate_predicted_mode(
            measured,
            predicted_mode_id=None,
            predicted_frequency_hz=None,
            frequency_tolerance_hz=1.0,
        )
        == 'not_attempted'
    )
    assert (
        associate_predicted_mode(
            measured,
            predicted_mode_id='theory-1',
            predicted_frequency_hz=53.0,
            frequency_tolerance_hz=1.0,
        )
        == 'unmatched'
    )
    # Within tolerance but no spatial-pattern evidence: ambiguous, never
    # silently 'associated'.
    assert (
        associate_predicted_mode(
            measured,
            predicted_mode_id='theory-1',
            predicted_frequency_hz=50.5,
            frequency_tolerance_hz=1.0,
        )
        == 'ambiguous'
    )
    assert (
        associate_predicted_mode(
            measured,
            predicted_mode_id='theory-1',
            predicted_frequency_hz=50.5,
            frequency_tolerance_hz=1.0,
            spatial_pattern_agreement=True,
        )
        == 'associated'
    )
    # Explicit disagreement within tolerance also stays ambiguous.
    assert (
        associate_predicted_mode(
            measured,
            predicted_mode_id='theory-1',
            predicted_frequency_hz=50.5,
            frequency_tolerance_hz=1.0,
            spatial_pattern_agreement=False,
        )
        == 'ambiguous'
    )


def test_association_rejects_invalid_inputs() -> None:
    measured = _mode('m', 50.0)
    with pytest.raises(ValueError, match='finite and positive'):
        associate_predicted_mode(
            measured,
            predicted_mode_id='t',
            predicted_frequency_hz=-1.0,
            frequency_tolerance_hz=1.0,
        )
    with pytest.raises(ValueError, match='positive'):
        associate_predicted_mode(
            measured,
            predicted_mode_id='t',
            predicted_frequency_hz=50.0,
            frequency_tolerance_hz=0.0,
        )
