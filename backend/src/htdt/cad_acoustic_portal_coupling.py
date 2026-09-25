"""Connected-space acoustic portal coupling authority (#1029).

A home theater rarely terminates at its own walls: an open doorway leaks
low-frequency energy into a hallway, a stair opening couples a second
volume's modes into the room, a window radiates to the exterior. The
existing geometry compiler and portal-graph infra describe *where* an
opening is — this module records what it is *acoustically*: the physical
portal, its acoustic state, what terminates the far side, and whether a
coupled analysis is supportable.

The contract keeps the physical and acoustic records distinct:

- :class:`ConnectedAcousticRegion` — a bounded acoustic volume that may
  exchange energy with the primary room. Regions are declared with their
  own volume/enclosure evidence — an adjacent space is never silently
  treated as a perfectly absorbing wall.
- :class:`AcousticPortalCoupling` — the acoustic authority for one
  physical opening: its portal kind (doorway, passage, stair, grille,
  duct, window, unresolved external), a state profile (closed / open /
  partially open / custom open fraction), and the *termination* on the
  far side — an explicit adjacent region, a measured termination
  impedance, a bounded absorbing termination, exterior radiation, or a
  user-declared approximation. ``unknown_adjacent_space`` is recorded,
  never defaulted to anechoic.
- :func:`evaluate_portal_readiness` — per-portal readiness is a set of
  checks (topology, aperture geometry, state, adjacent-region geometry,
  solver support), not one READY boolean. A single-room workflow is
  unchanged: models without portals evaluate to NOT_APPLICABLE.
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


PORTAL_COUPLING_AUTHORITY_VERSION = 'portal-coupling-1'

#: Physical kind of the opening — geometry semantics for the coupling.
AcousticPortalKind = Literal[
    'doorway',
    'partial_open',
    'passage',
    'stair',
    'grille',
    'duct',
    'window',
    'unresolved_external',
]

#: The acoustic state the portal is in — ``custom`` carries an explicit
#: open fraction; ``unknown`` means nobody recorded it.
AcousticPortalState = Literal[
    'closed', 'open', 'partially_open', 'custom', 'unknown'
]

#: What acoustically terminates the far side of the portal.
PortalTerminationKind = Literal[
    'unknown_adjacent_space',
    'measured_termination_impedance',
    'bounded_absorbing_termination',
    'exterior_radiation',
    'user_declared_approximation',
    'connected_region',
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


class ConnectedAcousticRegion(BaseModel):
    """A bounded acoustic volume that can couple to the primary room.

    ``volume_m3`` is the region's own volume — declared or measured, never
    assumed infinite. ``enclosure_evidence`` notes what bounds the region
    (e.g. 'hallway walls + stair', 'exterior shell').
    """

    model_config = ConfigDict(frozen=True)

    region_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    volume_m3: float | None = Field(default=None, gt=0.0)
    enclosure_evidence: str | None = Field(default=None, min_length=1)
    #: Whether the region's own modal behaviour is modelled — distinct
    #: from acting as a lumped termination.
    modal_domain: Literal['resolved', 'lumped', 'ignored', 'unknown'] = (
        'unknown'
    )
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def finite_region(self) -> 'ConnectedAcousticRegion':
        if self.volume_m3 is not None:
            _finite(self.volume_m3, field_name='region volume')
        return self


class AcousticPortalCoupling(BaseModel):
    """Acoustic authority for one physical portal between regions.

    ``open_fraction`` is only meaningful for ``state='partially_open'`` or
    ``'custom'``; a closed portal still records state (closed doors still
    leak at LF) — state is evidence, not a skip.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'portal-coupling-1'
    ] = PORTAL_COUPLING_AUTHORITY_VERSION
    portal_coupling_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    #: The physical portal this couples (e.g. a PhysicalSpacePortal or
    #: PortalDeclaration id) — geometry stays on the physical record.
    physical_portal_ref: str = Field(min_length=1)
    from_region_id: str = Field(min_length=1)
    to_region_id: str | None = Field(default=None, min_length=1)
    kind: AcousticPortalKind = 'unresolved_external'
    state: AcousticPortalState = 'unknown'
    open_fraction: float | None = Field(default=None, gt=0.0, lt=1.0)
    termination: PortalTerminationKind = 'unknown_adjacent_space'
    #: The effective aperture area for acoustic exchange (m^2) — the
    #: physical opening's flow area, not the frame.
    effective_aperture_m2: float | None = Field(default=None, gt=0.0)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_coupling(self) -> 'AcousticPortalCoupling':
        if self.open_fraction is not None:
            _finite(self.open_fraction, field_name='open_fraction')
            if self.state not in ('partially_open', 'custom'):
                raise ValueError(
                    'open_fraction requires a partially_open or custom '
                    'state'
                )
        if self.state in ('partially_open', 'custom') and (
            self.open_fraction is None
        ):
            raise ValueError(
                'partially_open/custom state requires an open_fraction'
            )
        if self.termination == 'connected_region':
            if self.to_region_id is None:
                raise ValueError(
                    'connected_region termination requires to_region_id'
                )
            if self.to_region_id == self.from_region_id:
                raise ValueError('a portal cannot couple a region to itself')
        if self.effective_aperture_m2 is not None:
            _finite(self.effective_aperture_m2, field_name='aperture')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('AcousticPortalCoupling semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'portal_coupling_id': self.portal_coupling_id,
            'version': self.version,
            'physical_portal_ref': self.physical_portal_ref,
            'from_region_id': self.from_region_id,
            'to_region_id': self.to_region_id,
            'kind': self.kind,
            'state': self.state,
            'open_fraction': self.open_fraction,
            'termination': self.termination,
            'effective_aperture_m2': self.effective_aperture_m2,
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


def build_connected_acoustic_region(
    *,
    region_id: str,
    label: str,
    volume_m3: float | None = None,
    enclosure_evidence: str | None = None,
    modal_domain: Literal['resolved', 'lumped', 'ignored', 'unknown'] = 'unknown',
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> ConnectedAcousticRegion:
    return ConnectedAcousticRegion(
        region_id=region_id,
        label=label,
        volume_m3=volume_m3,
        enclosure_evidence=enclosure_evidence,
        modal_domain=modal_domain,
        provenance=provenance,
    )


def build_acoustic_portal_coupling(
    *,
    portal_coupling_id: str | None = None,
    version: str = '1',
    physical_portal_ref: str,
    from_region_id: str,
    to_region_id: str | None = None,
    kind: AcousticPortalKind = 'unresolved_external',
    state: AcousticPortalState = 'unknown',
    open_fraction: float | None = None,
    termination: PortalTerminationKind = 'unknown_adjacent_space',
    effective_aperture_m2: float | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> AcousticPortalCoupling:
    payload: dict[str, Any] = {
        'authority_version': PORTAL_COUPLING_AUTHORITY_VERSION,
        'portal_coupling_id': portal_coupling_id or str(uuid4()),
        'version': version,
        'physical_portal_ref': physical_portal_ref,
        'from_region_id': from_region_id,
        'to_region_id': to_region_id,
        'kind': kind,
        'state': state,
        'open_fraction': open_fraction,
        'termination': termination,
        'effective_aperture_m2': effective_aperture_m2,
        'provenance': provenance,
    }
    provisional = AcousticPortalCoupling.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return AcousticPortalCoupling(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class PortalReadinessCheck(BaseModel):
    """One per-portal readiness check."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


class PortalReadinessReport(BaseModel):
    """Per-portal readiness: topology, aperture, state, adjacent geometry
    and solver support are separate checks — never collapsed into one
    READY flag."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    portal_coupling_id: str
    coupling_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[PortalReadinessCheck, ...]
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_report(self) -> 'PortalReadinessReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('portal readiness report hash mismatch')
        expected = 'pcr-' + self.report_sha256[:24]
        if self.report_id != expected:
            raise ValueError('portal readiness report id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': PORTAL_COUPLING_AUTHORITY_VERSION,
            'portal_coupling_id': self.portal_coupling_id,
            'coupling_sha256': self.coupling_sha256,
            'checks': [
                check.model_dump(mode='json') for check in self.checks
            ],
        }


def evaluate_portal_readiness(
    *,
    coupling: AcousticPortalCoupling,
    known_region_ids: tuple[str, ...] = (),
    solver_capabilities: tuple[str, ...] = (),
) -> PortalReadinessReport:
    """Report the readiness of one acoustic portal coupling.

    - ``topology_recorded``: kind + both endpoint regions declared;
      ``connected_region`` termination requires the adjacent region to
      resolve — an explicit region is never treated as an absorbing wall;
    - ``aperture_recorded``: effective aperture or open fraction recorded;
      a fully-closed portal is PASS (closed is a valid acoustic state);
    - ``state_recorded``: the portal's open/closed state is declared;
    - ``adjacent_geometry``: the far side carries its own volume/geometry
      evidence when a connected region is claimed;
    - ``solver_support``: the solver must declare a portal-coupling
      capability ('portal_coupling') — undeclared support is UNKNOWN.
    """

    checks: list[PortalReadinessCheck] = []
    known_regions = set(known_region_ids)

    if coupling.termination == 'connected_region':
        resolved = coupling.to_region_id in known_regions
        checks.append(
            PortalReadinessCheck(
                check='topology_recorded',
                status='PASS' if resolved else 'FAIL',
                reason=(
                    f"portal couples '{coupling.from_region_id}' to "
                    f"'{coupling.to_region_id}'"
                    if resolved
                    else f"adjacent region '{coupling.to_region_id}' "
                    'unresolved — never treated as absorbing wall'
                ),
            )
        )
    else:
        checks.append(
            PortalReadinessCheck(
                check='topology_recorded',
                status='PASS',
                reason=(
                    f'physical portal {coupling.kind} terminates as '
                    f'{coupling.termination}'
                ),
            )
        )

    if coupling.state == 'closed':
        checks.append(
            PortalReadinessCheck(
                check='aperture_recorded',
                status='PASS',
                reason='portal closed — no aperture required (closed is '
                'a valid acoustic state, not a skipped record)',
            )
        )
    else:
        has_aperture = coupling.effective_aperture_m2 is not None or (
            coupling.open_fraction is not None
        )
        checks.append(
            PortalReadinessCheck(
                check='aperture_recorded',
                status='PASS' if has_aperture else 'UNKNOWN',
                reason=(
                    'aperture/open fraction recorded'
                    if has_aperture
                    else 'no effective aperture or open fraction recorded'
                ),
            )
        )

    checks.append(
        PortalReadinessCheck(
            check='state_recorded',
            status='PASS' if coupling.state != 'unknown' else 'UNKNOWN',
            reason=(
                f'portal state: {coupling.state}'
                if coupling.state != 'unknown'
                else 'portal state not recorded'
            ),
        )
    )

    if coupling.termination == 'connected_region':
        checks.append(
            PortalReadinessCheck(
                check='adjacent_geometry',
                status='UNKNOWN',
                reason='adjacent region geometry needs a '
                'ConnectedAcousticRegion record (volume/enclosure) — '
                'supplied separately',
            )
        )
    elif coupling.termination == 'measured_termination_impedance':
        checks.append(
            PortalReadinessCheck(
                check='adjacent_geometry',
                status='PASS',
                reason='far side terminated by measured impedance record',
            )
        )
    else:
        checks.append(
            PortalReadinessCheck(
                check='adjacent_geometry',
                status='NOT_APPLICABLE',
                reason=(
                    f'{coupling.termination} carries no region geometry '
                    'claim'
                ),
            )
        )

    if not solver_capabilities:
        solver_status: EvaluationStatus = 'UNKNOWN'
        solver_reason = 'no solver capabilities declared'
    elif 'portal_coupling' in solver_capabilities:
        solver_status = 'PASS'
        solver_reason = 'solver declares portal coupling support'
    else:
        solver_status = 'FAIL'
        solver_reason = 'solver lacks portal_coupling capability'
    checks.append(
        PortalReadinessCheck(
            check='solver_support',
            status=solver_status,
            reason=solver_reason,
        )
    )

    probe = PortalReadinessReport.model_construct(
        report_id='',
        portal_coupling_id=coupling.portal_coupling_id,
        coupling_sha256=coupling.semantic_sha256,
        checks=tuple(checks),
        report_sha256='',
    )
    digest = _hash(probe.identity_payload())
    return PortalReadinessReport(
        **probe.model_dump(
            mode='python',
            exclude={'report_sha256', 'report_id'},
        ),
        report_id='pcr-' + digest[:24],
        report_sha256=digest,
    )


__all__ = [
    'AcousticPortalCoupling',
    'AcousticPortalKind',
    'AcousticPortalState',
    'ConnectedAcousticRegion',
    'PORTAL_COUPLING_AUTHORITY_VERSION',
    'PortalReadinessCheck',
    'PortalReadinessReport',
    'PortalTerminationKind',
    'build_acoustic_portal_coupling',
    'build_connected_acoustic_region',
    'evaluate_portal_readiness',
]
