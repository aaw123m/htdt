from __future__ import annotations

from .cad_standards import (
    CriterionDefinition,
    CriterionSource,
    StandardsProfile,
    StandardsSourceAuthority,
    build_standards_profile,
)
from .cad_standards_authorities import (
    AURO3D_HOME_GUIDE_URI,
    DOLBY_ATMOS_GUIDE_URI,
    RP22_SOURCE_URI,
    auro3d_home_v12_source_authority,
    dolby_atmos_home_5_1_2_source_authority,
    rp22_spatial_source_authority,
)


def _sourced_criterion(
    authority: StandardsSourceAuthority,
    extraction_id: str,
    *,
    criterion_id: str,
    name: str,
    applicable_domains: tuple[str, ...],
    required_inputs: tuple[str, ...] = (),
    required_capabilities: tuple[str, ...] = (),
    note: str | None = None,
) -> CriterionDefinition:
    """Build a published criterion exactly bound to one retained extraction.

    The citation text, normalized quantity/unit, and comparison rule all come
    from the authority's extraction record so the persisted criterion cannot
    drift from the claimed source authority.
    """

    extraction = authority.extraction(extraction_id)
    if extraction is None:
        raise ValueError(
            f'source authority has no extraction {extraction_id!r}'
        )
    return CriterionDefinition(
        criterion_id=criterion_id,
        name=name,
        source=CriterionSource(
            publisher=authority.publisher,
            document_title=authority.document_title,
            document_version=authority.document_version,
            reference=extraction.reference,
            source_uri=authority.source_uri,
            content_kind=extraction.content_kind,
            authority_ref=authority.ref(),
            extraction_id=extraction.extraction_id,
        ),
        quantity=extraction.quantity,
        unit=extraction.unit,
        applicable_domains=applicable_domains,
        required_inputs=required_inputs,
        required_capabilities=required_capabilities,
        rule=extraction.rule,
        note=note,
    )


def rp22_spatial_profile(level: int) -> StandardsProfile:
    """Return the explicit spatial/layout subset for one RP22 performance level.

    This deliberately excludes SPL/headroom, acoustic-response, and other RP22
    criteria that belong to separate physical/evidence authorities.
    """

    authority = rp22_spatial_source_authority(level)

    criteria: list[CriterionDefinition] = [
        _sourced_criterion(
            authority,
            'rp22.p01.listener-boundary-distance',
            criterion_id='rp22.p01.listener-boundary-distance',
            name='Minimum listener-to-boundary distance',
            applicable_domains=('seat',),
            required_inputs=('listener_head_to_nearest_room_boundary_m',),
            required_capabilities=('scene-geometry-distance-v1',),
            note='RP22 Appendix A uses a strict greater-than boundary for Parameter 1.',
        ),
        _sourced_criterion(
            authority,
            'rp22.p03.screen-speakers-outside-zone-count',
            criterion_id='rp22.p03.screen-speakers-outside-zone-count',
            name='Screen-wall speakers outside recommended zonal locations',
            applicable_domains=('room', 'speaker_layout'),
            required_inputs=('screen_wall_speakers_outside_recommended_zone_count',),
            required_capabilities=('rp22-recommended-zone-evaluation-v1',),
        ),
        _sourced_criterion(
            authority,
            'rp22.p07.wide-horizontal-median-deviation',
            criterion_id='rp22.p07.wide-horizontal-median-deviation',
            name='Wide-speaker horizontal deviation from median angle',
            applicable_domains=('wide_speaker',),
            required_inputs=('wide_horizontal_deviation_from_median_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
    ]

    if level >= 2:
        criteria.extend(
            (
                _sourced_criterion(
                    authority,
                    'rp22.p05.max-adjacent-surround-horizontal-angle',
                    criterion_id='rp22.p05.max-adjacent-surround-horizontal-angle',
                    name='Maximum horizontal angle between adjacent surround speakers',
                    applicable_domains=('seat', 'speaker_layout'),
                    required_inputs=('max_adjacent_surround_horizontal_angle_deg',),
                    required_capabilities=('layout-angle-v1',),
                ),
                _sourced_criterion(
                    authority,
                    'rp22.p09.max-adjacent-upper-vertical-angle',
                    criterion_id='rp22.p09.max-adjacent-upper-vertical-angle',
                    name='Maximum vertical angle between adjacent upper speakers',
                    applicable_domains=('seat', 'speaker_layout'),
                    required_inputs=('max_adjacent_upper_vertical_angle_deg',),
                    required_capabilities=('layout-angle-v1',),
                ),
                _sourced_criterion(
                    authority,
                    'rp22.p11.surround-wide-upper-outside-zone-count',
                    criterion_id='rp22.p11.surround-wide-upper-outside-zone-count',
                    name='Surround, wide, and upper speakers outside recommended zones',
                    applicable_domains=('room', 'speaker_layout'),
                    required_inputs=(
                        'surround_wide_upper_speakers_outside_recommended_zone_count',
                    ),
                    required_capabilities=('rp22-recommended-zone-evaluation-v1',),
                ),
            )
        )

    if level >= 3:
        criteria.append(
            _sourced_criterion(
                authority,
                'rp22.p08.upfiring-elevation-speakers-prohibited',
                criterion_id='rp22.p08.upfiring-elevation-speakers-prohibited',
                name='Up-firing elevation speakers prohibited',
                applicable_domains=('room', 'speaker_layout'),
                required_inputs=('uses_upfiring_elevation_speakers',),
                required_capabilities=('speaker-rendering-mode-v1',),
                note=(
                    'Only Levels 3 and 4 are encoded: the source explicitly disallows '
                    'up-firing/elevation speakers there. Levels 1 and 2 say they are '
                    'allowed, which is not treated as a requirement to use them.'
                ),
            )
        )

    return build_standards_profile(
        profile_id=f'cedia-cta-rp22-spatial-level-{level}',
        version='1.2-2023-09-prov1',
        name=f'CEDIA/CTA RP22 v1.2 spatial/layout subset — Level {level}',
        profile_kind='published',
        criteria=criteria,
    )


def dolby_atmos_home_5_1_2_profile() -> StandardsProfile:
    """Public Dolby 5.1.2 azimuth ranges mapped to HTDT signed azimuth.\n
    HTDT uses 0 degrees toward the screen/front, positive toward +X/right,
    negative toward -X/left, normalized to [-180, 180).
    """

    authority = dolby_atmos_home_5_1_2_source_authority()
    criteria = (
        _sourced_criterion(
            authority,
            'dolby.5.1.2.front-left-azimuth',
            criterion_id='dolby.5.1.2.front-left-azimuth',
            name='Front-left speaker azimuth',
            applicable_domains=('speaker_layout',),
            required_inputs=('front_left_azimuth_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
        _sourced_criterion(
            authority,
            'dolby.5.1.2.front-right-azimuth',
            criterion_id='dolby.5.1.2.front-right-azimuth',
            name='Front-right speaker azimuth',
            applicable_domains=('speaker_layout',),
            required_inputs=('front_right_azimuth_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
        _sourced_criterion(
            authority,
            'dolby.5.1.2.surround-left-azimuth',
            criterion_id='dolby.5.1.2.surround-left-azimuth',
            name='Surround-left speaker azimuth',
            applicable_domains=('speaker_layout',),
            required_inputs=('surround_left_azimuth_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
        _sourced_criterion(
            authority,
            'dolby.5.1.2.surround-right-azimuth',
            criterion_id='dolby.5.1.2.surround-right-azimuth',
            name='Surround-right speaker azimuth',
            applicable_domains=('speaker_layout',),
            required_inputs=('surround_right_azimuth_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
    )
    return build_standards_profile(
        profile_id='dolby-atmos-home-5.1.2-layout',
        version='r3.1-2018-12-13-prov1',
        name='Dolby Atmos Home Theater 5.1.2 layout guidance',
        profile_kind='published',
        criteria=criteria,
    )


def auro3d_home_v12_profile() -> StandardsProfile:
    """Explicit public AURO-3D Rev.12 elevation/opening-angle criteria.

    Table 3 is titled "Normative Speaker Positions". This profile deliberately
    avoids horizontal azimuth rows whose published table contains an apparent
    sign inconsistency for Height Right; HTDT does not silently repair source data.
    """

    authority = auro3d_home_v12_source_authority()
    criteria = (
        _sourced_criterion(
            authority,
            'auro.v12.lower-layer-max-elevation',
            criterion_id='auro.v12.lower-layer-max-elevation',
            name='Maximum lower-layer speaker elevation',
            applicable_domains=('auro_lower_speaker',),
            required_inputs=('lower_layer_speaker_elevation_deg',),
            required_capabilities=('layout-angle-v1',),
            note=(
                'The source states the Surround layer should not exceed 10° and '
                'Table 3 gives 10° as the maximum elevation for lower-layer roles. '
                'No unstated lower bound is inferred.'
            ),
        ),
        _sourced_criterion(
            authority,
            'auro.v12.height-layer-elevation',
            criterion_id='auro.v12.height-layer-elevation',
            name='Height-layer speaker elevation',
            applicable_domains=('auro_height_speaker',),
            required_inputs=('height_layer_speaker_elevation_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
        _sourced_criterion(
            authority,
            'auro.v12.top-speaker-elevation',
            criterion_id='auro.v12.top-speaker-elevation',
            name='Top speaker elevation',
            applicable_domains=('auro_top_speaker',),
            required_inputs=('top_speaker_elevation_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
        _sourced_criterion(
            authority,
            'auro.v12.surround-height-opening-angle',
            criterion_id='auro.v12.surround-height-opening-angle',
            name='Minimum opening angle between Surround and Height layers',
            applicable_domains=('speaker_layout',),
            required_inputs=('surround_height_opening_angle_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
        _sourced_criterion(
            authority,
            'auro.v12.screen-height-opening-angle',
            criterion_id='auro.v12.screen-height-opening-angle',
            name='Minimum opening angle for Height screen channels',
            applicable_domains=('speaker_layout',),
            required_inputs=('screen_height_opening_angle_deg',),
            required_capabilities=('layout-angle-v1',),
        ),
    )
    return build_standards_profile(
        profile_id='auro3d-home-layout',
        version='rev12-2024-05-16-prov1',
        name='AURO-3D Home Theater Setup Rev.12 explicit layout criteria',
        profile_kind='published',
        criteria=criteria,
    )


def builtin_standards_profiles() -> tuple[StandardsProfile, ...]:
    """Profiles whose pass/fail boundaries are explicit in public source material."""

    return (
        rp22_spatial_profile(1),
        rp22_spatial_profile(2),
        rp22_spatial_profile(3),
        rp22_spatial_profile(4),
        dolby_atmos_home_5_1_2_profile(),
        auro3d_home_v12_profile(),
    )
