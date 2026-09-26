"""CTA WAVE device-playback qualification import (#1080).

Imports published CTA WAVE (Web Application Video Ecosystem) test-suite
results — e.g. the Device Playback Capabilities (DPC) suite run by the
official open test harness — into the #1045
``MediaPlaybackSourceCondition`` authority.

Rules:

- the report is an *identity-pinned observation*: device + app/browser
  + suite version all bind the result; a report without those fields is
  ``incomplete``, never imported;
- a WAVE PASS of a capability test maps to ``observed_working`` only
  for the exact tested configuration — the importer never generalizes
  a pass across firmware/codec variants;
- imported rows keep their per-test granularity — never a single
  "device supports WAVE" aggregate;
- suite-report payloads are stored as verbatim JSON + sha256; parsing
  is an independent implementation (no WAVE code vendored).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_media_source_capability import (
    SourceCapabilityObservation,
    SourceCapabilityState,
)


WAVE_QUALIFICATION_AUTHORITY_VERSION = 'wave-qualification-1'


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


WaveSuite = Literal[
    'dpctf',  # Device Playback Capabilities Test Framework
    'ctf',  # CTA WAVE content test framework
    'dpc_cap',  # device playback capability database reports
    'other',
]

WaveTestVerdict = Literal['pass', 'fail', 'not_executed', 'warning']


class WaveTestRow(BaseModel):
    """One test inside a WAVE suite report."""

    model_config = ConfigDict(frozen=True)

    test_id: str = Field(min_length=1)
    verdict: WaveTestVerdict
    detail: str | None = None


class WaveSuiteReport(BaseModel):
    """One imported WAVE suite report — identity-pinned."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    suite: WaveSuite
    suite_version: str = Field(min_length=1)
    device_identity: str = Field(min_length=1)
    """Manufacturer/model or browser identity string, verbatim."""
    app_identity: str | None = None
    app_version: str | None = None
    firmware_version: str | None = None
    os_platform_version: str | None = None
    executed_at_utc: str | None = None
    tests: tuple[WaveTestRow, ...] = Field(min_length=1)
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_uri: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _check(self) -> 'WaveSuiteReport':
        ids = [t.test_id for t in self.tests]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate test ids in WAVE report')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('WAVE report semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )


def build_wave_report(
    *,
    report_id: str,
    suite: WaveSuite,
    suite_version: str,
    device_identity: str,
    tests: tuple[WaveTestRow, ...],
    report_sha256: str,
    source_uri: str,
    app_identity: str | None = None,
    app_version: str | None = None,
    firmware_version: str | None = None,
    os_platform_version: str | None = None,
    executed_at_utc: str | None = None,
) -> WaveSuiteReport:
    probe = WaveSuiteReport.model_construct(
        report_id=report_id,
        suite=suite,
        suite_version=suite_version,
        device_identity=device_identity,
        app_identity=app_identity,
        app_version=app_version,
        firmware_version=firmware_version,
        os_platform_version=os_platform_version,
        executed_at_utc=executed_at_utc,
        tests=tuple(tests),
        report_sha256=report_sha256,
        source_uri=source_uri,
        semantic_sha256='',
    )
    return WaveSuiteReport(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# --- report JSON import --------------------------------------------------

_WAVE_VERDICT_MAP: dict[str, WaveTestVerdict] = {
    'pass': 'pass',
    'passed': 'pass',
    'fail': 'fail',
    'failed': 'fail',
    'warning': 'warning',
    'not_executed': 'not_executed',
    'skipped': 'not_executed',
}


class WaveImport(NamedTuple):
    verdict: Literal['imported', 'incomplete', 'invalid']
    report: WaveSuiteReport | None
    detail: str


def import_wave_report_json(
    payload: str,
    *,
    report_id: str,
    source_uri: str,
) -> WaveImport:
    """Import a WAVE test-runner JSON report.

    Accepts the documented result shape ``{suite, version, device,
    results: [{test, status}...]}``. Fail-closed: missing device or
    suite identity, or an unknown verdict string, rejects the import.
    """
    try:
        doc = json.loads(payload)
    except json.JSONDecodeError as exc:
        return WaveImport('invalid', None, f'JSON parse error: {exc}')
    if not isinstance(doc, dict):
        return WaveImport('invalid', None, 'report root must be an object')
    suite_raw = doc.get('suite')
    version = doc.get('version') or doc.get('suite_version')
    device = doc.get('device') or doc.get('device_identity')
    results = doc.get('results') or doc.get('tests')
    if not all([suite_raw, version, device, isinstance(results, list)]):
        return WaveImport(
            'incomplete',
            None,
            'report lacks suite/version/device/results identity',
        )
    suite_key = str(suite_raw).lower().replace('-', '_')
    suite: WaveSuite = (
        suite_key if suite_key in WaveSuite.__args__ else 'other'  # type: ignore[assignment]
    )
    rows: list[WaveTestRow] = []
    for item in results:
        if not isinstance(item, dict):
            return WaveImport(
                'invalid', None, 'test row must be an object'
            )
        tid = item.get('test') or item.get('test_id')
        status = str(
            item.get('status') or item.get('verdict') or ''
        ).lower()
        mapped = _WAVE_VERDICT_MAP.get(status)
        if not tid or mapped is None:
            return WaveImport(
                'invalid',
                None,
                f'unrecognized test row {item!r}',
            )
        rows.append(
            WaveTestRow(
                test_id=str(tid),
                verdict=mapped,
                detail=item.get('detail'),
            )
        )
    if not rows:
        return WaveImport(
            'incomplete', None, 'report contains no test rows'
        )
    report = build_wave_report(
        report_id=report_id,
        suite=suite,
        suite_version=str(version),
        device_identity=str(device),
        app_identity=doc.get('app'),
        app_version=doc.get('app_version'),
        firmware_version=doc.get('firmware'),
        os_platform_version=doc.get('os'),
        executed_at_utc=doc.get('executed_at'),
        tests=tuple(rows),
        report_sha256=_sha256_text(payload),
        source_uri=source_uri,
    )
    return WaveImport('imported', report, f'{len(rows)} test rows')


# --- mapping into #1045 capability observations ---------------------------

_VERDICT_TO_CAPABILITY: dict[WaveTestVerdict, SourceCapabilityState] = {
    'pass': 'observed_working',
    'fail': 'observed_failure',
    'not_executed': 'unknown',
    'warning': 'observed_working',
}


def wave_capability_observations(
    report: WaveSuiteReport,
) -> tuple[SourceCapabilityObservation, ...]:
    """Per-test capability observations for a #1045 condition.

    Each test becomes one aspect (``wave:<suite>:<test_id>``); a WAVE
    warning maps to ``observed_working`` with the detail kept — never
    silently upgraded.
    """
    observations: list[SourceCapabilityObservation] = []
    for row in report.tests:
        observations.append(
            SourceCapabilityObservation(
                aspect=f'wave:{report.suite}:{row.test_id}',
                state=_VERDICT_TO_CAPABILITY[row.verdict],
                detail=(
                    row.detail
                    if row.verdict in ('fail', 'warning')
                    else None
                ),
                observed_at_utc=report.executed_at_utc,
            )
        )
    return tuple(observations)


def wave_report_provenance(report: WaveSuiteReport) -> (
    EquipmentDataProvenance
):
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name=f'cta-wave:{report.suite}',
        source_version=report.suite_version,
        source_reference=report.report_id,
        source_sha256=report.report_sha256,
    )


__all__ = [
    'WAVE_QUALIFICATION_AUTHORITY_VERSION',
    'WaveImport',
    'WaveSuite',
    'WaveSuiteReport',
    'WaveTestRow',
    'WaveTestVerdict',
    'build_wave_report',
    'import_wave_report_json',
    'wave_capability_observations',
    'wave_report_provenance',
]
