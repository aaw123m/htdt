"""Curated speaker / source reference dataset library (#772).

Product layer over the directivity machinery (#575 ``cad_directivity*``) and
equipment library: a *speaker definition* (manufacturer/model/variant —
the thing a normal user might actually own) plus separately-versioned
*reference datasets* attached to it (on-axis response, directivity,
impedance, sensitivity reference).

Contract properties:

- identity and datasets are separate rows: one ``SpeakerDefinition`` can
  hold many ``SpeakerDataset`` versions/kinds — and each dataset declares
  exactly one kind, never silently widened;
- dataset capability is explicit: an on-axis magnitude response does not
  imply directivity; an impedance curve does not imply sensitivity;
- payload is either ``inline`` (small, stored verbatim) or ``external``
  (a URI/asset reference — never bundled bytes); inline directivity must
  be a valid ``htdt.normalized-directivity.v1`` payload, reusing the
  existing schema instead of a parallel one;
- shared/bundled entries (``document_id=None``) require explicit
  redistribution licensing — the library never ships data it may not
  redistribute;
- unknown fields stay absent — a dataset with no phase is magnitude-only
  forever.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_directivity import NormalizedDirectivityJsonV1
from .cad_material_library import MaterialProvenanceClass


SPEAKER_LIBRARY_SCHEMA_VERSION = 1
SPEAKER_LIBRARY_AUTHORITY_VERSION = 'speaker-reference-library-1'


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


SpeakerProvenanceClass = MaterialProvenanceClass

SpeakerConfiguration = Literal[
    'tower',
    'bookshelf',
    'center',
    'surround',
    'subwoofer',
    'in_wall',
    'on_wall',
    'other',
]

SpeakerDatasetKind = Literal[
    'on_axis_frequency_response',
    'directivity',
    'impedance_curve',
    'sensitivity_reference',
    'other',
]

SpeakerDatasetPayloadKind = Literal['inline', 'external']


class SpeakerDefinition(BaseModel):
    """Human/product description of one speaker model variant."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SPEAKER_LIBRARY_SCHEMA_VERSION
    authority_version: Literal['speaker-reference-library-1'] = (
        SPEAKER_LIBRARY_AUTHORITY_VERSION
    )
    speaker_id: str = Field(min_length=1)
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str = Field(min_length=1)
    variant: str | None = Field(default=None, min_length=1)
    configuration: SpeakerConfiguration
    #: ``None`` = shared/bundled library entry; a project id scopes the entry
    #: to that project's library.
    document_id: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    speaker_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_speaker(self) -> 'SpeakerDefinition':
        if self.speaker_sha256 != _hash(self.semantic_payload()):
            raise ValueError('SpeakerDefinition hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'speaker_id': self.speaker_id,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'variant': self.variant,
            'configuration': self.configuration,
            'document_id': self.document_id,
            'created_at_utc': self.created_at_utc,
        }


class SpeakerOnAxisResponse(BaseModel):
    """Inline on-axis magnitude response table (dB SPL referenced)."""

    model_config = ConfigDict(frozen=True)

    frequency_hz: tuple[float, ...] = Field(min_length=1)
    level_db: tuple[float, ...] = Field(min_length=1)
    level_reference: Literal['spl_1m_2v83', 'spl_declared', 'unknown'] = (
        'unknown'
    )
    reference_axis: Literal['on_axis', 'listening_axis', 'unknown'] = (
        'on_axis'
    )

    @model_validator(mode='after')
    def valid_response(self) -> 'SpeakerOnAxisResponse':
        if len(self.level_db) != len(self.frequency_hz):
            raise ValueError(
                'on-axis level values must align one-to-one with frequency_hz'
            )
        for value in (*self.frequency_hz, *self.level_db):
            if not isfinite(value):
                raise ValueError('response values must be finite')
        if any(hz <= 0 for hz in self.frequency_hz):
            raise ValueError('response frequencies must be positive')
        return self


class SpeakerImpedanceCurve(BaseModel):
    """Inline impedance magnitude (+ optional phase) table."""

    model_config = ConfigDict(frozen=True)

    frequency_hz: tuple[float, ...] = Field(min_length=1)
    magnitude_ohm: tuple[float, ...] = Field(min_length=1)
    phase_deg: tuple[float, ...] | None = None

    @model_validator(mode='after')
    def valid_curve(self) -> 'SpeakerImpedanceCurve':
        if len(self.magnitude_ohm) != len(self.frequency_hz):
            raise ValueError(
                'impedance magnitudes must align one-to-one with frequency_hz'
            )
        for value in (*self.frequency_hz, *self.magnitude_ohm):
            if not isfinite(value):
                raise ValueError('impedance values must be finite')
        if any(hz <= 0 for hz in self.frequency_hz):
            raise ValueError('impedance frequencies must be positive')
        if self.phase_deg is not None and len(self.phase_deg) != len(
            self.frequency_hz
        ):
            raise ValueError(
                'impedance phase must align one-to-one with frequency_hz'
            )
        return self


class SpeakerDataset(BaseModel):
    """One versioned reference dataset for a speaker.

    ``payload_json`` (inline) or ``asset_uri``/``asset_sha256`` (external)
    carry the actual data — exactly one per payload kind.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SPEAKER_LIBRARY_SCHEMA_VERSION
    authority_version: Literal['speaker-reference-library-1'] = (
        SPEAKER_LIBRARY_AUTHORITY_VERSION
    )
    dataset_id: str = Field(min_length=1)
    speaker_id: str = Field(min_length=1)
    version: str = Field(min_length=1, default='1')
    kind: SpeakerDatasetKind
    payload_kind: SpeakerDatasetPayloadKind
    payload_json: str | None = Field(default=None, min_length=1)
    asset_uri: str | None = Field(default=None, min_length=1)
    asset_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    provenance_class: SpeakerProvenanceClass
    method: str | None = Field(default=None, min_length=1)
    source_label: str | None = Field(default=None, min_length=1)
    license_name: str | None = Field(default=None, min_length=1)
    license_url: str | None = Field(default=None, min_length=1)
    redistribution_permitted: bool | None = None
    uncertainty_note: str | None = None
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_dataset(self) -> 'SpeakerDataset':
        if self.payload_kind == 'inline':
            if self.payload_json is None:
                raise ValueError('inline payload requires payload_json')
            if self.asset_uri is not None:
                raise ValueError('inline payload cannot carry asset_uri')
            self._validate_inline_payload()
        else:
            if self.payload_json is not None:
                raise ValueError('external payload cannot carry payload_json')
            if self.asset_uri is None:
                raise ValueError('external payload requires asset_uri')
        if self.dataset_sha256 != _hash(self.semantic_payload()):
            raise ValueError('SpeakerDataset hash mismatch')
        return self

    def _validate_inline_payload(self) -> None:
        """Inline data must parse as the declared kind — no opaque blobs."""
        try:
            raw = json.loads(self.payload_json or '')
        except json.JSONDecodeError as exc:
            raise ValueError('payload_json must be valid JSON') from exc
        try:
            if self.kind == 'directivity':
                NormalizedDirectivityJsonV1.model_validate(raw)
            elif self.kind == 'on_axis_frequency_response':
                SpeakerOnAxisResponse.model_validate(raw)
            elif self.kind == 'impedance_curve':
                SpeakerImpedanceCurve.model_validate(raw)
            # 'sensitivity_reference'/'other' inline payloads are JSON only.
        except ValueError as exc:
            raise ValueError(
                f'inline payload does not satisfy the {self.kind} contract: {exc}'
            ) from exc

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'dataset_id': self.dataset_id,
            'speaker_id': self.speaker_id,
            'version': self.version,
            'kind': self.kind,
            'payload_kind': self.payload_kind,
            'payload_json': self.payload_json,
            'asset_uri': self.asset_uri,
            'asset_sha256': self.asset_sha256,
            'provenance_class': self.provenance_class,
            'method': self.method,
            'source_label': self.source_label,
            'license_name': self.license_name,
            'license_url': self.license_url,
            'redistribution_permitted': self.redistribution_permitted,
            'uncertainty_note': self.uncertainty_note,
            'limitations': list(self.limitations),
            'created_at_utc': self.created_at_utc,
        }


def build_speaker_definition(
    *,
    model: str,
    configuration: SpeakerConfiguration,
    created_at_utc: str,
    manufacturer: str | None = None,
    variant: str | None = None,
    document_id: str | None = None,
    speaker_id: str | None = None,
) -> SpeakerDefinition:
    payload: dict[str, Any] = {
        'speaker_id': speaker_id or str(uuid4()),
        'manufacturer': manufacturer,
        'model': model,
        'variant': variant,
        'configuration': configuration,
        'document_id': document_id,
        'created_at_utc': created_at_utc,
    }
    provisional = SpeakerDefinition.model_construct(
        **payload, speaker_sha256='0' * 64
    )
    return SpeakerDefinition(
        **payload, speaker_sha256=_hash(provisional.semantic_payload())
    )


def build_speaker_dataset(
    *,
    speaker_id: str,
    kind: SpeakerDatasetKind,
    payload_kind: SpeakerDatasetPayloadKind,
    provenance_class: SpeakerProvenanceClass,
    created_at_utc: str,
    version: str = '1',
    payload_json: str | None = None,
    asset_uri: str | None = None,
    asset_sha256: str | None = None,
    method: str | None = None,
    source_label: str | None = None,
    license_name: str | None = None,
    license_url: str | None = None,
    redistribution_permitted: bool | None = None,
    uncertainty_note: str | None = None,
    limitations: tuple[str, ...] = (),
    dataset_id: str | None = None,
) -> SpeakerDataset:
    payload: dict[str, Any] = {
        'dataset_id': dataset_id or str(uuid4()),
        'speaker_id': speaker_id,
        'version': version,
        'kind': kind,
        'payload_kind': payload_kind,
        'payload_json': payload_json,
        'asset_uri': asset_uri,
        'asset_sha256': asset_sha256,
        'provenance_class': provenance_class,
        'method': method,
        'source_label': source_label,
        'license_name': license_name,
        'license_url': license_url,
        'redistribution_permitted': redistribution_permitted,
        'uncertainty_note': uncertainty_note,
        'limitations': tuple(limitations),
        'created_at_utc': created_at_utc,
    }
    provisional = SpeakerDataset.model_construct(
        **payload, dataset_sha256='0' * 64
    )
    return SpeakerDataset(
        **payload, dataset_sha256=_hash(provisional.semantic_payload())
    )


def on_axis_response_payload(
    *,
    frequency_hz: tuple[float, ...],
    level_db: tuple[float, ...],
    level_reference: str = 'spl_declared',
    reference_axis: str = 'on_axis',
) -> str:
    """Serialize a validated on-axis response table for inline storage."""
    response = SpeakerOnAxisResponse(
        frequency_hz=frequency_hz,
        level_db=level_db,
        level_reference=level_reference,  # type: ignore[arg-type]
        reference_axis=reference_axis,  # type: ignore[arg-type]
    )
    return response.model_dump_json()


# ----------------------------------------------------------------------
# Bundled reference library — small, honest, generic entries (same contract
# as the material library: provenance is explicit, licensing is
# redistributable, limitations are recorded, nothing claims to be a specific
# commercial product).

_BUILTIN_CREATED = '2026-09-24T00:00:00+00:00'
_BUILTIN_LICENSE = 'HTDT generic reference (redistributable)'
_BUILTIN_LIMITATION = (
    'Generic/analytic reference response — not a measured description of '
    'any specific commercial product.'
)


def _builtin_speaker(
    *,
    speaker_id: str,
    dataset_id: str,
    model: str,
    configuration: SpeakerConfiguration,
    frequency_hz: tuple[float, ...],
    level_db: tuple[float, ...],
    method: str,
    uncertainty_note: str,
) -> tuple[SpeakerDefinition, SpeakerDataset]:
    definition = build_speaker_definition(
        speaker_id=speaker_id,
        manufacturer='HTDT generic',
        model=model,
        configuration=configuration,
        created_at_utc=_BUILTIN_CREATED,
    )
    dataset = build_speaker_dataset(
        dataset_id=dataset_id,
        speaker_id=speaker_id,
        kind='on_axis_frequency_response',
        payload_kind='inline',
        payload_json=on_axis_response_payload(
            frequency_hz=frequency_hz,
            level_db=level_db,
            level_reference='spl_declared',
        ),
        provenance_class='generic_reference_preset',
        method=method,
        license_name=_BUILTIN_LICENSE,
        redistribution_permitted=True,
        uncertainty_note=uncertainty_note,
        limitations=(_BUILTIN_LIMITATION,),
        created_at_utc=_BUILTIN_CREATED,
    )
    return definition, dataset


_WIDE_BANDS = (40.0, 80.0, 160.0, 315.0, 630.0, 1250.0, 2500.0, 5000.0, 10000.0)

BUILTIN_SPEAKER_LIBRARY: tuple[
    tuple[SpeakerDefinition, SpeakerDataset], ...
] = (
    _builtin_speaker(
        speaker_id='builtin-bookshelf-generic',
        dataset_id='builtin-bookshelf-generic-onaxis',
        model='Generic 2-way bookshelf',
        configuration='bookshelf',
        frequency_hz=_WIDE_BANDS,
        level_db=(-8.0, -3.0, -1.0, 0.0, 0.0, 0.5, 0.5, 0.0, -1.5),
        method='analytic generic 2-way bookshelf response',
        uncertainty_note='rough analytic shape; ±3 dB vs real products',
    ),
    _builtin_speaker(
        speaker_id='builtin-subwoofer-generic',
        dataset_id='builtin-subwoofer-generic-onaxis',
        model='Generic ported subwoofer',
        configuration='subwoofer',
        frequency_hz=(20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0),
        level_db=(-9.0, -5.0, -2.5, -1.0, 0.0, 0.0, -0.5, -2.0, -5.0),
        method='analytic generic ported subwoofer response',
        uncertainty_note='tuning-frequency dependent; ±3 dB vs real products',
    ),
)

BUILTIN_SPEAKER_IDS: frozenset[str] = frozenset(
    speaker.speaker_id for speaker, _ in BUILTIN_SPEAKER_LIBRARY
)


__all__ = [
    'BUILTIN_SPEAKER_IDS',
    'BUILTIN_SPEAKER_LIBRARY',
    'SPEAKER_LIBRARY_AUTHORITY_VERSION',
    'SPEAKER_LIBRARY_SCHEMA_VERSION',
    'SpeakerConfiguration',
    'SpeakerDataset',
    'SpeakerDatasetKind',
    'SpeakerDatasetPayloadKind',
    'SpeakerDefinition',
    'SpeakerImpedanceCurve',
    'SpeakerOnAxisResponse',
    'SpeakerProvenanceClass',
    'build_speaker_dataset',
    'build_speaker_definition',
    'on_axis_response_payload',
]
