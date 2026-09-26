"""Native 3D acoustic field explorer workflow authority (#953 / #517).

#517 closed with the solver-neutral spatial-field backend contract
(:mod:`htdt.cad_spatial_field`) but no Native workflow consumed it: the
``scalar_field`` prediction result kind existed as a declared type with no
producer, no persistence and no explorer surface.

This module is the product bridge. A :class:`FieldExplorerSession` is the
immutable, hash-bound record of one explored field: it pins the exact
SceneRevision (id + content hash), the saved prediction run the field was
derived from, the selected room mode, the volume request spec and the
materialized :class:`SpatialFieldResult` — reopening a session replays the
exact authority, and moving the scene makes every bound session STALE
instead of silently reinterpreting.

Honesty constraints:

- The only producer today is the analytical rigid rectangular-room mode
  field (``build_rectangular_mode_field``): it requires the saved
  ``geometry_modes`` run to be ``exact_for_model_geometry`` — a
  rectangular-approximation or unsupported run never opens a field.
- The mode field is normalized analytical pressure (unit amplitude), not
  an absolute solver or measured level: ``absolute_pressure_reference``
  stays ``False`` so SPL views are gated off by the field contract itself.
- Derived slice/probe views never mutate canonical samples; off-grid
  probes keep the explicit ``nearest_sample``/``interpolated`` states the
  #951 contract defines.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import FrequencyDomain
from .cad_prediction_models import CadPredictionResult
from .cad_predictions import (
    RectangularRoomFrame,
    exact_rectangular_room_frame,
)
from .cad_repository import SceneRevision
from .cad_scene import Position3
from .cad_spatial_field import (
    FieldPlaneRequest,
    FieldProbeValue,
    FieldQuantity,
    FieldSliceView,
    FieldVolumeRequest,
    RegularGridAxis,
    SpatialFieldRequestSpec,
    SpatialFieldResult,
    build_rectangular_mode_field,
    build_spatial_field_request,
    extract_field_slice,
    probe_field,
)

FIELD_EXPLORER_SCHEMA_VERSION = 1
FIELD_EXPLORER_SESSION_AUTHORITY_VERSION = 'field-explorer-session-1'

# The only field producer wired today: the analytical rigid rectangular
# room mode shape behind the saved geometry_modes run.
FIELD_EXPLORER_MODE_FIELD_PRODUCER = 'analytical_rectangular_mode_field'

MAX_FIELD_EXPLORER_SAMPLES = 4_000_000


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class FieldExplorerSession(BaseModel):
    """Immutable record of one explored field bound to one exact run+revision."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = FIELD_EXPLORER_SCHEMA_VERSION
    authority_version: Literal[
        'field-explorer-session-1'
    ] = FIELD_EXPLORER_SESSION_AUTHORITY_VERSION
    session_id: str = Field(
        pattern=r'^field-explorer-session:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    prediction_run_id: str = Field(min_length=1)
    prediction_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)

    producer: Literal[
        'analytical_rectangular_mode_field'
    ] = FIELD_EXPLORER_MODE_FIELD_PRODUCER
    mode_n_x: int = Field(ge=0)
    mode_n_y: int = Field(ge=0)
    mode_n_z: int = Field(ge=0)

    request: SpatialFieldRequestSpec
    result: SpatialFieldResult

    @model_validator(mode='after')
    def validate_session(self) -> 'FieldExplorerSession':
        if (
            self.request.semantic_sha256
            != self.result.request_semantic_sha256
        ):
            raise ValueError(
                'field explorer request/result binding is inconsistent'
            )
        if (
            self.request.frequency_hz != self.result.frequency_hz
        ):
            raise ValueError('field explorer request/result frequency mismatch')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('FieldExplorerSession semantic hash mismatch')
        if self.session_id != f'field-explorer-session:{expected}':
            raise ValueError('FieldExplorerSession id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'session_id', 'semantic_sha256'},
        )


def _mode_by_index(
    result: CadPredictionResult,
    mode_indices: tuple[int, int, int],
) -> tuple[float, int, int, int]:
    n_x, n_y, n_z = mode_indices
    for mode in result.modes:
        if (mode.n_x, mode.n_y, mode.n_z) == mode_indices:
            return mode.frequency_hz, n_x, n_y, n_z
    raise ValueError(
        f'field explorer mode ({n_x},{n_y},{n_z}) is not in the pinned '
        'geometry_modes result'
    )


def _volume_axes(
    frame: RectangularRoomFrame,
    stride_m: float,
) -> tuple[RegularGridAxis, RegularGridAxis, RegularGridAxis]:
    stride = _finite(stride_m, field_name='grid stride')
    axes: list[RegularGridAxis] = []
    for name, origin, extent in (
        ('x_m', frame.origin_x_m, frame.width_m),
        ('y_m', frame.origin_y_m, frame.depth_m),
        ('z_m', 0.0, frame.height_m),
    ):
        count = int(extent / stride) + 1
        if count < 2:
            count = 2
        axes.append(
            RegularGridAxis(
                name=name,  # type: ignore[arg-type]
                origin_m=origin,
                spacing_m=stride,
                count=count,
            )
        )
    total = axes[0].count * axes[1].count * axes[2].count
    if total > MAX_FIELD_EXPLORER_SAMPLES:
        raise ValueError(
            f'field explorer grid exceeds the bounded sample contract: '
            f'{total} > {MAX_FIELD_EXPLORER_SAMPLES}'
        )
    return axes[0], axes[1], axes[2]


def build_mode_field_explorer_session(
    *,
    revision: SceneRevision,
    modes_result: CadPredictionResult,
    mode_indices: tuple[int, int, int],
    stride_m: float,
) -> FieldExplorerSession:
    """Open one explorer session for one saved analytical mode field.

    Fails closed unless ``modes_result`` is a ``geometry_modes`` record of
    the exact rectangular model on ``revision`` — a rectangular
    approximation or an unsupported room never opens a field view.
    """

    if modes_result.result_kind != 'geometry_modes':
        raise ValueError('field explorer requires a geometry_modes result')
    if modes_result.geometry_compatibility != 'exact_for_model_geometry':
        raise ValueError(
            'field explorer requires an exact rectangular model run; '
            f'run is {modes_result.geometry_compatibility}'
        )
    if modes_result.scene_revision_id != revision.revision_id:
        raise ValueError('field explorer run does not pin this SceneRevision')
    if modes_result.scene_content_hash != revision.content_hash:
        raise ValueError('field explorer run is stale for this SceneRevision')
    room = revision.document.room
    if room is None:
        raise ValueError('field explorer requires a room')
    frame = exact_rectangular_room_frame(room)
    if frame is None:
        raise ValueError(
            'field explorer requires an axis-aligned rectangular room'
        )
    frequency_hz, n_x, n_y, n_z = _mode_by_index(modes_result, mode_indices)
    axes = _volume_axes(frame, stride_m)

    request = build_spatial_field_request(
        acoustic_scene_snapshot_id=revision.revision_id,
        acoustic_scene_snapshot_sha256=revision.content_hash,
        source_scenario_id=f'native-prediction-run:{modes_result.run_id}',
        source_scenario_sha256=_digest(
            {
                'kind': 'field-explorer-scenario',
                'run_id': modes_result.run_id,
                'mode_indices': [n_x, n_y, n_z],
                'scene_revision_id': revision.revision_id,
            }
        ),
        solver_result_id=modes_result.prediction_id,
        solver_result_sha256=modes_result.result_sha256,
        provider_id=modes_result.model_id,
        provider_version=modes_result.model_version,
        quantity='pressure_magnitude_pa',
        frequency_hz=frequency_hz,
        volume=FieldVolumeRequest(
            origin=Position3(
                x_m=frame.origin_x_m, y_m=frame.origin_y_m, z_m=0.0
            ),
            size_m=Position3(
                x_m=frame.width_m, y_m=frame.depth_m, z_m=frame.height_m
            ),
            stride_m=stride_m,
        ),
    )
    # The produced authority is single-frequency: express that as the
    # narrowest valid domain the contract permits.
    half_width = max(1e-9, frequency_hz * 1e-9)
    domain = FrequencyDomain(
        minimum_hz=frequency_hz - half_width,
        maximum_hz=frequency_hz + half_width,
    )
    result = build_rectangular_mode_field(
        request=request,
        room_size_m=(frame.width_m, frame.depth_m, frame.height_m),
        mode_indices=(n_x, n_y, n_z),
        axes=axes,
        amplitude_pa=1.0,
        absolute_pressure_reference=False,
        valid_frequency_domain=domain,
    )
    probe = FieldExplorerSession.model_construct(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        prediction_run_id=modes_result.run_id,
        prediction_id=modes_result.prediction_id,
        model_id=modes_result.model_id,
        model_version=modes_result.model_version,
        mode_n_x=n_x,
        mode_n_y=n_y,
        mode_n_z=n_z,
        request=request,
        result=result,
    )
    digest = _digest(probe.semantic_payload())
    return FieldExplorerSession(
        session_id=f'field-explorer-session:{digest}',
        semantic_sha256=digest,
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        prediction_run_id=modes_result.run_id,
        prediction_id=modes_result.prediction_id,
        model_id=modes_result.model_id,
        model_version=modes_result.model_version,
        mode_n_x=n_x,
        mode_n_y=n_y,
        mode_n_z=n_z,
        request=request,
        result=result,
    )


class FieldExplorerCurrency(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    state: Literal['CURRENT', 'STALE']
    stale_reasons: tuple[str, ...] = ()


def field_explorer_session_currency(
    session: FieldExplorerSession,
    revision: SceneRevision,
) -> FieldExplorerCurrency:
    """A session goes stale when its pinned revision is no longer current."""

    reasons: list[str] = []
    if session.scene_revision_id != revision.revision_id:
        reasons.append('scene revision superseded')
    elif session.scene_content_hash != revision.content_hash:
        reasons.append('scene content hash drifted')
    if session.document_id != revision.document_id:
        reasons.append('document mismatch')
    if reasons:
        return FieldExplorerCurrency(
            state='STALE', stale_reasons=tuple(reasons)
        )
    return FieldExplorerCurrency(state='CURRENT', stale_reasons=())


def explorer_quantities(
    session: FieldExplorerSession,
) -> tuple[tuple[FieldQuantity, bool, str | None], ...]:
    """Display-quantity gate: what this field can honestly show."""
    quantities: list[tuple[FieldQuantity, bool, str | None]] = []
    for quantity in ('pressure_magnitude_pa', 'spl_db', 'phase_deg'):
        supported, reason = session.result.supports_quantity(quantity)
        quantities.append((quantity, supported, reason))
    return tuple(quantities)


def explorer_plane_coordinates(
    session: FieldExplorerSession,
    axis_plane: Literal['xy', 'xz', 'yz'],
) -> tuple[float, ...]:
    """Exact grid coordinates along the fixed axis of one plane."""
    fixed_axis = {'xy': 2, 'xz': 1, 'yz': 0}[axis_plane]
    axis = session.result.axes[fixed_axis]
    return tuple(axis.coordinate(i) for i in range(axis.count))


def explorer_slice(
    session: FieldExplorerSession,
    *,
    axis_plane: Literal['xy', 'xz', 'yz'],
    coordinate_m: float,
    quantity: FieldQuantity,
    phase_mask_min_magnitude_pa: float | None = None,
) -> FieldSliceView:
    """Exact-grid slice view for one session — display product only."""
    return extract_field_slice(
        session.result,
        FieldPlaneRequest(
            axis_plane=axis_plane,
            coordinate_m=_finite(
                coordinate_m, field_name='plane coordinate'
            ),
        ),
        quantity,
        phase_mask_min_magnitude_pa=phase_mask_min_magnitude_pa,
    )


def explorer_probe(
    session: FieldExplorerSession,
    *,
    position: Position3,
    quantity: FieldQuantity,
    interpolation: Literal['exact_samples', 'trilinear'] = 'exact_samples',
) -> FieldProbeValue:
    """Probe the session field with explicit exact/nearest/interpolated state."""
    return probe_field(
        session.result,
        position,
        quantity,
        interpolation=interpolation,
    )
