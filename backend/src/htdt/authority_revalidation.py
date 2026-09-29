"""Post-update authority revalidation (round 14).

When a new HTDT build re-keys persisted authority identities — an
algorithm-version bump, a changed sealing convention, a dropped replay
registration — records that were canonical under the previous build
surface as ``stale_authority`` / ``noncanonical_derivation`` diagnostics
and every read path that replays them refuses. Before this module there
was no way forward except discarding the records: backups refused to
export the data and, when a schema migration was pending, the upgrade
lifecycle refused to run at all.

``revalidate_native_authority_graph`` gives the user a first-class
再検証 lane. It walks the same diagnostics the authority audit reports
and, for authority families with an honest re-derivation lane, re-derives
the record under the current build:

- ``routing_profile`` / ``wiring_check`` — rebuild the record from its
  stored semantic fields so a new seal convention re-signs the same
  semantics; wiring checks re-bind to profiles whose seal moved in the
  same pass.
- ``measurement_comparison`` — rerun the replayable algorithm over the
  persisted datasets and promote only when the freshly computed output
  equals the stored result (``algorithm_version`` aside). An
  unregistered version falls back to the current algorithm: promotion
  then asserts "the stored numbers are exactly what the current
  algorithm produces for these inputs" — still a true, re-verified
  statement.

Nothing is silently promoted: a rebuild whose semantic fields drift from
the stored payload, whose output differs from the stored result, or whose
post-write canonical read still fails is left stale with an explicit
reason. After the write pass a fresh audit re-runs over the live file so
the report states what was actually proven, not what was attempted.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from . import comparison as _comparison
from .cad_authority_resolver import AuthorityRef
from .cad_measurement_authorities import (
    CadElectricalLoadObservation,
    CadRoutingProfileBinding,
    build_routing_profile,
    build_wiring_check,
)
from .cad_measurement_models import (
    build_measurement_comparison,
)
from .cad_schema import connect_sqlite
from .comparison import (
    ComparisonResult,
    FrequencyResponse,
)
from .native_authority_audit import (
    AuthorityAuditDiagnostic,
    AuthorityAuditReport,
    audit_native_authority_graph,
)

_LOGGER = logging.getLogger('htdt.revalidation')


RevalidationAction = Literal['revalidated', 'kept_stale']


@dataclass(frozen=True)
class RevalidationOutcome:
    """What the revalidation lane did about one failing authority row."""

    authority: str
    record_ref: str
    action: RevalidationAction
    detail: str


@dataclass(frozen=True)
class RevalidationReport:
    """Result of one revalidation pass over a managed database."""

    database_path: Path
    outcomes: tuple[RevalidationOutcome, ...]
    #: A fresh audit taken AFTER the write pass — the honest final state.
    audit: AuthorityAuditReport

    @property
    def revalidated(self) -> tuple[RevalidationOutcome, ...]:
        return tuple(o for o in self.outcomes if o.action == 'revalidated')

    @property
    def kept_stale(self) -> tuple[RevalidationOutcome, ...]:
        return tuple(o for o in self.outcomes if o.action == 'kept_stale')

    @property
    def resolved(self) -> bool:
        """True when the post-pass audit is clean."""
        return self.audit.ok

    def summary_ja(self) -> str:
        """User-facing Japanese summary of the revalidation pass."""
        if not self.outcomes:
            return '再検証が必要な記録はありませんでした。'
        recovered = len(self.revalidated)
        remaining = len(self.kept_stale)
        if remaining == 0 and self.audit.ok:
            return (
                f'{recovered} 件の記録を再検証し、すべて現在の形式で'
                '再署名されました。データは完全に検証済みです。'
            )
        lines = [
            f'{recovered} 件の記録を再検証しました。',
            f'{remaining} 件は引き続き未検証のままです'
            '（保存済みの内容が現在のビルドで再導出できませんでした）。',
        ]
        lines.append('理由:')
        for outcome in self.kept_stale[:10]:
            lines.append(
                f'・{outcome.authority} ({outcome.record_ref[:8]}…): '
                f'{outcome.detail}'
            )
        if len(self.kept_stale) > 10:
            lines.append(f'・ほか {len(self.kept_stale) - 10} 件')
        lines.append(
            '未検証の記録を含むバックアップは「検証を通過しない記録を'
            '含めてバックアップする」を有効にするか '
            '--backup-allow-stale で作成できます。'
        )
        return '\n'.join(lines)


class _RevalidationContext:
    """Repositories plus the write connection for one revalidation pass."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self._scene_repository = None
        self._measurement_repository = None
        self._quality_repository = None
        # routing_profile_id -> re-sealed sha256 for wiring-check rebinds.
        self.profile_sha_remap: dict[str, str] = {}

    @property
    def measurement(self):
        if self._measurement_repository is None:
            from .cad_measurement_repository import CadMeasurementRepository

            self._measurement_repository = CadMeasurementRepository(
                self.scene
            )
        return self._measurement_repository

    @property
    def quality(self):
        if self._quality_repository is None:
            from .cad_measurement_quality_repository import (
                CadMeasurementQualityRepository,
            )

            self._quality_repository = CadMeasurementQualityRepository(
                self.measurement
            )
        return self._quality_repository

    @property
    def scene(self):
        if self._scene_repository is None:
            from .cad_repository import SceneRepository

            self._scene_repository = SceneRepository(self.database_path)
        return self._scene_repository

    def connect(self):
        return connect_sqlite(self.database_path)


def _kept(
    diagnostic: AuthorityAuditDiagnostic, detail: str
) -> RevalidationOutcome:
    return RevalidationOutcome(
        authority=diagnostic.authority,
        record_ref=diagnostic.record_ref,
        action='kept_stale',
        detail=detail,
    )


def _semantic_drift(
    stored: Mapping[str, Any],
    rebuilt: Mapping[str, Any],
    *,
    ignore: frozenset[str],
) -> str | None:
    """Fields a rebuild changed besides seals/bindings, or None.

    The honest re-derivation contract: every field the stored payload
    carries must survive the rebuild bit-identically, except the seal the
    re-key deliberately recomputes. A dropped or silently re-shaped field
    means promotion would lose semantics — the record stays stale instead.
    """
    drifted: list[str] = []
    for key, value in stored.items():
        if key in ignore:
            continue
        if key not in rebuilt or rebuilt[key] != value:
            drifted.append(key)
    if drifted:
        return ', '.join(sorted(drifted))
    return None


def _revalidate_routing_profile(
    ctx: _RevalidationContext,
    diagnostic: AuthorityAuditDiagnostic,
) -> RevalidationOutcome:
    """Re-seal a routing profile under the current identity convention.

    Re-keyed seal conventions leave stored payloads self-inconsistent
    (``routing_profile_sha256`` no longer matches the current
    ``identity_payload``), so the stored row cannot even be model-parsed.
    Rebuilding via ``build_routing_profile`` re-derives the seal over the
    same stored semantics; the drift gate then proves no stored field was
    lost or altered.
    """

    record_ref = diagnostic.record_ref
    with ctx.connect() as connection:
        row = connection.execute(
            'SELECT routing_profile_sha256, payload_json '
            'FROM cad_routing_profiles WHERE routing_profile_id=?',
            (record_ref,),
        ).fetchone()
    if row is None:
        return _kept(diagnostic, '記録が見つかりません')
    try:
        stored = json.loads(row['payload_json'])
    except (TypeError, ValueError) as exc:
        return _kept(diagnostic, f'保存済みペイロードを読めません: {exc}')
    try:
        profile = build_routing_profile(
            routing_profile_id=record_ref,
            profile_name=stored['profile_name'],
            document_id=stored.get('document_id'),
            scene_revision_id=stored.get('scene_revision_id'),
            entries=stored.get('entries', ()),
            created_at_utc=stored['created_at_utc'],
            provenance_json=stored.get('provenance_json', '{}'),
        )
    except Exception as exc:
        return _kept(
            diagnostic,
            f'このビルドではプロファイルを再署名できません: {exc}',
        )
    drift = _semantic_drift(
        stored,
        profile.model_dump(mode='json'),
        ignore=frozenset({'routing_profile_sha256'}),
    )
    if drift is not None:
        return _kept(
            diagnostic,
            f'再署名で意味内容が変わります ({drift}) — '
            '安全のため未検証のまま保持します',
        )
    old_row = (row['routing_profile_sha256'], row['payload_json'])
    with ctx.connect() as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'UPDATE cad_routing_profiles SET routing_profile_sha256=?, '
            'payload_json=? WHERE routing_profile_id=?',
            (
                profile.routing_profile_sha256,
                profile.model_dump_json(),
                record_ref,
            ),
        )
    try:
        ctx.quality.get_routing_profile(record_ref)
    except Exception as exc:
        # The re-sealed row still fails the canonical read (e.g. its
        # pinned scene revision can no longer be replayed): roll the
        # stored bytes back so the row stays exactly what it was — stale,
        # but untouched.
        with ctx.connect() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                'UPDATE cad_routing_profiles SET routing_profile_sha256=?, '
                'payload_json=? WHERE routing_profile_id=?',
                (*old_row, record_ref),
            )
        return _kept(
            diagnostic,
            f'再署名後も正規の検証に失敗します: {exc}',
        )
    ctx.profile_sha_remap[record_ref] = profile.routing_profile_sha256
    return RevalidationOutcome(
        authority=diagnostic.authority,
        record_ref=record_ref,
        action='revalidated',
        detail=(
            'プロファイルを現在のシール規約で再署名しました '
            f'(sha …{profile.routing_profile_sha256[-12:]})'
        ),
    )


_WIRING_CHECK_REF_FIELDS = ('routing_profile', 'routing_profile_ref')


def _wiring_check_kwargs(
    stored: Mapping[str, Any],
    profile_sha_remap: Mapping[str, str],
) -> dict[str, Any]:
    """Map a stored wiring-check payload onto ``build_wiring_check`` kwargs.

    Typed sub-payloads are re-validated into their models; routing-profile
    bindings whose profile was re-sealed in this same pass are re-pinned
    to the new seal so the rebuilt check resolves.
    """

    def _binding(value: Any) -> CadRoutingProfileBinding | None:
        if value is None:
            return None
        binding = dict(value)
        moved = profile_sha_remap.get(binding.get('routing_profile_id'))
        if moved is not None:
            binding['routing_profile_sha256'] = moved
        return CadRoutingProfileBinding.model_validate(binding)

    def _evidence(value: Any) -> AuthorityRef | str:
        if isinstance(value, Mapping):
            return AuthorityRef.model_validate(value)
        return value

    kwargs: dict[str, Any] = {
        'check_id': stored['check_id'],
        'document_id': stored['document_id'],
        'check_kind': stored['check_kind'],
        'method': stored['method'],
        'result': stored['result'],
        'measured_at_utc': stored['measured_at_utc'],
        'scene_revision_id': stored['scene_revision_id'],
        'scene_revision_sha256': stored['scene_revision_sha256'],
        'system_variant_id': stored.get('system_variant_id'),
        'system_variant_sha256': stored.get('system_variant_sha256'),
        'routing_profile': _binding(stored.get('routing_profile')),
        'expected_output_reference': stored.get('expected_output_reference'),
        'expected_speaker_ids': tuple(stored.get('expected_speaker_ids') or ()),
        'source_speaker_ids': tuple(stored.get('source_speaker_ids') or ()),
        'observed_output_reference': stored.get('observed_output_reference'),
        'observed_speaker_ids': tuple(stored.get('observed_speaker_ids') or ()),
        'applied_compensation': tuple(
            stored.get('applied_compensation') or ()
        ),
        'evidence_refs': tuple(
            _evidence(ref) for ref in (stored.get('evidence_refs') or ())
        ),
        'routing_profile_ref': _binding(stored.get('routing_profile_ref')),
        'load_observation': (
            CadElectricalLoadObservation.model_validate(
                stored['load_observation']
            )
            if stored.get('load_observation') is not None
            else None
        ),
        'operator': stored.get('operator'),
        'reason': stored.get('reason'),
        'notes': tuple(stored.get('notes') or ()),
        'provenance_json': stored.get('provenance_json', '{}'),
    }
    return kwargs


def _revalidate_wiring_check(
    ctx: _RevalidationContext,
    diagnostic: AuthorityAuditDiagnostic,
) -> RevalidationOutcome:
    """Re-seal a wiring check, re-binding profiles re-sealed this pass."""

    record_ref = diagnostic.record_ref
    with ctx.connect() as connection:
        row = connection.execute(
            'SELECT check_sha256, payload_json FROM cad_wiring_checks '
            'WHERE check_id=?',
            (record_ref,),
        ).fetchone()
    if row is None:
        return _kept(diagnostic, '記録が見つかりません')
    try:
        stored = json.loads(row['payload_json'])
    except (TypeError, ValueError) as exc:
        return _kept(diagnostic, f'保存済みペイロードを読めません: {exc}')
    try:
        check = build_wiring_check(
            **_wiring_check_kwargs(stored, ctx.profile_sha_remap)
        )
    except Exception as exc:
        return _kept(
            diagnostic,
            f'このビルドではチェックを再署名できません: {exc}',
        )
    rebuilt = check.model_dump(mode='json')
    drift = _semantic_drift(
        stored,
        rebuilt,
        ignore=frozenset({'check_sha256'}),
    )
    # The one sanctioned drift: a binding sha that legitimately moved with
    # a re-sealed profile in this same pass. Anything else loses semantics.
    if drift is not None:
        allowed = {
            key
            for key in _WIRING_CHECK_REF_FIELDS
            if isinstance(stored.get(key), Mapping)
            and stored[key].get('routing_profile_id') in ctx.profile_sha_remap
            and rebuilt[key] is not None
            and rebuilt[key].get('routing_profile_sha256')
            == ctx.profile_sha_remap[stored[key]['routing_profile_id']]
            and {
                k: v for k, v in stored[key].items() if k != 'routing_profile_sha256'
            }
            == {
                k: v for k, v in rebuilt[key].items() if k != 'routing_profile_sha256'
            }
        }
        residual = [key for key in drift.split(', ') if key not in allowed]
        if residual:
            return _kept(
                diagnostic,
                f'再署名で意味内容が変わります ({", ".join(residual)}) — '
                '安全のため未検証のまま保持します',
            )
    old_row = (row['check_sha256'], row['payload_json'])
    with ctx.connect() as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'UPDATE cad_wiring_checks SET check_sha256=?, payload_json=? '
            'WHERE check_id=?',
            (check.check_sha256, check.model_dump_json(), record_ref),
        )
    try:
        ctx.quality.get_wiring_check(record_ref)
    except Exception as exc:
        with ctx.connect() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                'UPDATE cad_wiring_checks SET check_sha256=?, payload_json=? '
                'WHERE check_id=?',
                (*old_row, record_ref),
            )
        return _kept(
            diagnostic,
            f'再署名後も正規の検証に失敗します: {exc}',
        )
    return RevalidationOutcome(
        authority=diagnostic.authority,
        record_ref=record_ref,
        action='revalidated',
        detail='チェックを現在のシール規約で再署名しました',
    )


def _comparison_result_equivalent(
    stored_payload: Mapping[str, Any],
    computed: ComparisonResult,
) -> bool:
    """Stored result vs. freshly computed output, minus algorithm_version.

    JSON round-trip normalizes tuples/lists on both sides; the version
    field is the sanctioned difference — promotion asserts the stored
    numbers ARE what the replayable algorithm produces for the same
    inputs, re-verified just now.
    """
    stored = {
        field.name: stored_payload.get(field.name)
        for field in fields(ComparisonResult)
        if field.name != 'algorithm_version'
    }
    fresh = {
        field.name: asdict(computed)[field.name]
        for field in fields(ComparisonResult)
        if field.name != 'algorithm_version'
    }
    normalize = json.loads(
        json.dumps(stored, ensure_ascii=False, sort_keys=True)
    ) == json.loads(
        json.dumps(fresh, ensure_ascii=False, sort_keys=True)
    )
    return normalize


def _revalidate_comparison(
    ctx: _RevalidationContext,
    diagnostic: AuthorityAuditDiagnostic,
) -> RevalidationOutcome:
    """Re-derive a persisted comparison under a replayable algorithm.

    Two honest outcomes:

    - the stored ``algorithm_version`` is still registered — the replay
      builder re-runs the pinned algorithm; promotion requires the output
      to equal the stored result exactly;
    - the stored version was dropped — the current registered algorithm
      re-runs over the same persisted datasets; promotion requires every
      output field (except the version stamp itself) to equal the stored
      result. Anything else stays stale.
    """

    record_ref = diagnostic.record_ref
    with ctx.connect() as connection:
        row = connection.execute(
            'SELECT * FROM cad_measurement_comparisons '
            'WHERE comparison_id=?',
            (record_ref,),
        ).fetchone()
    if row is None:
        return _kept(diagnostic, '記録が見つかりません')
    try:
        stored_payload = json.loads(row['result_json'])
        stored_result = ComparisonResult(
            requested_band_hz=tuple(stored_payload['requested_band_hz']),
            actual_band_hz=tuple(stored_payload['actual_band_hz']),
            grid_hz=tuple(stored_payload['grid_hz']),
            a_db=tuple(stored_payload['a_db']),
            b_db=tuple(stored_payload['b_db']),
            difference_db=tuple(stored_payload['difference_db']),
            mean_difference_db=stored_payload['mean_difference_db'],
            rms_difference_db=stored_payload['rms_difference_db'],
            level_offset_db=stored_payload['level_offset_db'],
            shape_rms_db=stored_payload['shape_rms_db'],
            valid_points=stored_payload['valid_points'],
            total_grid_points=stored_payload['total_grid_points'],
            algorithm_version=stored_payload['algorithm_version'],
            reference_band_hz=(
                tuple(stored_payload['reference_band_hz'])
                if stored_payload.get('reference_band_hz') is not None
                else None
            ),
            excluded_bands=tuple(
                tuple(band)
                for band in (stored_payload.get('excluded_bands') or ())
            ),
        )
    except Exception as exc:
        return _kept(
            diagnostic,
            f'保存済みの結果ペイロードを解釈できません: {exc}',
        )
    try:
        dataset_a = ctx.measurement.get_dataset(row['dataset_a_id'])
        dataset_b = ctx.measurement.get_dataset(row['dataset_b_id'])
    except Exception as exc:
        return _kept(
            diagnostic,
            f'比較対象のデータセットが検証できません: {exc}',
        )
    if dataset_a is None or dataset_b is None:
        return _kept(diagnostic, '比較対象のデータセットが存在しません')
    if (
        stored_payload.get('dataset_a_sha256')
        != dataset_a.dataset_sha256
        or stored_payload.get('dataset_b_sha256')
        != dataset_b.dataset_sha256
    ):
        return _kept(
            diagnostic,
            '比較対象のデータセットが記録された内容と一致しません',
        )
    version = stored_result.algorithm_version
    if version not in _comparison._COMPARISON_RESULT_REPLAY:
        version = _comparison.ALGORITHM_VERSION
    builder = _comparison._COMPARISON_RESULT_REPLAY[version][1]
    try:
        computed = builder(
            FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
            FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
            stored_result.requested_band_hz[0],
            stored_result.requested_band_hz[1],
            reference_band_hz=stored_result.reference_band_hz,
            excluded_bands=stored_result.excluded_bands,
        )
    except Exception as exc:
        return _kept(
            diagnostic,
            f'アルゴリズムの再実行に失敗しました: {exc}',
        )
    if not _comparison_result_equivalent(stored_payload, computed):
        return _kept(
            diagnostic,
            '保存済みの結果がこのビルドのアルゴリズム出力と一致しません'
            '（再導出できないため未検証のまま保持）',
        )
    try:
        comparison = build_measurement_comparison(
            comparison_id=record_ref,
            document_id=row['document_id'],
            dataset_a_id=row['dataset_a_id'],
            dataset_b_id=row['dataset_b_id'],
            dataset_a_sha256=dataset_a.dataset_sha256,
            dataset_b_sha256=dataset_b.dataset_sha256,
            scene_revision_a_id=row['scene_revision_a_id'],
            scene_revision_b_id=row['scene_revision_b_id'],
            created_at=row['created_at'],
            result=computed,
            semantics_json=stored_payload.get('semantics_json'),
            label_a=stored_payload.get('label_a'),
            label_b=stored_payload.get('label_b'),
            level_compatibility=stored_payload.get('level_compatibility'),
        )
    except Exception as exc:
        return _kept(
            diagnostic,
            f'比較レコードを再署名できません: {exc}',
        )
    result_payload = {
        **asdict(comparison.comparison_result()),
        'dataset_a_sha256': comparison.dataset_a_sha256,
        'dataset_b_sha256': comparison.dataset_b_sha256,
        'algorithm_sha256': comparison.algorithm_sha256,
        'spec_sha256': comparison.spec_sha256,
        'comparison_sha256': comparison.comparison_sha256,
        'semantics_json': comparison.semantics_json,
        'label_a': comparison.label_a,
        'label_b': comparison.label_b,
        'level_compatibility': comparison.level_compatibility,
    }
    old_payload = row['result_json']
    with ctx.connect() as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'UPDATE cad_measurement_comparisons SET result_json=? '
            'WHERE comparison_id=?',
            (
                json.dumps(
                    result_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ),
                record_ref,
            ),
        )
    try:
        ctx.measurement.get_comparison(record_ref)
    except Exception as exc:
        with ctx.connect() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                'UPDATE cad_measurement_comparisons SET result_json=? '
                'WHERE comparison_id=?',
                (old_payload, record_ref),
            )
        return _kept(
            diagnostic,
            f'再導出後も正規の検証に失敗します: {exc}',
        )
    return RevalidationOutcome(
        authority=diagnostic.authority,
        record_ref=record_ref,
        action='revalidated',
        detail=(
            f'アルゴリズム {computed.algorithm_version} の再実行結果が'
            '保存済みの値と一致したため、現在の形式で再署名しました'
        ),
    )


#: Re-derivers run in dependency order — profiles must be re-sealed
#: before checks that bind them can re-pin the moved sha.
_REVALIDATORS: dict[
    str,
    Callable[
        [_RevalidationContext, AuthorityAuditDiagnostic],
        RevalidationOutcome,
    ],
] = {
    'routing_profile': _revalidate_routing_profile,
    'wiring_check': _revalidate_wiring_check,
    'measurement_comparison': _revalidate_comparison,
}

_REVALIDATOR_ORDER = {
    'routing_profile': 0,
    'wiring_check': 1,
    'measurement_comparison': 2,
}


def revalidate_native_authority_graph(
    database_path: Path,
) -> RevalidationReport:
    """Re-derive what honestly re-derives; leave the rest flagged stale.

    Runs the semantic authority audit, dispatches each diagnostic to the
    authority family's re-deriver, then re-audits the live file so the
    report reflects what was actually proven. Records with no honest
    re-derivation lane — or whose re-derivation fails an honesty gate —
    stay stale with an explicit reason; nothing is silently promoted.
    """

    database_path = Path(database_path)
    initial = audit_native_authority_graph(database_path)
    if initial.ok:
        return RevalidationReport(
            database_path=database_path,
            outcomes=(),
            audit=initial,
        )

    ctx = _RevalidationContext(database_path)
    # One diagnostic per (authority, record) — probes may emit duplicates.
    unique: dict[tuple[str, str], AuthorityAuditDiagnostic] = {}
    for diagnostic in initial.diagnostics:
        unique.setdefault(
            (diagnostic.authority, diagnostic.record_ref), diagnostic
        )
    diagnostics = sorted(
        unique.values(),
        key=lambda d: _REVALIDATOR_ORDER.get(d.authority, 100),
    )
    outcomes: list[RevalidationOutcome] = []
    for diagnostic in diagnostics:
        revalidator = _REVALIDATORS.get(diagnostic.authority)
        if revalidator is None:
            outcomes.append(
                _kept(
                    diagnostic,
                    'この機関には再導出経路がありません: '
                    f'{diagnostic.message[:120]}',
                )
            )
            continue
        try:
            outcomes.append(revalidator(ctx, diagnostic))
        except Exception as exc:  # noqa: BLE001 - a failing lane keeps data stale, never drops it
            _LOGGER.warning(
                'revalidation lane failed for %s:%s',
                diagnostic.authority,
                diagnostic.record_ref,
                exc_info=True,
            )
            outcomes.append(_kept(diagnostic, f'再検証に失敗しました: {exc}'))

    post = audit_native_authority_graph(database_path)
    still_failing = {
        (d.authority, d.record_ref) for d in post.diagnostics
    }  # noqa: E501
    verified_outcomes = tuple(
        outcome
        if outcome.action != 'revalidated'
        or (outcome.authority, outcome.record_ref) not in still_failing
        else RevalidationOutcome(
            authority=outcome.authority,
            record_ref=outcome.record_ref,
            action='kept_stale',
            detail='再署名は行われましたが、最終監査で依然として検証に'
            '失敗しています',
        )
        for outcome in outcomes
    )
    return RevalidationReport(
        database_path=database_path,
        outcomes=verified_outcomes,
        audit=post,
    )


# ------------------------------------------------------------------
# First-run-after-update detection (round 14)

LAUNCH_MARKER_NAME = 'htdt-launch-build.json'


def read_launch_marker(data_dir: Path) -> dict[str, Any] | None:
    """The build stamp of the last launch against this data root."""

    marker_path = Path(data_dir) / LAUNCH_MARKER_NAME
    try:
        payload = json.loads(marker_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_launch_marker(data_dir: Path) -> Path | None:
    """Atomically record this build as the last one launched here.

    Best-effort: marker I/O failure must never block launch, so errors
    return ``None`` instead of raising.
    """

    try:
        from .build_info import get_build_info

        info = get_build_info()
        data_dir = Path(data_dir)
        marker_path = data_dir / LAUNCH_MARKER_NAME
        payload = {
            'display_version': info.display_version,
            'commit_sha': info.commit_sha,
            'marked_at_utc': datetime.now(timezone.utc).isoformat(),
        }
        temp = marker_path.with_name(
            f'.{marker_path.name}.{uuid4().hex}.tmp'
        )
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding='utf-8',
        )
        os.replace(temp, marker_path)
        return marker_path
    except Exception:  # noqa: BLE001 - marker loss must never block launch
        _LOGGER.debug('launch marker write failed', exc_info=True)
        return None


def launch_build_changed(data_dir: Path) -> bool:
    """True when this build differs from the last one launched here.

    A missing marker counts as changed: a store last opened by a
    pre-marker build gets one post-update audit. Fresh installs simply
    audit clean and stamp the marker.
    """

    from .build_info import get_build_info

    marker = read_launch_marker(data_dir)
    if marker is None:
        return True
    info = get_build_info()
    return (
        marker.get('display_version') != info.display_version
        or (marker.get('commit_sha') or None) != (info.commit_sha or None)
    )


def post_update_copy_ja(stale_count: int) -> str:
    """Japanese summary for the first-run-after-update dialog."""

    return (
        'このバージョンのHTDTでデータを確認したところ、'
        f'{stale_count} 件の記録が再検証を必要としています。'
        '以前のバージョンで保存された計算結果や配線の検証記録が'
        '現在の形式と一致しなくなったため、関連する画面は'
        'エラーになる可能性があります。\n\n'
        '「再検証を実行」を選ぶと、現在のビルドで再導出できる記録を'
        '安全に再署名します。再導出できない記録は未検証のまま保持され、'
        '理由が表示されます（「あとで」を選んでもデータは失われません。'
        'いつでも「データ管理」の「記録を再検証」から実行できます）。'
    )
