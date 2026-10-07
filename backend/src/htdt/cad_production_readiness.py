"""Production-readiness decision authority (#801).

The product-level credibility gate: one sealed chain

    exact room/equipment authority
    → preregistered owned-room campaign (#813)
    → calibration / holdout / repeatability evidence
    → solver numerical qualification (#809)
    → predicted-vs-measured residual evaluation (#810)
    → applicability domain (#811)
    → production adoption decision
    → recommendation enablement

Two records:

* `ProductionReadinessDecision` (prd-) — the adoption decision. Every
  gate is a pinned ref, never a claim. ``production_ready`` requires
  the entire chain; ``no_go`` and ``limited`` always carry an
  auditable rationale — a failed candidate produces NO_GO, never an
  implicit fallback.
* `RecommendationSurfaceDecision` (rsd-) — which user-facing
  recommendation surfaces become enabled at the decision's outcome.
  ``automatic_recommendation`` can only be enabled at
  ``production_ready``; the issued surface map is re-verified against
  the decision by ``verify_surface_decision`` so a stale or forged
  enablement cannot survive a replay.

Software PASS is never presented as physical-model validity: this
module knows nothing about solver numerics — it only binds the
authorities that already answered those questions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

# ---------------------------------------------------------------------------
# vocabulary


AdoptionOutcome = Literal['no_go', 'limited', 'production_ready']
"""The production adoption decision (#801). ``no_go`` is auditable,
never an implicit fallback; ``limited`` is an explicit bounded
capability; ``production_ready`` requires the full chain."""

SolverPathKind = Literal[
    'wave_only', 'geometrical_only', 'hybrid', 'other_declared']
"""Which solver path the recommendation rides on. A ``hybrid`` path
additionally requires the #812 hybrid-validation gate."""

SurfaceKind = Literal[
    'inspect_prediction', 'compare_candidates', 'automatic_recommendation']
"""Recommendation surfaces — identical tokens to the #814
applicability-envelope decision vocabulary."""

SurfaceState = Literal['enabled', 'limited', 'disabled']


PRODUCTION_LABELS: dict[str, str] = {
    'no_go': 'NO_GO（採用不可）',
    'limited': '限定採用',
    'production_ready': '本番適格',
    'wave_only': '波動ソルバー',
    'geometrical_only': '幾何音響ソルバー',
    'hybrid': 'ハイブリッド',
    'other_declared': 'その他宣言パス',
    'inspect_prediction': '予測の閲覧',
    'compare_candidates': '候補比較',
    'automatic_recommendation': '自動推奨',
    'enabled': '有効',
    'disabled': '無効',
}

# Surface states get their own label map: 'limited' means 限定採用 as an
# adoption outcome but 限定有効 as a surface state — one token, two
# contexts.
SURFACE_STATE_LABELS: dict[str, str] = {
    'enabled': '有効',
    'limited': '限定有効',
    'disabled': '無効',
}


class ProductionReadinessIntegrityError(ValueError):
    """A sealed production-readiness record failed re-verification."""


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is None:
            continue
        if ref.ref_sha256 is None:
            raise ValueError(
                'authority refs must carry the pinned ref_sha256')


def _seal(
    model: type[BaseModel],
    fields: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> BaseModel:
    record = model.model_construct(**fields)
    sha = canonical_sha256(record.identity_payload())
    fields[sha_field] = sha
    fields[id_field] = f'{prefix}-{sha[:24]}'
    return model.model_validate(fields)


# ---------------------------------------------------------------------------
# the adoption decision


class ProductionReadinessDecision(BaseModel):
    """One production adoption decision (prd- prefix).

    Every required gate is a pinned ref into the landed authorities —
    the record can never *claim* a gate, only *bind* its answer.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: str
    decision_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    scene_ref: AuthorityRef
    system_variant_ref: AuthorityRef | None = None
    equipment_authority_refs: tuple[AuthorityRef, ...] = ()
    solver_path_kind: SolverPathKind
    solver_version_ref: AuthorityRef
    campaign_ref: AuthorityRef | None = None
    solver_qualification_ref: AuthorityRef | None = None
    residual_evaluation_ref: AuthorityRef | None = None
    applicability_ref: AuthorityRef | None = None
    hybrid_validation_ref: AuthorityRef | None = None
    stale_authority_refs: tuple[AuthorityRef, ...] = ()
    outcome: AdoptionOutcome
    outcome_rationale: str
    decided_at_utc: str
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ProductionReadinessDecision':
        _require_refs(
            self.scene_ref, self.system_variant_ref,
            self.solver_version_ref, self.campaign_ref,
            self.solver_qualification_ref, self.residual_evaluation_ref,
            self.applicability_ref, self.hybrid_validation_ref,
            *self.equipment_authority_refs,
            *self.stale_authority_refs)
        for required in ('outcome_rationale', 'decided_at_utc'):
            if not getattr(self, required):
                raise ValueError(f'{required} is required')
        if not self.equipment_authority_refs:
            raise ValueError(
                'exact room/equipment authority is required')
        if self.solver_path_kind == 'hybrid' \
                and self.hybrid_validation_ref is None:
            raise ValueError(
                'a hybrid path requires the #812 hybrid-validation ref')
        if self.solver_path_kind != 'hybrid' \
                and self.hybrid_validation_ref is not None:
            raise ValueError(
                'a non-hybrid path must not carry a hybrid-validation '
                'ref')
        if self.stale_authority_refs \
                and self.outcome == 'production_ready':
            raise ValueError(
                'production_ready is rejected while stale authorities '
                'exist')
        if self.outcome == 'production_ready':
            missing = [
                name for name in (
                    'campaign_ref', 'solver_qualification_ref',
                    'residual_evaluation_ref', 'applicability_ref',
                ) if getattr(self, name) is None]
            if missing:
                raise ValueError(
                    'production_ready requires the full evidence '
                    'chain: ' + ', '.join(missing))
        if self.outcome == 'limited':
            if self.campaign_ref is None \
                    or self.solver_qualification_ref is None:
                raise ValueError(
                    'limited requires at least campaign and solver '
                    'qualification refs')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={
                'decision_id', 'decision_sha256'})

    @classmethod
    def create(cls, **fields: Any) -> 'ProductionReadinessDecision':
        return _seal(cls, fields, 'decision_id', 'decision_sha256',
                     'prd')  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# surface enablement


def evaluate_surface_enablement(
    decision: ProductionReadinessDecision,
) -> dict[SurfaceKind, SurfaceState]:
    """Derive which recommendation surfaces a decision enables.

    ``automatic_recommendation`` is only ever enabled at
    ``production_ready`` — a ``limited`` adoption keeps inspection and
    bounded candidate comparison but never auto-recommends.
    """
    if decision.outcome == 'production_ready':
        return {
            'inspect_prediction': 'enabled',
            'compare_candidates': 'enabled',
            'automatic_recommendation': 'enabled',
        }
    if decision.outcome == 'limited':
        return {
            'inspect_prediction': 'enabled',
            'compare_candidates': 'limited',
            'automatic_recommendation': 'disabled',
        }
    return {
        'inspect_prediction': 'disabled',
        'compare_candidates': 'disabled',
        'automatic_recommendation': 'disabled',
    }


class SurfaceEnablement(BaseModel):
    """One surface's state inside a surface decision."""

    model_config = ConfigDict(frozen=True)

    surface: SurfaceKind
    state: SurfaceState
    reason: str


class RecommendationSurfaceDecision(BaseModel):
    """Issued enablement map bound to a readiness decision (rsd-)."""

    model_config = ConfigDict(frozen=True)

    surface_decision_id: str
    surface_decision_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    decision_ref: AuthorityRef
    outcome_at_issue: AdoptionOutcome
    surfaces: tuple[SurfaceEnablement, ...]
    issued_at_utc: str
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'RecommendationSurfaceDecision':
        _require_refs(self.decision_ref)
        if not self.issued_at_utc:
            raise ValueError('issued_at_utc is required')
        kinds = {s.surface for s in self.surfaces}
        expected = {
            'inspect_prediction', 'compare_candidates',
            'automatic_recommendation'}
        if kinds != expected:
            raise ValueError(
                'every surface decision must cover all three '
                'recommendation surfaces')
        for s in self.surfaces:
            if s.surface == 'automatic_recommendation' \
                    and s.state == 'enabled' \
                    and self.outcome_at_issue != 'production_ready':
                raise ValueError(
                    'automatic_recommendation can only be enabled at '
                    'production_ready')
            if s.state == 'enabled' and not s.reason:
                raise ValueError('an enabled surface needs a reason')
            if not s.reason:
                raise ValueError('every surface row needs a reason')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={
                'surface_decision_id', 'surface_decision_sha256'})

    @classmethod
    def create(cls, **fields: Any) -> 'RecommendationSurfaceDecision':
        return _seal(cls, fields, 'surface_decision_id',
                     'surface_decision_sha256', 'rsd')  # type: ignore[return-value]


def verify_surface_decision(
    decision: ProductionReadinessDecision,
    surface_decision: RecommendationSurfaceDecision,
) -> None:
    """Replay the enablement derivation and fail on any divergence —
    a forged or stale enablement map cannot survive re-verification."""
    if surface_decision.decision_ref.ref_id != decision.decision_id \
            or surface_decision.decision_ref.ref_sha256 \
            != decision.decision_sha256:
        raise ProductionReadinessIntegrityError(
            'surface decision is not bound to this readiness decision')
    if surface_decision.outcome_at_issue != decision.outcome:
        raise ProductionReadinessIntegrityError(
            'surface decision outcome disagrees with the readiness '
            'decision')
    expected = evaluate_surface_enablement(decision)
    for surface in surface_decision.surfaces:
        if surface.state != expected[surface.surface]:
            raise ProductionReadinessIntegrityError(
                f'surface {surface.surface} state disagrees with the '
                'readiness decision')


def issue_surface_decision(
    decision: ProductionReadinessDecision,
    issued_at_utc: str,
    reason: str = 'derived from readiness decision',
) -> RecommendationSurfaceDecision:
    """Compose the canonical surface decision for a readiness
    decision."""
    enablement = evaluate_surface_enablement(decision)
    return RecommendationSurfaceDecision.create(
        document_id=decision.document_id,
        decision_ref=AuthorityRef(
            kind='production_readiness_decision',
            ref_id=decision.decision_id,
            ref_sha256=decision.decision_sha256),
        outcome_at_issue=decision.outcome,
        surfaces=tuple(
            SurfaceEnablement(
                surface=surface, state=state, reason=reason)
            for surface, state in enablement.items()),
        issued_at_utc=issued_at_utc)
