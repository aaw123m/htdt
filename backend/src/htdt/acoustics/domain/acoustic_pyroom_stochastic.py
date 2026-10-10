from __future__ import annotations

from hashlib import sha256
from math import isfinite, sqrt
from pathlib import Path
from statistics import mean, stdev
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_bakeoff import (
    BakeoffCandidateManifest,
    BakeoffFixtureEvidence,
    BakeoffObservableEvidence,
    observable_tolerance_violations,
)
from .acoustic_benchmark import (
    AcousticBenchmarkFixture,
    AcousticBenchmarkManifest,
    canonical_benchmark_json,
)


class PyroomStochasticAuthority(BaseModel):
    """Pre-registered R100B controls for the pinned pyroomacoustics candidate."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-pyroom-stochastic-1'] = 'r100b-pyroom-stochastic-1'
    authority_id: str = Field(min_length=1)
    fixture_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    backend_version: Literal['0.10.1'] = '0.10.1'
    same_seed_repeats: Literal[2] = 2
    independent_seeds: tuple[int, ...] = Field(min_length=3)
    ray_budgets: tuple[int, ...] = Field(min_length=3)
    sampling_rate_hz: int = Field(gt=0)
    receiver_radius_sequence_m: tuple[float, ...] = Field(min_length=3)
    histogram_bin_size_sequence_s: tuple[float, ...] = Field(min_length=3)
    energy_threshold: float = Field(gt=0.0)
    time_threshold_s: float = Field(gt=0.0)
    frequency_hz: tuple[float, ...] = Field(min_length=1)
    sample_times_s: tuple[float, ...] = Field(min_length=2)
    estimator: Literal['normalized-schroeder-energy-decay-db']
    stddev_ddof: Literal[1] = 1
    require_exact_same_seed_histogram_replay: Literal[True] = True
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_controls(self) -> 'PyroomStochasticAuthority':
        if len(set(self.independent_seeds)) != len(self.independent_seeds):
            raise ValueError('independent seeds must be unique')
        if any(seed < 0 or seed >= 2**64 for seed in self.independent_seeds):
            raise ValueError('independent seeds must fit the unsigned 64-bit libroom seed range')
        if len(set(self.ray_budgets)) != len(self.ray_budgets):
            raise ValueError('ray budgets must be unique')
        if any(value <= 0 for value in self.ray_budgets):
            raise ValueError('ray budgets must be positive')
        if any(
            self.ray_budgets[index] >= self.ray_budgets[index + 1]
            for index in range(len(self.ray_budgets) - 1)
        ):
            raise ValueError('ray budgets must be strictly increasing')
        if len(set(self.frequency_hz)) != len(self.frequency_hz):
            raise ValueError('frequencies must be unique')
        if any(value <= 0.0 or not isfinite(value) for value in self.frequency_hz):
            raise ValueError('frequencies must be finite and positive')
        if any(value <= 0.0 or not isfinite(value) for value in self.sample_times_s):
            raise ValueError('sample times must be finite and positive')
        if any(
            self.sample_times_s[index] >= self.sample_times_s[index + 1]
            for index in range(len(self.sample_times_s) - 1)
        ):
            raise ValueError('sample times must be strictly increasing')
        if self.sample_times_s[-1] >= self.time_threshold_s:
            raise ValueError('sample times must be strictly below the ray time threshold')
        if len(self.receiver_radius_sequence_m) != len(self.ray_budgets):
            raise ValueError('receiver-radius refinement must match ray-budget levels')
        if len(self.histogram_bin_size_sequence_s) != len(self.ray_budgets):
            raise ValueError('histogram-bin refinement must match ray-budget levels')
        if any(
            not isfinite(value) or value <= 0.0
            for value in self.receiver_radius_sequence_m + self.histogram_bin_size_sequence_s
        ):
            raise ValueError('estimator refinement controls must be finite and positive')
        if any(
            self.receiver_radius_sequence_m[index]
            <= self.receiver_radius_sequence_m[index + 1]
            for index in range(len(self.receiver_radius_sequence_m) - 1)
        ):
            raise ValueError('receiver radius must decrease strictly with refinement')
        if any(
            self.histogram_bin_size_sequence_s[index]
            <= self.histogram_bin_size_sequence_s[index + 1]
            for index in range(len(self.histogram_bin_size_sequence_s) - 1)
        ):
            raise ValueError('histogram bin size must decrease strictly with refinement')
        for histogram_bin_size_s in self.histogram_bin_size_sequence_s:
            for sample_time in self.sample_times_s:
                bin_number = round(sample_time / histogram_bin_size_s)
                aligned = bin_number * histogram_bin_size_s
                if abs(aligned - sample_time) > 1e-12:
                    raise ValueError(
                        'sample times must align exactly to every histogram-bin refinement level'
                    )
        return self

    def canonical_json(self) -> str:
        return canonical_benchmark_json(self.model_dump(mode='json'))

    def semantic_hash(self) -> str:
        return sha256(self.canonical_json().encode('utf-8')).hexdigest()


def load_pyroom_stochastic_authority(path: str | Path) -> PyroomStochasticAuthority:
    return PyroomStochasticAuthority.model_validate_json(Path(path).read_text(encoding='utf-8'))


def stochastic_fixture_semantic_hash(fixture: AcousticBenchmarkFixture) -> str:
    payload = canonical_benchmark_json(fixture.model_dump(mode='json'))
    return sha256(payload.encode('utf-8')).hexdigest()


def stochastic_sample_keys(authority: PyroomStochasticAuthority) -> tuple[str, ...]:
    return tuple(
        f'f{frequency:g}Hz-t{sample_time:.3f}s'
        for frequency in authority.frequency_hz
        for sample_time in authority.sample_times_s
    )


ObservationStatus = Literal['ok', 'insufficient_support']


class PyroomStochasticObservation(BaseModel):
    """One backend-produced stochastic observation with no pass/fail judgment."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    seed: int = Field(ge=0, lt=2**64)
    ray_budget: int = Field(gt=0)
    receiver_radius_m: float = Field(gt=0.0)
    histogram_bin_size_s: float = Field(gt=0.0)
    replicate: int = Field(ge=0)
    status: ObservationStatus
    sample_keys: tuple[str, ...] = Field(min_length=1)
    values_db: tuple[float, ...] = ()
    selected_band_centers_hz: tuple[float, ...] = Field(min_length=1)
    histogram_shape: tuple[int, int]
    histogram_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    archive_member: str = Field(min_length=1)
    histogram_total_energy: float = Field(ge=0.0)
    nonzero_bin_count: int = Field(ge=0)
    setup_s: float = Field(ge=0.0)
    solve_s: float = Field(ge=0.0)
    postprocess_s: float = Field(ge=0.0)
    peak_ram_mb: float = Field(ge=0.0)
    raw_output_bytes: int = Field(ge=0)
    diagnostic: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_observation(self) -> 'PyroomStochasticObservation':
        metrics = (
            self.receiver_radius_m,
            self.histogram_bin_size_s,
            self.histogram_total_energy,
            self.setup_s,
            self.solve_s,
            self.postprocess_s,
            self.peak_ram_mb,
        )
        if any(not isfinite(float(value)) for value in metrics):
            raise ValueError('stochastic observation metrics must be finite')
        if any(value <= 0 for value in self.histogram_shape):
            raise ValueError('histogram shape entries must be positive')
        if self.status == 'ok':
            if len(self.values_db) != len(self.sample_keys):
                raise ValueError('ok observation must provide one value for every sample key')
            if any(not isfinite(float(value)) for value in self.values_db):
                raise ValueError('stochastic observation values must be finite')
        elif self.values_db:
            raise ValueError('insufficient_support observation must not invent response values')
        return self


class PyroomStochasticRawEvidence(BaseModel):
    """Raw evidence envelope bound to the exact current authority identities."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-pyroom-stochastic-raw-1'] = (
        'r100b-pyroom-stochastic-raw-1'
    )
    evidence_ref: str = Field(min_length=1)
    r100a_manifest_id: str = Field(min_length=1)
    r100a_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    fixture_id: str = Field(min_length=1)
    fixture_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    stochastic_authority_id: str = Field(min_length=1)
    stochastic_authority_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_manifest_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    backend_version: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    wheel_filename: str | None = None
    wheel_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    raw_archive_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_archive_bytes: int = Field(ge=0)
    dependencies: tuple[str, ...] = Field(min_length=1)
    observations: tuple[PyroomStochasticObservation, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_observations(self) -> 'PyroomStochasticRawEvidence':
        ids = [item.observation_id for item in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError('stochastic observation ids must be unique')
        identities = [(item.seed, item.ray_budget, item.replicate) for item in self.observations]
        if len(identities) != len(set(identities)):
            raise ValueError('stochastic observation seed/budget/replicate identities must be unique')
        return self


class StochasticBudgetVariation(BaseModel):
    model_config = ConfigDict(frozen=True)

    ray_budget: int = Field(gt=0)
    receiver_radius_m: float = Field(gt=0.0)
    histogram_bin_size_s: float = Field(gt=0.0)
    seed_count: int = Field(ge=2)
    mean_curve_db: tuple[float, ...] = Field(min_length=1)
    point_stddev_db: tuple[float, ...] = Field(min_length=1)
    max_point_stddev_db: float = Field(ge=0.0)


class StochasticBudgetResourceScaling(BaseModel):
    """Descriptive per-budget resource evidence for the independent-seed set."""

    model_config = ConfigDict(frozen=True)

    ray_budget: int = Field(gt=0)
    receiver_radius_m: float = Field(gt=0.0)
    histogram_bin_size_s: float = Field(gt=0.0)
    observation_count: int = Field(ge=1)
    supported_observation_count: int = Field(ge=0)
    setup_s_total: float = Field(ge=0.0)
    solve_s_total: float = Field(ge=0.0)
    solve_s_mean: float = Field(ge=0.0)
    postprocess_s_total: float = Field(ge=0.0)
    peak_ram_mb: float = Field(ge=0.0)
    raw_output_bytes_total: int = Field(ge=0)


class PyroomStochasticEvaluation(BaseModel):
    """Explicitly separates seed replay from statistical convergence."""

    model_config = ConfigDict(frozen=True)

    status: Literal['pass', 'fail']
    repeatability_status: Literal['pass', 'fail']
    exact_histogram_replay: bool
    exact_curve_replay: bool
    replay_max_absolute_error_db: float | None = Field(default=None, ge=0.0)
    replay_relative_rms_error: float | None = Field(default=None, ge=0.0)
    convergence_status: Literal['pass', 'fail']
    monotonic_budget_refinement: bool
    adjacent_budget_mean_rms_delta_db: tuple[float, ...] = ()
    final_budget_absolute_rms_delta_db: float | None = Field(default=None, ge=0.0)
    final_budget_relative_rms_delta: float | None = Field(default=None, ge=0.0)
    finest_budget_seed_stddev_max_db: float | None = Field(default=None, ge=0.0)
    budget_variation: tuple[StochasticBudgetVariation, ...] = ()
    resource_scaling: tuple[StochasticBudgetResourceScaling, ...] = ()
    insufficient_observation_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


def _rms(values: tuple[float, ...] | list[float]) -> float:
    if not values:
        raise ValueError('RMS requires at least one value')
    return sqrt(sum(float(value) ** 2 for value in values) / len(values))


def _rms_delta(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError('RMS delta requires equal non-empty vectors')
    return _rms([a - b for a, b in zip(left, right)])


def _relative_rms(error_rms: float, reference: tuple[float, ...]) -> float | None:
    reference_rms = _rms(reference)
    if reference_rms == 0.0:
        return 0.0 if error_rms == 0.0 else None
    return error_rms / reference_rms


def _resource_violations(
    fixture: AcousticBenchmarkFixture,
    *,
    setup_s: float,
    solve_s: float,
    postprocess_s: float,
    peak_ram_mb: float,
    disk_mb: float,
    output_mb: float,
) -> list[str]:
    budget = fixture.resource_budget
    checks = (
        ('setup/compile_s', setup_s, budget.max_compile_s),
        ('solve_s', solve_s, budget.max_solve_s),
        ('postprocess_s', postprocess_s, budget.max_postprocess_s),
        ('peak_ram_mb', peak_ram_mb, float(budget.ram_budget_mb)),
        ('disk_mb', disk_mb, float(budget.disk_budget_mb)),
        ('output_mb', output_mb, budget.max_output_mb),
    )
    return [
        f'{name}={value:.9g} exceeds frozen R100A budget {limit:.9g}'
        for name, value, limit in checks
        if value > limit
    ]


def evaluate_pyroom_stochastic_fixture(
    benchmark: AcousticBenchmarkManifest,
    candidates: BakeoffCandidateManifest,
    fixture: AcousticBenchmarkFixture,
    authority: PyroomStochasticAuthority,
    raw: PyroomStochasticRawEvidence,
) -> tuple[BakeoffFixtureEvidence, PyroomStochasticEvaluation]:
    """Evaluate replay and independent-seed budget convergence without conflating them."""

    if authority.fixture_id != fixture.fixture_id:
        raise ValueError('stochastic authority references the wrong R100A fixture')
    if raw.r100a_manifest_id != benchmark.manifest_id:
        raise ValueError('raw stochastic evidence references the wrong R100A manifest id')
    if raw.r100a_semantic_hash != benchmark.semantic_hash():
        raise ValueError('raw stochastic evidence R100A semantic hash is stale')
    if raw.fixture_id != fixture.fixture_id:
        raise ValueError('raw stochastic evidence references the wrong fixture')
    if raw.fixture_semantic_hash != stochastic_fixture_semantic_hash(fixture):
        raise ValueError('raw stochastic evidence fixture semantic hash is stale')
    if raw.stochastic_authority_id != authority.authority_id:
        raise ValueError('raw stochastic evidence references the wrong stochastic authority id')
    if raw.stochastic_authority_semantic_hash != authority.semantic_hash():
        raise ValueError('raw stochastic evidence stochastic authority hash is stale')
    if raw.candidate_id != authority.candidate_id:
        raise ValueError('raw stochastic evidence candidate does not match authority')
    if raw.backend_version != authority.backend_version:
        raise ValueError('raw stochastic evidence backend version does not match authority')
    if raw.candidate_manifest_hash != candidates.semantic_hash():
        raise ValueError('raw stochastic evidence candidate manifest hash is stale')
    candidate_by_id = {item.candidate_id: item for item in candidates.candidates}
    candidate = candidate_by_id.get(authority.candidate_id)
    if candidate is None:
        raise ValueError('stochastic authority candidate is missing from candidate manifest')
    if raw.candidate_source_commit_sha != candidate.source_commit_sha:
        raise ValueError('raw stochastic evidence candidate source commit is stale')

    if fixture.random_seed is None:
        raise ValueError('R100A stochastic fixture is missing its explicit random seed')
    if fixture.random_seed in authority.independent_seeds:
        raise ValueError('independent seed set must not contain the R100A replay seed')
    if tuple(float(value) for value in fixture.comparison.frequency_grid.values_hz) != tuple(
        authority.frequency_hz
    ):
        raise ValueError('stochastic authority frequency grid does not match R100A fixture')
    if len(fixture.observables) != 1:
        raise ValueError('stochastic fixture must expose exactly one observable')
    expected = fixture.observables[0]
    if (
        expected.observable_id != 'same-seed-repeatability'
        or expected.kind != 'energy_decay_db'
        or expected.unit != 'dB'
        or expected.acceptance_relation != 'repeatable_same_seed'
    ):
        raise ValueError('R100A stochastic observable semantics changed unexpectedly')

    expected_keys = stochastic_sample_keys(authority)
    observation_by_identity = {
        (item.seed, item.ray_budget, item.replicate): item for item in raw.observations
    }
    replay_identities = tuple(
        (fixture.random_seed, authority.ray_budgets[-1], replicate)
        for replicate in range(authority.same_seed_repeats)
    )
    independent_identities = tuple(
        (seed, budget, 0)
        for budget in authority.ray_budgets
        for seed in authority.independent_seeds
    )
    expected_identities = set(replay_identities + independent_identities)
    actual_identities = set(observation_by_identity)
    if actual_identities != expected_identities:
        raise ValueError(
            'raw stochastic observation identities do not match authority: '
            f'missing={sorted(expected_identities - actual_identities)} '
            f'extra={sorted(actual_identities - expected_identities)}'
        )

    for observation in raw.observations:
        level_index = authority.ray_budgets.index(observation.ray_budget)
        if observation.receiver_radius_m != authority.receiver_radius_sequence_m[level_index]:
            raise ValueError(
                f'observation {observation.observation_id} receiver radius does not match authority'
            )
        if (
            observation.histogram_bin_size_s
            != authority.histogram_bin_size_sequence_s[level_index]
        ):
            raise ValueError(
                f'observation {observation.observation_id} histogram bin does not match authority'
            )
        if observation.sample_keys != expected_keys:
            raise ValueError(
                f'observation {observation.observation_id} sample keys do not match authority'
            )
        if tuple(observation.selected_band_centers_hz) != tuple(authority.frequency_hz):
            raise ValueError(
                f'observation {observation.observation_id} selected bands do not match authority'
            )

    insufficient = tuple(
        item.observation_id for item in raw.observations if item.status == 'insufficient_support'
    )

    replay = tuple(observation_by_identity[identity] for identity in replay_identities)
    replay_ok = not any(item.status != 'ok' for item in replay)
    exact_histogram_replay = len({item.histogram_sha256 for item in replay}) == 1
    exact_curve_replay = replay_ok and all(
        item.values_db == replay[0].values_db for item in replay[1:]
    )
    replay_abs: float | None = None
    replay_rel: float | None = None
    if replay_ok:
        replay_abs = max(
            abs(left - right)
            for left, right in zip(replay[0].values_db, replay[1].values_db)
        )
        replay_rms = _rms_delta(replay[0].values_db, replay[1].values_db)
        replay_rel = _relative_rms(replay_rms, replay[0].values_db)
    repeatability_pass = (
        replay_ok
        and exact_histogram_replay
        and exact_curve_replay
        and replay_abs == 0.0
        and replay_rel == 0.0
    )

    variation: list[StochasticBudgetVariation] = []
    resource_scaling: list[StochasticBudgetResourceScaling] = []
    means_by_budget: dict[int, tuple[float, ...]] = {}
    independent_insufficient: list[str] = []
    for budget in authority.ray_budgets:
        observations = tuple(
            observation_by_identity[(seed, budget, 0)]
            for seed in authority.independent_seeds
        )
        supported = tuple(item for item in observations if item.status == 'ok')
        resource_scaling.append(
            StochasticBudgetResourceScaling(
                ray_budget=budget,
                receiver_radius_m=authority.receiver_radius_sequence_m[
                    authority.ray_budgets.index(budget)
                ],
                histogram_bin_size_s=authority.histogram_bin_size_sequence_s[
                    authority.ray_budgets.index(budget)
                ],
                observation_count=len(observations),
                supported_observation_count=len(supported),
                setup_s_total=sum(item.setup_s for item in observations),
                solve_s_total=sum(item.solve_s for item in observations),
                solve_s_mean=mean(item.solve_s for item in observations),
                postprocess_s_total=sum(item.postprocess_s for item in observations),
                peak_ram_mb=max(item.peak_ram_mb for item in observations),
                raw_output_bytes_total=sum(item.raw_output_bytes for item in observations),
            )
        )
        if len(supported) != len(observations):
            independent_insufficient.extend(
                item.observation_id for item in observations if item.status != 'ok'
            )
            continue
        point_values = tuple(
            tuple(item.values_db[index] for item in observations)
            for index in range(len(expected_keys))
        )
        mean_curve = tuple(mean(values) for values in point_values)
        point_stddev = tuple(stdev(values) for values in point_values)
        means_by_budget[budget] = mean_curve
        variation.append(
            StochasticBudgetVariation(
                ray_budget=budget,
                receiver_radius_m=authority.receiver_radius_sequence_m[
                    authority.ray_budgets.index(budget)
                ],
                histogram_bin_size_s=authority.histogram_bin_size_sequence_s[
                    authority.ray_budgets.index(budget)
                ],
                seed_count=len(observations),
                mean_curve_db=mean_curve,
                point_stddev_db=point_stddev,
                max_point_stddev_db=max(point_stddev),
            )
        )

    convergence_data_complete = not independent_insufficient
    deltas: tuple[float, ...] = ()
    monotonic = False
    final_abs: float | None = None
    final_rel: float | None = None
    finest_stddev: float | None = None
    convergence_violations: list[str] = []
    if convergence_data_complete:
        deltas = tuple(
            _rms_delta(
                means_by_budget[authority.ray_budgets[index]],
                means_by_budget[authority.ray_budgets[index + 1]],
            )
            for index in range(len(authority.ray_budgets) - 1)
        )
        monotonic = all(
            deltas[index] > deltas[index + 1]
            for index in range(len(deltas) - 1)
        )
        final_abs = deltas[-1]
        finest_curve = means_by_budget[authority.ray_budgets[-1]]
        final_rel = _relative_rms(final_abs, finest_curve)
        finest_stddev = variation[-1].max_point_stddev_db
        convergence_probe = BakeoffObservableEvidence(
            observable_id=expected.observable_id,
            status='pass',
            summary='independent-seed ray-budget convergence provisional evidence',
            absolute_error=final_abs,
            relative_error=final_rel,
            statistical_stddev=finest_stddev,
        )
        convergence_violations.extend(observable_tolerance_violations(expected, convergence_probe))
        if not monotonic:
            convergence_violations.append(
                'independent-seed mean-curve RMS deltas do not decrease strictly '
                'with increasing ray budget'
            )
    else:
        convergence_violations.append(
            'one or more independent-seed ray-budget observations have insufficient positive '
            'cumulative energy; no zero response was substituted'
        )

    convergence_pass = not convergence_violations

    combined_abs = max(
        value for value in (replay_abs, final_abs) if value is not None
    ) if any(value is not None for value in (replay_abs, final_abs)) else None
    combined_rel = max(
        value for value in (replay_rel, final_rel) if value is not None
    ) if any(value is not None for value in (replay_rel, final_rel)) else None

    setup_s = sum(item.setup_s for item in raw.observations)
    solve_s = sum(item.solve_s for item in raw.observations)
    postprocess_s = sum(item.postprocess_s for item in raw.observations)
    peak_ram_mb = max(item.peak_ram_mb for item in raw.observations)
    output_mb = raw.raw_archive_bytes / (1024.0 * 1024.0)
    disk_mb = output_mb
    resource_violations = _resource_violations(
        fixture,
        setup_s=setup_s,
        solve_s=solve_s,
        postprocess_s=postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=disk_mb,
        output_mb=output_mb,
    )

    violations: list[str] = []
    if not repeatability_pass:
        violations.append(
            'same-seed replay is not exactly identical at raw histogram and extracted-curve levels'
        )
    violations.extend(convergence_violations)
    violations.extend(resource_violations)

    provisional = BakeoffObservableEvidence(
        observable_id=expected.observable_id,
        status='pass',
        summary='combined replay and stochastic convergence provisional evidence',
        absolute_error=combined_abs,
        relative_error=combined_rel,
        statistical_stddev=finest_stddev,
    )
    for violation in observable_tolerance_violations(expected, provisional):
        if violation not in violations:
            violations.append(violation)

    status: Literal['pass', 'fail'] = 'fail' if violations else 'pass'
    observable = BakeoffObservableEvidence(
        observable_id=expected.observable_id,
        status=status,
        summary=(
            '; '.join(violations)
            if violations
            else (
                'same-seed replay is exact and independent-seed mean energy-decay '
                'curves converge under the frozen R100A-2 tolerance'
            )
        ),
        absolute_error=combined_abs,
        relative_error=combined_rel,
        statistical_stddev=finest_stddev,
    )
    fixture_evidence = BakeoffFixtureEvidence(
        fixture_id=fixture.fixture_id,
        status=status,
        evidence_ref=raw.evidence_ref,
        adapter_id=raw.adapter_id,
        adapter_version=raw.adapter_version,
        backend_version=raw.backend_version,
        precision='float64',
        compile_s=setup_s,
        solve_s=solve_s,
        postprocess_s=postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=disk_mb,
        output_mb=output_mb,
        observables=(observable,),
        diagnostics=(
            'compile_s records aggregate adapter/room setup time; the pinned wheel '
            'requires no native compile inside this numerical probe.',
            f'raw_archive_sha256={raw.raw_archive_sha256}',
            f'raw_archive_bytes={raw.raw_archive_bytes}',
            *tuple(resource_violations),
        ),
    )
    evaluation = PyroomStochasticEvaluation(
        status=status,
        repeatability_status='pass' if repeatability_pass else 'fail',
        exact_histogram_replay=exact_histogram_replay,
        exact_curve_replay=exact_curve_replay,
        replay_max_absolute_error_db=replay_abs,
        replay_relative_rms_error=replay_rel,
        convergence_status='pass' if convergence_pass else 'fail',
        monotonic_budget_refinement=monotonic,
        adjacent_budget_mean_rms_delta_db=deltas,
        final_budget_absolute_rms_delta_db=final_abs,
        final_budget_relative_rms_delta=final_rel,
        finest_budget_seed_stddev_max_db=finest_stddev,
        budget_variation=tuple(variation),
        resource_scaling=tuple(resource_scaling),
        insufficient_observation_ids=insufficient,
        diagnostics=(
            'Seed repeatability and statistical convergence are evaluated separately.',
            'Independent-seed variation is retained at every ray budget.',
            *tuple(convergence_violations),
            *tuple(resource_violations),
        ),
    )
    return fixture_evidence, evaluation
