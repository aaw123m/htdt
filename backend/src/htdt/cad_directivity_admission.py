"""#1066 — open loudspeaker-directivity admission batch.

Registers two measured loudspeaker-directivity corpora for the #357
``DirectivityDataset`` adapters:

- **Aalto sound-source directivity** (Zenodo record 10255555, concept DOI
  10.5281/zenodo.10255554, CC BY 4.0): four SOFA payloads — calibrated
  omnidirectional source, two Genelec loudspeakers, and a mixed-order
  cluster — captured on a near-full sphere. ``download_on_demand``.
- **Princeton 3D3A anechoic directivity** (princeton.edu/3D3A): IR
  measurements on horizontal and vertical planes only. No checksums are
  published, so the record is a ``user_import_candidate`` pointer — and its
  coverage is planes, NOT a sphere; consuming code must never promote it.

Coordinate normalization is explicit, not assumed: Aalto records azimuth
clockwise seen from above with elevation up, on an "azimuth ∈ [0, 360),
elevation ∈ [-90, 90]" spherical grid; HTDT's convention is forward/up
(+X forward, +Z up, azimuth counter-clockwise about +Z). The
normalization spec here is the ONLY document a loader may rely on —
``DirectivityDataset`` still requires a complete regular grid, so a
partial-coverage corpus loads only as plane-polar data with its declared
capability ceiling.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import DirectivityCapabilityTier
from .cad_external_admission import (
    ExternalAssetAdmission,
    build_external_asset_admission,
    external_asset_file,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


DIRECTIVITY_ADMISSION_AUTHORITY_VERSION = 'directivity-admission-1'






# ---------------------------------------------------------------------------
# Coverage declaration — what a source actually covers
# ---------------------------------------------------------------------------

DirectivityCoverage = Literal[
    'full_sphere',
    'horizontal_plane',
    'vertical_plane',
    'horizontal_and_vertical_planes',
    'partial_sector',
    'unknown',
]


class CoordinateNormalizationSpec(BaseModel):
    """How a dataset's published convention maps onto HTDT axes.

    HTDT convention: ``+X`` forward (on-axis), ``+Z`` up; azimuth is
    counter-clockwise about +Z seen from above, elevation up.
    """

    model_config = ConfigDict(frozen=True)

    source_convention: str = Field(min_length=1)
    source_azimuth_sense: Literal['clockwise', 'counter_clockwise']
    source_azimuth_origin: str = Field(min_length=1)
    source_elevation_sense: Literal['up_positive', 'down_positive']
    htdt_transform: str = Field(min_length=1)
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'CoordinateNormalizationSpec':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'coordinate normalization spec semantic hash mismatch'
            )
        return self


def coordinate_normalization_spec(
    *,
    source_convention: str,
    source_azimuth_sense: Literal['clockwise', 'counter_clockwise'],
    source_azimuth_origin: str,
    source_elevation_sense: Literal['up_positive', 'down_positive'],
    htdt_transform: str,
) -> CoordinateNormalizationSpec:
    probe = CoordinateNormalizationSpec.model_construct(
        source_convention=source_convention,
        source_azimuth_sense=source_azimuth_sense,
        source_azimuth_origin=source_azimuth_origin,
        source_elevation_sense=source_elevation_sense,
        htdt_transform=htdt_transform,
        semantic_sha256='',
    )
    return CoordinateNormalizationSpec(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


class DirectivityAdmissionRecord(BaseModel):
    """Admission + capability ceiling for one directivity corpus.

    ``capability_ceiling`` is the #357-tier the richest payload may
    reach (IR data → ``complex``); ``full_sphere_capable`` is the
    mechanical guard that stops a plane/sector corpus from ever being
    promoted into a full-sphere ``DirectivityDataset`` domain.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['directivity-admission-1'] = (
        DIRECTIVITY_ADMISSION_AUTHORITY_VERSION
    )
    admission: ExternalAssetAdmission
    coverage: DirectivityCoverage
    capability_ceiling: DirectivityCapabilityTier
    full_sphere_capable: bool = False
    coordinate_spec: CoordinateNormalizationSpec
    measurement_class: str = Field(min_length=1)
    capability_note: str = ''
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DirectivityAdmissionRecord':
        if self.full_sphere_capable and self.coverage != (
            'full_sphere'
        ):
            raise ValueError(
                'only full_sphere coverage may be marked '
                'full_sphere_capable'
            )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'directivity admission record semantic hash mismatch'
            )
        return self


def build_directivity_admission(
    *,
    admission: ExternalAssetAdmission,
    coverage: DirectivityCoverage,
    capability_ceiling: DirectivityCapabilityTier,
    coordinate_spec: CoordinateNormalizationSpec,
    measurement_class: str,
    full_sphere_capable: bool = False,
    capability_note: str = '',
) -> DirectivityAdmissionRecord:
    probe = DirectivityAdmissionRecord.model_construct(
        schema_version=1,
        authority_version=DIRECTIVITY_ADMISSION_AUTHORITY_VERSION,
        admission=admission,
        coverage=coverage,
        capability_ceiling=capability_ceiling,
        full_sphere_capable=full_sphere_capable,
        coordinate_spec=coordinate_spec,
        measurement_class=measurement_class,
        capability_note=capability_note,
        semantic_sha256='',
    )
    return DirectivityAdmissionRecord(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# ---------------------------------------------------------------------------
# Aalto — 4 calibrated SOFA payloads, near-full sphere
# ---------------------------------------------------------------------------

AALTO_COORD_SPEC = coordinate_normalization_spec(
    source_convention=(
        'SOFA per AES69: azimuth 0..360 clockwise seen from above, '
        'elevation -90..90, radius in metres, origin on-axis'
    ),
    source_azimuth_sense='clockwise',
    source_azimuth_origin='on-axis forward (0 deg = front)',
    source_elevation_sense='up_positive',
    htdt_transform=(
        'azimuth_htdt = 360 - azimuth_source (mod 360); '
        'elevation passes through unchanged; HTDT +X forward, +Z up'
    ),
)

_AALTO_RECORD = '10255555'


def _aalto_file(name: str) -> str:
    return f'https://zenodo.org/records/{_AALTO_RECORD}/files/{name}'


AALTO_DIRECTIVITY_ADMISSION = build_external_asset_admission(
    admission_id='ledger/aalto-ls-directivity',
    dataset_name='aalto-ls-directivity',
    dataset_title=(
        'Sound-source directivity dataset for four loudspekaers'
    ),
    publisher='Aalto Acoustics Lab (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.10255554',
    version_doi='10.5281/zenodo.10255555',
    version_record_id='10255555',
    record_uri='https://zenodo.org/records/10255555',
    license_id='cc-by-4.0',
    license_family='cc_by',
    license_uri='https://creativecommons.org/licenses/by/4.0/',
    files=(
        external_asset_file(
            file_name='LS_directivity_Calibrated_Omni.sofa',
            uri=_aalto_file('LS_directivity_Calibrated_Omni.sofa'),
            size_bytes=437738527,
            md5='e8efed0f4a9b348f1408080e025f171e',
            role='primary_payload',
        ),
        external_asset_file(
            file_name='LS_directivity_Calibrated_GENELEC_8331A.sofa',
            uri=_aalto_file('LS_directivity_Calibrated_GENELEC_8331A.sofa'),
            size_bytes=410523274,
            md5='a00e40164556183420c315e9f6ecd7fd',
            role='primary_payload',
        ),
        external_asset_file(
            file_name='LS_directivity_Calibrated_GENELEC_8030B.sofa',
            uri=_aalto_file('LS_directivity_Calibrated_GENELEC_8030B.sofa'),
            size_bytes=418157375,
            md5='e5b905ec1dcf7df7529e21849ecd00a7',
            role='primary_payload',
        ),
        external_asset_file(
            file_name='LS_directivity_Calibrated_Mixed_order_393.sofa',
            uri=_aalto_file(
                'LS_directivity_Calibrated_Mixed_order_393.sofa'
            ),
            size_bytes=326629106,
            md5='b572beb7968a6b845908e004294ea1b3',
            role='primary_payload',
        ),
        external_asset_file(
            file_name='azimuth_elevation.pdf',
            uri=_aalto_file('azimuth_elevation.pdf'),
            size_bytes=23168,
            md5='4323de0ede1a5540ffa54462592b1703',
            role='coordinate_documentation',
        ),
        external_asset_file(
            file_name='OmniSource.zip',
            uri=_aalto_file('OmniSource.zip'),
            size_bytes=444325480,
            md5='8c6b6c9c7477f7141a7e3dd9ebe306a9',
            role='raw_measurements',
        ),
        external_asset_file(
            file_name='Genelec_8030B.zip',
            uri=_aalto_file('Genelec_8030B.zip'),
            size_bytes=466294978,
            md5='46f663764e43e1fb26663e88ae31d4fb',
            role='raw_measurements',
        ),
        external_asset_file(
            file_name='Genelec_8331A.zip',
            uri=_aalto_file('Genelec_8331A.zip'),
            size_bytes=465585079,
            md5='dc01ceee32d1fac1b8e1f0a2c916ee1f',
            role='raw_measurements',
        ),
        external_asset_file(
            file_name='Mixed-order_393.zip',
            uri=_aalto_file('Mixed-order_393.zip'),
            size_bytes=361533655,
            md5='49b80f1955b09fa8a3e0f4f7c5c2ff09',
            role='raw_measurements',
        ),
    ),
    dataset_notes=(
        'Calibrated impulse responses for Omni, GENELEC 8331A, GENELEC '
        '8030B, and a mixed-order 393-channel cluster on a near-complete '
        'spherical grid; azimuth_elevation.pdf documents the measurement '
        'angles.'
    ),
)


AALTO_DIRECTIVITY_RECORD = build_directivity_admission(
    admission=AALTO_DIRECTIVITY_ADMISSION,
    coverage='full_sphere',
    capability_ceiling='complex',
    full_sphere_capable=True,
    coordinate_spec=AALTO_COORD_SPEC,
    measurement_class='anechoic_spherical_capture',
    capability_note=(
        'Calibrated SOFA impulse responses: near-complete spherical '
        'grid may feed a complex DirectivityDataset after coordinate '
        'normalization; raw zip archives are supporting material only'
    ),
)


# ---------------------------------------------------------------------------
# Princeton 3D3A — anechoic IRs on horizontal + vertical planes
# ---------------------------------------------------------------------------

PRINCETON_DIRECTIVITY_ADMISSION = build_external_asset_admission(
    admission_id='ledger/princeton-3d3a-directivity',
    dataset_name='princeton-3d3a-directivity',
    dataset_title=(
        'Princeton 3D3A Lab loudspeaker directivity measurements'
    ),
    publisher='Princeton University 3D Audio and Applied Acoustics Lab',
    source_kind='institutional_repository',
    admission_state='user_import_candidate',
    record_uri='https://www.princeton.edu/3D3A/Directivity.html',
    license_id='unstated',
    license_family='unknown',
    license_note=(
        'No license terms are stated on the dataset page — treat as '
        'user-import research material; do not redistribute payloads'
    ),
    files=(
        external_asset_file(
            file_name='directivity-dataset',
            uri='https://www.princeton.edu/3D3A/Directivity.html',
            size_bytes=0,
            checksum_source='none',
            role='landing_page',
        ),
    ),
    dataset_notes=(
        'Anechoic loudspeaker impulse-response measurements on the '
        'horizontal and vertical planes. The site does not publish '
        'checksums or a stable versioned archive, so this record pins '
        'only the landing page — the user downloads and supplies the '
        'files.'
    ),
)


PRINCETON_COORD_SPEC = coordinate_normalization_spec(
    source_convention=(
        'per published MATLAB tables: azimuth clockwise seen from above '
        'in 5-degree steps on the horizontal plane and on the vertical '
        'plane (2 planes per source); forward = 0 deg'
    ),
    source_azimuth_sense='clockwise',
    source_azimuth_origin='on-axis forward (0 deg = front)',
    source_elevation_sense='up_positive',
    htdt_transform=(
        'azimuth_htdt = 360 - azimuth_source (mod 360); each plane '
        'loads as a DirectivityDataset whose declared geometry is the '
        'plane — never as full_sphere'
    ),
)

PRINCETON_DIRECTIVITY_RECORD = build_directivity_admission(
    admission=PRINCETON_DIRECTIVITY_ADMISSION,
    coverage='horizontal_and_vertical_planes',
    capability_ceiling='complex',
    coordinate_spec=PRINCETON_COORD_SPEC,
    measurement_class='anechoic_plane_capture',
    full_sphere_capable=False,  # ring cross, never a sphere
    capability_note=(
        'H and V plane paths only — a horizontal ring plus a '
        'vertical ring is not a sphere; full_sphere_capable=False '
        'is the mechanical guarantee the data is never promoted'
    ),
)


DIRECTIVITY_ADMISSION_RECORDS: tuple[DirectivityAdmissionRecord, ...] = (
    AALTO_DIRECTIVITY_RECORD,
    PRINCETON_DIRECTIVITY_RECORD,
)


def directivity_record_for(
    dataset_name: str,
) -> DirectivityAdmissionRecord | None:
    for record in DIRECTIVITY_ADMISSION_RECORDS:
        if record.admission.dataset_name == dataset_name:
            return record
    return None


__all__ = [
    'AALTO_COORD_SPEC',
    'AALTO_DIRECTIVITY_ADMISSION',
    'AALTO_DIRECTIVITY_RECORD',
    'DIRECTIVITY_ADMISSION_AUTHORITY_VERSION',
    'DIRECTIVITY_ADMISSION_RECORDS',
    'CoordinateNormalizationSpec',
    'DirectivityAdmissionRecord',
    'DirectivityCoverage',
    'PRINCETON_COORD_SPEC',
    'PRINCETON_DIRECTIVITY_ADMISSION',
    'PRINCETON_DIRECTIVITY_RECORD',
    'build_directivity_admission',
    'coordinate_normalization_spec',
    'directivity_record_for',
]
