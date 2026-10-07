"""DSP deployment-target registry (#838 slice B).

A calibration deployment must never be decided against a generic "DSP
target". This module seals *exact* target profiles — one per documented
product/family/variant — each declaring what that target can honor:

- supported sample rates, filter classes and PEQ slot budgets;
- coefficient sign conventions (miniDSP negates a1/a2 relative to the
  HTDT-native ``1+a1*z^-1+a2*z^-2`` denominator);
- deploy / read-back / runtime-attestation / rollback mechanisms, so a
  file-export target can never impersonate a machine-verifiable device;
- pinned document sources the profile was reviewed against — profiles
  are version-bound claims, never permanent truth.

Fail-closed: an exact profile is required for any deployment evaluation;
unknown profile ids, unasserted fields (``None``), and parameters
outside the declared classes block rather than assume.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import CadCalibrationExportSnapshot
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


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


# ----------------------------------------------------------------------
# vocabulary

DSPTargetFamily = Literal[
    'camilladsp_websocket',
    'minidsp_biquad_export',
    'equalizer_apo_config',
    'generic_peq_fir_file',
    'manual_avr_settings',
]

DSPDeployMechanism = Literal[
    'machine_write', 'file_export', 'manual_entry', 'none',
]

DSPReadbackMechanism = Literal[
    'machine_exact', 'operator_captured_file', 'none',
]

DSPRuntimeAttestation = Literal['telemetry', 'none']

DSPRollbackMechanism = Literal[
    'previous_config', 'operator_only', 'none',
]

DSPFilterClass = Literal[
    'peq', 'gain', 'delay', 'polarity', 'crossover', 'fir', 'routing',
]

BiquadSignConvention = Literal[
    'denominator=1+a1*z^-1+a2*z^-2',
    'denominator=1-a1*z^-1-a2*z^-2',
]

TargetFitVerdict = Literal[
    'fits', 'exceeds_profile', 'rate_unsupported',
    'unsupported_parameters', 'unknown_profile',
]

DSP_TARGET_LABELS: dict[str, str] = {
    'camilladsp_websocket': 'CamillaDSP WebSocket API',
    'minidsp_biquad_export': 'miniDSP バイクワッドテキストエクスポート',
    'equalizer_apo_config': 'Equalizer APO コンフィグ',
    'generic_peq_fir_file': '汎用 PEQ/FIR ファイル',
    'manual_avr_settings': 'AVR 手動設定',
    'machine_write': '機器書き込み（API）',
    'file_export': 'ファイルエクスポート',
    'manual_entry': '手動入力',
    'machine_exact': '機器読み戻し（完全一致）',
    'operator_captured_file': 'オペレータ取得ファイル',
    'telemetry': 'ランタイムテレメトリ',
    'previous_config': '直前コンフィグ復帰',
    'operator_only': 'オペレータのみ',
    'fits': 'プロファイル適合',
    'exceeds_profile': 'プロファイル上限超過',
    'rate_unsupported': 'サンプルレート未対応',
    'unsupported_parameters': '未対応パラメータ',
    'unknown_profile': '不明なターゲットプロファイル',
    'none': 'なし',
}


class UnknownTargetProfileError(LookupError):
    """A deployment was requested against an unregistered target."""


class DSPTargetTopology(BaseModel):
    """Channel/routing shape asserted by the reviewed documentation.

    ``None`` is *unasserted*, not unlimited — a fit evaluation treats an
    unasserted bound as unavailable evidence, never as permission.
    """

    model_config = ConfigDict(frozen=True)

    input_channels: int | None = Field(default=None, ge=1)
    output_channels: int | None = Field(default=None, ge=1)
    routing_semantics: Literal[
        'fixed_per_output', 'matrix_mixer', 'external',
    ] = 'fixed_per_output'
    peq_slots_per_input: int | None = Field(default=None, ge=0)
    peq_slots_per_output: int | None = Field(default=None, ge=0)
    crossover_biquad_slots_per_output: int | None = Field(
        default=None, ge=0)


class DSPTargetProfile(BaseModel):
    """Sealed exact deployment-target profile (dtp-).

    One row per documented product/family/variant. ``source_refs`` pin
    the documentation the profile was reviewed against; the profile is a
    claim bound to those sources at ``profile_revision``, not a claim
    about the product in perpetuity.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_family: DSPTargetFamily
    device_family: str = Field(min_length=1)
    #: The exact marketed unit/family this profile covers — never a
    #: generic 'minidsp' or 'dsp' token.
    device_model: str = Field(min_length=1)
    #: Variant discriminator when one marketed unit maps to several
    #: plugin firmwares (e.g. miniDSP Flex with and without Dirac Live).
    profile_variant: str | None = None
    #: Registry revision tag binding the profile to its review event.
    profile_revision: str = Field(min_length=1)
    #: Exact supported processing sample rates. Empty = the device
    #: declares its rate at deploy time (e.g. CamillaDSP reads it from
    #: the audio device); non-empty = closed set.
    supported_sample_rates_hz: tuple[int, ...] = ()
    supported_filter_classes: tuple[DSPFilterClass, ...] = ()
    topology: DSPTargetTopology
    biquad_coefficient_convention: BiquadSignConvention
    gain_db_range: tuple[float, float] | None = None
    q_range: tuple[float, float] | None = None
    fir_max_taps: int | None = Field(default=None, ge=0)
    fir_formats: tuple[str, ...] = ()
    deploy_mechanism: DSPDeployMechanism
    readback_mechanism: DSPReadbackMechanism
    runtime_attestation: DSPRuntimeAttestation
    rollback_mechanism: DSPRollbackMechanism
    headroom_notes: tuple[str, ...] = ()
    #: 'title | url | reviewed <iso-date>' — at least one pinned source
    #: is required; a profile without provenance cannot seal.
    source_refs: tuple[str, ...]
    boundary_notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'DSPTargetProfile':
        for name in (
            'supported_sample_rates_hz', 'supported_filter_classes',
            'fir_formats', 'source_refs',
        ):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f'{name} must be unique')
        if not self.source_refs:
            raise ValueError(
                'a target profile requires pinned source_refs')
        if (
            self.deploy_mechanism == 'none'
            and self.readback_mechanism == 'machine_exact'
        ):
            raise ValueError(
                'a target with no deploy mechanism cannot claim '
                'machine read-back')
        if self.profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('DSPTargetProfile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DSPTargetProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'dtp')


# ----------------------------------------------------------------------
# built-in registry
#
# Sources reviewed 2026-10-07:
# * miniDSP "REW integration with miniDSP" application note — the
#   official AutoEQ plugin table (rates + PEQ counts per platform).
# * miniDSP "Advanced biquad programming" application note — the
#   biquad text file syntax (`biquadN,` + b0/b1/b2/a1/a2 lines) and the
#   sign convention relative to RBJ cookbook coefficients.
# * CamillaDSP websocket.md — documented WebSocket JSON API commands.

_MINIDSP_REW_SOURCE = (
    'miniDSP app note: REW integration with miniDSP | '
    'https://www.minidsp.com/applications/advanced-app-notes/'
    'rew-integration-with-minidsp | reviewed 2026-10-07'
)
_MINIDSP_BIQUAD_SOURCE = (
    'miniDSP app note: Advanced biquad programming | '
    'https://www.minidsp.com/applications/advanced-app-notes/'
    'advanced-biquad-programming | reviewed 2026-10-07'
)
_CAMILLADSP_WS_SOURCE = (
    'CamillaDSP WebSocket command reference (websocket.md) | '
    'https://github.com/HEnquist/camilladsp/blob/master/websocket.md | '
    'reviewed 2026-10-07'
)
_CAMILLADSP_CONFIG_SOURCE = (
    'CamillaDSP configuration reference (configs.md) | '
    'https://github.com/HEnquist/camilladsp/blob/master/configs.md | '
    'reviewed 2026-10-07'
)
_HTDT_EXPORT_SOURCE = (
    'HTDT generic biquad export format htdt-generic-biquad '
    'generic-biquad-json-csv-1 | backend cad_calibration.py | '
    'reviewed 2026-10-07'
)

_MINIDSP_SIGN_CONVENTION: BiquadSignConvention = (
    'denominator=1-a1*z^-1-a2*z^-2'
)
_HTDT_SIGN_CONVENTION: BiquadSignConvention = (
    'denominator=1+a1*z^-1+a2*z^-2'
)


def _minidsp_profile(
    device_model: str,
    *,
    rate_hz: int,
    peq_per_output: int,
    input_channels: int | None,
    output_channels: int | None,
    variant: str | None,
    revision: str,
    boundary: tuple[str, ...] = (),
) -> DSPTargetProfile:
    return DSPTargetProfile.create(
        target_family='minidsp_biquad_export',
        device_family='minidsp',
        device_model=device_model,
        profile_variant=variant,
        profile_revision=revision,
        supported_sample_rates_hz=(rate_hz,),
        supported_filter_classes=('peq',),
        topology=DSPTargetTopology(
            input_channels=input_channels,
            output_channels=output_channels,
            routing_semantics='external',
            peq_slots_per_output=peq_per_output,
            crossover_biquad_slots_per_output=8,
        ),
        biquad_coefficient_convention=_MINIDSP_SIGN_CONVENTION,
        deploy_mechanism='file_export',
        readback_mechanism='none',
        runtime_attestation='none',
        rollback_mechanism='operator_only',
        headroom_notes=(
            'REW export path carries per-output PEQ only; channel gain, '
            'delay, polarity and crossovers live in other device sections '
            'and are never silently approximated.',
        ),
        source_refs=(_MINIDSP_REW_SOURCE, _MINIDSP_BIQUAD_SOURCE),
        boundary_notes=(
            (
                'No public machine read-back/control API is claimed for '
                'this target; operator import + post-deployment '
                'measurement are the verification path.'
            ),
            *boundary,
        ),
    )


DSP_TARGET_PROFILES: tuple[DSPTargetProfile, ...] = (
    # -- CamillaDSP (open DSP, documented WebSocket JSON API) --------
    DSPTargetProfile.create(
        target_family='camilladsp_websocket',
        device_family='camilladsp',
        device_model='CamillaDSP',
        profile_variant=None,
        profile_revision='websocket-md-2026-10-07',
        supported_sample_rates_hz=(),
        supported_filter_classes=(
            'peq', 'gain', 'delay', 'polarity', 'crossover', 'routing',
        ),
        topology=DSPTargetTopology(
            routing_semantics='matrix_mixer',
        ),
        biquad_coefficient_convention=_HTDT_SIGN_CONVENTION,
        deploy_mechanism='machine_write',
        readback_mechanism='machine_exact',
        runtime_attestation='telemetry',
        rollback_mechanism='previous_config',
        headroom_notes=(
            'Processing rate is declared by the bound audio device at '
            'deploy time (devices.samplerate); the profile does not pin '
            'a closed rate set.',
        ),
        source_refs=(_CAMILLADSP_WS_SOURCE, _CAMILLADSP_CONFIG_SOURCE),
        boundary_notes=(
            'Conv/Fir filters require external impulse files the '
            'WebSocket config path does not transfer; the fir class is '
            'therefore not claimed here.',
            'Default listen address is 127.0.0.1; remote endpoints '
            'require explicit operator configuration — no silent LAN '
            'discovery.',
            'SetConfigJson returning Ok is a device ack only — never '
            'acoustic verification.',
        ),
    ),
    # -- miniDSP "miniDSP" plugin family: 48 kHz / 6 PEQ -------------
    _minidsp_profile(
        'miniDSP 2x4', rate_hz=48000, peq_per_output=6,
        input_channels=2, output_channels=4, variant=None,
        revision='rew-autoeq-minidsp-48k-6peq',
    ),
    _minidsp_profile(
        'miniDSP C-DSP 6x8', rate_hz=48000, peq_per_output=6,
        input_channels=6, output_channels=8, variant=None,
        revision='rew-autoeq-minidsp-48k-6peq',
    ),
    _minidsp_profile(
        'miniDSP 10x10 HD', rate_hz=48000, peq_per_output=6,
        input_channels=8, output_channels=10, variant=None,
        revision='rew-autoeq-minidsp-48k-6peq',
    ),
    _minidsp_profile(
        'miniDSP OpenDRC-DI', rate_hz=48000, peq_per_output=6,
        input_channels=None, output_channels=2, variant=None,
        revision='rew-autoeq-minidsp-48k-6peq',
    ),
    _minidsp_profile(
        'miniDSP PWR-ICE', rate_hz=48000, peq_per_output=6,
        input_channels=2, output_channels=2, variant='2x2-fir-plugin',
        revision='rew-autoeq-minidsp-48k-6peq',
    ),
    # -- miniDSP "miniDSP-96k" plugin family: 96 kHz / 5 PEQ ---------
    _minidsp_profile(
        'miniDSP 4x10 HD', rate_hz=96000, peq_per_output=5,
        input_channels=4, output_channels=10, variant=None,
        revision='rew-autoeq-minidsp-96k-5peq',
    ),
    _minidsp_profile(
        'miniDSP nanoDIGI 2x8', rate_hz=96000, peq_per_output=5,
        input_channels=None, output_channels=8, variant=None,
        revision='rew-autoeq-minidsp-96k-5peq',
    ),
    # -- miniDSP "2x4 HD" plugin family: 96 kHz / 10 PEQ -------------
    _minidsp_profile(
        'miniDSP 2x4 HD', rate_hz=96000, peq_per_output=10,
        input_channels=2, output_channels=4, variant=None,
        revision='rew-autoeq-2x4hd-96k-10peq',
    ),
    _minidsp_profile(
        'miniDSP Flex', rate_hz=96000, peq_per_output=10,
        input_channels=2, output_channels=4, variant='non_dirac',
        revision='rew-autoeq-2x4hd-96k-10peq',
        boundary=(
            'A Flex unit running Dirac Live is a different profile '
            '(minidsp_flex_dirac): Dirac firmware processes at 48 kHz.',
        ),
    ),
    _minidsp_profile(
        'miniDSP Flex Eight', rate_hz=96000, peq_per_output=10,
        input_channels=2, output_channels=8, variant='non_dirac',
        revision='rew-autoeq-2x4hd-96k-10peq',
        boundary=(
            'A Flex Eight unit running Dirac Live is a different '
            'profile: Dirac firmware processes at 48 kHz.',
        ),
    ),
    _minidsp_profile(
        'miniDSP SHD Series', rate_hz=96000, peq_per_output=10,
        input_channels=2, output_channels=4, variant=None,
        revision='rew-autoeq-2x4hd-96k-10peq',
    ),
    _minidsp_profile(
        'miniDSP HA-DSP', rate_hz=96000, peq_per_output=10,
        input_channels=None, output_channels=2, variant=None,
        revision='rew-autoeq-2x4hd-96k-10peq',
    ),
    _minidsp_profile(
        'miniDSP PWR-ICE', rate_hz=96000, peq_per_output=10,
        input_channels=2, output_channels=2, variant='2x2-plugin',
        revision='rew-autoeq-2x4hd-96k-10peq',
    ),
    # -- miniDSP C-DSP 8x12 (non-Dirac): 192 kHz / 10 PEQ ------------
    _minidsp_profile(
        'miniDSP C-DSP 8x12', rate_hz=192000, peq_per_output=10,
        input_channels=8, output_channels=12, variant=None,
        revision='rew-autoeq-cdsp8x12-192k-10peq',
    ),
    # -- miniDSP Dirac-capable family (8x12 DL class): 48 kHz / 10 PEQ
    _minidsp_profile(
        'miniDSP DDRC-24', rate_hz=48000, peq_per_output=10,
        input_channels=2, output_channels=4, variant=None,
        revision='rew-autoeq-8x12dl-48k-10peq',
    ),
    _minidsp_profile(
        'miniDSP Flex', rate_hz=48000, peq_per_output=10,
        input_channels=2, output_channels=4, variant='dirac',
        revision='rew-autoeq-8x12dl-48k-10peq',
    ),
    _minidsp_profile(
        'miniDSP Flex Eight', rate_hz=48000, peq_per_output=10,
        input_channels=2, output_channels=8, variant='dirac',
        revision='rew-autoeq-8x12dl-48k-10peq',
    ),
    _minidsp_profile(
        'miniDSP Flex HT', rate_hz=48000, peq_per_output=10,
        input_channels=None, output_channels=8, variant=None,
        revision='rew-autoeq-8x12dl-48k-10peq',
    ),
    _minidsp_profile(
        'miniDSP Flex HTx', rate_hz=48000, peq_per_output=10,
        input_channels=None, output_channels=8, variant=None,
        revision='rew-autoeq-8x12dl-48k-10peq',
    ),
    _minidsp_profile(
        'miniDSP C-DSP 8x12 DL', rate_hz=48000, peq_per_output=10,
        input_channels=8, output_channels=12, variant=None,
        revision='rew-autoeq-8x12dl-48k-10peq',
    ),
    _minidsp_profile(
        'miniDSP Harmony DSP 8x12', rate_hz=48000, peq_per_output=10,
        input_channels=8, output_channels=12, variant=None,
        revision='rew-autoeq-8x12dl-48k-10peq',
    ),
    _minidsp_profile(
        'miniDSP DDRC-88', rate_hz=48000, peq_per_output=10,
        input_channels=8, output_channels=8, variant=None,
        revision='rew-autoeq-8x12dl-48k-10peq',
    ),
    # -- miniDSP nanoAVR: 96 kHz / 10 PEQ ----------------------------
    _minidsp_profile(
        'miniDSP nanoAVR', rate_hz=96000, peq_per_output=10,
        input_channels=None, output_channels=8, variant=None,
        revision='rew-autoeq-nanoavr-96k-10peq',
    ),
    # -- HTDT generic file fallback ----------------------------------
    DSPTargetProfile.create(
        target_family='generic_peq_fir_file',
        device_family='generic-file-target',
        device_model='htdt generic biquad JSON/CSV',
        profile_variant=None,
        profile_revision='htdt-generic-biquad-1',
        supported_sample_rates_hz=(),
        supported_filter_classes=('peq', 'gain', 'delay', 'polarity'),
        topology=DSPTargetTopology(routing_semantics='external'),
        biquad_coefficient_convention=_HTDT_SIGN_CONVENTION,
        deploy_mechanism='file_export',
        readback_mechanism='operator_captured_file',
        runtime_attestation='none',
        rollback_mechanism='operator_only',
        headroom_notes=(
            'Rate-agnostic text export; the operator declares the rate '
            'to the device on import.',
        ),
        source_refs=(_HTDT_EXPORT_SOURCE,),
        boundary_notes=(
            'Generic fallback, not a specific product interface — '
            'machine read-back is never claimed on this target.',
        ),
    ),
)

_PROFILE_INDEX: dict[str, DSPTargetProfile] = {
    profile.profile_id: profile for profile in DSP_TARGET_PROFILES
}


def list_dsp_target_profiles(
    *,
    target_family: DSPTargetFamily | None = None,
) -> tuple[DSPTargetProfile, ...]:
    """Registered profiles, optionally filtered to one family."""
    if target_family is None:
        return DSP_TARGET_PROFILES
    return tuple(
        profile for profile in DSP_TARGET_PROFILES
        if profile.target_family == target_family
    )


def get_dsp_target_profile(profile_id: str) -> DSPTargetProfile:
    """Exact profile lookup — fail closed on unknown ids."""
    profile = _PROFILE_INDEX.get(profile_id)
    if profile is None:
        raise UnknownTargetProfileError(
            f'no DSP target profile registered for {profile_id!r}')
    return profile


def find_dsp_target_profile(
    device_family: str,
    device_model: str,
    profile_variant: str | None = None,
) -> DSPTargetProfile | None:
    """Resolve a profile by exact family/model/variant identity."""
    for profile in DSP_TARGET_PROFILES:
        if (
            profile.device_family == device_family
            and profile.device_model == device_model
            and profile.profile_variant == profile_variant
        ):
            return profile
    return None


# ----------------------------------------------------------------------
# fit evaluation — fail closed


def evaluate_export_target_fit(
    export: CadCalibrationExportSnapshot,
    profile: DSPTargetProfile,
) -> tuple[TargetFitVerdict, tuple[str, ...]]:
    """Whether one export fits one exact target profile.

    Returns the worst verdict plus machine-readable reason strings.
    Anything the profile does not declare is a reason, never an
    assumption; callers surface the reasons as
    ``MaterializedCalibrationSettings.unsupported_items`` so the #806
    deployment gate blocks them.
    """
    reasons: list[str] = []
    verdict: TargetFitVerdict = 'fits'
    classes = set(profile.supported_filter_classes)
    topology = profile.topology

    if (
        profile.supported_sample_rates_hz
        and export.sample_rate_hz not in profile.supported_sample_rates_hz
    ):
        verdict = 'rate_unsupported'
        reasons.append(
            f'sample_rate_hz:{export.sample_rate_hz} not in '
            f'{sorted(profile.supported_sample_rates_hz)}')

    if (
        topology.output_channels is not None
        and len(export.channels) > topology.output_channels
    ):
        verdict = 'exceeds_profile'
        reasons.append(
            f'channel_count:{len(export.channels)} exceeds '
            f'output_channels:{topology.output_channels}')

    unsupported_seen = False
    for channel in export.channels:
        if 'peq' not in classes and channel.peq:
            unsupported_seen = True
            reasons.append(f'{channel.channel_id}:peq unsupported')
        elif (
            channel.peq
            and topology.peq_slots_per_output is not None
            and len(channel.peq) > topology.peq_slots_per_output
        ):
            verdict = 'exceeds_profile'
            reasons.append(
                f'{channel.channel_id}:peq_count:{len(channel.peq)} '
                f'exceeds slots:{topology.peq_slots_per_output}')
        elif channel.peq and topology.peq_slots_per_output is None:
            unsupported_seen = True
            reasons.append(
                f'{channel.channel_id}:peq slot budget not asserted '
                'by profile')
        if channel.crossovers and 'crossover' not in classes:
            unsupported_seen = True
            reasons.append(
                f'{channel.channel_id}:crossover unsupported')
        if channel.delay_s > 0.0 and 'delay' not in classes:
            unsupported_seen = True
            reasons.append(f'{channel.channel_id}:delay unsupported')
        if channel.polarity == 'inverted' and 'polarity' not in classes:
            unsupported_seen = True
            reasons.append(
                f'{channel.channel_id}:polarity unsupported')
        if abs(channel.gain_db) > 0.0 and 'gain' not in classes:
            unsupported_seen = True
            reasons.append(f'{channel.channel_id}:gain unsupported')

    if unsupported_seen:
        verdict = 'unsupported_parameters'
    return verdict, tuple(reasons)


__all__ = [
    'BiquadSignConvention',
    'DSPDeployMechanism',
    'DSPFilterClass',
    'DSPReadbackMechanism',
    'DSPRollbackMechanism',
    'DSPRuntimeAttestation',
    'DSPTargetFamily',
    'DSPTargetProfile',
    'DSPTargetTopology',
    'DSP_TARGET_LABELS',
    'DSP_TARGET_PROFILES',
    'TargetFitVerdict',
    'UnknownTargetProfileError',
    'evaluate_export_target_fit',
    'find_dsp_target_profile',
    'get_dsp_target_profile',
    'list_dsp_target_profiles',
]
