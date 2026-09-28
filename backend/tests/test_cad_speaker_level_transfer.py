"""Speaker-level electrical path transfer tests (#1004)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    FrequencyDomain,
    Offset3,
    Size3,
    build_equipment_definition,
)
from htdt.cad_speaker_impedance import (
    ImpedanceSample,
    build_speaker_impedance_authority,
)
from htdt.cad_speaker_level_transfer import (
    AmplifierOutputImpedanceAuthority,
    build_amplifier_output_impedance,
    build_speaker_cable_electrical_profile,
    build_speaker_electrical_path,
    evaluate_speaker_level_transfer,
)


DOMAIN = FrequencyDomain(minimum_hz=20.0, maximum_hz=20000.0)


def _provenance(name: str = 'src', digit: str = '1'):
    return EquipmentDataProvenance(
        evidence_kind='manufacturer',
        source_name=name,
        source_version='1',
        source_reference='datasheet',
        source_sha256=digit * 64,
    )


def _definition(definition_id: str = 'spk-1'):
    provenance = _provenance(definition_id, '2')
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown', data_format='unknown', provenance=provenance
        ),
    )


def _complex_load(impedance_id: str = 'load-1', definition_id='spk-1'):
    return build_speaker_impedance_authority(
        impedance_id=impedance_id,
        version='1',
        equipment_definition=_definition(definition_id),
        tier='complex_curve',
        nominal_impedance_ohm=8.0,
        minimum_impedance_ohm=4.0,
        minimum_frequency_hz=1000.0,
        samples=(
            ImpedanceSample(frequency_hz=20.0, real_ohm=8.0, imag_ohm=0.0),
            ImpedanceSample(frequency_hz=1000.0, real_ohm=4.0, imag_ohm=2.0),
            ImpedanceSample(
                frequency_hz=20000.0, real_ohm=12.0, imag_ohm=4.0
            ),
        ),
        interpolation='linear',
        valid_frequency_domain=DOMAIN,
        provenance=_provenance(impedance_id, '3'),
    )


def _magnitude_load(impedance_id: str = 'load-mag'):
    return build_speaker_impedance_authority(
        impedance_id=impedance_id,
        version='1',
        equipment_definition=_definition('spk-mag'),
        tier='magnitude_only_curve',
        nominal_impedance_ohm=8.0,
        minimum_impedance_ohm=4.0,
        minimum_frequency_hz=1000.0,
        samples=(
            ImpedanceSample(frequency_hz=20.0, magnitude_ohm=8.0),
            ImpedanceSample(frequency_hz=20000.0, magnitude_ohm=10.0),
        ),
        interpolation='linear',
        valid_frequency_domain=DOMAIN,
        provenance=_provenance(impedance_id, '4'),
    )


def _nominal_load(impedance_id: str = 'load-nom'):
    return build_speaker_impedance_authority(
        impedance_id=impedance_id,
        version='1',
        equipment_definition=_definition('spk-nom'),
        tier='nominal_impedance_only',
        nominal_impedance_ohm=8.0,
        valid_frequency_domain=DOMAIN,
        provenance=_provenance(impedance_id, '5'),
    )


def _amp(**overrides):
    kwargs = dict(
        impedance_id='amp-out-1',
        version='1',
        amplifier_ref='amp-1',
        tier='scalar',
        scalar_ohm=0.05,
        provenance=(_provenance('amp-out-1', '6'),),
    )
    kwargs.update(overrides)
    return build_amplifier_output_impedance(**kwargs)


def _cable(**overrides):
    kwargs = dict(
        cable_profile_id='cable-12awg',
        version='1',
        material='copper',
        conductor_gauge='12 AWG',
        series_resistance_ohm_per_m=0.0052,
        provenance=(_provenance('cable-12awg', '7'),),
    )
    kwargs.update(overrides)
    return build_speaker_cable_electrical_profile(**kwargs)


def _path(**overrides):
    kwargs = dict(
        path_id='path-1',
        version='1',
        amplifier_impedance_id='amp-out-1',
        cable_profile_id='cable-12awg',
        cable_run_id='run-1',
        cable_length_m=8.0,
        load_topology='single',
        load_impedance_ids=('load-1',),
        provenance=(_provenance('path-1', '8'),),
    )
    kwargs.update(overrides)
    return build_speaker_electrical_path(**kwargs)


def test_damping_factor_derived_keeps_reference():
    amp = _amp(
        tier='damping_factor_derived',
        scalar_ohm=None,
        damping_factor=400.0,
        damping_factor_reference_load_ohm=8.0,
        damping_factor_reference_frequency_hz=50.0,
    )
    assert amp.magnitude_at(50.0) == pytest.approx(0.02)
    # the record keeps what the DF figure was published against
    assert amp.damping_factor_reference_load_ohm == 8.0
    assert amp.damping_factor_reference_frequency_hz == 50.0
    assert not amp.supports_complex_transfer


def test_damping_factor_derived_resolves_only_at_reference_frequency():
    # The DF figure is exact at its declared reference frequency — it is
    # not a broadband constant, so off-reference lookups fail closed.
    amp = _amp(
        tier='damping_factor_derived',
        scalar_ohm=None,
        damping_factor=400.0,
        damping_factor_reference_load_ohm=8.0,
        damping_factor_reference_frequency_hz=50.0,
    )
    assert amp.magnitude_at(50.0) == pytest.approx(0.02)
    assert amp.magnitude_at(49.9) is None
    assert amp.magnitude_at(100.0) is None
    assert amp.magnitude_at(20000.0) is None

    result = evaluate_speaker_level_transfer(
        path=_path(load_impedance_ids=('load-mag',)),
        amplifier_impedance=amp,
        cable_profile=_cable(),
        load_impedances=(_magnitude_load(),),
        frequencies_hz=(40.0, 50.0, 60.0),
    )
    assert result.status == 'computed_magnitude'
    assert len(result.points) == 1
    assert result.points[0].frequency_hz == pytest.approx(50.0)
    assert sum(
        'not defined at this frequency' in reason
        for reason in result.reasons
    ) == 2


def test_declared_nearest_interpolation_is_honored():
    nearest_amp = _amp(
        tier='magnitude_curve',
        scalar_ohm=None,
        samples=(
            ImpedanceSample(frequency_hz=100.0, magnitude_ohm=8.0),
            ImpedanceSample(frequency_hz=200.0, magnitude_ohm=4.0),
        ),
        interpolation='nearest',
    )
    assert nearest_amp.magnitude_at(160.0) == pytest.approx(4.0)
    assert nearest_amp.magnitude_at(140.0) == pytest.approx(8.0)
    assert nearest_amp.magnitude_at(150.0) == pytest.approx(8.0)  # tie -> lower

    linear_amp = _amp(
        tier='magnitude_curve',
        scalar_ohm=None,
        samples=(
            ImpedanceSample(frequency_hz=100.0, magnitude_ohm=8.0),
            ImpedanceSample(frequency_hz=200.0, magnitude_ohm=4.0),
        ),
        interpolation='linear',
    )
    assert linear_amp.magnitude_at(160.0) == pytest.approx(5.6)
    assert linear_amp.magnitude_at(150.0) == pytest.approx(6.0)


def test_complex_curve_declared_nearest_interpolation_is_honored():
    amp = _amp(
        tier='complex_curve',
        scalar_ohm=None,
        samples=(
            ImpedanceSample(frequency_hz=100.0, real_ohm=8.0, imag_ohm=0.0),
            ImpedanceSample(frequency_hz=200.0, real_ohm=4.0, imag_ohm=2.0),
        ),
        interpolation='nearest',
    )
    assert amp.complex_at(160.0) == pytest.approx(complex(4.0, 2.0))
    assert amp.magnitude_at(160.0) == pytest.approx(
        abs(complex(4.0, 2.0))
    )

    nearest_load = build_speaker_impedance_authority(
        impedance_id='load-near',
        version='1',
        equipment_definition=_definition('spk-near'),
        tier='complex_curve',
        nominal_impedance_ohm=8.0,
        minimum_impedance_ohm=4.0,
        minimum_frequency_hz=1000.0,
        samples=(
            ImpedanceSample(frequency_hz=100.0, real_ohm=8.0, imag_ohm=0.0),
            ImpedanceSample(frequency_hz=200.0, real_ohm=4.0, imag_ohm=2.0),
        ),
        interpolation='nearest',
        valid_frequency_domain=DOMAIN,
        provenance=_provenance('load-near', '5'),
    )
    result = evaluate_speaker_level_transfer(
        path=_path(load_impedance_ids=('load-near',)),
        amplifier_impedance=_amp(scalar_ohm=1.0),
        cable_profile=_cable(series_resistance_ohm_per_m=0.0),
        load_impedances=(nearest_load,),
        frequencies_hz=(160.0,),
        source_voltage_v=1.0,
    )
    assert result.status == 'computed_complex'
    assert len(result.points) == 1
    # nearest sample at 160 Hz is the 200 Hz point: z_load = 4 + 2j;
    # linear would produce z_load = 5.6 + 1.2j and a different transfer.
    transfer = complex(4.0, 2.0) / complex(5.0, 2.0)
    assert result.points[0].voltage_transfer_real == pytest.approx(
        transfer.real
    )
    assert result.points[0].voltage_transfer_imag == pytest.approx(
        transfer.imag
    )


def test_amplifier_tier_field_contracts():
    with pytest.raises(ValidationError):
        _amp(tier='scalar', scalar_ohm=None)
    with pytest.raises(ValidationError, match='damping factor'):
        _amp(tier='scalar', damping_factor=200.0)
    with pytest.raises(ValidationError):
        _amp(tier='complex_curve', samples=())


def test_cable_profile_requires_resistance():
    with pytest.raises(ValidationError, match='series resistance'):
        build_speaker_cable_electrical_profile(
            cable_profile_id='c',
            series_resistance_ohm_per_m=None,
        )


def test_path_topology_is_explicit():
    with pytest.raises(ValidationError, match='exactly one'):
        _path(load_topology='single', load_impedance_ids=('a', 'b'))
    with pytest.raises(ValidationError, match='at least two'):
        _path(load_topology='parallel', load_impedance_ids=('a',))
    with pytest.raises(ValidationError, match='unknown topology'):
        _path(load_topology='unknown', load_impedance_ids=('a',))


def test_complex_transfer_computes():
    result = evaluate_speaker_level_transfer(
        path=_path(),
        amplifier_impedance=_amp(),
        cable_profile=_cable(),
        load_impedances=(_complex_load(),),
        frequencies_hz=(100.0, 1000.0),
        source_voltage_v=20.0,
    )
    assert result.status == 'computed_complex'
    assert len(result.points) == 2
    point = result.points[0]
    assert point.voltage_transfer_real is not None
    assert point.voltage_transfer_db < 0.0  # cable+amp drop
    assert point.terminal_current_a > 0.0


def test_nominal_only_load_cannot_compute_exact_transfer():
    result = evaluate_speaker_level_transfer(
        path=_path(load_impedance_ids=('load-nom',)),
        amplifier_impedance=_amp(),
        cable_profile=_cable(),
        load_impedances=(_nominal_load(),),
        frequencies_hz=(100.0,),
    )
    assert result.status == 'not_computable'
    assert not result.points
    assert any('nominal' in reason for reason in result.reasons)


def test_unknown_topology_is_not_computable():
    path = build_speaker_electrical_path(
        path_id='p-unk',
        amplifier_impedance_id='amp-out-1',
        cable_profile_id='cable-12awg',
        cable_length_m=8.0,
        load_topology='unknown',
        provenance=(_provenance('p', '9'),),
    )
    result = evaluate_speaker_level_transfer(
        path=path,
        amplifier_impedance=_amp(),
        cable_profile=_cable(),
        load_impedances=(_complex_load(),),
        frequencies_hz=(100.0,),
    )
    assert result.status == 'not_computable'
    assert any('topology' in reason for reason in result.reasons)


def test_series_and_parallel_topologies_compute():
    load_a = _complex_load(impedance_id='load-1')
    load_b = _complex_load(impedance_id='load-2', definition_id='spk-2')
    series = evaluate_speaker_level_transfer(
        path=_path(
            load_topology='series',
            load_impedance_ids=('load-1', 'load-2'),
        ),
        amplifier_impedance=_amp(),
        cable_profile=_cable(),
        load_impedances=(load_a, load_b),
        frequencies_hz=(100.0,),
        source_voltage_v=20.0,
    )
    parallel = evaluate_speaker_level_transfer(
        path=_path(
            path_id='path-par',
            load_topology='parallel',
            load_impedance_ids=('load-1', 'load-2'),
        ),
        amplifier_impedance=_amp(),
        cable_profile=_cable(),
        load_impedances=(load_a, load_b),
        frequencies_hz=(100.0,),
        source_voltage_v=20.0,
    )
    assert series.status == 'computed_complex'
    assert parallel.status == 'computed_complex'
    # series doubles the load → more transfer, less current than parallel
    assert (
        series.points[0].voltage_transfer_db
        > parallel.points[0].voltage_transfer_db
    )
    assert (
        series.points[0].terminal_current_a
        < parallel.points[0].terminal_current_a
    )


def test_magnitude_only_load_yields_magnitude_result():
    result = evaluate_speaker_level_transfer(
        path=_path(load_impedance_ids=('load-mag',)),
        amplifier_impedance=_amp(),
        cable_profile=_cable(),
        load_impedances=(_magnitude_load(),),
        frequencies_hz=(100.0,),
    )
    assert result.status == 'computed_magnitude'
    assert result.points[0].voltage_transfer_real is None


def test_unresolved_load_fails_closed():
    result = evaluate_speaker_level_transfer(
        path=_path(),
        amplifier_impedance=_amp(),
        cable_profile=_cable(),
        load_impedances=(),  # nothing supplied to resolve 'load-1'
        frequencies_hz=(100.0,),
    )
    assert result.status == 'not_computable'
    assert any('unresolved' in reason for reason in result.reasons)
