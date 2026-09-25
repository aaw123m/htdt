"""Reference playback level authority (#733).

Binds program/test signal level, device master-volume indication and
calibrated acoustic SPL for one system into a versioned
:class:`PlaybackLevelCondition` — while keeping the three dB domains
explicitly separate:

- ``program`` — digital/stimulus level (dBFS-class: sweep level, pink-noise
  reference, program reference);
- ``device`` — the master-volume *indication* on the device's own scale
  (relative-dB display, vendor 0-100, step index, or UNKNOWN);
- ``acoustic`` — calibrated dB-SPL evidence at a listener/measurement point.

Authority boundary (per the issue contract):

- no numeric equality is ever asserted between the three domains — a
  device display of ``0.0`` means nothing acoustically by itself, and a
  measured SPL says nothing about the device scale;
- an acoustic SPL mapping requires exact measurement evidence plus a
  reference to the SPL-calibration authority it derives from — a relative
  frequency response can never establish absolute playback level;
- reference profiles (e.g. user targets, sourced criteria) are versioned
  authorities bound by ``profile_id + version`` + semantic hash — nothing
  is hard-coded globally;
- LFE/redirected-bass semantics stay in their own fields — no implicit
  +10 dB rules;
- device read-back (when present) proves device state, never acoustic SPL.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


PLAYBACK_LEVEL_SCHEMA_VERSION = 1
PLAYBACK_LEVEL_AUTHORITY_VERSION = 'playback-level-1'
REFERENCE_PROFILE_AUTHORITY_VERSION = 'reference-playback-profile-1'


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


class PlaybackLevelProvenanceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


# ----------------------------------------------------------------------
# Program / test stimulus level (digital domain — dBFS-class)

ProgramLevelKind = Literal[
    'program_reference',
    'test_sweep',
    'pink_noise',
    'external_calibration_source',
    'declared_other',
    'unknown',
]

TestSignalLevelUnit = Literal['dbfs_rms', 'dbfs_peak', 'declared_level', 'unknown']


class PlaybackProgramLevel(BaseModel):
    """Exact excitation semantics used for the program/test signal.

    ``stimulus_label`` identifies what was actually played (a REW sweep is a
    test stimulus — it is never silently labeled a cinema program
    reference). ``level`` is in ``level_unit`` only; it is never an SPL.
    """

    model_config = ConfigDict(frozen=True)

    kind: ProgramLevelKind
    level: float | None = None
    level_unit: TestSignalLevelUnit = 'unknown'
    stimulus_label: str | None = Field(default=None, min_length=1)
    stimulus_source_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_program_level(self) -> 'PlaybackProgramLevel':
        if self.level is not None:
            if not isfinite(self.level):
                raise ValueError('program level must be finite')
            if self.level_unit == 'unknown':
                raise ValueError(
                    'a numeric program level requires an explicit level_unit'
                )
        if self.level is None and self.level_unit != 'unknown':
            raise ValueError(
                'level_unit without a numeric level is not meaningful'
            )
        if self.kind == 'unknown' and self.level is not None:
            raise ValueError(
                'unknown program level kind cannot carry a numeric level'
            )
        return self


# ----------------------------------------------------------------------
# Device master-volume indication (device domain — vendor scale)

DeviceVolumeScale = Literal[
    'relative_db',
    'vendor_numeric',
    'step_index',
    'unknown',
]


class DeviceVolumeIndication(BaseModel):
    """The exact master-volume presentation a device reports.

    ``scale`` preserves the vendor presentation (``-20.0`` on a relative-dB
    display, ``62`` on a 0-100 scale, step ``47`` on a discrete dial, or an
    UNKNOWN scale). The value is *device-domain only* — it is never
    interpreted as SPL or dBFS.
    """

    model_config = ConfigDict(frozen=True)

    scale: DeviceVolumeScale
    value: float | None = None
    #: Exact displayed label where the device shows one (e.g. '-20.0 dB',
    #: '62'), recorded verbatim; never normalized into another domain.
    display_label: str | None = Field(default=None, min_length=1)
    device_ref: str | None = Field(default=None, min_length=1)
    #: #726 observed-state evidence id proving the indication, when bound.
    observed_state_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_device_volume(self) -> 'DeviceVolumeIndication':
        if self.value is not None and not isfinite(self.value):
            raise ValueError('device volume value must be finite')
        if self.scale == 'unknown' and self.value is not None:
            raise ValueError(
                'an unknown device scale cannot carry a numeric value'
            )
        return self


# ----------------------------------------------------------------------
# Acoustic level evidence (acoustic domain — calibrated dB SPL)

AcousticWeighting = Literal['A', 'C', 'Z', 'unweighted', 'unknown']


class PlaybackAcousticEvidence(BaseModel):
    """One measured absolute-level point backing an SPL claim.

    ``calibration_ref`` names the #643-compatible absolute-SPL calibration
    authority the mapping derives from — without it the row cannot claim
    calibrated SPL.
    """

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    listener_ref: str = Field(min_length=1)
    level_db_spl: float
    weighting: AcousticWeighting = 'unknown'
    uncertainty_db: float | None = None
    calibration_ref: str = Field(min_length=1)
    note: str | None = None

    @model_validator(mode='after')
    def valid_acoustic(self) -> 'PlaybackAcousticEvidence':
        if not isfinite(self.level_db_spl):
            raise ValueError('acoustic level must be finite')
        if self.uncertainty_db is not None and not (
            isfinite(self.uncertainty_db) and self.uncertainty_db >= 0
        ):
            raise ValueError('uncertainty must be a non-negative finite value')
        return self


# ----------------------------------------------------------------------
# Reference / target profiles (versioned, source-aware)

ReferenceProfileSource = Literal[
    'user_authored',
    'external_standard',
    'builtin_analytic',
    'imported',
]

#: LFE program semantics and redirected main-channel bass are *separate*
#: fields — neither may be inferred from the other.
class LfeReferenceSemantics(BaseModel):
    """LFE/redirected-bass offsets relative to the stated reference.

    Every offset is explicit and per-domain; there is no implicit global
    +10 dB LFE rule.
    """

    model_config = ConfigDict(frozen=True)

    lfe_program_offset_db: float | None = None
    redirected_bass_offset_db: float | None = None
    sub_trim_offset_db: float | None = None
    note: str | None = None

    @model_validator(mode='after')
    def valid_lfe(self) -> 'LfeReferenceSemantics':
        for value in (
            self.lfe_program_offset_db,
            self.redirected_bass_offset_db,
            self.sub_trim_offset_db,
        ):
            if value is not None and not isfinite(value):
                raise ValueError('LFE offsets must be finite')
        return self


class ReferencePlaybackProfile(BaseModel):
    """Versioned target/reference listening condition.

    A profile records *what* the reference is and *where it came from*
    (``source`` + ``source_label``/``source_version``) — standards-derived
    criteria must bind an exact source/version, and no profile is implied
    globally.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PLAYBACK_LEVEL_SCHEMA_VERSION
    authority_version: Literal['reference-playback-profile-1'] = (
        REFERENCE_PROFILE_AUTHORITY_VERSION
    )
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1, default='1')
    name: str = Field(min_length=1)
    #: Required for ``user_authored`` profiles (they live inside one
    #: project); optional for shared external/builtin sources.
    document_id: str | None = Field(default=None, min_length=1)
    source: ReferenceProfileSource
    source_label: str | None = Field(default=None, min_length=1)
    source_version: str | None = Field(default=None, min_length=1)
    #: Optional stated acoustic target at the reference listening position —
    #: in calibrated dB SPL only. Absence means the profile defines
    #: program/device semantics without an SPL target.
    target_level_db_spl: float | None = None
    target_weighting: AcousticWeighting = 'unknown'
    lfe: LfeReferenceSemantics | None = None
    applicability: str | None = None
    limitations: tuple[str, ...] = ()
    provenance: tuple[PlaybackLevelProvenanceItem, ...] = ()
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'ReferencePlaybackProfile':
        if self.target_level_db_spl is not None and not isfinite(
            self.target_level_db_spl
        ):
            raise ValueError('target level must be finite')
        if self.source == 'user_authored' and self.document_id is None:
            raise ValueError(
                'user-authored reference profiles are project-scoped '
                '(document_id required)'
            )
        if self.source in {'external_standard', 'imported'} and (
            self.source_label is None or self.source_version is None
        ):
            raise ValueError(
                'sourced reference profiles must bind an exact '
                'source_label + source_version'
            )
        if self.target_level_db_spl is not None and self.target_weighting == 'unknown':
            raise ValueError(
                'an SPL target must declare its weighting'
            )
        provenance_keys = [item.key for item in self.provenance]
        if len(provenance_keys) != len(set(provenance_keys)):
            raise ValueError('profile provenance keys must be unique')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ReferencePlaybackProfile hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'profile_id': self.profile_id,
            'version': self.version,
            'name': self.name,
            'document_id': self.document_id,
            'source': self.source,
            'source_label': self.source_label,
            'source_version': self.source_version,
            'target_level_db_spl': self.target_level_db_spl,
            'target_weighting': self.target_weighting,
            'lfe': (
                self.lfe.model_dump(mode='json') if self.lfe is not None else None
            ),
            'applicability': self.applicability,
            'limitations': list(self.limitations),
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }


class ReferenceProfileRef(BaseModel):
    """Exact pin of a versioned :class:`ReferencePlaybackProfile`."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


# ----------------------------------------------------------------------
# The composed operating point

class PlaybackLevelCondition(BaseModel):
    """Immutable system-level playback operating point.

    Binds — as applicable — the pinned SceneRevision, SystemVariant,
    operating preset, excitation scenario, routing profile, calibration
    plan/export, applied settings, device volume indication, program/test
    level, acoustic SPL evidence and a versioned reference profile. Missing
    domains stay absent; nothing is synthesized between domains.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PLAYBACK_LEVEL_SCHEMA_VERSION
    authority_version: Literal['playback-level-1'] = (
        PLAYBACK_LEVEL_AUTHORITY_VERSION
    )
    condition_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    operating_preset_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    excitation_scenario_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    routing_profile_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    calibration_plan_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    device_volume: DeviceVolumeIndication | None = None
    program_level: PlaybackProgramLevel | None = None
    acoustic_evidence: tuple[PlaybackAcousticEvidence, ...] = ()
    reference_profile: ReferenceProfileRef | None = None
    limitations: tuple[str, ...] = ()
    provenance: tuple[PlaybackLevelProvenanceItem, ...] = ()
    created_at_utc: str = Field(min_length=1)
    condition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_condition(self) -> 'PlaybackLevelCondition':
        measurement_ids = [
            item.measurement_id for item in self.acoustic_evidence
        ]
        if len(measurement_ids) != len(set(measurement_ids)):
            raise ValueError('acoustic evidence measurement ids must be unique')
        provenance_keys = [item.key for item in self.provenance]
        if len(provenance_keys) != len(set(provenance_keys)):
            raise ValueError('condition provenance keys must be unique')
        if self.condition_sha256 != _hash(self.semantic_payload()):
            raise ValueError('PlaybackLevelCondition hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'condition_id': self.condition_id,
            'document_id': self.document_id,
            'name': self.name,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'system_variant_id': self.system_variant_id,
            'operating_preset_sha256': self.operating_preset_sha256,
            'excitation_scenario_sha256': self.excitation_scenario_sha256,
            'routing_profile_sha256': self.routing_profile_sha256,
            'calibration_plan_sha256': self.calibration_plan_sha256,
            'device_volume': (
                self.device_volume.model_dump(mode='json')
                if self.device_volume is not None
                else None
            ),
            'program_level': (
                self.program_level.model_dump(mode='json')
                if self.program_level is not None
                else None
            ),
            'acoustic_evidence': [
                item.model_dump(mode='json') for item in self.acoustic_evidence
            ],
            'reference_profile': (
                self.reference_profile.model_dump(mode='json')
                if self.reference_profile is not None
                else None
            ),
            'limitations': list(self.limitations),
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }


def build_reference_profile(
    *,
    name: str,
    source: ReferenceProfileSource,
    document_id: str | None = None,
    source_label: str | None = None,
    source_version: str | None = None,
    target_level_db_spl: float | None = None,
    target_weighting: AcousticWeighting = 'unknown',
    lfe: LfeReferenceSemantics | None = None,
    applicability: str | None = None,
    limitations: tuple[str, ...] = (),
    provenance: tuple[PlaybackLevelProvenanceItem, ...] = (),
    created_at_utc: str,
    profile_id: str | None = None,
    version: str = '1',
) -> ReferencePlaybackProfile:
    payload: dict[str, Any] = {
        'profile_id': profile_id or str(uuid4()),
        'version': version,
        'name': name,
        'document_id': document_id,
        'source': source,
        'source_label': source_label,
        'source_version': source_version,
        'target_level_db_spl': target_level_db_spl,
        'target_weighting': target_weighting,
        'lfe': lfe,
        'applicability': applicability,
        'limitations': tuple(limitations),
        'provenance': tuple(provenance),
        'created_at_utc': created_at_utc,
    }
    provisional = ReferencePlaybackProfile.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ReferencePlaybackProfile(
        **payload, semantic_sha256=_hash(provisional.semantic_payload())
    )


def build_playback_level_condition(
    *,
    document_id: str,
    name: str,
    scene_revision_id: str,
    scene_content_hash: str,
    created_at_utc: str,
    system_variant_id: str | None = None,
    operating_preset_sha256: str | None = None,
    excitation_scenario_sha256: str | None = None,
    routing_profile_sha256: str | None = None,
    calibration_plan_sha256: str | None = None,
    device_volume: DeviceVolumeIndication | None = None,
    program_level: PlaybackProgramLevel | None = None,
    acoustic_evidence: tuple[PlaybackAcousticEvidence, ...] = (),
    reference_profile: ReferenceProfileRef | None = None,
    limitations: tuple[str, ...] = (),
    provenance: tuple[PlaybackLevelProvenanceItem, ...] = (),
    condition_id: str | None = None,
) -> PlaybackLevelCondition:
    payload: dict[str, Any] = {
        'condition_id': condition_id or str(uuid4()),
        'document_id': document_id,
        'name': name,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'system_variant_id': system_variant_id,
        'operating_preset_sha256': operating_preset_sha256,
        'excitation_scenario_sha256': excitation_scenario_sha256,
        'routing_profile_sha256': routing_profile_sha256,
        'calibration_plan_sha256': calibration_plan_sha256,
        'device_volume': device_volume,
        'program_level': program_level,
        'acoustic_evidence': tuple(acoustic_evidence),
        'reference_profile': reference_profile,
        'limitations': tuple(limitations),
        'provenance': tuple(provenance),
        'created_at_utc': created_at_utc,
    }
    provisional = PlaybackLevelCondition.model_construct(
        **payload, condition_sha256='0' * 64
    )
    return PlaybackLevelCondition(
        **payload, condition_sha256=_hash(provisional.semantic_payload())
    )


__all__ = [
    'AcousticWeighting',
    'DeviceVolumeIndication',
    'DeviceVolumeScale',
    'LfeReferenceSemantics',
    'PlaybackAcousticEvidence',
    'PlaybackLevelCondition',
    'PlaybackLevelProvenanceItem',
    'PlaybackProgramLevel',
    'PLAYBACK_LEVEL_AUTHORITY_VERSION',
    'PLAYBACK_LEVEL_SCHEMA_VERSION',
    'ProgramLevelKind',
    'REFERENCE_PROFILE_AUTHORITY_VERSION',
    'ReferencePlaybackProfile',
    'ReferenceProfileRef',
    'ReferenceProfileSource',
    'TestSignalLevelUnit',
    'build_playback_level_condition',
    'build_reference_profile',
]
