"""Owned-room holdout campaign authority (#813).

ASME V&V-20 logic: a prediction is only honest against evidence the
model never saw. This module seals the campaign protocol BEFORE holdout
acquisition, binds every field measurement to a declared role and a raw
asset hash, and keeps holdout leakage from ever passing as prediction.

Rules that this module enforces structurally:

* A campaign is preregistered and hash-bound BEFORE any holdout-role
  measurement may serve promotion (`preregistered_at_utc < earliest
  holdout acquired_at_utc` — evaluated, not asserted).
* Calibration and holdout condition ids are disjoint sets inside a
  campaign identity (`CampaignPreregistration` validator); a measurement
  whose role is ``holdout`` may never appear in calibration reference
  sets, and vice versa.
* A measurement that leaked into calibration forces a NEW campaign
  identity (`supersedes_campaign_ref` + `leakage_reason`), never a
  quiet reuse of the same protocol/version.
* Repeatability evidence (same-condition repeats) precedes residual
  interpretation — promotion gates require a repeatability gate ref
  before any ranking verdict can elevate the campaign past
  ``owned_room_insufficient``.
* Absolute-response and decision/ranking claims carry SEPARATE
  verdicts — common model bias does not kill decision usefulness.
* The campaign may conclude in `aborted`/`insufficient` outcomes
  (`StopReason`); a recommendation is never synthesized from a
  failing campaign.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

# ---------------------------------------------------------------------------
# vocabulary


MeasurementRole = Literal[
    'calibration',
    'holdout',
    'repeatability',
    'perturbation',
    'screening',
]
"""A measurement's declared role inside a campaign. ``holdout`` data
may never feed calibration; ``repeatability`` pairs same-condition
repeats; ``perturbation`` covers bounded position/installation
perturbations; ``screening`` is exploratory data excluded from
promotion decisions."""

HoldoutDimension = Literal[
    'receiver_position',
    'seat_position',
    'speaker_listener_configuration',
    'candidate_placement',
    'source_position',
    'independent_repeat_session',
    'other_declared',
]
"""Physical independence dimensions a holdout may exercise (#813 §2)."""

PredictionClaimKind = Literal[
    'absolute_response',
    'shape_after_normalization',
    'modal_peak_location',
    'arrival_timing',
    'candidate_direction',
    'candidate_ranking',
    'pareto_trend',
    'robustness_tolerance',
]
"""Separate claim families — absolute/shape prediction and decision
prediction get independent verdicts (#813 §5)."""

CampaignClaimVerdict = Literal[
    'pass',
    'fail',
    'insufficient_evidence',
    'not_evaluated',
]

PromotionOutcome = Literal[
    'not_evaluated',
    'external_only',
    'owned_room_insufficient',
    'owned_room_absolute_prediction_limited',
    'owned_room_trend_validated',
    'owned_room_domain_validated',
    'recommendation_eligible',
]
"""Promotion ladder (#813 §9). A campaign never jumps rungs: each level
requires the previous plus its own pinned gates."""

StopReason = Literal[
    'completed',
    'model_invalid_for_context',
    'material_authority_insufficient',
    'repeatability_floor_too_high',
    'candidate_differences_unresolvable',
    'calibration_does_not_generalize',
    'ranking_unstable_under_tolerance',
    'additional_evidence_required',
]
"""Honest termination classes (#813 §10). A campaign may conclude
failure; it may not be forced into a recommendation."""


CAMPAIGN_LABELS: dict[str, str] = {
    # roles
    'calibration': '校正',
    'holdout': 'ホールドアウト',
    'repeatability': '再現性',
    'perturbation': '摂動',
    'screening': 'スクリーニング',
    # claim kinds
    'absolute_response': '絶対応答',
    'shape_after_normalization': '正規化後形状',
    'modal_peak_location': 'モードピーク位置',
    'arrival_timing': '到達時刻',
    'candidate_direction': '候補改善方向',
    'candidate_ranking': '候補順位',
    'pareto_trend': 'パレート傾向',
    'robustness_tolerance': 'ロバスト性/許容差',
    # promotion outcomes
    'not_evaluated': '未評価',
    'external_only': '外部検証のみ',
    'owned_room_insufficient': '所有ルーム証拠不足',
    'owned_room_absolute_prediction_limited': '絶対予測限定',
    'owned_room_trend_validated': '傾向検証済み',
    'owned_room_domain_validated': 'ドメイン検証済み',
    'recommendation_eligible': '推奨適格',
    # stop reasons
    'completed': '完了',
    'model_invalid_for_context': 'このコンテキストでモデル無効',
    'material_authority_insufficient': '材料権威不足',
    'repeatability_floor_too_high': '再現性フロア過大',
    'candidate_differences_unresolvable': '候補差分解不能',
    'calibration_does_not_generalize': '校正が汎化しない',
    'ranking_unstable_under_tolerance': '許容差下で順位不安定',
    'additional_evidence_required': '追加証拠が必要',
}


class OwnedRoomCampaignIntegrityError(ValueError):
    """A sealed campaign record failed id/sha or column re-verification."""


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is None:
            continue
        if ref.ref_sha256 is None:
            raise ValueError(
                'authority refs must carry the pinned ref_sha256')


def _seal(
    model: type[BaseModel],
    fields: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> BaseModel:
    record = model.model_construct(**fields)
    sha = canonical_sha256(record.identity_payload())
    fields[sha_field] = sha
    fields[id_field] = f'{prefix}-{sha[:24]}'
    return model.model_validate(fields)


# ---------------------------------------------------------------------------
# preregistration


class CampaignPreregistration(BaseModel):
    """Hash-bound campaign protocol frozen BEFORE holdout acquisition
    (crc- prefix).

    Everything the campaign is allowed to conclude later is declared
    here: scene/system variant, solver/adapter versions, input
    authorities, which physical conditions are calibration vs holdout,
    measurement positions, candidate identities, metrics, uncertainty
    method, decision rules, allowed exclusions, stop conditions,
    environmental requirements. Changing any material rule after
    viewing holdout results requires a new protocol version and a new
    campaign identity.
    """

    model_config = ConfigDict(frozen=True)

    preregistration_id: str
    preregistration_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    protocol_id: str
    protocol_version: str
    scene_ref: AuthorityRef
    system_variant_ref: AuthorityRef | None = None
    solver_ref: AuthorityRef
    adapter_refs: tuple[AuthorityRef, ...] = ()
    input_authority_refs: tuple[AuthorityRef, ...] = ()
    calibration_condition_ids: tuple[str, ...]
    holdout_condition_ids: tuple[str, ...]
    holdout_dimensions: tuple[HoldoutDimension, ...]
    measurement_positions: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    metrics: tuple[str, ...]
    uncertainty_method_ref: AuthorityRef | None = None
    decision_thresholds: tuple[str, ...] = ()
    allowed_exclusions: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...]
    environmental_requirements: tuple[str, ...] = ()
    spatial_claim: Literal[
        'single_seat', 'multi_seat', 'topology_variant'] | None = None
    preregistered_at_utc: str
    supersedes_campaign_ref: AuthorityRef | None = None
    leakage_reason: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CampaignPreregistration':
        _require_refs(
            self.scene_ref, self.system_variant_ref, self.solver_ref,
            self.uncertainty_method_ref, self.supersedes_campaign_ref,
            *self.adapter_refs, *self.input_authority_refs)
        for required in ('protocol_id', 'protocol_version',
                         'preregistered_at_utc'):
            if not getattr(self, required):
                raise ValueError(f'{required} is required')
        if not self.calibration_condition_ids:
            raise ValueError('calibration_condition_ids is required')
        if not self.holdout_condition_ids:
            raise ValueError('holdout_condition_ids is required')
        overlap = (set(self.calibration_condition_ids)
                   & set(self.holdout_condition_ids))
        if overlap:
            raise ValueError(
                'calibration and holdout conditions must be disjoint: '
                + ', '.join(sorted(overlap)))
        if not self.holdout_dimensions:
            raise ValueError(
                'holdout_dimensions must declare which physical '
                'independence the holdout exercises')
        if not self.measurement_positions:
            raise ValueError('measurement_positions is required')
        if not self.candidate_ids:
            raise ValueError('candidate_ids is required')
        if not self.metrics:
            raise ValueError('metrics is required')
        if not self.stop_conditions:
            raise ValueError(
                'stop_conditions is required — a campaign must be '
                'allowed to conclude failure')
        if self.supersedes_campaign_ref is not None \
                and not self.leakage_reason:
            raise ValueError(
                'superseding campaigns must declare the leakage/'
                'revision reason')
        if self.leakage_reason is not None \
                and self.supersedes_campaign_ref is None:
            raise ValueError(
                'leakage_reason requires supersedes_campaign_ref')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={
                'preregistration_id', 'preregistration_sha256'})

    @classmethod
    def create(cls, **fields: Any) -> 'CampaignPreregistration':
        return _seal(cls, fields, 'preregistration_id',
                     'preregistration_sha256', 'crc')  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# field measurements


class CampaignMeasurement(BaseModel):
    """One field acquisition bound to a campaign and a declared role
    (crm- prefix).

    Every holdout measurement retains its measurement-authority chain
    (#813 §7): raw asset hash, acquisition context, microphone
    calibration identity, SPL calibration when absolute level is
    claimed, routing, AVR/DSP state, position/orientation authority,
    uncertainty budget and quality report.
    """

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    campaign_ref: AuthorityRef
    role: MeasurementRole
    condition_id: str
    position_id: str | None = None
    raw_asset_sha256: str = Field(pattern=_SHA256_PATTERN)
    acquired_at_utc: str
    microphone_calibration_ref: AuthorityRef | None = None
    calibration_file_ref: AuthorityRef | None = None
    spl_calibration_ref: AuthorityRef | None = None
    claims_absolute_level: bool = False
    sample_rate_hz: float | None = None
    interface_id: str | None = None
    routing: str | None = None
    avr_dsp_state_ref: AuthorityRef | None = None
    environmental_state: str | None = None
    position_orientation_ref: AuthorityRef | None = None
    uncertainty_budget_ref: AuthorityRef | None = None
    quality_report_ref: AuthorityRef | None = None
    acquisition_context: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CampaignMeasurement':
        _require_refs(
            self.campaign_ref, self.microphone_calibration_ref,
            self.calibration_file_ref, self.spl_calibration_ref,
            self.avr_dsp_state_ref, self.position_orientation_ref,
            self.uncertainty_budget_ref, self.quality_report_ref)
        for required in ('condition_id', 'acquired_at_utc'):
            if not getattr(self, required):
                raise ValueError(f'{required} is required')
        if self.claims_absolute_level and self.spl_calibration_ref \
                is None:
            raise ValueError(
                'absolute-level claims require spl_calibration_ref')
        if self.sample_rate_hz is not None \
                and self.sample_rate_hz <= 0.0:
            raise ValueError('sample_rate_hz must be > 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={
                'measurement_id', 'measurement_sha256'})

    @classmethod
    def create(cls, **fields: Any) -> 'CampaignMeasurement':
        return _seal(cls, fields, 'measurement_id',
                     'measurement_sha256', 'crm')  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# verdict / promotion


class ClaimVerdict(BaseModel):
    """One claim family's verdict with its evidence chain."""

    model_config = ConfigDict(frozen=True)

    claim_kind: PredictionClaimKind
    verdict: CampaignClaimVerdict
    evidence_refs: tuple[AuthorityRef, ...] = ()
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ClaimVerdict':
        _require_refs(*self.evidence_refs)
        if self.verdict == 'pass' and not self.evidence_refs:
            raise ValueError(
                'a passing claim verdict requires pinned evidence refs')
        return self


class CampaignVerdict(BaseModel):
    """Sealed campaign conclusion (crv- prefix).

    Promotion outcomes form a ladder; each rung requires every
    applicable gate pinned — never one aggregate score. The verdict
    keeps absolute-response and decision claims separate.
    """

    model_config = ConfigDict(frozen=True)

    verdict_id: str
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    campaign_ref: AuthorityRef
    protocol_id: str
    protocol_version: str
    claim_verdicts: tuple[ClaimVerdict, ...]
    promotion_outcome: PromotionOutcome
    external_benchmark_ref: AuthorityRef | None = None
    numerical_convergence_ref: AuthorityRef | None = None
    input_qualification_ref: AuthorityRef | None = None
    measurement_uncertainty_ref: AuthorityRef | None = None
    holdout_residual_ref: AuthorityRef | None = None
    repeatability_ref: AuthorityRef | None = None
    candidate_separation_ref: AuthorityRef | None = None
    applicability_ref: AuthorityRef | None = None
    robustness_ref: AuthorityRef | None = None
    holdout_measurement_refs: tuple[AuthorityRef, ...] = ()
    calibration_measurement_refs: tuple[AuthorityRef, ...] = ()
    stale_authority_refs: tuple[AuthorityRef, ...] = ()
    stop_reason: StopReason | None = None
    concluded_at_utc: str
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CampaignVerdict':
        _require_refs(
            self.campaign_ref, self.external_benchmark_ref,
            self.numerical_convergence_ref, self.input_qualification_ref,
            self.measurement_uncertainty_ref, self.holdout_residual_ref,
            self.repeatability_ref, self.candidate_separation_ref,
            self.applicability_ref, self.robustness_ref,
            *self.holdout_measurement_refs,
            *self.calibration_measurement_refs,
            *self.stale_authority_refs)
        for required in ('protocol_id', 'protocol_version',
                         'concluded_at_utc'):
            if not getattr(self, required):
                raise ValueError(f'{required} is required')
        if not self.claim_verdicts:
            raise ValueError('claim_verdicts is required')
        if self.promotion_outcome == 'recommendation_eligible':
            missing = [
                name for name in (
                    'external_benchmark_ref', 'numerical_convergence_ref',
                    'input_qualification_ref',
                    'measurement_uncertainty_ref', 'holdout_residual_ref',
                    'repeatability_ref', 'candidate_separation_ref',
                    'applicability_ref',
                ) if getattr(self, name) is None]
            if missing:
                raise ValueError(
                    'recommendation_eligible requires every promotion '
                    'gate pinned: ' + ', '.join(missing))
            if self.stale_authority_refs:
                raise ValueError(
                    'recommendation_eligible rejected while stale '
                    'authority refs exist')
        if self.promotion_outcome in (
                'owned_room_trend_validated',
                'owned_room_domain_validated',
                'recommendation_eligible') \
                and self.holdout_residual_ref is None:
            raise ValueError(
                'trend/domain validation requires holdout_residual_ref')
        return self

    @property
    def recommendation_eligible(self) -> bool:
        return self.promotion_outcome == 'recommendation_eligible'

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={
                'verdict_id', 'verdict_sha256'})

    @classmethod
    def create(cls, **fields: Any) -> 'CampaignVerdict':
        return _seal(cls, fields, 'verdict_id', 'verdict_sha256',
                     'crv')  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# promotion evaluator


def evaluate_campaign_promotion(
    prereg: CampaignPreregistration | None,
    measurements: tuple[CampaignMeasurement, ...],
    verdict: CampaignVerdict | None,
) -> PromotionOutcome:
    """Fail-closed promotion ladder for an owned-room campaign.

    Any structural violation — leakage between calibration and holdout
    identities, preregistration later than the first holdout
    acquisition, missing repeatability floor — floors the outcome at
    ``owned_room_insufficient`` regardless of the claimed verdict.
    """
    if verdict is None:
        return 'not_evaluated'
    holdout = tuple(m for m in measurements if m.role == 'holdout')
    if not holdout:
        return 'external_only'
    if prereg is None:
        return 'owned_room_insufficient'

    # preregistration must precede the earliest holdout acquisition
    earliest = min(m.acquired_at_utc for m in holdout)
    if prereg.preregistered_at_utc >= earliest:
        return 'owned_room_insufficient'

    # leakage: holdout measurements must only be used as holdout
    holdout_ids = {m.measurement_id for m in holdout}
    calibration_ids = {
        ref.ref_id for ref in verdict.calibration_measurement_refs}
    holdout_ref_ids = {
        ref.ref_id for ref in verdict.holdout_measurement_refs}
    if holdout_ids & calibration_ids:
        return 'owned_room_insufficient'
    used_holdout_ids = {
        m.measurement_id for m in measurements
        if m.role != 'holdout' and m.measurement_id in holdout_ref_ids}
    if used_holdout_ids:
        return 'owned_room_insufficient'

    # repeatability floor must exist before residuals are interpretable
    if verdict.repeatability_ref is None:
        return 'owned_room_insufficient'
    if verdict.measurement_uncertainty_ref is None \
            or verdict.candidate_separation_ref is None:
        return 'owned_room_insufficient'
    if verdict.stale_authority_refs:
        return 'owned_room_insufficient'

    by_kind = {cv.claim_kind: cv for cv in verdict.claim_verdicts}
    absolute_pass = by_kind.get('absolute_response') is not None \
        and by_kind['absolute_response'].verdict == 'pass'
    decision_pass = any(
        by_kind.get(k) is not None and by_kind[k].verdict == 'pass'
        for k in ('candidate_direction', 'candidate_ranking',
                  'pareto_trend'))
    if not decision_pass:
        return 'owned_room_insufficient'
    if not absolute_pass:
        # Decision evidence can stay useful while absolute prediction
        # carries common model bias — report it honestly.
        return 'owned_room_absolute_prediction_limited'
    gates = (
        verdict.external_benchmark_ref,
        verdict.numerical_convergence_ref,
        verdict.input_qualification_ref,
        verdict.holdout_residual_ref,
        verdict.applicability_ref,
    )
    if any(g is None for g in gates):
        return 'owned_room_trend_validated'
    if verdict.promotion_outcome == 'recommendation_eligible':
        # The verdict validator already required every promotion gate
        # and rejected stale authorities.
        return 'recommendation_eligible'
    return 'owned_room_domain_validated'
