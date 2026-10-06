"""Projector optical-radiation safety authority (#627, REV57-PROJ).

Placement optimisation can create an optical-radiation hazard: a
projector position that is geometrically valid may still put an
accessible position inside the manufacturer-declared hazard zone, and a
lens or mount change can invalidate an earlier clearance. This module
is the fail-closed gate for that:

- :class:`CadProjectorSafetyIdentity` — the projector's safety basis
  exactly as documented: illumination source type, IEC 62471-5 risk
  group and IEC 60825-1 laser class kept as *distinct* fields (an RG2
  projector and a Class 1 laser product are different quantities and
  are never collapsed), each pinned to its source document.
- :class:`CadManufacturerSafetyConstraints` — manufacturer-declared
  safety geometry: hazard distance(s), prohibited orientations,
  audience/operator restrictions, lens-accessory applicability,
  service-state rules. Every value is source-pinned; an undocumented
  field stays UNKNOWN and is never filled by inference.
- :class:`CadProjectorPlacementDeclaration` — the sealed placement the
  verdict applies to: projector pose, throw distance, accessible
  positions, viewer geometry, lens accessory in use, operating state.
- :class:`CadOpticalSafetyEvaluation` + :func:`evaluate_optical_safety`
  — the verdict. When the manufacturer does not document enough to
  decide, the result is ``local_review_required`` /
  ``insufficient_evidence`` — HTDT returns a review state rather than
  calculating an improvised safe distance.

Safety boundaries baked in:

- Risk group and laser class are never merged or compared as one
  quantity; ``risk_group`` and ``laser_class`` are independent fields.
- A hazard distance is only ever used as the manufacturer declared it
  (per lens/accessory configuration); HTDT never derives one.
- ``remote_power_capable`` — "we can turn the projector off over IP" —
  is recorded but is *never* a safety control; it cannot upgrade any
  verdict.
- Service states (interlock-defeat, alignment modes) are classified
  ``service_state_not_user_safe`` and can never qualify an audience
  installation.
- A placement whose geometry differs from the one an earlier verdict
  bound (``placement_sha256`` mismatch) is reported
  ``stale_after_change`` — prior safety claims do not survive a move.
- HTDT never issues regulatory certification and never provides
  instructions to defeat interlocks or protective housings.

Literature basis
----------------
- IEC 62471-5:2015 — photobiological safety of image projectors;
  risk-group classification (RG0–RG3) and the concept of a
  manufacturer-declared hazard distance.
- IEC 60825-1:2014 — laser product classification; kept distinct from
  IEC 62471-5 risk groups per #627.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


OPTICAL_SAFETY_SCHEMA_VERSION = 'proj-optical-safety-1'
OPTICAL_SAFETY_EVALUATION_VERSION = 'proj-optical-safety-eval-1'

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


# ---------------------------------------------------------------------------
# Taxonomies (#627)
# ---------------------------------------------------------------------------

IlluminationSource = Literal[
    'laser', 'laser_hybrid', 'led', 'uhp_lamp', 'xenon_lamp',
    'other_lamp', 'unknown',
]

RiskGroup = Literal['rg0', 'rg1', 'rg2', 'rg3', 'unknown']
"""IEC 62471-5 risk group — a photobiological classification, NOT a
laser class."""

LaserClass = Literal[
    'class_1', 'class_1m', 'class_2', 'class_2m', 'class_3r',
    'class_3b', 'class_4', 'not_applicable', 'unknown',
]
"""IEC 60825-1 laser class — distinct quantity from RiskGroup."""

ViewerPosition = Literal[
    'no_audience_in_hazard_zone',
    'audience_below_hazard_zone',
    'audience_within_hazard_zone',
    'unknown',
]

OperatingState = Literal[
    'normal_operation',
    'low_power_mode',
    'test_pattern_alignment',
    'service_interlock_defeat',
    'service_open_housing',
    'unknown',
]
"""Service states are never audience-safe and never qualify an
installation."""

AccessibilityKind = Literal[
    'floor_standing',
    'seated',
    'standing',
    'catwalk',
    'adjacent_structure',
    'temporary_platform',
    'unknown',
]

SafetyVerdict = Literal[
    'installation_within_documented_constraints',
    'qualified_with_limitations',
    'safety_zone_conflict',
    'lens_accessory_applicability_unknown',
    'service_state_not_user_safe',
    'local_review_required',
    'insufficient_evidence',
    'stale_after_change',
]


# ---------------------------------------------------------------------------
# Safety identity
# ---------------------------------------------------------------------------


class CadProjectorSafetyIdentity(BaseModel):
    """The projector's documented optical-safety basis.

    ``risk_group`` (IEC 62471-5) and ``laser_class`` (IEC 60825-1) are
    independent fields pinned to their own sources where known —
    combining them into one value is a modelling error this schema
    refuses.
    """

    model_config = ConfigDict(frozen=True)

    identity_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    projector_ref: AuthorityRef | None = None
    manufacturer: str | None = None
    model: str | None = None
    hardware_revision: str | None = None
    illumination_source: IlluminationSource = 'unknown'
    risk_group: RiskGroup = 'unknown'
    risk_group_source: str | None = None
    laser_class: LaserClass = 'unknown'
    laser_class_source: str | None = None
    emission_notes: str | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    identity_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadProjectorSafetyIdentity':
        _require_iso8601(self.declared_at_utc, 'identity declared_at_utc')
        if self.projector_ref is not None and (
            self.projector_ref.ref_sha256 is None
        ):
            raise ValueError('projector_ref must pin its sha256')
        if self.risk_group != 'unknown' and not self.risk_group_source:
            raise ValueError(
                'a classified risk group requires its source document'
            )
        if self.laser_class not in ('unknown', 'not_applicable') and (
            not self.laser_class_source
        ):
            raise ValueError(
                'a classified laser class requires its source document'
            )
        expected = _hash(self.identity_payload())
        if self.identity_sha256 != expected:
            raise ValueError('safety identity hash mismatch')
        if self.identity_id != _semantic_id('pjsafe', expected):
            raise ValueError('identity id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'projector_ref': (
                self.projector_ref.model_dump(mode='json')
                if self.projector_ref is not None else None
            ),
            'manufacturer': self.manufacturer,
            'model': self.model,
            'hardware_revision': self.hardware_revision,
            'illumination_source': self.illumination_source,
            'risk_group': self.risk_group,
            'risk_group_source': self.risk_group_source,
            'laser_class': self.laser_class,
            'laser_class_source': self.laser_class_source,
            'emission_notes': self.emission_notes,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def safety_identity_binding(
    identity: CadProjectorSafetyIdentity,
) -> AuthorityRef:
    return AuthorityRef(
        kind='projector_safety_identity',
        ref_id=identity.identity_id,
        ref_sha256=identity.identity_sha256,
    )


# ---------------------------------------------------------------------------
# Manufacturer safety constraints
# ---------------------------------------------------------------------------


class CadHazardDistanceRule(BaseModel):
    """One manufacturer-declared hazard distance, scoped to the
    lens/accessory/optical configuration it was published for."""

    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(min_length=1)
    applies_to: str = Field(min_length=1)
    """Configuration scope, e.g. 'standard lens', 'ultra-short-throw
    accessory'. Not parsed — matched by the evaluator against the
    placement's declared accessory label."""
    hazard_distance_m: float
    restricted_below_m: float | None = None
    """Optional height restriction (e.g. 'no access below 2.5 m within
    the hazard distance')."""
    notes: str | None = None

    @model_validator(mode='after')
    def valid_rule(self) -> 'CadHazardDistanceRule':
        _require_finite(self.hazard_distance_m, 'hazard_distance_m')
        if self.hazard_distance_m < 0:
            raise ValueError('hazard distance must be non-negative')
        if self.restricted_below_m is not None:
            _require_finite(
                self.restricted_below_m, 'restricted_below_m'
            )
            if self.restricted_below_m < 0:
                raise ValueError('restricted height must be non-negative')
        return self


class CadManufacturerSafetyConstraints(BaseModel):
    """Manufacturer-documented optical-safety envelope.

    Every constraint is source-pinned. Undocumented quantities stay
    UNKNOWN — the evaluator then returns ``local_review_required`` /
    ``insufficient_evidence`` instead of improvising numbers.
    """

    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    safety_identity_ref: AuthorityRef | None = None
    hazard_distance_rules: tuple[CadHazardDistanceRule, ...] = ()
    prohibited_orientations: str | None = None
    audience_restrictions: str | None = None
    operator_restrictions: str | None = None
    lens_accessory_notes: str | None = None
    service_state_rules: str | None = None
    minimum_separation_m: float | None = None
    source_document: str = Field(min_length=1)
    source_revision: str | None = None
    source_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    constraint_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_constraints(self) -> 'CadManufacturerSafetyConstraints':
        _require_iso8601(
            self.declared_at_utc, 'safety constraints declared_at_utc'
        )
        if self.safety_identity_ref is not None and (
            self.safety_identity_ref.ref_sha256 is None
        ):
            raise ValueError('safety_identity_ref must pin its sha256')
        rule_ids = [r.rule_id for r in self.hazard_distance_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError('hazard distance rule ids must be unique')
        if self.minimum_separation_m is not None:
            _require_finite(
                self.minimum_separation_m, 'minimum_separation_m'
            )
            if self.minimum_separation_m < 0:
                raise ValueError(
                    'minimum separation must be non-negative'
                )
        expected = _hash(self.identity_payload())
        if self.constraint_sha256 != expected:
            raise ValueError('safety constraints hash mismatch')
        if self.constraint_id != _semantic_id('pjscons', expected):
            raise ValueError('constraint id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'safety_identity_ref': (
                self.safety_identity_ref.model_dump(mode='json')
                if self.safety_identity_ref is not None else None
            ),
            'hazard_distance_rules': [
                r.model_dump(mode='json')
                for r in self.hazard_distance_rules
            ],
            'prohibited_orientations': self.prohibited_orientations,
            'audience_restrictions': self.audience_restrictions,
            'operator_restrictions': self.operator_restrictions,
            'lens_accessory_notes': self.lens_accessory_notes,
            'service_state_rules': self.service_state_rules,
            'minimum_separation_m': self.minimum_separation_m,
            'source_document': self.source_document,
            'source_revision': self.source_revision,
            'source_sha256': self.source_sha256,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def safety_constraint_binding(
    constraints: CadManufacturerSafetyConstraints,
) -> AuthorityRef:
    return AuthorityRef(
        kind='manufacturer_safety_constraints',
        ref_id=constraints.constraint_id,
        ref_sha256=constraints.constraint_sha256,
    )


# ---------------------------------------------------------------------------
# Placement declaration
# ---------------------------------------------------------------------------


class CadAccessiblePosition(BaseModel):
    """One position a person can occupy relative to the projector beam."""

    model_config = ConfigDict(frozen=True)

    position_id: str = Field(min_length=1)
    kind: AccessibilityKind = 'unknown'
    distance_from_lens_m: float | None = None
    height_m: float | None = None
    in_beam_path: bool | None = None
    description: str | None = None

    @model_validator(mode='after')
    def valid_position(self) -> 'CadAccessiblePosition':
        for label, value in (
            ('distance_from_lens_m', self.distance_from_lens_m),
            ('height_m', self.height_m),
        ):
            if value is not None:
                _require_finite(value, f'position {label}')
                if value < 0:
                    raise ValueError(
                        f'position {label} must be non-negative'
                    )
        return self


class CadProjectorPlacementDeclaration(BaseModel):
    """The sealed placement a safety verdict applies to.

    ``lens_accessory`` names the lens/accessory configuration so the
    evaluator can match the correct hazard-distance rule; ``None`` means
    the base lens and stays honest (rules scoped to accessories then
    cannot match).
    """

    model_config = ConfigDict(frozen=True)

    placement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    projector_ref: AuthorityRef | None = None
    safety_identity_ref: AuthorityRef
    constraint_ref: AuthorityRef | None = None
    operating_state: OperatingState = 'normal_operation'
    throw_distance_m: float | None = None
    lens_accessory: str | None = None
    mount_orientation: str | None = None
    viewer_position: ViewerPosition = 'unknown'
    accessible_positions: tuple[CadAccessiblePosition, ...] = ()
    remote_power_capable: bool = False
    """Recorded but never a safety control — IP power-off does not
    protect eyes."""
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    placement_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_placement(self) -> 'CadProjectorPlacementDeclaration':
        _require_iso8601(self.declared_at_utc, 'placement declared_at_utc')
        if self.safety_identity_ref.ref_sha256 is None:
            raise ValueError('safety_identity_ref must pin its sha256')
        for ref, label in (
            (self.projector_ref, 'projector_ref'),
            (self.constraint_ref, 'constraint_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.throw_distance_m is not None:
            _require_finite(self.throw_distance_m, 'throw_distance_m')
            if self.throw_distance_m < 0:
                raise ValueError('throw distance must be non-negative')
        position_ids = [p.position_id for p in self.accessible_positions]
        if len(position_ids) != len(set(position_ids)):
            raise ValueError('accessible position ids must be unique')
        expected = _hash(self.identity_payload())
        if self.placement_sha256 != expected:
            raise ValueError('placement hash mismatch')
        if self.placement_id != _semantic_id('pjplace', expected):
            raise ValueError('placement id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'projector_ref': (
                self.projector_ref.model_dump(mode='json')
                if self.projector_ref is not None else None
            ),
            'safety_identity_ref': (
                self.safety_identity_ref.model_dump(mode='json')
            ),
            'constraint_ref': (
                self.constraint_ref.model_dump(mode='json')
                if self.constraint_ref is not None else None
            ),
            'operating_state': self.operating_state,
            'throw_distance_m': self.throw_distance_m,
            'lens_accessory': self.lens_accessory,
            'mount_orientation': self.mount_orientation,
            'viewer_position': self.viewer_position,
            'accessible_positions': [
                p.model_dump(mode='json')
                for p in self.accessible_positions
            ],
            'remote_power_capable': self.remote_power_capable,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def placement_binding(
    placement: CadProjectorPlacementDeclaration,
) -> AuthorityRef:
    return AuthorityRef(
        kind='projector_placement',
        ref_id=placement.placement_id,
        ref_sha256=placement.placement_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class CadOpticalSafetyEvaluation(BaseModel):
    """Sealed optical-safety verdict for one placement.

    ``zone_results`` carries one entry per matched hazard-distance rule
    with the measured comparison — the verdict logic is auditable and
    the raw comparison is never hidden inside the aggregate.
    """

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    placement_ref: AuthorityRef
    safety_identity_ref: AuthorityRef
    constraint_ref: AuthorityRef | None = None
    prior_evaluation_ref: AuthorityRef | None = None
    verdict: SafetyVerdict
    zone_results: tuple[dict[str, Any], ...] = ()
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'CadOpticalSafetyEvaluation':
        _require_iso8601(
            self.evaluated_at_utc, 'evaluation evaluated_at_utc'
        )
        for ref, label in (
            (self.placement_ref, 'placement_ref'),
            (self.safety_identity_ref, 'safety_identity_ref'),
            (self.constraint_ref, 'constraint_ref'),
            (self.prior_evaluation_ref, 'prior_evaluation_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('safety evaluation hash mismatch')
        if self.evaluation_id != _semantic_id('pjseval', expected):
            raise ValueError('evaluation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'placement_ref': self.placement_ref.model_dump(mode='json'),
            'safety_identity_ref': (
                self.safety_identity_ref.model_dump(mode='json')
            ),
            'constraint_ref': (
                self.constraint_ref.model_dump(mode='json')
                if self.constraint_ref is not None else None
            ),
            'prior_evaluation_ref': (
                self.prior_evaluation_ref.model_dump(mode='json')
                if self.prior_evaluation_ref is not None else None
            ),
            'verdict': self.verdict,
            'zone_results': list(self.zone_results),
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


def optical_safety_binding(
    evaluation: CadOpticalSafetyEvaluation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='optical_safety_evaluation',
        ref_id=evaluation.evaluation_id,
        ref_sha256=evaluation.evaluation_sha256,
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_safety_identity(**kwargs: Any) -> CadProjectorSafetyIdentity:
    """Seal a projector safety identity (risk group + laser class kept
    distinct, each source-pinned)."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadProjectorSafetyIdentity, dict(kwargs),
        'identity_id', 'identity_sha256', 'pjsafe',
    )


def build_safety_constraints(
    **kwargs: Any,
) -> CadManufacturerSafetyConstraints:
    """Seal manufacturer optical-safety constraints."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadManufacturerSafetyConstraints, dict(kwargs),
        'constraint_id', 'constraint_sha256', 'pjscons',
    )


def build_placement(
    *,
    document_id: str,
    safety_identity: CadProjectorSafetyIdentity | AuthorityRef,
    constraints: CadManufacturerSafetyConstraints | AuthorityRef | None = None,
    projector_ref: AuthorityRef | None = None,
    operating_state: OperatingState = 'normal_operation',
    throw_distance_m: float | None = None,
    lens_accessory: str | None = None,
    mount_orientation: str | None = None,
    viewer_position: ViewerPosition = 'unknown',
    accessible_positions: tuple[CadAccessiblePosition, ...] = (),
    remote_power_capable: bool = False,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadProjectorPlacementDeclaration:
    """Seal a projector placement declaration."""
    identity_ref = (
        safety_identity_binding(safety_identity)
        if isinstance(safety_identity, CadProjectorSafetyIdentity)
        else safety_identity
    )
    constraint_ref: AuthorityRef | None
    if isinstance(constraints, CadManufacturerSafetyConstraints):
        constraint_ref = safety_constraint_binding(constraints)
    else:
        constraint_ref = constraints
    payload = dict(
        document_id=document_id,
        projector_ref=projector_ref,
        safety_identity_ref=identity_ref,
        constraint_ref=constraint_ref,
        operating_state=operating_state,
        throw_distance_m=throw_distance_m,
        lens_accessory=lens_accessory,
        mount_orientation=mount_orientation,
        viewer_position=viewer_position,
        accessible_positions=accessible_positions,
        remote_power_capable=remote_power_capable,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadProjectorPlacementDeclaration, payload,
        'placement_id', 'placement_sha256', 'pjplace',
    )


def evaluate_optical_safety(
    *,
    document_id: str,
    placement: CadProjectorPlacementDeclaration,
    safety_identity: CadProjectorSafetyIdentity | None = None,
    constraints: CadManufacturerSafetyConstraints | None = None,
    prior_evaluation: CadOpticalSafetyEvaluation | None = None,
    evaluated_at_utc: str | None = None,
) -> CadOpticalSafetyEvaluation:
    """Fail-closed optical-safety verdict for one placement.

    Never invents distances: when the manufacturer did not document a
    hazard distance applicable to the placement's lens/accessory state,
    the verdict is ``local_review_required`` / ``insufficient_evidence``
    — an improvised computation is a worse outcome than a review flag.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    zone_results: list[dict[str, Any]] = []

    if safety_identity is not None and (
        placement.safety_identity_ref.ref_id
        != safety_identity.identity_id
        or placement.safety_identity_ref.ref_sha256
        != safety_identity.identity_sha256
    ):
        raise ValueError(
            'the supplied safety identity does not match the placement'
        )
    if constraints is not None and placement.constraint_ref is not None and (
        placement.constraint_ref.ref_id != constraints.constraint_id
        or placement.constraint_ref.ref_sha256
        != constraints.constraint_sha256
    ):
        raise ValueError(
            'the supplied safety constraints do not match the placement'
        )

    # Staleness: a prior verdict is void when the placement changed.
    if prior_evaluation is not None and (
        prior_evaluation.placement_ref.ref_sha256
        != placement.placement_sha256
    ):
        verdict: SafetyVerdict = 'stale_after_change'
        reasons.append(
            'placement geometry changed since the prior verdict — '
            'the earlier safety claim does not carry over'
        )
        return _seal_verdict(
            document_id=document_id,
            placement=placement,
            safety_identity=safety_identity,
            constraints=constraints,
            prior_evaluation=prior_evaluation,
            verdict=verdict,
            zone_results=zone_results,
            reasons=reasons,
            evaluated_at_utc=evaluated_at_utc,
        )

    # Service states are never audience-safe.
    if placement.operating_state in (
        'service_interlock_defeat', 'service_open_housing',
    ):
        verdict = 'service_state_not_user_safe'
        reasons.append(
            f'operating state {placement.operating_state} is a service '
            'mode — it cannot qualify an audience installation'
        )
        return _seal_verdict(
            document_id=document_id,
            placement=placement,
            safety_identity=safety_identity,
            constraints=constraints,
            prior_evaluation=prior_evaluation,
            verdict=verdict,
            zone_results=zone_results,
            reasons=reasons,
            evaluated_at_utc=evaluated_at_utc,
        )

    if safety_identity is None:
        verdict = 'insufficient_evidence'
        reasons.append(
            'no safety identity bound — risk group and laser class are '
            'unknown and the placement cannot be qualified'
        )
        return _seal_verdict(
            document_id=document_id,
            placement=placement,
            safety_identity=None,
            constraints=constraints,
            prior_evaluation=prior_evaluation,
            verdict=verdict,
            zone_results=zone_results,
            reasons=reasons,
            evaluated_at_utc=evaluated_at_utc,
        )

    if safety_identity.risk_group == 'unknown' and (
        safety_identity.laser_class == 'unknown'
    ):
        verdict = 'insufficient_evidence'
        reasons.append(
            'neither risk group (IEC 62471-5) nor laser class '
            '(IEC 60825-1) is documented'
        )
        return _seal_verdict(
            document_id=document_id,
            placement=placement,
            safety_identity=safety_identity,
            constraints=constraints,
            prior_evaluation=prior_evaluation,
            verdict=verdict,
            zone_results=zone_results,
            reasons=reasons,
            evaluated_at_utc=evaluated_at_utc,
        )

    if constraints is None:
        verdict = 'local_review_required'
        reasons.append(
            'no manufacturer safety constraints bound — HTDT returns a '
            'review state rather than improvising a safe distance'
        )
        if (
            safety_identity.risk_group == 'rg3'
            or safety_identity.laser_class in ('class_3b', 'class_4')
        ):
            reasons.append(
                'high-risk classification without documented hazard '
                'geometry — professional review mandatory'
            )
        return _seal_verdict(
            document_id=document_id,
            placement=placement,
            safety_identity=safety_identity,
            constraints=constraints,
            prior_evaluation=prior_evaluation,
            verdict=verdict,
            zone_results=zone_results,
            reasons=reasons,
            evaluated_at_utc=evaluated_at_utc,
        )

    # Lens/accessory applicability: pick rules whose applies_to scope
    # matches the placement's declared accessory. No fuzzy matching —
    # 'standard lens' never silently covers an anamorphic adapter.
    applicable_rules = [
        rule for rule in constraints.hazard_distance_rules
        if placement.lens_accessory is not None
        and rule.applies_to == placement.lens_accessory
    ]
    base_rules = [
        rule for rule in constraints.hazard_distance_rules
        if rule.applies_to in ('base', 'standard', 'all')
    ]
    matched_rules = applicable_rules or (
        base_rules if placement.lens_accessory is None else []
    )

    if placement.lens_accessory is not None and not applicable_rules:
        verdict = 'lens_accessory_applicability_unknown'
        reasons.append(
            f'lens/accessory "{placement.lens_accessory}" has no '
            'manufacturer hazard-distance rule — its applicability is '
            'undocumented'
        )
        return _seal_verdict(
            document_id=document_id,
            placement=placement,
            safety_identity=safety_identity,
            constraints=constraints,
            prior_evaluation=prior_evaluation,
            verdict=verdict,
            zone_results=zone_results,
            reasons=reasons,
            evaluated_at_utc=evaluated_at_utc,
        )

    if not matched_rules:
        verdict = 'local_review_required'
        reasons.append(
            'manufacturer documentation does not declare a hazard '
            'distance for this configuration — a review is required '
            'rather than an improvised computation'
        )
        return _seal_verdict(
            document_id=document_id,
            placement=placement,
            safety_identity=safety_identity,
            constraints=constraints,
            prior_evaluation=prior_evaluation,
            verdict=verdict,
            zone_results=zone_results,
            reasons=reasons,
            evaluated_at_utc=evaluated_at_utc,
        )

    # Zone check: every accessible position must be outside every
    # applicable hazard distance (or above the restricted height).
    conflict = False
    positions_unknown = False
    for rule in matched_rules:
        for pos in placement.accessible_positions:
            if pos.distance_from_lens_m is None or pos.in_beam_path is None:
                positions_unknown = True
                zone_results.append({
                    'rule_id': rule.rule_id,
                    'position_id': pos.position_id,
                    'result': 'unknown',
                    'reason': (
                        'position distance or beam-path membership '
                        'undeclared'
                    ),
                })
                continue
            if not pos.in_beam_path:
                zone_results.append({
                    'rule_id': rule.rule_id,
                    'position_id': pos.position_id,
                    'result': 'outside_beam_path',
                })
                continue
            inside_distance = (
                pos.distance_from_lens_m < rule.hazard_distance_m
            )
            below_restricted = (
                rule.restricted_below_m is not None
                and pos.height_m is not None
                and pos.height_m < rule.restricted_below_m
            )
            if inside_distance and (
                rule.restricted_below_m is None or below_restricted
            ):
                conflict = True
                zone_results.append({
                    'rule_id': rule.rule_id,
                    'position_id': pos.position_id,
                    'result': 'conflict',
                    'distance_from_lens_m': pos.distance_from_lens_m,
                    'hazard_distance_m': rule.hazard_distance_m,
                })
                reasons.append(
                    f'position {pos.position_id} is '
                    f'{pos.distance_from_lens_m} m from the lens inside '
                    f'the {rule.hazard_distance_m} m hazard zone '
                    f'(rule {rule.rule_id})'
                )
            elif inside_distance:
                zone_results.append({
                    'rule_id': rule.rule_id,
                    'position_id': pos.position_id,
                    'result': 'conflict',
                    'distance_from_lens_m': pos.distance_from_lens_m,
                    'hazard_distance_m': rule.hazard_distance_m,
                })
                conflict = True
            else:
                zone_results.append({
                    'rule_id': rule.rule_id,
                    'position_id': pos.position_id,
                    'result': 'clear',
                    'distance_from_lens_m': pos.distance_from_lens_m,
                    'hazard_distance_m': rule.hazard_distance_m,
                })

    if placement.viewer_position == 'audience_within_hazard_zone':
        conflict = True
        reasons.append(
            'audience position declared inside the hazard zone'
        )
    elif placement.viewer_position == 'unknown':
        positions_unknown = True

    if conflict:
        verdict = 'safety_zone_conflict'
    elif positions_unknown or not placement.accessible_positions:
        verdict = 'qualified_with_limitations'
        reasons.append(
            'no documented conflict, but accessible positions or the '
            'audience relation are not fully declared — the verdict '
            'covers only the declared geometry'
        )
    else:
        verdict = 'installation_within_documented_constraints'

    if placement.remote_power_capable:
        reasons.append(
            'remote power-off capability noted but not credited — '
            'IP control is not a safety control'
        )

    return _seal_verdict(
        document_id=document_id,
        placement=placement,
        safety_identity=safety_identity,
        constraints=constraints,
        prior_evaluation=prior_evaluation,
        verdict=verdict,
        zone_results=zone_results,
        reasons=reasons,
        evaluated_at_utc=evaluated_at_utc,
    )


def _seal_verdict(
    *,
    document_id: str,
    placement: CadProjectorPlacementDeclaration,
    safety_identity: CadProjectorSafetyIdentity | None,
    constraints: CadManufacturerSafetyConstraints | None,
    prior_evaluation: CadOpticalSafetyEvaluation | None,
    verdict: SafetyVerdict,
    zone_results: list[dict[str, Any]],
    reasons: list[str],
    evaluated_at_utc: str,
) -> CadOpticalSafetyEvaluation:
    identity_ref = (
        safety_identity_binding(safety_identity)
        if safety_identity is not None
        else placement.safety_identity_ref
    )
    payload = dict(
        document_id=document_id,
        placement_ref=placement_binding(placement),
        safety_identity_ref=identity_ref,
        constraint_ref=(
            safety_constraint_binding(constraints)
            if constraints is not None
            else placement.constraint_ref
        ),
        prior_evaluation_ref=(
            optical_safety_binding(prior_evaluation)
            if prior_evaluation is not None else None
        ),
        verdict=verdict,
        zone_results=tuple(zone_results),
        reasons=tuple(reasons),
        evaluation_version=OPTICAL_SAFETY_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadOpticalSafetyEvaluation, payload,
        'evaluation_id', 'evaluation_sha256', 'pjseval',
    )


__all__ = [
    'OPTICAL_SAFETY_EVALUATION_VERSION',
    'OPTICAL_SAFETY_SCHEMA_VERSION',
    'AccessibilityKind',
    'CadAccessiblePosition',
    'CadHazardDistanceRule',
    'CadManufacturerSafetyConstraints',
    'CadOpticalSafetyEvaluation',
    'CadProjectorPlacementDeclaration',
    'CadProjectorSafetyIdentity',
    'IlluminationSource',
    'LaserClass',
    'OperatingState',
    'RiskGroup',
    'SafetyVerdict',
    'ViewerPosition',
    'build_placement',
    'build_safety_constraints',
    'build_safety_identity',
    'evaluate_optical_safety',
    'optical_safety_binding',
    'placement_binding',
    'safety_constraint_binding',
    'safety_identity_binding',
]
