"""Geometric surface-scattering model qualification authority (#684).

An ISO scattering coefficient must not silently become a Lambert
distribution. This authority seals which solver reflection model consumes
the surface evidence, the explicit coefficient→distribution mapping, the
incidence/frequency domain over which the model applies, and the
validation tier behind the claim. Models that cannot show the mapping are
``insufficient_evidence``; a model that cannot redirect energy to the
directions a geometry demands is refused.

Scope discipline:

* ``cad_surface_scattering`` (#1032) already separates the measured
  *quantity* kinds (ISO 17497-1 random-incidence scattering vs ISO
  17497-2 directional diffusion) and decides whether a scalar GA
  fraction exists. This authority composes on top: it records which
  solver model *consumes* the evidence and how the scalar/distribution
  maps to outgoing directions.
* The hard ISO rule is mechanical: an ISO 17497-2 diffusion coefficient
  (or any non-fraction kind) cannot feed a solver's scalar scatter
  input — ``mapping_role='solver_input'`` combined with such a quantity
  is a construction error, not a runtime warning.
* ``diffraction_embedded`` / ``scattering_couples_absorption`` declare
  the model's relationship to #681 and #570 so energy is not
  double-counted.

Literature basis: ISO 17497-1 (scattering coefficient) vs ISO 17497-2
(directional diffusion — explicitly not a solver input); Vorländer,
"Auralization" — the Lambert/Born-style diffuse tail and the energy split
s between specular and scattered; Born/Kirchhoff approximations for
rough-surface scattering; Cox & D'Antonio on diffuser directionality.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_bass_management import FrequencyBand
from .cad_surface_scattering import ScatteringQuantityKind
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


SCATTERING_MODEL_SCHEMA_VERSION = 'scattering-model-1'
SCATTERING_MODEL_EVALUATION_VERSION = 'scattering-model-eval-1'

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

SolverReflectionModelKind = Literal[
    'perfect_specular',
    'specular_plus_lambert_diffuse',
    'specular_plus_cosine_power_diffuse',
    'specular_plus_measured_directional',
    'specular_plus_vector_based_scattering',
    'stochastic_ray_scattering',
    'absorption_only_no_scatter',
    'geometric_brdf_model',
    'custom_validated',
]
"""How the solver turns incident energy into outgoing directions."""

ScatteringDistributionLaw = Literal[
    'lambert',
    'cosine_power',
    'vector_based',
    'measured_kernel',
    'specular_only',
    'custom',
    'none',
]

IncidenceHandling = Literal[
    'single_incidence_measured',
    'multi_incidence_measured',
    'angle_interpolated',
    'random_incidence_scalar',
    'geometry_predicted',
    'unknown',
]

CoefficientMappingRole = Literal[
    'solver_input',
    'validation_reference',
    'display_documentation',
]
"""What the coefficient is used for. Only ``solver_input`` feeds the
solver — and only when the quantity is actually a fraction."""

EarlyLateApplicability = Literal[
    'early_reflection_validated',
    'late_diffuse_approximation_only',
    'all_ray_events_approximate',
    'unknown',
]

ScatteringValidationTier = Literal[
    'benchmark_measured',
    'benchmark_simulated',
    'literature_validated',
    'implementation_defined',
    'user_assumed',
    'unknown',
]

ScatteringVerdict = Literal[
    'qualified_for_declared_domain',
    'qualified_with_limitations',
    'directional_redirection_unsupported',
    'insufficient_evidence',
    'not_qualified',
]

# Kinds that legitimately feed a solver scalar (mirrors #1032's
# _GA_SCALAR_KINDS; ISO 17497-2 diffusion coefficients are excluded by
# the standard itself).
_SCALAR_CAPABLE_KINDS = frozenset(
    {
        'random_incidence_scattering_coefficient',
        'model_derived_scattering',
    }
)

_NON_FRACTION_KINDS = frozenset(
    {
        'directional_diffusion_coefficient',
        'random_incidence_diffusion_coefficient',
        'directional_scattering_distribution',
        'explicit_geometry_only',
        'unknown',
    }
)


# ---------------------------------------------------------------------------
# Sub-specs
# ---------------------------------------------------------------------------


class CoefficientDistributionMapping(BaseModel):
    """The explicit law turning a scalar coefficient into a directional
    distribution.

    * ``input_quantity_kind`` mirrors the evidence's
      ``ScatteringQuantityKind`` — ISO 17497-2 diffusion coefficients
      cannot carry ``mapping_role='solver_input'``.
    * ``distribution_law`` is the outgoing-direction law the solver
      uses; a scalar coefficient alone cannot claim a directional law
      stronger than ``lambert``/``cosine_power``/``vector_based`` when
      no directional measurement backs it.
    """

    model_config = ConfigDict(frozen=True)

    input_quantity_kind: ScatteringQuantityKind
    mapping_role: CoefficientMappingRole = 'solver_input'
    distribution_law: ScatteringDistributionLaw = 'none'
    incidence_handling: IncidenceHandling = 'unknown'
    specular_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    diffuse_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    energy_normalization: Literal[
        'unit_energy', 'declared_only', 'none'
    ] = 'unit_energy'
    frequency_interpolation: str = ''
    mapping_version: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'CoefficientDistributionMapping':
        if (
            self.mapping_role == 'solver_input'
            and self.input_quantity_kind in _NON_FRACTION_KINDS
        ):
            raise ValueError(
                f'input quantity {self.input_quantity_kind} cannot feed '
                'a solver scalar input (ISO 17497-2 diffusion and '
                'non-fraction kinds are documentation only)'
            )
        if self.mapping_role == 'solver_input' and (
            self.distribution_law == 'none'
        ):
            raise ValueError(
                'a solver-input mapping must declare its '
                'distribution_law'
            )
        if (
            self.specular_fraction is not None
            and self.diffuse_fraction is not None
            and abs(
                self.specular_fraction + self.diffuse_fraction - 1.0
            )
            > 1e-6
            and self.energy_normalization == 'unit_energy'
        ):
            raise ValueError(
                'unit_energy normalization requires specular + '
                'diffuse fractions to sum to 1'
            )
        if self.distribution_law == 'measured_kernel' and (
            self.incidence_handling == 'random_incidence_scalar'
        ):
            raise ValueError(
                'a measured-kernel distribution cannot be built from a '
                'random-incidence scalar'
            )
        return self


class DirectionalDiscretization(BaseModel):
    """Outgoing/incident angular discretization the solver uses."""

    model_config = ConfigDict(frozen=True)

    outgoing_step_deg: float | None = Field(default=None, gt=0.0)
    incident_grid: str = ''
    hemisphere_samples: int | None = Field(default=None, ge=1)


class ScatteringValidation(BaseModel):
    """Validation tier and evidence refs behind the model claim."""

    model_config = ConfigDict(frozen=True)

    tier: ScatteringValidationTier
    evidence_refs: tuple[AuthorityRef, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'ScatteringValidation':
        for ref in self.evidence_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'scattering validation refs must pin their sha256'
                )
        if self.tier in {
            'benchmark_measured',
            'benchmark_simulated',
            'literature_validated',
        } and not self.evidence_refs:
            raise ValueError(
                f'{self.tier} requires evidence refs'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class SurfaceReflectionModelProfile(BaseModel):
    """Sealed declaration of the solver's surface-reflection model."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    surface_ref: AuthorityRef | None = None
    material_evidence_ref: AuthorityRef | None = None
    solver_model: SolverReflectionModelKind
    implementation: str = ''
    implementation_version: str = ''
    coefficient_mapping: CoefficientDistributionMapping | None = None
    directional_redirection: bool = False
    incidence_domain: IncidenceHandling = 'unknown'
    frequency_domain_hz: FrequencyBand | None = None
    early_late_applicability: EarlyLateApplicability = 'unknown'
    discretization: DirectionalDiscretization | None = None
    validation: ScatteringValidation | None = None
    diffraction_embedded: bool = False
    scattering_couples_absorption: bool = False
    authority_version: str = Field(
        default=SCATTERING_MODEL_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'surface_ref': (
                self.surface_ref.model_dump(mode='json')
                if self.surface_ref is not None
                else None
            ),
            'material_evidence_ref': (
                self.material_evidence_ref.model_dump(mode='json')
                if self.material_evidence_ref is not None
                else None
            ),
            'solver_model': self.solver_model,
            'implementation': self.implementation,
            'implementation_version': self.implementation_version,
            'coefficient_mapping': (
                self.coefficient_mapping.model_dump(mode='json')
                if self.coefficient_mapping is not None
                else None
            ),
            'directional_redirection': self.directional_redirection,
            'incidence_domain': self.incidence_domain,
            'frequency_domain_hz': (
                self.frequency_domain_hz.model_dump(mode='json')
                if self.frequency_domain_hz is not None
                else None
            ),
            'early_late_applicability': self.early_late_applicability,
            'discretization': (
                self.discretization.model_dump(mode='json')
                if self.discretization is not None
                else None
            ),
            'validation': (
                self.validation.model_dump(mode='json')
                if self.validation is not None
                else None
            ),
            'diffraction_embedded': self.diffraction_embedded,
            'scattering_couples_absorption': (
                self.scattering_couples_absorption
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'SurfaceReflectionModelProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        for ref, label in (
            (self.surface_ref, 'surface_ref'),
            (self.material_evidence_ref, 'material_evidence_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        coefficient_models = {
            'specular_plus_lambert_diffuse',
            'specular_plus_cosine_power_diffuse',
            'specular_plus_vector_based_scattering',
            'stochastic_ray_scattering',
        }
        if (
            self.solver_model in coefficient_models
            and self.coefficient_mapping is None
        ):
            raise ValueError(
                f'solver model {self.solver_model} consumes a '
                'coefficient; a CoefficientDistributionMapping is '
                'required'
            )
        if (
            self.solver_model
            == 'specular_plus_measured_directional'
            and (
                self.coefficient_mapping is None
                or self.coefficient_mapping.distribution_law
                != 'measured_kernel'
            )
        ):
            raise ValueError(
                'specular_plus_measured_directional requires a '
                'measured_kernel mapping'
            )
        if (
            self.solver_model == 'custom_validated'
            and (
                self.validation is None
                or self.validation.tier
                in {'implementation_defined', 'user_assumed', 'unknown'}
            )
        ):
            raise ValueError(
                'custom_validated requires a measured/simulated/'
                'literature validation tier'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError(
                'surface reflection model profile hash mismatch'
            )
        if self.profile_id != _semantic_id('scatprof', expected):
            raise ValueError(
                'surface reflection model profile id does not match '
                'its hash'
            )
        return self


class ScatteringModelQualification(BaseModel):
    """Sealed verdict of :func:`evaluate_scattering_model`."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    verdict: ScatteringVerdict
    effective_model: SolverReflectionModelKind | None = None
    requires_redirection: bool = False
    reflection_order: Literal[
        'early_reflection', 'late_decay_tail', 'full_path', 'unknown'
    ] = 'unknown'
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=SCATTERING_MODEL_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'verdict': self.verdict,
            'effective_model': self.effective_model,
            'requires_redirection': self.requires_redirection,
            'reflection_order': self.reflection_order,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluated_at_utc': self.evaluated_at_utc,
            'evaluation_version': self.evaluation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ScatteringModelQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.profile_ref.kind != 'surface_reflection_model_profile':
            raise ValueError(
                "profile_ref must pin a "
                "'surface_reflection_model_profile'"
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError(
                'scattering model qualification hash mismatch'
            )
        if self.qualification_id != _semantic_id('scatqual', expected):
            raise ValueError(
                'scattering model qualification id does not match '
                'its hash'
            )
        return self


def scattering_model_binding(
    profile: SurfaceReflectionModelProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='surface_reflection_model_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


def scattering_model_qualification_binding(
    qualification: ScatteringModelQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='scattering_model_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

_REDIRECTING_MODELS = {
    'specular_plus_measured_directional',
    'specular_plus_vector_based_scattering',
    'geometric_brdf_model',
    'custom_validated',
}


def evaluate_scattering_model(
    document_id: str,
    profile: SurfaceReflectionModelProfile,
    *,
    requires_redirection: bool = False,
    reflection_order: Literal[
        'early_reflection', 'late_decay_tail', 'full_path', 'unknown'
    ] = 'unknown',
    evaluated_at_utc: str | None = None,
) -> ScatteringModelQualification:
    """Evaluate whether the declared model covers the request.

    Fail-closed rules:

    * geometry needs directional redirection and the model cannot
      redirect → ``directional_redirection_unsupported``
    * coefficient-consuming model without a mapping →
      ``insufficient_evidence`` (unreachable in a well-formed profile —
      construction rejects it — but kept fail-closed for replayed
      payloads)
    * early reflections on a ``late_diffuse_approximation_only`` model →
      ``qualified_with_limitations``
    * unvalidated/user-assumed tiers cap the verdict
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    reasons: list[str] = []
    limitations: list[str] = []
    effective_model: SolverReflectionModelKind | None = (
        profile.solver_model
    )

    if requires_redirection and not (
        profile.directional_redirection
        or profile.solver_model in _REDIRECTING_MODELS
    ):
        return _finish_scat(
            document_id, profile,
            'directional_redirection_unsupported',
            None, requires_redirection, reflection_order,
            [
                f'solver model {profile.solver_model} cannot redirect '
                'energy to required directions'
            ],
            limitations, evaluated_at_utc,
        )

    if (
        profile.solver_model
        in {
            'specular_plus_lambert_diffuse',
            'specular_plus_cosine_power_diffuse',
            'specular_plus_vector_based_scattering',
            'stochastic_ray_scattering',
        }
        and profile.coefficient_mapping is None
    ):
        return _finish_scat(
            document_id, profile, 'insufficient_evidence',
            None, requires_redirection, reflection_order,
            ['coefficient-consuming model has no declared mapping'],
            limitations, evaluated_at_utc,
        )

    verdict: ScatteringVerdict = 'qualified_for_declared_domain'

    if (
        reflection_order == 'early_reflection'
        and profile.early_late_applicability
        == 'late_diffuse_approximation_only'
    ):
        limitations.append(
            'model declared late-diffuse only; early reflections are '
            'approximate'
        )
        verdict = 'qualified_with_limitations'
    elif (
        reflection_order == 'early_reflection'
        and profile.early_late_applicability == 'unknown'
    ):
        limitations.append('early/late applicability never declared')
        verdict = 'qualified_with_limitations'

    if profile.incidence_domain == 'unknown':
        limitations.append('incidence domain never declared')
        verdict = 'qualified_with_limitations'
    elif profile.incidence_domain == 'random_incidence_scalar' and (
        reflection_order == 'early_reflection'
    ):
        limitations.append(
            'angle-specific early reflection on a random-incidence '
            'scalar coefficient'
        )
        verdict = 'qualified_with_limitations'

    if profile.validation is None:
        limitations.append('no validation evidence declared')
        verdict = 'qualified_with_limitations'
    elif profile.validation.tier in {
        'implementation_defined', 'user_assumed', 'unknown',
    }:
        limitations.append(
            f'validation tier is {profile.validation.tier}'
        )
        verdict = 'qualified_with_limitations'

    if profile.solver_model in {
        'absorption_only_no_scatter', 'perfect_specular',
    }:
        limitations.append(
            f'model {profile.solver_model} redirects no diffuse '
            'energy'
        )
        verdict = 'qualified_with_limitations'

    if profile.diffraction_embedded:
        limitations.append(
            'diffraction is embedded in the scattering model; an '
            'explicit #681 diffraction authority must not double-count '
            'the same edges'
        )
        verdict = 'qualified_with_limitations'

    return _finish_scat(
        document_id, profile, verdict,
        effective_model, requires_redirection, reflection_order,
        reasons, limitations, evaluated_at_utc,
    )


def _finish_scat(
    document_id: str,
    profile: SurfaceReflectionModelProfile,
    verdict: ScatteringVerdict,
    effective_model: SolverReflectionModelKind | None,
    requires_redirection: bool,
    reflection_order: str,
    reasons: list[str],
    limitations: list[str],
    evaluated_at_utc: str,
) -> ScatteringModelQualification:
    return _seal(
        ScatteringModelQualification,
        {
            'document_id': document_id,
            'profile_ref': scattering_model_binding(profile),
            'verdict': verdict,
            'effective_model': effective_model,
            'requires_redirection': requires_redirection,
            'reflection_order': reflection_order,
            'reasons': sorted(set(reasons)),
            'limitations': sorted(set(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': SCATTERING_MODEL_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'scatqual',
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_surface_reflection_model_profile(
    document_id: str,
    solver_model: SolverReflectionModelKind,
    *,
    surface_ref: AuthorityRef | None = None,
    material_evidence_ref: AuthorityRef | None = None,
    implementation: str = '',
    implementation_version: str = '',
    coefficient_mapping: CoefficientDistributionMapping | None = None,
    directional_redirection: bool = False,
    incidence_domain: IncidenceHandling = 'unknown',
    frequency_domain_hz: FrequencyBand | None = None,
    early_late_applicability: EarlyLateApplicability = 'unknown',
    discretization: DirectionalDiscretization | None = None,
    validation: ScatteringValidation | None = None,
    diffraction_embedded: bool = False,
    scattering_couples_absorption: bool = False,
    declared_at_utc: str | None = None,
) -> SurfaceReflectionModelProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        SurfaceReflectionModelProfile,
        {
            'document_id': document_id,
            'surface_ref': (
                surface_ref.model_dump(mode='json')
                if surface_ref is not None
                else None
            ),
            'material_evidence_ref': (
                material_evidence_ref.model_dump(mode='json')
                if material_evidence_ref is not None
                else None
            ),
            'solver_model': solver_model,
            'implementation': implementation,
            'implementation_version': implementation_version,
            'coefficient_mapping': (
                coefficient_mapping.model_dump(mode='json')
                if coefficient_mapping is not None
                else None
            ),
            'directional_redirection': directional_redirection,
            'incidence_domain': incidence_domain,
            'frequency_domain_hz': (
                frequency_domain_hz.model_dump(mode='json')
                if frequency_domain_hz is not None
                else None
            ),
            'early_late_applicability': early_late_applicability,
            'discretization': (
                discretization.model_dump(mode='json')
                if discretization is not None
                else None
            ),
            'validation': (
                validation.model_dump(mode='json')
                if validation is not None
                else None
            ),
            'diffraction_embedded': diffraction_embedded,
            'scattering_couples_absorption': (
                scattering_couples_absorption
            ),
            'authority_version': SCATTERING_MODEL_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'scatprof',
    )
