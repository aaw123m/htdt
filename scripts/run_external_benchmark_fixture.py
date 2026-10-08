#!/usr/bin/env python3
"""Deterministic replay CLI for external measured benchmark fixtures (#948).

Subcommands (all fail closed, exit non-zero on any integrity failure):

    verify  Check every consumed payload pin of a fixture record
    import  Verify + read the licensed SOFA RIR → normalized BenchmarkCase
    plan    Freeze a run spec (solver/provider/revision/mesh/seed/runtime)
    run     Execute the spec against a provider → sealed evidence JSON
    replay  Re-execute and compare the sealed evidence identity

Payloads are looked up under ``--corpus-root`` using the
``scripts/fetch_external_corpus.py`` layout
(``<root>/<dataset_name>/<file_name>``). Dataset/scene authorities resolve
from the built-in corpus manifest unless ``--dataset``/``--scene`` point to
external JSON records (used for fixtures over non-BRAS corpora).

    python scripts/run_external_benchmark_fixture.py verify \
        --fixture fixture.json --corpus-root D:/corpus
    python scripts/run_external_benchmark_fixture.py plan \
        --fixture fixture.json --provider analytic-direct \
        --spec-id rs8-01a-arrival --profile-id arrival --profile-version 1 \
        --out spec.json
    python scripts/run_external_benchmark_fixture.py run \
        --fixture fixture.json --corpus-root D:/corpus --spec spec.json \
        --observables observables.json --rir-member 'Scene/RS8_01a/mic.sofa' \
        --out evidence.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'backend' / 'src'))

from htdt.cad_authority_resolver import AuthorityRef  # noqa: E402
from htdt.cad_benchmark import (  # noqa: E402
    BenchmarkObservable,
    EvaluationProfile,
)
from htdt.cad_external_admission import ExternalAssetAdmission  # noqa: E402
from htdt.cad_external_benchmark_fixture import (  # noqa: E402
    AnalyticDirectPathProvider,
    ExternalBenchmarkFixture,
    FixtureImportError,
    FixtureIntegrityError,
    FixtureRunEvidence,
    FixtureRunSpec,
    StaticFixtureReplayProvider,
    build_fixture_run_spec,
    import_fixture_case,
    replay_fixture,
    run_fixture,
    runtime_descriptor,
    verify_fixture_payloads,
)
from htdt.cad_external_corpus_manifest import (  # noqa: E402
    CorpusScene,
    corpus_dataset,
    corpus_scene,
    external_corpus_manifest,
)


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _load_fixture(path: str) -> ExternalBenchmarkFixture:
    return ExternalBenchmarkFixture.model_validate_json(
        Path(path).read_text(encoding='utf-8')
    )


def _resolve_dataset_scene(
    fixture: ExternalBenchmarkFixture, args: argparse.Namespace
) -> tuple[ExternalAssetAdmission, CorpusScene]:
    if getattr(args, 'dataset', None):
        dataset = ExternalAssetAdmission.model_validate_json(
            Path(args.dataset).read_text(encoding='utf-8')
        )
    else:
        dataset = corpus_dataset(
            external_corpus_manifest(), fixture.dataset_ref.ref_id
        )
        if dataset is None:
            raise SystemExit(
                f'dataset admission {fixture.dataset_ref.ref_id} is not in '
                'the built-in corpus manifest — pass --dataset <json>'
            )
    if getattr(args, 'scene', None):
        scene = CorpusScene.model_validate_json(
            Path(args.scene).read_text(encoding='utf-8')
        )
    else:
        scene = corpus_scene(
            external_corpus_manifest(), fixture.scene_ref.ref_id
        )
        if scene is None:
            raise SystemExit(
                f'corpus scene {fixture.scene_ref.ref_id} is not in the '
                'built-in corpus manifest — pass --scene <json>'
            )
    return dataset, scene


def _load_observables(args: argparse.Namespace) -> tuple[BenchmarkObservable, ...]:
    if not getattr(args, 'observables', None):
        return ()
    raw = _load_json(args.observables)
    if not isinstance(raw, list):
        raise SystemExit('--observables must be a JSON list')
    return tuple(BenchmarkObservable(**item) for item in raw)


def _load_provider(fixture: ExternalBenchmarkFixture, args: argparse.Namespace):
    kind = getattr(args, 'provider', 'analytic-direct')
    if kind == 'analytic-direct':
        c = getattr(args, 'speed_of_sound', None)
        return AnalyticDirectPathProvider(speed_of_sound_m_per_s=c)
    if kind == 'static-replay':
        if not getattr(args, 'predictions', None):
            raise SystemExit(
                'static-replay provider requires --predictions <json>'
            )
        predictions = _load_json(args.predictions)
        if not isinstance(predictions, dict):
            raise SystemExit('--predictions must be a JSON object')
        return StaticFixtureReplayProvider(predictions)
    raise SystemExit(f'unknown provider {kind!r}')


def _imported(fixture, dataset, scene, args):
    return import_fixture_case(
        fixture,
        dataset,
        scene,
        args.corpus_root,
        rir_member_path=getattr(args, 'rir_member', None),
    )


def cmd_verify(args: argparse.Namespace) -> int:
    fixture = _load_fixture(args.fixture)
    dataset, _ = _resolve_dataset_scene(fixture, args)
    receipts = verify_fixture_payloads(
        args.corpus_root, fixture, dataset
    )
    report = [
        {
            'file_name': r.file_name,
            'member_path': r.member_path,
            'role': r.role,
            'verdict': r.verdict,
            'size_bytes': r.size_bytes,
        }
        for r in receipts
    ]
    print(json.dumps(report, indent=2, sort_keys=True))
    consumed = {(p.file_name, p.member_path) for p in fixture.consumed_files}
    failed = [
        r
        for r in receipts
        if (r.file_name, r.member_path) in consumed
        and r.verdict != 'verified'
    ]
    return 2 if failed else 0


def cmd_import(args: argparse.Namespace) -> int:
    fixture = _load_fixture(args.fixture)
    dataset, scene = _resolve_dataset_scene(fixture, args)
    imported = _imported(fixture, dataset, scene, args)
    out = {
        'case': imported.case.model_dump(mode='json'),
        'measurement': imported.measurement.model_dump(mode='json'),
        'fixture_sha256': imported.fixture_sha256,
        'receipts': [r.model_dump(mode='json') for r in imported.receipts],
    }
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
        print(f'wrote {args.out}')
    else:
        print(text)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    fixture = _load_fixture(args.fixture)
    provider = _load_provider(fixture, args)
    profile = EvaluationProfile(
        profile_id=args.profile_id, version=args.profile_version
    )
    spec = build_fixture_run_spec(
        spec_id=args.spec_id,
        fixture=fixture,
        provider=provider,
        evaluation_profile=profile,
        run_mode=args.run_mode,
        informed_parameters=tuple(args.informed_parameter or ()),
        prior_unfitted_evidence_sha256=args.prior_unfitted_evidence,
        solver_path=args.solver_path,
        solver_revision=args.solver_revision,
        mesh_resolution=args.mesh_resolution,
        seed=args.seed,
    )
    text = spec.model_dump_json(indent=2)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
        print(f'wrote {args.out}')
    else:
        print(text)
    return 0


def _run(args: argparse.Namespace):
    fixture = _load_fixture(args.fixture)
    dataset, scene = _resolve_dataset_scene(fixture, args)
    spec = FixtureRunSpec.model_validate_json(
        Path(args.spec).read_text(encoding='utf-8')
    )
    if spec.fixture_ref.ref_sha256 != fixture.fixture_sha256:
        raise FixtureIntegrityError(
            'spec pins a different fixture revision than --fixture'
        )
    provider = _load_provider(fixture, args)
    profile = EvaluationProfile(
        profile_id=spec.evaluation_profile_id,
        version=spec.evaluation_profile_version,
    )
    imported = _imported(fixture, dataset, scene, args)
    observables = _load_observables(args)
    if not observables:
        raise SystemExit('run requires --observables <json list>')
    return fixture, imported, observables, provider, spec, profile


def cmd_run(args: argparse.Namespace) -> int:
    fixture, imported, observables, provider, spec, profile = _run(args)
    evidence = run_fixture(
        fixture, imported, observables, provider, spec, profile
    )
    text = evidence.model_dump_json(indent=2)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
        print(f'wrote {args.out}')
    else:
        print(text)
    summary = {
        'status': evidence.status,
        'verdict_counts': evidence.verdict_counts,
        'evidence_sha256': evidence.evidence_sha256,
    }
    print(json.dumps(summary, indent=2, sort_keys=True), file=sys.stderr)
    return 0 if evidence.status == 'pass' else 3


def cmd_replay(args: argparse.Namespace) -> int:
    fixture, imported, observables, provider, spec, profile = _run(args)
    prior = None
    if getattr(args, 'evidence', None):
        prior = FixtureRunEvidence.model_validate_json(
            Path(args.evidence).read_text(encoding='utf-8')
        )
    report = replay_fixture(
        fixture, imported, observables, provider, spec, prior, profile
    )
    print(report.model_dump_json(indent=2))
    return 0 if report.verdict == 'reproduced' else 4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument('--fixture', required=True, help='fixture JSON')
        p.add_argument(
            '--corpus-root',
            help='corpus root dir (fetch_external_corpus layout)',
        )
        p.add_argument('--dataset', help='override dataset admission JSON')
        p.add_argument('--scene', help='override corpus scene JSON')
        p.add_argument(
            '--rir-member', help='zip member path of the SOFA RIR'
        )

    p_verify = sub.add_parser('verify')
    _common(p_verify)

    p_import = sub.add_parser('import')
    _common(p_import)
    p_import.add_argument('--out')

    p_plan = sub.add_parser('plan')
    _common(p_plan)
    p_plan.add_argument('--spec-id', required=True)
    p_plan.add_argument(
        '--provider',
        default='analytic-direct',
        choices=('analytic-direct', 'static-replay'),
    )
    p_plan.add_argument('--predictions')
    p_plan.add_argument('--speed-of-sound', type=float, default=None)
    p_plan.add_argument('--profile-id', required=True)
    p_plan.add_argument('--profile-version', required=True)
    p_plan.add_argument(
        '--run-mode',
        default='preregistered_unfitted',
        choices=('preregistered_unfitted', 'informed_calibrated'),
    )
    p_plan.add_argument('--informed-parameter', action='append')
    p_plan.add_argument('--prior-unfitted-evidence')
    p_plan.add_argument('--solver-path')
    p_plan.add_argument('--solver-revision')
    p_plan.add_argument('--mesh-resolution')
    p_plan.add_argument('--seed')
    p_plan.add_argument('--out')

    for name in ('run', 'replay'):
        p = sub.add_parser(name)
        _common(p)
        p.add_argument('--spec', required=True)
        p.add_argument('--observables', required=True)
        p.add_argument(
            '--provider',
            default='analytic-direct',
            choices=('analytic-direct', 'static-replay'),
        )
        p.add_argument('--predictions')
        p.add_argument('--speed-of-sound', type=float, default=None)
        p.add_argument('--out')
        if name == 'replay':
            p.add_argument('--evidence')

    args = parser.parse_args()
    if args.command == 'verify':
        return cmd_verify(args)
    if args.command == 'import':
        return cmd_import(args)
    if args.command == 'plan':
        return cmd_plan(args)
    if args.command == 'run':
        return cmd_run(args)
    if args.command == 'replay':
        return cmd_replay(args)
    return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (FixtureImportError, FixtureIntegrityError) as exc:
        print(f'fail-closed: {exc}', file=sys.stderr)
        raise SystemExit(2)
