"""Listener-area loudspeaker coverage / acoustic-aim authority (#634, REV57-AUD).

A measured 3D directivity dataset plus exact speaker XYZ/yaw does not by
itself prove the intended listening area receives uniform early-arriving
sound from each loudspeaker. This module owns the
source-directivity → installed-aim → listener-area → measured-coverage
chain:

- :class:`CadAcousticAimState` — the five aim identities kept separate:
  cabinet pose, acoustic reference axis, dataset reference axis, design
  aim target and as-built observed aim, plus the cabinet→dataset
  coordinate transform. Acoustic aim is never inferred from a rendered
  enclosure model.
- :class:`CadCoverageListenerArea` — the declared listener domain (seat /
  ear positions with design / holdout roles), bound to the #581 sampling
  campaign where one exists. Coverage is defined against the *intended*
  listener domain — never the convex hull of measured points.
- :class:`CadCoveragePrediction` — the sealed per-source → per-listener
  direct/early prediction set: off-axis angle, distance, arrival time,
  band magnitudes, occlusion state and validity per path, plus the
  declared summation model.
- :class:`CadCoverageMeasurementSet` — the sealed field-evidence set
  (stimulus, per-position band levels, mic/instrument/timebase/state
  pins, fit vs holdout roles).
- :class:`CadCoverageQualification` +
  :func:`evaluate_coverage_qualification` — the fail-closed verdict on
  the #634 §17 state ladder (predicted only / field measured /
  predicted+measured agree / qualified with limitations /
  directivity-limited / occlusion-limited / sampling-insufficient /
  profile-source-ambiguous / indeterminate).

Honesty rules baked in:

- Off-axis response is frequency-dependent, never one scalar beamwidth:
  a ``nominal_beamwidth_only`` source class caps the result at a weaker
  evidence tier and can never upgrade to 3D-directivity truth.
- A listener direction outside the measured directivity domain returns a
  ``LIMITED`` path validity — never nearest-neighbour extrapolation
  disguised as truth (#634 §2).
- Occlusion is an eligibility state, not an EQ problem: a blocked direct
  path is reported ``occlusion_limited``, never repaired conceptually by
  gain/EQ (#634 §8).
- Coherent complex summation is allowed only with a bound #609
  timing/phase capability — otherwise magnitudes sum incoherently and the
  choice is explicit (#634 §11).
- A single MLP trace cannot qualify whole-area coverage: whole-area
  claims require independent holdout positions (#634 §14).
- AVIXA A102 is an optional external profile whose exact revision is
  unresolved (AVIXA page 2022 vs ANSI Webstore 2023) — HTDT never
  hard-codes its thresholds or claims conformance to a guessed revision;
  that conflict is owned by #599 (#634 §16).

Literature basis
----------------
- ANSI/AVIXA A102.01 (2022 page label / 2023 ANSI listing — revision
  conflict owned by #599): early-arriving-energy coverage uniformity
  across a defined listener area.
- D'Appolito, AES 74 (1983) — multiway driver geometry and crossover
  produce frequency-dependent off-axis lobing/nulls.
- Horbach & Keele, AES 32nd Conference (2007) Parts 1/2 — crossover
  topology creates frequency-dependent vertical beam shape; measured
  directivity must be consumed rather than guessed from driver spacing.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


COVERAGE_AIM_SCHEMA_VERSION = 'aud-coverage-aim-1'
COVERAGE_EVALUATION_VERSION = 'aud-coverage-eval-1'

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


def _require_unit_vector(
    vector: tuple[float, float, float] | None, label: str
) -> None:
    if vector is None:
        return
    if len(vector) != 3:
        raise ValueError(f'{label} must be a 3-vector')
    norm = sum(component * component for component in vector) ** 0.5
    for component in vector:
        if not isfinite(float(component)):
            raise ValueError(f'{label} must be finite')
    if norm <= 1e-9:
        raise ValueError(f'{label} must be non-zero')
    if abs(norm - 1.0) > 1e-3:
        raise ValueError(f'{label} must be unit length')


# ---------------------------------------------------------------------------
# Taxonomies (#634)
# ---------------------------------------------------------------------------

AimAxisKind = Literal[
    'cabinet_pose',
    'acoustic_reference_axis',
    'dataset_reference_axis',
    'design_aim_target',
    'as_built_observed_aim',
]
"""#634 §1 — the five aim identities stay separate: a cabinet forward
vector is not the acoustic reference axis, and a design intent is not an
as-built observation."""

CoverageQuantity = Literal[
    'direct_sound',
    'early_arriving_energy',
    'steady_state',
    'late_energy',
]
"""#634 §4 — direct / early / steady-state / late are distinct physical
quantities. A102-family scope is early-arriving coverage; a steady-state
level can hide inadequate direct energy."""

SourceDirectivityClass = Literal[
    'measured_3d_dataset',
    'nominal_beamwidth_only',
    'unavailable',
]
"""#634 §6 — ``nominal_beamwidth_only`` is a weaker evidence class and can
never be upgraded to a 3D directivity balloon."""

CoverageOcclusionState = Literal[
    'clear', 'partially_occluded', 'occluded', 'unknown'
]
"""#634 §8 per-path occlusion (#590 compose)."""

PathValidity = Literal[
    'within_capability',
    'interpolated',
    'outside_angular_domain',
    'outside_frequency_domain',
    'invalid',
]
"""Whether the source directivity supports this source→listener path.
Outside-domain paths are honestly limited — never extrapolated."""

SummationModel = Literal[
    'independent',
    'incoherent_energy',
    'coherent_complex',
]
"""#634 §11 — coherent summation only with a bound #609 timing/phase
capability; otherwise magnitudes stay incoherent/independent."""

MultiSourceClass = Literal[
    'single_physical_source',
    'multiple_physical_for_one_logical',
    'array_distributed',
    'overlapping_adjacent',
]
"""#634 §10 — multi-speaker channels must verify every physical source;
one active speaker never proves array coverage."""

ListenerPositionRole = Literal['design', 'holdout', 'auxiliary']

AppliedTransferKind = Literal['screen', 'grille', 'installed_boundary', 'other']

CoverageState = Literal[
    'predicted_only',
    'field_measured',
    'predicted_and_measured_agree_within_envelope',
    'qualified_with_limitations',
    'source_directivity_limited',
    'occlusion_limited',
    'spatial_sampling_insufficient',
    'profile_source_ambiguous',
    'indeterminate',
]
"""#634 §17 — verdict ladder. There is no ``coverage_pass = true``."""


# ---------------------------------------------------------------------------
# Embedded models
# ---------------------------------------------------------------------------


class CadAimAxis(BaseModel):
    """One aim identity for a loudspeaker (#634 §1)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: AimAxisKind
    vector: tuple[float, float, float] | None = None
    """Unit vector in the declared frame — None means unknown."""
    source: str = Field(default='declared', min_length=1)
    """Evidence source — 'scene_entity', 'measured', 'design_intent',
    'manufacturer_declared', ..."""

    @model_validator(mode='after')
    def _check(self) -> 'CadAimAxis':
        _require_unit_vector(self.vector, f'aim axis {self.kind}')
        return self


class CadCoverageListenerPosition(BaseModel):
    """One declared listener position (#634 §3)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_id: str = Field(min_length=1)
    ear_xyz_m: tuple[float, float, float] | None = None
    role: ListenerPositionRole = 'design'
    priority: int | None = None
    seat_entity_ref: AuthorityRef | None = None
    """Optional pin to the #590 seating entity."""

    @model_validator(mode='after')
    def _check(self) -> 'CadCoverageListenerPosition':
        if self.ear_xyz_m is not None:
            if len(self.ear_xyz_m) != 3:
                raise ValueError('ear_xyz_m must be a 3-vector')
            for component in self.ear_xyz_m:
                _require_finite(component, 'ear_xyz_m')
        if self.seat_entity_ref is not None and (
            self.seat_entity_ref.ref_sha256 is None
        ):
            raise ValueError('seat entity ref must pin its sha256')
        return self


class CadPathBandLevel(BaseModel):
    """One predicted/measured band level for a source→listener path."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: float = Field(gt=0.0)
    level_db: float
    """Direct/early level in the band — the quantity identity comes from
    the enclosing prediction/measurement, never from the number alone."""

    @model_validator(mode='after')
    def _check(self) -> 'CadPathBandLevel':
        _require_finite(self.band_hz, 'band_hz')
        _require_finite(self.level_db, 'level_db')
        return self


class CadCoveragePathPrediction(BaseModel):
    """One source→listener predicted path (#634 §5).

    Per-path raw quantities stay canonical — no heatmap or score replaces
    them. ``validity`` records whether the bound directivity actually
    covers this direction; outside-domain paths are never silently
    extrapolated.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_id: str = Field(min_length=1)
    speaker_entity_id: str = Field(min_length=1)
    off_axis_angle_deg: float | None = Field(default=None, ge=0.0)
    distance_m: float | None = Field(default=None, gt=0.0)
    direct_arrival_time_s: float | None = Field(default=None, ge=0.0)
    band_levels: tuple[CadPathBandLevel, ...] = ()
    on_axis_attenuation_db: float | None = None
    occlusion: CoverageOcclusionState = 'unknown'
    validity: PathValidity = 'within_capability'
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'CadCoveragePathPrediction':
        for value, label in (
            (self.off_axis_angle_deg, 'off_axis_angle_deg'),
            (self.distance_m, 'distance_m'),
            (self.direct_arrival_time_s, 'direct_arrival_time_s'),
            (self.on_axis_attenuation_db, 'on_axis_attenuation_db'),
        ):
            if value is not None:
                _require_finite(value, label)
        bands = [band.band_hz for band in self.band_levels]
        if len(bands) != len(set(bands)):
            raise ValueError('band levels must be unique per band_hz')
        return self


class CadCoverageSourceBinding(BaseModel):
    """One physical source's identity inside a prediction (#634 §2/§9).

    Pins the aim state and the exact directivity evidence, and lists every
    installation transfer applied — screen/grille transfers are applied
    exactly once each, never silently re-applied.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    speaker_entity_id: str = Field(min_length=1)
    aim_ref: AuthorityRef | None = None
    """Pin to a :class:`CadAcousticAimState`."""
    directivity_ref: AuthorityRef | None = None
    """Pin to a #286 DirectivityDataset; ``None`` with a
    ``nominal_beamwidth_only``/``unavailable`` class records the weaker
    evidence tier honestly."""
    directivity_class: SourceDirectivityClass = 'unavailable'
    applied_transfers: tuple[tuple[AppliedTransferKind, str], ...] = ()
    """(kind, ref_id) — each installation transfer applied exactly once."""

    @model_validator(mode='after')
    def _check(self) -> 'CadCoverageSourceBinding':
        for ref, label in (
            (self.aim_ref, 'aim_ref'),
            (self.directivity_ref, 'directivity_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.directivity_class == 'measured_3d_dataset' and (
            self.directivity_ref is None
        ):
            raise ValueError(
                'a measured_3d_dataset class requires a bound '
                'directivity dataset — nominal beamwidth can never '
                'upgrade itself to 3D evidence'
            )
        transfer_keys = [key for key, _ in self.applied_transfers]
        if len(transfer_keys) != len(set(zip(
            transfer_keys,
            [ref for _, ref in self.applied_transfers],
        ))):
            raise ValueError(
                'each applied transfer must be unique — a screen/grille '
                'transfer is applied exactly once'
            )
        return self


class CadCoverageObservation(BaseModel):
    """One measured field observation at one position (#634 §14)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_id: str = Field(min_length=1)
    speaker_entity_id: str = Field(min_length=1)
    band_levels: tuple[CadPathBandLevel, ...] = ()
    early_window_profile: str | None = Field(default=None, min_length=1)
    mic_xyz_m: tuple[float, float, float] | None = None
    instrument_ref: AuthorityRef | None = None
    """#611 instrument/calibration pin."""
    timebase_ref: AuthorityRef | None = None
    """#609 timebase pin — required when the observation claims
    arrival-time evidence."""
    state_ref: AuthorityRef | None = None
    """#573 operating-state pin."""
    arrival_time_s: float | None = None
    observed_at_utc: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'CadCoverageObservation':
        if self.arrival_time_s is not None:
            _require_finite(self.arrival_time_s, 'arrival_time_s')
        if self.mic_xyz_m is not None:
            if len(self.mic_xyz_m) != 3:
                raise ValueError('mic_xyz_m must be a 3-vector')
            for component in self.mic_xyz_m:
                _require_finite(component, 'mic_xyz_m')
        for ref, label in (
            (self.instrument_ref, 'instrument_ref'),
            (self.timebase_ref, 'timebase_ref'),
            (self.state_ref, 'state_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.arrival_time_s is not None and self.timebase_ref is None:
            raise ValueError(
                'an arrival-time claim requires a bound #609 timebase'
            )
        bands = [band.band_hz for band in self.band_levels]
        if len(bands) != len(set(bands)):
            raise ValueError('band levels must be unique per band_hz')
        return self


class CadCoverageProfileRef(BaseModel):
    """Optional external/project evaluation profile binding (#634 §16).

    The A102 family's exact revision is unresolved (AVIXA page 2022 vs
    ANSI Webstore 2023 — owned by #599). Until resolved, HTDT can record
    the profile as the evaluation scope but can never claim conformance
    to a guessed revision.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['a102_family', 'project', 'other']
    revision_label: str | None = Field(default=None, min_length=1)
    resolved: bool = True
    """``False`` records a known revision conflict (e.g. A102 2022 vs
    2023) — the evaluation then stays ``profile_source_ambiguous`` for
    any claimed conformance."""
    envelope_db: float | None = Field(default=None, gt=0.0)
    """Predicted-vs-measured agreement envelope supplied by the profile."""

    @model_validator(mode='after')
    def _check(self) -> 'CadCoverageProfileRef':
        if self.envelope_db is not None:
            _require_finite(self.envelope_db, 'envelope_db')
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


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


class CadAcousticAimState(BaseModel):
    """The installed aim identities for one loudspeaker (#634 §1).

    Cabinet pose, acoustic reference axis, dataset reference axis, design
    aim and as-built observed aim are five separate identities. When both
    the cabinet frame and the dataset frame are declared, the transform
    between them must be explicit — never inferred from a rendered
    enclosure model.
    """

    model_config = ConfigDict(frozen=True)

    aim_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    speaker_entity_id: str = Field(min_length=1)
    aim_axes: tuple[CadAimAxis, ...] = ()
    cabinet_to_dataset_rotation: tuple[float, float, float, float] | None = None
    """Quaternion rotating cabinet-frame vectors into dataset coords.
    Required before an acoustic-reference claim can be evaluated when
    both frames are declared."""
    mounting_ref: AuthorityRef | None = None
    """Optional #614 installed-mounting pin."""
    authority_version: str = Field(
        default=COVERAGE_AIM_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    aim_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'speaker_entity_id': self.speaker_entity_id,
            'aim_axes': [axis.model_dump(mode='json') for axis in self.aim_axes],
            'cabinet_to_dataset_rotation': (
                list(self.cabinet_to_dataset_rotation)
                if self.cabinet_to_dataset_rotation is not None
                else None
            ),
            'mounting_ref': (
                self.mounting_ref.model_dump(mode='json')
                if self.mounting_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    def axis(self, kind: AimAxisKind) -> CadAimAxis | None:
        for axis in self.aim_axes:
            if axis.kind == kind:
                return axis
        return None

    @model_validator(mode='after')
    def _check(self) -> 'CadAcousticAimState':
        _require_iso8601(self.declared_at_utc, 'aim declared_at_utc')
        kinds = [axis.kind for axis in self.aim_axes]
        if len(kinds) != len(set(kinds)):
            raise ValueError('each aim-axis kind may appear only once')
        if self.cabinet_to_dataset_rotation is not None:
            if len(self.cabinet_to_dataset_rotation) != 4:
                raise ValueError('cabinet→dataset rotation must be a quaternion')
            norm = sum(
                c * c for c in self.cabinet_to_dataset_rotation
            ) ** 0.5
            for component in self.cabinet_to_dataset_rotation:
                _require_finite(component, 'cabinet→dataset rotation')
            if abs(norm - 1.0) > 1e-3:
                raise ValueError(
                    'cabinet→dataset rotation must be a unit quaternion'
                )
        if self.mounting_ref is not None and (
            self.mounting_ref.ref_sha256 is None
        ):
            raise ValueError('mounting ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.aim_sha256 != expected:
            raise ValueError('aim state hash mismatch')
        if self.aim_id != _semantic_id('aim', expected):
            raise ValueError('aim state id does not match its hash')
        return self


def aim_binding(aim: CadAcousticAimState) -> AuthorityRef:
    return AuthorityRef(
        kind='acoustic_aim_state',
        ref_id=aim.aim_id,
        ref_sha256=aim.aim_sha256,
    )


class CadCoverageListenerArea(BaseModel):
    """The declared listener domain (#634 §3).

    Positions carry design/holdout roles — coverage claims bind to the
    intended domain, never to the convex hull of whatever microphones
    happened to exist.
    """

    model_config = ConfigDict(frozen=True)

    area_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    positions: tuple[CadCoverageListenerPosition, ...] = Field(min_length=1)
    campaign_ref: AuthorityRef | None = None
    """Optional pin to the #581 spatial campaign design this area
    declares."""
    authority_version: str = Field(
        default=COVERAGE_AIM_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    area_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'label': self.label,
            'positions': [
                position.model_dump(mode='json')
                for position in self.positions
            ],
            'campaign_ref': (
                self.campaign_ref.model_dump(mode='json')
                if self.campaign_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @property
    def design_position_ids(self) -> frozenset[str]:
        return frozenset(
            position.position_id
            for position in self.positions
            if position.role == 'design'
        )

    @property
    def holdout_position_ids(self) -> frozenset[str]:
        return frozenset(
            position.position_id
            for position in self.positions
            if position.role == 'holdout'
        )

    @model_validator(mode='after')
    def _check(self) -> 'CadCoverageListenerArea':
        _require_iso8601(self.declared_at_utc, 'area declared_at_utc')
        ids = [position.position_id for position in self.positions]
        if len(ids) != len(set(ids)):
            raise ValueError('listener position ids must be unique')
        if self.campaign_ref is not None and (
            self.campaign_ref.ref_sha256 is None
        ):
            raise ValueError('campaign ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.area_sha256 != expected:
            raise ValueError('listener area hash mismatch')
        if self.area_id != _semantic_id('covarea', expected):
            raise ValueError('listener area id does not match its hash')
        return self


def listener_area_binding(area: CadCoverageListenerArea) -> AuthorityRef:
    return AuthorityRef(
        kind='coverage_listener_area',
        ref_id=area.area_id,
        ref_sha256=area.area_sha256,
    )


class CadCoveragePrediction(BaseModel):
    """Sealed predicted coverage for one source set over one area
    (#634 §5/§10/§11).

    Raw per-path quantities are canonical — a derived map never replaces
    them. The declared ``quantity`` and ``summation_model`` are part of
    the identity: a steady-state map and an early-arriving map are
    different evidence, and coherent summation is only allowed with a
    bound #609 timing capability.
    """

    model_config = ConfigDict(frozen=True)

    prediction_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    area_ref: AuthorityRef
    quantity: CoverageQuantity
    early_window_profile: str | None = Field(default=None, min_length=1)
    """Exact early-arrival window/profile — REQUIRED when ``quantity`` is
    'early_arriving_energy'; A102-class profiles need the exact window."""
    multi_source_class: MultiSourceClass = 'single_physical_source'
    summation_model: SummationModel = 'independent'
    timebase_capability_ref: AuthorityRef | None = None
    """#609 pin — required for ``coherent_complex`` summation."""
    sources: tuple[CadCoverageSourceBinding, ...] = Field(min_length=1)
    paths: tuple[CadCoveragePathPrediction, ...] = Field(min_length=1)
    authority_version: str = Field(
        default=COVERAGE_AIM_SCHEMA_VERSION, min_length=1
    )
    predicted_at_utc: str = Field(min_length=1)
    prediction_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'area_ref': self.area_ref.model_dump(mode='json'),
            'quantity': self.quantity,
            'early_window_profile': self.early_window_profile,
            'multi_source_class': self.multi_source_class,
            'summation_model': self.summation_model,
            'timebase_capability_ref': (
                self.timebase_capability_ref.model_dump(mode='json')
                if self.timebase_capability_ref is not None
                else None
            ),
            'sources': [
                source.model_dump(mode='json') for source in self.sources
            ],
            'paths': [path.model_dump(mode='json') for path in self.paths],
            'authority_version': self.authority_version,
            'predicted_at_utc': self.predicted_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadCoveragePrediction':
        _require_iso8601(
            self.predicted_at_utc, 'prediction predicted_at_utc'
        )
        if self.area_ref.kind != 'coverage_listener_area':
            raise ValueError(
                "area_ref must pin a 'coverage_listener_area' authority"
            )
        if self.area_ref.ref_sha256 is None:
            raise ValueError('area ref must pin its sha256')
        if self.quantity == 'early_arriving_energy' and (
            self.early_window_profile is None
        ):
            raise ValueError(
                'early-arriving coverage requires an exact '
                'early_window_profile — A102-class scope is defined by '
                'the window, not by a label'
            )
        if self.summation_model == 'coherent_complex' and (
            self.timebase_capability_ref is None
        ):
            raise ValueError(
                'coherent complex summation requires a bound #609 '
                'timing/phase capability — magnitudes cannot be added '
                'as coherent pressure (#634 §11)'
            )
        if self.timebase_capability_ref is not None and (
            self.timebase_capability_ref.kind != 'measurement_timebase'
        ):
            raise ValueError(
                "timebase_capability_ref must pin a "
                "'measurement_timebase' authority"
            )
        source_ids = [source.speaker_entity_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError('source speaker entity ids must be unique')
        if self.multi_source_class == 'single_physical_source' and (
            len(self.sources) != 1
        ):
            raise ValueError(
                'single_physical_source predictions carry exactly one '
                'source — arrays/multi-speaker channels use an explicit '
                'multi-source class'
            )
        declared = set(source_ids)
        for path in self.paths:
            if path.speaker_entity_id not in declared:
                raise ValueError(
                    'every predicted path must reference a declared source'
                )
        if len(self.sources) > 1 and any(
            source.directivity_class == 'unavailable'
            for source in self.sources
        ):
            # allowed, but never silently coherent:
            if self.summation_model == 'coherent_complex':
                raise ValueError(
                    'coherent summation cannot apply to an undirectivity '
                    'source'
                )
        expected = _hash(self.identity_payload())
        if self.prediction_sha256 != expected:
            raise ValueError('coverage prediction hash mismatch')
        if self.prediction_id != _semantic_id('covpred', expected):
            raise ValueError('coverage prediction id does not match its hash')
        return self


def coverage_prediction_binding(
    prediction: CadCoveragePrediction,
) -> AuthorityRef:
    return AuthorityRef(
        kind='coverage_prediction',
        ref_id=prediction.prediction_id,
        ref_sha256=prediction.prediction_sha256,
    )


class CadCoverageMeasurementSet(BaseModel):
    """Sealed field-coverage evidence for one area (#634 §14).

    Binds the exact stimulus (#608), the channel/speaker under test, the
    per-position observations with instrument/timebase/state pins, and
    the declared window profile. Holdout roles come from the bound
    listener area — a position's role is declared there, never relabelled
    here.
    """

    model_config = ConfigDict(frozen=True)

    set_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    area_ref: AuthorityRef
    speaker_entity_ids: tuple[str, ...] = Field(min_length=1)
    """Physical sources actually under test — multi-speaker channels list
    every intended source (#634 §10)."""
    stimulus_ref: AuthorityRef | None = None
    """#608 stimulus pin."""
    quantity: CoverageQuantity
    early_window_profile: str | None = Field(default=None, min_length=1)
    observations: tuple[CadCoverageObservation, ...] = Field(min_length=1)
    authority_version: str = Field(
        default=COVERAGE_AIM_SCHEMA_VERSION, min_length=1
    )
    measured_at_utc: str = Field(min_length=1)
    set_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'area_ref': self.area_ref.model_dump(mode='json'),
            'speaker_entity_ids': list(self.speaker_entity_ids),
            'stimulus_ref': (
                self.stimulus_ref.model_dump(mode='json')
                if self.stimulus_ref is not None
                else None
            ),
            'quantity': self.quantity,
            'early_window_profile': self.early_window_profile,
            'observations': [
                obs.model_dump(mode='json') for obs in self.observations
            ],
            'authority_version': self.authority_version,
            'measured_at_utc': self.measured_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadCoverageMeasurementSet':
        _require_iso8601(
            self.measured_at_utc, 'measurement measured_at_utc'
        )
        if self.area_ref.kind != 'coverage_listener_area':
            raise ValueError(
                "area_ref must pin a 'coverage_listener_area' authority"
            )
        if self.area_ref.ref_sha256 is None:
            raise ValueError('area ref must pin its sha256')
        if len(set(self.speaker_entity_ids)) != len(self.speaker_entity_ids):
            raise ValueError('speaker entity ids must be unique')
        if self.quantity == 'early_arriving_energy' and (
            self.early_window_profile is None
        ):
            raise ValueError(
                'early-arriving coverage requires an exact '
                'early_window_profile'
            )
        for obs in self.observations:
            if obs.speaker_entity_id not in set(self.speaker_entity_ids):
                raise ValueError(
                    'an observation must reference a speaker under test'
                )
        if self.stimulus_ref is not None and (
            self.stimulus_ref.ref_sha256 is None
        ):
            raise ValueError('stimulus ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.set_sha256 != expected:
            raise ValueError('coverage measurement hash mismatch')
        if self.set_id != _semantic_id('covmeas', expected):
            raise ValueError('coverage measurement id does not match its hash')
        return self


def coverage_measurement_binding(
    measurement_set: CadCoverageMeasurementSet,
) -> AuthorityRef:
    return AuthorityRef(
        kind='coverage_measurement_set',
        ref_id=measurement_set.set_id,
        ref_sha256=measurement_set.set_sha256,
    )


# ---------------------------------------------------------------------------
# Qualification (#634 §17)
# ---------------------------------------------------------------------------

ResidualAttribution = Literal[
    'source_dataset_limitation',
    'aim_pose_error',
    'geometry_occlusion',
    'screen_transfer_mismatch',
    'room_early_reflection',
    'measurement_uncertainty',
    'unattributed',
]
"""#634 §15 — residual attribution classes; a residual is never silently
absorbed into a refit of the source directivity."""


class CadCoverageQualification(BaseModel):
    """Sealed coverage verdict for one listener area (#634 §17).

    Reports every axis: the coverage state, the limiting seats and
    bands, residual attribution and the bound prediction/measurement
    pins. No composite ``coverage_pass`` boolean exists.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    area_ref: AuthorityRef
    prediction_ref: AuthorityRef | None = None
    measurement_ref: AuthorityRef | None = None
    profile: CadCoverageProfileRef | None = None
    coverage_state: CoverageState
    limiting_position_ids: tuple[str, ...] = ()
    limiting_band_hz: tuple[float, ...] = ()
    residual_attributions: tuple[ResidualAttribution, ...] = ()
    position_states: tuple[tuple[str, CoverageState], ...] = ()
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'area_ref': self.area_ref.model_dump(mode='json'),
            'prediction_ref': (
                self.prediction_ref.model_dump(mode='json')
                if self.prediction_ref is not None
                else None
            ),
            'measurement_ref': (
                self.measurement_ref.model_dump(mode='json')
                if self.measurement_ref is not None
                else None
            ),
            'profile': (
                self.profile.model_dump(mode='json')
                if self.profile is not None
                else None
            ),
            'coverage_state': self.coverage_state,
            'limiting_position_ids': list(self.limiting_position_ids),
            'limiting_band_hz': list(self.limiting_band_hz),
            'residual_attributions': list(self.residual_attributions),
            'position_states': [
                [position_id, state]
                for position_id, state in self.position_states
            ],
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadCoverageQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.area_ref.kind != 'coverage_listener_area':
            raise ValueError(
                "area_ref must pin a 'coverage_listener_area' authority"
            )
        for ref, label, kind in (
            (self.prediction_ref, 'prediction_ref', 'coverage_prediction'),
            (
                self.measurement_ref,
                'measurement_ref',
                'coverage_measurement_set',
            ),
        ):
            if ref is not None:
                if ref.kind != kind:
                    raise ValueError(f'{label} must pin a {kind!r}')
                if ref.ref_sha256 is None:
                    raise ValueError(f'{label} must pin its sha256')
        for band in self.limiting_band_hz:
            _require_finite(band, 'limiting_band_hz')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('coverage qualification hash mismatch')
        if self.qualification_id != _semantic_id('coveval', expected):
            raise ValueError(
                'coverage qualification id does not match its hash'
            )
        return self


def coverage_qualification_binding(
    qualification: CadCoverageQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='coverage_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


_LIMITING_VALIDITIES: frozenset[PathValidity] = frozenset(
    {'outside_angular_domain', 'outside_frequency_domain', 'invalid'}
)


def evaluate_coverage_qualification(
    *,
    document_id: str,
    area: CadCoverageListenerArea,
    prediction: CadCoveragePrediction | None = None,
    measurement_set: CadCoverageMeasurementSet | None = None,
    profile: CadCoverageProfileRef | None = None,
    evaluated_at_utc: str | None = None,
) -> CadCoverageQualification:
    """Fail-closed coverage verdict (#634 §17).

    The state ladder is honest: no evidence → ``indeterminate``;
    prediction only → ``predicted_only``; field data →
    ``field_measured`` (sampling limits still apply); both agreeing
    within the profile envelope →
    ``predicted_and_measured_agree_within_envelope``. Source-directivity
    limits, occlusion and insufficient sampling each cap the verdict —
    none of them are smoothed away.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    limitations: list[str] = []
    limiting_positions: list[str] = []
    limiting_bands: list[float] = []
    attributions: list[ResidualAttribution] = []
    occluded_positions: list[str] = []
    directivity_limited_positions: list[str] = []

    if prediction is not None and (
        prediction.area_ref.ref_id != area.area_id
        or prediction.area_ref.ref_sha256 != area.area_sha256
    ):
        raise ValueError(
            'prediction does not bind this exact listener area — '
            'evaluating against a different domain would be a silent '
            'scope change'
        )
    if measurement_set is not None and (
        measurement_set.area_ref.ref_id != area.area_id
        or measurement_set.area_ref.ref_sha256 != area.area_sha256
    ):
        raise ValueError(
            'measurement set does not bind this exact listener area'
        )
    if (
        prediction is not None
        and measurement_set is not None
        and prediction.quantity != measurement_set.quantity
    ):
        # Steady-state evidence cannot be compared to an early-arrival
        # prediction as if they were the same quantity (#634 §4).
        raise ValueError(
            'predicted and measured quantities differ — direct/early '
            'coverage cannot be compared against a steady-state trace'
        )
    if (
        prediction is not None
        and measurement_set is not None
        and prediction.early_window_profile
        != measurement_set.early_window_profile
    ):
        raise ValueError(
            'predicted and measured early-arrival windows differ'
        )

    # --- unresolved external profile (#634 §16) -------------------------
    if profile is not None and not profile.resolved:
        reasons.append(
            'external coverage profile revision unresolved — no '
            'conformance claim is possible (owned by #599)'
        )
        state: CoverageState = 'profile_source_ambiguous'
        return _seal(
            CadCoverageQualification,
            {
                'document_id': document_id,
                'area_ref': listener_area_binding(area).model_dump(
                    mode='json'
                ),
                'prediction_ref': (
                    coverage_prediction_binding(prediction).model_dump(
                        mode='json'
                    )
                    if prediction is not None
                    else None
                ),
                'measurement_ref': (
                    coverage_measurement_binding(measurement_set).model_dump(
                        mode='json'
                    )
                    if measurement_set is not None
                    else None
                ),
                'profile': profile.model_dump(mode='json'),
                'coverage_state': state,
                'limiting_position_ids': [],
                'limiting_band_hz': [],
                'residual_attributions': [],
                'position_states': [],
                'reasons': reasons,
                'limitations': limitations,
                'evaluation_version': COVERAGE_EVALUATION_VERSION,
                'evaluated_at_utc': evaluated_at_utc,
            },
            'qualification_id',
            'qualification_sha256',
            'coveval',
        )

    design_ids = area.design_position_ids
    holdout_ids = area.holdout_position_ids

    # --- prediction-axis limits -----------------------------------------
    nominal_only = False
    if prediction is not None:
        for source in prediction.sources:
            if source.directivity_class == 'nominal_beamwidth_only':
                nominal_only = True
            if source.directivity_class == 'unavailable':
                limitations.append(
                    f'{source.speaker_entity_id}: no source-directivity '
                    'evidence bound'
                )
        for path in prediction.paths:
            if path.validity in _LIMITING_VALIDITIES:
                limiting_positions.append(path.position_id)
                directivity_limited_positions.append(path.position_id)
                attributions.append('source_dataset_limitation')
                reasons.append(
                    f'{path.position_id}/{path.speaker_entity_id}: '
                    f'{path.validity} — no extrapolated coverage claimed'
                )
            elif path.validity == 'interpolated':
                limitations.append(
                    f'{path.position_id}/{path.speaker_entity_id}: '
                    'interpolated directivity'
                )
            if path.occlusion in {'occluded', 'partially_occluded'}:
                limiting_positions.append(path.position_id)
                occluded_positions.append(path.position_id)
                attributions.append('geometry_occlusion')
                reasons.append(
                    f'{path.position_id}/{path.speaker_entity_id}: '
                    f'{path.occlusion} — EQ cannot repair a blocked '
                    'direct path'
                )
    measured_ids = frozenset(
        obs.position_id
        for obs in (measurement_set.observations if measurement_set else ())
    )
    holdout_measured = measured_ids & holdout_ids
    design_measured = measured_ids & design_ids

    # --- spatial-sampling sufficiency ------------------------------------
    sampling_insufficient = False
    if measurement_set is not None and len(design_ids) > 1:
        if not design_measured:
            sampling_insufficient = True
        elif len(design_ids - measured_ids) > 0 and not holdout_measured:
            # Whole-area claim without holdout evidence cannot qualify
            # (#634 §14/COV90).
            sampling_insufficient = True

    # --- predicted vs measured comparison --------------------------------
    measured_agree: bool | None = None
    if (
        prediction is not None
        and measurement_set is not None
        and profile is not None
        and profile.envelope_db is not None
    ):
        pred_by_key: dict[tuple[str, str, float], float] = {}
        for path in prediction.paths:
            for band in path.band_levels:
                pred_by_key[
                    (path.position_id, path.speaker_entity_id, band.band_hz)
                ] = band.level_db
        residuals: list[float] = []
        for obs in measurement_set.observations:
            for band in obs.band_levels:
                predicted = pred_by_key.get(
                    (obs.position_id, obs.speaker_entity_id, band.band_hz)
                )
                if predicted is None:
                    attributions.append('unattributed')
                    continue
                residuals.append(abs(predicted - band.level_db))
        if residuals:
            measured_agree = max(residuals) <= float(profile.envelope_db)
            if not measured_agree:
                attributions.append('measurement_uncertainty')
        else:
            measured_agree = None

    # --- verdict ----------------------------------------------------------
    if prediction is None and measurement_set is None:
        state = 'indeterminate'
        reasons.append('no coverage evidence bound')
    elif prediction is not None and measurement_set is None:
        if nominal_only:
            state = 'qualified_with_limitations'
            limitations.append(
                'nominal beamwidth only — never upgraded to 3D '
                'directivity truth'
            )
        elif any(
            p.validity in _LIMITING_VALIDITIES for p in prediction.paths
        ):
            state = 'source_directivity_limited'
        elif any(
            p.occlusion in {'occluded', 'partially_occluded'}
            for p in prediction.paths
        ):
            state = 'occlusion_limited'
        else:
            state = 'predicted_only'
    elif measurement_set is not None and prediction is None:
        if sampling_insufficient:
            state = 'spatial_sampling_insufficient'
            reasons.append(
                'whole-area claims require independent holdout '
                'positions — a single MLP trace cannot qualify '
                'listener-area coverage'
            )
        else:
            state = 'field_measured'
    else:
        # both bound
        assert prediction is not None and measurement_set is not None
        if sampling_insufficient:
            state = 'spatial_sampling_insufficient'
        elif measured_agree is True:
            if any(
                p.occlusion in {'occluded', 'partially_occluded'}
                for p in prediction.paths
            ):
                state = 'occlusion_limited'
            elif any(
                p.validity in _LIMITING_VALIDITIES
                for p in prediction.paths
            ):
                state = 'source_directivity_limited'
            else:
                state = 'predicted_and_measured_agree_within_envelope'
        elif measured_agree is False:
            state = 'qualified_with_limitations'
            reasons.append('predicted/measured residuals exceed envelope')
        else:
            state = 'qualified_with_limitations'
            limitations.append(
                'no comparable predicted/measured band pairs — residuals '
                'could not be evaluated'
            )

    occluded_set = set(occluded_positions)
    directivity_set = set(directivity_limited_positions)
    position_states = []
    for position_id in sorted(design_ids | holdout_ids):
        if position_id in occluded_set:
            position_states.append((position_id, 'occlusion_limited'))
        elif position_id in directivity_set:
            position_states.append(
                (position_id, 'source_directivity_limited')
            )
        elif position_id not in measured_ids and (
            measurement_set is not None
        ):
            position_states.append(
                (position_id, 'spatial_sampling_insufficient')
            )
        else:
            position_states.append((position_id, state))

    return _seal(
        CadCoverageQualification,
        {
            'document_id': document_id,
            'area_ref': listener_area_binding(area).model_dump(mode='json'),
            'prediction_ref': (
                coverage_prediction_binding(prediction).model_dump(
                    mode='json'
                )
                if prediction is not None
                else None
            ),
            'measurement_ref': (
                coverage_measurement_binding(measurement_set).model_dump(
                    mode='json'
                )
                if measurement_set is not None
                else None
            ),
            'profile': (
                profile.model_dump(mode='json') if profile is not None
                else None
            ),
            'coverage_state': state,
            'limiting_position_ids': sorted(set(limiting_positions)),
            'limiting_band_hz': sorted(set(limiting_bands)),
            'residual_attributions': sorted(set(attributions)),
            'position_states': position_states,
            'reasons': reasons,
            'limitations': limitations,
            'evaluation_version': COVERAGE_EVALUATION_VERSION,
            'evaluated_at_utc': evaluated_at_utc,
        },
        'qualification_id',
        'qualification_sha256',
        'coveval',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_aim_state(
    *,
    document_id: str,
    speaker_entity_id: str,
    aim_axes: Sequence[CadAimAxis] = (),
    cabinet_to_dataset_rotation: Sequence[float] | None = None,
    mounting_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
) -> CadAcousticAimState:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadAcousticAimState,
        {
            'document_id': document_id,
            'speaker_entity_id': speaker_entity_id,
            'aim_axes': [
                axis.model_dump(mode='json')
                if isinstance(axis, CadAimAxis)
                else axis
                for axis in aim_axes
            ],
            'cabinet_to_dataset_rotation': (
                list(cabinet_to_dataset_rotation)
                if cabinet_to_dataset_rotation is not None
                else None
            ),
            'mounting_ref': (
                mounting_ref.model_dump(mode='json')
                if mounting_ref is not None
                else None
            ),
            'authority_version': COVERAGE_AIM_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'aim_id',
        'aim_sha256',
        'aim',
    )


def build_listener_area(
    *,
    document_id: str,
    label: str,
    positions: Sequence[CadCoverageListenerPosition],
    campaign_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
) -> CadCoverageListenerArea:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadCoverageListenerArea,
        {
            'document_id': document_id,
            'label': label,
            'positions': [
                position.model_dump(mode='json')
                if isinstance(position, CadCoverageListenerPosition)
                else position
                for position in positions
            ],
            'campaign_ref': (
                campaign_ref.model_dump(mode='json')
                if campaign_ref is not None
                else None
            ),
            'authority_version': COVERAGE_AIM_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'area_id',
        'area_sha256',
        'covarea',
    )


def build_coverage_prediction(
    *,
    document_id: str,
    area: CadCoverageListenerArea,
    quantity: CoverageQuantity,
    sources: Sequence[CadCoverageSourceBinding],
    paths: Sequence[CadCoveragePathPrediction],
    early_window_profile: str | None = None,
    multi_source_class: MultiSourceClass = 'single_physical_source',
    summation_model: SummationModel = 'independent',
    timebase_capability_ref: AuthorityRef | None = None,
    predicted_at_utc: str | None = None,
) -> CadCoveragePrediction:
    predicted_at_utc = predicted_at_utc or _utc_now()
    return _seal(
        CadCoveragePrediction,
        {
            'document_id': document_id,
            'area_ref': listener_area_binding(area).model_dump(mode='json'),
            'quantity': quantity,
            'early_window_profile': early_window_profile,
            'multi_source_class': multi_source_class,
            'summation_model': summation_model,
            'timebase_capability_ref': (
                timebase_capability_ref.model_dump(mode='json')
                if timebase_capability_ref is not None
                else None
            ),
            'sources': [
                source.model_dump(mode='json')
                if isinstance(source, CadCoverageSourceBinding)
                else source
                for source in sources
            ],
            'paths': [
                path.model_dump(mode='json')
                if isinstance(path, CadCoveragePathPrediction)
                else path
                for path in paths
            ],
            'authority_version': COVERAGE_AIM_SCHEMA_VERSION,
            'predicted_at_utc': predicted_at_utc,
        },
        'prediction_id',
        'prediction_sha256',
        'covpred',
    )


def build_coverage_measurement_set(
    *,
    document_id: str,
    area: CadCoverageListenerArea,
    speaker_entity_ids: Sequence[str],
    quantity: CoverageQuantity,
    observations: Sequence[CadCoverageObservation],
    stimulus_ref: AuthorityRef | None = None,
    early_window_profile: str | None = None,
    measured_at_utc: str | None = None,
) -> CadCoverageMeasurementSet:
    measured_at_utc = measured_at_utc or _utc_now()
    return _seal(
        CadCoverageMeasurementSet,
        {
            'document_id': document_id,
            'area_ref': listener_area_binding(area).model_dump(mode='json'),
            'speaker_entity_ids': list(speaker_entity_ids),
            'stimulus_ref': (
                stimulus_ref.model_dump(mode='json')
                if stimulus_ref is not None
                else None
            ),
            'quantity': quantity,
            'early_window_profile': early_window_profile,
            'observations': [
                obs.model_dump(mode='json')
                if isinstance(obs, CadCoverageObservation)
                else obs
                for obs in observations
            ],
            'authority_version': COVERAGE_AIM_SCHEMA_VERSION,
            'measured_at_utc': measured_at_utc,
        },
        'set_id',
        'set_sha256',
        'covmeas',
    )


__all__ = [
    'AimAxisKind',
    'AppliedTransferKind',
    'COVERAGE_AIM_SCHEMA_VERSION',
    'COVERAGE_EVALUATION_VERSION',
    'CadAcousticAimState',
    'CadAimAxis',
    'CadCoverageListenerArea',
    'CadCoverageListenerPosition',
    'CadCoverageMeasurementSet',
    'CadCoverageObservation',
    'CadCoveragePathPrediction',
    'CadCoveragePrediction',
    'CadCoverageProfileRef',
    'CadCoverageQualification',
    'CadCoverageSourceBinding',
    'CadPathBandLevel',
    'CoverageOcclusionState',
    'CoverageQuantity',
    'CoverageState',
    'ListenerPositionRole',
    'MultiSourceClass',
    'PathValidity',
    'ResidualAttribution',
    'SourceDirectivityClass',
    'SummationModel',
    'aim_binding',
    'build_aim_state',
    'build_coverage_measurement_set',
    'build_coverage_prediction',
    'build_listener_area',
    'coverage_measurement_binding',
    'coverage_prediction_binding',
    'coverage_qualification_binding',
    'evaluate_coverage_qualification',
    'listener_area_binding',
]
