"""FOAM 01/02 material-physics benchmark batch (#1071).

Admission + typed bridge for the TU Graz FOAM impedance-tube datasets so
the #790 material-model path (``cad_acoustic_construction`` porous/JCAL
evaluation) can be validated against published measurements.

- FOAM 01 (Zenodo 10.5281/zenodo.10551344, Apache-2.0): 3079
  impedance-tube absorption measurements, 269–2191 Hz, 1 Hz grid
  (``alphas.csv`` + one-hot ``targets.csv`` + Read_Me.pdf).
- FOAM 02 (Zenodo record 18242697, concept 10.5281/zenodo.14190550,
  CC BY 4.0): 864 impedance-tube measurements of two porous materials
  (Basotect and Pinta Plano Polar) with diameter variation, plus
  *identified* JCAL parameter sets (``JCAL_params_*.csv``).

Rules:

- measured absorption coefficients (``external_measured``) and the
  *identified* JCAL parameters (``inferred`` — fitted by the authors,
  not directly measured) are distinct evidence rows; a JCAL row never
  upgrades to ``measured``;
- JCAL parameter sets carry the material they were identified for and
  the measurement frequency domain they were derived against;
- :func:`foam_benchmark_case` emits ``BenchmarkCase`` skeletons whose
  observables reference the pinned CSV columns — rows stay
  download-on-demand; the repo never bundles the payload.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, NamedTuple

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


FOAM_BATCH_AUTHORITY_VERSION = 'foam-batch-1'


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest()


# --- admission records (Zenodo-verified 2026-09) -------------------------

FOAM01_ADMISSION: ExternalAssetAdmission = build_external_asset_admission(
    admission_id='foam-01-zenodo-10551344',
    dataset_name='foam-01',
    dataset_title='FOAM 01: Acoustic Material',
    publisher='TU Graz (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.10551343',
    version_doi='10.5281/zenodo.10551344',
    version_record_id='10551344',
    record_uri='https://doi.org/10.5281/zenodo.10551344',
    license_id='apache-2.0',
    license_family='apache_2_0',
    license_uri='https://opensource.org/licenses/Apache-2.0',
    license_note=(
        'Record declares Apache-2.0 for data and code; reuse must cite '
        'Stender et al., JASA 149(3), 2021.'
    ),
    files=(
        external_asset_file(
            file_name='alphas.csv',
            uri='https://zenodo.org/api/records/10551344/files/'
            'alphas.csv/content',
            size_bytes=47632014,
            md5='06113f3f81fdf0d74fbe14ba9f69f0cc',
            role='absorption_measurements',
        ),
        external_asset_file(
            file_name='targets.csv',
            uri='https://zenodo.org/api/records/10551344/files/'
            'targets.csv/content',
            size_bytes=163187,
            md5='2ca5288d5aa94ff5bf53a7eef317e008',
            role='measurement_parameter_vectors',
        ),
        external_asset_file(
            file_name='Read_Me.pdf',
            uri='https://zenodo.org/api/records/10551344/files/'
            'Read_Me.pdf/content',
            size_bytes=23277,
            md5='d8c9bb85863c85e55e5d26339773f394',
            role='documentation',
        ),
    ),
    dataset_notes=(
        '3079 impedance-tube absorption measurements; alphas.csv rows '
        'are alpha(f) on a 269–2191 Hz, 1 Hz grid; targets.csv holds '
        'one-hot parameter combinations per measurement.'
    ),
)

FOAM02_ADMISSION: ExternalAssetAdmission = build_external_asset_admission(
    admission_id='foam-02-zenodo-18242697',
    dataset_name='foam-02',
    dataset_title=(
        'FOAM 02: Impedance tube measurements of two porous materials '
        'with diameter variation'
    ),
    publisher='TU Graz et al. (Zenodo)',
    source_kind='zenodo_record',
    admission_state='download_on_demand_candidate',
    concept_doi='10.5281/zenodo.14190550',
    version_doi='10.5281/zenodo.18242697',
    version_record_id='18242697',
    record_uri='https://doi.org/10.5281/zenodo.18242697',
    license_id='cc-by-4.0',
    license_family='cc_by',
    license_uri='https://creativecommons.org/licenses/by/4.0/',
    license_note=(
        'Companion paper: Caiazzo et al., Acta Acustica 9 (50), 2025, '
        'doi:10.1051/aacus/2025033.'
    ),
    files=(
        external_asset_file(
            file_name='alphas.csv',
            uri='https://zenodo.org/api/records/18242697/files/'
            'alphas.csv/content',
            size_bytes=5355691,
            md5='a4870c3406ef6af5467fe5f1a1790f2b',
            role='absorption_measurements',
        ),
        external_asset_file(
            file_name='targets.csv',
            uri='https://zenodo.org/api/records/18242697/files/'
            'targets.csv/content',
            size_bytes=33699,
            md5='d19e4a9849474a0c194e9bcc9235a1df',
            role='measurement_parameter_vectors',
        ),
        external_asset_file(
            file_name='diameter.csv',
            uri='https://zenodo.org/api/records/18242697/files/'
            'diameter.csv/content',
            size_bytes=32775,
            md5='e614501066dcfa922ca3681b1580a1b3',
            role='sample_diameter_measurements',
        ),
        external_asset_file(
            file_name='JCAL_params_Pinta.csv',
            uri='https://zenodo.org/api/records/18242697/files/'
            'JCAL_params_Pinta.csv/content',
            size_bytes=776,
            md5='fa37f60ab074b9fe8870cac96f1d0663',
            role='jcal_identified_params',
        ),
        external_asset_file(
            file_name='JCAL_params_Basotect.csv',
            uri='https://zenodo.org/api/records/18242697/files/'
            'JCAL_params_Basotect.csv/content',
            size_bytes=775,
            md5='c0ece095643ed892fd11a6a6ecf2d97b',
            role='jcal_identified_params',
        ),
        external_asset_file(
            file_name='Read_Me.pdf',
            uri='https://zenodo.org/api/records/18242697/files/'
            'Read_Me.pdf/content',
            size_bytes=84514,
            md5='ad28141e898f759c2ca1954d3fb14c41',
            role='documentation',
        ),
    ),
    dataset_notes=(
        '864 impedance-tube measurements of Basotect and Pinta Plano '
        'Polar with calliper diameter records; JCAL_params_*.csv carry '
        'Johnson–Champoux–Allard–Lafarge parameters identified by the '
        'authors (SI units per column header).'
    ),
)

FOAM_ADMISSIONS: tuple[ExternalAssetAdmission, ...] = (
    FOAM01_ADMISSION,
    FOAM02_ADMISSION,
)


# --- typed JCAL parameter evidence --------------------------------------

JcalParameterName = Literal[
    'airflow_resistivity',
    'porosity',
    'tortuosity',
    'viscous_characteristic_length',
    'thermal_characteristic_length',
]
"""The five JCAL parameters, identified (not directly measured)."""

JcalEvidenceBasis = Literal['identified', 'user_supplied']
"""How the parameter set was produced. FOAM 02 values are 'identified'
by the dataset authors via fitting — they remain inference evidence."""


class JcalParameterSet(BaseModel):
    """One identified JCAL parameter set for one porous material.

    All SI units: airflow resistivity Pa·s/m², lengths m, porosity and
    tortuosity dimensionless. ``None`` where the publication does not
    identify a parameter — never filled with a guess.
    """

    model_config = ConfigDict(frozen=True)

    material_id: str = Field(min_length=1)
    admission_id: str = Field(min_length=1)
    basis: JcalEvidenceBasis = 'identified'
    airflow_resistivity_pa_s_m2: float | None = Field(default=None, gt=0)
    porosity: float | None = Field(default=None, gt=0.0, le=1.0)
    tortuosity: float | None = Field(default=None, ge=1.0)
    viscous_characteristic_length_m: float | None = Field(
        default=None, gt=0
    )
    thermal_characteristic_length_m: float | None = Field(
        default=None, gt=0
    )
    derived_frequency_domain_hz: tuple[float, float] | None = None
    """(min, max) of the measurement grid the set was identified
    against — the valid domain for model comparison."""
    source_file: str = Field(min_length=1)
    notes: str = ''
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _check(self) -> 'JcalParameterSet':
        if self.derived_frequency_domain_hz is not None:
            lo, hi = self.derived_frequency_domain_hz
            if not (0.0 < lo < hi):
                raise ValueError(
                    'frequency domain must be increasing positive'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('jcal parameter set semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )


def build_jcal_parameter_set(
    *,
    material_id: str,
    admission_id: str,
    source_file: str,
    basis: JcalEvidenceBasis = 'identified',
    airflow_resistivity_pa_s_m2: float | None = None,
    porosity: float | None = None,
    tortuosity: float | None = None,
    viscous_characteristic_length_m: float | None = None,
    thermal_characteristic_length_m: float | None = None,
    derived_frequency_domain_hz: tuple[float, float] | None = None,
    notes: str = '',
) -> JcalParameterSet:
    probe = JcalParameterSet.model_construct(
        material_id=material_id,
        admission_id=admission_id,
        basis=basis,
        airflow_resistivity_pa_s_m2=airflow_resistivity_pa_s_m2,
        porosity=porosity,
        tortuosity=tortuosity,
        viscous_characteristic_length_m=(
            viscous_characteristic_length_m
        ),
        thermal_characteristic_length_m=(
            thermal_characteristic_length_m
        ),
        derived_frequency_domain_hz=derived_frequency_domain_hz,
        source_file=source_file,
        notes=notes,
        semantic_sha256='',
    )
    return JcalParameterSet(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def parse_jcal_params_csv(text: str, *, admission_id: str) -> (
    tuple[JcalParameterSet, ...]
):
    """Parse a FOAM-02 ``JCAL_params_*.csv`` file into parameter sets.

    The published CSV is a two-column ``parameter,value`` table in SI
    units (see FOAM 02 Read_Me). Parsing is exact — an unknown parameter
    name or a missing value raises ``ValueError`` rather than guessing.
    """
    import csv
    import io

    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    header = [c.strip().lower() for c in rows[0]]
    if len(header) < 2 or 'parameter' not in header[0]:
        raise ValueError(
            'JCAL params CSV must start with a parameter,value header'
        )
    material_id: str | None = None
    values: dict[str, float] = {}
    name_map = {
        'airflow_resistivity': 'airflow_resistivity_pa_s_m2',
        'sigma': 'airflow_resistivity_pa_s_m2',
        'porosity': 'porosity',
        'phi': 'porosity',
        'tortuosity': 'tortuosity',
        'alpha_inf': 'tortuosity',
        'viscous_characteristic_length': (
            'viscous_characteristic_length_m'
        ),
        'lambda': 'viscous_characteristic_length_m',
        'thermal_characteristic_length': (
            'thermal_characteristic_length_m'
        ),
        "lambda'": 'thermal_characteristic_length_m',
        'lambda_prime': 'thermal_characteristic_length_m',
    }
    for row in rows[1:]:
        key = row[0].strip()
        low = key.lower()
        if low in ('material', 'material_id', 'sample'):
            material_id = row[1].strip()
            continue
        field = name_map.get(low)
        if field is None:
            raise ValueError(
                f'unrecognized JCAL parameter name: {key!r}'
            )
        values[field] = float(row[1])
    if material_id is None:
        raise ValueError('JCAL params CSV must name its material')
    return (
        build_jcal_parameter_set(
            material_id=material_id,
            admission_id=admission_id,
            source_file='parsed_csv',
            **values,
        ),
    )


# --- #790 benchmark bridge ----------------------------------------------

FoamMeasurementKind = Literal[
    'absorption_coefficient',
    'jcal_parameters',
    'diameter_variation',
]
"""Observable families carried by the FOAM batch."""


class FoamCaseDescriptor(NamedTuple):
    case_id: str
    admission_id: str
    measurement_kind: FoamMeasurementKind
    material_id: str | None
    frequency_domain_hz: tuple[float, float]
    measurement_rows: int
    detail: str


FOAM_CASE_MATRIX: tuple[FoamCaseDescriptor, ...] = (
    FoamCaseDescriptor(
        case_id='foam01-absorption-grid',
        admission_id=FOAM01_ADMISSION.admission_id,
        measurement_kind='absorption_coefficient',
        material_id=None,
        frequency_domain_hz=(269.0, 2191.0),
        measurement_rows=3079,
        detail=(
            'alphas.csv — 3079 impedance-tube absorption measurements '
            'on a 1 Hz grid; targets.csv pins each row\'s parameter '
            'combination.'
        ),
    ),
    FoamCaseDescriptor(
        case_id='foam02-absorption-diameter',
        admission_id=FOAM02_ADMISSION.admission_id,
        measurement_kind='absorption_coefficient',
        material_id=None,
        frequency_domain_hz=(0.0, 0.0),
        measurement_rows=864,
        detail=(
            'alphas.csv + diameter.csv — 864 measurements across two '
            'porous materials with documented diameter variation.'
        ),
    ),
    FoamCaseDescriptor(
        case_id='foam02-jcal-basotect',
        admission_id=FOAM02_ADMISSION.admission_id,
        measurement_kind='jcal_parameters',
        material_id='basotect',
        frequency_domain_hz=(0.0, 0.0),
        measurement_rows=1,
        detail=(
            'JCAL_params_Basotect.csv — author-identified JCAL '
            'parameter set for Basotect melamine foam (inferred '
            'evidence, not measured alpha).'
        ),
    ),
    FoamCaseDescriptor(
        case_id='foam02-jcal-pinta',
        admission_id=FOAM02_ADMISSION.admission_id,
        measurement_kind='jcal_parameters',
        material_id='pinta_plano_polar',
        frequency_domain_hz=(0.0, 0.0),
        measurement_rows=1,
        detail=(
            'JCAL_params_Pinta.csv — author-identified JCAL parameter '
            'set for Pinta Plano Polar.'
        ),
    ),
)
"""Curated case descriptors — the batch's observable matrix."""


def _foam_source_asset(
    admission: ExternalAssetAdmission, descriptor: FoamCaseDescriptor
) -> BenchmarkSourceAsset:
    file_role = {
        'absorption_coefficient': 'absorption_measurements',
        'jcal_parameters': 'jcal_identified_params',
        'diameter_variation': 'sample_diameter_measurements',
    }[descriptor.measurement_kind]
    sha = _hash(
        {
            'admission_id': admission.admission_id,
            'case_id': descriptor.case_id,
        }
    )
    probe = BenchmarkSourceAsset.model_construct(
        asset_id=descriptor.case_id,
        dataset_name=admission.dataset_name,
        dataset_version=(
            admission.version_record_id or admission.version_doi or ''
        ),
        origin_uri=admission.record_uri,
        content_sha256=None,
        license_id=admission.license_id,
        admission_ref=admission.admission_id,
        evidence_class='external_measured',
        provenance=(
            EquipmentDataProvenance(
                evidence_kind=(
                    'measured'
                    if descriptor.measurement_kind
                    == 'absorption_coefficient'
                    else 'inferred'
                ),
                source_name=f'zenodo:{admission.dataset_name}',
                source_version=(
                    admission.version_record_id or 'unversioned'
                ),
                source_reference=file_role,
                source_sha256=sha,
            ),
        ),
        semantic_sha256='',
    )
    return BenchmarkSourceAsset(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def foam_benchmark_case(descriptor: FoamCaseDescriptor) -> BenchmarkCase:
    """Emit the immutable ``BenchmarkCase`` skeleton for one descriptor."""
    admission = next(
        a
        for a in FOAM_ADMISSIONS
        if a.admission_id == descriptor.admission_id
    )
    source = _foam_source_asset(admission, descriptor)
    domain = descriptor.frequency_domain_hz
    observables = (
        BenchmarkObservable(
            observable_id=f'{descriptor.case_id}-alpha',
            kind='magnitude_fr',
            valid_band_hz=None if domain == (0.0, 0.0) else domain,
            preprocessing='impedance_tube_alpha',
            metric_id='normal_incidence_absorption',
            metric_version='foam-1',
            tolerance=None,
            tolerance_unit=None,
            reference={
                'source_file': 'alphas.csv',
                'column_role': 'absorption_coefficient',
                'measurement_rows': descriptor.measurement_rows,
            },
        ),
    )
    probe = BenchmarkCase.model_construct(
        benchmark_id=descriptor.case_id,
        version=FOAM_BATCH_AUTHORITY_VERSION,
        title=descriptor.detail,
        source_asset=source,
        coordinate_convention='impedance_tube_normal_incidence',
        geometry=None,
        sources=(
            BenchmarkPoint(point_id='sample', role='porous_sample'),
        ),
        receivers=(),
        materials=(
            {'material_id': descriptor.material_id}
            if descriptor.material_id
            else None
        ),
        environment=None,
        sample_rate_hz=None,
        frequency_grid_hz=None,
        limitations=(
            'Download-on-demand: payload rows are not bundled; the '
            'admission record pins file names, sizes and checksums. '
            'JCAL parameter sets are author-identified inference — '
            'they are not measured absorption.'
        ),
        observables=observables,
        importer_id='cad_foam_material_batch',
        importer_version=FOAM_BATCH_AUTHORITY_VERSION,
        semantic_sha256='',
    )
    return BenchmarkCase(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def foam_benchmark_cases() -> tuple[BenchmarkCase, ...]:
    return tuple(foam_benchmark_case(d) for d in FOAM_CASE_MATRIX)


__all__ = [
    'FOAM01_ADMISSION',
    'FOAM02_ADMISSION',
    'FOAM_ADMISSIONS',
    'FOAM_BATCH_AUTHORITY_VERSION',
    'FOAM_CASE_MATRIX',
    'FoamCaseDescriptor',
    'FoamMeasurementKind',
    'JcalEvidenceBasis',
    'JcalParameterName',
    'JcalParameterSet',
    'build_jcal_parameter_set',
    'foam_benchmark_case',
    'foam_benchmark_cases',
    'parse_jcal_params_csv',
]
