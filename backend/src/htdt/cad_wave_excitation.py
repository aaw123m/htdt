from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import (
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
)
from .cad_equipment_repository import CadEquipmentRepository
from .cad_r110_source import R110CompiledSourceModel
from .cad_r110_source_repository import CadR110SourceRepository
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema


WAVE_EXCITATION_AUTHORITY_VERSION = 'r110-wave-excitation-1'
WAVE_SOURCE_BINDING_AUTHORITY_VERSION = 'r110-wave-source-binding-1'

WaveExcitationModel = Literal[
    'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'
]
WaveExcitationPhasorConvention = Literal['exp(-i*omega*t)']


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ComplexVolumeVelocitySample(BaseModel):
    """Absolute complex acoustic volume velocity in SI units."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    real_m3_s: float
    imag_m3_s: float

    @model_validator(mode='after')
    def finite_sample(self) -> 'ComplexVolumeVelocitySample':
        if not all(
            isfinite(float(value))
            for value in (
                self.frequency_hz,
                self.real_m3_s,
                self.imag_m3_s,
            )
        ):
            raise ValueError('wave-excitation sample values must be finite')
        return self


class AcousticWaveExcitationAuthority(BaseModel):
    """Explicit acoustic source-strength authority.

    This is not inferred from electrical sensitivity. It represents an exact
    equivalent-monopole volume-velocity spectrum at the equipment acoustic
    reference point.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r110-wave-excitation-1'
    ] = WAVE_EXCITATION_AUTHORITY_VERSION
    excitation_id: str = Field(pattern=r'^acoustic-wave-excitation:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    excitation_model: WaveExcitationModel = (
        'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'
    )
    quantity: Literal['complex_volume_velocity_m3_s'] = 'complex_volume_velocity_m3_s'
    phasor_convention: WaveExcitationPhasorConvention = 'exp(-i*omega*t)'
    reference_semantics: Literal[
        'equipment_acoustic_reference_point'
    ] = 'equipment_acoustic_reference_point'

    samples: tuple[ComplexVolumeVelocitySample, ...] = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain
    interpolation: InterpolationProvenance
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    approximation_note: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_authority(self) -> 'AcousticWaveExcitationAuthority':
        frequencies = [float(item.frequency_hz) for item in self.samples]
        if frequencies != sorted(frequencies) or len(frequencies) != len(set(frequencies)):
            raise ValueError(
                'wave-excitation frequencies must be unique and sorted'
            )
        if (
            float(self.valid_frequency_domain.minimum_hz) != frequencies[0]
            or float(self.valid_frequency_domain.maximum_hz) != frequencies[-1]
        ):
            raise ValueError(
                'wave-excitation valid frequency domain must equal sample bounds'
            )
        provenance_hashes = {item.source_sha256 for item in self.provenance}
        if self.interpolation.provenance.source_sha256 not in provenance_hashes:
            raise ValueError(
                'wave-excitation interpolation provenance must resolve to '
                'an excitation provenance item'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('AcousticWaveExcitationAuthority semantic hash mismatch')
        if self.excitation_id != f'acoustic-wave-excitation:{expected}':
            raise ValueError('AcousticWaveExcitationAuthority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'excitation_id', 'semantic_sha256'},
        )


class WaveSourceExcitationBinding(BaseModel):
    """Exact composition of one persisted R110 source and one excitation authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r110-wave-source-binding-1'
    ] = WAVE_SOURCE_BINDING_AUTHORITY_VERSION
    binding_id: str = Field(pattern=r'^wave-source-excitation-binding:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    r110_compiled_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    excitation_id: str = Field(pattern=r'^acoustic-wave-excitation:[0-9a-f]{64}$')
    excitation_authority_version: str = Field(min_length=1)
    excitation_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    valid_frequency_domain: FrequencyDomain
    source_model: WaveExcitationModel

    @model_validator(mode='after')
    def validate_binding(self) -> 'WaveSourceExcitationBinding':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('WaveSourceExcitationBinding semantic hash mismatch')
        if self.binding_id != f'wave-source-excitation-binding:{expected}':
            raise ValueError('WaveSourceExcitationBinding id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'binding_id', 'semantic_sha256'},
        )


def build_acoustic_wave_excitation_authority(
    *,
    definition_id: str,
    definition_version: str,
    definition_sha256: str,
    samples: Sequence[ComplexVolumeVelocitySample],
    interpolation: InterpolationProvenance,
    provenance: Sequence[EquipmentDataProvenance],
    approximation_note: str,
) -> AcousticWaveExcitationAuthority:
    sample_tuple = tuple(samples)
    if len(sample_tuple) < 2:
        raise ValueError(
            'wave-excitation authority requires at least two frequency samples'
        )
    frequencies = [float(item.frequency_hz) for item in sample_tuple]
    if frequencies != sorted(frequencies) or len(frequencies) != len(set(frequencies)):
        raise ValueError('wave-excitation frequencies must be unique and sorted')
    domain = FrequencyDomain(
        minimum_hz=frequencies[0],
        maximum_hz=frequencies[-1],
    )
    core = {
        'authority_version': WAVE_EXCITATION_AUTHORITY_VERSION,
        'definition_id': definition_id,
        'definition_version': definition_version,
        'definition_sha256': definition_sha256,
        'excitation_model': (
            'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'
        ),
        'quantity': 'complex_volume_velocity_m3_s',
        'phasor_convention': 'exp(-i*omega*t)',
        'reference_semantics': 'equipment_acoustic_reference_point',
        'samples': [item.model_dump(mode='json') for item in sample_tuple],
        'valid_frequency_domain': domain.model_dump(mode='json'),
        'interpolation': interpolation.model_dump(mode='json'),
        'provenance': [item.model_dump(mode='json') for item in provenance],
        'approximation_note': approximation_note,
    }
    digest = _digest(core)
    return AcousticWaveExcitationAuthority(
        excitation_id=f'acoustic-wave-excitation:{digest}',
        semantic_sha256=digest,
        definition_id=definition_id,
        definition_version=definition_version,
        definition_sha256=definition_sha256,
        samples=sample_tuple,
        valid_frequency_domain=domain,
        interpolation=interpolation,
        provenance=tuple(provenance),
        approximation_note=approximation_note,
    )


def bind_wave_excitation_to_r110_source(
    *,
    source: R110CompiledSourceModel,
    excitation: AcousticWaveExcitationAuthority,
) -> WaveSourceExcitationBinding:
    source = R110CompiledSourceModel.model_validate(
        source.model_dump(mode='python')
    )
    excitation = AcousticWaveExcitationAuthority.model_validate(
        excitation.model_dump(mode='python')
    )
    if (
        excitation.definition_id != source.equipment_definition_id
        or excitation.definition_version != source.equipment_definition_version
        or excitation.definition_sha256 != source.equipment_definition_sha256
    ):
        raise ValueError(
            'wave-excitation EquipmentDefinition does not match exact R110 source'
        )
    core = {
        'authority_version': WAVE_SOURCE_BINDING_AUTHORITY_VERSION,
        'r110_compiled_source_sha256': source.semantic_sha256,
        'source_entity_id': source.source_entity_id,
        'equipment_definition_id': source.equipment_definition_id,
        'equipment_definition_version': source.equipment_definition_version,
        'equipment_definition_sha256': source.equipment_definition_sha256,
        'excitation_id': excitation.excitation_id,
        'excitation_authority_version': excitation.authority_version,
        'excitation_semantic_sha256': excitation.semantic_sha256,
        'valid_frequency_domain': excitation.valid_frequency_domain.model_dump(
            mode='json'
        ),
        'source_model': excitation.excitation_model,
    }
    digest = _digest(core)
    return WaveSourceExcitationBinding(
        binding_id=f'wave-source-excitation-binding:{digest}',
        semantic_sha256=digest,
        r110_compiled_source_sha256=source.semantic_sha256,
        source_entity_id=source.source_entity_id,
        equipment_definition_id=source.equipment_definition_id,
        equipment_definition_version=source.equipment_definition_version,
        equipment_definition_sha256=source.equipment_definition_sha256,
        excitation_id=excitation.excitation_id,
        excitation_authority_version=excitation.authority_version,
        excitation_semantic_sha256=excitation.semantic_sha256,
        valid_frequency_domain=excitation.valid_frequency_domain,
        source_model=excitation.excitation_model,
    )


class CadWaveExcitationRepository:
    """Append-only explicit acoustic excitation and R110-binding persistence."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        equipment_repository: CadEquipmentRepository | None = None,
        r110_repository: CadR110SourceRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.equipment_repository = (
            equipment_repository
            if equipment_repository is not None
            else CadEquipmentRepository(scene_repository)
        )
        self.r110_repository = (
            r110_repository
            if r110_repository is not None
            else CadR110SourceRepository(
                scene_repository,
                equipment_repository=self.equipment_repository,
            )
        )
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('Equipment', self.equipment_repository),
            ('R110', self.r110_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'wave excitation and {label} repositories must share '
                    'one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_acoustic_wave_excitations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    excitation_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    equipment_definition_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_wave_excitation_equipment_seq
                    ON cad_acoustic_wave_excitations(
                        equipment_definition_sha256,
                        seq ASC
                    );

                CREATE TABLE IF NOT EXISTS cad_wave_source_excitation_bindings (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    binding_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    r110_compiled_source_sha256 TEXT NOT NULL,
                    excitation_id TEXT NOT NULL,
                    excitation_semantic_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(excitation_id)
                        REFERENCES cad_acoustic_wave_excitations(excitation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_wave_source_binding_r110_seq
                    ON cad_wave_source_excitation_bindings(
                        r110_compiled_source_sha256,
                        seq ASC
                    );
                """
            )

    def _validate_excitation(
        self,
        excitation: AcousticWaveExcitationAuthority,
    ) -> AcousticWaveExcitationAuthority:
        excitation = AcousticWaveExcitationAuthority.model_validate(
            excitation.model_dump(mode='python')
        )
        definition = self.equipment_repository.get_definition_by_hash(
            excitation.definition_sha256
        )
        if definition is None:
            raise ValueError(
                'wave excitation references missing EquipmentDefinition'
            )
        if (
            definition.definition_id != excitation.definition_id
            or definition.version != excitation.definition_version
        ):
            raise ValueError('wave excitation EquipmentDefinition identity mismatch')
        return excitation

    def save_excitation(
        self,
        excitation: AcousticWaveExcitationAuthority,
    ) -> AcousticWaveExcitationAuthority:
        excitation = self._validate_excitation(excitation)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_wave_excitations
                WHERE excitation_id=?
                """,
                (excitation.excitation_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticWaveExcitationAuthority.model_validate_json(
                    existing['payload_json']
                )
                if persisted != excitation:
                    raise ValueError(
                        'wave excitation id exists with different semantics'
                    )
                return self._validate_excitation(persisted)
            connection.execute(
                """
                INSERT INTO cad_acoustic_wave_excitations(
                    excitation_id,
                    semantic_sha256,
                    equipment_definition_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    excitation.excitation_id,
                    excitation.semantic_sha256,
                    excitation.definition_sha256,
                    excitation.model_dump_json(),
                    _utc_now(),
                ),
            )
        return excitation

    def get_excitation(
        self,
        excitation_id: str,
    ) -> AcousticWaveExcitationAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_wave_excitations
                WHERE excitation_id=?
                """,
                (excitation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_excitation(
            AcousticWaveExcitationAuthority.model_validate_json(
                row['payload_json']
            )
        )

    def get_excitation_by_hash(
        self,
        semantic_sha256: str,
    ) -> AcousticWaveExcitationAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_wave_excitations
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_excitation(
            AcousticWaveExcitationAuthority.model_validate_json(
                row['payload_json']
            )
        )

    def _validate_binding(
        self,
        binding: WaveSourceExcitationBinding,
    ) -> WaveSourceExcitationBinding:
        binding = WaveSourceExcitationBinding.model_validate(
            binding.model_dump(mode='python')
        )
        source = self.r110_repository.get_model(
            binding.r110_compiled_source_sha256
        )
        if source is None:
            raise ValueError(
                'wave source binding references missing R110CompiledSourceModel'
            )
        excitation = self.get_excitation(binding.excitation_id)
        if excitation is None:
            raise ValueError(
                'wave source binding references missing excitation authority'
            )
        if excitation.semantic_sha256 != binding.excitation_semantic_sha256:
            raise ValueError('wave source binding excitation hash mismatch')
        recomputed = bind_wave_excitation_to_r110_source(
            source=source,
            excitation=excitation,
        )
        if recomputed != binding:
            raise ValueError(
                'wave source binding does not reproduce from exact authorities'
            )
        return binding

    def save_binding(
        self,
        binding: WaveSourceExcitationBinding,
    ) -> WaveSourceExcitationBinding:
        binding = self._validate_binding(binding)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_wave_source_excitation_bindings
                WHERE binding_id=?
                """,
                (binding.binding_id,),
            ).fetchone()
            if existing is not None:
                persisted = WaveSourceExcitationBinding.model_validate_json(
                    existing['payload_json']
                )
                if persisted != binding:
                    raise ValueError(
                        'wave source binding id exists with different semantics'
                    )
                return self._validate_binding(persisted)
            connection.execute(
                """
                INSERT INTO cad_wave_source_excitation_bindings(
                    binding_id,
                    semantic_sha256,
                    r110_compiled_source_sha256,
                    excitation_id,
                    excitation_semantic_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.semantic_sha256,
                    binding.r110_compiled_source_sha256,
                    binding.excitation_id,
                    binding.excitation_semantic_sha256,
                    binding.model_dump_json(),
                    _utc_now(),
                ),
            )
        return binding

    def get_binding(
        self,
        binding_id: str,
    ) -> WaveSourceExcitationBinding | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_wave_source_excitation_bindings
                WHERE binding_id=?
                """,
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_binding(
            WaveSourceExcitationBinding.model_validate_json(
                row['payload_json']
            )
        )
