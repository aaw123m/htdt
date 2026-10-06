"""Camera / imaging-measurement-chain authority (issue #716).

A photograph/video/camera-derived measurement is not direct display
truth unless the camera+lens+sensor+exposure+geometry+timing+ISP chain
is qualified for the specific measurand. Lens distortion is not
projector geometry error; camera MTF can cap a display-resolution
measurement; rolling shutter/exposure can corrupt flicker/motion
evidence; ISP tone-mapping/denoise/sharpening alters photometric or
gradation evidence; auto exposure/WB makes repeated captures
incomparable; compressed frames are not raw optical measurements.

Basis: SID/ICDM IDMS v1.3 (camera and imaging-light-measurement-device
setup §3.8, array-detector alias avoidance §3.2.11, 1:1 pixel
correspondence §3.2.12, geometric alignment five-marker §3.8.1, moiré
avoidance §3.10); ISO 17850:2015 (camera geometric distortion);
ISO 9335:2025 (OTF measurement incl. equipment performance/correction).
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


_SHUTTER_KINDS = ('global', 'rolling', 'unknown')
_MEASURAND_KINDS = (
    'geometry', 'uniformity', 'temporal_motion', 'gradation_chroma',
    'resolution_mtf', 'photometric', 'other',
)
_PROCESSING_STATES = (
    'raw_sensor', 'demosaiced_linear', 'tone_mapped', 'compressed',
    'unknown',
)
_CHAIN_STATES = ('unqualified', 'partially_qualified', 'qualified')

ImagingVerdict = Literal[
    'qualified_derived',
    'chain_limited',
    'incomparable_auto_settings',
    'not_display_truth',
    'insufficient_chain_evidence',
]


class ImagingMeasurementChain(BaseModel):
    """Declares one imaging measurement chain end to end (#716).

    An 'unqualified' chain_state may not produce evidence; anything
    short of 'qualified' constrains which measurands may be claimed.
    """

    model_config = ConfigDict(frozen=True)

    chain_id: str
    chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    camera_ref: AuthorityRef | None = None
    lens_ref: AuthorityRef | None = None
    sensor_descriptor: str | None = None
    shutter_kind: Literal['global', 'rolling', 'unknown'] = 'unknown'
    exposure_control: Literal['manual', 'auto', 'unknown'] = 'unknown'
    white_balance_control: Literal[
        'manual', 'auto', 'unknown'
    ] = 'unknown'
    isp_descriptor: str | None = None
    geometry_alignment_ref: AuthorityRef | None = None
    timing_evidence_ref: AuthorityRef | None = None
    chain_state: Literal[
        'unqualified', 'partially_qualified', 'qualified'
    ] = 'unqualified'
    qualified_measurands: tuple[str, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('shutter_kind') not in _SHUTTER_KINDS:
                raise ValueError('unknown shutter kind')
            if data.get('chain_state') not in _CHAIN_STATES:
                raise ValueError('unknown chain state')
            meas = data.get('qualified_measurands') or ()
            unknown = [m for m in meas if m not in _MEASURAND_KINDS]
            if unknown:
                raise ValueError(f'unknown measurands: {unknown}')
            if (
                data.get('chain_state') == 'qualified'
                and not meas
            ):
                raise ValueError(
                    'a qualified chain must name its qualified '
                    'measurands'
                )
            if (
                data.get('chain_state') == 'qualified'
                and data.get('geometry_alignment_ref') is None
            ):
                raise ValueError(
                    'a qualified chain requires a pinned geometric '
                    'alignment record (IDMS five-marker §3.8.1)'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'chain_id', 'chain_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'ImagingMeasurementChain':
        return _seal(cls, payload, 'chain_id', 'chain_sha256', 'imc')


class CameraCalibrationProfile(BaseModel):
    """Pinned calibration evidence for a camera chain (#716).

    Lens distortion (ISO 17850), MTF qualification (ISO 9335) and
    moiré assessment (IDMS §3.10) are each independent pinned test
    references — a chain may claim only what is calibrated.
    """

    model_config = ConfigDict(frozen=True)

    calibration_id: str
    calibration_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    chain_ref: AuthorityRef
    distortion_calibration_ref: AuthorityRef | None = None
    mtf_qualification_ref: AuthorityRef | None = None
    moire_assessment_ref: AuthorityRef | None = None
    alias_correspondence: Literal[
        'pixel_1_1', 'oversampled', 'unknown'
    ] = 'unknown'

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get('chain_ref') is None:
            raise ValueError('calibration requires a pinned chain_ref')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'calibration_id', 'calibration_sha256'},
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'CameraCalibrationProfile':
        return _seal(
            cls, payload, 'calibration_id', 'calibration_sha256',
            'camcal',
        )


class CameraDerivedObservation(BaseModel):
    """One camera-derived measurement under a pinned chain (#716)."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    chain_ref: AuthorityRef
    calibration_ref: AuthorityRef | None = None
    measurand: str
    processing_state: Literal[
        'raw_sensor', 'demosaiced_linear', 'tone_mapped', 'compressed',
        'unknown',
    ]
    exposure_was_auto: bool = False
    white_balance_was_auto: bool = False
    figure_ref: AuthorityRef | None = None
    data_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('chain_ref') is None:
                raise ValueError(
                    'a camera-derived observation requires a pinned '
                    'chain_ref'
                )
            if data.get('measurand') not in _MEASURAND_KINDS:
                raise ValueError('unknown measurand')
            if data.get('processing_state') not in _PROCESSING_STATES:
                raise ValueError('unknown processing state')
            if (
                data.get('figure_ref') is None
                and data.get('data_ref') is None
            ):
                raise ValueError(
                    'an observation without a pinned figure or data '
                    'is not evidence'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'CameraDerivedObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'cdo'
        )


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is None or ref.ref_sha256 is None:
            raise ValueError('authority references must be sha-pinned')


def evaluate_imaging_evidence_claim(
    chain: ImagingMeasurementChain | None,
    observation: CameraDerivedObservation | None,
    calibration: CameraCalibrationProfile | None = None,
) -> tuple[ImagingVerdict, str]:
    """Judge whether a camera-derived observation may stand as display
    evidence for its measurand (#716).
    """
    if chain is None or observation is None:
        return (
            'insufficient_chain_evidence',
            'no qualified chain or observation pinned',
        )
    if chain.chain_state == 'unqualified':
        return (
            'not_display_truth',
            'unqualified imaging chain — a photograph is not display '
            'truth',
        )
    if (
        chain.chain_state == 'qualified'
        and observation.measurand not in chain.qualified_measurands
    ):
        return (
            'chain_limited',
            f'chain not qualified for measurand {observation.measurand}',
        )
    if (
        chain.chain_state == 'qualified'
        and observation.measurand == 'resolution_mtf'
        and calibration is not None
        and calibration.mtf_qualification_ref is None
    ):
        return (
            'chain_limited',
            'camera MTF unqualified — it can cap the measured display '
            'resolution',
        )
    if (
        observation.exposure_was_auto
        or observation.white_balance_was_auto
        or chain.exposure_control == 'auto'
        or chain.white_balance_control == 'auto'
    ):
        return (
            'incomparable_auto_settings',
            'auto exposure/white balance makes repeated captures '
            'incomparable',
        )
    if observation.processing_state in ('tone_mapped', 'compressed'):
        return (
            'chain_limited',
            f'{observation.processing_state} frames are not raw optical '
            'measurements',
        )
    if (
        observation.measurand == 'temporal_motion'
        and chain.shutter_kind in ('rolling', 'unknown')
    ):
        return (
            'chain_limited',
            'rolling/unknown shutter corrupts temporal-motion evidence',
        )
    return (
        'qualified_derived',
        'chain qualified and observation within its pinned measurands',
    )


IMAGING_LABELS: dict[str, str] = {
    'qualified_derived': '適格なカメラ導出証拠',
    'chain_limited': 'チェーン性能による限定',
    'incomparable_auto_settings': '自動設定のため比較不能',
    'not_display_truth': 'カメラ像はディスプレイ真値ではない',
    'insufficient_chain_evidence': 'チェーン証拠不足',
}
