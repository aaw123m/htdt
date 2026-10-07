"""Sealed external-validation corpus manifest (#836 Action 1).

The external qualification program (#836) moves solver validation upstream
of the owned room: literature/method evidence → HTDT verification →
*external measured benchmark qualification* → project-input qualification →
minimal preregistered owned-room holdout. That ladder only works if the
external corpus itself is a sealed, versioned authority — otherwise
benchmark constants drift into fixtures anonymously and the #809
qualification layer has nothing exact to pin.

This module is that authority:

- :class:`ExternalAssetAdmission` records (shared #834 ledger vocabulary)
  pin each dataset by its exact identifiers — version DOI, record URI,
  licence, and per-file name/size/checksum pins. Payloads are never
  vendored into this repository; the record says where the bytes live and
  what they hash to.
- :class:`CorpusScene` is the sealed per-scene declaration: dataset pin,
  exact source/receiver/material asset files, ``reference_strength``
  (``direct_reference`` vs ``supporting``), expected validity band,
  phenomena exercised, and the HTDT claims the scene may support.
  ``supporting`` scenes can never carry a qualification claim — the
  BRAS CR1–CR4 warning ("must not be treated as direct reference truth")
  is structural, not a note.
- :class:`ExternalCorpusManifest` seals the whole corpus into one
  versioned manifest whose hash downstream records (#809
  preregistrations, #801 adoption evidence) can pin.
- :func:`corpus_fetch_plan` + :func:`verify_fetched_file` implement the
  deterministic fetch/import path: the manifest fixes exactly which URIs
  to pull and which checksums must match — no benchmark bytes and no
  anonymized constants ever enter the repository.

Checksum honesty (mirrors ``cad_external_admission``): a file's ``sha256``
is populated only for payloads actually retrieved and hashed while
building this manifest (``checksum_source='computed'``); files pinned from
the publisher's catalogue carry their published MD5 with
``checksum_source='publisher'`` — the record says so instead of
fabricating a hash.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_external_admission import (
    ExternalAssetAdmission,
    ExternalAssetFile,
    build_external_asset_admission,
    external_asset_file,
)
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


EXTERNAL_CORPUS_AUTHORITY_VERSION = 'external-corpus-manifest-1'
EXTERNAL_CORPUS_SCHEMA_VERSION: Literal[1] = 1

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

CorpusReferenceStrength = Literal['direct_reference', 'supporting']
"""#836 §2.1: ``direct_reference`` scenes (BRAS RS1–RS7, RS8) may act as
per-phenomenon qualification oracles; ``supporting`` scenes (CR1–CR4)
have increased measurement uncertainty and may only support room-scale
comparison — never a primary pass/fail oracle."""

CorpusPermittedClaim = Literal[
    'phenomenon_direct_qualification',
    'input_authority_probe',
    'applicability_limit_evidence',
    'room_scale_supporting_comparison',
    'importer_regression',
]
"""What HTDT may claim from a corpus scene:

- ``phenomenon_direct_qualification`` — the scene may qualify a named
  phenomenon per observable/band for a solver path (#809 evaluation).
- ``input_authority_probe`` — the scene's material/source/receiver
  authority may be evaluated (input-quality classification, #836 A7).
- ``applicability_limit_evidence`` — the scene may bound a solver
  applicability envelope (e.g. RS8 faceting/curvature limits).
- ``room_scale_supporting_comparison`` — room-scale supporting evidence
  only (CR1–CR4; never a pass/fail oracle).
- ``importer_regression`` — the scene may pin importer conformance.
"""

CORPUS_PERMITTED_CLAIMS: frozenset[str] = frozenset(
    {
        'phenomenon_direct_qualification',
        'input_authority_probe',
        'applicability_limit_evidence',
        'room_scale_supporting_comparison',
        'importer_regression',
    }
)

CLAIMS_NEVER_DERIVABLE_FROM_CORPUS: tuple[str, ...] = (
    'owned_room_validation',
    'production_recommendation_eligibility',
    'measured_room_truth',
)
"""Claims an external corpus can never produce regardless of verdict
quality — they stay behind the owned-room campaign (#813) and the
production-adoption gate (#801). Listed for callers/tests that need to
assert the vocabulary complement."""

_SUPPORTING_ONLY_CLAIMS: frozenset[str] = frozenset(
    {'room_scale_supporting_comparison', 'importer_regression'}
)
"""Claims a ``supporting`` scene may carry — anything else would promote
higher-uncertainty data to a direct oracle."""

CorpusFileVerdict = Literal[
    'verified',
    'missing',
    'size_mismatch',
    'md5_mismatch',
    'sha256_mismatch',
    'unverifiable',
]
"""Result of checking one fetched file against its manifest pin.
``verified`` requires every pinned checksum to match; a file with no
pinned checksum is ``unverifiable``, never silently accepted."""


class CorpusScene(BaseModel):
    """Sealed declaration of one scene inside the external corpus."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = EXTERNAL_CORPUS_SCHEMA_VERSION
    scene_id: str = Field(min_length=1)
    dataset_ref: AuthorityRef
    title: str = Field(min_length=1)
    phenomenon_ids: tuple[str, ...] = Field(min_length=1)
    reference_strength: CorpusReferenceStrength
    expected_validity_band_hz: tuple[float, float] | None = None
    validity_band_basis: str = ''
    permitted_claims: tuple[CorpusPermittedClaim, ...] = Field(min_length=1)
    asset_file_names: tuple[str, ...] = Field(min_length=1)
    applicability_notes: str = ''
    scene_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _check(self) -> 'CorpusScene':
        if self.dataset_ref.ref_sha256 is None:
            raise ValueError('dataset_ref must pin its sha256')
        if len(set(self.phenomenon_ids)) != len(self.phenomenon_ids):
            raise ValueError('phenomenon_ids must be unique')
        if len(set(self.permitted_claims)) != len(self.permitted_claims):
            raise ValueError('permitted_claims must be unique')
        if len(set(self.asset_file_names)) != len(self.asset_file_names):
            raise ValueError('asset_file_names must be unique')
        if self.reference_strength == 'supporting' and not set(
            self.permitted_claims
        ) <= _SUPPORTING_ONLY_CLAIMS:
            raise ValueError(
                'supporting scenes may only carry '
                'room_scale_supporting_comparison / importer_regression '
                'claims (#836 §2.1: CR scenes are never direct oracles)'
            )
        if self.expected_validity_band_hz is not None:
            low, high = self.expected_validity_band_hz
            if not (low > 0.0 and high > low):
                raise ValueError(
                    'expected_validity_band_hz must satisfy 0 < low < high'
                )
            if not self.validity_band_basis:
                raise ValueError(
                    'a declared validity band must carry its basis'
                )
        if self.scene_sha256 != _hash(self.identity_payload()):
            raise ValueError('corpus scene semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'scene_sha256'})


def build_corpus_scene(
    *,
    scene_id: str,
    dataset_ref: AuthorityRef,
    title: str,
    phenomenon_ids: tuple[str, ...] | list[str],
    reference_strength: CorpusReferenceStrength,
    permitted_claims: tuple[CorpusPermittedClaim, ...] | list[str],
    asset_file_names: tuple[str, ...] | list[str],
    expected_validity_band_hz: tuple[float, float] | None = None,
    validity_band_basis: str = '',
    applicability_notes: str = '',
) -> CorpusScene:
    probe = CorpusScene.model_construct(
        **canonicalize_payload(
            CorpusScene,
            dict(
                schema_version=EXTERNAL_CORPUS_SCHEMA_VERSION,
                scene_id=scene_id,
                dataset_ref=dataset_ref,
                title=title,
                phenomenon_ids=tuple(phenomenon_ids),
                reference_strength=reference_strength,
                expected_validity_band_hz=expected_validity_band_hz,
                validity_band_basis=validity_band_basis,
                permitted_claims=tuple(permitted_claims),
                asset_file_names=tuple(asset_file_names),
                applicability_notes=applicability_notes,
                scene_sha256='',
            ),
        )
    )
    return CorpusScene(
        **probe.model_dump(mode='python', exclude={'scene_sha256'}),
        scene_sha256=_hash(probe.identity_payload()),
    )


class ExternalCorpusManifest(BaseModel):
    """Sealed, versioned manifest of the external-validation corpus."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = EXTERNAL_CORPUS_SCHEMA_VERSION
    authority_version: Literal['external-corpus-manifest-1'] = (
        EXTERNAL_CORPUS_AUTHORITY_VERSION
    )
    manifest_id: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    manifest_version: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    issued_on: str = Field(min_length=1)
    retrieved_on: str = Field(min_length=1)
    datasets: tuple[ExternalAssetAdmission, ...] = Field(min_length=1)
    scenes: tuple[CorpusScene, ...] = Field(min_length=1)
    retrieval_notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'ExternalCorpusManifest':
        dataset_by_id: dict[str, ExternalAssetAdmission] = {}
        for dataset in self.datasets:
            if dataset.admission_id in dataset_by_id:
                raise ValueError('duplicate dataset admission_id')
            dataset_by_id[dataset.admission_id] = dataset
        scene_ids = [scene.scene_id for scene in self.scenes]
        if len(scene_ids) != len(set(scene_ids)):
            raise ValueError('duplicate corpus scene ids')
        for scene in self.scenes:
            ref = scene.dataset_ref
            dataset = dataset_by_id.get(ref.ref_id)
            if dataset is None:
                raise ValueError(
                    f'scene {scene.scene_id} references unknown dataset '
                    f'{ref.ref_id}'
                )
            if ref.ref_sha256 != dataset.semantic_sha256:
                raise ValueError(
                    f'scene {scene.scene_id} pins a stale dataset '
                    'admission hash'
                )
            file_names = {f.file_name for f in dataset.files}
            missing = set(scene.asset_file_names) - file_names
            if missing:
                raise ValueError(
                    f'scene {scene.scene_id} lists assets not present in '
                    f'its dataset: {sorted(missing)}'
                )
        if self.manifest_sha256 != _hash(self.identity_payload()):
            raise ValueError('external corpus manifest hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'manifest_id', 'manifest_sha256'}
        )

    @classmethod
    def create(
        cls,
        *,
        manifest_version: str,
        corpus_id: str,
        issued_on: str,
        retrieved_on: str,
        datasets: tuple[ExternalAssetAdmission, ...] | list[ExternalAssetAdmission],
        scenes: tuple[CorpusScene, ...] | list[CorpusScene],
        retrieval_notes: str = '',
    ) -> 'ExternalCorpusManifest':
        probe = cls.model_construct(
            **canonicalize_payload(
                cls,
                dict(
                    schema_version=EXTERNAL_CORPUS_SCHEMA_VERSION,
                    authority_version=EXTERNAL_CORPUS_AUTHORITY_VERSION,
                    manifest_id='',
                    manifest_sha256='',
                    manifest_version=manifest_version,
                    corpus_id=corpus_id,
                    issued_on=issued_on,
                    retrieved_on=retrieved_on,
                    datasets=tuple(datasets),
                    scenes=tuple(scenes),
                    retrieval_notes=retrieval_notes,
                ),
            )
        )
        digest = _hash(probe.identity_payload())
        return cls(
            **probe.model_dump(
                mode='python', exclude={'manifest_id', 'manifest_sha256'}
            ),
            manifest_sha256=digest,
            manifest_id=f'ecm-{digest[:24]}',
        )


class CorpusFetchStep(BaseModel):
    """One deterministic fetch step derived from the manifest."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    admission_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    target_relpath: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    md5: str | None = None
    sha256: str | None = None
    checksum_source: str = Field(min_length=1)


class CorpusFileReceipt(BaseModel):
    """Verification receipt for one fetched corpus file."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    file_name: str = Field(min_length=1)
    target_relpath: str = Field(min_length=1)
    verdict: CorpusFileVerdict
    size_bytes: int | None = None
    computed_md5: str | None = None
    computed_sha256: str | None = None


# --- admitted datasets (publisher-verified identifiers) ----------------
#
# BRAS v3 — DepositOnce item 38410727-febb-4769-8002-9c710ba393c4,
# version DOI 10.14279/depositonce-6726.3, handle 11303/7506.3,
# issued 2020-10-06. Licence read from the record: CC BY-SA 4.0
# (dc.rights.uri). Files listed are the ORIGINAL bundle in declared order;
# MD5 + size are the publisher's bitstream catalogue values. SHA-256 is
# populated only where bytes were retrieved and hashed on 2026-10-07
# (checksum_source='computed'); the remaining large packs keep the
# publisher MD5 pin ('publisher').

_V3_BITSTREAM = (
    'https://api-depositonce.tu-berlin.de/server/api/core/bitstreams/'
)
_V3_ORIGINAL_BUNDLE = '117716ac-83ab-4d0b-abfd-7e285e19da1a'


def _v3_uri(bitstream_id: str) -> str:
    return f'{_V3_BITSTREAM}{bitstream_id}/content'


def _v3_file(
    bitstream_id: str,
    file_name: str,
    size_bytes: int,
    md5: str,
    *,
    sha256: str | None = None,
    role: str,
) -> ExternalAssetFile:
    return external_asset_file(
        file_name=file_name,
        uri=_v3_uri(bitstream_id),
        size_bytes=size_bytes,
        md5=md5,
        sha256=sha256,
        checksum_source='computed' if sha256 else 'publisher',
        role=role,
    )


_BRAS_V3_FILES: tuple[ExternalAssetFile, ...] = (
    _v3_file(
        'ba6b4f29-418b-4ec8-bb9d-72d488b8e5e6',
        'Documentation.pdf',
        3678548,
        '19fa6422aa13b16592ddd13f0b25c936',
        sha256=(
            '70c8deff6861f3d4ac7991fddba49c84b1b57bba9bcc0a0928d38263ac0fe4c6'
        ),
        role='documentation',
    ),
    _v3_file(
        '6c82b63c-35be-4348-9f55-cfc9033b5375',
        '1_scene_descriptions-RS1.zip',
        77136315,
        'e7a5d81e041cad49b7caf2ae4d6a4fbf',
        sha256=(
            '4ab7c960219da32bfd13bcc41c2b61f2ba28cc2d586d2439fac394ab07a5c354'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        'aa4d0307-1624-4665-8ddc-d3057478ffdd',
        '1_scene_descriptions-RS2.zip',
        43733693,
        '36cb81a9b4d1048af620a6ee04ca9172',
        sha256=(
            '364bee26173c80571dfa2150c9a59dc0d55aefd2b4b8a58e9d2656eb9324b1e3'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        'e8a64540-c8e2-4f67-b3e9-8434d52e8d73',
        '1_scene_descriptions-RS3.zip',
        46081048,
        '75f53e09784731d5bdf77670de96cf56',
        sha256=(
            '85ee961693fdcbb5c617e58170b8632dbcbb27c6c1de45b593d6ff72f324f3c1'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        '17802d4d-c2fe-4dd7-a378-2be8ddf92ecf',
        '1_scene_descriptions-RS4.zip',
        9745582,
        '5a8c22e282617034d87359b121bc1061',
        sha256=(
            '0112e6b34fbc844f04050e68d9752cd178510d2e60711f0bd5044c579fcc532d'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        'ccce535a-c508-4046-8748-4458b8e73d13',
        '1_scene_descriptions-RS5.zip',
        22138580,
        '4deed6baf50ee8fcbd433850f2422fcf',
        sha256=(
            'b9cb03c945fcf46bf742135108d1acc5246b576e8afb39090547e0f3093f58b5'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        '682afd25-8a84-4202-95fe-5d4c6867e7da',
        '1_scene_descriptions-RS6.zip',
        3904728,
        '0f13a84ad1993a580901fa2f93ecb881',
        sha256=(
            'fe2cb45669ed979d582afd33bd6ebe0caccba8fc23a20608bc7d508260c3ce33'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        '7832c009-906d-40a7-acd5-96c552cce197',
        '1_scene_descriptions-RS7.zip',
        4059197,
        'f925ce5fe37c2e8543359ff5953f0e47',
        sha256=(
            '9526bd3b8a5e0fac08e9b21228bf069bcda15144802076c5a785f7e7985a435e'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        '1278fae8-7d1d-4b92-b522-35b45f2b73fc',
        '1_scene_descriptions-CR1.zip',
        389430541,
        '1248343f851c35ecaa83c253114a2895',
        sha256=(
            '37b984c5dc816bf2d266a8e395c4d4cb21340e971970324c95ecedd04cd7aebc'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        '53c3cf64-3547-4aa6-946b-1b4755729f2a',
        '1_scene_descriptions-CR2.zip',
        615751495,
        '6aa695551c819d88cd5e68aebcf8531b',
        sha256=(
            '8aa6d9959c0979b69773334d42f81533485d5f477968660232d3490bf1214040'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        'e7b13112-0306-4596-9d9f-c6db057b0552',
        '1_scene_descriptions-CR3.zip',
        739877895,
        '796b1a677b6148e02267b92a526a0a1f',
        sha256=(
            'd76e870bd955424dd960456e190f440152f61279db4e1a9961cb378ed88a4f85'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        'bad0610b-293c-47cb-9926-c30c32f9b4c8',
        '1_scene_descriptions-CR4.zip',
        785106943,
        'd0e09fbbbe2b9af501cb9d3c86368bd1',
        sha256=(
            '9abbc98441a75a62fd907ae709d90c9072373f20f90be5869777080bd9622232'
        ),
        role='scene_descriptions',
    ),
    _v3_file(
        'cc60a580-7b43-415d-9fca-ba05d30c2868',
        '2_source_and_receiver_descriptions-FABIAN_HRIRs.zip',
        4705377239,
        '4b2376aa1f375bbe33834b1a5fcd61e5',
        role='source_receiver_descriptions',
    ),
    _v3_file(
        '47712370-a8d7-4c92-9553-b1d919f07556',
        '2_source_and_receiver_descriptions-FABIAN_meshes.zip',
        4733293,
        '014db7a829f2c096f4fbee89a4e76405',
        sha256=(
            'd1dd6965c7f4e430765795bb3de5485b9bd577ab4af19af0ba008c209c01b09b'
        ),
        role='source_receiver_descriptions',
    ),
    _v3_file(
        '7ef363e4-e9b4-4c62-bfb7-b42339d3754f',
        '2_source_and_receiver_descriptions-Genelec_8020c.zip',
        1256424235,
        'd5509c12e006137e024d0da36fd2f579',
        sha256=(
            '6a90c2001c6ba925a46a980273f2c9c08494bee80aa1bfa28be23307e66c271c'
        ),
        role='source_receiver_descriptions',
    ),
    _v3_file(
        '0b08680e-4490-4f99-9234-b53ae12e767b',
        '2_source_and_receiver_descriptions-ITA_dodecahedron.zip',
        253639185,
        '308c4d44f674a451429617257ce32818',
        sha256=(
            'f90429483a8f78aa484948698e1b2aa74c2d3c15f78f9e8fcb406c2acd35715f'
        ),
        role='source_receiver_descriptions',
    ),
    _v3_file(
        '2d4f44cb-6e0d-4ca7-b5d6-e9bc00ebf25e',
        '2_source_and_receiver_descriptions-QSC_K8.zip',
        1358092735,
        '63b65beb977516381f2250b851ab5054',
        sha256=(
            'e848aa4a29f003a6bb972dc82dd580be9ca99d75d745718d25211b0682167ddb'
        ),
        role='source_receiver_descriptions',
    ),
    _v3_file(
        'b2970524-fb10-482a-ab14-f07da5ad7615',
        '3_surface_descriptions.zip',
        235694104,
        'c6457c67b4b073bf6cad39fa937ce313',
        sha256=(
            '9edc0c4286053e4a8a099d8de759537e202254e2e0da0bcc55b2c29463c34114'
        ),
        role='material_surface_data',
    ),
    _v3_file(
        '5e27800e-78ef-47a8-855e-78ac5a5adff6',
        '4_additional_data.zip',
        23634327,
        '045877c4106f28408e74a20f5b1c14b4',
        sha256=(
            '8bdeeff49cf0198dc667b87353345b86612c041236340877ef1c469593b28144'
        ),
        role='additional_evaluation_data',
    ),
)

BRAS_V3_ADMISSION: ExternalAssetAdmission = build_external_asset_admission(
    admission_id='bras-v3',
    dataset_name='bras-v3',
    dataset_title='BRAS - Benchmark for Room Acoustical Simulation',
    publisher='Technische Universität Berlin (DepositOnce)',
    source_kind='institutional_repository',
    admission_state='download_on_demand_candidate',
    version_doi='10.14279/depositonce-6726.3',
    version_record_id='11303/7506.3',
    record_uri=(
        'https://depositonce.tu-berlin.de/items/'
        '38410727-febb-4769-8002-9c710ba393c4'
    ),
    license_id='cc-by-sa-4.0',
    license_family='cc_by_sa',
    license_uri='https://creativecommons.org/licenses/by-sa/4.0/',
    license_note=(
        'dc.rights.uri on the version record is CC BY-SA 4.0; payloads are '
        'never vendored — retrieved on demand through the fetch command '
        'only. Share-alike applies to any redistributed derivative data.'
    ),
    files=_BRAS_V3_FILES,
    dataset_notes=(
        'Seven reference scenes (RS1–RS7) plus four complex scenes '
        '(CR1–CR4). Per the dataset warning, the complex scenes have '
        'increased measurement uncertainty and must not be treated as '
        'direct reference truth. Concept paper: '
        'doi:10.1016/j.apacoust.2020.107867. Issued 2020-10-06.'
    ),
)

# BRAS RS8 — DepositOnce item 326fed8c-636a-47f2-977d-b4fd6a0ce771,
# version DOI 10.14279/depositonce-25649, handle 11303/26816,
# issued 2026-05-08. Licence read from the record: CC BY-SA 4.0.
# All four ORIGINAL files were retrieved and hashed on 2026-10-07.

_RS8_BITSTREAM = _V3_BITSTREAM

_BRAS_RS8_FILES: tuple[ExternalAssetFile, ...] = (
    external_asset_file(
        file_name='Documentation.pdf',
        uri=(
            f'{_RS8_BITSTREAM}'
            'ae5a98a8-abdd-4d27-addb-be6d489b936a/content'
        ),
        size_bytes=1076205,
        md5='a46e1b60b9d4916dbb7a393633be9e8a',
        sha256=(
            'cefc0056f0dd37c61a652417eff764fe3056cdf9dbf2d69686218dcf5be7e0b4'
        ),
        checksum_source='computed',
        role='documentation',
    ),
    external_asset_file(
        file_name='1_Scene_descriptions.zip',
        uri=(
            f'{_RS8_BITSTREAM}'
            '51014f74-ecce-4df6-ae62-d1c043184830/content'
        ),
        size_bytes=255795525,
        md5='c0aaa9dffaa332a4d8b928e0c3959c07',
        sha256=(
            '0d0cf1f3b4d60f55b7fe9c45f40097c8a4771e9d4e861d196f823e5e5e9eb0f4'
        ),
        checksum_source='computed',
        role='scene_descriptions',
    ),
    external_asset_file(
        file_name='2_Source_descriptions.zip',
        uri=(
            f'{_RS8_BITSTREAM}'
            '3db85912-cac0-4547-bb45-f02a515715c8/content'
        ),
        size_bytes=776309027,
        md5='7d8587b29d26c4d8836386df26f84085',
        sha256=(
            '14a4bf7d42d455babff5fbda8e61f02b761c21ce9bee5c2a35486e6ad5d3c911'
        ),
        checksum_source='computed',
        role='source_receiver_descriptions',
    ),
    external_asset_file(
        file_name='3_Surface_descriptions.zip',
        uri=(
            f'{_RS8_BITSTREAM}'
            '78b5cef0-99c3-42f6-bcca-8084e351561b/content'
        ),
        size_bytes=304822,
        md5='274596f6249a648704f76c027dd7f13d',
        sha256=(
            'd052bed3d5a4e03d971bc805c59570ed0bb885b3170dc2a952953258e7504c81'
        ),
        checksum_source='computed',
        role='material_surface_data',
    ),
)

BRAS_RS8_ADMISSION: ExternalAssetAdmission = build_external_asset_admission(
    admission_id='bras-rs8',
    dataset_name='bras-rs8',
    dataset_title=(
        'BRAS: RS8 - Benchmark for Room Acoustical Simulation: '
        'Reference Scene 8'
    ),
    publisher='Technische Universität Berlin (DepositOnce)',
    source_kind='institutional_repository',
    admission_state='download_on_demand_candidate',
    version_doi='10.14279/depositonce-25649',
    version_record_id='11303/26816',
    record_uri=(
        'https://depositonce.tu-berlin.de/items/'
        '326fed8c-636a-47f2-977d-b4fd6a0ce771'
    ),
    license_id='cc-by-sa-4.0',
    license_family='cc_by_sa',
    license_uri='https://creativecommons.org/licenses/by-sa/4.0/',
    license_note=(
        'RS8 is explicitly CC BY-SA 4.0 per the dataset record '
        '(dc.rights.uri). Payloads are never vendored — retrieved on '
        'demand through the fetch command only.'
    ),
    files=_BRAS_RS8_FILES,
    dataset_notes=(
        'Finite curved-reflector extension (2026): nine scene '
        'configurations (RS8_01a, RS8_01b, RS8_01c, RS8_02, RS8_03a–e) '
        'with calibrated RIRs (SOFA + wav), STL/Blender geometry plus a '
        'parametric faceting script, measured Genelec 8331A source '
        'directivity in SOFA on a 1°×1° grid, third-octave '
        'absorption/scattering and complex surface impedance for '
        '100 Hz–4 kHz. Companion paper: doi:10.71568/daga2026.367. '
        'Issued 2026-05-08.'
    ),
)

EXTERNAL_CORPUS_DATASETS: tuple[ExternalAssetAdmission, ...] = (
    BRAS_V3_ADMISSION,
    BRAS_RS8_ADMISSION,
)
"""Every dataset the corpus may draw scenes from, pinned by admission."""


def _dataset_ref(admission: ExternalAssetAdmission) -> AuthorityRef:
    return AuthorityRef(
        kind='external_asset_admission',
        ref_id=admission.admission_id,
        ref_sha256=admission.semantic_sha256,
    )


def _v3_scene_assets(scene_file: str) -> tuple[str, ...]:
    """Files a v3 reference scene consumes: its scene package plus the
    shared source/receiver and material authorities it was measured
    against."""
    return (
        'Documentation.pdf',
        scene_file,
        '2_source_and_receiver_descriptions-FABIAN_HRIRs.zip',
        '2_source_and_receiver_descriptions-FABIAN_meshes.zip',
        '2_source_and_receiver_descriptions-Genelec_8020c.zip',
        '2_source_and_receiver_descriptions-ITA_dodecahedron.zip',
        '2_source_and_receiver_descriptions-QSC_K8.zip',
        '3_surface_descriptions.zip',
    )


_V3_SCENE_NOTES = (
    'Dataset publishes no scene-level frequency bound; the usable band '
    'must come from the metric manifest (#836 Action 3), bounded by the '
    'measured source/receiver authority above.'
)

_V3_RS_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        'RS1',
        'RS1 single reflection (infinite plate)',
        ('specular_reflection', 'scattering'),
    ),
    (
        'RS2',
        'RS2 single reflection (finite plate)',
        ('specular_reflection', 'edge_diffraction'),
    ),
    (
        'RS3',
        'RS3 multiple reflection (parallel finite plates)',
        ('specular_reflection', 'late_energy_decay'),
    ),
    (
        'RS4',
        'RS4 single reflection (reflector array)',
        ('specular_reflection', 'scattering'),
    ),
    (
        'RS5',
        'RS5 diffraction (infinite wedge)',
        ('edge_diffraction',),
    ),
    (
        'RS6',
        'RS6 diffraction (finite body)',
        ('edge_diffraction',),
    ),
    (
        'RS7',
        'RS7 multiple diffraction (seat dip effect)',
        ('edge_diffraction',),
    ),
)

_V3_CR_NOTES = (
    'Complex scene with increased measurement uncertainty — dataset '
    'warning: must not be treated as direct reference truth (#836 §2.1). '
    'Room-scale supporting comparison only.'

)


def _v3_reference_scenes() -> list[CorpusScene]:
    scenes: list[CorpusScene] = []
    for scene_id, title, phenomena in _V3_RS_SPECS:
        scenes.append(
            build_corpus_scene(
                scene_id=scene_id,
                dataset_ref=_dataset_ref(BRAS_V3_ADMISSION),
                title=title,
                phenomenon_ids=phenomena,
                reference_strength='direct_reference',
                permitted_claims=(
                    'phenomenon_direct_qualification',
                    'input_authority_probe',
                    'importer_regression',
                ),
                asset_file_names=_v3_scene_assets(
                    f'1_scene_descriptions-{scene_id}.zip'
                ),
                applicability_notes=_V3_SCENE_NOTES,
            )
        )
    for index in range(1, 5):
        scenes.append(
            build_corpus_scene(
                scene_id=f'CR{index}',
                dataset_ref=_dataset_ref(BRAS_V3_ADMISSION),
                title=f'BRAS complex scene CR{index}',
                phenomenon_ids=('complex_room_response',),
                reference_strength='supporting',
                permitted_claims=(
                    'room_scale_supporting_comparison',
                    'importer_regression',
                ),
                asset_file_names=(
                    *_v3_scene_assets(
                        f'1_scene_descriptions-CR{index}.zip'
                    ),
                    '4_additional_data.zip',
                ),
                applicability_notes=_V3_CR_NOTES,
            )
        )
    return scenes


_RS8_CONFIG_IDS: tuple[str, ...] = (
    'RS8_01a',
    'RS8_01b',
    'RS8_01c',
    'RS8_02',
    'RS8_03a',
    'RS8_03b',
    'RS8_03c',
    'RS8_03d',
    'RS8_03e',
)
"""The nine measured scene configurations inside RS8's
``1_Scene_descriptions.zip`` (RS8 curved reflector (finite plate))."""

_RS8_SCENE_NOTES = (
    'For the specific RS8 reflector geometry the companion paper records '
    'conservative geometric criteria — curvature approximately satisfied '
    'above ~350 Hz, reflector-height criterion above ~450 Hz. These are '
    'scene-specific physical applicability limits, not universal GA '
    'crossover frequencies. Planar faceting of the reflector can create '
    'discontinuities in the reflected field — the dataset is ground truth '
    'for exactly those GA behaviours.'
)

_RS8_SCENE_FILES: tuple[str, ...] = (
    'Documentation.pdf',
    '1_Scene_descriptions.zip',
    '2_Source_descriptions.zip',
    '3_Surface_descriptions.zip',
)


def _rs8_scenes() -> list[CorpusScene]:
    return [
        build_corpus_scene(
            scene_id=config_id,
            dataset_ref=_dataset_ref(BRAS_RS8_ADMISSION),
            title=f'RS8 finite curved reflector — configuration {config_id[4:]}',
            phenomenon_ids=('specular_reflection', 'edge_diffraction'),
            reference_strength='direct_reference',
            permitted_claims=(
                'phenomenon_direct_qualification',
                'input_authority_probe',
                'applicability_limit_evidence',
                'importer_regression',
            ),
            asset_file_names=_RS8_SCENE_FILES,
            expected_validity_band_hz=(100.0, 4000.0),
            validity_band_basis=(
                'dataset material data (third-octave absorption/scattering '
                'and complex surface impedance) published for '
                '100 Hz–4 kHz'
            ),
            applicability_notes=_RS8_SCENE_NOTES,
        )
        for config_id in _RS8_CONFIG_IDS
    ]


def corpus_scenes() -> tuple[CorpusScene, ...]:
    """Every declared corpus scene, direct-reference first."""
    return tuple([*_v3_reference_scenes(), *_rs8_scenes()])


def external_corpus_manifest() -> ExternalCorpusManifest:
    """The sealed, versioned external-validation corpus manifest."""
    return ExternalCorpusManifest.create(
        manifest_version='1',
        corpus_id='bras-external-validation-corpus',
        issued_on='2026-10-07',
        retrieved_on='2026-10-07',
        datasets=EXTERNAL_CORPUS_DATASETS,
        scenes=corpus_scenes(),
        retrieval_notes=(
            'File names, sizes and MD5 checksums retrieved from the '
            'DepositOnce REST catalogue on 2026-10-07; SHA-256 recorded '
            'for every payload actually fetched and hashed this session '
            '(checksum_source=computed). The one pack not retrieved in '
            'this pass — the 4.7 GB FABIAN HRIR set — keeps its publisher '
            'MD5 pin until a fetch run records a computed hash.'
        ),
    )


def corpus_dataset(
    manifest: ExternalCorpusManifest, admission_id: str
) -> ExternalAssetAdmission | None:
    for dataset in manifest.datasets:
        if dataset.admission_id == admission_id:
            return dataset
    return None


def corpus_scene(
    manifest: ExternalCorpusManifest, scene_id: str
) -> CorpusScene | None:
    for scene in manifest.scenes:
        if scene.scene_id == scene_id:
            return scene
    return None


def corpus_fetch_plan(
    manifest: ExternalCorpusManifest,
    dataset_ids: tuple[str, ...] | list[str] | None = None,
) -> tuple[CorpusFetchStep, ...]:
    """Deterministic fetch plan: manifest order, no wall-clock fields.

    Only files of datasets admitted as ``download_on_demand_candidate``
    are planned — an admission state that does not allow retrieval
    produces no steps instead of a silent download.
    """
    allowed = (
        None if dataset_ids is None else frozenset(dataset_ids)
    )
    steps: list[CorpusFetchStep] = []
    for dataset in manifest.datasets:
        if allowed is not None and dataset.admission_id not in allowed:
            continue
        if dataset.admission_state != 'download_on_demand_candidate':
            continue
        for file in dataset.files:
            steps.append(
                CorpusFetchStep(
                    admission_id=dataset.admission_id,
                    file_name=file.file_name,
                    uri=file.uri,
                    target_relpath=f'{dataset.dataset_name}/{file.file_name}',
                    size_bytes=file.size_bytes,
                    md5=file.md5,
                    sha256=file.sha256,
                    checksum_source=file.checksum_source,
                )
            )
    return tuple(steps)


def verify_fetched_file(
    step: CorpusFetchStep, path: Path | str
) -> CorpusFileReceipt:
    """Check a fetched file against its manifest pin — fail closed.

    ``verified`` requires the size and every pinned checksum to match.
    A file whose step pins no checksum at all is ``unverifiable``; the
    caller must not treat it as admitted bytes.
    """
    path = Path(path)
    base = {
        'file_name': step.file_name,
        'target_relpath': step.target_relpath,
    }
    if not path.is_file():
        return CorpusFileReceipt(**base, verdict='missing')
    size = path.stat().st_size
    if size != step.size_bytes:
        return CorpusFileReceipt(
            **base, verdict='size_mismatch', size_bytes=size
        )
    if step.md5 is None and step.sha256 is None:
        return CorpusFileReceipt(
            **base, verdict='unverifiable', size_bytes=size
        )
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            md5.update(chunk)
            sha256.update(chunk)
    computed_md5 = md5.hexdigest()
    computed_sha256 = sha256.hexdigest()
    if step.md5 is not None and computed_md5 != step.md5:
        return CorpusFileReceipt(
            **base,
            verdict='md5_mismatch',
            size_bytes=size,
            computed_md5=computed_md5,
            computed_sha256=computed_sha256,
        )
    if step.sha256 is not None and computed_sha256 != step.sha256:
        return CorpusFileReceipt(
            **base,
            verdict='sha256_mismatch',
            size_bytes=size,
            computed_md5=computed_md5,
            computed_sha256=computed_sha256,
        )
    return CorpusFileReceipt(
        **base,
        verdict='verified',
        size_bytes=size,
        computed_md5=computed_md5,
        computed_sha256=computed_sha256,
    )


__all__ = [
    'BRAS_RS8_ADMISSION',
    'BRAS_V3_ADMISSION',
    'CLAIMS_NEVER_DERIVABLE_FROM_CORPUS',
    'CORPUS_PERMITTED_CLAIMS',
    'EXTERNAL_CORPUS_AUTHORITY_VERSION',
    'EXTERNAL_CORPUS_DATASETS',
    'EXTERNAL_CORPUS_SCHEMA_VERSION',
    'CorpusFetchStep',
    'CorpusFileReceipt',
    'CorpusFileVerdict',
    'CorpusPermittedClaim',
    'CorpusReferenceStrength',
    'CorpusScene',
    'ExternalCorpusManifest',
    'build_corpus_scene',
    'corpus_dataset',
    'corpus_fetch_plan',
    'corpus_scene',
    'corpus_scenes',
    'external_corpus_manifest',
    'verify_fetched_file',
]
