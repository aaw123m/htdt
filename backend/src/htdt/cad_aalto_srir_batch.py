"""#1060 — Aalto open SRIR benchmark admission batch.

Registers the three Aalto-published open SRIR datasets as #834-compatible
download-on-demand admission records and normalizes each into a #875
``BenchmarkCase`` skeleton carrying an explicit observable matrix.

Datasets (all CC BY 4.0, Zenodo):

- **OK5** — Spatial Room Impulse Responses from 25 Spaces (record
  18622201, concept DOI 10.5281/zenodo.18622200): 70 SRIRs across 25
  rooms, Genelec 8030A source + GRAS 50VI-1 32-ch spherical array.
- **Variable-acoustics room** — 6DoF SRIRs in a room with configurable
  absorbers (version record 6382405, concept DOI 10.5281/zenodo.5720723):
  Eigenmike + Zylia captures, raw and spherical-harmonic domains.
- **Coupled-room transition** — SRIRs for the transition between coupled
  rooms (record 7848561): 16 SOFA files over 4 room pairs × source room ×
  LOS/no-LOS — the primary #1029 coupled-volume validation candidate.

Contract: measured datasets enter as ``external_measured`` (never conflated
with the simulated corpus); what the publishers did not state — room
materials, absolute level calibration — stays UNKNOWN in the normalized
case rather than being filled from photographs or guesses.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_benchmark import (
    BenchmarkCase,
    BenchmarkObservable,
    BenchmarkPoint,
    BenchmarkSourceAsset,
)
from .cad_equipment import EquipmentDataProvenance
from .cad_external_admission import (
    ExternalAssetAdmission,
    build_external_asset_admission,
    external_asset_file,
)


def _canonical(value: Any) -> str:
    import json
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict) -> str:
    import hashlib
    return hashlib.sha256(
        _canonical(payload).encode('utf-8')
    ).hexdigest()


def _zenodo_file(record: str, name: str) -> str:
    return f'https://zenodo.org/records/{record}/files/{name}'


def _provenance(source_reference: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='zenodo-record-verified',
        source_version='1',
        source_reference=source_reference,
        source_sha256='0' * 64,
    )


# ---------------------------------------------------------------------------
# OK5 — 25-spaces SRIR corpus
# ---------------------------------------------------------------------------

OK5_ADMISSION = build_external_asset_admission(
    admission_id='ledger/aalto-ok5-srir',
    dataset_name='aalto-ok5-srir',
    dataset_title=(
        'OK5: Spatial Room Impulse Responses from 25 Spaces in our Work '
        'Environment'
    ),
    publisher='Aalto Acoustics Lab (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.18622200',
    version_doi='10.5281/zenodo.18622201',
    version_record_id='18622201',
    record_uri='https://zenodo.org/records/18622201',
    license_id='cc-by-4.0',
    license_family='cc_by',
    license_uri='https://creativecommons.org/licenses/by/4.0/',
    license_note=(
        'CC BY 4.0 per Zenodo record; the concept DOI resolves to the '
        'same family — pin the version record id 18622201 for file '
        'checksums'
    ),
    files=(
        external_asset_file(
            file_name='measurements.zip',
            uri=_zenodo_file('18622201', 'measurements.zip'),
            size_bytes=541020037,
            md5='659c7b7c2e3ff865ccd263a79269242c',
            role='primary_payload',
        ),
        external_asset_file(
            file_name='floor_plan.pdf',
            uri=_zenodo_file('18622201', 'floor_plan.pdf'),
            size_bytes=221282,
            md5='488b62c0cad16de2de91a26efb76dd0a',
            role='geometry_documentation',
        ),
        external_asset_file(
            file_name='pictures.zip',
            uri=_zenodo_file('18622201', 'pictures.zip'),
            size_bytes=575843330,
            md5='49b1973b330b1f55df94ff7a7d228e48',
            role='supporting_photos',
        ),
    ),
    dataset_notes=(
        '70 SRIRs across 25 spaces (Zenodo record metadata; the issue text '
        'says 71 — the published record is authoritative). Source: Genelec '
        '8030A; receiver: GRAS 50VI-1 32-channel spherical array.'
    ),
)

# ---------------------------------------------------------------------------
# Variable-acoustics room — 6DoF SRIRs
# ---------------------------------------------------------------------------

VARIABLE_ACOUSTICS_ROOM_ADMISSION = build_external_asset_admission(
    admission_id='ledger/aalto-variable-acoustics-room',
    dataset_name='aalto-variable-acoustics-room',
    dataset_title=(
        'Dataset of Spatial Room Impulse Responses in a Variable '
        'Acoustics Room for Six Degrees-of-Freedom Rendering and Analysis'
    ),
    publisher='Aalto Acoustics Lab (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.5720723',
    version_doi='10.5281/zenodo.6382405',
    version_record_id='6382405',
    record_uri='https://zenodo.org/records/6382405',
    license_id='cc-by-4.0',
    license_family='cc_by',
    license_uri='https://creativecommons.org/licenses/by/4.0/',
    license_note='CC BY 4.0; version 1.1 record id 6382405',
    files=(
        external_asset_file(
            file_name='6dof_SRIRs_eigenmike_raw.zip',
            uri=_zenodo_file('6382405', '6dof_SRIRs_eigenmike_raw.zip'),
            size_bytes=2838201534,
            md5='4ae31c4d8040034b06d51d0791c4445e',
            role='primary_payload_eigenmike_raw',
        ),
        external_asset_file(
            file_name='6dof_SRIRs_eigenmike_SH.zip',
            uri=_zenodo_file('6382405', '6dof_SRIRs_eigenmike_SH.zip'),
            size_bytes=2211211749,
            md5='8ec02a2c67494fbb8693fd4b7de8aed9',
            role='primary_payload_eigenmike_sh',
        ),
        external_asset_file(
            file_name='6dof_SRIRs_zylia_raw.zip',
            uri=_zenodo_file('6382405', '6dof_SRIRs_zylia_raw.zip'),
            size_bytes=1680935972,
            md5='74bc3faf38555fe04b1d42eb3d3ab058',
            role='primary_payload_zylia_raw',
        ),
        external_asset_file(
            file_name='6dof_SRIRs_zylia_SH.zip',
            uri=_zenodo_file('6382405', '6dof_SRIRs_zylia_SH.zip'),
            size_bytes=1415680948,
            md5='7d986770000197be730722d95427f8fe',
            role='primary_payload_zylia_sh',
        ),
        external_asset_file(
            file_name='6dof_source_and_receiver_positions.pdf',
            uri=_zenodo_file(
                '6382405', '6dof_source_and_receiver_positions.pdf'
            ),
            size_bytes=244102,
            md5='9ff8b93d000785773d81887b1ae7dd3e',
            role='geometry_documentation',
        ),
        external_asset_file(
            file_name='6dof_source_and_receiver_positions.jpg',
            uri=_zenodo_file(
                '6382405', '6dof_source_and_receiver_positions.jpg'
            ),
            size_bytes=1190764,
            md5='c5807dd89c0c866ae64caf16b22a61eb',
            role='geometry_documentation_image',
        ),
        external_asset_file(
            file_name='eigenmike_rotation_compensation.mat',
            uri=_zenodo_file(
                '6382405', 'eigenmike_rotation_compensation.mat'
            ),
            size_bytes=1691,
            md5='aae28aeb46862ee5fabe88e2503e4141',
            role='rotation_compensation_table',
        ),
        external_asset_file(
            file_name='CompensationRotationForEigenmikeMeasurements.m',
            uri=_zenodo_file(
                '6382405',
                'CompensationRotationForEigenmikeMeasurements.m',
            ),
            size_bytes=1306,
            md5='3bad7698ed1d3952f0be4bd9ea3c5a2a',
            role='rotation_compensation_script',
        ),
        external_asset_file(
            file_name=(
                '6dof_frontal_source_noEcho_100percent_absorbers_'
                'enabled.wav'
            ),
            uri=_zenodo_file(
                '6382405',
                '6dof_frontal_source_noEcho_100percent_absorbers_'
                'enabled.wav',
            ),
            size_bytes=29670,
            md5='ee5fb61249e73ba8765dc1dbf3d94088',
            role='preview_audio',
        ),
    ),
    dataset_notes=(
        'Eigenmike em32 + Zylia ZM-1 captures of one configurable room in '
        'two absorption configurations; raw and spherical-harmonic domains '
        'both published.'
    ),
)

# ---------------------------------------------------------------------------
# Coupled-room transition — primary #1029 candidate
# ---------------------------------------------------------------------------

_COUPLED_ROOM_FILES = (
    'officeToStairwell_srcStairwell_noLOS.sofa',
    'officeToStairwell_srcStairwell_LOS.sofa',
    'officeToStairwell_srcOffice_noLOS.sofa',
    'officeToStairwell_srcOffice_LOS.sofa',
    'officeToChamber_srcChamber_noLOS.sofa',
    'officeToChamber_srcChamber_LOS.sofa',
    'officeToChamber_srcOffice_noLOS.sofa',
    'officeToChamber_srcOffice_LOS.sofa',
    'officeToKitchen_srcKitchen_noLOS.sofa',
    'officeToKitchen_srcKitchen_LOS.sofa',
    'officeToKitchen_srcOffice_noLOS.sofa',
    'officeToKitchen_srcOffice_LOS.sofa',
    'roomToHallway_srcHallway_noLOS.sofa',
    'roomToHallway_srcHallway_LOS.sofa',
    'roomToHallway_srcRoom_noLOS.sofa',
    'roomToHallway_srcRoom_LOS.sofa',
)

_COUPLED_ROOM_MD5 = {
    'officeToStairwell_srcStairwell_noLOS.sofa': 'ad5a050a63640ca2fc20e66e56a7fa37',
    'officeToStairwell_srcStairwell_LOS.sofa': 'fade8aa3a9817bfd02299b3fec10ceda',
    'officeToStairwell_srcOffice_noLOS.sofa': '405ae3d242938dec6cebea4c4a95ea20',
    'officeToStairwell_srcOffice_LOS.sofa': '4fd61619ad151a704731128e036f4887',
    'officeToChamber_srcChamber_noLOS.sofa': '5d7fece82d2ac33de745d1811b43b3d1',
    'officeToChamber_srcChamber_LOS.sofa': 'bb902cc145c9d3f5b229dbaa6ab80d31',
    'officeToChamber_srcOffice_noLOS.sofa': '0030c976993d58f3b5c117be2f8ca40e',
    'officeToChamber_srcOffice_LOS.sofa': 'cfa12ae159915a422c6ab4ace5c49ce5',
    'officeToKitchen_srcKitchen_noLOS.sofa': '71066e29dba119e6008763152e01a6b0',
    'officeToKitchen_srcKitchen_LOS.sofa': '0bc49bf917286b14d3a1a803363b215e',
    'officeToKitchen_srcOffice_noLOS.sofa': 'eb9e024255a046a2f3b381dfa333cae3',
    'officeToKitchen_srcOffice_LOS.sofa': 'bbaf18a659518cb7402d51396d9a495a',
    'roomToHallway_srcHallway_noLOS.sofa': 'd1da22b00854c88334f76b1375787468',
    'roomToHallway_srcHallway_LOS.sofa': '18c2bd64cf45f373ec905ba53f7f3924',
    'roomToHallway_srcRoom_noLOS.sofa': '2f459b710858b0687a67a73ecd9b04be',
    'roomToHallway_srcRoom_LOS.sofa': '6765ec762eaf7da04e2dc428130e0184',
}

_COUPLED_ROOM_SIZE = {
    'officeToStairwell_srcStairwell_noLOS.sofa': 1158513080,
    'officeToStairwell_srcStairwell_LOS.sofa': 1152297263,
    'officeToStairwell_srcOffice_noLOS.sofa': 1158993581,
    'officeToStairwell_srcOffice_LOS.sofa': 1167294545,
    'officeToChamber_srcChamber_noLOS.sofa': 1202931882,
    'officeToChamber_srcChamber_LOS.sofa': 1166274596,
    'officeToChamber_srcOffice_noLOS.sofa': 1155687479,
    'officeToChamber_srcOffice_LOS.sofa': 1133430682,
    'officeToKitchen_srcKitchen_noLOS.sofa': 1140581729,
    'officeToKitchen_srcKitchen_LOS.sofa': 1148489585,
    'officeToKitchen_srcOffice_noLOS.sofa': 1150256437,
    'officeToKitchen_srcOffice_LOS.sofa': 1139872494,
    'roomToHallway_srcHallway_noLOS.sofa': 1180170948,
    'roomToHallway_srcHallway_LOS.sofa': 1177918314,
    'roomToHallway_srcRoom_noLOS.sofa': 1174764487,
    'roomToHallway_srcRoom_LOS.sofa': 1175889030,
}

COUPLED_ROOM_ADMISSION = build_external_asset_admission(
    admission_id='ledger/aalto-coupled-room-srir',
    dataset_name='aalto-coupled-room-srir',
    dataset_title=(
        'A dataset of measured spatial room impulse responses for the '
        'transition between coupled rooms'
    ),
    publisher='Aalto Acoustics Lab (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.4095493',
    version_doi='10.5281/zenodo.7848561',
    version_record_id='7848561',
    record_uri='https://zenodo.org/records/7848561',
    license_id='cc-by-4.0',
    license_family='cc_by',
    license_uri='https://creativecommons.org/licenses/by/4.0/',
    license_note='CC BY 4.0; version 1.4 record id 7848561',
    files=tuple(
        external_asset_file(
            file_name=name,
            uri=_zenodo_file('7848561', name),
            size_bytes=_COUPLED_ROOM_SIZE[name],
            md5=_COUPLED_ROOM_MD5[name],
            role='primary_payload_sofa',
        )
        for name in _COUPLED_ROOM_FILES
    )
    + (
        external_asset_file(
            file_name=(
                'Dataset of Room Impulse Responses for the Transition '
                'between Coupled Rooms PLOTS.zip'
            ),
            uri=_zenodo_file(
                '7848561',
                'Dataset%20of%20Room%20Impulse%20Responses%20for%20the'
                '%20Transition%20between%20Coupled%20Rooms%20PLOTS.zip',
            ),
            size_bytes=4435092,
            md5='42a86122b5174b318445b780bc38e865',
            role='publisher_plots',
        ),
    ),
    dataset_notes=(
        '4 room pairs (office↔stairwell, office↔chamber, '
        'office↔kitchen, room↔hallway) × source room × LOS/no-LOS. '
        'Primary candidate for the #1029 coupled-volume benchmark.'
    ),
)


AALTO_SRIR_ADMISSIONS: tuple[ExternalAssetAdmission, ...] = (
    OK5_ADMISSION,
    VARIABLE_ACOUSTICS_ROOM_ADMISSION,
    COUPLED_ROOM_ADMISSION,
)


# ---------------------------------------------------------------------------
# #875 normalization — observable matrix per case
# ---------------------------------------------------------------------------


class AaltoCaseKind(BaseModel):
    """Per-case observable matrix descriptor (admission-time contract)."""

    model_config = ConfigDict(frozen=True)

    case_key: str = Field(min_length=1)
    observables: tuple[str, ...]
    geometry_documentation: tuple[str, ...]
    coupled_volume_pairing: bool = False


AALTO_CASE_MATRIX: dict[str, AaltoCaseKind] = {
    'aalto-ok5-srir': AaltoCaseKind(
        case_key='aalto-ok5-srir',
        observables=(
            'decay_metric/T20',
            'decay_metric/T30',
            'decay_metric/EDT',
            'decay_metric/C50',
            'arrival_timing/first_arrival',
        ),
        geometry_documentation=('floor_plan.pdf',),
    ),
    'aalto-variable-acoustics-room': AaltoCaseKind(
        case_key='aalto-variable-acoustics-room',
        observables=(
            'decay_metric/T20',
            'decay_metric/T30',
            'decay_metric/EDT',
            'impulse_window/srir_channel',
        ),
        geometry_documentation=(
            '6dof_source_and_receiver_positions.pdf',
            '6dof_source_and_receiver_positions.jpg',
        ),
    ),
    'aalto-coupled-room-srir': AaltoCaseKind(
        case_key='aalto-coupled-room-srir',
        observables=(
            'decay_metric/T20',
            'decay_metric/T30',
            'decay_metric/EDT',
            'magnitude_fr/level_difference',
            'arrival_timing/first_arrival',
        ),
        geometry_documentation=(),
        coupled_volume_pairing=True,
    ),
}


def benchmark_case_for_admission(
    admission: ExternalAssetAdmission,
    *,
    version: str,
    title: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> BenchmarkCase:
    """Normalize one admitted Aalto dataset into a #875 BenchmarkCase.

    The case is a *skeleton*: sources/receivers are declared as symbolic
    points (positions come from the dataset's own documentation when the
    downloader supplies them); materials stay ``None`` (UNKNOWN) because
    the publishers did not release absorption data; observables cover the
    declared matrix for the dataset.
    """
    matrix = AALTO_CASE_MATRIX.get(admission.dataset_name)
    if matrix is None:
        raise ValueError(
            f'no case matrix for admission {admission.dataset_name!r}'
        )
    primary = [
        f for f in admission.files if f.role == 'primary_payload'
    ]
    asset_probe = BenchmarkSourceAsset.model_construct(
        schema_version=1,
        asset_id=f'asset/{admission.dataset_name}',
        dataset_name=admission.dataset_name,
        dataset_version=version,
        origin_uri=admission.record_uri,
        content_sha256=None,
        license_id=admission.license_id,
        admission_ref=admission.admission_id,
        evidence_class='external_measured',
        provenance=tuple(provenance) or (_provenance(admission.record_uri),),
        semantic_sha256='',
    )
    asset = BenchmarkSourceAsset(
        **asset_probe.model_dump(
            mode='python', exclude={'semantic_sha256'}
        ),
        semantic_sha256=_hash(asset_probe.semantic_payload()),
    )
    sources = (
        BenchmarkPoint(
            point_id='source/genelec-8030a',
            role='loudspeaker source (documented in dataset)',
        ),
    )
    receivers = (
        BenchmarkPoint(
            point_id='receiver/measurement-array',
            role='spherical microphone array (documented in dataset)',
        ),
    )
    observables = tuple(
        BenchmarkObservable(
            observable_id=(
                f'{admission.dataset_name}/{name.split("/")[1]}'
            ),
            kind=name.split('/')[0],
            metric_id=name.split('/')[1] if '/' in name else name,
            metric_version='dataset-published',
            reference={'declared': True},
        )
        for name in matrix.observables
    )
    case_probe = BenchmarkCase.model_construct(
        schema_version=1,
        benchmark_id=f'benchmark/{admission.dataset_name}',
        version=version,
        title=title or admission.dataset_title,
        source_asset=asset,
        coordinate_convention=None,
        geometry=None,
        sources=sources,
        receivers=receivers,
        materials=None,
        environment=None,
        sample_rate_hz=None,
        frequency_grid_hz=None,
        time_origin_s=None,
        preprocessing=None,
        limitations=(
            'Measured dataset; geometry from dataset documentation only. '
            'Materials, absolute level calibration, and environmental '
            'conditions are not published — remain UNKNOWN.'
        ),
        observables=observables,
        importer_id='htdt-aalto-srir-batch',
        importer_version='1',
        semantic_sha256='',
    )
    return BenchmarkCase(
        **case_probe.model_dump(
            mode='python', exclude={'semantic_sha256'}
        ),
        semantic_sha256=_hash(case_probe.semantic_payload()),
    )


def aalto_srir_benchmark_cases(
    *, version: str = '1'
) -> tuple[BenchmarkCase, ...]:
    return tuple(
        benchmark_case_for_admission(a, version=version)
        for a in AALTO_SRIR_ADMISSIONS
    )


__all__ = [
    'AALTO_CASE_MATRIX',
    'AALTO_SRIR_ADMISSIONS',
    'COUPLED_ROOM_ADMISSION',
    'OK5_ADMISSION',
    'VARIABLE_ACOUSTICS_ROOM_ADMISSION',
    'AaltoCaseKind',
    'aalto_srir_benchmark_cases',
    'benchmark_case_for_admission',
]
