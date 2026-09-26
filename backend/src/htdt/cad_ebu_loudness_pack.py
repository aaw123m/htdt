"""EBU loudness conformance fixture pack (#1085).

Admission + conformance records for the EBU R 128 test sequences
(Tech 3341 minimum-requirements metering signals and Tech 3342 LRA
signals) so the application's BS.1770 loudness measurement can be
validated deterministically against published expected values.

Rules:

- the sequence payloads stay download-on-demand from tech.ebu.ch —
  the pack carries the published *expected responses* and the
  admission record, never the audio;
- expected values are transcribed verbatim from EBU Tech 3341 (2023)
  Table 1 and EBU Tech 3342 (2021) Table 1 — tolerance is part of the
  fixture;
- :func:`check_loudness_measurement` is the only conformance path —
  measurement vs expected within the documented ±tolerance; anything
  else reports ``FAIL``; missing/unknown measurement reports
  ``incomplete``, never PASS;
- 'EBU Mode' loudness uses LUFS (absolute) or LU relative to
  −23.0 LUFS = 0.0 LU per R 128; the pack stores expectations in the
  unit the table publishes (LUFS unless labelled LU).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_external_admission import (
    ExternalAssetAdmission,
    build_external_asset_admission,
)


EBU_LOUDNESS_AUTHORITY_VERSION = 'ebu-loudness-1'


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest()


LoudnessMetric = Literal[
    'integrated_lufs',   # I — BS.1770 integrated, absolute LUFS
    'integrated_lu',     # I — relative to -23.0 LUFS = 0.0 LU
    'short_term_lufs',   # S — 3 s window
    'momentary_lufs',    # M — 400 ms window
    'max_short_term_lufs',
    'max_momentary_lufs',
    'lra_lu',            # Loudness Range in LU (Tech 3342)
    'max_true_peak_dbtp',
]

CheckVerdict = Literal['pass', 'fail', 'incomplete']


class LoudnessExpectation(BaseModel):
    """One published expected measurement on one test signal."""

    model_config = ConfigDict(frozen=True)

    metric: LoudnessMetric
    expected: float | tuple[float, ...]
    """Single expected value, or the ordered list of successive
    values for 'live meter' segment tests (Tech 3341 #11/#14)."""
    tolerance_plus: float = Field(gt=0.0)
    tolerance_minus: float = Field(gt=0.0)
    """Asymmetric tolerances (e.g. dBTP +0.2/−0.4)."""
    constant_after_s: float | None = Field(
        default=None, gt=0.0
    )
    """For M/S tests, the publish's 'constant after N s' qualifier."""
    detail: str = ''


class LoudnessTestCase(BaseModel):
    """One published EBU conformance test signal + expectations."""

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    tech_doc: Literal['tech3341', 'tech3342']
    case_number: int = Field(gt=0)
    signal_description: str = Field(min_length=1)
    expectations: tuple[LoudnessExpectation, ...] = Field(
        min_length=1
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _check(self) -> 'LoudnessTestCase':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('loudness case hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )


def _expect(
    metric: LoudnessMetric,
    expected: float | tuple[float, ...],
    tol: float,
    *,
    tol_minus: float | None = None,
    constant_after_s: float | None = None,
    detail: str = '',
) -> LoudnessExpectation:
    return LoudnessExpectation(
        metric=metric,
        expected=expected,
        tolerance_plus=tol,
        tolerance_minus=tol if tol_minus is None else tol_minus,
        constant_after_s=constant_after_s,
        detail=detail,
    )


def _case(
    case_id: str,
    tech_doc: Literal['tech3341', 'tech3342'],
    case_number: int,
    signal_description: str,
    expectations: tuple[LoudnessExpectation, ...],
) -> LoudnessTestCase:
    probe = LoudnessTestCase.model_construct(
        case_id=case_id,
        tech_doc=tech_doc,
        case_number=case_number,
        signal_description=signal_description,
        expectations=expectations,
        semantic_sha256='',
    )
    return LoudnessTestCase(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# --- EBU Tech 3341 (2023) Table 1, transcribed verbatim -------------------

TECH3341_CASES: tuple[LoudnessTestCase, ...] = (
    _case(
        'ebu-3341-01', 'tech3341', 1,
        'Stereo sine 1 kHz, −23.0 dBFS per-channel peak, in-phase, 20 s',
        (
            _expect('integrated_lufs', -23.0, 0.1),
            _expect('momentary_lufs', -23.0, 0.1),
            _expect('short_term_lufs', -23.0, 0.1),
            _expect('integrated_lu', 0.0, 0.1),
        ),
    ),
    _case(
        'ebu-3341-02', 'tech3341', 2,
        'As #1 at −33.0 dBFS',
        (
            _expect('integrated_lufs', -33.0, 0.1),
            _expect('momentary_lufs', -33.0, 0.1),
            _expect('short_term_lufs', -33.0, 0.1),
            _expect('integrated_lu', -10.0, 0.1),
        ),
    ),
    _case(
        'ebu-3341-03', 'tech3341', 3,
        '3 tones: 10 s −36.0 dBFS; 60 s −23.0 dBFS; 10 s −36.0 dBFS '
        '(gating)',
        (
            _expect('integrated_lufs', -23.0, 0.1),
            _expect('integrated_lu', 0.0, 0.1),
        ),
    ),
    _case(
        'ebu-3341-04', 'tech3341', 4,
        '5 tones: 10 s −72.0; 10 s −36.0; 60 s −23.0; 10 s −36.0; '
        '10 s −72.0 dBFS (absolute+relative gating)',
        (
            _expect('integrated_lufs', -23.0, 0.1),
            _expect('integrated_lu', 0.0, 0.1),
        ),
    ),
    _case(
        'ebu-3341-05', 'tech3341', 5,
        '3 tones: 20 s −26.0; 20.1 s −20.0; 20 s −26.0 dBFS '
        '(gate-boundary)',
        (
            _expect('integrated_lufs', -23.0, 0.1),
            _expect('integrated_lu', 0.0, 0.1),
        ),
    ),
    _case(
        'ebu-3341-06', 'tech3341', 6,
        '5.0-channel sine 1 kHz 20 s; per-channel peaks −28.0 (L,R), '
        '−24.0 (C), −30.0 (Ls,Rs) dBFS (K-weighted channel sum)',
        (
            _expect('integrated_lufs', -23.0, 0.1),
            _expect('integrated_lu', 0.0, 0.1),
        ),
    ),
    _case(
        'ebu-3341-07', 'tech3341', 7,
        'Authentic programme 1, stereo, narrow loudness range (promo '
        'genre)',
        (
            _expect('integrated_lufs', -23.0, 0.1),
            _expect('integrated_lu', 0.0, 0.1),
        ),
    ),
    _case(
        'ebu-3341-08', 'tech3341', 8,
        'Authentic programme 2, stereo, wide loudness range '
        '(movie/drama genre)',
        (
            _expect('integrated_lufs', -23.0, 0.1),
            _expect('integrated_lu', 0.0, 0.1),
        ),
    ),
    _case(
        'ebu-3341-09', 'tech3341', 9,
        '(1.34 s −20.0 dBFS; 1.66 s −30.0 dBFS) ×5 — short-term '
        'response',
        (
            _expect(
                'short_term_lufs', -23.0, 0.1,
                constant_after_s=3.0,
            ),
        ),
    ),
    _case(
        'ebu-3341-10', 'tech3341', 10,
        'File-based; 20 segments: i·0.15 s silence; 3 s −23.0 dBFS; '
        '1 s silence (i=0..19)',
        (
            _expect('max_short_term_lufs', -23.0, 0.1,
                    detail='per segment'),
        ),
    ),
    _case(
        'ebu-3341-11', 'tech3341', 11,
        'Live meters; 20 tones: i·0.15 s silence; 3 s −38.0+i dBFS; '
        '3−i·0.15 s silence (i=0..19)',
        (
            _expect(
                'max_short_term_lufs',
                tuple(float(-38.0 + i) for i in range(20)),
                0.1,
                detail='successive values −38.0…−19.0 LUFS',
            ),
        ),
    ),
    _case(
        'ebu-3341-12', 'tech3341', 12,
        '(0.18 s −20.0 dBFS; 0.22 s −30.0 dBFS) ×25 — momentary '
        'response',
        (
            _expect(
                'momentary_lufs', -23.0, 0.1,
                constant_after_s=1.0,
            ),
        ),
    ),
    _case(
        'ebu-3341-13', 'tech3341', 13,
        'File-based; 20 segments: i·20 ms silence; 400 ms −23.0 dBFS; '
        '1 s silence (i=0..19)',
        (
            _expect('max_momentary_lufs', -23.0, 0.1,
                    detail='per segment'),
        ),
    ),
    _case(
        'ebu-3341-14', 'tech3341', 14,
        'Live meters; 20 tones: i·20 ms silence; 400 ms −38.0+i dBFS; '
        '400−i·20 ms silence (i=0..19)',
        (
            _expect(
                'max_momentary_lufs',
                tuple(float(-38.0 + i) for i in range(20)),
                0.1,
                detail='successive values −38.0…−19.0 LUFS',
            ),
        ),
    ),
    _case(
        'ebu-3341-15', 'tech3341', 15,
        'Stereo sine fs/4 Hz, 0.50 FFS amplitude, 0.0° phase '
        '(true-peak); 10 ms tapers',
        (
            _expect('max_true_peak_dbtp', -6.0, 0.2, tol_minus=0.4),
        ),
    ),
    _case(
        'ebu-3341-16', 'tech3341', 16,
        'Stereo sine fs/4 Hz, 0.50 FFS, 45.0° phase',
        (
            _expect('max_true_peak_dbtp', -6.0, 0.2, tol_minus=0.4),
        ),
    ),
    _case(
        'ebu-3341-17', 'tech3341', 17,
        'Stereo sine fs/6 Hz, 0.50 FFS, 60.0° phase',
        (
            _expect('max_true_peak_dbtp', -6.0, 0.2, tol_minus=0.4),
        ),
    ),
    _case(
        'ebu-3341-18', 'tech3341', 18,
        'Stereo sine fs/8 Hz, 0.50 FFS, 67.5° phase',
        (
            _expect('max_true_peak_dbtp', -6.0, 0.2, tol_minus=0.4),
        ),
    ),
    _case(
        'ebu-3341-19', 'tech3341', 19,
        'Stereo sine fs/4 Hz, 1.41 FFS, 45.0° phase',
        (
            _expect('max_true_peak_dbtp', 3.0, 0.2, tol_minus=0.4),
        ),
    ),
    _case(
        'ebu-3341-20', 'tech3341', 20,
        'Sine fs/6 at 0.50 FFS containing one period of fs/4 at 1.00 '
        'amplitude; synthesized at 4·fs then lowpass-filtered and '
        'downsampled with 0-sample offset',
        (
            _expect('max_true_peak_dbtp', 0.0, 0.2, tol_minus=0.4),
        ),
    ),
    _case(
        'ebu-3341-21', 'tech3341', 21,
        'As #20, downsampled at a 1-sample offset',
        (
            _expect('max_true_peak_dbtp', 0.0, 0.2, tol_minus=0.4),
        ),
    ),
    _case(
        'ebu-3341-22', 'tech3341', 22,
        'As #20, downsampled at a 2-sample offset',
        (
            _expect('max_true_peak_dbtp', 0.0, 0.2, tol_minus=0.4),
        ),
    ),
    _case(
        'ebu-3341-23', 'tech3341', 23,
        'As #20, downsampled at a 3-sample offset',
        (
            _expect('max_true_peak_dbtp', 0.0, 0.2, tol_minus=0.4),
        ),
    ),
)


# --- EBU Tech 3342 (2021) Table 1, transcribed verbatim -------------------

TECH3342_CASES: tuple[LoudnessTestCase, ...] = (
    _case(
        'ebu-3342-01', 'tech3342', 1,
        'Stereo sine 1 kHz, 20 s at −20.0 dBFS then 20 s at '
        '−30.0 dBFS',
        (_expect('lra_lu', 10.0, 1.0),),
    ),
    _case(
        'ebu-3342-02', 'tech3342', 2,
        'As #1 with tones at −20.0 and −15.0 dBFS',
        (_expect('lra_lu', 5.0, 1.0),),
    ),
    _case(
        'ebu-3342-03', 'tech3342', 3,
        'As #1 with tones at −40.0 and −20.0 dBFS',
        (_expect('lra_lu', 20.0, 1.0),),
    ),
    _case(
        'ebu-3342-04', 'tech3342', 4,
        '5 tone-segments at −50.0, −35.0, −20.0, −35.0, −50.0 dBFS; '
        '20 s each',
        (_expect('lra_lu', 15.0, 1.0),),
    ),
    _case(
        'ebu-3342-05', 'tech3342', 5,
        'Authentic programme 1, stereo, narrow loudness range',
        (_expect('lra_lu', 5.0, 1.0),),
    ),
    _case(
        'ebu-3342-06', 'tech3342', 6,
        'Authentic programme 2, stereo, wide loudness range',
        (_expect('lra_lu', 15.0, 1.0),),
    ),
)

LOUDNESS_CASE_MATRIX: tuple[LoudnessTestCase, ...] = (
    TECH3341_CASES + TECH3342_CASES
)


class LoudnessCheck(NamedTuple):
    verdict: CheckVerdict
    case_id: str
    metric: LoudnessMetric
    detail: str


def _within(
    value: float,
    expected: float,
    tol_plus: float,
    tol_minus: float,
) -> bool:
    return (
        expected - tol_minus <= value <= expected + tol_plus
    )


def check_loudness_measurement(
    case: LoudnessTestCase,
    metric: LoudnessMetric,
    measured: float | tuple[float, ...] | None,
) -> LoudnessCheck:
    """Conformance check for one published expectation.

    ``incomplete`` when the metric isn't in the case or ``measured``
    is absent — never a PASS.
    """
    exp = next(
        (e for e in case.expectations if e.metric == metric), None
    )
    if exp is None:
        return LoudnessCheck(
            'incomplete',
            case.case_id,
            metric,
            'metric not exercised by this case',
        )
    if measured is None:
        return LoudnessCheck(
            'incomplete', case.case_id, metric, 'no measurement'
        )
    if isinstance(exp.expected, tuple):
        if not isinstance(measured, tuple) or len(measured) != len(
            exp.expected
        ):
            return LoudnessCheck(
                'incomplete',
                case.case_id,
                metric,
                'expected a sequence of successive values',
            )
        ok = all(
            _within(m, e, exp.tolerance_plus, exp.tolerance_minus)
            for m, e in zip(measured, exp.expected)
        )
    else:
        if isinstance(measured, tuple):
            return LoudnessCheck(
                'incomplete',
                case.case_id,
                metric,
                'expected a single value',
            )
        ok = _within(
            measured,
            exp.expected,
            exp.tolerance_plus,
            exp.tolerance_minus,
        )
    return LoudnessCheck(
        'pass' if ok else 'fail',
        case.case_id,
        metric,
        (
            'within tolerance'
            if ok
            else f'out of tolerance (expected {exp.expected} '
            f'+{exp.tolerance_plus}/-{exp.tolerance_minus})'
        ),
    )


def loudness_case(case_id: str) -> LoudnessTestCase | None:
    for c in LOUDNESS_CASE_MATRIX:
        if c.case_id == case_id:
            return c
    return None


# --- admission records ----------------------------------------------------

EBU_TECH3341_ADMISSION: ExternalAssetAdmission = (
    build_external_asset_admission(
        admission_id='ebu-tech3341-test-signals',
        dataset_name='ebu-tech3341',
        dataset_title=(
            'EBU Tech 3341 minimum-requirements test signals'
        ),
        publisher='European Broadcasting Union',
        source_kind='institutional_repository',
        admission_state='download_on_demand_candidate',
        record_uri='https://tech.ebu.ch/loudness',
        license_id='ebu-test-material',
        license_family='unknown',
        license_note=(
            'Test signals are published for download at tech.ebu.ch '
            'for conformance testing; reuse terms are governed by the '
            'EBU site — payloads are user-downloaded, never bundled.'
        ),
        files=(),
        dataset_notes=(
            'EBU Tech 3341 (2023) Table 1 minimum-requirements set — '
            'sine/silence sequences for M/S/I, plus true-peak cases '
            '15–23 (not for listening). Expected responses are pinned '
            'in TECH3341_CASES.'
        ),
    )
)

EBU_TECH3342_ADMISSION: ExternalAssetAdmission = (
    build_external_asset_admission(
        admission_id='ebu-tech3342-test-signals',
        dataset_name='ebu-tech3342',
        dataset_title='EBU Tech 3342 loudness-range test signals',
        publisher='European Broadcasting Union',
        source_kind='institutional_repository',
        admission_state='download_on_demand_candidate',
        record_uri='https://tech.ebu.ch/loudness',
        license_id='ebu-test-material',
        license_family='unknown',
        license_note=(
            'Same EBU download terms as Tech 3341 signals.'
        ),
        files=(),
        dataset_notes=(
            'EBU Tech 3342 (2021) Table 1 LRA conformance set — 6 '
            'cases; expected LRA values pinned in TECH3342_CASES.'
        ),
    )
)

EBU_REFERENCE_LISTENING_ADMISSION: ExternalAssetAdmission = (
    build_external_asset_admission(
        admission_id='ebu-reference-listening',
        dataset_name='ebu-reference-listening-signal',
        dataset_title='EBU reference listening signal (Tech 3343)',
        publisher='European Broadcasting Union',
        source_kind='institutional_repository',
        admission_state='download_on_demand_candidate',
        record_uri='https://tech.ebu.ch/loudness',
        license_id='ebu-test-material',
        license_family='unknown',
        license_note=(
            'The EBU reference-listening signal accompanies the R 128 '
            'documentation set; same EBU download terms.'
        ),
        files=(),
        dataset_notes=(
            'Reference listening/meter training material published by '
            'the EBU for loudness measurement practice.'
        ),
    )
)

LOUDNESS_ADMISSIONS: tuple[ExternalAssetAdmission, ...] = (
    EBU_TECH3341_ADMISSION,
    EBU_TECH3342_ADMISSION,
    EBU_REFERENCE_LISTENING_ADMISSION,
)


__all__ = [
    'CheckVerdict',
    'EBU_LOUDNESS_AUTHORITY_VERSION',
    'EBU_REFERENCE_LISTENING_ADMISSION',
    'EBU_TECH3341_ADMISSION',
    'EBU_TECH3342_ADMISSION',
    'LOUDNESS_ADMISSIONS',
    'LOUDNESS_CASE_MATRIX',
    'LoudnessCheck',
    'LoudnessExpectation',
    'LoudnessMetric',
    'LoudnessTestCase',
    'TECH3341_CASES',
    'TECH3342_CASES',
    'check_loudness_measurement',
    'loudness_case',
]
