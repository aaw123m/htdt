"""Versioned target-curve profile authority (#508).

A room/EQ house target today exists only as a one-shot ``CadTargetCurve``
embedded inside a ``CadCalibrationPlan`` — ad-hoc text in a plan payload.
This module makes the target a first-class, document-scoped, versioned
profile so the *exact* intended target persists across calibration sessions
and can be referenced — by ``profile_id + version + semantic_sha256`` — from
calibration plans, preview/evaluation layers and comparison checks without
re-entering curve points or guessing which text was used.

Contract properties:

- profiles are immutable and versioned: the same ``profile_id`` may have
  many versions; a version row is append-only (repository rejects reuse of
  ``(profile_id, version)``) and carries a ``semantic_sha256`` of the whole
  payload — including the embedded curve — so a "used target" reference is
  unambiguous;
- the profile stores an actual :class:`CadTargetCurve` plus the optional
  :class:`CadTargetNormalizationCondition` — never a screenshot, filename or
  description — so stored hashes make saved plans, comparison results and
  reports auditable back to the declared target;
- binding a profile to a plan is an explicit append-only
  :class:`CalibrationPlanTargetBinding` record — ``CadCalibrationPlan``
  ``semantic_payload`` is a fixed authority dict and is *not* extended;
- target *meaning* is explicit: ``kind`` names the standard/room target
  family, ``source`` records where the target came from; nothing is silently
  assumed from curve shape.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import (
    CadTargetCurve,
    CadTargetNormalizationCondition,
)


TARGET_PROFILE_AUTHORITY_VERSION = 'target-curve-profile-1'

TargetProfileKind = Literal[
    'custom',
    'anechoic',
    'in_room',
    'diffuse_field',
    'manufacturer',
    'other',
]
TargetProfileSource = Literal[
    'authored',
    'manufacturer',
    'standard',
    'measured',
    'imported',
]


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


class TargetProfileTolerance(BaseModel):
    """A declared allowed-deviation band over one frequency interval.

    Tolerances are part of the profile's *meaning* (what counts as matching
    the target), so they ride inside the hashed payload.
    """

    model_config = ConfigDict(frozen=True)

    from_hz: float = Field(gt=0)
    to_hz: float = Field(gt=0)
    tolerance_db: float = Field(gt=0)

    @model_validator(mode='after')
    def valid_band(self) -> 'TargetProfileTolerance':
        if self.from_hz >= self.to_hz:
            raise ValueError('tolerance band must be strictly increasing')
        return self


class CadTargetCurveProfile(BaseModel):
    """Versioned, document-scoped target curve authority.

    ``curve`` is the declared target itself; ``normalization`` declares how a
    measured/predicted response is normalized against it. ``bindings`` record
    *where the profile applies* as opaque semantic references (e.g. a room
    or speaker selection recorded elsewhere) — the record carries them as
    ``artifact_ref`` strings so the profile hash is stable over unrelated
    authority evolution.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'target-curve-profile-1'
    ] = TARGET_PROFILE_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: TargetProfileKind
    source: TargetProfileSource
    description: str | None = None
    curve: CadTargetCurve
    normalization: CadTargetNormalizationCondition | None = None
    tolerances: tuple[TargetProfileTolerance, ...] = ()
    #: Semantic references this target applies to (room/speaker selections,
    #: exported plan bindings). Opaque here — resolved by callers.
    applies_to: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadTargetCurveProfile':
        if len(set(self.applies_to)) != len(self.applies_to):
            raise ValueError('profile applies_to references must be unique')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CadTargetCurveProfile semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'profile_id': self.profile_id,
            'version': self.version,
            'document_id': self.document_id,
            'name': self.name,
            'kind': self.kind,
            'source': self.source,
            'description': self.description,
            'curve': self.curve.model_dump(mode='json'),
            'normalization': (
                None
                if self.normalization is None
                else self.normalization.model_dump(mode='json')
            ),
            'tolerances': [
                item.model_dump(mode='json') for item in self.tolerances
            ],
            'applies_to': list(self.applies_to),
            'created_at_utc': self.created_at_utc,
        }


def build_target_profile(
    *,
    document_id: str,
    name: str,
    kind: TargetProfileKind,
    source: TargetProfileSource,
    curve: CadTargetCurve,
    created_at_utc: str,
    profile_id: str | None = None,
    version: str = '1',
    description: str | None = None,
    normalization: CadTargetNormalizationCondition | None = None,
    tolerances: Sequence[TargetProfileTolerance] = (),
    applies_to: Sequence[str] = (),
) -> CadTargetCurveProfile:
    payload: dict[str, Any] = {
        'authority_version': TARGET_PROFILE_AUTHORITY_VERSION,
        'profile_id': profile_id or str(uuid4()),
        'version': version,
        'document_id': document_id,
        'name': name,
        'kind': kind,
        'source': source,
        'description': description,
        'curve': curve,
        'normalization': normalization,
        'tolerances': tuple(tolerances),
        'applies_to': tuple(applies_to),
        'created_at_utc': created_at_utc,
    }
    provisional = CadTargetCurveProfile.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return CadTargetCurveProfile(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class CalibrationPlanTargetBinding(BaseModel):
    """Append-only fact binding a CadCalibrationPlan to an exact profile.

    The plan's frozen payload can only embed the inline ``target_curve``;
    which *profile version* that curve was authored from (or must equal)
    lives here, pinned by both semantic hashes. A binding records an
    identity assertion — the repository checks both authorities exist.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'target-curve-profile-1'
    ] = TARGET_PROFILE_AUTHORITY_VERSION
    binding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    bound_at_utc: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_binding(self) -> 'CalibrationPlanTargetBinding':
        if self.binding_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CalibrationPlanTargetBinding hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'binding_id': self.binding_id,
            'document_id': self.document_id,
            'plan_id': self.plan_id,
            'plan_semantic_sha256': self.plan_semantic_sha256,
            'profile_id': self.profile_id,
            'profile_version': self.profile_version,
            'profile_semantic_sha256': self.profile_semantic_sha256,
            'bound_at_utc': self.bound_at_utc,
        }


def build_plan_target_binding(
    *,
    document_id: str,
    plan_id: str,
    plan_semantic_sha256: str,
    profile: CadTargetCurveProfile,
    bound_at_utc: str,
    binding_id: str | None = None,
) -> CalibrationPlanTargetBinding:
    payload: dict[str, Any] = {
        'authority_version': TARGET_PROFILE_AUTHORITY_VERSION,
        'binding_id': binding_id or str(uuid4()),
        'document_id': document_id,
        'plan_id': plan_id,
        'plan_semantic_sha256': plan_semantic_sha256,
        'profile_id': profile.profile_id,
        'profile_version': profile.version,
        'profile_semantic_sha256': profile.semantic_sha256,
        'bound_at_utc': bound_at_utc,
    }
    provisional = CalibrationPlanTargetBinding.model_construct(
        **payload, binding_sha256='0' * 64
    )
    return CalibrationPlanTargetBinding(
        **payload,
        binding_sha256=_hash(provisional.semantic_payload()),
    )


__all__ = [
    'CadTargetCurveProfile',
    'CalibrationPlanTargetBinding',
    'TARGET_PROFILE_AUTHORITY_VERSION',
    'TargetProfileKind',
    'TargetProfileSource',
    'TargetProfileTolerance',
    'build_plan_target_binding',
    'build_target_profile',
]
