
import importlib.metadata
import json
import math
import os
import platform
import sys
from ...cad_equipment import FrequencyDomain
from ...canonical_json import (
    canonical_json as _canonical_json,
    canonical_sha256 as _digest,
)
from ...r120_geometry_compiler import ExactExternalAuthorityRef
from ..domain.acoustic_pffdtd_causal_boundary import PffdtdCausalBoundaryCompilation
from ..domain.cad_acoustic_solver_result import (
    AcousticSolverArtifactManifest,
    AcousticSolverArtifactManifestResolver,
    AcousticSolverResultEnvelope,
)
from collections.abc import Callable
from pathlib import Path
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from typing import (
    Any,
    Literal,
    Protocol,
)
from uuid import uuid4

PFFDTD_IMPEDANCE_MAPPING_ID = (
    'htdt.pffdtd.exact_frequency_independent_resistive_specific_impedance_def'
)
PFFDTD_IMPEDANCE_MAPPING_VERSION = '1'


PFFDTD_CANDIDATE_ADAPTER_ID = 'htdt.r130a.pffdtd_candidate_wave'

PFFDTD_CANDIDATE_ADAPTER_VERSION = '1'

PFFDTD_CANDIDATE_IMPEDANCE_ADAPTER_VERSION = '2'

PFFDTD_CANDIDATE_CAUSAL_ADAPTER_VERSION = '3'

PFFDTD_CANDIDATE_INPUT_AUTHORITY_VERSION = 'r130a-candidate-wave-input-1'

PFFDTD_CANDIDATE_IMPEDANCE_INPUT_AUTHORITY_VERSION = 'r130b-candidate-wave-input-1'

PFFDTD_CANDIDATE_CAUSAL_INPUT_AUTHORITY_VERSION = 'r130c-candidate-wave-input-1'

PFFDTD_CANDIDATE_POLYHEDRAL_INPUT_AUTHORITY_VERSION = 'r130d-candidate-wave-input-1'

PFFDTD_CANDIDATE_CONFIGURATION_VERSION = 'r130a-pffdtd-config-1'

COMPLEX_PRESSURE_ARTIFACT_SCHEMA_VERSION = (
    'htdt.r130a.candidate-complex-pressure-artifact-1'
)

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

    def solver_artifact_manifest_resolver(
        self,
        *,
        encoding_schema_ref: ExactExternalAuthorityRef,
    ) -> AcousticSolverArtifactManifestResolver:
        """Typed manifest resolver over stored solver-artifact payloads.

        The exact artifact payload is itself the manifest: it must declare the
        governing ``schema_version`` (equal to the encoding schema authority
        version), a non-empty ``quantity_type`` observable and an explicit
        ``valid_domain`` frequency domain. Anything else fails closed.
        """

        def resolve_manifest(
            ref: ExactExternalAuthorityRef,
        ) -> AcousticSolverArtifactManifest | None:
            try:
                payload = self.read_payload(ref)
            except ValueError:
                return None
            if not isinstance(payload, dict):
                return None
            if (
                payload.get('schema_version')
                != encoding_schema_ref.authority_version
            ):
                return None
            observable = payload.get('quantity_type')
            if not isinstance(observable, str) or not observable:
                return None
            try:
                domain = FrequencyDomain.model_validate(
                    payload.get('valid_domain')
                )
            except ValidationError:
                return None
            channel_identity = {
                key: payload[key]
                for key in ('receiver_identity_order', 'frequency_axis_hz')
                if key in payload
            }
            solver_lineage = {
                key: payload[key]
                for key in (
                    'solver_execution_id',
                    'candidate_execution_input_id',
                    'candidate_execution_input_sha256',
                )
                if isinstance(payload.get(key), str) and payload[key]
            }
            return AcousticSolverArtifactManifest(
                artifact_ref=ref,
                observable=observable,
                encoding_schema_ref=encoding_schema_ref,
                valid_frequency_domain=domain,
                channel_identity=channel_identity,
                solver_lineage=solver_lineage,
            )

        return resolve_manifest

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
    #: Below 2 points per wavelength the Cartesian grid cannot even
    #: represent one wavelength at fmax (spatial Nyquist bound) — the
    #: solver would run and return numbers that carry no wave physics.
    points_per_wavelength: float = Field(ge=2.0)
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
    # Atmospheric absorption (e.g. ISO 9613-1) has no validated implementation
    # in this engine; the omission is declared rather than silently neglected.
    atmospheric_attenuation_policy: Literal['omitted_unsupported'] = (
        'omitted_unsupported'
    )
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
        'atmospheric_attenuation_policy': 'omitted_unsupported',
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

class CandidateTreatmentCompositionBinding(BaseModel):
    """Provenance of a treatment-composed boundary; base authority stays bound."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    composition_authority: ExactExternalAuthorityRef
    base_material_authority: ExactExternalAuthorityRef
    base_boundary_physics_authority: ExactExternalAuthorityRef
    selected_treatment_lifecycle: Literal['proposed', 'installed']
    attached_treatment_overlays: tuple[ExactExternalAuthorityRef, ...] = Field(
        min_length=1
    )
    selected_treatment_material_authorities: tuple[
        ExactExternalAuthorityRef, ...
    ] = Field(min_length=1)

    @model_validator(mode='after')
    def one_material_per_overlay(self) -> 'CandidateTreatmentCompositionBinding':
        if len(self.attached_treatment_overlays) != len(
            self.selected_treatment_material_authorities
        ):
            raise ValueError(
                'treatment composition requires one selected material per overlay'
            )
        return self

class CandidateBoundaryBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_surface_id: str = Field(min_length=1)
    material_authority: ExactExternalAuthorityRef
    boundary_physics_authority: ExactExternalAuthorityRef
    impedance_mapping: CandidateImpedanceBoundaryMapping | None = None
    causal_mapping: PffdtdCausalBoundaryCompilation | None = None
    treatment_composition: CandidateTreatmentCompositionBinding | None = None

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

class CandidatePolyhedralGeometryBinding(BaseModel):
    """Exact R120B -> solver-representation binding for the R130D lane."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    scene_revision_id: str = Field(min_length=1)
    scene_revision_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_id: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    semantic_geometry_ref: ExactExternalAuthorityRef
    compiled_geometry_ref: ExactExternalAuthorityRef
    solver_geometry_ref: ExactExternalAuthorityRef
    exact_topology_report_id: str = Field(min_length=1)
    exact_topology_report_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_tolerance_m: float = Field(gt=0.0)
    region_id: str = Field(min_length=1)
    containment_algorithm_id: str = Field(min_length=1)
    containment_algorithm_version: str = Field(min_length=1)
    containment_tolerance_m: float = Field(gt=0.0)
    grid_algorithm_id: str = Field(min_length=1)
    grid_algorithm_version: str = Field(min_length=1)
    grid_origin_m: tuple[float, float, float]
    grid_spacing_m: float = Field(gt=0.0)
    grid_dimensions: tuple[int, int, int]
    grid_geometry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

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
        'r130d-candidate-wave-input-1',
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
        'htdt.r130d.pffdtd_polyhedral_input_compiler',
    ] = 'htdt.r130a.pffdtd_candidate_input_compiler'
    adapter_compiler_version: Literal['1', '2', '3', '4'] = '1'
    solver_model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    runtime_identity: CandidateRuntimeIdentity
    resource_configuration: CandidateResourceConfiguration
    polyhedral_geometry_binding: CandidatePolyhedralGeometryBinding | None = None

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
        # R130D adds this optional binding without changing the byte/semantic
        # shape of existing R130A/B/C input identities.
        if self.polyhedral_geometry_binding is None:
            payload.pop('polyhedral_geometry_binding', None)
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

class CandidateWaveExecutor(Protocol):
    """Domain port for the bounded candidate-wave executor.

    The concrete ``PffdtdCandidateWaveExecutor`` lives at services rank; domain
    code binds against this structural contract instead of importing it."""

    work_root: Path

    def compile_input(
        self,
        *,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
    ) -> tuple[CandidateWaveExecutionInput, dict[str, Any]]: ...

    def execute(
        self,
        *,
        dispatch_binding_id: str,
        configuration: PffdtdCandidateConfiguration,
        resource_estimate_ref: ExactExternalAuthorityRef | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AcousticSolverResultEnvelope: ...
