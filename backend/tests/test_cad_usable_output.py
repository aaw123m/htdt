"""Speaker usable-output capability tests (#648)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance, FrequencyDomain
from htdt.cad_usable_output import (
    DistortionMetric,
    ExcursionEvidence,
    LimiterState,
    OutputSample,
    SourceUsableOutputProfile,
    build_source_usable_output_profile,
    evaluate_headroom,
    usable_output_tier,
)


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='measured',
            source_name='klippel',
            source_version='dB-Lab 213',
            source_reference='ANE-0042 run',
            source_sha256='4' * 64,
        ),
    )


def _base_kwargs(**overrides):
    kwargs = dict(
        profile_id='uo-lcr-1',
        version='1',
        equipment_definition_id='def-book-1',
        equipment_definition_version='1',
        equipment_definition_sha256='3' * 64,
        mounting_condition='2π half-space baffle',
        measurement_distance_m=1.0,
        reference_axis='on_axis',
        environment='anechoic',
        excitation_method='log sweep',
        valid_domain='anechoic half-space, 1 m, on-axis',
        uncertainty_db=0.5,
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return kwargs


def test_tiers_progress_with_evidence():
    none_profile = build_source_usable_output_profile(
        samples=(), **_base_kwargs()
    )
    assert usable_output_tier(none_profile) == 'unknown'

    scalar = build_source_usable_output_profile(
        samples=(
            OutputSample(frequency_hz=1000.0, level_db_spl=95.0),
        ),
        **_base_kwargs(),
    )
    assert usable_output_tier(scalar) == 'scalar'

    curve = build_source_usable_output_profile(
        samples=(
            OutputSample(frequency_hz=100.0, level_db_spl=90.0),
            OutputSample(frequency_hz=1000.0, level_db_spl=95.0),
        ),
        **_base_kwargs(),
    )
    assert usable_output_tier(curve) == 'curve'

    comp = build_source_usable_output_profile(
        compression_reference_db_spl=95.0,
        samples=(
            OutputSample(
                frequency_hz=1000.0, level_db_spl=105.0,
                compression_db=1.2,
            ),
        ),
        **_base_kwargs(),
    )
    assert usable_output_tier(comp) == 'compression'

    both = build_source_usable_output_profile(
        compression_reference_db_spl=95.0,
        samples=(
            OutputSample(
                frequency_hz=1000.0, level_db_spl=105.0,
                compression_db=1.2,
                distortion=(DistortionMetric(kind='thd', percent=3.0),),
            ),
        ),
        **_base_kwargs(),
    )
    assert usable_output_tier(both) == 'combined'

    modeled = build_source_usable_output_profile(
        has_excursion_model=True,
        samples=(
            OutputSample(frequency_hz=1000.0, level_db_spl=95.0),
        ),
        **_base_kwargs(),
    )
    assert usable_output_tier(modeled) == 'excursion_model'


def test_compression_requires_reference():
    with pytest.raises(ValueError, match='reference level'):
        build_source_usable_output_profile(
            samples=(
                OutputSample(
                    frequency_hz=100.0, level_db_spl=100.0,
                    compression_db=1.0,
                ),
            ),
            **_base_kwargs(),
        )


def test_distortion_kinds():
    with pytest.raises(ValueError, match='percent'):
        DistortionMetric(kind='thd')
    unavailable = DistortionMetric(kind='unavailable')
    assert unavailable.percent is None
    harmonic = DistortionMetric(kind='harmonic', percent=0.8,
                                harmonic_order=3)
    assert harmonic.harmonic_order == 3


def test_duration_classes_stay_distinct():
    burst = OutputSample(
        frequency_hz=40.0, level_db_spl=110.0, duration_class='burst'
    )
    continuous = OutputSample(
        frequency_hz=40.0, level_db_spl=102.0, duration_class='continuous'
    )
    profile = build_source_usable_output_profile(
        compression_reference_db_spl=95.0,
        samples=(
            continuous,
            burst,
            OutputSample(
                frequency_hz=40.0, level_db_spl=105.0,
                duration_class='continuous',
                compression_db=1.4,
                distortion=(DistortionMetric(kind='thd', percent=10.0),),
            ),
        ),
        **_base_kwargs(),
    )
    # burst capability must not answer a continuous query
    evaluation = evaluate_headroom(
        profile=profile,
        target_level_db_spl=104.0,
        frequency_hz=40.0,
        duration_class='continuous',
        max_distortion_percent=15.0,
    )
    assert evaluation.available_level_db_spl == 105.0
    assert evaluation.headroom_db == pytest.approx(1.0)
    assert evaluation.basis == 'distortion_qualified'
    assert evaluation.tier_used == 'combined'


def test_burst_headroom_uses_burst_samples():
    profile = build_source_usable_output_profile(
        samples=(
            OutputSample(
                frequency_hz=40.0, level_db_spl=100.0,
                duration_class='continuous',
            ),
            OutputSample(
                frequency_hz=40.0, level_db_spl=112.0,
                duration_class='burst',
            ),
        ),
        **_base_kwargs(),
    )
    burst_eval = evaluate_headroom(
        profile=profile,
        target_level_db_spl=104.0,
        frequency_hz=40.0,
        duration_class='burst',
    )
    assert burst_eval.headroom_db is None or (
        burst_eval.available_level_db_spl == 112.0
    )


def test_headroom_bases():
    scalar = build_source_usable_output_profile(
        samples=(
            OutputSample(frequency_hz=1000.0, level_db_spl=95.0),
        ),
        **_base_kwargs(),
    )
    declared = evaluate_headroom(
        profile=scalar,
        target_level_db_spl=90.0,
        declared_spl_db=100.0,
    )
    assert declared.basis == 'scalar_declared'
    assert declared.headroom_db == pytest.approx(10.0)
    assert declared.reference_axis == 'on_axis'

    amp = evaluate_headroom(
        profile=None,
        target_level_db_spl=90.0,
        amplifier_headroom_db=6.0,
    )
    assert amp.basis == 'amplifier_margin'
    assert amp.tier_used == 'unknown'
    assert amp.status == 'PASS'

    unknown = evaluate_headroom(
        profile=None, target_level_db_spl=90.0
    )
    assert unknown.basis == 'unknown'
    assert unknown.status == 'UNKNOWN'
    assert unknown.headroom_db is None


def test_distortion_policy_filters_qualifying_samples():
    profile = build_source_usable_output_profile(
        compression_reference_db_spl=90.0,
        samples=(
            OutputSample(
                frequency_hz=60.0, level_db_spl=95.0,
                compression_db=0.3,
                distortion=(DistortionMetric(kind='thd', percent=2.0),),
            ),
            OutputSample(
                frequency_hz=60.0, level_db_spl=100.0,
                compression_db=1.5,
                distortion=(DistortionMetric(kind='thd', percent=8.0),),
            ),
        ),
        **_base_kwargs(),
    )
    evaluation = evaluate_headroom(
        profile=profile,
        target_level_db_spl=97.0,
        frequency_hz=60.0,
        max_distortion_percent=5.0,
    )
    # only the 95 dB sample meets the 5% THD policy
    assert evaluation.available_level_db_spl == 95.0
    assert evaluation.status == 'FAIL'
    assert evaluation.basis == 'distortion_qualified'


def test_excursion_evidence_retained_without_spl_derivation():
    profile = build_source_usable_output_profile(
        excursion_evidence=(
            ExcursionEvidence(
                kind='xmax', value_mm=9.5, direction='one-way',
                provenance=_provenance(),
            ),
        ),
        **_base_kwargs(),
    )
    assert usable_output_tier(profile) == 'unknown'
    assert profile.excursion_evidence[0].value_mm == 9.5


def test_limiter_state_opaque():
    profile = build_source_usable_output_profile(
        limiter_state=LimiterState(
            present=True, policy_label='level-lok', opaque=True
        ),
        samples=(
            OutputSample(frequency_hz=1000.0, level_db_spl=95.0,
                         limiter_engaged=True),
        ),
        **_base_kwargs(),
    )
    assert profile.limiter_state.present is True
    assert profile.limiter_state.opaque is True


def test_profile_hash_integrity():
    profile = build_source_usable_output_profile(
        samples=(
            OutputSample(frequency_hz=1000.0, level_db_spl=95.0),
        ),
        **_base_kwargs(),
    )
    payload = profile.model_dump(mode='python')
    payload['environment'] = 'in-room'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        SourceUsableOutputProfile(**payload)


def test_raw_asset_and_parser_pairing():
    with pytest.raises(ValueError, match='supplied together'):
        build_source_usable_output_profile(
            raw_asset_sha256='a' * 64,  # missing parser_id
            **_base_kwargs(),
        )
    ok = build_source_usable_output_profile(
        raw_asset_sha256='a' * 64,
        parser_id='cea2010-csv-v1',
        **_base_kwargs(),
    )
    assert ok.parser_id == 'cea2010-csv-v1'
