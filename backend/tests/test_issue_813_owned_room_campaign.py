"""Issue #813 — owned-room holdout campaign authority.

Covers preregistration hash-binding, calibration/holdout disjointness,
measurement role + authority chains, promotion-ladder fail-closed
ordering, leakage forcing a new campaign identity, and repository
round-trip + tamper detection.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_owned_room_campaign import (
    CampaignMeasurement,
    CampaignPreregistration,
    CampaignVerdict,
    ClaimVerdict,
    OwnedRoomCampaignIntegrityError,
    evaluate_campaign_promotion,
)
from htdt.cad_owned_room_campaign_repository import (
    CadOwnedRoomCampaignRepository,
    OwnedRoomCampaignConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema
from htdt.measurement_evidence_display import (
    campaign_label,
    campaign_promotion_line,
)


def _ref(kind: str, rid: str = 'x') -> AuthorityRef:
    return AuthorityRef(
        kind=kind, ref_id=rid, ref_sha256='a' * 64)


def _prereg(document_id: str = 'doc-1', **kw) -> CampaignPreregistration:
    payload = dict(
        document_id=document_id,
        protocol_id='owned-room-o90',
        protocol_version='1.0',
        scene_ref=_ref('scene', 'scene-1'),
        system_variant_ref=_ref('system_variant', 'sv-1'),
        solver_ref=_ref('solver', 'r130d'),
        adapter_refs=(_ref('adapter', 'dsp-1'),),
        input_authority_refs=(_ref('material', 'wall-1'),),
        calibration_condition_ids=('cond-srcA-seat1',),
        holdout_condition_ids=('cond-srcB-seat2', 'cond-srcB-seat3'),
        holdout_dimensions=('receiver_position', 'source_position'),
        measurement_positions=('mlp', 'seat2', 'seat3'),
        candidate_ids=('cand-1', 'cand-2'),
        metrics=('fr_residual_db', 'rank_agreement'),
        uncertainty_method_ref=_ref('uncertainty', 'gum-1'),
        decision_thresholds=('rank_agreement>0.9',),
        allowed_exclusions=('hvac-on',),
        stop_conditions=('repeatability_floor_too_high',),
        environmental_requirements=('hvac off', 'windows closed'),
        spatial_claim='multi_seat',
        preregistered_at_utc='2026-10-01T00:00:00Z',
    )
    payload.update(kw)
    return CampaignPreregistration.create(**payload)


def _measurement(
    prereg: CampaignPreregistration,
    role: str = 'holdout',
    condition_id: str = 'cond-srcB-seat2',
    acquired_at_utc: str = '2026-10-02T00:00:00Z',
    **kw,
) -> CampaignMeasurement:
    payload = dict(
        document_id=prereg.document_id,
        campaign_ref=AuthorityRef(
            kind='campaign_preregistration',
            ref_id=prereg.preregistration_id,
            ref_sha256=prereg.preregistration_sha256),
        role=role,
        condition_id=condition_id,
        position_id='seat2',
        raw_asset_sha256='b' * 64,
        acquired_at_utc=acquired_at_utc,
        microphone_calibration_ref=_ref('mic_cal', 'mic-1'),
        calibration_file_ref=_ref('cal_file', 'cf-1'),
        sample_rate_hz=48000.0,
        routing='avr-ch1->mic-1',
        avr_dsp_state_ref=_ref('dsp_state', 'state-1'),
        position_orientation_ref=_ref('position', 'seat2'),
        uncertainty_budget_ref=_ref('uncertainty', 'budget-1'),
        quality_report_ref=_ref('quality', 'qr-1'),
    )
    payload.update(kw)
    return CampaignMeasurement.create(**payload)


def _mref(m: CampaignMeasurement) -> AuthorityRef:
    return AuthorityRef(
        kind='campaign_measurement',
        ref_id=m.measurement_id,
        ref_sha256=m.measurement_sha256)


def _verdict(
    prereg: CampaignPreregistration,
    measurements: tuple[CampaignMeasurement, ...],
    outcome: str = 'owned_room_insufficient',
    claim_verdicts: tuple[ClaimVerdict, ...] | None = None,
    **kw,
) -> CampaignVerdict:
    payload = dict(
        document_id=prereg.document_id,
        campaign_ref=AuthorityRef(
            kind='campaign_preregistration',
            ref_id=prereg.preregistration_id,
            ref_sha256=prereg.preregistration_sha256),
        protocol_id=prereg.protocol_id,
        protocol_version=prereg.protocol_version,
        claim_verdicts=claim_verdicts if claim_verdicts is not None else (
            ClaimVerdict(
                claim_kind='candidate_ranking', verdict='pass',
                evidence_refs=(_ref('evidence', 'e-rank'),)),
        ),
        promotion_outcome=outcome,
        repeatability_ref=_ref('repeatability', 'rep-1'),
        measurement_uncertainty_ref=_ref('uncertainty', 'unc-1'),
        candidate_separation_ref=_ref('separation', 'sep-1'),
        holdout_measurement_refs=tuple(
            _mref(m) for m in measurements if m.role == 'holdout'),
        calibration_measurement_refs=tuple(
            _mref(m) for m in measurements if m.role == 'calibration'),
        concluded_at_utc='2026-10-05T00:00:00Z',
    )
    payload.update(kw)
    return CampaignVerdict.create(**payload)


def _repo(tmp_path):
    db = tmp_path / 'scene.sqlite3'
    ensure_native_schema(db)
    return CadOwnedRoomCampaignRepository(SceneRepository(db))


# ------------------------------------------------------- preregistration


def test_prereg_requires_disjoint_conditions() -> None:
    with pytest.raises(ValueError):
        _prereg(holdout_condition_ids=('cond-srcA-seat1',))


def test_prereg_requires_material_fields() -> None:
    for field, value in (
            ('calibration_condition_ids', ()),
            ('holdout_condition_ids', ()),
            ('holdout_dimensions', ()),
            ('measurement_positions', ()),
            ('candidate_ids', ()),
            ('metrics', ()),
            ('stop_conditions', ()),
            ('protocol_version', '')):
        with pytest.raises(ValueError, match=field.replace('_', r'\_')
                           if field.endswith('_ids') else None):
            _prereg(**{field: value})


def test_supersede_requires_reason_and_vice_versa() -> None:
    with pytest.raises(ValueError):
        _prereg(supersedes_campaign_ref=_ref('campaign_preregistration'))
    with pytest.raises(ValueError):
        _prereg(leakage_reason='holdout leaked into calibration')
    ok = _prereg(
        supersedes_campaign_ref=_ref('campaign_preregistration'),
        leakage_reason='holdout leaked into calibration')
    assert ok.leakage_reason


# ------------------------------------------------------- measurements


def test_absolute_level_claim_requires_spl_calibration() -> None:
    prereg = _prereg()
    with pytest.raises(ValueError):
        _measurement(prereg, claims_absolute_level=True)
    ok = _measurement(
        prereg, claims_absolute_level=True,
        spl_calibration_ref=_ref('spl_cal', '94dB'))
    assert ok.claims_absolute_level


def test_measurement_requires_pinned_campaign() -> None:
    prereg = _prereg()
    with pytest.raises(ValueError):
        _measurement(
            prereg, campaign_ref=AuthorityRef(
                kind='campaign_preregistration',
                ref_id='x', ref_sha256=None))


def test_measurement_requires_raw_asset_hash() -> None:
    with pytest.raises(ValueError):
        _measurement(_prereg(), raw_asset_sha256='nothex')


# ------------------------------------------------------- verdict model


def test_recommendation_eligible_requires_all_gates() -> None:
    prereg = _prereg()
    m = _measurement(prereg)
    verdicts = (
        ClaimVerdict(claim_kind='absolute_response', verdict='pass',
                     evidence_refs=(_ref('ev', 'a'),)),
        ClaimVerdict(claim_kind='candidate_ranking', verdict='pass',
                     evidence_refs=(_ref('ev', 'b'),)),
    )
    # Missing external_benchmark_ref etc → reject.
    with pytest.raises(ValueError):
        _verdict(prereg, (m,), outcome='recommendation_eligible',
                 claim_verdicts=verdicts)
    ok = _verdict(
        prereg, (m,), outcome='recommendation_eligible',
        claim_verdicts=verdicts,
        external_benchmark_ref=_ref('bench'),
        numerical_convergence_ref=_ref('conv'),
        input_qualification_ref=_ref('inpq'),
        holdout_residual_ref=_ref('resid'),
        applicability_ref=_ref('appl'))
    assert ok.recommendation_eligible


def test_pass_claim_verdict_requires_evidence() -> None:
    with pytest.raises(ValueError):
        ClaimVerdict(claim_kind='candidate_ranking', verdict='pass')


# ------------------------------------------------------- evaluator


def test_evaluator_not_evaluated_and_external_only() -> None:
    prereg = _prereg()
    assert evaluate_campaign_promotion(prereg, (), None) \
        == 'not_evaluated'
    cal = _measurement(prereg, role='calibration',
                       condition_id='cond-srcA-seat1')
    v = _verdict(prereg, (cal,))
    assert evaluate_campaign_promotion(
        prereg, (cal,), v) == 'external_only'


def test_evaluator_prereg_after_acquisition_is_insufficient() -> None:
    prereg = _prereg(preregistered_at_utc='2026-10-03T00:00:00Z')
    m = _measurement(prereg, acquired_at_utc='2026-10-02T00:00:00Z')
    v = _verdict(prereg, (m,))
    assert evaluate_campaign_promotion(prereg, (m,), v) \
        == 'owned_room_insufficient'


def test_evaluator_missing_prereg_is_insufficient() -> None:
    prereg = _prereg()
    m = _measurement(prereg)
    v = _verdict(prereg, (m,))
    assert evaluate_campaign_promotion(None, (m,), v) \
        == 'owned_room_insufficient'


def test_evaluator_leakage_is_insufficient() -> None:
    prereg = _prereg()
    hold = _measurement(prereg)
    cal = _measurement(prereg, role='calibration',
                       condition_id='cond-srcA-seat1')
    v = _verdict(prereg, (hold, cal),
                 # holdout id smuggled into calibration refs
                 calibration_measurement_refs=(_mref(hold),))
    assert evaluate_campaign_promotion(
        prereg, (hold, cal), v) == 'owned_room_insufficient'


def test_evaluator_missing_repeatability_floor_is_insufficient() -> None:
    prereg = _prereg()
    m = _measurement(prereg)
    v = _verdict(prereg, (m,), repeatability_ref=None)
    assert evaluate_campaign_promotion(prereg, (m,), v) \
        == 'owned_room_insufficient'


def test_evaluator_absolute_limited_vs_domain_validated() -> None:
    prereg = _prereg()
    m = _measurement(prereg)
    rank_only = (
        ClaimVerdict(claim_kind='absolute_response', verdict='fail'),
        ClaimVerdict(claim_kind='candidate_ranking', verdict='pass',
                     evidence_refs=(_ref('ev',),)),
    )
    v = _verdict(
        prereg, (m,),
        outcome='owned_room_absolute_prediction_limited',
        claim_verdicts=rank_only)
    assert evaluate_campaign_promotion(prereg, (m,), v) \
        == 'owned_room_absolute_prediction_limited'

    both = (
        ClaimVerdict(claim_kind='absolute_response', verdict='pass',
                     evidence_refs=(_ref('ev', 'a'),)),
        ClaimVerdict(claim_kind='candidate_ranking', verdict='pass',
                     evidence_refs=(_ref('ev', 'b'),)),
    )
    # Core gates missing → trend only (evaluator overrides the
    # declared outcome; the verdict's own declared outcome stays
    # insufficient so its validator doesn't demand residual refs).
    v2 = _verdict(prereg, (m,), outcome='owned_room_insufficient',
                  claim_verdicts=both)
    assert evaluate_campaign_promotion(prereg, (m,), v2) \
        == 'owned_room_trend_validated'

    v3 = _verdict(prereg, (m,), outcome='owned_room_domain_validated',
                  claim_verdicts=both,
                  external_benchmark_ref=_ref('bench'),
                  numerical_convergence_ref=_ref('conv'),
                  input_qualification_ref=_ref('inpq'),
                  holdout_residual_ref=_ref('resid'),
                  applicability_ref=_ref('appl'))
    assert evaluate_campaign_promotion(prereg, (m,), v3) \
        == 'owned_room_domain_validated'

    v4 = _verdict(prereg, (m,), outcome='recommendation_eligible',
                  claim_verdicts=both,
                  external_benchmark_ref=_ref('bench'),
                  numerical_convergence_ref=_ref('conv'),
                  input_qualification_ref=_ref('inpq'),
                  holdout_residual_ref=_ref('resid'),
                  applicability_ref=_ref('appl'))
    assert evaluate_campaign_promotion(prereg, (m,), v4) \
        == 'recommendation_eligible'


def test_evaluator_stale_authority_is_insufficient() -> None:
    prereg = _prereg()
    m = _measurement(prereg)
    v = _verdict(prereg, (m,), stale_authority_refs=(_ref('old'),))
    assert evaluate_campaign_promotion(prereg, (m,), v) \
        == 'owned_room_insufficient'


# ------------------------------------------------------- repository


def test_repository_round_trip_all_records(tmp_path) -> None:
    repo = _repo(tmp_path)
    prereg = _prereg()
    m = _measurement(prereg)
    v = _verdict(prereg, (m,))
    repo.preregistrations.save(prereg)
    repo.measurements.save(m)
    repo.verdicts.save(v)
    assert repo.get_preregistration(prereg.preregistration_id) == prereg
    assert repo.get_measurement(m.measurement_id) == m
    assert repo.get_verdict(v.verdict_id) == v


def test_repository_append_only_conflict(tmp_path) -> None:
    repo = _repo(tmp_path)
    prereg = _prereg()
    repo.preregistrations.save(prereg)
    tampered = _prereg(protocol_version='2.0')
    object.__setattr__(
        tampered, 'preregistration_id', prereg.preregistration_id)
    object.__setattr__(
        tampered, 'preregistration_sha256', prereg.preregistration_sha256)
    with pytest.raises(
            (OwnedRoomCampaignConflictError,
             OwnedRoomCampaignIntegrityError)):
        repo.preregistrations.save(tampered)
    repo.preregistrations.save(prereg)


def test_repository_detects_column_tampering(tmp_path) -> None:
    repo = _repo(tmp_path)
    m = _measurement(_prereg())
    repo.measurements.save(m)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_campaign_measurements SET role=? '
            'WHERE measurement_id=?', ('calibration', m.measurement_id))
    with pytest.raises(OwnedRoomCampaignIntegrityError):
        repo.get_measurement(m.measurement_id)


# ------------------------------------------------------- labels


def test_ja_label_coverage() -> None:
    assert campaign_promotion_line('owned_room_insufficient') \
        == '所有ルーム昇格: 所有ルーム証拠不足'
    assert campaign_label('holdout') == 'ホールドアウト'
    assert campaign_label('__none__') == '__none__'
