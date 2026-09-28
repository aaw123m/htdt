"""Media source playback capability authority (#1045).

Can the content/app/device produce the requested A/V profile — before any
transport evaluation? Content profile, source playback capability and
observed source output are three separate authorities; a product spec
saying "Atmos supported" never proves the current app+settings emit it.

- :class:`ContentProfile` — bounded content/test-asset descriptor (exact
  asset hash where user-owned; service/title ref only where appropriate;
  never DRM keys or protected media).
- :class:`SourceCapabilityObservation` — one capability state for an exact
  device + app/software + firmware + settings tuple. Historical evidence
  stays pinned to that exact identity.
- :class:`ObservedSourceOutput` — what the source actually emitted.
- :class:`MediaPlaybackSourceCondition` — the immutable condition a
  downstream transport/path evaluation consumes.
- :func:`evaluate_source_capability` — requested profile vs capability vs
  observed output: fallback is representable even when HDMI transport
  succeeds.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




SourceCapabilityState = Literal[
    'documented_supported',
    'documented_unsupported',
    'unknown',
    'observed_working',
    'observed_fallback',
    'observed_failure',
]


class ContentProfile(BaseModel):
    """Bounded content or test-asset descriptor — an identity, never the
    protected media itself."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    container_or_manifest: str | None = None
    video_codec: str | None = None
    video_profile_level: str | None = None
    hdr_family: str | None = None
    audio_codec: str | None = None
    audio_profile: str | None = None
    audio_layout: str | None = None
    protection_family: str | None = None
    asset_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    standard_profile_ref: str | None = None


class SourceCapabilityObservation(BaseModel):
    """Capability of an exact source/app/device state for one aspect."""

    model_config = ConfigDict(frozen=True)

    aspect: str = Field(min_length=1)
    state: SourceCapabilityState
    detail: str | None = None
    observed_at_utc: str | None = None


class ObservedSourceOutput(BaseModel):
    """What the source actually emitted under an exact condition."""

    model_config = ConfigDict(frozen=True)

    video_codec: str | None = None
    resolution: str | None = None
    hdr_family: str | None = None
    audio_format: str | None = None
    audio_layout: str | None = None
    protection_family: str | None = None
    observed_at_utc: str = Field(min_length=1)
    evidence_ref: str | None = None


class MediaPlaybackSourceCondition(BaseModel):
    """Immutable condition pinning the exact source identity + observed
    state. App/software/OS/firmware changes produce a new condition —
    historical observations never silently carry forward."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['media-source-condition-1'] = (
        'media-source-condition-1'
    )
    condition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    device_equipment_id: str = Field(min_length=1)
    app_identity: str | None = None
    app_version: str | None = None
    os_platform_version: str | None = None
    firmware_version: str | None = None
    playback_settings_ref: str | None = None
    content_profile: ContentProfile | None = None
    capabilities: tuple[SourceCapabilityObservation, ...] = ()
    observed_output: ObservedSourceOutput | None = None
    limitations: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    condition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'condition_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'MediaPlaybackSourceCondition':
        aspects = [c.aspect for c in self.capabilities]
        if len(set(aspects)) != len(aspects):
            raise ValueError(
                'each capability aspect may appear at most once'
            )
        if self.condition_sha256 != _hash(self.semantic_payload()):
            raise ValueError('media source condition hash mismatch')
        return self


def build_media_source_condition(
    *,
    condition_id: str,
    version: str,
    device_equipment_id: str,
    app_identity: str | None = None,
    app_version: str | None = None,
    os_platform_version: str | None = None,
    firmware_version: str | None = None,
    playback_settings_ref: str | None = None,
    content_profile: ContentProfile | None = None,
    capabilities: tuple[SourceCapabilityObservation, ...] = (),
    observed_output: ObservedSourceOutput | None = None,
    limitations: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> MediaPlaybackSourceCondition:
    probe = MediaPlaybackSourceCondition.model_construct(**canonicalize_payload(MediaPlaybackSourceCondition, dict(
        condition_id=condition_id,
        version=version,
        device_equipment_id=device_equipment_id,
        app_identity=app_identity,
        app_version=app_version,
        os_platform_version=os_platform_version,
        firmware_version=firmware_version,
        playback_settings_ref=playback_settings_ref,
        content_profile=content_profile,
        capabilities=tuple(capabilities),
        observed_output=observed_output,
        limitations=limitations,
        provenance=tuple(provenance),
        condition_sha256='',
    )))
    return MediaPlaybackSourceCondition(
        **probe.model_dump(mode='python', exclude={'condition_sha256'}),
        condition_sha256=_hash(probe.semantic_payload()),
    )


class SourceCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str | None = None


def evaluate_source_capability(
    condition: MediaPlaybackSourceCondition,
) -> tuple[SourceCheckResult, ...]:
    """Requested profile vs recorded capability vs observed output.

    A documented or observed fallback is NOT a transport failure — the
    verdict distinguishes 'source produced the requested profile' from
    'downstream path can carry it' (the latter belongs to #820).
    """
    checks: list[SourceCheckResult] = []

    states = {c.aspect: c.state for c in condition.capabilities}

    if not condition.capabilities:
        checks.append(
            SourceCheckResult(
                check='capability_evidence',
                status='UNKNOWN',
                reason='no capability observations recorded',
            )
        )
    elif any(
        s in ('observed_failure', 'documented_unsupported')
        for s in states.values()
    ):
        failed = [
            a
            for a, s in states.items()
            if s in ('observed_failure', 'documented_unsupported')
        ]
        checks.append(
            SourceCheckResult(
                check='capability_evidence',
                status='FAIL',
                reason='unsupported/failed aspects: ' + ', '.join(failed),
            )
        )
    elif all(
        s in ('observed_working', 'documented_supported')
        for s in states.values()
    ):
        checks.append(
            SourceCheckResult(
                check='capability_evidence',
                status='PASS',
                reason='all recorded aspects supported or observed working',
            )
        )
    else:
        checks.append(
            SourceCheckResult(
                check='capability_evidence',
                status='UNKNOWN',
                reason='some aspects are unverified or observed as '
                'fallback',
            )
        )

    output = condition.observed_output
    profile = condition.content_profile
    if output is None or profile is None:
        checks.append(
            SourceCheckResult(
                check='requested_profile_preserved',
                status='UNKNOWN',
                reason='content profile or observed output not recorded',
            )
        )
    else:
        mismatches = []
        for label, requested, observed in (
            ('video_codec', profile.video_codec, output.video_codec),
            ('hdr_family', profile.hdr_family, output.hdr_family),
            ('audio_codec', profile.audio_codec, output.audio_format),
            ('audio_layout', profile.audio_layout, output.audio_layout),
        ):
            if (
                requested is not None
                and observed is not None
                and requested != observed
            ):
                mismatches.append(
                    f'{label}: requested {requested}, source emitted '
                    f'{observed}'
                )
        checks.append(
            SourceCheckResult(
                check='requested_profile_preserved',
                status='FAIL' if mismatches else 'PASS',
                reason=(
                    '; '.join(mismatches)
                    if mismatches
                    else 'observed source output matches the requested '
                    'content profile'
                ),
            )
        )

    if output is not None and output.protection_family is not None:
        checks.append(
            SourceCheckResult(
                check='protection_state',
                status='PASS',
                reason=f'protection family {output.protection_family} '
                'recorded as observation only — no key or bypass data',
            )
        )

    return tuple(checks)
