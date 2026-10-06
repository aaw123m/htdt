"""Fractional-octave band semantics authority (issue #763).

A result labelled `63 Hz`, `1/3 octave` or `octave band` is not
reproducible unless the fractional-octave band definition, exact centre
frequencies, filter realization, class/profile, processing domain and
integration semantics are preserved.

Basis: IEC 61260-1:2014 (octave-band and fractional-octave-band
filters; class 0/1/2 tolerances; exact mid-band frequencies from the
frequency-ratio expression; nominal designations), IEC 61260-3
(conformance tests), ISO 266 (preferred frequencies). REW semantics:
third-octave exports derived from FFT bins unless a filter bank is
applied — the bin-integration path must declare itself and never pose
as an IEC-conforming filter bank.
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


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


BandKind = Literal[
    'octave', 'half_octave', 'third_octave', 'sixth_octave',
    'twelfth_octave', 'twentyfourth_octave', 'custom_spacing', 'unknown',
]
BandFrequencyStandard = Literal[
    'iec61260_exact', 'iec61260_nominal', 'iso266_nominal',
    'computed_pow10', 'custom_table', 'unknown',
]
FilterClass = Literal[
    'class_0', 'class_1', 'class_2', 'non_iec_filter', 'no_filter',
    'unknown',
]
BandProcessDomain = Literal[
    'time_domain_filter', 'analog_filter_bank',
    'frequency_domain_bin_integration', 'fft_then_band', 'unknown',
]
IntegrationSemantics = Literal[
    'energy_sum', 'mean_pressure_squared', 'rms_level',
    'power_sum', 'incoherent_sum', 'log_average', 'peak_hold', 'unknown',
]
BandSemanticsVerdict = Literal[
    'semantics_pinned', 'partially_pinned', 'unpinned', 'incomparable',
]

BAND_KIND_LABELS: dict[str, str] = {
    'octave': '1/1オクターブ',
    'half_octave': '1/2オクターブ',
    'third_octave': '1/3オクターブ',
    'sixth_octave': '1/6オクターブ',
    'twelfth_octave': '1/12オクターブ',
    'twentyfourth_octave': '1/24オクターブ',
    'custom_spacing': '独自帯域間隔',
    'unknown': '不明',
}
FREQUENCY_STANDARD_LABELS: dict[str, str] = {
    'iec61260_exact': 'IEC 61260 精密中心周波数',
    'iec61260_nominal': 'IEC 61260 呼称中心周波数',
    'iso266_nominal': 'ISO 266 呼称周波数',
    'computed_pow10': '計算中心周波数',
    'custom_table': '独自周波数表',
    'unknown': '不明',
}
FILTER_CLASS_LABELS: dict[str, str] = {
    'class_0': 'IEC クラス0',
    'class_1': 'IEC クラス1',
    'class_2': 'IEC クラス2',
    'non_iec_filter': 'IEC 非適合フィルタ',
    'no_filter': 'フィルタなし',
    'unknown': '不明',
}
PROCESS_DOMAIN_LABELS: dict[str, str] = {
    'time_domain_filter': '時間領域フィルタ',
    'analog_filter_bank': 'アナログフィルタバンク',
    'frequency_domain_bin_integration': '周波数ビン統合',
    'fft_then_band': 'FFT 後帯域合成',
    'unknown': '不明',
}
INTEGRATION_LABELS: dict[str, str] = {
    'energy_sum': 'エネルギー和',
    'mean_pressure_squared': '平均二乗圧力',
    'rms_level': 'RMS レベル',
    'power_sum': 'パワー和',
    'incoherent_sum': '非相干和',
    'log_average': '対数平均',
    'peak_hold': 'ピーク保持',
    'unknown': '不明',
}
BAND_VERDICT_LABELS: dict[str, str] = {
    'semantics_pinned': '帯域定義確定',
    'partially_pinned': '帯域定義一部確定',
    'unpinned': '帯域定義未確定',
    'incomparable': '比較不能',
}

_IEC_STANDARDS = {'iec61260_exact', 'iec61260_nominal'}
_IEC_CLASSES = {'class_0', 'class_1', 'class_2'}
_FILTERED_DOMAINS = {'time_domain_filter', 'analog_filter_bank'}


class FractionalOctaveProfile(BaseModel):
    """Declared fractional-octave band semantics for one result family.

    An IEC-class claim requires a filtered processing domain; a
    bin-integration path (e.g. third-octave from FFT bins) is legal but
    must declare itself so it is never mistaken for a conforming IEC
    filter bank.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    band_kind: BandKind
    frequency_standard: BandFrequencyStandard = 'unknown'
    filter_class: FilterClass = 'unknown'
    process_domain: BandProcessDomain = 'unknown'
    integration_semantics: IntegrationSemantics = 'unknown'
    centre_frequency_table_ref: AuthorityRef | None = None
    standard_ref: AuthorityRef | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'FractionalOctaveProfile':
        if self.centre_frequency_table_ref is not None:
            _require_refs(self.centre_frequency_table_ref)
        if self.standard_ref is not None:
            _require_refs(self.standard_ref)
        if (
            self.frequency_standard in _IEC_STANDARDS
            and self.filter_class not in _IEC_CLASSES
        ):
            raise ValueError(
                'iec61260 frequency standards require a declared '
                'IEC class 0/1/2 filter'
            )
        if (
            self.frequency_standard in _IEC_STANDARDS
            and self.process_domain not in _FILTERED_DOMAINS
        ):
            raise ValueError(
                'an IEC 61260 band claim requires a filtered processing '
                'domain; bin integration is not an IEC filter bank'
            )
        if (
            self.frequency_standard == 'custom_table'
            and self.centre_frequency_table_ref is None
        ):
            raise ValueError(
                'custom_table frequency standard requires a pinned '
                'centre-frequency table'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'FractionalOctaveProfile':
        return _seal(
            cls, kwargs, 'profile_id', 'profile_sha256', 'foct'
        )


class BandIntegrationRecord(BaseModel):
    """One band-integrated result set bound to a profile.

    ``verdict`` is sealed by the producer; ``evaluate_band_semantics``
    re-derives it so a tampered verdict cannot launder an unpinned
    result into ``semantics_pinned``.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str
    record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    profile_ref: AuthorityRef
    source_result_ref: AuthorityRef | None = None
    band_centre_table_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    band_count: int = Field(default=0, ge=0)
    verdict: BandSemanticsVerdict = 'unpinned'
    reasons: tuple[str, ...] = ()
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'BandIntegrationRecord':
        _require_refs(self.profile_ref)
        if self.source_result_ref is not None:
            _require_refs(self.source_result_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'BandIntegrationRecord':
        return _seal(
            cls, kwargs, 'record_id', 'record_sha256', 'band'
        )


def evaluate_band_semantics(
    profile: FractionalOctaveProfile,
) -> BandSemanticsVerdict:
    """Fail-closed verdict for one band profile.

    ``semantics_pinned`` requires every axis declared; a non-IEC filter
    or bin-integration path is legal but only ``partially_pinned`` — it
    must not be read as an IEC-conforming band measurement.
    """
    if 'unknown' in (
        profile.band_kind,
        profile.frequency_standard,
        profile.filter_class,
        profile.process_domain,
        profile.integration_semantics,
    ):
        return 'unpinned'
    if (
        profile.filter_class in ('non_iec_filter', 'no_filter')
        or profile.process_domain
        in ('frequency_domain_bin_integration', 'fft_then_band')
    ):
        return 'partially_pinned'
    return 'semantics_pinned'


def compare_band_semantics(
    a: FractionalOctaveProfile,
    b: FractionalOctaveProfile,
) -> tuple[BandSemanticsVerdict, tuple[str, ...]]:
    """Comparability of two band results.

    Band results are comparable only when kind, frequency standard and
    integration semantics agree; a class-1 filter-bank result and a
    bin-integrated third-octave result are ``incomparable`` rather than
    silently averaged (IEC 61260 passbands differ from bin edges).
    """
    reasons: list[str] = []
    if a.band_kind != b.band_kind:
        reasons.append('band_kind_mismatch')
    if a.frequency_standard != b.frequency_standard:
        reasons.append('frequency_standard_mismatch')
    if a.integration_semantics != b.integration_semantics:
        reasons.append('integration_semantics_mismatch')
    filtered_a = a.process_domain in _FILTERED_DOMAINS
    filtered_b = b.process_domain in _FILTERED_DOMAINS
    if filtered_a != filtered_b:
        reasons.append('process_domain_mismatch')
    if reasons:
        return 'incomparable', tuple(reasons)
    va = evaluate_band_semantics(a)
    vb = evaluate_band_semantics(b)
    if va == 'unpinned' or vb == 'unpinned':
        return 'incomparable', ('unpinned_profile',)
    if 'partially_pinned' in (va, vb):
        return 'partially_pinned', ('non_iec_path',)
    return 'semantics_pinned', ()
