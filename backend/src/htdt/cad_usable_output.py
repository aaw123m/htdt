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

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .cad_video_geometry import EvaluationStatus, _combine_status


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


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
    'unknown',
]
"""What an evaluated headroom number actually rests on — always reported
alongside the number."""


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


class HeadroomEvaluation(BaseModel):
    """Usable-output headroom for a target level — the number plus the
    basis and tier it rests on."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    profile_id: str | None = None
    profile_sha256: str | None = None
    basis: HeadroomBasis
    tier_used: UsableOutputTier
    target_level_db_spl: float | None = None
    available_level_db_spl: float | None = None
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


def evaluate_headroom(
    *,
    profile: SourceUsableOutputProfile | None,
    target_level_db_spl: float | None,
    frequency_hz: float | None = None,
    duration_class: OutputDurationClass = 'continuous',
    declared_spl_db: float | None = None,
    amplifier_headroom_db: float | None = None,
    max_distortion_percent: float | None = None,
) -> HeadroomEvaluation:
    """Evaluate usable-output headroom for a target listening level.

    Basis precedence: distortion-qualified (when a THD/compression ceiling
    exists and a distortion policy is supplied) → scalar declared →
    amplifier margin → UNKNOWN. The result always carries ``basis``,
    ``tier_used``, ``reference_axis`` and ``duration_class`` so consumers
    see exactly what the number rests on.
    """

    tier = usable_output_tier(profile)
    basis: HeadroomBasis = 'unknown'
    available: float | None = None
    limiting: str | None = None
    axis = profile.reference_axis if profile else None
    status: EvaluationStatus = 'UNKNOWN'
    reason: str

    if profile is not None and tier in {'compression', 'thd', 'combined',
                                        'excursion_model'}:
        level = _level_at(profile, frequency_hz, duration_class)
        if level is not None and max_distortion_percent is not None:
            # Find the highest sample level that still meets the
            # distortion policy at/around the query point.
            qualifying = [
                s.level_db_spl
                for s in profile.samples
                if s.duration_class == duration_class
                and (frequency_hz is None
                     or (s.frequency_hz is not None
                         and abs(s.frequency_hz - frequency_hz) < 1e-6)
                     or (s.band is not None
                         and s.band.minimum_hz <= frequency_hz
                         <= s.band.maximum_hz))
                and s.distortion
                and all(
                    d.kind == 'unavailable'
                    or (d.percent is not None
                        and d.percent <= max_distortion_percent)
                    for d in s.distortion
                )
            ]
            if qualifying:
                available = max(qualifying)
                basis = 'distortion_qualified'
                limiting = 'distortion policy'
            else:
                available = level
                basis = 'distortion_qualified'
                limiting = 'distortion policy (no qualifying sample)'

    if basis == 'unknown' and declared_spl_db is not None:
        available = declared_spl_db
        basis = 'scalar_declared'
        limiting = 'declared SPL capability'

    if basis == 'unknown' and amplifier_headroom_db is not None:
        available = (
            None if target_level_db_spl is None
            else target_level_db_spl + amplifier_headroom_db
        )
        basis = 'amplifier_margin'
        limiting = 'amplifier margin'

    headroom: float | None = None
    if available is not None and target_level_db_spl is not None:
        headroom = available - target_level_db_spl
        status = 'PASS' if headroom >= 0.0 else 'FAIL'
        reason = (
            f'{basis}: {available:.1f} dB SPL available vs '
            f'{target_level_db_spl:.1f} dB SPL target'
        )
    elif available is not None:
        status = 'UNKNOWN'
        reason = f'{basis}: level available but no target supplied'
    else:
        reason = 'no usable-output basis available'

    probe = HeadroomEvaluation.model_construct(
        evaluation_id='',
        profile_id=profile.profile_id if profile else None,
        profile_sha256=profile.profile_sha256 if profile else None,
        basis=basis,
        tier_used=tier,
        target_level_db_spl=target_level_db_spl,
        available_level_db_spl=available,
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
