from __future__ import annotations

from .cad_standards import (
    CriterionDefinition,
    CriterionSource,
    EvidenceRequirement,
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
    dolby_atmos_home_5_1_2_source_authority_v2,
    rp22_performance_source_authority,
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
    evidence_requirement: EvidenceRequirement = 'predicted_or_measured',
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
        evidence_requirement=evidence_requirement,
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


def rp22_performance_profile(level: int) -> StandardsProfile:
    """RP22 dynamics/timbre criteria for one performance level (#805).

    SPL difference, SPL capability/headroom, background noise, bass
    extension and the measured seat-to-seat/response/reflection
    parameters whose limits are explicitly published per level. Criteria
    that the standard makes measured-evidence-only declare
    ``evidence_requirement='measured'``: a predicted observation always
    evaluates UNKNOWN there. Parameter 2 (renderer/speaker count) is not
    encoded — its L3/L4 boundary is format-conditional (15 feeds, 13 for
    an Auro-3D design) and no honest single criterion exists.
    """

    authority = rp22_performance_source_authority(level)

    criteria: list[CriterionDefinition] = [
        _sourced_criterion(
            authority,
            'rp22.p04.screen-spl-difference',
            criterion_id='rp22.p04.screen-spl-difference',
            name='Maximum SPL difference between screen wall speakers',
            applicable_domains=('seat', 'speaker_layout'),
            required_inputs=('screen_speaker_spl_difference_db',),
            required_capabilities=('spl-difference-evidence-v1',),
            note=(
                'Predicted values cover anechoic propagation only; '
                'in-room contributions require measured evidence.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p06.surround-spl-difference',
            criterion_id='rp22.p06.surround-spl-difference',
            name='Maximum SPL difference between surround speakers',
            applicable_domains=('seat', 'speaker_layout'),
            required_inputs=('surround_speaker_spl_difference_db',),
            required_capabilities=('spl-difference-evidence-v1',),
            note=(
                'Predicted values cover anechoic propagation only; '
                'in-room contributions require measured evidence.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p10.upper-spl-difference',
            criterion_id='rp22.p10.upper-spl-difference',
            name='Maximum SPL difference between upper speakers',
            applicable_domains=('seat', 'speaker_layout'),
            required_inputs=('upper_speaker_spl_difference_db',),
            required_capabilities=('spl-difference-evidence-v1',),
            note=(
                'Predicted values cover anechoic propagation only; '
                'in-room contributions require measured evidence.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p12.screen-spl-capability',
            criterion_id='rp22.p12.screen-spl-capability',
            name='Screen speakers SPL capability at the RSP',
            applicable_domains=('room', 'speaker_layout'),
            required_inputs=('screen_speaker_spl_capability_db',),
            required_capabilities=('spl-capability-evidence-v1',),
            note=(
                'System capability, not normal listening level; a '
                'sensitivity-plus-amplifier-watts figure is not '
                'evaluatable — the evidence must include '
                'output-limit/compression information.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p13.non-screen-spl-capability',
            criterion_id='rp22.p13.non-screen-spl-capability',
            name='Non-screen speakers SPL capability at the RSP',
            applicable_domains=('room', 'speaker_layout'),
            required_inputs=('non_screen_speaker_spl_capability_db',),
            required_capabilities=('spl-capability-evidence-v1',),
            note=(
                'System capability, not normal listening level; '
                'amplifier headroom is included.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p14.lfe-spl-capability',
            criterion_id='rp22.p14.lfe-spl-capability',
            name='LFE-band total SPL capability at the RSP',
            applicable_domains=('room', 'speaker_layout'),
            required_inputs=('lfe_spl_capability_db',),
            required_capabilities=('spl-capability-evidence-v1',),
            note=(
                'The evidence must compose the exact bass-management '
                'routing and subwoofer count; theoretical boundary or '
                'multi-sub gain is never counted twice.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p15.background-noise-ncb',
            criterion_id='rp22.p15.background-noise-ncb',
            name='Background noise floor (NCB) with all systems running',
            applicable_domains=('room',),
            required_inputs=('background_noise_ncb_rating',),
            required_capabilities=('operating-state-noise-measurement-v1',),
            evidence_requirement='measured',
            note=(
                'An octave-band NCB rating measured under the operating '
                'room/device state — a silent or cold-room measurement '
                'is not evidence.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p16.seat-to-seat-fr-variance-screen',
            criterion_id='rp22.p16.seat-to-seat-fr-variance-screen',
            name='Seat-to-seat response variance, screen speakers',
            applicable_domains=('seat', 'speaker_layout'),
            required_inputs=('seat_to_seat_fr_variance_screen_db',),
            required_capabilities=('seat-response-measurement-v1',),
            evidence_requirement='measured',
            note=(
                'Requires measured per-seat responses normalized to the '
                'RSP over 500 Hz–16 kHz with the declared 1-octave '
                'smoothing; a design prediction is not the claim.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p18.bass-extension',
            criterion_id='rp22.p18.bass-extension',
            name='In-room bass extension (-3 dB point)',
            applicable_domains=('room',),
            required_inputs=('bass_extension_hz',),
            required_capabilities=('bass-extension-evidence-v1',),
            note=(
                'A predicted extension does not verify the '
                'no-distortion/no-rattle condition at the Parameter 14 '
                'SPL — the full claim still requires verification '
                'evidence.'
            ),
        ),
        _sourced_criterion(
            authority,
            'rp22.p19.lf-response-vs-target',
            criterion_id='rp22.p19.lf-response-vs-target',
            name='LF response vs target below transition frequency',
            applicable_domains=('seat',),
            required_inputs=('lf_response_vs_target_db',),
            required_capabilities=('target-response-measurement-v1',),
            evidence_requirement='measured',
            note=(
                'Measured RSP response relative to a declared target '
                'profile below the declared transition frequency, '
                '1/3-octave smoothing — never a generic flatness score.'
            ),
        ),
    ]

    if level >= 3:
        criteria.append(
            _sourced_criterion(
                authority,
                'rp22.p17.seat-to-seat-fr-variance-surround-upper',
                criterion_id='rp22.p17.seat-to-seat-fr-variance-surround-upper',
                name='Seat-to-seat response variance, surround/upper speakers',
                applicable_domains=('seat', 'speaker_layout'),
                required_inputs=('seat_to_seat_fr_variance_surround_upper_db',),
                required_capabilities=('seat-response-measurement-v1',),
                evidence_requirement='measured',
                note='Evaluated only at Levels 3 and 4.',
            )
        )

    if level >= 2:
        criteria.extend(
            (
                _sourced_criterion(
                    authority,
                    'rp22.p20.seat-to-seat-lf-variance',
                    criterion_id='rp22.p20.seat-to-seat-lf-variance',
                    name='Seat-to-seat LF response variance',
                    applicable_domains=('seat', 'speaker_layout'),
                    required_inputs=('seat_to_seat_lf_variance_db',),
                    required_capabilities=('seat-response-measurement-v1',),
                    evidence_requirement='measured',
                    note=(
                        'Per-seat LF agreement with the measured RSP '
                        'response below the declared transition '
                        'frequency; evaluated from Level 2 upward.'
                    ),
                ),
                _sourced_criterion(
                    authority,
                    'rp22.p21.early-reflection-level',
                    criterion_id='rp22.p21.early-reflection-level',
                    name='Early-reflection level relative to direct sound',
                    applicable_domains=('room',),
                    required_inputs=('early_reflection_level_db',),
                    required_capabilities=('reflection-window-measurement-v1',),
                    evidence_requirement='measured',
                    note=(
                        'Measured early-reflection level in the 0–15 ms '
                        'window over 1–8 kHz; evaluated from Level 2 '
                        'upward.'
                    ),
                ),
            )
        )

    return build_standards_profile(
        profile_id=f'cedia-cta-rp22-performance-level-{level}',
        version='1.2-2023-09-prov1',
        name=(
            f'CEDIA/CTA RP22 v1.2 SPL/dynamics/timbre criteria — '
            f'Level {level}'
        ),
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


def dolby_atmos_home_5_1_2_profile_v2() -> StandardsProfile:
    """Dolby 5.1.2 layout guidance, revision 2 (#805).

    The prov1 azimuth criteria are re-anchored to the prov2 source
    authority (their normalized ranges are unchanged) and the Figure 11
    top-middle-overhead elevation window (65–100°) is added. New criteria
    enter only through a new profile revision: prov1 stays sealed and
    historical evaluations keep resolving their original revision.
    """

    authority = dolby_atmos_home_5_1_2_source_authority_v2()
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
        _sourced_criterion(
            authority,
            'dolby.5.1.2.top-middle-overhead-elevation',
            criterion_id='dolby.5.1.2.top-middle-overhead-elevation',
            name='Top middle overhead speaker elevation',
            applicable_domains=('speaker_layout',),
            required_inputs=('top_middle_overhead_elevation_deg',),
            required_capabilities=('layout-angle-v1',),
            note=(
                'Applies to the top-middle-overhead pair of a 5.1.2 '
                'overhead-speaker layout; 80 deg is the recommended '
                'position inside the published 65–100 deg window.'
            ),
        ),
    )
    return build_standards_profile(
        profile_id='dolby-atmos-home-5.1.2-layout',
        version='r3.1-2018-12-13-prov2',
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
    """Profiles whose pass/fail boundaries are explicit in public source material.

    Every emitted revision is returned, including superseded ones: a new
    revision never rewrites an old one, and historical evaluations keep
    resolving the exact profile version they were produced under.
    """

    return (
        rp22_spatial_profile(1),
        rp22_spatial_profile(2),
        rp22_spatial_profile(3),
        rp22_spatial_profile(4),
        rp22_performance_profile(1),
        rp22_performance_profile(2),
        rp22_performance_profile(3),
        rp22_performance_profile(4),
        dolby_atmos_home_5_1_2_profile(),
        dolby_atmos_home_5_1_2_profile_v2(),
        auro3d_home_v12_profile(),
    )
