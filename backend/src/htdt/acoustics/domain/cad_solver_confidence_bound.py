"""Solver confidence bounded by input authority (issue #811).

A high-fidelity solver with weak boundary/source/geometry inputs must not
inherit a high-confidence result. The literature basis is unambiguous:
FDTD/FMBEM solvers can agree with each other while material input data
still dominates simulation-vs-measurement deviation (Li 2022); boundary
parameters are a major experimental uncertainty driver (Thydal 2021);
absorption measurement reproducibility is itself bounded (ISO 12999-2,
Scrosati 2020); absorption->impedance conversion is a modeled, non-unique
step (Fratoni 2023, Fratoni & D'Orazio 2025); source-directivity choice
produces material differences in simulated room results (Wang & Vigeant
2008); and calibration can produce close agreement only *inside* its
calibration context (Pilch 2020) — a fitted value is never measured truth.

This module is the mechanical input-qualification envelope:

    claim credibility
        <= weakest materially relevant input authority
        <= solver validation domain for that input/model class

Four sealed per-dimension records declare input authority exactly once —
never inferred:

* ``MaterialInputAuthority`` — boundary/material evidence class
  (measured complex boundary, parameterized measured model, exact
  manufacturer laboratory data, measured energy coefficient, declared/
  non-identical/fitted effective, declared-model derived conversion,
  scalar-only/generic/inferred). A fitted value is pinned to its
  calibration context and can never be relabeled measured; a derived
  conversion pins its ``BoundaryConversionArtifact`` (model, assumptions,
  priors, uncertainty, rejected alternatives — #570 lineage).

* ``SourceDirectivityAuthority`` — directivity provenance (measured
  balloon, manufacturer dataset, fitted source correction, inferred,
  simplified model) kept strictly separate from format compliance:
  SOFA/AES69 validity is container conformance, never acoustic quality.
  Coverage, angular/frequency sampling, interpolation, frame and
  normalization are retained.

* ``GeometryInputAuthority`` — scene fidelity (exact scene revision,
  as-built measured, surveyed partial, simplified, assumed nominal).

* ``PoseInputAuthority`` — placement authority per subject
  (surveyed acoustic center, measured position, intended placement,
  assumed) with the modal-sensitivity flag that decides whether a
  position perturbation was evaluated against the local modal gradient.

* ``SolverInputEnvelope`` — the sealed bundle binding one solver request
  to every input-authority record plus environmental bindings
  (temperature, humidity, speed of sound, door/opening state, movable
  objects, playback state, time variance). Environmental claims are
  dependency-aware: an undeclared aspect stales only the claims that
  materially depend on it.

* ``ClaimBoundRecord`` — one row per claim class, exactly once, like the
  solver capability manifest declares every phenomenon. Each row carries
  the verdict (``envelope_inherited`` / ``solver_bounded`` /
  ``bounded_by_input`` / ``claim_denied`` / ``solver_unqualified`` /
  ``unbounded_input``), the confidence ceiling, the weakest dimension(s)
  and the reasons — there is no universal score.

Fail-closed rules: a materially relevant input that is undeclared yields
``unbounded_input``/``insufficient_authority``, never a mid-trust
default; a solver envelope/manifest that does not cover the claim yields
``solver_unqualified``; quantity semantics stay honest (diffusion is
never scattering, scalar absorption never carries phase authority).
"""

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_validation_envelope import AccuracyEnvelopeRecord
from ...cad_authority_registry import AuthorityRef
from ...cad_directivity_admission import DirectivityCoverage
from ...cad_material_evidence_compatibility import (
    BoundaryPhysicalQuantity,
    EvidenceIncidence,
    EvidenceMethodClass,
    EvidencePhase,
)
from .cad_solver_capability_manifest import SolverCapabilityManifest
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

SOLVER_CONFIDENCE_AUTHORITY_VERSION = 'solver-confidence-bound-1'
MATERIAL_INPUT_AUTHORITY_VERSION = 'material-input-authority-1'
DIRECTIVITY_INPUT_AUTHORITY_VERSION = 'source-directivity-authority-1'
GEOMETRY_INPUT_AUTHORITY_VERSION = 'geometry-input-authority-1'
POSE_INPUT_AUTHORITY_VERSION = 'pose-input-authority-1'
SOLVER_INPUT_ENVELOPE_VERSION = 'solver-input-envelope-1'
CLAIM_BOUND_RECORD_VERSION = 'claim-bound-record-1'
CONFIDENCE_BOUND_EVALUATOR_VERSION = 'confidence-bound-evaluator-1'

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
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )

def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')

# ---------------------------------------------------------------------------
# Claim classes, dimensions, verdict vocabulary
# ---------------------------------------------------------------------------

InputDimension = Literal[
    'material_boundary',
    'source_directivity',
    'geometry',
    'pose',
    'environment',
]

ClaimClass = Literal[
    'absolute_level_at_position',
    'local_frequency_response',
    'spatial_coverage',
    'early_reflection_structure',
    'modal_response',
    'decay_time',
    'late_diffuse_field',
    'phase_coherent_field',
    'speech_intelligibility',
    'hybrid_overlap_consistency',
]

CLAIM_CLASSES: tuple[ClaimClass, ...] = (
    'absolute_level_at_position',
    'local_frequency_response',
    'spatial_coverage',
    'early_reflection_structure',
    'modal_response',
    'decay_time',
    'late_diffuse_field',
    'phase_coherent_field',
    'speech_intelligibility',
    'hybrid_overlap_consistency',
)

BoundVerdict = Literal[
    'envelope_inherited',
    'solver_bounded',
    'bounded_by_input',
    'claim_denied',
    'solver_unqualified',
    'unbounded_input',
]

#: Per-claim confidence ceiling — a claim-class bound, never a score.
ConfidenceCeiling = Literal[
    'full_envelope',
    'solver_bound',
    'measured_bound',
    'documented_bound',
    'limited_bound',
    'derived_bound',
    'assumed_bound',
    'insufficient_authority',
]

#: Shared authority rank across every input dimension — a coarse
#: evidence ordering used only to compute the minimum bound. The
#: declared per-dimension class (kept on the record) is the provenance;
#: the rank never erases it.
AuthorityRank = Literal[
    'measured',
    'documented',
    'limited',
    'derived',
    'assumed',
    'insufficient',
]

_RANK_ORDER: dict[str, int] = {
    'measured': 5,
    'documented': 4,
    'limited': 3,
    'derived': 2,
    'assumed': 1,
    'insufficient': 0,
}

_RANK_CEILING: dict[str, ConfidenceCeiling] = {
    'measured': 'measured_bound',
    'documented': 'documented_bound',
    'limited': 'limited_bound',
    'derived': 'derived_bound',
    'assumed': 'assumed_bound',
    'insufficient': 'insufficient_authority',
}

#: Dimension-report token used in ``weakest_dimensions`` when the solver
#: domain itself sets the bound (not an input dimension).
SOLVER_DOMAIN = 'solver_domain'

BoundRowSupport = Literal['supports', 'bounds', 'denies', 'undeclared']

# ---------------------------------------------------------------------------
# Per-dimension authority classes
# ---------------------------------------------------------------------------

MaterialAuthorityClass = Literal[
    'measured_complex_boundary',
    'measured_parameterized_model',
    'manufacturer_laboratory_exact',
    'measured_energy_coefficient',
    'manufacturer_declared',
    'nonidentical_construction',
    'fitted_effective',
    'derived_conversion',
    'scalar_absorption_only',
    'generic_table_lookup',
    'inferred_construction',
    'undeclared',
]

DirectivityProvenanceClass = Literal[
    'measured_balloon',
    'manufacturer_dataset',
    'fitted_source_correction',
    'inferred_estimated',
    'simplified_model',
    'undeclared',
]

DirectivityFormatCompliance = Literal[
    'sofa_aes69',
    'normalized_directivity_json',
    'proprietary',
    'none',
    'undeclared',
]

SimplifiedSourceModel = Literal[
    'omnidirectional',
    'point_source',
    'cardioid_family',
    'other',
]

GeometryFidelityClass = Literal[
    'exact_scene_revision',
    'asbuilt_measured',
    'surveyed_partial',
    'simplified',
    'assumed_nominal',
    'undeclared',
]

PoseAuthorityClass = Literal[
    'surveyed_acoustic_center',
    'measured_position',
    'intended_placement',
    'assumed',
    'undeclared',
]

PoseSubjectKind = Literal[
    'source',
    'receiver',
    'treatment',
    'other',
]

ModalSensitivity = Literal[
    'evaluated',
    'not_material',
    'unevaluated',
]

EnvironmentAspect = Literal[
    'temperature',
    'humidity',
    'speed_of_sound',
    'door_opening_state',
    'movable_objects',
    'playback_state',
    'time_variance',
]

EnvironmentStateClass = Literal[
    'measured',
    'declared',
    'assumed',
    'undeclared',
]

#: Claims where a position perturbation must be evaluated against the
#: local modal gradient rather than treated as a cosmetic tolerance
#: (issue §5 — low-frequency modal sensitivity).
MODAL_SENSITIVE_CLAIMS: frozenset[str] = frozenset({
    'local_frequency_response',
    'modal_response',
    'phase_coherent_field',
})

#: Quantity semantics that carry scattering authority. A directional
#: diffusion coefficient is never scattering (issue §3, ISO 17497-1 vs
#: 17497-2); visual roughness is never numerical evidence.
_SCATTERING_QUANTITY = 'scattering_coefficient_random_incidence'

_MATERIAL_RANK: dict[str, AuthorityRank] = {
    'measured_complex_boundary': 'measured',
    'measured_parameterized_model': 'measured',
    'manufacturer_laboratory_exact': 'measured',
    'measured_energy_coefficient': 'limited',
    'manufacturer_declared': 'limited',
    'nonidentical_construction': 'limited',
    'fitted_effective': 'limited',
    'derived_conversion': 'derived',
    'scalar_absorption_only': 'assumed',
    'generic_table_lookup': 'assumed',
    'inferred_construction': 'assumed',
    'undeclared': 'insufficient',
}

_DIRECTIVITY_RANK: dict[str, AuthorityRank] = {
    'measured_balloon': 'measured',
    'manufacturer_dataset': 'documented',
    'fitted_source_correction': 'limited',
    'inferred_estimated': 'assumed',
    'simplified_model': 'assumed',
    'undeclared': 'insufficient',
}

_GEOMETRY_RANK: dict[str, AuthorityRank] = {
    'exact_scene_revision': 'measured',
    'asbuilt_measured': 'measured',
    'surveyed_partial': 'documented',
    'simplified': 'assumed',
    'assumed_nominal': 'assumed',
    'undeclared': 'insufficient',
}

_POSE_RANK: dict[str, AuthorityRank] = {
    'surveyed_acoustic_center': 'measured',
    'measured_position': 'measured',
    'intended_placement': 'documented',
    'assumed': 'assumed',
    'undeclared': 'insufficient',
}

_ENVIRONMENT_RANK: dict[str, AuthorityRank] = {
    'measured': 'measured',
    'declared': 'documented',
    'assumed': 'assumed',
    'undeclared': 'insufficient',
}

_SOLVER_STATE_RANK: dict[str, AuthorityRank] = {
    'VALIDATED_FOR_DECLARED_DOMAIN': 'measured',
    'VALIDATED_WITH_LIMITATIONS': 'limited',
    'EXPERIMENTAL': 'assumed',
    'INSUFFICIENT_EVIDENCE': 'insufficient',
    'NOT_APPLICABLE': 'insufficient',
}

# ---------------------------------------------------------------------------
# Per-claim material-relevance requirements — a declared, inspectable
# matrix. ``supports`` classes leave the claim unbounded by that
# dimension; ``bounds`` classes cap it at the class rank; every other
# declared class denies it. A dimension absent from the claim's map is
# not materially relevant and is skipped entirely — an undeclared
# irrelevant input can never stale the claim (issue §6 dependency-aware
# staleness, mirrored for inputs).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _DimensionRule:
    supports: frozenset[str]
    bounds: frozenset[str]

@dataclass(frozen=True)
class _ClaimRule:
    phenomenon: str | None
    observable: str | None
    environment_aspects: frozenset[str]
    material: _DimensionRule | None
    directivity: _DimensionRule | None
    geometry: _DimensionRule | None
    pose: _DimensionRule | None

_MATERIAL_STRONG = frozenset({
    'measured_complex_boundary',
    'measured_parameterized_model',
    'manufacturer_laboratory_exact',
})
_MATERIAL_LIMITED = frozenset({
    'measured_energy_coefficient',
    'manufacturer_declared',
    'nonidentical_construction',
    'fitted_effective',
})
_MATERIAL_WEAK = frozenset({
    'scalar_absorption_only',
    'generic_table_lookup',
    'inferred_construction',
})

_CLAIM_REQUIREMENTS: dict[str, _ClaimRule] = {
    'absolute_level_at_position': _ClaimRule(
        phenomenon='spatial_pressure_field',
        observable='spatial_field_db',
        environment_aspects=frozenset({'temperature', 'playback_state'}),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG | {'measured_energy_coefficient'},
            bounds=_MATERIAL_LIMITED - {'measured_energy_coefficient'}
            | {'derived_conversion', 'scalar_absorption_only'},
        ),
        directivity=_DimensionRule(
            supports={'measured_balloon', 'manufacturer_dataset'},
            bounds={
                'fitted_source_correction',
                'inferred_estimated',
                'simplified_model',
            },
        ),
        geometry=_DimensionRule(
            supports={'exact_scene_revision', 'asbuilt_measured'},
            bounds={'surveyed_partial', 'simplified', 'assumed_nominal'},
        ),
        pose=_DimensionRule(
            supports={'surveyed_acoustic_center', 'measured_position'},
            bounds={'intended_placement'},
        ),
    ),
    'local_frequency_response': _ClaimRule(
        phenomenon='spatial_pressure_field',
        observable='magnitude_response_db',
        environment_aspects=frozenset({
            'temperature', 'door_opening_state', 'movable_objects',
        }),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG | {'measured_energy_coefficient'},
            bounds=_MATERIAL_LIMITED - {'measured_energy_coefficient'}
            | {'derived_conversion', 'scalar_absorption_only'},
        ),
        directivity=_DimensionRule(
            supports={'measured_balloon', 'manufacturer_dataset'},
            bounds={
                'fitted_source_correction',
                'inferred_estimated',
                'simplified_model',
            },
        ),
        geometry=_DimensionRule(
            supports={'exact_scene_revision', 'asbuilt_measured'},
            bounds={'surveyed_partial', 'simplified'},
        ),
        pose=_DimensionRule(
            supports={'surveyed_acoustic_center', 'measured_position'},
            bounds={'intended_placement'},
        ),
    ),
    'spatial_coverage': _ClaimRule(
        phenomenon='spatial_pressure_field',
        observable='seat_to_seat_variation_db',
        environment_aspects=frozenset({'temperature'}),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG | {'measured_energy_coefficient'},
            bounds=_MATERIAL_LIMITED - {'measured_energy_coefficient'}
            | {'derived_conversion', 'scalar_absorption_only'},
        ),
        directivity=_DimensionRule(
            supports={'measured_balloon', 'manufacturer_dataset'},
            bounds={
                'fitted_source_correction',
                'inferred_estimated',
                'simplified_model',
            },
        ),
        geometry=_DimensionRule(
            supports={
                'exact_scene_revision', 'asbuilt_measured',
                'surveyed_partial',
            },
            bounds={'simplified', 'assumed_nominal'},
        ),
        pose=_DimensionRule(
            supports={
                'surveyed_acoustic_center', 'measured_position',
                'intended_placement',
            },
            bounds={'assumed'},
        ),
    ),
    'early_reflection_structure': _ClaimRule(
        phenomenon='specular_reflection',
        observable='reflection_arrival_time_s',
        environment_aspects=frozenset(),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG | _MATERIAL_LIMITED,
            bounds=_MATERIAL_WEAK | {'derived_conversion'},
        ),
        directivity=_DimensionRule(
            supports={'measured_balloon', 'manufacturer_dataset'},
            bounds={
                'fitted_source_correction',
                'inferred_estimated',
                'simplified_model',
            },
        ),
        geometry=_DimensionRule(
            supports={'exact_scene_revision', 'asbuilt_measured'},
            bounds={'surveyed_partial', 'simplified'},
        ),
        pose=_DimensionRule(
            supports={'surveyed_acoustic_center', 'measured_position'},
            bounds={'intended_placement'},
        ),
    ),
    'modal_response': _ClaimRule(
        phenomenon='low_frequency_modal_response',
        observable='modal_frequency_hz',
        environment_aspects=frozenset({'temperature', 'speed_of_sound'}),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG,
            bounds=_MATERIAL_LIMITED | {'derived_conversion'},
        ),
        directivity=None,
        geometry=_DimensionRule(
            supports={'exact_scene_revision', 'asbuilt_measured'},
            bounds={'surveyed_partial'},
        ),
        pose=_DimensionRule(
            supports={'surveyed_acoustic_center', 'measured_position'},
            bounds={'intended_placement'},
        ),
    ),
    'decay_time': _ClaimRule(
        phenomenon='late_energy_decay',
        observable='decay_time_s',
        environment_aspects=frozenset({'temperature', 'humidity'}),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG | {'measured_energy_coefficient'},
            bounds=(
                _MATERIAL_LIMITED - {'measured_energy_coefficient'}
                | _MATERIAL_WEAK | {'derived_conversion'}
            ),
        ),
        directivity=None,
        geometry=_DimensionRule(
            supports={'exact_scene_revision', 'asbuilt_measured'},
            bounds={'surveyed_partial', 'simplified', 'assumed_nominal'},
        ),
        pose=None,
    ),
    'late_diffuse_field': _ClaimRule(
        phenomenon='scattering',
        observable='decay_time_s',
        environment_aspects=frozenset({'temperature', 'humidity'}),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG | {'measured_energy_coefficient'},
            bounds=(
                _MATERIAL_LIMITED - {'measured_energy_coefficient'}
                | {'derived_conversion', 'scalar_absorption_only'}
            ),
        ),
        directivity=None,
        geometry=_DimensionRule(
            supports={
                'exact_scene_revision', 'asbuilt_measured',
                'surveyed_partial',
            },
            bounds={'simplified', 'assumed_nominal'},
        ),
        pose=None,
    ),
    'phase_coherent_field': _ClaimRule(
        phenomenon='coherent_phase',
        observable='phase_deg',
        environment_aspects=frozenset({'temperature'}),
        material=_DimensionRule(
            supports={'measured_complex_boundary'},
            bounds={
                'measured_parameterized_model',
                'manufacturer_laboratory_exact',
                'derived_conversion',
            },
        ),
        directivity=_DimensionRule(
            supports={'measured_balloon'},
            bounds={'manufacturer_dataset', 'fitted_source_correction'},
        ),
        geometry=_DimensionRule(
            supports={'exact_scene_revision', 'asbuilt_measured'},
            bounds={'surveyed_partial'},
        ),
        pose=_DimensionRule(
            supports={'surveyed_acoustic_center', 'measured_position'},
            bounds={'intended_placement'},
        ),
    ),
    'speech_intelligibility': _ClaimRule(
        # STI rides on band SPL + SNR — the spatial pressure field path.
        phenomenon='spatial_pressure_field',
        observable=None,
        environment_aspects=frozenset({
            'door_opening_state', 'playback_state',
        }),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG | {'measured_energy_coefficient'},
            bounds=_MATERIAL_LIMITED - {'measured_energy_coefficient'}
            | {'derived_conversion', 'scalar_absorption_only'},
        ),
        directivity=_DimensionRule(
            supports={'measured_balloon', 'manufacturer_dataset'},
            bounds={
                'fitted_source_correction',
                'inferred_estimated',
                'simplified_model',
            },
        ),
        geometry=_DimensionRule(
            supports={
                'exact_scene_revision', 'asbuilt_measured',
                'surveyed_partial',
            },
            bounds={'simplified', 'assumed_nominal'},
        ),
        pose=_DimensionRule(
            supports={'surveyed_acoustic_center', 'measured_position'},
            bounds={'intended_placement', 'assumed'},
        ),
    ),
    'hybrid_overlap_consistency': _ClaimRule(
        phenomenon=None,
        observable='hybrid_overlap_level_db',
        environment_aspects=frozenset({'temperature'}),
        material=_DimensionRule(
            supports=_MATERIAL_STRONG,
            bounds=_MATERIAL_LIMITED | {'derived_conversion'},
        ),
        directivity=None,
        geometry=_DimensionRule(
            supports={'exact_scene_revision', 'asbuilt_measured'},
            bounds={'surveyed_partial', 'simplified'},
        ),
        pose=None,
    ),
}

#: Product-facing Japanese labels for the bound vocabulary.
CONFIDENCE_BOUND_LABELS: dict[str, str] = {
    # verdicts
    'envelope_inherited': 'エンベロープ継承（入力が拘束を弱めない）',
    'solver_bounded': 'ソルバー領域による拘束',
    'bounded_by_input': '入力権限による拘束',
    'claim_denied': 'クレーム拒否',
    'solver_unqualified': 'ソルバー未適格',
    'unbounded_input': '未宣言入力のため拘束不可',
    # ceilings
    'full_envelope': '検証済みエンベロープ全域',
    'solver_bound': 'ソルバー領域上限',
    'measured_bound': '実測上限',
    'documented_bound': '文書化上限',
    'limited_bound': '限定証拠上限',
    'derived_bound': '導出上限',
    'assumed_bound': '仮定上限',
    'insufficient_authority': '権限不足',
    # dimensions
    'material_boundary': '材料・境界',
    'source_directivity': '音源指向性',
    'geometry': '幾何形状',
    'pose': '配置・姿勢',
    'environment': '環境状態',
    'solver_domain': 'ソルバー検証領域',
    # material classes
    'measured_complex_boundary': '実測複素境界',
    'measured_parameterized_model': '実測パラメータ境界モデル',
    'manufacturer_laboratory_exact': 'メーカー厳密ラボデータ',
    'measured_energy_coefficient': '実測エネルギー係数',
    'manufacturer_declared': 'メーカー公称（不確かさなし）',
    'nonidentical_construction': '非同種構成',
    'fitted_effective': '校正済み実効係数（実測ではない）',
    'derived_conversion': '変換導出（宣言モデル）',
    'scalar_absorption_only': 'スカラー吸音のみ（位相なし）',
    'generic_table_lookup': '汎用テーブル参照',
    'inferred_construction': '推定構成',
    # directivity classes
    'measured_balloon': '実測バルーン',
    'manufacturer_dataset': 'メーカーデータセット',
    'fitted_source_correction': '校正済み音源補正（実測ではない）',
    'inferred_estimated': '推定指向性',
    'simplified_model': '簡略音源モデル',
    # geometry classes
    'exact_scene_revision': '厳密シーンリビジョン',
    'asbuilt_measured': '竣工実測',
    'surveyed_partial': '部分実測',
    'simplified': '簡略化ジオメトリ',
    'assumed_nominal': '公称幾何',
    # pose classes
    'surveyed_acoustic_center': '実測音響中心',
    'measured_position': '実測位置',
    'intended_placement': '設計意図配置',
    'assumed': '仮定',
    # shared
    'undeclared': '未宣言',
}

# ---------------------------------------------------------------------------
# Environmental binding — nested inside the input envelope, per aspect.
# ---------------------------------------------------------------------------

class EnvironmentalBinding(BaseModel):
    """One environmental/operating-state input (issue §6).

    Only the claims whose requirement map names this aspect can be
    staled by it — a door-state change never invalidates decay-time
    evidence, and humidity stales only absorption-sensitive claims.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    aspect: EnvironmentAspect
    state: EnvironmentStateClass
    value: str | None = Field(default=None, min_length=1)
    uncertainty: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'EnvironmentalBinding':
        if self.state == 'measured' and not self.value:
            raise ValueError(
                'a measured environmental binding requires its value'
            )
        if self.state == 'undeclared' and (
            self.value is not None or self.uncertainty is not None
        ):
            raise ValueError(
                'an undeclared environmental aspect cannot carry a value'
            )
        return self

# ---------------------------------------------------------------------------
# Sealed per-dimension input-authority records
# ---------------------------------------------------------------------------

class MaterialInputAuthority(BaseModel):
    """Boundary/material input authority for one solver run (mia-).

    The declared ``authority_class`` is the provenance — measured,
    manufacturer-declared, fitted or derived are never collapsed. Class
    rules:

    * measured classes pin evidence refs, an explicit method and a valid
      frequency band;
    * ``fitted_effective`` pins ``fitted_context`` — a calibrated value
      stays an effective calibrated parameter in its calibration
      context, never measured material truth (issue §7, Pilch 2020);
    * ``derived_conversion`` pins ``conversion_ref`` to the
      ``BoundaryConversionArtifact`` that already seals model, version,
      priors, assumptions, frequency validity, residuals and
      uncertainty (issue §2 conversion lineage);
    * ``fitted_context`` is forbidden outside ``fitted_effective`` — the
      class label cannot launder fitted provenance into measured.

    Scattering authority is only ever a
    ``scattering_coefficient_random_incidence`` quantity on a measured
    energy-coefficient class — a directional diffusion coefficient is
    not scattering and cannot be claimed as such here.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['material-input-authority-1'] = (
        MATERIAL_INPUT_AUTHORITY_VERSION
    )
    material_id: str
    material_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject_ref: AuthorityRef
    authority_class: MaterialAuthorityClass
    quantity: BoundaryPhysicalQuantity
    method_class: EvidenceMethodClass = 'unknown_method'
    phase: EvidencePhase = 'unknown_phase'
    incidence: EvidenceIncidence = 'unknown'
    incidence_angle_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    standard_revision: str | None = Field(default=None, min_length=1)
    laboratory: str | None = Field(default=None, min_length=1)
    frequency_validity_hz: tuple[float, float] | None = None
    uncertainty_declared: bool = False
    mounting_state: str | None = Field(default=None, min_length=1)
    evidence_refs: tuple[AuthorityRef, ...] = ()
    conversion_ref: AuthorityRef | None = None
    fitted_context: str | None = Field(default=None, min_length=1)
    provenance_note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'MaterialInputAuthority':
        _require_refs(self.subject_ref)
        _require_refs(*self.evidence_refs)
        if self.conversion_ref is not None:
            _require_refs(self.conversion_ref)
        if self.incidence_angle_deg is not None and self.incidence != (
            'specific_angle'
        ):
            raise ValueError(
                'incidence_angle_deg requires specific_angle incidence'
            )
        if self.frequency_validity_hz is not None:
            lo, hi = self.frequency_validity_hz
            if not (0.0 < lo <= hi):
                raise ValueError(
                    'frequency validity must be positive and ordered'
                )
        # Fitted provenance stays fitted — a calibrated value can never
        # carry a measured class label (issue §7).
        if self.fitted_context is not None and self.authority_class != (
            'fitted_effective'
        ):
            raise ValueError(
                'fitted_context marks fitted provenance — the authority '
                'class must be fitted_effective, never a measured class'
            )
        if self.authority_class == 'fitted_effective' and not (
            self.fitted_context
        ):
            raise ValueError(
                'fitted_effective requires its calibration context — '
                'a fitted value without its context claims measured truth'
            )
        if self.authority_class == 'measured_complex_boundary':
            if self.method_class not in (
                'iso_10534_2_impedance_tube',
                'full_complex_reflection_measurement',
                'in_situ_measurement',
            ):
                raise ValueError(
                    'a measured complex boundary requires a declared '
                    'measurement method (ISO 10534-2, full complex '
                    'reflection, or in-situ)'
                )
            if self.quantity not in (
                'surface_impedance',
                'surface_admittance',
                'complex_reflection_coefficient',
            ):
                raise ValueError(
                    'a measured complex boundary requires a complex '
                    'quantity (impedance/admittance/complex reflection)'
                )
            if self.phase not in ('complex_impedance', 'complex_reflection'):
                raise ValueError(
                    'a measured complex boundary requires complex phase'
                )
        if self.authority_class in (
            'measured_complex_boundary',
            'measured_parameterized_model',
            'manufacturer_laboratory_exact',
            'measured_energy_coefficient',
        ):
            if not self.evidence_refs:
                raise ValueError(
                    'a measured-class boundary input requires bound '
                    'evidence refs'
                )
            if self.method_class == 'unknown_method':
                raise ValueError(
                    'a measured-class boundary input requires a declared '
                    'method'
                )
            if self.frequency_validity_hz is None:
                raise ValueError(
                    'a measured-class boundary input requires its valid '
                    'frequency band'
                )
        if self.authority_class == 'manufacturer_laboratory_exact':
            if not self.laboratory:
                raise ValueError(
                    'exact manufacturer laboratory data requires the '
                    'laboratory identity'
                )
            if not self.uncertainty_declared:
                raise ValueError(
                    'exact manufacturer laboratory data requires usable '
                    'uncertainty — without it the class is '
                    'manufacturer_declared'
                )
        if self.authority_class == 'measured_energy_coefficient':
            if self.phase not in ('magnitude_energy_only', 'unknown_phase'):
                raise ValueError(
                    'an energy-coefficient input cannot claim complex '
                    'phase'
                )
        if self.authority_class == 'derived_conversion':
            if self.conversion_ref is None:
                raise ValueError(
                    'a derived conversion requires the sealed conversion '
                    'artifact — model, assumptions and uncertainty are '
                    'part of the claim'
                )
        if self.conversion_ref is not None and self.authority_class != (
            'derived_conversion'
        ):
            raise ValueError(
                'conversion_ref is only valid for derived_conversion — '
                'a measured class cannot be a conversion product'
            )
        if self.authority_class == 'nonidentical_construction' and not (
            self.provenance_note
        ):
            raise ValueError(
                'non-identical construction requires a provenance note '
                'naming the construction it actually matches'
            )
        if self.authority_class == 'undeclared':
            if self.evidence_refs or self.conversion_ref is not None:
                raise ValueError(
                    'an undeclared material input cannot pin evidence'
                )
        if self.material_sha256 != _hash(self.identity_payload()):
            raise ValueError('material input authority hash mismatch')
        expected_id = _semantic_id('mia', self.material_sha256)
        if self.material_id != expected_id:
            raise ValueError('material input authority id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'material_id', 'material_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'MaterialInputAuthority':
        return _seal(
            cls, payload, 'material_id', 'material_sha256', 'mia',
        )

class SourceDirectivityAuthority(BaseModel):
    """Source/directivity input authority (sda-).

    ``provenance_class`` carries the acoustic quality claim;
    ``format_compliance`` carries container conformance only — a valid
    SOFA/AES69 file is never acoustic quality validation (issue §4,
    AES69). Coverage, angular/frequency sampling, interpolation method,
    coordinate frame, normalization, operating state and the valid
    radiation domain are retained so a simplified source model can never
    silently upgrade the claims it feeds.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['source-directivity-authority-1'] = (
        DIRECTIVITY_INPUT_AUTHORITY_VERSION
    )
    directivity_id: str
    directivity_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject_ref: AuthorityRef
    provenance_class: DirectivityProvenanceClass
    dataset_ref: AuthorityRef | None = None
    format_compliance: DirectivityFormatCompliance = 'undeclared'
    coverage: DirectivityCoverage = 'unknown'
    simplified_model: SimplifiedSourceModel | None = None
    angular_sampling: str | None = Field(default=None, min_length=1)
    frequency_sampling: str | None = Field(default=None, min_length=1)
    interpolation_method: str | None = Field(default=None, min_length=1)
    coordinate_frame: str | None = Field(default=None, min_length=1)
    normalization_reference: str | None = Field(default=None, min_length=1)
    operating_state: str | None = Field(default=None, min_length=1)
    valid_radiation_domain: str | None = Field(default=None, min_length=1)
    fitted_context: str | None = Field(default=None, min_length=1)
    limitations: tuple[str, ...] = ()
    provenance_note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'SourceDirectivityAuthority':
        _require_refs(self.subject_ref)
        if self.dataset_ref is not None:
            _require_refs(self.dataset_ref)
        # Format compliance never upgrades provenance — it rides along
        # as container metadata only.
        if self.fitted_context is not None and self.provenance_class != (
            'fitted_source_correction'
        ):
            raise ValueError(
                'fitted_context marks a fitted source correction — the '
                'provenance class must be fitted_source_correction, '
                'never a measured class'
            )
        if self.provenance_class == 'measured_balloon':
            if self.dataset_ref is None:
                raise ValueError(
                    'a measured balloon requires its bound dataset'
                )
            if self.coverage == 'unknown':
                raise ValueError(
                    'a measured balloon requires declared angular coverage'
                )
            if not self.angular_sampling or not self.frequency_sampling:
                raise ValueError(
                    'a measured balloon requires angular and frequency '
                    'sampling declarations'
                )
            if not self.interpolation_method:
                raise ValueError(
                    'a measured balloon requires its interpolation method'
                )
            if not self.coordinate_frame or not (
                self.normalization_reference
            ):
                raise ValueError(
                    'a measured balloon requires coordinate frame and '
                    'normalization reference'
                )
        if self.provenance_class == 'manufacturer_dataset':
            if self.dataset_ref is None:
                raise ValueError(
                    'a manufacturer dataset requires its bound dataset'
                )
            if self.coverage == 'unknown':
                raise ValueError(
                    'a manufacturer dataset requires declared coverage'
                )
        if self.provenance_class == 'fitted_source_correction':
            if not self.fitted_context:
                raise ValueError(
                    'a fitted source correction requires its calibration '
                    'context — it is an effective correction, not a '
                    'measured directivity'
                )
        if self.provenance_class == 'simplified_model':
            if self.simplified_model is None:
                raise ValueError(
                    'a simplified source model requires its model kind '
                    '(omnidirectional/point/...) — an unnamed assumption '
                    'is not a model'
                )
            if self.dataset_ref is not None:
                raise ValueError(
                    'a simplified model cannot pin a measured dataset'
                )
        if self.simplified_model is not None and self.provenance_class != (
            'simplified_model'
        ):
            raise ValueError(
                'simplified_model is only valid for the simplified_model '
                'provenance class'
            )
        if self.provenance_class == 'undeclared':
            if self.dataset_ref is not None or self.format_compliance not in (
                'none', 'undeclared'
            ):
                raise ValueError(
                    'an undeclared directivity input cannot pin a dataset'
                )
        if self.directivity_sha256 != _hash(self.identity_payload()):
            raise ValueError('source directivity authority hash mismatch')
        expected_id = _semantic_id('sda', self.directivity_sha256)
        if self.directivity_id != expected_id:
            raise ValueError('source directivity authority id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'directivity_id', 'directivity_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'SourceDirectivityAuthority':
        return _seal(
            cls, payload, 'directivity_id', 'directivity_sha256', 'sda',
        )

class GeometryInputAuthority(BaseModel):
    """Geometry/scene input authority (gia-).

    ``fidelity_class`` separates an exact scene revision from as-built
    measurement, partial survey, declared simplification and assumed
    nominal geometry. Openings/obstructions/curvature state are retained
    so a simplified scene can never read as the measured room.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['geometry-input-authority-1'] = (
        GEOMETRY_INPUT_AUTHORITY_VERSION
    )
    geometry_id: str
    geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    fidelity_class: GeometryFidelityClass
    scene_revision_ref: AuthorityRef | None = None
    deviation_bound_m: float | None = Field(default=None, gt=0.0)
    uncertainty_ref: AuthorityRef | None = None
    openings_state: Literal[
        'modeled', 'partial', 'omitted', 'unknown',
    ] = 'unknown'
    obstructions_state: Literal[
        'modeled', 'partial', 'omitted', 'unknown',
    ] = 'unknown'
    curvature_fidelity: Literal[
        'exact', 'approximated', 'planar_only', 'unknown',
    ] = 'unknown'
    detail_policy: str | None = Field(default=None, min_length=1)
    provenance_note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'GeometryInputAuthority':
        if self.scene_revision_ref is not None:
            _require_refs(self.scene_revision_ref)
        if self.uncertainty_ref is not None:
            _require_refs(self.uncertainty_ref)
        if self.fidelity_class in (
            'exact_scene_revision', 'asbuilt_measured',
        ) and self.scene_revision_ref is None:
            raise ValueError(
                f'{self.fidelity_class} requires the pinned scene '
                'revision — fidelity without a revision is a claim '
                'about nothing'
            )
        if self.fidelity_class == 'asbuilt_measured' and (
            self.deviation_bound_m is None and self.uncertainty_ref is None
        ):
            raise ValueError(
                'as-built geometry requires a deviation bound or a '
                'spatial uncertainty reference'
            )
        if self.fidelity_class == 'surveyed_partial' and (
            self.deviation_bound_m is None and self.uncertainty_ref is None
        ):
            raise ValueError(
                'partial survey geometry requires a deviation bound or '
                'a spatial uncertainty reference'
            )
        if self.fidelity_class == 'simplified' and not self.detail_policy:
            raise ValueError(
                'a simplified scene requires its simplification policy — '
                'what was dropped is part of the claim'
            )
        if self.fidelity_class == 'undeclared' and (
            self.scene_revision_ref is not None
        ):
            raise ValueError(
                'an undeclared geometry input cannot pin a scene revision'
            )
        if self.geometry_sha256 != _hash(self.identity_payload()):
            raise ValueError('geometry input authority hash mismatch')
        expected_id = _semantic_id('gia', self.geometry_sha256)
        if self.geometry_id != expected_id:
            raise ValueError('geometry input authority id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'geometry_id', 'geometry_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'GeometryInputAuthority':
        return _seal(
            cls, payload, 'geometry_id', 'geometry_sha256', 'gia',
        )

class PoseInputAuthority(BaseModel):
    """Pose/placement input authority per subject (pia-).

    ``authority_class`` separates a surveyed acoustic center, a measured
    position, design-intent placement and an assumed pose. At low
    frequency a position perturbation is evaluated against modal
    sensitivity, not a cosmetic tolerance — ``modal_sensitivity``
    records whether that evaluation happened.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['pose-input-authority-1'] = (
        POSE_INPUT_AUTHORITY_VERSION
    )
    pose_id: str
    pose_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    subject_kind: PoseSubjectKind
    subject_ref: AuthorityRef
    authority_class: PoseAuthorityClass
    position_bound_m: float | None = Field(default=None, gt=0.0)
    orientation_bound_deg: float | None = Field(default=None, gt=0.0)
    acoustic_center_ref: AuthorityRef | None = None
    pose_observation_ref: AuthorityRef | None = None
    modal_sensitivity: ModalSensitivity = 'unevaluated'
    provenance_note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'PoseInputAuthority':
        _require_refs(self.subject_ref)
        for ref in (self.acoustic_center_ref, self.pose_observation_ref):
            if ref is not None:
                _require_refs(ref)
        if self.authority_class == 'surveyed_acoustic_center':
            if self.acoustic_center_ref is None:
                raise ValueError(
                    'a surveyed acoustic center requires the source-'
                    'origin authority that defines the acoustic center'
                )
            if self.position_bound_m is None:
                raise ValueError(
                    'a surveyed acoustic center requires its position '
                    'bound'
                )
        if self.authority_class == 'measured_position':
            if self.position_bound_m is None and (
                self.pose_observation_ref is None
            ):
                raise ValueError(
                    'a measured position requires a position bound or a '
                    'bound pose observation'
                )
        if self.authority_class == 'assumed' and (
            self.acoustic_center_ref is not None
            or self.pose_observation_ref is not None
        ):
            raise ValueError(
                'an assumed pose cannot pin measurement evidence'
            )
        if self.authority_class == 'undeclared' and (
            self.acoustic_center_ref is not None
            or self.pose_observation_ref is not None
        ):
            raise ValueError(
                'an undeclared pose input cannot pin evidence'
            )
        if self.modal_sensitivity == 'evaluated' and (
            self.position_bound_m is None
        ):
            raise ValueError(
                'an evaluated modal sensitivity requires the position '
                'bound that was perturbed'
            )
        if self.pose_sha256 != _hash(self.identity_payload()):
            raise ValueError('pose input authority hash mismatch')
        expected_id = _semantic_id('pia', self.pose_sha256)
        if self.pose_id != expected_id:
            raise ValueError('pose input authority id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'pose_id', 'pose_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'PoseInputAuthority':
        return _seal(cls, payload, 'pose_id', 'pose_sha256', 'pia')

class SolverInputEnvelope(BaseModel):
    """The sealed input-qualification bundle for one solver run (sie-).

    Binds the solver request/capability context and every input-authority
    record by sha-pinned ref — material, source directivity, geometry,
    pose — plus the environmental bindings. An envelope with no record
    for a dimension is legal: evaluation then reports that dimension as
    undeclared and stales only the claims that materially need it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['solver-input-envelope-1'] = (
        SOLVER_INPUT_ENVELOPE_VERSION
    )
    envelope_id: str
    envelope_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    solver_request_ref: AuthorityRef
    capability_manifest_ref: AuthorityRef | None = None
    solver_envelope_ref: AuthorityRef | None = None
    scene_revision_ref: AuthorityRef | None = None
    material_refs: tuple[AuthorityRef, ...] = ()
    directivity_refs: tuple[AuthorityRef, ...] = ()
    geometry_refs: tuple[AuthorityRef, ...] = ()
    pose_refs: tuple[AuthorityRef, ...] = ()
    environment: tuple[EnvironmentalBinding, ...] = ()
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'SolverInputEnvelope':
        _require_refs(self.solver_request_ref)
        for ref in (
            self.capability_manifest_ref, self.solver_envelope_ref,
            self.scene_revision_ref,
        ):
            if ref is not None:
                _require_refs(ref)
        for refs in (
            self.material_refs, self.directivity_refs,
            self.geometry_refs, self.pose_refs,
        ):
            _require_refs(*refs)
            ref_ids = [ref.ref_id for ref in refs]
            if len(ref_ids) != len(set(ref_ids)):
                raise ValueError('envelope refs must be unique')
        aspects = [binding.aspect for binding in self.environment]
        if len(aspects) != len(set(aspects)):
            raise ValueError(
                'environmental bindings must declare each aspect once'
            )
        if self.envelope_sha256 != _hash(self.identity_payload()):
            raise ValueError('solver input envelope hash mismatch')
        expected_id = _semantic_id('sie', self.envelope_sha256)
        if self.envelope_id != expected_id:
            raise ValueError('solver input envelope id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'envelope_id', 'envelope_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'SolverInputEnvelope':
        return _seal(
            cls, payload, 'envelope_id', 'envelope_sha256', 'sie',
        )

# ---------------------------------------------------------------------------
# Claim bound row + record
# ---------------------------------------------------------------------------

class ClaimBoundRow(BaseModel):
    """One claim class's bound verdict inside a ClaimBoundRecord."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    claim: ClaimClass
    verdict: BoundVerdict
    ceiling: ConfidenceCeiling
    weakest_dimensions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    supporting_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'ClaimBoundRow':
        if self.verdict == 'envelope_inherited':
            if self.ceiling != 'full_envelope':
                raise ValueError(
                    'envelope_inherited requires the full_envelope ceiling'
                )
        elif self.verdict == 'solver_bounded':
            if self.ceiling != 'solver_bound':
                raise ValueError(
                    'solver_bounded requires the solver_bound ceiling'
                )
        elif self.verdict == 'bounded_by_input':
            if self.ceiling not in (
                'measured_bound', 'documented_bound', 'limited_bound',
                'derived_bound', 'assumed_bound',
            ):
                raise ValueError(
                    'bounded_by_input requires an input-authority ceiling'
                )
            if not self.weakest_dimensions:
                raise ValueError(
                    'bounded_by_input must name its weakest dimensions'
                )
        else:
            # claim_denied / solver_unqualified / unbounded_input
            if self.ceiling != 'insufficient_authority':
                raise ValueError(
                    f'{self.verdict} requires the insufficient_authority '
                    'ceiling — a failed gate never reads as bounded'
                )
            if not self.weakest_dimensions:
                raise ValueError(
                    f'{self.verdict} must name the failing dimensions'
                )
        if self.verdict in ('claim_denied', 'unbounded_input') and not (
            self.reasons
        ):
            raise ValueError(
                f'{self.verdict} requires its reasons'
            )
        _require_refs(*self.supporting_refs)
        return self

class ClaimBoundRecord(BaseModel):
    """The sealed per-claim confidence bound for one input envelope (cbr-).

    Covers every claim class exactly once — no silent gaps, the same
    discipline as the solver capability manifest. Downstream surfaces
    consume this record to know which claim classes are permitted,
    bounded or denied for the exact input-authority combination; they
    never re-derive confidence from a solver benchmark alone.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal['claim-bound-record-1'] = (
        CLAIM_BOUND_RECORD_VERSION
    )
    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    input_envelope_ref: AuthorityRef
    evaluator_version: Literal['confidence-bound-evaluator-1'] = (
        CONFIDENCE_BOUND_EVALUATOR_VERSION
    )
    rows: tuple[ClaimBoundRow, ...]
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'ClaimBoundRecord':
        _require_refs(self.input_envelope_ref)
        claims = [row.claim for row in self.rows]
        if sorted(claims) != sorted(CLAIM_CLASSES):
            missing = sorted(set(CLAIM_CLASSES) - set(claims))
            duplicated = sorted(
                {item for item in claims if claims.count(item) > 1}
            )
            raise ValueError(
                'a claim bound record must evaluate every claim class '
                f'exactly once (missing={missing}, duplicated={duplicated})'
            )
        if self.rows != tuple(
            sorted(self.rows, key=lambda row: str(row.claim))
        ):
            raise ValueError(
                'claim bound rows must be sorted by claim class'
            )
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('claim bound record hash mismatch')
        expected_id = _semantic_id('cbr', self.record_sha256)
        if self.record_id != expected_id:
            raise ValueError('claim bound record id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'},
        )

    @classmethod
    def create(cls, **payload: Any) -> 'ClaimBoundRecord':
        return _seal(cls, payload, 'record_id', 'record_sha256', 'cbr')

    def row_for(self, claim: ClaimClass) -> ClaimBoundRow:
        return next(row for row in self.rows if row.claim == claim)

# ---------------------------------------------------------------------------
# Evaluator — per-claim bound, weakest input identified, fail closed
# ---------------------------------------------------------------------------

_SUPPORT_ORDER: dict[str, int] = {
    'undeclared': 0,
    'denies': 1,
    'bounds': 2,
    'supports': 3,
}

def _dimension_support(
    rule: _DimensionRule,
    rank_map: dict[str, AuthorityRank],
    classes: Sequence[str] | None,
) -> tuple[BoundRowSupport, AuthorityRank, str | None]:
    """Weakest record in one dimension, judged against the claim rule.

    ``classes=None`` means the dimension has no bound records — the
    materially relevant input is undeclared and the claim fails closed.
    Otherwise every record in the dimension must pass the claim's rule;
    the weakest class sets the support and the rank.
    """
    if not classes:
        return ('undeclared', 'insufficient', None)
    support: BoundRowSupport = 'supports'
    weakest_rank: AuthorityRank = 'measured'
    weakest_class: str | None = None
    for cls in classes:
        if cls in rule.supports:
            s: BoundRowSupport = 'supports'
        elif cls in rule.bounds:
            s = 'bounds'
        else:
            s = 'denies'
        rank = rank_map.get(cls, 'insufficient')
        if _SUPPORT_ORDER[s] < _SUPPORT_ORDER[support]:
            support = s
        if _RANK_ORDER[rank] < _RANK_ORDER[weakest_rank]:
            weakest_rank = rank
            weakest_class = cls
    return (support, weakest_rank, weakest_class)

def _environment_support(
    aspects: frozenset[str],
    environment: Sequence[EnvironmentalBinding],
) -> tuple[BoundRowSupport, AuthorityRank, str | None]:
    """Dependency-aware environmental support for the required aspects.

    Only the aspects the claim names are checked; an aspect with no
    binding — or an ``undeclared``-state binding — stales just this
    claim, never unrelated evidence (issue §6).
    """
    bound = {binding.aspect: binding for binding in environment}
    support: BoundRowSupport = 'supports'
    weakest_rank: AuthorityRank = 'measured'
    weakest_aspect: str | None = None
    for aspect in aspects:
        binding = bound.get(aspect)
        state = 'undeclared' if binding is None else binding.state
        rank = _ENVIRONMENT_RANK.get(state, 'insufficient')
        if state in ('measured', 'declared'):
            s: BoundRowSupport = 'supports'
        elif state == 'assumed':
            s = 'bounds'
        else:
            s = 'undeclared'
        if _SUPPORT_ORDER[s] < _SUPPORT_ORDER[support]:
            support = s
        if _RANK_ORDER[rank] < _RANK_ORDER[weakest_rank]:
            weakest_rank = rank
            weakest_aspect = aspect
        elif weakest_aspect is None:
            weakest_aspect = aspect
    return (support, weakest_rank, weakest_aspect)

def _has_scattering_evidence(
    materials: Sequence[MaterialInputAuthority],
) -> bool:
    """ISO 17497-1 random-incidence scattering evidence only.

    A directional diffusion coefficient is not scattering and a default
    scatter fraction is not measured truth — neither satisfies this.
    """
    for record in materials:
        if record.quantity == _SCATTERING_QUANTITY and (
            record.authority_class == 'measured_energy_coefficient'
        ) and record.method_class in (
            'iso_17497_1_scattering', 'in_situ_measurement',
        ):
            return True
    return False

def evaluate_claim_bound(
    claim: ClaimClass,
    *,
    materials: Sequence[MaterialInputAuthority] | None,
    directivities: Sequence[SourceDirectivityAuthority] | None,
    geometries: Sequence[GeometryInputAuthority] | None,
    poses: Sequence[PoseInputAuthority] | None,
    environment: Sequence[EnvironmentalBinding],
    solver_envelopes: Sequence[AccuracyEnvelopeRecord] = (),
    manifest: SolverCapabilityManifest | None = None,
) -> ClaimBoundRow:
    """Bound one claim class by its materially relevant input authority.

    Gate order (first failing gate reports):

    1. ``unbounded_input`` — a materially relevant dimension has no bound
       record, or a required environmental aspect is unbound. Undeclared
       is never a mid-trust default.
    2. ``claim_denied`` — a declared input authority cannot support this
       claim class at any level (e.g. scalar absorption feeding a
       phase-coherent claim; an assumed pose feeding an at-position one).
    3. ``solver_unqualified`` — the solver validation domain does not
       cover the claim (envelope missing/non-covering, or the capability
       manifest marks the phenomenon UNSUPPORTED).
    4. ``bounded_by_input`` / ``solver_bounded`` / ``envelope_inherited`` —
       the claim is permitted at the minimum of input and solver
       ceilings, with the weakest dimension(s) named.
    """
    rule = _CLAIM_REQUIREMENTS[claim]
    reasons: list[str] = []
    weakest: list[str] = []
    bounding: list[str] = []
    supporting: list[AuthorityRef] = []

    def _classes(
        records: Sequence[Any] | None, attr: str,
    ) -> list[str] | None:
        if records is None:
            return None
        return [getattr(record, attr) for record in records]

    # --- input gates -------------------------------------------------
    material_support = material_rank = material_cls = None
    if rule.material is not None:
        material_support, material_rank, material_cls = _dimension_support(
            rule.material, _MATERIAL_RANK,
            _classes(materials, 'authority_class'),
        )
        if material_support == 'supports' and claim == 'late_diffuse_field':
            if not _has_scattering_evidence(materials or ()):  # type: ignore[arg-type]
                material_support = 'bounds'
                material_rank = 'limited'
                reasons.append(
                    'no ISO 17497-1 random-incidence scattering evidence — '
                    'unknown scattering propagates into late/high-order '
                    'field claims'
                )
        if material_support == 'undeclared':
            weakest.append('material_boundary')
            reasons.append(
                'material/boundary input authority undeclared'
            )
        elif material_support == 'denies':
            weakest.append('material_boundary')
            reasons.append(
                f'material class {material_cls} cannot support {claim}'
            )
        elif material_support == 'bounds':
            bounding.append('material_boundary')

    directivity_support = directivity_rank = directivity_cls = None
    if rule.directivity is not None:
        (
            directivity_support, directivity_rank, directivity_cls,
        ) = _dimension_support(
            rule.directivity, _DIRECTIVITY_RANK,
            _classes(directivities, 'provenance_class'),
        )
        if directivity_support == 'undeclared':
            weakest.append('source_directivity')
            reasons.append(
                'source directivity authority undeclared'
            )
        elif directivity_support == 'denies':
            weakest.append('source_directivity')
            reasons.append(
                f'directivity class {directivity_cls} cannot support '
                f'{claim}'
            )
        elif directivity_support == 'bounds':
            bounding.append('source_directivity')

    geometry_support = geometry_rank = geometry_cls = None
    if rule.geometry is not None:
        geometry_support, geometry_rank, geometry_cls = _dimension_support(
            rule.geometry, _GEOMETRY_RANK,
            _classes(geometries, 'fidelity_class'),
        )
        if geometry_support == 'undeclared':
            weakest.append('geometry')
            reasons.append('geometry fidelity authority undeclared')
        elif geometry_support == 'denies':
            weakest.append('geometry')
            reasons.append(
                f'geometry fidelity {geometry_cls} cannot support {claim}'
            )
        elif geometry_support == 'bounds':
            bounding.append('geometry')

    pose_support = pose_rank = pose_cls = None
    if rule.pose is not None:
        pose_support, pose_rank, pose_cls = _dimension_support(
            rule.pose, _POSE_RANK,
            _classes(poses, 'authority_class'),
        )
        if pose_support == 'undeclared':
            weakest.append('pose')
            reasons.append('pose/placement authority undeclared')
        elif pose_support == 'denies':
            weakest.append('pose')
            reasons.append(
                f'pose authority {pose_cls} cannot support {claim}'
            )
        elif (
            pose_support == 'supports'
            and claim in MODAL_SENSITIVE_CLAIMS
            and any(
                record.modal_sensitivity == 'unevaluated'
                for record in (poses or ())
            )
        ):
            pose_support = 'bounds'
            pose_rank = 'documented'
            bounding.append('pose')
            reasons.append(
                'position perturbation not evaluated against the local '
                'modal gradient — at-position claims bound by spatial '
                'uncertainty'
            )
        elif pose_support == 'bounds':
            bounding.append('pose')

    environment_support = environment_rank = environment_aspect = None
    if rule.environment_aspects:
        (
            environment_support, environment_rank, environment_aspect,
        ) = _environment_support(rule.environment_aspects, environment)
        if environment_support == 'undeclared':
            weakest.append('environment')
            reasons.append(
                f'environmental aspect {environment_aspect} undeclared'
            )
        elif environment_support == 'bounds':
            bounding.append('environment')

    if weakest:
        verdict: BoundVerdict = (
            'unbounded_input'
            if any(
                s == 'undeclared' for s in (
                    material_support, directivity_support,
                    geometry_support, pose_support, environment_support,
                )
            )
            else 'claim_denied'
        )
        return ClaimBoundRow(
            claim=claim,
            verdict=verdict,
            ceiling='insufficient_authority',
            weakest_dimensions=tuple(weakest),
            reasons=tuple(reasons),
            supporting_refs=(),
        )

    for record in tuple(materials or ()):
        supporting.append(record.subject_ref)
    for record in tuple(directivities or ()):
        supporting.append(record.subject_ref)
    for record in tuple(geometries or ()):
        if record.scene_revision_ref is not None:
            supporting.append(record.scene_revision_ref)
    for record in tuple(poses or ()):
        supporting.append(record.subject_ref)

    # --- solver gate --------------------------------------------------
    # Each *applicable* solver authority contributes a rank; the weakest
    # counts (the claim is never stronger than its weakest solver
    # evidence). A claim with no applicable solver authority at all is
    # unqualified; an explicit gap (no envelope for the claim's
    # observable, manifest UNSUPPORTED) reports as solver_unqualified.
    solver_ranks: list[AuthorityRank] = []
    solver_reasons: list[str] = []
    observable = rule.observable
    if observable is not None:
        matching = tuple(
            env for env in solver_envelopes
            if env.observable == observable
        )
        if not matching:
            solver_reasons.append(
                f'no accuracy envelope covers {observable}'
            )
            solver_ranks.append('insufficient')
        else:
            # The weakest matching envelope sets the solver-domain bound —
            # a solver is never stronger than its weakest declared
            # validation for the claimed observable.
            best = min(
                matching,
                key=lambda env: _RANK_ORDER[
                    _SOLVER_STATE_RANK[env.validation_state]
                ],
            )
            solver_ranks.append(
                _SOLVER_STATE_RANK[best.validation_state]
            )
            if best.validation_state not in (
                'VALIDATED_FOR_DECLARED_DOMAIN',
                'VALIDATED_WITH_LIMITATIONS',
            ):
                solver_reasons.append(
                    f'solver validation state {best.validation_state}'
                )
    if manifest is not None and rule.phenomenon is not None:
        row = manifest.row_for(rule.phenomenon)  # type: ignore[arg-type]
        if row.state == 'UNSUPPORTED':
            solver_ranks.append('insufficient')
            solver_reasons.append(
                f'capability manifest marks {rule.phenomenon} UNSUPPORTED'
            )
        elif row.state == 'BOUNDED':
            solver_ranks.append('limited')
        else:
            # SUPPORTED is a capability gate, not accuracy evidence: it
            # never weakens an accuracy envelope already covering the
            # claim's observable, but on its own (observable-free claims)
            # it qualifies only at documented strength.
            solver_ranks.append(
                'documented' if observable is None else 'measured'
            )
    solver_rank: AuthorityRank = (
        min(solver_ranks, key=lambda rank: _RANK_ORDER[rank])
        if solver_ranks
        else 'insufficient'
    )
    if not solver_ranks or solver_rank == 'insufficient':
        return ClaimBoundRow(
            claim=claim,
            verdict='solver_unqualified',
            ceiling='insufficient_authority',
            weakest_dimensions=(SOLVER_DOMAIN,),
            reasons=tuple(
                solver_reasons or [
                    'no solver validation envelope or capability manifest '
                    'bound — nothing declares this solver covers the claim'
                ]
            ),
            supporting_refs=(),
        )

    # --- combined bound ------------------------------------------------
    input_ranks: list[tuple[AuthorityRank, str]] = []
    if rule.material is not None:
        input_ranks.append((material_rank, 'material_boundary'))  # type: ignore[arg-type]
    if rule.directivity is not None:
        input_ranks.append((directivity_rank, 'source_directivity'))  # type: ignore[arg-type]
    if rule.geometry is not None:
        input_ranks.append((geometry_rank, 'geometry'))  # type: ignore[arg-type]
    if rule.pose is not None:
        input_ranks.append((pose_rank, 'pose'))  # type: ignore[arg-type]
    if rule.environment_aspects:
        input_ranks.append((environment_rank, 'environment'))  # type: ignore[arg-type]
    input_rank = min(
        input_ranks, key=lambda item: _RANK_ORDER[item[0]],
    )[0]
    bounded_dims = sorted({
        dim
        for rank, dim in input_ranks
        if _RANK_ORDER[rank] == _RANK_ORDER[input_rank]
    } | set(bounding))
    bound_supports = [
        s for s in (
            material_support, directivity_support,
            geometry_support, pose_support, environment_support,
        ) if s is not None
    ]

    if _RANK_ORDER[solver_rank] < _RANK_ORDER[input_rank]:
        return ClaimBoundRow(
            claim=claim,
            verdict='solver_bounded',
            ceiling='solver_bound',
            weakest_dimensions=(SOLVER_DOMAIN,),
            reasons=tuple(
                solver_reasons or [
                    'solver validation domain sets the bound — inputs '
                    'are not the weakest evidence'
                ]
            ),
            supporting_refs=tuple(supporting),
        )
    if input_rank != 'measured' or any(
        s == 'bounds' for s in bound_supports
    ):
        return ClaimBoundRow(
            claim=claim,
            verdict='bounded_by_input',
            ceiling=_RANK_CEILING[input_rank],
            weakest_dimensions=tuple(bounded_dims),
            reasons=tuple(
                reasons or [
                    f'{dim} authority bounds the claim at '
                    f'{_RANK_CEILING[input_rank]}'
                    for dim in bounded_dims
                ]
            ),
            supporting_refs=tuple(supporting),
        )
    return ClaimBoundRow(
        claim=claim,
        verdict='envelope_inherited',
        ceiling='full_envelope',
        weakest_dimensions=(),
        reasons=tuple(
            reasons or [
                'all materially relevant inputs meet the claim rule — '
                'the result inherits the solver validation domain'
            ]
        ),
        supporting_refs=tuple(supporting),
    )

def _resolve_envelope_refs(
    refs: Sequence[AuthorityRef],
    records: Sequence[Any],
    id_field: str,
    sha_field: str,
    label: str,
) -> tuple[Any, ...]:
    """Bind envelope refs to records — integrity checked, not positional.

    Every ref must resolve to exactly one record whose id matches and
    whose sealed sha matches the pinned ``ref_sha256``; extra records
    outside the envelope are ignored (the envelope names its inputs).
    """
    by_id = {getattr(record, id_field): record for record in records}
    resolved: list[Any] = []
    for ref in refs:
        record = by_id.get(ref.ref_id)
        if record is None:
            raise ValueError(
                f'{label} ref {ref.ref_id} does not resolve to a bound '
                'record'
            )
        if getattr(record, sha_field) != ref.ref_sha256:
            raise ValueError(
                f'{label} ref {ref.ref_id} sha does not match the '
                'envelope pin'
            )
        resolved.append(record)
    return tuple(resolved)

def evaluate_input_envelope(
    envelope: SolverInputEnvelope,
    *,
    materials: Sequence[MaterialInputAuthority] = (),
    directivities: Sequence[SourceDirectivityAuthority] = (),
    geometries: Sequence[GeometryInputAuthority] = (),
    poses: Sequence[PoseInputAuthority] = (),
    solver_envelopes: Sequence[AccuracyEnvelopeRecord] = (),
    manifest: SolverCapabilityManifest | None = None,
    evaluated_at_utc: str,
) -> ClaimBoundRecord:
    """Evaluate every claim class against the sealed input envelope.

    Resolves the envelope's refs to the bound records (integrity-checked
    id+sha), evaluates all ten claim classes once each, and seals the
    verdict record — the artifact downstream surfaces consume.
    """
    bound_materials = _resolve_envelope_refs(
        envelope.material_refs, materials, 'material_id',
        'material_sha256', 'material',
    )
    bound_directivities = _resolve_envelope_refs(
        envelope.directivity_refs, directivities, 'directivity_id',
        'directivity_sha256', 'directivity',
    )
    bound_geometries = _resolve_envelope_refs(
        envelope.geometry_refs, geometries, 'geometry_id',
        'geometry_sha256', 'geometry',
    )
    bound_poses = _resolve_envelope_refs(
        envelope.pose_refs, poses, 'pose_id', 'pose_sha256', 'pose',
    )
    # The solver-side pins are authority refs like every other one:
    # when the envelope declares them, only the pinned records may feed
    # the gate — a caller-passed envelope/manifest that contradicts the
    # seal cannot silently substitute.
    if envelope.solver_envelope_ref is not None:
        bound_solver_envelopes = _resolve_envelope_refs(
            (envelope.solver_envelope_ref,), solver_envelopes,
            'envelope_id', 'envelope_sha256', 'solver_envelope',
        )
    else:
        bound_solver_envelopes = tuple(solver_envelopes)
    if envelope.capability_manifest_ref is not None:
        bound_manifest = _resolve_envelope_refs(
            (envelope.capability_manifest_ref,),
            (manifest,) if manifest is not None else (),
            'manifest_id', 'semantic_sha256', 'capability_manifest',
        )[0]
    else:
        bound_manifest = manifest
    rows = tuple(
        sorted(
            (
                evaluate_claim_bound(
                    claim,
                    materials=bound_materials,
                    directivities=bound_directivities,
                    geometries=bound_geometries,
                    poses=bound_poses,
                    environment=envelope.environment,
                    solver_envelopes=bound_solver_envelopes,
                    manifest=bound_manifest,
                )
                for claim in CLAIM_CLASSES
            ),
            key=lambda row: str(row.claim),
        )
    )
    return ClaimBoundRecord.create(
        document_id=envelope.document_id,
        input_envelope_ref=AuthorityRef(
            kind='solver_input_envelope',
            ref_id=envelope.envelope_id,
            ref_sha256=envelope.envelope_sha256,
        ),
        rows=rows,
        evaluated_at_utc=evaluated_at_utc,
    )
