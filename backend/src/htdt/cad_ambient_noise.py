"""Measured background/ambient noise authority (#532).

A room can have excellent FR/decay behaviour and still be compromised by
HVAC, projector fan, HTPC or AVR cooling noise; ambient noise also bounds
the usable dynamic range for decay/IR and SNR quality gates. This module
persists the ambient spectrum as first-class evidence — exact operating
condition, exact level semantics, exact band data — and evaluates NC-style
criteria only through versioned criterion authorities. Uncalibrated or
relative spectra never produce authoritative absolute noise criteria, and
no HVAC/fan simulation is attempted: comparisons are measured evidence only.
"""

from __future__ import annotations

from contextlib import closing
from math import isfinite, log10
import sqlite3
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


EquipmentState = Literal['on', 'off', 'unknown']
AmbientLevelSemantics = Literal['absolute_spl', 'relative', 'unknown']
AmbientBandSpec = Literal['octave', 'third_octave', 'other']
AmbientMethod = Literal['measured', 'imported', 'unknown']

#: What an A/B comparison is *for*. The intent decides which differences
#: between the two acquisitions are controlled (the object of the
#: comparison) and which are uncontrolled contaminants.
AmbientComparisonIntent = Literal[
    'operating_state_ab',
    'spatial_noise_map',
    'repeatability',
    'instrument_crosscheck',
    'diagnostic_uncontrolled',
]

#: How far the two profiles' level bases allow the band deltas to be read.
AmbientLevelCompatibility = Literal[
    'quantitative_delta_db',
    'relative_difference',
    'qualitative_only',
    'blocked',
]

#: Comparison-check outcome — ``BLOCKED`` (hard incompatibility) sits next
#: to the shared evaluation statuses.
AmbientCheckStatus = Literal[
    'PASS', 'FAIL', 'UNKNOWN', 'BLOCKED', 'NOT_APPLICABLE'
]

#: Operating-condition axes compared between two acquisitions. ``notes``
#: is free text and never a semantic axis.
_CONDITION_AXES: tuple[str, ...] = (
    'hvac_state',
    'projector_state',
    'pc_state',
    'avr_state',
    'doors_windows_state',
)
AmbientVerdict = Literal['PASS', 'FAIL', 'UNKNOWN']
AMBIENT_SCHEMA_VERSION = 'ambient-noise-1'


class AmbientOperatingCondition(BaseModel):
    """Immutable equipment/door/window state a spectrum was measured under.

    Equipment states are declared, never inferred from manufacturer specs;
    unknown states stay explicit ``unknown``.
    """

    model_config = ConfigDict(frozen=True)

    condition_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    hvac_state: EquipmentState = 'unknown'
    projector_state: EquipmentState = 'unknown'
    pc_state: EquipmentState = 'unknown'
    avr_state: EquipmentState = 'unknown'
    doors_windows_state: str = 'unknown'
    notes: tuple[str, ...] = ()
    created_at: str = Field(min_length=1)
    condition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_condition(self) -> 'AmbientOperatingCondition':
        if self.condition_sha256 != _hash(self.identity_payload()):
            raise ValueError('ambient operating condition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': AMBIENT_SCHEMA_VERSION,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'hvac_state': self.hvac_state,
            'projector_state': self.projector_state,
            'pc_state': self.pc_state,
            'avr_state': self.avr_state,
            'doors_windows_state': self.doors_windows_state,
            'notes': list(self.notes),
        }


class AmbientNoiseProfile(BaseModel):
    """One exact measured/imported ambient-noise spectrum.

    ``level_semantics`` separates calibrated absolute SPL from relative or
    unknown levels: only ``absolute_spl`` may feed absolute noise criteria
    (NC/NCB-style evaluations) or absolute noise-floor claims.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    condition_id: str = Field(min_length=1)
    condition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_entity_id: str | None = Field(default=None, min_length=1)
    microphone_position: Position3
    acquisition_context_id: str | None = Field(default=None, min_length=1)
    calibration_authority_id: str | None = Field(default=None, min_length=1)
    method: AmbientMethod = 'unknown'
    level_semantics: AmbientLevelSemantics = 'unknown'
    weighting: str = 'unknown'
    band_spec: AmbientBandSpec = 'other'
    band_center_hz: tuple[float, ...] = ()
    band_level_db: tuple[float, ...] = ()
    overall_level_db: float | None = None
    integration_duration_s: float | None = None
    source_asset_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    captured_at: str = Field(min_length=1)
    imported_at: str = Field(min_length=1)
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'AmbientNoiseProfile':
        count = len(self.band_center_hz)
        if count != len(self.band_level_db):
            raise ValueError('ambient band centers and levels must have equal length')
        previous = 0.0
        for index, frequency in enumerate(self.band_center_hz):
            frequency = float(frequency)
            if not isfinite(frequency) or frequency <= 0:
                raise ValueError('ambient band centers must be finite and positive')
            if index and frequency <= previous:
                raise ValueError('ambient band centers must be strictly increasing')
            previous = frequency
        if any(not isfinite(float(v)) for v in self.band_level_db):
            raise ValueError('ambient band levels must be finite')
        if self.overall_level_db is not None and not isfinite(float(self.overall_level_db)):
            raise ValueError('overall level must be finite')
        if self.integration_duration_s is not None and self.integration_duration_s <= 0:
            raise ValueError('integration duration must be positive')
        if self.profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('ambient noise profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': AMBIENT_SCHEMA_VERSION,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'condition_id': self.condition_id,
            'condition_sha256': self.condition_sha256,
            'measurement_entity_id': self.measurement_entity_id,
            'microphone_position': {
                'x_m': self.microphone_position.x_m,
                'y_m': self.microphone_position.y_m,
                'z_m': self.microphone_position.z_m,
            },
            'acquisition_context_id': self.acquisition_context_id,
            'calibration_authority_id': self.calibration_authority_id,
            'method': self.method,
            'level_semantics': self.level_semantics,
            'weighting': self.weighting,
            'band_spec': self.band_spec,
            'band_center_hz': list(self.band_center_hz),
            'band_level_db': list(self.band_level_db),
            'overall_level_db': self.overall_level_db,
            'integration_duration_s': self.integration_duration_s,
            'source_asset_sha256': self.source_asset_sha256,
            'captured_at': self.captured_at,
        }


class AmbientNoiseCriterion(BaseModel):
    """Versioned per-band level criterion (e.g. an NC-style contour).

    Criteria are explicit versioned authorities — never hard-coded UI
    thresholds. ``band_center_hz``/``limit_level_db`` define the contour the
    evaluation compares against; evaluation also requires absolute-SPL level
    semantics on the measured profile.
    """

    model_config = ConfigDict(frozen=True)

    criterion_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    criterion_version: str = Field(min_length=1)
    band_spec: AmbientBandSpec = 'octave'
    band_center_hz: tuple[float, ...] = Field(min_length=1)
    limit_level_db: tuple[float, ...] = Field(min_length=1)
    requires_absolute_spl: bool = True
    criterion_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_criterion(self) -> 'AmbientNoiseCriterion':
        if len(self.band_center_hz) != len(self.limit_level_db):
            raise ValueError('criterion band centers and limits must have equal length')
        if self.criterion_sha256 != _hash(self.identity_payload()):
            raise ValueError('ambient criterion hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': AMBIENT_SCHEMA_VERSION,
            'name': self.name,
            'criterion_version': self.criterion_version,
            'band_spec': self.band_spec,
            'band_center_hz': list(self.band_center_hz),
            'limit_level_db': list(self.limit_level_db),
            'requires_absolute_spl': self.requires_absolute_spl,
        }


class AmbientCriteriaEvaluation(BaseModel):
    """Immutable evaluation of one profile against one exact criterion.

    ``band_verdicts`` records the per-band PASS/FAIL/UNKNOWN; ``verdict`` is
    FAIL if any band fails, PASS only when every band passes, UNKNOWN when
    the level semantics or band basis cannot produce an authoritative
    absolute-criterion result.
    """

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    criterion_id: str = Field(min_length=1)
    criterion_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    criterion_name: str = Field(min_length=1)
    criterion_version: str = Field(min_length=1)
    verdict: AmbientVerdict
    band_verdicts: tuple[AmbientVerdict, ...] = ()
    evaluated_at: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'AmbientCriteriaEvaluation':
        if self.evaluation_sha256 != _hash(self.identity_payload()):
            raise ValueError('ambient evaluation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': AMBIENT_SCHEMA_VERSION,
            'profile_id': self.profile_id,
            'profile_sha256': self.profile_sha256,
            'criterion_id': self.criterion_id,
            'criterion_sha256': self.criterion_sha256,
            'criterion_name': self.criterion_name,
            'criterion_version': self.criterion_version,
            'verdict': self.verdict,
            'band_verdicts': list(self.band_verdicts),
            'evaluated_at': self.evaluated_at,
        }


class AmbientComparisonCheck(BaseModel):
    """One compatibility check on an A/B comparison attempt."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: AmbientCheckStatus
    reason: str


class AmbientNoiseComparison(BaseModel):
    """Measured comparison of two ambient profiles under their conditions.

    A band-wise level difference between two measured conditions (e.g. HVAC
    off vs on). This is evidence about measured states only — it is never a
    simulation of fan or HVAC acoustics.

    ``difference_db`` is always the recorded band-wise subtraction — the raw
    evidence. ``level_compatibility`` records how far that subtraction may
    be *read*: quantitative dB deltas require matching absolute-SPL
    semantics, matching weighting and a verified shared calibration chain;
    mismatched level bases or weightings mark the subtraction ``blocked``.
    ``intent`` plus ``compatibility_checks`` decide which acquisition
    differences were controlled; :meth:`quantitative_delta_db` is the only
    accessor allowed to return bare numbers.
    """

    model_config = ConfigDict(frozen=True)

    comparison_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_a_id: str = Field(min_length=1)
    profile_a_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    profile_b_id: str = Field(min_length=1)
    profile_b_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    intent: AmbientComparisonIntent = 'diagnostic_uncontrolled'
    level_compatibility: AmbientLevelCompatibility = 'blocked'
    band_center_hz: tuple[float, ...] = ()
    difference_db: tuple[float, ...] = ()
    overall_difference_db: float | None = None
    compatibility_checks: tuple[AmbientComparisonCheck, ...] = ()
    differing_condition_axes: tuple[str, ...] = ()
    condition_a: AmbientOperatingCondition | None = None
    condition_b: AmbientOperatingCondition | None = None
    created_at: str = Field(min_length=1)
    comparison_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_comparison(self) -> 'AmbientNoiseComparison':
        if self.profile_a_id == self.profile_b_id:
            raise ValueError('ambient comparison requires two different profiles')
        if len(self.band_center_hz) != len(self.difference_db):
            raise ValueError('ambient comparison bands and differences must align')
        if self.comparison_sha256 != _hash(self.identity_payload()):
            raise ValueError('ambient comparison hash mismatch')
        return self

    def quantitative_delta_db(self) -> tuple[float | None, ...] | None:
        """Band deltas only when the compatibility grade allows a
        quantitative dB statement; otherwise ``None``."""
        if self.level_compatibility != 'quantitative_delta_db':
            return None
        return self.difference_db

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': AMBIENT_SCHEMA_VERSION,
            'document_id': self.document_id,
            'profile_a_id': self.profile_a_id,
            'profile_a_sha256': self.profile_a_sha256,
            'profile_b_id': self.profile_b_id,
            'profile_b_sha256': self.profile_b_sha256,
            'intent': self.intent,
            'level_compatibility': self.level_compatibility,
            'band_center_hz': list(self.band_center_hz),
            'difference_db': list(self.difference_db),
            'overall_difference_db': self.overall_difference_db,
            'compatibility_checks': [
                check.model_dump(mode='json')
                for check in self.compatibility_checks
            ],
            'differing_condition_axes': list(self.differing_condition_axes),
            'condition_a': (
                self.condition_a.model_dump(mode='json')
                if self.condition_a is not None
                else None
            ),
            'condition_b': (
                self.condition_b.model_dump(mode='json')
                if self.condition_b is not None
                else None
            ),
            'created_at': self.created_at,
        }


def differing_condition_axes(
    condition_a: AmbientOperatingCondition,
    condition_b: AmbientOperatingCondition,
) -> tuple[str, ...]:
    """The semantic operating-condition axes that differ between two
    acquisition conditions (free-text ``notes`` never counts)."""
    return tuple(
        axis
        for axis in _CONDITION_AXES
        if getattr(condition_a, axis) != getattr(condition_b, axis)
    )


# ---------------------------------------------------------------------------
# Factories


def build_ambient_operating_condition(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    hvac_state: EquipmentState = 'unknown',
    projector_state: EquipmentState = 'unknown',
    pc_state: EquipmentState = 'unknown',
    avr_state: EquipmentState = 'unknown',
    doors_windows_state: str = 'unknown',
    notes: tuple[str, ...] = (),
    created_at: str,
) -> AmbientOperatingCondition:
    payload: dict[str, Any] = {
        'condition_id': str(uuid4()),
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'hvac_state': hvac_state,
        'projector_state': projector_state,
        'pc_state': pc_state,
        'avr_state': avr_state,
        'doors_windows_state': doors_windows_state,
        'notes': list(notes),
        'created_at': created_at,
    }
    provisional = AmbientOperatingCondition.model_construct(
        **payload,
        condition_sha256='0' * 64,
    )
    return AmbientOperatingCondition(
        **payload,
        condition_sha256=_hash(provisional.identity_payload()),
    )


def build_ambient_noise_profile(
    condition: AmbientOperatingCondition,
    *,
    microphone_position: Position3,
    measurement_entity_id: str | None = None,
    acquisition_context_id: str | None = None,
    calibration_authority_id: str | None = None,
    method: AmbientMethod = 'unknown',
    level_semantics: AmbientLevelSemantics = 'unknown',
    weighting: str = 'unknown',
    band_spec: AmbientBandSpec = 'other',
    band_center_hz: tuple[float, ...] = (),
    band_level_db: tuple[float, ...] = (),
    overall_level_db: float | None = None,
    integration_duration_s: float | None = None,
    source_asset_sha256: str | None = None,
    captured_at: str,
    imported_at: str | None = None,
    provenance_json: str = '{}',
) -> AmbientNoiseProfile:
    payload: dict[str, Any] = {
        'profile_id': str(uuid4()),
        'document_id': condition.document_id,
        'scene_revision_id': condition.scene_revision_id,
        'scene_content_hash': condition.scene_content_hash,
        'condition_id': condition.condition_id,
        'condition_sha256': condition.condition_sha256,
        'measurement_entity_id': measurement_entity_id,
        'microphone_position': microphone_position,
        'acquisition_context_id': acquisition_context_id,
        'calibration_authority_id': calibration_authority_id,
        'method': method,
        'level_semantics': level_semantics,
        'weighting': weighting,
        'band_spec': band_spec,
        'band_center_hz': tuple(band_center_hz),
        'band_level_db': tuple(band_level_db),
        'overall_level_db': overall_level_db,
        'integration_duration_s': integration_duration_s,
        'source_asset_sha256': source_asset_sha256,
        'captured_at': captured_at,
        'imported_at': imported_at or captured_at,
        'provenance_json': provenance_json,
    }
    provisional = AmbientNoiseProfile.model_construct(**canonicalize_payload(AmbientNoiseProfile, dict(
        **payload,
        profile_sha256='0' * 64,
    )))
    return AmbientNoiseProfile(
        **payload,
        profile_sha256=_hash(provisional.identity_payload()),
    )


def build_ambient_noise_criterion(
    *,
    name: str,
    criterion_version: str,
    band_center_hz: tuple[float, ...],
    limit_level_db: tuple[float, ...],
    band_spec: AmbientBandSpec = 'octave',
    requires_absolute_spl: bool = True,
) -> AmbientNoiseCriterion:
    payload: dict[str, Any] = {
        'criterion_id': str(uuid4()),
        'name': name,
        'criterion_version': criterion_version,
        'band_spec': band_spec,
        'band_center_hz': tuple(band_center_hz),
        'limit_level_db': tuple(limit_level_db),
        'requires_absolute_spl': requires_absolute_spl,
    }
    provisional = AmbientNoiseCriterion.model_construct(**canonicalize_payload(AmbientNoiseCriterion, dict(
        **payload,
        criterion_sha256='0' * 64,
    )))
    return AmbientNoiseCriterion(
        **payload,
        criterion_sha256=_hash(provisional.identity_payload()),
    )


def evaluate_ambient_criterion(
    profile: AmbientNoiseProfile,
    criterion: AmbientNoiseCriterion,
    *,
    evaluated_at: str,
) -> AmbientCriteriaEvaluation:
    """Evaluate a measured ambient profile against a versioned criterion.

    Absolute criteria (``requires_absolute_spl``) only produce a verdict on
    ``absolute_spl`` profiles — relative/unknown levels return UNKNOWN per
    band instead of fabricating an authoritative NC-style result. A band
    basis mismatch (band spec or center frequencies) also stays UNKNOWN.
    """
    same_basis = (
        profile.band_spec == criterion.band_spec
        and tuple(profile.band_center_hz) == tuple(criterion.band_center_hz)
        and len(profile.band_level_db) == len(criterion.limit_level_db)
    )
    # #1025: a self-declared 'absolute_spl' level_semantics alone never
    # opens an absolute criterion — the profile must also bind a real
    # calibration authority.
    usable = (
        (
            profile.level_semantics == 'absolute_spl'
            and profile.calibration_authority_id is not None
        )
        or not criterion.requires_absolute_spl
    )
    if not same_basis or not usable:
        verdict: AmbientVerdict = 'UNKNOWN'
        band_verdicts = tuple('UNKNOWN' for _ in criterion.band_center_hz)
    else:
        band_verdicts = tuple(
            'PASS' if level <= limit else 'FAIL'
            for level, limit in zip(
                profile.band_level_db,
                criterion.limit_level_db,
                strict=True,
            )
        )
        verdict = 'FAIL' if 'FAIL' in band_verdicts else 'PASS'
    payload: dict[str, Any] = {
        'evaluation_id': str(uuid4()),
        'profile_id': profile.profile_id,
        'profile_sha256': profile.profile_sha256,
        'criterion_id': criterion.criterion_id,
        'criterion_sha256': criterion.criterion_sha256,
        'criterion_name': criterion.name,
        'criterion_version': criterion.criterion_version,
        'verdict': verdict,
        'band_verdicts': band_verdicts,
        'evaluated_at': evaluated_at,
    }
    provisional = AmbientCriteriaEvaluation.model_construct(
        **payload,
        evaluation_sha256='0' * 64,
    )
    return AmbientCriteriaEvaluation(
        **payload,
        evaluation_sha256=_hash(provisional.identity_payload()),
    )


def compare_ambient_profiles(
    profile_a: AmbientNoiseProfile,
    profile_b: AmbientNoiseProfile,
    *,
    created_at: str,
    intent: AmbientComparisonIntent = 'diagnostic_uncontrolled',
    condition_a: AmbientOperatingCondition | None = None,
    condition_b: AmbientOperatingCondition | None = None,
) -> AmbientNoiseComparison:
    """Measured A−B band comparison of two ambient profiles, under a
    declared intent.

    Both profiles must share the same document and band basis (hard
    preconditions). The raw ``difference_db`` is always recorded as
    evidence, but ``level_compatibility`` grades how far it may be read:
    a quantitative dB delta needs matching ``absolute_spl`` semantics,
    matching weighting and a verified shared calibration authority; equal
    non-absolute or unverified bases degrade to ``relative_difference`` /
    ``qualitative_only``; mismatched bases are ``blocked``. ``condition_a``
    /``condition_b`` pin the recorded operating conditions so the axes that
    differ — and any uncontrolled differences — are explicit on the record.
    """
    if profile_a.document_id != profile_b.document_id:
        raise ValueError('ambient comparison requires the same document')
    if profile_a.band_spec != profile_b.band_spec or (
        tuple(profile_a.band_center_hz) != tuple(profile_b.band_center_hz)
    ):
        raise ValueError('ambient comparison requires the same band basis')

    checks: list[AmbientComparisonCheck] = []

    if 'unknown' in (profile_a.level_semantics, profile_b.level_semantics):
        semantics_status: AmbientCheckStatus = 'UNKNOWN'
        semantics_reason = (
            'level semantics not recorded for one or both profiles'
        )
    elif profile_a.level_semantics != profile_b.level_semantics:
        semantics_status = 'BLOCKED'
        semantics_reason = (
            f'level semantics differ ({profile_a.level_semantics} vs '
            f'{profile_b.level_semantics}) — direct subtraction is not '
            'meaningful'
        )
    else:
        semantics_status = 'PASS'
        semantics_reason = (
            f'shared level semantics: {profile_a.level_semantics}'
        )
    checks.append(
        AmbientComparisonCheck(
            check='level_semantics_compatible',
            status=semantics_status,
            reason=semantics_reason,
        )
    )

    if 'unknown' in (profile_a.weighting, profile_b.weighting):
        weighting_status: AmbientCheckStatus = 'UNKNOWN'
        weighting_reason = 'weighting not recorded for one or both profiles'
    elif profile_a.weighting != profile_b.weighting:
        weighting_status = 'BLOCKED'
        weighting_reason = (
            f'weightings differ ({profile_a.weighting} vs '
            f'{profile_b.weighting}) — band levels are not comparable'
        )
    else:
        weighting_status = 'PASS'
        weighting_reason = f'shared weighting: {profile_a.weighting}'
    checks.append(
        AmbientComparisonCheck(
            check='weighting_compatible',
            status=weighting_status,
            reason=weighting_reason,
        )
    )

    if (
        profile_a.level_semantics != 'absolute_spl'
        or profile_b.level_semantics != 'absolute_spl'
    ):
        calibration_status: AmbientCheckStatus = 'BLOCKED'
        calibration_reason = (
            'profiles are not absolute SPL — no quantitative dB delta'
        )
    elif not (
        profile_a.calibration_authority_id
        and profile_b.calibration_authority_id
    ):
        calibration_status = 'UNKNOWN'
        calibration_reason = (
            'calibration authority not recorded for one or both profiles'
        )
    elif (
        profile_a.calibration_authority_id
        == profile_b.calibration_authority_id
    ):
        calibration_status = 'PASS'
        calibration_reason = 'shared calibration authority'
    else:
        calibration_status = 'UNKNOWN'
        calibration_reason = (
            'different calibration authorities — absolute accuracy of the '
            'delta is unverified'
        )
    checks.append(
        AmbientComparisonCheck(
            check='calibration_chain',
            status=calibration_status,
            reason=calibration_reason,
        )
    )

    axes: tuple[str, ...] = ()
    if condition_a is None or condition_b is None:
        checks.append(
            AmbientComparisonCheck(
                check='condition_axes_recorded',
                status='UNKNOWN',
                reason='operating conditions not supplied for one or both '
                'profiles — uncontrolled differences are unknowable',
            )
        )
    else:
        axes = differing_condition_axes(condition_a, condition_b)
        checks.append(
            AmbientComparisonCheck(
                check='condition_axes_recorded',
                status='PASS',
                reason=(
                    'differing axes: ' + ', '.join(axes)
                    if axes
                    else 'identical operating conditions'
                ),
            )
        )

    same_position = (
        profile_a.microphone_position == profile_b.microphone_position
    )
    if same_position:
        position_status: AmbientCheckStatus = 'PASS'
        position_reason = 'same measurement position'
    elif intent == 'spatial_noise_map':
        position_status = 'PASS'
        position_reason = (
            'position difference is the object of this comparison'
        )
    else:
        position_status = 'UNKNOWN'
        position_reason = (
            'measurement positions differ — the level difference conflates '
            'position and operating state'
        )
    checks.append(
        AmbientComparisonCheck(
            check='position_semantics',
            status=position_status,
            reason=position_reason,
        )
    )

    if intent == 'operating_state_ab':
        if condition_a is None or condition_b is None:
            intent_status: AmbientCheckStatus = 'UNKNOWN'
            intent_reason = 'A/B intent requires both recorded conditions'
        elif not axes:
            intent_status = 'UNKNOWN'
            intent_reason = (
                'no operating-state axis differs — nothing was toggled'
            )
        else:
            intent_status = 'PASS'
            intent_reason = 'controlled toggle on: ' + ', '.join(axes)
    elif intent == 'spatial_noise_map':
        if same_position:
            intent_status = 'UNKNOWN'
            intent_reason = 'positions identical — not a spatial map'
        else:
            intent_status = 'PASS'
            intent_reason = 'position difference recorded as the map axis'
    elif intent == 'repeatability':
        if not same_position:
            intent_status = 'UNKNOWN'
            intent_reason = (
                'position changed between repeats — not a repeatability '
                'probe'
            )
        elif axes:
            intent_status = 'UNKNOWN'
            intent_reason = (
                'operating axes changed between repeats: ' + ', '.join(axes)
            )
        else:
            intent_status = 'PASS'
            intent_reason = 'same position, same recorded conditions'
    elif intent == 'instrument_crosscheck':
        if (
            profile_a.acquisition_context_id is not None
            and profile_a.acquisition_context_id
            == profile_b.acquisition_context_id
        ):
            intent_status = 'UNKNOWN'
            intent_reason = (
                'same acquisition context — not an instrument crosscheck'
            )
        else:
            intent_status = 'PASS'
            intent_reason = 'distinct acquisition contexts'
    else:
        intent_status = 'PASS'
        intent_reason = 'diagnostic comparison — no intent requirements'
    checks.append(
        AmbientComparisonCheck(
            check='intent_requirements',
            status=intent_status,
            reason=intent_reason,
        )
    )

    if condition_a is None or condition_b is None:
        attribution_status: AmbientCheckStatus = 'UNKNOWN'
        attribution_reason = (
            'cannot attribute the difference — operating conditions '
            'unrecorded'
        )
    elif not same_position and intent != 'spatial_noise_map':
        attribution_status = 'UNKNOWN'
        attribution_reason = 'uncontrolled difference: microphone_position'
    elif intent == 'operating_state_ab':
        if not axes:
            attribution_status = 'UNKNOWN'
            attribution_reason = 'no differing axis recorded to attribute to'
        else:
            attribution_status = 'PASS'
            attribution_reason = 'difference attributable to recorded axes'
    elif axes:
        attribution_status = 'UNKNOWN'
        attribution_reason = (
            'uncontrolled operating differences: ' + ', '.join(axes)
        )
    else:
        attribution_status = 'PASS'
        attribution_reason = 'no uncontrolled differences detected'
    checks.append(
        AmbientComparisonCheck(
            check='difference_attributable',
            status=attribution_status,
            reason=attribution_reason,
        )
    )

    check_status = {check.check: check.status for check in checks}
    if (
        check_status['level_semantics_compatible'] == 'BLOCKED'
        or check_status['weighting_compatible'] == 'BLOCKED'
    ):
        level_compatibility: AmbientLevelCompatibility = 'blocked'
    elif (
        check_status['level_semantics_compatible'] == 'PASS'
        and check_status['weighting_compatible'] == 'PASS'
        and check_status['calibration_chain'] == 'PASS'
    ):
        level_compatibility = 'quantitative_delta_db'
    elif (
        check_status['level_semantics_compatible'] == 'PASS'
        and check_status['weighting_compatible'] == 'PASS'
    ):
        level_compatibility = 'relative_difference'
    else:
        level_compatibility = 'qualitative_only'

    difference = tuple(
        a - b
        for a, b in zip(profile_a.band_level_db, profile_b.band_level_db, strict=True)
    )
    overall = None
    if (
        profile_a.overall_level_db is not None
        and profile_b.overall_level_db is not None
    ):
        overall = profile_a.overall_level_db - profile_b.overall_level_db
    payload: dict[str, Any] = {
        'document_id': profile_a.document_id,
        'profile_a_id': profile_a.profile_id,
        'profile_a_sha256': profile_a.profile_sha256,
        'profile_b_id': profile_b.profile_id,
        'profile_b_sha256': profile_b.profile_sha256,
        'intent': intent,
        'level_compatibility': level_compatibility,
        'band_center_hz': profile_a.band_center_hz,
        'difference_db': difference,
        'overall_difference_db': overall,
        'compatibility_checks': tuple(checks),
        'differing_condition_axes': axes,
        'condition_a': condition_a,
        'condition_b': condition_b,
        'created_at': created_at,
    }
    provisional = AmbientNoiseComparison.model_construct(**canonicalize_payload(AmbientNoiseComparison, dict(
        **payload,
        comparison_id='',
        comparison_sha256='0' * 64,
    )))
    digest = _hash(provisional.identity_payload())
    return AmbientNoiseComparison(
        **payload,
        comparison_id='anc-' + digest[:24],
        comparison_sha256=digest,
    )


def ambient_snr_db(
    profile: AmbientNoiseProfile,
    signal_level_db: float,
) -> float:
    """SNR evidence for a signal of known level above this ambient profile.

    Requires a calibrated absolute-SPL profile with an overall level so the
    returned number is real evidence a quality gate can consume — never an
    estimate inferred from uncalibrated data.
    """
    if profile.level_semantics != 'absolute_spl':
        raise ValueError('ambient SNR requires absolute-SPL semantics')
    if profile.overall_level_db is None:
        raise ValueError('ambient SNR requires an overall level')
    if not isfinite(float(signal_level_db)):
        raise ValueError('signal level must be finite')
    return float(signal_level_db) - profile.overall_level_db


def ambient_overall_level_db(profile: AmbientNoiseProfile) -> float | None:
    """Overall level: the declared overall, or energy sum across bands.

    Only ``absolute_spl`` band data may be energy-combined into an overall
    SPL; relative/unknown profiles keep their declared overall (or None).
    """
    if profile.overall_level_db is not None:
        return profile.overall_level_db
    if profile.level_semantics != 'absolute_spl' or not profile.band_level_db:
        return None
    energy = sum(10.0 ** (level / 10.0) for level in profile.band_level_db)
    if energy <= 0:
        return None
    return 10.0 * log10(energy)


# ---------------------------------------------------------------------------
# Measurement-quality binding (#1025)


AmbientCompatibilityStatus = Literal['COMPATIBLE', 'INCOMPATIBLE', 'UNKNOWN']


class AmbientCompatibilityCheck(BaseModel):
    """One axis of ambient→measurement compatibility (#1025)."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: AmbientCheckStatus
    reason: str = Field(min_length=1)


class AmbientCompatibilityResult(BaseModel):
    """Ordered verdict: whether one profile may support one measurement."""

    model_config = ConfigDict(frozen=True)

    status: AmbientCompatibilityStatus
    checks: tuple[AmbientCompatibilityCheck, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_result(self) -> 'AmbientCompatibilityResult':
        statuses = {item.status for item in self.checks}
        expected: AmbientCompatibilityStatus
        if 'FAIL' in statuses or 'BLOCKED' in statuses:
            expected = 'INCOMPATIBLE'
        elif 'UNKNOWN' in statuses:
            expected = 'UNKNOWN'
        else:
            expected = 'COMPATIBLE'
        if self.status != expected:
            raise ValueError(
                f'compatibility status {self.status} disagrees with checks'
            )
        return self

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(item.reason for item in self.checks)


def check_ambient_measurement_compatibility(
    profile: AmbientNoiseProfile,
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    measurement_entity_id: str | None,
    measurement_position: Position3,
    position_tolerance_m: float = 0.0,
    acquisition_context_id: str | None = None,
    operating_condition: AmbientOperatingCondition | None = None,
    requires_absolute_spl: bool = False,
) -> AmbientCompatibilityResult:
    """Whether ``profile`` may feed quality evidence for one measurement.

    Every axis is explicit: document, exact SceneRevision, bound
    measurement entity, microphone position (within the declared
    tolerance), acquisition-context chain, resolved operating condition,
    and — only when the consumer needs absolute levels — the profile's
    calibrated-absolute-SPL authority. Any FAIL makes the profile
    INCOMPATIBLE; unknown axes keep the result UNKNOWN — never silently
    compatible.
    """

    checks: list[AmbientCompatibilityCheck] = []

    def _add(check: str, status: AmbientCheckStatus, reason: str) -> None:
        checks.append(
            AmbientCompatibilityCheck(check=check, status=status, reason=reason)
        )

    if profile.document_id == document_id:
        _add('document', 'PASS', 'ambient profile is bound to this document')
    else:
        _add(
            'document',
            'FAIL',
            f'ambient profile document {profile.document_id} != '
            f'{document_id}',
        )

    if (
        profile.scene_revision_id == scene_revision_id
        and profile.scene_content_hash == scene_content_hash
    ):
        _add('scene_revision', 'PASS', 'profile pins this exact scene revision')
    else:
        _add(
            'scene_revision',
            'FAIL',
            'ambient profile is bound to a different scene revision',
        )

    if profile.measurement_entity_id is None:
        _add(
            'measurement_entity',
            'UNKNOWN',
            'ambient profile declares no measurement entity',
        )
    elif measurement_entity_id is None:
        _add(
            'measurement_entity',
            'UNKNOWN',
            'ambient profile pins a measurement entity but the '
            'measurement declares none',
        )
    elif profile.measurement_entity_id == measurement_entity_id:
        _add(
            'measurement_entity',
            'PASS',
            'ambient profile pins this measurement entity',
        )
    else:
        _add(
            'measurement_entity',
            'FAIL',
            'ambient profile was captured at a different measurement entity',
        )

    distance = (
        (profile.microphone_position.x_m - measurement_position.x_m) ** 2
        + (profile.microphone_position.y_m - measurement_position.y_m) ** 2
        + (profile.microphone_position.z_m - measurement_position.z_m) ** 2
    ) ** 0.5
    if distance <= position_tolerance_m:
        _add(
            'microphone_position',
            'PASS',
            f'microphone position within {position_tolerance_m} m of the '
            'measurement position',
        )
    else:
        _add(
            'microphone_position',
            'FAIL',
            f'microphone is {distance:.3f} m from the measurement position '
            f'(tolerance {position_tolerance_m} m)',
        )

    if (
        profile.acquisition_context_id is not None
        and acquisition_context_id is not None
    ):
        if profile.acquisition_context_id == acquisition_context_id:
            _add(
                'acquisition_context',
                'PASS',
                'ambient profile shares the measurement acquisition context',
            )
        else:
            _add(
                'acquisition_context',
                'FAIL',
                'ambient profile used a different acquisition context',
            )
    else:
        _add(
            'acquisition_context',
            'UNKNOWN',
            'acquisition-context compatibility cannot be established',
        )

    if operating_condition is None:
        _add(
            'operating_condition',
            'UNKNOWN',
            'the resolved operating condition was not supplied',
        )
    elif operating_condition.condition_id == profile.condition_id and (
        operating_condition.condition_sha256 == profile.condition_sha256
    ):
        _add(
            'operating_condition',
            'PASS',
            'ambient profile is bound to this exact operating condition',
        )
    else:
        _add(
            'operating_condition',
            'FAIL',
            'ambient profile was captured under a different operating '
            'condition',
        )

    if requires_absolute_spl:
        if (
            profile.level_semantics == 'absolute_spl'
            and profile.calibration_authority_id is not None
        ):
            _add(
                'absolute_spl',
                'PASS',
                'profile declares calibrated absolute-SPL semantics',
            )
        else:
            _add(
                'absolute_spl',
                'FAIL',
                'absolute-SPL evidence requires calibrated absolute_spl '
                'semantics bound to a calibration authority (#1025)',
            )

    if any(item.status in ('FAIL', 'BLOCKED') for item in checks):
        status: AmbientCompatibilityStatus = 'INCOMPATIBLE'
    elif any(item.status == 'UNKNOWN' for item in checks):
        status = 'UNKNOWN'
    else:
        status = 'COMPATIBLE'
    return AmbientCompatibilityResult(status=status, checks=tuple(checks))


class AmbientNoiseEvidenceRef(BaseModel):
    """Typed ambient-noise lineage for measurement-quality evidence (#1025).

    Carries the exact profile identity plus the compatibility verdict the
    binding was produced under — and, only when COMPATIBLE, the derived
    noise-floor / SNR figures a consumer may use instead of provenance-free
    scalars.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    condition_id: str = Field(min_length=1)
    level_semantics: AmbientLevelSemantics
    compatibility: AmbientCompatibilityResult
    # The declared spatial tolerance and absolute-level requirement the
    # binding was checked under — pinned so authoritative reads can
    # recompute the exact same verdict.
    position_tolerance_m: float = Field(default=0.0, ge=0.0)
    requires_absolute_spl: bool = False
    noise_floor_db_spl: float | None = None
    snr_db: float | None = None

    @model_validator(mode='after')
    def valid_ref(self) -> 'AmbientNoiseEvidenceRef':
        for name, value in (
            ('noise_floor_db_spl', self.noise_floor_db_spl),
            ('snr_db', self.snr_db),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{name} must be finite')
        if self.compatibility.status != 'COMPATIBLE' and (
            self.noise_floor_db_spl is not None or self.snr_db is not None
        ):
            raise ValueError(
                'derived ambient figures require COMPATIBLE evidence'
            )
        return self


def bind_ambient_noise_evidence(
    profile: AmbientNoiseProfile,
    *,
    operating_condition: AmbientOperatingCondition,
    signal_level_db_spl: float | None = None,
    requires_absolute_spl: bool = False,
    **compatibility_kwargs: Any,
) -> AmbientNoiseEvidenceRef:
    """Bind ``profile`` to a measurement as typed quality evidence.

    Compatibility is checked first (``check_ambient_measurement_compatibility``
    kwargs). The operating condition the evidence was captured under is a
    required input — it becomes the ref's pinned condition so a replay can
    reproduce the exact verdict. Only a COMPATIBLE profile derives numbers:
    the noise floor is its overall level and the SNR is
    ``signal_level_db_spl`` minus that level through ``ambient_snr_db`` —
    which itself refuses non-absolute profiles. INCOMPATIBLE/UNKNOWN
    bindings carry no derived values.
    """

    compatibility = check_ambient_measurement_compatibility(
        profile,
        operating_condition=operating_condition,
        requires_absolute_spl=requires_absolute_spl,
        **compatibility_kwargs,
    )
    position_tolerance_m = float(
        compatibility_kwargs.get('position_tolerance_m', 0.0)
    )
    noise_floor: float | None = None
    snr: float | None = None
    if compatibility.status == 'COMPATIBLE':
        noise_floor = ambient_overall_level_db(profile)
        if signal_level_db_spl is not None:
            try:
                snr = ambient_snr_db(profile, signal_level_db_spl)
            except ValueError:
                # Non-absolute or level-less profiles never fabricate SNR.
                snr = None
    return AmbientNoiseEvidenceRef(
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        condition_id=operating_condition.condition_id,
        level_semantics=profile.level_semantics,
        compatibility=compatibility,
        position_tolerance_m=position_tolerance_m,
        requires_absolute_spl=requires_absolute_spl,
        noise_floor_db_spl=noise_floor,
        snr_db=snr,
    )


# ---------------------------------------------------------------------------
# Repository


class AmbientNoiseConflictError(ValueError):
    """An ambient-noise save violated append-only identity rules."""


class CadAmbientNoiseRepository:
    """Append-only storage for the ambient-noise authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self):
        return closing(connect_sqlite(self.path))

    def _initialize(self) -> None:
        with self._connect() as connection, connection:
            require_native_tables(connection, 'cad_ambient_conditions', 'cad_ambient_profiles', 'cad_ambient_criteria', 'cad_ambient_evaluations', 'cad_ambient_comparisons')

    def _save_model(self, table: str, key: str, payload_json: str, columns: tuple[str, ...], values: tuple) -> None:
        all_columns = (*columns, 'payload_json')
        with self._connect() as connection, connection:
            connection.execute(
                f"INSERT INTO {table} ({', '.join(all_columns)}) VALUES ({', '.join('?' for _ in all_columns)})",
                (*values, payload_json),
            )

    def save_condition(self, condition: AmbientOperatingCondition) -> None:
        if self.get_condition(condition.condition_id) is not None:
            raise AmbientNoiseConflictError('ambient conditions are append-only')
        revision = self.scene_repository.get(condition.scene_revision_id)
        if revision is None or revision.document_id != condition.document_id:
            raise ValueError('ambient condition SceneRevision does not exist')
        if revision.content_hash != condition.scene_content_hash:
            raise ValueError('ambient condition SceneRevision content hash mismatch')
        self._save_model(
            'cad_ambient_conditions',
            'condition_id',
            condition.model_dump_json(),
            ('condition_id', 'document_id', 'condition_sha256', 'created_at_utc'),
            (
                condition.condition_id,
                condition.document_id,
                condition.condition_sha256,
                _utc_now(),
            ),
        )

    def get_condition(self, condition_id: str) -> AmbientOperatingCondition | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_ambient_conditions WHERE condition_id=?',
                (condition_id,),
            ).fetchone()
        if row is None:
            return None
        return AmbientOperatingCondition.model_validate_json(row['payload_json'])

    def save_profile(self, profile: AmbientNoiseProfile) -> None:
        if self.get_profile(profile.profile_id) is not None:
            raise AmbientNoiseConflictError('ambient profiles are append-only')
        revision = self.scene_repository.get(profile.scene_revision_id)
        if revision is None or revision.document_id != profile.document_id:
            raise ValueError('ambient profile SceneRevision does not exist')
        if revision.content_hash != profile.scene_content_hash:
            raise ValueError('ambient profile SceneRevision content hash mismatch')
        condition = self.get_condition(profile.condition_id)
        if condition is None or condition.condition_sha256 != profile.condition_sha256:
            raise ValueError('ambient profile requires the exact persisted condition')
        # #1025: absolute-SPL claims need a bound calibration authority —
        # level_semantics is a declaration, never proof.
        if (
            profile.level_semantics == 'absolute_spl'
            and profile.calibration_authority_id is None
        ):
            raise ValueError(
                "absolute_spl ambient profiles require a bound "
                'calibration_authority_id (#643 authority)'
            )
        self._save_model(
            'cad_ambient_profiles',
            'profile_id',
            profile.model_dump_json(),
            ('profile_id', 'document_id', 'condition_id', 'profile_sha256', 'created_at_utc'),
            (
                profile.profile_id,
                profile.document_id,
                profile.condition_id,
                profile.profile_sha256,
                _utc_now(),
            ),
        )

    def get_profile(self, profile_id: str) -> AmbientNoiseProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_ambient_profiles WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return AmbientNoiseProfile.model_validate_json(row['payload_json'])

    def save_criterion(self, criterion: AmbientNoiseCriterion) -> None:
        if self.get_criterion(criterion.criterion_id) is not None:
            raise AmbientNoiseConflictError('ambient criteria are append-only')
        self._save_model(
            'cad_ambient_criteria',
            'criterion_id',
            criterion.model_dump_json(),
            ('criterion_id', 'criterion_sha256', 'created_at_utc'),
            (criterion.criterion_id, criterion.criterion_sha256, _utc_now()),
        )

    def get_criterion(self, criterion_id: str) -> AmbientNoiseCriterion | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_ambient_criteria WHERE criterion_id=?',
                (criterion_id,),
            ).fetchone()
        if row is None:
            return None
        return AmbientNoiseCriterion.model_validate_json(row['payload_json'])

    def save_evaluation(self, evaluation: AmbientCriteriaEvaluation) -> None:
        if self.get_evaluation(evaluation.evaluation_id) is not None:
            raise AmbientNoiseConflictError('ambient evaluations are append-only')
        profile = self.get_profile(evaluation.profile_id)
        if profile is None or profile.profile_sha256 != evaluation.profile_sha256:
            raise ValueError('evaluation requires the exact persisted profile')
        criterion = self.get_criterion(evaluation.criterion_id)
        if criterion is None or criterion.criterion_sha256 != evaluation.criterion_sha256:
            raise ValueError('evaluation requires the exact persisted criterion')
        replay = evaluate_ambient_criterion(
            profile,
            criterion,
            evaluated_at=evaluation.evaluated_at,
        )
        if replay.verdict != evaluation.verdict or replay.band_verdicts != evaluation.band_verdicts:
            raise ValueError('ambient evaluation does not replay to the persisted verdicts')
        self._save_model(
            'cad_ambient_evaluations',
            'evaluation_id',
            evaluation.model_dump_json(),
            (
                'evaluation_id',
                'profile_id',
                'criterion_id',
                'verdict',
                'evaluation_sha256',
                'created_at_utc',
            ),
            (
                evaluation.evaluation_id,
                evaluation.profile_id,
                evaluation.criterion_id,
                evaluation.verdict,
                evaluation.evaluation_sha256,
                _utc_now(),
            ),
        )

    def get_evaluation(self, evaluation_id: str) -> AmbientCriteriaEvaluation | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_ambient_evaluations WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return AmbientCriteriaEvaluation.model_validate_json(row['payload_json'])

    def save_comparison(self, comparison: AmbientNoiseComparison) -> None:
        if self.get_comparison(comparison.comparison_id) is not None:
            raise AmbientNoiseConflictError('ambient comparisons are append-only')
        profile_a = self.get_profile(comparison.profile_a_id)
        profile_b = self.get_profile(comparison.profile_b_id)
        if (
            profile_a is None
            or profile_b is None
            or profile_a.profile_sha256 != comparison.profile_a_sha256
            or profile_b.profile_sha256 != comparison.profile_b_sha256
        ):
            raise ValueError('ambient comparison requires both exact persisted profiles')
        replay = compare_ambient_profiles(
            profile_a,
            profile_b,
            created_at=comparison.created_at,
            intent=comparison.intent,
            condition_a=comparison.condition_a,
            condition_b=comparison.condition_b,
        )
        if (
            replay.difference_db != comparison.difference_db
            or replay.level_compatibility != comparison.level_compatibility
            or replay.differing_condition_axes
            != comparison.differing_condition_axes
            or replay.comparison_sha256 != comparison.comparison_sha256
        ):
            raise ValueError('ambient comparison does not replay to persisted values')
        self._save_model(
            'cad_ambient_comparisons',
            'comparison_id',
            comparison.model_dump_json(),
            ('comparison_id', 'document_id', 'comparison_sha256', 'created_at_utc'),
            (
                comparison.comparison_id,
                comparison.document_id,
                comparison.comparison_sha256,
                _utc_now(),
            ),
        )

    def get_comparison(self, comparison_id: str) -> AmbientNoiseComparison | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_ambient_comparisons WHERE comparison_id=?',
                (comparison_id,),
            ).fetchone()
        if row is None:
            return None
        return AmbientNoiseComparison.model_validate_json(row['payload_json'])

    def list_profiles(self, document_id: str) -> tuple[AmbientNoiseProfile, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_ambient_profiles WHERE document_id=? ORDER BY created_at_utc',
                (document_id,),
            ).fetchall()
        return tuple(
            AmbientNoiseProfile.model_validate_json(row['payload_json']) for row in rows
        )
