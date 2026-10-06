"""Acoustic eigenmode / mode-shape validation authority (#674,
REV58-VALIDMETH).

Matching predicted and measured resonance **frequencies** alone does not
validate the acoustic eigenmodes of a room. Two models can share peak
frequencies while differing materially in damping/complex eigenvalue,
spatial pressure mode shape, nodal/antinodal structure, ordering/pairing
and source/receiver participation. This module is the fail-closed layer
that keeps modal observables separate and refuses ``room modes
validated`` claims built on peak-frequency agreement alone.

Scope discipline (#674):

- Modal quantities are independent observables — natural frequency,
  complex eigenvalue/damping, pressure mode shape, nodal structure and
  participation are related but not interchangeable (#674 §1/§11/§12).
- Mode pairing is *explicit* and evidence-bearing — never default
  ``nearest frequency wins``; pairing carries its algorithm, the
  evidence dimensions used and its ambiguity (#674 §4).
- Near-degenerate or repeated modes are compared as a **modal
  subspace** — a basis rotation inside a degenerate subspace is not a
  solver failure, and close modes are never forced into a clean
  one-to-one correspondence (#674 §5).
- Mode-shape comparison metrics (normalized spatial correlation,
  MAC-style, amplitude-only, nodal overlap, field residual) are
  declared profiles on declared grids — never silent interpolation, and
  no hidden universal pass threshold (#674 §6/§7).
- A local FR peak can be weak near a node: low source participation or
  receiver observability explains non-observation without deleting the
  mode (#674 §10).
- Eigenmode validation stays separate from seat FR/RIR forced-response
  validation — a solver can nail eigenfrequencies and still predict the
  wrong seat response (#674 §13).
- Modes/positions consumed by calibration are tagged CALIBRATION and can
  never double as independent HOLDOUT validation (#674 §14).

Records:

- :class:`PredictedEigenmodePin` — the solver-side modal identity:
  solver/model/version/fidelity, scene revision, eigenfrequency,
  complex eigenvalue/damping where supported, mode-shape field ref,
  normalization + phase conventions, solver-local mode index (never a
  cross-run physical identity).
- :class:`MeasuredModalEvidencePin` — the measured/derived modal
  identity: measurement + receiver grid + identification algorithm +
  fit parameters + uncertainty + holdout positions; optionally pinned
  to a #972 ``MeasuredModalModel``.
- :class:`ModeShapeComparisonSpec` — the declared shape-comparison
  profile: metric family, spatial sample grid identity, normalization,
  phase-ambiguity policy, optional declared acceptance band.
- :class:`ModePairingRecord` — one predicted↔measured association with
  the evidence dimensions that support it and its ambiguity state.
- :class:`EigenmodeValidationVerdict` — the sealed fail-closed verdict
  from :func:`evaluate_eigenmode_validation`: frequency, shape and
  damping agreement reported separately; frequency-only agreement can
  never reach ``eigenmode_validated``.

Literature basis
----------------
- Whear & Morrey (1996), "A Technique for Experimental Acoustic Modal
  Analysis", Proc. IMechE — DOI 10.1243/PIME_PROC_1996_210_181_02.
  Spatial mode shape as validation evidence independent from resonance
  frequency.
- Kung & Singh (1985), JASA 77(2) — DOI 10.1121/1.392342. Complex modal
  eigen-information from measured transfer-function data.
- Čurović et al. (2024), "Estimation of sound absorption coefficient at
  modal frequencies using decay time measurements and eigenvalue model
  in a small room" — DOI 10.1177/14613484241258622. Measured modal
  frequency/decay statistics across source/microphone positions.
- Pham Vu & Lissek (2020), "Low frequency sound field reconstruction in
  a non-rectangular room using a small number of microphones" — DOI
  10.1051/aacus/2020006. Sparse-measurement path to spatial modal
  validation.
- MAC-style normalized spatial correlation for predicted↔reference
  mode-shape comparison (modern room-mode reconstruction literature) —
  useful, but not a universal pass threshold.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


EIGENMODE_SCHEMA_VERSION = 'eigenmode-validation-1'
EIGENMODE_EVALUATION_VERSION = 'eigenmode-validation-eval-1'

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


def _require_ref_sha(ref: AuthorityRef | None, label: str) -> None:
    if ref is not None and ref.ref_sha256 is None:
        raise ValueError(f'{label} must pin its sha256')


# ----------------------------------------------------------------------
# Taxonomies

ModalObservable = Literal[
    'natural_frequency',
    'complex_eigenvalue',
    'modal_decay_damping_q',
    'pressure_mode_shape',
    'nodal_antinodal_structure',
    'source_participation',
    'receiver_observability',
    'forced_response_peak',
]
"""#674 §1 — related but not interchangeable observables."""

ModePairingState = Literal[
    'paired_high_confidence',
    'paired_with_ambiguity',
    'degenerate_subspace_match',
    'unpaired_predicted',
    'unpaired_measured',
    'insufficient_spatial_evidence',
]
"""#674 §4 — pairing states; a degenerate subspace match is honest, not
a failure."""

PairingEvidenceKind = Literal[
    'frequency_proximity',
    'complex_eigenvalue_proximity',
    'mode_shape_similarity',
    'nodal_topology',
    'source_receiver_participation',
    'symmetry_family',
]
"""Candidate pairing evidence dimensions (#674 §4) — each remains
inspectable; no hidden association score."""

ModeShapeMetric = Literal[
    'normalized_complex_correlation',
    'mac_style_spatial_correlation',
    'amplitude_only_shape_correlation',
    'nodal_region_overlap',
    'field_residual_after_normalization',
    'custom_validated_metric',
]
"""#674 §6 — explicit comparison profiles."""

ParticipationState = Literal[
    'source_participation_low',
    'receiver_observability_low',
    'mode_visible',
    'mode_ambiguous',
    'not_evaluated',
]
"""#674 §10 — a weak local peak near a node explains low observability
without deleting the mode."""

EigenmodeValidationState = Literal[
    'eigenmode_validated',
    'eigenmode_validated_with_limitations',
    'frequency_only_match_insufficient',
    'frequency_mismatch',
    'shape_mismatch',
    'damping_mismatch',
    'pairing_ambiguous',
    'insufficient_evidence',
]
"""The fail-closed per-mode verdict."""

ValidationRole = Literal['calibration', 'holdout_validation']
"""#674 §14 — calibration modes can never double as independent
validation evidence."""

EigenmodeFixtureId = Literal[
    'MOD10', 'MOD20', 'MOD30', 'MOD40', 'MOD50', 'MOD60', 'MOD70',
    'MOD80',
]
"""#674 fixtures: MOD10 analytic rectangular modes, MOD20 same
frequencies + wrong shape, MOD30 source on a node, MOD40
near-degenerate pair, MOD50 coordinate offset, MOD60 damping mismatch,
MOD70 sparse reconstruction holdout, MOD80 treatment intervention."""


# ----------------------------------------------------------------------
# Modal identity pins

class PredictedEigenmodePin(BaseModel):
    """Solver-side modal identity (#674 §2).

    ``solver_mode_index`` is solver-local only — it is never a
    cross-run physical identity.
    """

    model_config = ConfigDict(frozen=True)

    solver_ref: AuthorityRef
    scene_revision_ref: AuthorityRef | None = None
    eigenfrequency_hz: float = Field(gt=0.0)
    complex_eigenvalue: str | None = None
    """Declared complex eigenvalue/damping representation where the
    solver supports it — canonical text form."""
    modal_decay_s: float | None = Field(default=None, gt=0.0)
    mode_shape_ref: AuthorityRef | None = None
    """Pins the mode-shape field/mesh artifact when one exists."""
    normalization_convention: str | None = None
    phase_sign_convention: str | None = None
    solver_mode_index: int | None = Field(default=None, ge=0)
    valid_band_hz: tuple[float, float] | None = None

    @model_validator(mode='after')
    def _check(self) -> 'PredictedEigenmodePin':
        _require_ref_sha(self.solver_ref, 'solver_ref')
        _require_ref_sha(self.scene_revision_ref, 'scene_revision_ref')
        _require_ref_sha(self.mode_shape_ref, 'mode_shape_ref')
        _require_finite(self.eigenfrequency_hz, 'eigenfrequency_hz')
        if self.modal_decay_s is not None:
            _require_finite(self.modal_decay_s, 'modal_decay_s')
        if self.valid_band_hz is not None:
            low, high = self.valid_band_hz
            _require_finite(low, 'valid band low')
            _require_finite(high, 'valid band high')
            if low >= high:
                raise ValueError('valid_band_hz must be low < high')
            if not (low <= self.eigenfrequency_hz <= high):
                raise ValueError(
                    'eigenfrequency must lie inside the valid band'
                )
        return self


class MeasuredModalEvidencePin(BaseModel):
    """Measured/derived modal identity (#674 §3).

    A reconstructed field is derived evidence — it is never promoted
    into direct measurement at every voxel.
    """

    model_config = ConfigDict(frozen=True)

    measurement_ref: AuthorityRef
    modal_model_ref: AuthorityRef | None = None
    """Pins a #972 ``MeasuredModalModel`` when the mode came from the
    multi-position identification authority."""
    identification_algorithm: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    identified_frequency_hz: float = Field(gt=0.0)
    identified_decay_s: float | None = Field(default=None, gt=0.0)
    spatial_mode_estimate_ref: AuthorityRef | None = None
    reconstruction_method: str | None = None
    """Declared when the spatial mode estimate is reconstructed from
    sparse measurements — stays derived evidence."""
    uncertainty_hz: float | None = Field(default=None, ge=0.0)
    uncertainty_decay_s: float | None = Field(default=None, ge=0.0)
    holdout_position_refs: tuple[AuthorityRef, ...] = ()
    receiver_position_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'MeasuredModalEvidencePin':
        _require_ref_sha(self.measurement_ref, 'measurement_ref')
        _require_ref_sha(self.modal_model_ref, 'modal_model_ref')
        _require_ref_sha(
            self.spatial_mode_estimate_ref, 'spatial_mode_estimate_ref'
        )
        _require_finite(
            self.identified_frequency_hz, 'identified_frequency_hz'
        )
        for label in (
            'identified_decay_s', 'uncertainty_hz', 'uncertainty_decay_s'
        ):
            value = getattr(self, label)
            if value is not None:
                _require_finite(value, label)
        for ref in self.holdout_position_refs:
            _require_ref_sha(ref, 'holdout_position_refs')
        for ref in self.receiver_position_refs:
            _require_ref_sha(ref, 'receiver_position_refs')
        return self


class ModeShapeComparisonSpec(BaseModel):
    """The declared shape-comparison profile (#674 §6/§7).

    The spatial sample grid and normalization are pinned — two fields on
    different grids are never silently interpolated into comparability;
    interpolation is an explicit derived transform.
    """

    model_config = ConfigDict(frozen=True)

    metric: ModeShapeMetric
    spatial_grid_ref: AuthorityRef | None = None
    """Pins the spatial sample set/grid both fields were evaluated on."""
    interpolation_declared: str | None = None
    """Declared when a field was resampled onto the common grid — the
    transform identity, never silent."""
    normalization: str = Field(min_length=1)
    phase_ambiguity_policy: Literal[
        'global_sign_free',
        'global_phase_free',
        'phase_significant',
    ]
    acceptance_band: tuple[float, float] | None = None
    """Optional declared agreement band for the metric — never a hidden
    universal threshold; ``None`` = report the metric, no gate."""
    metric_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'ModeShapeComparisonSpec':
        _require_ref_sha(self.spatial_grid_ref, 'spatial_grid_ref')
        if self.acceptance_band is not None:
            low, high = self.acceptance_band
            _require_finite(low, 'acceptance band low')
            _require_finite(high, 'acceptance band high')
            if low >= high:
                raise ValueError('acceptance_band must be low < high')
        return self


class ShapeComparisonResult(BaseModel):
    """One evaluated shape-comparison observation (#674 §6)."""

    model_config = ConfigDict(frozen=True)

    spec: ModeShapeComparisonSpec
    value: float
    meets_declared_band: bool | None = None
    """``None`` when the spec declares no acceptance band — the metric
    is reported, not gated."""

    @model_validator(mode='after')
    def _check(self) -> 'ShapeComparisonResult':
        _require_finite(self.value, 'shape comparison value')
        if (
            self.meets_declared_band is not None
            and self.spec.acceptance_band is None
        ):
            raise ValueError(
                'meets_declared_band requires a declared acceptance '
                'band in the spec'
            )
        return self


class ModePairingRecord(BaseModel):
    """One explicit predicted↔measured modal association (#674 §4/§5).

    ``nearest frequency wins`` is never a default: the record carries the
    evidence dimensions that support the association, the pairing
    algorithm/version and its ambiguity.
    """

    model_config = ConfigDict(frozen=True)

    pairing_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    predicted_mode_ref: AuthorityRef | None = None
    measured_mode_ref: AuthorityRef | None = None
    pairing_state: ModePairingState
    pairing_algorithm: str = Field(min_length=1)
    pairing_algorithm_version: str = Field(min_length=1)
    evidence_dimensions: tuple[PairingEvidenceKind, ...] = ()
    subspace_members: tuple[AuthorityRef, ...] = ()
    """Member mode refs when the match is a degenerate/modal-subspace
    correspondence rather than one-to-one."""
    frequency_error_hz: float | None = None
    shape_comparison: ShapeComparisonResult | None = None
    damping_error_s: float | None = None
    participation: ParticipationState = 'not_evaluated'
    validation_role: ValidationRole = 'holdout_validation'
    ambiguity_note: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=EIGENMODE_SCHEMA_VERSION, min_length=1
    )
    pairing_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'predicted_mode_ref': (
                self.predicted_mode_ref.model_dump(mode='json')
                if self.predicted_mode_ref is not None
                else None
            ),
            'measured_mode_ref': (
                self.measured_mode_ref.model_dump(mode='json')
                if self.measured_mode_ref is not None
                else None
            ),
            'pairing_state': self.pairing_state,
            'pairing_algorithm': self.pairing_algorithm,
            'pairing_algorithm_version': self.pairing_algorithm_version,
            'evidence_dimensions': list(self.evidence_dimensions),
            'subspace_members': [
                r.model_dump(mode='json') for r in self.subspace_members
            ],
            'frequency_error_hz': self.frequency_error_hz,
            'shape_comparison': (
                self.shape_comparison.model_dump(mode='json')
                if self.shape_comparison is not None
                else None
            ),
            'damping_error_s': self.damping_error_s,
            'participation': self.participation,
            'validation_role': self.validation_role,
            'ambiguity_note': self.ambiguity_note,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ModePairingRecord':
        _require_iso8601(self.declared_at_utc, 'pairing declared_at_utc')
        _require_ref_sha(self.predicted_mode_ref, 'predicted_mode_ref')
        _require_ref_sha(self.measured_mode_ref, 'measured_mode_ref')
        paired_states = {
            'paired_high_confidence',
            'paired_with_ambiguity',
            'degenerate_subspace_match',
        }
        if self.pairing_state in paired_states:
            if (
                self.predicted_mode_ref is None
                or self.measured_mode_ref is None
            ):
                raise ValueError(
                    'a paired state requires both predicted and '
                    'measured mode refs'
                )
            if not self.evidence_dimensions:
                raise ValueError(
                    'a paired state requires at least one evidence '
                    'dimension'
                )
        if self.pairing_state in (
            'unpaired_predicted',
            'unpaired_measured',
        ):
            if (
                self.predicted_mode_ref is None
                and self.measured_mode_ref is None
            ):
                raise ValueError(
                    'an unpaired state must name the unmatched side'
                )
        if self.pairing_state == 'degenerate_subspace_match' and (
            len(self.subspace_members) < 2
        ):
            raise ValueError(
                'a degenerate subspace match requires >=2 subspace '
                'members'
            )
        if (
            self.pairing_state == 'paired_high_confidence'
            and 'frequency_proximity' in self.evidence_dimensions
            and len(self.evidence_dimensions) == 1
        ):
            raise ValueError(
                'frequency proximity alone cannot reach '
                'paired_high_confidence'
            )
        for ref in self.subspace_members:
            _require_ref_sha(ref, 'subspace_members')
        for label in ('frequency_error_hz', 'damping_error_s'):
            value = getattr(self, label)
            if value is not None:
                _require_finite(value, label)
        if len(set(self.evidence_dimensions)) != len(
            self.evidence_dimensions
        ):
            raise ValueError('evidence_dimensions must be unique')
        expected = _hash(self.identity_payload())
        if self.pairing_sha256 != expected:
            raise ValueError('mode pairing record hash mismatch')
        if self.pairing_id != _semantic_id('modpair', expected):
            raise ValueError(
                'mode pairing id does not match its hash'
            )
        return self


def build_mode_pairing(
    *,
    document_id: str,
    pairing_state: ModePairingState,
    pairing_algorithm: str,
    pairing_algorithm_version: str,
    predicted_mode_ref: AuthorityRef | None = None,
    measured_mode_ref: AuthorityRef | None = None,
    evidence_dimensions: Sequence[PairingEvidenceKind] = (),
    subspace_members: Sequence[AuthorityRef] = (),
    frequency_error_hz: float | None = None,
    shape_comparison: ShapeComparisonResult | None = None,
    damping_error_s: float | None = None,
    participation: ParticipationState = 'not_evaluated',
    validation_role: ValidationRole = 'holdout_validation',
    ambiguity_note: str | None = None,
    declared_at_utc: str | None = None,
) -> ModePairingRecord:
    """Seal one mode-pairing record."""
    return _seal(
        ModePairingRecord,
        {
            'document_id': document_id,
            'pairing_state': pairing_state,
            'pairing_algorithm': pairing_algorithm,
            'pairing_algorithm_version': pairing_algorithm_version,
            'predicted_mode_ref': (
                predicted_mode_ref.model_dump(mode='json')
                if predicted_mode_ref is not None
                else None
            ),
            'measured_mode_ref': (
                measured_mode_ref.model_dump(mode='json')
                if measured_mode_ref is not None
                else None
            ),
            'evidence_dimensions': list(evidence_dimensions),
            'subspace_members': [
                r.model_dump(mode='json') for r in subspace_members
            ],
            'frequency_error_hz': frequency_error_hz,
            'shape_comparison': (
                shape_comparison.model_dump(mode='json')
                if shape_comparison is not None
                else None
            ),
            'damping_error_s': damping_error_s,
            'participation': participation,
            'validation_role': validation_role,
            'ambiguity_note': ambiguity_note,
            'declared_at_utc': declared_at_utc or _utc_now(),
        },
        'pairing_id',
        'pairing_sha256',
        'modpair',
    )


def pairing_binding(record: ModePairingRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='mode_pairing_record',
        ref_id=record.pairing_id,
        ref_sha256=record.pairing_sha256,
    )


# ----------------------------------------------------------------------
# Validation verdict

class EigenmodeValidationVerdict(BaseModel):
    """The sealed fail-closed eigenmode-validation verdict (#674).

    Frequency agreement, shape agreement and damping agreement are
    reported as separate observables — one scalar "modes validated" is
    exactly what this authority refuses.
    """

    model_config = ConfigDict(frozen=True)

    verdict_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    pairing_ref: AuthorityRef
    state: EigenmodeValidationState
    frequency_agreement: Literal[
        'agree', 'disagree', 'not_evaluated'
    ]
    shape_agreement: Literal[
        'agree', 'disagree', 'not_evaluated', 'subspace_evaluated'
    ]
    damping_agreement: Literal[
        'agree', 'disagree', 'not_evaluated'
    ]
    participation: ParticipationState
    calibration_contaminated: bool
    """True when the paired evidence was consumed by calibration —
    independent validation can never be claimed (#674 §14)."""
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=EIGENMODE_EVALUATION_VERSION, min_length=1
    )
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'pairing_ref': self.pairing_ref.model_dump(mode='json'),
            'state': self.state,
            'frequency_agreement': self.frequency_agreement,
            'shape_agreement': self.shape_agreement,
            'damping_agreement': self.damping_agreement,
            'participation': self.participation,
            'calibration_contaminated': self.calibration_contaminated,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'EigenmodeValidationVerdict':
        _require_iso8601(
            self.evaluated_at_utc, 'verdict evaluated_at_utc'
        )
        if self.pairing_ref.kind != 'mode_pairing_record':
            raise ValueError(
                "pairing_ref must pin a 'mode_pairing_record'"
            )
        _require_ref_sha(self.pairing_ref, 'pairing_ref')
        if self.state == 'eigenmode_validated' and (
            self.shape_agreement != 'agree'
            or self.frequency_agreement != 'agree'
        ):
            raise ValueError(
                'eigenmode_validated requires frequency AND shape '
                'agreement'
            )
        if self.state == 'eigenmode_validated' and (
            self.calibration_contaminated
        ):
            raise ValueError(
                'calibration-contaminated evidence cannot reach '
                'eigenmode_validated'
            )
        expected = _hash(self.identity_payload())
        if self.verdict_sha256 != expected:
            raise ValueError('eigenmode verdict hash mismatch')
        if self.verdict_id != _semantic_id('eigval', expected):
            raise ValueError(
                'eigenmode verdict id does not match its hash'
            )
        return self


def eigenmode_verdict_binding(
    verdict: EigenmodeValidationVerdict,
) -> AuthorityRef:
    return AuthorityRef(
        kind='eigenmode_validation_verdict',
        ref_id=verdict.verdict_id,
        ref_sha256=verdict.verdict_sha256,
    )


def evaluate_eigenmode_validation(
    document_id: str,
    pairing: ModePairingRecord,
    *,
    frequency_agreement: Literal[
        'agree', 'disagree', 'not_evaluated'
    ] = 'not_evaluated',
    declared_frequency_tolerance_hz: float | None = None,
    declared_damping_tolerance_s: float | None = None,
    evaluated_at_utc: str | None = None,
) -> EigenmodeValidationVerdict:
    """Fail-closed per-mode validation verdict (#674).

    Rules:

    - Unpaired/insufficient-evidence pairings → ``insufficient_evidence``
      or ``pairing_ambiguous``; nothing is silently validated.
    - A pairing whose only evidence dimension is
      ``frequency_proximity`` can never exceed
      ``frequency_only_match_insufficient``.
    - Degenerate subspace matches evaluate the subspace, not the vector
      — shape is ``subspace_evaluated`` and the verdict caps at
      ``eigenmode_validated_with_limitations``.
    - A shape comparison that misses its declared band →
      ``shape_mismatch``; a declared damping residual beyond the
      declared damping tolerance → ``damping_mismatch``.
    - ``validation_role='calibration'`` flags contamination and caps at
      ``eigenmode_validated_with_limitations``.
    - Low source participation / receiver observability is recorded and
      downgrades confidence, never deletes the mode.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []

    state_map = {
        'paired_high_confidence': None,
        'paired_with_ambiguity': 'pairing_ambiguous',
        'degenerate_subspace_match': None,
        'unpaired_predicted': 'insufficient_evidence',
        'unpaired_measured': 'insufficient_evidence',
        'insufficient_spatial_evidence': 'insufficient_evidence',
    }
    forced = state_map[pairing.pairing_state]

    freq_agree = frequency_agreement
    if (
        freq_agree == 'not_evaluated'
        and pairing.frequency_error_hz is not None
        and declared_frequency_tolerance_hz is not None
    ):
        _require_finite(
            declared_frequency_tolerance_hz,
            'declared_frequency_tolerance_hz',
        )
        freq_agree = (
            'agree'
            if abs(pairing.frequency_error_hz)
            <= declared_frequency_tolerance_hz
            else 'disagree'
        )

    shape_agreement: Literal[
        'agree', 'disagree', 'not_evaluated', 'subspace_evaluated'
    ] = 'not_evaluated'
    shape = pairing.shape_comparison
    if shape is not None:
        if shape.meets_declared_band is True:
            shape_agreement = 'agree'
        elif shape.meets_declared_band is False:
            shape_agreement = 'disagree'
        else:
            limitations.append(
                'shape metric reported without a declared acceptance '
                'band — the metric is descriptive, not gated'
            )
    if pairing.pairing_state == 'degenerate_subspace_match':
        if shape is not None:
            shape_agreement = 'subspace_evaluated'
        limitations.append(
            'near-degenerate modes evaluated as a modal subspace — a '
            'basis rotation inside the subspace is not a failure'
        )

    damping_agreement: Literal['agree', 'disagree', 'not_evaluated'] = (
        'not_evaluated'
    )

    state: EigenmodeValidationState
    if forced in ('insufficient_evidence',):
        state = forced  # type: ignore[assignment]
        reasons.append(
            f'pairing state {pairing.pairing_state} provides no '
            'comparable pair'
        )
    elif pairing.evidence_dimensions == ('frequency_proximity',):
        state = 'frequency_only_match_insufficient'
        reasons.append(
            'frequency proximity alone cannot validate an eigenmode — '
            'shape/damping evidence is required'
        )
    elif forced is not None:
        state = forced  # type: ignore[assignment]
        if forced == 'pairing_ambiguous':
            reasons.append(
                'pairing is ambiguous — ' + (
                    pairing.ambiguity_note
                    or 'the association is not unique'
                )
            )
    elif freq_agree == 'disagree':
        state = 'frequency_mismatch'
        reasons.append(
            'paired modes disagree in frequency beyond the declared '
            'tolerance'
        )
    elif shape_agreement == 'disagree':
        state = 'shape_mismatch'
        reasons.append(
            'mode-shape comparison misses the declared acceptance band'
        )
    elif shape_agreement in ('agree', 'subspace_evaluated') and (
        freq_agree == 'agree'
    ):
        state = 'eigenmode_validated'
    elif shape is None and freq_agree == 'agree':
        state = 'frequency_only_match_insufficient'
        reasons.append(
            'frequencies agree but no mode-shape evidence was declared'
        )
    elif freq_agree == 'not_evaluated' and shape is None:
        state = 'insufficient_evidence'
        reasons.append(
            'neither frequency nor shape agreement was evaluated'
        )
    else:
        state = 'eigenmode_validated_with_limitations'
        limitations.append(
            'partial modal agreement — see observable fields'
        )

    if pairing.damping_error_s is not None:
        if declared_damping_tolerance_s is not None:
            _require_finite(
                declared_damping_tolerance_s,
                'declared_damping_tolerance_s',
            )
            damping_agreement = (
                'agree'
                if abs(pairing.damping_error_s)
                <= declared_damping_tolerance_s
                else 'disagree'
            )
        else:
            limitations.append(
                'damping residual retained without a declared damping '
                'tolerance — the residual is reported, not gated'
            )
    if damping_agreement == 'disagree' and state in (
        'eigenmode_validated',
        'eigenmode_validated_with_limitations',
        'frequency_only_match_insufficient',
    ):
        state = 'damping_mismatch'
        reasons.append(
            'paired modes disagree in modal decay/damping beyond the '
            'declared tolerance'
        )
    elif damping_agreement == 'disagree':
        limitations.append(
            'damping residual beyond the declared tolerance'
        )

    calibration_contaminated = (
        pairing.validation_role == 'calibration'
    )
    if calibration_contaminated:
        limitations.append(
            'paired evidence was consumed by calibration — it cannot '
            'double as independent validation'
        )
        if state == 'eigenmode_validated':
            state = 'eigenmode_validated_with_limitations'

    if pairing.participation in (
        'source_participation_low',
        'receiver_observability_low',
        'mode_ambiguous',
    ):
        limitations.append(
            f'participation state {pairing.participation} — low '
            'observability explains weak evidence without deleting '
            'the mode'
        )
        if state == 'eigenmode_validated':
            state = 'eigenmode_validated_with_limitations'

    if pairing.pairing_state == 'degenerate_subspace_match' and (
        state == 'eigenmode_validated'
    ):
        state = 'eigenmode_validated_with_limitations'

    return _seal(
        EigenmodeValidationVerdict,
        {
            'document_id': document_id,
            'pairing_ref': pairing_binding(pairing).model_dump(
                mode='json'
            ),
            'state': state,
            'frequency_agreement': freq_agree,
            'shape_agreement': shape_agreement,
            'damping_agreement': damping_agreement,
            'participation': pairing.participation,
            'calibration_contaminated': calibration_contaminated,
            'reasons': tuple(reasons),
            'limitations': tuple(dict.fromkeys(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
        },
        'verdict_id',
        'verdict_sha256',
        'eigval',
    )
