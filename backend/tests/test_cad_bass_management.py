"""Bass-management authority tests (#633)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_bass_management import (
    BassManagementProfile,
    CrossoverSpec,
    FrequencyBand,
    LFEPathRule,
    MainChannelBassRule,
    SubwooferOutputGroup,
    VendorBassMapping,
    bass_management_status,
    build_bass_management_profile,
    evaluate_bass_management,
    routing_edges,
)
from htdt.cad_equipment import EquipmentDataProvenance


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='measured',
            source_name='avr-readback',
            source_version='fw 1.9',
            source_reference='denon x4800 web ui',
            source_sha256='6' * 64,
        ),
    )


def _groups():
    return (
        SubwooferOutputGroup(
            group_id='sub-out-1',
            grouping='single_output_multi_sub',
            output_channel_ref='subwoofer_preout_1',
            member_sub_ids=('sub-front', 'sub-rear'),
        ),
        SubwooferOutputGroup(
            group_id='sub-out-2',
            grouping='independent_output',
            output_channel_ref='subwoofer_preout_2',
            member_sub_ids=('sub-mid',),
        ),
    )


def _profile(**overrides):
    kwargs = dict(
        profile_id='bm-main',
        version='1',
        processor_ref='denon-x4800',
        lifecycle='current',
        main_rules=(
            MainChannelBassRule(
                logical_role_id='L',
                handling='high_pass',
                high_pass=CrossoverSpec(
                    frequency_hz=80.0,
                    slope_db_per_octave=24.0,
                    filter_family='linkwitz_riley',
                ),
                redirected_low_band=FrequencyBand(
                    low_hz=20.0, high_hz=80.0
                ),
                redirected_destinations=('sub-out-1',),
                provenance=_provenance(),
            ),
            MainChannelBassRule(
                logical_role_id='R',
                handling='high_pass',
                high_pass=CrossoverSpec(
                    frequency_hz=80.0,
                    slope_db_per_octave=24.0,
                    filter_family='linkwitz_riley',
                ),
                redirected_destinations=('sub-out-1',),
                provenance=_provenance(),
            ),
            MainChannelBassRule(
                logical_role_id='C',
                handling='full_range',
                provenance=_provenance(),
            ),
            MainChannelBassRule(
                logical_role_id='SL',
                handling='unknown',
                provenance=_provenance(),
            ),
        ),
        lfe_path=LFEPathRule(
            lfe_input_id='lfe-in',
            low_pass=CrossoverSpec(
                frequency_hz=120.0, filter_family='unknown'
            ),
            gain_reference_db=0.0,
            in_band_boost_db=10.0,
            destinations=('sub-out-1', 'sub-out-2'),
            duplication_policy='shared',
        ),
        sub_groups=_groups(),
        vendor_mappings=(
            VendorBassMapping(
                vendor_setting='small',
                applies_to_role_id='L',
                canonical_handling='high_pass',
                canonical_redirected_destinations=('sub-out-1',),
                adapter_id='denon-avr-adapter',
                adapter_version='1.0',
                verified=True,
            ),
        ),
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_bass_management_profile(**kwargs)


def test_profile_hash_and_duplicate_roles():
    profile = _profile()
    assert profile.profile_sha256
    payload = profile.model_dump(mode='python')
    payload['lifecycle'] = 'applied'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        BassManagementProfile(**payload)
    with pytest.raises(ValueError, match='at most one bass rule'):
        _profile(
            main_rules=(
                MainChannelBassRule(
                    logical_role_id='L', handling='unknown'
                ),
                MainChannelBassRule(
                    logical_role_id='L', handling='full_range'
                ),
            )
        )


def test_high_pass_rule_requires_crossover():
    with pytest.raises(ValueError, match='crossover spec'):
        MainChannelBassRule(
            logical_role_id='L', handling='high_pass'
        )
    with pytest.raises(ValueError, match='full_range'):
        MainChannelBassRule(
            logical_role_id='L',
            handling='full_range',
            high_pass=CrossoverSpec(frequency_hz=80.0),
        )


def test_unknown_filter_not_invented():
    spec = CrossoverSpec(frequency_hz=80.0)
    assert spec.filter_family == 'unknown'
    assert spec.slope_db_per_octave is None


def test_routing_edges():
    edges = routing_edges(_profile())
    as_set = {(e.source_id, e.destination_id, e.band) for e in edges}
    assert ('L', 'sub-out-1', 'redirected_low') in as_set
    assert ('R', 'sub-out-1', 'redirected_low') in as_set
    assert ('C', 'C', 'full_band') in as_set
    assert ('lfe-in', 'sub-out-1', 'lfe') in as_set
    assert ('lfe-in', 'sub-out-2', 'lfe') in as_set


def test_evaluation_coverage_and_resolution():
    evaluation = evaluate_bass_management(
        profile=_profile(),
        expected_role_ids=('L', 'R', 'C', 'SL', 'SR'),
        known_destination_ids=('sub-out-1', 'sub-out-2'),
    )
    checks = {
        (c.check, c.role_id): c.status for c in evaluation.checks
    }
    assert checks[('role_coverage', None)] == 'FAIL'  # SR missing
    assert checks[('destinations_resolve', 'L')] == 'PASS'
    assert checks[('high_pass_recorded', 'SL')] == 'UNKNOWN'
    assert checks[('lfe_path_independent', None)] == 'PASS'


def test_evaluation_passes_full_coverage():
    evaluation = evaluate_bass_management(
        profile=_profile(),
        expected_role_ids=('L', 'R', 'C', 'SL'),
    )
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['role_coverage'] == 'PASS'
    assert bass_management_status(evaluation) in ('PASS', 'UNKNOWN')
    # SL is UNKNOWN -> combined status must not collapse to PASS
    assert bass_management_status(evaluation) == 'UNKNOWN'


def test_unresolved_destination_fails():
    profile = _profile(
        main_rules=(
            MainChannelBassRule(
                logical_role_id='L',
                handling='high_pass',
                high_pass=CrossoverSpec(frequency_hz=80.0),
                redirected_destinations=('nowhere',),
            ),
        ),
        lfe_path=None,
    )
    evaluation = evaluate_bass_management(profile=profile)
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['destinations_resolve'] == 'FAIL'
    assert checks['lfe_path_independent'] == 'UNKNOWN'


def test_lfe_group_requires_dsp_ref():
    with pytest.raises(ValueError, match='DSP ref'):
        SubwooferOutputGroup(
            group_id='g', grouping='external_dsp'
        )


def test_vendor_mapping_is_adapter_record():
    profile = _profile()
    mapping = profile.vendor_mappings[0]
    assert mapping.adapter_id == 'denon-avr-adapter'
    assert mapping.verified is True
    # vendor term stays a string, canonical meaning explicit
    assert mapping.vendor_setting == 'small'
    assert mapping.canonical_handling == 'high_pass'
