"""Group-delay audibility and headphone-coupling authority
(issues #657, #702).

A group-delay peak or excess-phase deviation is not automatically
audible or worth correcting — audibility depends on frequency,
level, listener and stimulus; a lower number is not automatically
better (#657). And an exact BRIR/HRTF render is not what reaches
the eardrum unless headphone transfer function, fit/placement,
listener coupling and compensation filter are represented and
qualified (#702 — HpTF varies across listeners and repeated
placement).

Basis: group-delay audibility research (frequency-dependent
thresholds); headphone-transfer-function listener-variability
studies.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


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


class GroupDelayAudibility(BaseModel):
    """Perceptual verdict on a measured group-delay deviation
    (#657) — the peak value alone does not decide; the audibility
    context must be declared."""

    model_config = ConfigDict(frozen=True)

    verdict_id: str
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    peak_delay_ms: float
    frequency_hz: float
    stimulus_kind: Literal[
        'speech', 'music', 'impulsive', 'sine', 'other', 'unknown',
    ]
    audibility_threshold_ref: AuthorityRef | None = None
    listener_screened: bool | None = None
    measured_delay_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('peak_delay_ms') is None or (
                data.get('frequency_hz') is None
            ):
                raise ValueError(
                    'group-delay audibility requires the peak '
                    'value AND its frequency — thresholds are '
                    'frequency-dependent'
                )
            if data.get('stimulus_kind') not in (
                'speech', 'music', 'impulsive', 'sine', 'other',
                'unknown',
            ):
                raise ValueError('unknown stimulus kind')
            if data.get('stimulus_kind') == 'unknown':
                raise ValueError(
                    'audibility depends on stimulus — declare it'
                )
            if data.get('measured_delay_ref') is None:
                raise ValueError(
                    'audibility interpretation requires the pinned '
                    'delay measurement'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'verdict_id', 'verdict_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'GroupDelayAudibility':
        return _seal(
            cls, payload, 'verdict_id', 'verdict_sha256', 'gda'
        )


class HeadphoneCouplingEvidence(BaseModel):
    """Headphone→eardrum coupling qualification (#702) — HpTF/
    compensation identity bound to the exact headphone, listener
    and fit state; generic compensation is not listener-applicable."""

    model_config = ConfigDict(frozen=True)

    coupling_id: str
    coupling_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    headphone_model: str
    compensation_kind: Literal[
        'individual_hptf', 'average_hptf', 'diffuse_field',
        'free_field', 'uncompensated', 'other', 'unknown',
    ]
    fit_state: Literal[
        'verified_placement', 'nominal_placement', 'unknown',
    ]
    listener_individualized: bool | None = None
    compensation_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('compensation_kind') not in (
                'individual_hptf', 'average_hptf', 'diffuse_field',
                'free_field', 'uncompensated', 'other', 'unknown',
            ):
                raise ValueError('unknown compensation kind')
            if data.get('compensation_kind') == 'unknown':
                raise ValueError(
                    'headphone coupling requires declared '
                    'compensation semantics'
                )
            if data.get('fit_state') not in (
                'verified_placement', 'nominal_placement',
                'unknown',
            ):
                raise ValueError('unknown fit state')
            if data.get('fit_state') == 'unknown':
                raise ValueError(
                    'repeated placement materially changes the '
                    'transfer — declare the fit state'
                )
            if not data.get('headphone_model'):
                raise ValueError(
                    'coupling requires the exact headphone model'
                )
            if data.get('compensation_kind') != 'uncompensated' and (
                data.get('compensation_ref') is None
            ):
                raise ValueError(
                    'a compensation claim requires the filter/'
                    'curve ref'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'coupling_id', 'coupling_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'HeadphoneCouplingEvidence':
        return _seal(
            cls, payload, 'coupling_id', 'coupling_sha256', 'hpc'
        )


PerceptualVerdict = Literal[
    'qualified_perceptual',
    'peak_is_not_audibility',
    'lower_is_not_better',
    'brir_is_not_eardrum',
    'coupling_unqualified',
]


def evaluate_perceptual_claim(
    groupdelay: GroupDelayAudibility | None,
    coupling: HeadphoneCouplingEvidence | None,
    *,
    correction_reduced_delay: bool = False,
    brir_rendered: bool = False,
) -> tuple[PerceptualVerdict, str]:
    """Judge perceptual claims (#657/#702)."""
    if correction_reduced_delay and groupdelay is None:
        return (
            'lower_is_not_better',
            'correction reduced the group-delay number — the '
            'audibility threshold context was never pinned, so '
            'the improvement is unverified',
        )
    if groupdelay is not None and (
        groupdelay.audibility_threshold_ref is None
    ):
        return (
            'peak_is_not_audibility',
            'a peak value without an audibility threshold is not '
            'an audibility verdict',
        )
    if brir_rendered and coupling is None:
        return (
            'brir_is_not_eardrum',
            'an exact BRIR/HRTF render does not reach the eardrum '
            'unqualified — headphone coupling must be declared',
        )
    if coupling is not None:
        if coupling.compensation_kind == 'average_hptf' and (
            coupling.listener_individualized is not False
        ):
            pass  # honest: average declared as average
        if coupling.fit_state == 'nominal_placement' and (
            coupling.compensation_kind == 'individual_hptf'
        ):
            return (
                'coupling_unqualified',
                'individualized compensation claims need '
                'verified placement — repeated fit changes '
                'the transfer',
            )
        if coupling.compensation_kind == 'uncompensated':
            return (
                'coupling_unqualified',
                'uncompensated headphone chain — eardrum '
                'signal differs from the render',
            )
    return (
        'qualified_perceptual',
        'perceptual context declared and qualified',
    )


PERCEPTUAL_LABELS: dict[str, str] = {
    'qualified_perceptual': '知覚適格',
    'peak_is_not_audibility': 'ピーク値は可聴性ではない',
    'lower_is_not_better': '低い値はより良い音ではない',
    'brir_is_not_eardrum': 'BRIRは鼓膜ではない',
    'coupling_unqualified': '結合未適格',
}
