"""Regression tests for the #580 background-noise metric authority.

Fixture map (issue §14): BN10 steady HVAC on NC-30 contour; BN20 NCB
distinct from NC on identical evidence; BN30 revision immutability;
BN40 fluctuating LF -> out-of-scope/limited; BN50 operating-state
gating; BN60 localized-source worst-position visibility; BN70 guard-band
threshold semantics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_room_noise_metrics import (
    BackgroundNoiseMeasurement,
    NC_2019_CURVES,
    NCB_1989_CURVES,
    NoiseAcquisitionContext,
    NoiseBandLevels,
    NoiseOperatingState,
    NoisePositionSpectrum,
    S12_2_OCTAVE_BANDS_HZ,
    assert_no_scalar_substitution,
    build_noise_measurement,
    evaluate_noise_metric,
    profile_for_rp22,
    seed_room_noise_metric_profiles,
)
from htdt.cad_room_noise_metrics_repository import (
    CadRoomNoiseMetricRepository,
    RoomNoiseMetricConflictError,
    RoomNoiseMetricIntegrityError,
)


_DOC = 'doc-test'
_NC30 = dict(NC_2019_CURVES)['NC-30']
_NCB30 = dict(NCB_1989_CURVES)['NCB-30']


def _spectrum(
    levels: tuple[float, ...],
    *,
    semantics: str = 'absolute_spl',
    a_db: float | None = 31.5,
) -> NoiseBandLevels:
    return NoiseBandLevels(
        band_center_hz=S12_2_OCTAVE_BANDS_HZ,
        band_level_db=levels,
        band_spec='octave',
        level_semantics=semantics,
        a_weighted_level_db=a_db,
    )


def _measurement(
    levels: tuple[float, ...] = _NC30,
    *,
    temporal_class: str = 'steady_state',
    operating_state: NoiseOperatingState | None = None,
    positions: tuple[NoisePositionSpectrum, ...] | None = None,
    uncertainty_db: float | None = None,
    label: str = 'bn-fixture',
) -> BackgroundNoiseMeasurement:
    if positions is None:
        positions = (
            NoisePositionSpectrum(
                position_label='mlp',
                spectrum=_spectrum(levels),
            ),
        )
    return build_noise_measurement(
        document_id=_DOC,
        measurement_label=label,
        acquisition=NoiseAcquisitionContext(
            instrument_id='slm-1',
            instrument_class='class1',
            calibration_ref='cal-2026-01',
            method='integrated_leq',
        ),
        positions=positions,
        temporal_class=temporal_class,
        operating_state=operating_state
        or NoiseOperatingState(
            hvac='on',
            projector='on',
            rack_fans='off',
            avr_equipment='on',
            occupancy='occupied',
            doors_windows='closed',
        ),
        uncertainty_db=uncertainty_db,
        captured_at_utc='2026-10-01T00:00:00+00:00',
    )


def _profiles():
    return seed_room_noise_metric_profiles(
        document_id=_DOC, created_at_utc='2026-10-01T00:00:00+00:00'
    )


def _profile_by_family(profiles, family: str, edition: str | None = None):
    for profile in profiles:
        if profile.metric_family == family and (
            edition is None or profile.standard_edition == edition
        ):
            return profile
    raise AssertionError(f'no seeded {family}/{edition} profile')


# ---------------------------------------------------------------------------
# BN10 — steady HVAC under the 2026 NC profile
# ---------------------------------------------------------------------------


def test_seeded_profiles_cover_all_families() -> None:
    profiles = _profiles()
    families = {p.metric_family for p in profiles}
    assert {
        'nc', 'ncb', 'rc_mark_ii', 'rnc', 'a_weighted_level'
    } <= families
    statuses = {p.profile_label: p.status for p in profiles}
    assert statuses['ANSI/ASA S12.2-2026 NC curves'] == 'profile_current'
    ncb = _profile_by_family(profiles, 'ncb')
    assert ncb.status == (
        'profile_superseded_but_required_by_external_standard'
    )
    assert 'cedia-cta-rp22@v1.2' in ncb.external_standard_refs


def test_bn10_nc_rating_on_nc30_shaped_spectrum() -> None:
    """An NC-30-contour spectrum rates NC-<round(SIL)> under the SIL
    designation step — the SIL of the NC-30 contour at 500/1k/2k/4k is
    (35+32+29+28)/4 = 31.0 -> 'NC-31', the documented quirk of the
    two-step method."""
    nc = _profile_by_family(_profiles(), 'nc', '2026')
    evaluation = evaluate_noise_metric(_measurement(), nc)
    assert evaluation.applicability == 'in_scope'
    assert evaluation.rating_label == 'NC-31'
    assert evaluation.rating_value == pytest.approx(31.0)
    assert evaluation.aggregate_basis == 'worst_position'
    assert evaluation.position_results[0].method == 'sil_designation'


def test_nc_tangency_names_the_governing_band() -> None:
    """A 125 Hz excess drives the tangency rating and stays visible."""
    nc = _profile_by_family(_profiles(), 'nc', '2026')
    levels = list(_NC30)
    levels[3] = 60.0  # 125 Hz well above the NC-34 curve (~51.2)
    evaluation = evaluate_noise_metric(
        _measurement(tuple(levels)), nc
    )
    assert evaluation.applicability == 'in_scope'
    assert evaluation.position_results[0].method == 'tangency'
    assert evaluation.governing_band_hz == 125.0
    assert evaluation.rating_label == 'NC-45 (125 Hz)'


# ---------------------------------------------------------------------------
# BN20 — NCB is a distinct family, never an NC relabel
# ---------------------------------------------------------------------------


def test_bn20_ncb_distinct_from_nc_on_same_evidence() -> None:
    profiles = _profiles()
    nc = _profile_by_family(profiles, 'nc', '2026')
    ncb = _profile_by_family(profiles, 'ncb')
    measurement = _measurement()
    nc_eval = evaluate_noise_metric(measurement, nc)
    ncb_eval = evaluate_noise_metric(measurement, ncb)
    assert ncb_eval.metric_family == 'ncb'
    assert ncb_eval.rating_label.startswith('NCB-')
    assert ncb_eval.evaluation_id != nc_eval.evaluation_id
    with pytest.raises(ValueError, match='substitution'):
        assert_no_scalar_substitution(ncb_eval, claimed_family='nc')
    # The guard is quiet only for the honest family.
    assert_no_scalar_substitution(ncb_eval, claimed_family='ncb')


def test_ncb_imbalance_qualifiers_surface_rumble() -> None:
    ncb = _profile_by_family(_profiles(), 'ncb')
    levels = list(_NCB30)
    levels[1] += 10.0  # 31.5 Hz rumble beyond the +3 dB rule
    measurement = _measurement(tuple(levels))
    evaluation = evaluate_noise_metric(measurement, ncb)
    assert 'rumble' in evaluation.qualifiers


# ---------------------------------------------------------------------------
# BN30 — evaluation records are immutable derived rows
# ---------------------------------------------------------------------------


def test_bn30_repository_rejects_profile_or_mutation_rewrite(
    tmp_path: Path,
) -> None:
    repository = CadRoomNoiseMetricRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profiles = _profiles()
    nc = _profile_by_family(profiles, 'nc', '2026')
    repository.save_profile(nc)
    repository.save_profile(nc)  # identical re-save is a no-op
    assert repository.get_profile(nc.profile_id) == nc

    measurement = _measurement()
    repository.save_measurement(measurement)
    repository.save_measurement(measurement)
    assert (
        repository.get_measurement(measurement.measurement_id)
        == measurement
    )

    evaluation = evaluate_noise_metric(measurement, nc)
    repository.save_evaluation(evaluation)
    repository.save_evaluation(evaluation)
    assert (
        repository.get_evaluation(evaluation.evaluation_id)
        == evaluation
    )

    # Commit order: an evaluation cannot precede its evidence/profile.
    orphan_repo = CadRoomNoiseMetricRepository(
        SceneRepository(tmp_path / 'other.sqlite3')
    )
    with pytest.raises(RoomNoiseMetricIntegrityError):
        orphan_repo.save_evaluation(evaluation)

    # A forged reuse (stale sha over changed payload) is rejected —
    # model_copy bypasses validators, so the repository re-verifies the
    # seal before appending.
    forged = evaluation.model_copy(update={'rating_label': 'NC-99'})
    with pytest.raises(RoomNoiseMetricIntegrityError):
        repository.save_evaluation(forged)


# ---------------------------------------------------------------------------
# BN40 — fluctuating LF noise is outside the NC/NCB scope, and RNC
# fails closed without its registered curve table / LF corrections
# ---------------------------------------------------------------------------


def test_bn40_fluctuating_lf_outside_nc_scope() -> None:
    nc = _profile_by_family(_profiles(), 'nc', '2026')
    evaluation = evaluate_noise_metric(
        _measurement(temporal_class='low_frequency_fluctuating'), nc
    )
    assert evaluation.applicability == 'out_of_scope'
    assert evaluation.rating_label is None
    assert any(
        'temporal_class_low_frequency_fluctuating' in r
        for r in evaluation.applicability_reasons
    )


def test_bn40_rnc_requires_time_domain_corrections() -> None:
    rnc = _profile_by_family(_profiles(), 'rnc')
    measurement = _measurement(
        temporal_class='low_frequency_fluctuating'
    )
    evaluation = evaluate_noise_metric(measurement, rnc)
    assert evaluation.applicability == 'out_of_scope'
    assert evaluation.position_results[0].applicability_reasons == (
        'rnc_time_domain_correction_missing',
    )

    with_corrections = _measurement(
        temporal_class='low_frequency_fluctuating',
        positions=(
            NoisePositionSpectrum(
                position_label='mlp',
                spectrum=NoiseBandLevels(
                    band_center_hz=S12_2_OCTAVE_BANDS_HZ,
                    band_level_db=_NC30,
                    band_spec='octave',
                    level_semantics='absolute_spl',
                    lf_time_domain_correction_db={
                        '31.5': 2.0, '63': 1.0
                    },
                ),
            ),
        ),
    )
    evaluation = evaluate_noise_metric(with_corrections, rnc)
    # Curve table intentionally unregistered -> honest LIMITED.
    assert evaluation.applicability == 'limited'
    assert 'rnc_curve_table_not_registered' in (
        evaluation.position_results[0].applicability_reasons
    )


# ---------------------------------------------------------------------------
# BN50 — operating state is mandatory for qualified claims
# ---------------------------------------------------------------------------


def test_bn50_undeclared_operating_state_limits_the_claim() -> None:
    nc = _profile_by_family(_profiles(), 'nc', '2026')
    measurement = _measurement(
        operating_state=NoiseOperatingState()  # all 'unknown'
    )
    evaluation = evaluate_noise_metric(measurement, nc)
    assert evaluation.applicability == 'limited'
    assert 'operating_state_undeclared' in evaluation.applicability_reasons

    qualified = evaluate_noise_metric(_measurement(), nc)
    assert qualified.applicability == 'in_scope'


# ---------------------------------------------------------------------------
# BN60 — worst position wins; localized sources stay visible
# ---------------------------------------------------------------------------


def test_bn60_worst_position_drives_the_rating() -> None:
    nc = _profile_by_family(_profiles(), 'nc', '2026')
    loud_corner = list(_NC30)
    loud_corner[2] = 75.0  # 63 Hz bump at the corner position only
    measurement = _measurement(
        positions=(
            NoisePositionSpectrum(
                position_label='mlp', spectrum=_spectrum(_NC30)
            ),
            NoisePositionSpectrum(
                position_label='rear-corner',
                role='listening_area',
                spectrum=_spectrum(tuple(loud_corner)),
            ),
        ),
    )
    evaluation = evaluate_noise_metric(measurement, nc)
    rear = evaluation.position_results[1]
    assert rear.method == 'tangency'
    assert evaluation.rating_label == rear.rating_label
    assert any(
        lim.startswith('spatial_spread_') for lim in evaluation.limitations
    )


# ---------------------------------------------------------------------------
# BN70 — guard-band threshold verdicts (#577 compose)
# ---------------------------------------------------------------------------


def test_bn70_threshold_guard_band() -> None:
    nc = _profile_by_family(_profiles(), 'nc', '2026')
    measurement = _measurement()  # rates NC-31 (value 31.0)

    clear_fail = evaluate_noise_metric(measurement, nc, target_rating=25)
    assert clear_fail.threshold_verdict == 'fail'

    guarded = evaluate_noise_metric(
        measurement, nc, target_rating=32, uncertainty_db=2.0
    )
    assert guarded.threshold_verdict == 'indeterminate_guard_band'
    assert guarded.threshold_uncertainty_db == 2.0

    clear_pass = evaluate_noise_metric(measurement, nc, target_rating=40)
    assert clear_pass.threshold_verdict == 'pass'

    no_target = evaluate_noise_metric(measurement, nc)
    assert no_target.threshold_verdict == 'not_evaluated'


def test_rc_mark_ii_and_dba_profiles_rate_honestly() -> None:
    profiles = _profiles()
    rc = _profile_by_family(profiles, 'rc_mark_ii')
    dba = _profile_by_family(profiles, 'a_weighted_level')

    measurement = _measurement()
    rc_eval = evaluate_noise_metric(measurement, rc)
    assert rc_eval.applicability == 'in_scope'
    assert rc_eval.rating_label.startswith('RC-')
    assert '(N)' in rc_eval.rating_label or '(' in rc_eval.rating_label

    dba_eval = evaluate_noise_metric(measurement, dba)
    assert dba_eval.applicability == 'in_scope'
    assert dba_eval.rating_label == '31.5 dBA'
    assert dba_eval.metric_family == 'a_weighted_level'
    # dBA stays a *separate* metric — never a criterion rating.
    with pytest.raises(ValueError, match='substitution'):
        assert_no_scalar_substitution(dba_eval, claimed_family='nc')


def test_relative_level_evidence_is_limited_not_rated() -> None:
    nc = _profile_by_family(_profiles(), 'nc', '2026')
    relative = build_noise_measurement(
        document_id=_DOC,
        acquisition=NoiseAcquisitionContext(method='integrated_leq'),
        positions=(
            NoisePositionSpectrum(
                position_label='mlp',
                spectrum=_spectrum(_NC30, semantics='relative'),
            ),
        ),
        temporal_class='steady_state',
        operating_state=NoiseOperatingState(
            hvac='on', projector='on', occupancy='occupied'
        ),
        captured_at_utc='2026-10-01T00:00:00+00:00',
    )
    evaluation = evaluate_noise_metric(relative, nc)
    assert evaluation.applicability == 'limited'
    assert evaluation.position_results[0].applicability_reasons == (
        'level_semantics_not_absolute_spl',
    )


def test_profile_for_rp22_returns_the_ncb_profile() -> None:
    profiles = _profiles()
    rp22 = profile_for_rp22(profiles)
    assert rp22 is not None
    assert rp22.metric_family == 'ncb'
    assert rp22.standard_edition == '1995'


def test_measurement_id_is_content_derived() -> None:
    a = _measurement()
    b = _measurement()
    assert a.measurement_id == b.measurement_id
    c = _measurement(temporal_class='intermittent')
    assert c.measurement_id != a.measurement_id
