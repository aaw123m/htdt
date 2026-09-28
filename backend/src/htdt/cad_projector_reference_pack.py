"""#1065 — projector primary-source reference pack.

Curated, per-field-sourced optical geometry and video capability records for
current home-theater projectors. Every asserted field carries its own
provenance — which primary document (manufacturer product page, specification
sheet, or user manual) it came from and where in that document — instead of a
single catch-all "datasheet" string.

Hard rules carried by the data model:

- manufacturer-rated values are never promoted to ``independent_measured``;
- the *combined* lens-shift envelope (simultaneous max H and V shift) stays
  UNKNOWN unless the manufacturer publishes it — the individual axis limits
  are NOT conjunctive;
- throw-ratio and lens-shift numbers keep their stated reference (aspect
  ratio, axis direction convention) attached to the value;
- region/model variants are separate pack entries, never merged silently.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload






ProjectorFieldEvidenceClass = Literal[
    'manufacturer_rated',
    'manufacturer_measured',
    'independent_measured',
    'user_measured',
    'unknown',
]

ProjectorReferenceDocumentKind = Literal[
    'manufacturer_product_page',
    'manufacturer_specification_sheet',
    'manufacturer_user_manual',
    'independent_review',
]

# Bounded vocabulary of asserted fields. Anything not listed here simply has
# no assertion — absence means UNKNOWN, never a fabricated value.
ProjectorFieldName = Literal[
    'native_panel_resolution',
    'imaging_device',
    'light_source_type',
    'rated_brightness_lumens',
    'rated_native_contrast',
    'rated_dynamic_contrast',
    'rated_light_source_life_hours',
    'optical_zoom_ratio',
    'throw_ratio_min',
    'throw_ratio_max',
    'throw_ratio_reference_aspect',
    'lens_shift_vertical_pct',
    'lens_shift_horizontal_pct',
    'lens_shift_combined_envelope',
    'lens_shift_drive',
    'lens_memory_count',
    'image_size_min_inches',
    'image_size_max_inches',
    'projection_distance_min_m',
    'projection_distance_max_m',
    'hdmi_input_count',
    'hdmi_max_signal',
    'video_signal_support',
    'hdr_format_support',
    'color_space_claim',
    'aperture_range',
    'trigger_output',
    'network_control',
    'dimensions_inches',
    'weight_lb',
    'fan_noise_db',
]


class ProjectorReferenceSource(BaseModel):
    """One primary-source document backing one or more field assertions."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    document_kind: ProjectorReferenceDocumentKind
    uri: str = Field(min_length=1)
    retrieved_utc: str = Field(min_length=1)
    document_id: str | None = None
    document_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ProjectorReferenceSource':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'projector reference source semantic hash mismatch'
            )
        return self


class ProjectorFieldAssertion(BaseModel):
    """One field value, one source, one evidence class."""

    model_config = ConfigDict(frozen=True)

    field: ProjectorFieldName
    value_json: str = Field(min_length=1)
    unit: str | None = None
    evidence_class: ProjectorFieldEvidenceClass
    source_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    note: str | None = None
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ProjectorFieldAssertion':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'projector field assertion semantic hash mismatch'
            )
        return self

    def parsed_value(self) -> Any:
        return json.loads(self.value_json)


class ProjectorReferencePack(BaseModel):
    """All verified primary-source assertions for one model variant."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['projector-reference-pack-1'] = (
        'projector-reference-pack-1'
    )
    pack_id: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_family: str | None = None
    region_variant: str | None = None
    sources: tuple[ProjectorReferenceSource, ...]
    assertions: tuple[ProjectorFieldAssertion, ...]
    unknown_fields: tuple[ProjectorFieldName, ...] = ()
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ProjectorReferencePack':
        source_ids = {s.source_id for s in self.sources}
        seen: set[str] = set()
        for assertion in self.assertions:
            if assertion.source_id not in source_ids:
                raise ValueError(
                    'assertion references unknown source_id '
                    f'{assertion.source_id!r}'
                )
            if assertion.field in seen:
                raise ValueError(
                    f'duplicate assertion for field {assertion.field!r}'
                )
            seen.add(assertion.field)
        unknown = set(self.unknown_fields)
        if len(unknown) != len(self.unknown_fields):
            raise ValueError('duplicate entries in unknown_fields')
        for name in unknown:
            if name in seen:
                raise ValueError(
                    f'field {name!r} both asserted and declared unknown'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'projector reference pack semantic hash mismatch'
            )
        return self


def _source(
    *,
    source_id: str,
    publisher: str,
    document_title: str,
    document_kind: ProjectorReferenceDocumentKind,
    uri: str,
    retrieved_utc: str,
    document_id: str | None = None,
    document_sha256: str | None = None,
) -> ProjectorReferenceSource:
    probe = ProjectorReferenceSource.model_construct(
        source_id=source_id,
        publisher=publisher,
        document_title=document_title,
        document_kind=document_kind,
        uri=uri,
        retrieved_utc=retrieved_utc,
        document_id=document_id,
        document_sha256=document_sha256,
        semantic_sha256='',
    )
    return ProjectorReferenceSource(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def _assertion(
    field: ProjectorFieldName,
    value: Any,
    *,
    evidence_class: ProjectorFieldEvidenceClass,
    source_id: str,
    locator: str,
    unit: str | None = None,
    note: str | None = None,
) -> ProjectorFieldAssertion:
    probe = ProjectorFieldAssertion.model_construct(
        field=field,
        value_json=_canonical(value),
        unit=unit,
        evidence_class=evidence_class,
        source_id=source_id,
        locator=locator,
        note=note,
        semantic_sha256='',
    )
    return ProjectorFieldAssertion(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def build_projector_reference_pack(
    *,
    pack_id: str,
    manufacturer: str,
    model: str,
    model_family: str | None = None,
    region_variant: str | None = None,
    sources: tuple[ProjectorReferenceSource, ...],
    assertions: tuple[ProjectorFieldAssertion, ...],
    unknown_fields: tuple[ProjectorFieldName, ...] = (),
) -> ProjectorReferencePack:
    probe = ProjectorReferencePack.model_construct(**canonicalize_payload(ProjectorReferencePack, dict(
        schema_version=1,
        authority_version='projector-reference-pack-1',
        pack_id=pack_id,
        manufacturer=manufacturer,
        model=model,
        model_family=model_family,
        region_variant=region_variant,
        sources=tuple(sources),
        assertions=tuple(assertions),
        unknown_fields=tuple(unknown_fields),
        semantic_sha256='',
    )))
    return ProjectorReferencePack(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def pack_field_value(
    pack: ProjectorReferencePack, field: ProjectorFieldName
) -> Any | None:
    """Return the asserted value for ``field`` or None when unasserted."""
    for assertion in pack.assertions:
        if assertion.field == field:
            return assertion.parsed_value()
    return None


def pack_field_assertion(
    pack: ProjectorReferencePack, field: ProjectorFieldName
) -> ProjectorFieldAssertion | None:
    for assertion in pack.assertions:
        if assertion.field == field:
            return assertion
    return None


def reference_pack_optical_inputs(
    pack: ProjectorReferencePack,
) -> dict[str, Any]:
    """Project pack assertions onto the #455 optical-field vocabulary.

    Only asserted fields are returned; anything the primary sources did not
    state stays absent so downstream evidence records keep it UNKNOWN.
    'lens_shift_combined_envelope' is only present when a manufacturer
    actually published a combined envelope — axis limits alone are never
    merged into one.
    """
    mapping = {
        'native_panel_resolution': 'raster_native',
        'throw_ratio_min': 'throw_ratio_min',
        'throw_ratio_max': 'throw_ratio_max',
        'lens_shift_vertical_pct': 'lens_shift_vertical_pct',
        'lens_shift_horizontal_pct': 'lens_shift_horizontal_pct',
        'lens_shift_combined_envelope': 'lens_shift_combined_envelope',
        'optical_zoom_ratio': 'zoom_range',
        'image_size_min_inches': 'image_size_min_in',
        'image_size_max_inches': 'image_size_max_in',
    }
    out: dict[str, Any] = {}
    for field, key in mapping.items():
        value = pack_field_value(pack, field)  # type: ignore[arg-type]
        if value is not None:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Curated pack data. Every value below was read directly from the cited
# primary document on 2026-09-25. Unverifiable numbers are deliberately left
# unasserted and listed in ``unknown_fields``.
# ---------------------------------------------------------------------------

_JVC_NZ500_SPEC = _source(
    source_id='jvc-dla-nz500-specifications',
    publisher='JVCKENWOOD USA Corporation',
    document_title='DLA-NZ500 — Specifications',
    document_kind='manufacturer_product_page',
    uri='https://www.jvc.com/usa/projectors/dla-nz500/#specifications',
    retrieved_utc='2026-09-25',
)

_JVC_NZ700_SPEC = _source(
    source_id='jvc-dla-nz700-specifications',
    publisher='JVCKENWOOD USA Corporation',
    document_title='DLA-NZ700 — Specifications',
    document_kind='manufacturer_product_page',
    uri='https://www.jvc.com/usa/projectors/dla-nz700/#specifications',
    retrieved_utc='2026-09-25',
)

_EPSON_LS12000_MANUAL = _source(
    source_id='epson-ls12000-user-manual',
    publisher='Seiko Epson Corporation',
    document_title=(
        'Pro Cinema LS12000 / Home Cinema LS12000B Projector User Guide '
        '(PDF, 174 pages)'
    ),
    document_kind='manufacturer_user_manual',
    uri='https://files.support.epson.com/docid/cpd6/cpd61119.pdf',
    retrieved_utc='2026-09-25',
    document_id='cpd61119',
    document_sha256=(
        'a0c037f526506fd27a4dc160088b8171fda7ae0abf134edeb7c4717a0e341df2'
    ),
)

_SONY_XW6100ES_PAGE = _source(
    source_id='sony-vpl-xw6100es-product-page',
    publisher='Sony Electronics Inc.',
    document_title='VPL-XW6100ES (BRAVIA Projector 8) — Overview',
    document_kind='manufacturer_product_page',
    uri='https://electronics.sony.com/tv-video/projectors/all-projectors/'
    'p/vplxw6100es',
    retrieved_utc='2026-09-25',
)

_SONY_XW8100ES_PAGE = _source(
    source_id='sony-vpl-xw8100es-product-page',
    publisher='Sony Electronics Inc.',
    document_title='VPL-XW8100ES (BRAVIA Projector 9) — Overview',
    document_kind='manufacturer_product_page',
    uri='https://electronics.sony.com/tv-video/projectors/all-projectors/'
    'p/vplxw8100es',
    retrieved_utc='2026-09-25',
)


def _jvc_common_assertions(
    source_id: str,
    *,
    lumens: int,
    native_contrast: int,
    dila_generation: str,
    color_space_claim: str = '100% Rec.709',
) -> list[ProjectorFieldAssertion]:
    """Shared DLA-NZ platform assertions (both pages publish them)."""
    return [
        _assertion(
            'native_panel_resolution',
            {'width': 4096, 'height': 2160},
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Device',
        ),
        _assertion(
            'imaging_device',
            f'0.69-inch D-ILA ({dila_generation}), 3-chip',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Device',
        ),
        _assertion(
            'light_source_type',
            'BLU-Escent laser diode',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Light Source',
        ),
        _assertion(
            'rated_brightness_lumens',
            lumens,
            unit='lm',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Brightness',
        ),
        _assertion(
            'rated_native_contrast',
            native_contrast,
            unit=':1',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Native Contrast Ratio',
        ),
        _assertion(
            'rated_dynamic_contrast',
            'infinite:1',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Dynamic Contrast Ratio',
        ),
        _assertion(
            'rated_light_source_life_hours',
            20000,
            unit='h',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Light Source Life',
        ),
        _assertion(
            'optical_zoom_ratio',
            1.6,
            unit='x',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Zoom',
        ),
        _assertion(
            'throw_ratio_min',
            1.34,
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Throw Ratio',
            note=(
                '16:9 reference; the same table lists 1.26–2.01 for 17:9 '
                'raster content'
            ),
        ),
        _assertion(
            'throw_ratio_max',
            2.14,
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Throw Ratio',
            note='16:9 reference; 17:9 reference max is 2.01',
        ),
        _assertion(
            'throw_ratio_reference_aspect',
            '16:9',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Throw Ratio',
            note='table additionally lists a 17:9 column (1.26–2.01)',
        ),
        _assertion(
            'lens_shift_vertical_pct',
            70.0,
            unit='±%',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Lens Shift',
        ),
        _assertion(
            'lens_shift_horizontal_pct',
            28.0,
            unit='±%',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Lens Shift',
        ),
        _assertion(
            'lens_shift_drive',
            'motorized (zoom/focus/shift)',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Lens',
        ),
        _assertion(
            'lens_memory_count',
            5,
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Features > Installation Mode presets',
        ),
        _assertion(
            'image_size_min_inches',
            60,
            unit='in',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Display Size',
        ),
        _assertion(
            'image_size_max_inches',
            200,
            unit='in',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Display Size',
        ),
        _assertion(
            'hdmi_input_count',
            2,
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Input Terminals',
        ),
        _assertion(
            'hdmi_max_signal',
            '4K60P (32Gbps, HDCP 2.3); no 4K120 input',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Input Terminals / Input Signal',
        ),
        _assertion(
            'video_signal_support',
            [
                '3840x2160p24', '3840x2160p25', '3840x2160p30',
                '3840x2160p50', '3840x2160p60',
                '4096x2160p24', '4096x2160p25', '4096x2160p30',
                '4096x2160p50', '4096x2160p60',
            ],
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Input Signal',
        ),
        _assertion(
            'hdr_format_support',
            ['HDR10', 'HDR10+', 'HLG', 'Frame Adapt HDR Generation2'],
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Features > HDR',
        ),
        _assertion(
            'color_space_claim',
            color_space_claim,
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Features / Specifications > Color Space',
        ),
        _assertion(
            'aperture_range',
            {'min': -15, 'max': 0},
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Aperture',
        ),
        _assertion(
            'trigger_output',
            '12V trigger out (mini jack)',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Output Terminals',
        ),
        _assertion(
            'network_control',
            'LAN (RJ-45); Control4 SDDP',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Control Terminals',
        ),
        _assertion(
            'dimensions_inches',
            {'w': 17.72, 'h': 7.16, 'd': 18.87},
            unit='in',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Dimensions (W x H x D)',
        ),
        _assertion(
            'weight_lb',
            32.2,
            unit='lb',
            evidence_class='manufacturer_rated',
            source_id=source_id,
            locator='Specifications > Weight',
        ),
    ]


JVC_DLA_NZ500_PACK = build_projector_reference_pack(
    pack_id='projector/jvc-dla-nz500',
    manufacturer='JVC',
    model='DLA-NZ500',
    model_family='DLA-NZ series (Gen2 0.69-inch D-ILA)',
    sources=(_JVC_NZ500_SPEC,),
    assertions=tuple(
        _jvc_common_assertions(
            'jvc-dla-nz500-specifications',
            lumens=2000,
            native_contrast=40000,
            dila_generation='2nd generation',
        )
    ),
    unknown_fields=('lens_shift_combined_envelope', 'fan_noise_db'),
)

JVC_DLA_NZ700_PACK = build_projector_reference_pack(
    pack_id='projector/jvc-dla-nz700',
    manufacturer='JVC',
    model='DLA-NZ700',
    model_family='DLA-NZ series (Gen3 0.69-inch D-ILA)',
    sources=(_JVC_NZ700_SPEC,),
    assertions=tuple(
        _jvc_common_assertions(
            'jvc-dla-nz700-specifications',
            lumens=2300,
            native_contrast=80000,
            dila_generation='3rd generation',
            color_space_claim=(
                'DCI-P3 gamut coverage (Re.2020 mapping); 100% Rec.709'
            ),
        )
    ),
    unknown_fields=('lens_shift_combined_envelope', 'fan_noise_db'),
)

EPSON_LS12000_PACK = build_projector_reference_pack(
    pack_id='projector/epson-pro-cinema-ls12000',
    manufacturer='Epson',
    model='Pro Cinema LS12000 / Home Cinema LS12000B',
    model_family='Pro Cinema LS (4K PRO-UHD pixel shift)',
    region_variant='Pro Cinema (US dealer) / Home Cinema LS12000B share manual',
    sources=(_EPSON_LS12000_MANUAL,),
    assertions=(
        _assertion(
            'native_panel_resolution',
            {'width': 1920, 'height': 1080, 'panels': 3},
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > LCD panel',
            note=(
                '4K PRO-UHD output is produced by pixel shift; the physical '
                'panels are 1920x1080. Never record this model as native 4K.'
            ),
        ),
        _assertion(
            'imaging_device',
            '3LCD polysilicon TFT, 0.74-inch x3',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > LCD panel',
        ),
        _assertion(
            'light_source_type',
            'Laser diode',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Light source',
        ),
        _assertion(
            'rated_brightness_lumens',
            2700,
            unit='lm',
            evidence_class='manufacturer_measured',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Light output',
            note='ISO 21118 / IDMS 15.4 white and color output',
        ),
        _assertion(
            'rated_dynamic_contrast',
            '2500000:1',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Contrast ratio',
            note=(
                'with Dynamic Contrast on; conditions: Normal light output, '
                'Dynamic color mode, Wide zoom, V-shift ±50%, H centered'
            ),
        ),
        _assertion(
            'optical_zoom_ratio',
            2.1,
            unit='x',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Zoom',
            note='powered zoom, F 2.0–3.0, f 22.5–46.7 mm',
        ),
        _assertion(
            'lens_shift_drive',
            'powered (zoom/focus/lens shift)',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Adjusting the Image Position',
        ),
        _assertion(
            'lens_memory_count',
            10,
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Using Lens Position Memory (Lens1/Lens2 buttons)',
        ),
        _assertion(
            'image_size_min_inches',
            50,
            unit='in',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Image size (16:9)',
        ),
        _assertion(
            'image_size_max_inches',
            300,
            unit='in',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Image size (16:9)',
        ),
        _assertion(
            'projection_distance_min_m',
            1.47,
            unit='m',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Projection distance',
        ),
        _assertion(
            'projection_distance_max_m',
            19.0,
            unit='m',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Projection distance',
        ),
        _assertion(
            'hdmi_input_count',
            2,
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > HDMI inputs',
        ),
        _assertion(
            'hdmi_max_signal',
            '4K 3840x2160 / 4096x2160 up to 120 Hz',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Supported Video Display Formats',
        ),
        _assertion(
            'video_signal_support',
            [
                '3840x2160p24', '3840x2160p25', '3840x2160p30',
                '3840x2160p50', '3840x2160p60', '3840x2160p100',
                '3840x2160p120',
                '4096x2160p24', '4096x2160p25', '4096x2160p30',
                '4096x2160p50', '4096x2160p60', '4096x2160p100',
                '4096x2160p120',
            ],
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Supported Video Display Formats',
        ),
        _assertion(
            'trigger_output',
            '12V trigger out (mini jack)',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Trigger out',
        ),
        _assertion(
            'network_control',
            'LAN (RJ-45)',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > LAN',
        ),
        _assertion(
            'fan_noise_db',
            {'normal': 30.0, 'quiet': 22.0},
            unit='dB',
            evidence_class='manufacturer_rated',
            source_id='epson-ls12000-user-manual',
            locator='Appendix > Specifications > Fan noise',
        ),
    ),
    unknown_fields=(
        'throw_ratio_min',
        'throw_ratio_max',
        'throw_ratio_reference_aspect',
        'lens_shift_vertical_pct',
        'lens_shift_horizontal_pct',
        'lens_shift_combined_envelope',
        'color_space_claim',
        'hdr_format_support',
        'dimensions_inches',
        'weight_lb',
        'aperture_range',
        'rated_native_contrast',
        'rated_light_source_life_hours',
    ),
)

SONY_XW6100ES_PACK = build_projector_reference_pack(
    pack_id='projector/sony-vpl-xw6100es',
    manufacturer='Sony',
    model='VPL-XW6100ES (BRAVIA Projector 8)',
    model_family='Sony XW series (SXRD laser)',
    sources=(_SONY_XW6100ES_PAGE,),
    assertions=(
        _assertion(
            'native_panel_resolution',
            {'width': 3840, 'height': 2160},
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Highlights > Native 4K SXRD panel',
        ),
        _assertion(
            'imaging_device',
            'SXRD, native 4K',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Highlights',
        ),
        _assertion(
            'light_source_type',
            'Laser',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Highlights > Laser light source',
        ),
        _assertion(
            'rated_brightness_lumens',
            2700,
            unit='lm',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Highlights > Brightness',
        ),
        _assertion(
            'lens_shift_vertical_pct',
            85.0,
            unit='±%',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Specifications > Lens Shift',
        ),
        _assertion(
            'lens_shift_horizontal_pct',
            36.0,
            unit='±%',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Specifications > Lens Shift',
        ),
        _assertion(
            'lens_shift_drive',
            'motorized (zoom/focus/shift)',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Specifications > Lens',
        ),
        _assertion(
            'lens_memory_count',
            5,
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Picture Position Memory',
        ),
        _assertion(
            'hdmi_input_count',
            2,
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Specifications > Inputs',
        ),
        _assertion(
            'hdmi_max_signal',
            '4K120 (HDMI 2.1, ALLM)',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Specifications > Inputs / HDMI 2.1 features',
        ),
        _assertion(
            'hdr_format_support',
            ['HDR10', 'HLG'],
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw6100es-product-page',
            locator='Specifications > HDR compatibility',
        ),
    ),
    unknown_fields=(
        'throw_ratio_min',
        'throw_ratio_max',
        'throw_ratio_reference_aspect',
        'lens_shift_combined_envelope',
        'rated_native_contrast',
        'rated_dynamic_contrast',
        'rated_light_source_life_hours',
        'optical_zoom_ratio',
        'image_size_min_inches',
        'image_size_max_inches',
        'projection_distance_min_m',
        'projection_distance_max_m',
        'video_signal_support',
        'color_space_claim',
        'aperture_range',
        'trigger_output',
        'network_control',
        'dimensions_inches',
        'weight_lb',
        'fan_noise_db',
    ),
)

SONY_XW8100ES_PACK = build_projector_reference_pack(
    pack_id='projector/sony-vpl-xw8100es',
    manufacturer='Sony',
    model='VPL-XW8100ES (BRAVIA Projector 9)',
    model_family='Sony XW series (SXRD laser)',
    sources=(_SONY_XW8100ES_PAGE,),
    assertions=(
        _assertion(
            'native_panel_resolution',
            {'width': 3840, 'height': 2160},
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw8100es-product-page',
            locator='Highlights > Native 4K SXRD panel',
        ),
        _assertion(
            'imaging_device',
            'SXRD, native 4K',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw8100es-product-page',
            locator='Highlights',
        ),
        _assertion(
            'light_source_type',
            'Laser',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw8100es-product-page',
            locator='Highlights > Laser light source',
        ),
        _assertion(
            'rated_brightness_lumens',
            3400,
            unit='lm',
            evidence_class='manufacturer_rated',
            source_id='sony-vpl-xw8100es-product-page',
            locator='Highlights > Brightness',
        ),
    ),
    unknown_fields=(
        'throw_ratio_min',
        'throw_ratio_max',
        'throw_ratio_reference_aspect',
        'lens_shift_vertical_pct',
        'lens_shift_horizontal_pct',
        'lens_shift_combined_envelope',
        'lens_shift_drive',
        'lens_memory_count',
        'rated_native_contrast',
        'rated_dynamic_contrast',
        'rated_light_source_life_hours',
        'optical_zoom_ratio',
        'image_size_min_inches',
        'image_size_max_inches',
        'projection_distance_min_m',
        'projection_distance_max_m',
        'hdmi_input_count',
        'hdmi_max_signal',
        'video_signal_support',
        'hdr_format_support',
        'color_space_claim',
        'aperture_range',
        'trigger_output',
        'network_control',
        'dimensions_inches',
        'weight_lb',
        'fan_noise_db',
    ),
)


PROJECTOR_REFERENCE_PACKS: tuple[ProjectorReferencePack, ...] = (
    JVC_DLA_NZ500_PACK,
    JVC_DLA_NZ700_PACK,
    EPSON_LS12000_PACK,
    SONY_XW6100ES_PACK,
    SONY_XW8100ES_PACK,
)


def projector_reference_pack(
    pack_id: str,
) -> ProjectorReferencePack | None:
    for pack in PROJECTOR_REFERENCE_PACKS:
        if pack.pack_id == pack_id:
            return pack
    return None


__all__ = [
    'PROJECTOR_REFERENCE_PACKS',
    'JVC_DLA_NZ500_PACK',
    'JVC_DLA_NZ700_PACK',
    'EPSON_LS12000_PACK',
    'SONY_XW6100ES_PACK',
    'SONY_XW8100ES_PACK',
    'ProjectorFieldAssertion',
    'ProjectorFieldEvidenceClass',
    'ProjectorFieldName',
    'ProjectorReferenceDocumentKind',
    'ProjectorReferencePack',
    'ProjectorReferenceSource',
    'build_projector_reference_pack',
    'pack_field_assertion',
    'pack_field_value',
    'projector_reference_pack',
    'reference_pack_optical_inputs',
]
