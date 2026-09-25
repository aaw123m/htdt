"""Program dynamics & dialogue processing authority (#1036).

DRC, dialogue normalization, dialogue enhancement and limiter/protection
are *dynamic* processing mechanisms — separate authorities from static EQ
and from #980 loudness compensation (which changes tonal/spatial balance
vs playback level, not the quiet/loud peak distribution).

- :class:`DynamicsMechanismProfile` — capability/configuration for one
  mechanism (drc / dialogue_normalization / dialogue_enhancement /
  limiter) with its applicable codec/mode domain and provenance.
- :class:`ProgramDynamicsProcessingProfile` — the immutable set of
  mechanism profiles for one processor + firmware. A profile describes
  capability, never that it is currently active.
- :class:`EffectiveDynamicsProcessingState` — observed state bound to an
  exact device + firmware + input format + sound mode + operating preset.
- :func:`evaluate_dynamics_state` — conformance of effective state against
  the profile's domain; opaque/metadata-dependent mechanisms stay
  UNKNOWN rather than fabricating a transfer.

Dialogue normalization is never represented as center-channel trim; a
vendor dialog-enhancer label never fabricates a filter curve.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


DynamicsMechanism = Literal[
    'drc',
    'dialogue_normalization',
    'dialogue_enhancement',
    'limiter_protection',
]
"""The four dynamic mechanisms this authority keeps distinct. Loudness
compensation (Dynamic EQ, #980) is deliberately absent."""

EvidenceTier = Literal[
    'device_readback_verified',
    'user_confirmed_device_state',
    'documented_capability',
    'setting_only',
    'unknown',
]


class DynamicsMechanismProfile(BaseModel):
    """Capability/configuration record for one mechanism."""

    model_config = ConfigDict(frozen=True)

    mechanism: DynamicsMechanism
    setting_label: str | None = None
    applicable_codec_families: tuple[str, ...] = ()
    applicable_sound_modes: tuple[str, ...] = ()
    metadata_dependent: bool = False
    control_parameters: tuple[str, ...] = ()
    transfer_model_ref: str | None = None
    opaque_behavior: bool = True
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class ProgramDynamicsProcessingProfile(BaseModel):
    """Immutable capability profile for one processor + firmware."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['program-dynamics-profile-1'] = (
        'program-dynamics-profile-1'
    )
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    processor_equipment_id: str = Field(min_length=1)
    firmware_version: str | None = None
    mechanisms: tuple[DynamicsMechanismProfile, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'ProgramDynamicsProcessingProfile':
        kinds = [m.mechanism for m in self.mechanisms]
        if len(set(kinds)) != len(kinds):
            raise ValueError(
                'each dynamics mechanism may appear at most once'
            )
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError('program dynamics profile hash mismatch')
        return self


def build_program_dynamics_profile(
    *,
    profile_id: str,
    version: str,
    processor_equipment_id: str,
    firmware_version: str | None = None,
    mechanisms: tuple[DynamicsMechanismProfile, ...] = (),
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> ProgramDynamicsProcessingProfile:
    probe = ProgramDynamicsProcessingProfile.model_construct(
        profile_id=profile_id,
        version=version,
        processor_equipment_id=processor_equipment_id,
        firmware_version=firmware_version,
        mechanisms=tuple(mechanisms),
        provenance=tuple(provenance),
        profile_sha256='',
    )
    return ProgramDynamicsProcessingProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


class EffectiveDynamicsState(BaseModel):
    """One mechanism's observed state in the effective chain."""

    model_config = ConfigDict(frozen=True)

    mechanism: DynamicsMechanism
    state: str = Field(min_length=1)
    evidence_tier: EvidenceTier = 'unknown'
    metadata_observed: tuple[str, ...] = ()
    transfer_effective_ref: str | None = None


class EffectiveDynamicsProcessingState(BaseModel):
    """Observed/applied dynamics state bound to an exact context.

    ``observed_at_utc`` plus the exact device/firmware/input/mode binding
    keeps this evidence immutable — a later firmware or preset change
    produces a new state record.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    state_id: str = Field(min_length=1)
    device_equipment_id: str = Field(min_length=1)
    firmware_version: str | None = None
    input_source_id: str | None = None
    input_codec_family: str | None = None
    sound_mode_id: str | None = None
    operating_preset_id: str | None = None
    states: tuple[EffectiveDynamicsState, ...] = ()
    observed_at_utc: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    state_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'state_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'EffectiveDynamicsProcessingState':
        kinds = [s.mechanism for s in self.states]
        if len(set(kinds)) != len(kinds):
            raise ValueError(
                'each mechanism may appear at most once per state record'
            )
        if self.state_sha256 != _hash(self.semantic_payload()):
            raise ValueError('effective dynamics state hash mismatch')
        return self


def build_effective_dynamics_state(
    *,
    state_id: str,
    device_equipment_id: str,
    firmware_version: str | None = None,
    input_source_id: str | None = None,
    input_codec_family: str | None = None,
    sound_mode_id: str | None = None,
    operating_preset_id: str | None = None,
    states: tuple[EffectiveDynamicsState, ...] = (),
    observed_at_utc: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> EffectiveDynamicsProcessingState:
    probe = EffectiveDynamicsProcessingState.model_construct(
        state_id=state_id,
        device_equipment_id=device_equipment_id,
        firmware_version=firmware_version,
        input_source_id=input_source_id,
        input_codec_family=input_codec_family,
        sound_mode_id=sound_mode_id,
        operating_preset_id=operating_preset_id,
        states=tuple(states),
        observed_at_utc=observed_at_utc,
        provenance=tuple(provenance),
        state_sha256='',
    )
    return EffectiveDynamicsProcessingState(
        **probe.model_dump(mode='python', exclude={'state_sha256'}),
        state_sha256=_hash(probe.semantic_payload()),
    )


class DynamicsCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str | None = None


class DynamicsEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    profile_sha256: str
    state_sha256: str
    checks: tuple[DynamicsCheckResult, ...]
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DynamicsEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('dynamics evaluation hash mismatch')
        if self.evaluation_id != 'dyn-' + digest[:24]:
            raise ValueError('dynamics evaluation id mismatch')
        return self


def evaluate_dynamics_state(
    *,
    profile: ProgramDynamicsProcessingProfile,
    state: EffectiveDynamicsProcessingState,
) -> DynamicsEvaluation:
    """Conformance of observed dynamics state against the capability
    profile — fail-closed: opaque or out-of-domain mechanisms stay
    UNKNOWN, never an assumed transfer."""

    checks: list[DynamicsCheckResult] = []

    checks.append(
        DynamicsCheckResult(
            check='context_binding',
            status=(
                'PASS'
                if state.device_equipment_id == profile.processor_equipment_id
                else 'FAIL'
            ),
            reason=(
                'state bound to the profiled device'
                if state.device_equipment_id == profile.processor_equipment_id
                else 'state device does not match the profiled device'
            ),
        )
    )
    if (
        profile.firmware_version is not None
        and state.firmware_version is not None
        and state.firmware_version != profile.firmware_version
    ):
        checks.append(
            DynamicsCheckResult(
                check='firmware_match',
                status='FAIL',
                reason='state observed under different firmware',
            )
        )
    else:
        checks.append(
            DynamicsCheckResult(
                check='firmware_match',
                status=(
                    'PASS'
                    if profile.firmware_version
                    and profile.firmware_version == state.firmware_version
                    else 'UNKNOWN'
                ),
                reason=(
                    'firmware identical'
                    if profile.firmware_version
                    and profile.firmware_version == state.firmware_version
                    else 'firmware not recorded on both sides'
                ),
            )
        )

    profile_mechanisms = {m.mechanism: m for m in profile.mechanisms}
    for observed in state.states:
        mechanism = profile_mechanisms.get(observed.mechanism)
        name = f'mechanism:{observed.mechanism}'
        if mechanism is None:
            checks.append(
                DynamicsCheckResult(
                    check=name,
                    status='UNKNOWN',
                    reason='mechanism is not in the capability profile',
                )
            )
            continue
        if (
            mechanism.applicable_codec_families
            and state.input_codec_family is not None
            and state.input_codec_family
            not in mechanism.applicable_codec_families
        ):
            checks.append(
                DynamicsCheckResult(
                    check=name,
                    status='UNKNOWN',
                    reason='input codec outside the mechanism domain',
                )
            )
            continue
        if mechanism.metadata_dependent and not observed.metadata_observed:
            checks.append(
                DynamicsCheckResult(
                    check=name,
                    status='UNKNOWN',
                    reason='mechanism depends on program metadata that was '
                    'not observed; effective transfer remains unknown',
                )
            )
            continue
        if mechanism.opaque_behavior and observed.transfer_effective_ref is None:
            checks.append(
                DynamicsCheckResult(
                    check=name,
                    status='UNKNOWN',
                    reason='opaque vendor behavior; no effective transfer '
                    'measured',
                )
            )
            continue
        checks.append(
            DynamicsCheckResult(
                check=name,
                status='PASS',
                reason=f'observed state {observed.state!r} within profile '
                'domain',
            )
        )

    probe = DynamicsEvaluation.model_construct(
        evaluation_id='',
        profile_sha256=profile.profile_sha256,
        state_sha256=state.state_sha256,
        checks=tuple(checks),
        evaluation_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return DynamicsEvaluation(
        **probe.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        ),
        evaluation_id='dyn-' + digest[:24],
        evaluation_sha256=digest,
    )


def dynamics_evaluation_status(
    evaluation: DynamicsEvaluation,
) -> EvaluationStatus:
    return _combine_status(tuple(c.status for c in evaluation.checks))
