"""AV mounting / structural-support evidence authority (#620, REV57-MOUNT).

A CAD placement proves geometry, never constructability: an object that
fits in the model can still hang from a ceiling grid that cannot carry
it, anchor into a wall whose blocking nobody verified, or sit under a
suspension assembly the manufacturer never rated for overhead use. This
module is the fail-closed evidence layer between *placement* and
*support* — it organizes the requirements, approvals and inspection
records and refuses to claim structural approval HTDT cannot see.

Scope discipline (safety-critical):

- HTDT records *who approved what, on which evidence* — it never
  performs structural design, never invents a safe anchor count, never
  issues an engineering seal. When the evidence is incomplete the
  verdict is ``structural_approval_required``, not an estimate.
- Manufacturer enclosure/mount capability and building-support capacity
  are separate claims: an E1.8-rated enclosure is not a building
  approval, and manufacturer hardware approval never proves the
  attachment is adequate (#620 §6/§7).
- BIM/scan geometry is location evidence only — IFC element types and
  point-cloud appearances never manufacture hidden blocking, rebar or
  anchor condition (#620 §14, MNT50).
- One evidence class never silently upgrades another: a CAD note saying
  ``blocking provided`` is not engineering approval (#620 §4).
- Demand evidence (mass / CG / attachment loads / duty) is retained
  verbatim for the qualified designer — HTDT does not derive building
  capacity from it (#620 §2).
- Substitutions and relocations stale affected approvals through the
  evaluator's change axes — a substitute with similar acoustic
  performance is not structurally equivalent by default (#620 §13).

Records:

- :class:`CadMountingAssembly` — the complete proposed/installed
  assembly: equipment, manufacturer interface, bracket/frame, fasteners,
  secondary retention, support point(s), orientation and declared pose.
- :class:`CadMountLoadEvidence` — sealed demand evidence (mass, center
  of gravity, duty state, manufacturer attachment loads) with its
  source class.
- :class:`CadSupportElementRecord` — the element carrying the load
  (structural steel / concrete / timber / blocking / ceiling deck vs
  suspended grid / wall framing / baffle-wall structure / rigging point /
  manufacturer stand / unknown) plus declared-capacity evidence and
  hidden-condition honesty.
- :class:`CadManufacturerMountingRequirement` — the manufacturer's exact
  mounting instructions: approved points, orientation restrictions,
  fastener/retention requirements, prohibited configurations, enclosure
  suspension rating (E1.8 class) and VESA interface declaration.
- :class:`CadStructuralApprovalRecord` — one approval artifact with its
  evidence class (engineer design / qualified professional / permit /
  rigging-standard profile / installer declaration / field inspection /
  proof-test / assumed), scope, duty coverage, jurisdiction/code edition
  and the external document reference — sensitive engineering documents
  stay external.
- :class:`CadMountingInspectionRecord` — installation/periodic
  inspection as-built: component and support-point observations,
  secondary-retention observation, pose reconciliation, inaccessible
  points, findings and next-due — hidden conditions are never inferred.
- :class:`CadMountingQualification` +
  :func:`evaluate_mounting_support` — the fail-closed verdict on the
  #620 §5 ladder, including the suspended-loudspeaker three-layer check
  (enclosure capability / building support point / field rigging
  assembly).

Literature basis
----------------
- ANSI E1.56-2026 (ESTA) — permanent rigging support points attached to
  permanent facility structure: design, fabrication, installation,
  inspection and documentation requirements.
- ANSI E1.8-2018 (R2023) — structural characteristics and testing of
  loudspeaker enclosures intended for overhead suspension; covers the
  product, not the building.
- ANSI E1.47-2020 — entertainment-rigging inspection guidance (2026
  revision in public review — kept as a distinct #599 profile).
- AVIXA AV/IT higher-education guidelines (2021) — mounting equipment
  that could injure someone if it falls warrants structural-engineering
  review.
- Local building/structural codes and manufacturer instructions remain
  authoritative for the actual project; this authority references them,
  never replaces them.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


MOUNTING_SCHEMA_VERSION = 'mount-mounting-support-1'
MOUNTING_EVALUATION_VERSION = 'mount-mounting-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


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
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


# ---------------------------------------------------------------------------
# Taxonomies (#620)
# ---------------------------------------------------------------------------

MountEquipmentClass = Literal[
    'loudspeaker',
    'subwoofer',
    'projector',
    'display',
    'screen_or_masking',
    'rack_or_cabinet',
    'tactile_platform',
    'other',
    'unknown',
]
"""#620 — the equipment family the assembly carries. Tactile/motion
platforms stay explicit where relevant."""

MountSupportMethod = Literal[
    'wall',
    'ceiling',
    'floor',
    'rack',
    'pole',
    'suspended_rigging',
    'manufacturer_stand',
    'custom',
    'unknown',
]
"""How the assembly claims to be held. ``unknown`` is an honest state —
CAD presence alone never upgrades it."""

MountDutyState = Literal[
    'static',
    'moving_motorized',
    'moving_manual',
    'unknown',
]
"""#620 §10 — motorized screens, lifts and other moving assemblies are a
different duty class: a static-only approval never qualifies a moving
system."""

MountComponentRole = Literal[
    'equipment_interface',
    'bracket_or_frame',
    'fastener_hardware',
    'safety_retention',
    'support_point',
    'blocking_or_subframe',
    'pole_or_rig',
    'isolation_element',
    'other',
]
"""Assembly component roles (#620 §1). ``isolation_element`` marks
vibration-isolation hardware (#229/#589 composition — the structural and
vibration evidence stay independent)."""

ComponentIdentityState = Literal[
    'declared',
    'verified_observed',
    'unknown',
]
"""Whether the component identity was observed in the field or only
declared — a declared bracket is not a verified bracket."""

SecondaryRetentionState = Literal[
    'required_installed',
    'required_missing',
    'not_required',
    'unknown',
]
"""Declared secondary-retention (safety cable / backup) state against
the manufacturer requirement — ``required_missing`` is an installation
defect, not a stylistic choice."""

MountInterferenceState = Literal[
    'clear',
    'conflict_observed',
    'unresolved',
    'unknown',
]
"""Declared mount-vs-structure/cable interference observation. HTDT does
not run a geometry conflict engine — the state records what a survey or
review established, with #597 cable-route pins where relevant."""

SupportElementClass = Literal[
    'structural_steel',
    'concrete',
    'timber',
    'blocking_subframe',
    'ceiling_deck',
    'suspended_ceiling_grid',
    'wall_framing',
    'baffle_wall_structure',
    'rigging_support_point',
    'manufacturer_stand_base',
    'other',
    'unknown',
]
"""#620 §3 — the element that actually carries the load. A suspended
ceiling grid is named separately from the deck above it precisely
because it is not a structural support point."""

HiddenConditionState = Literal[
    'verified',
    'inaccessible',
    'unknown',
]
"""Hidden anchor / embedment / blocking condition honesty (#620 §11) —
photographs never verify what they cannot see."""

EvidenceSourceClass = Literal[
    'manufacturer_published',
    'approved_document',
    'field_measured',
    'installer_declared',
    'assumed',
    'unknown',
]
"""Where a declared value came from — an assumed rating is not a
manufacturer rating."""

ApprovalEvidenceClass = Literal[
    'manufacturer_installation_requirement',
    'structural_engineer_design',
    'qualified_professional_record',
    'local_code_permit_record',
    'rigging_standard_profile',
    'installer_declaration',
    'field_inspection',
    'load_proof_test',
    'user_assumed',
    'unknown',
]
"""#620 §4 — approval evidence classes. A rigging-standard profile is a
reference to how work should be done, not a certification of this
installation; an installer declaration is not a professional seal."""

ApprovalScope = Literal[
    'assembly',
    'support_element',
    'rigging_system',
    'project',
    'unknown',
]
"""What the approval actually covers — a project-level permit is not an
assembly sign-off."""

ApprovalDutyCoverage = Literal[
    'static_only',
    'static_and_dynamic',
    'moving_system',
    'unknown',
]
"""Which duty states the approval covers (#620 §10)."""

EnclosureSuspensionRating = Literal[
    'e1_8_rated',
    'manufacturer_rated',
    'not_rated_for_suspension',
    'not_applicable',
    'unknown',
]
"""#620 §7/§17 — enclosure suspension capability. E1.8-class evidence is
only claimable where the product is designed/represented for overhead
suspension; ``not_rated_for_suspension`` on an overhead mount is a hard
incompatibility, not a limitation."""

InspectionKind = Literal[
    'installation',
    'periodic',
    'post_event',
    'commissioning',
]
"""#620 §11/§12 — inspection record kinds. Periodic and post-event
inspections exist only where a profile/manufacturer requires them;
HTDT invents no universal interval."""

InspectorClass = Literal[
    'installer',
    'qualified_inspector',
    'engineer',
    'other',
    'unknown',
]

ObservationState = Literal[
    'verified',
    'mismatch',
    'absent',
    'not_visible',
    'unknown',
]
"""As-built observation honesty — ``not_visible`` is an explicit
limitation, never a silent pass."""

InspectionFindings = Literal[
    'pass',
    'pass_with_notes',
    'findings_open',
    'failed',
    'inconclusive',
]

MountSupportState = Literal[
    'design_support_evidence_complete',
    'approved_with_limitations',
    'installation_inspection_required',
    'structural_approval_required',
    'manufacturer_mounting_incompatible',
    'support_capacity_insufficient',
    'support_unknown',
    'as_built_mismatch',
    'stale_after_change',
]
"""#620 §5 eligibility ladder. ``support_capacity_insufficient`` covers
the declared-rating-below-declared-demand case; ``stale_after_change``
is the #596 invalidation path. There is no ``safe`` verdict — the best
state only says the *evidence record* is complete."""

SuspensionLayerState = Literal[
    'capable',
    'incompatible',
    'unknown',
    'not_applicable',
]
"""Per-layer state for the suspended-loudspeaker triple (#620 §7)."""

SubstitutionChangeAxis = Literal[
    'equipment_mass_or_cg',
    'mount_or_bracket',
    'support_point',
    'orientation',
    'duty_state',
    'equipment_identity',
]
"""#620 §13 — which change axes invalidate structural evidence. Fed by
#596 substitution outcomes and relocation/service events."""


# ---------------------------------------------------------------------------
# Embedded models
# ---------------------------------------------------------------------------


class CadMountingComponent(BaseModel):
    """One component of the mounting assembly (#620 §1).

    Every component keeps its manufacturer/model/revision where known;
    the identity state separates a declared part from one observed
    installed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    role: MountComponentRole
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    revision: str | None = Field(default=None, min_length=1)
    identity_state: ComponentIdentityState = 'unknown'
    vesa_pattern: str | None = Field(default=None, min_length=1)
    """VESA interface the component implements (e.g. 'MIS-D 100') —
    declared so a mount/equipment pattern mismatch is detectable."""
    source_ref: AuthorityRef | None = None
    """Catalog / document pin for the component identity."""
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'CadMountingComponent':
        if self.source_ref is not None and self.source_ref.ref_sha256 is None:
            raise ValueError('component source_ref must pin its sha256')
        if (
            self.identity_state == 'verified_observed'
            and self.manufacturer is None
            and self.model is None
            and self.source_ref is None
        ):
            raise ValueError(
                'a verified-observed component requires identity evidence '
                '— observed with nothing recorded is not verification'
            )
        return self


class CadMountPose(BaseModel):
    """Declared installed pose — location evidence, not approval."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    x_m: float
    y_m: float
    z_m: float
    orientation_note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'CadMountPose':
        for value, label in (
            (self.x_m, 'x_m'), (self.y_m, 'y_m'), (self.z_m, 'z_m')
        ):
            _require_finite(value, f'pose {label}')
        return self


class CadMountLoadEntry(BaseModel):
    """One named manufacturer attachment load (#620 §2)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'CadMountLoadEntry':
        _require_finite(self.value, 'attachment load value')
        return self


class CadDeclaredCapacity(BaseModel):
    """A *declared* support rating with its source (#620 §3).

    The rating is retained verbatim — HTDT never derives capacity from
    geometry or materials; an ``assumed`` rating stays visibly assumed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    value: float = Field(gt=0.0)
    unit: str = Field(min_length=1)
    source_class: EvidenceSourceClass = 'unknown'
    document_ref: str | None = Field(default=None, min_length=1)
    """External artifact pointer — ratings inside sealed engineering
    documents are referenced, not copied."""

    @model_validator(mode='after')
    def _check(self) -> 'CadDeclaredCapacity':
        _require_finite(self.value, 'declared capacity value')
        return self


class CadApproverIdentity(BaseModel):
    """Who/what authority approved — a name without a credential class is
    honest but weak."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str | None = Field(default=None, min_length=1)
    credential: str | None = Field(default=None, min_length=1)
    """License / registration class (e.g. 'PE', 'SE', 'ETCP rigging')."""
    organization: str | None = Field(default=None, min_length=1)


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class CadMountingAssembly(BaseModel):
    """The complete equipment→mount→hardware→support assembly (#620 §1).

    This record is *identity*: what exactly is proposed or installed and
    how it claims to be held. It never asserts the support is adequate —
    that verdict belongs to :class:`CadMountingQualification` only.
    """

    model_config = ConfigDict(frozen=True)

    assembly_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    equipment_ref: AuthorityRef | None = None
    """The #569 installed instance (or other exact equipment authority)
    the assembly carries. Unbound assemblies (CAD-only intent) stay
    honest but can never reach the top verdict."""
    placement_ref: AuthorityRef | None = None
    """The CAD placement / scene entity this assembly claims to mount —
    the link that stops placement from silently implying support."""
    equipment_class: MountEquipmentClass = 'unknown'
    support_method: MountSupportMethod = 'unknown'
    overhead_suspension: bool = False
    """The assembly hangs above occupied space — the threshold AVIXA/ESTA
    practice treats as requiring professional structural review."""
    components: tuple[CadMountingComponent, ...] = ()
    secondary_retention_state: SecondaryRetentionState = 'unknown'
    duty_state: MountDutyState = 'unknown'
    isolation_mount: bool = False
    """Vibration-isolation mount (#620 §9) — composes the structural
    requirement with an independent vibration-performance one."""
    declared_configuration_ids: tuple[str, ...] = ()
    """Opaque configuration identifiers the project declares for this
    assembly — matched verbatim against manufacturer
    ``prohibited_configurations``; HTDT never infers a match."""
    interference_state: MountInterferenceState = 'unknown'
    cable_route_refs: tuple[AuthorityRef, ...] = ()
    """#597 cable-run pins whose routes pass through/attach to this
    assembly — routing + droop evidence stays on the cable authority."""
    installed_pose: CadMountPose | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=MOUNTING_SCHEMA_VERSION, min_length=1
    )
    assembly_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'equipment_ref': (
                self.equipment_ref.model_dump(mode='json')
                if self.equipment_ref is not None
                else None
            ),
            'placement_ref': (
                self.placement_ref.model_dump(mode='json')
                if self.placement_ref is not None
                else None
            ),
            'equipment_class': self.equipment_class,
            'support_method': self.support_method,
            'overhead_suspension': self.overhead_suspension,
            'components': [
                component.model_dump(mode='json')
                for component in self.components
            ],
            'secondary_retention_state': self.secondary_retention_state,
            'duty_state': self.duty_state,
            'isolation_mount': self.isolation_mount,
            'declared_configuration_ids': list(
                self.declared_configuration_ids
            ),
            'interference_state': self.interference_state,
            'cable_route_refs': [
                ref.model_dump(mode='json') for ref in self.cable_route_refs
            ],
            'installed_pose': (
                self.installed_pose.model_dump(mode='json')
                if self.installed_pose is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadMountingAssembly':
        _require_iso8601(self.declared_at_utc, 'assembly declared_at_utc')
        for ref, label in (
            (self.equipment_ref, 'equipment_ref'),
            (self.placement_ref, 'placement_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.equipment_ref is not None and (
            self.equipment_ref.kind != 'installed_instance'
        ):
            raise ValueError(
                "equipment_ref must pin an 'installed_instance' authority"
            )
        for ref in self.cable_route_refs:
            if ref.kind != 'cable_run':
                raise ValueError(
                    "cable_route_refs must pin 'cable_run' authorities"
                )
            if ref.ref_sha256 is None:
                raise ValueError('cable route refs must pin their sha256')
        if len(set(self.declared_configuration_ids)) != len(
            self.declared_configuration_ids
        ):
            raise ValueError('declared configuration ids must be unique')
        expected = _hash(self.identity_payload())
        if self.assembly_sha256 != expected:
            raise ValueError('mounting assembly hash mismatch')
        if self.assembly_id != _semantic_id('mntassy', expected):
            raise ValueError('mounting assembly id does not match its hash')
        return self


def mounting_assembly_binding(
    assembly: CadMountingAssembly,
) -> AuthorityRef:
    return AuthorityRef(
        kind='mount_assembly',
        ref_id=assembly.assembly_id,
        ref_sha256=assembly.assembly_sha256,
    )


class CadMountLoadEvidence(BaseModel):
    """Sealed demand evidence for one assembly (#620 §2).

    Mass / center of gravity / attachment loads / duty are retained
    verbatim with their source class — this is the *input* handed to the
    qualified designer, never a capacity derivation.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assembly_ref: AuthorityRef
    mass_kg: float | None = Field(default=None, gt=0.0)
    weight_n: float | None = Field(default=None, gt=0.0)
    center_of_gravity: CadMountPose | None = None
    """CG offset in the assembly frame — pose class reused as a point."""
    attachment_loads: tuple[CadMountLoadEntry, ...] = ()
    duty_state: MountDutyState = 'unknown'
    service_load_state: Literal[
        'not_applicable', 'declared', 'unknown'
    ] = 'unknown'
    source_class: EvidenceSourceClass = 'unknown'
    measured_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'assembly_ref': self.assembly_ref.model_dump(mode='json'),
            'mass_kg': self.mass_kg,
            'weight_n': self.weight_n,
            'center_of_gravity': (
                self.center_of_gravity.model_dump(mode='json')
                if self.center_of_gravity is not None
                else None
            ),
            'attachment_loads': [
                entry.model_dump(mode='json')
                for entry in self.attachment_loads
            ],
            'duty_state': self.duty_state,
            'service_load_state': self.service_load_state,
            'source_class': self.source_class,
            'measured_at_utc': self.measured_at_utc,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadMountLoadEvidence':
        _require_iso8601(self.declared_at_utc, 'load declared_at_utc')
        if self.measured_at_utc is not None:
            _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        if self.assembly_ref.kind != 'mount_assembly':
            raise ValueError(
                "assembly_ref must pin a 'mount_assembly' authority"
            )
        if self.assembly_ref.ref_sha256 is None:
            raise ValueError('assembly_ref must pin its sha256')
        for value, label in (
            (self.mass_kg, 'mass_kg'), (self.weight_n, 'weight_n')
        ):
            if value is not None:
                _require_finite(value, label)
        if self.mass_kg is None and self.weight_n is None and (
            not self.attachment_loads
        ):
            raise ValueError(
                'load evidence with no mass, weight or attachment loads '
                'records nothing — record the assembly without it instead'
            )
        names = [entry.name for entry in self.attachment_loads]
        if len(names) != len(set(names)):
            raise ValueError('attachment load names must be unique')
        expected = _hash(self.identity_payload())
        if self.evidence_sha256 != expected:
            raise ValueError('mount load evidence hash mismatch')
        if self.evidence_id != _semantic_id('mntload', expected):
            raise ValueError('load evidence id does not match its hash')
        return self


def mount_load_binding(
    evidence: CadMountLoadEvidence,
) -> AuthorityRef:
    return AuthorityRef(
        kind='mount_load_evidence',
        ref_id=evidence.evidence_id,
        ref_sha256=evidence.evidence_sha256,
    )


class CadSupportElementRecord(BaseModel):
    """The element that carries the load (#620 §3).

    ``element_class`` records *what kind of thing* the load hangs from;
    ``geometry_ref`` may pin its #613/#578 geometry evidence — geometry
    confirms approximate location, never capacity. ``declared_capacity``
    is a verbatim declared rating; HTDT computes no rating of its own.
    """

    model_config = ConfigDict(frozen=True)

    element_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assembly_ref: AuthorityRef
    element_class: SupportElementClass = 'unknown'
    geometry_ref: AuthorityRef | None = None
    """#613 geo_element_evidence / #578 IFC element pin — position
    evidence only; a BIM material type is not a capacity (#620 §14)."""
    hidden_condition_state: HiddenConditionState = 'unknown'
    """Anchor/embedment/blocking visibility — scan geometry cannot see
    hidden structure (MNT50)."""
    declared_capacity: CadDeclaredCapacity | None = None
    location_note: str | None = Field(default=None, min_length=1)
    declared_at_utc: str = Field(min_length=1)
    element_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'assembly_ref': self.assembly_ref.model_dump(mode='json'),
            'element_class': self.element_class,
            'geometry_ref': (
                self.geometry_ref.model_dump(mode='json')
                if self.geometry_ref is not None
                else None
            ),
            'hidden_condition_state': self.hidden_condition_state,
            'declared_capacity': (
                self.declared_capacity.model_dump(mode='json')
                if self.declared_capacity is not None
                else None
            ),
            'location_note': self.location_note,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadSupportElementRecord':
        _require_iso8601(self.declared_at_utc, 'element declared_at_utc')
        if self.assembly_ref.kind != 'mount_assembly':
            raise ValueError(
                "assembly_ref must pin a 'mount_assembly' authority"
            )
        if self.assembly_ref.ref_sha256 is None:
            raise ValueError('assembly_ref must pin its sha256')
        if self.geometry_ref is not None and (
            self.geometry_ref.ref_sha256 is None
        ):
            raise ValueError('geometry_ref must pin its sha256')
        if self.element_class == 'suspended_ceiling_grid' and (
            self.hidden_condition_state == 'verified'
        ):
            raise ValueError(
                'a suspended ceiling grid cannot carry a verified '
                'structural-support claim — it is not a structural '
                'support element by definition'
            )
        expected = _hash(self.identity_payload())
        if self.element_sha256 != expected:
            raise ValueError('support element hash mismatch')
        if self.element_id != _semantic_id('mntsup', expected):
            raise ValueError('support element id does not match its hash')
        return self


def support_element_binding(
    element: CadSupportElementRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='mount_support_element',
        ref_id=element.element_id,
        ref_sha256=element.element_sha256,
    )


class CadManufacturerMountingRequirement(BaseModel):
    """The manufacturer's exact mounting instructions (#620 §6/§7/§17).

    Approved points, orientation restrictions, fastener/retention
    requirements, prohibited configurations, the enclosure suspension
    rating and the VESA interface are retained verbatim — a manufacturer
    prohibition is never overridden by generic rigging rules.
    """

    model_config = ConfigDict(frozen=True)

    requirement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_ref: AuthorityRef
    """The equipment the instructions apply to (equipment definition or
    installed instance pin)."""
    approved_mount_points: tuple[str, ...] = ()
    orientation_restrictions: tuple[str, ...] = ()
    fastener_requirements: tuple[str, ...] = ()
    secondary_retention: Literal[
        'required', 'optional', 'prohibited', 'unspecified'
    ] = 'unspecified'
    prohibited_configurations: tuple[str, ...] = ()
    environmental_limitations: tuple[str, ...] = ()
    enclosure_suspension: EnclosureSuspensionRating = 'unknown'
    vesa_pattern: str | None = Field(default=None, min_length=1)
    source_ref: AuthorityRef | None = None
    """Manual / datasheet pin — the requirement cites its document."""
    source_version: str | None = Field(default=None, min_length=1)
    declared_at_utc: str = Field(min_length=1)
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'subject_ref': self.subject_ref.model_dump(mode='json'),
            'approved_mount_points': list(self.approved_mount_points),
            'orientation_restrictions': list(
                self.orientation_restrictions
            ),
            'fastener_requirements': list(self.fastener_requirements),
            'secondary_retention': self.secondary_retention,
            'prohibited_configurations': list(
                self.prohibited_configurations
            ),
            'environmental_limitations': list(
                self.environmental_limitations
            ),
            'enclosure_suspension': self.enclosure_suspension,
            'vesa_pattern': self.vesa_pattern,
            'source_ref': (
                self.source_ref.model_dump(mode='json')
                if self.source_ref is not None
                else None
            ),
            'source_version': self.source_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadManufacturerMountingRequirement':
        _require_iso8601(
            self.declared_at_utc, 'requirement declared_at_utc'
        )
        if self.subject_ref.ref_sha256 is None:
            raise ValueError('subject_ref must pin its sha256')
        for ref, label in ((self.source_ref, 'source_ref'),):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if (
            self.enclosure_suspension == 'e1_8_rated'
            and self.source_ref is None
        ):
            raise ValueError(
                'an E1.8 suspension rating requires pinned manufacturer '
                'evidence — the claim is product documentation, not a '
                'default'
            )
        expected = _hash(self.identity_payload())
        if self.requirement_sha256 != expected:
            raise ValueError('manufacturer requirement hash mismatch')
        if self.requirement_id != _semantic_id('mntreq', expected):
            raise ValueError('requirement id does not match its hash')
        return self


def manufacturer_requirement_binding(
    requirement: CadManufacturerMountingRequirement,
) -> AuthorityRef:
    return AuthorityRef(
        kind='mount_manufacturer_requirement',
        ref_id=requirement.requirement_id,
        ref_sha256=requirement.requirement_sha256,
    )


class CadStructuralApprovalRecord(BaseModel):
    """One approval artifact for the support claim (#620 §4/§15/§16).

    The evidence class is first-class — engineer design, qualified
    professional, permit, rigging-standard profile, installer
    declaration, field inspection, proof-test and assumption are
    different tiers and never promote each other. Jurisdiction and code
    edition are referenced, never defaulted.
    """

    model_config = ConfigDict(frozen=True)

    approval_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assembly_ref: AuthorityRef | None = None
    """Optional assembly pin — a rigging-system or project-scope
    approval legitimately covers more than one assembly."""
    evidence_class: ApprovalEvidenceClass = 'unknown'
    approval_scope: ApprovalScope = 'unknown'
    duty_coverage: ApprovalDutyCoverage = 'unknown'
    approver: CadApproverIdentity | None = None
    standard_ref: AuthorityRef | None = None
    """#599 standards-profile pin (ANSI E1.56 / E1.8 / E1.47 …) — a
    rigging-standard profile records *which* standard governed, never
    that ESTA certified the work."""
    jurisdiction: str | None = Field(default=None, min_length=1)
    code_edition: str | None = Field(default=None, min_length=1)
    document_ref: str | None = Field(default=None, min_length=1)
    """External artifact pointer — sealed engineering documents stay
    outside the client bundle by default (#620 §16)."""
    declared_rating_kg: float | None = Field(default=None, gt=0.0)
    declared_safety_factor: float | None = Field(default=None, gt=0.0)
    """Only when the approval document itself states them."""
    conditions: tuple[str, ...] = ()
    issued_at_utc: str | None = None
    expires_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    approval_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'assembly_ref': (
                self.assembly_ref.model_dump(mode='json')
                if self.assembly_ref is not None
                else None
            ),
            'evidence_class': self.evidence_class,
            'approval_scope': self.approval_scope,
            'duty_coverage': self.duty_coverage,
            'approver': (
                self.approver.model_dump(mode='json')
                if self.approver is not None
                else None
            ),
            'standard_ref': (
                self.standard_ref.model_dump(mode='json')
                if self.standard_ref is not None
                else None
            ),
            'jurisdiction': self.jurisdiction,
            'code_edition': self.code_edition,
            'document_ref': self.document_ref,
            'declared_rating_kg': self.declared_rating_kg,
            'declared_safety_factor': self.declared_safety_factor,
            'conditions': list(self.conditions),
            'issued_at_utc': self.issued_at_utc,
            'expires_at_utc': self.expires_at_utc,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadStructuralApprovalRecord':
        _require_iso8601(
            self.declared_at_utc, 'approval declared_at_utc'
        )
        for value, label in (
            (self.issued_at_utc, 'issued_at_utc'),
            (self.expires_at_utc, 'expires_at_utc'),
        ):
            if value is not None:
                _require_iso8601(value, label)
        if (
            self.issued_at_utc is not None
            and self.expires_at_utc is not None
            and self.expires_at_utc <= self.issued_at_utc
        ):
            raise ValueError('expiry must follow issue')
        if self.assembly_ref is not None:
            if self.assembly_ref.kind != 'mount_assembly':
                raise ValueError(
                    "assembly_ref must pin a 'mount_assembly' authority"
                )
            if self.assembly_ref.ref_sha256 is None:
                raise ValueError('assembly_ref must pin its sha256')
        if self.standard_ref is not None:
            if self.standard_ref.kind != 'standards_profile':
                raise ValueError(
                    "standard_ref must pin a 'standards_profile' authority"
                )
            if self.standard_ref.ref_sha256 is None:
                raise ValueError('standard_ref must pin its sha256')
        professional = {
            'structural_engineer_design',
            'qualified_professional_record',
        }
        if self.evidence_class in professional:
            if self.approver is None or (
                self.approver.credential is None
                and self.approver.name is None
            ):
                raise ValueError(
                    'a professional approval class requires approver '
                    'identity evidence — an unattributed seal is not one'
                )
        if self.evidence_class == 'local_code_permit_record' and (
            self.jurisdiction is None
        ):
            raise ValueError(
                'a code/permit record requires its jurisdiction — the '
                'record names who issued it'
            )
        if self.evidence_class == 'rigging_standard_profile' and (
            self.standard_ref is None
        ):
            raise ValueError(
                'a rigging-standard profile approval requires its '
                'standards_profile pin — the standard is referenced, '
                'not assumed'
            )
        if self.evidence_class == 'load_proof_test' and (
            self.document_ref is None
            and self.declared_rating_kg is None
        ):
            raise ValueError(
                'a load/proof-test approval requires the test document '
                'or the declared tested rating'
            )
        if self.evidence_class in {'user_assumed', 'unknown'} and (
            self.declared_rating_kg is not None
            or self.declared_safety_factor is not None
        ):
            raise ValueError(
                'an assumed/unknown approval cannot carry declared '
                'ratings — ratings belong to documented approvals'
            )
        for value, label in (
            (self.declared_rating_kg, 'declared_rating_kg'),
            (self.declared_safety_factor, 'declared_safety_factor'),
        ):
            if value is not None:
                _require_finite(value, label)
        expected = _hash(self.identity_payload())
        if self.approval_sha256 != expected:
            raise ValueError('structural approval hash mismatch')
        if self.approval_id != _semantic_id('mntappr', expected):
            raise ValueError('approval id does not match its hash')
        return self


def structural_approval_binding(
    approval: CadStructuralApprovalRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='mount_structural_approval',
        ref_id=approval.approval_id,
        ref_sha256=approval.approval_sha256,
    )


class CadMountingInspectionRecord(BaseModel):
    """Installation / periodic inspection as-built (#620 §11/§12).

    Observations are per-aspect honest — ``not_visible`` is a recorded
    limitation, never a silent pass. ``next_due_at_utc`` is only what a
    profile/manufacturer declared; HTDT invents no interval.
    """

    model_config = ConfigDict(frozen=True)

    inspection_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assembly_ref: AuthorityRef
    inspection_kind: InspectionKind
    inspector_class: InspectorClass = 'unknown'
    support_point_observation: ObservationState = 'unknown'
    secondary_retention_observation: ObservationState = 'unknown'
    fastener_observation: ObservationState = 'unknown'
    pose_observation: Literal[
        'matches_assembly', 'differs', 'not_visible', 'unknown'
    ] = 'unknown'
    inaccessible_points: tuple[str, ...] = ()
    findings: InspectionFindings = 'inconclusive'
    profile_ref: AuthorityRef | None = None
    """#599 inspection-profile pin that required this inspection."""
    inspected_at_utc: str = Field(min_length=1)
    next_due_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    inspection_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'assembly_ref': self.assembly_ref.model_dump(mode='json'),
            'inspection_kind': self.inspection_kind,
            'inspector_class': self.inspector_class,
            'support_point_observation': self.support_point_observation,
            'secondary_retention_observation': (
                self.secondary_retention_observation
            ),
            'fastener_observation': self.fastener_observation,
            'pose_observation': self.pose_observation,
            'inaccessible_points': list(self.inaccessible_points),
            'findings': self.findings,
            'profile_ref': (
                self.profile_ref.model_dump(mode='json')
                if self.profile_ref is not None
                else None
            ),
            'inspected_at_utc': self.inspected_at_utc,
            'next_due_at_utc': self.next_due_at_utc,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadMountingInspectionRecord':
        _require_iso8601(self.inspected_at_utc, 'inspected_at_utc')
        _require_iso8601(self.declared_at_utc, 'inspection declared_at_utc')
        if self.next_due_at_utc is not None:
            _require_iso8601(self.next_due_at_utc, 'next_due_at_utc')
            if self.next_due_at_utc <= self.inspected_at_utc:
                raise ValueError('next due must follow inspection')
        if self.assembly_ref.kind != 'mount_assembly':
            raise ValueError(
                "assembly_ref must pin a 'mount_assembly' authority"
            )
        if self.assembly_ref.ref_sha256 is None:
            raise ValueError('assembly_ref must pin its sha256')
        if self.profile_ref is not None:
            if self.profile_ref.kind != 'standards_profile':
                raise ValueError(
                    "profile_ref must pin a 'standards_profile' authority"
                )
            if self.profile_ref.ref_sha256 is None:
                raise ValueError('profile_ref must pin its sha256')
        if (
            self.findings in {'pass', 'pass_with_notes'}
            and self.support_point_observation == 'mismatch'
        ):
            raise ValueError(
                'a passing inspection cannot carry a support-point '
                'mismatch — failed observations fail the record'
            )
        if self.findings == 'pass' and self.secondary_retention_observation == (
            'absent'
        ):
            raise ValueError(
                'a passing inspection cannot carry an absent secondary '
                'retention'
            )
        if len(set(self.inaccessible_points)) != len(
            self.inaccessible_points
        ):
            raise ValueError('inaccessible points must be unique')
        expected = _hash(self.identity_payload())
        if self.inspection_sha256 != expected:
            raise ValueError('mounting inspection hash mismatch')
        if self.inspection_id != _semantic_id('mntinsp', expected):
            raise ValueError('inspection id does not match its hash')
        return self


def mounting_inspection_binding(
    inspection: CadMountingInspectionRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='mount_inspection',
        ref_id=inspection.inspection_id,
        ref_sha256=inspection.inspection_sha256,
    )


# ---------------------------------------------------------------------------
# Qualification (#620 §5)
# ---------------------------------------------------------------------------


class CadSuspensionLayerStates(BaseModel):
    """The suspended-loudspeaker triple (#620 §7).

    A defensible suspended loudspeaker needs all three: enclosure
    suspension capability (E1.8-class / manufacturer), building
    support-point capability, and the field rigging assembly.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    enclosure_capability: SuspensionLayerState = 'not_applicable'
    building_support_point: SuspensionLayerState = 'not_applicable'
    field_rigging_assembly: SuspensionLayerState = 'not_applicable'


class CadMountingQualification(BaseModel):
    """Sealed fail-closed mounting verdict (#620 §5)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    assembly_ref: AuthorityRef
    requirement_ref: AuthorityRef | None = None
    load_evidence_ref: AuthorityRef | None = None
    support_element_refs: tuple[AuthorityRef, ...] = ()
    approval_refs: tuple[AuthorityRef, ...] = ()
    inspection_refs: tuple[AuthorityRef, ...] = ()
    suspension_layers: CadSuspensionLayerStates = CadSuspensionLayerStates()
    support_state: MountSupportState
    stale_flags: tuple[SubstitutionChangeAxis, ...] = ()
    demand_vs_rating_kg: float | None = None
    """Declared rating minus declared mass — a *reported* margin between
    two declared numbers, never an adequacy computation."""
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'assembly_ref': self.assembly_ref.model_dump(mode='json'),
            'requirement_ref': (
                self.requirement_ref.model_dump(mode='json')
                if self.requirement_ref is not None
                else None
            ),
            'load_evidence_ref': (
                self.load_evidence_ref.model_dump(mode='json')
                if self.load_evidence_ref is not None
                else None
            ),
            'support_element_refs': [
                ref.model_dump(mode='json')
                for ref in self.support_element_refs
            ],
            'approval_refs': [
                ref.model_dump(mode='json') for ref in self.approval_refs
            ],
            'inspection_refs': [
                ref.model_dump(mode='json')
                for ref in self.inspection_refs
            ],
            'suspension_layers': self.suspension_layers.model_dump(
                mode='json'
            ),
            'support_state': self.support_state,
            'stale_flags': list(self.stale_flags),
            'demand_vs_rating_kg': self.demand_vs_rating_kg,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadMountingQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.assembly_ref.kind != 'mount_assembly':
            raise ValueError(
                "assembly_ref must pin a 'mount_assembly' authority"
            )
        if self.assembly_ref.ref_sha256 is None:
            raise ValueError('assembly_ref must pin its sha256')
        for ref, label in (
            (self.requirement_ref, 'requirement_ref'),
            (self.load_evidence_ref, 'load_evidence_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.requirement_ref is not None and (
            self.requirement_ref.kind != 'mount_manufacturer_requirement'
        ):
            raise ValueError(
                'requirement_ref must pin a '
                'mount_manufacturer_requirement authority'
            )
        if self.load_evidence_ref is not None and (
            self.load_evidence_ref.kind != 'mount_load_evidence'
        ):
            raise ValueError(
                'load_evidence_ref must pin a mount_load_evidence '
                'authority'
            )
        for ref in self.support_element_refs:
            if ref.kind != 'mount_support_element':
                raise ValueError(
                    'support_element_refs must pin mount_support_element '
                    'authorities'
                )
            if ref.ref_sha256 is None:
                raise ValueError(
                    'support element refs must pin their sha256'
                )
        for ref in self.approval_refs:
            if ref.kind != 'mount_structural_approval':
                raise ValueError(
                    'approval_refs must pin mount_structural_approval '
                    'authorities'
                )
            if ref.ref_sha256 is None:
                raise ValueError('approval refs must pin their sha256')
        for ref in self.inspection_refs:
            if ref.kind != 'mount_inspection':
                raise ValueError(
                    'inspection_refs must pin mount_inspection '
                    'authorities'
                )
            if ref.ref_sha256 is None:
                raise ValueError('inspection refs must pin their sha256')
        if self.support_state != 'stale_after_change' and self.stale_flags:
            raise ValueError(
                'stale flags may only accompany a stale_after_change '
                'verdict'
            )
        if self.demand_vs_rating_kg is not None:
            _require_finite(
                self.demand_vs_rating_kg, 'demand_vs_rating_kg'
            )
        if (
            self.support_state == 'design_support_evidence_complete'
            and any(
                layer == 'incompatible'
                for layer in (
                    self.suspension_layers.enclosure_capability,
                    self.suspension_layers.building_support_point,
                    self.suspension_layers.field_rigging_assembly,
                )
            )
        ):
            raise ValueError(
                'a complete verdict cannot carry an incompatible '
                'suspension layer'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('mounting qualification hash mismatch')
        if self.qualification_id != _semantic_id('mntqual', expected):
            raise ValueError(
                'mounting qualification id does not match its hash'
            )
        return self


def mounting_qualification_binding(
    qualification: CadMountingQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='mount_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation (#620 §5/§7/§9/§10/§13)
# ---------------------------------------------------------------------------

#: Approval classes that can carry an overhead-suspension claim — the
#: AVIXA/ESTA "could injure someone if it falls" tier. A rigging-standard
#: profile documents which standard governed; it is not a project
#: approval (#620 §4).
_PROFESSIONAL_APPROVAL_CLASSES: frozenset[str] = frozenset(
    {
        'structural_engineer_design',
        'qualified_professional_record',
        'local_code_permit_record',
        'load_proof_test',
    }
)

#: Approval classes that document mounting rules but never prove this
#: installation — usable as supporting evidence, never alone sufficient.
_DOCUMENTARY_APPROVAL_CLASSES: frozenset[str] = frozenset(
    {
        'manufacturer_installation_requirement',
        'rigging_standard_profile',
    }
)

#: Support element classes a suspended load may defensibly hang from —
#: everything else (grid, unknown, manufacturer stand) fails closed.
_SUSPENDABLE_ELEMENT_CLASSES: frozenset[str] = frozenset(
    {
        'structural_steel',
        'concrete',
        'timber',
        'blocking_subframe',
        'ceiling_deck',
        'wall_framing',
        'baffle_wall_structure',
        'rigging_support_point',
    }
)


def _kg_of_capacity(capacity: CadDeclaredCapacity) -> float | None:
    """Declared rating in kg when the unit is plainly kg-equivalent."""
    unit = capacity.unit.strip().lower()
    if unit in {'kg', 'kilogram', 'kilograms'}:
        return capacity.value
    if unit in {'n', 'kn'}:
        factor = 1.0 if unit == 'n' else 1000.0
        return capacity.value * factor / 9.80665
    if unit in {'lbf', 'lb', 'lbs'}:
        return capacity.value * 0.45359237
    return None


def _kg_of_evidence(evidence: CadMountLoadEvidence) -> float | None:
    if evidence.mass_kg is not None:
        return evidence.mass_kg
    if evidence.weight_n is not None:
        return evidence.weight_n / 9.80665
    return None


def evaluate_mounting_support(
    *,
    document_id: str,
    assembly: CadMountingAssembly,
    requirement: CadManufacturerMountingRequirement | None = None,
    load_evidence: CadMountLoadEvidence | None = None,
    support_elements: Sequence[CadSupportElementRecord] = (),
    approvals: Sequence[CadStructuralApprovalRecord] = (),
    inspections: Sequence[CadMountingInspectionRecord] = (),
    substitution_flags: Sequence[SubstitutionChangeAxis] = (),
    evaluated_at_utc: str | None = None,
) -> CadMountingQualification:
    """Fail-closed mounting-support verdict (#620 §5/§7/§9/§10/§13).

    The ladder is ordered deepest-defect-first so one verdict names the
    most serious open problem. A complete verdict requires the whole
    evidence chain — exact equipment, declared demand, a real support
    element, professional approval where required, and a passing
    inspection where required. Anything less stays visibly open.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    stale = tuple(dict.fromkeys(substitution_flags))
    approvals = tuple(approvals)
    inspections = tuple(inspections)
    support_elements = tuple(support_elements)

    # -- suspension layers (#620 §7) ------------------------------------
    suspended_loudspeaker = (
        assembly.equipment_class in {'loudspeaker', 'subwoofer'}
        and (
            assembly.overhead_suspension
            or assembly.support_method in {
                'suspended_rigging',
                'ceiling',
            }
        )
    )
    enclosure_state: SuspensionLayerState = 'not_applicable'
    support_point_state: SuspensionLayerState = 'not_applicable'
    rigging_state: SuspensionLayerState = 'not_applicable'
    if suspended_loudspeaker:
        if requirement is not None:
            if requirement.enclosure_suspension in {
                'e1_8_rated',
                'manufacturer_rated',
            }:
                enclosure_state = 'capable'
            elif requirement.enclosure_suspension == (
                'not_rated_for_suspension'
            ):
                enclosure_state = 'incompatible'
            elif requirement.enclosure_suspension == 'not_applicable':
                enclosure_state = 'incompatible'
            else:
                enclosure_state = 'unknown'
        else:
            enclosure_state = 'unknown'
        structural_elements = [
            element
            for element in support_elements
            if element.element_class in _SUSPENDABLE_ELEMENT_CLASSES
        ]
        if any(
            element.element_class == 'suspended_ceiling_grid'
            for element in support_elements
        ):
            support_point_state = 'incompatible'
            reasons.append(
                'suspended loudspeaker hangs from a suspended ceiling '
                'grid — the grid is never a structural support point'
            )
        elif not structural_elements:
            support_point_state = 'unknown'
        elif any(
            element.hidden_condition_state == 'inaccessible'
            for element in structural_elements
        ):
            support_point_state = 'unknown'
        else:
            support_point_state = 'capable'
        has_rig_components = any(
            component.role in {
                'support_point',
                'pole_or_rig',
                'safety_retention',
            }
            for component in assembly.components
        )
        if not assembly.components:
            rigging_state = 'unknown'
        elif assembly.secondary_retention_state == 'required_missing':
            rigging_state = 'incompatible'
        elif has_rig_components and (
            assembly.secondary_retention_state
            in {'required_installed', 'not_required'}
        ):
            rigging_state = 'capable'
        else:
            rigging_state = 'unknown'

    layers = CadSuspensionLayerStates(
        enclosure_capability=enclosure_state,
        building_support_point=support_point_state,
        field_rigging_assembly=rigging_state,
    )

    # -- shared sub-analyses --------------------------------------------
    professional_approvals = [
        approval
        for approval in approvals
        if approval.evidence_class in _PROFESSIONAL_APPROVAL_CLASSES
    ]
    covering_professional = [
        approval
        for approval in professional_approvals
        if approval.approval_scope
        in {'assembly', 'support_element', 'rigging_system', 'project'}
    ]
    has_professional = bool(covering_professional)
    has_documentary = any(
        approval.evidence_class in _DOCUMENTARY_APPROVAL_CLASSES
        for approval in approvals
    )
    installer_only = bool(approvals) and not has_professional and any(
        approval.evidence_class == 'installer_declaration'
        for approval in approvals
    )
    latest_inspection = max(
        inspections, key=lambda record: record.inspected_at_utc
    ) if inspections else None
    inspection_failed = latest_inspection is not None and (
        latest_inspection.findings in {'failed', 'inconclusive'}
    )
    inspection_open = latest_inspection is not None and (
        latest_inspection.findings in {'findings_open'}
    )
    inspection_mismatch = latest_inspection is not None and (
        latest_inspection.pose_observation == 'differs'
        or latest_inspection.support_point_observation == 'mismatch'
    )
    inspection_limitations = latest_inspection is not None and (
        bool(latest_inspection.inaccessible_points)
        or 'not_visible'
        in {
            latest_inspection.support_point_observation,
            latest_inspection.secondary_retention_observation,
            latest_inspection.fastener_observation,
            latest_inspection.pose_observation,
        }
    )

    # declared demand vs declared rating (reported only)
    demand_kg = (
        _kg_of_evidence(load_evidence) if load_evidence is not None
        else None
    )
    rating_kg_values = [
        rating
        for rating in (
            _kg_of_capacity(element.declared_capacity)
            for element in support_elements
            if element.declared_capacity is not None
        )
        if rating is not None
    ]
    demand_vs_rating = None
    capacity_declared_insufficient = False
    if demand_kg is not None and rating_kg_values:
        margin = min(rating_kg_values) - demand_kg
        demand_vs_rating = margin
        if margin < 0:
            capacity_declared_insufficient = True
            reasons.append(
                f'declared demand {demand_kg:.3g} kg exceeds the '
                f'weakest declared rating {min(rating_kg_values):.3g} kg'
            )

    # manufacturer prohibition / retention / suspension checks
    manufacturer_incompatible = False
    if requirement is not None:
        prohibited = set(requirement.prohibited_configurations)
        hit = prohibited & set(assembly.declared_configuration_ids)
        if hit:
            manufacturer_incompatible = True
            reasons.append(
                'manufacturer-prohibited configuration(s): '
                + ' / '.join(sorted(hit))
            )
        if requirement.secondary_retention == 'required' and (
            assembly.secondary_retention_state == 'required_missing'
        ):
            manufacturer_incompatible = True
            reasons.append(
                'manufacturer requires secondary retention and the '
                'assembly records it missing'
            )
        if (
            assembly.overhead_suspension
            and requirement.enclosure_suspension
            == 'not_rated_for_suspension'
        ):
            manufacturer_incompatible = True
            reasons.append(
                'overhead mounting of equipment the manufacturer does '
                'not rate for suspension'
            )
        if requirement.vesa_pattern is not None:
            mount_patterns = {
                component.vesa_pattern
                for component in assembly.components
                if component.vesa_pattern is not None
            }
            if mount_patterns and (
                requirement.vesa_pattern not in mount_patterns
            ):
                manufacturer_incompatible = True
                reasons.append(
                    f'equipment VESA interface '
                    f'{requirement.vesa_pattern} matches no mount '
                    'component pattern'
                )
    if assembly.secondary_retention_state == 'required_missing':
        manufacturer_incompatible = True
        if not any('secondary retention' in r for r in reasons):
            reasons.append(
                'secondary retention is required but missing'
            )

    # duty coverage (#620 §10)
    moving_duty = assembly.duty_state in {
        'moving_motorized',
        'moving_manual',
    }
    duty_uncovered = False
    if moving_duty:
        if not approvals:
            duty_uncovered = True
        elif not any(
            approval.duty_coverage
            in {'static_and_dynamic', 'moving_system'}
            for approval in approvals
        ):
            duty_uncovered = True

    # ``support_unknown`` names the case where support records exist
    # but every element is explicitly unknown — nothing declared at all
    # falls through to the approval ladder instead.
    support_unknown = bool(support_elements) and all(
        element.element_class == 'unknown' for element in support_elements
    )

    # -- verdict ladder -------------------------------------------------
    if stale:
        support_state: MountSupportState = 'stale_after_change'
        reasons.append(
            'structural evidence predates a change on axes: '
            + ' / '.join(stale)
        )
    elif manufacturer_incompatible or (
        suspended_loudspeaker and enclosure_state == 'incompatible'
    ):
        support_state = 'manufacturer_mounting_incompatible'
        if not reasons:
            reasons.append(
                'manufacturer mounting requirements are not met'
            )
    elif support_unknown:
        support_state = 'support_unknown'
        reasons.append(
            'no structural support element is declared — CAD presence '
            'is not a support claim'
        )
    elif inspection_mismatch or inspection_failed:
        support_state = 'as_built_mismatch'
        reasons.append(
            'as-built inspection contradicts the declared assembly '
            'or failed'
        )
    elif capacity_declared_insufficient:
        support_state = 'support_capacity_insufficient'
    elif assembly.interference_state == 'conflict_observed':
        support_state = 'structural_approval_required'
        reasons.append(
            'mount/structure interference is observed and unresolved'
        )
    elif assembly.overhead_suspension and not has_professional:
        support_state = 'structural_approval_required'
        reasons.append(
            'overhead suspension lacks a professional structural '
            'approval — an installer declaration or CAD note is not '
            'one'
        )
    elif suspended_loudspeaker and 'unknown' in {
        enclosure_state,
        support_point_state,
        rigging_state,
    }:
        support_state = 'structural_approval_required'
        missing = [
            name
            for name, state in (
                ('enclosure', enclosure_state),
                ('support point', support_point_state),
                ('rigging assembly', rigging_state),
            )
            if state == 'unknown'
        ]
        reasons.append(
            'suspended-loudspeaker layers unproven: '
            + ' / '.join(missing)
        )
    elif assembly.isolation_mount and not has_professional:
        support_state = 'structural_approval_required'
        reasons.append(
            'vibration-isolation mount lacks structural suitability '
            'evidence — acoustic benefit never promotes it'
        )
    elif not has_professional and not (
        has_documentary or installer_only
    ):
        support_state = 'structural_approval_required'
        reasons.append(
            'no structural approval evidence of any class is bound'
        )
    elif duty_uncovered:
        support_state = 'approved_with_limitations'
        reasons.append(
            'moving/motorized equipment holds static-only approval — '
            'moving-system requirements unproven'
        )
    elif latest_inspection is None and (
        assembly.overhead_suspension
        or has_professional
        or any(
            approval.evidence_class == 'field_inspection'
            for approval in approvals
        )
    ):
        support_state = 'installation_inspection_required'
        reasons.append(
            'installation/periodic inspection evidence is required '
            'but none is bound'
        )
    elif inspection_open:
        support_state = 'installation_inspection_required'
        reasons.append('inspection findings remain open')
    elif inspection_limitations or (
        assembly.interference_state != 'clear'
    ) or (
        support_elements
        and any(
            element.hidden_condition_state == 'inaccessible'
            for element in support_elements
        )
    ) or not has_professional or assembly.equipment_ref is None or (
        demand_kg is None and assembly.overhead_suspension
    ):
        support_state = 'approved_with_limitations'
        if inspection_limitations:
            reasons.append(
                'inspection carries inaccessible/not-visible points'
            )
        if assembly.interference_state != 'clear':
            reasons.append(
                'mount/structure interference state is not resolved'
            )
        if not has_professional:
            reasons.append(
                'approval is documentary/installer-tier only'
            )
        if assembly.equipment_ref is None:
            reasons.append(
                'assembly is not bound to an exact equipment instance'
            )
        if demand_kg is None and assembly.overhead_suspension:
            reasons.append('no declared demand evidence bound')
        if not reasons:
            reasons.append(
                'evidence is complete to a limited tier only'
            )
    else:
        support_state = 'design_support_evidence_complete'

    requirement_ref = (
        manufacturer_requirement_binding(requirement)
        if requirement is not None
        else None
    )
    load_ref = (
        mount_load_binding(load_evidence)
        if load_evidence is not None
        else None
    )
    return _seal(
        CadMountingQualification,
        {
            'document_id': document_id,
            'assembly_ref': mounting_assembly_binding(assembly).model_dump(
                mode='json'
            ),
            'requirement_ref': (
                requirement_ref.model_dump(mode='json')
                if requirement_ref is not None
                else None
            ),
            'load_evidence_ref': (
                load_ref.model_dump(mode='json')
                if load_ref is not None
                else None
            ),
            'support_element_refs': [
                support_element_binding(element).model_dump(mode='json')
                for element in support_elements
            ],
            'approval_refs': [
                structural_approval_binding(approval).model_dump(
                    mode='json'
                )
                for approval in approvals
            ],
            'inspection_refs': [
                mounting_inspection_binding(inspection).model_dump(
                    mode='json'
                )
                for inspection in inspections
            ],
            'suspension_layers': layers.model_dump(mode='json'),
            'support_state': support_state,
            'stale_flags': list(stale),
            'demand_vs_rating_kg': demand_vs_rating,
            'reasons': reasons,
            'evaluation_version': MOUNTING_EVALUATION_VERSION,
            'evaluated_at_utc': evaluated_at_utc,
        },
        'qualification_id',
        'qualification_sha256',
        'mntqual',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_mounting_assembly(
    *,
    document_id: str,
    equipment_ref: AuthorityRef | None = None,
    placement_ref: AuthorityRef | None = None,
    equipment_class: MountEquipmentClass = 'unknown',
    support_method: MountSupportMethod = 'unknown',
    overhead_suspension: bool = False,
    components: Sequence[CadMountingComponent] = (),
    secondary_retention_state: SecondaryRetentionState = 'unknown',
    duty_state: MountDutyState = 'unknown',
    isolation_mount: bool = False,
    declared_configuration_ids: Sequence[str] = (),
    interference_state: MountInterferenceState = 'unknown',
    cable_route_refs: Sequence[AuthorityRef] = (),
    installed_pose: CadMountPose | None = None,
    declared_at_utc: str | None = None,
) -> CadMountingAssembly:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadMountingAssembly,
        {
            'document_id': document_id,
            'equipment_ref': (
                equipment_ref.model_dump(mode='json')
                if equipment_ref is not None
                else None
            ),
            'placement_ref': (
                placement_ref.model_dump(mode='json')
                if placement_ref is not None
                else None
            ),
            'equipment_class': equipment_class,
            'support_method': support_method,
            'overhead_suspension': overhead_suspension,
            'components': [
                component.model_dump(mode='json')
                if isinstance(component, CadMountingComponent)
                else component
                for component in components
            ],
            'secondary_retention_state': secondary_retention_state,
            'duty_state': duty_state,
            'isolation_mount': isolation_mount,
            'declared_configuration_ids': list(
                declared_configuration_ids
            ),
            'interference_state': interference_state,
            'cable_route_refs': [
                ref.model_dump(mode='json')
                if isinstance(ref, AuthorityRef)
                else ref
                for ref in cable_route_refs
            ],
            'installed_pose': (
                installed_pose.model_dump(mode='json')
                if installed_pose is not None
                else None
            ),
            'authority_version': MOUNTING_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'assembly_id',
        'assembly_sha256',
        'mntassy',
    )


def build_mount_load_evidence(
    *,
    document_id: str,
    assembly: CadMountingAssembly,
    mass_kg: float | None = None,
    weight_n: float | None = None,
    center_of_gravity: CadMountPose | None = None,
    attachment_loads: Sequence[CadMountLoadEntry] = (),
    duty_state: MountDutyState = 'unknown',
    service_load_state: str = 'unknown',
    source_class: EvidenceSourceClass = 'unknown',
    measured_at_utc: str | None = None,
    declared_at_utc: str | None = None,
) -> CadMountLoadEvidence:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadMountLoadEvidence,
        {
            'document_id': document_id,
            'assembly_ref': mounting_assembly_binding(assembly).model_dump(
                mode='json'
            ),
            'mass_kg': mass_kg,
            'weight_n': weight_n,
            'center_of_gravity': (
                center_of_gravity.model_dump(mode='json')
                if center_of_gravity is not None
                else None
            ),
            'attachment_loads': [
                entry.model_dump(mode='json')
                if isinstance(entry, CadMountLoadEntry)
                else entry
                for entry in attachment_loads
            ],
            'duty_state': duty_state,
            'service_load_state': service_load_state,
            'source_class': source_class,
            'measured_at_utc': measured_at_utc,
            'declared_at_utc': declared_at_utc,
        },
        'evidence_id',
        'evidence_sha256',
        'mntload',
    )


def build_support_element(
    *,
    document_id: str,
    assembly: CadMountingAssembly,
    element_class: SupportElementClass = 'unknown',
    geometry_ref: AuthorityRef | None = None,
    hidden_condition_state: HiddenConditionState = 'unknown',
    declared_capacity: CadDeclaredCapacity | None = None,
    location_note: str | None = None,
    declared_at_utc: str | None = None,
) -> CadSupportElementRecord:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadSupportElementRecord,
        {
            'document_id': document_id,
            'assembly_ref': mounting_assembly_binding(assembly).model_dump(
                mode='json'
            ),
            'element_class': element_class,
            'geometry_ref': (
                geometry_ref.model_dump(mode='json')
                if geometry_ref is not None
                else None
            ),
            'hidden_condition_state': hidden_condition_state,
            'declared_capacity': (
                declared_capacity.model_dump(mode='json')
                if declared_capacity is not None
                else None
            ),
            'location_note': location_note,
            'declared_at_utc': declared_at_utc,
        },
        'element_id',
        'element_sha256',
        'mntsup',
    )


def build_manufacturer_requirement(
    *,
    document_id: str,
    subject_ref: AuthorityRef,
    approved_mount_points: Sequence[str] = (),
    orientation_restrictions: Sequence[str] = (),
    fastener_requirements: Sequence[str] = (),
    secondary_retention: str = 'unspecified',
    prohibited_configurations: Sequence[str] = (),
    environmental_limitations: Sequence[str] = (),
    enclosure_suspension: EnclosureSuspensionRating = 'unknown',
    vesa_pattern: str | None = None,
    source_ref: AuthorityRef | None = None,
    source_version: str | None = None,
    declared_at_utc: str | None = None,
) -> CadManufacturerMountingRequirement:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadManufacturerMountingRequirement,
        {
            'document_id': document_id,
            'subject_ref': subject_ref.model_dump(mode='json'),
            'approved_mount_points': list(approved_mount_points),
            'orientation_restrictions': list(orientation_restrictions),
            'fastener_requirements': list(fastener_requirements),
            'secondary_retention': secondary_retention,
            'prohibited_configurations': list(prohibited_configurations),
            'environmental_limitations': list(environmental_limitations),
            'enclosure_suspension': enclosure_suspension,
            'vesa_pattern': vesa_pattern,
            'source_ref': (
                source_ref.model_dump(mode='json')
                if source_ref is not None
                else None
            ),
            'source_version': source_version,
            'declared_at_utc': declared_at_utc,
        },
        'requirement_id',
        'requirement_sha256',
        'mntreq',
    )


def build_structural_approval(
    *,
    document_id: str,
    assembly: CadMountingAssembly | None = None,
    evidence_class: ApprovalEvidenceClass = 'unknown',
    approval_scope: ApprovalScope = 'unknown',
    duty_coverage: ApprovalDutyCoverage = 'unknown',
    approver: CadApproverIdentity | None = None,
    standard_ref: AuthorityRef | None = None,
    jurisdiction: str | None = None,
    code_edition: str | None = None,
    document_ref: str | None = None,
    declared_rating_kg: float | None = None,
    declared_safety_factor: float | None = None,
    conditions: Sequence[str] = (),
    issued_at_utc: str | None = None,
    expires_at_utc: str | None = None,
    declared_at_utc: str | None = None,
) -> CadStructuralApprovalRecord:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadStructuralApprovalRecord,
        {
            'document_id': document_id,
            'assembly_ref': (
                mounting_assembly_binding(assembly).model_dump(
                    mode='json'
                )
                if assembly is not None
                else None
            ),
            'evidence_class': evidence_class,
            'approval_scope': approval_scope,
            'duty_coverage': duty_coverage,
            'approver': (
                approver.model_dump(mode='json')
                if approver is not None
                else None
            ),
            'standard_ref': (
                standard_ref.model_dump(mode='json')
                if standard_ref is not None
                else None
            ),
            'jurisdiction': jurisdiction,
            'code_edition': code_edition,
            'document_ref': document_ref,
            'declared_rating_kg': declared_rating_kg,
            'declared_safety_factor': declared_safety_factor,
            'conditions': list(conditions),
            'issued_at_utc': issued_at_utc,
            'expires_at_utc': expires_at_utc,
            'declared_at_utc': declared_at_utc,
        },
        'approval_id',
        'approval_sha256',
        'mntappr',
    )


def build_mounting_inspection(
    *,
    document_id: str,
    assembly: CadMountingAssembly,
    inspection_kind: InspectionKind,
    inspector_class: InspectorClass = 'unknown',
    support_point_observation: ObservationState = 'unknown',
    secondary_retention_observation: ObservationState = 'unknown',
    fastener_observation: ObservationState = 'unknown',
    pose_observation: str = 'unknown',
    inaccessible_points: Sequence[str] = (),
    findings: InspectionFindings = 'inconclusive',
    profile_ref: AuthorityRef | None = None,
    inspected_at_utc: str | None = None,
    next_due_at_utc: str | None = None,
    declared_at_utc: str | None = None,
) -> CadMountingInspectionRecord:
    declared_at_utc = declared_at_utc or _utc_now()
    inspected_at_utc = inspected_at_utc or declared_at_utc
    return _seal(
        CadMountingInspectionRecord,
        {
            'document_id': document_id,
            'assembly_ref': mounting_assembly_binding(assembly).model_dump(
                mode='json'
            ),
            'inspection_kind': inspection_kind,
            'inspector_class': inspector_class,
            'support_point_observation': support_point_observation,
            'secondary_retention_observation': (
                secondary_retention_observation
            ),
            'fastener_observation': fastener_observation,
            'pose_observation': pose_observation,
            'inaccessible_points': list(inaccessible_points),
            'findings': findings,
            'profile_ref': (
                profile_ref.model_dump(mode='json')
                if profile_ref is not None
                else None
            ),
            'inspected_at_utc': inspected_at_utc,
            'next_due_at_utc': next_due_at_utc,
            'declared_at_utc': declared_at_utc,
        },
        'inspection_id',
        'inspection_sha256',
        'mntinsp',
    )


__all__ = [
    'ApprovalDutyCoverage',
    'ApprovalEvidenceClass',
    'ApprovalScope',
    'CadApproverIdentity',
    'CadDeclaredCapacity',
    'CadManufacturerMountingRequirement',
    'CadMountingAssembly',
    'CadMountingComponent',
    'CadMountingInspectionRecord',
    'CadMountingQualification',
    'CadMountLoadEntry',
    'CadMountLoadEvidence',
    'CadMountPose',
    'CadStructuralApprovalRecord',
    'CadSupportElementRecord',
    'CadSuspensionLayerStates',
    'ComponentIdentityState',
    'EnclosureSuspensionRating',
    'EvidenceSourceClass',
    'HiddenConditionState',
    'InspectionFindings',
    'InspectionKind',
    'InspectorClass',
    'MOUNTING_EVALUATION_VERSION',
    'MOUNTING_SCHEMA_VERSION',
    'MountDutyState',
    'MountEquipmentClass',
    'MountInterferenceState',
    'MountSupportMethod',
    'MountSupportState',
    'ObservationState',
    'SecondaryRetentionState',
    'SubstitutionChangeAxis',
    'SupportElementClass',
    'SuspensionLayerState',
    'build_manufacturer_requirement',
    'build_mount_load_evidence',
    'build_mounting_assembly',
    'build_mounting_inspection',
    'build_structural_approval',
    'build_support_element',
    'evaluate_mounting_support',
    'manufacturer_requirement_binding',
    'mount_load_binding',
    'mounting_assembly_binding',
    'mounting_inspection_binding',
    'mounting_qualification_binding',
    'structural_approval_binding',
    'support_element_binding',
]
