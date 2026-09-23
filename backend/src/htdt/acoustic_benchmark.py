from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Direction3, Position3


BenchmarkCapability = Literal[
    'wave_rigid',
    'wave_impedance',
    'wave_radiation_termination',
    'portal_continuity',
    'geometric_specular',
    'geometric_scattering',
    'stochastic_rays',
    'hybrid_overlap',
]
BenchmarkObservableKind = Literal[
    'eigenfrequency_hz',
    'transfer_magnitude_db',
    'transfer_phase_deg',
    'complex_reflection_coefficient',
    'direct_path_length_m',
    'arrival_time_s',
    'reflection_point_m',
    'reflected_path_length_m',
    'field_pressure_pa',
    'complex_pressure_transfer_pa_per_m3_s',
    'energy_decay_db',
    'hybrid_overlap_level_db',
]
BenchmarkReferenceKind = Literal[
    'analytical',
    'closed_form',
    'independent_solver',
    'invariant',
    'cross_fixture',
    'statistical',
]
BenchmarkAcceptanceRelation = Literal[
    'matches_reference',
    'matches_peer',
    'monotonic_convergence',
    'repeatable_same_seed',
    'continuous_overlap',
    'must_differ_from_peer',
]


def canonical_benchmark_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


class AcousticVertex(BaseModel):
    model_config = ConfigDict(frozen=True)

    vertex_id: str = Field(min_length=1)
    position: Position3


class AcousticFace(BaseModel):
    model_config = ConfigDict(frozen=True)

    face_id: str = Field(min_length=1)
    vertex_ids: tuple[str, ...] = Field(min_length=3)
    boundary_id: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_vertices(self) -> 'AcousticFace':
        if len(self.vertex_ids) != len(set(self.vertex_ids)):
            raise ValueError('face vertex ids must be unique')
        return self


class AcousticRegion(BaseModel):
    """Solver-neutral closed air volume authority used by R100 benchmarks."""

    model_config = ConfigDict(frozen=True)

    region_id: str = Field(min_length=1)
    vertices: tuple[AcousticVertex, ...] = Field(min_length=4)
    faces: tuple[AcousticFace, ...] = Field(min_length=4)

    @model_validator(mode='after')
    def valid_topology_references(self) -> 'AcousticRegion':
        vertex_ids = [item.vertex_id for item in self.vertices]
        face_ids = [item.face_id for item in self.faces]
        if len(vertex_ids) != len(set(vertex_ids)):
            raise ValueError('region vertex ids must be unique')
        if len(face_ids) != len(set(face_ids)):
            raise ValueError('region face ids must be unique')
        known_vertices = set(vertex_ids)
        dangling = [
            face.face_id
            for face in self.faces
            if any(vertex_id not in known_vertices for vertex_id in face.vertex_ids)
        ]
        if dangling:
            raise ValueError(f'region faces reference unknown vertices: {dangling}')
        return self


class AcousticObstacle(BaseModel):
    """Explicit solid or thin acoustic object inside one host air region."""

    model_config = ConfigDict(frozen=True)

    obstacle_id: str = Field(min_length=1)
    host_region_id: str = Field(min_length=1)
    representation: Literal['solid_volume', 'thin_surface']
    vertices: tuple[AcousticVertex, ...] = Field(min_length=3)
    faces: tuple[AcousticFace, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_topology_references(self) -> 'AcousticObstacle':
        vertex_ids = [item.vertex_id for item in self.vertices]
        face_ids = [item.face_id for item in self.faces]
        if len(vertex_ids) != len(set(vertex_ids)):
            raise ValueError('obstacle vertex ids must be unique')
        if len(face_ids) != len(set(face_ids)):
            raise ValueError('obstacle face ids must be unique')
        known_vertices = set(vertex_ids)
        dangling = [
            face.face_id
            for face in self.faces
            if any(vertex_id not in known_vertices for vertex_id in face.vertex_ids)
        ]
        if dangling:
            raise ValueError(f'obstacle faces reference unknown vertices: {dangling}')
        if self.representation == 'solid_volume' and len(self.vertices) < 4:
            raise ValueError('solid obstacle requires at least four vertices')
        return self


class AcousticPortal(BaseModel):
    """Explicit pressure/velocity-continuity opening between two modeled regions."""

    model_config = ConfigDict(frozen=True)

    portal_id: str = Field(min_length=1)
    region_a_id: str = Field(min_length=1)
    region_b_id: str = Field(min_length=1)
    aperture: tuple[Position3, ...] = Field(min_length=3)
    continuity_model: Literal['pressure_velocity_continuity'] = 'pressure_velocity_continuity'

    @model_validator(mode='after')
    def distinct_regions(self) -> 'AcousticPortal':
        if self.region_a_id == self.region_b_id:
            raise ValueError('portal must connect two distinct acoustic regions')
        return self


class BoundaryTermination(BaseModel):
    """Explicit end condition for an opening that does not connect to a modeled region."""

    model_config = ConfigDict(frozen=True)

    termination_id: str = Field(min_length=1)
    region_id: str = Field(min_length=1)
    aperture: tuple[Position3, ...] = Field(min_length=3)
    kind: Literal['radiation', 'anechoic', 'rigid', 'impedance']
    boundary_id: str | None = None
    radiation_model: Literal['local_first_order_outgoing'] | None = None
    normal_convention: Literal['outward_from_region'] | None = None
    characteristic_impedance_model: Literal['rho_c_from_environment'] | None = None
    pressure_velocity_equation: Literal['p_eq_rho_c_u_n'] | None = None
    wavenumber_equation: Literal['k_eq_omega_over_c'] | None = None
    helmholtz_robin_equation: Literal['dp_dn_minus_i_k_p_eq_0'] | None = None

    @model_validator(mode='after')
    def boundary_requirement(self) -> 'BoundaryTermination':
        radiation_fields = (
            self.radiation_model,
            self.normal_convention,
            self.characteristic_impedance_model,
            self.pressure_velocity_equation,
            self.wavenumber_equation,
            self.helmholtz_robin_equation,
        )
        if self.kind != 'radiation' and any(value is not None for value in radiation_fields):
            raise ValueError(
                'radiation-specific authority is only valid for radiation termination'
            )
        if self.kind == 'impedance' and self.boundary_id is None:
            raise ValueError('impedance termination requires boundary_id')
        return self


class GeometricAcousticBand(BaseModel):
    model_config = ConfigDict(frozen=True)

    center_hz: float = Field(gt=0.0)
    absorption: float = Field(ge=0.0, le=1.0)
    scattering: float = Field(ge=0.0, le=1.0)


class SpecificImpedancePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    resistance_pa_s_m: float = Field(ge=0.0)
    reactance_pa_s_m: float

    @model_validator(mode='after')
    def finite_values(self) -> 'SpecificImpedancePoint':
        values = (self.frequency_hz, self.resistance_pa_s_m, self.reactance_pa_s_m)
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('impedance values must be finite')
        return self


class AcousticMaterial(BaseModel):
    """Wave and geometric material capabilities are explicit and never inferred from each other."""

    model_config = ConfigDict(frozen=True)

    material_id: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    version: str = Field(min_length=1)
    wave_model: Literal['rigid', 'specific_impedance_table', 'unsupported']
    specific_impedance: tuple[SpecificImpedancePoint, ...] = ()
    geometric_model: Literal['banded', 'unsupported'] = 'unsupported'
    geometric_bands: tuple[GeometricAcousticBand, ...] = ()

    @model_validator(mode='after')
    def valid_capabilities(self) -> 'AcousticMaterial':
        if self.wave_model == 'specific_impedance_table' and not self.specific_impedance:
            raise ValueError('specific impedance wave model requires impedance data')
        if self.wave_model != 'specific_impedance_table' and self.specific_impedance:
            raise ValueError('impedance data is only valid for specific_impedance_table')
        if self.geometric_model == 'banded' and not self.geometric_bands:
            raise ValueError('banded geometric model requires acoustic bands')
        if self.geometric_model != 'banded' and self.geometric_bands:
            raise ValueError('geometric bands are only valid for banded model')
        frequencies = [item.frequency_hz for item in self.specific_impedance]
        if frequencies != sorted(frequencies) or len(frequencies) != len(set(frequencies)):
            raise ValueError('specific impedance frequencies must be unique and sorted')
        band_centers = [item.center_hz for item in self.geometric_bands]
        if band_centers != sorted(band_centers) or len(band_centers) != len(set(band_centers)):
            raise ValueError('geometric band centers must be unique and sorted')
        return self


class AcousticBoundary(BaseModel):
    model_config = ConfigDict(frozen=True)

    boundary_id: str = Field(min_length=1)
    material_id: str = Field(min_length=1)


class BenchmarkSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    region_id: str = Field(min_length=1)
    position: Position3
    normalization: Literal['volume_velocity_m3_s', 'unit_energy_j', 'pressure_pa_at_1m']
    amplitude: float = Field(gt=0.0)
    phase_deg: float = 0.0
    directivity: Literal['omnidirectional'] = 'omnidirectional'

    @model_validator(mode='after')
    def finite_excitation(self) -> 'BenchmarkSource':
        if not isfinite(float(self.amplitude)) or not isfinite(float(self.phase_deg)):
            raise ValueError('source excitation must be finite')
        return self


class BenchmarkReceiver(BaseModel):
    model_config = ConfigDict(frozen=True)

    receiver_id: str = Field(min_length=1)
    region_id: str = Field(min_length=1)
    position: Position3
    forward: Direction3 = Field(default_factory=lambda: Direction3(x=0.0, y=1.0, z=0.0))
    calibration_state: Literal['ideal_flat', 'profile'] = 'ideal_flat'
    calibration_profile_id: str | None = None
    timing_reference: Literal['source_t0', 'absolute_external', 'none'] = 'source_t0'

    @model_validator(mode='after')
    def calibration_requirement(self) -> 'BenchmarkReceiver':
        if self.calibration_state == 'profile' and not self.calibration_profile_id:
            raise ValueError('profile receiver calibration requires calibration_profile_id')
        if self.calibration_state == 'ideal_flat' and self.calibration_profile_id is not None:
            raise ValueError('ideal_flat receiver must not name a calibration profile')
        return self


class BenchmarkEnvironment(BaseModel):
    model_config = ConfigDict(frozen=True)

    temperature_c: float
    sound_speed_m_s: float = Field(gt=0.0)
    density_kg_m3: float = Field(gt=0.0)
    relative_humidity_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    pressure_pa: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def finite_environment(self) -> 'BenchmarkEnvironment':
        values = [self.temperature_c, self.sound_speed_m_s, self.density_kg_m3]
        if self.relative_humidity_percent is not None:
            values.append(self.relative_humidity_percent)
        if self.pressure_pa is not None:
            values.append(self.pressure_pa)
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('environment values must be finite')
        return self


class BenchmarkFrequencyGrid(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['uniform', 'explicit', 'not_applicable']
    start_hz: float | None = None
    stop_hz: float | None = None
    step_hz: float | None = None
    values_hz: tuple[float, ...] = ()

    @model_validator(mode='after')
    def valid_grid(self) -> 'BenchmarkFrequencyGrid':
        if self.kind == 'uniform':
            if self.start_hz is None or self.stop_hz is None or self.step_hz is None:
                raise ValueError('uniform frequency grid requires start/stop/step')
            if self.start_hz < 0 or self.stop_hz <= self.start_hz or self.step_hz <= 0:
                raise ValueError('uniform frequency grid is invalid')
            if self.values_hz:
                raise ValueError('uniform frequency grid must not contain explicit values')
        elif self.kind == 'explicit':
            if not self.values_hz:
                raise ValueError('explicit frequency grid requires values')
            if any(value <= 0 or not isfinite(float(value)) for value in self.values_hz):
                raise ValueError('explicit frequency values must be finite and positive')
            if list(self.values_hz) != sorted(self.values_hz) or len(self.values_hz) != len(set(self.values_hz)):
                raise ValueError('explicit frequency values must be unique and sorted')
            if any(value is not None for value in (self.start_hz, self.stop_hz, self.step_hz)):
                raise ValueError('explicit frequency grid must not contain start/stop/step')
        else:
            if self.values_hz or any(value is not None for value in (self.start_hz, self.stop_hz, self.step_hz)):
                raise ValueError('not_applicable frequency grid must not contain samples')
        return self


class FiniteRecordTransferContract(BaseModel):
    """Solver-neutral finite-record transfer authority for R100A-4."""

    model_config = ConfigDict(frozen=True)

    excitation_model: Literal['causal_discrete_unit_sample_volume_velocity']
    sample_zero_reference: Literal['source_t0']
    record_interval: Literal['half_open_0_T']
    solver_time_step_policy: Literal['solver_native_recorded']
    dtft_kernel: Literal['exp(+i*2*pi*f*n*dt)']
    dtft_measure: Literal['dt_weighted_sum']
    numerator_quantity: Literal['physical_pressure']
    numerator_record_policy: Literal[
        'solver_pressure_or_declared_primary_field_conversion'
    ]
    denominator_record: Literal[
        'physical_volume_velocity_samples_on_solver_time_grid'
    ]
    transfer_definition: Literal['pressure_over_volume_velocity']
    frequency_evaluation: Literal['direct_scored_frequency_dtft']
    source_spectrum_requirement: Literal['finite_nonzero_on_scored_grid']
    zero_padding: Literal['none']


class NumericalComparisonContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    coordinate_system: Literal['x_right_y_rear_z_up'] = 'x_right_y_rear_z_up'
    length_unit: Literal['m'] = 'm'
    pressure_unit: Literal['Pa'] = 'Pa'
    fourier_sign: Literal['exp(-i*omega*t)'] = 'exp(-i*omega*t)'
    phase_wrap: Literal['[-180,180)'] = '[-180,180)'
    time_zero_reference: Literal['source_excitation_t0'] = 'source_excitation_t0'
    floating_point: Literal['float64'] = 'float64'
    interpolation: Literal['none', 'nearest', 'linear_complex'] = 'none'
    window: str = 'none'
    filter: str = 'none'
    frequency_grid: BenchmarkFrequencyGrid
    time_step_s: float | None = Field(default=None, gt=0.0)
    observation_time_s: float | None = Field(default=None, gt=0.0)
    finite_record_transfer: FiniteRecordTransferContract | None = None

    @model_validator(mode='after')
    def finite_time_contract(self) -> 'NumericalComparisonContract':
        values = (self.time_step_s, self.observation_time_s)
        if any(value is not None and not isfinite(float(value)) for value in values):
            raise ValueError('time contract values must be finite')
        return self


class BenchmarkTolerance(BaseModel):
    model_config = ConfigDict(frozen=True)

    absolute: float | None = Field(default=None, ge=0.0)
    relative: float | None = Field(default=None, ge=0.0)
    phase_deg: float | None = Field(default=None, ge=0.0)
    null_mask_below_db: float | None = None
    statistical_stddev_max: float | None = Field(default=None, ge=0.0)
    minimum_difference: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def has_metric(self) -> 'BenchmarkTolerance':
        if all(
            value is None
            for value in (
                self.absolute,
                self.relative,
                self.phase_deg,
                self.statistical_stddev_max,
                self.minimum_difference,
            )
        ):
            raise ValueError('benchmark tolerance must define at least one comparison metric')
        values = (
            self.absolute,
            self.relative,
            self.phase_deg,
            self.null_mask_below_db,
            self.statistical_stddev_max,
            self.minimum_difference,
        )
        if any(value is not None and not isfinite(float(value)) for value in values):
            raise ValueError('benchmark tolerance values must be finite')
        return self


class BenchmarkExpectedSample(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_key: str = Field(min_length=1)
    frequency_hz: float | None = Field(default=None, gt=0.0)
    scalar_value: float | None = None
    real_value: float | None = None
    imag_value: float | None = None
    vector_value: tuple[float, float, float] | None = None

    @model_validator(mode='after')
    def exactly_one_value_representation(self) -> 'BenchmarkExpectedSample':
        scalar = self.scalar_value is not None
        complex_value = self.real_value is not None or self.imag_value is not None
        vector = self.vector_value is not None
        if sum((scalar, complex_value, vector)) != 1:
            raise ValueError('expected sample must use exactly one value representation')
        if complex_value and (self.real_value is None or self.imag_value is None):
            raise ValueError('complex sample requires both real_value and imag_value')
        values: list[float] = []
        if self.scalar_value is not None:
            values.append(self.scalar_value)
        if self.real_value is not None:
            values.extend((self.real_value, self.imag_value or 0.0))
        if self.vector_value is not None:
            values.extend(self.vector_value)
        if self.frequency_hz is not None:
            values.append(self.frequency_hz)
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('expected sample values must be finite')
        return self


class BenchmarkExpectedObservable(BaseModel):
    model_config = ConfigDict(frozen=True)

    observable_id: str = Field(min_length=1)
    kind: BenchmarkObservableKind
    unit: str = Field(min_length=1)
    reference_kind: BenchmarkReferenceKind
    reference_description: str = Field(min_length=1)
    acceptance_relation: BenchmarkAcceptanceRelation = 'matches_reference'
    tolerance: BenchmarkTolerance
    samples: tuple[BenchmarkExpectedSample, ...] = ()
    peer_fixture_id: str | None = None

    @model_validator(mode='after')
    def reference_contract(self) -> 'BenchmarkExpectedObservable':
        if self.reference_kind in {'analytical', 'closed_form'} and not self.samples:
            raise ValueError('analytical/closed-form observables require expected samples')
        needs_peer = self.acceptance_relation in {'matches_peer', 'must_differ_from_peer'}
        if needs_peer and not self.peer_fixture_id:
            raise ValueError('peer comparison requires peer_fixture_id')
        if not needs_peer and self.peer_fixture_id is not None:
            raise ValueError('peer_fixture_id is only valid for peer comparisons')
        if self.acceptance_relation == 'must_differ_from_peer' and self.tolerance.minimum_difference is None:
            raise ValueError('must_differ_from_peer requires minimum_difference')
        if self.acceptance_relation != 'must_differ_from_peer' and self.tolerance.minimum_difference is not None:
            raise ValueError('minimum_difference is only valid for must_differ_from_peer')
        return self


class BenchmarkResourceBudget(BaseModel):
    """R100B comparison budget. Performance limits are not physics correctness tolerances."""

    model_config = ConfigDict(frozen=True)

    cpu_thread_budget: int = Field(ge=1)
    ram_budget_mb: int = Field(ge=1)
    disk_budget_mb: int = Field(ge=1)
    workload_candidates: int = Field(ge=1)
    max_compile_s: float = Field(gt=0.0)
    max_solve_s: float = Field(gt=0.0)
    max_postprocess_s: float = Field(gt=0.0)
    max_output_mb: float = Field(gt=0.0)


class BenchmarkHardGate(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate_id: str = Field(min_length=1)
    category: Literal[
        'physics_correctness',
        'cpu_baseline',
        'windows_packaging',
        'license_redistribution',
        'required_capability',
        'reproducible_authority',
    ]
    applies_to: Literal['candidate', 'reference', 'both']
    requirement: str = Field(min_length=1)


class AcousticBenchmarkFixture(BaseModel):
    model_config = ConfigDict(frozen=True)

    fixture_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    benchmark_role: Literal['wave', 'geometric', 'hybrid']
    required_capabilities: tuple[BenchmarkCapability, ...] = Field(min_length=1)
    regions: tuple[AcousticRegion, ...] = Field(min_length=1)
    obstacles: tuple[AcousticObstacle, ...] = ()
    portals: tuple[AcousticPortal, ...] = ()
    terminations: tuple[BoundaryTermination, ...] = ()
    materials: tuple[AcousticMaterial, ...] = Field(min_length=1)
    boundaries: tuple[AcousticBoundary, ...] = Field(min_length=1)
    sources: tuple[BenchmarkSource, ...] = Field(min_length=1)
    receivers: tuple[BenchmarkReceiver, ...] = Field(min_length=1)
    environment: BenchmarkEnvironment
    comparison: NumericalComparisonContract
    observables: tuple[BenchmarkExpectedObservable, ...] = Field(min_length=1)
    resource_budget: BenchmarkResourceBudget
    random_seed: int | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def cross_references_and_capabilities(self) -> 'AcousticBenchmarkFixture':
        def unique(items: list[str], label: str) -> set[str]:
            if len(items) != len(set(items)):
                raise ValueError(f'{label} ids must be unique')
            return set(items)

        unique(list(self.required_capabilities), 'required capability')
        region_ids = unique([item.region_id for item in self.regions], 'region')
        material_ids = unique([item.material_id for item in self.materials], 'material')
        boundary_ids = unique([item.boundary_id for item in self.boundaries], 'boundary')
        unique([item.obstacle_id for item in self.obstacles], 'obstacle')
        unique([item.portal_id for item in self.portals], 'portal')
        unique([item.termination_id for item in self.terminations], 'termination')
        unique([item.source_id for item in self.sources], 'source')
        unique([item.receiver_id for item in self.receivers], 'receiver')
        unique([item.observable_id for item in self.observables], 'observable')

        bad_boundaries = [
            boundary.boundary_id for boundary in self.boundaries if boundary.material_id not in material_ids
        ]
        if bad_boundaries:
            raise ValueError(f'boundaries reference unknown materials: {bad_boundaries}')

        bad_faces = [
            f'{region.region_id}:{face.face_id}'
            for region in self.regions
            for face in region.faces
            if face.boundary_id not in boundary_ids
        ]
        if bad_faces:
            raise ValueError(f'region faces reference unknown boundaries: {bad_faces}')

        bad_obstacles = [
            obstacle.obstacle_id
            for obstacle in self.obstacles
            if obstacle.host_region_id not in region_ids
            or any(face.boundary_id not in boundary_ids for face in obstacle.faces)
        ]
        if bad_obstacles:
            raise ValueError(f'obstacles reference unknown authority: {bad_obstacles}')

        bad_portals = [
            portal.portal_id
            for portal in self.portals
            if portal.region_a_id not in region_ids or portal.region_b_id not in region_ids
        ]
        if bad_portals:
            raise ValueError(f'portals reference unknown regions: {bad_portals}')

        bad_terminations = [
            termination.termination_id
            for termination in self.terminations
            if termination.region_id not in region_ids
            or (termination.boundary_id is not None and termination.boundary_id not in boundary_ids)
        ]
        if bad_terminations:
            raise ValueError(f'terminations reference unknown authority: {bad_terminations}')

        bad_sources = [item.source_id for item in self.sources if item.region_id not in region_ids]
        bad_receivers = [item.receiver_id for item in self.receivers if item.region_id not in region_ids]
        if bad_sources:
            raise ValueError(f'sources reference unknown regions: {bad_sources}')
        if bad_receivers:
            raise ValueError(f'receivers reference unknown regions: {bad_receivers}')

        material_by_id = {item.material_id: item for item in self.materials}
        boundary_materials = [material_by_id[item.material_id] for item in self.boundaries]
        capabilities = set(self.required_capabilities)
        if 'wave_rigid' in capabilities and not any(item.wave_model == 'rigid' for item in boundary_materials):
            raise ValueError('wave_rigid capability requires an explicit rigid wave material')
        if 'wave_impedance' in capabilities and not any(
            item.wave_model == 'specific_impedance_table' for item in boundary_materials
        ):
            raise ValueError('wave_impedance capability requires explicit phase-bearing impedance data')
        if 'wave_radiation_termination' in capabilities and not any(
            item.kind == 'radiation' for item in self.terminations
        ):
            raise ValueError(
                'wave_radiation_termination capability requires an explicit radiation termination'
            )
        if 'portal_continuity' in capabilities and not self.portals:
            raise ValueError('portal_continuity capability requires an explicit AcousticPortal')
        if 'geometric_specular' in capabilities and not any(
            item.geometric_model == 'banded' for item in boundary_materials
        ):
            raise ValueError('geometric_specular capability requires explicit geometric material bands')
        if 'geometric_scattering' in capabilities and not any(
            any(band.scattering > 0.0 for band in item.geometric_bands)
            for item in boundary_materials
        ):
            raise ValueError('geometric_scattering capability requires non-zero scattering data')
        if 'stochastic_rays' in capabilities and self.random_seed is None:
            raise ValueError('stochastic_rays capability requires random_seed authority')
        return self


class AcousticBenchmarkManifest(BaseModel):
    """Immutable R100A authority consumed by every R100B candidate/reference adapter."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100a-2', 'r100a-3', 'r100a-4'] = 'r100a-4'
    manifest_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    purpose: str = Field(min_length=1)
    hard_gates: tuple[BenchmarkHardGate, ...] = Field(min_length=1)
    fixtures: tuple[AcousticBenchmarkFixture, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_fixture_and_gate_ids(self) -> 'AcousticBenchmarkManifest':
        expected_revision = {'r100a-2': 2, 'r100a-3': 3, 'r100a-4': 4}[self.schema_version]
        if self.revision != expected_revision:
            raise ValueError(
                f'{self.schema_version} requires revision {expected_revision}, got {self.revision}'
            )

        fixture_ids = [item.fixture_id for item in self.fixtures]
        gate_ids = [item.gate_id for item in self.hard_gates]
        if len(fixture_ids) != len(set(fixture_ids)):
            raise ValueError('fixture ids must be unique')
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError('hard gate ids must be unique')
        required_gate_categories = {
            'physics_correctness',
            'cpu_baseline',
            'windows_packaging',
            'license_redistribution',
            'required_capability',
            'reproducible_authority',
        }
        present_gate_categories = {item.category for item in self.hard_gates}
        missing_gate_categories = sorted(required_gate_categories - present_gate_categories)
        if missing_gate_categories:
            raise ValueError(f'manifest is missing required hard gate categories: {missing_gate_categories}')
        known_fixtures = set(fixture_ids)
        dangling_peers = [
            f'{fixture.fixture_id}:{observable.observable_id}->{observable.peer_fixture_id}'
            for fixture in self.fixtures
            for observable in fixture.observables
            if observable.peer_fixture_id is not None and observable.peer_fixture_id not in known_fixtures
        ]
        if dangling_peers:
            raise ValueError(f'observables reference unknown peer fixtures: {dangling_peers}')

        if self.schema_version == 'r100a-2':
            for fixture in self.fixtures:
                if fixture.comparison.finite_record_transfer is not None:
                    raise ValueError(
                        f'R100A-2 fixture {fixture.fixture_id} cannot carry '
                        'R100A-4 finite-record transfer semantics'
                    )
                if 'wave_radiation_termination' in fixture.required_capabilities:
                    raise ValueError(
                        f'R100A-2 fixture {fixture.fixture_id} cannot declare '
                        'wave_radiation_termination capability'
                    )
                for termination in fixture.terminations:
                    radiation_fields = (
                        termination.radiation_model,
                        termination.normal_convention,
                        termination.characteristic_impedance_model,
                        termination.pressure_velocity_equation,
                        termination.wavenumber_equation,
                        termination.helmholtz_robin_equation,
                    )
                    if any(value is not None for value in radiation_fields):
                        raise ValueError(
                            f'R100A-2 radiation termination {termination.termination_id} '
                            'cannot carry R100A-3 radiation semantics'
                        )

        if self.schema_version in {'r100a-2', 'r100a-3'}:
            for fixture in self.fixtures:
                if fixture.comparison.finite_record_transfer is not None:
                    raise ValueError(
                        f'{self.schema_version.upper()} fixture {fixture.fixture_id} cannot carry '
                        'R100A-4 finite-record transfer semantics'
                    )
                if any(
                    observable.kind == 'complex_pressure_transfer_pa_per_m3_s'
                    for observable in fixture.observables
                ):
                    raise ValueError(
                        f'{self.schema_version.upper()} fixture {fixture.fixture_id} cannot carry '
                        'R100A-4 complex pressure-transfer observable semantics'
                    )

        if self.schema_version in {'r100a-3', 'r100a-4'}:
            for fixture in self.fixtures:
                radiation_terminations = [
                    item for item in fixture.terminations if item.kind == 'radiation'
                ]
                if not radiation_terminations:
                    continue
                if 'wave_radiation_termination' not in fixture.required_capabilities:
                    raise ValueError(
                        f'{self.schema_version.upper()} radiation fixture '
                        f'{fixture.fixture_id} must require '
                        'wave_radiation_termination capability'
                    )
                for termination in radiation_terminations:
                    radiation_fields = (
                        termination.radiation_model,
                        termination.normal_convention,
                        termination.characteristic_impedance_model,
                        termination.pressure_velocity_equation,
                        termination.wavenumber_equation,
                        termination.helmholtz_robin_equation,
                    )
                    if termination.boundary_id is None or any(
                        value is None for value in radiation_fields
                    ):
                        raise ValueError(
                            f'{self.schema_version.upper()} radiation termination '
                            f'{termination.termination_id} requires explicit '
                            'boundary/model/sign/normal authority'
                        )

        if self.schema_version == 'r100a-4':
            finite_record_fixture_ids = {
                fixture.fixture_id
                for fixture in self.fixtures
                if fixture.comparison.finite_record_transfer is not None
            }
            required_finite_record_fixture_ids = {
                fixture.fixture_id
                for fixture in self.fixtures
                if fixture.comparison.observation_time_s is not None
                and any(
                    observable.acceptance_relation == 'monotonic_convergence'
                    or observable.reference_kind == 'independent_solver'
                    for observable in fixture.observables
                )
            }
            if finite_record_fixture_ids != required_finite_record_fixture_ids:
                raise ValueError(
                    'R100A-4 finite-record transfer fixtures must be exactly '
                    f'{sorted(required_finite_record_fixture_ids)}, got '
                    f'{sorted(finite_record_fixture_ids)}'
                )

            for fixture in self.fixtures:
                contract = fixture.comparison.finite_record_transfer
                if contract is None:
                    continue
                comparison = fixture.comparison
                if comparison.time_step_s is not None:
                    raise ValueError(
                        f'R100A-4 finite-record fixture {fixture.fixture_id} '
                        'must leave time_step_s solver-native'
                    )
                if comparison.observation_time_s != 2.0:
                    raise ValueError(
                        f'R100A-4 finite-record fixture {fixture.fixture_id} '
                        'must use the frozen 2 s record'
                    )
                if (
                    comparison.time_zero_reference != 'source_excitation_t0'
                    or comparison.fourier_sign != 'exp(-i*omega*t)'
                    or comparison.window != 'none'
                    or comparison.filter != 'none'
                ):
                    raise ValueError(
                        f'R100A-4 finite-record fixture {fixture.fixture_id} '
                        'has incompatible time/Fourier/window/filter authority'
                    )
                if len(fixture.sources) != 1:
                    raise ValueError(
                        f'R100A-4 finite-record fixture {fixture.fixture_id} '
                        'requires exactly one source'
                    )
                source = fixture.sources[0]
                if (
                    source.normalization != 'volume_velocity_m3_s'
                    or float(source.amplitude) != 1.0
                    or float(source.phase_deg) != 0.0
                    or source.directivity != 'omnidirectional'
                ):
                    raise ValueError(
                        f'R100A-4 finite-record fixture {fixture.fixture_id} '
                        'requires the frozen unit volume-velocity source'
                    )

                for observable in fixture.observables:
                    if observable.acceptance_relation == 'monotonic_convergence':
                        if (
                            observable.kind != 'complex_pressure_transfer_pa_per_m3_s'
                            or observable.unit != 'Pa/(m3/s)'
                        ):
                            raise ValueError(
                                f'R100A-4 finite-record convergence observable '
                                f'{fixture.fixture_id}:{observable.observable_id} must use '
                                'complex_pressure_transfer_pa_per_m3_s / Pa/(m3/s)'
                            )
                    if (
                        observable.reference_kind == 'independent_solver'
                        and observable.kind == 'transfer_magnitude_db'
                        and observable.unit != 'dB re 1 Pa/(m3/s)'
                    ):
                        raise ValueError(
                            f'R100A-4 finite-record independent transfer magnitude '
                            f'{fixture.fixture_id}:{observable.observable_id} must use '
                            'dB re 1 Pa/(m3/s)'
                        )
        return self

    def canonical_json(self) -> str:
        return canonical_benchmark_json(self.model_dump(mode='json'))

    def semantic_hash(self) -> str:
        return sha256(self.canonical_json().encode('utf-8')).hexdigest()


def load_acoustic_benchmark_manifest(path: str | Path) -> AcousticBenchmarkManifest:
    raw = Path(path).read_text(encoding='utf-8')
    decoded = json.loads(raw)
    return AcousticBenchmarkManifest.model_validate(decoded)
