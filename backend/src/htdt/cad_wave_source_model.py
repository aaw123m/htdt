"""R130 source-model compatibility authority (#966).

The R130 wave path injects every loudspeaker as a single equivalent
monopole: ``WaveExcitationModel`` is exactly
``'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'``
and ``cad_wave_excitation`` validates nothing else. For a multi-radiator
loudspeaker (#1006) that collapse is a modelling decision, not a fact —
and until this module nothing recorded or evaluated it, so an R160
hybrid response could not state whether its composition was
source-model compatible at all.

:class:`WaveSourceModelCompatibility` is the evaluated authority that
closes the gap:

- it binds the exact ``WaveSourceExcitationBinding`` (R110 compiled
  source + excitation authority) by semantic hash, so the evaluation can
  never silently attach to a different source/excitation pair;
- it optionally binds the ``MultiRadiatorSourceModel`` recorded for the
  same equipment definition and evaluates — via
  :func:`evaluate_multi_radiator_coherence` — whether collapsing that
  model into one monopole is a supported operation or an unproven one;
- the resulting ``compatibility_state`` is embedded verbatim into the
  R160 composition spec and artifact, which makes the composed response
  honestly labelled ``unverified`` when no source model was assessed.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_multi_radiator_source import (
    MultiRadiatorSourceModel,
    evaluate_multi_radiator_coherence,
)
from .cad_wave_excitation import (
    AcousticWaveExcitationAuthority,
    WaveSourceExcitationBinding,
)


WAVE_SOURCE_MODEL_AUTHORITY_VERSION = 'wave-source-model-compatibility-1'

#: How the exact wave excitation represents the physical source.
WaveSourceCollapseState = Literal[
    # The source model has at most one radiating element at the acoustic
    # reference — a monopole is the native representation, not a collapse.
    'monopole_native',
    # Multi-radiator model coherently summable AND an evidenced point the
    # solver can stand for (effective acoustic center or solver-equivalent
    # point) is recorded.
    'collapse_supported',
    # Multi-radiator model coherently summable but no evidenced
    # acoustic-center / solver-equivalent point pins where the monopole
    # stands — collapse is provable in principle but the injection point
    # is unverified.
    'collapse_supported_unpinned',
    # Multi-radiator model recorded but coherent summation is not
    # supportable (missing complex transfer evidence, no shared phase
    # origin, ...) — a point-monopole collapse is falsified by the model.
    'collapse_unsupported',
    # No multi-radiator source model exists for this equipment definition
    # — the monopole representation was never assessed.
    'unverified',
]

WaveSourceCompatibilityState = Literal[
    'compatible',
    'compatible_with_limitations',
    'unverified',
    'unsupported',
]

#: Which reference point the monopole stands for.
WaveSourcePointBasis = Literal[
    'solver_equivalent_point',
    'effective_acoustic_center',
    'equipment_acoustic_reference',
]


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


class WaveSourceModelCompatibility(BaseModel):
    """Evaluated claim that a bound wave excitation's monopole represents
    the recorded source model — or an explicit record that it does not."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'wave-source-model-compatibility-1'
    ] = WAVE_SOURCE_MODEL_AUTHORITY_VERSION
    compatibility_id: str = Field(
        pattern=r'^wave-source-model-compatibility:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    binding_id: str = Field(
        pattern=r'^wave-source-excitation-binding:[0-9a-f]{64}$'
    )
    binding_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    r110_compiled_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    excitation_id: str = Field(
        pattern=r'^acoustic-wave-excitation:[0-9a-f]{64}$'
    )
    excitation_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str = Field(min_length=1)
    excitation_model: Literal[
        'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'
    ]

    multi_radiator_model_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    coherence_evaluation_id: str | None = Field(default=None, min_length=1)
    coherence_evaluation_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    collapse_state: WaveSourceCollapseState
    compatibility_state: WaveSourceCompatibilityState
    point_basis: WaveSourcePointBasis | None = None
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_compatibility(self) -> 'WaveSourceModelCompatibility':
        if (
            self.collapse_state
            in (
                'collapse_supported',
                'collapse_supported_unpinned',
                'collapse_unsupported',
            )
        ) != (self.coherence_evaluation_id is not None):
            raise ValueError(
                'an evaluated collapse requires the pinned coherence '
                'evaluation'
            )
        if (self.coherence_evaluation_id is None) != (
            self.coherence_evaluation_sha256 is None
        ):
            raise ValueError(
                'coherence evaluation id and hash come together'
            )
        if self.collapse_state == 'unverified' and (
            self.multi_radiator_model_sha256 is not None
        ):
            raise ValueError(
                'an unverified collapse cannot pin a source model'
            )
        if self.compatibility_state == 'compatible' and (
            self.collapse_state
            not in ('monopole_native', 'collapse_supported')
        ):
            raise ValueError(
                'compatible requires monopole_native or collapse_supported'
            )
        if self.compatibility_state == 'unsupported' and (
            self.collapse_state != 'collapse_unsupported'
        ):
            raise ValueError(
                'unsupported requires collapse_unsupported'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError(
                'WaveSourceModelCompatibility semantic hash mismatch'
            )
        if (
            self.compatibility_id
            != f'wave-source-model-compatibility:{expected}'
        ):
            raise ValueError('WaveSourceModelCompatibility id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'compatibility_id', 'semantic_sha256'},
        )
        for key in (
            'multi_radiator_model_sha256',
            'coherence_evaluation_id',
            'coherence_evaluation_sha256',
            'point_basis',
        ):
            if payload.get(key) is None:
                payload.pop(key)
        return payload


def _point_basis(
    model: MultiRadiatorSourceModel,
) -> WaveSourcePointBasis | None:
    if model.reference_points.solver_equivalent_point_m is not None:
        return 'solver_equivalent_point'
    if model.reference_points.effective_acoustic_center_m is not None:
        return 'effective_acoustic_center'
    return None


def evaluate_wave_source_model_compatibility(
    *,
    binding: WaveSourceExcitationBinding,
    excitation: AcousticWaveExcitationAuthority,
    multi_radiator_model: MultiRadiatorSourceModel | None = None,
) -> WaveSourceModelCompatibility:
    """Evaluate the monopole collapse a bound wave excitation performs.

    ``binding`` must pin ``excitation`` exactly; ``multi_radiator_model``
    must pin the binding's equipment definition. The evaluation never
    fabricates evidence: no recorded model means ``unverified`` — not a
    silent pass.
    """

    binding = WaveSourceExcitationBinding.model_validate(
        binding.model_dump(mode='python')
    )
    excitation = AcousticWaveExcitationAuthority.model_validate(
        excitation.model_dump(mode='python')
    )
    if (
        binding.excitation_id != excitation.excitation_id
        or binding.excitation_semantic_sha256 != excitation.semantic_sha256
    ):
        raise ValueError(
            'wave source-model compatibility binding does not pin this '
            'excitation authority'
        )
    if binding.source_model != excitation.excitation_model:
        raise ValueError('wave source binding/excitation model mismatch')

    if multi_radiator_model is None:
        core = _core(
            binding=binding,
            excitation=excitation,
            multi_radiator_model_sha256=None,
            coherence_evaluation_id=None,
            coherence_evaluation_sha256=None,
            collapse_state='unverified',
            compatibility_state='unverified',
            point_basis=None,
            reasons=(
                'no multi-radiator source model recorded for this '
                'equipment definition — the point-monopole representation '
                'was never assessed',
            ),
        )
        return _build(core)

    model = MultiRadiatorSourceModel.model_validate(
        multi_radiator_model.model_dump(mode='python')
    )
    if (
        model.equipment_definition_id != binding.equipment_definition_id
        or model.equipment_definition_version
        != binding.equipment_definition_version
        or model.equipment_definition_sha256
        != binding.equipment_definition_sha256
    ):
        raise ValueError(
            'multi-radiator source model does not pin the binding\'s '
            'equipment definition'
        )

    if len(model.elements) < 2:
        core = _core(
            binding=binding,
            excitation=excitation,
            multi_radiator_model_sha256=model.semantic_sha256,
            coherence_evaluation_id=None,
            coherence_evaluation_sha256=None,
            collapse_state='monopole_native',
            compatibility_state='compatible',
            point_basis='equipment_acoustic_reference',
            reasons=(
                'source model records fewer than two radiating elements '
                '— the equivalent monopole is the native representation',
            ),
        )
        return _build(core)

    coherence = evaluate_multi_radiator_coherence(model=model)
    point_basis = _point_basis(model)
    if not coherence.coherent_summation_supported:
        failed = '; '.join(
            check.reason
            for check in coherence.checks
            if check.status not in ('PASS', 'NOT_APPLICABLE')
        )
        core = _core(
            binding=binding,
            excitation=excitation,
            multi_radiator_model_sha256=model.semantic_sha256,
            coherence_evaluation_id=coherence.evaluation_id,
            coherence_evaluation_sha256=coherence.evaluation_sha256,
            collapse_state='collapse_unsupported',
            compatibility_state='unsupported',
            point_basis=point_basis,
            reasons=(
                'multi-radiator source cannot be coherently summed — '
                'collapsing it to one monopole is falsified: ' + failed,
            ),
        )
        return _build(core)

    if point_basis is None:
        core = _core(
            binding=binding,
            excitation=excitation,
            multi_radiator_model_sha256=model.semantic_sha256,
            coherence_evaluation_id=coherence.evaluation_id,
            coherence_evaluation_sha256=coherence.evaluation_sha256,
            collapse_state='collapse_supported_unpinned',
            compatibility_state='compatible_with_limitations',
            point_basis='equipment_acoustic_reference',
            reasons=(
                'multi-radiator source is coherently summable but no '
                'evidenced acoustic center or solver-equivalent point '
                'pins where the monopole stands — the equipment acoustic '
                'reference is used unverified',
            ),
        )
        return _build(core)

    core = _core(
        binding=binding,
        excitation=excitation,
        multi_radiator_model_sha256=model.semantic_sha256,
        coherence_evaluation_id=coherence.evaluation_id,
        coherence_evaluation_sha256=coherence.evaluation_sha256,
        collapse_state='collapse_supported',
        compatibility_state='compatible',
        point_basis=point_basis,
        reasons=(
            'multi-radiator source is coherently summable and an '
            f'evidenced {point_basis.replace("_", " ")} pins the '
            'monopole injection point',
        ),
    )
    return _build(core)


def _core(
    *,
    binding: WaveSourceExcitationBinding,
    excitation: AcousticWaveExcitationAuthority,
    multi_radiator_model_sha256: str | None,
    coherence_evaluation_id: str | None,
    coherence_evaluation_sha256: str | None,
    collapse_state: WaveSourceCollapseState,
    compatibility_state: WaveSourceCompatibilityState,
    point_basis: WaveSourcePointBasis | None,
    reasons: tuple[str, ...],
) -> dict[str, Any]:
    core: dict[str, Any] = {
        'authority_version': WAVE_SOURCE_MODEL_AUTHORITY_VERSION,
        'binding_id': binding.binding_id,
        'binding_semantic_sha256': binding.semantic_sha256,
        'r110_compiled_source_sha256': binding.r110_compiled_source_sha256,
        'excitation_id': excitation.excitation_id,
        'excitation_semantic_sha256': excitation.semantic_sha256,
        'source_entity_id': binding.source_entity_id,
        'excitation_model': excitation.excitation_model,
        'collapse_state': collapse_state,
        'compatibility_state': compatibility_state,
        'reasons': list(reasons),
    }
    if multi_radiator_model_sha256 is not None:
        core['multi_radiator_model_sha256'] = multi_radiator_model_sha256
    if coherence_evaluation_id is not None:
        core['coherence_evaluation_id'] = coherence_evaluation_id
        core['coherence_evaluation_sha256'] = coherence_evaluation_sha256
    if point_basis is not None:
        core['point_basis'] = point_basis
    return core


def _build(core: dict[str, Any]) -> WaveSourceModelCompatibility:
    digest = _digest(core)
    return WaveSourceModelCompatibility(
        compatibility_id=f'wave-source-model-compatibility:{digest}',
        semantic_sha256=digest,
        **core,
    )
