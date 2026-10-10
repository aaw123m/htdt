"""#807/#954 package-boundary audit — inventory, layer rules, cycles, budget.

Static (AST) analysis of ``backend/src/htdt``. Produces a machine-readable
report (JSON) and a markdown table; ``test_issue_807_boundaries`` enforces
the rules that must hold on every commit and ``test_issue_954_domain_split``
pins the migrated package's direction + the managed-debt inventory.

Dependency graph (real, resolved)
---------------------------------
Nodes are dotted module ids relative to ``backend/src/htdt``
(``cad_repository``; ``measurement.domain.cad_measurement_quality``) plus
package ``__init__`` ids (``measurement.domain``). Edges resolve every
import form — ``from .x import``, ``from ..y.z import``, ``from . import x``,
``from htdt.x import``, ``import htdt.x`` — to the module node it lands on
(longest-prefix against the module index). Compat shims (flat ``htdt.X``
modules left by #954 moves) are marked and their single forwarding edge is
not a layering event.

Layers (assigned by ``classify``, original #807 vocabulary):

- ``kernel`` — schema DDL, canonical-json, repository base: importable by
  all layers, may only import kernel.
- ``persistence`` — ``*_repository.py``, ``native_*`` integrity/audit/
  schema/migration/backup/journal modules. May import kernel + domain.
  Must never import Qt or UI.
- ``domain`` — default for everything else that does not import PySide6
  (``cad_*`` authorities, solvers, acoustic models, services without Qt).
  Must never import Qt.
- ``ui`` — modules that import PySide6 (panels, pages, workspaces,
  dialogs) plus declared UI-named modules that don't today.
- ``application`` — composition roots / process entry: ``native_cad``,
  ``native_worker``, ``native_diagnostics``, ``workflow_application``,
  ``application_composition``. May import anything; wiring lives here.

Declared packages (#954) live in ``PACKAGE_LAYERS``: each member module is
assigned to a package layer (``domain``/``services``/``persistence``/``ui``)
and the package documents one import direction — for ``htdt.measurement``:

    ui -> services -> persistence -> domain

Checked by rule ``package_import_direction``: an edge importer -> target is
a violation when ``rank(layer(importer)) < rank(target)`` where flat
(unpackaged) targets map to ranks kernel=-1, domain=0, persistence=1,
ui=3, application=4 — so a moved domain module reaching a flat repository,
or a moved service reaching a flat Qt module, is recorded debt.

Exemptions live in ``EXEMPTIONS`` — each entry needs a justification; the
test fails if an exemption becomes unused (the violation went away).

Baseline vs new-diff mode
-------------------------
``--write-baseline PATH`` snapshots the current violation/cycle/oversize
set into a JSON inventory (managed debt — checked in as
``scripts/package_boundary_inventory.json``). ``--diff PATH`` audits the
tree and exits non-zero on anything *not* in the baseline: new rule
violations, new import cycles (an SCC with no baseline home — blob-internal
growth is reported as ``grown_cycles`` debt, merging baseline SCCs is
``merged_cycle`` and fails), and new size-budget breaches. Baseline entries
that no longer reproduce are reported under ``resolved`` — regenerate the
baseline (a visible debt-reduction diff) rather than leaving stale entries.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent / 'backend' / 'src' / 'htdt'
INVENTORY = Path(__file__).resolve().parent / 'package_boundary_inventory.json'

SIZE_BUDGET_LINES = 5000
"""New modules must stay under this line count; existing giants are
grandfathered via ``SIZE_EXEMPTIONS`` with a decomposition note."""

KERNEL_NAMES = frozenset({
    'cad_schema_ddl',
    'canonical_json',
})
"""True leaves: zero htdt imports, shared by every layer."""

PERSISTENCE_NAMES = frozenset({
    'cad_schema',
    'cad_repository',
    'cad_authority_resolver',
})
"""Schema/connection/resolver infrastructure — persistence-adjacent,
allowed to pull domain modules it stores or resolves."""

PERSISTENCE_PREFIXES = (
    'native_schema',
    'native_row_integrity',
    'native_authority_audit',
    'native_backup',
    'native_journal',
    'native_migration',
    'migration_',
    'backup_',
)

APPLICATION_NAMES = frozenset({
    'native_cad',
    'native_worker',
    'native_diagnostics',
    'workflow_application',
    'application_composition',
    'application_pages',
})

#: #954 — declared packages and their layer membership. Each package owns a
#: documented direction (``measurement``: ui -> services -> persistence ->
#: domain); membership is by module *stem* (the flat shim keeps the old
#: ``htdt.<stem>`` path alive and is skipped by edge checks).
PACKAGE_LAYERS: dict[str, dict[str, tuple[str, ...]]] = {
    'measurement': {
        'domain': (
            'cad_adaptive_measurement_design', 'cad_impedance_measurement',
            'cad_measurement_authorities', 'cad_measurement_disposition',
            'cad_measurement_ir', 'cad_measurement_jobs',
            'cad_measurement_models', 'cad_measurement_plan',
            'cad_measurement_pose',
            'cad_measurement_quality', 'cad_measurement_runner',
            'cad_measurement_session', 'cad_measurement_state',
            'cad_measurement_stimulus', 'cad_measurement_target_pattern',
            'cad_measurement_targets', 'cad_measurement_transform',
            'cad_measurement_uncertainty', 'cad_measurements',
            'cad_moving_mic_measurement',
            'cad_prediction_measurement_registration',
            'cad_remeasure_queue',
            'cad_spatial_ir_measurement', 'cad_spatial_ir_metrics',
            'measurement_analysis', 'measurement_evidence_display',
            'measurement_instrument_onboarding', 'measurement_journey',
            'measurement_playback_safety',
        ),
        'services': (
            'cad_measurement_loop',
            'cad_measurement_quality_producer',
            'cad_prediction_measurement_service',
            'cad_remeasure_queue_service',
            'cad_system_variant_measurement_campaign',
            'measurement_target_service', 'measurement_workflow',
        ),
        'persistence': (
            'cad_measurement_evidence_repository',
            'cad_measurement_effective',
            'cad_measurement_pose_observation_repository',
            'cad_measurement_quality_repository',
            'cad_measurement_repository',
            'cad_measurement_runner_repository',
            'cad_measurement_setup_repository',
            'cad_target_pattern_repository',
            'cad_prediction_measurement_registration_repository',
            'cad_remeasure_queue_repository',
        ),
        'ui': (
            'measurement_authority_dialogs', 'measurement_explanations',
            'measurement_page_workspace', 'measurement_record_surfaces',
            'optimization_measurement_controller',
            'reflection_correspondence_panel',
        ),
    },
    'capture': {
        'domain': (
            'capture_binary_formats', 'capture_bundle',
            'capture_compatibility', 'capture_mesh_ingestion',
            'capture_mission', 'capture_plan_schema', 'capture_reference',
            'capture_schema_eval', 'capture_watch_failures',
            'capture_watch_guard',
        ),
        'services': (
            'capture_authoring', 'capture_connected_space',
            'capture_entity_promotion', 'capture_import',
            'capture_receiver', 'capture_retention',
        ),
        'persistence': (
            'capture_inbox', 'capture_ingestion_transaction',
            'capture_semantic_promotion',
        ),
        'ui': (
            'capture_receiver_controller', 'capture_receiver_settings',
            'capture_retention_ui', 'capture_watch_runner',
        ),
    },
    'calibration': {
        'domain': (
            'cad_calibration', 'cad_calibration_deployment',
            'cad_calibration_lifecycle', 'cad_external_calibration',
            'cad_mic_response_calibration', 'cad_model_calibration',
        ),
        'services': (
            'cad_calibration_wizard', 'cad_calibration_workflow',
        ),
        'persistence': (
            'cad_calibration_deployment_repository',
            'cad_calibration_lifecycle_repository',
            'cad_calibration_repository',
            'cad_calibration_wizard_repository',
            'cad_calibration_workflow_repository',
            'cad_model_calibration_repository',
        ),
    },
}
PACKAGE_DIRECTION = {
    'measurement': 'ui -> services -> persistence -> domain',
    'capture': 'ui -> services -> persistence -> domain',
    'calibration': 'ui -> services -> persistence -> domain',
}
"""Documented import direction per package."""

_PACKAGE_RANK = {'domain': 0, 'persistence': 1, 'services': 2, 'ui': 3}
"""Legal downward direction for a declared package's layers."""

_FLAT_TARGET_RANK = {
    'kernel': -1, 'domain': 0, 'persistence': 1, 'ui': 3, 'application': 4,
}
"""Rank a flat (unpackaged) target takes when a packaged module imports it.
Mirrors the package direction — kernel below domain, application above ui —
so e.g. measurement.domain -> htdt.cad_repository (persistence) and
measurement.services -> htdt.user_facing_error (ui) are recorded debt."""

#: stem -> (package, layer) for every moved module; the flat shim file
#: keeping the same stem is transparent to edge rules.
def _moved_index() -> dict[str, tuple[str, str]]:
    idx: dict[str, tuple[str, str]] = {}
    for pkg, layers in PACKAGE_LAYERS.items():
        for layer, stems in layers.items():
            for stem in stems:
                idx[stem] = (pkg, layer)
    return idx


MOVED = _moved_index()

#: ``module`` -> (rule, justification). Every entry must name a follow-up.
EXEMPTIONS: dict[str, dict[str, str]] = {
    'cad_input': {
        'imports_qt': (
            'named like a domain module but is a QWidget canvas; rename to '
            '*_canvas under the #807 decomposition plan'
        ),
    },
}

SIZE_EXEMPTIONS: dict[str, str] = {
    'native_row_integrity': 'row-binding ledger; decompose per domain package',
    'cad_schema_ddl': 'generated-style DDL registry; stays flat by design',
    'native_authority_audit': 'probe/policy registry; decompose per domain',
    'cad_geometric_acoustics_adapter': 'split solver-facing vs UI-facing halves',
    'room_workspace': 'workspace composition; extract per-stage controllers',
    'measurement_page_workspace': '#815 catch-boundary cleanup precedes split',
    'measurement_evidence_display': 'display composition; split per panel',
    'workflow_application': 'composition root; keep wiring, move widgets',
    'application_pages': 'page composition root; extract per-destination panels',
    'room_viewport': '3D viewport; split overlays from the canvas core',
}
"""Keyed by module *stem* — survives moves into declared packages."""


def iter_module_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob('*.py')
        if not (p.name == '__init__.py' and p.parent == root))


def module_id(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix('')
    parts = list(rel.parts)
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


def module_imports(path: Path, rel_id: str) -> tuple[frozenset[str], bool]:
    """(resolved htdt module ids, imports PySide6 at any level).

    ``rel_id`` is the importing module's own dotted id relative to the
    htdt root — relative imports resolve against its package part.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'))
    local: set[str] = set()
    qt = False
    pkg_parts = rel_id.split('.')[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith('PySide6'):
                    qt = True
                elif alias.name == 'htdt' or alias.name.startswith('htdt.'):
                    local.add(alias.name[5:] or '__init__less-root')
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith('PySide6'):
                qt = True
            if node.level:
                # 'from <level dots><mod> import <names>' — level-1 dots mean
                # "this package", each extra dot climbs one parent.
                base = pkg_parts[:len(pkg_parts) - (node.level - 1)]
                if node.module:
                    base = base + node.module.split('.')
                for alias in node.names:
                    local.add('.'.join(base + [alias.name]))
            elif node.module == 'htdt':
                for alias in node.names:
                    local.add(alias.name)
            elif node.module and node.module.startswith('htdt.'):
                if node.names and node.names[0].name == '*':
                    local.add(node.module[5:])
                else:
                    # 'from htdt.a.b import c' — 'a.b.c' resolves by
                    # longest-prefix to module 'a.b.c' or attr 'c' of 'a.b'.
                    for alias in node.names:
                        local.add(node.module[5:] + '.' + alias.name)
    return frozenset(local), qt


def resolve_targets(raw: frozenset[str], index: set[str]) -> frozenset[str]:
    """Keep only ids that resolve to a real module/package node — imports of
    *names* from a module (from .x import Func) surface as 'x.Func' here and
    drop to 'x' via longest-prefix matching."""
    out: set[str] = set()
    for t in raw:
        cand = t
        while cand and cand not in index:
            cand = cand.rpartition('.')[0]
        if cand:
            out.add(cand)
    return frozenset(out)


def classify(node_id: str, stem: str, imports_qt: bool) -> str:
    if stem in KERNEL_NAMES:
        return 'kernel'
    if stem in APPLICATION_NAMES:
        return 'application'
    if (stem.endswith('_repository') or stem in PERSISTENCE_NAMES
            or stem.startswith(PERSISTENCE_PREFIXES)):
        return 'persistence'
    if imports_qt or stem in EXEMPTIONS:
        return 'ui' if imports_qt else 'domain'
    return 'domain'


def is_shim(node_id: str) -> bool:
    """Flat ``htdt.<stem>`` compat forwarder for a moved module."""
    return '.' not in node_id and node_id in MOVED


def package_of(node_id: str) -> str | None:
    top = node_id.split('.')[0]
    return top if top in PACKAGE_LAYERS else None


def package_layer(node_id: str) -> str | None:
    pkg = package_of(node_id)
    if pkg is None:
        return None
    stem = node_id.split('.')[-1]
    return MOVED.get(stem, (None, None))[1]


def find_cycles(graph: dict[str, frozenset[str]]) -> list[tuple[str, ...]]:
    """Tarjan SCC — return components of size > 1."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = [0]
    result: list[tuple[str, ...]] = []

    def strongconnect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in graph:
                continue
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                result.append(tuple(sorted(component)))

    for node in graph:
        if node not in index:
            strongconnect(node)
    return result


def _touches_upper_layer(component: tuple[str, ...], layers: dict[str, str]) -> bool:
    """Cycles are enforced only where they cross into Qt/UI/application
    code — the flat cad_*/authority namespace is one giant SCC today
    (#807's problem statement) and is recorded as debt, not blocked."""
    return any(layers.get(m) in ('ui', 'application') for m in component)


#: Known small cycles — debt recorded for the #807 decomposition plan;
#: each must shrink or be split, but none may grow members.
# All three previously pinned pairs were resolved by the #807 refactor slice:
# dirty_state_dialog takes a structural _MountLike Protocol (shell keeps its
# lazy import), project_bundle reads the htdt_project_documents row directly,
# and the template-brief materializers moved into cad_project_template.
KNOWN_CYCLES: frozenset[tuple[str, ...]] = frozenset()


def _violation(rule: str, module: str, detail: str) -> dict[str, str]:
    return {'rule': rule, 'module': module, 'detail': detail}


def audit(root: Path = ROOT) -> dict:
    files = iter_module_files(root)
    index: set[str] = set()
    raw: dict[str, frozenset[str]] = {}
    qt_imports: dict[str, bool] = {}
    sizes: dict[str, int] = {}
    paths: dict[str, Path] = {}
    for path in files:
        node = module_id(root, path)
        index.add(node)
        imports, qt = module_imports(path, node)
        raw[node] = imports
        qt_imports[node] = qt
        paths[node] = path
        sizes[node] = sum(1 for _ in path.open(encoding='utf-8'))

    graph: dict[str, frozenset[str]] = {
        node: resolve_targets(imports, index)
        for node, imports in raw.items()
    }
    # Self-edges (a 'from .x import *' resolving to itself, or a shim's own
    # impl edge that normalizes back) carry no information.
    graph = {n: frozenset(t for t in deps if t != n)
             for n, deps in graph.items()}

    layers: dict[str, str] = {}
    package_layers: dict[str, str] = {}
    for node in index:
        stem = node.split('.')[-1]
        if is_shim(node):
            # The shim's layer is its target's: the single forwarding edge is
            # then layer-identical on both ends and can never violate.
            impl = f'{MOVED[stem][0]}.{MOVED[stem][1]}.{stem}'
            layers[node] = classify(impl, stem, qt_imports.get(impl, False))
            package_layers[node] = 'shim'
            continue
        layers[node] = classify(node, stem, qt_imports[node])
        pl = package_layer(node)
        if pl is not None:
            package_layers[node] = f'{package_of(node)}.{pl}'

    violations: list[dict[str, str]] = []
    for name, layer in layers.items():
        if is_shim(name):
            continue
        if layer in ('domain', 'persistence', 'kernel') and qt_imports[name]:
            if name in EXEMPTIONS and 'imports_qt' in EXEMPTIONS[name]:
                continue
            violations.append(_violation(
                'no_qt_in_lower_layers', name,
                f'{layer} module imports PySide6'))
        for dep in graph[name]:
            if dep not in layers:
                continue
            if layer == 'persistence' and layers[dep] in ('ui', 'application'):
                violations.append(_violation(
                    'persistence_must_not_import_ui', name,
                    f'imports {dep} ({layers[dep]})'))
            if layer == 'kernel' and layers[dep] != 'kernel':
                violations.append(_violation(
                    'kernel_imports_only_kernel', name,
                    f'imports {dep} ({layers[dep]})'))
            if (layer in ('domain', 'persistence', 'kernel')
                    and layers[dep] in ('ui', 'application')
                    and package_layer(name) is None):
                # UI/application reverse dependency — the edge form of
                # "domain models must not depend on Qt/UI" (#954 enforces it
                # at edge level; the direct-import variant above only sees
                # PySide6 itself). A compat-shim target takes its impl's
                # layer, so reaching a shimmed UI module still counts.
                if layer != 'persistence':  # persistence covered above
                    violations.append(_violation(
                        'lower_layer_imports_ui', name,
                        f'{layer} module imports {dep} ({layers[dep]})'))

        # Declared-package direction (#954): a packaged module may only
        # reach same-or-lower ranks, packaged or flat.
        pkg = package_of(name)
        if pkg is not None and package_layer(name) is not None:
            my_rank = _PACKAGE_RANK[package_layer(name)]
            for dep in graph[name]:
                if dep not in layers:
                    continue
                dep_pl = package_layer(dep)
                if dep_pl is not None:
                    dep_rank = _PACKAGE_RANK[dep_pl]
                    dep_desc = f'{package_of(dep)}.{dep_pl}'
                else:
                    dep_rank = _FLAT_TARGET_RANK[layers[dep]]
                    dep_desc = f'flat {layers[dep]}'
                if my_rank < dep_rank:
                    violations.append(_violation(
                        'package_import_direction', name,
                        f'{package_of(name)}.{package_layer(name)} module '
                        f'imports {dep} ({dep_desc})'))

    cycles = find_cycles(graph)
    for component in cycles:
        if not _touches_upper_layer(tuple(component), layers):
            continue
        if tuple(component) in KNOWN_CYCLES:
            continue
        violations.append(_violation(
            'no_new_cross_layer_cycles', ', '.join(component),
            'import cycle outside the authority blob'))

    oversized = {
        name: n for name, n in sizes.items()
        if n > SIZE_BUDGET_LINES
    }
    over_budget_new = {
        n: s for n, s in oversized.items()
        if n.split('.')[-1] not in SIZE_EXEMPTIONS
    }

    layer_counts: dict[str, int] = {}
    for node, layer in layers.items():
        if '.' in node and not package_of(node) and node.endswith('__init__'):
            continue
        layer_counts[layer] = layer_counts.get(layer, 0) + 1

    # Informational coupling sections ------------------------------------
    qt_domain_crossings = {
        'ui_to_domain': sorted(
            f'{n} -> {d}' for n in graph for d in graph[n]
            if d in layers and layers[n] == 'ui' and layers[d] == 'domain'),
        'lower_to_ui': sorted(
            f'{n} ({layers[n]}) -> {d} ({layers[d]})'
            for n in graph for d in graph[n]
            if d in layers and layers[n] in ('domain', 'persistence', 'kernel')
            and layers[d] in ('ui', 'application') and not is_shim(n)),
    }
    persistence_infra = PERSISTENCE_NAMES | {
        'native_schema', 'native_row_integrity', 'native_authority_audit',
        'native_backup', 'native_journal', 'managed_assets',
    }
    schema_coupling = sorted(
        f'{n} ({layers[n]}) -> {d}'
        for n in graph for d in graph[n]
        if d in layers and layers[n] not in ('persistence', 'kernel', 'ui',
                                           'application')
        and dep_is_infra(d, layers, persistence_infra))

    return {
        'module_count': sum(
            1 for n in index if not n.endswith('__init__')),
        'edge_count': sum(len(d) for d in graph.values()),
        'layer_counts': layer_counts,
        'layers': layers,
        'package_layers': package_layers,
        'packages': {
            pkg: {layer: sorted(mods) for layer, mods in layers_map.items()}
            for pkg, layers_map in PACKAGE_LAYERS.items()
        },
        'qt_import_modules': sorted(n for n, q in qt_imports.items() if q),
        'qt_domain_crossings': qt_domain_crossings,
        'persistence_coupling': schema_coupling,
        'violations': violations,
        'cycles': [list(c) for c in cycles],
        'cycles_touching_ui_or_application': [
            list(c) for c in cycles
            if _touches_upper_layer(tuple(c), layers)
        ],
        'oversized': oversized,
        'over_budget_unexempted': over_budget_new,
        'size_budget_lines': SIZE_BUDGET_LINES,
        'graph': {n: sorted(d) for n, d in graph.items()},
    }


def dep_is_infra(dep: str, layers: dict[str, str], infra: set[str]) -> bool:
    stem = dep.split('.')[-1]
    return stem in infra or (dep in layers and layers[dep] == 'persistence')


# Baseline / managed-debt machinery ---------------------------------------

def violation_key(v: dict[str, str]) -> str:
    return f"{v['rule']}|{v['module']}|{v['detail']}"


def write_baseline(report: dict, path: Path) -> None:
    payload = {
        'kind': 'package-boundary-debt-inventory',
        'generated_from': 'scripts/package_boundary_audit.py --write-baseline',
        'note': (
            'Managed debt for the htdt domain split (#807/#954). Entries '
            'here are pre-existing violations recorded, not approved — '
            'regenerate after resolving debt so the inventory ratchets down.'
        ),
        'violations': sorted(violation_key(v) for v in report['violations']),
        'cycles': [sorted(c) for c in report['cycles']],
        'oversized': report['oversized'],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')


def diff_against_baseline(report: dict, baseline_path: Path) -> dict:
    base = json.loads(baseline_path.read_text(encoding='utf-8'))
    base_viol = set(base['violations'])
    now_viol = {violation_key(v): v for v in report['violations']}

    new_violations = sorted(
        (v for k, v in now_viol.items() if k not in base_viol),
        key=violation_key)
    resolved = sorted(k for k in base_viol if k not in now_viol)

    base_cycles = [set(c) for c in base['cycles']]
    new_cycles, merged_cycles, grown_cycles = [], [], []
    for c in report['cycles']:
        members = set(c)
        homes = [b for b in base_cycles if members & b]
        if not homes:
            new_cycles.append(sorted(c))
        elif any(members - b for b in homes) and len(homes) >= 1:
            if len(homes) > 1:
                merged_cycles.append(sorted(c))
            elif not members <= homes[0]:
                grown_cycles.append(sorted(c))

    base_over = set(base['oversized'])
    new_oversized = {
        n: s for n, s in report['over_budget_unexempted'].items()
        if n not in base_over
    }

    return {
        'new_violations': new_violations,
        'resolved': resolved,
        'new_cycles': new_cycles,
        'merged_cycles': merged_cycles,
        'grown_cycles': grown_cycles,
        'new_oversized': new_oversized,
        'debt_count': len([k for k in now_viol if k in base_viol]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', type=Path, default=None)
    parser.add_argument('--markdown', type=Path, default=None)
    parser.add_argument('--write-baseline', type=Path, default=None,
                        help='record the current violation set as the '
                             'managed-debt inventory')
    parser.add_argument('--diff', type=Path, default=None,
                        help='fail on violations/cycles/oversize not '
                             'present in the given baseline inventory')
    args = parser.parse_args()
    report = audit()
    text = json.dumps(
        {k: v for k, v in report.items() if k != 'graph'},
        indent=2, sort_keys=True)
    if args.json:
        args.json.write_text(text, encoding='utf-8')
    else:
        print(text)
    if args.markdown:
        lines = ['# htdt package-boundary audit', '',
                 f"modules: {report['module_count']}",
                 f"edges: {report['edge_count']}", '',
                 '| layer | modules |', '|---|---|']
        for layer, count in sorted(report['layer_counts'].items()):
            lines.append(f'| {layer} | {count} |')
        for pkg, pkg_layers in sorted(report['packages'].items()):
            lines += ['', f'## Package `{pkg}` '
                      f"({PACKAGE_DIRECTION.get(pkg, '')})", '']
            for layer, mods in sorted(pkg_layers.items()):
                lines.append(f'- **{layer}** ({len(mods)}): '
                             + ', '.join(f'`{m}`' for m in mods))
        lines += ['', '## Violations', '']
        for v in report['violations']:
            lines.append(f"- `{v['module']}` {v['rule']}: {v['detail']}")
        lines += ['', '## Qt/domain crossings', '']
        lines.append(f"ui -> domain edges: "
                     f"{len(report['qt_domain_crossings']['ui_to_domain'])}")
        for e in report['qt_domain_crossings']['lower_to_ui']:
            lines.append(f'- reverse: {e}')
        lines += ['', '## Persistence/schema coupling', '']
        for e in report['persistence_coupling']:
            lines.append(f'- {e}')
        lines += ['', '## Import cycles', '']
        for c in report['cycles']:
            lines.append(f"- {' ↔ '.join(c)}")
        lines += ['', f"## Modules over {report['size_budget_lines']} lines",
                  '']
        for name, size in sorted(
                report['oversized'].items(), key=lambda kv: -kv[1]):
            marker = (
                '' if name.split('.')[-1] in SIZE_EXEMPTIONS
                else ' **UNEXEMPTED**')
            lines.append(f'- `{name}`: {size}{marker}')
        args.markdown.write_text('\n'.join(lines), encoding='utf-8')

    if args.write_baseline:
        write_baseline(report, args.write_baseline)
        print(f'baseline written: {args.write_baseline} '
              f'({len(report["violations"])} violations, '
              f'{len(report["cycles"])} cycles)', file=sys.stderr)

    if args.diff:
        diff = diff_against_baseline(report, args.diff)
        for k, v in diff.items():
            if k == 'debt_count' or not v:
                continue
            print(f'[{k}]', file=sys.stderr)
            entries = (v if isinstance(v, list)
                       else [f'{x}: {y}' for x, y in v.items()])
            for e in entries:
                print(f'  {e}', file=sys.stderr)
        bad = (diff['new_violations'] or diff['new_cycles']
               or diff['merged_cycles'] or diff['new_oversized'])
        if bad:
            print('diff FAILED: new boundary violations present', file=sys.stderr)
            return 1
        print(f'diff clean — {diff["debt_count"]} inventoried violations, '
              f'{len(diff["resolved"])} resolved, '
              f'{len(diff["grown_cycles"])} grown cycles', file=sys.stderr)
        return 0

    return 1 if (report['violations']
                 or report['over_budget_unexempted']) else 0


if __name__ == '__main__':
    sys.exit(main())
