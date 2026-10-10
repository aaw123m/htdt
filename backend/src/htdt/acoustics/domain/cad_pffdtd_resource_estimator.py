from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..services.cad_candidate_wave_execution import (
    CandidateWaveExecutionCancelled,
    CandidateWaveExecutionError,
    CandidateWaveExecutionInput,
    PffdtdCandidateConfiguration,
    PffdtdCandidateWaveExecutor,
)
from ...cad_multifidelity import MultiFidelityAuthorityRef
from ...cad_r140_executor import (
    CpuResourceEstimateRequest,
    DeclaredCpuResourceEstimator,
    ExecutionCancelled,
    ExecutionInvocationContext,
    ExecutionResourceEstimate,
    ExecutionWorkerFailure,
    ExecutionWorkerOutput,
    ResourceQuantity,
)
from ...r120_geometry_compiler import ExactExternalAuthorityRef
from ...canonical_json import canonical_json as _canonical_json, canonical_sha256 as _digest


PFFDTD_RESOURCE_ESTIMATOR_ID = (
    'htdt.r140.pffdtd_python_numba_cpu.resource_estimator'
)
PFFDTD_RESOURCE_ESTIMATOR_VERSION = '1'
PFFDTD_RESOURCE_WORKLOAD_AUTHORITY_VERSION = 'r140-pffdtd-workload-estimate-1'
SUPPORTED_PFFDTD_COMMIT_SHA = 'aa319f6c86517cb95aabfae8656277da62c3ead5'

_FLOAT64_BYTES = 8
_COMPLEX128_BYTES = 16
_INT64_BYTES = 8
_INT32_BYTES = 4
_INT8_BYTES = 1
_BOOL_BYTES = 1
_CARTESIAN_NEIGHBORS = 6
_INTERPOLATION_POINTS = 8
_GRID_OFFSET_CELLS = 3.5
_COURANT_BACKOFF = 0.999
_HDF5_METADATA_RESERVE_PER_FILE = 64 * 1024
_JSON_METADATA_RESERVE = 16 * 1024






class PffdtdResourceComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    component: str = Field(min_length=1)
    bytes: int = Field(ge=0)
    derivation: str = Field(min_length=1)


class PffdtdCandidateWorkloadEstimate(BaseModel):
    """Solver-specific, source-bound workload authority for the pinned CPU path."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r140-pffdtd-workload-estimate-1'
    ] = PFFDTD_RESOURCE_WORKLOAD_AUTHORITY_VERSION
    workload_estimate_id: str = Field(
        pattern=r'^r140-pffdtd-workload:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    candidate_execution_input_id: str
    candidate_execution_input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    runtime_backend_id: str = Field(min_length=1)
    runtime_logical_threads: int = Field(ge=1)
    estimator_algorithm_id: Literal[
        'htdt.r140.pffdtd_python_numba_cpu.resource_estimator'
    ] = PFFDTD_RESOURCE_ESTIMATOR_ID
    estimator_algorithm_version: Literal['1'] = PFFDTD_RESOURCE_ESTIMATOR_VERSION

    geometry_bbox_extent_m: tuple[float, float, float]
    geometry_point_count: int = Field(ge=1)
    geometry_triangle_count: int = Field(ge=4)
    grid_spacing_m: float = Field(gt=0.0)
    grid_shape: tuple[int, int, int]
    grid_cells: int = Field(ge=1)
    time_step_s: float = Field(gt=0.0)
    time_step_count: int = Field(ge=1)
    cell_time_updates: int = Field(ge=1)
    receiver_count: int = Field(ge=1)
    raw_receiver_grid_trace_count: int = Field(ge=1)

    logical_cpu_demand: int = Field(ge=1)
    inner_solver_threads: int = Field(ge=1)
    setup_processes: int = Field(ge=1)
    peak_memory_bytes: ResourceQuantity
    scratch_bytes: ResourceQuantity
    gpu_slots: ResourceQuantity
    vram_bytes: ResourceQuantity
    raw_solver_output_cap_bytes: int = Field(ge=1)

    ram_components: tuple[PffdtdResourceComponent, ...]
    scratch_components: tuple[PffdtdResourceComponent, ...]
    semantics: tuple[str, ...]

    @model_validator(mode='after')
    def validate_identity(self) -> 'PffdtdCandidateWorkloadEstimate':
        expected = _digest(self.semantic_payload())
        if expected != self.semantic_sha256:
            raise ValueError('PFFDTD workload estimate semantic hash mismatch')
        if self.workload_estimate_id != f'r140-pffdtd-workload:{expected}':
            raise ValueError('PFFDTD workload estimate id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'workload_estimate_id', 'semantic_sha256'},
        )

    def authority_ref(self) -> MultiFidelityAuthorityRef:
        return MultiFidelityAuthorityRef(
            authority_kind='pffdtd_resource_workload',
            authority_id=self.workload_estimate_id,
            authority_version=self.authority_version,
            semantic_sha256=self.semantic_sha256,
        )


@dataclass(frozen=True)
class PffdtdCandidateResourceEstimation:
    workload: PffdtdCandidateWorkloadEstimate
    execution_resource_estimate: ExecutionResourceEstimate


def _multifidelity_ref(
    ref: ExactExternalAuthorityRef,
    *,
    authority_kind: str,
) -> MultiFidelityAuthorityRef:
    return MultiFidelityAuthorityRef(
        authority_kind=authority_kind,
        authority_id=ref.authority_id,
        authority_version=ref.authority_version,
        semantic_sha256=ref.semantic_hash_sha256,
    )


def pffdtd_execution_backend_ref(
    authority: CandidateWaveExecutionInput,
) -> MultiFidelityAuthorityRef:
    return _multifidelity_ref(
        authority.solver_implementation_ref,
        authority_kind='solver_implementation',
    )


def pffdtd_execution_configuration_ref(
    authority: CandidateWaveExecutionInput,
) -> MultiFidelityAuthorityRef:
    return _multifidelity_ref(
        authority.solver_configuration_ref,
        authority_kind='solver_configuration',
    )


def pffdtd_candidate_input_ref(
    authority: CandidateWaveExecutionInput,
) -> MultiFidelityAuthorityRef:
    return MultiFidelityAuthorityRef(
        authority_kind='candidate_wave_execution_input',
        authority_id=authority.execution_input_id,
        authority_version=authority.authority_version,
        semantic_sha256=authority.semantic_sha256,
        model_id='PFFDTD-python-numba-cpu',
        model_version=authority.solver_implementation_ref.authority_version,
        fidelity='r130a-candidate',
    )


def _model_geometry(
    model: dict[str, Any],
) -> tuple[tuple[float, float, float], int, int, int]:
    mats = model.get('mats_hash')
    if not isinstance(mats, dict) or not mats:
        raise ValueError('PFFDTD model has no material geometry groups')

    first_points: list[Any] | None = None
    triangle_count = 0
    nonrigid_material_count = 0
    for group_name, group in mats.items():
        if not isinstance(group, dict):
            raise ValueError('PFFDTD material geometry group payload is invalid')
        points = group.get('pts')
        triangles = group.get('tris')
        if not isinstance(points, list) or not points:
            raise ValueError('PFFDTD model has no geometry points')
        if not isinstance(triangles, list):
            raise ValueError('PFFDTD model material group triangles are invalid')
        if first_points is None:
            first_points = points
        elif points != first_points:
            raise ValueError(
                'PFFDTD mixed material groups must share the exact point authority'
            )
        triangle_count += len(triangles)
        if group_name != '_RIGID':
            nonrigid_material_count += 1

    assert first_points is not None
    if triangle_count < 4:
        raise ValueError('PFFDTD model has insufficient triangles')

    normalized: list[tuple[float, float, float]] = []
    for point in first_points:
        if not isinstance(point, (list, tuple)) or len(point) != 3:
            raise ValueError('PFFDTD model point is not 3D')
        xyz = tuple(float(value) for value in point)
        if not all(math.isfinite(value) for value in xyz):
            raise ValueError('PFFDTD model point must be finite')
        normalized.append(xyz)

    mins = tuple(min(point[axis] for point in normalized) for axis in range(3))
    maxs = tuple(max(point[axis] for point in normalized) for axis in range(3))
    extents = tuple(maxs[axis] - mins[axis] for axis in range(3))
    if any(value <= 0.0 for value in extents):
        raise ValueError('PFFDTD model geometry bbox must have positive extent')
    return (
        extents,
        len(normalized),
        triangle_count,
        nonrigid_material_count,
    )


def _grid_shape(
    extents: tuple[float, float, float],
    *,
    grid_spacing_m: float,
) -> tuple[int, int, int]:
    offset = _GRID_OFFSET_CELLS * grid_spacing_m
    return tuple(
        int(
            math.ceil(
                ((extent + 2.0 * offset) / grid_spacing_m)
            )
        )
        + 1
        for extent in extents
    )


def _voxel_grid_upper_bound(
    *,
    grid_shape: tuple[int, int, int],
    grid_cells: int,
    triangle_count: int,
    geometry_volume_m3: float,
    grid_spacing_m: float,
) -> tuple[int, int, int]:
    """Return (Nh, voxel_count, total voxel point count).

    This reproduces the pinned VoxGrid default sizing, then sums every voxel
    point-shape. Treating every voxel as non-empty is conservative for scratch.
    """
    nvox_est = int(math.ceil(0.025 * math.sqrt(triangle_count * grid_cells)))
    if nvox_est <= 0:
        raise ValueError('PFFDTD voxel estimate is non-positive')
    if nvox_est == 1:
        nh = max(grid_shape) - 1
    else:
        voxel_side = (geometry_volume_m3 / nvox_est) ** (1.0 / 3.0)
        nh = max(int(round(voxel_side / grid_spacing_m)), 4)
    if nh <= 3:
        raise ValueError('PFFDTD voxel size is outside pinned source contract')

    counts = tuple(int(math.floor((dim - 2) / nh)) for dim in grid_shape)
    if any(value <= 0 for value in counts):
        raise ValueError('PFFDTD voxel grid has no voxels')

    total_points = 0
    nx, ny, nz = grid_shape
    for ix in range(counts[0]):
        sx = (nh + 2) if ix < counts[0] - 1 else nx - ix * nh
        for iy in range(counts[1]):
            sy = (nh + 2) if iy < counts[1] - 1 else ny - iy * nh
            for iz in range(counts[2]):
                sz = (nh + 2) if iz < counts[2] - 1 else nz - iz * nh
                total_points += sx * sy * sz
    return nh, math.prod(counts), total_points


def _ram_components(
    *,
    grid_shape: tuple[int, int, int],
    grid_cells: int,
    time_steps: int,
    receiver_count: int,
    frequency_count: int,
    triangle_count: int,
    point_count: int,
    setup_processes: int,
    nonrigid_material_count: int,
) -> tuple[tuple[PffdtdResourceComponent, ...], ResourceQuantity]:
    nx, ny, nz = grid_shape
    n_axis = nx + ny + nz
    nr = _INTERPOLATION_POINTS * receiver_count
    nba = (
        2 * (nx * ny + nx * nz + ny * nz)
        - 12 * (nx + ny + nz)
        + 56
    )
    if nba < 0:
        raise ValueError('PFFDTD ABC node count is invalid')

    engine = (
        PffdtdResourceComponent(
            component='engine_full_grid_float64_state',
            bytes=3 * _FLOAT64_BYTES * grid_cells,
            derivation='u0 + u1 + Lu1; pinned sim_fdtd.py allocate_mem',
        ),
        PffdtdResourceComponent(
            component='engine_full_grid_boundary_mask',
            bytes=_BOOL_BYTES * grid_cells,
            derivation='bn_mask bool full grid; pinned setup_mask',
        ),
        PffdtdResourceComponent(
            component='loaded_voxel_boundary_arrays_upper_bound',
            bytes=(8 + 6 + 1 + 8) * grid_cells,
            derivation=(
                'Nb<=Ngrid: bn_ixyz int64 + adj_bn bool[6] + '
                'mat_bn int8 + saf_bn float64'
            ),
        ),
        PffdtdResourceComponent(
            component='loaded_grid_axes',
            bytes=_FLOAT64_BYTES * n_axis,
            derivation='xv/yv/zv float64 arrays loaded from vox_out.h5',
        ),
        PffdtdResourceComponent(
            component='abc_arrays',
            bytes=(1 + 8 + 8) * nba,
            derivation='Q_bna int8 + bna_ixyz int64 + u2ba float64',
        ),
        PffdtdResourceComponent(
            component='communication_arrays',
            bytes=(
                8 * _INT64_BYTES
                + 3 * _INT64_BYTES * nr
                + _FLOAT64_BYTES * _INTERPOLATION_POINTS * time_steps
            ),
            derivation=(
                'in_ixyz[8], out_ixyz/out_reorder int64, out_alpha float64, '
                'in_sigs float64[8,Nt]'
            ),
        ),
        PffdtdResourceComponent(
            component='engine_raw_receiver_output',
            bytes=_FLOAT64_BYTES * nr * time_steps,
            derivation='u_out float64[Nr,Nt], Nr=8*receiver_count',
        ),
        PffdtdResourceComponent(
            component=(
                'rigid_material_coefficients'
                if nonrigid_material_count == 0
                else 'material_coefficients_and_lossy_boundary_state_upper_bound'
            ),
            bytes=(
                696
                if nonrigid_material_count == 0
                else (
                    (nonrigid_material_count + 1) * 680
                    + 16
                    + (8 + 3 * 12 * 8) * grid_cells
                )
            ),
            derivation=(
                'one structured coefficient row for Nm=0 plus av[2]; '
                'pinned set_coeffs dtype'
                if nonrigid_material_count == 0
                else (
                    'pinned SimEngine MMb=12: (Nm+1) coefficient rows plus av[2], '
                    'and conservative Nbl<=Ngrid reserve for u2b + vh0/vh1/gh1'
                )
            ),
        ),
    )
    engine_bytes = sum(item.bytes for item in engine)

    post = (
        PffdtdResourceComponent(
            component='postprocess_raw_grid_copy',
            bytes=_FLOAT64_BYTES * nr * time_steps,
            derivation='np.asarray(h5 u_out, dtype=float64)',
        ),
        PffdtdResourceComponent(
            component='postprocess_recombine_product',
            bytes=_FLOAT64_BYTES * nr * time_steps,
            derivation='raw_grid * out_alpha[:,None] temporary',
        ),
        PffdtdResourceComponent(
            component='postprocess_receiver_potential',
            bytes=_FLOAT64_BYTES * receiver_count * time_steps,
            derivation='recombined receiver potential float64[R,Nt]',
        ),
        PffdtdResourceComponent(
            component='postprocess_pressure_records',
            bytes=_FLOAT64_BYTES * receiver_count * time_steps,
            derivation='pressure_records float64[R,Nt]',
        ),
        PffdtdResourceComponent(
            component='postprocess_frequency_kernel',
            bytes=_COMPLEX128_BYTES * frequency_count * time_steps,
            derivation='finite_record_pressure_transfer complex128[F,Nt] kernel',
        ),
        PffdtdResourceComponent(
            component='postprocess_time_and_source_vectors',
            bytes=2 * _FLOAT64_BYTES * time_steps,
            derivation='times float64[Nt] + unit_source float64[Nt]',
        ),
        PffdtdResourceComponent(
            component='postprocess_complex_outputs',
            bytes=_COMPLEX128_BYTES
            * (
                3 * receiver_count * frequency_count
                + 3 * frequency_count
            ),
            derivation='transfer/pressure spectra plus per-frequency q/spectra reserve',
        ),
    )
    post_bytes = engine_bytes + sum(item.bytes for item in post)

    setup_components = (
        PffdtdResourceComponent(
            component='setup_room_geometry_arrays',
            bytes=(
                48 * point_count
                + 1152 * triangle_count
            ),
            derivation=(
                'source-derived upper reserve for RoomGeo pts/tris, '
                'tris_pre structured records and tris_precompute temporaries'
            ),
        ),
        PffdtdResourceComponent(
            component='setup_voxel_named_arrays_full_grid_upper_bound',
            bytes=(
                24 + 8 + 1 + 6 + 1 + 4 + 8 + 24 + 1 + 1
                + 1 + 8 + 1 + 1 + 24 + 24 + 24
            )
            * grid_cells,
            derivation=(
                'sum of pinned process_voxel int64 meshgrids, float64 distance/'
                'xyz/ray arrays, Cartesian adjacency and boolean masks, each '
                'bounded by full-grid point count'
            ),
        ),
        PffdtdResourceComponent(
            component='setup_numpy_vectorized_transient_safety_reserve',
            bytes=96 * grid_cells,
            derivation=(
                'estimator-v1 conservative policy reserve for simultaneous '
                'NumPy expression temporaries in process_voxel; not an '
                'empirical GB-per-room coefficient'
            ),
        ),
        PffdtdResourceComponent(
            component='setup_consolidated_boundary_upper_bound',
            bytes=(8 + 6 + 4 + 8 + 8 + 8 + 1) * grid_cells,
            derivation=(
                'Nb<=Ngrid unified bn_ixyz/adj/tidx/ndist plus derived '
                'saf/saf0/mat arrays'
            ),
        ),
    )
    setup_bytes = sum(item.bytes for item in setup_components)

    components = engine + post + setup_components
    if setup_processes != 1:
        return (
            components,
            ResourceQuantity.unknown('bytes'),
        )
    return (
        components,
        ResourceQuantity.known(max(post_bytes, setup_bytes), 'bytes'),
    )


def _scratch_components(
    *,
    model: dict[str, Any],
    grid_shape: tuple[int, int, int],
    grid_cells: int,
    time_steps: int,
    receiver_count: int,
    total_voxel_points: int,
    voxel_count: int,
    raw_output_cap_bytes: int,
    frequency_count: int,
    nonrigid_material_count: int,
) -> tuple[tuple[PffdtdResourceComponent, ...], ResourceQuantity]:
    nr = _INTERPOLATION_POINTS * receiver_count
    nx, ny, nz = grid_shape
    model_json_bytes = len(
        (json.dumps(model, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    )

    components = (
        PffdtdResourceComponent(
            component='compiled_model_json',
            bytes=model_json_bytes,
            derivation='exact bytes written by current R130A executor',
        ),
        PffdtdResourceComponent(
            component='voxel_temp_hdf5_payload_upper_bound',
            bytes=(6 + 4 + 8 + 8) * total_voxel_points,
            derivation=(
                'every voxel treated non-empty: adj bool[6] + tidx int32 + '
                'ndist float64 + local index int64'
            ),
        ),
        PffdtdResourceComponent(
            component='adjacency_check_memmap',
            bytes=grid_cells,
            derivation='Cartesian check_adj_full uint8 full-grid memmap',
        ),
        PffdtdResourceComponent(
            component='persistent_setup_hdf5_payload_upper_bound',
            bytes=(
                _FLOAT64_BYTES * (nx + ny + nz)
                + (8 + 6 + 1 + 8) * grid_cells
                + _FLOAT64_BYTES * _INTERPOLATION_POINTS * time_steps
                + 3 * _INT64_BYTES * nr
                + 256
            ),
            derivation=(
                'cart_grid/vox_out/comms/sim_consts/sim_mats dataset payloads '
                'with Nb bounded by Ngrid'
            ),
        ),
        PffdtdResourceComponent(
            component='raw_solver_output_file_cap',
            bytes=raw_output_cap_bytes,
            derivation='exact CandidateResourceConfiguration.max_output_bytes cap',
        ),
        PffdtdResourceComponent(
            component='hdf5_metadata_policy_reserve',
            bytes=_HDF5_METADATA_RESERVE_PER_FILE * (voxel_count + 7),
            derivation=(
                'estimator-v1 per-HDF5-file metadata/allocation reserve; '
                'separate from numerical payload sizes'
            ),
        ),
        PffdtdResourceComponent(
            component='final_complex_pressure_artifact_reserve',
            bytes=(
                _JSON_METADATA_RESERVE
                + receiver_count * frequency_count * 2 * 32
                + receiver_count * 256
                + frequency_count * 32
            ),
            derivation=(
                'bounded JSON metadata plus 32-byte decimal-token reserve for '
                'real/imag pressure samples and axes'
            ),
        ),
    )
    if nonrigid_material_count:
        components = components + (
            PffdtdResourceComponent(
                component='frequency_dependent_material_hdf5_upper_bound',
                bytes=(
                    2
                    * nonrigid_material_count
                    * (_HDF5_METADATA_RESERVE_PER_FILE + 12 * 3 * _FLOAT64_BYTES)
                ),
                derivation=(
                    'individual DEF HDF5 plus packaged sim_mats.h5 reserve, '
                    'bounded by pinned MMb=12 per non-rigid material'
                ),
            ),
        )
    total = sum(item.bytes for item in components)
    return components, ResourceQuantity.known(total, 'bytes')


class PffdtdCandidateResourceEstimator:
    """Deterministic estimator for the exact pinned R130A Python/Numba CPU path.

    It estimates scheduler-owned task resources, not total process RSS. Shared
    interpreter/library baseline must already be excluded from the machine
    capacity authority. UNKNOWN is used when pinned source does not support a
    portable bound (currently multiprocessing setup RAM for Nprocs>1).
    """

    def estimate(
        self,
        *,
        authority: CandidateWaveExecutionInput,
        model: dict[str, Any],
        configuration: PffdtdCandidateConfiguration,
        sound_speed_m_s: float,
    ) -> PffdtdCandidateResourceEstimation:
        if (
            configuration.expected_pffdtd_commit_sha
            != SUPPORTED_PFFDTD_COMMIT_SHA
            or authority.solver_implementation_ref.authority_version
            != SUPPORTED_PFFDTD_COMMIT_SHA
        ):
            raise ValueError(
                'PFFDTD estimator v1 only supports the independently inspected '
                f'commit {SUPPORTED_PFFDTD_COMMIT_SHA}'
            )
        if configuration.as_external_ref() != authority.solver_configuration_ref:
            raise ValueError('PFFDTD estimator configuration/input authority mismatch')
        if configuration.resource != authority.resource_configuration:
            raise ValueError('PFFDTD estimator resource configuration mismatch')
        if _digest(model) != authority.solver_model_sha256:
            raise ValueError('PFFDTD estimator solver model/input authority mismatch')
        if authority.runtime_identity.backend_id != 'pffdtd-python-numba-cpu':
            raise ValueError('PFFDTD estimator v1 is CPU Python/Numba only')
        if not math.isfinite(sound_speed_m_s) or sound_speed_m_s <= 0.0:
            raise ValueError('sound speed must be finite and positive')
        if configuration.fcc_flag is not False:
            raise ValueError('PFFDTD estimator v1 is Cartesian-only')

        (
            extents,
            point_count,
            triangle_count,
            nonrigid_material_count,
        ) = _model_geometry(model)
        h = float(sound_speed_m_s) / (
            float(configuration.fmax_hz)
            * float(configuration.points_per_wavelength)
        )
        shape = _grid_shape(extents, grid_spacing_m=h)
        cells = math.prod(shape)
        if cells > configuration.resource.max_grid_cells:
            raise ValueError(
                f'derived PFFDTD grid exceeds candidate max_grid_cells: {cells}'
            )

        courant = math.sqrt(1.0 / 3.0) * _COURANT_BACKOFF
        time_step = h / float(sound_speed_m_s) * courant
        time_steps = int(math.ceil(float(configuration.duration_s) / time_step))
        if time_steps > configuration.resource.max_time_steps:
            raise ValueError(
                'derived PFFDTD time-step count exceeds candidate max_time_steps: '
                f'{time_steps}'
            )

        receiver_count = len(authority.receivers)
        frequency_count = len(authority.frequency_samples_hz)
        nr = _INTERPOLATION_POINTS * receiver_count
        raw_payload = _FLOAT64_BYTES * nr * time_steps
        if raw_payload > configuration.resource.max_output_bytes:
            raise ValueError(
                'raw u_out payload alone exceeds candidate max_output_bytes'
            )

        geometry_volume = extents[0] * extents[1] * extents[2]
        _, voxel_count, total_voxel_points = _voxel_grid_upper_bound(
            grid_shape=shape,
            grid_cells=cells,
            triangle_count=triangle_count,
            geometry_volume_m3=geometry_volume,
            grid_spacing_m=h,
        )

        ram_components, peak_ram = _ram_components(
            grid_shape=shape,
            grid_cells=cells,
            time_steps=time_steps,
            receiver_count=receiver_count,
            frequency_count=frequency_count,
            triangle_count=triangle_count,
            point_count=point_count,
            setup_processes=configuration.resource.setup_processes,
            nonrigid_material_count=nonrigid_material_count,
        )
        scratch_components, scratch = _scratch_components(
            model=model,
            grid_shape=shape,
            grid_cells=cells,
            time_steps=time_steps,
            receiver_count=receiver_count,
            total_voxel_points=total_voxel_points,
            voxel_count=voxel_count,
            raw_output_cap_bytes=configuration.resource.max_output_bytes,
            frequency_count=frequency_count,
            nonrigid_material_count=nonrigid_material_count,
        )

        logical_cpu = max(
            configuration.resource.solver_threads,
            configuration.resource.setup_processes,
        )
        semantics = (
            'logical CPU demand is max(setup_processes, solver_threads) because '
            'setup and solve phases are sequential; the outer R140 worker is not '
            'added a second time',
            'inner_solver_threads is exact CandidateResourceConfiguration.solver_threads',
            'peak RAM is task-incremental solver/setup numerical allocation; shared '
            'Python/NumPy/Numba/HDF5 process baseline belongs to capacity authority',
            'setup_processes>1 peak RAM is UNKNOWN because Python multiprocessing '
            'copy/share working-set behavior is not portably bounded by pinned source',
            'scratch includes temporary voxel HDF5, uint8 adjacency memmap, setup '
            'datasets, configured raw-output cap and bounded final JSON reserve',
            'GPU slots are KNOWN zero for this CPU-only candidate; VRAM is UNAVAILABLE',
            'no production solver adoption or CPU/GPU numerical-equivalence claim',
        )
        if nonrigid_material_count:
            semantics = semantics + (
                'mixed rigid/non-rigid PFFDTD geometry is accepted only when every '
                'material group shares the exact point authority; triangle counts '
                'are summed and lossy boundary state is conservatively bounded by Ngrid',
            )

        core = {
            'authority_version': PFFDTD_RESOURCE_WORKLOAD_AUTHORITY_VERSION,
            'candidate_execution_input_id': authority.execution_input_id,
            'candidate_execution_input_sha256': authority.semantic_sha256,
            'solver_model_sha256': authority.solver_model_sha256,
            'solver_implementation_ref': authority.solver_implementation_ref.model_dump(
                mode='json'
            ),
            'solver_configuration_ref': authority.solver_configuration_ref.model_dump(
                mode='json'
            ),
            'runtime_backend_id': authority.runtime_identity.backend_id,
            'runtime_logical_threads': authority.runtime_identity.logical_threads,
            'estimator_algorithm_id': PFFDTD_RESOURCE_ESTIMATOR_ID,
            'estimator_algorithm_version': PFFDTD_RESOURCE_ESTIMATOR_VERSION,
            'geometry_bbox_extent_m': list(extents),
            'geometry_point_count': point_count,
            'geometry_triangle_count': triangle_count,
            'grid_spacing_m': h,
            'grid_shape': list(shape),
            'grid_cells': cells,
            'time_step_s': time_step,
            'time_step_count': time_steps,
            'cell_time_updates': cells * time_steps,
            'receiver_count': receiver_count,
            'raw_receiver_grid_trace_count': nr,
            'logical_cpu_demand': logical_cpu,
            'inner_solver_threads': configuration.resource.solver_threads,
            'setup_processes': configuration.resource.setup_processes,
            'peak_memory_bytes': peak_ram.model_dump(mode='json'),
            'scratch_bytes': scratch.model_dump(mode='json'),
            'gpu_slots': ResourceQuantity.known(0, 'slots').model_dump(mode='json'),
            'vram_bytes': ResourceQuantity.unavailable('bytes').model_dump(mode='json'),
            'raw_solver_output_cap_bytes': configuration.resource.max_output_bytes,
            'ram_components': [
                item.model_dump(mode='json') for item in ram_components
            ],
            'scratch_components': [
                item.model_dump(mode='json') for item in scratch_components
            ],
            'semantics': list(semantics),
        }
        digest = _digest(core)
        workload = PffdtdCandidateWorkloadEstimate(
            workload_estimate_id=f'r140-pffdtd-workload:{digest}',
            semantic_sha256=digest,
            **core,
        )

        generic = DeclaredCpuResourceEstimator().estimate(
            CpuResourceEstimateRequest(
                execution_backend_ref=pffdtd_execution_backend_ref(authority),
                execution_configuration_ref=pffdtd_execution_configuration_ref(
                    authority
                ),
                logical_cpu_demand=ResourceQuantity.known(
                    logical_cpu,
                    'logical_threads',
                ),
                physical_core_demand=ResourceQuantity.unknown('cores'),
                inner_solver_threads=ResourceQuantity.known(
                    configuration.resource.solver_threads,
                    'threads',
                ),
                peak_memory_bytes=peak_ram,
                scratch_bytes=scratch,
                gpu_slots=ResourceQuantity.known(0, 'slots'),
                vram_bytes=ResourceQuantity.unavailable('bytes'),
                estimate_method=PFFDTD_RESOURCE_ESTIMATOR_ID,
                estimate_method_version=PFFDTD_RESOURCE_ESTIMATOR_VERSION,
                confidence=(
                    'MEDIUM'
                    if peak_ram.state == 'KNOWN'
                    else 'UNKNOWN'
                ),
                assumptions=(
                    f'candidate_execution_input_id={authority.execution_input_id}',
                    f'candidate_execution_input_sha256={authority.semantic_sha256}',
                    f'pffdtd_workload_estimate_id={workload.workload_estimate_id}',
                    f'pffdtd_workload_semantic_sha256={workload.semantic_sha256}',
                    f'pffdtd_commit={SUPPORTED_PFFDTD_COMMIT_SHA}',
                    'scheduler_memory_is_task_incremental_not_total_process_rss',
                    'production_solver_selected=false',
                    'gpu_numerical_equivalence_validated=false',
                ),
            )
        )
        return PffdtdCandidateResourceEstimation(
            workload=workload,
            execution_resource_estimate=generic,
        )


class PffdtdR140Worker:
    """R140 worker adapter executing the real bounded R130A candidate path."""

    def __init__(
        self,
        *,
        candidate_executor: PffdtdCandidateWaveExecutor,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
        expected_input: CandidateWaveExecutionInput,
    ) -> None:
        self.candidate_executor = candidate_executor
        self.dispatch_binding_id = dispatch_binding_id
        self.configuration = configuration
        self.expected_input = expected_input

    @staticmethod
    def _dir_usage(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(
            item.stat().st_size
            for item in path.rglob('*')
            if item.is_file()
        )

    def __call__(
        self,
        task,
        context: ExecutionInvocationContext,
    ) -> ExecutionWorkerOutput:
        context.raise_if_cancelled()
        try:
            current, _ = self.candidate_executor.compile_input(
                dispatch_binding_id=self.dispatch_binding_id,
                configuration=self.configuration,
            )
            if current != self.expected_input:
                raise ExecutionWorkerFailure(
                    'PFFDTD candidate input changed after R140 estimation',
                    exit_condition='candidate_input_identity_mismatch',
                )
            if task.candidate != pffdtd_candidate_input_ref(current):
                raise ExecutionWorkerFailure(
                    'R140 task candidate does not bind exact PFFDTD input',
                    exit_condition='candidate_task_binding_mismatch',
                )
            if task.execution_backend_ref != pffdtd_execution_backend_ref(current):
                raise ExecutionWorkerFailure(
                    'R140 task backend does not bind exact PFFDTD implementation',
                    exit_condition='candidate_backend_binding_mismatch',
                )
            if (
                task.execution_configuration_ref
                != pffdtd_execution_configuration_ref(current)
            ):
                raise ExecutionWorkerFailure(
                    'R140 task configuration does not bind exact PFFDTD configuration',
                    exit_condition='candidate_configuration_binding_mismatch',
                )

            context.report_progress(0.05, 'exact PFFDTD input verified')
            result = self.candidate_executor.execute(
                dispatch_binding_id=self.dispatch_binding_id,
                configuration=self.configuration,
                cancel_check=context.is_cancelled,
            )
            run_dir = (
                self.candidate_executor.work_root
                / self.expected_input.semantic_sha256
            )
            context.report_scratch_usage(self._dir_usage(run_dir))
            context.report_progress(1.0, 'bounded PFFDTD candidate completed')
        except CandidateWaveExecutionCancelled as exc:
            raise ExecutionCancelled(str(exc)) from exc
        except ExecutionCancelled:
            raise
        except ExecutionWorkerFailure:
            raise
        except CandidateWaveExecutionError as exc:
            raise ExecutionWorkerFailure(
                str(exc),
                exit_condition='pffdtd_candidate_failure',
            ) from exc

        return ExecutionWorkerOutput(
            result_authority_ref=MultiFidelityAuthorityRef(
                authority_kind='acoustic_solver_result',
                authority_id=result.result_id,
                authority_version=result.authority_version,
                semantic_sha256=result.semantic_sha256,
                model_id='PFFDTD-python-numba-cpu',
                model_version=SUPPORTED_PFFDTD_COMMIT_SHA,
                fidelity='r130a-candidate',
            ),
            execution_provenance_ref=_multifidelity_ref(
                result.execution_provenance_ref,
                authority_kind='solver_execution_provenance',
            ),
        )
