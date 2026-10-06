"""Regression tests for REV59-QUALNUM (#703/#716/#717)."""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_solver_reproducibility import (
    CrossPlatformNumericalComparison,
    NumericalReproducibilityProfile,
    StochasticRealizationRecord,
    evaluate_numerical_difference_claim,
)
from htdt.cad_imaging_chain import (
    CameraCalibrationProfile,
    CameraDerivedObservation,
    ImagingMeasurementChain,
    evaluate_imaging_evidence_claim,
)
from htdt.cad_wireless_av import (
    WirelessAVLink,
    WirelessSynchronizationEvidence,
    WirelessTransportObservation,
    evaluate_wireless_claim,
)


def _ref(rid: str = 'x-1') -> AuthorityRef:
    return AuthorityRef(
        kind='doc', ref_id=rid,
        ref_sha256='a' * 64,
    )


class TestNumericalReproducibility:
    def test_profile_creation(self) -> None:
        p = NumericalReproducibilityProfile.create({
            'document_id': 'doc-1',
            'solver_ref': _ref('solver-1'),
            'precision_kind': 'fp64',
            'parallelism_kind': 'gpu',
        })
        assert p.profile_id.startswith('nrep-')
        assert len(p.profile_sha256) == 64

    def test_pinned_order_requires_repro_ref(self) -> None:
        with pytest.raises(ValueError):
            NumericalReproducibilityProfile.create({
                'document_id': 'doc-1',
                'solver_ref': _ref(),
                'precision_kind': 'fp64',
                'parallelism_kind': 'threaded',
                'reduction_order_pinned': True,
            })

    def test_stochastic_requires_seed(self) -> None:
        with pytest.raises(ValueError):
            StochasticRealizationRecord.create({
                'document_id': 'doc-1',
                'profile_ref': _ref('prof'),
                'realization_kind': 'stochastic_realization',
                'result_ref': _ref('res'),
            })

    def test_realization_requires_result(self) -> None:
        with pytest.raises(ValueError):
            StochasticRealizationRecord.create({
                'document_id': 'doc-1',
                'profile_ref': _ref('prof'),
                'realization_kind': 'deterministic_run',
            })

    def _cmp(self) -> CrossPlatformNumericalComparison:
        return CrossPlatformNumericalComparison.create({
            'document_id': 'doc-1',
            'domain': 'same_platform_different_parallelism',
            'realization_refs': [_ref('r1'), _ref('r2'), _ref('r3')],
            'metric_deviations': {'spl_63hz_db': 0.4},
        })

    def test_comparison_needs_two_realizations(self) -> None:
        with pytest.raises(ValueError):
            CrossPlatformNumericalComparison.create({
                'document_id': 'doc-1',
                'domain': 'cross_platform',
                'realization_refs': [_ref('r1')],
                'metric_deviations': {'x': 0.1},
            })

    def test_not_comparable(self) -> None:
        v, _ = evaluate_numerical_difference_claim(
            1.0, None, 'spl_63hz_db')
        assert v == 'not_comparable'

    def test_uncharacterized_metric(self) -> None:
        v, _ = evaluate_numerical_difference_claim(
            1.0, self._cmp(), 'spl_125hz_db')
        assert v == 'insufficient_realizations'

    def test_within_variability(self) -> None:
        v, r = evaluate_numerical_difference_claim(
            0.2, self._cmp(), 'spl_63hz_db')
        assert v == 'within_numerical_variability'
        assert '0.4' in r

    def test_deterministic_needs_pinned_profile(self) -> None:
        profile = NumericalReproducibilityProfile.create({
            'document_id': 'doc-1',
            'solver_ref': _ref('s'),
            'precision_kind': 'fp64',
            'parallelism_kind': 'gpu',
            'seed_policy': 'unknown',
        })
        v, _ = evaluate_numerical_difference_claim(
            2.0, self._cmp(), 'spl_63hz_db', profile=profile)
        assert v == 'within_numerical_variability'

    def test_deterministic_order(self) -> None:
        profile = NumericalReproducibilityProfile.create({
            'document_id': 'doc-1',
            'solver_ref': _ref('s'),
            'precision_kind': 'fp64',
            'parallelism_kind': 'serial',
            'seed_policy': 'fixed',
        })
        v, _ = evaluate_numerical_difference_claim(
            2.0, self._cmp(), 'spl_63hz_db', profile=profile)
        assert v == 'deterministic_order'

    def test_ensemble_margin(self) -> None:
        cmp_ = CrossPlatformNumericalComparison.create({
            'document_id': 'doc-1',
            'domain': 'cross_platform',
            'realization_refs': [_ref('a'), _ref('b')],
            'metric_deviations': {'m': 0.3},
            'ensemble_uncertainty': {'m': 0.5},
        })
        v, _ = evaluate_numerical_difference_claim(0.7, cmp_, 'm')
        assert v == 'within_numerical_variability'


class TestImagingChain:
    def _chain(self, **kw) -> ImagingMeasurementChain:
        base = {
            'document_id': 'doc-1',
            'camera_ref': _ref('cam'),
            'shutter_kind': 'global',
            'exposure_control': 'manual',
            'white_balance_control': 'manual',
            'chain_state': 'qualified',
            'qualified_measurands': ('uniformity', 'photometric'),
            'geometry_alignment_ref': _ref('align'),
        }
        base.update(kw)
        return ImagingMeasurementChain.create(base)

    def _obs(self, **kw) -> CameraDerivedObservation:
        base = {
            'document_id': 'doc-1',
            'chain_ref': _ref('chain'),
            'measurand': 'uniformity',
            'processing_state': 'raw_sensor',
            'data_ref': _ref('data'),
        }
        base.update(kw)
        return CameraDerivedObservation.create(base)

    def test_chain_creation(self) -> None:
        c = self._chain()
        assert c.chain_id.startswith('imc-')

    def test_qualified_needs_alignment(self) -> None:
        with pytest.raises(ValueError):
            self._chain(geometry_alignment_ref=None)

    def test_observation_needs_evidence(self) -> None:
        with pytest.raises(ValueError):
            self._obs(data_ref=None)

    def test_unqualified_not_truth(self) -> None:
        c = self._chain(chain_state='unqualified',
                        qualified_measurands=())
        v, _ = evaluate_imaging_evidence_claim(c, self._obs())
        assert v == 'not_display_truth'

    def test_measurand_not_qualified(self) -> None:
        v, r = evaluate_imaging_evidence_claim(
            self._chain(),
            self._obs(measurand='resolution_mtf'))
        assert v == 'chain_limited'

    def test_auto_exposure_incomparable(self) -> None:
        v, _ = evaluate_imaging_evidence_claim(
            self._chain(exposure_control='auto'),
            self._obs())
        assert v == 'incomparable_auto_settings'

    def test_compressed_limited(self) -> None:
        v, _ = evaluate_imaging_evidence_claim(
            self._chain(),
            self._obs(processing_state='compressed'))
        assert v == 'chain_limited'

    def test_rolling_shutter_temporal(self) -> None:
        c = self._chain(shutter_kind='rolling',
                        qualified_measurands=('temporal_motion',))
        v, _ = evaluate_imaging_evidence_claim(
            c, self._obs(measurand='temporal_motion'))
        assert v == 'chain_limited'

    def test_mtf_calibration_gate(self) -> None:
        c = self._chain(qualified_measurands=('resolution_mtf',))
        cal = CameraCalibrationProfile.create({
            'document_id': 'doc-1',
            'chain_ref': _ref('chain'),
        })
        v, r = evaluate_imaging_evidence_claim(
            c, self._obs(measurand='resolution_mtf'), cal)
        assert v == 'chain_limited'
        assert 'MTF' in r

    def test_qualified(self) -> None:
        v, _ = evaluate_imaging_evidence_claim(
            self._chain(), self._obs())
        assert v == 'qualified_derived'


class TestWirelessAV:
    def _link(self, **kw) -> WirelessAVLink:
        base = {
            'document_id': 'doc-1',
            'transport_kind': 'wisa_ht',
            'endpoint_roles': ('speaker', 'subwoofer'),
        }
        base.update(kw)
        return WirelessAVLink.create(base)

    def _obs(self, **kw) -> WirelessTransportObservation:
        base = {
            'document_id': 'doc-1',
            'link_ref': _ref('link'),
            'data_ref': _ref('rf-data'),
        }
        base.update(kw)
        return WirelessTransportObservation.create(base)

    def test_claim_needs_provider_ref(self) -> None:
        with pytest.raises(ValueError):
            self._link(claimed_latency_ms=5.0)

    def test_routing_not_transport(self) -> None:
        v, _ = evaluate_wireless_claim(
            self._link(), None, routing_verified=True)
        assert v == 'routing_is_not_transport'

    def test_provider_claim_only(self) -> None:
        v, _ = evaluate_wireless_claim(
            self._link(claimed_latency_ms=5.0,
                       provider_claim_ref=_ref('wisa-spec')),
            None)
        assert v == 'provider_claim_only'

    def test_unmeasured(self) -> None:
        v, _ = evaluate_wireless_claim(self._link(), None)
        assert v == 'unmeasured_link'

    def test_dropout(self) -> None:
        v, r = evaluate_wireless_claim(
            self._link(), self._obs(dropout_events=3))
        assert v == 'dropout_evidence'
        assert '3' in r

    def test_sync_required(self) -> None:
        v, _ = evaluate_wireless_claim(
            self._link(), self._obs())
        assert v == 'sync_not_demonstrated'

    def test_qualified(self) -> None:
        sync = WirelessSynchronizationEvidence.create({
            'document_id': 'doc-1',
            'link_ref': _ref('link'),
            'max_inter_speaker_offset_ms': 0.05,
        })
        v, _ = evaluate_wireless_claim(
            self._link(), self._obs(), sync)
        assert v == 'qualified'

    def test_single_endpoint_no_sync_needed(self) -> None:
        v, _ = evaluate_wireless_claim(
            self._link(endpoint_roles=('subwoofer',)),
            self._obs())
        assert v == 'qualified'


# ---------------------------------------------------------------------
# Repository roundtrip / tamper / fresh-migrate

from pathlib import Path  # noqa: E402

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_schema import connect_sqlite, ensure_native_schema  # noqa: E402
from htdt.cad_numerical_transport_repository import (  # noqa: E402
    CadNumericalTransportRepository,
    NumericalTransportIntegrityError,
)


def _qn_repo(tmp_path: Path) -> CadNumericalTransportRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadNumericalTransportRepository(SceneRepository(db))


def test_qualnum_repository_roundtrip(tmp_path: Path) -> None:
    repo = _qn_repo(tmp_path)
    prof = NumericalReproducibilityProfile.create({
        'document_id': 'doc-rt',
        'solver_ref': _ref('solver'),
        'precision_kind': 'fp64',
        'parallelism_kind': 'gpu',
    })
    rez = StochasticRealizationRecord.create({
        'document_id': 'doc-rt',
        'profile_ref': AuthorityRef(
            kind='authority', ref_id=prof.profile_id,
            ref_sha256=prof.profile_sha256),
        'realization_kind': 'ensemble_member',
        'seed': 42,
        'result_ref': _ref('res'),
    })
    cmp_ = CrossPlatformNumericalComparison.create({
        'document_id': 'doc-rt',
        'domain': 'cross_platform',
        'realization_refs': [_ref('a'), _ref('b')],
        'metric_deviations': {'m': 0.3},
    })
    chain = ImagingMeasurementChain.create({
        'document_id': 'doc-rt',
        'camera_ref': _ref('cam'),
        'shutter_kind': 'global',
        'chain_state': 'partially_qualified',
    })
    obs = CameraDerivedObservation.create({
        'document_id': 'doc-rt',
        'chain_ref': _ref('chain'),
        'measurand': 'uniformity',
        'processing_state': 'raw_sensor',
        'data_ref': _ref('d'),
    })
    link = WirelessAVLink.create({
        'document_id': 'doc-rt',
        'transport_kind': 'bluetooth_le_audio',
        'endpoint_roles': ('speaker', 'headphones'),
    })
    wto = WirelessTransportObservation.create({
        'document_id': 'doc-rt',
        'link_ref': _ref('link'),
        'dropout_events': 0,
        'data_ref': _ref('rf'),
    })
    sync = WirelessSynchronizationEvidence.create({
        'document_id': 'doc-rt',
        'link_ref': _ref('link'),
        'max_inter_speaker_offset_ms': 0.1,
    })
    repo.save_repro_profile(prof)
    repo.save_realization(rez)
    repo.save_comparison(cmp_)
    repo.save_imaging_chain(chain)
    repo.save_camera_observation(obs)
    repo.save_wireless_link(link)
    repo.save_wireless_observation(wto)
    repo.save_wireless_sync(sync)
    assert repo.get_repro_profile(prof.profile_id) == prof
    assert repo.get_realization(rez.record_id) == rez
    assert repo.get_comparison(cmp_.comparison_id) == cmp_
    assert repo.get_imaging_chain(chain.chain_id) == chain
    assert repo.get_camera_observation(obs.observation_id) == obs
    assert repo.get_wireless_link(link.link_id) == link
    assert repo.get_wireless_observation(wto.observation_id) == wto
    assert repo.get_wireless_sync(sync.evidence_id) == sync


def test_qualnum_detects_column_tamper(tmp_path: Path) -> None:
    repo = _qn_repo(tmp_path)
    link = WirelessAVLink.create({
        'document_id': 'doc-t',
        'transport_kind': 'wifi',
        'endpoint_roles': ('speaker',),
    })
    repo.save_wireless_link(link)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_wireless_av_links '
            "SET transport_kind='wisa_ht' WHERE link_id=?",
            (link.link_id,),
        )
        connection.commit()
    import pytest as _pt
    with _pt.raises(NumericalTransportIntegrityError):
        repo.get_wireless_link(link.link_id)


def test_qualnum_tables_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_numerical_repro_profiles',
        'cad_stochastic_realizations',
        'cad_numerical_comparisons',
        'cad_imaging_measurement_chains',
        'cad_camera_calibrations',
        'cad_camera_derived_observations',
        'cad_wireless_av_links',
        'cad_wireless_transport_observations',
        'cad_wireless_sync_evidence',
    }
    assert expected <= tables
