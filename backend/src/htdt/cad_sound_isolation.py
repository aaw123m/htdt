"""Sound-isolation planning authority (#559).

HTDT models sound *inside* the theater in increasing detail (#101 solves
in-room acoustics and explicit connected-region propagation) but had no
first-class record answering how much sound is expected to *leave* the
theater toward an adjacent space. This module is that bounded authority —
deliberately not a building-acoustics simulator.

Contract properties:

- construction transmission data is separate from in-room
  absorption/reflection material data; an absorption coefficient is never
  converted into transmission loss;
- :class:`IsolationAssembly` carries evidence-backed band transmission-loss
  data with provenance and an explicit validity domain; STC/Rw/OITC ratings
  are retained as source metadata and never substitute for missing
  low-frequency TL;
- source and receiving regions, separating assemblies, portals and opening
  state are explicit on every :class:`IsolationPath`;
- :func:`estimate_isolation` produces only a bounded *direct-path* estimate
  over declared paths whose assembly data covers the evaluation band;
  flanking, structure-borne and junction-loss mechanisms stay listed as
  explicitly unmodeled, and missing data stays ``UNKNOWN`` — never a
  generic default wall;
- parallel paths through one partition combine *transmission coefficients*
  energetically (``τ`` = 10^-TL/10^), never by averaging dB ratings;
- :class:`IsolationMeasurement` stores measured source-room/receiving-room
  evidence under an exact method profile (ASTM E336, ISO 16283-1,
  ISO 717-1 rating, or informal); informal data is never relabeled as a
  standardized rating, and measured evidence never becomes predicted truth;
- isolation goals evaluate independently of in-room FR/decay objectives and
  produce no code-compliance or legal claim.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite, log10
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import EquipmentDataProvenance, FrequencyDomain


ISOLATION_ASSEMBLY_SCHEMA_VERSION = 1
ISOLATION_ASSEMBLY_AUTHORITY_VERSION = 'isolation-assembly-1'
ISOLATION_SCENARIO_SCHEMA_VERSION = 1
ISOLATION_ESTIMATE_AUTHORITY_VERSION = 'isolation-estimate-1'
ISOLATION_MEASUREMENT_SCHEMA_VERSION = 1


#: Evidence tier per the acquisition hierarchy on the issue. Each tier opens
#: fewer claims — a lab-tested assembly is not proof of the as-built wall.
IsolationEvidenceTier = Literal[
    'measured_field',
    'lab_tl_spectrum',
    'lab_rating_only',
    'user_declared',
    'generic_label',
]

IsolationPathKind = Literal[
    'partition',
    'door',
    'window',
    'penetration',
    'opening',
    'other',
]

#: Exact field/lab method identity. ``informal`` is real evidence but is
#: never a standardized rating.
IsolationMethodProfile = Literal[
    'astm_e90',
    'astm_e336',
    'iso_10140_2',
    'iso_16283_1',
    'iso_717_1',
    'informal',
]

#: Mechanisms a direct-path estimate never claims. Always surfaced on the
#: estimate so reports cannot present a false whole-building number.
UNMODELED_ISOLATION_MECHANISMS: tuple[str, ...] = (
    'flanking_paths',
    'structure_borne_transmission',
    'junction_losses',
    'impact_isolation',
)

PathBandStatus = Literal['AVAILABLE', 'UNKNOWN']
IsolationGoalStatus = Literal['PASS', 'FAIL', 'UNKNOWN', 'UNSUPPORTED']


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _finite(value: object, *, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
    ):
        raise ValueError(f'{field_name} must be a finite number')
    return float(value)


def _tau(tl_db: float) -> float:
    return 10.0 ** (-tl_db / 10.0)


class IsolationRating(BaseModel):
    """A single-number source rating retained as metadata only.

    ``method`` names the exact rating basis (for example ``stc``/``rw``/
    ``oitc``); a rating never fills missing band TL and never claims the
    subwoofer band.
    """

    model_config = ConfigDict(frozen=True)

    method: str = Field(min_length=1)
    value: float

    @field_validator('value')
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='rating value')


class TransmissionLossBand(BaseModel):
    """Laboratory/measured transmission loss over one explicit band."""

    model_config = ConfigDict(frozen=True)

    band_id: str = Field(min_length=1)
    frequency: FrequencyDomain
    tl_db: float = Field(ge=0.0)


class IsolationAssembly(BaseModel):
    """Evidence-bound transmission authority for one construction assembly.

    Distinct from internal absorption material authority: absorption
    coefficients do not convert to TL. ``valid_frequency`` pins where the
    data applies — bands outside it stay ``UNKNOWN``.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ISOLATION_ASSEMBLY_SCHEMA_VERSION
    authority_version: Literal['isolation-assembly-1'] = (
        ISOLATION_ASSEMBLY_AUTHORITY_VERSION
    )
    assembly_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    evidence_tier: IsolationEvidenceTier
    tl_bands: tuple[TransmissionLossBand, ...] = ()
    ratings: tuple[IsolationRating, ...] = ()
    valid_frequency: FrequencyDomain | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_assembly(self) -> 'IsolationAssembly':
        band_ids = [band.band_id for band in self.tl_bands]
        if len(band_ids) != len(set(band_ids)):
            raise ValueError('assembly TL band ids must be unique')
        rating_methods = [item.method for item in self.ratings]
        if len(rating_methods) != len(set(rating_methods)):
            raise ValueError('assembly rating methods must be unique')
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('IsolationAssembly semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'assembly_id': self.assembly_id,
            'name': self.name,
            'evidence_tier': self.evidence_tier,
            'tl_bands': [item.model_dump(mode='json') for item in self.tl_bands],
            'ratings': [item.model_dump(mode='json') for item in self.ratings],
            'valid_frequency': (
                None if self.valid_frequency is None
                else self.valid_frequency.model_dump(mode='json')
            ),
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'note': self.note,
        }

    def tl_at(self, band: FrequencyDomain) -> float | None:
        """Exact TL where one recorded band covers ``band``; else ``None``."""
        if (
            self.valid_frequency is not None
            and not (
                self.valid_frequency.minimum_hz <= band.minimum_hz
                and band.maximum_hz <= self.valid_frequency.maximum_hz
            )
        ):
            return None
        for item in self.tl_bands:
            if (
                item.frequency.minimum_hz <= band.minimum_hz
                and band.maximum_hz <= item.frequency.maximum_hz
            ):
                return item.tl_db
        return None


def build_isolation_assembly(
    *,
    assembly_id: str,
    name: str,
    evidence_tier: IsolationEvidenceTier,
    provenance: Sequence[EquipmentDataProvenance],
    tl_bands: Sequence[TransmissionLossBand] = (),
    ratings: Sequence[IsolationRating] = (),
    valid_frequency: FrequencyDomain | None = None,
    note: str | None = None,
) -> IsolationAssembly:
    payload: dict[str, Any] = {
        'schema_version': ISOLATION_ASSEMBLY_SCHEMA_VERSION,
        'authority_version': ISOLATION_ASSEMBLY_AUTHORITY_VERSION,
        'assembly_id': assembly_id,
        'name': name,
        'evidence_tier': evidence_tier,
        'tl_bands': [item.model_dump(mode='json') for item in tl_bands],
        'ratings': [item.model_dump(mode='json') for item in ratings],
        'valid_frequency': (
            None if valid_frequency is None
            else valid_frequency.model_dump(mode='json')
        ),
        'provenance': [item.model_dump(mode='json') for item in provenance],
        'note': note,
    }
    return IsolationAssembly(
        assembly_id=assembly_id,
        name=name,
        evidence_tier=evidence_tier,
        tl_bands=tuple(tl_bands),
        ratings=tuple(ratings),
        valid_frequency=valid_frequency,
        provenance=tuple(provenance),
        note=note,
        semantic_sha256=_digest(payload),
    )


class IsolationPath(BaseModel):
    """One declared airborne path between a source and a receiving region."""

    model_config = ConfigDict(frozen=True)

    path_id: str = Field(min_length=1)
    kind: IsolationPathKind
    source_region_id: str = Field(min_length=1)
    receiving_region_id: str = Field(min_length=1)
    #: Assembly providing TL data; absent means UNKNOWN — never a default wall.
    assembly_id: str | None = Field(default=None, min_length=1)
    area_m2: float = Field(gt=0.0)
    #: ``open`` paths transmit without an assembly claim; ``closed`` requires
    #: the assembly; ``unknown`` stays unestimated.
    opening_state: Literal['closed', 'open', 'unknown'] = 'closed'
    note: str | None = Field(default=None, min_length=1)


class IsolationScenario(BaseModel):
    """One bounded source/receiver evaluation context."""

    model_config = ConfigDict(frozen=True)

    scenario_id: str = Field(min_length=1)
    source_region_id: str = Field(min_length=1)
    receiving_region_id: str = Field(min_length=1)
    paths: tuple[IsolationPath, ...] = Field(min_length=1)
    evaluation_bands: tuple[FrequencyDomain, ...] = Field(min_length=1)
    #: Excitation/playback scenario reference — kept as a ref so the exact
    #: operating state (#556) stays its own authority.
    excitation_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_scenario(self) -> 'IsolationScenario':
        path_ids = [item.path_id for item in self.paths]
        if len(path_ids) != len(set(path_ids)):
            raise ValueError('isolation path ids must be unique')
        for path in self.paths:
            if (
                path.source_region_id != self.source_region_id
                or path.receiving_region_id != self.receiving_region_id
            ):
                raise ValueError(
                    'isolation path regions must match the scenario regions'
                )
        return self


class PathBandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    path_id: str = Field(min_length=1)
    kind: IsolationPathKind
    band_index: int = Field(ge=0)
    status: PathBandStatus
    tl_db: float | None = None
    tau: float | None = None
    reason: str | None = Field(default=None, min_length=1)


class IsolationBandEstimate(BaseModel):
    """Energetically combined direct-path estimate for one band.

    ``combined_tl_db`` exists only where at least one declared path carries
    data; ``unknown_path_ids`` keeps unmodelled declared paths visible.
    """

    model_config = ConfigDict(frozen=True)

    band_index: int = Field(ge=0)
    frequency: FrequencyDomain
    combined_tl_db: float | None = None
    modeled_path_ids: tuple[str, ...] = ()
    unknown_path_ids: tuple[str, ...] = ()


class IsolationEstimate(BaseModel):
    """Bounded direct-path output — never a whole-building isolation claim."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['isolation-estimate-1'] = (
        ISOLATION_ESTIMATE_AUTHORITY_VERSION
    )
    scenario_id: str = Field(min_length=1)
    source_region_id: str = Field(min_length=1)
    receiving_region_id: str = Field(min_length=1)
    path_results: tuple[PathBandResult, ...]
    band_estimates: tuple[IsolationBandEstimate, ...]
    unmodeled_mechanisms: tuple[str, ...] = UNMODELED_ISOLATION_MECHANISMS
    estimate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_estimate(self) -> 'IsolationEstimate':
        if self.estimate_sha256 != _digest(self.semantic_payload()):
            raise ValueError('IsolationEstimate semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'scenario_id': self.scenario_id,
            'source_region_id': self.source_region_id,
            'receiving_region_id': self.receiving_region_id,
            'path_results': [item.model_dump(mode='json') for item in self.path_results],
            'band_estimates': [
                item.model_dump(mode='json') for item in self.band_estimates
            ],
            'unmodeled_mechanisms': list(self.unmodeled_mechanisms),
        }

    @property
    def dominant_path_ids(self) -> tuple[str, ...]:
        """Highest-transmission modeled path per band (largest τ share)."""
        winners: list[str] = []
        for band in self.band_estimates:
            contributions = [
                item
                for item in self.path_results
                if item.band_index == band.band_index
                and item.status == 'AVAILABLE'
                and item.tau is not None
            ]
            if not contributions:
                continue
            best = max(contributions, key=lambda item: item.tau or 0.0)
            winners.append(best.path_id)
        return tuple(winners)


def estimate_isolation(
    *,
    scenario: IsolationScenario,
    assemblies: Sequence[IsolationAssembly],
) -> IsolationEstimate:
    """Bounded direct-path estimate over the declared paths only.

    Missing assemblies and bands outside an assembly's validity domain stay
    ``UNKNOWN``; open paths transmit at τ = 1 within their declared area
    without needing an assembly.
    """

    by_id = {item.assembly_id: item for item in assemblies}
    path_results: list[PathBandResult] = []
    band_estimates: list[IsolationBandEstimate] = []

    for band_index, band in enumerate(scenario.evaluation_bands):
        tau_area_sum = 0.0
        area_sum = 0.0
        modeled: list[str] = []
        unknown: list[str] = []
        for path in scenario.paths:
            if path.opening_state == 'open':
                path_results.append(PathBandResult(
                    path_id=path.path_id,
                    kind=path.kind,
                    band_index=band_index,
                    status='AVAILABLE',
                    tl_db=0.0,
                    tau=1.0,
                    reason='open path transmits at tau=1 over its declared area',
                ))
                tau_area_sum += path.area_m2
                area_sum += path.area_m2
                modeled.append(path.path_id)
                continue
            if path.opening_state == 'unknown':
                path_results.append(PathBandResult(
                    path_id=path.path_id,
                    kind=path.kind,
                    band_index=band_index,
                    status='UNKNOWN',
                    reason='opening state is not declared',
                ))
                unknown.append(path.path_id)
                continue
            if path.assembly_id is None:
                path_results.append(PathBandResult(
                    path_id=path.path_id,
                    kind=path.kind,
                    band_index=band_index,
                    status='UNKNOWN',
                    reason='no transmission assembly bound to path',
                ))
                unknown.append(path.path_id)
                continue
            assembly = by_id.get(path.assembly_id)
            if assembly is None:
                path_results.append(PathBandResult(
                    path_id=path.path_id,
                    kind=path.kind,
                    band_index=band_index,
                    status='UNKNOWN',
                    reason=f'assembly {path.assembly_id!r} does not resolve',
                ))
                unknown.append(path.path_id)
                continue
            tl = assembly.tl_at(band)
            if tl is None:
                path_results.append(PathBandResult(
                    path_id=path.path_id,
                    kind=path.kind,
                    band_index=band_index,
                    status='UNKNOWN',
                    reason='band outside assembly TL validity/coverage',
                ))
                unknown.append(path.path_id)
                continue
            tau = _tau(tl)
            path_results.append(PathBandResult(
                path_id=path.path_id,
                kind=path.kind,
                band_index=band_index,
                status='AVAILABLE',
                tl_db=tl,
                tau=tau,
            ))
            tau_area_sum += path.area_m2 * tau
            area_sum += path.area_m2
            modeled.append(path.path_id)
        combined = (
            None
            if not modeled or area_sum <= 0.0 or tau_area_sum <= 0.0
            else -10.0 * log10(tau_area_sum / area_sum)
        )
        band_estimates.append(IsolationBandEstimate(
            band_index=band_index,
            frequency=band,
            combined_tl_db=combined,
            modeled_path_ids=tuple(sorted(modeled)),
            unknown_path_ids=tuple(sorted(unknown)),
        ))

    payload: dict[str, Any] = {
        'authority_version': ISOLATION_ESTIMATE_AUTHORITY_VERSION,
        'scenario_id': scenario.scenario_id,
        'source_region_id': scenario.source_region_id,
        'receiving_region_id': scenario.receiving_region_id,
        'path_results': [item.model_dump(mode='json') for item in path_results],
        'band_estimates': [item.model_dump(mode='json') for item in band_estimates],
        'unmodeled_mechanisms': list(UNMODELED_ISOLATION_MECHANISMS),
    }
    return IsolationEstimate(
        scenario_id=scenario.scenario_id,
        source_region_id=scenario.source_region_id,
        receiving_region_id=scenario.receiving_region_id,
        path_results=tuple(path_results),
        band_estimates=tuple(band_estimates),
        estimate_sha256=_digest(payload),
    )


class IsolationMeasurement(BaseModel):
    """Measured source-room/receiving-room field evidence.

    ``method_profile`` is the exact test/metric identity; informal data stays
    informal and is never retroactively labelled a standardized rating.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ISOLATION_MEASUREMENT_SCHEMA_VERSION
    measurement_id: str = Field(min_length=1)
    method_profile: IsolationMethodProfile
    source_region_id: str = Field(min_length=1)
    receiving_region_id: str = Field(min_length=1)
    #: Band-by-band field attenuation; a single-number rating lives only in
    #: ``single_number`` under an explicit rating method.
    band_attenuation_db: tuple[TransmissionLossBand, ...] = ()
    single_number_method: str | None = Field(default=None, min_length=1)
    single_number_value: float | None = None
    source_scenario_ref: str | None = Field(default=None, min_length=1)
    source_position_ids: tuple[str, ...] = ()
    receiving_position_ids: tuple[str, ...] = ()
    opening_state: Literal['closed', 'open', 'unknown'] = 'unknown'
    measured_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @field_validator('single_number_value')
    @classmethod
    def finite_rating(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='single-number rating')

    @model_validator(mode='after')
    def valid_measurement(self) -> 'IsolationMeasurement':
        if (self.single_number_method is None) != (self.single_number_value is None):
            raise ValueError(
                'single-number method and value must be supplied together'
            )
        if self.method_profile == 'informal' and self.single_number_method is not None:
            raise ValueError(
                'informal evidence cannot carry a standardized single-number rating'
            )
        return self

    @property
    def is_standardized(self) -> bool:
        return self.method_profile != 'informal'


class IsolationGoal(BaseModel):
    """A user/sourced receiving-space or isolation target — not a code claim."""

    model_config = ConfigDict(frozen=True)

    goal_id: str = Field(min_length=1)
    kind: Literal['minimum_isolation', 'maximum_received_level']
    frequency: FrequencyDomain
    value_db: float
    source: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @field_validator('value_db')
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='goal value')


class IsolationGoalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    goal_id: str = Field(min_length=1)
    status: IsolationGoalStatus
    reason: str = Field(min_length=1)
    observed_db: float | None = None


def evaluate_isolation_goal(
    goal: IsolationGoal,
    estimate: IsolationEstimate,
) -> IsolationGoalResult:
    """Evaluate one isolation goal against the bounded direct-path estimate.

    Only ``minimum_isolation`` goals can resolve from TL alone; a
    ``maximum_received_level`` goal additionally requires source-side level
    evidence this estimate does not carry, so it reports ``UNSUPPORTED``
    rather than fabricating a level.
    """

    for band in estimate.band_estimates:
        if (
            band.frequency.minimum_hz <= goal.frequency.minimum_hz
            and goal.frequency.maximum_hz <= band.frequency.maximum_hz
        ):
            if band.combined_tl_db is None:
                return IsolationGoalResult(
                    goal_id=goal.goal_id,
                    status='UNKNOWN',
                    reason='no modeled path covers the goal band',
                )
            if goal.kind == 'maximum_received_level':
                return IsolationGoalResult(
                    goal_id=goal.goal_id,
                    status='UNSUPPORTED',
                    reason=(
                        'received-level goals need source-side level evidence; '
                        'the direct-path estimate carries TL only'
                    ),
                    observed_db=band.combined_tl_db,
                )
            met = band.combined_tl_db >= goal.value_db
            return IsolationGoalResult(
                goal_id=goal.goal_id,
                status='PASS' if met else 'FAIL',
                reason='combined direct-path TL compared to goal',
                observed_db=band.combined_tl_db,
            )
    return IsolationGoalResult(
        goal_id=goal.goal_id,
        status='UNKNOWN',
        reason='goal band is outside the evaluated band set',
    )


__all__ = [
    'ISOLATION_ASSEMBLY_AUTHORITY_VERSION',
    'ISOLATION_ASSEMBLY_SCHEMA_VERSION',
    'ISOLATION_ESTIMATE_AUTHORITY_VERSION',
    'ISOLATION_MEASUREMENT_SCHEMA_VERSION',
    'ISOLATION_SCENARIO_SCHEMA_VERSION',
    'IsolationAssembly',
    'IsolationBandEstimate',
    'IsolationEstimate',
    'IsolationEvidenceTier',
    'IsolationGoal',
    'IsolationGoalResult',
    'IsolationGoalStatus',
    'IsolationMeasurement',
    'IsolationMethodProfile',
    'IsolationPath',
    'IsolationPathKind',
    'IsolationRating',
    'IsolationScenario',
    'PathBandResult',
    'PathBandStatus',
    'TransmissionLossBand',
    'UNMODELED_ISOLATION_MECHANISMS',
    'build_isolation_assembly',
    'estimate_isolation',
    'evaluate_isolation_goal',
]
