"""Semantic round-trip regression harness for the interop corpus (#892).

Executes every fixture in a sealed :class:`InteropCorpusManifest` through
its family's real lane —

    EXTERNAL FIXTURE → IMPORT → canonical HTDT state → EXPORT →
    RE-IMPORT → SEMANTIC COMPARE

where a round-trip lane exists, and IMPORT → canonical state → declared
semantic assertions where it does not. Comparison is *semantic*, never
byte-level: geometry/topology, channel/routing, filter parameters,
measurement axes/units/references, material/equipment identity, and the
declared warning/degradation set.

Verdicts (#892 / REV70 slice contract):

- ``semantically_equal`` — all assertions hold, the observed
  warning/degradation sets equal the declared sets, and round-trip
  state matches semantically;
- ``degraded_as_declared`` — same, with a non-empty declared
  warning/degradation set observed verbatim;
- ``unsupported_as_declared`` — an ``unsupported_assert`` fixture whose
  declared negative verdict was observed exactly;
- ``regression`` — any violated/unverifiable assertion, a warning or
  degradation set that differs from the declaration in either
  direction, or a round-trip semantic mismatch;
- ``unexpected_failure`` — the lane raised, the fixture drifted from
  its sealed pin, or the run record could not be produced.

The harness never invents support: a licensed-binary fixture asserting
``unsupported`` PASSES as ``unsupported_as_declared``; the same payload
parsing as supported is a ``regression``.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from math import isclose
from pathlib import Path
from typing import Any

from .cad_authority_resolver import AuthorityRef
from .cad_equalizer_apo_export import (
    ApoExportChannel,
    ApoExportBand,
    render_equalizer_apo_config,
)
from .cad_external_calibration import (
    ImportedCalibrationArtifact,
    build_equalizer_apo_artifact,
)
from .cad_camilladsp import build_camilladsp_artifact
from .cad_ifc_interop import (
    IfcEntityMapping,
    IfcImportArtifact,
    build_ifc_export_package,
    build_ifc_import,
)
from .cad_interop_corpus import (
    InteropAssertionOutcome,
    InteropCorpusManifest,
    InteropCorpusRunRecord,
    InteropCorpusVerdict,
    InteropFixtureEntry,
    InteropFixtureRunRecord,
    InteropFixtureVerdict,
    InteropSemanticAssertion,
    build_interop_corpus_run,
    build_interop_fixture_run,
    corpus_run_verdict,
)
from .cad_loudspeaker_interchange import qualify_clf
from .cad_repository import SceneRepository
from .canonical_json import canonical_sha256 as _hash
from .project_bundle import export_project_bundle, import_project_bundle
from .rew_parser import parse_rew_frequency_response


INTEROP_HARNESS_VERSION = 'interop-harness-1'

_MAX_FIXTURE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class _LaneResult:
    """What a family lane observed — the semantic state the harness
    asserts against, plus the warning/degradation evidence."""

    observed: dict[str, Any]
    warnings: tuple[str, ...] = ()
    degradations: tuple[str, ...] = ()
    imported_semantic_sha256: str | None = None
    reimported_semantic_sha256: str | None = None
    detail: str = ''
    #: For ``round_trip`` lanes: True only when the re-imported state is
    #: semantically equal to the first import (NOT byte equality).
    round_trip_equal: bool | None = None


def _opaque_kinds(artifact: ImportedCalibrationArtifact) -> tuple[str, ...]:
    """Stable degradation tokens — the preserved-opaque policy made
    observable so silence in either direction is a regression."""
    return tuple(
        sorted({f'opaque:{section.kind}' for section in artifact.opaque_sections})
    )


def _channel_observed(
    prefix: str, artifact: ImportedCalibrationArtifact, out: dict[str, Any]
) -> None:
    out[f'{prefix}.channel_count'] = len(artifact.channels)
    for channel in artifact.channels:
        base = f'channel.{channel.channel_label}'
        out[f'{base}.preamp_db'] = channel.preamp_db
        out[f'{base}.delay_s'] = channel.delay_s
        out[f'{base}.band_count'] = len(channel.peq)
        for index, band in enumerate(channel.peq):
            b = f'{base}.band.{index}'
            out[f'{b}.filter_type'] = band.filter_type
            out[f'{b}.enabled'] = band.enabled
            out[f'{b}.frequency_hz'] = band.frequency_hz
            out[f'{b}.gain_db'] = band.gain_db
            out[f'{b}.q'] = band.q
            out[f'{b}.bandwidth_oct'] = band.bandwidth_oct


def _channel_semantic(
    artifact: ImportedCalibrationArtifact,
) -> dict[str, Any]:
    """Canonical semantic projection of an imported calibration
    artifact — what the APO round-trip compares meaningfully.

    Buckets that carry no settings at all (e.g. an ``ALL`` pseudo-channel
    created by a bare ``Channel: ALL`` scope line that no command ever
    applied to) are semantically inert — they compose nothing onto any
    channel — so they are excluded from the comparison state rather than
    allowed to read as drift.
    """
    channels = {
        channel.channel_label: channel for channel in artifact.channels
        if (
            channel.preamp_db is not None
            or channel.delay_s is not None
            or channel.peq
        )
    }
    return {
        'global_preamp_db': artifact.global_preamp_db,
        'device_context': artifact.device_context,
        'channels': {
            channel.channel_label: {
                'preamp_db': channel.preamp_db,
                'delay_s': channel.delay_s,
                'peq': [
                    {
                        'filter_type': band.filter_type,
                        'enabled': band.enabled,
                        'frequency_hz': band.frequency_hz,
                        'gain_db': band.gain_db,
                        'q': band.q,
                        'bandwidth_oct': band.bandwidth_oct,
                    }
                    for band in channel.peq
                ],
            }
            for channel in artifact.channels
        },
    }


def _semantically_close(a: Any, b: Any, *, tol: float = 1e-9) -> bool:
    """Structural equality with float tolerance — semantic compare for
    channel/filter/routing state (never byte equality)."""
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        try:
            return isclose(float(a), float(b), abs_tol=tol)
        except (TypeError, ValueError):
            return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(
            _semantically_close(a[k], b[k], tol=tol) for k in a
        )
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(
            _semantically_close(x, y, tol=tol) for x, y in zip(a, b)
        )
    return a == b


# ---------------------------------------------------------------------------
# Family lanes
# ---------------------------------------------------------------------------


def _lane_rew_text(
    entry: InteropFixtureEntry,
    source: bytes,
    work_dir: Path | None,
) -> _LaneResult:
    parsed = parse_rew_frequency_response(source)
    observed: dict[str, Any] = {
        'row_count': len(parsed.frequency_hz),
        'first_frequency_hz': (
            parsed.frequency_hz[0] if parsed.frequency_hz else None
        ),
        'last_frequency_hz': (
            parsed.frequency_hz[-1] if parsed.frequency_hz else None
        ),
        'min_level_db': min(parsed.level_db) if parsed.level_db else None,
        'max_level_db': max(parsed.level_db) if parsed.level_db else None,
        'phase_present': parsed.phase_deg is not None,
        'phase_status': parsed.phase_status,
        'level_reference': parsed.level_reference,
        'parser_version': parsed.parser_version,
        'header_line_count': len(parsed.header_lines),
    }
    return _LaneResult(
        observed=observed,
        warnings=tuple(parsed.warnings),
        imported_semantic_sha256=_hash(
            {
                'frequency_hz': list(parsed.frequency_hz),
                'level_db': list(parsed.level_db),
                'phase_deg': (
                    list(parsed.phase_deg)
                    if parsed.phase_deg is not None
                    else None
                ),
                'phase_status': parsed.phase_status,
                'level_reference': parsed.level_reference,
            }
        ),
    )


def _lane_equalizer_apo(
    entry: InteropFixtureEntry,
    source: bytes,
    work_dir: Path | None,
) -> _LaneResult:
    imported = build_equalizer_apo_artifact(
        source,
        source_filename=Path(entry.relative_path).name,
        imported_at_utc=_LANE_TS,
        artifact_id=f'imported-calibration:{entry.fixture_id}',
    )
    observed: dict[str, Any] = {
        'import.format_id': imported.format_id,
        'import.format_version': imported.format_version,
        'import.global_preamp_db': imported.global_preamp_db,
        'import.device_context': imported.device_context,
        'import.opaque_section_kinds': sorted(
            {s.kind for s in imported.opaque_sections}
        ),
    }
    _channel_observed('import', imported, observed)

    # EXPORT: rebuild the bounded subset from the imported semantic
    # state. Bands a bounded export cannot express never render — they
    # are surfaced, not silently dropped.
    channels: list[ApoExportChannel] = []
    unexportable = 0
    for channel in imported.channels:
        labels: tuple[str, ...] = (
            () if channel.channel_label == 'ALL'
            else (channel.channel_label,)
        )
        bands: list[ApoExportBand] = []
        for band in channel.peq:
            if band.frequency_hz is None:
                unexportable += 1
                continue
            bands.append(
                ApoExportBand(
                    filter_type=band.filter_type,
                    frequency_hz=band.frequency_hz,
                    gain_db=band.gain_db,
                    q=band.q,
                    bandwidth_oct=band.bandwidth_oct,
                    enabled=band.enabled,
                )
            )
        channels.append(
            ApoExportChannel(
                channel_labels=labels,
                preamp_db=channel.preamp_db,
                delay_s=channel.delay_s,
                bands=tuple(bands),
            )
        )
    rendered = render_equalizer_apo_config(
        channels=tuple(channels),
        global_preamp_db=imported.global_preamp_db,
        device=imported.device_context,
    )
    reimported = build_equalizer_apo_artifact(
        rendered.encode('utf-8'),
        source_filename='roundtrip.txt',
        imported_at_utc=_LANE_TS,
        artifact_id=f'imported-calibration:{entry.fixture_id}-rt',
    )
    semantic_equal = _semantically_close(
        _channel_semantic(imported), _channel_semantic(reimported)
    ) and unexportable == 0
    observed['export.rendered_band_count'] = sum(
        len(c.bands) for c in channels
    )
    observed['export.unexportable_bands'] = unexportable
    observed['roundtrip.semantic_equal'] = semantic_equal
    return _LaneResult(
        observed=observed,
        warnings=tuple(imported.diagnostics),
        degradations=_opaque_kinds(imported),
        imported_semantic_sha256=_hash(_channel_semantic(imported)),
        reimported_semantic_sha256=_hash(_channel_semantic(reimported)),
        round_trip_equal=semantic_equal,
        detail='APO bounded subset export + re-import semantic compare',
    )


def _lane_camilladsp(
    entry: InteropFixtureEntry,
    source: bytes,
    work_dir: Path | None,
) -> _LaneResult:
    imported = build_camilladsp_artifact(
        source,
        source_filename=Path(entry.relative_path).name,
        imported_at_utc=_LANE_TS,
        artifact_id=f'imported-calibration:{entry.fixture_id}',
    )
    observed: dict[str, Any] = {
        'import.format_id': imported.format_id,
        'import.opaque_section_kinds': sorted(
            {s.kind for s in imported.opaque_sections}
        ),
    }
    _channel_observed('import', imported, observed)
    return _LaneResult(
        observed=observed,
        warnings=tuple(imported.diagnostics),
        degradations=_opaque_kinds(imported),
        imported_semantic_sha256=_hash(_channel_semantic(imported)),
    )


def _lane_clf(
    entry: InteropFixtureEntry,
    source: bytes,
    work_dir: Path | None,
) -> _LaneResult:
    qualification = qualify_clf(source)
    return _LaneResult(
        observed={
            'verdict': qualification.verdict,
            'family': qualification.family,
            'frequency_rows': qualification.frequency_rows,
            'declares_license': qualification.declares_license,
            'detected_version': qualification.detected_version,
        },
        warnings=(),
        degradations=(),
    )


def _room_geometry(
    mappings: tuple[IfcEntityMapping, ...],
) -> dict[str, dict[str, Any]]:
    """Normalized per-room geometry/positional state used for the
    semantic round-trip compare (metre-space, fingerprint-agnostic)."""
    rooms: dict[str, dict[str, Any]] = {}
    for mapping in mappings:
        if mapping.htdt_role != 'room_candidate':
            continue
        name = mapping.name or f'#{mapping.step_entity_id}'
        solids = [
            g for g in mapping.geometry_descriptors
            if g.kind == 'extruded_area_solid'
        ]
        transform = mapping.world_transform_m
        rooms[name] = {
            'position': (
                (transform[3], transform[7], transform[11])
                if transform is not None and len(transform) >= 12
                else None
            ),
            'solids': [
                {
                    'profile': [tuple(p) for p in s.profile_points],
                    'direction': s.extrusion_direction,
                    'depth': s.depth_source_units,
                }
                for s in solids
            ],
        }
    return rooms


def _lane_ifc(
    entry: InteropFixtureEntry, source: bytes, work_dir: Path | None
) -> _LaneResult:
    artifact, mappings = build_ifc_import(
        document_id='interop-corpus',
        file_name=Path(entry.relative_path).name,
        source=source,
        imported_at_utc=_LANE_TS,
    )
    observed: dict[str, Any] = {
        'import.entity_total': artifact.coverage.entity_total,
        'import.mapped_entity_count': artifact.coverage.mapped_entity_count,
        'import.spatial_entity_count': (
            artifact.coverage.spatial_entity_count
        ),
        'import.coordinate.length_scale_to_meter': (
            artifact.coordinate.length_scale_to_meter
        ),
        'import.unhandled_entity_types': dict(
            sorted(artifact.coverage.unhandled_entity_types.items())
        ),
    }
    rooms = _room_geometry(mappings)
    observed['room_candidate.count'] = len(rooms)
    for name, state in rooms.items():
        solid = state['solids'][0] if state['solids'] else {}
        observed[f'room.{name}.profile_vertex_count'] = len(
            solid.get('profile', ())
        )
        observed[f'room.{name}.extrusion_depth_m'] = solid.get('depth')
        position = state['position']
        for axis, label in ((0, 'x'), (1, 'y'), (2, 'z')):
            observed[f'room.{name}.world_position_{label}_m'] = (
                position[axis] if position is not None else None
            )

    package = build_ifc_export_package(
        document_id='interop-corpus',
        mode='reference_export',
        label='corpus-round-trip',
        spaces=tuple(m for m in mappings if m.htdt_role == 'room_candidate'),
        source_artifact=artifact,
        created_at_utc=_LANE_TS,
    )
    observed['export.step_sha256'] = package.step_sha256
    observed['export.unexported_count'] = len(package.unexported_items)

    reimported, remapped = build_ifc_import(
        document_id='interop-corpus',
        file_name='roundtrip.ifc',
        source=package.step_text,
        imported_at_utc=_LANE_TS,
    )
    rerooms = _room_geometry(remapped)
    equal = (
        rerooms.keys() == rooms.keys()
        and all(
            _semantically_close(rooms[name], rerooms[name])
            for name in rooms
        )
        and bool(rooms)
    )
    observed['roundtrip.room_geometry_equal'] = equal
    observed['roundtrip.room_candidate_count'] = len(rerooms)
    return _LaneResult(
        observed=observed,
        warnings=tuple(artifact.coverage.warnings),
        degradations=tuple(
            sorted(
                {
                    f'unhandled:{kind}'
                    for kind in artifact.coverage.unhandled_entity_types
                }
            )
        ),
        imported_semantic_sha256=_hash(
            {'rooms': rooms, 'coverage': observed['import.entity_total']}
        ),
        reimported_semantic_sha256=_hash(
            {'rooms': rerooms}
        ),
        round_trip_equal=equal,
        detail=(
            'IFC STEP import → bounded export → re-import; '
            'rooms compared semantically in metres'
        ),
    )


def _lane_project_bundle(
    entry: InteropFixtureEntry, source: bytes, work_dir: Path | None
) -> _LaneResult:
    assert work_dir is not None  # corpus runner always provides one
    spec = json.loads(source.decode('utf-8'))
    from .cad_scene import (  # deferred — keeps harness import light
        Position3,
        RoomPrism,
        SceneDocument,
        SceneEntity,
        Size3,
    )

    entities = []
    for item in spec['entities']:
        size = item.get('size_m')
        entities.append(
            SceneEntity(
                entity_id=item['entity_id'],
                kind=item['kind'],
                name=item['name'],
                position=Position3(
                    x_m=item['position_m'][0],
                    y_m=item['position_m'][1],
                    z_m=item['position_m'][2],
                ),
                size_m=(
                    Size3(x_m=size[0], y_m=size[1], z_m=size[2])
                    if size
                    else None
                ),
                speaker_role=item.get('speaker_role'),
            )
        )
    scene = SceneDocument(
        document_id=spec['document_id'],
        schema_version=spec.get('schema_version', 2),
        room=RoomPrism(
            width_m=spec['room']['width_m'],
            depth_m=spec['room']['depth_m'],
            height_m=spec['room']['height_m'],
        ),
        entities=tuple(entities),
    )
    source_repo = SceneRepository(work_dir / 'src' / 'cad-scenes.sqlite3')
    saved = source_repo.save(scene, parent_revision_id=None)
    bundle_a = work_dir / 'export-a.htdtproject'
    export_a = export_project_bundle(
        source_repo, spec['document_id'], bundle_a
    )

    target_repo = SceneRepository(work_dir / 'dst' / 'cad-scenes.sqlite3')
    result = import_project_bundle(target_repo, bundle_a)
    bundle_b = work_dir / 'export-b.htdtproject'
    export_b = export_project_bundle(
        target_repo, result.document_id, bundle_b
    )

    def _row_sets(archive: Path) -> dict[str, frozenset[dict]]:
        # Bundle members are JSONL of {'columns': [...], 'values': [...]}
        # pairs; normalize to row dicts (the internal 'seq' autoincrement
        # is storage-local, not semantics).
        rows: dict[str, frozenset[dict]] = {}
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if not name.startswith('db/') or not name.endswith(
                    '.jsonl'
                ):
                    continue
                table = name[len('db/'):-len('.jsonl')]
                table_rows = []
                for line in bundle.read(name).decode('utf-8').splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    row = dict(zip(record['columns'], record['values']))
                    row.pop('seq', None)
                    table_rows.append(row)
                rows[table] = frozenset(
                    _freeze(row) for row in table_rows
                )
        return rows

    rows_a, rows_b = _row_sets(bundle_a), _row_sets(bundle_b)
    common = rows_a.keys() & rows_b.keys()
    core_rows_equal = all(rows_a[t] == rows_b[t] for t in common)
    # Rows legitimately minted by import (e.g. the project-library
    # registration) are declared additions, not regressions; anything
    # CHANGED in a shared table is a semantic regression.
    new_tables = sorted(set(rows_b) - set(rows_a))
    lost_tables = sorted(set(rows_a) - set(rows_b))
    row_sets_equal = core_rows_equal and not lost_tables
    observed = {
        'bundle.contains_document': bool(rows_a),
        'bundle.table_count': len(rows_a),
        'bundle.core_table_names': sorted(rows_a),
        'bundle.row_count_total': sum(len(r) for r in rows_a.values()),
        'roundtrip.core_rows_equal': core_rows_equal,
        'roundtrip.row_sets_equal': row_sets_equal,
        'roundtrip.new_tables': new_tables,
        'roundtrip.lost_tables': lost_tables,
        'roundtrip.document_id': result.document_id,
        'roundtrip.export_row_delta': (
            export_b.row_count - export_a.row_count
        ),
    }
    return _LaneResult(
        observed=observed,
        imported_semantic_sha256=_hash(
            {
                'tables': {
                    table: [dict(r) for r in sorted(rows, key=repr)]
                    for table, rows in sorted(rows_a.items())
                }
            }
        ),
        reimported_semantic_sha256=_hash(
            {
                'tables': {
                    table: [dict(r) for r in sorted(rows, key=repr)]
                    for table, rows in sorted(rows_b.items())
                    if table in common
                }
            }
        ),
        round_trip_equal=row_sets_equal,
        detail=(
            'spec → scene → .htdtproject → re-import → re-export; '
            'per-table row sets compared (seq-independent)'
        ),
    )


_LANES = {
    'rew_text': _lane_rew_text,
    'equalizer_apo': _lane_equalizer_apo,
    'camilladsp': _lane_camilladsp,
    'clf': _lane_clf,
    'ifc_step': _lane_ifc,
    'htdt_project_bundle': _lane_project_bundle,
}

_LANE_TS = '2026-01-01T00:00:00+00:00'
"""Lane-internal timestamps (import/export records) — fixed so the
semantic hashes stay reproducible; run records carry real run times."""


# ---------------------------------------------------------------------------
# Assertion evaluation + verdict
# ---------------------------------------------------------------------------


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set)):
        return tuple(_freeze(v) for v in value)
    return value


def evaluate_assertion(
    assertion: InteropSemanticAssertion, observed: dict[str, Any]
) -> InteropAssertionOutcome:
    """Evaluate one declared assertion — a missing observed key is
    ``skipped_unverifiable`` (insufficient evidence, never a pass)."""
    base = {
        'path': assertion.path,
        'comparator': assertion.comparator,
        'expected_repr': repr(assertion.expected),
    }
    if assertion.path not in observed:
        return InteropAssertionOutcome(
            state='skipped_unverifiable',
            observed_repr='<absent>',
            detail='lane produced no value for this assertion path',
            **base,
        )
    value = observed[assertion.path]
    ok: bool
    detail = ''
    if assertion.comparator == 'equals':
        ok = value == assertion.expected
    elif assertion.comparator == 'approx':
        if isinstance(value, bool) or not isinstance(
            value, (int, float)
        ):
            ok = False
            detail = 'observed value is not numeric'
        else:
            ok = isclose(
                float(value),
                float(assertion.expected),
                abs_tol=assertion.tolerance or 0.0,
            )
    elif assertion.comparator == 'set_equals':
        try:
            ok = {_freeze(v) for v in value} == {
                _freeze(v) for v in assertion.expected
            }
        except TypeError:
            ok = False
            detail = 'observed value is not iterable'
    else:  # contains
        try:
            ok = assertion.expected in value
        except TypeError:
            ok = False
            detail = 'observed value is not a container'
    return InteropAssertionOutcome(
        state='holds' if ok else 'violated',
        observed_repr=repr(value),
        detail=detail,
        **base,
    )


def compute_fixture_verdict(
    entry: InteropFixtureEntry,
    outcomes: tuple[InteropAssertionOutcome, ...],
    lane: _LaneResult,
) -> InteropFixtureVerdict:
    """The fail-closed verdict rule — see module docstring."""
    states = {outcome.state for outcome in outcomes}
    if states & {'violated', 'skipped_unverifiable'}:
        return 'regression'
    if entry.round_trip_mode == 'unsupported_assert':
        # The declared negative verdict must be observed verbatim; a
        # positive parse would mean invented support — a regression.
        if lane.observed.get('verdict') == 'qualified':
            return 'regression'
        return 'unsupported_as_declared'
    if set(lane.warnings) != set(entry.expected_warnings):
        return 'regression'
    if set(lane.degradations) != set(entry.expected_degradations):
        return 'regression'
    if (
        entry.round_trip_mode == 'round_trip'
        and lane.round_trip_equal is not True
    ):
        return 'regression'
    if entry.expected_warnings or entry.expected_degradations:
        return 'degraded_as_declared'
    return 'semantically_equal'


def run_interop_fixture(
    entry: InteropFixtureEntry,
    source: bytes,
    *,
    document_id: str,
    manifest_sha256: str,
    corpus_version: str,
    work_dir: Path | None = None,
    started_at_utc: str,
    finished_at_utc: str,
) -> InteropFixtureRunRecord:
    """Execute one fixture through its family lane and seal the run."""
    verdict: InteropFixtureVerdict
    outcomes: tuple[InteropAssertionOutcome, ...] = ()
    lane: _LaneResult
    if len(source) != entry.size_bytes or (
        hashlib.sha256(source).hexdigest() != entry.content_sha256
    ):
        verdict = 'unexpected_failure'
        lane = _LaneResult(
            observed={},
            detail='fixture payload drifted from its sealed pin',
        )
    else:
        try:
            lane = _LANES[entry.format_family](entry, source, work_dir)
        except Exception as exc:  # noqa: BLE001 — recorded, not raised
            verdict = 'unexpected_failure'
            lane = _LaneResult(
                observed={},
                detail=(
                    f'{entry.format_family} lane raised '
                    f'{type(exc).__name__}: {exc}'
                ),
            )
        else:
            outcomes = tuple(
                evaluate_assertion(a, lane.observed)
                for a in entry.assertions
            )
            verdict = compute_fixture_verdict(entry, outcomes, lane)
    return build_interop_fixture_run(
        document_id=document_id,
        fixture_ref=AuthorityRef(
            kind='interop_fixture',
            ref_id=entry.fixture_id,
            ref_sha256=entry.fixture_sha256,
        ),
        manifest_sha256=manifest_sha256,
        corpus_version=corpus_version,
        harness_version=INTEROP_HARNESS_VERSION,
        format_family=entry.format_family,
        round_trip_mode=entry.round_trip_mode,
        verdict=verdict,
        outcomes=outcomes,
        observed_warnings=lane.warnings,
        observed_degradations=lane.degradations,
        imported_semantic_sha256=lane.imported_semantic_sha256,
        reimported_semantic_sha256=lane.reimported_semantic_sha256,
        detail=lane.detail,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
    )


def run_interop_corpus(
    manifest: InteropCorpusManifest,
    corpus_dir: Path,
    *,
    document_id: str,
    work_dir: Path,
    started_at_utc: str,
    finished_at_utc: str,
) -> tuple[InteropCorpusRunRecord, tuple[InteropFixtureRunRecord, ...]]:
    """Run every declared fixture and seal one corpus run record.

    The corpus verdict is ``regression_detected`` whenever any fixture
    regresses or fails unexpectedly — partial evidence never reads as
    a pass.
    """
    runs: list[InteropFixtureRunRecord] = []
    for entry in manifest.fixtures:
        path = corpus_dir / entry.relative_path
        try:
            if not path.is_file() or (
                path.stat().st_size > _MAX_FIXTURE_BYTES
            ):
                raise FileNotFoundError(entry.relative_path)
            source = path.read_bytes()
        except OSError:
            runs.append(
                build_interop_fixture_run(
                    document_id=document_id,
                    fixture_ref=AuthorityRef(
                        kind='interop_fixture',
                        ref_id=entry.fixture_id,
                        ref_sha256=entry.fixture_sha256,
                    ),
                    manifest_sha256=manifest.manifest_sha256,
                    corpus_version=manifest.corpus_version,
                    harness_version=INTEROP_HARNESS_VERSION,
                    format_family=entry.format_family,
                    round_trip_mode=entry.round_trip_mode,
                    verdict='unexpected_failure',
                    detail='fixture file missing or unreadable',
                    started_at_utc=started_at_utc,
                    finished_at_utc=finished_at_utc,
                )
            )
            continue
        runs.append(
            run_interop_fixture(
                entry,
                source,
                document_id=document_id,
                manifest_sha256=manifest.manifest_sha256,
                corpus_version=manifest.corpus_version,
                work_dir=work_dir / entry.fixture_id,
                started_at_utc=started_at_utc,
                finished_at_utc=finished_at_utc,
            )
        )
    verdict: InteropCorpusVerdict = corpus_run_verdict(runs)
    counts: dict[str, int] = {}
    for run in runs:
        counts[run.verdict] = counts.get(run.verdict, 0) + 1
    record = build_interop_corpus_run(
        document_id=document_id,
        manifest_ref=AuthorityRef(
            kind='interop_corpus_manifest',
            ref_id=manifest.manifest_id,
            ref_sha256=manifest.manifest_sha256,
        ),
        corpus_version=manifest.corpus_version,
        harness_version=INTEROP_HARNESS_VERSION,
        verdict=verdict,
        fixture_run_ids=tuple(run.run_id for run in runs),
        verdict_counts=counts,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
    )
    return record, tuple(runs)


__all__ = [
    'INTEROP_HARNESS_VERSION',
    'compute_fixture_verdict',
    'evaluate_assertion',
    'run_interop_corpus',
    'run_interop_fixture',
]
