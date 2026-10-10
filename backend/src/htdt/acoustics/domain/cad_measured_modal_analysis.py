"""Measured low-frequency modal identification authority (#972).

Predicted eigenmodes (#101) and per-measurement decay diagnostics (#511)
are not the same thing as a **modal model identified from multi-position
measured evidence**. This module owns that third authority:

- ``MeasuredModalAnalysisSpec`` — the immutable spec the analysis replays
  from (exact measurement set + positions, band, window, algorithm
  identity, model order, noise/stability policy, holdouts);
- ``MeasuredMode`` — one fitted modal component (pole frequency, decay,
  per-position complex residues, stability/fit evidence), stored as
  derived measured-model evidence — never solver truth and never
  automatically equal to a theoretical ``(nx, ny, nz)`` room mode;
- ``MeasuredModalModel`` — the sealed multi-position model with explicit
  common-pole semantics and holdout bindings;
- deterministic helpers for common-pole aggregation across positions and
  normalized reconstructed mode-shape evaluation (explicitly labelled
  ``reconstructed`` — never "measured everywhere");
- predicted/measured association states where nearest-frequency matching
  alone can never assert physical mode identity.
"""

from __future__ import annotations

from math import isfinite, pi, sqrt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_scene import Position3
from ...canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


ModalIdentificationAlgorithm = Literal[
    'rew_modal_import',
    'matrix_pencil',
    'subspace',
    'producer',
    'imported',
    'unknown',
]

MeasuredModeStability = Literal[
    'stable', 'marginal', 'unstable', 'unevaluated', 'unknown'
]

ModalAssociationState = Literal[
    'associated', 'ambiguous', 'unmatched', 'not_attempted', 'unknown'
]
"""``associated`` requires more than nearest-frequency agreement —
frequency proximity plus explicit supporting evidence (e.g. a compatible
spatial pattern check)."""

ModeShapeKind = Literal['reconstructed', 'imported', 'unknown']


class MeasuredModeResidue(BaseModel):
    """Complex residue/amplitude of one mode at one measured position."""

    model_config = ConfigDict(frozen=True)

    position_id: str = Field(min_length=1)
    amplitude: float = Field(ge=0.0)
    phase_rad: float | None = None

    @model_validator(mode='after')
    def valid_residue(self) -> 'MeasuredModeResidue':
        if not isfinite(float(self.amplitude)):
            raise ValueError('residue amplitude must be finite')
        if self.phase_rad is not None and not isfinite(float(self.phase_rad)):
            raise ValueError('residue phase must be finite')
        return self


class MeasuredMode(BaseModel):
    """One fitted modal component — derived evidence, not solver truth."""

    model_config = ConfigDict(frozen=True)

    mode_id: str = Field(min_length=1)
    center_frequency_hz: float = Field(gt=0.0)
    decay_rate_nepers_per_s: float | None = None
    decay_time_s: float | None = Field(default=None, gt=0.0)
    residues: tuple[MeasuredModeResidue, ...] = ()
    stability: MeasuredModeStability = 'unknown'
    stability_evidence_json: str = '{}'
    snr_db: float | None = None
    fit_evidence_json: str = '{}'
    uncertainty_json: str | None = None
    analysis_band_hz: tuple[float, float] | None = None
    common_pole_group_id: str | None = None
    ambiguous: bool = False

    @model_validator(mode='after')
    def valid_mode(self) -> 'MeasuredMode':
        if not isfinite(float(self.center_frequency_hz)):
            raise ValueError('center frequency must be finite')
        for value in (self.decay_rate_nepers_per_s, self.decay_time_s, self.snr_db):
            if value is not None and not isfinite(float(value)):
                raise ValueError('mode numeric fields must be finite')
        if self.decay_rate_nepers_per_s is not None and self.decay_time_s is not None:
            expected = 6.907755278982137 / float(self.decay_time_s)
            if abs(expected - float(self.decay_rate_nepers_per_s)) > max(
                1e-6, abs(expected) * 1e-3
            ):
                raise ValueError(
                    'decay_rate and decay_time disagree under the declared '
                    'T60 convention (rate = ln(10^3)/T60)'
                )
        position_ids = [res.position_id for res in self.residues]
        if len(position_ids) != len(set(position_ids)):
            raise ValueError('residue position ids must be unique')
        return self

    @property
    def pole_frequency_hz(self) -> float:
        return float(self.center_frequency_hz)


class MeasuredModalAnalysisSpec(BaseModel):
    """Immutable spec a measured modal model replays from.

    The measurement set is explicit: adding one microphone produces a new
    analysis result, never an in-place mutation of a sealed model.
    """

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = None
    source_scenario_ref: str | None = None
    measurement_ids: tuple[str, ...] = Field(min_length=1)
    measurement_positions: tuple[Position3, ...] = ()
    dataset_sha256s: tuple[str, ...] = ()
    ir_capability: Literal['measured_ir', 'complex_response', 'imported', 'unknown'] = 'unknown'
    analysis_band_hz: tuple[float, float] = Field(default=(5.0, 300.0))
    ir_window_s: tuple[float, float] | None = None
    calibration_ref: str | None = None
    timing_reference_id: str | None = None
    preprocessing: str | None = None
    algorithm: ModalIdentificationAlgorithm = 'unknown'
    algorithm_version: str = Field(min_length=1)
    model_order: int | None = Field(default=None, ge=1)
    order_selection_rule: str | None = None
    noise_model_json: str = '{}'
    stability_checks: tuple[str, ...] = ()
    spatial_reconstruction_model: str | None = None
    reconstruction_version: str | None = None
    holdout_position_ids: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_spec(self) -> 'MeasuredModalAnalysisSpec':
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError('measurement_ids must be unique')
        if self.measurement_positions and len(self.measurement_positions) != len(
            self.measurement_ids
        ):
            raise ValueError(
                'measurement_positions must align 1:1 with measurement_ids'
            )
        if self.dataset_sha256s and len(self.dataset_sha256s) != len(
            self.measurement_ids
        ):
            raise ValueError('dataset_sha256s must align 1:1 with measurement_ids')
        if self.algorithm == 'unknown':
            raise ValueError('modal analysis requires an explicit algorithm identity')
        if self.ir_capability == 'unknown':
            raise ValueError(
                'modal identification requires measured IR or complex-response '
                'capability — magnitude-only evidence cannot carry it'
            )
        low, high = self.analysis_band_hz
        if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
            raise ValueError('analysis_band_hz must satisfy 0 < low < high')
        if self.ir_window_s is not None and self.ir_window_s[1] <= self.ir_window_s[0]:
            raise ValueError('ir_window end must be after start')
        if self.spec_sha256 != _hash(self.identity_payload()):
            raise ValueError('modal analysis spec hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'spec_sha256'})


class ReconstructedModeShape(BaseModel):
    """Normalized spatial mode shape — reconstructed, never 'measured
    everywhere'. Evaluation points are explicit."""

    model_config = ConfigDict(frozen=True)

    mode_id: str = Field(min_length=1)
    kind: ModeShapeKind = 'reconstructed'
    evaluation_positions: tuple[Position3, ...] = ()
    normalized_values: tuple[float, ...] = ()
    normalization: Literal['max_amplitude', 'unit_energy', 'unknown'] = 'unknown'

    @model_validator(mode='after')
    def valid_shape(self) -> 'ReconstructedModeShape':
        if len(self.evaluation_positions) != len(self.normalized_values):
            raise ValueError('shape evaluation arrays must align')
        if any(not isfinite(v) for v in self.normalized_values):
            raise ValueError('shape values must be finite')
        if self.normalized_values and max(self.normalized_values) > 1.0 + 1e-6:
            raise ValueError('normalized shape values must not exceed 1')
        return self


class MeasuredModalModel(BaseModel):
    """Sealed measured modal model for one exact spec."""

    model_config = ConfigDict(frozen=True)

    model_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    modes: tuple[MeasuredMode, ...] = ()
    mode_shapes: tuple[ReconstructedModeShape, ...] = ()
    holdout_evaluation_json: str | None = None
    created_at_utc: str = Field(min_length=1)
    model_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_model(self) -> 'MeasuredModalModel':
        mode_ids = [mode.mode_id for mode in self.modes]
        if len(mode_ids) != len(set(mode_ids)):
            raise ValueError('mode ids must be unique')
        shape_ids = [shape.mode_id for shape in self.mode_shapes]
        if len(shape_ids) != len(set(shape_ids)):
            raise ValueError('shape mode ids must be unique')
        for shape in self.mode_shapes:
            if shape.mode_id not in mode_ids:
                raise ValueError('mode shape must reference a declared mode')
        if self.model_sha256 != _hash(self.identity_payload()):
            raise ValueError('measured modal model hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'model_sha256'})


def build_modal_analysis_spec(**kwargs: Any) -> MeasuredModalAnalysisSpec:
    """Assemble and seal a :class:`MeasuredModalAnalysisSpec`."""
    payload = {'spec_sha256': '0' * 64, **kwargs}
    provisional = MeasuredModalAnalysisSpec.model_construct(**canonicalize_payload(MeasuredModalAnalysisSpec, dict(**payload)))
    payload['spec_sha256'] = _hash(provisional.identity_payload())
    return MeasuredModalAnalysisSpec(**payload)


def build_measured_modal_model(
    *,
    model_id: str,
    spec: MeasuredModalAnalysisSpec,
    modes: tuple[MeasuredMode, ...],
    mode_shapes: tuple[ReconstructedModeShape, ...] = (),
    holdout_evaluation_json: str | None = None,
    created_at_utc: str,
) -> MeasuredModalModel:
    payload: dict[str, Any] = {
        'model_id': model_id,
        'spec_id': spec.spec_id,
        'spec_sha256': spec.spec_sha256,
        'document_id': spec.document_id,
        'modes': modes,
        'mode_shapes': mode_shapes,
        'holdout_evaluation_json': holdout_evaluation_json,
        'created_at_utc': created_at_utc,
        'model_sha256': '0' * 64,
    }
    provisional = MeasuredModalModel.model_construct(**canonicalize_payload(MeasuredModalModel, dict(**payload)))
    payload['model_sha256'] = _hash(provisional.identity_payload())
    return MeasuredModalModel(**payload)


def aggregate_common_poles(
    per_position_modes: dict[str, tuple[MeasuredMode, ...]],
    *,
    frequency_tolerance_hz: float,
) -> tuple[tuple[str, ...], ...]:
    """Cluster per-position modes into common-pole groups.

    Multi-position semantics: a real room mode should appear at nearly the
    same pole frequency at every measurement position, while per-position
    residues stay position-specific. Groups below the position count keep
    each member flagged ``ambiguous`` by the caller — this helper only
    returns membership; identity decisions live in the sealed model.
    """
    if not isfinite(frequency_tolerance_hz) or frequency_tolerance_hz <= 0:
        raise ValueError('frequency_tolerance_hz must be positive')
    flattened: list[tuple[str, MeasuredMode]] = []
    for position_id, modes in per_position_modes.items():
        if not position_id:
            raise ValueError('position ids must be non-empty')
        flattened.extend((position_id, mode) for mode in modes)
    flattened.sort(key=lambda item: item[1].center_frequency_hz)
    groups: list[list[str]] = []
    group_centers: list[float] = []
    for _position_id, mode in flattened:
        placed = False
        for index, center in enumerate(group_centers):
            if abs(mode.center_frequency_hz - center) <= frequency_tolerance_hz:
                groups[index].append(mode.mode_id)
                group_centers[index] = (
                    center * (len(groups[index]) - 1) + mode.center_frequency_hz
                ) / len(groups[index])
                placed = True
                break
        if not placed:
            groups.append([mode.mode_id])
            group_centers.append(mode.center_frequency_hz)
    return tuple(tuple(group) for group in groups)


def reconstruct_mode_shape(
    mode: MeasuredMode,
    *,
    evaluation_positions: tuple[Position3, ...] | None = None,
    evaluation_position_ids: tuple[str, ...] | None = None,
) -> ReconstructedModeShape:
    """Normalized spatial mode shape from per-position residues.

    The shape is reconstructed evidence: per-position residue amplitudes
    normalized by their maximum, evaluated at declared positions. It is
    never labelled as measured at unmeasured locations.

    Residues are keyed by ``position_id`` while ``Position3`` carries no
    identity, so ``evaluation_position_ids`` must be supplied in the same
    order as ``evaluation_positions`` and must cover exactly the residue
    position set — otherwise an ordering slip would silently attribute a
    residue measured at one position to another.
    """
    if not mode.residues:
        raise ValueError('mode shape requires per-position residues')
    amplitudes = [residue.amplitude for residue in mode.residues]
    peak = max(amplitudes)
    if peak <= 0:
        raise ValueError('cannot normalize a zero-amplitude mode shape')
    if evaluation_positions is None or evaluation_position_ids is None:
        raise ValueError(
            'evaluation positions and ids must be explicit — a '
            'reconstructed shape may only be quoted at declared positions'
        )
    if len(evaluation_positions) != len(evaluation_position_ids):
        raise ValueError(
            'evaluation position ids must align 1:1 with positions'
        )
    if len(set(evaluation_position_ids)) != len(evaluation_position_ids):
        raise ValueError('evaluation position ids must be unique')
    normalized_by_id = {
        residue.position_id: residue.amplitude / peak
        for residue in mode.residues
    }
    if set(evaluation_position_ids) != set(normalized_by_id):
        raise ValueError(
            'evaluation positions must be exactly the residue positions — '
            'a residue may only be quoted at its own measured position'
        )
    normalized = tuple(normalized_by_id[pid] for pid in evaluation_position_ids)
    return ReconstructedModeShape(
        mode_id=mode.mode_id,
        kind='reconstructed',
        evaluation_positions=evaluation_positions,
        normalized_values=normalized,
        normalization='max_amplitude',
    )


def associate_predicted_mode(
    measured_mode: MeasuredMode,
    *,
    predicted_mode_id: str | None,
    predicted_frequency_hz: float | None,
    frequency_tolerance_hz: float,
    spatial_pattern_agreement: bool | None = None,
) -> ModalAssociationState:
    """Associate a measured mode with a predicted/theoretical mode.

    Frequency proximity alone can never assert physical identity: an
    ``associated`` state additionally requires supporting evidence such
    as spatial-pattern agreement. Within tolerance but without that
    evidence the state stays ``ambiguous`` rather than force-matched.
    """
    if predicted_mode_id is None or predicted_frequency_hz is None:
        return 'not_attempted'
    if not isfinite(predicted_frequency_hz) or predicted_frequency_hz <= 0:
        raise ValueError('predicted frequency must be finite and positive')
    if not isfinite(frequency_tolerance_hz) or frequency_tolerance_hz <= 0:
        raise ValueError('frequency_tolerance_hz must be positive')
    within = (
        abs(measured_mode.center_frequency_hz - predicted_frequency_hz)
        <= frequency_tolerance_hz
    )
    if not within:
        return 'unmatched'
    if spatial_pattern_agreement is True:
        return 'associated'
    return 'ambiguous'
