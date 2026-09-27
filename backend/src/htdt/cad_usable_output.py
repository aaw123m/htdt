"""Speaker/source usable-output authority (#648).

``SplCapability`` on ``EquipmentDefinition`` stays what it is — a *declared*
level figure. This module adds the separate, level-dependent reality: how
output degrades with drive level (compression), how distortion rises, and at
what frequencies — as a distinct authority so a datasheet number is never
silently upgraded into a measured capability.

- :class:`SourceUsableOutputProfile` — versioned, self-hashed authority bound
  to an ``EquipmentDefinition`` (id + version + sha triple). Carries
  frequency/band samples each holding the produced level, compression versus
  an explicit reference baseline, and per-sample distortion evidence. The
  profile records its mounting/reference condition, measurement distance,
  environment, excitation method and its valid domain — anechoic half-space
  data is never applied to an in-room claim without saying so.
- :class:`UsableOutputTier` — an explicit capability ladder:
  ``unknown`` → ``scalar`` (one level figure) → ``curve`` (level vs
  frequency) → ``compression`` → ``thd`` → ``combined`` →
  ``excursion_model``. :func:`usable_output_tier` reports the tier a profile
  actually reaches — headroom evaluation always shows the tier used.
- Compression is always versus a declared reference level on the profile
  (``compression_reference_db_spl``); a bare "compressed X dB" is
  meaningless without it.
- :class:`DistortionMetric` distinguishes THD / per-harmonic / aggregate /
  unavailable — ``unavailable`` is recorded, not silently dropped.
- Duration classes are distinct: ``continuous``, ``burst`` and ``thermal``
  samples (e.g. CEA-2010 bursts for subwoofers) live side by side; a burst
  number never substitutes for continuous capability.
- An active limiter is opaque: ``limiter_state`` records its existence and
  policy, never a modelled transfer function. A passive speaker + amplifier
  pair reports the *earliest limiter* — the element that clips first —
  rather than a fused figure.
- Xmax-style excursion evidence is retained verbatim
  (:class:`ExcursionEvidence`) but no SPL is derived from it without an
  excursion model (tier ``excursion_model``).
- :func:`evaluate_headroom` distinguishes amplifier margin, scalar declared
  SPL, distortion-qualified output and UNKNOWN — and always names the tier
  and the reference axis the claim applies to.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash




UsableOutputTier = Literal[
    'unknown',
    'scalar',
    'curve',
    'compression',
    'thd',
    'combined',
    'excursion_model',
]
"""Capability ladder. Higher tiers subsume lower ones:

- ``unknown``: no usable-output data.
- ``scalar``: at least one level figure exists.
- ``curve``: level vs frequency/band samples exist.
- ``compression``: compression-vs-level data exists (needs a reference
  baseline).
- ``thd``: distortion metrics exist per sample.
- ``combined``: compression + distortion together.
- ``excursion_model``: an excursion-limited model is attached, which is the
  only tier allowed to derive SPL from Xmax-style data.
"""

OutputDurationClass = Literal['continuous', 'burst', 'thermal']
"""How a measured level was sustained. Burst datasets (e.g. CEA-2010 tone
bursts for subwoofers) are never treated as continuous capability."""

DistortionKind = Literal['thd', 'harmonic', 'aggregate', 'unavailable']

HeadroomBasis = Literal[
    'amplifier_margin',
    'scalar_declared',
    'distortion_qualified',
    'distortion_unqualified',
    'unknown',
]
"""What an evaluated headroom number actually rests on — always reported
alongside the number. ``distortion_unqualified`` means the supplied policy
was applied and no measured sample met it: no qualified available level
exists, whatever level evidence remains is diagnostic only."""

ReferenceBasis = Literal[
    'same_reference',
    'measured_room_transfer',
    'propagation_model',
    'predicted_transfer',
]
"""The authority kind under which a source-reference level is compared to a
listener/seat target. Nothing is ever compared across bases implicitly —
there is no implicit free-field 20log10(r) correction in-room."""

UsableOutputBindingClass = Literal['exact', 'advisory']
"""How a profile binds its EquipmentDefinition (#1026):

- ``exact``: id + version + sha256 triple — the only binding allowed to
  drive authoritative consumers such as O100 hard-headroom objectives.
- ``advisory``: definition referenced by id only — useful context, never a
  hard-capability authority.
"""


class ExcursionEvidence(BaseModel):
    """Xmax-style evidence, retained verbatim. No SPL is derived from it
    unless a profile reaches the ``excursion_model`` tier."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['xmax', 'xmech', 'xdamage', 'other'] = 'xmax'
    value_mm: float = Field(gt=0.0)
    direction: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class DistortionMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: DistortionKind
    percent: float | None = Field(default=None, ge=0.0)
    harmonic_order: int | None = Field(default=None, ge=2)

    @model_validator(mode='after')
    def _check(self) -> 'DistortionMetric':
        if self.kind == 'unavailable' and self.percent is not None:
            raise ValueError('unavailable distortion carries no value')
        if self.kind == 'harmonic' and self.harmonic_order is None:
            raise ValueError('harmonic distortion requires its order')
        if self.kind != 'unavailable' and self.percent is None:
            raise ValueError('a distortion metric requires percent')
        return self


class OutputSample(BaseModel):
    """One frequency/band operating point of usable output."""

    model_config = ConfigDict(frozen=True)

    frequency_hz: float | None = Field(default=None, gt=0.0)
    band: FrequencyDomain | None = None
    level_db_spl: float
    duration_class: OutputDurationClass = 'continuous'
    compression_db: float | None = Field(default=None, ge=0.0)
    distortion: tuple[DistortionMetric, ...] = ()
    limiter_engaged: bool | None = None

    @model_validator(mode='after')
    def _check(self) -> 'OutputSample':
        if self.frequency_hz is None and self.band is None:
            raise ValueError('a sample needs a frequency or a band')
        return self


class LimiterState(BaseModel):
    """Active-limiter declaration — existence and policy only, never a
    modelled curve."""

    model_config = ConfigDict(frozen=True)

    present: bool
    policy_label: str | None = None
    opaque: bool = True


class SourceUsableOutputProfile(BaseModel):
    """Level-dependent usable output for one equipment definition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['usable-output-1'] = 'usable-output-1'
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str | None = None
    equipment_definition_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    mounting_condition: str | None = None
    reference_condition: str | None = None
    measurement_distance_m: float | None = Field(default=None, gt=0.0)
    reference_axis: str = 'on_axis'
    environment: str | None = None
    excitation_method: str | None = None
    compression_reference_db_spl: float | None = None
    samples: tuple[OutputSample, ...] = ()
    limiter_state: LimiterState | None = None
    excursion_evidence: tuple[ExcursionEvidence, ...] = ()
    has_excursion_model: bool = False
    valid_domain: str | None = None
    uncertainty_db: float | None = Field(default=None, ge=0.0)
    raw_asset_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    parser_id: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @property
    def binding_class(self) -> UsableOutputBindingClass:
        """#1026: exact id+version+sha256 binding versus id-only advisory."""
        if (
            self.equipment_definition_version is not None
            and self.equipment_definition_sha256 is not None
        ):
            return 'exact'
        return 'advisory'

    @model_validator(mode='after')
    def _check(self) -> 'SourceUsableOutputProfile':
        has_compression = any(
            s.compression_db is not None for s in self.samples
        )
        if has_compression and self.compression_reference_db_spl is None:
            raise ValueError(
                'compression figures require an explicit reference level '
                '(compression_reference_db_spl)'
            )
        if (self.equipment_definition_version is None) != (
            self.equipment_definition_sha256 is None
        ):
            raise ValueError(
                'equipment definition version and sha256 must be supplied '
                'together (#1026)'
            )
        if (self.raw_asset_sha256 is not None) != (
            self.parser_id is not None
        ):
            raise ValueError(
                'raw asset hash and parser id must be supplied together'
            )
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError('usable output profile semantic hash mismatch')
        return self


def build_source_usable_output_profile(
    *,
    profile_id: str,
    version: str,
    equipment_definition_id: str,
    equipment_definition_version: str | None = None,
    equipment_definition_sha256: str | None = None,
    mounting_condition: str | None = None,
    reference_condition: str | None = None,
    measurement_distance_m: float | None = None,
    reference_axis: str = 'on_axis',
    environment: str | None = None,
    excitation_method: str | None = None,
    compression_reference_db_spl: float | None = None,
    samples: tuple[OutputSample, ...] = (),
    limiter_state: LimiterState | None = None,
    excursion_evidence: tuple[ExcursionEvidence, ...] = (),
    has_excursion_model: bool = False,
    valid_domain: str | None = None,
    uncertainty_db: float | None = None,
    raw_asset_sha256: str | None = None,
    parser_id: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> SourceUsableOutputProfile:
    probe = SourceUsableOutputProfile.model_construct(
        profile_id=profile_id,
        version=version,
        equipment_definition_id=equipment_definition_id,
        equipment_definition_version=equipment_definition_version,
        equipment_definition_sha256=equipment_definition_sha256,
        mounting_condition=mounting_condition,
        reference_condition=reference_condition,
        measurement_distance_m=measurement_distance_m,
        reference_axis=reference_axis,
        environment=environment,
        excitation_method=excitation_method,
        compression_reference_db_spl=compression_reference_db_spl,
        samples=tuple(samples),
        limiter_state=limiter_state,
        excursion_evidence=tuple(excursion_evidence),
        has_excursion_model=has_excursion_model,
        valid_domain=valid_domain,
        uncertainty_db=uncertainty_db,
        raw_asset_sha256=raw_asset_sha256,
        parser_id=parser_id,
        provenance=tuple(provenance),
        profile_sha256='',
    )
    return SourceUsableOutputProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


def usable_output_tier(
    profile: SourceUsableOutputProfile | None,
) -> UsableOutputTier:
    """The capability tier a profile actually reaches."""

    if profile is None or not profile.samples:
        return 'unknown'
    if profile.has_excursion_model:
        return 'excursion_model'
    has_compression = any(
        s.compression_db is not None for s in profile.samples
    )
    has_thd = any(
        any(d.kind != 'unavailable' for d in s.distortion)
        for s in profile.samples
    )
    if has_compression and has_thd:
        return 'combined'
    if has_compression:
        return 'compression'
    if has_thd:
        return 'thd'
    if len(profile.samples) > 1:
        return 'curve'
    return 'scalar'


class ListenerTransferAuthority(BaseModel):
    """Explicit authority that maps a source-reference SPL onto a listener
    position (#821).

    ``same_reference`` asserts the target is defined at the profile's own
    reference condition (same distance/axis/environment) — the evaluator
    verifies the recorded conditions actually coincide. Every other kind
    carries the authority's own ``transfer_db`` (the level shift from the
    profile reference point to the listener it establishes; negative =
    attenuation) plus its model/version and applicability limits. The
    evaluator consumes the mapped level; it never derives a free-field
    correction itself.
    """

    model_config = ConfigDict(frozen=True)

    kind: ReferenceBasis
    reference_distance_m: float | None = Field(default=None, gt=0.0)
    listener_distance_m: float | None = Field(default=None, gt=0.0)
    listener_axis: str | None = None
    directivity_authority: str | None = None
    propagation_model: str | None = None
    model_version: str | None = None
    environment: str | None = None
    applicability_limits: str | None = None
    transfer_db: float | None = None
    uncertainty_db: float | None = Field(default=None, ge=0.0)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'ListenerTransferAuthority':
        if self.kind == 'same_reference':
            if self.transfer_db is not None:
                raise ValueError(
                    'same_reference authority carries no level shift'
                )
        else:
            if self.transfer_db is None:
                raise ValueError(
                    'a listener transfer authority requires the level shift '
                    'it establishes (transfer_db)'
                )
            if self.kind == 'propagation_model' and (
                self.propagation_model is None
            ):
                raise ValueError(
                    'propagation_model authority must name the model used'
                )
        return self


class HeadroomEvaluation(BaseModel):
    """Usable-output headroom for a target level — the number plus the
    basis and tier it rests on."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    profile_id: str | None = None
    profile_sha256: str | None = None
    # The binding class the profile carried at evaluation time (#1026):
    # ``exact`` (id+version+sha256) or ``advisory`` (id only). ``None``
    # means no profile was supplied.
    profile_binding: UsableOutputBindingClass | None = None
    basis: HeadroomBasis
    tier_used: UsableOutputTier
    target_level_db_spl: float | None = None
    # Inclusive frequency band the evidence was required to cover; ``None``
    # on evaluations evaluated for a single frequency or broadband peak.
    frequency_band_hz: tuple[float, float] | None = None
    # The level the evidence establishes at the profile/declared reference
    # condition — never silently a listener level.
    available_level_db_spl: float | None = None
    # The level at the listener under an explicit transfer authority; only
    # this is compared against ``target_level_db_spl``.
    listener_level_db_spl: float | None = None
    reference_basis: ReferenceBasis | None = None
    # Electrical margin stays a separate result dimension from acoustic
    # listener headroom; it never produces a PASS by itself.
    electrical_headroom_db: float | None = None
    headroom_db: float | None = None
    limiting_element: str | None = None
    reference_axis: str | None = None
    duration_class: OutputDurationClass | None = None
    status: EvaluationStatus
    status_reason: str
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'HeadroomEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('headroom evaluation hash mismatch')
        if self.evaluation_id != 'uhe-' + digest[:24]:
            raise ValueError('headroom evaluation id mismatch')
        return self


def _band_candidates(
    profile: SourceUsableOutputProfile,
    frequency_band_hz: tuple[float, float],
    duration_class: OutputDurationClass,
) -> list[OutputSample]:
    """In-band evidence for an inclusive (low, high) band (#1047).

    A sample is in-band when its point frequency lies inside the band or
    its own declared band overlaps it — the honest-band ceiling is the
    *minimum* qualifying level across that evidence.
    """
    low_hz, high_hz = frequency_band_hz
    return [
        s
        for s in profile.samples
        if s.duration_class == duration_class
        and (
            (
                s.frequency_hz is not None
                and low_hz <= s.frequency_hz <= high_hz
            )
            or (
                s.band is not None
                and s.band.minimum_hz <= high_hz
                and s.band.maximum_hz >= low_hz
            )
        )
    ]


def _level_at(
    profile: SourceUsableOutputProfile,
    frequency_hz: float | None,
    duration_class: OutputDurationClass,
) -> float | None:
    """Best in-domain level at a frequency (nearest sample when no exact
    match); burst/thermal samples never serve continuous queries."""

    candidates = [
        s for s in profile.samples
        if s.duration_class == duration_class
    ]
    if not candidates:
        return None
    if frequency_hz is None:
        return max(s.level_db_spl for s in candidates)
    exact = [
        s for s in candidates
        if s.frequency_hz is not None
        and abs(s.frequency_hz - frequency_hz) < 1e-6
    ]
    if exact:
        return max(s.level_db_spl for s in exact)
    banded = [
        s for s in candidates
        if s.band is not None
        and s.band.minimum_hz <= frequency_hz <= s.band.maximum_hz
    ]
    if banded:
        return min(s.level_db_spl for s in banded)
    return None


def _policy_candidates(
    profile: SourceUsableOutputProfile,
    frequency_hz: float | None,
    duration_class: OutputDurationClass,
) -> list[OutputSample]:
    return [
        s for s in profile.samples
        if s.duration_class == duration_class
        and (frequency_hz is None
             or (s.frequency_hz is not None
                 and abs(s.frequency_hz - frequency_hz) < 1e-6)
             or (s.band is not None
                 and s.band.minimum_hz <= frequency_hz
                 <= s.band.maximum_hz))
    ]


def _sample_meets_policy(
    sample: OutputSample,
    *,
    max_distortion_percent: float | None,
    max_compression_db: float | None,
) -> bool:
    """Whether one measured sample satisfies every named policy criterion.

    Only metrics the policy actually names are evaluated. A THD criterion
    needs at least one measured ``thd``/``aggregate`` metric (a single
    ``harmonic`` order cannot establish THD; ``unavailable`` is recorded
    evidence that cannot satisfy anything); every such measured metric must
    meet the bound. A compression criterion needs a measured
    ``compression_db`` within the bound. Tier labels never substitute for
    criterion evaluation.
    """
    if max_distortion_percent is not None:
        measured = [
            d for d in sample.distortion
            if d.kind in {'thd', 'aggregate'} and d.percent is not None
        ]
        if not measured:
            return False
        if any(d.percent > max_distortion_percent for d in measured):
            return False
    if max_compression_db is not None:
        if sample.compression_db is None or (
            sample.compression_db > max_compression_db
        ):
            return False
    return True


def _reference_compatible(
    profile: SourceUsableOutputProfile | None,
    transfer: ListenerTransferAuthority,
) -> tuple[bool, str | None]:
    """Whether the authority lets a source-reference level be compared to a
    listener target. Returns (allowed, blocking_reason)."""
    axis = profile.reference_axis if profile else None
    if transfer.kind == 'same_reference':
        if (
            profile is not None
            and profile.measurement_distance_m is not None
            and transfer.listener_distance_m is not None
            and abs(
                transfer.listener_distance_m
                - profile.measurement_distance_m
            ) > 1e-9
        ):
            return False, (
                'same-reference claim but listener distance '
                f'{transfer.listener_distance_m} m differs from profile '
                f'reference {profile.measurement_distance_m} m'
            )
        if (
            profile is not None
            and profile.measurement_distance_m is None
        ):
            return False, (
                'same-reference claim but the profile records no '
                'measurement distance'
            )
        if (
            transfer.listener_axis is not None
            and axis is not None
            and transfer.listener_axis != axis
        ):
            return False, (
                'same-reference claim but listener axis '
                f'{transfer.listener_axis!r} differs from profile '
                f'reference axis {axis!r}'
            )
        if (
            transfer.environment is not None
            and profile is not None
            and profile.environment is not None
            and transfer.environment != profile.environment
        ):
            return False, (
                'same-reference claim but environments differ '
                f'({transfer.environment!r} vs {profile.environment!r})'
            )
        return True, None
    # mapped transfer kinds carry the level shift the authority established
    if (
        transfer.listener_axis is not None
        and axis is not None
        and transfer.listener_axis != axis
        and transfer.directivity_authority is None
    ):
        return False, (
            f'profile is measured on {axis!r} but the listener sits on '
            f'{transfer.listener_axis!r} and the transfer carries no '
            'directivity authority'
        )
    return True, None


def evaluate_headroom(
    *,
    profile: SourceUsableOutputProfile | None,
    target_level_db_spl: float | None,
    frequency_hz: float | None = None,
    frequency_band_hz: tuple[float, float] | None = None,
    duration_class: OutputDurationClass = 'continuous',
    declared_spl_db: float | None = None,
    amplifier_headroom_db: float | None = None,
    max_distortion_percent: float | None = None,
    max_compression_db: float | None = None,
    reference_transfer: ListenerTransferAuthority | None = None,
    require_exact_binding: bool = False,
) -> HeadroomEvaluation:
    """Evaluate usable-output headroom for a target listening level.

    Basis precedence: distortion/compression-qualified (only measured
    samples that meet every named policy criterion) → scalar declared →
    UNKNOWN. The number a source-reference basis establishes is never
    subtracted from a listener target directly: it is only compared under
    an explicit ``reference_transfer`` authority (#821), and an electrical
    amplifier margin is reported as its own dimension, never as acoustic
    listener headroom.
    """

    if frequency_hz is not None and frequency_band_hz is not None:
        raise ValueError(
            'frequency_hz and frequency_band_hz are mutually exclusive'
        )
    if frequency_band_hz is not None and not (
        frequency_band_hz[0] < frequency_band_hz[1]
    ):
        raise ValueError('frequency band requires low_hz < high_hz')

    advisory_binding_blocked = (
        profile is not None
        and require_exact_binding
        and profile.binding_class != 'exact'
    )

    tier = usable_output_tier(profile)
    basis: HeadroomBasis = 'unknown'
    available: float | None = None
    limiting: str | None = None
    axis = profile.reference_axis if profile else None
    status: EvaluationStatus = 'UNKNOWN'
    reason: str
    policy_active = (
        max_distortion_percent is not None or max_compression_db is not None
    )
    policy_fail_at_or_below_target = False

    if advisory_binding_blocked:
        # #1026: an id-only (advisory) binding is context, never authority —
        # it cannot establish a qualified or declared headroom basis.
        pass
    elif profile is not None and tier in {'compression', 'thd', 'combined',
                                          'excursion_model'}:
        if frequency_band_hz is not None:
            candidates = _band_candidates(
                profile, frequency_band_hz, duration_class
            )
        else:
            candidates = _policy_candidates(
                profile, frequency_hz, duration_class
            )
        if policy_active and candidates:
            qualifying = [
                s for s in candidates
                if _sample_meets_policy(
                    s,
                    max_distortion_percent=max_distortion_percent,
                    max_compression_db=max_compression_db,
                )
            ]
            if qualifying:
                # Band evaluation takes the minimum qualifying ceiling —
                # the weakest in-band evidence bounds the band claim (#1047).
                if frequency_band_hz is not None:
                    available = min(
                        s.level_db_spl for s in qualifying
                    )
                else:
                    available = max(s.level_db_spl for s in qualifying)
                basis = 'distortion_qualified'
                limiting = 'distortion/compression policy'
            else:
                basis = 'distortion_unqualified'
                highest = max(s.level_db_spl for s in candidates)
                limiting = (
                    'distortion/compression policy (no qualifying sample; '
                    f'highest measured {highest:.1f} dB SPL is retained as '
                    'diagnostic evidence only)'
                )
                if (
                    target_level_db_spl is not None
                    and any(
                        s.level_db_spl <= target_level_db_spl
                        for s in candidates
                    )
                ):
                    policy_fail_at_or_below_target = True

    if basis == 'unknown' and declared_spl_db is not None and (
        not advisory_binding_blocked
    ):
        available = declared_spl_db
        basis = 'scalar_declared'
        limiting = 'declared SPL capability at its declared reference'

    electrical = amplifier_headroom_db
    if (
        basis == 'unknown'
        and electrical is not None
        and not advisory_binding_blocked
    ):
        basis = 'amplifier_margin'
        limiting = 'amplifier margin (electrical, at operating point)'

    # Reference-basis gate: a source-reference level is only compared to a
    # listener target under an explicit transfer authority.
    listener_level: float | None = None
    reference_basis: ReferenceBasis | None = None
    transfer_block: str | None = None
    if available is not None:
        if reference_transfer is None:
            transfer_block = (
                'no listener transfer authority — a source-reference level '
                'is never directly compared to a listener/seat target'
            )
        else:
            ok, why = _reference_compatible(profile, reference_transfer)
            if ok:
                reference_basis = reference_transfer.kind
                listener_level = available + (
                    reference_transfer.transfer_db or 0.0
                )
            else:
                transfer_block = why

    headroom: float | None = None
    if advisory_binding_blocked:
        status = 'UNKNOWN'
        reason = (
            'usable-output profile binding is advisory (equipment '
            'definition referenced by id only, without version+sha256) — '
            'it cannot drive an authoritative headroom decision (#1026)'
        )
    elif basis == 'distortion_unqualified':
        if policy_fail_at_or_below_target:
            status = 'FAIL'
            reason = (
                'distortion/compression policy: measured samples at or '
                'below the target already exceed the criterion'
            )
        else:
            status = 'UNKNOWN'
            reason = (
                'distortion/compression policy: no qualifying measured '
                'sample — the policy-qualified ceiling is unestablished'
            )
    elif listener_level is not None and target_level_db_spl is not None:
        headroom = listener_level - target_level_db_spl
        status = 'PASS' if headroom >= 0.0 else 'FAIL'
        reason = (
            f'{basis} on {reference_basis}: {listener_level:.1f} dB SPL '
            f'at listener vs {target_level_db_spl:.1f} dB SPL target'
        )
    elif basis == 'amplifier_margin':
        status = 'UNKNOWN'
        reason = (
            f'amplifier margin {electrical:.1f} dB is electrical headroom, '
            'not acoustic listener headroom'
        )
    elif available is not None and target_level_db_spl is None:
        status = 'UNKNOWN'
        reason = f'{basis}: level available but no target supplied'
    elif available is not None:
        status = 'UNKNOWN'
        reason = f'{basis}: {transfer_block}'
    else:
        reason = 'no usable-output basis available'

    probe = HeadroomEvaluation.model_construct(
        evaluation_id='',
        profile_id=profile.profile_id if profile else None,
        profile_sha256=profile.profile_sha256 if profile else None,
        profile_binding=(
            None if profile is None else profile.binding_class
        ),
        frequency_band_hz=(
            None
            if frequency_band_hz is None
            else (float(frequency_band_hz[0]), float(frequency_band_hz[1]))
        ),
        basis=basis,
        tier_used=tier,
        target_level_db_spl=target_level_db_spl,
        available_level_db_spl=available,
        listener_level_db_spl=listener_level,
        reference_basis=reference_basis,
        electrical_headroom_db=electrical,
        headroom_db=headroom,
        limiting_element=limiting,
        reference_axis=axis,
        duration_class=duration_class,
        status=status,
        status_reason=reason,
        evaluation_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return HeadroomEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='uhe-' + digest[:24],
        evaluation_sha256=digest,
    )
