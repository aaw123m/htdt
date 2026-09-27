"""Anechoic programme-audio reference pack (#1070).

Admitted CC-licensed dry (anechoic) programme material — orchestra, speech,
and instrument recordings — as named, provenance-pinned source assets for
deterministic auralization renders (#515) and ABX listening validation
(#518, #784). Programme audio is *source material*: a pack entry describes
a published recording with its exact repository pin, license and declared
permitted uses — it never bundles bytes, never invents a take's metadata,
and never upgrades a recording into a room measurement.

Rules:

- every asset resolves to an :class:`ExternalAssetAdmission` record — the
  pin (DOI/record id, file names, sizes, checksums where published) is the
  identity, not a local file path;
- ``permitted_uses`` is fail-closed: a use not declared for the asset's
  license family reports ``license_blocked`` instead of being assumed;
- anechoic recordings are ``measured`` evidence (real performances
  captured in an anechoic chamber) — they are *programme*, so they may
  feed :mod:`cad_auralization` renders but are never presented as impulse
  responses, room data, or ABX ground truth;
- recordings whose license is not verified CC-compatible are admitted
  with ``license_family='unknown'`` and are blocked for every use until
  the #834 ledger is updated.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_external_admission import (
    ExternalAssetAdmission,
    LicenseFamily,
    build_external_asset_admission,
    external_asset_file,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


ANECHOIC_PROGRAMME_AUTHORITY_VERSION = 'anechoic-programme-1'






ProgrammeCategory = Literal[
    'orchestral',
    'ensemble',
    'instrument',
    'speech',
    'mixed',
]
"""Content category of one anechoic programme asset."""

ProgrammeUse = Literal[
    'auralization_source',
    'abx_programme',
    'render_benchmark',
    'directivity_probe',
]
"""Declared use of a programme asset. ``auralization_source`` feeds #515
renders; ``abx_programme`` feeds #518/#784 blind comparisons;
``render_benchmark`` anchors reproducible render checks;
``directivity_probe`` uses instrument tracks for directivity fitting."""

ProgrammeAdmissibility = Literal[
    'ready',
    'license_blocked',
    'unadmitted',
    'missing_asset',
]
"""Fail-closed admissibility verdicts for a (asset, use) pair."""


# --- admitted datasets (publisher-verified identifiers) -----------------
#
# Sources verified 2026-09 against the publisher repositories:
# - TU Berlin DepositOnce (CC BY 4.0, doi:10.14279/depositonce-6729.2)
# - Zenodo records 840025 (cc-by-4.0), 1186905 (cc-by-sa-4.0),
#   7818761 (cc-by-4.0).

TUB_BEETHOVEN8_ADMISSION: ExternalAssetAdmission = (
    build_external_asset_admission(
        admission_id='tub-beethoven8-anechoic',
        dataset_name='tub-beethoven8-op93',
        dataset_title=(
            'A Multi-channel Anechoic Orchestra Recording of '
            "Beethoven's Symphony No. 8 op. 93"
        ),
        publisher='TU Berlin (DepositOnce)',
        source_kind='institutional_repository',
        admission_state='download_on_demand_candidate',
        version_doi='10.14279/depositonce-6729.2',
        record_uri=(
            'https://depositonce.tu-berlin.de/items/'
            'eacae473-68e1-48fa-8137-3c2cd30919f0'
        ),
        license_id='cc-by-4.0',
        license_family='cc_by',
        license_uri='https://creativecommons.org/licenses/by/4.0/',
        license_note=(
            'Publisher declares CC BY 4.0 (dc.rights.uri on the '
            'DepositOnce record).'
        ),
        files=(
            external_asset_file(
                file_name='movement1.zip',
                uri='https://depositonce.tu-berlin.de/items/'
                'eacae473-68e1-48fa-8137-3c2cd30919f0',
                size_bytes=612 * 1000 * 1000,
                checksum_source='none',
                role='orchestra_tracks',
            ),
            external_asset_file(
                file_name='movement2.zip',
                uri='https://depositonce.tu-berlin.de/items/'
                'eacae473-68e1-48fa-8137-3c2cd30919f0',
                size_bytes=560 * 1000 * 1000,
                checksum_source='none',
                role='orchestra_tracks',
            ),
            external_asset_file(
                file_name='movement4.zip',
                uri='https://depositonce.tu-berlin.de/items/'
                'eacae473-68e1-48fa-8137-3c2cd30919f0',
                size_bytes=708 * 1000 * 1000,
                checksum_source='none',
                role='orchestra_tracks',
            ),
            external_asset_file(
                file_name='micfilter.zip',
                uri='https://depositonce.tu-berlin.de/items/'
                'eacae473-68e1-48fa-8137-3c2cd30919f0',
                size_bytes=331 * 1000,
                checksum_source='none',
                role='measurement_filters',
            ),
        ),
        dataset_notes=(
            'Anechoic recording performed by Orchester Wiener Akademie '
            '(cond. Martin Haselboeck) in the TU Berlin anechoic '
            'chamber; per-instrument close-mic stems plus documentation. '
            'Publisher publishes sizes but no checksums '
            "(checksum_source='none')."
        ),
    )
)

PHENICX_ANECHOIC_ADMISSION: ExternalAssetAdmission = (
    build_external_asset_admission(
        admission_id='phenicx-anechoic-denoised',
        dataset_name='phenicx-anechoic',
        dataset_title=(
            'PHENICX-Anechoic: note annotations for Aalto anechoic '
            'orchestral database'
        ),
        publisher='UPF / Aalto University (Zenodo)',
        source_kind='zenodo_record',
        admission_state='download_on_demand_candidate',
        concept_doi='10.5281/zenodo.840024',
        version_doi='10.5281/zenodo.840025',
        version_record_id='840025',
        record_uri='https://doi.org/10.5281/zenodo.840025',
        license_id='cc-by-4.0',
        license_family='cc_by',
        license_uri='https://creativecommons.org/licenses/by/4.0/',
        files=(
            external_asset_file(
                file_name='PHENICX-Anechoic.zip',
                uri='https://zenodo.org/api/records/840025/files/'
                'PHENICX-Anechoic.zip/content',
                size_bytes=763716294,
                checksum_source='none',
                role='denoised_orchestra_stems',
            ),
        ),
        dataset_notes=(
            'Denoised versions of the Aalto anechoic orchestral '
            'recordings (Paetynen, Pulkki, Lokki 2008); the original '
            'recordings stay with Aalto — only the denoised set is '
            'CC BY 4.0 here.'
        ),
    )
)

OPENAIR_ANECHOIC_ADMISSION: ExternalAssetAdmission = (
    build_external_asset_admission(
        admission_id='openair-anechoic-snapshot-2018',
        dataset_name='openair-anechoic-2018-02-26',
        dataset_title=(
            'Snapshot of anechoic data from OpenAIRlib.net, '
            '26th February 2018'
        ),
        publisher='OpenAIR / University of York (Zenodo)',
        source_kind='zenodo_record',
        admission_state='download_on_demand_candidate',
        version_doi='10.5281/zenodo.1186905',
        version_record_id='1186905',
        record_uri='https://doi.org/10.5281/zenodo.1186905',
        license_id='cc-by-sa-4.0',
        license_family='cc_by_sa',
        license_uri='https://creativecommons.org/licenses/by-sa/4.0/',
        license_note=(
            'Share-alike: redistribution of auralized derivatives must '
            'carry CC BY-SA. Use is permitted; republishing rendered '
            'bundles requires share-alike handling.'
        ),
        files=(
            external_asset_file(
                file_name='anechoic_openAIRlib_ccsa.zip',
                uri='https://zenodo.org/api/records/1186905/files/'
                'anechoic_openAIRlib_ccsa.zip/content',
                size_bytes=575093490,
                checksum_source='none',
                role='mixed_anechoic_programme',
            ),
        ),
        dataset_notes=(
            'Mixed anechoic recordings (speech, instruments, solo '
            'passages) published under CC BY-SA 4.0.'
        ),
    )
)

AIRCADE_ADMISSION: ExternalAssetAdmission = build_external_asset_admission(
    admission_id='aircade-v0-0-1',
    dataset_name='aircade',
    dataset_title=(
        'Anechoic and IR Convolution-based Auralization Data '
        'Compilation Ensemble (AIRCADE)'
    ),
    publisher='AIRCADE authors (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.7818760',
    version_doi='10.5281/zenodo.7818761',
    version_record_id='7818761',
    record_uri='https://doi.org/10.5281/zenodo.7818761',
    license_id='cc-by-4.0',
    license_family='cc_by',
    license_uri='https://creativecommons.org/licenses/by/4.0/',
    files=(
        external_asset_file(
            file_name='speech.zip',
            uri='https://doi.org/10.5281/zenodo.7818761',
            size_bytes=0,
            checksum_source='none',
            role='speech_anechoic',
        ),
        external_asset_file(
            file_name='song.zip',
            uri='https://doi.org/10.5281/zenodo.7818761',
            size_bytes=0,
            checksum_source='none',
            role='song_anechoic',
        ),
        external_asset_file(
            file_name='guitar.zip',
            uri='https://doi.org/10.5281/zenodo.7818761',
            size_bytes=808800000,
            checksum_source='none',
            role='instrument_anechoic',
        ),
    ),
    dataset_notes=(
        'Anechoic speech, song and acoustic-guitar recordings plus IR '
        'samples for convolution auralization; ensemble sizes '
        'tiny..large. File sizes are record-level where published.'
    ),
)

ANECHOIC_PROGRAMME_ADMISSIONS: tuple[ExternalAssetAdmission, ...] = (
    TUB_BEETHOVEN8_ADMISSION,
    PHENICX_ANECHOIC_ADMISSION,
    OPENAIR_ANECHOIC_ADMISSION,
    AIRCADE_ADMISSION,
)
"""All admitted anechoic programme datasets, pinned by admission record."""


class AnechoicProgrammeAsset(BaseModel):
    """One programme asset drawn from an admitted dataset."""

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(min_length=1)
    admission_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: ProgrammeCategory
    source_file: str | None = None
    """File name inside the admission record when the asset maps to one
    published file; ``None`` for per-track datasets."""
    channel_count: int | None = Field(default=None, ge=1)
    sample_rate_hz: int | None = Field(default=None, gt=0)
    duration_s: float | None = Field(default=None, gt=0.0)
    permitted_uses: tuple[ProgrammeUse, ...] = Field(min_length=1)
    notes: str = ''
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _check(self) -> 'AnechoicProgrammeAsset':
        if not self.permitted_uses:
            raise ValueError('permitted_uses must not be empty')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('programme asset semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )


def build_programme_asset(
    *,
    asset_id: str,
    admission_id: str,
    title: str,
    category: ProgrammeCategory,
    permitted_uses: tuple[ProgrammeUse, ...],
    source_file: str | None = None,
    channel_count: int | None = None,
    sample_rate_hz: int | None = None,
    duration_s: float | None = None,
    notes: str = '',
) -> AnechoicProgrammeAsset:
    probe = AnechoicProgrammeAsset.model_construct(
        asset_id=asset_id,
        admission_id=admission_id,
        title=title,
        category=category,
        source_file=source_file,
        channel_count=channel_count,
        sample_rate_hz=sample_rate_hz,
        duration_s=duration_s,
        permitted_uses=tuple(permitted_uses),
        notes=notes,
        semantic_sha256='',
    )
    return AnechoicProgrammeAsset(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def _uses_for_license(family: LicenseFamily) -> tuple[ProgrammeUse, ...]:
    if family in ('cc0', 'cc_by', 'apache_2_0', 'mit'):
        return (
            'auralization_source',
            'abx_programme',
            'render_benchmark',
            'directivity_probe',
        )
    if family == 'cc_by_sa':
        return (
            'auralization_source',
            'abx_programme',
            'render_benchmark',
        )
    return ()


def programme_assets() -> tuple[AnechoicProgrammeAsset, ...]:
    """The curated programme-asset pack across the admitted datasets.

    Permitted uses are derived from each dataset's license family —
    never hand-edited per asset.
    """
    assets: list[AnechoicProgrammeAsset] = []
    for admission in ANECHOIC_PROGRAMME_ADMISSIONS:
        uses = _uses_for_license(admission.license_family)
        if not uses:
            continue
        if admission.admission_id == 'tub-beethoven8-anechoic':
            for movement in ('movement1', 'movement2', 'movement4'):
                assets.append(
                    build_programme_asset(
                        asset_id=f'tub-beethoven8-{movement}',
                        admission_id=admission.admission_id,
                        title=(
                            'Beethoven Symphony No. 8 op. 93 — '
                            f'{movement} (anechoic, TU Berlin)'
                        ),
                        category='orchestral',
                        source_file=f'{movement}.zip',
                        permitted_uses=uses,
                        notes=(
                            'Per-instrument close-mic stems; '
                            'anechoic chamber TU Berlin.'
                        ),
                    )
                )
        elif admission.admission_id == 'phenicx-anechoic-denoised':
            assets.append(
                build_programme_asset(
                    asset_id='phenicx-anechoic-ensemble',
                    admission_id=admission.admission_id,
                    title=(
                        'PHENICX-Anechoic denoised orchestral '
                        'recordings (Aalto)'
                    ),
                    category='orchestral',
                    source_file='PHENICX-Anechoic.zip',
                    permitted_uses=uses,
                    notes=(
                        'Denoised Aalto anechoic orchestral stems '
                        'with note annotations.'
                    ),
                )
            )
        elif admission.admission_id == 'openair-anechoic-snapshot-2018':
            assets.append(
                build_programme_asset(
                    asset_id='openair-anechoic-mixed',
                    admission_id=admission.admission_id,
                    title='OpenAIR anechoic snapshot (mixed speech/instrument)',
                    category='mixed',
                    source_file='anechoic_openAIRlib_ccsa.zip',
                    permitted_uses=uses,
                    notes='CC BY-SA — share-alike on redistributed renders.',
                )
            )
        elif admission.admission_id == 'aircade-v0-0-1':
            for part, category in (
                ('speech', 'speech'),
                ('song', 'mixed'),
                ('guitar', 'instrument'),
            ):
                assets.append(
                    build_programme_asset(
                        asset_id=f'aircade-{part}',
                        admission_id=admission.admission_id,
                        title=f'AIRCADE {part} anechoic samples',
                        category=category,  # type: ignore[arg-type]
                        source_file=f'{part}.zip',
                        permitted_uses=uses,
                    )
                )
    return tuple(assets)


def admission_for(asset: AnechoicProgrammeAsset) -> (
    ExternalAssetAdmission | None
):
    for admission in ANECHOIC_PROGRAMME_ADMISSIONS:
        if admission.admission_id == asset.admission_id:
            return admission
    return None


def evaluate_programme_admissibility(
    asset: AnechoicProgrammeAsset, use: ProgrammeUse
) -> ProgrammeAdmissibility:
    """Fail-closed verdict for binding ``asset`` into ``use``.

    ``ready`` only when the admission record exists, its state allows
    download-on-demand use, and the asset declares the use — otherwise
    the exact blocking verdict is reported.
    """
    admission = admission_for(asset)
    if admission is None:
        return 'missing_asset'
    if admission.admission_state in (
        'ineligible',
        'permission_required',
        'license_review_required',
        'third_party_discovery_only',
    ):
        return 'unadmitted'
    if use not in asset.permitted_uses:
        return 'license_blocked'
    return 'ready'


__all__ = [
    'AIRCADE_ADMISSION',
    'ANECHOIC_PROGRAMME_ADMISSIONS',
    'ANECHOIC_PROGRAMME_AUTHORITY_VERSION',
    'AnechoicProgrammeAsset',
    'OPENAIR_ANECHOIC_ADMISSION',
    'PHENICX_ANECHOIC_ADMISSION',
    'ProgrammeAdmissibility',
    'ProgrammeCategory',
    'ProgrammeUse',
    'TUB_BEETHOVEN8_ADMISSION',
    'admission_for',
    'build_programme_asset',
    'evaluate_programme_admissibility',
    'programme_assets',
]
