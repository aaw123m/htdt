"""#807 package-boundary audit — inventory, layer rules, cycles, size budget.

Static (AST) analysis of ``backend/src/htdt``. Produces a machine-readable
report (JSON) and a markdown table; ``test_issue_807_boundaries`` enforces
the rules that must hold on every commit.

Layers (assigned by ``classify``):

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

Exemptions live in ``EXEMPTIONS`` — each entry needs a justification; the
test fails if an exemption becomes unused (the violation went away).
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent / 'backend' / 'src' / 'htdt'

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
    'capture_ingestion_transaction': 'transaction pipeline; stage modules',
    'workflow_application': 'composition root; keep wiring, move widgets',
}


def iter_modules(root: Path) -> list[Path]:
    return sorted(p for p in root.glob('*.py') if p.name != '__init__.py')


def module_imports(path: Path) -> tuple[frozenset[str], bool]:
    """(local htdt imports, imports PySide6 at any level)."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    local: set[str] = set()
    qt = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith('PySide6'):
                    qt = True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith('PySide6'):
                qt = True
            if node.level and node.module:
                local.add(node.module.split('.')[-1])
            elif node.level and not node.module:
                for alias in node.names:
                    local.add(alias.name)
    return frozenset(local), qt


def classify(name: str, imports_qt: bool) -> str:
    if name in KERNEL_NAMES:
        return 'kernel'
    if name in APPLICATION_NAMES:
        return 'application'
    if (name.endswith('_repository') or name in PERSISTENCE_NAMES
            or name.startswith(PERSISTENCE_PREFIXES)):
        return 'persistence'
    if imports_qt or name in EXEMPTIONS:
        return 'ui' if imports_qt else 'domain'
    return 'domain'


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
KNOWN_CYCLES: frozenset[tuple[str, ...]] = frozenset({
    ('dirty_state_dialog', 'workflow_shell'),
    ('project_bundle', 'project_library_repository'),
    ('cad_project_template', 'project_setup_intent'),
})


def audit(root: Path = ROOT) -> dict:
    files = iter_modules(root)
    graph: dict[str, frozenset[str]] = {}
    layers: dict[str, str] = {}
    qt_imports: dict[str, bool] = {}
    sizes: dict[str, int] = {}
    for path in files:
        name = path.stem
        imports, qt = module_imports(path)
        graph[name] = imports
        qt_imports[name] = qt
        layers[name] = classify(name, qt)
        sizes[name] = sum(1 for _ in path.open(encoding='utf-8'))

    violations: list[dict[str, str]] = []
    for name, layer in layers.items():
        if layer in ('domain', 'persistence', 'kernel') and qt_imports[name]:
            if name in EXEMPTIONS and 'imports_qt' in EXEMPTIONS[name]:
                continue
            violations.append({
                'module': name, 'rule': 'no_qt_in_lower_layers',
                'detail': f'{layer} module imports PySide6',
            })
        if layer == 'persistence':
            for dep in graph[name]:
                if dep in layers and layers[dep] in ('ui', 'application'):
                    violations.append({
                        'module': name,
                        'rule': 'persistence_must_not_import_ui',
                        'detail': f'imports {dep} ({layers[dep]})',
                    })
        if layer == 'kernel':
            for dep in graph[name]:
                if dep in layers and layers[dep] != 'kernel':
                    violations.append({
                        'module': name,
                        'rule': 'kernel_imports_only_kernel',
                        'detail': f'imports {dep} ({layers[dep]})',
                    })

    cycles = find_cycles(graph)
    cycle_violations: list[dict[str, str]] = []
    for component in cycles:
        if not _touches_upper_layer(tuple(component), layers):
            # Intra-authority/persistence SCCs: recorded debt (#807), not
            # enforced — splitting the blob is the migration plan.
            continue
        if tuple(component) in KNOWN_CYCLES:
            continue
        cycle_violations.append({
            'module': ', '.join(component),
            'rule': 'no_new_cross_layer_cycles',
            'detail': 'import cycle outside the authority blob',
        })
    violations.extend(cycle_violations)

    oversized = {
        name: n for name, n in sizes.items()
        if n > SIZE_BUDGET_LINES
    }
    over_budget_new = {
        n: s for n, s in oversized.items() if n not in SIZE_EXEMPTIONS
    }

    layer_counts: dict[str, int] = {}
    for layer in layers.values():
        layer_counts[layer] = layer_counts.get(layer, 0) + 1

    return {
        'module_count': len(files),
        'layer_counts': layer_counts,
        'layers': layers,
        'qt_import_modules': sorted(n for n, q in qt_imports.items() if q),
        'violations': violations,
        'cycles': [list(c) for c in cycles],
        'cycles_touching_ui_or_application': [
            list(c) for c in cycles
            if _touches_upper_layer(tuple(c), layers)
        ],
        'oversized': oversized,
        'over_budget_unexempted': over_budget_new,
        'size_budget_lines': SIZE_BUDGET_LINES,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', type=Path, default=None)
    parser.add_argument('--markdown', type=Path, default=None)
    args = parser.parse_args()
    report = audit()
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        args.json.write_text(text, encoding='utf-8')
    else:
        print(text)
    if args.markdown:
        lines = ['# htdt package-boundary audit', '',
                 f"modules: {report['module_count']}", '',
                 '| layer | modules |', '|---|---|']
        for layer, count in sorted(report['layer_counts'].items()):
            lines.append(f'| {layer} | {count} |')
        lines += ['', '## Violations', '']
        for v in report['violations']:
            lines.append(f"- `{v['module']}` {v['rule']}: {v['detail']}")
        lines += ['', '## Import cycles', '']
        for c in report['cycles']:
            lines.append(f"- {' ↔ '.join(c)}")
        lines += ['', f"## Modules over {report['size_budget_lines']} lines", '']
        for name, size in sorted(
                report['oversized'].items(), key=lambda kv: -kv[1]):
            marker = '' if name in SIZE_EXEMPTIONS else ' **UNEXEMPTED**'
            lines.append(f'- `{name}`: {size}{marker}')
        args.markdown.write_text('\n'.join(lines), encoding='utf-8')
    return 1 if (report['violations']
                 or report['over_budget_unexempted']) else 0


if __name__ == '__main__':
    sys.exit(main())
