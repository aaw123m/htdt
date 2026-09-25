"""Raised seating-platform acoustic assembly authority (#1024).

A raised seating platform ("riser") is common in home theaters — but its
acoustic role is *decided*, not assumed. A riser may be purely structural
(seating elevation only), an acoustically inert target, a tactile
transducer platform, a vented low-frequency absorber, a sealed resonant
cavity, an equipment enclosure, or some mix — and the model must not infer
"every riser is a bass trap".

This module models the platform's acoustic assembly explicitly:

- :class:`RaisedPlatformAcousticAssembly` — the declared purposes of the
  platform. Purposes are *declared*, never inferred from geometry: a
  filled sealed riser is not automatically a bass absorber.
- :class:`PlatformCavity` — an enclosed sub-volume of the platform with
  its volume, partitions and porous fill. Multiple cavities are kept
  distinct — a multi-cell platform is not blindly merged into one box.
- :class:`PlatformOpening` — a vent/port/slot coupling a cavity to the
  room, with area, neck length, end-correction and blockage semantics.
  Openings are the difference between a sealed cavity and a resonator.
- :class:`PlatformPorousFill` — porous fill material references inside a
  cavity, with coverage fraction. Fill is provenance, not a spec.
- :func:`evaluate_platform_readiness` — per-platform readiness checks:
  declared purposes, cavity volumes, openings bound to cavities, unknown
  interiors stay UNKNOWN — an unspecified interior never reads as
  acoustically inert or absorptive.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus


RAISED_PLATFORM_AUTHORITY_VERSION = 'raised-platform-1'

#: What the platform is *for*, acoustically. Purposes are declared —
#: geometry never upgrades a platform to an absorber.
PlatformPurpose = Literal[
    'seating_elevation_only',
    'acoustically_inert_target',
    'tactile_platform',
    'vented_low_frequency_absorber',
    'sealed_resonant_platform',
    'equipment_enclosure',
    'mixed',
    'unknown',
]

#: How a platform opening communicates with the room.
PlatformOpeningKind = Literal[
    'slot',
    'round_port',
    'duct',
    'grille',
    'gap',
    'unknown',
]

#: End correction on an opening — what terminates the neck acoustically.
PlatformOpeningTermination = Literal[
    'flanged', 'unflanged', 'free_edge', 'unknown'
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


def _finite(value: object, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class PlatformPorousFill(BaseModel):
    """Porous fill record inside one platform cavity.

    ``coverage_fraction`` is the share of cavity volume filled — 0.8 means
    80% fill, not a density claim. ``material_ref`` binds a recorded
    porous-material authority; fill constants are never implied.
    """

    model_config = ConfigDict(frozen=True)

    material_ref: str = Field(min_length=1)
    coverage_fraction: float = Field(ge=0.0, le=1.0)
    placement_note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def finite_fill(self) -> 'PlatformPorousFill':
        _finite(self.coverage_fraction, field_name='coverage_fraction')
        return self


class PlatformCavity(BaseModel):
    """One enclosed sub-volume inside a raised platform.

    ``interior_state='unknown'` means the interior was never recorded — it
    must not be treated as empty, filled, or sealed by default.
    """

    model_config = ConfigDict(frozen=True)

    cavity_id: str = Field(min_length=1)
    volume_m3: float | None = Field(default=None, gt=0.0)
    interior_state: Literal['recorded', 'empty', 'unknown'] = 'unknown'
    porous_fill: tuple[PlatformPorousFill, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def valid_cavity(self) -> 'PlatformCavity':
        if self.volume_m3 is not None:
            _finite(self.volume_m3, field_name='cavity volume')
        if self.porous_fill and self.interior_state == 'unknown':
            raise ValueError(
                'recorded porous fill requires a recorded interior'
            )
        return self


class PlatformOpening(BaseModel):
    """One opening coupling a platform cavity to the room.

    ``kind`` + ``termination`` + geometry decide whether the cavity can
    resonate; ``blockage_state`` records whether the opening is clear,
    partially obstructed or sealed — a blocked vent is not an absorber.
    """

    model_config = ConfigDict(frozen=True)

    opening_id: str = Field(min_length=1)
    cavity_id: str | None = Field(default=None, min_length=1)
    kind: PlatformOpeningKind = 'unknown'
    area_m2: float | None = Field(default=None, gt=0.0)
    neck_length_m: float | None = Field(default=None, ge=0.0)
    termination: PlatformOpeningTermination = 'unknown'
    blockage_state: Literal['clear', 'partial', 'blocked', 'unknown'] = (
        'unknown'
    )
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def valid_opening(self) -> 'PlatformOpening':
        for name in ('area_m2', 'neck_length_m'):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        if self.blockage_state == 'blocked' and self.area_m2 is not None:
            raise ValueError(
                'a blocked opening cannot carry an open-area figure'
            )
        return self


class RaisedPlatformAcousticAssembly(BaseModel):
    """The acoustic assembly record for one raised platform.

    ``declared_purposes`` are the platform's *claimed* roles — the model
    keeps them so a consumer can ask "is this riser supposed to absorb?"
    without inferring it from the fact that cavities exist. Cavities and
    openings are per-cell records, never merged.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'raised-platform-1'
    ] = RAISED_PLATFORM_AUTHORITY_VERSION
    assembly_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    platform_entity_id: str = Field(min_length=1)
    declared_purposes: tuple[PlatformPurpose, ...] = ()
    cavities: tuple[PlatformCavity, ...] = ()
    openings: tuple[PlatformOpening, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_assembly(self) -> 'RaisedPlatformAcousticAssembly':
        cavity_ids = [cavity.cavity_id for cavity in self.cavities]
        if len(set(cavity_ids)) != len(cavity_ids):
            raise ValueError('duplicate platform cavity ids')
        opening_ids = [
            opening.opening_id for opening in self.openings
        ]
        if len(set(opening_ids)) != len(opening_ids):
            raise ValueError('duplicate platform opening ids')
        known = set(cavity_ids)
        for opening in self.openings:
            if opening.cavity_id is not None and (
                opening.cavity_id not in known
            ):
                raise ValueError(
                    f'opening {opening.opening_id} binds unknown cavity '
                    f'{opening.cavity_id}'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'RaisedPlatformAcousticAssembly semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'assembly_id': self.assembly_id,
            'version': self.version,
            'platform_entity_id': self.platform_entity_id,
            'declared_purposes': list(self.declared_purposes),
            'cavities': [
                cavity.model_dump(mode='json') for cavity in self.cavities
            ],
            'openings': [
                opening.model_dump(mode='json') for opening in self.openings
            ],
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


def build_raised_platform_assembly(
    *,
    assembly_id: str | None = None,
    version: str = '1',
    platform_entity_id: str,
    declared_purposes: tuple[PlatformPurpose, ...] = (),
    cavities: tuple[PlatformCavity, ...] = (),
    openings: tuple[PlatformOpening, ...] = (),
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> RaisedPlatformAcousticAssembly:
    payload: dict[str, Any] = {
        'authority_version': RAISED_PLATFORM_AUTHORITY_VERSION,
        'assembly_id': assembly_id or str(uuid4()),
        'version': version,
        'platform_entity_id': platform_entity_id,
        'declared_purposes': declared_purposes,
        'cavities': cavities,
        'openings': openings,
        'provenance': provenance,
    }
    provisional = RaisedPlatformAcousticAssembly.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return RaisedPlatformAcousticAssembly(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class PlatformReadinessCheck(BaseModel):
    """One readiness check on a platform assembly."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


class PlatformReadinessReport(BaseModel):
    """Readiness report for one raised platform — declared purposes, cavity
    records and openings are evaluated separately so an unspecified
    interior stays UNKNOWN, never acoustically inert by default."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    assembly_id: str
    assembly_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[PlatformReadinessCheck, ...]
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_report(self) -> 'PlatformReadinessReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('platform readiness report hash mismatch')
        expected = 'prr-' + self.report_sha256[:24]
        if self.report_id != expected:
            raise ValueError('platform readiness report id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': RAISED_PLATFORM_AUTHORITY_VERSION,
            'assembly_id': self.assembly_id,
            'assembly_sha256': self.assembly_sha256,
            'checks': [
                check.model_dump(mode='json') for check in self.checks
            ],
        }


def evaluate_platform_readiness(
    *, assembly: RaisedPlatformAcousticAssembly
) -> PlatformReadinessReport:
    """Report what a platform assembly can support acoustically.

    - ``purpose_declared``: the platform's acoustic role is recorded —
      ``unknown`` or absent is UNKNOWN, not a default;
    - ``cavities_recorded``: every declared cavity has a volume and a
      recorded interior state — an ``unknown`` interior stays UNKNOWN;
    - ``openings_bound``: every opening binds to a cavity (or is
      explicitly unbound) and carries geometry when a vented purpose is
      declared;
    - ``openings_unblocked``: openings carry a clear blockage state — a
      blocked or unrecorded vent is not an absorber.
    """

    checks: list[PlatformReadinessCheck] = []

    real_purposes = [
        purpose for purpose in assembly.declared_purposes
        if purpose not in ('unknown',)
    ]
    checks.append(
        PlatformReadinessCheck(
            check='purpose_declared',
            status='PASS' if real_purposes else 'UNKNOWN',
            reason=(
                'declared purposes: ' + ', '.join(real_purposes)
                if real_purposes
                else 'no acoustic purpose declared — the platform is not '
                'assumed inert or absorptive'
            ),
        )
    )

    if not assembly.cavities:
        checks.append(
            PlatformReadinessCheck(
                check='cavities_recorded',
                status='UNKNOWN',
                reason='no cavity records — the interior is unspecified',
            )
        )
    else:
        unknown_interiors = [
            cavity.cavity_id
            for cavity in assembly.cavities
            if cavity.interior_state == 'unknown'
            or cavity.volume_m3 is None
        ]
        checks.append(
            PlatformReadinessCheck(
                check='cavities_recorded',
                status='PASS' if not unknown_interiors else 'UNKNOWN',
                reason=(
                    'all cavities carry volume and interior state'
                    if not unknown_interiors
                    else 'cavities with unspecified interior/volume: '
                    + ', '.join(unknown_interiors)
                ),
            )
        )

    cavity_ids = {cavity.cavity_id for cavity in assembly.cavities}
    vented = 'vented_low_frequency_absorber' in assembly.declared_purposes
    bad_openings: list[str] = []
    for opening in assembly.openings:
        if opening.cavity_id is not None and (
            opening.cavity_id not in cavity_ids
        ):
            bad_openings.append(opening.opening_id)
        elif vented and opening.area_m2 is None:
            bad_openings.append(opening.opening_id)
    if not assembly.openings and vented:
        checks.append(
            PlatformReadinessCheck(
                check='openings_bound',
                status='UNKNOWN',
                reason='vented absorber purpose declared but no openings '
                'recorded',
            )
        )
    else:
        checks.append(
            PlatformReadinessCheck(
                check='openings_bound',
                status='PASS' if not bad_openings else 'UNKNOWN',
                reason=(
                    'all openings bound and geometrically recorded'
                    if not bad_openings
                    else 'openings missing binding or geometry: '
                    + ', '.join(bad_openings)
                ),
            )
        )

    if not assembly.openings:
        checks.append(
            PlatformReadinessCheck(
                check='openings_unblocked',
                status='NOT_APPLICABLE',
                reason='no openings recorded',
            )
        )
    else:
        blocked = [
            opening.opening_id
            for opening in assembly.openings
            if opening.blockage_state in ('blocked', 'unknown')
        ]
        checks.append(
            PlatformReadinessCheck(
                check='openings_unblocked',
                status='PASS' if not blocked else 'UNKNOWN',
                reason=(
                    'all openings carry a clear blockage state'
                    if not blocked
                    else 'openings blocked or blockage unknown: '
                    + ', '.join(blocked)
                    + ' — a blocked vent is not an absorber'
                ),
            )
        )

    probe = PlatformReadinessReport.model_construct(
        report_id='',
        assembly_id=assembly.assembly_id,
        assembly_sha256=assembly.semantic_sha256,
        checks=tuple(checks),
        report_sha256='',
    )
    digest = _hash(probe.identity_payload())
    return PlatformReadinessReport(
        **probe.model_dump(
            mode='python',
            exclude={'report_sha256', 'report_id'},
        ),
        report_id='prr-' + digest[:24],
        report_sha256=digest,
    )


__all__ = [
    'PlatformCavity',
    'PlatformOpening',
    'PlatformOpeningKind',
    'PlatformOpeningTermination',
    'PlatformPorousFill',
    'PlatformPurpose',
    'PlatformReadinessCheck',
    'PlatformReadinessReport',
    'RAISED_PLATFORM_AUTHORITY_VERSION',
    'RaisedPlatformAcousticAssembly',
    'build_raised_platform_assembly',
    'evaluate_platform_readiness',
]
