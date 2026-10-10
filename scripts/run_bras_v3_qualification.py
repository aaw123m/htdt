#!/usr/bin/env python3
"""BRAS v3 qualification CLI (#836 Actions 4–6).

Fail-closed surface for the BRAS v3 qualification binding: no solver
physics is fabricated — every native solver lane is bound as ``blocked``
with the precise missing prerequisites, and only lanes that actually
emit predictions (currently the analytic direct-path lane, an
import-consistency check rather than a solver) produce numeric verdicts.

Subcommands (all exit non-zero on any integrity failure):

    bind    Import a scene and emit the sealed solver-lane binding JSON
    run     Import + build the metric manifest + execute the
            qualification run → sealed evidence JSON
    report  Bind + run + aggregate → sealed report JSON (+ markdown)

Payloads resolve under ``--corpus-root`` in the
``scripts/fetch_external_corpus.py`` layout
(``<root>/bras-v3/<file_name>``).

    python scripts/run_bras_v3_qualification.py bind \
        --scene RS4 --corpus-root D:/corpus --out binding.json
    python scripts/run_bras_v3_qualification.py run \
        --scene RS4 --member onCenter --indexes 0,4,8 \
        --bands 500:2000 --corpus-root D:/corpus --out evidence.json
    python scripts/run_bras_v3_qualification.py report \
        --scene RS4 --member onCenter --indexes 0,4,8 \
        --corpus-root D:/corpus --out report.json --markdown report.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'backend' / 'src'))

from htdt.cad_bras_v3_importer import (  # noqa: E402
    BrasImportError,
    import_bras_v3_scene,
)
from htdt.cad_bras_v3_metrics import (  # noqa: E402
    build_bras_v3_metric_manifest,
)
from htdt.cad_bras_v3_qualification import (  # noqa: E402
    BrasV3QualificationError,
    bind_bras_v3_solver_lanes,
    build_bras_v3_qualification_config,
    build_bras_v3_qualification_report,
    render_bras_v3_report_markdown,
    run_bras_v3_qualification,
)
from htdt.cad_external_corpus_manifest import (  # noqa: E402
    BRAS_V3_ADMISSION,
    corpus_scene,
    external_corpus_manifest,
)


def _scene(scene_id: str):
    scene = corpus_scene(external_corpus_manifest(), scene_id)
    if scene is None:
        raise SystemExit(
            f'corpus scene {scene_id!r} is not in the built-in manifest'
        )
    return scene


def _import(scene_id: str, root: Path):
    return import_bras_v3_scene(BRAS_V3_ADMISSION, _scene(scene_id), root)


def _member(scene_import, needle: str | None) -> str:
    imported = [m.member_path for m in scene_import.sofa_members
                if m.disposition == 'imported']
    if not imported:
        raise SystemExit('scene has no imported GeneralFIR members')
    if needle is None:
        if len(imported) == 1:
            return imported[0]
        raise SystemExit(
            'scene has multiple imported members — pass --member '
            '<substring>: ' + ', '.join(imported)
        )
    matches = [m for m in imported if needle in m]
    if len(matches) != 1:
        raise SystemExit(
            f'--member {needle!r} matches {len(matches)} imported members: '
            + ', '.join(imported)
        )
    return matches[0]


def _indexes(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in text.split(',') if p.strip())
    except ValueError:
        raise SystemExit('--indexes must be comma-separated integers')


def _bands(text: str | None) -> tuple[tuple[float, float], ...]:
    if not text:
        return ()
    try:
        return tuple(
            (float(lo), float(hi))
            for lo, hi in (p.split(':') for p in text.split(',') if p.strip())
        )
    except ValueError:
        raise SystemExit('--bands must be comma-separated lo:hi pairs')


def _dump(model, out: str | None) -> str:
    payload = model.model_dump_json(indent=2, exclude_none=True)
    if out:
        Path(out).write_text(payload, encoding='utf-8')
    return payload


def _bind(scene_import, member: str):
    bound = [m for m in scene_import.sofa_members
             if m.member_path == member and m.measurement_sha256]
    if len(bound) != 1:
        raise SystemExit(f'member {member!r} has no bound measurement')
    return bind_bras_v3_solver_lanes(
        scene_import.case, member_path=member,
        measurement_sha256=bound[0].measurement_sha256)


def _run(scene_import, scene_id: str, root: Path, member: str,
         args: argparse.Namespace):
    indexes = _indexes(args.indexes)
    manifest = build_bras_v3_metric_manifest(
        BRAS_V3_ADMISSION, _scene(scene_id), root,
        member_path=member, measurement_indexes=indexes,
        bands_hz=_bands(getattr(args, 'bands', None)))
    config = build_bras_v3_qualification_config(
        member_path=member, measurement_indexes=indexes,
        run_mode=args.run_mode,
        arrival_tolerance_s=args.arrival_tolerance_s)
    evidence, binding = run_bras_v3_qualification(
        scene_import, manifest, config)
    return manifest, evidence, binding


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    def _common(p: argparse.ArgumentParser):
        p.add_argument('--scene', required=True,
                       help='corpus scene id (e.g. RS4)')
        p.add_argument('--corpus-root', required=True,
                       help='root containing <root>/bras-v3/')
        p.add_argument('--member', default=None,
                       help='substring picking the imported SOFA member')
        p.add_argument('--out', default=None,
                       help='write the sealed JSON here (stdout always prints a summary)')

    bind_p = sub.add_parser('bind', help='sealed solver-lane binding')
    _common(bind_p)

    run_p = sub.add_parser('run', help='qualification run → evidence')
    _common(run_p)
    run_p.add_argument('--indexes', required=True,
                       help='comma-separated measurement indexes')
    run_p.add_argument('--bands', default=None,
                       help='comma-separated lo:hi band pairs')
    run_p.add_argument('--run-mode', default='preregistered_unfitted',
                       choices=('preregistered_unfitted',
                                'informed_calibrated'))
    run_p.add_argument('--arrival-tolerance-s', type=float, default=0.002)

    report_p = sub.add_parser('report', help='aggregate report (+ markdown)')
    _common(report_p)
    report_p.add_argument('--indexes', required=True)
    report_p.add_argument('--bands', default=None)
    report_p.add_argument('--run-mode',
                          default='preregistered_unfitted',
                          choices=('preregistered_unfitted',
                                   'informed_calibrated'))
    report_p.add_argument('--arrival-tolerance-s', type=float, default=0.002)
    report_p.add_argument('--markdown', default=None,
                          help='write the markdown render here')

    args = parser.parse_args()
    root = Path(args.corpus_root)

    try:
        if args.command == 'bind':
            scene_import = _import(args.scene, root)
            member = _member(scene_import, args.member)
            binding = _bind(scene_import, member)
            payload = _dump(binding, args.out)
            for lane in binding.lanes:
                print(f'[{lane.lane}] {lane.verdict} — produced '
                      f'{len(lane.produced_observables)} observables')
            print(f'binding {binding.binding_id}: produced '
                  f'{len(binding.produced_observables)} observables')
            if not args.out:
                print(payload)
            return 0

        if args.command == 'run':
            scene_import = _import(args.scene, root)
            member = _member(scene_import, args.member)
            _, evidence, _ = _run(
                scene_import, args.scene, root, member, args)
            payload = _dump(evidence, args.out)
            for r in evidence.results:
                error = '—' if r.error is None else f'{r.error:.6g}'
                print(f'[{r.observable_id}] {r.verdict} err={error} '
                      f'{r.reason}')
            print(f'evidence {evidence.evidence_id}: {evidence.status}')
            if not args.out:
                print(payload)
            return 0

        if args.command == 'report':
            scene_import = _import(args.scene, root)
            member = _member(scene_import, args.member)
            manifest, evidence, binding = _run(
                scene_import, args.scene, root, member, args)
            report = build_bras_v3_qualification_report(
                scene_import=scene_import, manifest=manifest,
                binding=binding, evidences=(evidence,))
            payload = _dump(report, args.out)
            markdown = render_bras_v3_report_markdown(report)
            if args.markdown:
                Path(args.markdown).write_text(markdown, encoding='utf-8')
            print(markdown if not args.out else
                  f'report {report.report_id}: '
                  f'{report.verdict_counts}')
            if not args.out and not args.markdown:
                print(payload)
            return 0
    except (BrasImportError,
            BrasV3QualificationError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
