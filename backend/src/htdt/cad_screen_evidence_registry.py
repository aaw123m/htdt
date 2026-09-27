"""#1062 — projection-screen reference data registry.

Curates published optical, acoustic, and moiré-structure evidence for
acoustically-transparent (AT) screen materials without turning manufacturer
claims into measured truth. Families are kept strictly separate — an optical
record never upgrades an acoustic claim, and vice versa — so #541
(acoustic transfer), #631 (moiré/geometry) and #1017 (microstructure
authority) can each bind only the records written in their own family.

Hard rules carried by the data model:

- ``evidence_class`` records *who asserts* the value and *how it was
  obtained*; a manufacturer spec sheet can never carry
  ``independent_measured``;
- spacing/angle/backing/test-geometry statements are retained verbatim —
  including prose-only guidance (e.g. "place the projector at least X feet
  away"), which stays prose and is never digitised into fabricated curves;
- partial knowledge is normal: a record may assert a single parameter;
- records carry no quality score — a manufacturer declaration is not
  promoted or demoted, only classified.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


SCREEN_EVIDENCE_SCHEMA_VERSION = 1
SCREEN_EVIDENCE_AUTHORITY_VERSION = 'screen-evidence-1'

ScreenEvidenceFamily = Literal['optical', 'acoustic', 'moire_structure']

ScreenEvidenceClass = Literal[
    'manufacturer_declared',
    'manufacturer_measured',
    'manufacturer_commissioned_third_party',
    'independent_measured',
    'user_measured',
    'derived',
    'unknown',
]

ScreenEvidenceSubject = Literal[
    # optical family
    'peak_gain',
    'half_gain',
    'viewing_cone_deg',
    'minimum_throw_distance',
    'maximum_size',
    'ambient_light_rejection_pct',
    'resolution_capability',
    'color_shift',
    # acoustic family
    'acoustic_transparency',
    'frequency_range_with_reduced_impact',
    'speaker_placement_distance',
    'attenuation_db',
    # moire_structure family
    'microstructure_type',
    'perforation_density',
    'open_area_fraction_pct',
    'weave_pattern',
    'seating_distance_min_ft',
    'moire_resistance',
]






class ScreenEvidenceSource(BaseModel):
    """One source document for screen evidence."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    document_kind: Literal[
        'manufacturer_specification_sheet',
        'manufacturer_product_page',
        'manufacturer_press_release',
        'manufacturer_guidance',
        'independent_review',
        'user_measurement',
    ]
    uri: str = Field(min_length=1)
    retrieved_utc: str = Field(min_length=1)
    document_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ScreenEvidenceSource':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('screen evidence source semantic hash mismatch')
        return self


class ScreenEvidenceRecord(BaseModel):
    """One assertion about one screen material, in exactly one family."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCREEN_EVIDENCE_SCHEMA_VERSION
    authority_version: Literal['screen-evidence-1'] = (
        SCREEN_EVIDENCE_AUTHORITY_VERSION
    )
    record_id: str = Field(min_length=1)
    family: ScreenEvidenceFamily
    subject: ScreenEvidenceSubject
    manufacturer: str = Field(min_length=1)
    screen_model: str = Field(min_length=1)
    evidence_class: ScreenEvidenceClass
    value_json: str = Field(min_length=1)
    unit: str | None = None
    source_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ScreenEvidenceRecord':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('screen evidence record semantic hash mismatch')
        return self

    def parsed_value(self) -> Any:
        return json.loads(self.value_json)


class ScreenEvidenceRegistry(BaseModel):
    """The curated screen-evidence store — a flat registry of records."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SCREEN_EVIDENCE_SCHEMA_VERSION
    authority_version: Literal['screen-evidence-1'] = (
        SCREEN_EVIDENCE_AUTHORITY_VERSION
    )
    registry_id: str = Field(min_length=1)
    records: tuple[ScreenEvidenceRecord, ...]
    sources: tuple[ScreenEvidenceSource, ...]
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ScreenEvidenceRegistry':
        source_ids = {s.source_id for s in self.sources}
        record_ids: set[str] = set()
        for record in self.records:
            if record.record_id in record_ids:
                raise ValueError(
                    f'duplicate record_id {record.record_id!r}'
                )
            record_ids.add(record.record_id)
            if record.source_id not in source_ids:
                raise ValueError(
                    f'record {record.record_id!r} references unknown '
                    f'source_id {record.source_id!r}'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'screen evidence registry semantic hash mismatch'
            )
        return self


def _source(
    *,
    source_id: str,
    publisher: str,
    document_title: str,
    document_kind: Literal[
        'manufacturer_specification_sheet',
        'manufacturer_product_page',
        'manufacturer_press_release',
        'manufacturer_guidance',
        'independent_review',
        'user_measurement',
    ],
    uri: str,
    retrieved_utc: str,
    document_sha256: str | None = None,
) -> ScreenEvidenceSource:
    probe = ScreenEvidenceSource.model_construct(
        source_id=source_id,
        publisher=publisher,
        document_title=document_title,
        document_kind=document_kind,
        uri=uri,
        retrieved_utc=retrieved_utc,
        document_sha256=document_sha256,
        semantic_sha256='',
    )
    return ScreenEvidenceSource(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def _record(
    *,
    record_id: str,
    family: ScreenEvidenceFamily,
    subject: ScreenEvidenceSubject,
    manufacturer: str,
    screen_model: str,
    evidence_class: ScreenEvidenceClass,
    value: Any,
    source_id: str,
    locator: str,
    unit: str | None = None,
    note: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> ScreenEvidenceRecord:
    probe = ScreenEvidenceRecord.model_construct(
        schema_version=SCREEN_EVIDENCE_SCHEMA_VERSION,
        authority_version=SCREEN_EVIDENCE_AUTHORITY_VERSION,
        record_id=record_id,
        family=family,
        subject=subject,
        manufacturer=manufacturer,
        screen_model=screen_model,
        evidence_class=evidence_class,
        value_json=_canonical(value),
        unit=unit,
        source_id=source_id,
        locator=locator,
        note=note,
        provenance=tuple(provenance),
        semantic_sha256='',
    )
    return ScreenEvidenceRecord(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def build_screen_evidence_registry(
    *,
    registry_id: str,
    records: tuple[ScreenEvidenceRecord, ...],
    sources: tuple[ScreenEvidenceSource, ...],
) -> ScreenEvidenceRegistry:
    probe = ScreenEvidenceRegistry.model_construct(
        schema_version=SCREEN_EVIDENCE_SCHEMA_VERSION,
        authority_version=SCREEN_EVIDENCE_AUTHORITY_VERSION,
        registry_id=registry_id,
        records=tuple(records),
        sources=tuple(sources),
        semantic_sha256='',
    )
    return ScreenEvidenceRegistry(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def registry_records(
    registry: ScreenEvidenceRegistry,
    *,
    family: ScreenEvidenceFamily | None = None,
    screen_model: str | None = None,
    subject: ScreenEvidenceSubject | None = None,
) -> tuple[ScreenEvidenceRecord, ...]:
    """Filtered read — callers restrict to exactly the family they consume."""
    return tuple(
        record
        for record in registry.records
        if (family is None or record.family == family)
        and (screen_model is None or record.screen_model == screen_model)
        and (subject is None or record.subject == subject)
    )


def registry_family_coverage(
    registry: ScreenEvidenceRegistry, screen_model: str
) -> dict[ScreenEvidenceFamily, tuple[str, ...]]:
    """Which subjects are known for a model, per family. Read-only summary."""
    out: dict[ScreenEvidenceFamily, list[str]] = {
        'optical': [],
        'acoustic': [],
        'moire_structure': [],
    }
    for record in registry.records:
        if record.screen_model == screen_model:
            out[record.family].append(record.subject)
    return {
        family: tuple(sorted(set(subjects)))
        for family, subjects in out.items()
    }


# ---------------------------------------------------------------------------
# Curated first registry entries — three materials named in #1062, with only
# what their primary sources actually state.
# ---------------------------------------------------------------------------

_HARMONY_SPEC = _source(
    source_id='stewart-harmony-g3-spec-sheet-2025',
    publisher='Stewart Filmscreen',
    document_title='Harmony G3 — Material Data Sheet (Brochure 2025)',
    document_kind='manufacturer_specification_sheet',
    uri='https://www.stewartfilmscreen.com/Files/files/Support%20Material/'
    'Material%20Data%20Sheets/HarmonyG3_SpecSheet_Brochure2025.pdf',
    retrieved_utc='2026-09-25',
)

_HARMONY_PRESS = _source(
    source_id='stewart-harmony-g3-press-release',
    publisher='Stewart Filmscreen',
    document_title=(
        'Stewart Filmscreen Introduces Harmony G3 Woven Screen Material'
    ),
    document_kind='manufacturer_press_release',
    uri='https://www.stewartfilmscreen.com/en/news/'
    'stewart-filmscreen-introduces-harmony-g3-woven-screen-material-'
    'a-breakthrough-in-acoustic-transparency-and-visual-excellence',
    retrieved_utc='2026-09-25',
)

_SEYMOUR_XD_PAGE = _source(
    source_id='seymour-center-stage-xd-product-page',
    publisher='Seymour AV',
    document_title='Center Stage screens (Center Stage XD)',
    document_kind='manufacturer_product_page',
    uri='https://www.seymourav.com/screens.php',
    retrieved_utc='2026-09-25',
)

_SEYMOUR_XD_DIY = _source(
    source_id='seymour-center-stage-xd-diy-page',
    publisher='Seymour AV',
    document_title='Center Stage XD — DIY screen material',
    document_kind='manufacturer_product_page',
    uri='https://seymourav.com/screensDIY.php',
    retrieved_utc='2026-09-25',
)

_MICROPERF_SV = _source(
    source_id='soundandvision-microperf-x2-review',
    publisher='Sound & Vision',
    document_title=(
        'Private Screening (measured MicroPerf X2 acoustic data)'
    ),
    document_kind='independent_review',
    uri='https://www.soundandvision.com/content/private-screening-page-4',
    retrieved_utc='2026-09-25',
)


PROJECTION_SCREEN_EVIDENCE_REGISTRY = build_screen_evidence_registry(
    registry_id='projection-screen-evidence-1',
    sources=(
        _HARMONY_SPEC,
        _HARMONY_PRESS,
        _SEYMOUR_XD_PAGE,
        _SEYMOUR_XD_DIY,
        _MICROPERF_SV,
    ),
    records=(
        # --- Stewart Harmony G3: optical (manufacturer declared) ---
        _record(
            record_id='harmony-g3/optical/peak-gain',
            family='optical',
            subject='peak_gain',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value=0.7,
            unit='ratio',
            source_id='stewart-harmony-g3-spec-sheet-2025',
            locator='Material Data Sheet > Peak Gain',
        ),
        _record(
            record_id='harmony-g3/optical/half-gain',
            family='optical',
            subject='half_gain',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value='Lambertian',
            source_id='stewart-harmony-g3-spec-sheet-2025',
            locator='Material Data Sheet > Half Gain',
        ),
        _record(
            record_id='harmony-g3/optical/viewing-cone',
            family='optical',
            subject='viewing_cone_deg',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value=85,
            unit='deg',
            source_id='stewart-harmony-g3-spec-sheet-2025',
            locator='Material Data Sheet > Viewing Cone',
        ),
        _record(
            record_id='harmony-g3/optical/min-throw',
            family='optical',
            subject='minimum_throw_distance',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value='0.3 x image width',
            source_id='stewart-harmony-g3-spec-sheet-2025',
            locator='Material Data Sheet > Minimum Throw Distance',
            note='relative guidance, not a distance curve',
        ),
        _record(
            record_id='harmony-g3/optical/max-size',
            family='optical',
            subject='maximum_size',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value={'height_ft': 15, 'width_ft': 90},
            source_id='stewart-harmony-g3-spec-sheet-2025',
            locator='Material Data Sheet > Maximum Size',
        ),
        _record(
            record_id='harmony-g3/optical/alr',
            family='optical',
            subject='ambient_light_rejection_pct',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value=15,
            unit='%',
            source_id='stewart-harmony-g3-spec-sheet-2025',
            locator='Material Data Sheet > Ambient Light Rejection Value',
        ),
        # --- Stewart Harmony G3: acoustic (manufacturer declared) ---
        _record(
            record_id='harmony-g3/acoustic/transparency',
            family='acoustic',
            subject='acoustic_transparency',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value='woven AT material',
            source_id='stewart-harmony-g3-spec-sheet-2025',
            locator='Material Data Sheet > Perforation Options',
            note='"Is Acoustically Transparent" — declared, not measured',
        ),
        _record(
            record_id='harmony-g3/acoustic/range',
            family='acoustic',
            subject='frequency_range_with_reduced_impact',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value='15 kHz',
            source_id='stewart-harmony-g3-press-release',
            locator='press release paragraph 2',
            note=(
                '"lessens the impact on the audio signal to 15 kHz" — '
                'manufacturer claim citing third-party tests; not an '
                'independent measurement we can name'
            ),
        ),
        _record(
            record_id='harmony-g3/acoustic/speaker-distance',
            family='acoustic',
            subject='speaker_placement_distance',
            manufacturer='Stewart Filmscreen',
            screen_model='Harmony G3',
            evidence_class='manufacturer_declared',
            value='as close as 1 inch behind the screen',
            source_id='stewart-harmony-g3-spec-sheet-2025',
            locator='Material Data Sheet prose',
            note='prose-only guidance; kept verbatim, not converted',
        ),
        # --- Stewart MicroPerf X2: independent acoustic measurement ---
        _record(
            record_id='microperf-x2/acoustic/attenuation-on-axis',
            family='acoustic',
            subject='attenuation_db',
            manufacturer='Stewart Filmscreen',
            screen_model='MicroPerf X2 THX Ultra',
            evidence_class='independent_measured',
            value='+4.1 / -12.1 dB on-axis',
            source_id='soundandvision-microperf-x2-review',
            locator='Sound & Vision 2009 review, page 4',
            note=(
                'independent measurement; +11.5/-5.8 dB off-axis when the '
                'Stewart Cinema Sonic Processor compensation was used'
            ),
        ),
        _record(
            record_id='microperf-x2/moire/type',
            family='moire_structure',
            subject='microstructure_type',
            manufacturer='Stewart Filmscreen',
            screen_model='MicroPerf X2 THX Ultra',
            evidence_class='manufacturer_declared',
            value='perforated vinyl (micro-perforation)',
            source_id='soundandvision-microperf-x2-review',
            locator='Sound & Vision 2009 review, page 4',
            note=(
                'perforated (not woven); hole count/diameter not published '
                'in the cited source'
            ),
        ),
        # --- Seymour Center Stage XD: optical + acoustic + moiré ---
        _record(
            record_id='center-stage-xd/optical/peak-gain',
            family='optical',
            subject='peak_gain',
            manufacturer='Seymour AV',
            screen_model='Center Stage XD',
            evidence_class='manufacturer_declared',
            value=1.2,
            unit='ratio',
            source_id='seymour-center-stage-xd-diy-page',
            locator='Center Stage XD specification table',
            note=(
                '"benchmarked gain" 1.2; the manufacturer also states an '
                'unbenchmarked 1.0 for comparison with other brands'
            ),
        ),
        _record(
            record_id='center-stage-xd/acoustic/attenuation',
            family='acoustic',
            subject='attenuation_db',
            manufacturer='Seymour AV',
            screen_model='Center Stage XD',
            evidence_class='manufacturer_declared',
            value='-1.4 dB average above 2 kHz',
            unit='dB',
            source_id='seymour-center-stage-xd-diy-page',
            locator='Center Stage XD specification table',
            note='manufacturer-declared improvement vs prior version',
        ),
        _record(
            record_id='center-stage-xd/moire/density',
            family='moire_structure',
            subject='perforation_density',
            manufacturer='Seymour AV',
            screen_model='Center Stage XD',
            evidence_class='manufacturer_declared',
            value=1739,
            unit='holes per square inch',
            source_id='seymour-center-stage-xd-diy-page',
            locator='Center Stage XD description paragraph',
        ),
        _record(
            record_id='center-stage-xd/moire/min-seating',
            family='moire_structure',
            subject='seating_distance_min_ft',
            manufacturer='Seymour AV',
            screen_model='Center Stage XD',
            evidence_class='manufacturer_declared',
            value=10,
            unit='ft',
            source_id='seymour-center-stage-xd-diy-page',
            locator='Center Stage XD specification table',
        ),
    ),
)


__all__ = [
    'PROJECTION_SCREEN_EVIDENCE_REGISTRY',
    'SCREEN_EVIDENCE_AUTHORITY_VERSION',
    'SCREEN_EVIDENCE_SCHEMA_VERSION',
    'ScreenEvidenceClass',
    'ScreenEvidenceFamily',
    'ScreenEvidenceRecord',
    'ScreenEvidenceRegistry',
    'ScreenEvidenceSource',
    'ScreenEvidenceSubject',
    'build_screen_evidence_registry',
    'registry_family_coverage',
    'registry_records',
]
