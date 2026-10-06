"""Video test-pattern-generator fidelity authority (#682).

The exact test asset a calibration workflow requests is **not** proof that
the pattern generator, GPU, player or internal display TPG emitted the
intended code values, range, bit depth, colour encoding and HDR metadata at
the display input.  Field failures exist (a 2025 LG C5 internal-TPG bug
crushed near-black patches under one calibration tool until a later
software release), so generator identity and delivered-signal verification
are part of measurement provenance — not a UI nicety.

The authority keeps five identities separate:

- the logical test asset / patch — owned by the #608 stimulus registry and
  referenced here only by pin;
- the **requested** numeric patch identity — exact code triplets, bit
  depth, range, encoding, EOTF, colorimetry, chroma sampling, raster,
  refresh, HDR metadata, window/APL, dwell;
- the **generator instance** — hardware / software / display-internal /
  LUT-box / video-processor / media-player, with model, serial, firmware,
  control software and API versions (bugs are version-specific);
- the **delivered signal observation** — what was actually measured or
  independently read back on the link, at the display input, or reported
  by the device;
- the **qualification verdict** — fail-closed.

Contract properties:

- ``pattern displayed = true`` is never sufficient evidence;
- an unverified generator path cannot qualify for the claimed precision —
  it returns ``insufficient_evidence``;
- a delivered mismatch (code values, range, bit depth, chroma, HDR
  metadata) is a ``fidelity_unqualified`` defect, never silently rounded;
- control-input bit depth, internal precision and output-link bit depth
  are independent declarations — a 10-bit output label never proves
  10-bit addressability;
- Legal/Full/Extended rescaling and RGB↔Y'CbCr conversion are declared
  transforms — their absence in provenance is itself evidence-gating;
- firmware/software changes stale prior qualification: a qualification
  sha-pins the exact generator instance record.

Literature basis (issue §research): ITU-R BT.2111-3 (05/2025) — exact
HDR test-pattern code values and 10/12-bit code-value precision;
ColourSpace TPG/patch-scale documentation — Legal/Full/Extended scaling
and bit-depth conversion can duplicate or skip patch values; Portrait
Displays generator ecosystem and the documented LG C5 internal-TPG
field failure.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


PATTERN_GENERATOR_SCHEMA_VERSION = 'pattern-generator-fidelity-1'
PATTERN_GENERATOR_EVALUATION_VERSION = 'pattern-generator-eval-1'

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


# ---------------------------------------------------------------------------
# Taxonomies (issue §1–§8)
# ---------------------------------------------------------------------------

GeneratorClass = Literal[
    'hardware_generator',
    'software_generator',
    'display_internal_tpg',
    'lut_box',
    'video_processor',
    'media_player',
    'gpu_output',
    'unknown',
]
"""What renders the patch. ``display_internal_tpg`` and ``gpu_output`` are
explicit members because both have documented field-failure modes and must
never be implied by the host device class."""

SignalRange = Literal[
    'full',
    'legal_narrow',
    'extended',
    'unknown',
]
"""Video range. ``legal_narrow`` covers the broadcast/legal (limited)
domain; ``unknown`` is honest, never defaulted."""

ColorEncoding = Literal[
    'rgb',
    'ycbcr_444',
    'ycbcr_422',
    'ycbcr_420',
    'icncp',
    'unknown',
]

ColorimetrySpec = Literal[
    'bt601_625',
    'bt601_525',
    'bt709',
    'bt2020',
    'dci_p3',
    'unknown',
]

EotfSpec = Literal[
    'bt1886_gamma',
    'gamma_2_2',
    'gamma_2_4',
    'pq_st2084',
    'hlg',
    'linear',
    'unknown',
]

HdrMetadataState = Literal['none', 'static', 'dynamic', 'unknown']

VerificationMeans = Literal[
    'protocol_readback',
    'link_analyzer_measurement',
    'display_self_report',
    'optical_patch_verification',
    'visual_inspection',
    'unverified',
]

ObservationPoint = Literal[
    'generator_output',
    'link_negotiated',
    'display_input',
    'displayed_optical',
]
"""Where in the chain the delivered signal was observed. Optical
verification at the display is the strongest; a negotiated-mode report is
weaker than a measured link capture."""

MismatchKind = Literal[
    'code_value_shift',
    'range_mapping',
    'bit_depth_quantization',
    'chroma_subsampling',
    'color_matrix',
    'eotf_mismatch',
    'hdr_metadata',
    'patch_geometry',
    'timing_refresh',
    'content_replacement',
]

QualificationVerdict = Literal[
    'fidelity_qualified',
    'fidelity_qualified_with_limitations',
    'fidelity_insufficient_evidence',
    'fidelity_unqualified',
]

FidelityReason = Literal[
    'GENERATOR_FIDELITY_QUALIFIED',
    'GENERATOR_FIDELITY_LIMITED',
    'DELIVERED_MATCHES_REQUESTED',
    'DELIVERED_MISMATCH',
    'DELIVERY_UNVERIFIED',
    'GENERATOR_INSTANCE_INCOMPLETE',
    'PATCH_IDENTITY_INCOMPLETE',
    'BIT_DEPTH_CONVERSION_DECLARED',
    'RANGE_MAPPING_UNDECLARED',
    'HDR_METADATA_UNDECLARED',
    'STIMULUS_REF_UNPINNED',
    'OBSERVATION_STALE',
    'CLAIMED_PRECISION_EXCEEDS_PROOF',
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class GeneratorCapability(BaseModel):
    """Declared generator precision at each stage (issue §4).

    Input control depth, internal processing precision and output-link
    depth are independent — a 10-bit output label never proves 10-bit
    addressability upstream.
    """

    model_config = ConfigDict(frozen=True)

    control_input_bit_depth: int | None = Field(default=None, ge=8, le=16)
    internal_processing_bit_depth: int | None = Field(
        default=None, ge=8, le=32
    )
    output_link_bit_depth: int | None = Field(default=None, ge=8, le=16)
    supports_rgb: bool = True
    supports_ycbcr: bool = True
    supported_ranges: tuple[SignalRange, ...] = ()
    max_raster_hz: float | None = Field(default=None, gt=0)
    notes: str = ''


class PatternGeneratorInstance(BaseModel):
    """Sealed identity of the exact generator (issue §2).

    Firmware/software belong to identity because generator behaviour is
    version-specific — changing either stales prior qualification.
    """

    model_config = ConfigDict(frozen=True)

    generator_id: str = Field(pattern=r'^pginst-[0-9a-f]{24}$')
    generator_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    generator_class: GeneratorClass
    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    instance_serial: str = ''
    firmware: str = ''
    control_software: str = ''
    control_software_version: str = ''
    api_adapter: str = ''
    api_adapter_version: str = ''
    output_port: str = ''
    config_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    capability: GeneratorCapability = Field(
        default_factory=GeneratorCapability
    )
    notes: str = ''

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'generator_id', 'generator_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'PatternGeneratorInstance':
        return _seal(
            cls, kwargs, 'generator_id', 'generator_sha256', 'pginst'
        )


class PatchNumericIdentity(BaseModel):
    """Exact numeric identity of one requested patch (issue §3)."""

    model_config = ConfigDict(frozen=True)

    code_values: tuple[float, float, float]
    bit_depth: int = Field(ge=8, le=16)
    signal_range: SignalRange
    color_encoding: ColorEncoding
    colorimetry: ColorimetrySpec
    eotf: EotfSpec
    chroma_sampling: str = ''
    raster: str = ''
    refresh_hz: float | None = Field(default=None, gt=0)
    hdr_metadata: HdrMetadataState = 'none'
    hdr_metadata_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @model_validator(mode='after')
    def _check(self) -> 'PatchNumericIdentity':
        if any(not isfinite(v) for v in self.code_values):
            raise ValueError('code values must be finite')
        if self.signal_range == 'unknown':
            raise ValueError(
                'patch signal range must be declared — unknown range is '
                'a provenance defect, not a patch property'
            )
        if self.hdr_metadata != 'none' and self.hdr_metadata_sha256 is None:
            raise ValueError(
                'static/dynamic HDR metadata must pin its exact content'
            )
        if self.color_encoding != 'rgb' and not self.chroma_sampling:
            raise ValueError(
                'non-RGB encodings must declare chroma sampling'
            )
        return self


class RequestedVideoPatch(BaseModel):
    """One requested patch — stimulus pin plus exact numerics."""

    model_config = ConfigDict(frozen=True)

    patch_id: str = Field(pattern=r'^reqpatch-[0-9a-f]{24}$')
    patch_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    stimulus_ref: AuthorityRef
    numeric: PatchNumericIdentity
    window_size_percent: float | None = Field(
        default=None, ge=0.0, le=100.0
    )
    apl_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    background_level: str = ''
    patch_position: str = ''
    dwell_s: float | None = Field(default=None, gt=0)
    sequence_index: int | None = Field(default=None, ge=0)
    label: str = ''

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'patch_id', 'patch_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'RequestedVideoPatch':
        if self.stimulus_ref.ref_sha256 is None:
            raise ValueError(
                'patch must sha-pin its #608 stimulus asset'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'RequestedVideoPatch':
        return _seal(
            cls, kwargs, 'patch_id', 'patch_sha256', 'reqpatch'
        )


class SignalTransformDeclaration(BaseModel):
    """A declared transform in the generator output path (issue §5)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[
        'range_rescale',
        'rgb_ycbcr_conversion',
        'ycbcr_rgb_conversion',
        'bit_depth_conversion',
        'dithering',
        'hdr_metadata_rewrite',
    ]
    direction_detail: str = ''
    deterministic_mapping_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )


class DeliveredStimulusObservation(BaseModel):
    """What actually left the generator / arrived at the display (§6).

    ``verification='unverified'`` is legal — it records that no delivered-
    signal evidence exists and caps the qualification accordingly.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(pattern=r'^delobs-[0-9a-f]{24}$')
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    patch_ref: AuthorityRef
    generator_ref: AuthorityRef
    observation_point: ObservationPoint
    verification: VerificationMeans
    delivered_code_values: tuple[float, float, float] | None = None
    delivered_bit_depth: int | None = Field(default=None, ge=8, le=16)
    delivered_range: SignalRange = 'unknown'
    delivered_encoding: ColorEncoding = 'unknown'
    delivered_colorimetry: ColorimetrySpec = 'unknown'
    delivered_eotf: EotfSpec = 'unknown'
    delivered_hdr_metadata_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    observed_at_utc: str
    declared_transforms: tuple[SignalTransformDeclaration, ...] = ()
    evidence_ref: AuthorityRef | None = None
    notes: str = ''

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'observation_id', 'observation_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DeliveredStimulusObservation':
        _require_refs(self.patch_ref, self.generator_ref)
        try:
            from datetime import datetime
            datetime.fromisoformat(self.observed_at_utc)
        except ValueError as exc:
            raise ValueError('observed_at_utc must be ISO-8601') from exc
        if self.verification == 'unverified':
            if self.delivered_code_values is not None:
                raise ValueError(
                    'an unverified observation cannot assert delivered '
                    'code values'
                )
        elif self.verification != 'visual_inspection':
            if self.delivered_code_values is None:
                raise ValueError(
                    'a non-visual verification must record delivered '
                    'code values'
                )
            if self.delivered_bit_depth is None:
                raise ValueError(
                    'a non-visual verification must record delivered '
                    'bit depth'
                )
        if (
            self.verification in ('protocol_readback', 'link_analyzer_measurement')
            and self.evidence_ref is None
        ):
            raise ValueError(
                'readback/analyzer observations must pin their evidence'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'DeliveredStimulusObservation':
        return _seal(
            cls, kwargs, 'observation_id', 'observation_sha256', 'delobs'
        )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(
                f'{ref.kind} reference must pin its sha256'
            )


class PatchMismatch(BaseModel):
    """One proven mismatch between requested and delivered identity."""

    model_config = ConfigDict(frozen=True)

    kind: MismatchKind
    detail: str = Field(min_length=1)
    expected: str = ''
    observed: str = ''


class PrecisionRequirement(BaseModel):
    """The precision the downstream calibration claims to need."""

    model_config = ConfigDict(frozen=True)

    required_bit_depth: int = Field(ge=8, le=16)
    required_range: SignalRange
    required_encoding: ColorEncoding
    requires_hdr_metadata: bool = False
    tolerance_code_values: float = Field(default=0.0, ge=0.0)


class PatchQualification(BaseModel):
    """Per-patch verdict inside a generator-fidelity qualification."""

    model_config = ConfigDict(frozen=True)

    patch_ref: AuthorityRef
    verdict: Literal[
        'delivered_verified',
        'delivered_with_documented_transform',
        'delivered_mismatch',
        'delivery_unverified',
    ]
    mismatches: tuple[PatchMismatch, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'PatchQualification':
        if self.patch_ref.ref_sha256 is None:
            raise ValueError('patch ref must pin sha256')
        if self.verdict == 'delivered_mismatch' and not self.mismatches:
            raise ValueError('a mismatch verdict must list its mismatches')
        if self.verdict == 'delivered_verified' and self.mismatches:
            raise ValueError(
                'a verified verdict cannot carry mismatches'
            )
        return self


class GeneratorFidelityQualification(BaseModel):
    """Sealed fail-closed verdict for one generator path + patch set."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(pattern=r'^genfq-[0-9a-f]{24}$')
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    generator_ref: AuthorityRef
    requirement: PrecisionRequirement
    patch_verdicts: tuple[PatchQualification, ...]
    verdict: QualificationVerdict
    reasons: tuple[FidelityReason, ...]
    evaluation_version: str = PATTERN_GENERATOR_EVALUATION_VERSION
    issued_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'GeneratorFidelityQualification':
        _require_refs(self.generator_ref)
        if not self.patch_verdicts:
            raise ValueError('qualification needs at least one patch')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'GeneratorFidelityQualification':
        return _seal(
            cls, kwargs, 'qualification_id', 'qualification_sha256',
            'genfq',
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_patch_delivery(
    patch: RequestedVideoPatch,
    observation: DeliveredStimulusObservation | None,
    *,
    requirement: PrecisionRequirement,
) -> PatchQualification:
    """Compare one requested patch against its delivered observation.

    Never called on a patch whose stimulus pin is missing (the model
    rejects it at construction); an absent observation is honest
    ``delivery_unverified``, not an implied pass.
    """
    patch_ref = AuthorityRef(
        kind='requested_video_patch',
        ref_id=patch.patch_id,
        ref_sha256=patch.patch_sha256,
    )
    if observation is None or observation.verification == 'unverified':
        return PatchQualification(
            patch_ref=patch_ref,
            verdict='delivery_unverified',
        )
    numeric = patch.numeric
    mismatches: list[PatchMismatch] = []
    if observation.delivered_code_values is not None:
        delta = max(
            abs(d - r)
            for d, r in zip(
                observation.delivered_code_values, numeric.code_values
            )
        )
        if delta > requirement.tolerance_code_values:
            mismatches.append(
                PatchMismatch(
                    kind='code_value_shift',
                    detail='delivered code values differ from requested',
                    expected=str(numeric.code_values),
                    observed=str(observation.delivered_code_values),
                )
            )
    if (
        observation.delivered_range != 'unknown'
        and observation.delivered_range != numeric.signal_range
    ):
        mismatches.append(
            PatchMismatch(
                kind='range_mapping',
                detail='delivered signal range differs from requested',
                expected=numeric.signal_range,
                observed=observation.delivered_range,
            )
        )
    if (
        observation.delivered_bit_depth is not None
        and observation.delivered_bit_depth < numeric.bit_depth
    ):
        mismatches.append(
            PatchMismatch(
                kind='bit_depth_quantization',
                detail='delivered bit depth below requested domain',
                expected=str(numeric.bit_depth),
                observed=str(observation.delivered_bit_depth),
            )
        )
    if (
        observation.delivered_encoding != 'unknown'
        and observation.delivered_encoding != numeric.color_encoding
    ):
        mismatches.append(
            PatchMismatch(
                kind='color_matrix',
                detail='delivered colour encoding differs from requested',
                expected=numeric.color_encoding,
                observed=observation.delivered_encoding,
            )
        )
    if (
        numeric.hdr_metadata != 'none'
        and observation.delivered_hdr_metadata_sha256 is not None
        and observation.delivered_hdr_metadata_sha256
        != numeric.hdr_metadata_sha256
    ):
        mismatches.append(
            PatchMismatch(
                kind='hdr_metadata',
                detail='delivered HDR metadata differs from requested',
            )
        )
    if mismatches:
        return PatchQualification(
            patch_ref=patch_ref,
            verdict='delivered_mismatch',
            mismatches=tuple(mismatches),
        )
    if observation.declared_transforms:
        return PatchQualification(
            patch_ref=patch_ref,
            verdict='delivered_with_documented_transform',
        )
    return PatchQualification(
        patch_ref=patch_ref, verdict='delivered_verified'
    )


def evaluate_generator_fidelity(
    generator: PatternGeneratorInstance,
    patches: Sequence[RequestedVideoPatch],
    observations: dict[str, DeliveredStimulusObservation],
    *,
    requirement: PrecisionRequirement,
    issued_at_utc: str | None = None,
) -> GeneratorFidelityQualification:
    """Fail-closed verdict for one generator + patch set + requirement."""
    patch_verdicts = tuple(
        evaluate_patch_delivery(
            patch,
            observations.get(patch.patch_id),
            requirement=requirement,
        )
        for patch in patches
    )
    reasons: list[FidelityReason] = []
    if any(v.verdict == 'delivered_mismatch' for v in patch_verdicts):
        verdict: QualificationVerdict = 'fidelity_unqualified'
        reasons.append('DELIVERED_MISMATCH')
    elif all(v.verdict == 'delivered_verified' for v in patch_verdicts):
        verdict = 'fidelity_qualified'
        reasons.extend(
            ('GENERATOR_FIDELITY_QUALIFIED', 'DELIVERED_MATCHES_REQUESTED')
        )
    elif all(
        v.verdict
        in ('delivered_verified', 'delivered_with_documented_transform')
        for v in patch_verdicts
    ):
        verdict = 'fidelity_qualified_with_limitations'
        reasons.extend(
            ('GENERATOR_FIDELITY_LIMITED', 'BIT_DEPTH_CONVERSION_DECLARED')
        )
    else:
        verdict = 'fidelity_insufficient_evidence'
        reasons.append('DELIVERY_UNVERIFIED')
    cap = generator.capability
    if (
        cap.control_input_bit_depth is not None
        and cap.control_input_bit_depth < requirement.required_bit_depth
        and verdict != 'fidelity_unqualified'
    ):
        verdict = 'fidelity_qualified_with_limitations'
        if 'BIT_DEPTH_CONVERSION_DECLARED' not in reasons:
            reasons.append('BIT_DEPTH_CONVERSION_DECLARED')
    if (
        requirement.required_range not in cap.supported_ranges
        and cap.supported_ranges
    ):
        if 'RANGE_MAPPING_UNDECLARED' not in reasons:
            reasons.append('RANGE_MAPPING_UNDECLARED')
        if verdict == 'fidelity_qualified':
            verdict = 'fidelity_qualified_with_limitations'
    if (
        requirement.requires_hdr_metadata
        and any(
            p.numeric.hdr_metadata == 'unknown' for p in patches
        )
    ):
        reasons.append('HDR_METADATA_UNDECLARED')
        if verdict == 'fidelity_qualified':
            verdict = 'fidelity_qualified_with_limitations'
    return GeneratorFidelityQualification.create(
        document_id=generator.document_id,
        generator_ref=AuthorityRef(
            kind='pattern_generator_instance',
            ref_id=generator.generator_id,
            ref_sha256=generator.generator_sha256,
        ),
        requirement=requirement,
        patch_verdicts=patch_verdicts,
        verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
        issued_at_utc=issued_at_utc or _utc_now(),
    )


# ---------------------------------------------------------------------------
# JA labels
# ---------------------------------------------------------------------------

GENERATOR_CLASS_LABELS: dict[str, str] = {
    'hardware_generator': 'ハードウェアジェネレーター',
    'software_generator': 'ソフトウェアジェネレーター',
    'display_internal_tpg': 'ディスプレイ内蔵TPG',
    'lut_box': 'LUTボックス',
    'video_processor': '映像プロセッサー',
    'media_player': 'メディアプレーヤー',
    'gpu_output': 'GPU出力',
    'unknown': '不明',
}

VERDICT_LABELS: dict[str, str] = {
    'fidelity_qualified': 'ジェネレーター忠実度: 適格',
    'fidelity_qualified_with_limitations': 'ジェネレーター忠実度: 条件付き適格',
    'fidelity_insufficient_evidence': 'ジェネレーター忠実度: 証拠不足',
    'fidelity_unqualified': 'ジェネレーター忠実度: 不適格',
}

PATCH_VERDICT_LABELS: dict[str, str] = {
    'delivered_verified': '送出信号検証済み',
    'delivered_with_documented_transform': '送出信号検証済み（変換宣言あり）',
    'delivered_mismatch': '送出信号不一致',
    'delivery_unverified': '送出信号未検証',
}

REASON_LABELS: dict[str, str] = {
    'GENERATOR_FIDELITY_QUALIFIED': 'ジェネレーター忠実度は適格です',
    'GENERATOR_FIDELITY_LIMITED': 'ジェネレーター忠実度は条件付き適格です',
    'DELIVERED_MATCHES_REQUESTED': '要求パッチが送出信号と一致しました',
    'DELIVERED_MISMATCH': '送出信号が要求パッチと一致しません',
    'DELIVERY_UNVERIFIED': '送出信号が未検証です',
    'GENERATOR_INSTANCE_INCOMPLETE': 'ジェネレーター識別が不完全です',
    'PATCH_IDENTITY_INCOMPLETE': 'パッチ識別が不完全です',
    'BIT_DEPTH_CONVERSION_DECLARED': 'ビット深度変換が宣言されています',
    'RANGE_MAPPING_UNDECLARED': 'レンジ対応が未宣言です',
    'HDR_METADATA_UNDECLARED': 'HDRメタデータが未宣言です',
    'STIMULUS_REF_UNPINNED': '刺激アセットがピン留めされていません',
    'OBSERVATION_STALE': '観測が古くなっています',
    'CLAIMED_PRECISION_EXCEEDS_PROOF': '要求精度が検証能力を超えています',
}
