"""Validation coverage & accuracy dashboard backend (#780).

#727 answers the project-local question — what capability can this
project use now; this module answers the corpus/release question: across
accumulated benchmark and owned-room evidence, where is HTDT validated,
where is coverage thin, and did the latest solver/model version regress
anything?

Hard rules:
- never one aggregate accuracy score — coverage is reported per cell
  (provider version x geometry class x observable x band x evidence
  level) with explicit denominators;
- failures are never hidden behind medians — every aggregate cell links
  back to exact per-case verdicts;
- version comparison shows newly passing AND newly failing cases; a new
  version is never labelled globally better;
- corpus absence is a coverage gap, not an implementation claim.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_schema import ensure_native_schema, require_native_tables


CaseVerdict = Literal['pass', 'fail', 'not_applicable']

EvidenceLevel = Literal[
    'analytic_reference',
    'numerical_convergence',
    'independent_solver',
    'external_measurement',
    'owned_room_calibration',
    'owned_room_holdout',
    'locked_benchmark',
    'fresh_physical_validation',
]

GeometryClass = Literal[
    'rectangular',
    'concave',
    'sloped_ceiling',
    'openings_portal',
    'coupled_regions',
    'obstacle_furniture',
]

SourceClass = Literal[
    'monopole_reference',
    'subwoofer',
    'directional_main',
    'single_source',
    'coherent_multi_source',
]

ObservableClass = Literal[
    'magnitude_fr',
    'complex_transfer',
    'phase',
    'arrival_timing',
    'impulse_response',
    'modal_behavior',
    'early_paths',
    'late_decay',
    'spatial_field',
    'candidate_ranking',
]

QualificationState = Literal[
    'production_qualified',
    'candidate',
    'experimental',
    'not_qualified',
]

CoverageStrength = Literal[
    'strong_coverage',
    'limited_coverage',
    'thin_coverage',
    'no_coverage',
]

VersionComparisonOutcome = Literal[
    'newly_passing',
    'newly_failing',
    'unchanged_pass',
    'unchanged_fail',
    'not_rerun',
    'new_case',
    'removed_case',
]

# Coverage-strength thresholds on eligible (non-not_applicable) case count.
_STRONG_AT = 5
_LIMITED_AT = 2


class ValidationCaseRecord(BaseModel):
    """One corpus case's evidence binding (#773)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    case_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    geometry_class: GeometryClass
    source_class: SourceClass
    observable: ObservableClass
    band_low_hz: float = Field(ge=0.0)
    band_high_hz: float = Field(gt=0.0)
    evidence_level: EvidenceLevel
    verdict: CaseVerdict
    qualification: QualificationState = 'candidate'
    is_holdout: bool = False
    detail: str = ''
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_case(self) -> 'ValidationCaseRecord':
        if self.band_high_hz <= self.band_low_hz:
            raise ValueError('band_high_hz must exceed band_low_hz')
        return self


class CoverageCell(BaseModel):
    """Aggregate over one dashboard cell — explicit denominator."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    provider_version: str
    geometry_class: GeometryClass
    source_class: SourceClass
    observable: ObservableClass
    evidence_level: EvidenceLevel
    eligible_cases: int = Field(ge=0)
    passing_cases: int = Field(ge=0)
    failing_cases: int = Field(ge=0)
    holdout_cases: int = Field(ge=0)
    strength: CoverageStrength
    qualification: QualificationState | None = None

    @model_validator(mode='after')
    def valid_cell(self) -> 'CoverageCell':
        if self.passing_cases + self.failing_cases > self.eligible_cases:
            raise ValueError('case counts exceed eligible denominator')
        return self


def _cell_strength(eligible: int, holdout: int) -> CoverageStrength:
    if eligible == 0:
        return 'no_coverage'
    if holdout == 0 and eligible < _LIMITED_AT:
        return 'thin_coverage'
    if eligible >= _STRONG_AT and holdout > 0:
        return 'strong_coverage'
    return 'limited_coverage'


def build_coverage_cells(
    cases: tuple[ValidationCaseRecord, ...],
) -> tuple[CoverageCell, ...]:
    """Aggregate cases into coverage cells; never emits percentages."""
    groups: dict[tuple[Any, ...], list[ValidationCaseRecord]] = {}
    for case in cases:
        key = (
            case.provider_id,
            case.provider_version,
            case.geometry_class,
            case.source_class,
            case.observable,
            case.evidence_level,
        )
        groups.setdefault(key, []).append(case)
    cells: list[CoverageCell] = []
    for key, members in groups.items():
        eligible = [c for c in members if c.verdict != 'not_applicable']
        passing = [c for c in eligible if c.verdict == 'pass']
        failing = [c for c in eligible if c.verdict == 'fail']
        holdouts = [c for c in eligible if c.is_holdout]
        quals = {c.qualification for c in eligible}
        qualification = (
            'production_qualified'
            if quals == {'production_qualified'}
            else ('experimental' if 'experimental' in quals else
                  'candidate' if 'candidate' in quals else
                  'not_qualified' if quals == {'not_qualified'} else None)
        )
        cells.append(
            CoverageCell(
                provider_id=key[0],
                provider_version=key[1],
                geometry_class=key[2],
                source_class=key[3],
                observable=key[4],
                evidence_level=key[5],
                eligible_cases=len(eligible),
                passing_cases=len(passing),
                failing_cases=len(failing),
                holdout_cases=len(holdouts),
                strength=_cell_strength(len(eligible), len(holdouts)),
                qualification=qualification,
            )
        )
    return tuple(
        sorted(
            cells,
            key=lambda c: (
                c.provider_id,
                c.provider_version,
                c.geometry_class,
                c.observable,
            ),
        )
    )


def drilldown_cases(
    cases: tuple[ValidationCaseRecord, ...],
    *,
    provider_id: str | None = None,
    provider_version: str | None = None,
    geometry_class: GeometryClass | None = None,
    source_class: SourceClass | None = None,
    observable: ObservableClass | None = None,
    evidence_level: EvidenceLevel | None = None,
) -> tuple[ValidationCaseRecord, ...]:
    """Per-case drill-down behind any aggregate cell (#780 §4)."""
    result = []
    for case in cases:
        if provider_id is not None and case.provider_id != provider_id:
            continue
        if (
            provider_version is not None
            and case.provider_version != provider_version
        ):
            continue
        if geometry_class is not None and case.geometry_class != geometry_class:
            continue
        if source_class is not None and case.source_class != source_class:
            continue
        if observable is not None and case.observable != observable:
            continue
        if evidence_level is not None and case.evidence_level != evidence_level:
            continue
        result.append(case)
    return tuple(
        sorted(result, key=lambda c: (c.case_id, c.provider_version))
    )


def find_coverage_gaps(
    cases: tuple[ValidationCaseRecord, ...],
) -> tuple[str, ...]:
    """Explicit missing-evidence statements (#780 §5)."""
    gaps: list[str] = []
    cells = build_coverage_cells(cases)

    holdout_observables = {
        c.observable for c in cases if c.is_holdout and c.verdict != 'not_applicable'
    }
    for observable in (
        'complex_transfer', 'magnitude_fr', 'arrival_timing'
    ):
        if observable not in holdout_observables:
            gaps.append(f'no owned-room holdout cases for {observable}')

    geometry_with_holdout = {
        c.geometry_class
        for c in cases
        if c.is_holdout and c.verdict != 'not_applicable'
    }
    for geometry in ('concave', 'sloped_ceiling', 'openings_portal'):
        if geometry not in geometry_with_holdout:
            gaps.append(f'no {geometry} holdout evidence')

    if not any(
        c.source_class == 'directional_main' and c.is_holdout
        for c in cases
    ):
        gaps.append('no directional-speaker owned-room cases')

    above_100 = [
        c for c in cases
        if c.band_high_hz > 100.0 and c.evidence_level != 'analytic_reference'
    ]
    if not above_100:
        gaps.append('no evidence above 100 Hz beyond analytic reference')

    hardwareish = {
        c.provider_id for c in cases
    }
    if len(hardwareish) < 2 and cells:
        gaps.append('evidence exists for only one provider/backend')

    for cell in cells:
        if cell.strength in ('thin_coverage', 'no_coverage'):
            gaps.append(
                f'{cell.provider_id} {cell.provider_version}: '
                f'{cell.observable} / {cell.geometry_class} — '
                f'{cell.strength.replace("_", " ")}'
            )
    return tuple(sorted(set(gaps)))


class VersionCaseDelta(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    outcome: VersionComparisonOutcome
    old_verdict: CaseVerdict | None = None
    new_verdict: CaseVerdict | None = None


def compare_provider_versions(
    cases: tuple[ValidationCaseRecord, ...],
    *,
    provider_id: str,
    old_version: str,
    new_version: str,
) -> tuple[VersionCaseDelta, ...]:
    """Symmetric §3 diff: newly passing AND newly failing plus cases not
    rerun — never a global "better" label."""
    old = {
        c.case_id: c
        for c in cases
        if c.provider_id == provider_id and c.provider_version == old_version
    }
    new = {
        c.case_id: c
        for c in cases
        if c.provider_id == provider_id and c.provider_version == new_version
    }
    deltas: list[VersionCaseDelta] = []
    for case_id in sorted(set(old) | set(new)):
        old_case = old.get(case_id)
        new_case = new.get(case_id)
        if old_case is None:
            outcome: VersionComparisonOutcome = 'new_case'
        elif new_case is None:
            outcome = 'removed_case'
        elif (
            old_case.verdict != 'not_applicable'
            and new_case.verdict == 'not_applicable'
        ):
            outcome = 'not_rerun'
        elif old_case.verdict == 'pass' and new_case.verdict == 'pass':
            outcome = 'unchanged_pass'
        elif old_case.verdict == 'fail' and new_case.verdict == 'fail':
            outcome = 'unchanged_fail'
        elif old_case.verdict != 'pass' and new_case.verdict == 'pass':
            outcome = 'newly_passing'
        elif old_case.verdict == 'pass' and new_case.verdict != 'pass':
            outcome = 'newly_failing'
        else:
            outcome = 'not_rerun'
        deltas.append(
            VersionCaseDelta(
                case_id=case_id,
                outcome=outcome,
                old_verdict=old_case.verdict if old_case else None,
                new_verdict=new_case.verdict if new_case else None,
            )
        )
    return tuple(deltas)


class ValidationEvidenceRepository:
    """Append-only validation evidence store on the shared cad DB."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_validation_cases',
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def save_case(
        self, evidence_id: str, case: ValidationCaseRecord
    ) -> ValidationCaseRecord:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_validation_cases('
                'evidence_id, case_id, provider_id, provider_version, '
                'geometry_class, source_class, observable, evidence_level, '
                'verdict, is_holdout, recorded_at_utc, payload_json) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(evidence_id) DO NOTHING',
                (
                    evidence_id,
                    case.case_id,
                    case.provider_id,
                    case.provider_version,
                    case.geometry_class,
                    case.source_class,
                    case.observable,
                    case.evidence_level,
                    case.verdict,
                    1 if case.is_holdout else 0,
                    case.recorded_at_utc,
                    case.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_validation_cases '
                'WHERE evidence_id=?',
                (evidence_id,),
            ).fetchone()
            if row['payload_json'] != case.model_dump_json():
                raise ValueError(
                    f'validation evidence {evidence_id} already persisted '
                    'with different content'
                )
        return case

    def list_cases(
        self, *, provider_id: str | None = None
    ) -> tuple[ValidationCaseRecord, ...]:
        with closing(self._connect()) as connection:
            if provider_id is None:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_validation_cases '
                    'ORDER BY case_id ASC, provider_version ASC',
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_validation_cases '
                    'WHERE provider_id=? '
                    'ORDER BY case_id ASC, provider_version ASC',
                    (provider_id,),
                ).fetchall()
        return tuple(
            ValidationCaseRecord.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CaseVerdict',
    'CoverageCell',
    'CoverageStrength',
    'EvidenceLevel',
    'GeometryClass',
    'ObservableClass',
    'QualificationState',
    'SourceClass',
    'ValidationCaseRecord',
    'ValidationEvidenceRepository',
    'VersionCaseDelta',
    'VersionComparisonOutcome',
    'build_coverage_cells',
    'compare_provider_versions',
    'drilldown_cases',
    'find_coverage_gaps',
]
