"""Display 1D/3D LUT closed-loop calibration authority (#666).

A generated LUT file is **not** proof of calibrated displayed output.
Professional tools treat the loop as first-class: characterise → generate
the LUT → verify the artifact numerically (and through an Active-LUT-style
simulation path) **before** upload → deploy to an exact device/slot →
read back the persisted state → re-measure independently after upload.
Each stage is a distinct sealed record; a later stage never upgrades an
earlier one retroactively.

Artifact taxonomy (issue §1) — these are never interchangeable:

- ``1d_lut`` — per-channel tone/EOTF correction only;
- ``3d_lut`` — volumetric colour correction;
- ``matrix_3x3_or_other`` — linear matrix model;
- ``shaper_plus_3d_lut``;
- ``device_native_calibration_table`` — vendor-internal calibration
  object;
- ``icc_vcgt_or_os_profile`` — OS/gamma tag, not a display LUT;
- ``external_provider_artifact``;
- ``unknown``.

A 1D grayscale/EOTF correction never implies gamut/volumetric correction.

Contract properties:

- every artifact binds an exact target (primaries/white point, EOTF,
  SDR/PQ/HLG profile, peak/reference luminance, range semantics) — a
  ``Rec.709`` label without target semantics is rejected;
- generation records pin the characterisation measurement set, the probe
  correction (#680), the stimulus/pattern source (#608/#682), the display
  state (#625), and the algorithm + version + parameters — never only
  the final .cube file;
- domain scaling is explicit: input code domain, LUT internal domain,
  output code domain, device-expected LUT scale and signal-path range are
  independent declarations — a mathematically correct LUT with wrong
  domain scaling is deployment-invalid;
- a device write is only claimed by a readback hash equal to the
  artifact hash — ``written = true`` is not evidence;
- post-upload verification uses independent holdout patches over the
  same physical signal path — regenerating the training set is not
  verification;
- calibration that improves colour while harming gradation, clipping,
  latency or dynamic behavior is reported as ``loop_limited``, not
  silently scored as a pass.

Literature basis (issue §research): ColourSpace current documentation
(profiling → LUT generation → Active LUT verification → upload → second
post-upload verification; full/data vs video/legal scaling regression
case); Portrait Displays AutoCal device-write workflows (device-native
calibration tables are a distinct artifact kind).
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


LUT_SCHEMA_VERSION = 'lut-closed-loop-1'
LUT_EVALUATION_VERSION = 'lut-closed-loop-eval-1'

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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

LutArtifactKind = Literal[
    '1d_lut',
    '3d_lut',
    'matrix_3x3_or_other',
    'shaper_plus_3d_lut',
    'device_native_calibration_table',
    'icc_vcgt_or_os_profile',
    'external_provider_artifact',
    'unknown',
]

CodeDomain = Literal[
    'full_range',
    'legal_narrow',
    'extended',
    'normalized_float',
    'unknown',
]

EotfTarget = Literal[
    'bt1886_gamma',
    'gamma_2_2',
    'gamma_2_4',
    'pq_st2084',
    'hlg',
    'linear',
    'unknown',
]

LutCapabilityKind = Literal[
    'tone_eotf_correction',
    'white_point_correction',
    'gamut_correction',
    'volumetric_correction',
    'vendor_internal_correction',
]

LoopVerdict = Literal[
    'loop_closed',
    'loop_limited',
    'loop_unverified',
    'loop_failed',
    'insufficient_evidence',
]

LoopReason = Literal[
    'LOOP_CLOSED',
    'PREFLIGHT_PASSED',
    'PREFLIGHT_FAILED',
    'PREFLIGHT_MISSING',
    'DEPLOYMENT_READBACK_MATCH',
    'DEPLOYMENT_READBACK_MISMATCH',
    'DEPLOYMENT_UNVERIFIED',
    'POST_VERIFICATION_PASSED',
    'POST_VERIFICATION_FAILED',
    'POST_VERIFICATION_MISSING',
    'HOLDOUT_NOT_INDEPENDENT',
    'DOMAIN_SCALING_UNDECLARED',
    'TARGET_SEMANTICS_MISSING',
    'GENERATION_PROVENANCE_MISSING',
    'SIDE_EFFECT_OBSERVED',
    'ARTIFACT_KIND_UNSUPPORTED',
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class LutTargetSpec(BaseModel):
    """Exact target semantics (issue §2) — never a bare label."""

    model_config = ConfigDict(frozen=True)

    primaries: str = Field(min_length=1)
    white_point: str = Field(min_length=1)
    eotf: EotfTarget
    signal_profile: str = ''
    target_peak_nits: float | None = Field(default=None, gt=0)
    reference_white_nits: float | None = Field(default=None, gt=0)
    black_level_semantics: str = ''
    viewing_environment_ref: AuthorityRef | None = None
    target_revision_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @model_validator(mode='after')
    def _check(self) -> 'LutTargetSpec':
        if self.eotf in ('pq_st2084', 'hlg') and self.target_peak_nits is None:
            raise ValueError(
                'an HDR target must declare its peak luminance — '
                'a bare HDR label is not target semantics'
            )
        if (
            self.viewing_environment_ref is not None
            and self.viewing_environment_ref.ref_sha256 is None
        ):
            raise ValueError('viewing environment ref must pin sha256')
        return self


class LutDomainSpec(BaseModel):
    """Independent domain declarations (issue §6) — a mathematically
    correct LUT with wrong domain scaling is deployment-invalid."""

    model_config = ConfigDict(frozen=True)

    input_code_domain: CodeDomain
    internal_domain: CodeDomain
    output_code_domain: CodeDomain
    device_expected_scale: CodeDomain
    signal_path_range: CodeDomain

    @model_validator(mode='after')
    def _check(self) -> 'LutDomainSpec':
        declared = (
            self.input_code_domain, self.internal_domain,
            self.output_code_domain, self.device_expected_scale,
            self.signal_path_range,
        )
        if any(d == 'unknown' for d in declared):
            raise ValueError(
                'every LUT domain must be declared — unknown scaling is '
                'a provenance defect'
            )
        return self


class LutGenerationSpec(BaseModel):
    """Algorithm identity for a generated LUT (issue §5)."""

    model_config = ConfigDict(frozen=True)

    engine: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    interpolation: str = Field(min_length=1)
    regularization: str = ''
    out_of_gamut_policy: str = ''
    black_handling: str = ''
    luminance_normalization: str = ''
    gamut_mapping: str = ''
    shaper_strategy: str = ''
    deterministic_seed: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'LutGenerationSpec':
        if not self.gamut_mapping:
            raise ValueError(
                'gamut mapping policy must be declared'
            )
        return self


class DisplayLUTArtifact(BaseModel):
    """The sealed LUT artifact — kind, grid, domains, target, content."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(pattern=r'^lutart-[0-9a-f]{24}$')
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    kind: LutArtifactKind
    grid_size: int | None = Field(default=None, ge=2)
    entry_count: int | None = Field(default=None, ge=1)
    precision_bits: int | None = Field(default=None, ge=8, le=32)
    domains: LutDomainSpec
    target: LutTargetSpec
    artifact_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_format: str = ''

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'artifact_id', 'artifact_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DisplayLUTArtifact':
        if self.kind == 'unknown':
            raise ValueError('LUT kind must be declared')
        if self.kind in ('3d_lut', 'shaper_plus_3d_lut'):
            if self.grid_size is None:
                raise ValueError('a 3D LUT must declare its grid size')
        if self.kind == '1d_lut' and self.entry_count is None:
            raise ValueError('a 1D LUT must declare its entry count')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'DisplayLUTArtifact':
        return _seal(
            cls, kwargs, 'artifact_id', 'artifact_sha256', 'lutart'
        )


class LUTGenerationRecord(BaseModel):
    """Provenance: which measurements + settings generated the artifact
    (issue §3–§5)."""

    model_config = ConfigDict(frozen=True)

    generation_id: str = Field(pattern=r'^lutgen-[0-9a-f]{24}$')
    generation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    artifact_ref: AuthorityRef
    display_state_ref: AuthorityRef
    characterisation_ref: AuthorityRef
    patch_set_ref: AuthorityRef
    meter_correction_ref: AuthorityRef | None = None
    generator_ref: AuthorityRef | None = None
    uncertainty_ref: AuthorityRef | None = None
    generation: LutGenerationSpec
    generated_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'generation_id', 'generation_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'LUTGenerationRecord':
        _require_refs(
            self.artifact_ref, self.display_state_ref,
            self.characterisation_ref, self.patch_set_ref,
        )
        for ref in (
            self.meter_correction_ref, self.generator_ref,
            self.uncertainty_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        _require_iso8601(self.generated_at_utc, 'generated_at_utc')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'LUTGenerationRecord':
        return _seal(
            cls, kwargs, 'generation_id', 'generation_sha256', 'lutgen'
        )


class LUTPreflightVerification(BaseModel):
    """Numeric/simulated verification before upload (issue goal §4–5)."""

    model_config = ConfigDict(frozen=True)

    preflight_id: str = Field(pattern=r'^lutpre-[0-9a-f]{24}$')
    preflight_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    artifact_ref: AuthorityRef
    numeric_validation_passed: bool
    simulated_residuals_delta_e: tuple[float, ...] = ()
    simulation_kind: Literal[
        'active_lut_path', 'numeric_only', 'none'
    ] = 'numeric_only'
    domain_scaling_checked: bool = False
    verified_at_utc: str
    notes: str = ''

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'preflight_id', 'preflight_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'LUTPreflightVerification':
        _require_refs(self.artifact_ref)
        _require_iso8601(self.verified_at_utc, 'verified_at_utc')
        if self.numeric_validation_passed and not self.domain_scaling_checked:
            raise ValueError(
                'a numeric pass without a domain-scaling check is not a '
                'preflight pass'
            )
        if any(
            not isfinite(v) or v < 0 for v in self.simulated_residuals_delta_e
        ):
            raise ValueError('simulated residuals must be non-negative')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'LUTPreflightVerification':
        return _seal(
            cls, kwargs, 'preflight_id', 'preflight_sha256', 'lutpre'
        )


class LUTDeploymentRecord(BaseModel):
    """Which exact device/slot received the artifact, with readback
    (issue §6).  A write without matching readback is not deployment."""

    model_config = ConfigDict(frozen=True)

    deployment_id: str = Field(pattern=r'^lutdep-[0-9a-f]{24}$')
    deployment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    artifact_ref: AuthorityRef
    device_instance: str = Field(min_length=1)
    device_model: str = Field(min_length=1)
    firmware: str = ''
    slot: str = Field(min_length=1)
    write_method: str = Field(min_length=1)
    device_snapshot_ref: AuthorityRef | None = None
    deployed_at_utc: str
    readback_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    """Hash of the device state read back after the write."""

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'deployment_id', 'deployment_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'LUTDeploymentRecord':
        _require_refs(self.artifact_ref)
        if (
            self.device_snapshot_ref is not None
            and self.device_snapshot_ref.ref_sha256 is None
        ):
            raise ValueError('device snapshot ref must pin sha256')
        _require_iso8601(self.deployed_at_utc, 'deployed_at_utc')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'LUTDeploymentRecord':
        return _seal(
            cls, kwargs, 'deployment_id', 'deployment_sha256', 'lutdep'
        )


class LUTPostVerification(BaseModel):
    """Independent post-upload verification (issue §7–8)."""

    model_config = ConfigDict(frozen=True)

    post_verification_id: str = Field(pattern=r'^lutpost-[0-9a-f]{24}$')
    post_verification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    deployment_ref: AuthorityRef
    holdout_patch_set_ref: AuthorityRef
    """Must differ from the generation patch set — re-measuring the
    training set is not verification."""
    same_physical_path: bool
    residuals_delta_e: tuple[float, ...]
    pass_threshold_delta_e: float = Field(gt=0)
    side_effects: tuple[str, ...] = ()
    """Observed regressions: gradation_loss / clipping / added_latency /
    dynamic_behavior_change — reported, never hidden."""
    verified_at_utc: str
    passed: bool

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={
                'post_verification_id', 'post_verification_sha256'
            },
        )

    @model_validator(mode='after')
    def _check(self) -> 'LUTPostVerification':
        _require_refs(self.deployment_ref, self.holdout_patch_set_ref)
        _require_iso8601(self.verified_at_utc, 'verified_at_utc')
        if not self.residuals_delta_e:
            raise ValueError('post verification needs residuals')
        if any(
            not isfinite(v) or v < 0 for v in self.residuals_delta_e
        ):
            raise ValueError('residuals must be finite non-negative')
        if (
            self.passed
            and max(self.residuals_delta_e) > self.pass_threshold_delta_e
        ):
            raise ValueError(
                'a passing post-verification cannot exceed its threshold'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'LUTPostVerification':
        return _seal(
            cls, kwargs,
            'post_verification_id', 'post_verification_sha256', 'lutpost',
        )


class LUTLoopQualification(BaseModel):
    """Sealed closed-loop verdict (issue goal)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(pattern=r'^lutq-[0-9a-f]{24}$')
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    artifact_ref: AuthorityRef
    generation_ref: AuthorityRef | None = None
    preflight_ref: AuthorityRef | None = None
    deployment_ref: AuthorityRef | None = None
    post_verification_ref: AuthorityRef | None = None
    verdict: LoopVerdict
    reasons: tuple[LoopReason, ...]
    evaluated_at_utc: str
    evaluation_version: str = LUT_EVALUATION_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'LUTLoopQualification':
        _require_refs(self.artifact_ref)
        for ref in (
            self.generation_ref, self.preflight_ref,
            self.deployment_ref, self.post_verification_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'LUTLoopQualification':
        return _seal(
            cls, kwargs,
            'qualification_id', 'qualification_sha256', 'lutq',
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_lut_closed_loop(
    artifact: DisplayLUTArtifact,
    *,
    generation: LUTGenerationRecord | None,
    preflight: LUTPreflightVerification | None,
    deployment: LUTDeploymentRecord | None,
    post_verification: LUTPostVerification | None,
    generation_patch_set_sha256: str | None,
    evaluated_at_utc: str | None = None,
) -> LUTLoopQualification:
    """Evaluate the full calibration loop for one artifact.

    fail-closed ladder:
    - generation provenance absent → ``insufficient_evidence``;
    - preflight missing/failed → ``loop_unverified`` / ``loop_failed``;
    - deployment readback missing → ``loop_unverified``; mismatched →
      ``loop_failed``;
    - post-verification missing or holdout not independent →
      ``loop_unverified``; failed → ``loop_failed``;
    - observed side effects cap the verdict at ``loop_limited``.
    """
    reasons: list[LoopReason] = []
    refs: dict[str, AuthorityRef | None] = {
        'generation': None, 'preflight': None,
        'deployment': None, 'post': None,
    }
    if generation is not None:
        refs['generation'] = AuthorityRef(
            kind='lut_generation_record',
            ref_id=generation.generation_id,
            ref_sha256=generation.generation_sha256,
        )
        if generation.artifact_ref.ref_sha256 != artifact.artifact_sha256:
            reasons.append('GENERATION_PROVENANCE_MISSING')
            generation = None
    if generation is None:
        reasons.append('GENERATION_PROVENANCE_MISSING')

    verdict: LoopVerdict = 'loop_closed'
    if preflight is None:
        verdict = 'loop_unverified'
        reasons.append('PREFLIGHT_MISSING')
    elif not preflight.numeric_validation_passed:
        verdict = 'loop_failed'
        reasons.append('PREFLIGHT_FAILED')
    else:
        reasons.append('PREFLIGHT_PASSED')

    if verdict != 'loop_failed':
        if deployment is None or deployment.readback_sha256 is None:
            if verdict == 'loop_closed':
                verdict = 'loop_unverified'
            reasons.append('DEPLOYMENT_UNVERIFIED')
        elif deployment.readback_sha256 != artifact.artifact_payload_sha256:
            verdict = 'loop_failed'
            reasons.append('DEPLOYMENT_READBACK_MISMATCH')
        else:
            reasons.append('DEPLOYMENT_READBACK_MATCH')

    if verdict != 'loop_failed':
        if post_verification is None:
            if verdict == 'loop_closed':
                verdict = 'loop_unverified'
            reasons.append('POST_VERIFICATION_MISSING')
        else:
            if (
                generation_patch_set_sha256 is not None
                and post_verification.holdout_patch_set_ref.ref_sha256
                == generation_patch_set_sha256
            ):
                if verdict == 'loop_closed':
                    verdict = 'loop_unverified'
                reasons.append('HOLDOUT_NOT_INDEPENDENT')
            elif not post_verification.same_physical_path:
                if verdict == 'loop_closed':
                    verdict = 'loop_unverified'
                reasons.append('HOLDOUT_NOT_INDEPENDENT')
            elif not post_verification.passed:
                verdict = 'loop_failed'
                reasons.append('POST_VERIFICATION_FAILED')
            else:
                reasons.append('POST_VERIFICATION_PASSED')
            if post_verification.side_effects and verdict == 'loop_closed':
                verdict = 'loop_limited'
                reasons.append('SIDE_EFFECT_OBSERVED')

    if generation is None and verdict == 'loop_closed':
        verdict = 'insufficient_evidence'
    if verdict == 'loop_closed':
        reasons.append('LOOP_CLOSED')

    return LUTLoopQualification.create(
        document_id=artifact.document_id,
        artifact_ref=AuthorityRef(
            kind='display_lut_artifact',
            ref_id=artifact.artifact_id,
            ref_sha256=artifact.artifact_sha256,
        ),
        generation_ref=refs['generation'],
        preflight_ref=(
            None if preflight is None else AuthorityRef(
                kind='lut_preflight_verification',
                ref_id=preflight.preflight_id,
                ref_sha256=preflight.preflight_sha256,
            )
        ),
        deployment_ref=(
            None if deployment is None else AuthorityRef(
                kind='lut_deployment_record',
                ref_id=deployment.deployment_id,
                ref_sha256=deployment.deployment_sha256,
            )
        ),
        post_verification_ref=(
            None if post_verification is None else AuthorityRef(
                kind='lut_post_verification',
                ref_id=post_verification.post_verification_id,
                ref_sha256=post_verification.post_verification_sha256,
            )
        ),
        verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
        evaluated_at_utc=evaluated_at_utc or _utc_now(),
    )


# ---------------------------------------------------------------------------
# JA labels
# ---------------------------------------------------------------------------

ARTIFACT_KIND_LABELS: dict[str, str] = {
    '1d_lut': '1D LUT',
    '3d_lut': '3D LUT',
    'matrix_3x3_or_other': '3×3マトリクス等',
    'shaper_plus_3d_lut': 'シェーパー+3D LUT',
    'device_native_calibration_table': 'デバイス固有較正テーブル',
    'icc_vcgt_or_os_profile': 'ICC/VCGT/OS プロファイル',
    'external_provider_artifact': '外部プロバイダーアーティファクト',
    'unknown': '不明',
}

VERDICT_LABELS: dict[str, str] = {
    'loop_closed': 'LUTループ完了',
    'loop_limited': 'LUTループ条件付き完了',
    'loop_unverified': 'LUTループ未検証',
    'loop_failed': 'LUTループ失敗',
    'insufficient_evidence': 'LUTループ証拠不足',
}

REASON_LABELS: dict[str, str] = {
    'LOOP_CLOSED': 'LUT較正ループは閉じています',
    'PREFLIGHT_PASSED': '転送前検証に合格しました',
    'PREFLIGHT_FAILED': '転送前検証に失敗しました',
    'PREFLIGHT_MISSING': '転送前検証が未実施です',
    'DEPLOYMENT_READBACK_MATCH': 'デバイス読み戻しが一致しました',
    'DEPLOYMENT_READBACK_MISMATCH': 'デバイス読み戻しが一致しません',
    'DEPLOYMENT_UNVERIFIED': 'デプロイが未検証です',
    'POST_VERIFICATION_PASSED': '転送後検証に合格しました',
    'POST_VERIFICATION_FAILED': '転送後検証に失敗しました',
    'POST_VERIFICATION_MISSING': '転送後検証が未実施です',
    'HOLDOUT_NOT_INDEPENDENT': '検証パッチが独立ではありません',
    'DOMAIN_SCALING_UNDECLARED': 'ドメインスケーリングが未宣言です',
    'TARGET_SEMANTICS_MISSING': '目標セマンティクスが不足しています',
    'GENERATION_PROVENANCE_MISSING': '生成プロベナンスが不足しています',
    'SIDE_EFFECT_OBSERVED': '副作用が観測されました',
    'ARTIFACT_KIND_UNSUPPORTED': 'アーティファクト種別が未対応です',
}
