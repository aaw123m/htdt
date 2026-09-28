"""Bass-management authority (#633).

Models frequency-dependent bass routing as a durable, versioned system
configuration — separate from channel-level routing, separate from any
excitation scenario, and separate from the calibration plan lifecycle.

Three layers the issue requires, and where they live:

1. :class:`BassManagementProfile` (this module) — the persistent authority:
   per-channel high-pass + redirected-bass destinations, an independent LFE
   path, physical subwoofer output groups, and vendor-term adapter mappings.
2. Excitation scenarios — referenced by id (``excitation_scenario_id``) and
   owned elsewhere; this profile only names the scenario it was configured
   under, it never embeds one.
3. Calibration-plan lifecycle — represented by the ``lifecycle`` field
   (``current``/``proposed``/``applied``) plus ``supersedes`` chaining, so a
   proposed profile never silently becomes the applied one.

Key contract details honoured here:

- Crossover semantics are explicit: :class:`CrossoverSpec` records the corner
  frequency and, when known, the exact filter family/slope. Unknown filters
  stay ``filter_family='unknown'`` — no Linkwitz-Riley curve is invented.
- Vendor terms (Small/Large/LFE+Main/double-bass) are *adapter mappings
  only*: :class:`VendorBassMapping` binds a vendor setting label to a
  canonical interpretation through a named, versioned adapter. A capture of
  "Fronts = Small" means nothing in the twin until a verified adapter has
  mapped it.
- The LFE path is independent of redirected bass: it has its own input id,
  low-pass, gain reference, destinations and duplication policy — it is not
  the sum of the mains' high-passed content.
- Physical sub grouping is explicit: one output may drive many subs
  (``single_output_multi_sub``), outputs may be independent, or an external
  DSP may sit between — recorded on :class:`SubwooferOutputGroup`.
- Partial knowledge is a first-class state: missing crossovers or
  destinations produce UNKNOWN evaluation results, never fabricated values.
- ``routing_edges()`` renders the profile as ``(source, destination, band)``
  triples so a diagram layer can draw the same semantics the twin stores.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




FilterFamily = Literal[
    'butterworth',
    'linkwitz_riley',
    'bessel',
    'custom',
    'unknown',
]
"""Exact filter family; ``unknown`` when the device does not say — never
assumed Linkwitz-Riley."""

BassHandling = Literal['full_range', 'high_pass', 'unknown']
"""Per-channel bass handling: ``full_range`` (no HPF), ``high_pass`` (HPF +
redirect), or ``unknown`` when unrecorded."""

BassLifecycle = Literal['proposed', 'current', 'applied']
"""Profile lifecycle: ``proposed`` (draft), ``current`` (the live reference
state of the system), ``applied`` (verified pushed to the processor)."""

SubGroupingKind = Literal[
    'single_output_multi_sub',
    'independent_output',
    'external_dsp',
    'unknown',
]

LFEDuplicationPolicy = Literal['shared', 'independent', 'unknown']
"""Whether multiple sub destinations receive the same LFE signal
(``shared``) or distinct feeds (``independent``)."""


class CrossoverSpec(BaseModel):
    """One explicit crossover. ``filter_family='unknown'`` + no slope means
    only the corner frequency is known — the curve is not synthesised."""

    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    slope_db_per_octave: float | None = Field(default=None, gt=0.0)
    filter_family: FilterFamily = 'unknown'
    custom_label: str | None = None


class FrequencyBand(BaseModel):
    model_config = ConfigDict(frozen=True)

    low_hz: float = Field(ge=0.0)
    high_hz: float = Field(gt=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'FrequencyBand':
        if self.low_hz >= self.high_hz:
            raise ValueError('band low_hz must be below high_hz')
        return self


class MainChannelBassRule(BaseModel):
    """Bass handling for one logical channel role.

    ``redirected_destinations`` lists physical sub group ids and/or output
    channel refs that receive this channel's redirected low band. It may be
    empty for partial knowledge — evaluation reports UNKNOWN rather than
    assuming the LFE output.
    """

    model_config = ConfigDict(frozen=True)

    logical_role_id: str = Field(min_length=1)
    handling: BassHandling = 'unknown'
    high_pass: CrossoverSpec | None = None
    redirected_low_band: FrequencyBand | None = None
    redirected_destinations: tuple[str, ...] = ()
    transition_notes: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'MainChannelBassRule':
        if self.handling == 'high_pass' and self.high_pass is None:
            raise ValueError(
                'high_pass handling requires an explicit crossover spec'
            )
        if self.handling == 'full_range':
            if self.high_pass is not None or self.redirected_destinations:
                raise ValueError(
                    'full_range channels carry no high-pass or redirected '
                    'bass'
                )
        return self


class LFEPathRule(BaseModel):
    """The discrete LFE (.1) path — independent of redirected bass."""

    model_config = ConfigDict(frozen=True)

    lfe_input_id: str = Field(min_length=1)
    low_pass: CrossoverSpec | None = None
    gain_reference_db: float | None = None
    in_band_boost_db: float | None = None
    destinations: tuple[str, ...] = ()
    duplication_policy: LFEDuplicationPolicy = 'unknown'
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class SubwooferOutputGroup(BaseModel):
    """Physical subwoofers sharing (or owned by) one output."""

    model_config = ConfigDict(frozen=True)

    group_id: str = Field(min_length=1)
    grouping: SubGroupingKind = 'unknown'
    output_channel_ref: str | None = None
    member_sub_ids: tuple[str, ...] = ()
    external_dsp_ref: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'SubwooferOutputGroup':
        ids = list(self.member_sub_ids)
        if len(set(ids)) != len(ids):
            raise ValueError('sub group must not duplicate member ids')
        if self.grouping == 'external_dsp' and self.external_dsp_ref is None:
            raise ValueError('external_dsp grouping requires a DSP ref')
        return self


class VendorBassMapping(BaseModel):
    """A vendor setting (Small/Large/LFE+Main/…) mapped to canonical meaning.

    This is an adapter output record, not a claim — ``adapter_id`` and
    ``adapter_version`` identify the verified mapping that produced it.
    """

    model_config = ConfigDict(frozen=True)

    vendor_setting: str = Field(min_length=1)
    applies_to_role_id: str | None = None
    canonical_handling: BassHandling
    canonical_redirected_destinations: tuple[str, ...] = ()
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    verified: bool = False
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class BassManagementProfile(BaseModel):
    """The persistent bass-management authority for one processor."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['bass-management-1'] = 'bass-management-1'
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    processor_ref: str | None = None
    lifecycle: BassLifecycle = 'proposed'
    supersedes_profile_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    excitation_scenario_id: str | None = None
    main_rules: tuple[MainChannelBassRule, ...] = ()
    lfe_path: LFEPathRule | None = None
    sub_groups: tuple[SubwooferOutputGroup, ...] = ()
    vendor_mappings: tuple[VendorBassMapping, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'BassManagementProfile':
        roles = [r.logical_role_id for r in self.main_rules]
        if len(set(roles)) != len(roles):
            raise ValueError(
                'each logical role may have at most one bass rule'
            )
        groups = [g.group_id for g in self.sub_groups]
        if len(set(groups)) != len(groups):
            raise ValueError('sub group ids must be unique')
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError('bass management profile semantic hash mismatch')
        return self


def build_bass_management_profile(
    *,
    profile_id: str,
    version: str,
    lifecycle: BassLifecycle = 'proposed',
    processor_ref: str | None = None,
    supersedes_profile_sha256: str | None = None,
    excitation_scenario_id: str | None = None,
    main_rules: tuple[MainChannelBassRule, ...] = (),
    lfe_path: LFEPathRule | None = None,
    sub_groups: tuple[SubwooferOutputGroup, ...] = (),
    vendor_mappings: tuple[VendorBassMapping, ...] = (),
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> BassManagementProfile:
    probe = BassManagementProfile.model_construct(**canonicalize_payload(BassManagementProfile, dict(
        profile_id=profile_id,
        version=version,
        processor_ref=processor_ref,
        lifecycle=lifecycle,
        supersedes_profile_sha256=supersedes_profile_sha256,
        excitation_scenario_id=excitation_scenario_id,
        main_rules=tuple(main_rules),
        lfe_path=lfe_path,
        sub_groups=tuple(sub_groups),
        vendor_mappings=tuple(vendor_mappings),
        provenance=tuple(provenance),
        profile_sha256='',
    )))
    return BassManagementProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


class RoutingEdge(BaseModel):
    """One (source → destination, band) edge for routing diagrams."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    band: Literal['redirected_low', 'lfe', 'full_band']


def routing_edges(profile: BassManagementProfile) -> tuple[RoutingEdge, ...]:
    """Flatten the profile into diagram edges — the same semantics the
    authority stores, in drawable form.

    Only *recorded* edges appear: a high-pass rule with no redirected
    destination produces no edge, which consumers must read as incomplete
    routing knowledge (see ``redirect_destination_recorded``), never as
    intentional silence.
    """

    edges: list[RoutingEdge] = []
    for rule in profile.main_rules:
        for destination in rule.redirected_destinations:
            edges.append(
                RoutingEdge(
                    source_id=rule.logical_role_id,
                    destination_id=destination,
                    band='redirected_low',
                )
            )
        if rule.handling == 'full_range':
            edges.append(
                RoutingEdge(
                    source_id=rule.logical_role_id,
                    destination_id=rule.logical_role_id,
                    band='full_band',
                )
            )
    if profile.lfe_path is not None:
        for destination in profile.lfe_path.destinations:
            edges.append(
                RoutingEdge(
                    source_id=profile.lfe_path.lfe_input_id,
                    destination_id=destination,
                    band='lfe',
                )
            )
    return tuple(edges)


class BassCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    role_id: str | None = None
    reason: str | None = None


class BassManagementEvaluation(BaseModel):
    """Per-channel and per-path commissioning evaluation."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    profile: BassManagementProfile
    checks: tuple[BassCheckResult, ...]
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'BassManagementEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('bass management evaluation hash mismatch')
        if self.evaluation_id != 'bme-' + digest[:24]:
            raise ValueError('bass management evaluation id mismatch')
        return self


def evaluate_bass_management(
    *,
    profile: BassManagementProfile,
    expected_role_ids: tuple[str, ...] = (),
    known_destination_ids: tuple[str, ...] = (),
) -> BassManagementEvaluation:
    """Evaluate coverage and resolvability of a bass-management profile.

    - ``role_coverage``: every expected channel role has a rule (UNKNOWN if
      the caller supplies no expectation).
    - per-rule ``high_pass_recorded`` for unrecorded handling; for a
      ``high_pass`` rule ``redirect_destination_recorded`` and
      ``redirect_destination_resolves`` are separate checks — an empty
      destination set is incomplete knowledge (UNKNOWN), never a resolved
      route, and only a recorded destination can resolve.
    - ``lfe_destination_resolves`` / ``lfe_duplication_policy_known``: LFE
      destination resolution is proven separately from shared-routing
      semantics — resolvable destinations alone do not establish that the
      .1 path is routed independently of redirected bass.
    """

    checks: list[BassCheckResult] = []
    known = set(known_destination_ids) | {
        g.group_id for g in profile.sub_groups
    }

    if not expected_role_ids:
        checks.append(
            BassCheckResult(
                check='role_coverage',
                status='UNKNOWN',
                reason='no expected role list supplied',
            )
        )
    else:
        covered = {r.logical_role_id for r in profile.main_rules}
        missing = sorted(set(expected_role_ids) - covered)
        checks.append(
            BassCheckResult(
                check='role_coverage',
                status='PASS' if not missing else 'FAIL',
                reason=(
                    'all expected roles have bass rules'
                    if not missing
                    else 'missing rules for: ' + ', '.join(missing)
                ),
            )
        )

    for rule in profile.main_rules:
        if rule.handling == 'unknown':
            checks.append(
                BassCheckResult(
                    check='high_pass_recorded',
                    status='UNKNOWN',
                    role_id=rule.logical_role_id,
                    reason='bass handling not recorded',
                )
            )
            continue
        if rule.handling == 'high_pass':
            recorded = bool(rule.redirected_destinations)
            checks.append(
                BassCheckResult(
                    check='redirect_destination_recorded',
                    status='PASS' if recorded else 'UNKNOWN',
                    role_id=rule.logical_role_id,
                    reason=(
                        'redirected bass destination is recorded'
                        if recorded
                        else 'redirected bass has no destination recorded'
                    ),
                )
            )
            unresolved = [
                d for d in rule.redirected_destinations
                if d not in known
            ]
            checks.append(
                BassCheckResult(
                    check='redirect_destination_resolves',
                    status=(
                        'UNKNOWN'
                        if not recorded
                        else ('FAIL' if unresolved else 'PASS')
                    ),
                    role_id=rule.logical_role_id,
                    reason=(
                        'no destination recorded to resolve'
                        if not recorded
                        else (
                            'unresolved destinations: ' + ', '.join(unresolved)
                            if unresolved
                            else 'redirected bass destinations resolve'
                        )
                    ),
                )
            )

    if profile.lfe_path is None:
        checks.append(
            BassCheckResult(
                check='lfe_destination_resolves',
                status='UNKNOWN',
                reason='no LFE path recorded',
            )
        )
        checks.append(
            BassCheckResult(
                check='lfe_duplication_policy_known',
                status='UNKNOWN',
                reason='no LFE path recorded',
            )
        )
    else:
        lfe_dests = set(profile.lfe_path.destinations)
        if not lfe_dests:
            checks.append(
                BassCheckResult(
                    check='lfe_destination_resolves',
                    status='UNKNOWN',
                    reason='LFE path has no destinations recorded',
                )
            )
        else:
            unresolved = [d for d in lfe_dests if d not in known]
            checks.append(
                BassCheckResult(
                    check='lfe_destination_resolves',
                    status='FAIL' if unresolved else 'PASS',
                    reason=(
                        'unresolved LFE destinations: ' + ', '.join(unresolved)
                        if unresolved
                        else 'LFE destinations resolve'
                    ),
                )
            )
        checks.append(
            BassCheckResult(
                check='lfe_duplication_policy_known',
                status=(
                    'UNKNOWN'
                    if profile.lfe_path.duplication_policy == 'unknown'
                    else 'PASS'
                ),
                reason=(
                    'LFE duplication/shared-routing policy not recorded'
                    if profile.lfe_path.duplication_policy == 'unknown'
                    else 'LFE duplication policy is '
                    f"'{profile.lfe_path.duplication_policy}'"
                ),
            )
        )

    probe = BassManagementEvaluation.model_construct(**canonicalize_payload(BassManagementEvaluation, dict(
        evaluation_id='',
        profile=profile,
        checks=tuple(checks),
        evaluation_sha256='',
    )))
    digest = _hash(probe.semantic_payload())
    return BassManagementEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='bme-' + digest[:24],
        evaluation_sha256=digest,
    )


def bass_management_status(
    evaluation: BassManagementEvaluation,
) -> EvaluationStatus:
    return _combine_status(tuple(c.status for c in evaluation.checks))
