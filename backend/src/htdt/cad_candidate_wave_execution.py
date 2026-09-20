from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_benchmark import AcousticMaterial
from .acoustic_pffdtd_causal_boundary import (
    PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION,
    PffdtdCausalBoundaryCompilation,
    CausalFrequencyDependentBoundaryAuthority,
    compile_causal_boundary_to_pffdtd,
    pffdtd_causal_boundary_mapping_authority_payload,
)
from .acoustic_pffdtd_adapter import (
    apply_pffdtd_runtime_compatibility_patches,
    finite_record_pressure_transfer,
    pffdtd_git_head,
    pffdtd_velocity_potential_to_pressure_trace,
    recombine_pffdtd_receiver_traces,
)
from .acoustic_pffdtd_impedance_adapter import (
    PFFDTD_IMPEDANCE_MAPPING_ID,
    PFFDTD_IMPEDANCE_MAPPING_VERSION,
    compile_frequency_independent_resistive_impedance_boundary,
    pffdtd_impedance_mapping_authority_payload,
)
from .cad_acoustic_snapshot import (
    AcousticPredictionRequest,
    AcousticSceneSnapshot,
)
from .cad_acoustic_snapshot_repository import CadAcousticSnapshotRepository
from .cad_acoustic_solver_adapter import AcousticSolverDispatchBinding
from .cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from .cad_acoustic_solver_result import (
    AcousticSolverObservableArtifact,
    AcousticSolverResultEnvelope,
    CadAcousticSolverResultRepository,
    build_acoustic_solver_result_envelope,
)
from .cad_equipment import FrequencyDomain
from .cad_r110_source import R110CompiledSourceModel
from .cad_r110_source_repository import CadR110SourceRepository
from .cad_wave_excitation import (
    AcousticWaveExcitationAuthority,
    CadWaveExcitationRepository,
    WaveSourceExcitationBinding,
)
from .r120_geometry_compiler import (
    ExactExternalAuthorityRef,
    R120CompiledGeometry,
)
from .r120_geometry_compiler_repository import R120GeometryCompilerRepository


PFFDTD_CANDIDATE_ADAPTER_ID = 'htdt.r130a.pffdtd_candidate_wave'
PFFDTD_CANDIDATE_ADAPTER_VERSION = '1'
PFFDTD_CANDIDATE_IMPEDANCE_ADAPTER_VERSION = '2'
PFFDTD_CANDIDATE_CAUSAL_ADAPTER_VERSION = '3'
PFFDTD_CANDIDATE_INPUT_AUTHORITY_VERSION = 'r130a-candidate-wave-input-1'
PFFDTD_CANDIDATE_IMPEDANCE_INPUT_AUTHORITY_VERSION = 'r130b-candidate-wave-input-1'
PFFDTD_CANDIDATE_CAUSAL_INPUT_AUTHORITY_VERSION = 'r130c-candidate-wave-input-1'
PFFDTD_CANDIDATE_CONFIGURATION_VERSION = 'r130a-pffdtd-config-1'
COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION = (
    'htdt.r130a.candidate-complex-pressure-artifact-1'
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CandidateWaveExecutionError(RuntimeError):
    pass


class CandidateWaveExecutionCancelled(CandidateWaveExecutionError):
    pass


class ExactJsonAuthorityStore:
    """Content-addressed external authority/artifact store.

    Each ref resolves only when the on-disk metadata, canonical payload hash and
    requested exact identity all agree. Missing, modified or schema-confused
    files therefore fail closed.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: ExactExternalAuthorityRef) -> Path:
        identity = {
            'authority_id': ref.authority_id,
            'authority_version': ref.authority_version,
            'semantic_hash_sha256': ref.semantic_hash_sha256,
        }
        return self.root / f'{_digest(identity)}.json'

    def path_for(self, ref: ExactExternalAuthorityRef) -> Path:
        return self._path(ref)

    def put_json(
        self,
        authority_id_prefix: str,
        authority_version: str,
        payload: object,
    ) -> ExactExternalAuthorityRef:
        semantic_hash = _digest(payload)
        ref = ExactExternalAuthorityRef(
            authority_id=f'{authority_id_prefix}:{semantic_hash}',
            authority_version=authority_version,
            semantic_hash_sha256=semantic_hash,
        )
        return self.put_exact_json(ref, payload)

    def put_exact_json(
        self,
        ref: ExactExternalAuthorityRef,
        payload: object,
    ) -> ExactExternalAuthorityRef:
        if _digest(payload) != ref.semantic_hash_sha256:
            raise ValueError('external authority payload semantic hash mismatch')
        document = {
            'authority_id': ref.authority_id,
            'authority_version': ref.authority_version,
            'semantic_hash_sha256': ref.semantic_hash_sha256,
            'payload': payload,
        }
        encoded = _canonical_json(document) + '\n'
        path = self._path(ref)
        if path.exists():
            if path.read_text(encoding='utf-8') != encoded:
                raise ValueError(
                    'content-addressed authority path already exists with '
                    'different bytes'
                )
            return ref
        temporary = path.with_suffix(f'.{uuid4().hex}.tmp')
        temporary.write_text(encoded, encoding='utf-8')
        temporary.replace(path)
        return ref

    def read_payload(self, ref: ExactExternalAuthorityRef) -> Any:
        path = self._path(ref)
        if not path.is_file():
            raise ValueError('exact external authority artifact is missing')
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError('exact external authority artifact is unreadable') from exc
        if (
            document.get('authority_id') != ref.authority_id
            or document.get('authority_version') != ref.authority_version
            or document.get('semantic_hash_sha256')
            != ref.semantic_hash_sha256
        ):
            raise ValueError('exact external authority metadata mismatch')
        payload = document.get('payload')
        if _digest(payload) != ref.semantic_hash_sha256:
            raise ValueError('exact external authority payload was modified')
        return payload

    def resolve(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> ExactExternalAuthorityRef | None:
        try:
            self.read_payload(ref)
        except ValueError:
            return None
        return ref


class CandidateResourceConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    backend: Literal['python-numba-cpu'] = 'python-numba-cpu'
    solver_threads: int = Field(ge=1, le=8)
    setup_processes: int = Field(ge=1, le=4)
    max_grid_cells: int = Field(ge=1)
    max_time_steps: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1)
    max_solver_wall_seconds: float = Field(gt=0.0)


class PffdtdCandidateConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r130a-pffdtd-config-1'
    ] = PFFDTD_CANDIDATE_CONFIGURATION_VERSION
    configuration_id: str = Field(pattern=r'^pffdtd-candidate-config:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    expected_pffdtd_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    fmax_hz: float = Field(gt=0.0)
    points_per_wavelength: float = Field(gt=0.0)
    duration_s: float = Field(gt=0.0)
    frequency_samples_hz: tuple[float, ...] = Field(min_length=2)
    fcc_flag: Literal[False] = False
    input_signal: Literal['impulse'] = 'impulse'
    source_injection_mapping: Literal[
        'unit_discrete_volume_velocity_impulse_for_transfer_then_exact_Q_spectrum'
    ] = 'unit_discrete_volume_velocity_impulse_for_transfer_then_exact_Q_spectrum'
    pressure_conversion: Literal[
        'p=rho*d(phi)/dt_second_order'
    ] = 'p=rho*d(phi)/dt_second_order'
    transfer_definition: Literal[
        'finite_record_direct_dtft_P_over_Q_exp_plus_iwt'
    ] = 'finite_record_direct_dtft_P_over_Q_exp_plus_iwt'
    density_kg_m3: float = Field(gt=0.0)
    density_authority_ref: ExactExternalAuthorityRef
    relative_humidity_percent: float = Field(ge=0.0, le=100.0)
    humidity_authority_ref: ExactExternalAuthorityRef
    resource: CandidateResourceConfiguration

    @model_validator(mode='after')
    def validate_identity(self) -> 'PffdtdCandidateConfiguration':
        frequencies = tuple(float(item) for item in self.frequency_samples_hz)
        if frequencies != tuple(sorted(set(frequencies))):
            raise ValueError(
                'candidate output frequencies must be unique and sorted'
            )
        if frequencies[-1] > float(self.fmax_hz):
            raise ValueError('candidate output frequency exceeds fmax')
        expected = _digest(self.semantic_payload())
        if expected != self.semantic_sha256:
            raise ValueError('PFFDTD candidate configuration hash mismatch')
        if self.configuration_id != f'pffdtd-candidate-config:{expected}':
            raise ValueError('PFFDTD candidate configuration id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'configuration_id', 'semantic_sha256'},
        )

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.configuration_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_pffdtd_candidate_configuration(
    *,
    expected_pffdtd_commit_sha: str,
    fmax_hz: float,
    points_per_wavelength: float,
    duration_s: float,
    frequency_samples_hz: tuple[float, ...],
    density_kg_m3: float,
    density_authority_ref: ExactExternalAuthorityRef,
    relative_humidity_percent: float,
    humidity_authority_ref: ExactExternalAuthorityRef,
    resource: CandidateResourceConfiguration,
) -> PffdtdCandidateConfiguration:
    core = {
        'authority_version': PFFDTD_CANDIDATE_CONFIGURATION_VERSION,
        'expected_pffdtd_commit_sha': expected_pffdtd_commit_sha,
        'fmax_hz': float(fmax_hz),
        'points_per_wavelength': float(points_per_wavelength),
        'duration_s': float(duration_s),
        'frequency_samples_hz': [
            float(item) for item in frequency_samples_hz
        ],
        'fcc_flag': False,
        'input_signal': 'impulse',
        'source_injection_mapping': (
            'unit_discrete_volume_velocity_impulse_for_transfer_then_exact_Q_spectrum'
        ),
        'pressure_conversion': 'p=rho*d(phi)/dt_second_order',
        'transfer_definition': (
            'finite_record_direct_dtft_P_over_Q_exp_plus_iwt'
        ),
        'density_kg_m3': float(density_kg_m3),
        'density_authority_ref': density_authority_ref.model_dump(mode='json'),
        'relative_humidity_percent': float(relative_humidity_percent),
        'humidity_authority_ref': humidity_authority_ref.model_dump(mode='json'),
        'resource': resource.model_dump(mode='json'),
    }
    digest = _digest(core)
    return PffdtdCandidateConfiguration(
        configuration_id=f'pffdtd-candidate-config:{digest}',
        semantic_sha256=digest,
        **core,
    )


class CandidateRuntimeIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    backend_id: Literal[
        'pffdtd-python-numba-cpu'
    ] = 'pffdtd-python-numba-cpu'
    operating_system: str = Field(min_length=1)
    architecture: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    cpu_identity: str = Field(min_length=1)
    logical_threads: int = Field(ge=1)
    package_versions: tuple[tuple[str, str], ...]


def capture_candidate_runtime() -> CandidateRuntimeIdentity:
    packages = []
    for name in ('numpy', 'numba', 'h5py', 'scipy'):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = 'not-installed'
        packages.append((name, version))
    return CandidateRuntimeIdentity(
        operating_system=platform.platform() or sys.platform,
        architecture=platform.machine() or 'unknown',
        python_version=platform.python_version(),
        cpu_identity=(
            platform.processor()
            or os.environ.get('PROCESSOR_IDENTIFIER')
            or 'unknown-cpu'
        ),
        logical_threads=max(1, os.cpu_count() or 1),
        package_versions=tuple(packages),
    )


class CandidateImpedanceBoundaryMapping(BaseModel):
    """Execution-derived exact mapping; the material authority remains truth."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    physical_quantity_type: Literal[
        'specific_acoustic_impedance'
    ] = 'specific_acoustic_impedance'
    unit: Literal['Pa*s/m'] = 'Pa*s/m'
    complex_capability: Literal[
        'explicit_resistance_reactance'
    ] = 'explicit_resistance_reactance'
    material_id: str = Field(min_length=1)
    material_version: str = Field(min_length=1)
    material_provenance: str = Field(min_length=1)
    boundary_provenance: dict[str, Any]
    valid_frequency_domain: FrequencyDomain
    frequency_samples_hz: tuple[float, ...] = Field(min_length=1)
    physical_resistance_pa_s_m: float = Field(gt=0.0)
    physical_reactance_pa_s_m: Literal[0.0] = 0.0
    density_kg_m3: float = Field(gt=0.0)
    density_authority_ref: ExactExternalAuthorityRef
    sound_speed_m_s: float = Field(gt=0.0)
    sound_speed_authority_ref: ExactExternalAuthorityRef
    characteristic_impedance_pa_s_m: float = Field(gt=0.0)
    normalized_impedance: float = Field(gt=0.0)
    normalized_admittance: float = Field(gt=0.0)
    def_coefficients: tuple[tuple[float, float, float], ...] = Field(
        min_length=1
    )
    mapping_authority_ref: ExactExternalAuthorityRef
    mapping_id: Literal[
        'htdt.pffdtd.exact_frequency_independent_resistive_specific_impedance_def'
    ] = PFFDTD_IMPEDANCE_MAPPING_ID
    mapping_version: Literal['1'] = PFFDTD_IMPEDANCE_MAPPING_VERSION

    @model_validator(mode='after')
    def validate_exact_subset(self) -> 'CandidateImpedanceBoundaryMapping':
        if not self.boundary_provenance:
            raise ValueError('impedance boundary requires explicit provenance')
        frequencies = tuple(float(item) for item in self.frequency_samples_hz)
        if frequencies != tuple(sorted(set(frequencies))):
            raise ValueError('impedance boundary frequencies must be unique/sorted')
        if (
            not self.valid_frequency_domain.contains(frequencies[0])
            or not self.valid_frequency_domain.contains(frequencies[-1])
        ):
            raise ValueError('impedance boundary frequency samples exceed valid domain')
        physical_values = (
            self.physical_resistance_pa_s_m,
            self.density_kg_m3,
            self.sound_speed_m_s,
            self.characteristic_impedance_pa_s_m,
            self.normalized_impedance,
            self.normalized_admittance,
        )
        if any(not math.isfinite(float(value)) for value in physical_values):
            raise ValueError('impedance boundary physical quantities must be finite')
        expected_rho_c = float(self.density_kg_m3) * float(self.sound_speed_m_s)
        if not math.isclose(
            float(self.characteristic_impedance_pa_s_m),
            expected_rho_c,
            rel_tol=1.0e-12,
            abs_tol=1.0e-12,
        ):
            raise ValueError('impedance boundary characteristic impedance != rho*c')
        expected_zn = (
            float(self.physical_resistance_pa_s_m)
            / float(self.characteristic_impedance_pa_s_m)
        )
        if not math.isclose(
            float(self.normalized_impedance),
            expected_zn,
            rel_tol=1.0e-12,
            abs_tol=1.0e-12,
        ):
            raise ValueError('impedance boundary normalized impedance != Z/(rho*c)')
        if not math.isclose(
            float(self.normalized_admittance),
            1.0 / float(self.normalized_impedance),
            rel_tol=1.0e-12,
            abs_tol=1.0e-12,
        ):
            raise ValueError('impedance boundary normalized admittance != 1/Zn')
        if self.mapping_authority_ref.authority_version != self.mapping_version:
            raise ValueError('impedance boundary mapping authority version mismatch')
        if self.def_coefficients != (
            (0.0, float(self.normalized_impedance), 0.0),
        ):
            raise ValueError('impedance boundary DEF does not match exact resistive mapping')
        return self


class CandidateBoundaryBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str = Field(min_length=1)
    material_authority: ExactExternalAuthorityRef
    boundary_physics_authority: ExactExternalAuthorityRef
    impedance_mapping: CandidateImpedanceBoundaryMapping | None = None
    causal_mapping: PffdtdCausalBoundaryCompilation | None = None

    @model_validator(mode='after')
    def one_nonrigid_mapping(self) -> 'CandidateBoundaryBinding':
        if self.impedance_mapping is not None and self.causal_mapping is not None:
            raise ValueError('boundary binding cannot contain two non-rigid mappings')
        return self


class CandidateBoundaryMaterialAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str = Field(min_length=1)
    pffdtd_material_group: str = Field(min_length=1)
    material_file_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    def_coefficients: tuple[tuple[float, float, float], ...]
    mapping_authority_ref: ExactExternalAuthorityRef
    active_boundary_node_count: int = Field(ge=1)


class CandidateReceiverBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    position_m: tuple[float, float, float]


class CandidateWaveExecutionInput(BaseModel):
    """Deterministic, execution-specific identity above READY dispatch.

    READY itself remains only a dispatch capability decision. This authority is
    the exact compilation identity for one bounded candidate execution.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r130a-candidate-wave-input-1',
        'r130b-candidate-wave-input-1',
        'r130c-candidate-wave-input-1',
    ] = PFFDTD_CANDIDATE_INPUT_AUTHORITY_VERSION
    execution_input_id: str = Field(
        pattern=r'^candidate-wave-input:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    snapshot_id: str
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_request_id: str
    prediction_request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    dispatch_binding_id: str
    dispatch_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dispatch_deterministic_solver_input_hash: str = Field(
        pattern=r'^[0-9a-f]{64}$'
    )

    compiled_geometry_id: str
    compiled_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    compiled_topology_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    material_boundary_configuration_sha256: str = Field(
        pattern=r'^[0-9a-f]{64}$'
    )
    boundary_bindings: tuple[CandidateBoundaryBinding, ...]
    treatment_boundary_composition_sha256: str = Field(
        pattern=r'^[0-9a-f]{64}$'
    )
    acoustic_region_authority_ref: ExactExternalAuthorityRef
    portal_authority_ref: ExactExternalAuthorityRef
    boundary_termination_authority_ref: ExactExternalAuthorityRef | None

    source_entity_id: str
    r110_compiled_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    wave_excitation_binding_id: str
    wave_excitation_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    wave_excitation_id: str
    wave_excitation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    receivers: tuple[CandidateReceiverBinding, ...] = Field(min_length=1)
    requested_frequency_domain: FrequencyDomain
    frequency_samples_hz: tuple[float, ...] = Field(min_length=2)
    observation_time_s: float = Field(gt=0.0)

    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    adapter_descriptor_id: str
    adapter_descriptor_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_compiler_id: Literal[
        'htdt.r130a.pffdtd_candidate_input_compiler',
        'htdt.r130b.pffdtd_candidate_impedance_input_compiler',
        'htdt.r130c.pffdtd_candidate_causal_boundary_input_compiler',
    ] = 'htdt.r130a.pffdtd_candidate_input_compiler'
    adapter_compiler_version: Literal['1', '2', '3'] = '1'
    solver_model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    runtime_identity: CandidateRuntimeIdentity
    resource_configuration: CandidateResourceConfiguration

    @model_validator(mode='after')
    def validate_identity(self) -> 'CandidateWaveExecutionInput':
        expected = _digest(self.semantic_payload())
        if expected != self.semantic_sha256:
            raise ValueError('candidate wave execution input hash mismatch')
        if self.execution_input_id != f'candidate-wave-input:{expected}':
            raise ValueError('candidate wave execution input id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'execution_input_id', 'semantic_sha256'},
        )
        # Preserve the exact pre-R130B rigid identity: the optional impedance
        # field did not exist in R130A and therefore must not serialize as null.
        payload['boundary_bindings'] = [
            item.model_dump(mode='json', exclude_none=True)
            for item in self.boundary_bindings
        ]
        return payload


class CandidateNumericalOutput(BaseModel):
    """Raw complex-pressure extraction before result-envelope wrapping."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    receiver_ids: tuple[str, ...] = Field(min_length=1)
    frequency_hz: tuple[float, ...] = Field(min_length=2)
    pressure_real_pa: tuple[tuple[float, ...], ...]
    pressure_imag_pa: tuple[tuple[float, ...], ...]
    raw_solver_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_solver_asset_name: str = Field(min_length=1)
    time_step_s: float = Field(gt=0.0)
    time_step_count: int = Field(ge=1)
    grid_shape: tuple[int, int, int]
    sound_speed_m_s: float = Field(gt=0.0)
    compile_seconds: float = Field(ge=0.0)
    solve_seconds: float = Field(ge=0.0)
    postprocess_seconds: float = Field(ge=0.0)
    compatibility_patch: dict[str, Any]
    boundary_material_assets: tuple[CandidateBoundaryMaterialAsset, ...] = ()

    @model_validator(mode='after')
    def validate_shape(self) -> 'CandidateNumericalOutput':
        count = len(self.frequency_hz)
        if tuple(self.frequency_hz) != tuple(sorted(set(self.frequency_hz))):
            raise ValueError('numerical output frequency axis must be sorted/unique')
        if len(self.pressure_real_pa) != len(self.receiver_ids):
            raise ValueError('real pressure receiver dimension mismatch')
        if len(self.pressure_imag_pa) != len(self.receiver_ids):
            raise ValueError('imag pressure receiver dimension mismatch')
        for row in (*self.pressure_real_pa, *self.pressure_imag_pa):
            if len(row) != count:
                raise ValueError('complex pressure frequency dimension mismatch')
            if any(not math.isfinite(float(value)) for value in row):
                raise ValueError('complex pressure output must be finite')
        return self


def _position_tuple(position: Any) -> tuple[float, float, float]:
    return (
        float(position.x_m),
        float(position.y_m),
        float(position.z_m),
    )


def _vector_sub(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[0] - right[0],
        left[1] - right[1],
        left[2] - right[2],
    )


def _cross(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return sum(a * b for a, b in zip(left, right))


def _compile_rigid_pffdtd_model(
    *,
    geometry: R120CompiledGeometry,
    source: R110CompiledSourceModel,
    receivers: tuple[CandidateReceiverBinding, ...],
) -> dict[str, Any]:
    if (
        not geometry.closed_shell_diagnostics.closed_shell
        or geometry.closed_shell_diagnostics.boundary_edge_count != 0
        or geometry.closed_shell_diagnostics.non_manifold_edge_count != 0
    ):
        raise CandidateWaveExecutionError(
            'candidate PFFDTD compiler requires an exact closed two-manifold shell'
        )
    if geometry.approximation_operations or geometry.dropped_features:
        raise CandidateWaveExecutionError(
            'candidate PFFDTD compiler refuses approximated/dropped R120 geometry'
        )

    points = [
        (float(item.x_m), float(item.y_m), float(item.z_m))
        for item in geometry.vertices
    ]
    triangles = [
        [int(item.a), int(item.b), int(item.c)]
        for item in geometry.triangles
    ]

    directed: dict[tuple[int, int], int] = {}
    undirected: dict[tuple[int, int], int] = {}
    for triangle in triangles:
        for index in range(3):
            a = triangle[index]
            b = triangle[(index + 1) % 3]
            directed[(a, b)] = directed.get((a, b), 0) + 1
            key = (a, b) if a < b else (b, a)
            undirected[key] = undirected.get(key, 0) + 1
    if any(count != 2 for count in undirected.values()):
        raise CandidateWaveExecutionError(
            'candidate PFFDTD compiler found non-two-manifold triangle edges'
        )
    if any(
        directed.get((a, b), 0) != 1
        or directed.get((b, a), 0) != 1
        for a, b in undirected
    ):
        raise CandidateWaveExecutionError(
            'candidate PFFDTD compiler requires consistently oriented triangles'
        )

    signed_volume = 0.0
    for a, b, c in triangles:
        signed_volume += _dot(points[a], _cross(points[b], points[c])) / 6.0
    if abs(signed_volume) <= 1.0e-12:
        raise CandidateWaveExecutionError(
            'candidate PFFDTD compiler found zero signed volume'
        )
    if signed_volume < 0.0:
        triangles = [[a, c, b] for a, b, c in triangles]

    bounds = geometry.bounding_volume
    def require_inside(label: str, position: tuple[float, float, float]) -> None:
        if not (
            float(bounds.min_x_m) <= position[0] <= float(bounds.max_x_m)
            and float(bounds.min_y_m) <= position[1] <= float(bounds.max_y_m)
            and float(bounds.min_z_m) <= position[2] <= float(bounds.max_z_m)
        ):
            raise CandidateWaveExecutionError(
                f'{label} is outside exact R120 bounding volume'
            )

    source_position = _position_tuple(source.source_acoustic_reference_world_position)
    require_inside('source acoustic reference', source_position)
    for receiver in receivers:
        require_inside(f'receiver {receiver.receiver_id}', receiver.position_m)

    return {
        'mats_hash': {
            '_RIGID': {
                'tris': triangles,
                'pts': [list(item) for item in points],
                'color': [220, 220, 220],
                'sides': [0] * len(triangles),
            }
        },
        'sources': [
            {
                'xyz': list(source_position),
                'name': source.source_entity_id,
            }
        ],
        'receivers': [
            {
                'xyz': list(item.position_m),
                'name': item.receiver_id,
            }
            for item in receivers
        ],
        'export_datetime': 'HTDT R130A candidate deterministic compiler v1',
    }


def _impedance_material_group(binding: CandidateBoundaryBinding) -> str:
    if binding.impedance_mapping is None:
        raise CandidateWaveExecutionError(
            'rigid boundary has no PFFDTD impedance material group'
        )
    digest = _digest(
        {
            'source_surface_id': binding.source_surface_id,
            'material_authority': binding.material_authority.model_dump(mode='json'),
            'boundary_physics_authority': (
                binding.boundary_physics_authority.model_dump(mode='json')
            ),
            'mapping_authority_ref': (
                binding.impedance_mapping.mapping_authority_ref.model_dump(
                    mode='json'
                )
            ),
            'normalized_impedance': binding.impedance_mapping.normalized_impedance,
        }
    )
    return f'HTDT_Z_{digest[:20]}'


def _causal_material_group(binding: CandidateBoundaryBinding) -> str:
    mapping = binding.causal_mapping
    if mapping is None:
        raise CandidateWaveExecutionError(
            'boundary has no PFFDTD causal material group'
        )
    digest = _digest(
        {
            'source_surface_id': binding.source_surface_id,
            'material_authority': binding.material_authority.model_dump(mode='json'),
            'boundary_physics_authority': (
                binding.boundary_physics_authority.model_dump(mode='json')
            ),
            'mapping_authority_ref': mapping.mapping_authority_ref.model_dump(
                mode='json'
            ),
            'compiled_boundary_sha256': mapping.semantic_sha256,
        }
    )
    return f'HTDT_YFD_{digest[:20]}'


def _compile_mixed_pffdtd_model(
    *,
    geometry: R120CompiledGeometry,
    source: R110CompiledSourceModel,
    receivers: tuple[CandidateReceiverBinding, ...],
    boundary_bindings: tuple[CandidateBoundaryBinding, ...],
) -> dict[str, Any]:
    """Compile mixed rigid/impedance groups after the exact rigid geometry gate."""

    base = _compile_rigid_pffdtd_model(
        geometry=geometry,
        source=source,
        receivers=receivers,
    )
    oriented_triangles = base['mats_hash']['_RIGID']['tris']
    points = base['mats_hash']['_RIGID']['pts']
    if len(oriented_triangles) != len(geometry.triangles):
        raise CandidateWaveExecutionError(
            'candidate PFFDTD mixed-boundary triangle identity mismatch'
        )

    binding_by_surface = {
        item.source_surface_id: item for item in boundary_bindings
    }
    if len(binding_by_surface) != len(boundary_bindings):
        raise CandidateWaveExecutionError(
            'candidate boundary bindings must be unique per semantic surface'
        )
    triangle_surfaces = {
        item.source_surface_id for item in geometry.triangles
    }
    if set(binding_by_surface) != triangle_surfaces:
        raise CandidateWaveExecutionError(
            'candidate boundary bindings do not exactly cover compiled surfaces'
        )

    groups: dict[str, dict[str, Any]] = {}
    for compiled_triangle, oriented in zip(
        geometry.triangles,
        oriented_triangles,
        strict=True,
    ):
        binding = binding_by_surface[compiled_triangle.source_surface_id]
        if (
            binding.impedance_mapping is None
            and binding.causal_mapping is None
        ):
            group = '_RIGID'
            side = 0
            color = [220, 220, 220]
        else:
            group = (
                _causal_material_group(binding)
                if binding.causal_mapping is not None
                else _impedance_material_group(binding)
            )
            # _compile_rigid_pffdtd_model normalizes a closed shell to outward
            # winding. PFFDTD side=1 activates the back/negative-normal side,
            # which is the room-interior side for that outward winding.
            side = 1
            color = [180, 180, 180]
        target = groups.setdefault(
            group,
            {
                'tris': [],
                'pts': points,
                'color': color,
                'sides': [],
            },
        )
        target['tris'].append(list(oriented))
        target['sides'].append(side)

    if not any(
        item.impedance_mapping is not None or item.causal_mapping is not None
        for item in boundary_bindings
    ):
        raise CandidateWaveExecutionError(
            'mixed PFFDTD compiler requires at least one non-rigid boundary'
        )

    has_causal = any(
        item.causal_mapping is not None for item in boundary_bindings
    )
    return {
        **base,
        'mats_hash': groups,
        'export_datetime': (
            'HTDT R130C candidate deterministic causal boundary compiler v1'
            if has_causal
            else 'HTDT R130B candidate deterministic impedance compiler v1'
        ),
    }


class PffdtdCandidateWaveExecutor:
    """Bounded PFFDTD candidate executor over existing exact HTDT authorities."""

    def __init__(
        self,
        *,
        snapshot_repository: CadAcousticSnapshotRepository,
        dispatch_repository: CadAcousticSolverDispatchRepository,
        r120_repository: R120GeometryCompilerRepository,
        r110_repository: CadR110SourceRepository,
        wave_excitation_repository: CadWaveExcitationRepository,
        result_repository: CadAcousticSolverResultRepository,
        authority_store: ExactJsonAuthorityStore,
        output_schema_ref: ExactExternalAuthorityRef,
        upstream_root: Path,
        work_root: Path,
    ) -> None:
        paths = {
            Path(snapshot_repository.path),
            Path(dispatch_repository.path),
            Path(r120_repository.path),
            Path(r110_repository.path),
            Path(wave_excitation_repository.path),
            Path(result_repository.path),
        }
        if len(paths) != 1:
            raise ValueError(
                'candidate execution repositories must share one native CAD database'
            )
        self.snapshot_repository = snapshot_repository
        self.dispatch_repository = dispatch_repository
        self.r120_repository = r120_repository
        self.r110_repository = r110_repository
        self.wave_excitation_repository = wave_excitation_repository
        self.result_repository = result_repository
        self.authority_store = authority_store
        self.output_schema_ref = output_schema_ref
        self.upstream_root = Path(upstream_root)
        self.work_root = Path(work_root)

    def _require_external(
        self,
        ref: ExactExternalAuthorityRef,
        *,
        label: str,
    ) -> Any:
        try:
            return self.authority_store.read_payload(ref)
        except ValueError as exc:
            raise CandidateWaveExecutionError(
                f'{label} exact external authority mismatch/missing'
            ) from exc

    def compile_input(
        self,
        *,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
    ) -> tuple[CandidateWaveExecutionInput, dict[str, Any]]:
        dispatch = self.dispatch_repository.get_dispatch(dispatch_binding_id)
        if dispatch is None:
            raise CandidateWaveExecutionError(
                'candidate execution references missing solver dispatch'
            )
        if dispatch.state != 'READY':
            raise CandidateWaveExecutionError(
                'candidate execution requires READY solver dispatch'
            )
        request = self.snapshot_repository.get_prediction_request(
            dispatch.prediction_request_id
        )
        if request is None:
            raise CandidateWaveExecutionError(
                'candidate execution references missing prediction request'
            )
        snapshot = self.snapshot_repository.get_snapshot(
            dispatch.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise CandidateWaveExecutionError(
                'candidate execution references missing acoustic snapshot'
            )
        if (
            request.request_id != dispatch.prediction_request_id
            or request.request_semantic_sha256
            != dispatch.prediction_request_semantic_sha256
            or snapshot.snapshot_id != request.acoustic_scene_snapshot_id
            or snapshot.semantic_sha256
            != request.acoustic_scene_snapshot_sha256
        ):
            raise CandidateWaveExecutionError(
                'candidate execution snapshot/request/dispatch identity is stale'
            )

        descriptor = self.dispatch_repository.get_descriptor(
            dispatch.adapter_descriptor_id
        )
        if descriptor is None:
            raise CandidateWaveExecutionError(
                'candidate execution references missing solver adapter descriptor'
            )
        if (
            descriptor.adapter_id != PFFDTD_CANDIDATE_ADAPTER_ID
            or descriptor.adapter_version
            not in {
                PFFDTD_CANDIDATE_ADAPTER_VERSION,
                PFFDTD_CANDIDATE_IMPEDANCE_ADAPTER_VERSION,
                PFFDTD_CANDIDATE_CAUSAL_ADAPTER_VERSION,
            }
            or descriptor.acoustic_domain != 'wave'
        ):
            raise CandidateWaveExecutionError(
                'READY dispatch does not target the bounded PFFDTD candidate adapter'
            )
        if request.requested_observables != ('complex_pressure',):
            raise CandidateWaveExecutionError(
                'PFFDTD candidate slice supports only complex_pressure'
            )
        if configuration.as_external_ref() != dispatch.solver_configuration_ref:
            raise CandidateWaveExecutionError(
                'candidate solver configuration authority does not match dispatch'
            )
        if (
            configuration.expected_pffdtd_commit_sha
            != dispatch.solver_implementation_ref.authority_version
        ):
            raise CandidateWaveExecutionError(
                'candidate configuration PFFDTD commit does not match implementation authority'
            )
        self._require_external(
            dispatch.solver_implementation_ref,
            label='solver implementation',
        )
        self._require_external(
            descriptor.solver_configuration_schema_ref,
            label='solver configuration schema',
        )
        self._require_external(
            dispatch.solver_configuration_ref,
            label='solver configuration',
        )
        self._require_external(
            configuration.density_authority_ref,
            label='density',
        )
        self._require_external(
            configuration.humidity_authority_ref,
            label='relative humidity',
        )

        frequencies = tuple(
            float(item) for item in configuration.frequency_samples_hz
        )
        requested = request.requested_frequency_domain
        if (
            not math.isclose(
                frequencies[0],
                float(requested.minimum_hz),
                rel_tol=0.0,
                abs_tol=1.0e-12,
            )
            or not math.isclose(
                frequencies[-1],
                float(requested.maximum_hz),
                rel_tol=0.0,
                abs_tol=1.0e-12,
            )
        ):
            raise CandidateWaveExecutionError(
                'candidate frequency sampling must include exact request band endpoints'
            )
        if any(
            item < float(requested.minimum_hz)
            or item > float(requested.maximum_hz)
            for item in frequencies
        ):
            raise CandidateWaveExecutionError(
                'candidate output frequency is outside requested band'
            )

        geometry = self.r120_repository.get_compiled_geometry(
            snapshot.r120_compiled_geometry_id
        )
        if geometry is None:
            raise CandidateWaveExecutionError(
                'candidate execution references missing R120CompiledGeometry'
            )
        if (
            geometry.compiled_hash_sha256
            != snapshot.r120_compiled_geometry_sha256
            or geometry.topology_identity_sha256
            != snapshot.compiled_topology_sha256
        ):
            raise CandidateWaveExecutionError(
                'candidate execution R120 geometry identity mismatch'
            )

        if snapshot.treatment_boundary_bindings:
            raise CandidateWaveExecutionError(
                'bounded PFFDTD candidate does not map treatment composition; '
                'it refuses non-empty treatment bindings'
            )
        treatment_hash = _digest(
            [
                item.model_dump(mode='json')
                for item in snapshot.treatment_boundary_bindings
            ]
        )

        boundary_bindings: list[CandidateBoundaryBinding] = []
        for item in snapshot.surface_boundary_configuration:
            if (
                item.material_authority is None
                or item.boundary_physics_authority is None
            ):
                raise CandidateWaveExecutionError(
                    'candidate PFFDTD compiler requires exact material and boundary authorities'
                )
            material_payload = self._require_external(
                item.material_authority,
                label=f'material {item.source_surface_id}',
            )
            boundary_payload = self._require_external(
                item.boundary_physics_authority,
                label=f'boundary {item.source_surface_id}',
            )
            if not isinstance(boundary_payload, dict) or (
                boundary_payload.get('authority_kind')
                != 'wave_boundary_physics'
            ):
                raise CandidateWaveExecutionError(
                    'candidate boundary authority kind is unsupported'
                )

            if boundary_payload.get('model') == 'rigid_zero_normal_velocity':
                boundary_bindings.append(
                    CandidateBoundaryBinding(
                        source_surface_id=item.source_surface_id,
                        material_authority=item.material_authority,
                        boundary_physics_authority=item.boundary_physics_authority,
                    )
                )
                continue

            if boundary_payload.get('model') == 'causal_specific_admittance_def':
                if (
                    descriptor.adapter_version
                    != PFFDTD_CANDIDATE_CAUSAL_ADAPTER_VERSION
                ):
                    raise CandidateWaveExecutionError(
                        'causal frequency-dependent execution requires the R130C adapter version'
                    )
                if (
                    boundary_payload.get('physical_quantity_type')
                    != 'specific_acoustic_admittance'
                    or boundary_payload.get('unit') != 'm/(Pa*s)'
                    or boundary_payload.get('normalization')
                    != 'Yn=rho*c*Y_specific'
                    or boundary_payload.get('representation')
                    != 'parallel_series_RLC_normalized_DEF'
                    or boundary_payload.get('interpolation_semantics')
                    != 'analytic_rational_evaluation_no_interpolation'
                    or boundary_payload.get('extrapolation_rule') != 'forbidden'
                ):
                    raise CandidateWaveExecutionError(
                        'causal boundary quantity/unit/normalization/representation mismatch'
                    )
                try:
                    causal_ref = ExactExternalAuthorityRef.model_validate(
                        boundary_payload.get('causal_boundary_authority_ref')
                    )
                    density_ref = ExactExternalAuthorityRef.model_validate(
                        boundary_payload.get('density_authority_ref')
                    )
                    sound_speed_ref = ExactExternalAuthorityRef.model_validate(
                        boundary_payload.get('sound_speed_authority_ref')
                    )
                    mapping_ref = ExactExternalAuthorityRef.model_validate(
                        boundary_payload.get('pffdtd_mapping_authority_ref')
                    )
                    valid_domain = FrequencyDomain.model_validate(
                        boundary_payload.get('valid_frequency_domain')
                    )
                    causal_authority = (
                        CausalFrequencyDependentBoundaryAuthority.model_validate(
                            {
                                **material_payload,
                                'authority_id': causal_ref.authority_id,
                                'authority_version': causal_ref.authority_version,
                                'semantic_sha256': causal_ref.semantic_hash_sha256,
                            }
                        )
                    )
                except Exception as exc:
                    raise CandidateWaveExecutionError(
                        'causal boundary exact authority payload is malformed'
                    ) from exc
                if causal_ref != item.material_authority:
                    raise CandidateWaveExecutionError(
                        'causal boundary material authority identity mismatch'
                    )
                if causal_authority.as_external_ref() != causal_ref:
                    raise CandidateWaveExecutionError(
                        'causal boundary semantic authority identity mismatch'
                    )
                if valid_domain != causal_authority.valid_frequency_domain:
                    raise CandidateWaveExecutionError(
                        'causal boundary valid frequency domain mismatch'
                    )
                if density_ref != configuration.density_authority_ref:
                    raise CandidateWaveExecutionError(
                        'causal boundary density authority identity mismatch'
                    )
                if (
                    snapshot.environment is None
                    or snapshot.environment.sound_speed_m_s is None
                    or snapshot.environment.sound_speed_source_authority is None
                ):
                    raise CandidateWaveExecutionError(
                        'causal boundary requires exact sound-speed environment authority'
                    )
                if sound_speed_ref != snapshot.environment.sound_speed_source_authority:
                    raise CandidateWaveExecutionError(
                        'causal boundary sound-speed authority identity mismatch'
                    )
                if mapping_ref.authority_version != PFFDTD_CAUSAL_BOUNDARY_MAPPING_VERSION:
                    raise CandidateWaveExecutionError(
                        'causal boundary PFFDTD mapping version mismatch'
                    )
                mapping_payload = self._require_external(
                    mapping_ref,
                    label=f'PFFDTD causal boundary mapping {item.source_surface_id}',
                )
                if mapping_payload != pffdtd_causal_boundary_mapping_authority_payload():
                    raise CandidateWaveExecutionError(
                        'causal boundary PFFDTD mapping authority mismatch'
                    )
                density_payload = self._require_external(
                    density_ref,
                    label='causal boundary density',
                )
                sound_speed_payload = self._require_external(
                    sound_speed_ref,
                    label='causal boundary sound speed',
                )
                if (
                    not isinstance(density_payload, dict)
                    or density_payload.get('quantity')
                    not in {'air_density_kg_m3', 'density_kg_m3'}
                    or not math.isclose(
                        float(density_payload.get('value', math.nan)),
                        float(configuration.density_kg_m3),
                        rel_tol=0.0,
                        abs_tol=1.0e-12,
                    )
                ):
                    raise CandidateWaveExecutionError(
                        'causal boundary density does not match exact authority'
                    )
                if (
                    not isinstance(sound_speed_payload, dict)
                    or sound_speed_payload.get('quantity') != 'sound_speed_m_s'
                    or not math.isclose(
                        float(sound_speed_payload.get('value', math.nan)),
                        float(snapshot.environment.sound_speed_m_s),
                        rel_tol=0.0,
                        abs_tol=1.0e-12,
                    )
                ):
                    raise CandidateWaveExecutionError(
                        'causal boundary sound speed does not match exact authority'
                    )
                try:
                    causal_mapping = compile_causal_boundary_to_pffdtd(
                        authority=causal_authority,
                        source_boundary_authority_ref=causal_ref,
                        mapping_authority_ref=mapping_ref,
                        requested_frequency_hz=frequencies,
                        density_kg_m3=float(configuration.density_kg_m3),
                        density_authority_ref=density_ref,
                        sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
                        sound_speed_authority_ref=sound_speed_ref,
                        expected_scene_revision_id=snapshot.scene_revision_id,
                        expected_scene_content_hash=snapshot.scene_content_hash,
                        expected_surface_id=item.source_surface_id,
                    )
                except ValueError as exc:
                    raise CandidateWaveExecutionError(
                        f'UNSUPPORTED causal frequency-dependent boundary: {exc}'
                    ) from exc
                boundary_bindings.append(
                    CandidateBoundaryBinding(
                        source_surface_id=item.source_surface_id,
                        material_authority=item.material_authority,
                        boundary_physics_authority=item.boundary_physics_authority,
                        causal_mapping=causal_mapping,
                    )
                )
                continue

            if boundary_payload.get('model') != 'specific_impedance_table':
                raise CandidateWaveExecutionError(
                    'bounded PFFDTD candidate boundary quantity/model is unsupported; '
                    'no rigid fallback or implicit conversion is authorized'
                )
            if (
                descriptor.adapter_version
                != PFFDTD_CANDIDATE_IMPEDANCE_ADAPTER_VERSION
            ):
                raise CandidateWaveExecutionError(
                    'explicit impedance execution requires the R130B adapter version'
                )
            if (
                boundary_payload.get('physical_quantity_type')
                != 'specific_acoustic_impedance'
                or boundary_payload.get('unit') != 'Pa*s/m'
                or boundary_payload.get('complex_capability')
                != 'explicit_resistance_reactance'
            ):
                raise CandidateWaveExecutionError(
                    'explicit impedance boundary quantity/unit/capability mismatch'
                )
            provenance = boundary_payload.get('provenance')
            if not isinstance(provenance, dict) or not provenance:
                raise CandidateWaveExecutionError(
                    'explicit impedance boundary requires exact provenance metadata'
                )
            try:
                material_ref = ExactExternalAuthorityRef.model_validate(
                    boundary_payload.get('material_authority_ref')
                )
                density_ref = ExactExternalAuthorityRef.model_validate(
                    boundary_payload.get('density_authority_ref')
                )
                sound_speed_ref = ExactExternalAuthorityRef.model_validate(
                    boundary_payload.get('sound_speed_authority_ref')
                )
                mapping_ref = ExactExternalAuthorityRef.model_validate(
                    boundary_payload.get('pffdtd_mapping_authority_ref')
                )
                valid_domain = FrequencyDomain.model_validate(
                    boundary_payload.get('valid_frequency_domain')
                )
                material = AcousticMaterial.model_validate(material_payload)
            except Exception as exc:
                raise CandidateWaveExecutionError(
                    'explicit impedance boundary exact authority payload is malformed'
                ) from exc
            if material_ref != item.material_authority:
                raise CandidateWaveExecutionError(
                    'impedance boundary material authority identity mismatch'
                )
            if density_ref != configuration.density_authority_ref:
                raise CandidateWaveExecutionError(
                    'impedance boundary density authority identity mismatch'
                )
            if (
                snapshot.environment is None
                or snapshot.environment.sound_speed_m_s is None
                or snapshot.environment.sound_speed_source_authority is None
            ):
                raise CandidateWaveExecutionError(
                    'impedance boundary requires exact sound-speed environment authority'
                )
            if sound_speed_ref != snapshot.environment.sound_speed_source_authority:
                raise CandidateWaveExecutionError(
                    'impedance boundary sound-speed authority identity mismatch'
                )
            if (
                mapping_ref.authority_version
                != PFFDTD_IMPEDANCE_MAPPING_VERSION
            ):
                raise CandidateWaveExecutionError(
                    'impedance boundary PFFDTD mapping version mismatch'
                )

            mapping_payload = self._require_external(
                mapping_ref,
                label=f'PFFDTD impedance mapping {item.source_surface_id}',
            )
            expected_mapping_payload = (
                pffdtd_impedance_mapping_authority_payload()
            )
            if mapping_payload != expected_mapping_payload:
                raise CandidateWaveExecutionError(
                    'impedance boundary PFFDTD mapping authority mismatch'
                )

            density_payload = self._require_external(
                density_ref,
                label='impedance density',
            )
            sound_speed_payload = self._require_external(
                sound_speed_ref,
                label='impedance sound speed',
            )
            if (
                not isinstance(density_payload, dict)
                or density_payload.get('quantity')
                not in {'air_density_kg_m3', 'density_kg_m3'}
                or not math.isclose(
                    float(density_payload.get('value', math.nan)),
                    float(configuration.density_kg_m3),
                    rel_tol=0.0,
                    abs_tol=1.0e-12,
                )
            ):
                raise CandidateWaveExecutionError(
                    'impedance density value does not match exact density authority'
                )
            if (
                not isinstance(sound_speed_payload, dict)
                or sound_speed_payload.get('quantity') != 'sound_speed_m_s'
                or not math.isclose(
                    float(sound_speed_payload.get('value', math.nan)),
                    float(snapshot.environment.sound_speed_m_s),
                    rel_tol=0.0,
                    abs_tol=1.0e-12,
                )
            ):
                raise CandidateWaveExecutionError(
                    'impedance sound speed does not match exact environment authority'
                )

            try:
                mapped = compile_frequency_independent_resistive_impedance_boundary(
                    material=material,
                    frequencies_hz=frequencies,
                    density_kg_m3=float(configuration.density_kg_m3),
                    sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
                )
            except ValueError as exc:
                raise CandidateWaveExecutionError(
                    f'UNSUPPORTED explicit impedance boundary: {exc}'
                ) from exc

            table_min = float(material.specific_impedance[0].frequency_hz)
            table_max = float(material.specific_impedance[-1].frequency_hz)
            if (
                not math.isclose(
                    float(valid_domain.minimum_hz),
                    table_min,
                    rel_tol=0.0,
                    abs_tol=1.0e-12,
                )
                or not math.isclose(
                    float(valid_domain.maximum_hz),
                    table_max,
                    rel_tol=0.0,
                    abs_tol=1.0e-12,
                )
            ):
                raise CandidateWaveExecutionError(
                    'impedance boundary valid frequency domain does not match '
                    'the exact material impedance table'
                )

            boundary_bindings.append(
                CandidateBoundaryBinding(
                    source_surface_id=item.source_surface_id,
                    material_authority=item.material_authority,
                    boundary_physics_authority=item.boundary_physics_authority,
                    impedance_mapping=CandidateImpedanceBoundaryMapping(
                        material_id=str(mapped['material_id']),
                        material_version=str(mapped['material_version']),
                        material_provenance=str(mapped['material_provenance']),
                        boundary_provenance=provenance,
                        valid_frequency_domain=valid_domain,
                        frequency_samples_hz=tuple(mapped['frequencies_hz']),
                        physical_resistance_pa_s_m=float(
                            mapped['physical_resistance_pa_s_m']
                        ),
                        physical_reactance_pa_s_m=0.0,
                        density_kg_m3=float(mapped['density_kg_m3']),
                        density_authority_ref=density_ref,
                        sound_speed_m_s=float(mapped['sound_speed_m_s']),
                        sound_speed_authority_ref=sound_speed_ref,
                        characteristic_impedance_pa_s_m=float(
                            mapped['characteristic_impedance_pa_s_m']
                        ),
                        normalized_impedance=float(
                            mapped['normalized_impedance']
                        ),
                        normalized_admittance=float(
                            mapped['normalized_admittance']
                        ),
                        def_coefficients=tuple(
                            tuple(float(value) for value in row)
                            for row in mapped['def_coefficients']
                        ),
                        mapping_authority_ref=mapping_ref,
                        mapping_id=str(mapped['mapping_id']),
                        mapping_version=str(mapped['mapping_version']),
                    ),
                )
            )


        if snapshot.acoustic_region_authority_ref is None:
            raise CandidateWaveExecutionError(
                'candidate execution requires exact AcousticRegion authority'
            )
        self._require_external(
            snapshot.acoustic_region_authority_ref,
            label='acoustic region',
        )
        if snapshot.portal_authority_ref is None:
            raise CandidateWaveExecutionError(
                'candidate execution requires exact Portal authority'
            )
        portal_payload = self._require_external(
            snapshot.portal_authority_ref,
            label='portal',
        )
        if (
            not isinstance(portal_payload, dict)
            or portal_payload.get('declaration_mode') != 'explicit_none'
        ):
            raise CandidateWaveExecutionError(
                'bounded PFFDTD candidate supports only explicit no-portal authority'
            )
        if snapshot.boundary_termination_authority_ref is not None:
            termination_payload = self._require_external(
                snapshot.boundary_termination_authority_ref,
                label='boundary termination',
            )
            if (
                not isinstance(termination_payload, dict)
                or termination_payload.get('declaration_mode') != 'explicit_none'
            ):
                raise CandidateWaveExecutionError(
                    'bounded closed-shell PFFDTD candidate supports only '
                    'explicit no-termination authority'
                )

        if len(snapshot.sources) != 1:
            raise CandidateWaveExecutionError(
                'bounded PFFDTD candidate requires exactly one source'
            )
        source_snapshot = snapshot.sources[0]
        source = self.r110_repository.get_model(
            source_snapshot.r110_compiled_source_sha256
        )
        if source is None:
            raise CandidateWaveExecutionError(
                'candidate execution references missing R110CompiledSourceModel'
            )
        if source.semantic_sha256 != source_snapshot.r110_compiled_source_sha256:
            raise CandidateWaveExecutionError('candidate R110 source identity mismatch')

        if len(snapshot.wave_source_excitation_bindings) != 1:
            raise CandidateWaveExecutionError(
                'candidate execution requires exactly one explicit wave excitation binding'
            )
        wave_binding_snapshot = snapshot.wave_source_excitation_bindings[0]
        wave_binding = self.wave_excitation_repository.get_binding(
            wave_binding_snapshot.binding_id
        )
        if wave_binding is None or wave_binding != wave_binding_snapshot:
            raise CandidateWaveExecutionError(
                'candidate wave excitation binding is stale or missing'
            )
        if wave_binding.r110_compiled_source_sha256 != source.semantic_sha256:
            raise CandidateWaveExecutionError(
                'candidate wave excitation does not bind exact R110 source'
            )
        excitation = self.wave_excitation_repository.get_excitation(
            wave_binding.excitation_id
        )
        if excitation is None:
            raise CandidateWaveExecutionError(
                'candidate execution references missing AcousticWaveExcitationAuthority'
            )
        if excitation.semantic_sha256 != wave_binding.excitation_semantic_sha256:
            raise CandidateWaveExecutionError(
                'candidate acoustic wave excitation identity mismatch'
            )
        excitation_by_frequency = {
            float(item.frequency_hz): item
            for item in excitation.samples
        }
        if any(item not in excitation_by_frequency for item in frequencies):
            raise CandidateWaveExecutionError(
                'candidate requires exact AcousticWaveExcitationAuthority sample '
                'at every requested output frequency'
            )

        if not snapshot.receivers or len(snapshot.receivers) > 4:
            raise CandidateWaveExecutionError(
                'bounded PFFDTD candidate supports one to four receivers'
            )
        receivers = tuple(
            CandidateReceiverBinding(
                receiver_id=item.receiver_id,
                entity_id=item.entity_id,
                position_m=_position_tuple(item.world_position),
            )
            for item in snapshot.receivers
        )

        if (
            snapshot.environment is None
            or snapshot.environment.sound_speed_m_s is None
            or snapshot.environment.sound_speed_source_authority is None
        ):
            raise CandidateWaveExecutionError(
                'candidate execution requires exact sound-speed environment authority'
            )
        self._require_external(
            snapshot.environment.authority,
            label='environment',
        )
        self._require_external(
            snapshot.environment.sound_speed_source_authority,
            label='sound speed source',
        )
        if snapshot.environment.temperature_c is not None:
            assert snapshot.environment.temperature_source_authority is not None
            self._require_external(
                snapshot.environment.temperature_source_authority,
                label='temperature source',
            )

        runtime = capture_candidate_runtime()
        has_causal = any(
            item.causal_mapping is not None for item in boundary_bindings
        )
        has_impedance = any(
            item.impedance_mapping is not None for item in boundary_bindings
        )
        if has_causal:
            model = _compile_mixed_pffdtd_model(
                geometry=geometry,
                source=source,
                receivers=receivers,
                boundary_bindings=tuple(boundary_bindings),
            )
            input_authority_version = PFFDTD_CANDIDATE_CAUSAL_INPUT_AUTHORITY_VERSION
            compiler_id = 'htdt.r130c.pffdtd_candidate_causal_boundary_input_compiler'
            compiler_version = '3'
        elif has_impedance:
            model = _compile_mixed_pffdtd_model(
                geometry=geometry,
                source=source,
                receivers=receivers,
                boundary_bindings=tuple(boundary_bindings),
            )
            input_authority_version = (
                PFFDTD_CANDIDATE_IMPEDANCE_INPUT_AUTHORITY_VERSION
            )
            compiler_id = (
                'htdt.r130b.pffdtd_candidate_impedance_input_compiler'
            )
            compiler_version = '2'
        else:
            model = _compile_rigid_pffdtd_model(
                geometry=geometry,
                source=source,
                receivers=receivers,
            )
            input_authority_version = PFFDTD_CANDIDATE_INPUT_AUTHORITY_VERSION
            compiler_id = 'htdt.r130a.pffdtd_candidate_input_compiler'
            compiler_version = '1'
        model_hash = _digest(model)

        core = {
            'authority_version': input_authority_version,
            'snapshot_id': snapshot.snapshot_id,
            'snapshot_sha256': snapshot.semantic_sha256,
            'prediction_request_id': request.request_id,
            'prediction_request_sha256': request.request_semantic_sha256,
            'prediction_deterministic_input_hash': request.deterministic_input_hash,
            'dispatch_binding_id': dispatch.binding_id,
            'dispatch_binding_sha256': dispatch.semantic_sha256,
            'dispatch_deterministic_solver_input_hash': (
                dispatch.deterministic_solver_input_hash
            ),
            'compiled_geometry_id': geometry.compiled_geometry_id,
            'compiled_geometry_sha256': geometry.compiled_hash_sha256,
            'compiled_topology_sha256': geometry.topology_identity_sha256,
            'material_boundary_configuration_sha256': (
                snapshot.material_boundary_configuration_sha256
            ),
            'boundary_bindings': [
                item.model_dump(mode='json', exclude_none=True)
                for item in boundary_bindings
            ],
            'treatment_boundary_composition_sha256': treatment_hash,
            'acoustic_region_authority_ref': (
                snapshot.acoustic_region_authority_ref.model_dump(mode='json')
            ),
            'portal_authority_ref': (
                snapshot.portal_authority_ref.model_dump(mode='json')
            ),
            'boundary_termination_authority_ref': (
                None
                if snapshot.boundary_termination_authority_ref is None
                else snapshot.boundary_termination_authority_ref.model_dump(
                    mode='json'
                )
            ),
            'source_entity_id': source.source_entity_id,
            'r110_compiled_source_sha256': source.semantic_sha256,
            'wave_excitation_binding_id': wave_binding.binding_id,
            'wave_excitation_binding_sha256': wave_binding.semantic_sha256,
            'wave_excitation_id': excitation.excitation_id,
            'wave_excitation_sha256': excitation.semantic_sha256,
            'receivers': [item.model_dump(mode='json') for item in receivers],
            'requested_frequency_domain': requested.model_dump(mode='json'),
            'frequency_samples_hz': list(frequencies),
            'observation_time_s': float(configuration.duration_s),
            'solver_implementation_ref': (
                dispatch.solver_implementation_ref.model_dump(mode='json')
            ),
            'solver_configuration_ref': (
                dispatch.solver_configuration_ref.model_dump(mode='json')
            ),
            'adapter_descriptor_id': descriptor.descriptor_id,
            'adapter_descriptor_sha256': descriptor.semantic_sha256,
            'adapter_compiler_id': compiler_id,
            'adapter_compiler_version': compiler_version,
            'solver_model_sha256': model_hash,
            'runtime_identity': runtime.model_dump(mode='json'),
            'resource_configuration': configuration.resource.model_dump(
                mode='json'
            ),
        }
        digest = _digest(core)
        authority = CandidateWaveExecutionInput(
            execution_input_id=f'candidate-wave-input:{digest}',
            semantic_sha256=digest,
            **core,
        )
        return authority, model

    def _run_pffdtd(
        self,
        *,
        authority: CandidateWaveExecutionInput,
        model: dict[str, Any],
        configuration: PffdtdCandidateConfiguration,
        excitation: AcousticWaveExcitationAuthority,
        sound_speed_m_s: float,
        cancel_check: Callable[[], bool],
    ) -> CandidateNumericalOutput:
        def cancelled() -> None:
            if cancel_check():
                raise CandidateWaveExecutionCancelled(
                    'candidate wave execution was cancelled; no result artifact saved'
                )

        cancelled()
        try:
            actual_head = pffdtd_git_head(self.upstream_root)
        except Exception as exc:
            raise CandidateWaveExecutionError(
                'PFFDTD implementation identity resolution failed: '
                f'{type(exc).__name__}: {exc}'
            ) from exc
        if actual_head != configuration.expected_pffdtd_commit_sha:
            raise CandidateWaveExecutionError(
                'PFFDTD checkout does not match exact implementation authority'
            )
        try:
            compatibility = apply_pffdtd_runtime_compatibility_patches(
                self.upstream_root
            )
        except Exception as exc:
            raise CandidateWaveExecutionError(
                'PFFDTD exact compatibility mapping failed: '
                f'{type(exc).__name__}: {exc}'
            ) from exc
        upstream_python = self.upstream_root / 'python'
        if not (upstream_python / 'sim_setup.py').is_file():
            raise CandidateWaveExecutionError(
                'PFFDTD Python runtime is missing from exact checkout'
            )
        sys.path.insert(0, str(upstream_python))
        try:
            import h5py
            import numpy as np
            from sim_setup import sim_setup
            from fdtd.sim_fdtd import SimEngine
            from materials.adm_funcs import (
                write_freq_dep_mat,
                write_freq_ind_mat_from_Zn,
            )
        except Exception as exc:
            raise CandidateWaveExecutionError(
                f'PFFDTD runtime import failed: {type(exc).__name__}: {exc}'
            ) from exc

        run_dir = self.work_root / authority.semantic_sha256
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        material_dir = run_dir / 'materials'
        material_dir.mkdir()
        sim_dir = run_dir / 'sim'
        sim_dir.mkdir()
        model_path = run_dir / 'pffdtd_model.json'
        model_path.write_text(
            json.dumps(model, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )

        material_files: dict[str, str] = {}
        material_asset_specs: list[dict[str, Any]] = []
        boundary_material_assets: list[CandidateBoundaryMaterialAsset] = []
        for binding in authority.boundary_bindings:
            impedance_mapping = binding.impedance_mapping
            causal_mapping = binding.causal_mapping
            if impedance_mapping is None and causal_mapping is None:
                continue
            if causal_mapping is not None:
                group = _causal_material_group(binding)
                expected_def = np.asarray(
                    causal_mapping.def_coefficients,
                    dtype=np.float64,
                )
                mapping_ref = causal_mapping.mapping_authority_ref
            else:
                assert impedance_mapping is not None
                group = _impedance_material_group(binding)
                expected_def = np.asarray(
                    impedance_mapping.def_coefficients,
                    dtype=np.float64,
                )
                mapping_ref = impedance_mapping.mapping_authority_ref
            material_path = material_dir / f'{group}.h5'
            try:
                if causal_mapping is not None:
                    write_freq_dep_mat(expected_def, material_path)
                else:
                    assert impedance_mapping is not None
                    write_freq_ind_mat_from_Zn(
                        float(impedance_mapping.normalized_impedance),
                        material_path,
                    )
                with h5py.File(material_path, 'r') as handle:
                    actual_def = np.asarray(
                        handle['DEF'][...],
                        dtype=np.float64,
                    )
            except Exception as exc:
                raise CandidateWaveExecutionError(
                    'PFFDTD exact boundary material generation failed: '
                    f'{type(exc).__name__}: {exc}'
                ) from exc
            if (
                actual_def.shape != expected_def.shape
                or not np.array_equal(actual_def, expected_def)
            ):
                raise CandidateWaveExecutionError(
                    'PFFDTD material writer changed exact boundary DEF authority'
                )
            material_files[group] = material_path.name
            material_asset_specs.append(
                {
                    'source_surface_id': binding.source_surface_id,
                    'pffdtd_material_group': group,
                    'material_file_sha256': _file_sha256(material_path),
                    'def_coefficients': tuple(
                        tuple(float(value) for value in row)
                        for row in expected_def
                    ),
                    'mapping_authority_ref': mapping_ref,
                }
            )

        tc_control = 20.0 * (float(sound_speed_m_s) / 343.2) ** 2
        compile_started = time.perf_counter()
        try:
            sim_setup(
                insig_type=configuration.input_signal,
                fmax=float(configuration.fmax_hz),
                PPW=float(configuration.points_per_wavelength),
                save_folder=sim_dir,
                model_json_file=model_path,
                mat_folder=material_dir,
                mat_files_dict=material_files,
                duration=float(configuration.duration_s),
                Tc=tc_control,
                rh=float(configuration.relative_humidity_percent),
                source_num=1,
                draw_vox=False,
                fcc_flag=False,
                Nprocs=configuration.resource.setup_processes,
                compress=0,
            )
            if material_asset_specs:
                ordered_groups = sorted(material_files)
                specs_by_group = {
                    item['pffdtd_material_group']: item
                    for item in material_asset_specs
                }
                packaged_path = sim_dir / 'sim_mats.h5'
                voxel_path = sim_dir / 'vox_out.h5'
                if not packaged_path.is_file() or not voxel_path.is_file():
                    raise CandidateWaveExecutionError(
                        'PFFDTD impedance setup did not persist material/voxel authority'
                    )
                with h5py.File(packaged_path, 'r') as packaged:
                    if int(packaged['Nmat'][()]) != len(ordered_groups):
                        raise CandidateWaveExecutionError(
                            'PFFDTD packaged material count mismatch'
                        )
                    packaged_defs = [
                        np.asarray(
                            packaged[f'mat_{index:02d}_DEF'][...],
                            dtype=np.float64,
                        )
                        for index in range(len(ordered_groups))
                    ]
                with h5py.File(voxel_path, 'r') as voxels:
                    material_nodes = np.asarray(
                        voxels['mat_bn'][...],
                        dtype=np.int64,
                    )
                for index, group in enumerate(ordered_groups):
                    spec = specs_by_group[group]
                    expected_def = np.asarray(
                        spec['def_coefficients'],
                        dtype=np.float64,
                    )
                    if (
                        packaged_defs[index].shape != expected_def.shape
                        or not np.array_equal(packaged_defs[index], expected_def)
                    ):
                        raise CandidateWaveExecutionError(
                            'PFFDTD packaged DEF differs from exact impedance authority'
                        )
                    active_count = int(np.count_nonzero(material_nodes == index))
                    if active_count < 1:
                        raise CandidateWaveExecutionError(
                            'PFFDTD impedance material has no active boundary nodes; '
                            'rigid fallback/wrong-side mapping is refused'
                        )
                    boundary_material_assets.append(
                        CandidateBoundaryMaterialAsset(
                            **spec,
                            active_boundary_node_count=active_count,
                        )
                    )
            cancelled()
            engine = SimEngine(
                sim_dir,
                energy_on=False,
                nthreads=min(
                    configuration.resource.solver_threads,
                    authority.runtime_identity.logical_threads,
                ),
            )
            engine.load_h5_data()
            engine.setup_mask()
            engine.allocate_mem()
            engine.set_coeffs()
            engine.checks()
        except CandidateWaveExecutionCancelled:
            raise
        except Exception as exc:
            raise CandidateWaveExecutionError(
                f'PFFDTD setup/compile failed: {type(exc).__name__}: {exc}'
            ) from exc
        compile_seconds = time.perf_counter() - compile_started

        grid_shape = (int(engine.Nx), int(engine.Ny), int(engine.Nz))
        grid_cells = math.prod(grid_shape)
        if grid_cells > configuration.resource.max_grid_cells:
            raise CandidateWaveExecutionError(
                f'PFFDTD grid exceeds bounded resource contract: {grid_cells}'
            )
        if int(engine.Nt) > configuration.resource.max_time_steps:
            raise CandidateWaveExecutionError(
                f'PFFDTD time steps exceed bounded resource contract: {engine.Nt}'
            )
        if not math.isclose(
            float(engine.c),
            float(sound_speed_m_s),
            rel_tol=0.0,
            abs_tol=1.0e-12,
        ):
            raise CandidateWaveExecutionError(
                'PFFDTD native sound-speed mapping differs from exact HTDT authority'
            )
        record_last_time_s = float((int(engine.Nt) - 1) * engine.Ts)
        record_next_time_s = float(int(engine.Nt) * engine.Ts)
        record_tolerance_s = max(
            1.0e-12,
            abs(float(engine.Ts)) * 1.0e-9,
        )
        if not (
            record_last_time_s < float(configuration.duration_s)
            and record_next_time_s + record_tolerance_s
            >= float(configuration.duration_s)
        ):
            raise CandidateWaveExecutionError(
                'PFFDTD native time samples do not satisfy exact finite-record '
                '[0,T) requirement'
            )

        cancelled()
        solve_started = time.perf_counter()
        try:
            engine.run_all(nsteps=int(engine.Nt))
        except Exception as exc:
            raise CandidateWaveExecutionError(
                f'PFFDTD numerical execution failed: {type(exc).__name__}: {exc}'
            ) from exc
        solve_seconds = time.perf_counter() - solve_started
        if (
            solve_seconds
            > configuration.resource.max_solver_wall_seconds
        ):
            raise CandidateWaveExecutionError(
                'PFFDTD numerical execution exceeded bounded wall-time contract'
            )
        cancelled()

        post_started = time.perf_counter()
        try:
            engine.save_outputs()
            output_path = sim_dir / 'sim_outs.h5'
            if not output_path.is_file():
                raise CandidateWaveExecutionError(
                    'PFFDTD did not produce its raw sim_outs.h5 asset'
                )
            if output_path.stat().st_size > configuration.resource.max_output_bytes:
                raise CandidateWaveExecutionError(
                    'PFFDTD raw output exceeds bounded output-size contract'
                )
            with h5py.File(output_path, 'r') as handle:
                raw_grid = np.asarray(handle['u_out'][...], dtype=np.float64)
            receiver_potential = recombine_pffdtd_receiver_traces(
                raw_grid,
                engine.out_alpha,
                receiver_count=len(authority.receivers),
                nt=int(engine.Nt),
            )
            pressure_records = np.asarray(
                [
                    pffdtd_velocity_potential_to_pressure_trace(
                        receiver_potential[index],
                        time_step_s=float(engine.Ts),
                        density_kg_m3=float(configuration.density_kg_m3),
                    )
                    for index in range(len(authority.receivers))
                ],
                dtype=np.float64,
            )
            frequencies = np.asarray(
                authority.frequency_samples_hz,
                dtype=np.float64,
            )
            unit_source = np.zeros(int(engine.Nt), dtype=np.float64)
            unit_source[0] = 1.0
            transfer = np.asarray(
                [
                    finite_record_pressure_transfer(
                        pressure_records[index],
                        unit_source,
                        time_step_s=float(engine.Ts),
                        frequency_hz=frequencies,
                    )
                    for index in range(len(authority.receivers))
                ],
                dtype=np.complex128,
            )
            exact_samples = {
                float(item.frequency_hz): complex(
                    float(item.real_m3_s),
                    float(item.imag_m3_s),
                )
                for item in excitation.samples
            }
            q = np.asarray(
                [exact_samples[float(item)] for item in frequencies],
                dtype=np.complex128,
            )
            pressure = transfer * q[None, :]
            if not (
                np.all(np.isfinite(pressure.real))
                and np.all(np.isfinite(pressure.imag))
            ):
                raise CandidateWaveExecutionError(
                    'PFFDTD candidate complex pressure contains non-finite values'
                )
            raw_hash = _file_sha256(output_path)
        except CandidateWaveExecutionError:
            raise
        except Exception as exc:
            raise CandidateWaveExecutionError(
                f'PFFDTD receiver extraction failed: {type(exc).__name__}: {exc}'
            ) from exc
        postprocess_seconds = time.perf_counter() - post_started
        cancelled()

        return CandidateNumericalOutput(
            receiver_ids=tuple(item.receiver_id for item in authority.receivers),
            frequency_hz=tuple(float(item) for item in frequencies),
            pressure_real_pa=tuple(
                tuple(float(item) for item in row)
                for row in pressure.real
            ),
            pressure_imag_pa=tuple(
                tuple(float(item) for item in row)
                for row in pressure.imag
            ),
            raw_solver_asset_sha256=raw_hash,
            raw_solver_asset_name='sim_outs.h5',
            time_step_s=float(engine.Ts),
            time_step_count=int(engine.Nt),
            grid_shape=grid_shape,
            sound_speed_m_s=float(engine.c),
            compile_seconds=compile_seconds,
            solve_seconds=solve_seconds,
            postprocess_seconds=postprocess_seconds,
            compatibility_patch=compatibility,
            boundary_material_assets=tuple(boundary_material_assets),
        )

    def execute(
        self,
        *,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
        resource_estimate_ref: ExactExternalAuthorityRef | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AcousticSolverResultEnvelope:
        check = cancel_check or (lambda: False)
        authority, model = self.compile_input(
            dispatch_binding_id=dispatch_binding_id,
            configuration=configuration,
        )
        resource_estimate_payload: dict[str, Any] | None = None
        if resource_estimate_ref is not None:
            resolved_resource_estimate = self._require_external(
                resource_estimate_ref,
                label='PFFDTD resource estimate',
            )
            if not isinstance(resolved_resource_estimate, dict):
                raise CandidateWaveExecutionError(
                    'PFFDTD resource estimate payload is malformed'
                )
            if (
                resolved_resource_estimate.get('candidate_execution_input_id')
                != authority.execution_input_id
                or resolved_resource_estimate.get(
                    'candidate_execution_input_sha256'
                )
                != authority.semantic_sha256
                or resolved_resource_estimate.get('solver_model_sha256')
                != authority.solver_model_sha256
            ):
                raise CandidateWaveExecutionError(
                    'PFFDTD resource estimate is stale for candidate execution input'
                )
            resource_estimate_payload = resolved_resource_estimate
        if check():
            raise CandidateWaveExecutionCancelled(
                'candidate wave execution was cancelled before backend start; '
                'no result artifact saved'
            )

        snapshot = self.snapshot_repository.get_snapshot(authority.snapshot_id)
        request = self.snapshot_repository.get_prediction_request(
            authority.prediction_request_id
        )
        dispatch = self.dispatch_repository.get_dispatch(
            authority.dispatch_binding_id
        )
        if snapshot is None or request is None or dispatch is None:
            raise CandidateWaveExecutionError(
                'candidate execution exact chain disappeared after compilation'
            )
        wave_binding = self.wave_excitation_repository.get_binding(
            authority.wave_excitation_binding_id
        )
        if wave_binding is None:
            raise CandidateWaveExecutionError(
                'candidate execution wave excitation binding disappeared'
            )
        excitation = self.wave_excitation_repository.get_excitation(
            wave_binding.excitation_id
        )
        if excitation is None:
            raise CandidateWaveExecutionError(
                'candidate execution wave excitation authority disappeared'
            )
        assert snapshot.environment is not None
        assert snapshot.environment.sound_speed_m_s is not None

        numerical = self._run_pffdtd(
            authority=authority,
            model=model,
            configuration=configuration,
            excitation=excitation,
            sound_speed_m_s=float(snapshot.environment.sound_speed_m_s),
            cancel_check=check,
        )
        if check():
            raise CandidateWaveExecutionCancelled(
                'candidate wave execution was cancelled before artifact commit; '
                'no result artifact saved'
            )

        schema_payload = self._require_external(
            self.output_schema_ref,
            label='complex pressure artifact schema',
        )
        if (
            not isinstance(schema_payload, dict)
            or schema_payload.get('schema_version')
            != COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION
            or schema_payload.get('quantity_type') != 'complex_pressure'
        ):
            raise CandidateWaveExecutionError(
                'complex-pressure artifact schema authority is incompatible'
            )

        has_causal = any(
            item.causal_mapping is not None
            for item in authority.boundary_bindings
        )
        has_impedance = any(
            item.impedance_mapping is not None
            for item in authority.boundary_bindings
        )
        has_nonrigid = has_causal or has_impedance
        execution_prefix = (
            'r130c-candidate-causal-boundary'
            if has_causal
            else (
                'r130b-candidate-impedance'
                if has_impedance
                else 'r130a-candidate-wave'
            )
        )
        execution_id = (
            f'{execution_prefix}:{authority.semantic_sha256[:20]}:{uuid4().hex}'
        )
        artifact_payload = {
            'schema_version': COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
            'quantity_type': 'complex_pressure',
            'complex_representation': {
                'form': 'cartesian_real_imag',
                'phasor_convention': 'exp(-i*omega*t)',
                'analysis_fourier_kernel': 'exp(+i*omega*t)',
            },
            'receiver_identity_order': [
                {
                    'receiver_id': item.receiver_id,
                    'entity_id': item.entity_id,
                    'position_m': list(item.position_m),
                }
                for item in authority.receivers
            ],
            'frequency_axis_hz': list(numerical.frequency_hz),
            'time_sampling': {
                'time_step_s': numerical.time_step_s,
                'sample_count': numerical.time_step_count,
                'finite_record_interval': '[0,T)',
                'requested_duration_s': authority.observation_time_s,
            },
            'units': 'Pa',
            'reference': (
                'absolute complex acoustic pressure from finite-record P/Q '
                'transfer multiplied by exact AcousticWaveExcitationAuthority Q(f)'
            ),
            'valid_domain': request.requested_frequency_domain.model_dump(
                mode='json'
            ),
            'solver_execution_id': execution_id,
            'candidate_execution_input_id': authority.execution_input_id,
            'candidate_execution_input_sha256': authority.semantic_sha256,
            'source_authority': {
                'r110_compiled_source_sha256': (
                    authority.r110_compiled_source_sha256
                ),
                'wave_excitation_binding_sha256': (
                    authority.wave_excitation_binding_sha256
                ),
                'wave_excitation_sha256': authority.wave_excitation_sha256,
            },
            'solver_raw_asset': {
                'name': numerical.raw_solver_asset_name,
                'sha256': numerical.raw_solver_asset_sha256,
            },
            'pressure_real_pa': [
                list(row) for row in numerical.pressure_real_pa
            ],
            'pressure_imag_pa': [
                list(row) for row in numerical.pressure_imag_pa
            ],
        }
        if has_nonrigid:
            artifact_payload['boundary_authority'] = {
                'material_boundary_configuration_sha256': (
                    authority.material_boundary_configuration_sha256
                ),
                'bindings': [
                    item.model_dump(mode='json', exclude_none=True)
                    for item in authority.boundary_bindings
                ],
                'pffdtd_material_assets': [
                    item.model_dump(mode='json')
                    for item in numerical.boundary_material_assets
                ],
            }

        artifact_ref = self.authority_store.put_json(
            'acoustic-solver-artifact',
            COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION,
            artifact_payload,
        )
        provenance_schema_version = (
            'htdt.r130c.candidate-causal-boundary-execution-provenance-1'
            if has_causal
            else (
                'htdt.r130b.candidate-impedance-execution-provenance-1'
                if has_impedance
                else 'htdt.r130a.candidate-execution-provenance-1'
            )
        )
        provenance_payload = {
            'schema_version': provenance_schema_version,
            'execution_id': execution_id,
            'candidate_only': True,
            'production_solver_selected': False,
            'r130_numerical_acceptance_completed': False,
            'execution_input_id': authority.execution_input_id,
            'execution_input_sha256': authority.semantic_sha256,
            'solver_implementation_ref': (
                dispatch.solver_implementation_ref.model_dump(mode='json')
            ),
            'solver_configuration_ref': (
                dispatch.solver_configuration_ref.model_dump(mode='json')
            ),
            'adapter_id': PFFDTD_CANDIDATE_ADAPTER_ID,
            'adapter_version': (
                PFFDTD_CANDIDATE_CAUSAL_ADAPTER_VERSION
                if has_causal
                else (
                    PFFDTD_CANDIDATE_IMPEDANCE_ADAPTER_VERSION
                    if has_impedance
                    else PFFDTD_CANDIDATE_ADAPTER_VERSION
                )
            ),
            'runtime_identity': authority.runtime_identity.model_dump(mode='json'),
            'resource_configuration': (
                authority.resource_configuration.model_dump(mode='json')
            ),
            'compiled_solver_model_sha256': authority.solver_model_sha256,
            'raw_solver_asset_sha256': numerical.raw_solver_asset_sha256,
            'grid_shape': list(numerical.grid_shape),
            'time_step_s': numerical.time_step_s,
            'time_step_count': numerical.time_step_count,
            'sound_speed_m_s': numerical.sound_speed_m_s,
            'timings_s': {
                'compile': numerical.compile_seconds,
                'solve': numerical.solve_seconds,
                'postprocess': numerical.postprocess_seconds,
            },
            'compatibility_patch': numerical.compatibility_patch,
        }
        if resource_estimate_payload is not None:
            assert resource_estimate_ref is not None
            provenance_payload['resource_estimate_ref'] = (
                resource_estimate_ref.model_dump(mode='json')
            )
            provenance_payload['resource_estimate_identity'] = {
                'workload_estimate_id': resource_estimate_ref.authority_id,
                'workload_estimate_sha256': resource_estimate_ref.semantic_hash_sha256,
                'grid_shape': resource_estimate_payload.get('grid_shape'),
                'time_step_count': resource_estimate_payload.get('time_step_count'),
                'peak_memory_bytes': resource_estimate_payload.get('peak_memory_bytes'),
                'scratch_bytes': resource_estimate_payload.get('scratch_bytes'),
            }
        if has_nonrigid:
            if has_causal:
                provenance_payload['r130c_candidate_execution_completed'] = True
                provenance_payload['r130c_physics_acceptance_completed'] = False
            else:
                provenance_payload['r130b_numerical_acceptance_completed'] = False
            provenance_payload['owned_room_evidence'] = False
            provenance_payload['boundary_execution'] = {
                'material_boundary_configuration_sha256': (
                    authority.material_boundary_configuration_sha256
                ),
                'bindings': [
                    item.model_dump(mode='json', exclude_none=True)
                    for item in authority.boundary_bindings
                ],
                'pffdtd_material_assets': [
                    item.model_dump(mode='json')
                    for item in numerical.boundary_material_assets
                ],
            }

        provenance_ref = self.authority_store.put_json(
            'solver-execution-provenance',
            provenance_schema_version,
            provenance_payload,
        )
        artifact = AcousticSolverObservableArtifact(
            observable='complex_pressure',
            artifact_authority=artifact_ref,
            encoding_schema_ref=self.output_schema_ref,
            valid_frequency_domain=request.requested_frequency_domain,
        )
        envelope = build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id=execution_id,
            execution_provenance_ref=provenance_ref,
            artifacts=(artifact,),
            completed_at_utc=_utc_now(),
        )
        return self.result_repository.save(envelope)


def register_authority_model(
    store: ExactJsonAuthorityStore,
    *,
    ref: ExactExternalAuthorityRef,
    model: BaseModel,
    exclude: set[str],
) -> ExactExternalAuthorityRef:
    """Register an existing exact authority without changing its identity."""

    payload = model.model_dump(mode='json', exclude=exclude)
    return store.put_exact_json(ref, payload)
