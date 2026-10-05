"""As-built cable / port traceability authority (#597).

Two stores already existed but neither tracks the *physical* wiring:

- ``cad_signal_path`` is a *logical* graph (source → AVR → display); an
  edge says the signal should flow, not which cable carries it;
- ``cad_cable_run`` is a *designed* route (segments, path kinds, lengths);
  it has no termination identity, no labels and no verification state.

This module adds the physical layer: ordered interconnect hops between two
declared terminations (device port, patch cable, panel, wall plate,
permanent run, adapter, destination port), label identity from a
referenced external label profile (e.g. AVIXA F501.01 — referenced by
``standard_id@edition`` registry key from the external-standards
authority, never baked into core), observation states, domain-scoped
verification records, logical→physical bindings, a derived cable
schedule, and a service-change supersede chain.

Fail-closed rules honoured:

- a logical route without a bound physical path is ``unverified_routing``;
- designed wiring and field wiring are different states — only a
  verification record can promote a path to ``verified_path``;
- a path declared ``verified_path`` without a passing domain-appropriate
  test surfaces ``promotion_gap``, not silent agreement;
- speaker polarity is *physical wiring* (termination ``polarity``), never
  a DSP/phase claim;
- no hidden cable geometry is fabricated: ``observation_state`` records
  what was actually seen; ``documented_not_observed`` stays honest.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _digest


PHYSICAL_INTERCONNECT_SCHEMA_VERSION = 1
PHYSICAL_INTERCONNECT_AUTHORITY_VERSION = 'rev56-physical-interconnect-1'
WIRING_VERIFICATION_AUTHORITY_VERSION = 'rev56-wiring-verification-1'
LOGICAL_PHYSICAL_BINDING_AUTHORITY_VERSION = 'rev56-logical-physical-binding-1'
CABLE_SCHEDULE_AUTHORITY_VERSION = 'rev56-cable-schedule-1'


#: Path medium/class (#597 §4).
PhysicalPathClass = Literal[
    'analog_audio',
    'digital_audio',
    'speaker_level',
    'hdmi',
    'network_copper',
    'network_fiber',
    'control',
    'trigger',
    'power',
    'other',
]

#: As-built declaration — *what the record claims*, not what was seen.
PathEvidenceState = Literal[
    'designed_path',
    'installed_reported_path',
    'field_observed_path',
    'verified_path',
]

#: What was actually observed on the path (#597 §13).
ObservationState = Literal[
    'observed_both_ends',
    'observed_one_end',
    'documented_not_observed',
    'inferred_by_test',
    'unknown_route',
]

#: Verification test kinds — each is an independent claim (#597 §14).
WiringTestKind = Literal[
    'visual_label_check',
    'continuity',
    'pinout_polarity',
    'loop_resistance',
    'length',
    'certification',
    'optical_loss',
    'link_negotiation',
    'hdmi_domain_reference',
    'speaker_polarity',
    'domain_qualification',
]

WiringTestResult = Literal['pass', 'fail', 'indeterminate', 'not_applicable']

#: Hop kinds inside an ordered physical path (#597 §5).
InterconnectHopKind = Literal[
    'device_port',
    'patch_cable',
    'patch_panel',
    'wall_plate',
    'terminal_block',
    'permanent_run',
    'adapter_extender',
    'destination_termination',
]

TerminationRole = Literal['from', 'to', 'intermediate']

TerminationPolarity = Literal[
    'normal', 'reversed', 'unknown', 'not_applicable'
]

#: Logical→physical binding verdict (#597 §12).
LogicalBindingState = Literal[
    'verified_binding',
    'observed_binding',
    'reported_binding',
    'designed_binding',
    'unverified_routing',
    'unverified_declaration',
    'mismatched',
    'stale',
    'failed_path',
]


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


#: Tests that are meaningful per path class — other tests are simply not
#: recorded against that class instead of being forced (#597 §14).
_APPLICABLE_TESTS: dict[str, frozenset[str]] = {
    'analog_audio': frozenset({
        'visual_label_check', 'continuity', 'pinout_polarity',
        'loop_resistance', 'length', 'domain_qualification',
    }),
    'digital_audio': frozenset({
        'visual_label_check', 'continuity', 'pinout_polarity',
        'length', 'domain_qualification',
    }),
    'speaker_level': frozenset({
        'visual_label_check', 'continuity', 'pinout_polarity',
        'loop_resistance', 'length', 'speaker_polarity',
        'domain_qualification',
    }),
    'hdmi': frozenset({
        'visual_label_check', 'continuity', 'length', 'certification',
        'hdmi_domain_reference', 'domain_qualification',
    }),
    'network_copper': frozenset({
        'visual_label_check', 'continuity', 'pinout_polarity',
        'loop_resistance', 'length', 'certification',
        'link_negotiation', 'domain_qualification',
    }),
    'network_fiber': frozenset({
        'visual_label_check', 'continuity', 'length', 'certification',
        'optical_loss', 'link_negotiation', 'domain_qualification',
    }),
    'control': frozenset({
        'visual_label_check', 'continuity', 'length',
        'domain_qualification',
    }),
    'trigger': frozenset({
        'visual_label_check', 'continuity', 'pinout_polarity', 'length',
        'domain_qualification',
    }),
    'power': frozenset({
        'visual_label_check', 'continuity', 'pinout_polarity',
        'loop_resistance', 'domain_qualification',
    }),
    'other': frozenset({
        'visual_label_check', 'continuity', 'length',
        'domain_qualification',
    }),
}


def applicable_test_kinds(path_class: PhysicalPathClass) -> frozenset[str]:
    """Domain-appropriate verification tests for a path class."""
    return _APPLICABLE_TESTS[path_class]


class CableTermination(BaseModel):
    """Exact termination/port identity at one end (or intermediate point).

    A termination names *where the cable lands*: device instance + port
    identity, or a panel/wall-plate/terminal position. Port identity is
    declared explicitly — never inferred from the signal role (#597 §8).
    ``polarity`` is physical wiring truth (speaker +/-), not DSP phase.
    """

    model_config = ConfigDict(frozen=True)

    role: TerminationRole
    device_instance_ref: str | None = Field(default=None, min_length=1)
    entity_id: str | None = Field(default=None, min_length=1)
    port_identity: str | None = Field(default=None, min_length=1)
    connector_type: str | None = Field(default=None, min_length=1)
    patch_panel_ref: str | None = Field(default=None, min_length=1)
    patch_position: str | None = Field(default=None, min_length=1)
    wall_plate_ref: str | None = Field(default=None, min_length=1)
    terminal_ref: str | None = Field(default=None, min_length=1)
    polarity: TerminationPolarity = 'not_applicable'
    pinout_ref: str | None = Field(default=None, min_length=1)
    observed_label: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_termination(self) -> 'CableTermination':
        anchors = (
            self.device_instance_ref,
            self.entity_id,
            self.port_identity,
            self.patch_panel_ref,
            self.wall_plate_ref,
            self.terminal_ref,
            self.observed_label,
        )
        if all(anchor is None for anchor in anchors):
            raise ValueError(
                'a termination requires at least one identity anchor '
                '(device/port/panel/plate/terminal/label)'
            )
        if (self.patch_position is None) != (self.patch_panel_ref is None):
            raise ValueError(
                'patch position requires its patch panel reference'
            )
        return self

    def identity_key(self) -> str:
        """Canonical endpoint identity for logical↔physical comparison."""
        anchor = (
            self.device_instance_ref
            or self.entity_id
            or self.terminal_ref
            or self.patch_panel_ref
            or self.wall_plate_ref
            or self.observed_label
            or 'unknown'
        )
        port = self.port_identity or self.patch_position or ''
        return f'{anchor.strip().lower()}|{port.strip().lower()}'


class InterconnectHop(BaseModel):
    """One ordered element of the physical path graph."""

    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=0)
    kind: InterconnectHopKind
    ref_id: str | None = Field(default=None, min_length=1)
    label: str | None = Field(default=None, min_length=1)
    length_m: float | None = Field(default=None, gt=0.0)
    note: str | None = Field(default=None, min_length=1)

    @field_validator('length_m')
    @classmethod
    def finite_length(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='hop length')


class PhysicalInterconnect(BaseModel):
    """Sealed as-built physical path between two terminations.

    ``evidence_state`` is the declared as-built state — it is promoted to
    ``verified_path`` only by reconciliation against passing
    ``WiringVerificationRecord`` evidence, never by assertion.
    ``observation_state`` records what was actually seen so a
    ``documented_not_observed`` path can never masquerade as field-checked.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PHYSICAL_INTERCONNECT_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-physical-interconnect-1'
    ] = PHYSICAL_INTERCONNECT_AUTHORITY_VERSION
    path_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str | None = Field(default=None, min_length=1)
    scene_content_hash: str | None = Field(default=None, min_length=1)
    path_class: PhysicalPathClass
    evidence_state: PathEvidenceState
    observation_state: ObservationState = 'documented_not_observed'
    from_termination: CableTermination
    to_termination: CableTermination
    hops: tuple[InterconnectHop, ...] = ()
    label_end_a: str | None = Field(default=None, min_length=1)
    label_end_b: str | None = Field(default=None, min_length=1)
    intermediate_labels: tuple[str, ...] = ()
    label_profile_standard_ref: str | None = Field(
        default=None, min_length=1
    )
    cable_manufacturer: str | None = Field(default=None, min_length=1)
    cable_model: str | None = Field(default=None, min_length=1)
    cable_category: str | None = Field(default=None, min_length=1)
    conductor_count: int | None = Field(default=None, ge=1)
    conductor_gauge: str | None = Field(default=None, min_length=1)
    length_designed_m: float | None = Field(default=None, gt=0.0)
    length_estimated_m: float | None = Field(default=None, gt=0.0)
    length_measured_m: float | None = Field(default=None, gt=0.0)
    installed_route_ref: str | None = Field(default=None, min_length=1)
    supersedes_path_id: str | None = Field(default=None, min_length=1)
    supersedes_path_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    service_action: str | None = Field(default=None, min_length=1)
    attachment_refs: tuple[str, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator(
        'length_designed_m', 'length_estimated_m', 'length_measured_m'
    )
    @classmethod
    def finite_path_length(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='path length')

    @field_validator('intermediate_labels', 'attachment_refs')
    @classmethod
    def nonempty_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item or not item.strip():
                raise ValueError('label/attachment entries must be non-empty')
        return value

    @model_validator(mode='after')
    def valid_path(self) -> 'PhysicalInterconnect':
        if (self.scene_revision_id is None) != (
            self.scene_content_hash is None
        ):
            raise ValueError(
                'scene pin requires revision id and content hash together'
            )
        if (self.supersedes_path_id is None) != (
            self.supersedes_path_sha256 is None
        ):
            raise ValueError(
                'a superseded path requires id and content hash together'
            )
        if self.supersedes_path_id is not None and (
            self.supersedes_path_id == self.path_id
            and self.supersedes_path_sha256 == self.semantic_sha256
        ):
            raise ValueError('a path cannot supersede itself')
        sequences = [hop.sequence for hop in self.hops]
        if len(sequences) != len(set(sequences)):
            raise ValueError('hop sequences must be unique')
        if sequences != sorted(sequences):
            raise ValueError('hops must be ordered by sequence')
        for hop in self.hops:
            if hop.kind == 'permanent_run' and hop.ref_id is None:
                raise ValueError(
                    'a permanent_run hop requires a route reference'
                )
        if self.from_termination.role != 'from':
            raise ValueError('from_termination must carry role "from"')
        if self.to_termination.role != 'to':
            raise ValueError('to_termination must carry role "to"')
        if self.observation_state == 'observed_both_ends' and (
            self.from_termination.observed_label is None
            or self.to_termination.observed_label is None
        ):
            raise ValueError(
                'observed_both_ends requires observed labels at both '
                'terminations'
            )
        if self.observation_state == 'observed_one_end' and (
            self.from_termination.observed_label is None
            and self.to_termination.observed_label is None
        ):
            raise ValueError(
                'observed_one_end requires an observed label at at '
                'least one termination'
            )
        if self.label_profile_standard_ref is not None and (
            '@' not in self.label_profile_standard_ref
        ):
            raise ValueError(
                'label profile must reference an external-standards '
                'registry key (standard_id@edition)'
            )
        digest = _digest(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('PhysicalInterconnect hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'path_id': self.path_id,
            'version': self.version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'path_class': self.path_class,
            'evidence_state': self.evidence_state,
            'observation_state': self.observation_state,
            'from_termination': self.from_termination.model_dump(mode='json'),
            'to_termination': self.to_termination.model_dump(mode='json'),
            'hops': [hop.model_dump(mode='json') for hop in self.hops],
            'label_end_a': self.label_end_a,
            'label_end_b': self.label_end_b,
            'intermediate_labels': list(self.intermediate_labels),
            'label_profile_standard_ref': self.label_profile_standard_ref,
            'cable_manufacturer': self.cable_manufacturer,
            'cable_model': self.cable_model,
            'cable_category': self.cable_category,
            'conductor_count': self.conductor_count,
            'conductor_gauge': self.conductor_gauge,
            'length_designed_m': self.length_designed_m,
            'length_estimated_m': self.length_estimated_m,
            'length_measured_m': self.length_measured_m,
            'installed_route_ref': self.installed_route_ref,
            'supersedes_path_id': self.supersedes_path_id,
            'supersedes_path_sha256': self.supersedes_path_sha256,
            'service_action': self.service_action,
            'attachment_refs': list(self.attachment_refs),
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
            'created_at_utc': self.created_at_utc,
        }

    def labels(self) -> tuple[str, ...]:
        """All declared labels along the path (ends + intermediates)."""
        entries: list[str] = []
        if self.label_end_a is not None:
            entries.append(self.label_end_a)
        entries.extend(self.intermediate_labels)
        if self.label_end_b is not None:
            entries.append(self.label_end_b)
        return tuple(entries)

    def endpoint_identities(self) -> tuple[str, str]:
        return (
            self.from_termination.identity_key(),
            self.to_termination.identity_key(),
        )


def build_physical_interconnect(
    *,
    path_id: str,
    version: str,
    document_id: str,
    path_class: PhysicalPathClass,
    evidence_state: PathEvidenceState,
    from_termination: CableTermination,
    to_termination: CableTermination,
    created_at_utc: str,
    observation_state: ObservationState = 'documented_not_observed',
    hops: Sequence[InterconnectHop] = (),
    label_end_a: str | None = None,
    label_end_b: str | None = None,
    intermediate_labels: Sequence[str] = (),
    label_profile_standard_ref: str | None = None,
    cable_manufacturer: str | None = None,
    cable_model: str | None = None,
    cable_category: str | None = None,
    conductor_count: int | None = None,
    conductor_gauge: str | None = None,
    length_designed_m: float | None = None,
    length_estimated_m: float | None = None,
    length_measured_m: float | None = None,
    installed_route_ref: str | None = None,
    scene_revision_id: str | None = None,
    scene_content_hash: str | None = None,
    supersedes_path_id: str | None = None,
    supersedes_path_sha256: str | None = None,
    service_action: str | None = None,
    attachment_refs: Sequence[str] = (),
    provenance: Sequence[EquipmentDataProvenance] = (),
) -> PhysicalInterconnect:
    """Assemble a sealed physical interconnect declaration."""
    payload: dict[str, Any] = {
        'schema_version': PHYSICAL_INTERCONNECT_SCHEMA_VERSION,
        'authority_version': PHYSICAL_INTERCONNECT_AUTHORITY_VERSION,
        'path_id': path_id,
        'version': version,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'path_class': path_class,
        'evidence_state': evidence_state,
        'observation_state': observation_state,
        'from_termination': from_termination.model_dump(mode='json'),
        'to_termination': to_termination.model_dump(mode='json'),
        'hops': [hop.model_dump(mode='json') for hop in hops],
        'label_end_a': label_end_a,
        'label_end_b': label_end_b,
        'intermediate_labels': list(intermediate_labels),
        'label_profile_standard_ref': label_profile_standard_ref,
        'cable_manufacturer': cable_manufacturer,
        'cable_model': cable_model,
        'cable_category': cable_category,
        'conductor_count': conductor_count,
        'conductor_gauge': conductor_gauge,
        'length_designed_m': length_designed_m,
        'length_estimated_m': length_estimated_m,
        'length_measured_m': length_measured_m,
        'installed_route_ref': installed_route_ref,
        'supersedes_path_id': supersedes_path_id,
        'supersedes_path_sha256': supersedes_path_sha256,
        'service_action': service_action,
        'attachment_refs': list(attachment_refs),
        'provenance': [item.model_dump(mode='json') for item in provenance],
        'created_at_utc': created_at_utc,
    }
    return PhysicalInterconnect(
        path_id=path_id,
        version=version,
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        path_class=path_class,
        evidence_state=evidence_state,
        observation_state=observation_state,
        from_termination=from_termination,
        to_termination=to_termination,
        hops=tuple(hops),
        label_end_a=label_end_a,
        label_end_b=label_end_b,
        intermediate_labels=tuple(intermediate_labels),
        label_profile_standard_ref=label_profile_standard_ref,
        cable_manufacturer=cable_manufacturer,
        cable_model=cable_model,
        cable_category=cable_category,
        conductor_count=conductor_count,
        conductor_gauge=conductor_gauge,
        length_designed_m=length_designed_m,
        length_estimated_m=length_estimated_m,
        length_measured_m=length_measured_m,
        installed_route_ref=installed_route_ref,
        supersedes_path_id=supersedes_path_id,
        supersedes_path_sha256=supersedes_path_sha256,
        service_action=service_action,
        attachment_refs=tuple(attachment_refs),
        provenance=tuple(provenance),
        created_at_utc=created_at_utc,
        semantic_sha256=_digest(payload),
    )


def record_service_change(
    previous: PhysicalInterconnect,
    *,
    version: str,
    created_at_utc: str,
    service_action: str,
    evidence_state: PathEvidenceState | None = None,
    observation_state: ObservationState | None = None,
    from_termination: CableTermination | None = None,
    to_termination: CableTermination | None = None,
    hops: Sequence[InterconnectHop] | None = None,
    label_end_a: str | None = None,
    label_end_b: str | None = None,
    intermediate_labels: Sequence[str] | None = None,
    cable_manufacturer: str | None = None,
    cable_model: str | None = None,
    cable_category: str | None = None,
    conductor_count: int | None = None,
    conductor_gauge: str | None = None,
    length_designed_m: float | None = None,
    length_estimated_m: float | None = None,
    length_measured_m: float | None = None,
    installed_route_ref: str | None = None,
    attachment_refs: Sequence[str] | None = None,
    provenance: Sequence[EquipmentDataProvenance] = (),
) -> PhysicalInterconnect:
    """Record a re-termination / replacement as a new sealed revision.

    The previous physical path is preserved; the new record supersedes it
    so bindings against the old hash become ``stale`` honestly (#597 §15).
    """
    path = build_physical_interconnect(
        path_id=previous.path_id,
        version=version,
        document_id=previous.document_id,
        path_class=previous.path_class,
        evidence_state=evidence_state or previous.evidence_state,
        from_termination=from_termination or previous.from_termination,
        to_termination=to_termination or previous.to_termination,
        created_at_utc=created_at_utc,
        observation_state=(
            observation_state
            if observation_state is not None
            else previous.observation_state
        ),
        hops=previous.hops if hops is None else hops,
        label_end_a=(
            previous.label_end_a if label_end_a is None else label_end_a
        ),
        label_end_b=(
            previous.label_end_b if label_end_b is None else label_end_b
        ),
        intermediate_labels=(
            previous.intermediate_labels
            if intermediate_labels is None
            else intermediate_labels
        ),
        label_profile_standard_ref=previous.label_profile_standard_ref,
        cable_manufacturer=(
            previous.cable_manufacturer
            if cable_manufacturer is None
            else cable_manufacturer
        ),
        cable_model=(
            previous.cable_model if cable_model is None else cable_model
        ),
        cable_category=(
            previous.cable_category
            if cable_category is None
            else cable_category
        ),
        conductor_count=(
            previous.conductor_count
            if conductor_count is None
            else conductor_count
        ),
        conductor_gauge=(
            previous.conductor_gauge
            if conductor_gauge is None
            else conductor_gauge
        ),
        length_designed_m=(
            previous.length_designed_m
            if length_designed_m is None
            else length_designed_m
        ),
        length_estimated_m=(
            previous.length_estimated_m
            if length_estimated_m is None
            else length_estimated_m
        ),
        length_measured_m=(
            previous.length_measured_m
            if length_measured_m is None
            else length_measured_m
        ),
        installed_route_ref=(
            previous.installed_route_ref
            if installed_route_ref is None
            else installed_route_ref
        ),
        scene_revision_id=previous.scene_revision_id,
        scene_content_hash=previous.scene_content_hash,
        supersedes_path_id=previous.path_id,
        supersedes_path_sha256=previous.semantic_sha256,
        service_action=service_action,
        attachment_refs=(
            previous.attachment_refs
            if attachment_refs is None
            else attachment_refs
        ),
        provenance=tuple(provenance) or previous.provenance,
    )
    # Bookkeeping fields (version, timestamps, supersede links, action) do
    # not make a *physical* change: a revision identical in every physical
    # field is a no-op and must not be recorded — a no-change inspection
    # belongs in a WiringVerificationRecord instead.
    bookkeeping = {
        'version',
        'created_at_utc',
        'supersedes_path_id',
        'supersedes_path_sha256',
        'service_action',
    }
    new_payload = {
        key: value
        for key, value in path.semantic_payload().items()
        if key not in bookkeeping
    }
    old_payload = {
        key: value
        for key, value in previous.semantic_payload().items()
        if key not in bookkeeping
    }
    if new_payload == old_payload:
        raise ValueError(
            'service change produced an identical path — record an actual '
            'physical change or a verification record instead'
        )
    return path


class WiringVerificationRecord(BaseModel):
    """One domain-scoped physical verification against a sealed path.

    ``test_kind`` is checked against the path class's applicable set —
    a speaker-polarity test on a network run is rejected, never silently
    stored (#597 §14). ``domain_evidence_ref`` points at the domain
    qualification (HDMI qualification #583, network #591, wiring check
    #645, electrical #593) instead of duplicating it.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PHYSICAL_INTERCONNECT_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-wiring-verification-1'
    ] = WIRING_VERIFICATION_AUTHORITY_VERSION
    verification_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    path_class: PhysicalPathClass
    test_kind: WiringTestKind
    result: WiringTestResult
    measured_quantity: str | None = Field(default=None, min_length=1)
    measured_value: float | None = None
    measured_unit: str | None = Field(default=None, min_length=1)
    domain_evidence_ref: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)
    evidence_refs: tuple[str, ...] = ()
    provenance: EquipmentDataProvenance
    verified_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('measured_value')
    @classmethod
    def finite_measured(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='measured value')

    @model_validator(mode='after')
    def valid_verification(self) -> 'WiringVerificationRecord':
        if self.test_kind not in _APPLICABLE_TESTS[self.path_class]:
            raise ValueError(
                f'test {self.test_kind!r} is not applicable to path class '
                f'{self.path_class!r}'
            )
        if (self.measured_quantity is None) != (self.measured_value is None):
            raise ValueError(
                'a measured quantity requires its value together'
            )
        if self.measured_value is not None and self.measured_unit is None:
            raise ValueError('a measured value requires a unit')
        digest = _digest(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('WiringVerificationRecord hash mismatch')
        if self.verification_id != _semantic_id('wire-ver', digest):
            raise ValueError('WiringVerificationRecord id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'path_id': self.path_id,
            'path_sha256': self.path_sha256,
            'path_class': self.path_class,
            'test_kind': self.test_kind,
            'result': self.result,
            'measured_quantity': self.measured_quantity,
            'measured_value': self.measured_value,
            'measured_unit': self.measured_unit,
            'domain_evidence_ref': self.domain_evidence_ref,
            'note': self.note,
            'evidence_refs': list(self.evidence_refs),
            'provenance': self.provenance.model_dump(mode='json'),
            'verified_at_utc': self.verified_at_utc,
        }


def build_wiring_verification(
    *,
    path: PhysicalInterconnect,
    test_kind: WiringTestKind,
    result: WiringTestResult,
    provenance: EquipmentDataProvenance,
    verified_at_utc: str,
    measured_quantity: str | None = None,
    measured_value: float | None = None,
    measured_unit: str | None = None,
    domain_evidence_ref: str | None = None,
    note: str | None = None,
    evidence_refs: Sequence[str] = (),
) -> WiringVerificationRecord:
    """Assemble a sealed verification record bound to *path*'s hash."""
    payload: dict[str, Any] = {
        'schema_version': PHYSICAL_INTERCONNECT_SCHEMA_VERSION,
        'authority_version': WIRING_VERIFICATION_AUTHORITY_VERSION,
        'path_id': path.path_id,
        'path_sha256': path.semantic_sha256,
        'path_class': path.path_class,
        'test_kind': test_kind,
        'result': result,
        'measured_quantity': measured_quantity,
        'measured_value': measured_value,
        'measured_unit': measured_unit,
        'domain_evidence_ref': domain_evidence_ref,
        'note': note,
        'evidence_refs': list(evidence_refs),
        'provenance': provenance.model_dump(mode='json'),
        'verified_at_utc': verified_at_utc,
    }
    digest = _digest(payload)
    return WiringVerificationRecord(
        verification_id=_semantic_id('wire-ver', digest),
        path_id=path.path_id,
        path_sha256=path.semantic_sha256,
        path_class=path.path_class,
        test_kind=test_kind,
        result=result,
        measured_quantity=measured_quantity,
        measured_value=measured_value,
        measured_unit=measured_unit,
        domain_evidence_ref=domain_evidence_ref,
        note=note,
        evidence_refs=tuple(evidence_refs),
        provenance=provenance,
        verified_at_utc=verified_at_utc,
        semantic_sha256=digest,
    )


class PathStateAssessment(BaseModel):
    """Evaluated (not asserted) state of one physical path.

    ``evaluated_state`` can only reach ``verified_path`` through a passing
    domain-appropriate verification; ``promotion_gap`` explains a declared
    state the evidence does not support (#597 §9).
    """

    model_config = ConfigDict(frozen=True)

    path_id: str = Field(min_length=1)
    path_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    declared_state: PathEvidenceState
    evaluated_state: Literal[
        'designed_path',
        'installed_reported_path',
        'field_observed_path',
        'verified_path',
        'unverified_declaration',
        'failed_path',
    ]
    applicable_test_kinds: tuple[str, ...]
    passing_tests: tuple[WiringTestKind, ...]
    failing_tests: tuple[WiringTestKind, ...]
    indeterminate_tests: tuple[WiringTestKind, ...]
    inapplicable_tests: tuple[WiringTestKind, ...]
    promotion_gap: str | None = None


def evaluate_physical_path_state(
    path: PhysicalInterconnect,
    verifications: Sequence[WiringVerificationRecord] = (),
) -> PathStateAssessment:
    """Evaluate the honest state of a path under verification evidence.

    A declared ``verified_path`` without a passing applicable test is
    reported via ``promotion_gap`` — declaration alone cannot promote a
    path. A failing applicable test evaluates to ``failed_path``.
    """
    applicable = _APPLICABLE_TESTS[path.path_class]
    current = [
        record
        for record in verifications
        if record.path_sha256 == path.semantic_sha256
        and record.path_id == path.path_id
    ]
    passing = tuple(
        sorted({r.test_kind for r in current if r.result == 'pass'})
    )
    failing = tuple(
        sorted({r.test_kind for r in current if r.result == 'fail'})
    )
    indeterminate = tuple(
        sorted(
            {
                r.test_kind
                for r in current
                if r.result == 'indeterminate'
                and r.test_kind not in passing
                and r.test_kind not in failing
            }
        )
    )
    inapplicable = tuple(
        sorted(
            {
                r.test_kind
                for r in current
                if r.test_kind not in applicable
            }
        )
    )
    if failing:
        evaluated: Literal[
            'designed_path',
            'installed_reported_path',
            'field_observed_path',
            'verified_path',
            'unverified_declaration',
            'failed_path',
        ] = 'failed_path'
    elif passing:
        evaluated = 'verified_path'
    elif path.evidence_state == 'verified_path':
        # A self-declared verified path without a passing applicable test
        # is an unproven declaration — it is not silently echoed.
        evaluated = 'unverified_declaration'
    else:
        evaluated = path.evidence_state

    promotion_gap: str | None = None
    if path.evidence_state == 'verified_path' and evaluated != 'verified_path':
        promotion_gap = (
            'declared verified_path without a passing applicable '
            'verification — declaration cannot promote a path'
        )
    elif evaluated == 'verified_path' and path.evidence_state != 'verified_path':
        promotion_gap = (
            f'verification evidence promotes declared '
            f'{path.evidence_state!r} to verified_path — the stored '
            'declaration stays unchanged'
        )
    return PathStateAssessment(
        path_id=path.path_id,
        path_sha256=path.semantic_sha256,
        declared_state=path.evidence_state,
        evaluated_state=evaluated,
        applicable_test_kinds=tuple(sorted(applicable)),
        passing_tests=passing,
        failing_tests=failing,
        indeterminate_tests=indeterminate,
        inapplicable_tests=inapplicable,
        promotion_gap=promotion_gap,
    )


class LogicalPhysicalBinding(BaseModel):
    """Binding of a logical signal route to a sealed physical path.

    ``logical_from_identity`` / ``logical_to_identity`` carry the logical
    endpoints' declared identities (``device|port`` canonical keys) so the
    evaluator can detect a mismatch instead of trusting the association.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PHYSICAL_INTERCONNECT_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-logical-physical-binding-1'
    ] = LOGICAL_PHYSICAL_BINDING_AUTHORITY_VERSION
    binding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    logical_ref_kind: str = Field(min_length=1)
    logical_ref_id: str = Field(min_length=1)
    logical_from_identity: str | None = Field(default=None, min_length=1)
    logical_to_identity: str | None = Field(default=None, min_length=1)
    path_id: str = Field(min_length=1)
    path_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    recorded_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_binding(self) -> 'LogicalPhysicalBinding':
        digest = _digest(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('LogicalPhysicalBinding hash mismatch')
        if self.binding_id != _semantic_id('wire-bind', digest):
            raise ValueError('LogicalPhysicalBinding id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'logical_ref_kind': self.logical_ref_kind,
            'logical_ref_id': self.logical_ref_id,
            'logical_from_identity': self.logical_from_identity,
            'logical_to_identity': self.logical_to_identity,
            'path_id': self.path_id,
            'path_sha256': self.path_sha256,
            'recorded_at_utc': self.recorded_at_utc,
        }


def build_logical_physical_binding(
    *,
    document_id: str,
    logical_ref_kind: str,
    logical_ref_id: str,
    path: PhysicalInterconnect,
    recorded_at_utc: str,
    logical_from_identity: str | None = None,
    logical_to_identity: str | None = None,
) -> LogicalPhysicalBinding:
    """Record the association of a logical route to a sealed path."""
    payload: dict[str, Any] = {
        'schema_version': PHYSICAL_INTERCONNECT_SCHEMA_VERSION,
        'authority_version': LOGICAL_PHYSICAL_BINDING_AUTHORITY_VERSION,
        'document_id': document_id,
        'logical_ref_kind': logical_ref_kind,
        'logical_ref_id': logical_ref_id,
        'logical_from_identity': logical_from_identity,
        'logical_to_identity': logical_to_identity,
        'path_id': path.path_id,
        'path_sha256': path.semantic_sha256,
        'recorded_at_utc': recorded_at_utc,
    }
    digest = _digest(payload)
    return LogicalPhysicalBinding(
        binding_id=_semantic_id('wire-bind', digest),
        document_id=document_id,
        logical_ref_kind=logical_ref_kind,
        logical_ref_id=logical_ref_id,
        logical_from_identity=logical_from_identity,
        logical_to_identity=logical_to_identity,
        path_id=path.path_id,
        path_sha256=path.semantic_sha256,
        recorded_at_utc=recorded_at_utc,
        semantic_sha256=digest,
    )


def _norm_identity(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower()


def evaluate_logical_physical_binding(
    binding: LogicalPhysicalBinding,
    *,
    path: PhysicalInterconnect | None,
    verifications: Sequence[WiringVerificationRecord] = (),
) -> LogicalBindingState:
    """Evaluate one logical→physical binding honestly.

    - no resolved path → ``unverified_routing``
    - path hash drift (service change) → ``stale``
    - endpoint identity disagreement → ``mismatched``
    - otherwise maps the evaluated path state to the binding strength.
    """
    if path is None or path.path_id != binding.path_id:
        return 'unverified_routing'
    if path.semantic_sha256 != binding.path_sha256:
        return 'stale'
    assessment = evaluate_physical_path_state(path, verifications)
    if assessment.evaluated_state == 'failed_path':
        return 'failed_path'
    if assessment.evaluated_state == 'unverified_declaration':
        return 'unverified_declaration'
    bound_from, bound_to = path.endpoint_identities()
    logical_from = _norm_identity(binding.logical_from_identity)
    logical_to = _norm_identity(binding.logical_to_identity)
    if logical_from is not None and bound_from != logical_from:
        return 'mismatched'
    if logical_to is not None and bound_to != logical_to:
        return 'mismatched'
    if assessment.evaluated_state == 'verified_path':
        return 'verified_binding'
    if assessment.evaluated_state == 'field_observed_path':
        return 'observed_binding'
    if assessment.evaluated_state == 'installed_reported_path':
        return 'reported_binding'
    return 'designed_binding'


class CableScheduleEntry(BaseModel):
    """One row of the derived cable schedule — a view, never a truth store."""

    model_config = ConfigDict(frozen=True)

    path_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    path_class: PhysicalPathClass
    from_endpoint: str
    to_endpoint: str
    through: tuple[str, ...]
    cable_type: str
    length_m: float | None
    length_semantics: Literal[
        'designed', 'estimated', 'measured', 'unknown'
    ]
    labels: tuple[str, ...]
    evaluated_state: str
    logical_refs: tuple[str, ...]
    last_changed_utc: str


class CableSchedule(BaseModel):
    """Derived cable schedule: a view of the physical-interconnect store.

    The schedule is *derived* — it can always be regenerated from the
    sealed paths and is never edited directly (#597 §16).
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'rev56-cable-schedule-1'
    ] = CABLE_SCHEDULE_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    entries: tuple[CableScheduleEntry, ...]
    unbound_logical_refs: tuple[str, ...] = ()
    generated_at_utc: str = Field(min_length=1)
    schedule_id: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_schedule(self) -> 'CableSchedule':
        digest = _digest(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('CableSchedule hash mismatch')
        if self.schedule_id != _semantic_id('cable-schedule', digest):
            raise ValueError('CableSchedule id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'entries': [
                entry.model_dump(mode='json') for entry in self.entries
            ],
            'unbound_logical_refs': list(self.unbound_logical_refs),
            'generated_at_utc': self.generated_at_utc,
        }


def _endpoint_text(termination: CableTermination) -> str:
    anchor = (
        termination.device_instance_ref
        or termination.entity_id
        or termination.patch_panel_ref
        or termination.wall_plate_ref
        or termination.terminal_ref
        or termination.observed_label
        or 'unknown'
    )
    port = termination.port_identity or termination.patch_position
    return f'{anchor}:{port}' if port else anchor


def derive_cable_schedule(
    *,
    document_id: str,
    paths: Sequence[PhysicalInterconnect],
    generated_at_utc: str,
    verifications_by_path: dict[str, Sequence[WiringVerificationRecord]]
    | None = None,
    bindings: Sequence[LogicalPhysicalBinding] = (),
) -> CableSchedule:
    """Derive a cable schedule from sealed paths (not a parallel store).

    ``unbound_logical_refs`` lists logical refs whose evaluated binding is
    ``unverified_routing`` — designed routes with no physical evidence.
    """
    verifications_by_path = verifications_by_path or {}
    bindings_by_path: dict[str, list[str]] = {}
    unbound: list[str] = []
    path_by_id = {path.path_id: path for path in paths}
    for binding in bindings:
        state = evaluate_logical_physical_binding(
            binding,
            path=path_by_id.get(binding.path_id),
            verifications=verifications_by_path.get(binding.path_id, ()),
        )
        if state == 'unverified_routing':
            unbound.append(binding.logical_ref_id)
        elif state in (
            'verified_binding', 'observed_binding', 'reported_binding',
            'designed_binding', 'unverified_declaration', 'mismatched',
        ):
            bindings_by_path.setdefault(binding.path_id, []).append(
                binding.logical_ref_id
            )
    entries: list[CableScheduleEntry] = []
    for path in sorted(paths, key=lambda item: (item.path_id, item.version)):
        assessment = evaluate_physical_path_state(
            path, verifications_by_path.get(path.path_id, ())
        )
        if path.length_measured_m is not None:
            length_m: float | None = float(path.length_measured_m)
            semantics: Literal[
                'designed', 'estimated', 'measured', 'unknown'
            ] = 'measured'
        elif path.length_estimated_m is not None:
            length_m = float(path.length_estimated_m)
            semantics = 'estimated'
        elif path.length_designed_m is not None:
            length_m = float(path.length_designed_m)
            semantics = 'designed'
        else:
            length_m = None
            semantics = 'unknown'
        cable_parts = [
            part
            for part in (
                path.cable_manufacturer,
                path.cable_model,
                path.cable_category,
                path.conductor_gauge,
            )
            if part
        ]
        entries.append(
            CableScheduleEntry(
                path_id=path.path_id,
                version=path.version,
                path_class=path.path_class,
                from_endpoint=_endpoint_text(path.from_termination),
                to_endpoint=_endpoint_text(path.to_termination),
                through=tuple(
                    hop.label or hop.ref_id or hop.kind
                    for hop in path.hops
                    if hop.kind
                    in (
                        'patch_panel',
                        'wall_plate',
                        'terminal_block',
                        'permanent_run',
                        'adapter_extender',
                    )
                ),
                cable_type=' '.join(cable_parts) or 'undeclared',
                length_m=length_m,
                length_semantics=semantics,
                labels=path.labels(),
                evaluated_state=assessment.evaluated_state,
                logical_refs=tuple(
                    sorted(bindings_by_path.get(path.path_id, []))
                ),
                last_changed_utc=path.created_at_utc,
            )
        )
    payload = {
        'authority_version': CABLE_SCHEDULE_AUTHORITY_VERSION,
        'document_id': document_id,
        'entries': [entry.model_dump(mode='json') for entry in entries],
        'unbound_logical_refs': sorted(set(unbound)),
        'generated_at_utc': generated_at_utc,
    }
    digest = _digest(payload)
    return CableSchedule(
        document_id=document_id,
        entries=tuple(entries),
        unbound_logical_refs=tuple(sorted(set(unbound))),
        generated_at_utc=generated_at_utc,
        schedule_id=_semantic_id('cable-schedule', digest),
        semantic_sha256=digest,
    )


def candidate_fault_segments(
    path: PhysicalInterconnect,
    *,
    upstream_verified_sequence: int | None = None,
) -> tuple[InterconnectHop, ...]:
    """Diagnostic narrowing: hops downstream of a known-good point.

    Not a fault assertion — the segment list a field check should probe,
    in path order. ``upstream_verified_sequence`` is the sequence of the
    last verified hop; ``None`` returns the whole path (#597 §15).
    """
    if upstream_verified_sequence is None:
        return path.hops
    return tuple(
        hop for hop in path.hops if hop.sequence > upstream_verified_sequence
    )


__all__ = [
    'CABLE_SCHEDULE_AUTHORITY_VERSION',
    'LOGICAL_PHYSICAL_BINDING_AUTHORITY_VERSION',
    'PHYSICAL_INTERCONNECT_AUTHORITY_VERSION',
    'PHYSICAL_INTERCONNECT_SCHEMA_VERSION',
    'WIRING_VERIFICATION_AUTHORITY_VERSION',
    'CableSchedule',
    'CableScheduleEntry',
    'CableTermination',
    'InterconnectHop',
    'InterconnectHopKind',
    'LogicalBindingState',
    'LogicalPhysicalBinding',
    'ObservationState',
    'PathEvidenceState',
    'PathStateAssessment',
    'PhysicalInterconnect',
    'PhysicalPathClass',
    'TerminationPolarity',
    'TerminationRole',
    'WiringTestKind',
    'WiringTestResult',
    'WiringVerificationRecord',
    'applicable_test_kinds',
    'build_logical_physical_binding',
    'build_physical_interconnect',
    'build_wiring_verification',
    'candidate_fault_segments',
    'derive_cable_schedule',
    'evaluate_logical_physical_binding',
    'evaluate_physical_path_state',
    'record_service_change',
]
