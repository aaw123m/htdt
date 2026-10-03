from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json, canonical_sha256 as semantic_hash
from .export_io import write_text_atomic


PLAN_SCHEMA = 'htdt.r130d.general3d-validation-plan-1'
EVIDENCE_SCHEMA = 'htdt.r130d.general3d-validation-evidence-1'
EVIDENCE_ENVELOPE_SCHEMA = 'htdt.r130d.general3d-validation-envelope-1'
TARGET_WINDOW_DIAGNOSTIC_PLAN_SCHEMA = (
    'htdt.r130d.target-window-diagnostic-plan-1'
)
TARGET_WINDOW_DIAGNOSTIC_PLAN_SHA256 = (
    'ff42a7e0c44ed4726ea34edfa2549d018d66a37181786df18bbd4cf59c61d0db'
)
SPATIAL_REPRESENTATION_DIAGNOSTIC_PLAN_SCHEMA = (
    'htdt.r130d.spatial-representation-diagnostic-plan-1'
)
SPATIAL_REPRESENTATION_DIAGNOSTIC_PLAN_SHA256 = (
    '7703ca0d2b083e6b732c04d3b1ef206dc67fe5448bbd9d05dfa25d3b7f637ad4'
)
DENSE_FREQUENCY_DIAGNOSTIC_PLAN_SCHEMA = (
    'htdt.r130d.dense-frequency-diagnostic-plan-1'
)
DENSE_FREQUENCY_DIAGNOSTIC_PLAN_SHA256 = (
    '5922d03be22b86634c9397d94d15b1164b00c7a9c3e012d9628fd3a63dd3c80a'
)
STENCIL_SENSITIVITY_DIAGNOSTIC_PLAN_SCHEMA = (
    'htdt.r130d.stencil-sensitivity-diagnostic-plan-1'
)
STENCIL_SENSITIVITY_DIAGNOSTIC_PLAN_SHA256 = (
    '25229cb1a1c4778bba64ffb0b1a57ed6cc92d87c056b312aa7c061c04406a914'
)
STENCIL_SENSITIVITY_VARIANT_IDS = (
    'canonical_trilinear',
    'nearest_node',
    'uniform_eight_node',
)
STENCIL_SENSITIVITY_CANONICAL_CELL_ID = (
    'canonical_trilinear|canonical_trilinear'
)
VOXEL_STAIRCASE_SENSITIVITY_DIAGNOSTIC_PLAN_SCHEMA = (
    'htdt.r130d.voxel-staircase-sensitivity-diagnostic-plan-1'
)
VOXEL_STAIRCASE_SENSITIVITY_DIAGNOSTIC_PLAN_SHA256 = (
    '0e39ec4222d352b4f66560241c05825ce7c9b3792a410a60ac7e16cd6c45af9b'
)
VOXEL_STAIRCASE_SENSITIVITY_VARIANT_IDS = (
    'canonical_voxelization',
    'dilated_boundary_layer',
    'near_boundary_nodes_as_air',
    'open_boundary_as_air',
    'fully_blocked_boundary',
)
VOXEL_STAIRCASE_SENSITIVITY_CANONICAL_CELL_ID = 'canonical_voxelization'
VOXEL_STAIRCASE_NEIGHBOR_DIRECTIONS = (
    (1, 0, 0), (-1, 0, 0), (0, 1, 0),
    (0, -1, 0), (0, 0, 1), (0, 0, -1),
)
TIME_GATE_LOCALIZATION_DIAGNOSTIC_PLAN_SCHEMA = (
    'htdt.r130d.time-gate-localization-diagnostic-plan-1'
)
TIME_GATE_LOCALIZATION_DIAGNOSTIC_PLAN_SHA256 = (
    '32200abe7c7b35e3af82b871793fe417a59b2b911071bfdea4dcd2dede63f89e'
)
TIME_GATE_CANONICAL_CELL_ID = 'full_record'
TIME_GATE_PREFIX_CELL_IDS = (
    'prefix_10ms',
    'prefix_25ms',
    'prefix_50ms',
    'prefix_100ms',
)
TIME_GATE_TAIL_CELL_IDS = (
    'tail_10ms',
    'tail_25ms',
    'tail_50ms',
    'tail_100ms',
)
TIME_GATE_BAND_CELL_IDS = (
    'band_mid_50_150ms',
    'band_late_150_250ms',
)
TIME_GATE_CELL_IDS = (
    TIME_GATE_CANONICAL_CELL_ID,
    *TIME_GATE_PREFIX_CELL_IDS,
    *TIME_GATE_TAIL_CELL_IDS,
    *TIME_GATE_BAND_CELL_IDS,
)
TIME_GATE_PARTITION_CELL_IDS = (
    'prefix_50ms',
    'band_mid_50_150ms',
    'band_late_150_250ms',
)
TIME_GATE_PARTITION_MAX_ABS_TOLERANCE = 1.0e-12
DENSE_FREQUENCY_LOCALIZED_MAX_COUNT = 11
DENSE_FREQUENCY_PERSISTS_MIN_COUNT = 23






def load_target_window_diagnostic_plan(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('R130D target-window diagnostic plan must be a JSON object')
    if payload.get('schema_version') != TARGET_WINDOW_DIAGNOSTIC_PLAN_SCHEMA:
        raise ValueError('R130D target-window diagnostic plan schema mismatch')
    digest = semantic_hash(payload)
    if digest != TARGET_WINDOW_DIAGNOSTIC_PLAN_SHA256:
        raise ValueError(
            'R130D target-window diagnostic plan differs from the frozen pre-run '
            f'authority: {digest}'
        )
    return payload


def load_spatial_representation_diagnostic_plan(
    path: str | Path,
) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('R130D spatial diagnostic plan must be a JSON object')
    if payload.get('schema_version') != SPATIAL_REPRESENTATION_DIAGNOSTIC_PLAN_SCHEMA:
        raise ValueError('R130D spatial diagnostic plan schema mismatch')
    digest = semantic_hash(payload)
    if digest != SPATIAL_REPRESENTATION_DIAGNOSTIC_PLAN_SHA256:
        raise ValueError(
            'R130D spatial diagnostic plan differs from the frozen pre-run '
            f'authority: {digest}'
        )
    return payload


def load_dense_frequency_diagnostic_plan(
    path: str | Path,
) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('R130D dense frequency diagnostic plan must be a JSON object')
    if payload.get('schema_version') != DENSE_FREQUENCY_DIAGNOSTIC_PLAN_SCHEMA:
        raise ValueError('R130D dense frequency diagnostic plan schema mismatch')
    digest = semantic_hash(payload)
    if digest != DENSE_FREQUENCY_DIAGNOSTIC_PLAN_SHA256:
        raise ValueError(
            'R130D dense frequency diagnostic plan differs from the frozen '
            f'pre-run authority: {digest}'
        )
    return payload


def load_stencil_sensitivity_diagnostic_plan(
    path: str | Path,
) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('R130D stencil sensitivity diagnostic plan must be a JSON object')
    if payload.get('schema_version') != STENCIL_SENSITIVITY_DIAGNOSTIC_PLAN_SCHEMA:
        raise ValueError('R130D stencil sensitivity diagnostic plan schema mismatch')
    digest = semantic_hash(payload)
    if digest != STENCIL_SENSITIVITY_DIAGNOSTIC_PLAN_SHA256:
        raise ValueError(
            'R130D stencil sensitivity diagnostic plan differs from the frozen '
            f'pre-run authority: {digest}'
        )
    return payload


def load_voxel_staircase_sensitivity_diagnostic_plan(
    path: str | Path,
) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(
            'R130D voxel-staircase sensitivity diagnostic plan must be a JSON object'
        )
    if payload.get('schema_version') != (
        VOXEL_STAIRCASE_SENSITIVITY_DIAGNOSTIC_PLAN_SCHEMA
    ):
        raise ValueError(
            'R130D voxel-staircase sensitivity diagnostic plan schema mismatch'
        )
    digest = semantic_hash(payload)
    if digest != VOXEL_STAIRCASE_SENSITIVITY_DIAGNOSTIC_PLAN_SHA256:
        raise ValueError(
            'R130D voxel-staircase sensitivity diagnostic plan differs from the '
            f'frozen pre-run authority: {digest}'
        )
    return payload


def load_time_gate_localization_diagnostic_plan(
    path: str | Path,
) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(
            'R130D time-gate localization diagnostic plan must be a JSON object'
        )
    if payload.get('schema_version') != (
        TIME_GATE_LOCALIZATION_DIAGNOSTIC_PLAN_SCHEMA
    ):
        raise ValueError(
            'R130D time-gate localization diagnostic plan schema mismatch'
        )
    digest = semantic_hash(payload)
    if digest != TIME_GATE_LOCALIZATION_DIAGNOSTIC_PLAN_SHA256:
        raise ValueError(
            'R130D time-gate localization diagnostic plan differs from the '
            f'frozen pre-run authority: {digest}'
        )
    return payload


def dense_frequency_grid(
    bands: Sequence[dict[str, Any]],
) -> tuple[float, ...]:
    frequencies: list[float] = []
    for band in bands:
        band_id = band.get('band_id')
        start = float(band.get('start_hz', math.nan))
        stop = float(band.get('stop_hz', math.nan))
        step = float(band.get('step_hz', math.nan))
        center = float(band.get('canonical_center_hz', math.nan))
        if not all(math.isfinite(x) for x in (start, stop, step)):
            raise ValueError('dense frequency band bounds must be finite')
        if step <= 0.0 or stop < start:
            raise ValueError('dense frequency band must have start <= stop, step > 0')
        span = (stop - start) / step
        count = int(round(span)) + 1
        if not math.isclose(span, count - 1, rel_tol=0.0, abs_tol=1.0e-9):
            raise ValueError('dense frequency band does not divide evenly by step')
        values = [start + index * step for index in range(count)]
        if not math.isclose(values[-1], stop, rel_tol=0.0, abs_tol=1.0e-9):
            raise ValueError('dense frequency band does not land on its stop bound')
        if not math.isfinite(center) or not any(
            math.isclose(x, center, rel_tol=0.0, abs_tol=1.0e-9) for x in values
        ):
            raise ValueError(
                f'dense frequency band {band_id!r} must contain its canonical center'
            )
        frequencies.extend(values)
    if len(frequencies) != len(set(frequencies)):
        raise ValueError('dense frequency grid must not repeat a frequency')
    return tuple(frequencies)


def validate_spatial_representation_diagnostic_binding(
    plan: 'R130DGeneral3DValidationPlan',
    diagnostic: dict[str, Any],
) -> None:
    parent = diagnostic.get('parent_general3d_plan', {})
    frozen = diagnostic.get('frozen_solver_contract', {})
    checks = (
        ('parent plan id', parent.get('plan_id'), plan.plan_id),
        ('parent plan sha256', parent.get('semantic_sha256'), plan.plan_sha256()),
        ('fixture id', frozen.get('fixture_id'), plan.fixture.fixture_id),
        ('geometry kind', frozen.get('geometry_kind'), plan.fixture.geometry_kind),
        (
            'source position',
            tuple(float(x) for x in frozen.get('source_position_m', ())),
            tuple(float(x) for x in plan.fixture.source_position_m),
        ),
        (
            'receiver position',
            tuple(float(x) for x in frozen.get('receiver_position_m', ())),
            tuple(float(x) for x in plan.fixture.receiver_position_m),
        ),
        (
            'PFFDTD source commit',
            frozen.get('pffdtd_source_commit_sha'),
            plan.pffdtd.source_commit_sha,
        ),
        (
            'PFFDTD PPW',
            tuple(float(x) for x in frozen.get('pffdtd_ppw', ())),
            tuple(float(x) for x in plan.pffdtd.points_per_wavelength),
        ),
        (
            'duration',
            float(frozen.get('requested_duration_s', math.nan)),
            float(plan.physical_quantity.duration_s),
        ),
        (
            'canonical frequencies',
            tuple(float(x) for x in frozen.get('canonical_frequency_hz', ())),
            tuple(float(x) for x in plan.physical_quantity.frequency_hz),
        ),
        (
            'magnitude mask',
            float(frozen.get('magnitude_mask_relative_db', math.nan)),
            float(plan.acceptance.magnitude_mask_relative_db),
        ),
    )
    for label, actual, expected in checks:
        if actual != expected:
            raise ValueError(
                f'R130D spatial diagnostic binding mismatch for {label}: '
                f'{actual!r} != {expected!r}'
            )
    expected_thresholds = plan.acceptance.pffdtd_self_convergence.model_dump(
        mode='json', exclude_none=True
    )
    if frozen.get('pffdtd_self_convergence_thresholds') != expected_thresholds:
        raise ValueError('R130D spatial diagnostic changes canonical PFFDTD thresholds')
    frequency = diagnostic.get('frequency_neighborhood', {})
    if tuple(float(x) for x in frequency.get('diagnostic_frequency_hz', ())) != (
        39.0, 40.0, 41.0, 79.0, 80.0, 81.0
    ):
        raise ValueError('R130D diagnostic frequency neighborhood is not frozen')
    if tuple(float(x) for x in frequency.get('canonical_scored_frequency_hz', ())) != (
        40.0, 80.0
    ):
        raise ValueError('R130D diagnostic changed canonical scored frequencies')
    if frequency.get('canonical_acceptance_inclusion') is not False:
        raise ValueError('diagnostic-only frequencies cannot enter canonical acceptance')
    forbidden = diagnostic.get('forbidden_changes', {})
    if any(bool(value) for value in forbidden.values()):
        raise ValueError('R130D spatial diagnostic forbidden-change flag is enabled')
    decision = diagnostic.get('decision_semantics', {})
    if not (
        decision.get('diagnostic_only') is True
        and decision.get('canonical_solver_execution_unchanged') is True
        and decision.get('canonical_pr295_reproduction_required') is True
        and decision.get('canonical_self_convergence_unchanged') is True
        and decision.get('cross_solver_unblocked_by_diagnostic') is False
        and decision.get('general_3d_validation_promoted_by_diagnostic') is False
    ):
        raise ValueError('R130D spatial diagnostic decision semantics are not fail-closed')


def validate_dense_frequency_diagnostic_binding(
    plan: 'R130DGeneral3DValidationPlan',
    diagnostic: dict[str, Any],
) -> None:
    parent = diagnostic.get('parent_general3d_plan', {})
    spatial_parent = diagnostic.get('parent_spatial_representation_diagnostic', {})
    frozen = diagnostic.get('frozen_solver_contract', {})
    checks = (
        ('parent plan id', parent.get('plan_id'), plan.plan_id),
        ('parent plan sha256', parent.get('semantic_sha256'), plan.plan_sha256()),
        (
            'parent spatial diagnostic sha256',
            spatial_parent.get('semantic_sha256'),
            SPATIAL_REPRESENTATION_DIAGNOSTIC_PLAN_SHA256,
        ),
        ('fixture id', frozen.get('fixture_id'), plan.fixture.fixture_id),
        ('geometry kind', frozen.get('geometry_kind'), plan.fixture.geometry_kind),
        (
            'source position',
            tuple(float(x) for x in frozen.get('source_position_m', ())),
            tuple(float(x) for x in plan.fixture.source_position_m),
        ),
        (
            'receiver position',
            tuple(float(x) for x in frozen.get('receiver_position_m', ())),
            tuple(float(x) for x in plan.fixture.receiver_position_m),
        ),
        (
            'PFFDTD source commit',
            frozen.get('pffdtd_source_commit_sha'),
            plan.pffdtd.source_commit_sha,
        ),
        (
            'PFFDTD PPW',
            tuple(float(x) for x in frozen.get('pffdtd_ppw', ())),
            tuple(float(x) for x in plan.pffdtd.points_per_wavelength),
        ),
        (
            'duration',
            float(frozen.get('requested_duration_s', math.nan)),
            float(plan.physical_quantity.duration_s),
        ),
        (
            'canonical frequencies',
            tuple(float(x) for x in frozen.get('canonical_frequency_hz', ())),
            tuple(float(x) for x in plan.physical_quantity.frequency_hz),
        ),
        (
            'magnitude mask',
            float(frozen.get('magnitude_mask_relative_db', math.nan)),
            float(plan.acceptance.magnitude_mask_relative_db),
        ),
    )
    for label, actual, expected in checks:
        if actual != expected:
            raise ValueError(
                f'R130D dense frequency diagnostic binding mismatch for {label}: '
                f'{actual!r} != {expected!r}'
            )
    expected_thresholds = plan.acceptance.pffdtd_self_convergence.model_dump(
        mode='json', exclude_none=True
    )
    if frozen.get('pffdtd_self_convergence_thresholds') != expected_thresholds:
        raise ValueError('R130D dense frequency diagnostic changes canonical thresholds')
    dense = diagnostic.get('dense_frequency_neighborhood', {})
    grid = dense_frequency_grid(dense.get('bands', ()))
    if tuple(float(x) for x in dense.get('diagnostic_frequency_hz', ())) != grid:
        raise ValueError('R130D dense diagnostic frequency list is not the band grid')
    canonical = tuple(
        float(x) for x in dense.get('canonical_scored_frequency_hz', ())
    )
    if canonical != tuple(
        float(x) for x in plan.physical_quantity.frequency_hz
    ):
        raise ValueError('R130D dense diagnostic changed canonical scored frequencies')
    if not all(x in grid for x in canonical):
        raise ValueError('dense frequency grid must evaluate the canonical frequencies')
    diagnostic_only = set(float(x) for x in dense.get('diagnostic_only_frequency_hz', ()))
    if diagnostic_only != set(grid) - set(canonical):
        raise ValueError('diagnostic-only set must be the dense grid minus canonical')
    if dense.get('canonical_acceptance_inclusion') is not False:
        raise ValueError('diagnostic-only frequencies cannot enter canonical acceptance')
    if dense.get('normalized_complex_difference_formula') != (
        '|H_b-H_a| / max(|H_b|, |H_a|, fixed_floor)'
    ):
        raise ValueError('dense diagnostic metric formula is not the frozen one')
    if float(dense.get('fixed_floor', math.nan)) != 1.0e-12:
        raise ValueError('dense diagnostic fixed floor is not the frozen 1e-12')
    if list(dense.get('pairs', ())) != ['8_to_10', '10_to_12']:
        raise ValueError('dense diagnostic pairs must be exactly 8->10 and 10->12')
    classification = dense.get('classification', {})
    if int(classification.get('localized_max_count', -1)) != (
        DENSE_FREQUENCY_LOCALIZED_MAX_COUNT
    ) or int(classification.get('persists_min_count', -1)) != (
        DENSE_FREQUENCY_PERSISTS_MIN_COUNT
    ):
        raise ValueError('dense diagnostic classification thresholds are not frozen')
    binding = diagnostic.get('run76_record_binding', {})
    levels = list(binding.get('levels', ()))
    if [float(item.get('points_per_wavelength', math.nan)) for item in levels] != [
        float(x) for x in plan.pffdtd.points_per_wavelength
    ]:
        raise ValueError('run76 record binding levels must match the PPW ladder')
    for item in levels:
        for key in ('pressure_trace_sha256', 'source_trace_sha256'):
            value = str(item.get(key, ''))
            if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError(f'run76 record binding {key} is not a sha256')
    forbidden = diagnostic.get('forbidden_changes', {})
    if any(bool(value) for value in forbidden.values()):
        raise ValueError('R130D dense frequency diagnostic forbidden-change flag is on')
    decision = diagnostic.get('decision_semantics', {})
    if not (
        decision.get('diagnostic_only') is True
        and decision.get('canonical_solver_execution_unchanged') is True
        and decision.get('canonical_pr295_reproduction_required') is True
        and decision.get('canonical_self_convergence_unchanged') is True
        and decision.get('cross_solver_unblocked_by_diagnostic') is False
        and decision.get('general_3d_validation_promoted_by_diagnostic') is False
    ):
        raise ValueError('R130D dense diagnostic decision semantics are not fail-closed')


def validate_stencil_sensitivity_diagnostic_binding(
    plan: 'R130DGeneral3DValidationPlan',
    diagnostic: dict[str, Any],
    dense_diagnostic: dict[str, Any],
) -> None:
    parent = diagnostic.get('parent_general3d_plan', {})
    spatial_parent = diagnostic.get('parent_spatial_representation_diagnostic', {})
    dense_parent = diagnostic.get('parent_dense_frequency_diagnostic', {})
    frozen = diagnostic.get('frozen_solver_contract', {})
    checks = (
        ('parent plan id', parent.get('plan_id'), plan.plan_id),
        ('parent plan sha256', parent.get('semantic_sha256'), plan.plan_sha256()),
        (
            'parent spatial diagnostic sha256',
            spatial_parent.get('semantic_sha256'),
            SPATIAL_REPRESENTATION_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'parent dense diagnostic sha256',
            dense_parent.get('semantic_sha256'),
            DENSE_FREQUENCY_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'bound dense diagnostic sha256',
            dense_parent.get('semantic_sha256'),
            semantic_hash(dense_diagnostic),
        ),
        ('fixture id', frozen.get('fixture_id'), plan.fixture.fixture_id),
        ('geometry kind', frozen.get('geometry_kind'), plan.fixture.geometry_kind),
        (
            'source position',
            tuple(float(x) for x in frozen.get('source_position_m', ())),
            tuple(float(x) for x in plan.fixture.source_position_m),
        ),
        (
            'receiver position',
            tuple(float(x) for x in frozen.get('receiver_position_m', ())),
            tuple(float(x) for x in plan.fixture.receiver_position_m),
        ),
        (
            'PFFDTD source commit',
            frozen.get('pffdtd_source_commit_sha'),
            plan.pffdtd.source_commit_sha,
        ),
        (
            'PFFDTD PPW',
            tuple(float(x) for x in frozen.get('pffdtd_ppw', ())),
            tuple(float(x) for x in plan.pffdtd.points_per_wavelength),
        ),
        (
            'duration',
            float(frozen.get('requested_duration_s', math.nan)),
            float(plan.physical_quantity.duration_s),
        ),
        (
            'canonical frequencies',
            tuple(float(x) for x in frozen.get('canonical_frequency_hz', ())),
            tuple(float(x) for x in plan.physical_quantity.frequency_hz),
        ),
        (
            'magnitude mask',
            float(frozen.get('magnitude_mask_relative_db', math.nan)),
            float(plan.acceptance.magnitude_mask_relative_db),
        ),
    )
    for label, actual, expected in checks:
        if actual != expected:
            raise ValueError(
                f'R130D stencil sensitivity diagnostic binding mismatch for {label}: '
                f'{actual!r} != {expected!r}'
            )
    expected_thresholds = plan.acceptance.pffdtd_self_convergence.model_dump(
        mode='json', exclude_none=True
    )
    if frozen.get('pffdtd_self_convergence_thresholds') != expected_thresholds:
        raise ValueError('R130D stencil diagnostic changes canonical thresholds')
    stencil = diagnostic.get('stencil_sensitivity', {})
    variants = list(stencil.get('stencil_variants', ()))
    variant_ids = [str(item.get('variant_id', '')) for item in variants]
    if tuple(variant_ids) != STENCIL_SENSITIVITY_VARIANT_IDS:
        raise ValueError('stencil sensitivity variants are not the frozen set')
    control_flags = [bool(item.get('control')) for item in variants]
    if control_flags != [True, False, False]:
        raise ValueError('stencil sensitivity control flags are not frozen')
    matrix = stencil.get('cell_matrix', {})
    if tuple(matrix.get('source_variants', ())) != STENCIL_SENSITIVITY_VARIANT_IDS:
        raise ValueError('stencil sensitivity source variants are not frozen')
    if tuple(matrix.get('receiver_variants', ())) != STENCIL_SENSITIVITY_VARIANT_IDS:
        raise ValueError('stencil sensitivity receiver variants are not frozen')
    if matrix.get('canonical_cell_id') != STENCIL_SENSITIVITY_CANONICAL_CELL_ID:
        raise ValueError('stencil sensitivity canonical cell is not frozen')
    evaluation = stencil.get('evaluation', {})
    dense_block = dense_diagnostic.get('dense_frequency_neighborhood', {})
    if evaluation.get('normalized_complex_difference_formula') != dense_block.get(
        'normalized_complex_difference_formula'
    ):
        raise ValueError('stencil diagnostic metric formula differs from dense authority')
    if float(evaluation.get('fixed_floor', math.nan)) != float(
        dense_block.get('fixed_floor', math.nan)
    ):
        raise ValueError('stencil diagnostic fixed floor differs from dense authority')
    if list(evaluation.get('pairs', ())) != list(dense_block.get('pairs', ())):
        raise ValueError('stencil diagnostic pairs differ from dense authority')
    per_cell = evaluation.get('per_cell_classification', {})
    dense_classification = dense_block.get('classification', {})
    if int(per_cell.get('localized_max_count', -1)) != int(
        dense_classification.get('localized_max_count', -2)
    ) or int(per_cell.get('persists_min_count', -1)) != int(
        dense_classification.get('persists_min_count', -2)
    ):
        raise ValueError('stencil diagnostic cell thresholds differ from dense authority')
    if evaluation.get('canonical_acceptance_inclusion') is not False:
        raise ValueError('diagnostic-only cells cannot enter canonical acceptance')
    binding = diagnostic.get('run76_record_binding', {})
    dense_binding = dense_diagnostic.get('run76_record_binding', {})
    levels = list(binding.get('levels', ()))
    dense_levels = list(dense_binding.get('levels', ()))
    if [float(item.get('points_per_wavelength', math.nan)) for item in levels] != [
        float(x) for x in plan.pffdtd.points_per_wavelength
    ]:
        raise ValueError('run76 record binding levels must match the PPW ladder')
    for item in levels:
        for key in ('pressure_trace_sha256', 'source_trace_sha256'):
            value = str(item.get(key, ''))
            if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError(f'run76 record binding {key} is not a sha256')
    for item, dense_item in zip(levels, dense_levels):
        if (
            float(item.get('points_per_wavelength', math.nan))
            != float(dense_item.get('points_per_wavelength', math.nan))
            or item.get('pressure_trace_sha256')
            != dense_item.get('pressure_trace_sha256')
            or item.get('source_trace_sha256')
            != dense_item.get('source_trace_sha256')
        ):
            raise ValueError('run76 record binding pins differ from dense authority')
    forbidden = diagnostic.get('forbidden_changes', {})
    if any(bool(value) for value in forbidden.values()):
        raise ValueError('R130D stencil diagnostic forbidden-change flag is on')
    decision = diagnostic.get('decision_semantics', {})
    if not (
        decision.get('diagnostic_only') is True
        and decision.get('canonical_solver_execution_unchanged') is True
        and decision.get('canonical_pr295_reproduction_required') is True
        and decision.get('canonical_self_convergence_unchanged') is True
        and decision.get('cross_solver_unblocked_by_diagnostic') is False
        and decision.get('general_3d_validation_promoted_by_diagnostic') is False
    ):
        raise ValueError('R130D stencil diagnostic decision semantics are not fail-closed')


def validate_voxel_staircase_sensitivity_diagnostic_binding(
    plan: 'R130DGeneral3DValidationPlan',
    diagnostic: dict[str, Any],
    dense_diagnostic: dict[str, Any],
) -> None:
    parent = diagnostic.get('parent_general3d_plan', {})
    spatial_parent = diagnostic.get('parent_spatial_representation_diagnostic', {})
    dense_parent = diagnostic.get('parent_dense_frequency_diagnostic', {})
    stencil_parent = diagnostic.get('parent_stencil_sensitivity_diagnostic', {})
    frozen = diagnostic.get('frozen_solver_contract', {})
    checks = (
        ('parent plan id', parent.get('plan_id'), plan.plan_id),
        ('parent plan sha256', parent.get('semantic_sha256'), plan.plan_sha256()),
        (
            'parent spatial diagnostic sha256',
            spatial_parent.get('semantic_sha256'),
            SPATIAL_REPRESENTATION_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'parent dense diagnostic sha256',
            dense_parent.get('semantic_sha256'),
            DENSE_FREQUENCY_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'parent stencil diagnostic sha256',
            stencil_parent.get('semantic_sha256'),
            STENCIL_SENSITIVITY_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'bound dense diagnostic sha256',
            dense_parent.get('semantic_sha256'),
            semantic_hash(dense_diagnostic),
        ),
        ('fixture id', frozen.get('fixture_id'), plan.fixture.fixture_id),
        ('geometry kind', frozen.get('geometry_kind'), plan.fixture.geometry_kind),
        (
            'source position',
            tuple(float(x) for x in frozen.get('source_position_m', ())),
            tuple(float(x) for x in plan.fixture.source_position_m),
        ),
        (
            'receiver position',
            tuple(float(x) for x in frozen.get('receiver_position_m', ())),
            tuple(float(x) for x in plan.fixture.receiver_position_m),
        ),
        (
            'PFFDTD source commit',
            frozen.get('pffdtd_source_commit_sha'),
            plan.pffdtd.source_commit_sha,
        ),
        (
            'PFFDTD PPW',
            tuple(float(x) for x in frozen.get('pffdtd_ppw', ())),
            tuple(float(x) for x in plan.pffdtd.points_per_wavelength),
        ),
        (
            'duration',
            float(frozen.get('requested_duration_s', math.nan)),
            float(plan.physical_quantity.duration_s),
        ),
        (
            'canonical frequencies',
            tuple(float(x) for x in frozen.get('canonical_frequency_hz', ())),
            tuple(float(x) for x in plan.physical_quantity.frequency_hz),
        ),
        (
            'magnitude mask',
            float(frozen.get('magnitude_mask_relative_db', math.nan)),
            float(plan.acceptance.magnitude_mask_relative_db),
        ),
    )
    for label, actual, expected in checks:
        if actual != expected:
            raise ValueError(
                'R130D voxel-staircase sensitivity diagnostic binding mismatch for '
                f'{label}: {actual!r} != {expected!r}'
            )
    expected_thresholds = plan.acceptance.pffdtd_self_convergence.model_dump(
        mode='json', exclude_none=True
    )
    if frozen.get('pffdtd_self_convergence_thresholds') != expected_thresholds:
        raise ValueError('R130D voxel-staircase diagnostic changes canonical thresholds')
    voxel = diagnostic.get('voxel_staircase_sensitivity', {})
    variants = list(voxel.get('boundary_variants', ()))
    variant_ids = [str(item.get('variant_id', '')) for item in variants]
    if tuple(variant_ids) != VOXEL_STAIRCASE_SENSITIVITY_VARIANT_IDS:
        raise ValueError('voxel-staircase sensitivity variants are not the frozen set')
    control_flags = [bool(item.get('control')) for item in variants]
    if control_flags != [True, False, False, False, False]:
        raise ValueError('voxel-staircase sensitivity control flags are not frozen')
    cell_axis = voxel.get('cell_axis', {})
    if tuple(cell_axis.get('cells', ())) != VOXEL_STAIRCASE_SENSITIVITY_VARIANT_IDS:
        raise ValueError('voxel-staircase sensitivity cell axis is not frozen')
    if cell_axis.get('canonical_cell_id') != (
        VOXEL_STAIRCASE_SENSITIVITY_CANONICAL_CELL_ID
    ):
        raise ValueError('voxel-staircase sensitivity canonical cell is not frozen')
    evaluation = voxel.get('evaluation', {})
    dense_block = dense_diagnostic.get('dense_frequency_neighborhood', {})
    if evaluation.get('normalized_complex_difference_formula') != dense_block.get(
        'normalized_complex_difference_formula'
    ):
        raise ValueError(
            'voxel-staircase diagnostic metric formula differs from dense authority'
        )
    if float(evaluation.get('fixed_floor', math.nan)) != float(
        dense_block.get('fixed_floor', math.nan)
    ):
        raise ValueError(
            'voxel-staircase diagnostic fixed floor differs from dense authority'
        )
    if list(evaluation.get('pairs', ())) != list(dense_block.get('pairs', ())):
        raise ValueError(
            'voxel-staircase diagnostic pairs differ from dense authority'
        )
    per_cell = evaluation.get('per_cell_classification', {})
    dense_classification = dense_block.get('classification', {})
    if int(per_cell.get('localized_max_count', -1)) != int(
        dense_classification.get('localized_max_count', -2)
    ) or int(per_cell.get('persists_min_count', -1)) != int(
        dense_classification.get('persists_min_count', -2)
    ):
        raise ValueError(
            'voxel-staircase diagnostic cell thresholds differ from dense authority'
        )
    if evaluation.get('canonical_acceptance_inclusion') is not False:
        raise ValueError('diagnostic-only cells cannot enter canonical acceptance')
    binding = diagnostic.get('run76_record_binding', {})
    dense_binding = dense_diagnostic.get('run76_record_binding', {})
    levels = list(binding.get('levels', ()))
    dense_levels = list(dense_binding.get('levels', ()))
    if [float(item.get('points_per_wavelength', math.nan)) for item in levels] != [
        float(x) for x in plan.pffdtd.points_per_wavelength
    ]:
        raise ValueError('run76 record binding levels must match the PPW ladder')
    for item in levels:
        for key in ('pressure_trace_sha256', 'source_trace_sha256'):
            value = str(item.get(key, ''))
            if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError(f'run76 record binding {key} is not a sha256')
    for item, dense_item in zip(levels, dense_levels):
        if (
            float(item.get('points_per_wavelength', math.nan))
            != float(dense_item.get('points_per_wavelength', math.nan))
            or item.get('pressure_trace_sha256')
            != dense_item.get('pressure_trace_sha256')
            or item.get('source_trace_sha256')
            != dense_item.get('source_trace_sha256')
        ):
            raise ValueError('run76 record binding pins differ from dense authority')
    forbidden = diagnostic.get('forbidden_changes', {})
    if any(bool(value) for value in forbidden.values()):
        raise ValueError('R130D voxel-staircase diagnostic forbidden-change flag is on')
    decision = diagnostic.get('decision_semantics', {})
    if not (
        decision.get('diagnostic_only') is True
        and decision.get('canonical_solver_execution_unchanged') is True
        and decision.get('canonical_pr295_reproduction_required') is True
        and decision.get('canonical_self_convergence_unchanged') is True
        and decision.get('cross_solver_unblocked_by_diagnostic') is False
        and decision.get('general_3d_validation_promoted_by_diagnostic') is False
    ):
        raise ValueError(
            'R130D voxel-staircase diagnostic decision semantics are not fail-closed'
        )


def validate_time_gate_localization_diagnostic_binding(
    plan: 'R130DGeneral3DValidationPlan',
    diagnostic: dict[str, Any],
    dense_diagnostic: dict[str, Any],
) -> None:
    parent = diagnostic.get('parent_general3d_plan', {})
    spatial_parent = diagnostic.get('parent_spatial_representation_diagnostic', {})
    dense_parent = diagnostic.get('parent_dense_frequency_diagnostic', {})
    stencil_parent = diagnostic.get('parent_stencil_sensitivity_diagnostic', {})
    voxel_parent = diagnostic.get(
        'parent_voxel_staircase_sensitivity_diagnostic', {}
    )
    frozen = diagnostic.get('frozen_solver_contract', {})
    checks = (
        ('parent plan id', parent.get('plan_id'), plan.plan_id),
        ('parent plan sha256', parent.get('semantic_sha256'), plan.plan_sha256()),
        (
            'parent spatial diagnostic sha256',
            spatial_parent.get('semantic_sha256'),
            SPATIAL_REPRESENTATION_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'parent dense diagnostic sha256',
            dense_parent.get('semantic_sha256'),
            DENSE_FREQUENCY_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'parent stencil diagnostic sha256',
            stencil_parent.get('semantic_sha256'),
            STENCIL_SENSITIVITY_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'parent voxel-staircase diagnostic sha256',
            voxel_parent.get('semantic_sha256'),
            VOXEL_STAIRCASE_SENSITIVITY_DIAGNOSTIC_PLAN_SHA256,
        ),
        (
            'bound dense diagnostic sha256',
            dense_parent.get('semantic_sha256'),
            semantic_hash(dense_diagnostic),
        ),
        ('fixture id', frozen.get('fixture_id'), plan.fixture.fixture_id),
        ('geometry kind', frozen.get('geometry_kind'), plan.fixture.geometry_kind),
        (
            'source position',
            tuple(float(x) for x in frozen.get('source_position_m', ())),
            tuple(float(x) for x in plan.fixture.source_position_m),
        ),
        (
            'receiver position',
            tuple(float(x) for x in frozen.get('receiver_position_m', ())),
            tuple(float(x) for x in plan.fixture.receiver_position_m),
        ),
        (
            'PFFDTD source commit',
            frozen.get('pffdtd_source_commit_sha'),
            plan.pffdtd.source_commit_sha,
        ),
        (
            'PFFDTD PPW',
            tuple(float(x) for x in frozen.get('pffdtd_ppw', ())),
            tuple(float(x) for x in plan.pffdtd.points_per_wavelength),
        ),
        (
            'duration',
            float(frozen.get('requested_duration_s', math.nan)),
            float(plan.physical_quantity.duration_s),
        ),
        (
            'canonical frequencies',
            tuple(float(x) for x in frozen.get('canonical_frequency_hz', ())),
            tuple(float(x) for x in plan.physical_quantity.frequency_hz),
        ),
        (
            'magnitude mask',
            float(frozen.get('magnitude_mask_relative_db', math.nan)),
            float(plan.acceptance.magnitude_mask_relative_db),
        ),
    )
    for label, actual, expected in checks:
        if actual != expected:
            raise ValueError(
                'R130D time-gate localization diagnostic binding mismatch for '
                f'{label}: {actual!r} != {expected!r}'
            )
    expected_thresholds = plan.acceptance.pffdtd_self_convergence.model_dump(
        mode='json', exclude_none=True
    )
    if frozen.get('pffdtd_self_convergence_thresholds') != expected_thresholds:
        raise ValueError('R130D time-gate diagnostic changes canonical thresholds')
    time_gate = diagnostic.get('time_gate_localization', {})
    gates = list(time_gate.get('gates', ()))
    gate_ids = [str(item.get('gate_id', '')) for item in gates]
    if tuple(gate_ids) != TIME_GATE_CELL_IDS:
        raise ValueError('time-gate localization gates are not the frozen set')
    control_flags = [bool(item.get('control')) for item in gates]
    if control_flags != [True] + [False] * (len(TIME_GATE_CELL_IDS) - 1):
        raise ValueError('time-gate localization control flags are not frozen')
    duration_s = float(plan.physical_quantity.duration_s)
    partition_intervals: list[tuple[float, float]] = []
    for item in gates:
        interval = item.get('interval_s', ())
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or not all(math.isfinite(float(x)) for x in interval)
        ):
            raise ValueError('time-gate interval must be a [start,end) pair')
        t_start, t_end = float(interval[0]), float(interval[1])
        if not (0.0 <= t_start < t_end <= duration_s):
            raise ValueError(
                f'time-gate interval {interval!r} is outside the frozen record'
            )
        kind = str(item.get('kind', ''))
        gate_id = str(item.get('gate_id', ''))
        if kind == 'control' and (t_start, t_end) != (0.0, duration_s):
            raise ValueError('time-gate control cell must span the whole record')
        if kind == 'prefix' and t_start != 0.0:
            raise ValueError('time-gate prefix cell must start at t=0')
        if kind == 'tail' and t_end != duration_s:
            raise ValueError('time-gate tail cell must end at T')
        if kind not in ('control', 'prefix', 'tail', 'band'):
            raise ValueError(f'time-gate cell {gate_id!r} has unknown kind')
        if gate_id in TIME_GATE_PARTITION_CELL_IDS:
            partition_intervals.append((t_start, t_end))
    edges = sorted({0.0, duration_s} | {x for pair in partition_intervals for x in pair})
    partition_union = [
        (edges[i], edges[i + 1]) for i in range(len(edges) - 1)
    ]
    if sorted(partition_intervals) != partition_union:
        raise ValueError(
            'time-gate partition cells must tile [0,T) disjointly and exactly'
        )
    cell_axis = time_gate.get('cell_axis', {})
    if tuple(cell_axis.get('cells', ())) != TIME_GATE_CELL_IDS:
        raise ValueError('time-gate localization cell axis is not frozen')
    if cell_axis.get('canonical_cell_id') != TIME_GATE_CANONICAL_CELL_ID:
        raise ValueError('time-gate localization canonical cell is not frozen')
    evaluation = time_gate.get('evaluation', {})
    dense_block = dense_diagnostic.get('dense_frequency_neighborhood', {})
    if evaluation.get('normalized_complex_difference_formula') != dense_block.get(
        'normalized_complex_difference_formula'
    ):
        raise ValueError(
            'time-gate diagnostic metric formula differs from dense authority'
        )
    if float(evaluation.get('fixed_floor', math.nan)) != float(
        dense_block.get('fixed_floor', math.nan)
    ):
        raise ValueError(
            'time-gate diagnostic fixed floor differs from dense authority'
        )
    if list(evaluation.get('pairs', ())) != list(dense_block.get('pairs', ())):
        raise ValueError('time-gate diagnostic pairs differ from dense authority')
    per_cell = evaluation.get('per_cell_classification', {})
    dense_classification = dense_block.get('classification', {})
    if int(per_cell.get('localized_max_count', -1)) != int(
        dense_classification.get('localized_max_count', -2)
    ) or int(per_cell.get('persists_min_count', -1)) != int(
        dense_classification.get('persists_min_count', -2)
    ):
        raise ValueError(
            'time-gate diagnostic cell thresholds differ from dense authority'
        )
    partition_check = evaluation.get('partition_identity_check', {})
    if tuple(partition_check.get('partition_cells', ())) != (
        TIME_GATE_PARTITION_CELL_IDS
    ):
        raise ValueError('time-gate partition check cells are not frozen')
    if float(partition_check.get('max_abs_complex_component_tolerance', math.nan)) != (
        TIME_GATE_PARTITION_MAX_ABS_TOLERANCE
    ):
        raise ValueError('time-gate partition tolerance is not the frozen 1e-12')
    classification = evaluation.get('classification', {})
    if tuple(classification.get(key, '') for key in (
        'invariant', 'early_carried', 'late_carried', 'broadband', 'mixed',
        'not_evaluated',
    )) != (
        'TIME_GATE_WORSENING_PATTERN_INVARIANT',
        'TIME_GATE_WORSENING_EARLY_CARRIED',
        'TIME_GATE_WORSENING_LATE_CARRIED',
        'TIME_GATE_WORSENING_BROADBAND',
        'TIME_GATE_WORSENING_PATTERN_MIXED',
        'NOT_EVALUATED',
    ):
        raise ValueError('time-gate classification labels are not frozen')
    if evaluation.get('canonical_acceptance_inclusion') is not False:
        raise ValueError('diagnostic-only cells cannot enter canonical acceptance')
    binding = diagnostic.get('run76_record_binding', {})
    dense_binding = dense_diagnostic.get('run76_record_binding', {})
    levels = list(binding.get('levels', ()))
    dense_levels = list(dense_binding.get('levels', ()))
    if [float(item.get('points_per_wavelength', math.nan)) for item in levels] != [
        float(x) for x in plan.pffdtd.points_per_wavelength
    ]:
        raise ValueError('run76 record binding levels must match the PPW ladder')
    for item in levels:
        for key in ('pressure_trace_sha256', 'source_trace_sha256'):
            value = str(item.get(key, ''))
            if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError(f'run76 record binding {key} is not a sha256')
    for item, dense_item in zip(levels, dense_levels):
        if (
            float(item.get('points_per_wavelength', math.nan))
            != float(dense_item.get('points_per_wavelength', math.nan))
            or item.get('pressure_trace_sha256')
            != dense_item.get('pressure_trace_sha256')
            or item.get('source_trace_sha256')
            != dense_item.get('source_trace_sha256')
        ):
            raise ValueError('run76 record binding pins differ from dense authority')
    forbidden = diagnostic.get('forbidden_changes', {})
    if any(bool(value) for value in forbidden.values()):
        raise ValueError('R130D time-gate diagnostic forbidden-change flag is on')
    decision = diagnostic.get('decision_semantics', {})
    if not (
        decision.get('diagnostic_only') is True
        and decision.get('canonical_solver_execution_unchanged') is True
        and decision.get('canonical_pr295_reproduction_required') is True
        and decision.get('canonical_self_convergence_unchanged') is True
        and decision.get('cross_solver_unblocked_by_diagnostic') is False
        and decision.get('general_3d_validation_promoted_by_diagnostic') is False
    ):
        raise ValueError(
            'R130D time-gate diagnostic decision semantics are not fail-closed'
        )


def normalized_complex_difference(
    first: complex,
    second: complex,
    *,
    fixed_floor: float,
) -> float:
    floor = float(fixed_floor)
    if not math.isfinite(floor) or floor <= 0.0:
        raise ValueError('normalized complex difference floor must be finite/positive')
    a = complex(first)
    b = complex(second)
    if not all(math.isfinite(x) for x in (a.real, a.imag, b.real, b.imag)):
        raise ValueError('normalized complex difference inputs must be finite')
    return float(abs(b - a) / max(abs(a), abs(b), floor))


def apply_time_gate(
    record: np.ndarray,
    *,
    time_step_s: float,
    interval_s: Sequence[float],
) -> np.ndarray:
    """Apply a frozen half-open causal time gate to a bound raw record.

    The gated record keeps the samples n with
    ``t_start_s <= n * time_step_s < t_end_s`` and is zero outside the gate;
    the solver time grid itself is unchanged, so disjoint gates that tile
    ``[0, T)`` decompose the finite-record transform additively.
    """
    samples = np.asarray(record, dtype=np.float64)
    if samples.ndim != 1 or samples.size < 2:
        raise ValueError('time gate requires a 1D record of at least two samples')
    if not np.all(np.isfinite(samples)):
        raise ValueError('time gate record must be finite')
    if not math.isfinite(time_step_s) or time_step_s <= 0.0:
        raise ValueError('time gate time_step_s must be finite and positive')
    if (
        not isinstance(interval_s, Sequence)
        or len(interval_s) != 2
        or not all(math.isfinite(float(x)) for x in interval_s)
    ):
        raise ValueError('time gate interval must be a [start,end) pair')
    t_start = float(interval_s[0])
    t_end = float(interval_s[1])
    record_end = samples.size * float(time_step_s)
    if not (0.0 <= t_start < t_end <= record_end):
        raise ValueError(
            f'time gate interval {(t_start, t_end)!r} is outside the record '
            f'[0,{record_end})'
        )
    times = np.arange(samples.size, dtype=np.float64) * float(time_step_s)
    mask = (times >= t_start) & (times < t_end)
    if not np.any(mask):
        raise ValueError(
            f'time gate interval {(t_start, t_end)!r} selects no record samples'
        )
    return samples * mask


def classify_frequency_neighborhood(
    d_8_10: Sequence[float],
    d_10_12: Sequence[float],
) -> dict[str, Any]:
    coarse = tuple(float(x) for x in d_8_10)
    fine = tuple(float(x) for x in d_10_12)
    if len(coarse) != 6 or len(fine) != 6:
        raise ValueError('frequency-neighborhood classifier requires six frequencies')
    if any(not math.isfinite(x) or x < 0.0 for x in (*coarse, *fine)):
        raise ValueError('frequency-neighborhood differences must be finite/nonnegative')
    worsening = tuple(b > a for a, b in zip(coarse, fine, strict=True))
    count = sum(worsening)
    if count >= 4:
        classification = 'NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD'
    elif count <= 2:
        classification = 'NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'
    else:
        classification = 'MIXED_NEIGHBORHOOD_SENSITIVITY'
    return {
        'classification': classification,
        'worsening_count': count,
        'worsening_by_frequency': list(worsening),
    }


def classify_dense_frequency_neighborhood(
    d_8_10: Sequence[float],
    d_10_12: Sequence[float],
) -> dict[str, Any]:
    coarse = tuple(float(x) for x in d_8_10)
    fine = tuple(float(x) for x in d_10_12)
    if len(coarse) != len(fine):
        raise ValueError('dense frequency-neighborhood series must have equal length')
    if len(coarse) < 2:
        raise ValueError('dense frequency-neighborhood classifier requires a grid')
    if any(not math.isfinite(x) or x < 0.0 for x in (*coarse, *fine)):
        raise ValueError('dense frequency-neighborhood differences must be finite/nonnegative')
    worsening = tuple(b > a for a, b in zip(coarse, fine, strict=True))
    count = sum(worsening)
    if count >= DENSE_FREQUENCY_PERSISTS_MIN_COUNT:
        classification = 'DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD'
    elif count <= DENSE_FREQUENCY_LOCALIZED_MAX_COUNT:
        classification = 'DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'
    else:
        classification = 'DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'
    return {
        'classification': classification,
        'worsening_count': count,
        'worsening_by_frequency': list(worsening),
    }


def classify_stencil_sensitivity(
    *,
    canonical_worsening_by_frequency: Sequence[bool],
    canonical_classification: str,
    noncanonical_cells: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    canonical_vector = tuple(bool(x) for x in canonical_worsening_by_frequency)
    if len(canonical_vector) < 2:
        raise ValueError('stencil sensitivity classifier requires the canonical vector')
    hamming: dict[str, int] = {}
    shifted_cell_ids: list[str] = []
    reclassified_cell_ids: list[str] = []
    identical_cell_count = 0
    for cell in noncanonical_cells:
        cell_id = str(cell.get('cell_id', ''))
        if cell_id == STENCIL_SENSITIVITY_CANONICAL_CELL_ID:
            raise ValueError('non-canonical cell list must not contain the control')
        vector = tuple(bool(x) for x in cell.get('worsening_by_frequency', ()))
        if len(vector) != len(canonical_vector):
            raise ValueError(
                f'stencil cell {cell_id!r} worsening vector length mismatch'
            )
        distance = sum(a != b for a, b in zip(vector, canonical_vector))
        hamming[cell_id] = int(distance)
        classification = str(cell.get('classification', ''))
        if distance == 0:
            identical_cell_count += 1
        elif classification == canonical_classification:
            shifted_cell_ids.append(cell_id)
        else:
            reclassified_cell_ids.append(cell_id)
    if reclassified_cell_ids:
        classification = 'STENCIL_WORSENING_PATTERN_RECLASSIFIED'
    elif shifted_cell_ids:
        classification = 'STENCIL_WORSENING_PATTERN_SHIFTED'
    else:
        classification = 'STENCIL_WORSENING_PATTERN_INVARIANT'
    return {
        'classification': classification,
        'canonical_cell_id': STENCIL_SENSITIVITY_CANONICAL_CELL_ID,
        'canonical_dense_classification': canonical_classification,
        'evaluated_noncanonical_cell_count': len(noncanonical_cells),
        'identical_vector_cell_count': identical_cell_count,
        'shifted_cell_ids': shifted_cell_ids,
        'reclassified_cell_ids': reclassified_cell_ids,
        'hamming_distance_by_cell': hamming,
    }


def classify_voxel_staircase_sensitivity(
    *,
    canonical_worsening_by_frequency: Sequence[bool],
    canonical_classification: str,
    noncanonical_cells: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    canonical_vector = tuple(bool(x) for x in canonical_worsening_by_frequency)
    if len(canonical_vector) < 2:
        raise ValueError(
            'voxel-staircase sensitivity classifier requires the canonical vector'
        )
    hamming: dict[str, int] = {}
    shifted_cell_ids: list[str] = []
    reclassified_cell_ids: list[str] = []
    identical_cell_count = 0
    for cell in noncanonical_cells:
        cell_id = str(cell.get('cell_id', ''))
        if cell_id == VOXEL_STAIRCASE_SENSITIVITY_CANONICAL_CELL_ID:
            raise ValueError('non-canonical cell list must not contain the control')
        vector = tuple(bool(x) for x in cell.get('worsening_by_frequency', ()))
        if len(vector) != len(canonical_vector):
            raise ValueError(
                f'voxel-staircase cell {cell_id!r} worsening vector length mismatch'
            )
        distance = sum(a != b for a, b in zip(vector, canonical_vector))
        hamming[cell_id] = int(distance)
        classification = str(cell.get('classification', ''))
        if distance == 0:
            identical_cell_count += 1
        elif classification == canonical_classification:
            shifted_cell_ids.append(cell_id)
        else:
            reclassified_cell_ids.append(cell_id)
    if reclassified_cell_ids:
        classification = 'VOXEL_STAIRCASE_WORSENING_PATTERN_RECLASSIFIED'
    elif shifted_cell_ids:
        classification = 'VOXEL_STAIRCASE_WORSENING_PATTERN_SHIFTED'
    else:
        classification = 'VOXEL_STAIRCASE_WORSENING_PATTERN_INVARIANT'
    return {
        'classification': classification,
        'canonical_cell_id': VOXEL_STAIRCASE_SENSITIVITY_CANONICAL_CELL_ID,
        'canonical_dense_classification': canonical_classification,
        'evaluated_noncanonical_cell_count': len(noncanonical_cells),
        'identical_vector_cell_count': identical_cell_count,
        'shifted_cell_ids': shifted_cell_ids,
        'reclassified_cell_ids': reclassified_cell_ids,
        'hamming_distance_by_cell': hamming,
    }


def classify_time_gate_localization(
    *,
    canonical_worsening_by_frequency: Sequence[bool],
    canonical_classification: str,
    noncanonical_cells: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Frozen rule for the causal time-gate axis.

    A gate "carries" the worsening iff its per-cell dense classification is
    not localized (worsening count above the frozen localized maximum).
    Evaluation order: invariant -> early_carried -> late_carried -> broadband
    -> mixed.
    """
    canonical_vector = tuple(bool(x) for x in canonical_worsening_by_frequency)
    if len(canonical_vector) < 2:
        raise ValueError('time-gate classifier requires the canonical vector')
    hamming: dict[str, int] = {}
    prefix_exact: list[str] = []
    tail_exact: list[str] = []
    prefix_localized = True
    tail_localized = True
    prefix_carries: list[str] = []
    tail_carries: list[str] = []
    identical_cell_count = 0
    localized_label = 'DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'
    seen_ids: list[str] = []
    for cell in noncanonical_cells:
        cell_id = str(cell.get('cell_id', ''))
        if cell_id == TIME_GATE_CANONICAL_CELL_ID:
            raise ValueError('non-canonical cell list must not contain the control')
        if cell_id not in TIME_GATE_CELL_IDS:
            raise ValueError(f'time-gate cell {cell_id!r} is not in the frozen set')
        seen_ids.append(cell_id)
        vector = tuple(bool(x) for x in cell.get('worsening_by_frequency', ()))
        if len(vector) != len(canonical_vector):
            raise ValueError(
                f'time-gate cell {cell_id!r} worsening vector length mismatch'
            )
        distance = sum(a != b for a, b in zip(vector, canonical_vector))
        hamming[cell_id] = int(distance)
        classification = str(cell.get('classification', ''))
        carries = classification != localized_label
        if distance == 0:
            identical_cell_count += 1
        if cell_id in TIME_GATE_PREFIX_CELL_IDS:
            if distance == 0:
                prefix_exact.append(cell_id)
            if carries:
                prefix_carries.append(cell_id)
                prefix_localized = False
        elif cell_id in TIME_GATE_TAIL_CELL_IDS:
            if distance == 0:
                tail_exact.append(cell_id)
            if carries:
                tail_carries.append(cell_id)
                tail_localized = False
    if set(seen_ids) != set(TIME_GATE_CELL_IDS) - {TIME_GATE_CANONICAL_CELL_ID}:
        raise ValueError(
            'time-gate classifier requires exactly the frozen non-control cells'
        )
    if identical_cell_count == len(noncanonical_cells):
        classification = 'TIME_GATE_WORSENING_PATTERN_INVARIANT'
    elif tail_localized and prefix_exact:
        classification = 'TIME_GATE_WORSENING_EARLY_CARRIED'
    elif prefix_localized and tail_exact:
        classification = 'TIME_GATE_WORSENING_LATE_CARRIED'
    elif prefix_carries and tail_carries:
        classification = 'TIME_GATE_WORSENING_BROADBAND'
    else:
        classification = 'TIME_GATE_WORSENING_PATTERN_MIXED'
    return {
        'classification': classification,
        'canonical_cell_id': TIME_GATE_CANONICAL_CELL_ID,
        'canonical_dense_classification': canonical_classification,
        'evaluated_noncanonical_cell_count': len(noncanonical_cells),
        'identical_vector_cell_count': identical_cell_count,
        'all_prefix_cells_localized': prefix_localized,
        'all_tail_cells_localized': tail_localized,
        'prefix_exact_reproduction_cell_ids': prefix_exact,
        'tail_exact_reproduction_cell_ids': tail_exact,
        'prefix_carrying_cell_ids': prefix_carries,
        'tail_carrying_cell_ids': tail_carries,
        'hamming_distance_by_cell': hamming,
    }


def classify_spatial_representation_trend(
    levels: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(levels, key=lambda item: float(item['points_per_wavelength']))
    if [float(item['points_per_wavelength']) for item in ordered] != [8.0, 10.0, 12.0]:
        raise ValueError('spatial trend requires exact 8/10/12 PPW levels')
    volume = [abs(float(item['relative_volume_error'])) for item in ordered]
    plane = [
        float(item['sloped_rms_abs_normal_distance_m']) for item in ordered
    ]
    if any(not math.isfinite(x) or x < 0.0 for x in (*volume, *plane)):
        raise ValueError('spatial trend metrics must be finite/nonnegative')
    worsened = bool(volume[2] > volume[1] or plane[2] > plane[1])
    return {
        'classification': (
            'SPATIAL_REPRESENTATION_NON_MONOTONIC'
            if worsened
            else 'SPATIAL_REPRESENTATION_MONOTONIC'
        ),
        'volume_relative_error': volume,
        'sloped_rms_abs_normal_distance_m': plane,
        'ten_to_twelve_volume_worsened': volume[2] > volume[1],
        'ten_to_twelve_plane_rms_worsened': plane[2] > plane[1],
    }


def stencil_variant_weights(
    *,
    variant_id: str,
    canonical_weights: Sequence[float],
    node_positions_m: Sequence[Sequence[float]],
    exact_position_m: Sequence[float],
) -> np.ndarray:
    if variant_id not in STENCIL_SENSITIVITY_VARIANT_IDS:
        raise ValueError(f'unknown stencil weight variant {variant_id!r}')
    alpha = np.asarray(canonical_weights, dtype=np.float64)
    positions = np.asarray(node_positions_m, dtype=np.float64)
    target = np.asarray(exact_position_m, dtype=np.float64)
    if alpha.ndim != 1 or alpha.size < 1:
        raise ValueError('stencil canonical weights must be a nonempty vector')
    if positions.ndim != 2 or positions.shape != (alpha.size, 3):
        raise ValueError('stencil node positions must be an (n,3) matrix')
    if target.shape != (3,) or not np.all(np.isfinite(target)):
        raise ValueError('stencil exact position must be finite xyz')
    if not np.all(np.isfinite(alpha)) or not np.all(np.isfinite(positions)):
        raise ValueError('stencil weights/node positions must be finite')
    if variant_id == 'canonical_trilinear':
        return alpha.copy()
    if variant_id == 'nearest_node':
        distance = np.sum((positions - target[None, :]) ** 2, axis=1)
        nearest = int(np.argmin(distance))
        variant = np.zeros(alpha.size, dtype=np.float64)
        variant[nearest] = 1.0
    else:
        variant = np.full(alpha.size, 1.0 / alpha.size, dtype=np.float64)
    if not math.isclose(
        float(np.sum(variant)), 1.0, rel_tol=0.0, abs_tol=1.0e-12
    ):
        raise ValueError('stencil variant weights must sum to one')
    return variant


def voxel_staircase_boundary_variant(
    *,
    variant_id: str,
    dimensions: Sequence[int],
    boundary_linear_indices: Sequence[int],
    boundary_adjacency: Any,
) -> dict[str, Any]:
    if variant_id not in VOXEL_STAIRCASE_SENSITIVITY_VARIANT_IDS:
        raise ValueError(f'unknown voxel-staircase variant {variant_id!r}')
    dims = tuple(int(x) for x in dimensions)
    if len(dims) != 3 or any(x <= 0 for x in dims):
        raise ValueError('voxel-staircase dimensions must be three positive ints')
    nx, ny, nz = dims
    total = nx * ny * nz
    bn = np.asarray(boundary_linear_indices, dtype=np.int64).ravel()
    adj = np.asarray(boundary_adjacency, dtype=bool)
    if adj.shape != (bn.size, len(VOXEL_STAIRCASE_NEIGHBOR_DIRECTIONS)):
        raise ValueError(
            'voxel-staircase adjacency must be an (Nb,6) array matching bn_ixyz'
        )
    if bn.size and (int(bn.min()) < 0 or int(bn.max()) >= total):
        raise ValueError('voxel-staircase boundary indices out of grid range')
    if int(np.unique(bn).size) != int(bn.size):
        raise ValueError('voxel-staircase boundary indices must be unique')

    report: dict[str, Any] = {
        'variant_id': variant_id,
        'canonical_boundary_node_count': int(bn.size),
        'dropped_boundary_node_count': 0,
        'appended_boundary_node_count': 0,
    }
    kept = np.arange(bn.size, dtype=np.int64)
    if variant_id == 'canonical_voxelization':
        new_bn = bn.copy()
        new_adj = adj.copy()
    elif variant_id == 'near_boundary_nodes_as_air':
        keep_mask = adj.any(axis=1)
        kept = np.flatnonzero(keep_mask).astype(np.int64)
        new_bn = bn[keep_mask]
        new_adj = adj[keep_mask]
        report['dropped_boundary_node_count'] = int(bn.size - new_bn.size)
    elif variant_id == 'open_boundary_as_air':
        new_bn = bn.copy()
        new_adj = np.ones_like(adj)
    elif variant_id == 'fully_blocked_boundary':
        new_bn = bn.copy()
        new_adj = np.zeros_like(adj)
    else:  # dilated_boundary_layer
        offsets = (
            ny * nz, -ny * nz, nz, -nz, 1, -1,
        )

        def on_halo(index: int) -> bool:
            iz = index % nz
            iy = (index // nz) % ny
            ix = index // (ny * nz)
            return (
                ix in (0, nx - 1)
                or iy in (0, ny - 1)
                or iz in (0, nz - 1)
            )

        bn_set = set(int(x) for x in bn)
        parent_by_candidate: dict[int, int] = {}
        for row in range(bn.size):
            if not adj[row].any():
                continue
            for column, offset in enumerate(offsets):
                if not adj[row, column]:
                    continue
                candidate = int(bn[row]) + offset
                if candidate < 0 or candidate >= total:
                    continue
                if candidate in bn_set or on_halo(candidate):
                    continue
                current = parent_by_candidate.get(candidate)
                if current is None or int(bn[row]) < int(bn[current]):
                    parent_by_candidate[candidate] = row
        appended = sorted(parent_by_candidate)
        new_bn = np.concatenate([bn, np.asarray(appended, dtype=np.int64)])
        new_adj = np.concatenate(
            [
                adj,
                np.asarray(
                    [adj[parent_by_candidate[c]] for c in appended],
                    dtype=bool,
                ).reshape(-1, len(VOXEL_STAIRCASE_NEIGHBOR_DIRECTIONS)),
            ]
        )
        report['appended_boundary_node_count'] = len(appended)
        report['appended_linear_index_sha256'] = semantic_hash(appended)
    report['boundary_node_count'] = int(new_bn.size)
    report['boundary_set_sha256'] = semantic_hash([int(x) for x in new_bn])
    report['adjacency_open_edge_count'] = int(np.sum(new_adj))
    return {
        'bn_ixyz': new_bn,
        'adj_bn': new_adj,
        'canonical_row_indices': kept,
        'report': report,
    }


def interpolation_stencil_diagnostic(
    *,
    xv: Sequence[float],
    yv: Sequence[float],
    zv: Sequence[float],
    linear_indices: Sequence[int],
    weights: Sequence[float],
    exact_position_m: Sequence[float],
    grid_spacing_m: float,
) -> dict[str, Any]:
    axes = tuple(np.asarray(axis, dtype=np.float64) for axis in (xv, yv, zv))
    dims = tuple(int(axis.size) for axis in axes)
    if any(size < 2 for size in dims):
        raise ValueError('stencil grid axes must contain at least two nodes')
    indices = np.asarray(linear_indices, dtype=np.int64)
    alpha = np.asarray(weights, dtype=np.float64)
    target = np.asarray(exact_position_m, dtype=np.float64)
    h = float(grid_spacing_m)
    if indices.shape != (8,) or alpha.shape != (8,):
        raise ValueError('trilinear stencil must contain exactly eight nodes/weights')
    if target.shape != (3,) or not np.all(np.isfinite(target)):
        raise ValueError('stencil exact position must be finite xyz')
    if not math.isfinite(h) or h <= 0.0:
        raise ValueError('stencil grid spacing must be finite/positive')
    ngrid = math.prod(dims)
    if np.any(indices < 0) or np.any(indices >= ngrid):
        raise ValueError('stencil linear index outside grid')
    if not np.all(np.isfinite(alpha)):
        raise ValueError('stencil weights must be finite')

    iyz = dims[1] * dims[2]
    ix = indices // iyz
    remainder = indices % iyz
    iy = remainder // dims[2]
    iz = remainder % dims[2]
    grid_indices = np.column_stack((ix, iy, iz))
    positions = np.column_stack(
        (axes[0][ix], axes[1][iy], axes[2][iz])
    )
    reconstructed = np.sum(alpha[:, None] * positions, axis=0)
    lower = np.min(positions, axis=0)
    fractional = (target - lower) / h
    reconstruction_error = float(np.linalg.norm(reconstructed - target))
    core = {
        'surrounding_linear_indices': [int(x) for x in indices],
        'surrounding_grid_indices': [
            [int(v) for v in row] for row in grid_indices
        ],
        'node_positions_m': [
            [float(v) for v in row] for row in positions
        ],
        'interpolation_weights': [float(x) for x in alpha],
        'weight_sum': float(np.sum(alpha)),
        'fractional_cell_coordinate': [float(x) for x in fractional],
        'reconstructed_coordinate_m': [float(x) for x in reconstructed],
        'reconstruction_error_m': reconstruction_error,
    }
    return {**core, 'stencil_sha256': semantic_hash(core)}


def connected_air_domain_node_metrics(
    *,
    dimensions: Sequence[int],
    boundary_linear_indices: Sequence[int],
    boundary_adjacency: Sequence[Sequence[bool]] | np.ndarray,
    source_linear_indices: Sequence[int],
    neighbor_directions: Sequence[Sequence[int]],
) -> dict[str, Any]:
    dims = tuple(int(x) for x in dimensions)
    if len(dims) != 3 or any(x < 1 for x in dims):
        raise ValueError('air-domain dimensions must be three positive integers')
    directions = tuple(tuple(int(v) for v in row) for row in neighbor_directions)
    if directions != (
        (1, 0, 0), (-1, 0, 0), (0, 1, 0),
        (0, -1, 0), (0, 0, 1), (0, 0, -1),
    ):
        raise ValueError('air-domain neighbor directions differ from frozen Cartesian authority')
    ngrid = math.prod(dims)
    bn = np.asarray(boundary_linear_indices, dtype=np.int64)
    adj = np.asarray(boundary_adjacency, dtype=bool)
    sources = np.asarray(source_linear_indices, dtype=np.int64)
    if bn.ndim != 1 or adj.shape != (bn.size, 6):
        raise ValueError('air-domain boundary indices/adjacency shape mismatch')
    if sources.ndim != 1 or sources.size < 1:
        raise ValueError('air-domain source stencil must contain at least one node')
    if np.any(bn < 0) or np.any(bn >= ngrid) or np.unique(bn).size != bn.size:
        raise ValueError('air-domain boundary indices are invalid')
    if np.any(sources < 0) or np.any(sources >= ngrid):
        raise ValueError('air-domain source index is outside grid')
    boundary_row = {int(index): row for row, index in enumerate(bn)}
    reverse = (1, 0, 3, 2, 5, 4)
    ny, nz = dims[1], dims[2]
    yz = ny * nz

    def coords(index: int) -> tuple[int, int, int]:
        ix = index // yz
        rem = index % yz
        iy = rem // nz
        iz = rem % nz
        return ix, iy, iz

    def linear(ix: int, iy: int, iz: int) -> int:
        return ix * yz + iy * nz + iz

    reached = np.zeros(ngrid, dtype=bool)
    queue: list[int] = [int(sources[0])]
    reached[queue[0]] = True
    head = 0
    while head < len(queue):
        current = queue[head]
        head += 1
        ix, iy, iz = coords(current)
        current_row = boundary_row.get(current)
        for direction_index, (dx, dy, dz) in enumerate(directions):
            nx, ny_, nz_ = ix + dx, iy + dy, iz + dz
            if not (0 <= nx < dims[0] and 0 <= ny_ < dims[1] and 0 <= nz_ < dims[2]):
                continue
            neighbor = linear(nx, ny_, nz_)
            if current_row is not None and not bool(adj[current_row, direction_index]):
                continue
            neighbor_row = boundary_row.get(neighbor)
            if neighbor_row is not None and not bool(adj[neighbor_row, reverse[direction_index]]):
                continue
            if not reached[neighbor]:
                reached[neighbor] = True
                queue.append(neighbor)

    if not np.all(reached[sources]):
        raise ValueError('source trilinear stencil spans disconnected air components')
    reachable_count = int(np.count_nonzero(reached))
    boundary_reachable = int(np.count_nonzero(reached[bn]))
    return {
        'active_node_count': ngrid,
        'reachable_air_node_count': reachable_count,
        'interior_node_count': reachable_count - boundary_reachable,
        'boundary_node_count': boundary_reachable,
        'exterior_or_disconnected_node_count': ngrid - reachable_count,
        'reachable_mask': reached,
    }


def plane_distance_metrics(
    samples_m: Sequence[Sequence[float]],
    *,
    plane_point_m: Sequence[float],
    plane_unit_normal: Sequence[float],
    grid_spacing_m: float,
) -> dict[str, Any]:
    samples = np.asarray(samples_m, dtype=np.float64)
    point = np.asarray(plane_point_m, dtype=np.float64)
    normal = np.asarray(plane_unit_normal, dtype=np.float64)
    h = float(grid_spacing_m)
    if samples.ndim != 2 or samples.shape[1] != 3 or samples.shape[0] < 1:
        raise ValueError('plane-distance metric requires one or more xyz samples')
    if point.shape != (3,) or normal.shape != (3,):
        raise ValueError('plane point/normal must be xyz')
    norm = float(np.linalg.norm(normal))
    if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1.0e-12):
        raise ValueError('plane normal must be unit length')
    if not math.isfinite(h) or h <= 0.0:
        raise ValueError('plane-distance grid spacing must be finite/positive')
    signed = (samples - point) @ normal
    absolute = np.abs(signed)
    rms = float(np.sqrt(np.mean(np.square(absolute))))
    maximum = float(np.max(absolute))
    return {
        'sloped_boundary_sample_count': int(samples.shape[0]),
        'sloped_signed_normal_distance_m': [float(x) for x in signed],
        'sloped_rms_abs_normal_distance_m': rms,
        'sloped_max_abs_normal_distance_m': maximum,
        'sloped_rms_abs_normal_distance_over_h': rms / h,
        'sloped_max_abs_normal_distance_over_h': maximum / h,
    }


def target_window_sampling_metadata(
    *,
    solver: str,
    requested_duration_s: float,
    dt_s: float,
    sample_count: int,
    frequency_hz: Sequence[float],
    source_sampling: str,
    pressure_sampling: str,
) -> dict[str, Any]:
    duration = float(requested_duration_s)
    dt = float(dt_s)
    count = int(sample_count)
    if not solver:
        raise ValueError('sampling metadata solver must be non-empty')
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError('requested duration must be finite and positive')
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError('native dt must be finite and positive')
    if count < 1:
        raise ValueError('sample count must be positive')
    frequencies = [float(item) for item in frequency_hz]
    if (
        not frequencies
        or any(not math.isfinite(item) or item <= 0.0 for item in frequencies)
    ):
        raise ValueError('frequency grid must be finite and positive')

    first = 0.0
    last = float((count - 1) * dt)
    native_end = float(count * dt)
    tolerance = max(1.0e-15, abs(dt) * 1.0e-12)
    if last >= duration + tolerance:
        raise ValueError('native record contains a sample at/after target duration')
    if native_end + tolerance < duration:
        raise ValueError('native record does not cover requested target duration')

    return {
        'solver': solver,
        'requested_duration_s': duration,
        'native_dt_s': dt,
        'generated_sample_count': count,
        'actual_first_sample_time_s': first,
        'actual_last_sample_time_s': last,
        'canonical_effective_integration_interval_s': [first, native_end],
        'canonical_endpoint_convention': (
            'sample timestamps are t_n=n*dt with t_n<T; every native sample '
            'receives a full dt left-rectangle weight'
        ),
        'actual_n_dt_s': native_end,
        'n_dt_minus_requested_duration_s': native_end - duration,
        'target_effective_integration_interval_s': [first, duration],
        'target_endpoint_convention': (
            'exact [0,T); final left-rectangle cell is clipped at T and no '
            'sample at T is included'
        ),
        'phasor_convention': 'exp(-i*omega*t)',
        'analysis_fourier_kernel': 'exp(+i*omega*t)',
        'canonical_rectangular_weighting_rule': (
            'dt * sum_n y[n] * exp(+i*2*pi*f*n*dt)'
        ),
        'target_window_weighting_rule': (
            'sum_n Delta_t[n] * y[n] * exp(+i*2*pi*f*n*dt), '
            'Delta_t[n]=min(dt,T-n*dt)'
        ),
        'source_q_t_sampling': source_sampling,
        'pressure_p_t_sampling': pressure_sampling,
        'frequency_evaluation_rule': (
            'direct evaluation at exact requested frequencies; no FFT-bin '
            'rounding, masking change, shift, or fit'
        ),
        'frequency_hz': frequencies,
    }


def native_window_left_rectangle_spectrum(
    samples: Sequence[complex] | np.ndarray,
    *,
    dt_s: float,
    frequency_hz: Sequence[float] | np.ndarray,
) -> np.ndarray:
    values = np.asarray(samples)
    if values.ndim != 1 or values.size < 1:
        raise ValueError('native-window spectrum requires a non-empty 1D trace')
    if not (
        np.all(np.isfinite(values.real))
        and np.all(np.isfinite(values.imag))
    ):
        raise ValueError('native-window trace must be finite')
    dt = float(dt_s)
    frequencies = np.asarray(frequency_hz, dtype=np.float64)
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError('native-window dt must be finite and positive')
    if (
        frequencies.ndim != 1
        or frequencies.size == 0
        or not np.all(np.isfinite(frequencies))
        or np.any(frequencies <= 0.0)
    ):
        raise ValueError('native-window frequencies must be finite and positive')
    times = np.arange(values.size, dtype=np.float64) * dt
    kernel = np.exp(
        2j * np.pi * frequencies[:, None] * times[None, :]
    )
    spectrum = dt * (kernel @ values.astype(np.complex128, copy=False))
    if not (
        np.all(np.isfinite(spectrum.real))
        and np.all(np.isfinite(spectrum.imag))
    ):
        raise ValueError('native-window spectrum is non-finite')
    return np.asarray(spectrum, dtype=np.complex128)


def native_window_left_rectangle_transfer(
    pressure_trace: Sequence[complex] | np.ndarray,
    source_volume_velocity_trace: Sequence[complex] | np.ndarray,
    *,
    dt_s: float,
    frequency_hz: Sequence[float] | np.ndarray,
) -> np.ndarray:
    pressure = np.asarray(pressure_trace)
    source = np.asarray(source_volume_velocity_trace)
    if pressure.ndim != 1 or source.ndim != 1 or pressure.shape != source.shape:
        raise ValueError(
            'native-window P/Q requires matching 1D pressure/source traces'
        )
    p_spectrum = native_window_left_rectangle_spectrum(
        pressure, dt_s=dt_s, frequency_hz=frequency_hz
    )
    q_spectrum = native_window_left_rectangle_spectrum(
        source, dt_s=dt_s, frequency_hz=frequency_hz
    )
    source_floor = np.finfo(np.float64).eps * max(
        1.0, float(np.max(np.abs(q_spectrum)))
    )
    if np.any(np.abs(q_spectrum) <= source_floor):
        raise ValueError('native-window physical source spectrum is zero')
    transfer = p_spectrum / q_spectrum
    if not (
        np.all(np.isfinite(transfer.real))
        and np.all(np.isfinite(transfer.imag))
    ):
        raise ValueError('native-window transfer is non-finite')
    return np.asarray(transfer, dtype=np.complex128)


def analytic_sampled_complex_harmonic_left_rectangle_spectrum(
    *,
    amplitude: complex,
    harmonic_frequency_hz: float,
    analysis_frequency_hz: Sequence[float] | np.ndarray,
    dt_s: float,
    sample_count: int,
) -> np.ndarray:
    frequencies = np.asarray(analysis_frequency_hz, dtype=np.float64)
    dt = float(dt_s)
    count = int(sample_count)
    harmonic = float(harmonic_frequency_hz)
    if not math.isfinite(dt) or dt <= 0.0 or count < 1:
        raise ValueError('analytic sampled harmonic requires positive dt/count')
    delta = frequencies - harmonic
    phase_step = np.exp(2j * np.pi * delta * dt)
    denominator = 1.0 - phase_step
    near = np.abs(denominator) <= 1.0e-13
    series = np.empty(frequencies.shape, dtype=np.complex128)
    series[near] = float(count)
    if np.any(~near):
        series[~near] = (
            1.0 - np.power(phase_step[~near], count)
        ) / denominator[~near]
    return complex(amplitude) * dt * series


def target_window_clipped_left_rectangle_spectrum(
    samples: Sequence[complex] | np.ndarray,
    *,
    dt_s: float,
    target_duration_s: float,
    frequency_hz: Sequence[float] | np.ndarray,
) -> np.ndarray:
    values = np.asarray(samples)
    if values.ndim != 1 or values.size < 1:
        raise ValueError('target-window clipped left-rectangle spectrum requires a non-empty 1D trace')
    if not (
        np.all(np.isfinite(values.real))
        and np.all(np.isfinite(values.imag))
    ):
        raise ValueError('target-window clipped left-rectangle trace must be finite')

    dt = float(dt_s)
    duration = float(target_duration_s)
    frequencies = np.asarray(frequency_hz, dtype=np.float64)
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError('target-window clipped left-rectangle dt must be finite and positive')
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError('target-window clipped left-rectangle duration must be finite and positive')
    if (
        frequencies.ndim != 1
        or frequencies.size == 0
        or not np.all(np.isfinite(frequencies))
        or np.any(frequencies <= 0.0)
    ):
        raise ValueError('target-window clipped left-rectangle frequencies must be finite and positive')

    coverage_end = float(values.size * dt)
    tolerance = max(1.0e-15, abs(dt) * 1.0e-12)
    if coverage_end + tolerance < duration:
        raise ValueError('target-window clipped left-rectangle trace does not cover target duration')

    starts = np.arange(values.size, dtype=np.float64) * dt
    active = starts < duration
    starts = starts[active]
    widths = np.minimum(dt, duration - starts)
    active_values = values[active].astype(np.complex128, copy=False)
    kernel = np.exp(
        2j * np.pi * frequencies[:, None] * starts[None, :]
    )
    spectrum = (kernel * widths[None, :]) @ active_values
    if not (
        np.all(np.isfinite(spectrum.real))
        and np.all(np.isfinite(spectrum.imag))
    ):
        raise ValueError('target-window clipped left-rectangle spectrum is non-finite')
    return np.asarray(spectrum, dtype=np.complex128)


def target_window_clipped_left_rectangle_transfer(
    pressure_trace: Sequence[complex] | np.ndarray,
    source_volume_velocity_trace: Sequence[complex] | np.ndarray,
    *,
    dt_s: float,
    target_duration_s: float,
    frequency_hz: Sequence[float] | np.ndarray,
) -> np.ndarray:
    pressure = np.asarray(pressure_trace)
    source = np.asarray(source_volume_velocity_trace)
    if pressure.ndim != 1 or source.ndim != 1 or pressure.shape != source.shape:
        raise ValueError(
            'target-window P/Q requires matching 1D pressure/source traces'
        )
    p_spectrum = target_window_clipped_left_rectangle_spectrum(
        pressure,
        dt_s=dt_s,
        target_duration_s=target_duration_s,
        frequency_hz=frequency_hz,
    )
    q_spectrum = target_window_clipped_left_rectangle_spectrum(
        source,
        dt_s=dt_s,
        target_duration_s=target_duration_s,
        frequency_hz=frequency_hz,
    )
    source_floor = np.finfo(np.float64).eps * max(
        1.0, float(np.max(np.abs(q_spectrum)))
    )
    if np.any(np.abs(q_spectrum) <= source_floor):
        raise ValueError('target-window physical source spectrum is zero')
    transfer = p_spectrum / q_spectrum
    if not (
        np.all(np.isfinite(transfer.real))
        and np.all(np.isfinite(transfer.imag))
    ):
        raise ValueError('target-window transfer is non-finite')
    return np.asarray(transfer, dtype=np.complex128)


def analytic_complex_harmonic_spectrum(
    *,
    amplitude: complex,
    harmonic_frequency_hz: float,
    analysis_frequency_hz: Sequence[float] | np.ndarray,
    duration_s: float,
) -> np.ndarray:
    frequencies = np.asarray(analysis_frequency_hz, dtype=np.float64)
    duration = float(duration_s)
    harmonic = float(harmonic_frequency_hz)
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError('analytic harmonic duration must be finite and positive')
    if not math.isfinite(harmonic):
        raise ValueError('analytic harmonic frequency must be finite')
    delta = frequencies - harmonic
    output = np.empty(frequencies.shape, dtype=np.complex128)
    near = np.isclose(delta, 0.0, rtol=0.0, atol=1.0e-14)
    output[near] = complex(amplitude) * duration
    if np.any(~near):
        d = delta[~near]
        output[~near] = complex(amplitude) * (
            np.exp(2j * np.pi * d * duration) - 1.0
        ) / (2j * np.pi * d)
    return output


class FixtureContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    fixture_id: str = Field(min_length=1)
    source_key: Literal['sloped']
    geometry_kind: Literal['explicit_polyhedral']
    vertices_m: tuple[tuple[float, float, float], ...] = Field(min_length=4)
    faces: tuple[tuple[str, tuple[int, ...]], ...] = Field(min_length=4)
    base_tetrahedra: tuple[tuple[int, int, int, int], ...] = Field(min_length=1)
    base_tetrahedralization_volume_m3: float = Field(gt=0.0)
    source_position_m: tuple[float, float, float]
    receiver_position_m: tuple[float, float, float]
    boundary_model: Literal['natural_neumann_rigid']
    density_kg_m3: float = Field(gt=0.0)
    sound_speed_m_s: float = Field(gt=0.0)

    @model_validator(mode='after')
    def validate_indices(self) -> 'FixtureContract':
        n = len(self.vertices_m)
        for _, loop in self.faces:
            if len(loop) < 3 or any(index < 0 or index >= n for index in loop):
                raise ValueError('fixture face index is invalid')
        for tet in self.base_tetrahedra:
            if len(set(tet)) != 4 or any(index < 0 or index >= n for index in tet):
                raise ValueError('fixture tetrahedron index is invalid')
        return self


class PhysicalQuantityContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    quantity: Literal['finite_record_complex_acoustic_pressure_per_volume_velocity']
    unit: Literal['Pa/(m3/s)']
    record_interval: Literal['[0,T)']
    duration_s: float = Field(gt=0.0)
    frequency_hz: tuple[float, ...] = Field(min_length=2)
    phasor_convention: Literal['exp(-i*omega*t)']
    analysis_fourier_kernel: Literal['exp(+i*omega*t)']
    source_contract: str = Field(min_length=1)
    pffdtd_artifact_normalization: str = Field(min_length=1)
    mfem_initial_condition: str = Field(min_length=1)
    pressure_conversion: str = Field(min_length=1)
    normalization_claim: str = Field(min_length=1)
    window_function: Literal['rectangular_no_taper'] = 'rectangular_no_taper'
    frequency_bin_policy: str = 'arbitrary finite-record evaluation frequencies'
    source_normalization: str = 'unit discrete volume-velocity impulse'
    receiver_observable: str = 'point acoustic pressure'
    geometry_units: Literal['m'] = 'm'

    @model_validator(mode='after')
    def validate_frequency_axis(self) -> 'PhysicalQuantityContract':
        if tuple(self.frequency_hz) != tuple(sorted(set(self.frequency_hz))):
            raise ValueError('comparison frequencies must be sorted and unique')
        if any(item <= 0.0 or not math.isfinite(item) for item in self.frequency_hz):
            raise ValueError('comparison frequencies must be finite and positive')
        if self.frequency_bin_policy == 'record_coherent_integer_cycles':
            for frequency in self.frequency_hz:
                cycles = float(frequency) * float(self.duration_s)
                if not math.isclose(cycles, round(cycles), rel_tol=0.0, abs_tol=1.0e-12):
                    raise ValueError(
                        'record-coherent comparison requires an integer cycle count'
                    )
        return self


class IndependentReferenceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    solver: Literal['MFEM']
    source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    formulation: str = Field(min_length=1)
    spatial_discretization: str = Field(min_length=1)
    pffdtd_voxel_or_triangle_intersection_reuse: Literal[False] = False
    polynomial_order: int = Field(ge=1)
    uniform_refinements: tuple[int, ...] = Field(min_length=3)
    expected_element_counts: tuple[int, ...] = Field(min_length=3)
    expected_dofs: tuple[int, ...] | None = None
    boundary_condition: str = Field(min_length=1)
    mass_assembly: str = Field(min_length=1)
    stiffness_assembly: str = Field(min_length=1)
    source_functional: str = Field(min_length=1)
    receiver_functional: str = Field(min_length=1)
    modal_solver: str = Field(min_length=1)
    modal_numpy_version: str = Field(min_length=1)
    modal_scipy_version: str = Field(min_length=1)
    modal_sample_rate_hz: int = Field(ge=1)
    mass_orthonormality_max_abs_tolerance: float = Field(gt=0.0)
    generalized_eigen_residual_relative_tolerance: float = Field(gt=0.0)

    @model_validator(mode='after')
    def validate_schedule(self) -> 'IndependentReferenceContract':
        legacy = ((0, 1, 2), (6, 48, 384))
        diagnosed = ((1, 2, 3), (48, 384, 3072))
        schedule = (self.uniform_refinements, self.expected_element_counts)
        if schedule not in (legacy, diagnosed):
            raise ValueError(
                'MFEM refinement schedule must be the PR #282 legacy series '
                'or the predeclared diagnosed 1/2/3 series'
            )
        if self.uniform_refinements == diagnosed[0]:
            if self.expected_dofs != (125, 729, 4913):
                raise ValueError('diagnosed MFEM DOF schedule is frozen to 125/729/4913')
        elif self.expected_dofs is not None and len(self.expected_dofs) != 3:
            raise ValueError('legacy MFEM expected_dofs must have three entries when set')
        return self


class PffdtdContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    backend: Literal['Python/Numba CPU']
    points_per_wavelength: tuple[float, ...] = Field(min_length=3)
    fmax_hz: float = Field(gt=0.0)
    max_grid_cells: int = Field(ge=1)
    max_time_steps: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1)
    max_solver_wall_seconds: float = Field(gt=0.0)
    solver_threads: int = Field(ge=1)
    setup_processes: int = Field(ge=1)

    @model_validator(mode='after')
    def validate_schedule(self) -> 'PffdtdContract':
        if self.points_per_wavelength not in (
            (6.0, 8.0, 10.0),
            (8.0, 10.0, 12.0),
        ):
            raise ValueError(
                'PFFDTD refinement schedule must be PR #282 legacy 6/8/10 '
                'or the predeclared diagnosed 8/10/12 series'
            )
        return self


class MetricThreshold(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    complex_rms_relative_max: float = Field(gt=0.0)
    magnitude_max_relative: float = Field(gt=0.0)
    phase_max_deg: float = Field(gt=0.0)
    magnitude_max_db: float | None = Field(default=None, gt=0.0)


class AcceptanceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    magnitude_mask_relative_db: float
    reference_self_convergence: MetricThreshold
    pffdtd_self_convergence: MetricThreshold
    cross_solver_fine_fine: MetricThreshold
    rules: tuple[str, ...] = Field(min_length=1)


class ResourceCeiling(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    workflow_timeout_minutes: int = Field(ge=1)
    mfem_build_parallelism: int = Field(ge=1)
    max_reference_wall_seconds_per_level: float = Field(gt=0.0)
    max_reference_peak_ram_mb: float = Field(gt=0.0)
    max_total_evidence_mb: float = Field(gt=0.0)


class ScopeNonClaims(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    production_solver_selected: Literal[False] = False
    concave_validated: Literal[False] = False
    multi_region_validated: Literal[False] = False
    portal_validated: Literal[False] = False
    owned_room_evidence: Literal[False] = False
    gpu_validated: Literal[False] = False


class R130DGeneral3DValidationPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal['htdt.r130d.general3d-validation-plan-1'] = PLAN_SCHEMA
    plan_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    fixture: FixtureContract
    physical_quantity: PhysicalQuantityContract
    independent_reference: IndependentReferenceContract
    pffdtd: PffdtdContract
    acceptance: AcceptanceContract
    resource_ceiling: ResourceCeiling
    scope_nonclaims: ScopeNonClaims

    def plan_sha256(self) -> str:
        return semantic_hash(self.model_dump(mode='json'))

    def fixture_sha256(self) -> str:
        return semantic_hash(self.fixture.model_dump(mode='json'))

    def reference_mesh_sha256(self, refinement: int) -> str:
        if refinement not in self.independent_reference.uniform_refinements:
            raise ValueError('reference refinement is not predeclared')
        return semantic_hash(
            {
                'fixture_sha256': self.fixture_sha256(),
                'base_tetrahedra': self.fixture.base_tetrahedra,
                'uniform_refinement': refinement,
                'polynomial_order': self.independent_reference.polynomial_order,
                'mesh_algorithm': 'MFEM uniform tetra refinement',
                'mfem_source_commit_sha': self.independent_reference.source_commit_sha,
            }
        )


def load_validation_plan(path: str | Path) -> R130DGeneral3DValidationPlan:
    return R130DGeneral3DValidationPlan.model_validate_json(
        Path(path).read_text(encoding='utf-8')
    )


def validate_exact_binding(
    plan: R130DGeneral3DValidationPlan,
    *,
    vertices_m: Sequence[Sequence[float]],
    faces: Sequence[tuple[str, Sequence[int]]],
    source_position_m: Sequence[float],
    receiver_position_m: Sequence[float],
    quantity: str,
    unit: str,
    phasor_convention: str,
    analysis_fourier_kernel: str,
    pffdtd_source_commit_sha: str,
    independent_source_commit_sha: str,
) -> None:
    expected_faces = tuple((key, tuple(loop)) for key, loop in plan.fixture.faces)
    actual_faces = tuple((str(key), tuple(int(i) for i in loop)) for key, loop in faces)
    checks: tuple[tuple[str, object, object], ...] = (
        (
            'vertices',
            tuple(tuple(float(x) for x in row) for row in vertices_m),
            plan.fixture.vertices_m,
        ),
        ('faces', actual_faces, expected_faces),
        (
            'source_position_m',
            tuple(float(x) for x in source_position_m),
            plan.fixture.source_position_m,
        ),
        (
            'receiver_position_m',
            tuple(float(x) for x in receiver_position_m),
            plan.fixture.receiver_position_m,
        ),
        ('quantity', quantity, plan.physical_quantity.quantity),
        ('unit', unit, plan.physical_quantity.unit),
        (
            'phasor_convention',
            phasor_convention,
            plan.physical_quantity.phasor_convention,
        ),
        (
            'analysis_fourier_kernel',
            analysis_fourier_kernel,
            plan.physical_quantity.analysis_fourier_kernel,
        ),
        (
            'pffdtd_source_commit_sha',
            pffdtd_source_commit_sha,
            plan.pffdtd.source_commit_sha,
        ),
        (
            'independent_source_commit_sha',
            independent_source_commit_sha,
            plan.independent_reference.source_commit_sha,
        ),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise ValueError(f'R130D validation binding mismatch for {name}')


def validate_refinement_schedule(
    plan: R130DGeneral3DValidationPlan,
    *,
    reference_refinements: Sequence[int],
    pffdtd_points_per_wavelength: Sequence[float],
) -> None:
    if tuple(int(x) for x in reference_refinements) != (
        plan.independent_reference.uniform_refinements
    ):
        raise ValueError('reference refinement schedule differs from predeclared plan')
    if tuple(float(x) for x in pffdtd_points_per_wavelength) != (
        plan.pffdtd.points_per_wavelength
    ):
        raise ValueError('PFFDTD refinement schedule differs from predeclared plan')


class ObservableContractMismatch(ValueError):
    pass


def validate_physical_observable_contract(
    *,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> None:
    required = (
        'quantity',
        'unit',
        'source_position_m',
        'receiver_position_m',
        'source_convention',
        'pressure_normalization',
        'excitation_normalization',
        'phasor_convention',
        'analysis_fourier_kernel',
        'record_duration_s',
        'record_interval',
        'window_function',
        'frequency_hz',
        'sound_speed_m_s',
        'density_kg_m3',
        'boundary_condition',
        'geometry_sha256',
        'geometry_units',
    )
    for name in required:
        if name not in expected or name not in actual:
            raise ObservableContractMismatch(
                f'R130D physical observable contract missing {name}'
            )
        left = expected[name]
        right = actual[name]
        if isinstance(left, float) or isinstance(right, float):
            try:
                equal = math.isclose(
                    float(left), float(right), rel_tol=0.0, abs_tol=1.0e-12
                )
            except (TypeError, ValueError):
                equal = False
        else:
            equal = left == right
        if not equal:
            raise ObservableContractMismatch(
                f'R130D physical observable contract mismatch for {name}: '
                f'{right!r} != {left!r}'
            )


class PairMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    complex_rms_relative: float = Field(ge=0.0)
    magnitude_max_relative: float = Field(ge=0.0)
    magnitude_max_db: float = Field(ge=0.0)
    phase_max_deg: float = Field(ge=0.0)
    mask_floor: float = Field(ge=0.0)
    compared_frequency_count: int = Field(ge=1)
    frequency_metrics: tuple[dict[str, float | bool], ...] = Field(min_length=1)


def _as_complex(values: Sequence[Sequence[float]]) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError('complex values must be [[real, imag], ...]')
    if not np.all(np.isfinite(array)):
        raise ValueError('complex values must be finite')
    return array[:, 0] + 1j * array[:, 1]


def compare_complex_transfer(
    *,
    reference: Sequence[Sequence[float]],
    candidate: Sequence[Sequence[float]],
    frequency_hz: Sequence[float],
    magnitude_mask_relative_db: float,
) -> PairMetrics:
    ref = _as_complex(reference)
    cand = _as_complex(candidate)
    freq = np.asarray(frequency_hz, dtype=np.float64)
    if ref.shape != cand.shape or ref.shape != freq.shape:
        raise ValueError('comparison axes do not match')
    ref_mag = np.abs(ref)
    mask_floor = float(np.max(ref_mag) * (10.0 ** (magnitude_mask_relative_db / 20.0)))
    floor = max(mask_floor, np.finfo(np.float64).tiny)
    mask = ref_mag >= floor
    if not np.any(mask):
        raise ValueError('magnitude mask removed every comparison frequency')

    delta = cand - ref
    rms = float(
        np.linalg.norm(delta[mask])
        / max(float(np.linalg.norm(ref[mask])), np.finfo(np.float64).tiny)
    )
    mag_rel = np.abs(np.abs(cand) - ref_mag) / np.maximum(ref_mag, floor)
    ratio = np.maximum(np.abs(cand), floor) / np.maximum(ref_mag, floor)
    mag_db = np.abs(20.0 * np.log10(ratio))
    phase = np.angle(cand / ref, deg=True)
    phase = np.abs((phase + 180.0) % 360.0 - 180.0)

    per_frequency = tuple(
        {
            'frequency_hz': float(freq[index]),
            'masked_in': bool(mask[index]),
            'magnitude_absolute': float(abs(abs(cand[index]) - ref_mag[index])),
            'magnitude_relative': float(mag_rel[index]),
            'magnitude_db': float(mag_db[index]),
            'phase_deg': float(phase[index]),
            'complex_relative': float(
                abs(delta[index]) / max(abs(ref[index]), floor)
            ),
        }
        for index in range(freq.size)
    )
    return PairMetrics(
        complex_rms_relative=rms,
        magnitude_max_relative=float(np.max(mag_rel[mask])),
        magnitude_max_db=float(np.max(mag_db[mask])),
        phase_max_deg=float(np.max(phase[mask])),
        mask_floor=mask_floor,
        compared_frequency_count=int(np.count_nonzero(mask)),
        frequency_metrics=per_frequency,
    )


ValidationStatus = Literal['PASS', 'FAIL', 'BLOCKED']


def metric_status(metrics: PairMetrics, threshold: MetricThreshold) -> ValidationStatus:
    if metrics.complex_rms_relative > threshold.complex_rms_relative_max:
        return 'FAIL'
    if metrics.magnitude_max_relative > threshold.magnitude_max_relative:
        return 'FAIL'
    if metrics.phase_max_deg > threshold.phase_max_deg:
        return 'FAIL'
    if (
        threshold.magnitude_max_db is not None
        and metrics.magnitude_max_db > threshold.magnitude_max_db
    ):
        return 'FAIL'
    return 'PASS'


class SelfConvergenceAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    state: Literal['SELF_CONVERGENCE_PASS', 'SELF_CONVERGENCE_FAILED']
    pair_count: int = Field(ge=2)
    final_pair_status: Literal['PASS', 'FAIL']
    complex_rms_trend: Literal['DECREASING', 'NON_DECREASING']
    magnitude_relative_trend: Literal['DECREASING', 'NON_DECREASING']
    phase_trend: Literal['DECREASING', 'NON_DECREASING']
    pair_metrics: tuple[PairMetrics, ...] = Field(min_length=2)


def _decreasing(values: Sequence[float]) -> bool:
    numeric = [float(value) for value in values]
    tolerance = 1.0e-12
    return (
        len(numeric) >= 2
        and all(
            following <= previous * (1.0 + tolerance) + tolerance
            for previous, following in zip(numeric, numeric[1:])
        )
        and numeric[-1] < numeric[0]
    )


def assess_refinement_series(
    metrics: Sequence[PairMetrics],
    threshold: MetricThreshold,
) -> SelfConvergenceAssessment:
    pairs = tuple(metrics)
    if len(pairs) < 2:
        raise ValueError(
            'self-convergence requires at least two adjacent comparisons '
            'from three predeclared levels'
        )
    rms_decreasing = _decreasing([item.complex_rms_relative for item in pairs])
    magnitude_decreasing = _decreasing(
        [item.magnitude_max_relative for item in pairs]
    )
    phase_decreasing = _decreasing([item.phase_max_deg for item in pairs])
    final_status = metric_status(pairs[-1], threshold)
    passed = bool(
        final_status == 'PASS'
        and rms_decreasing
        and magnitude_decreasing
        and phase_decreasing
    )
    return SelfConvergenceAssessment(
        state='SELF_CONVERGENCE_PASS' if passed else 'SELF_CONVERGENCE_FAILED',
        pair_count=len(pairs),
        final_pair_status=final_status,
        complex_rms_trend='DECREASING' if rms_decreasing else 'NON_DECREASING',
        magnitude_relative_trend=(
            'DECREASING' if magnitude_decreasing else 'NON_DECREASING'
        ),
        phase_trend='DECREASING' if phase_decreasing else 'NON_DECREASING',
        pair_metrics=pairs,
    )


def validation_decision_v2(
    *,
    execution_state: Literal['PASS', 'EXECUTION_FAILED'],
    contract_state: Literal['MATCH', 'CONTRACT_MISMATCH'],
    reference_assessment: SelfConvergenceAssessment | None,
    pffdtd_assessment: SelfConvergenceAssessment | None,
    cross_solver_metrics: PairMetrics | None,
    plan: R130DGeneral3DValidationPlan,
) -> dict[str, str]:
    if execution_state != 'PASS':
        return {
            'execution_state': 'EXECUTION_FAILED',
            'contract_state': contract_state,
            'reference_self_convergence_state': 'NOT_EVALUATED',
            'pffdtd_self_convergence_state': 'NOT_EVALUATED',
            'cross_solver_state': 'CROSS_SOLVER_BLOCKED',
            'validation_state': 'NOT_VALIDATED',
            'reference_build_execution_status': 'FAIL',
            'reference_self_convergence_status': 'BLOCKED',
            'pffdtd_self_convergence_status': 'BLOCKED',
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'BLOCKED',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if contract_state != 'MATCH':
        return {
            'execution_state': 'PASS',
            'contract_state': 'CONTRACT_MISMATCH',
            'reference_self_convergence_state': (
                reference_assessment.state
                if reference_assessment is not None
                else 'NOT_EVALUATED'
            ),
            'pffdtd_self_convergence_state': (
                pffdtd_assessment.state
                if pffdtd_assessment is not None
                else 'NOT_EVALUATED'
            ),
            'cross_solver_state': 'CROSS_SOLVER_BLOCKED',
            'validation_state': 'NOT_VALIDATED',
            'reference_build_execution_status': 'PASS',
            'reference_self_convergence_status': (
                'PASS'
                if reference_assessment is not None
                and reference_assessment.state == 'SELF_CONVERGENCE_PASS'
                else 'BLOCKED'
            ),
            'pffdtd_self_convergence_status': (
                'PASS'
                if pffdtd_assessment is not None
                and pffdtd_assessment.state == 'SELF_CONVERGENCE_PASS'
                else 'BLOCKED'
            ),
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'BLOCKED',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if reference_assessment is None or pffdtd_assessment is None:
        raise ValueError('successful execution requires both self-convergence assessments')

    reference_pass = reference_assessment.state == 'SELF_CONVERGENCE_PASS'
    pffdtd_pass = pffdtd_assessment.state == 'SELF_CONVERGENCE_PASS'
    if not reference_pass or not pffdtd_pass:
        return {
            'execution_state': 'PASS',
            'contract_state': 'MATCH',
            'reference_self_convergence_state': reference_assessment.state,
            'pffdtd_self_convergence_state': pffdtd_assessment.state,
            'cross_solver_state': 'CROSS_SOLVER_BLOCKED',
            'validation_state': 'NOT_VALIDATED',
            'reference_build_execution_status': 'PASS',
            'reference_self_convergence_status': 'PASS' if reference_pass else 'FAIL',
            'pffdtd_self_convergence_status': 'PASS' if pffdtd_pass else 'FAIL',
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'FAIL',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if cross_solver_metrics is None:
        raise ValueError(
            'cross-solver metrics are required only after both self-convergence gates pass'
        )
    cross_pass = (
        metric_status(cross_solver_metrics, plan.acceptance.cross_solver_fine_fine)
        == 'PASS'
    )
    return {
        'execution_state': 'PASS',
        'contract_state': 'MATCH',
        'reference_self_convergence_state': reference_assessment.state,
        'pffdtd_self_convergence_state': pffdtd_assessment.state,
        'cross_solver_state': (
            'CROSS_SOLVER_PASS' if cross_pass else 'CROSS_SOLVER_FAILED'
        ),
        'validation_state': 'VALIDATED' if cross_pass else 'NOT_VALIDATED',
        'reference_build_execution_status': 'PASS',
        'reference_self_convergence_status': 'PASS',
        'pffdtd_self_convergence_status': 'PASS',
        'cross_solver_agreement_status': 'PASS' if cross_pass else 'FAIL',
        'fixture_validation_result': 'PASS' if cross_pass else 'FAIL',
        'general_3d_validation_state': 'VALIDATED' if cross_pass else 'NOT_VALIDATED',
    }


def validation_decision(
    *,
    execution_status: ValidationStatus,
    reference_metrics: PairMetrics | None,
    pffdtd_metrics: PairMetrics | None,
    cross_solver_metrics: PairMetrics | None,
    plan: R130DGeneral3DValidationPlan,
) -> dict[str, str]:
    if execution_status != 'PASS':
        return {
            'reference_build_execution_status': execution_status,
            'reference_self_convergence_status': 'BLOCKED',
            'pffdtd_self_convergence_status': 'BLOCKED',
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'BLOCKED',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if reference_metrics is None or pffdtd_metrics is None:
        return {
            'reference_build_execution_status': 'PASS',
            'reference_self_convergence_status': 'BLOCKED',
            'pffdtd_self_convergence_status': 'BLOCKED',
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'BLOCKED',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }

    reference_status = metric_status(
        reference_metrics, plan.acceptance.reference_self_convergence
    )
    pffdtd_status = metric_status(
        pffdtd_metrics, plan.acceptance.pffdtd_self_convergence
    )
    if reference_status != 'PASS' or pffdtd_status != 'PASS':
        return {
            'reference_build_execution_status': 'PASS',
            'reference_self_convergence_status': reference_status,
            'pffdtd_self_convergence_status': pffdtd_status,
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'FAIL',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if cross_solver_metrics is None:
        cross_status: ValidationStatus = 'BLOCKED'
    else:
        cross_status = metric_status(
            cross_solver_metrics, plan.acceptance.cross_solver_fine_fine
        )
    fixture_status: ValidationStatus = (
        'PASS' if cross_status == 'PASS' else ('FAIL' if cross_status == 'FAIL' else 'BLOCKED')
    )
    return {
        'reference_build_execution_status': 'PASS',
        'reference_self_convergence_status': reference_status,
        'pffdtd_self_convergence_status': pffdtd_status,
        'cross_solver_agreement_status': cross_status,
        'fixture_validation_result': fixture_status,
        'general_3d_validation_state': (
            'VALIDATED_BOUNDED_SLOPED_FIXTURE'
            if fixture_status == 'PASS'
            else 'NOT_VALIDATED'
        ),
    }


def save_evidence(path: str | Path, payload: dict[str, Any]) -> str:
    if payload.get('schema_version') != EVIDENCE_SCHEMA:
        raise ValueError('R130D evidence payload schema mismatch')
    digest = semantic_hash(payload)
    envelope = {
        'schema_version': EVIDENCE_ENVELOPE_SCHEMA,
        'semantic_sha256': digest,
        'payload': payload,
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(output, canonical_json(envelope) + '\n')
    return digest


def load_evidence(path: str | Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    if document.get('schema_version') != EVIDENCE_ENVELOPE_SCHEMA:
        raise ValueError('R130D evidence envelope schema mismatch')
    payload = document.get('payload')
    if not isinstance(payload, dict) or payload.get('schema_version') != EVIDENCE_SCHEMA:
        raise ValueError('R130D evidence payload is malformed')
    if document.get('semantic_sha256') != semantic_hash(payload):
        raise ValueError('R130D evidence payload was modified')
    return payload
