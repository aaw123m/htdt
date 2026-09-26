"""#1067 — HRTF/SOFA starter admission pack.

A small, provenance-safe starter set for binaural auralization: every
fixture pins its dataset admission record, its subject/variant identity,
its fixture class, and an explicit coordinate normalization. Classes are
deliberately distinct — a BRIR is never a free-field HRTF, and a generic
mannequin is never presented as an individualized measurement:

- ``generic_mannequin`` — measured on a dummy head (KU100, KEMAR);
- ``generic_human`` — a published human-subject set used generically;
- ``individual_measured`` — a specific person's own measured set;
- ``individual_computed`` — individualized by computation, not capture;
- ``brir`` — binaural ROOM impulse responses (a room is baked in —
  must never feed the free-field reproduction profile, #809);
- ``headphone_ir`` — headphone compensation/equalization IRs.

SADIE II ships under Apache-2.0 (the shared ``SpatialDatasetLicense``
vocabulary has no apache entry — the raw license string is kept verbatim
on the admission record rather than widening that enum silently).

Coordinate conventions are pinned per fixture, and the pack carries
``COORDINATE_REGRESSION_VECTORS`` — expected mappings a loader test can
assert so a convention flip fails loudly instead of rotating heads.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_directivity_admission import (
    CoordinateNormalizationSpec,
    coordinate_normalization_spec,
)
from .cad_external_admission import (
    ExternalAssetAdmission,
    build_external_asset_admission,
    external_asset_file,
)


SPATIAL_STARTER_AUTHORITY_VERSION = 'spatial-starter-pack-1'


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict) -> str:
    return hashlib.sha256(
        _canonical(payload).encode('utf-8')
    ).hexdigest()


SpatialFixtureClass = Literal[
    'generic_mannequin',
    'generic_human',
    'individual_measured',
    'individual_computed',
    'brir',
    'headphone_ir',
]


class SpatialStarterFixture(BaseModel):
    """One starter-pack fixture bound to an admission record."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['spatial-starter-pack-1'] = (
        SPATIAL_STARTER_AUTHORITY_VERSION
    )
    fixture_id: str = Field(min_length=1)
    admission: ExternalAssetAdmission
    fixture_class: SpatialFixtureClass
    sofa_convention: str | None = None
    subject_id: str | None = None
    subject_identity_note: str = ''
    coordinate_spec: CoordinateNormalizationSpec
    content_payload_names: tuple[str, ...] = ()
    allowed_uses: tuple[str, ...] = ()
    forbidden_uses: tuple[str, ...] = ()
    fixture_note: str = ''
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SpatialStarterFixture':
        # a BRIR is not a free-field HRTF — it can never claim SOFA's
        # free-field HRIR convention or reproduction-profile use
        if self.fixture_class == 'brir':
            if self.sofa_convention == 'SimpleFreeFieldHRIR':
                raise ValueError(
                    'brir fixture cannot claim SimpleFreeFieldHRIR'
                )
            if 'reproduction_profile' in self.allowed_uses:
                raise ValueError(
                    'brir fixture cannot feed the reproduction profile'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'spatial starter fixture semantic hash mismatch'
            )
        return self


def build_spatial_fixture(
    *,
    fixture_id: str,
    admission: ExternalAssetAdmission,
    fixture_class: SpatialFixtureClass,
    coordinate_spec: CoordinateNormalizationSpec,
    sofa_convention: str | None = None,
    subject_id: str | None = None,
    subject_identity_note: str = '',
    content_payload_names: tuple[str, ...] = (),
    allowed_uses: tuple[str, ...] = (),
    forbidden_uses: tuple[str, ...] = (),
    fixture_note: str = '',
) -> SpatialStarterFixture:
    probe = SpatialStarterFixture.model_construct(
        schema_version=1,
        authority_version=SPATIAL_STARTER_AUTHORITY_VERSION,
        fixture_id=fixture_id,
        admission=admission,
        fixture_class=fixture_class,
        sofa_convention=sofa_convention,
        subject_id=subject_id,
        subject_identity_note=subject_identity_note,
        coordinate_spec=coordinate_spec,
        content_payload_names=tuple(content_payload_names),
        allowed_uses=tuple(allowed_uses),
        forbidden_uses=tuple(forbidden_uses),
        fixture_note=fixture_note,
        semantic_sha256='',
    )
    return SpatialStarterFixture(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


class SpatialStarterPack(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['spatial-starter-pack-1'] = (
        SPATIAL_STARTER_AUTHORITY_VERSION
    )
    pack_id: str = Field(min_length=1)
    fixtures: tuple[SpatialStarterFixture, ...]
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'SpatialStarterPack':
        ids = [f.fixture_id for f in self.fixtures]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate fixture ids in pack')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'spatial starter pack semantic hash mismatch'
            )
        return self


def build_spatial_starter_pack(
    *, pack_id: str, fixtures: tuple[SpatialStarterFixture, ...]
) -> SpatialStarterPack:
    probe = SpatialStarterPack.model_construct(
        schema_version=1,
        authority_version=SPATIAL_STARTER_AUTHORITY_VERSION,
        pack_id=pack_id,
        fixtures=tuple(fixtures),
        semantic_sha256='',
    )
    return SpatialStarterPack(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# ---------------------------------------------------------------------------
# Coordinate conventions
# ---------------------------------------------------------------------------

SOFA_SPHERICAL_COORD_SPEC = coordinate_normalization_spec(
    source_convention=(
        'SOFA spherical (AES69): azimuth degrees counter-clockwise from '
        'front (0 front, +90 left), elevation +up, distance metres'
    ),
    source_azimuth_sense='counter_clockwise',
    source_azimuth_origin='front (0 deg)',
    source_elevation_sense='up_positive',
    htdt_transform=(
        'identical to the #809 normalized frame — wrap azimuth to '
        '[-180, 180), clamp elevation to [-90, 90]'
    ),
)

# Expected mappings regression tests assert. (azimuth_in, elevation_in)
# -> (azimuth_htdt, elevation_htdt) in the #809 normalized frame.
COORDINATE_REGRESSION_VECTORS: tuple[
    tuple[float, float, float, float, float], ...
] = (
    # src_az, src_el, dist -> htdt_az, htdt_el
    (0.0, 0.0, 1.0, 0.0, 0.0),
    (90.0, 0.0, 1.0, 90.0, 0.0),      # left stays left
    (270.0, 0.0, 1.0, -90.0, 0.0),    # right wraps to -90
    (180.0, 30.0, 1.5, -180.0, 30.0),  # rear wraps to -180
    (45.0, -30.0, 1.0, 45.0, -30.0),
)


# ---------------------------------------------------------------------------
# SADIE II — Apache-2.0, mannequin + human subjects
# ---------------------------------------------------------------------------

_SADIE_RECORD = '12092466'


def _sadie_file(name: str) -> str:
    return (
        f'https://zenodo.org/records/{_SADIE_RECORD}/files/'
        f'{name.replace(" ", "%20")}'
    )


SADIE_II_ADMISSION = build_external_asset_admission(
    admission_id='ledger/sadie-ii',
    dataset_name='sadie-ii',
    dataset_title='SADIE II Database',
    publisher='University of York / SADIE project (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.10886408',
    version_doi='10.5281/zenodo.12092466',
    version_record_id='12092466',
    record_uri='https://zenodo.org/records/12092466',
    license_id='apache-2.0',
    license_family='apache_2_0',
    license_uri='https://www.apache.org/licenses/LICENSE-2.0',
    license_note=(
        'Apache-2.0 per the Zenodo record and the bundled LICENSE.txt — '
        'the shared SpatialDatasetLicense vocabulary has no apache entry '
        'so the raw license id is kept verbatim here'
    ),
    files=(
        external_asset_file(
            file_name='D1_HRIR_SOFA.zip',
            uri=_sadie_file('D1_HRIR_SOFA.zip'),
            size_bytes=38151804,
            md5='4850c1eb8e63e2d4f605edcdb4d5c883',
            role='hrir_sofa_d1_ku100',
        ),
        external_asset_file(
            file_name='D2_HRIR_SOFA.zip',
            uri=_sadie_file('D2_HRIR_SOFA.zip'),
            size_bytes=38042020,
            md5='f555491d1b6b1a108bb4d26b63ad84a4',
            role='hrir_sofa_d2_kemar',
        ),
        external_asset_file(
            file_name='H20_HRIR_SOFA.zip',
            uri=_sadie_file('H20_HRIR_SOFA.zip'),
            size_bytes=9299084,
            md5='d838ecc8a8c97b2ad0157a5ca55c0e73',
            role='hrir_sofa_human_subject_h20',
        ),
        external_asset_file(
            file_name='D1_BRIR_SOFA.zip',
            uri=_sadie_file('D1_BRIR_SOFA.zip'),
            size_bytes=8061418,
            md5='7725508069e99fb8f519d452c156fdb3',
            role='brir_sofa_d1',
        ),
        external_asset_file(
            file_name='SADIE II V2-2 README.pdf',
            uri=_sadie_file('SADIE II V2-2 README.pdf'),
            size_bytes=38065,
            md5='da0d5fea67c6aba70fba0cca4f14d643',
            role='documentation',
        ),
        external_asset_file(
            file_name='LICENSE.txt',
            uri=_sadie_file('LICENSE.txt'),
            size_bytes=11558,
            md5='0ba5044c64ef53cb0189c9546081e228',
            role='license_text',
        ),
    ),
    dataset_notes=(
        'Version 2-2 record (record id 12092466, concept 10886408). '
        'D1 = Neumann KU100 dummy head, D2 = KEMAR dummy head, H1-H20 = '
        'human subjects; BRIR zips are room-measured and are a different '
        'fixture class, never free-field HRTFs.'
    ),
)


# ---------------------------------------------------------------------------
# Princeton 3D3A subject HRTF — CC BY pointer, user import
# ---------------------------------------------------------------------------

PRINCETON_HRTF_ADMISSION = build_external_asset_admission(
    admission_id='ledger/princeton-3d3a-hrtf',
    dataset_name='princeton-3d3a-hrtf',
    dataset_title='Princeton 3D3A subject HRTF measurements',
    publisher='Princeton University 3D Audio and Applied Acoustics Lab',
    source_kind='institutional_repository',
    admission_state='user_import_candidate',
    record_uri='https://www.princeton.edu/3D3A/Subjects.html',
    license_id='cc-by',
    license_family='cc_by',
    license_note=(
        'CC BY per the 3D3A publication; the site publishes no checksums '
        'or versioned archive so files are pinned at import time'
    ),
    files=(
        external_asset_file(
            file_name='subjects-landing-page',
            uri='https://www.princeton.edu/3D3A/Subjects.html',
            size_bytes=0,
            checksum_source='none',
            role='landing_page',
        ),
    ),
    dataset_notes=(
        'Human-subject HRTF sets measured by the 3D3A lab; the starter '
        'pack binds ONE subject as an individualized fixture — the user '
        'downloads the subject SOFA/IR set and imports it.'
    ),
)


# ---------------------------------------------------------------------------
# The starter pack itself
# ---------------------------------------------------------------------------

_SADIE_FIXTURES = (
    build_spatial_fixture(
        fixture_id='sadie-ii/d1-ku100',
        admission=SADIE_II_ADMISSION,
        fixture_class='generic_mannequin',
        sofa_convention='SimpleFreeFieldHRIR',
        subject_id='D1',
        subject_identity_note='Neumann KU100 dummy head',
        coordinate_spec=SOFA_SPHERICAL_COORD_SPEC,
        content_payload_names=('D1_HRIR_SOFA.zip',),
        allowed_uses=('reproduction_profile', 'binaural_preview'),
    ),
    build_spatial_fixture(
        fixture_id='sadie-ii/d2-kemar',
        admission=SADIE_II_ADMISSION,
        fixture_class='generic_mannequin',
        sofa_convention='SimpleFreeFieldHRIR',
        subject_id='D2',
        subject_identity_note='GRAS KEMAR dummy head',
        coordinate_spec=SOFA_SPHERICAL_COORD_SPEC,
        content_payload_names=('D2_HRIR_SOFA.zip',),
        allowed_uses=('reproduction_profile', 'binaural_preview'),
    ),
    build_spatial_fixture(
        fixture_id='sadie-ii/h20-human',
        admission=SADIE_II_ADMISSION,
        fixture_class='individual_measured',
        sofa_convention='SimpleFreeFieldHRIR',
        subject_id='H20',
        subject_identity_note=(
            'one real human subject — individualized by measurement; '
            'using it for another listener is a documented approximation'
        ),
        coordinate_spec=SOFA_SPHERICAL_COORD_SPEC,
        content_payload_names=('H20_HRIR_SOFA.zip',),
        allowed_uses=(
            'reproduction_profile', 'binaural_preview',
            'individualized_demo',
        ),
    ),
    build_spatial_fixture(
        fixture_id='sadie-ii/d1-brir',
        admission=SADIE_II_ADMISSION,
        fixture_class='brir',
        sofa_convention='SimpleBRIR',
        subject_id='D1',
        subject_identity_note='KU100 in measured rooms',
        coordinate_spec=SOFA_SPHERICAL_COORD_SPEC,
        content_payload_names=('D1_BRIR_SOFA.zip',),
        allowed_uses=('binaural_preview',),
        forbidden_uses=('reproduction_profile',),
        fixture_note=(
            'BRIR has the measurement room baked in — must never feed '
            'the free-field reproduction profile (#809)'
        ),
    ),
    build_spatial_fixture(
        fixture_id='princeton-3d3a/subject-1',
        admission=PRINCETON_HRTF_ADMISSION,
        fixture_class='individual_measured',
        subject_id='subject-1',
        subject_identity_note=(
            'first published 3D3A subject; exact subject id is pinned '
            'at user import time'
        ),
        coordinate_spec=SOFA_SPHERICAL_COORD_SPEC,
        allowed_uses=('reproduction_profile', 'binaural_preview'),
        fixture_note='user-import candidate — site publishes no checksums',
    ),
)

SPATIAL_STARTER_PACK = build_spatial_starter_pack(
    pack_id='spatial-starter-pack-v1',
    fixtures=_SADIE_FIXTURES,
)


__all__ = [
    'COORDINATE_REGRESSION_VECTORS',
    'PRINCETON_HRTF_ADMISSION',
    'SADIE_II_ADMISSION',
    'SOFA_SPHERICAL_COORD_SPEC',
    'SPATIAL_STARTER_AUTHORITY_VERSION',
    'SPATIAL_STARTER_PACK',
    'SpatialFixtureClass',
    'SpatialStarterFixture',
    'SpatialStarterPack',
    'build_spatial_fixture',
    'build_spatial_starter_pack',
]
