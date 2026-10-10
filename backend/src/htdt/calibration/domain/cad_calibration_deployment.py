"""Calibration deployment / verification-loop authority (#806).

Designing a correction and proving it reached the playback chain are
different claims. This module seals the loop stages the device-adapter
layer already produces — ``MaterializedCalibrationSettings`` (export
handed to a target), ``DeviceApplyAck`` (a write was issued),
``EffectiveAppliedSettingsSnapshot`` (observed installed state) — into
immutable deployment authority, post-deployment effectiveness
comparison, and scoped rollback records.

Rules enforced here, matching issue #806:

* designed != exported != deployed != verified — an apply ack or a
  materialized payload alone can never produce a verified deployment;
  only a machine read-back snapshot can.
* manual attestation is typed and visibly weaker than machine readback
  (``deployment_attested`` vs ``deployment_verified``).
* unsupported parameters fail closed: materialization that surfaced
  device-side drops/clamps (``unsupported_items``) blocks the
  deployment gate — the adapter may not silently honor less than the
  export claimed.
* device firmware/config identity participates via the pinned
  ``AdapterDeviceBinding`` hash; a binding change is a different
  deployment.
* post-deployment measurement binds to the deployed state and stays
  separate evidence — it never overwrites the deployment record.
* rollback/replacement invalidates only evidence explicitly bound to
  the superseded deployment; room/material/geometry evidence is
  untouched.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_resolver import AuthorityRef
from ...cad_device_adapter import (
    AdapterCapabilityReport,
    MaterializedCalibrationSettings,
)
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is not None and ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


DeploymentTargetClass = Literal[
    'camilladsp_device', 'minidsp_file_export', 'generic_peq_fir_file',
    'avr_manual_handoff', 'other_adapter', 'unknown',
]

DeploymentEvidenceMode = Literal[
    'machine_readback', 'operator_attestation', 'unverified_export',
]

DeploymentState = Literal[
    'deployment_verified', 'deployment_attested', 'deployment_unverified',
    'deployment_mismatch', 'deployment_blocked', 'deployment_superseded',
    'unknown',
]

EffectivenessVerdict = Literal[
    'improvement_verified', 'no_measurable_change', 'regression_observed',
    'inconclusive', 'unmeasured',
]

MetricOutcome = Literal['improved', 'unchanged', 'regressed', 'indeterminate']

DeploymentGateVerdict = Literal['deployable', 'deployment_blocked']


DEPLOYMENT_LABELS: dict[str, str] = {
    'deployable': 'デプロイ可能（機能・制限内）',
    'deployment_verified': 'デプロイ確認済み（機器読み戻し一致）',
    'deployment_attested': 'デプロイ申告済み（手動証言・機器読み戻しなし）',
    'deployment_unverified': 'デプロイ未確認（エクスポート/ACK のみ）',
    'deployment_mismatch': 'デプロイ状態が意図と不一致',
    'deployment_blocked': 'デプロイ不可（未対応パラメータ/能力不足）',
    'deployment_superseded': 'ロールバック/置換済みデプロイ',
    'improvement_verified': 'デプロイ後の改善を検証済み',
    'no_measurable_change': '測定可能な変化なし',
    'regression_observed': 'デプロイ後に退行を観測',
    'inconclusive': '判定不能（証拠不十分）',
    'unmeasured': 'デプロイ後未測定',
    'machine_readback': '機器読み戻し',
    'operator_attestation': 'オペレータ証言',
    'unverified_export': '未検証エクスポート',
    'camilladsp_device': 'CamillaDSP デバイス',
    'minidsp_file_export': 'miniDSP ファイルエクスポート',
    'generic_peq_fir_file': '汎用 PEQ/FIR ファイル',
    'avr_manual_handoff': 'AVR 手動入力ハンドオフ',
    'other_adapter': 'その他アダプタ',
    'unknown': '不明',
    'improved': '改善',
    'unchanged': '不変',
    'regressed': '退行',
    'indeterminate': '判定不能',
}


class DeploymentCapabilityDeclaration(BaseModel):
    """What one adapter declared it could honor at deploy time.

    Frozen snapshot of an ``AdapterCapabilityReport`` plus the
    feature/limit vocabulary the issue requires adapters to declare —
    supported filter kinds, gain/headroom limits, and readback support.
    The deployment record pins this declaration's semantic hash, so the
    capability context of a deployment survives later adapter changes.
    """

    model_config = ConfigDict(frozen=True)

    declaration_id: str
    declaration_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    adapter_kind: str = Field(min_length=1)
    device_family: str = Field(min_length=1)
    supports_apply: bool
    supports_read_back: bool
    supports_materialization: bool = True
    #: Filter/feature kinds the adapter claims to honor exactly
    #: (e.g. 'peq', 'fir', 'gain', 'delay', 'polarity', 'crossover',
    #: 'routing'). Anything outside this list is unsupported.
    supported_features: tuple[str, ...] = ()
    #: Adapter-declared headroom/limit notes (max gain dB, max filters,
    #: firmware constraints). Human/operator facing — enforced limits
    #: belong in the adapter itself.
    declared_limit_notes: tuple[str, ...] = ()
    # #878 common manifest carry-through — the declaration pins the same
    # mechanism/authority vocabulary the report publishes, so path
    # evaluation on a frozen declaration stays exact.
    deploy_mechanism: str = 'none'
    readback_mechanism: str = 'none'
    rollback_mechanism: str = 'none'
    runtime_observation: str = 'none'
    protocol_authority: str = 'unknown'
    auth_requirements: tuple[str, ...] = ()
    applicability: str | None = None
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'DeploymentCapabilityDeclaration':
        if len(self.supported_features) != len(set(self.supported_features)):
            raise ValueError('supported_features must be unique')
        if self.declaration_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'DeploymentCapabilityDeclaration hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'declaration_id', 'declaration_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DeploymentCapabilityDeclaration':
        return _seal(
            cls, payload, 'declaration_id', 'declaration_sha256', 'dcd')


def build_capability_declaration(
    report: AdapterCapabilityReport,
    *,
    document_id: str,
    declared_at_utc: str,
) -> DeploymentCapabilityDeclaration:
    """#878: seal the adapter's published capability manifest verbatim —
    the declaration is the frozen, pinnable form of the report."""
    return DeploymentCapabilityDeclaration.create(
        document_id=document_id,
        adapter_id=report.adapter_id,
        adapter_version=report.adapter_version,
        adapter_kind=report.adapter_kind,
        device_family=report.device_family,
        supports_apply=report.supports_apply,
        supports_read_back=report.supports_read_back,
        supports_materialization=report.supports_materialization,
        supported_features=tuple(report.supported_features),
        declared_limit_notes=tuple(report.limit_notes),
        deploy_mechanism=report.deploy_mechanism,
        readback_mechanism=report.readback_mechanism,
        rollback_mechanism=report.rollback_mechanism,
        runtime_observation=report.runtime_observation,
        protocol_authority=report.protocol_authority,
        auth_requirements=tuple(report.auth_requirements),
        applicability=report.applicability,
        declared_at_utc=declared_at_utc,
    )


class CalibrationDeployment(BaseModel):
    """Immutable deployed-state authority for one calibration plan (cald-).

    Pins the full chain: device-neutral plan → export snapshot →
    materialized device payload → device binding → declared adapter
    capabilities → observed state evidence. ``deployment_state`` is
    derived from the evidence at creation — callers pass the snapshot
    and mismatch facts; this class never accepts a stronger state than
    the evidence supports.
    """

    model_config = ConfigDict(frozen=True)

    deployment_id: str
    deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    calibration_plan_id: str = Field(min_length=1)
    calibration_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    export_id: str = Field(min_length=1)
    export_sha256: str = Field(pattern=_SHA256_PATTERN)
    materialization_id: str = Field(min_length=1)
    materialization_sha256: str = Field(pattern=_SHA256_PATTERN)
    binding_id: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    capability_declaration_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_class: DeploymentTargetClass = 'unknown'
    apply_ack_id: str | None = None
    observed_snapshot_id: str | None = None
    observed_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    evidence_mode: DeploymentEvidenceMode = 'unverified_export'
    deployment_state: DeploymentState = 'deployment_unverified'
    deployed_at_utc: str = Field(min_length=1)
    #: Operator identity when evidence_mode is operator_attestation.
    operator_attestor: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CalibrationDeployment':
        if self.observed_snapshot_id is None \
                and self.observed_snapshot_sha256 is not None:
            raise ValueError(
                'snapshot sha pinned without a snapshot id')
        if self.deployment_state == 'deployment_verified':
            if self.evidence_mode != 'machine_readback':
                raise ValueError(
                    'deployment_verified requires machine_readback')
            if not self.observed_snapshot_id:
                raise ValueError(
                    'deployment_verified requires an observed snapshot')
        if self.deployment_state == 'deployment_attested':
            if self.evidence_mode != 'operator_attestation':
                raise ValueError(
                    'deployment_attested requires operator_attestation')
            if not self.observed_snapshot_id:
                raise ValueError(
                    'deployment_attested requires an observed snapshot')
            if not self.operator_attestor:
                raise ValueError(
                    'deployment_attested requires operator_attestor')
        if self.deployment_state == 'deployment_unverified':
            if self.evidence_mode != 'unverified_export':
                raise ValueError(
                    'deployment_unverified requires unverified_export')
            if self.observed_snapshot_id is not None:
                raise ValueError(
                    'deployment_unverified must not pin a snapshot')
        if self.deployment_state == 'deployment_mismatch' \
                and not self.observed_snapshot_id:
            raise ValueError(
                'deployment_mismatch requires an observed snapshot')
        if self.deployment_state == 'deployment_superseded':
            raise ValueError(
                'deployment_superseded is derived by rollback '
                'evaluation, never stored')
        if self.deployment_sha256 != _hash(self.identity_payload()):
            raise ValueError('CalibrationDeployment hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'deployment_id', 'deployment_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CalibrationDeployment':
        return _seal(
            cls, payload, 'deployment_id', 'deployment_sha256', 'cald')


class EffectivenessMetricDelta(BaseModel):
    """One named before/after comparison bound to a deployment.

    Values stay as repr strings — the metric's unit/profile semantics
    live on the referenced measurement evidence, not here.
    """

    model_config = ConfigDict(frozen=True)

    metric_id: str = Field(min_length=1)
    before_value_repr: str | None = None
    after_value_repr: str | None = None
    outcome: MetricOutcome


class DeploymentEffectivenessReport(BaseModel):
    """Before/after measurement comparison bound to a deployment (defx-).

    Post-deployment measurement is separate evidence pinned by ref;
    this report only compares — it never overwrites the deployment or
    the underlying measurements.
    """

    model_config = ConfigDict(frozen=True)

    report_id: str
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    deployment_ref: AuthorityRef
    pre_measurement_refs: tuple[AuthorityRef, ...] = ()
    post_measurement_refs: tuple[AuthorityRef, ...] = ()
    deltas: tuple[EffectivenessMetricDelta, ...] = ()
    verdict: EffectivenessVerdict = 'unmeasured'
    evaluated_at_utc: str = Field(min_length=1)
    evaluator_note: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'DeploymentEffectivenessReport':
        _require_refs(self.deployment_ref)
        if self.deployment_ref.kind != 'calibration_deployment':
            raise ValueError(
                "deployment_ref kind must be 'calibration_deployment'")
        _require_refs(*self.pre_measurement_refs)
        _require_refs(*self.post_measurement_refs)
        if self.verdict == 'improvement_verified':
            if not self.post_measurement_refs:
                raise ValueError(
                    'improvement_verified requires post measurements')
            if not any(d.outcome == 'improved' for d in self.deltas):
                raise ValueError(
                    'improvement_verified requires an improved delta')
            if any(d.outcome == 'regressed' for d in self.deltas):
                raise ValueError(
                    'improvement_verified cannot include regressed '
                    'deltas')
        if self.verdict == 'regression_observed' \
                and not any(d.outcome == 'regressed' for d in self.deltas):
            raise ValueError(
                'regression_observed requires a regressed delta')
        if self.verdict == 'unmeasured' \
                and self.post_measurement_refs:
            raise ValueError(
                'unmeasured cannot pin post measurements')
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'DeploymentEffectivenessReport hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'report_id', 'report_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DeploymentEffectivenessReport':
        return _seal(cls, payload, 'report_id', 'report_sha256', 'defx')


class DeploymentRollbackRecord(BaseModel):
    """Rollback/replacement of one deployment (drbk-).

    Only evidence bound to the superseded deployment is invalidated —
    explicitly listed in ``affected_evidence_refs``. Unbound room,
    material and geometry evidence is structurally untouched.
    """

    model_config = ConfigDict(frozen=True)

    rollback_id: str
    rollback_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    deployment_ref: AuthorityRef
    rolled_back_at_utc: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    affected_evidence_refs: tuple[AuthorityRef, ...] = ()
    replacement_deployment_ref: AuthorityRef | None = None
    operator_ref: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'DeploymentRollbackRecord':
        _require_refs(self.deployment_ref)
        if self.deployment_ref.kind != 'calibration_deployment':
            raise ValueError(
                "deployment_ref kind must be 'calibration_deployment'")
        _require_refs(*self.affected_evidence_refs)
        _require_refs(self.replacement_deployment_ref)
        if self.replacement_deployment_ref is not None \
                and self.replacement_deployment_ref.kind \
                != 'calibration_deployment':
            raise ValueError(
                "replacement_deployment_ref kind must be "
                "'calibration_deployment'")
        if self.rollback_sha256 != _hash(self.identity_payload()):
            raise ValueError('DeploymentRollbackRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'rollback_id', 'rollback_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DeploymentRollbackRecord':
        return _seal(
            cls, payload, 'rollback_id', 'rollback_sha256', 'drbk')


# ----------------------------------------------------------------------
# evaluators


def evaluate_deployment_gate(
    materialization: MaterializedCalibrationSettings,
    declaration: DeploymentCapabilityDeclaration | None = None,
) -> tuple[DeploymentGateVerdict, str]:
    """Pre-apply gate: can this materialization be honestly deployed?

    Fails closed: any device-side drop/clamp the adapter surfaced during
    materialization blocks the deployment instead of silently shipping a
    degraded configuration. A missing capability declaration also
    blocks — deploy decisions require declared adapter capabilities.
    """
    if declaration is None:
        return ('deployment_blocked', 'no_capability_declaration')
    if not declaration.supports_materialization:
        return ('deployment_blocked',
                'adapter:materialization_unsupported')
    if not declaration.supports_apply:
        return ('deployment_blocked', 'adapter:apply_unsupported')
    if materialization.unsupported_items:
        return (
            'deployment_blocked',
            'unsupported_items:'
            + ','.join(materialization.unsupported_items))
    return ('deployable',
            'quantization_notes:'
            + ','.join(materialization.quantization_notes)
            if materialization.quantization_notes else 'clean')


def derive_deployment_state(
    observed_snapshot_source: Literal['read_back', 'operator_entered'] | None,
    observed_divergence: bool = False,
) -> tuple[DeploymentState, DeploymentEvidenceMode]:
    """Strongest honest deployment state the evidence supports.

    ``observed_snapshot_source`` is the ``EffectiveAppliedSettingsSnapshot.source``
    (``'read_back'`` / ``'operator_entered'``) or ``None`` when no state
    was observed. ``observed_divergence`` is set by the caller's drift
    analysis when the observed state cannot be reconciled with the
    pinned export (``export ⊕ deviations`` semantics).
    """
    if observed_snapshot_source is None:
        return ('deployment_unverified', 'unverified_export')
    if observed_divergence:
        if observed_snapshot_source == 'read_back':
            return ('deployment_mismatch', 'machine_readback')
        return ('deployment_mismatch', 'operator_attestation')
    if observed_snapshot_source == 'read_back':
        return ('deployment_verified', 'machine_readback')
    return ('deployment_attested', 'operator_attestation')


def evaluate_deployment_effectiveness(
    deployment: CalibrationDeployment | None,
    report: DeploymentEffectivenessReport | None,
) -> tuple[EffectivenessVerdict, str]:
    """Can the post-deployment comparison be believed?

    Improvement claims require a deployment whose state is at least
    operator-attested — comparing measurements against an unverified or
    blocked deployment is inconclusive by construction.
    """
    if deployment is None:
        return ('inconclusive', 'no_deployment')
    if report is None:
        return ('unmeasured', 'no_effectiveness_report')
    if report.deployment_ref.ref_id != deployment.deployment_id:
        return ('inconclusive', 'report_targets_other_deployment')
    if deployment.deployment_state in (
            'deployment_unverified', 'deployment_blocked'):
        return ('inconclusive',
                'deployment_state:' + deployment.deployment_state)
    if deployment.deployment_state == 'deployment_superseded':
        return ('inconclusive', 'deployment_superseded')
    if deployment.deployment_state == 'deployment_mismatch':
        return ('inconclusive', 'deployment_mismatch')
    return (report.verdict, 'report_verdict:' + report.verdict)


def deployment_state_with_rollbacks(
    deployment: CalibrationDeployment,
    rollbacks: tuple[DeploymentRollbackRecord, ...],
) -> DeploymentState:
    """Read-time state: a rollback superseding this deployment
    invalidates it without touching any other evidence class."""
    for rollback in rollbacks:
        if rollback.deployment_ref.ref_id == deployment.deployment_id:
            return 'deployment_superseded'
    return deployment.deployment_state


def rollback_invalidates_evidence(
    rollback: DeploymentRollbackRecord,
    evidence_ref: AuthorityRef,
) -> bool:
    """Scope check: only explicitly bound evidence is invalidated."""
    return any(
        ref.kind == evidence_ref.kind
        and ref.ref_id == evidence_ref.ref_id
        for ref in rollback.affected_evidence_refs
    )
