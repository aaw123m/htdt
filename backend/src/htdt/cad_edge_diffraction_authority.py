"""Edge-diffraction model qualification authority (#681).

A diffraction engine may only claim accuracy inside the domain it was
validated for. This authority seals the model family (BTM/UTD/GTD/
approximation), the edge geometry it applies to (finite vs idealized
infinite wedge, wedge angle, adjacent faces), the boundary assumption
(rigid vs impedance), the diffraction order and numerics, and the
benchmark evidence behind the claim. Unverified accuracy claims are
``insufficient_evidence``/``unqualified`` — never assumed.

Scope discipline:

* The authority declares *model validity*; it does not compute
  diffraction. ``DiffractionBenchmarkResult`` records the outcome of a
  fixture run (DIF10–DIF80 class) against a declared reference.
* ``WedgeMaterialSemantics`` keeps rigid-reference models honest: a
  rigid-BTM model cannot claim accuracy on a non-rigid wedge without an
  impedance-boundary capability declaration.
* ``EdgeDiffractionProfile.embeds_scattering`` declares when the engine
  also models surface scattering so a #684 authority does not
  double-count the same energy.

Literature basis: Biot–Tolstoy–Medwin time-domain wedge diffraction;
Medwin's finite-edge correction to BTM; Svensson et al. validation of
BTM against the analytic wedge solution; Uniform Theory of Diffraction
(Keller/Kouyoumjian–Pathak) and its singularity structure at shadow and
reflection boundaries; error analysis of reduced-order IIR approximations
used in interactive auralization.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_bass_management import FrequencyBand
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


EDGE_DIFFRACTION_SCHEMA_VERSION = 'edge-diffraction-1'
EDGE_DIFFRACTION_EVALUATION_VERSION = 'edge-diffraction-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


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


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

DiffractionModelFamily = Literal[
    'btm_finite_edge',
    'biot_tolstoy_ideal_wedge',
    'gtd',
    'utd',
    'iir_reduced_approximation',
    'numerical_wave_reference',
    'custom_validated',
    'unknown',
]
"""The diffraction theory the engine implements. ``btm_finite_edge``
covers the Medwin finite-edge correction of the Biot–Tolstoy wedge
solution; ``iir_reduced_approximation`` is a reduced-order filter model
whose accuracy must be benchmarked, not assumed."""

EdgeGeometryKind = Literal[
    'finite_straight_edge',
    'infinite_wedge',
    'polyhedral_corner',
    'multiple_connected_edges',
    'curved_edge',
    'unknown',
]

WedgeBoundary = Literal[
    'rigid_reference',
    'impedance_boundary_supported',
    'material_correction_approximation',
    'unsupported_nonrigid_edge',
]

BoundaryRegionCapability = Literal[
    'lit_region',
    'shadow_region',
    'specular_boundary',
    'all_regions',
    'unknown',
]

DiffractionOrder = Literal[
    'first_order_edge',
    'second_order_edge',
    'reflection_then_diffraction',
    'diffraction_then_reflection',
    'diffraction_of_reflected_path',
    'multiple_diffraction',
]

DiffractionNumericsKind = Literal[
    'analytic_integral',
    'series_summation',
    'numeric_quadrature',
    'subdivision_integration',
    'iir_approximation',
    'convolution_rendering',
    'unknown',
]

DiffractionCapability = Literal[
    'physical_reference_capability',
    'physical_approximation_capability',
    'perceptual_approximation_capability',
    'unqualified',
    'insufficient_evidence',
]

FixtureKind = Literal[
    'infinite_wedge_reference',
    'finite_edge',
    'shadow_boundary_sweep',
    'wedge_angle_sweep',
    'edge_end_effects',
    'ordered_path',
    'multiple_diffraction',
    'nlos_room',
    'bras_scene',
    'custom',
]

FixtureReferenceClass = Literal[
    'analytic',
    'bras',
    'measured',
    'numerical',
    'unknown',
]

ObservableState = Literal[
    'matches',
    'mismatches',
    'not_evaluated',
]

FixtureResultState = Literal['pass', 'fail', 'limited']


# ---------------------------------------------------------------------------
# Sub-specs
# ---------------------------------------------------------------------------


class EdgeGeometryPin(BaseModel):
    """Declared edge geometry the diffraction model applies to."""

    model_config = ConfigDict(frozen=True)

    edge_kind: EdgeGeometryKind
    edge_length_m: float | None = Field(default=None, gt=0.0)
    wedge_angle_deg: float | None = Field(
        default=None, gt=0.0, le=360.0
    )
    adjacent_face_refs: tuple[AuthorityRef, ...] = ()
    geometry_revision_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _check(self) -> 'EdgeGeometryPin':
        for ref in self.adjacent_face_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'adjacent face refs must pin their sha256'
                )
        if self.geometry_revision_ref is not None and (
            self.geometry_revision_ref.ref_sha256 is None
        ):
            raise ValueError(
                'geometry_revision_ref must pin its sha256'
            )
        if self.edge_kind == 'finite_straight_edge' and (
            self.edge_length_m is None
        ):
            raise ValueError(
                'finite_straight_edge requires a declared edge_length_m'
            )
        return self


class WedgeMaterialSemantics(BaseModel):
    """Boundary assumption the model makes on the wedge faces."""

    model_config = ConfigDict(frozen=True)

    boundary: WedgeBoundary = 'rigid_reference'
    impedance_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'WedgeMaterialSemantics':
        for ref in self.impedance_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'impedance refs must pin their sha256'
                )
        if (
            self.boundary == 'impedance_boundary_supported'
            and not self.impedance_refs
        ):
            raise ValueError(
                'impedance_boundary_supported requires impedance refs'
            )
        return self


class DiffractionOrderSpec(BaseModel):
    """Diffraction orders the engine is declared to handle."""

    model_config = ConfigDict(frozen=True)

    orders: tuple[DiffractionOrder, ...] = Field(min_length=1)
    max_order: int = Field(default=1, ge=1)


class DiffractionNumericsSpec(BaseModel):
    """How the diffraction integral is evaluated."""

    model_config = ConfigDict(frozen=True)

    kind: DiffractionNumericsKind
    tolerance: float | None = Field(default=None, gt=0.0)
    subdivision_step_m: float | None = Field(default=None, gt=0.0)
    convergence_evidence_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'DiffractionNumericsSpec':
        if self.convergence_evidence_ref is not None and (
            self.convergence_evidence_ref.ref_sha256 is None
        ):
            raise ValueError(
                'convergence_evidence_ref must pin its sha256'
            )
        return self


class DiffractionVisibilitySpec(BaseModel):
    """How the engine decides whether an edge contributes."""

    model_config = ConfigDict(frozen=True)

    visibility_checks: tuple[str, ...] = ()
    conservative_culling: bool = False
    notes: str = ''


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class EdgeDiffractionProfile(BaseModel):
    """Sealed declaration of a diffraction engine's domain."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    model_family: DiffractionModelFamily
    implementation: str = ''
    implementation_version: str = ''
    domain: Literal['time_domain', 'frequency_domain', 'hybrid'] = (
        'time_domain'
    )
    edge_geometry: EdgeGeometryPin
    wedge_material: WedgeMaterialSemantics = WedgeMaterialSemantics()
    order_spec: DiffractionOrderSpec | None = None
    numerics: DiffractionNumericsSpec | None = None
    visibility: DiffractionVisibilitySpec | None = None
    frequency_domain_hz: FrequencyBand | None = None
    embeds_scattering: bool = False
    authority_version: str = Field(
        default=EDGE_DIFFRACTION_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'model_family': self.model_family,
            'implementation': self.implementation,
            'implementation_version': self.implementation_version,
            'domain': self.domain,
            'edge_geometry': self.edge_geometry.model_dump(mode='json'),
            'wedge_material': (
                self.wedge_material.model_dump(mode='json')
            ),
            'order_spec': (
                self.order_spec.model_dump(mode='json')
                if self.order_spec is not None
                else None
            ),
            'numerics': (
                self.numerics.model_dump(mode='json')
                if self.numerics is not None
                else None
            ),
            'visibility': (
                self.visibility.model_dump(mode='json')
                if self.visibility is not None
                else None
            ),
            'frequency_domain_hz': (
                self.frequency_domain_hz.model_dump(mode='json')
                if self.frequency_domain_hz is not None
                else None
            ),
            'embeds_scattering': self.embeds_scattering,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'EdgeDiffractionProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        if self.model_family == 'unknown':
            raise ValueError(
                'model_family unknown is not a declarable profile; '
                'omit the profile instead'
            )
        if (
            self.model_family == 'custom_validated'
            and not self.implementation
        ):
            raise ValueError(
                'custom_validated requires an implementation label'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('edge diffraction profile hash mismatch')
        if self.profile_id != _semantic_id('edfprof', expected):
            raise ValueError(
                'edge diffraction profile id does not match its hash'
            )
        return self


class DiffractionBenchmarkResult(BaseModel):
    """Sealed fixture outcome for a diffraction engine.

    Each result pins the profile it was measured for plus the fixture
    kind and the per-observable match state. ``result='fail'`` must cap
    the qualification at ``unqualified`` for the covered domain.
    """

    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    fixture_id: str = Field(min_length=1)
    fixture_kind: FixtureKind
    reference_class: FixtureReferenceClass
    observables: tuple[tuple[str, ObservableState], ...] = ()
    result: FixtureResultState
    valid_band_hz: FrequencyBand | None = None
    notes: str = ''
    authority_version: str = Field(
        default=EDGE_DIFFRACTION_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'fixture_id': self.fixture_id,
            'fixture_kind': self.fixture_kind,
            'reference_class': self.reference_class,
            'observables': [list(o) for o in self.observables],
            'result': self.result,
            'valid_band_hz': (
                self.valid_band_hz.model_dump(mode='json')
                if self.valid_band_hz is not None
                else None
            ),
            'notes': self.notes,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'DiffractionBenchmarkResult':
        _require_iso8601(self.declared_at_utc, 'result declared_at_utc')
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'edge_diffraction_profile':
            raise ValueError(
                "profile_ref must pin an 'edge_diffraction_profile'"
            )
        if self.result == 'pass' and any(
            state == 'mismatches' for _, state in self.observables
        ):
            raise ValueError(
                'a passing benchmark cannot contain mismatched '
                'observables'
            )
        expected = _hash(self.identity_payload())
        if self.result_sha256 != expected:
            raise ValueError(
                'diffraction benchmark result hash mismatch'
            )
        if self.result_id != _semantic_id('difbench', expected):
            raise ValueError(
                'diffraction benchmark result id does not match its '
                'hash'
            )
        return self


class EdgeDiffractionQualification(BaseModel):
    """Sealed verdict of :func:`evaluate_diffraction_model`."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    capability: DiffractionCapability
    benchmark_refs: tuple[AuthorityRef, ...] = ()
    covered_fixture_kinds: tuple[FixtureKind, ...] = ()
    boundary_limited: bool = False
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=EDGE_DIFFRACTION_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'capability': self.capability,
            'benchmark_refs': [
                r.model_dump(mode='json') for r in self.benchmark_refs
            ],
            'covered_fixture_kinds': list(self.covered_fixture_kinds),
            'boundary_limited': self.boundary_limited,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'EdgeDiffractionQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'edge_diffraction_profile':
            raise ValueError(
                "profile_ref must pin an 'edge_diffraction_profile'"
            )
        for ref in self.benchmark_refs:
            if ref.ref_sha256 is None:
                raise ValueError('benchmark refs must pin their sha256')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError(
                'edge diffraction qualification hash mismatch'
            )
        if self.qualification_id != _semantic_id('difqual', expected):
            raise ValueError(
                'edge diffraction qualification id does not match '
                'its hash'
            )
        return self


def edge_diffraction_binding(
    profile: EdgeDiffractionProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='edge_diffraction_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


def diffraction_benchmark_binding(
    result: DiffractionBenchmarkResult,
) -> AuthorityRef:
    return AuthorityRef(
        kind='diffraction_benchmark_result',
        ref_id=result.result_id,
        ref_sha256=result.result_sha256,
    )


def edge_diffraction_qualification_binding(
    qualification: EdgeDiffractionQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='edge_diffraction_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

_STRONG_FIXTURES = {
    'finite_edge',
    'shadow_boundary_sweep',
    'bras_scene',
    'wedge_angle_sweep',
    'multiple_diffraction',
    'nlos_room',
}


def evaluate_diffraction_model(
    document_id: str,
    profile: EdgeDiffractionProfile,
    *,
    benchmarks: Sequence[DiffractionBenchmarkResult] = (),
    wedge_nonrigid: bool = False,
    evaluated_at_utc: str | None = None,
) -> EdgeDiffractionQualification:
    """Grade the diffraction engine against its declared domain.

    Fail-closed rules:

    * a ``fail`` fixture caps the engine at ``unqualified``
    * no benchmarks → ``insufficient_evidence``
    * rigid-reference model claiming accuracy on a non-rigid wedge →
      ``unqualified``/boundary-limited
    * only idealized-wedge benchmarks with a finite-edge claim → capped
      at ``physical_approximation_capability`` (boundary_limited)
    * reduced-order approximation (IIR) without reference fixtures →
      ``perceptual_approximation_capability`` at best
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    reasons: list[str] = []
    limitations: list[str] = []
    boundary_limited = False

    covered = tuple(
        sorted({b.fixture_kind for b in benchmarks})
    )
    benchmark_refs = tuple(
        diffraction_benchmark_binding(b) for b in benchmarks
    )

    # A failing fixture immediately caps the engine.
    if any(b.result == 'fail' for b in benchmarks):
        failing = sorted({b.fixture_id for b in benchmarks if b.result == 'fail'})
        reasons.append(
            f'benchmark fixture(s) {failing} reported fail'
        )
        return _finish_dif(
            document_id, profile, 'unqualified',
            benchmark_refs, covered, boundary_limited,
            reasons, limitations, evaluated_at_utc,
        )

    if not benchmarks:
        if profile.model_family == 'numerical_wave_reference':
            capability: DiffractionCapability = (
                'physical_reference_capability'
            )
            limitations.append(
                'reference-class engine declared without fixture '
                'results; retained as reference'
            )
        else:
            return _finish_dif(
                document_id, profile, 'insufficient_evidence',
                benchmark_refs, covered, boundary_limited,
                ['no diffraction benchmark evidence declared'],
                limitations, evaluated_at_utc,
            )
    else:
        strong = {b.fixture_kind for b in benchmarks} & _STRONG_FIXTURES
        if profile.model_family == 'iir_reduced_approximation':
            if any(b.result == 'pass' for b in benchmarks) and strong:
                capability = 'physical_approximation_capability'
            else:
                capability = 'perceptual_approximation_capability'
                limitations.append(
                    'reduced-order IIR approximation has no strong '
                    'physical fixture evidence'
                )
        elif strong:
            capability = 'physical_reference_capability'
        else:
            capability = 'physical_approximation_capability'
            limitations.append(
                'benchmarks cover idealized-wedge fixtures only; '
                'finite-edge claims remain approximate'
            )
            boundary_limited = profile.edge_geometry.edge_kind in {
                'finite_straight_edge',
                'polyhedral_corner',
                'multiple_connected_edges',
            }

    # Boundary semantics: a rigid-reference model cannot claim accuracy
    # on a non-rigid wedge.
    if wedge_nonrigid and profile.wedge_material.boundary in {
        'rigid_reference', 'unsupported_nonrigid_edge',
    }:
        if capability == 'physical_reference_capability':
            capability = 'physical_approximation_capability'
        boundary_limited = True
        limitations.append(
            'wedge is non-rigid but the model assumes a rigid '
            'reference boundary'
        )
    elif wedge_nonrigid and (
        profile.wedge_material.boundary
        == 'material_correction_approximation'
    ):
        if capability == 'physical_reference_capability':
            capability = 'physical_approximation_capability'
        limitations.append(
            'non-rigid wedge handled by a declared material correction'
        )

    if profile.order_spec is not None and (
        profile.order_spec.max_order == 1
    ):
        if any(
            b.fixture_kind in {'multiple_diffraction', 'ordered_path'}
            for b in benchmarks
        ) and all(
            b.result != 'pass'
            for b in benchmarks
            if b.fixture_kind in {'multiple_diffraction', 'ordered_path'}
        ):
            limitations.append(
                'multi-order fixtures did not pass under a '
                'first-order engine'
            )
            capability = 'physical_approximation_capability'

    if profile.numerics is not None and (
        profile.numerics.convergence_evidence_ref is None
    ):
        limitations.append(
            'no convergence evidence declared for the diffraction '
            'numerics'
        )
        if capability == 'physical_reference_capability':
            capability = 'physical_approximation_capability'

    return _finish_dif(
        document_id, profile, capability,
        benchmark_refs, covered, boundary_limited,
        reasons, limitations, evaluated_at_utc,
    )


def _finish_dif(
    document_id: str,
    profile: EdgeDiffractionProfile,
    capability: DiffractionCapability,
    benchmark_refs: tuple[AuthorityRef, ...],
    covered: tuple[FixtureKind, ...],
    boundary_limited: bool,
    reasons: list[str],
    limitations: list[str],
    evaluated_at_utc: str,
) -> EdgeDiffractionQualification:
    return _seal(
        EdgeDiffractionQualification,
        {
            'document_id': document_id,
            'profile_ref': edge_diffraction_binding(profile),
            'capability': capability,
            'benchmark_refs': [
                r.model_dump(mode='json') for r in benchmark_refs
            ],
            'covered_fixture_kinds': list(covered),
            'boundary_limited': boundary_limited,
            'reasons': sorted(set(reasons)),
            'limitations': sorted(set(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': EDGE_DIFFRACTION_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'difqual',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_edge_diffraction_profile(
    document_id: str,
    model_family: DiffractionModelFamily,
    edge_geometry: EdgeGeometryPin,
    *,
    implementation: str = '',
    implementation_version: str = '',
    domain: Literal['time_domain', 'frequency_domain', 'hybrid'] = (
        'time_domain'
    ),
    wedge_material: WedgeMaterialSemantics | None = None,
    order_spec: DiffractionOrderSpec | None = None,
    numerics: DiffractionNumericsSpec | None = None,
    visibility: DiffractionVisibilitySpec | None = None,
    frequency_domain_hz: FrequencyBand | None = None,
    embeds_scattering: bool = False,
    declared_at_utc: str | None = None,
) -> EdgeDiffractionProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        EdgeDiffractionProfile,
        {
            'document_id': document_id,
            'model_family': model_family,
            'implementation': implementation,
            'implementation_version': implementation_version,
            'domain': domain,
            'edge_geometry': edge_geometry.model_dump(mode='json'),
            'wedge_material': (
                wedge_material.model_dump(mode='json')
                if wedge_material is not None
                else WedgeMaterialSemantics().model_dump(mode='json')
            ),
            'order_spec': (
                order_spec.model_dump(mode='json')
                if order_spec is not None
                else None
            ),
            'numerics': (
                numerics.model_dump(mode='json')
                if numerics is not None
                else None
            ),
            'visibility': (
                visibility.model_dump(mode='json')
                if visibility is not None
                else None
            ),
            'frequency_domain_hz': (
                frequency_domain_hz.model_dump(mode='json')
                if frequency_domain_hz is not None
                else None
            ),
            'embeds_scattering': embeds_scattering,
            'authority_version': EDGE_DIFFRACTION_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'edfprof',
    )


def build_diffraction_benchmark_result(
    document_id: str,
    profile: EdgeDiffractionProfile,
    *,
    fixture_id: str,
    fixture_kind: FixtureKind,
    reference_class: FixtureReferenceClass,
    result: FixtureResultState,
    observables: Sequence[tuple[str, ObservableState]] = (),
    valid_band_hz: FrequencyBand | None = None,
    notes: str = '',
    declared_at_utc: str | None = None,
) -> DiffractionBenchmarkResult:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        DiffractionBenchmarkResult,
        {
            'document_id': document_id,
            'profile_ref': edge_diffraction_binding(profile),
            'fixture_id': fixture_id,
            'fixture_kind': fixture_kind,
            'reference_class': reference_class,
            'observables': [list(o) for o in observables],
            'result': result,
            'valid_band_hz': (
                valid_band_hz.model_dump(mode='json')
                if valid_band_hz is not None
                else None
            ),
            'notes': notes,
            'authority_version': EDGE_DIFFRACTION_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'result_id',
        'result_sha256',
        'difbench',
    )
