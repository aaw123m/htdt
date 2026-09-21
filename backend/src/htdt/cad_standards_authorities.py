from __future__ import annotations

from .cad_standards import (
    CriterionSourceExtraction,
    CriterionRule,
    StandardsSourceAuthority,
    build_standards_source_authority,
)


RP22_SOURCE_URI = (
    'https://cedia.org/site/assets/files/6057/'
    'cedia-cta_rp22_v1_2_sept_2023.pdf'
)
DOLBY_ATMOS_GUIDE_URI = (
    'https://www.dolby.com/siteassets/technologies/dolby-atmos/'
    'atmos-installation-guidelines-121318_r3.1.pdf'
)
AURO3D_HOME_GUIDE_URI = (
    'https://www.auro-3d.com/wp-content/uploads/2024/05/'
    'Auro-3D-Home-Theater-Setup-Guidelines-v12-20240516.pdf'
)

RP22_PUBLISHER = 'CEDIA / Consumer Technology Association (CTA)'
RP22_DOCUMENT_TITLE = (
    'CEDIA/CTA-RP22 Recommended Practice for Immersive Audio Design'
)
RP22_DOCUMENT_VERSION = 'v1.2, September 2023'

DOLBY_PUBLISHER = 'Dolby Laboratories'
DOLBY_DOCUMENT_TITLE = 'Dolby Atmos Home Theater Installation Guidelines'
DOLBY_DOCUMENT_VERSION = 'R3.1, 13 December 2018'

AURO3D_PUBLISHER = 'NEWAURO BV'
AURO3D_DOCUMENT_TITLE = 'AURO-3D Home Theater Setup — Installation Guidelines'
AURO3D_DOCUMENT_VERSION = 'Rev. 12, 16 May 2024'


_RP22_LISTENER_WALL_MIN_M = {
    1: 0.5,
    2: 0.8,
    3: 1.2,
    4: 1.5,
}
_RP22_MAX_HORIZONTAL_ADJACENT_DEG = {
    2: 80.0,
    3: 60.0,
    4: 50.0,
}
_RP22_WIDE_DEVIATION_MAX_DEG = {
    1: 10.0,
    2: 7.0,
    3: 5.0,
    4: 2.0,
}
_RP22_MAX_VERTICAL_ADJACENT_DEG = {
    2: 80.0,
    3: 60.0,
    4: 50.0,
}


def rp22_spatial_source_authority(level: int) -> StandardsSourceAuthority:
    """Exact RP22 v1.2 extraction records for one encoded performance level.

    Extraction ids match the criterion ids they back so a published criterion
    cannot silently reuse another level's or parameter's recorded threshold.
    """

    if level not in {1, 2, 3, 4}:
        raise ValueError('RP22 performance level must be 1, 2, 3, or 4')

    extractions: list[CriterionSourceExtraction] = [
        CriterionSourceExtraction(
            extraction_id='rp22.p01.listener-boundary-distance',
            reference='Appendix A, Parameter 1; §4.1.4',
            content_kind='normative',
            quantity='listener_head_to_nearest_room_boundary',
            unit='m',
            rule=CriterionRule(
                operator='min',
                minimum=_RP22_LISTENER_WALL_MIN_M[level],
                lower_inclusive=False,
            ),
            note=(
                f'RP22 Appendix A Parameter 1 Level {level} listener-to-boundary '
                f'distance {_RP22_LISTENER_WALL_MIN_M[level]} m with a strict '
                'greater-than boundary.'
            ),
        ),
        CriterionSourceExtraction(
            extraction_id='rp22.p03.screen-speakers-outside-zone-count',
            reference='Appendix A, Parameter 3; §5.5.4',
            content_kind='normative',
            quantity='screen_wall_speakers_outside_recommended_zone_count',
            unit='count',
            rule=CriterionRule(operator='max', maximum=0.0),
            note=(
                'RP22 Appendix A Parameter 3 requires zero screen-wall speakers '
                'outside the recommended zonal locations.'
            ),
        ),
        CriterionSourceExtraction(
            extraction_id='rp22.p07.wide-horizontal-median-deviation',
            reference='Appendix A, Parameter 7; §5.7',
            content_kind='normative',
            quantity='wide_horizontal_angle_deviation_from_median',
            unit='deg',
            rule=CriterionRule(
                operator='max',
                maximum=_RP22_WIDE_DEVIATION_MAX_DEG[level],
                angle_wrap='signed_180',
                absolute_value=True,
            ),
            note=(
                f'RP22 Appendix A Parameter 7 Level {level} maximum absolute '
                'wide-speaker horizontal deviation from the median angle '
                f'{_RP22_WIDE_DEVIATION_MAX_DEG[level]} deg.'
            ),
        ),
    ]

    if level in _RP22_MAX_HORIZONTAL_ADJACENT_DEG:
        extractions.extend(
            (
                CriterionSourceExtraction(
                    extraction_id='rp22.p05.max-adjacent-surround-horizontal-angle',
                    reference='Appendix A, Parameter 5; §5.6.2.1',
                    content_kind='normative',
                    quantity='adjacent_surround_speaker_horizontal_angle',
                    unit='deg',
                    rule=CriterionRule(
                        operator='max',
                        maximum=_RP22_MAX_HORIZONTAL_ADJACENT_DEG[level],
                    ),
                    note=(
                        f'RP22 Appendix A Parameter 5 Level {level} maximum '
                        'horizontal angle between adjacent surround speakers '
                        f'{_RP22_MAX_HORIZONTAL_ADJACENT_DEG[level]} deg.'
                    ),
                ),
                CriterionSourceExtraction(
                    extraction_id='rp22.p09.max-adjacent-upper-vertical-angle',
                    reference='Appendix A, Parameter 9; §5.8.2',
                    content_kind='normative',
                    quantity='adjacent_upper_speaker_vertical_angle',
                    unit='deg',
                    rule=CriterionRule(
                        operator='max',
                        maximum=_RP22_MAX_VERTICAL_ADJACENT_DEG[level],
                    ),
                    note=(
                        f'RP22 Appendix A Parameter 9 Level {level} maximum '
                        'vertical angle between adjacent upper speakers '
                        f'{_RP22_MAX_VERTICAL_ADJACENT_DEG[level]} deg.'
                    ),
                ),
                CriterionSourceExtraction(
                    extraction_id='rp22.p11.surround-wide-upper-outside-zone-count',
                    reference='Appendix A, Parameter 11; §5.9.3',
                    content_kind='normative',
                    quantity='surround_wide_upper_speakers_outside_recommended_zone_count',
                    unit='count',
                    rule=CriterionRule(operator='max', maximum=0.0),
                    note=(
                        'RP22 Appendix A Parameter 11 requires zero surround, '
                        'wide, and upper speakers outside recommended zones.'
                    ),
                ),
            )
        )

    if level >= 3:
        extractions.append(
            CriterionSourceExtraction(
                extraction_id='rp22.p08.upfiring-elevation-speakers-prohibited',
                reference='Appendix A, Parameter 8; §5.8.2',
                content_kind='normative',
                quantity='uses_upfiring_elevation_speakers',
                unit='boolean',
                rule=CriterionRule(operator='equals', expected=False),
                note=(
                    'Only Levels 3 and 4 are encoded: the source explicitly '
                    'disallows up-firing/elevation speakers there. Levels 1 and '
                    '2 say they are allowed, which is not treated as a '
                    'requirement to use them.'
                ),
            )
        )

    return build_standards_source_authority(
        publisher=RP22_PUBLISHER,
        document_title=RP22_DOCUMENT_TITLE,
        document_version=RP22_DOCUMENT_VERSION,
        source_uri=RP22_SOURCE_URI,
        extractions=extractions,
        note=(
            'Publicly available RP22 v1.2 source; document bytes are not '
            'bundled with HTDT. The authority retains only the explicitly '
            'encoded spatial/layout extraction subset for the declared '
            'performance level; it is not an overall RP22 performance-level '
            'certification.'
        ),
    )


def dolby_atmos_home_5_1_2_source_authority() -> StandardsSourceAuthority:
    """Exact Dolby R3.1 extraction records for the encoded 5.1.2 azimuth ranges.

    The published figure presents the endpoints as the placement range, so the
    recorded rules keep inclusive bounds. HTDT maps them into its explicit
    signed azimuth convention; this is a coordinate mapping, not an added
    tolerance.
    """

    reference = 'Figure 12, page 28 — 5.1.2 speaker placement'
    extractions = (
        CriterionSourceExtraction(
            extraction_id='dolby.5.1.2.front-left-azimuth',
            reference=reference,
            content_kind='guidance',
            quantity='speaker_azimuth_from_mlp',
            unit='deg',
            rule=CriterionRule(
                operator='range',
                minimum=-30.0,
                maximum=-22.0,
                angle_wrap='signed_180',
            ),
            note=(
                'Dolby Figure 12 front-left azimuth 22–30 deg left of the '
                'screen axis, mapped to HTDT signed azimuth -30..-22 deg.'
            ),
        ),
        CriterionSourceExtraction(
            extraction_id='dolby.5.1.2.front-right-azimuth',
            reference=reference,
            content_kind='guidance',
            quantity='speaker_azimuth_from_mlp',
            unit='deg',
            rule=CriterionRule(
                operator='range',
                minimum=22.0,
                maximum=30.0,
                angle_wrap='signed_180',
            ),
            note=(
                'Dolby Figure 12 front-right azimuth 22–30 deg right of the '
                'screen axis, mapped to HTDT signed azimuth +22..+30 deg.'
            ),
        ),
        CriterionSourceExtraction(
            extraction_id='dolby.5.1.2.surround-left-azimuth',
            reference=reference,
            content_kind='guidance',
            quantity='speaker_azimuth_from_mlp',
            unit='deg',
            rule=CriterionRule(
                operator='range',
                minimum=-110.0,
                maximum=-90.0,
                angle_wrap='signed_180',
            ),
            note=(
                'Dolby Figure 12 surround-left azimuth 90–110 deg left, mapped '
                'to HTDT signed azimuth -110..-90 deg.'
            ),
        ),
        CriterionSourceExtraction(
            extraction_id='dolby.5.1.2.surround-right-azimuth',
            reference=reference,
            content_kind='guidance',
            quantity='speaker_azimuth_from_mlp',
            unit='deg',
            rule=CriterionRule(
                operator='range',
                minimum=90.0,
                maximum=110.0,
                angle_wrap='signed_180',
            ),
            note=(
                'Dolby Figure 12 surround-right azimuth 90–110 deg right, '
                'mapped to HTDT signed azimuth +90..+110 deg.'
            ),
        ),
    )
    return build_standards_source_authority(
        publisher=DOLBY_PUBLISHER,
        document_title=DOLBY_DOCUMENT_TITLE,
        document_version=DOLBY_DOCUMENT_VERSION,
        source_uri=DOLBY_ATMOS_GUIDE_URI,
        extractions=extractions,
        note=(
            'Public Dolby installation guidance; document bytes are not '
            'bundled with HTDT. HTDT evaluates only the explicit speaker-angle '
            'ranges encoded here; no additional tolerance is inferred.'
        ),
    )


def auro3d_home_v12_source_authority() -> StandardsSourceAuthority:
    """Exact AURO-3D Rev.12 extraction records for the encoded layout criteria.

    Only unambiguous public min/max statements are recorded. The Rev.12 table
    also publishes horizontal azimuth rows whose Height Right row contains an
    apparent sign inconsistency; that row is deliberately absent rather than
    silently repaired.
    """

    extractions = (
        CriterionSourceExtraction(
            extraction_id='auro.v12.lower-layer-max-elevation',
            reference='§3.3.1.1, pages 23–24; §3.3.2 Table 3, page 26',
            content_kind='normative',
            quantity='speaker_elevation_from_mlp',
            unit='deg',
            rule=CriterionRule(operator='max', maximum=10.0),
            note=(
                'The source states the Surround layer should not exceed 10 deg '
                'and Table 3 gives 10 deg as the maximum elevation for '
                'lower-layer roles. No unstated lower bound is inferred.'
            ),
        ),
        CriterionSourceExtraction(
            extraction_id='auro.v12.height-layer-elevation',
            reference='§3.3.2 Table 3, page 26 — Normative Speaker Positions',
            content_kind='normative',
            quantity='speaker_elevation_from_mlp',
            unit='deg',
            rule=CriterionRule(
                operator='range',
                minimum=25.0,
                maximum=40.0,
            ),
            note='AURO Table 3 Height-layer speaker elevation 25–40 deg.',
        ),
        CriterionSourceExtraction(
            extraction_id='auro.v12.top-speaker-elevation',
            reference='§3.3.2 Table 3, page 26 — Normative Speaker Positions',
            content_kind='normative',
            quantity='speaker_elevation_from_mlp',
            unit='deg',
            rule=CriterionRule(
                operator='range',
                minimum=65.0,
                maximum=100.0,
            ),
            note='AURO Table 3 Top speaker elevation 65–100 deg.',
        ),
        CriterionSourceExtraction(
            extraction_id='auro.v12.surround-height-opening-angle',
            reference='§3.3.1.1, page 24; §3.3.2 Table 3 note, page 26',
            content_kind='normative',
            quantity='surround_to_height_opening_angle',
            unit='deg',
            rule=CriterionRule(operator='min', minimum=25.0),
            note=(
                'Minimum opening angle between Surround and Height layers '
                '25 deg per §3.3.1.1 and the Table 3 note.'
            ),
        ),
        CriterionSourceExtraction(
            extraction_id='auro.v12.screen-height-opening-angle',
            reference='§3.3.2 Table 3 note, page 26',
            content_kind='normative',
            quantity='screen_to_height_opening_angle',
            unit='deg',
            rule=CriterionRule(operator='min', minimum=22.0),
            note=(
                'Minimum opening angle for Height screen channels 22 deg per '
                'the Table 3 note.'
            ),
        ),
    )
    return build_standards_source_authority(
        publisher=AURO3D_PUBLISHER,
        document_title=AURO3D_DOCUMENT_TITLE,
        document_version=AURO3D_DOCUMENT_VERSION,
        source_uri=AURO3D_HOME_GUIDE_URI,
        extractions=extractions,
        note=(
            'Public AURO-3D home-theater guidance; document bytes are not '
            'bundled with HTDT. HTDT encodes only explicit normative min/max '
            'or minimum-angle criteria from the cited sections.'
        ),
    )


def builtin_standards_source_authorities() -> tuple[StandardsSourceAuthority, ...]:
    """Retained source authorities backing every built-in published profile."""

    return (
        rp22_spatial_source_authority(1),
        rp22_spatial_source_authority(2),
        rp22_spatial_source_authority(3),
        rp22_spatial_source_authority(4),
        dolby_atmos_home_5_1_2_source_authority(),
        auro3d_home_v12_source_authority(),
    )
