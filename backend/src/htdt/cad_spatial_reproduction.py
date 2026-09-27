"""Spatial reproduction dataset profiles — SOFA-first (#809, SRP10).

Registers HRTF datasets as normalized, auditable artifacts instead of
baking HRTF data into solver code.

- ``SpatialDatasetLicense`` makes per-dataset licensing explicit:
  attribution, redistribution and research-only restrictions are
  recorded with the profile, not assumed.
- ``SpatialReproductionProfile`` binds dataset identity + conventions.
  Only the ``SimpleFreeFieldHRIR`` convention is admitted: free-field
  HRIRs are the project's baseline; BRIR (binaural room impulse
  response) datasets are rejected — the room's own acoustic model must
  never be convolved with a second room baked into the dataset
  (double-room convolution).
- Coordinate system is normalized at intake: azimuth degrees
  counter-clockwise from front (0° front, +90° left), elevation
  degrees (+up), distance metres — matching SOFA's spherical
  convention. Both 0..360° and -180..180° inputs normalize to
  [-180, 180).
- ``load_sofa_dataset_profile`` parses a .sofa (netCDF4/HDF5) file via
  h5py — lazily imported so non-spatial flows work without it — and
  validates the required SOFA variables and convention attributes.
  ``personalization_scope`` distinguishes generic vs individualized
  (subject-specific) datasets explicitly.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ingress import read_file_bounded
from .limits import MAX_ATTACHMENT_BYTES


SpatialDatasetLicense = Literal[
    'cc0_public',
    'cc_by',
    'cc_by_sa',
    'cc_by_nc',
    'research_only',
    'proprietary_purchased',
    'individualized_private',
    'unknown',
]

SpatialConvention = Literal['SimpleFreeFieldHRIR']

PersonalizationScope = Literal['generic', 'individualized', 'mixed']

SPATIAL_PROFILE_SCHEMA_VERSION = 1


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def normalize_spherical_position(
    *,
    azimuth_deg: float,
    elevation_deg: float,
    distance_m: float,
) -> tuple[float, float, float]:
    """Normalize (azimuth, elevation, distance) to the project frame.

    Frame: azimuth 0° = front, + = counter-clockwise (left); wrapped to
    [-180, 180). Elevation + = up, clamped to [-90, 90]. Distance metres.
    Inputs are expected in the SOFA spherical convention already.
    """
    azimuth = float(azimuth_deg) % 360.0
    if azimuth >= 180.0:
        azimuth -= 360.0
    elevation = max(-90.0, min(90.0, float(elevation_deg)))
    distance = float(distance_m)
    if distance <= 0.0:
        raise ValueError('source distance must be positive')
    return azimuth, elevation, distance


class SpatialMeasurementPoint(BaseModel):
    """One normalized source position of a SOFA dataset."""

    model_config = ConfigDict(frozen=True)

    measurement_index: int = Field(ge=0)
    azimuth_deg: float = Field(ge=-180.0, lt=180.0)
    elevation_deg: float = Field(ge=-90.0, le=90.0)
    distance_m: float = Field(gt=0.0)


class SpatialReproductionProfile(BaseModel):
    """Normalized spatial reproduction profile (SRP10, #809).

    Owns dataset authority + conventions only — it records where the
    HRTF data lives (content hash), which convention it follows, its
    licensing, and its personalization scope. The impulse-response
    samples themselves are never copied into the profile."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    dataset_name: str = Field(min_length=1)
    source_file_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    convention: SpatialConvention
    sample_rate_hz: float = Field(gt=0.0)
    measurement_count: int = Field(ge=1)
    emitter_count: int = Field(ge=1)
    receiver_count: int = Field(ge=1)
    source_points: tuple[SpatialMeasurementPoint, ...] = ()
    personalization_scope: PersonalizationScope
    license_kind: SpatialDatasetLicense
    license_note: str = ''
    global_title: str = ''
    global_organization: str = ''
    global_listener_short_name: str = ''
    opaque_attributes: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'SpatialReproductionProfile':
        if not self.profile_id.startswith('spatial-profile:'):
            raise ValueError(
                'profile id must use spatial-profile: prefix'
            )
        if len(self.source_points) != self.measurement_count:
            raise ValueError('source points must match measurement_count')
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('spatial profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'profile_id': self.profile_id,
            'profile_version': self.profile_version,
            'created_at_utc': self.created_at_utc,
            'dataset_name': self.dataset_name,
            'source_file_sha256': self.source_file_sha256,
            'convention': self.convention,
            'sample_rate_hz': self.sample_rate_hz,
            'measurement_count': self.measurement_count,
            'emitter_count': self.emitter_count,
            'receiver_count': self.receiver_count,
            'source_points': [
                point.model_dump(mode='json')
                for point in self.source_points
            ],
            'personalization_scope': self.personalization_scope,
            'license_kind': self.license_kind,
            'license_note': self.license_note,
            'global_title': self.global_title,
            'global_organization': self.global_organization,
            'global_listener_short_name': self.global_listener_short_name,
            'opaque_attributes': list(self.opaque_attributes),
        }


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def load_sofa_dataset_profile(
    sofa_path: str | Path,
    *,
    profile_id: str,
    profile_version: str,
    personalization_scope: PersonalizationScope,
    license_kind: SpatialDatasetLicense,
    license_note: str = '',
    created_at_utc: str,
) -> SpatialReproductionProfile:
    """Load and normalize one SOFA dataset into an SRP10 profile.

    Validates the SOFA convention (``SimpleFreeFieldHRIR`` only —
    BRIR/room datasets are rejected to prevent double-room convolution),
    extracts the spherical source grid (normalizing it into the project
    frame), and binds the raw file content hash as dataset authority.
    Unrecognized attributes are recorded in ``opaque_attributes`` so
    nothing is silently dropped.
    """
    try:
        import h5py  # lazy: only needed for .sofa ingestion
    except ImportError as exc:  # pragma: no cover - dep declared in pyproject
        raise RuntimeError(
            'h5py is required for SOFA dataset ingestion'
        ) from exc

    path = Path(sofa_path)
    file_bytes = read_file_bounded(path, MAX_ATTACHMENT_BYTES)
    file_hash = sha256(file_bytes).hexdigest()

    with h5py.File(path, 'r') as handle:
        conventions = _as_text(handle.attrs.get('Conventions', b'SOFA'))
        sofa_convention = _as_text(
            handle.attrs.get('SOFAConventions', b'')
        )
        if sofa_convention != 'SimpleFreeFieldHRIR':
            raise ValueError(
                'only SimpleFreeFieldHRIR datasets are admitted; '
                f'found {sofa_convention!r} (BRIR/room datasets are '
                'rejected — no double-room convolution)'
            )
        if 'SourcePosition' not in handle or 'Data.SamplingRate' not in handle:
            raise ValueError(
                'SOFA file lacks required SourcePosition or '
                'Data.SamplingRate variables'
            )
        source_position = handle['SourcePosition'][...]
        sampling_rate = handle['Data.SamplingRate'][...]
        data_ir = handle.get('Data.IR')
        emitter_count = int(data_ir.shape[2]) if data_ir is not None else 0
        receiver_count = (
            int(data_ir.shape[1]) if data_ir is not None else 0
        )

        points: list[SpatialMeasurementPoint] = []
        for index in range(source_position.shape[0]):
            azimuth, elevation, distance = normalize_spherical_position(
                azimuth_deg=float(source_position[index, 0]),
                elevation_deg=float(source_position[index, 1]),
                distance_m=float(source_position[index, 2]),
            )
            points.append(
                SpatialMeasurementPoint(
                    measurement_index=index,
                    azimuth_deg=azimuth,
                    elevation_deg=elevation,
                    distance_m=distance,
                )
            )

        known_attrs = {
            'Conventions',
            'SOFAConventions',
            'DataType',
            'RoomType',
            'DatabaseName',
            'Title',
            'Organization',
            'ListenerShortName',
            'DateCreated',
            'DateModified',
            'Author',
            'License',
            'Version',
            'ApplicationName',
            'ApplicationVersion',
            'GLOBAL:Conventions',
            'GLOBAL:SOFAConventions',
            'GLOBAL:Title',
            'GLOBAL:Organization',
            'GLOBAL:ListenerShortName',
            'GLOBAL:RoomType',
            'GLOBAL:DataType',
            'GLOBAL:DatabaseName',
            'GLOBAL:License',
            'GLOBAL:Author',
            'GLOBAL:Version',
            'GLOBAL:ApplicationName',
            'GLOBAL:ApplicationVersion',
            'GLOBAL:DateCreated',
            'GLOBAL:DateModified',
        }
        opaque = sorted(
            f'{name}={_as_text(value)}'
            for name, value in handle.attrs.items()
            if name not in known_attrs
        )
        title = _as_text(
            handle.attrs.get('GLOBAL:Title', handle.attrs.get('Title', b''))
        )
        organization = _as_text(
            handle.attrs.get(
                'GLOBAL:Organization', handle.attrs.get('Organization', b'')
            )
        )
        listener = _as_text(
            handle.attrs.get(
                'GLOBAL:ListenerShortName',
                handle.attrs.get('ListenerShortName', b''),
            )
        )
        dataset_name = _as_text(
            handle.attrs.get(
                'GLOBAL:DatabaseName',
                handle.attrs.get('DatabaseName', path.stem),
            )
        )
        rate = float(sampling_rate.flatten()[0])
        _ = conventions  # kept for clarity; SOFAConventions is authoritative

    payload: dict[str, Any] = {
        'profile_id': profile_id,
        'profile_version': profile_version,
        'created_at_utc': created_at_utc,
        'dataset_name': dataset_name,
        'source_file_sha256': file_hash,
        'convention': 'SimpleFreeFieldHRIR',
        'sample_rate_hz': rate,
        'measurement_count': len(points),
        'emitter_count': emitter_count,
        'receiver_count': receiver_count,
        'source_points': tuple(points),
        'personalization_scope': personalization_scope,
        'license_kind': license_kind,
        'license_note': license_note,
        'global_title': title,
        'global_organization': organization,
        'global_listener_short_name': listener,
        'opaque_attributes': tuple(opaque),
    }
    provisional = SpatialReproductionProfile.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return SpatialReproductionProfile.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


__all__ = [
    'SPATIAL_PROFILE_SCHEMA_VERSION',
    'PersonalizationScope',
    'SpatialConvention',
    'SpatialDatasetLicense',
    'SpatialMeasurementPoint',
    'SpatialReproductionProfile',
    'load_sofa_dataset_profile',
    'normalize_spherical_position',
]
