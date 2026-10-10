"""Measurement-fixture / observer-scattering authority (issue #743).

A calibrated measurement microphone does not prove the complete
physical setup is acoustically transparent — clips, boom arms,
stands, array frames, cables, speaker stands, laptops/tables and
operator bodies near the capsule contaminate exactly the high-
frequency magnitude, phase, early-reflection and directional
observables HTDT validates. Measured effect: a 1 mm holder band can
shift response ~2 dB at 20 kHz (Terashima et al. 2021).

Basis: Terashima et al. 2021 (holder-band response deviation);
measurement-fixture scattering literature.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_resolver import AuthorityRef
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload


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


_FIXTURE_KINDS = (
    'clip_holder', 'boom_arm', 'stand_tripod', 'array_frame',
    'cable_near_capsule', 'speaker_stand', 'laptop_table_cart',
    'operator_body', 'survey_fixture', 'other',
)

FixtureVerdict = Literal[
    'fixture_qualified',
    'fixture_unregistered',
    'scattering_unbounded',
    'calibrated_mic_is_not_setup',
]


class MeasurementFixture(BaseModel):
    """Declared fixture in the acoustic path (#743) — each object near
    capsule/source is registered so its scattering is bounded."""

    model_config = ConfigDict(frozen=True)

    fixture_id: str
    fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    fixture_kind: Literal[
        'clip_holder', 'boom_arm', 'stand_tripod', 'array_frame',
        'cable_near_capsule', 'speaker_stand', 'laptop_table_cart',
        'operator_body', 'survey_fixture', 'other',
    ]
    distance_to_capsule_m: float | None = None
    descriptor: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('fixture_kind') not in _FIXTURE_KINDS:
                raise ValueError('unknown fixture kind')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'fixture_id', 'fixture_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'MeasurementFixture':
        return _seal(
            cls, payload, 'fixture_id', 'fixture_sha256', 'mfx'
        )


class FixtureScatteringEvidence(BaseModel):
    """Bounded scattering contribution of the fixture set (#743) —
    an estimated or measured bound plus the observable it limits."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    fixture_refs: tuple[AuthorityRef, ...]
    bound_kind: Literal['measured', 'estimated', 'declared_absent']
    magnitude_bound_db: float | None = None
    frequency_range_hz: tuple[float, float] | None = None
    evidence_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('bound_kind') == 'measured' and (
                data.get('evidence_ref') is None
            ):
                raise ValueError(
                    'a measured scattering bound requires evidence'
                )
            if data.get('bound_kind') == 'declared_absent' and (
                data.get('fixture_refs')
            ):
                raise ValueError(
                    'declared_absent cannot list fixtures'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'FixtureScatteringEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'fsx'
        )


def evaluate_fixture_claim(
    fixtures: tuple[MeasurementFixture, ...] | None,
    scattering: FixtureScatteringEvidence | None,
    *,
    mic_calibrated: bool = False,
) -> tuple[FixtureVerdict, str]:
    """Judge whether the physical setup is transparent enough for the
    claimed observables (#743)."""
    if scattering is not None and scattering.bound_kind == (
        'declared_absent'
    ):
        return (
            'fixture_qualified',
            'setup declared fixture-free — bound is honesty-checked',
        )
    if fixtures is None or len(fixtures) == 0:
        if mic_calibrated:
            return (
                'calibrated_mic_is_not_setup',
                'a calibrated microphone does not make the physical '
                'setup acoustically transparent',
            )
        return (
            'fixture_unregistered',
            'no fixtures registered — setup transparency unknown',
        )
    if scattering is None:
        return (
            'scattering_unbounded',
            f'{len(fixtures)} fixture(s) registered with no scattering '
            'bound — a 1 mm holder band measurably shifts HF response',
        )
    if scattering.bound_kind == 'estimated' and (
        scattering.magnitude_bound_db is None
    ):
        return (
            'scattering_unbounded',
            'estimated bound without a magnitude limit',
        )
    return (
        'fixture_qualified',
        'fixtures registered and scattering bounded',
    )


FIXTURE_LABELS: dict[str, str] = {
    'fixture_qualified': '治具適格',
    'fixture_unregistered': '治具未登録',
    'scattering_unbounded': '散乱上限なし',
    'calibrated_mic_is_not_setup': '校正済みマイクはセットアップ適格ではない',
}
