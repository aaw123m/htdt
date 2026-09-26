"""Shared download-on-demand external-asset admission records.

Used by the open-dataset admission batches (#1060 Aalto SRIR, #1066
loudspeaker directivity, #1067 HRTF starter pack, #1068 FLAIR). Each
``ExternalAssetAdmission`` pins a published dataset by its exact identifiers
(DOI/version DOI, file names, sizes, checksums) and its #834 admission
ledger state — payloads are never bundled into the repository; the record
says where the bytes live and what they hash to, so a downloader can fetch
and verify them later without trusting the network.

Rules:

- ``admission_state`` follows docs/EXTERNAL_ASSET_ADMISSION_LEDGER.md
  vocabulary (BUNDLE_CANDIDATE, DOWNLOAD_ON_DEMAND_CANDIDATE,
  USER_IMPORT_CANDIDATE, LICENSE_REVIEW_REQUIRED, PERMISSION_REQUIRED,
  THIRD_PARTY_DISCOVERY_ONLY, ...);
- a file checksum may be ``None`` only when the upstream publishes none —
  the record then says so instead of fabricating one;
- ``license_id`` is the publisher's exact license string (e.g.
  ``apache-2.0``), kept verbatim even when it maps onto a broader
  ``license_family``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EXTERNAL_ADMISSION_AUTHORITY_VERSION = 'external-admission-1'


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


ExternalAdmissionState = Literal[
    'bundle_candidate',
    'download_on_demand_candidate',
    'user_import_candidate',
    'license_review_required',
    'permission_required',
    'third_party_discovery_only',
    'documented_text_interchange',
    'user_measured',
    'ineligible',
]

ExternalAssetSourceKind = Literal[
    'zenodo_record',
    'institutional_repository',
    'manufacturer_site',
    'api_service',
    'other',
]

LicenseFamily = Literal[
    'cc0',
    'cc_by',
    'cc_by_sa',
    'cc_by_nc',
    'apache_2_0',
    'mit',
    'research_only',
    'proprietary',
    'unknown',
]


class ExternalAssetFile(BaseModel):
    """One file inside an admitted dataset — pinned by name/size/hash."""

    model_config = ConfigDict(frozen=True)

    file_name: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    md5: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    checksum_source: Literal['publisher', 'computed', 'none'] = 'publisher'
    role: str | None = None
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ExternalAssetFile':
        if self.checksum_source == 'none' and (
            self.md5 is not None or self.sha256 is not None
        ):
            raise ValueError(
                'checksum_source=none cannot carry checksum values'
            )
        if self.md5 is None and self.sha256 is None:
            if self.checksum_source != 'none':
                raise ValueError(
                    'file without checksums must declare '
                    "checksum_source='none'"
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('external asset file semantic hash mismatch')
        return self


class ExternalAssetAdmission(BaseModel):
    """An admission record for one published external dataset."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['external-admission-1'] = (
        EXTERNAL_ADMISSION_AUTHORITY_VERSION
    )
    admission_id: str = Field(min_length=1)
    dataset_name: str = Field(min_length=1)
    dataset_title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    source_kind: ExternalAssetSourceKind
    admission_state: ExternalAdmissionState
    concept_doi: str | None = None
    version_doi: str | None = None
    version_record_id: str | None = None
    record_uri: str = Field(min_length=1)
    license_id: str = Field(min_length=1)
    license_family: LicenseFamily
    license_uri: str | None = None
    license_note: str = ''
    files: tuple[ExternalAssetFile, ...] = ()
    dataset_notes: str = ''
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ExternalAssetAdmission':
        names = [f.file_name for f in self.files]
        if len(names) != len(set(names)):
            raise ValueError('duplicate file names in admission')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'external asset admission semantic hash mismatch'
            )
        return self


def external_asset_file(
    *,
    file_name: str,
    uri: str,
    size_bytes: int,
    md5: str | None = None,
    sha256: str | None = None,
    checksum_source: Literal['publisher', 'computed', 'none'] = (
        'publisher'
    ),
    role: str | None = None,
) -> ExternalAssetFile:
    probe = ExternalAssetFile.model_construct(
        file_name=file_name,
        uri=uri,
        size_bytes=size_bytes,
        md5=md5,
        sha256=sha256,
        checksum_source=checksum_source,
        role=role,
        semantic_sha256='',
    )
    return ExternalAssetFile(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def build_external_asset_admission(
    *,
    admission_id: str,
    dataset_name: str,
    dataset_title: str,
    publisher: str,
    source_kind: ExternalAssetSourceKind,
    admission_state: ExternalAdmissionState,
    license_id: str,
    license_family: LicenseFamily,
    record_uri: str,
    concept_doi: str | None = None,
    version_doi: str | None = None,
    version_record_id: str | None = None,
    license_uri: str | None = None,
    license_note: str = '',
    files: tuple[ExternalAssetFile, ...] = (),
    dataset_notes: str = '',
) -> ExternalAssetAdmission:
    probe = ExternalAssetAdmission.model_construct(
        schema_version=1,
        authority_version=EXTERNAL_ADMISSION_AUTHORITY_VERSION,
        admission_id=admission_id,
        dataset_name=dataset_name,
        dataset_title=dataset_title,
        publisher=publisher,
        source_kind=source_kind,
        admission_state=admission_state,
        concept_doi=concept_doi,
        version_doi=version_doi,
        version_record_id=version_record_id,
        record_uri=record_uri,
        license_id=license_id,
        license_family=license_family,
        license_uri=license_uri,
        license_note=license_note,
        files=tuple(files),
        dataset_notes=dataset_notes,
        semantic_sha256='',
    )
    return ExternalAssetAdmission(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def admission_file(
    admission: ExternalAssetAdmission, file_name: str
) -> ExternalAssetFile | None:
    for f in admission.files:
        if f.file_name == file_name:
            return f
    return None


__all__ = [
    'EXTERNAL_ADMISSION_AUTHORITY_VERSION',
    'ExternalAdmissionState',
    'ExternalAssetAdmission',
    'ExternalAssetFile',
    'ExternalAssetSourceKind',
    'LicenseFamily',
    'admission_file',
    'build_external_asset_admission',
    'external_asset_file',
]
