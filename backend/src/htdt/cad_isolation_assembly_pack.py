"""Tested sound-isolation assembly reference pack (#1077).

Curated sound-transmission-loss assemblies with *exact* construction
and test provenance, for comparison against ``cad_sound_isolation``
predictions. Every entry pins the publishing laboratory report or
manufacturer-compiled assembly table — a field-assembled wall is never
claimed to reproduce the lab rating.

Rules:

- ``construction`` is an ordered, verbatim layer list from the source —
  no paraphrased "equivalent" builds;
- STC/OITC single-number ratings and the third-octave TL table are kept
  together; a TL band published in the report is never dropped or
  filled in;
- ``provenance_class`` separates ``lab_measured`` (a named lab report
  such as WEAL TL21-231) from ``manufacturer_compiled`` (vendor/STC
  tables that cite underlying lab reports);
- lab STC ≠ field performance: consumers get the exact evidence, and
  ``field_rating`` stays ``None`` unless a field standard (ASTM E336)
  measurement is separately recorded — the pack never infers one.
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


ISOLATION_PACK_AUTHORITY_VERSION = 'isolation-assembly-1'






AssemblyProvenanceClass = Literal[
    'lab_measured',
    'manufacturer_compiled',
]
"""lab_measured = a named accredited lab test report;
manufacturer_compiled = a vendor/industry table that cites lab
reports (STC summary rows without the full TL sheet)."""

TestStandard = Literal[
    'astm_e90',
    'astm_e336_field',
    'iso_10140',
    'other',
]

STC_BANDS_HZ: tuple[int, ...] = (
    125, 160, 200, 250, 315, 400,
    500, 630, 800, 1000, 1250, 1600,
    2000, 2500, 3150, 4000,
)
"""The 16 ASTM E413 STC contour third-octave bands."""

_EXTENDED_TL_BANDS_HZ: frozenset[int] = frozenset(
    (
        50, 63, 80, 100,
        *STC_BANDS_HZ,
        5000, 6300, 8000,
    )
)
"""Third-octave centers a lab report may publish; reports frequently
carry bands below 125 Hz (contour range starts at 125)."""


class IsolationAssembly(BaseModel):
    """One curated, provenance-pinned sound-isolation assembly."""

    model_config = ConfigDict(frozen=True)

    assembly_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    provenance_class: AssemblyProvenanceClass
    test_standard: TestStandard
    source_title: str = Field(min_length=1)
    """Exact report/table identifier, e.g. 'WEAL TL21-231'."""
    source_uri: str = Field(min_length=1)
    laboratory: str | None = None
    test_date: str | None = None
    construction: tuple[str, ...] = Field(min_length=1)
    """Ordered layer list, verbatim from the source document."""
    declared_stc: int | None = Field(default=None, ge=0, le=100)
    declared_oitc: int | None = Field(default=None, ge=0, le=100)
    tl_db: dict[int, float] | None = None
    """Third-octave transmission loss keyed by band center Hz. Only
    bands published in the source may appear."""
    field_rating: int | None = Field(default=None, ge=0, le=100)
    """Field STC/NIC only when a *separate* field measurement exists —
    never derived from the lab rating."""
    specimen_area_m2: float | None = Field(default=None, gt=0.0)
    notes: str = ''
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _check(self) -> 'IsolationAssembly':
        if self.tl_db is not None:
            for band in self.tl_db:
                if band not in _EXTENDED_TL_BANDS_HZ:
                    raise ValueError(
                        f'tl_db band {band} is not a standard '
                        'third-octave center'
                    )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'isolation assembly semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )


def build_isolation_assembly(
    *,
    assembly_id: str,
    title: str,
    provenance_class: AssemblyProvenanceClass,
    test_standard: TestStandard,
    source_title: str,
    source_uri: str,
    construction: tuple[str, ...],
    laboratory: str | None = None,
    test_date: str | None = None,
    declared_stc: int | None = None,
    declared_oitc: int | None = None,
    tl_db: dict[int, float] | None = None,
    field_rating: int | None = None,
    specimen_area_m2: float | None = None,
    notes: str = '',
) -> IsolationAssembly:
    probe = IsolationAssembly.model_construct(
        assembly_id=assembly_id,
        title=title,
        provenance_class=provenance_class,
        test_standard=test_standard,
        source_title=source_title,
        source_uri=source_uri,
        laboratory=laboratory,
        test_date=test_date,
        construction=tuple(construction),
        declared_stc=declared_stc,
        declared_oitc=declared_oitc,
        tl_db=tl_db,
        field_rating=field_rating,
        specimen_area_m2=specimen_area_m2,
        notes=notes,
        semantic_sha256='',
    )
    return IsolationAssembly(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# --- curated pack entries -------------------------------------------------

# WEAL report TL21-231 (Western Electro-Acoustic Laboratory, client
# CEMCO, tested 2021-04-14, ASTM E90-09(2016)+E2235): single 2x6 wood
# stud wall, 2x 5/8" type X gypsum each side (one face on CEMCO RC1-XD
# resilient channel), R-19 fiberglass cavity. STC 62, OITC 49, EWR 63.
# TL table transcribed from the report sheet.
WEAL_TL21_231_ASSEMBLY: IsolationAssembly = build_isolation_assembly(
    assembly_id='weal-tl21-231',
    title=(
        'CEMCO 2x6 wood stud, double 5/8" Type X both sides, '
        'RC1-XD resilient channel, R-19'
    ),
    provenance_class='lab_measured',
    test_standard='astm_e90',
    source_title='WEAL Sound Transmission Loss Test Report TL21-231',
    source_uri=(
        'https://cemcosteel.com/app/uploads/2021/05/'
        'WEAL_TL21-231_2x6_double-layer_STC_62.pdf'
    ),
    laboratory='Western Electro-Acoustic Laboratory',
    test_date='2021-04-14',
    construction=(
        '2x 16 mm (5/8") type X gypsum board (source side)',
        '140 mm 2x6 wood studs @406 mm o.c. with R-19 fiberglass batt',
        '13 mm CEMCO RC1-XD resilient channel @610 mm o.c.',
        '2x 16 mm (5/8") type X gypsum board (receive side)',
    ),
    declared_stc=62,
    declared_oitc=49,
    tl_db={
        125: 46.0,
        160: 48.0,
        200: 50.0,
        250: 54.0,
        315: 57.0,
        400: 59.0,
        500: 63.0,
        630: 65.0,
        800: 66.0,
        1000: 67.0,
        1250: 68.0,
        1600: 66.0,
        2000: 59.0,
        2500: 58.0,
        3150: 62.0,
        4000: 67.0,
        5000: 70.0,
    },
    specimen_area_m2=5.95,
    notes=(
        'Specimen 2.44x2.44 m, surface density 51.6 kg/m2; ASTM minimum '
        'volume met at 80 Hz and above. Report sheet also lists 63, 80, '
        '100 Hz rows (below the E413 contour range).'
    ),
)

# WWCCA STC Assemblies Data Base (public compilation of vendor lab
# reports; entries cite the underlying report IDs). These rows carry
# no TL tables — manufacturer_compiled evidence.
_WWCCA_URI = (
    'https://wwcca.org/wp-content/uploads/2025/06/'
    '01_STC-Assemblies-Data-Base_06_03_25.pdf'
)

WWCCA_CD_TL20_413: IsolationAssembly = build_isolation_assembly(
    assembly_id='wwcca-cd-tl20-413',
    title=(
        '3-5/8" 30 mil metal studs @16" o.c., single 5/8" Type X '
        'gypsum each side, cavity batt insulation'
    ),
    provenance_class='manufacturer_compiled',
    test_standard='astm_e90',
    source_title='WWCCA STC Assemblies Data Base, report CD-TL20-413',
    source_uri=_WWCCA_URI,
    laboratory=None,
    test_date=None,
    construction=(
        '16 mm (5/8") Type X gypsum board',
        '30 mil x 3-5/8" metal studs @16" o.c. with batt insulation',
        '16 mm (5/8") Type X gypsum board',
    ),
    declared_stc=38,
    tl_db=None,
    notes='STC summary row; underlying proprietary lab report cited.',
)

WWCCA_PAB_NOAL_18_0902: IsolationAssembly = build_isolation_assembly(
    assembly_id='wwcca-pab-noal-18-0902',
    title=(
        '3-5/8" 33 mil metal studs @16" o.c., single 5/8" '
        'QuietRock 530 each side, cavity batt insulation'
    ),
    provenance_class='manufacturer_compiled',
    test_standard='astm_e90',
    source_title='WWCCA STC Assemblies Data Base, report PAB-NOAL '
    '18-0902',
    source_uri=_WWCCA_URI,
    construction=(
        '16 mm (5/8") QuietRock 530',
        '33 mil x 3-5/8" metal studs @16" o.c. with batt insulation',
        '16 mm (5/8") QuietRock 530',
    ),
    declared_stc=54,
    tl_db=None,
    notes='STC summary row; underlying proprietary lab report cited.',
)

ISOLATION_ASSEMBLY_PACK: tuple[IsolationAssembly, ...] = (
    WEAL_TL21_231_ASSEMBLY,
    WWCCA_CD_TL20_413,
    WWCCA_PAB_NOAL_18_0902,
)


class AssemblyLookup(NamedTuple):
    found: bool
    assembly: IsolationAssembly | None
    detail: str


def lookup_assembly(assembly_id: str) -> AssemblyLookup:
    for a in ISOLATION_ASSEMBLY_PACK:
        if a.assembly_id == assembly_id:
            return AssemblyLookup(True, a, 'found')
    return AssemblyLookup(False, None, 'assembly not in pack')


def tl_at_band(
    assembly: IsolationAssembly, band_hz: int
) -> float | None:
    """Exact-band TL lookup — ``None`` when the report didn't publish
    that band; no interpolation between bands."""
    if assembly.tl_db is None:
        return None
    return assembly.tl_db.get(band_hz)


__all__ = [
    'AssemblyLookup',
    'AssemblyProvenanceClass',
    'ISOLATION_ASSEMBLY_PACK',
    'ISOLATION_PACK_AUTHORITY_VERSION',
    'IsolationAssembly',
    'STC_BANDS_HZ',
    'TestStandard',
    'WEAL_TL21_231_ASSEMBLY',
    'WWCCA_CD_TL20_413',
    'WWCCA_PAB_NOAL_18_0902',
    'build_isolation_assembly',
    'lookup_assembly',
    'tl_at_band',
]
