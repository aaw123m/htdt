"""Append-only persistence for REV59-DIGCHAIN authorities.

Eleven tables in one repository — clock jitter (#745), word-length /
dither path (#744), playback SRC (#739) and interchannel crosstalk
(#650):

* ``cad_jitter_profiles`` / ``cad_jitter_observations`` /
  ``cad_jitter_transfer_measurements`` /
  ``cad_converter_jitter_susceptibility``
* ``cad_dither_profiles`` / ``cad_digital_path_transforms``
* ``cad_playback_src_profiles`` / ``cad_src_qualifications`` /
  ``cad_clock_domain_crossings``
* ``cad_interchannel_leakage_measurements`` /
  ``cad_channel_separation_qualifications``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_clock_jitter import (
    ConverterJitterSusceptibility,
    JitterTransferMeasurement,
    SampleClockJitterObservation,
    SampleClockJitterProfile,
)
from .cad_wordlength_path import (
    DigitalPathTransformRecord,
    DitherNoiseShapeProfile,
)
from .cad_playback_src import (
    ClockDomainCrossingRecord,
    PlaybackSrcProfile,
    SrcQualificationRecord,
)
from .cad_interchannel_crosstalk import (
    ChannelSeparationQualification,
    InterchannelLeakageMeasurement,
)


class SignalIntegrityConflictError(ValueError):
    """A signal-integrity save violated append-only identity rules."""


class SignalIntegrityIntegrityError(ValueError):
    """A stored signal-integrity row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SignalIntegrityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SignalIntegrityIntegrityError(
            'record id does not match its sealed sha256'
        )


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise SignalIntegrityConflictError(
                f'{self.table} records are append-only'
            )
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise SignalIntegrityIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise SignalIntegrityIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise SignalIntegrityIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadSignalIntegrityRepository:
    """Native storage for the #745/#744/#739/#650 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_jitter_profiles',
                'cad_jitter_observations',
                'cad_jitter_transfer_measurements',
                'cad_converter_jitter_susceptibility',
                'cad_dither_profiles',
                'cad_digital_path_transforms',
                'cad_playback_src_profiles',
                'cad_src_qualifications',
                'cad_clock_domain_crossings',
                'cad_interchannel_leakage_measurements',
                'cad_channel_separation_qualifications',
            )
        self.jitter_profiles = _SealedStore(
            self._connect, 'cad_jitter_profiles',
            SampleClockJitterProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('spectrum_capable', 'spectrum_capable'),
            ),
        )
        self.jitter_observations = _SealedStore(
            self._connect, 'cad_jitter_observations',
            SampleClockJitterObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('jitter_kind', 'jitter_kind'),
            ),
        )
        self.jitter_transfers = _SealedStore(
            self._connect, 'cad_jitter_transfer_measurements',
            JitterTransferMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
            ),
        )
        self.converter_susceptibility = _SealedStore(
            self._connect, 'cad_converter_jitter_susceptibility',
            ConverterJitterSusceptibility, 'susceptibility_id',
            'susceptibility_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('converter_ref_id', 'converter_ref'),
            ),
        )
        self.dither_profiles = _SealedStore(
            self._connect, 'cad_dither_profiles',
            DitherNoiseShapeProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('dither_kind', 'dither_kind'),
            ),
        )
        self.path_transforms = _SealedStore(
            self._connect, 'cad_digital_path_transforms',
            DigitalPathTransformRecord, 'transform_id',
            'transform_sha256',
            (
                ('document_id', '__document_id__'),
                ('transform_kind', 'transform_kind'),
            ),
        )
        self.src_profiles = _SealedStore(
            self._connect, 'cad_playback_src_profiles',
            PlaybackSrcProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('algorithm', 'algorithm'),
            ),
        )
        self.src_qualifications = _SealedStore(
            self._connect, 'cad_src_qualifications',
            SrcQualificationRecord, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('src_profile_ref_id', 'src_profile_ref'),
            ),
        )
        self.clock_crossings = _SealedStore(
            self._connect, 'cad_clock_domain_crossings',
            ClockDomainCrossingRecord, 'crossing_id', 'crossing_sha256',
            (
                ('document_id', '__document_id__'),
                ('declared_kind', 'declared_kind'),
            ),
        )
        self.leakage_measurements = _SealedStore(
            self._connect, 'cad_interchannel_leakage_measurements',
            InterchannelLeakageMeasurement, 'measurement_id',
            'measurement_sha256',
            (
                ('document_id', '__document_id__'),
                ('stage', 'stage'),
                ('method', 'method'),
            ),
        )
        self.separation_qualifications = _SealedStore(
            self._connect, 'cad_channel_separation_qualifications',
            ChannelSeparationQualification, 'qualification_id',
            'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                ('stage', 'stage'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # Wrappers used by the audit replay chain and callers.
    def save_jitter_profile(
        self, record: SampleClockJitterProfile
    ) -> None:
        self.jitter_profiles.save(record)

    def get_jitter_profile(
        self, profile_id: str
    ) -> SampleClockJitterProfile | None:
        return self.jitter_profiles.get(profile_id)

    def save_jitter_observation(
        self, record: SampleClockJitterObservation
    ) -> None:
        self.jitter_observations.save(record)

    def get_jitter_observation(
        self, observation_id: str
    ) -> SampleClockJitterObservation | None:
        return self.jitter_observations.get(observation_id)

    def save_jitter_transfer(
        self, record: JitterTransferMeasurement
    ) -> None:
        self.jitter_transfers.save(record)

    def get_jitter_transfer(
        self, measurement_id: str
    ) -> JitterTransferMeasurement | None:
        return self.jitter_transfers.get(measurement_id)

    def save_converter_susceptibility(
        self, record: ConverterJitterSusceptibility
    ) -> None:
        self.converter_susceptibility.save(record)

    def get_converter_susceptibility(
        self, susceptibility_id: str
    ) -> ConverterJitterSusceptibility | None:
        return self.converter_susceptibility.get(susceptibility_id)

    def save_dither_profile(
        self, record: DitherNoiseShapeProfile
    ) -> None:
        self.dither_profiles.save(record)

    def get_dither_profile(
        self, profile_id: str
    ) -> DitherNoiseShapeProfile | None:
        return self.dither_profiles.get(profile_id)

    def save_path_transform(
        self, record: DigitalPathTransformRecord
    ) -> None:
        self.path_transforms.save(record)

    def get_path_transform(
        self, transform_id: str
    ) -> DigitalPathTransformRecord | None:
        return self.path_transforms.get(transform_id)

    def save_src_profile(self, record: PlaybackSrcProfile) -> None:
        self.src_profiles.save(record)

    def get_src_profile(
        self, profile_id: str
    ) -> PlaybackSrcProfile | None:
        return self.src_profiles.get(profile_id)

    def save_src_qualification(
        self, record: SrcQualificationRecord
    ) -> None:
        self.src_qualifications.save(record)

    def get_src_qualification(
        self, qualification_id: str
    ) -> SrcQualificationRecord | None:
        return self.src_qualifications.get(qualification_id)

    def save_clock_crossing(
        self, record: ClockDomainCrossingRecord
    ) -> None:
        self.clock_crossings.save(record)

    def get_clock_crossing(
        self, crossing_id: str
    ) -> ClockDomainCrossingRecord | None:
        return self.clock_crossings.get(crossing_id)

    def save_leakage_measurement(
        self, record: InterchannelLeakageMeasurement
    ) -> None:
        self.leakage_measurements.save(record)

    def get_leakage_measurement(
        self, measurement_id: str
    ) -> InterchannelLeakageMeasurement | None:
        return self.leakage_measurements.get(measurement_id)

    def save_separation_qualification(
        self, record: ChannelSeparationQualification
    ) -> None:
        self.separation_qualifications.save(record)

    def get_separation_qualification(
        self, qualification_id: str
    ) -> ChannelSeparationQualification | None:
        return self.separation_qualifications.get(qualification_id)
