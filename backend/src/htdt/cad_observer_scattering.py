"""Measurement-fixture / observer-scattering authority (issue #743).

A calibrated measurement microphone does NOT prove that the complete
physical measurement setup is acoustically transparent. The fixture —
mic mount, boom/stand, observer position, nearby reflective objects —
can itself scatter sound into the measurement. Transparency evidence
must pin the fixture geometry and a scattering characterization
before a measurement is claimed fixture-clean.

Basis: issue #743 scope; #611/#732 microphone calibration; #573
measurement-state stability; #581 spatial campaign design; #564
prediction↔measurement registration; #566 solver validation.
"""

from __future__ import annotations

from typing import Any

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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


FIXTURE_LABELS: dict[str, str] = {
    'fixture_transparent': '測定治具は透明と評価済み',
    'fixture_contaminating': '測定治具の散乱が測定を汚染',
    'calibration_is_not_transparency': '校正は透明性の証明ではない',
    'insufficient_evidence': '証拠不足',
}


class MeasurementFixtureProfile(BaseModel):
    """Declared physical measurement fixture (fxp- prefix)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    mic_ref: AuthorityRef
    mount_description: str
    observer_distance_m: float | None = None
    nearby_objects: tuple[str, ...] = ()
    scattering_characterization_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'MeasurementFixtureProfile':
        _require_refs(self.mic_ref)
        if not self.mount_description:
            raise ValueError('mount_description must be declared')
        if self.observer_distance_m is not None \
                and self.observer_distance_m < 0.0:
            raise ValueError('observer_distance_m must be >= 0')
        if self.scattering_characterization_ref is not None:
            _require_refs(self.scattering_characterization_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'MeasurementFixtureProfile':
        return _seal(cls, payload, 'profile_id', 'profile_sha256', 'fxp')


class FixtureScatteringObservation(BaseModel):
    """Scattering/contamination observation bound to a fixture
    profile (fxo- prefix)."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    fixture_ref: AuthorityRef
    contamination_detected: bool
    evidence_summary: str | None = None
    result_data_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'FixtureScatteringObservation':
        _require_refs(self.fixture_ref)
        if self.result_data_ref is not None:
            _require_refs(self.result_data_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'FixtureScatteringObservation':
        return _seal(
            cls, payload, 'observation_id',
            'observation_sha256', 'fxo')


def evaluate_transparency_claim(
    profile: MeasurementFixtureProfile | None,
    observation: FixtureScatteringObservation | None,
    mic_calibrated: bool = False,
) -> tuple[str, str]:
    """Mic calibration never substitutes for fixture transparency."""
    if profile is None:
        if mic_calibrated:
            return ('calibration_is_not_transparency',
                    'calibrated_mic_without_fixture_evidence')
        return ('insufficient_evidence', 'no_fixture_profile')
    if observation is None:
        if profile.scattering_characterization_ref is None:
            return ('insufficient_evidence',
                    'no_scattering_characterization')
        return ('insufficient_evidence', 'no_scattering_observation')
    if observation.contamination_detected:
        return ('fixture_contaminating', 'scattering_detected')
    return ('fixture_transparent', 'no_contamination_observed')
