"""Blind listening / ABX subjective-evaluation authority (#518).

Subjective listening evidence is a distinct reproducible evidence type: it is
not Measurement evidence, not solver validation, and never an automatic
"best design" selector. A session pins the exact alternatives (exact
AuralizationArtifact / playback source refs), the level-matching policy, the
protocol (A/B preference vs ABX discrimination — different claims, different
statistics), the preregistered fixed-N analysis parameters and the committed
randomization seed before the first scored trial.

Trial assignments are generated deterministically from the committed seed and
are append-only once answered. An early-aborted session never receives the
unchanged fixed-N inferential claim. Preference results are descriptive, not
objective acoustic validation.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import random
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


LISTENING_SCHEMA_VERSION = 1
LISTENING_SPEC_AUTHORITY_VERSION = 'blind-listening-spec-1'
LISTENING_RESULT_AUTHORITY_VERSION = 'blind-listening-result-1'

ListeningProtocol = Literal['ab_preference', 'abx_discrimination']
LevelMatchingPolicyKind = Literal[
    'preserve_physical_level',
    'rms_matched_window',
    'declared_method',
]
RandomizationAlgorithm = Literal['python_random_mt19937_v1']
ABX_ANALYSIS_METHOD = 'exact_binomial_two_sided_v1'
PREFERENCE_ANALYSIS_METHOD = 'descriptive_counts_v1'
EXCERPT_BOUNDS_EQUALITY = 'matched_program_position_required'


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


class ListeningAlternative(BaseModel):
    """One exact presentation alternative bound to an audio authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    presentation_label: Literal['A', 'B']
    alternative_id: str = Field(min_length=1)
    artifact_id: str | None = Field(default=None, min_length=1)
    artifact_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    playback_source_id: str | None = Field(default=None, min_length=1)
    playback_source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    evidence_kind: Literal['predicted_auralization', 'measured_auralization', 'direct_playback']

    @model_validator(mode='after')
    def validate_alternative(self) -> 'ListeningAlternative':
        artifact = self.artifact_id is not None
        playback = self.playback_source_id is not None
        if artifact == playback:
            raise ValueError(
                'an alternative pins exactly one artifact or playback source'
            )
        if (self.artifact_id is None) != (self.artifact_sha256 is None):
            raise ValueError('artifact id/hash must be supplied together')
        if (self.playback_source_id is None) != (self.playback_source_sha256 is None):
            raise ValueError('playback source id/hash must be supplied together')
        return self


class LevelMatchingPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: LevelMatchingPolicyKind
    method_id: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    rms_window_s: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def validate_policy(self) -> 'LevelMatchingPolicy':
        if self.kind == 'rms_matched_window' and self.rms_window_s is None:
            raise ValueError('rms_matched_window requires an explicit window')
        if self.kind != 'rms_matched_window' and self.rms_window_s is not None:
            raise ValueError('rms window only applies to rms_matched_window')
        return self


class RandomizationCommitment(BaseModel):
    """Randomization authority committed before the first scored trial."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    algorithm: RandomizationAlgorithm = 'python_random_mt19937_v1'
    committed_seed: int = Field(ge=0)
    sequence_length: int = Field(ge=0)


class AbxAnalysisSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    method: Literal['exact_binomial_two_sided_v1'] = ABX_ANALYSIS_METHOD
    null_success_probability: float = 0.5
    significance_alpha: float | None = Field(default=None, gt=0.0, lt=1.0)

    @model_validator(mode='after')
    def validate_analysis(self) -> 'AbxAnalysisSpec':
        # exact_binomial_two_sided_v1 is defined only for the symmetric
        # guessing null: the 2*min(lower, upper) two-sided shortcut is not a
        # valid two-sided p for any other null probability (#955).
        if self.null_success_probability != 0.5:
            raise ValueError(
                'exact_binomial_two_sided_v1 requires null_success_probability '
                '0.5; a different null needs a different preregistered method'
            )
        return self


class ListeningSessionSpec(BaseModel):
    """Preregistered immutable blind-listening session design.

    Fixed-N parameters (planned scored trials, null probability, analysis
    method, threshold, randomization, level policy, replay policy) freeze at
    spec creation — the spec hash *is* the preregistration.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LISTENING_SCHEMA_VERSION
    authority_version: Literal[
        'blind-listening-spec-1'
    ] = LISTENING_SPEC_AUTHORITY_VERSION
    spec_id: str = Field(pattern=r'^listening-session-spec:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    protocol: ListeningProtocol
    alternatives: tuple[ListeningAlternative, ListeningAlternative]
    level_policy: LevelMatchingPolicy
    program_material_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    excerpt_start_s: float = Field(ge=0.0)
    excerpt_end_s: float = Field(gt=0.0)
    excerpt_position_semantics: Literal[
        'matched_program_position_required'
    ] = EXCERPT_BOUNDS_EQUALITY
    planned_scored_trials: int = Field(gt=0)
    planned_training_trials: int = Field(ge=0, default=0)
    randomization: RandomizationCommitment
    replay_policy: Literal['unlimited', 'none'] = 'unlimited'
    analysis: AbxAnalysisSpec | None = None
    listener_pseudonym: str | None = Field(default=None, min_length=1)
    notes: str | None = None

    @field_validator('excerpt_start_s', 'excerpt_end_s')
    @classmethod
    def finite_bounds(cls, value: float) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('excerpt bounds must be finite')
        return number

    @model_validator(mode='after')
    def validate_spec(self) -> 'ListeningSessionSpec':
        labels = [item.presentation_label for item in self.alternatives]
        if labels != ['A', 'B']:
            raise ValueError('session alternatives must be exactly A and B')
        if self.alternatives[0].alternative_id == self.alternatives[1].alternative_id:
            raise ValueError('alternatives must be distinct')
        if self.excerpt_end_s <= self.excerpt_start_s:
            raise ValueError('excerpt end must exceed start')
        expected_length = (
            self.planned_scored_trials + self.planned_training_trials
        )
        if self.randomization.sequence_length != expected_length:
            raise ValueError(
                'randomization sequence length must cover training + scored trials'
            )
        if self.protocol == 'abx_discrimination':
            if self.analysis is None:
                raise ValueError('ABX protocol requires a preregistered analysis spec')
            if self.analysis.null_success_probability <= 0.0 or (
                self.analysis.null_success_probability >= 1.0
            ):
                raise ValueError('ABX null probability must be inside (0, 1)')
        elif self.analysis is not None:
            raise ValueError('preference protocol uses descriptive reporting only')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('listening session spec semantic hash mismatch')
        if self.spec_id != f'listening-session-spec:{expected}':
            raise ValueError('listening session spec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'spec_id', 'semantic_sha256'},
        )


def build_listening_session_spec(**kwargs: Any) -> ListeningSessionSpec:
    probe = ListeningSessionSpec.model_construct(
        spec_id='listening-session-spec:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return ListeningSessionSpec(
        spec_id=f'listening-session-spec:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


class StimulusPreflightFinding(BaseModel):
    """Cue-leakage check result for blinded stimulus parity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    check: str = Field(min_length=1)
    # UNKNOWN means the required verification was not performed — it never
    # counts as a pass (#954).
    state: Literal['PASS', 'FAIL', 'UNKNOWN']
    detail: str | None = None


class TrialAssignment(BaseModel):
    """One committed random assignment; hidden from normal trial UI."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    trial_index: int = Field(ge=0)
    kind: Literal['training', 'scored']
    # ABX: the hidden identity of X. Preference: the presentation order.
    assignment: Literal['a', 'b', 'ab', 'ba']


def generate_assignment_sequence(
    spec: ListeningSessionSpec,
) -> tuple[TrialAssignment, ...]:
    """Deterministic sequence from the committed seed (mt19937, seeded)."""
    rng = random.Random(spec.randomization.committed_seed)
    assignments: list[TrialAssignment] = []
    scored_index = 0
    training_index = 0
    for index in range(spec.randomization.sequence_length):
        kind: Literal['training', 'scored'] = (
            'training'
            if training_index < spec.planned_training_trials
            else 'scored'
        )
        if kind == 'training':
            training_index += 1
        else:
            scored_index += 1
        if spec.protocol == 'abx_discrimination':
            assignment: Literal['a', 'b', 'ab', 'ba'] = rng.choice(('a', 'b'))
        else:
            assignment = rng.choice(('ab', 'ba'))
        assignments.append(
            TrialAssignment(
                trial_index=index, kind=kind, assignment=assignment
            )
        )
    return tuple(assignments)


def randomization_commitment_sha256(
    spec: ListeningSessionSpec,
    assignments: tuple[TrialAssignment, ...],
) -> str:
    """Commit hash over seed + full sequence; stored before trial one."""
    return _digest(
        {
            'kind': 'listening-randomization-commitment',
            'spec_semantic_sha256': spec.semantic_sha256,
            'committed_seed': spec.randomization.committed_seed,
            'assignments': [
                item.model_dump(mode='json') for item in assignments
            ],
        }
    )


class ListeningTrialRecord(BaseModel):
    """Append-only scored/training trial evidence.

    Persisted records keep the assignment for reproducibility (the product's
    own database is not treated as adversarial); the trial UI receives the
    blinded view only.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    trial_index: int = Field(ge=0)
    kind: Literal['training', 'scored']
    assignment: Literal['a', 'b', 'ab', 'ba']
    response: Literal['a', 'b', 'prefer_a', 'prefer_b', 'no_preference']
    correct: bool | None = None
    committed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_record(self) -> 'ListeningTrialRecord':
        if self.assignment in ('a', 'b'):
            if self.response not in ('a', 'b'):
                raise ValueError('ABX response must answer a/b identity')
            if self.correct is None:
                raise ValueError('ABX trial record requires a correctness verdict')
            if self.correct != (self.assignment == self.response):
                raise ValueError('ABX correctness verdict mismatch')
        else:
            if self.response not in ('prefer_a', 'prefer_b', 'no_preference'):
                raise ValueError('preference response must be prefer_a/prefer_b/no_preference')
            if self.correct is not None:
                raise ValueError('preference trials never carry correctness')
        return self


def exact_binomial_two_sided_p(
    successes: int,
    trials: int,
    null_probability: float = 0.5,
) -> float:
    """Exact two-sided binomial p-value (method exact_binomial_two_sided_v1).

    2 * min(P(X <= k), P(X >= k)) under the preregistered null, capped at 1.
    """
    if trials <= 0 or successes < 0 or successes > trials:
        raise ValueError('invalid binomial trial counts')
    if not (0.0 < null_probability < 1.0):
        raise ValueError('null probability must be inside (0, 1)')
    low = sum(
        math.comb(trials, i)
        * null_probability**i
        * (1.0 - null_probability) ** (trials - i)
        for i in range(0, successes + 1)
    )
    high = sum(
        math.comb(trials, i)
        * null_probability**i
        * (1.0 - null_probability) ** (trials - i)
        for i in range(successes, trials + 1)
    )
    return min(1.0, 2.0 * min(low, high))


class SubjectiveListeningResult(BaseModel):
    """Immutable closed-session result; subjective, never objective evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = LISTENING_SCHEMA_VERSION
    authority_version: Literal[
        'blind-listening-result-1'
    ] = LISTENING_RESULT_AUTHORITY_VERSION
    result_id: str = Field(pattern=r'^listening-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(pattern=r'^listening-session-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    randomization_commitment_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    protocol: ListeningProtocol
    completed_scored_trials: int = Field(ge=0)
    planned_scored_trials: int = Field(gt=0)
    aborted: bool
    inferential_state: Literal[
        'fixed_n_evaluable',
        'fixed_n_not_reached',
        'non_inferential',
        'descriptive_only',
    ]
    correct_count: int | None = None
    exact_binomial_p: float | None = None
    preference_counts: tuple[int, int, int] | None = None
    analysis_method: str = Field(min_length=1)
    # Preregistered analysis parameters are persisted on the result so replay
    # uses the pinned null/alpha, not defaults (#955).
    null_success_probability: float | None = None
    significance_alpha: float | None = None
    # The bound stimulus-parity preflight authority (#954); persisted so the
    # inferential claim is auditable.
    stimulus_preflight_state: Literal['PASS', 'FAIL', 'MISSING'] | None = None
    stimulus_preflight: tuple[StimulusPreflightFinding, ...] | None = None
    assignments: tuple[TrialAssignment, ...] = ()
    blinded_labels_preserved: Literal[True] = True

    @model_validator(mode='after')
    def validate_result(self) -> 'SubjectiveListeningResult':
        if self.aborted != (self.completed_scored_trials != self.planned_scored_trials):
            raise ValueError('aborted must equal completed != planned')
        if self.inferential_state == 'fixed_n_evaluable':
            if self.aborted:
                raise ValueError(
                    'an early-aborted session never receives the fixed-N claim'
                )
            if self.protocol != 'abx_discrimination':
                raise ValueError('fixed_n_evaluable only applies to ABX')
            if self.correct_count is None or self.exact_binomial_p is None:
                raise ValueError('evaluable ABX requires correctness + p')
            if self.null_success_probability is None:
                raise ValueError(
                    'evaluable ABX must persist the preregistered null probability'
                )
            if self.stimulus_preflight_state != 'PASS':
                raise ValueError(
                    'fixed_n_evaluable requires a passing bound stimulus '
                    'preflight'
                )
            expected_p = exact_binomial_two_sided_p(
                self.correct_count,
                self.completed_scored_trials,
                self.null_success_probability,
            )
            if abs(self.exact_binomial_p - expected_p) > 1e-12:
                raise ValueError('reported p-value does not match exact binomial')
        else:
            if self.exact_binomial_p is not None:
                raise ValueError(
                    'non-evaluable sessions must not report a fixed-N p-value'
                )
        if self.inferential_state == 'non_inferential' and (
            self.stimulus_preflight_state == 'PASS'
        ):
            raise ValueError(
                'a passing preflight cannot produce a non-inferential result'
            )
        if self.protocol == 'abx_discrimination':
            if self.null_success_probability is None:
                raise ValueError(
                    'ABX results persist the preregistered null probability'
                )
            if self.stimulus_preflight_state is None:
                raise ValueError(
                    'ABX results must record the bound preflight state'
                )
        if self.protocol == 'ab_preference':
            if self.preference_counts is None:
                raise ValueError('preference results require counts')
            if self.correct_count is not None:
                raise ValueError('preference results never report correctness')
            if sum(self.preference_counts) != self.completed_scored_trials:
                raise ValueError('preference counts must cover scored trials')
        else:
            if self.correct_count is not None and (
                self.correct_count < 0
                or self.correct_count > self.completed_scored_trials
            ):
                raise ValueError('correct count outside completed trials')
            if self.preference_counts is not None:
                raise ValueError('ABX results never report preference counts')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('listening result semantic hash mismatch')
        if self.result_id != f'listening-result:{expected}':
            raise ValueError('listening result id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )


class BlindedTrialView(BaseModel):
    """What the trial UI may see: no identity-bearing fields."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    trial_index: int = Field(ge=0)
    kind: Literal['training', 'scored']
    responses_allowed: tuple[str, ...]
    replay_policy: Literal['unlimited', 'none']


def blinded_trial_view(
    spec: ListeningSessionSpec,
    assignment: TrialAssignment,
) -> BlindedTrialView:
    return BlindedTrialView(
        trial_index=assignment.trial_index,
        kind=assignment.kind,
        responses_allowed=(
            ('a', 'b')
            if spec.protocol == 'abx_discrimination'
            else ('prefer_a', 'prefer_b', 'no_preference')
        ),
        replay_policy=spec.replay_policy,
    )


class BlindListeningSession:
    """Runtime session: committed sequence, append-only trials, late reveal.

    The stimulus-parity preflight authority is bound before the first scored
    trial (#954): a session without a passing bound preflight can still run
    (diagnostic use), but its result is 'non_inferential' — it never
    receives the fixed-N claim.
    """

    def __init__(
        self,
        spec: ListeningSessionSpec,
        *,
        stimulus_preflight: tuple[StimulusPreflightFinding, ...] | None = None,
    ) -> None:
        self.spec = spec
        self._stimulus_preflight = (
            tuple(stimulus_preflight)
            if stimulus_preflight is not None
            else None
        )
        self._assignments = generate_assignment_sequence(spec)
        self.randomization_commitment_sha256 = randomization_commitment_sha256(
            spec, self._assignments
        )
        self._records: list[ListeningTrialRecord] = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stimulus_preflight(
        self,
    ) -> tuple[StimulusPreflightFinding, ...] | None:
        return self._stimulus_preflight

    @property
    def preflight_state(self) -> Literal['PASS', 'FAIL', 'MISSING']:
        return _preflight_state(self.spec, self._stimulus_preflight)

    @property
    def readiness(self) -> Literal['ready', 'blocked', 'compromised']:
        """UI-facing lifecycle state for the bound preflight (#954)."""
        state = self.preflight_state
        if state == 'MISSING':
            return 'blocked'
        if state == 'FAIL':
            return 'compromised'
        return 'ready'

    def trial_view(self, trial_index: int) -> BlindedTrialView:
        with self._lock:
            if self._closed:
                raise ValueError('session is closed')
            if trial_index != len(self._records):
                raise ValueError('trials must run in committed order')
            return blinded_trial_view(
                self.spec, self._assignments[trial_index]
            )

    def commit_response(
        self,
        response: Literal['a', 'b', 'prefer_a', 'prefer_b', 'no_preference'],
        *,
        committed_at_utc: str,
    ) -> ListeningTrialRecord:
        with self._lock:
            if self._closed:
                raise ValueError('cannot commit to a closed session')
            index = len(self._records)
            if index >= len(self._assignments):
                raise ValueError('trial sequence exhausted')
            assignment = self._assignments[index]
            correct: bool | None = None
            if assignment.assignment in ('a', 'b'):
                correct = assignment.assignment == response
            record = ListeningTrialRecord(
                spec_semantic_sha256=self.spec.semantic_sha256,
                trial_index=index,
                kind=assignment.kind,
                assignment=assignment.assignment,
                response=response,
                correct=correct,
                committed_at_utc=committed_at_utc,
            )
            self._records.append(record)
            return record

    @property
    def records(self) -> tuple[ListeningTrialRecord, ...]:
        return tuple(self._records)

    def close(self) -> SubjectiveListeningResult:
        with self._lock:
            if self._closed:
                raise ValueError('session is already closed')
            self._closed = True
            scored = [
                record for record in self._records if record.kind == 'scored'
            ]
            completed = len(scored)
            planned = self.spec.planned_scored_trials
            aborted = completed != planned
            preflight_state = self.preflight_state
            preflight_findings = (
                [item.model_dump(mode='json') for item in self._stimulus_preflight]
                if self._stimulus_preflight is not None
                else None
            )
            if self.spec.protocol == 'abx_discrimination':
                correct = sum(1 for record in scored if record.correct)
                if aborted:
                    inferential_state = 'fixed_n_not_reached'
                    p_value = None
                elif (
                    preflight_state != 'PASS'
                    or self.readiness != 'ready'
                ):
                    # A failed/missing/incomplete preflight never yields a
                    # fixed-N inferential claim (#954).
                    inferential_state = 'non_inferential'
                    p_value = None
                else:
                    inferential_state = 'fixed_n_evaluable'
                    p_value = exact_binomial_two_sided_p(
                        correct, completed,
                        self.spec.analysis.null_success_probability,
                    )
                payload: dict[str, Any] = {
                    'schema_version': LISTENING_SCHEMA_VERSION,
                    'authority_version': LISTENING_RESULT_AUTHORITY_VERSION,
                    'spec_id': self.spec.spec_id,
                    'spec_semantic_sha256': self.spec.semantic_sha256,
                    'randomization_commitment_sha256': self.randomization_commitment_sha256,
                    'protocol': self.spec.protocol,
                    'completed_scored_trials': completed,
                    'planned_scored_trials': planned,
                    'aborted': aborted,
                    'inferential_state': inferential_state,
                    'correct_count': correct,
                    'exact_binomial_p': p_value,
                    'preference_counts': None,
                    'analysis_method': ABX_ANALYSIS_METHOD,
                    'null_success_probability': (
                        self.spec.analysis.null_success_probability
                    ),
                    'significance_alpha': self.spec.analysis.significance_alpha,
                    'stimulus_preflight_state': preflight_state,
                    'stimulus_preflight': preflight_findings,
                    'assignments': [
                        item.model_dump(mode='json')
                        for item in self._assignments
                    ],
                    'blinded_labels_preserved': True,
                }
            else:
                counts = (
                    sum(1 for r in scored if r.response == 'prefer_a'),
                    sum(1 for r in scored if r.response == 'prefer_b'),
                    sum(1 for r in scored if r.response == 'no_preference'),
                )
                inferential_state = 'descriptive_only'
                payload = {
                    'schema_version': LISTENING_SCHEMA_VERSION,
                    'authority_version': LISTENING_RESULT_AUTHORITY_VERSION,
                    'spec_id': self.spec.spec_id,
                    'spec_semantic_sha256': self.spec.semantic_sha256,
                    'randomization_commitment_sha256': self.randomization_commitment_sha256,
                    'protocol': self.spec.protocol,
                    'completed_scored_trials': completed,
                    'planned_scored_trials': planned,
                    'aborted': aborted,
                    'inferential_state': inferential_state,
                    'correct_count': None,
                    'exact_binomial_p': None,
                    'preference_counts': list(counts),
                    'analysis_method': PREFERENCE_ANALYSIS_METHOD,
                    'null_success_probability': None,
                    'significance_alpha': None,
                    'stimulus_preflight_state': preflight_state,
                    'stimulus_preflight': preflight_findings,
                    'assignments': [
                        item.model_dump(mode='json')
                        for item in self._assignments
                    ],
                    'blinded_labels_preserved': True,
                }
            digest = _digest(payload)
            if self.spec.protocol == 'abx_discrimination':
                return SubjectiveListeningResult(
                    spec_id=self.spec.spec_id,
                    spec_semantic_sha256=self.spec.semantic_sha256,
                    randomization_commitment_sha256=self.randomization_commitment_sha256,
                    protocol=self.spec.protocol,
                    completed_scored_trials=completed,
                    planned_scored_trials=planned,
                    aborted=aborted,
                    inferential_state=inferential_state,
                    correct_count=correct,
                    exact_binomial_p=p_value,
                    analysis_method=ABX_ANALYSIS_METHOD,
                    null_success_probability=(
                        self.spec.analysis.null_success_probability
                    ),
                    significance_alpha=self.spec.analysis.significance_alpha,
                    stimulus_preflight_state=preflight_state,
                    stimulus_preflight=self._stimulus_preflight,
                    assignments=self._assignments,
                    result_id=f'listening-result:{digest}',
                    semantic_sha256=digest,
                )
            return SubjectiveListeningResult(
                spec_id=self.spec.spec_id,
                spec_semantic_sha256=self.spec.semantic_sha256,
                randomization_commitment_sha256=self.randomization_commitment_sha256,
                protocol=self.spec.protocol,
                completed_scored_trials=completed,
                planned_scored_trials=planned,
                aborted=aborted,
                inferential_state='descriptive_only',
                preference_counts=counts,
                analysis_method=PREFERENCE_ANALYSIS_METHOD,
                stimulus_preflight_state=preflight_state,
                stimulus_preflight=self._stimulus_preflight,
                assignments=self._assignments,
                result_id=f'listening-result:{digest}',
                semantic_sha256=digest,
            )


# Identity-cue checks every blinded session must pass, plus the level-policy
# check selected by the session's own policy (#954).
_CUE_CHECKS = (
    'output_format_match',
    'excerpt_bounds_match',
    'clipping_free',
    'seamless_switch',
)


def _required_preflight_checks(
    spec: ListeningSessionSpec,
) -> frozenset[str]:
    """Checks a session must see PASS to bind a passing preflight (#954)."""
    if spec.level_policy.kind == 'rms_matched_window':
        # RMS level matching over the declared window must be numerically
        # verified, not assumed.
        level_checks = ('rms_level_match',)
    elif spec.level_policy.kind == 'preserve_physical_level':
        # The level difference itself is the tested presentation.
        level_checks = ('level_difference_is_tested',)
    else:
        # declared_method requires a documented method authority.
        level_checks = ('declared_method_authority',)
    return frozenset(_CUE_CHECKS + level_checks)


def _preflight_state(
    spec: ListeningSessionSpec,
    findings: tuple[StimulusPreflightFinding, ...] | None,
) -> Literal['PASS', 'FAIL', 'MISSING']:
    if findings is None:
        return 'MISSING'
    by_check = {item.check: item.state for item in findings}
    for check in _required_preflight_checks(spec):
        if by_check.get(check) != 'PASS':
            return 'FAIL'
    return 'PASS'


def stimulus_parity_preflight(
    *,
    sample_rate_hz_a: int,
    sample_rate_hz_b: int,
    channel_count_a: int,
    channel_count_b: int,
    excerpt_bounds_a_s: tuple[float, float],
    excerpt_bounds_b_s: tuple[float, float],
    clipped_a: bool,
    clipped_b: bool,
    seamless_switch_supported: bool,
    rms_level_match_verified: bool | None = None,
    physical_level_is_tested_difference: bool | None = None,
    declared_method_authority: bool | None = None,
) -> tuple[StimulusPreflightFinding, ...]:
    """Check the session alternatives for non-acoustic identity cues.

    Level-policy checks are emitted as UNKNOWN when the caller did not
    perform the corresponding verification — never silently skipped (#954).
    """
    def _tri(value: bool | None) -> Literal['PASS', 'FAIL', 'UNKNOWN']:
        if value is None:
            return 'UNKNOWN'
        return 'PASS' if value else 'FAIL'

    findings = [
        StimulusPreflightFinding(
            check='output_format_match',
            state='PASS'
            if (sample_rate_hz_a, channel_count_a)
            == (sample_rate_hz_b, channel_count_b)
            else 'FAIL',
            detail=(
                None
                if (sample_rate_hz_a, channel_count_a)
                == (sample_rate_hz_b, channel_count_b)
                else 'sample rate/channel format differs between alternatives'
            ),
        ),
        StimulusPreflightFinding(
            check='excerpt_bounds_match',
            state='PASS'
            if excerpt_bounds_a_s == excerpt_bounds_b_s
            else 'FAIL',
            detail=(
                None
                if excerpt_bounds_a_s == excerpt_bounds_b_s
                else 'excerpt boundaries/program position differ'
            ),
        ),
        StimulusPreflightFinding(
            check='clipping_free',
            state='PASS' if not (clipped_a or clipped_b) else 'FAIL',
            detail=(
                None
                if not (clipped_a or clipped_b)
                else 'at least one alternative clips; clipping is a cue'
            ),
        ),
        StimulusPreflightFinding(
            check='seamless_switch',
            state='PASS' if seamless_switch_supported else 'FAIL',
            detail=(
                None
                if seamless_switch_supported
                else 'switch latency/gap may reveal identity; mark the session'
            ),
        ),
        StimulusPreflightFinding(
            check='rms_level_match',
            state=_tri(rms_level_match_verified),
            detail=(
                'rms level match numerically verified over the declared window'
                if rms_level_match_verified
                else (
                    'rms level match FAILED over the declared window'
                    if rms_level_match_verified is False
                    else 'rms level match not verified'
                )
            ),
        ),
        StimulusPreflightFinding(
            check='level_difference_is_tested',
            state=_tri(physical_level_is_tested_difference),
            detail=(
                'level difference is the tested presentation'
                if physical_level_is_tested_difference
                else (
                    'level difference between alternatives is an unintended cue'
                    if physical_level_is_tested_difference is False
                    else 'physical-level difference semantics not declared'
                )
            ),
        ),
        StimulusPreflightFinding(
            check='declared_method_authority',
            state=_tri(declared_method_authority),
            detail=(
                'level-matching method authority present'
                if declared_method_authority
                else (
                    'level-matching method authority absent'
                    if declared_method_authority is False
                    else 'level-matching method authority not supplied'
                )
            ),
        ),
    ]
    return tuple(findings)


class SessionCurrency(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    spec_id: str = Field(min_length=1)
    state: Literal['CURRENT', 'STALE']
    stale_reasons: tuple[str, ...]

    @model_validator(mode='after')
    def validate_state(self) -> 'SessionCurrency':
        if self.state == 'CURRENT' and self.stale_reasons:
            raise ValueError('CURRENT session cannot carry stale reasons')
        if self.state == 'STALE' and not self.stale_reasons:
            raise ValueError('STALE session requires reasons')
        return self


def assess_session_currency(
    spec: ListeningSessionSpec,
    *,
    current_alternative_sha256: tuple[str, str],
    current_program_material_sha256: str,
) -> SessionCurrency:
    """A session result goes stale when any pinned alternative/asset changes."""
    reasons: list[str] = []
    pinned = (
        spec.alternatives[0].artifact_sha256 or spec.alternatives[0].playback_source_sha256,
        spec.alternatives[1].artifact_sha256 or spec.alternatives[1].playback_source_sha256,
    )
    if pinned != current_alternative_sha256:
        reasons.append('an alternative artifact/source changed')
    if spec.program_material_sha256 != current_program_material_sha256:
        reasons.append('program material changed')
    return SessionCurrency(
        spec_id=spec.spec_id,
        state='STALE' if reasons else 'CURRENT',
        stale_reasons=tuple(reasons),
    )
