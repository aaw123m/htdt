"""Mechanical enumeration of htdt's import surface for packaging parity.

Walks backend/src/htdt AST and reports:
  1. _LAZY_* dict literal module targets (module-__getattr__ lazy exports)
  2. importlib.import_module / __import__ call sites and their args
  3. nested (function/class-level) imports of third-party modules
  4. top-level third-party imports (dependency surface)
  5. importlib.metadata.version() queried distribution names
  6. __file__-relative resource paths
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / 'backend' / 'src'
PKG = 'htdt'
STDLIB = set(sys.stdlib_module_names)

lazy_tables: dict[str, dict] = {}
import_module_calls: list[dict] = []
nested_imports: dict[str, set] = {}
top_level_3p: set = set()
metadata_queries: set = set()
file_rel_paths: list[str] = []


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f'{base}.{node.attr}' if base else node.attr
    return None


def scan(path: Path):
    rel = path.relative_to(ROOT)
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(rel))

    for node in ast.walk(tree):
        # _LAZY_* dicts
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names and names[0].startswith('_LAZY'):
                table = {}
                for k, v in zip(node.value.keys, node.value.values):
                    key = _const_str(k)
                    mod, attr = None, None
                    if isinstance(v, (ast.Tuple, ast.List)) and len(v.elts) >= 1:
                        mod = _const_str(v.elts[0])
                        attr = _const_str(v.elts[1]) if len(v.elts) > 1 else None
                    elif isinstance(v, ast.Constant):
                        mod = v.value
                    table[key] = (mod, attr)
                lazy_tables[f'{rel}:{names[0]}'] = table

        # import_module / __import__ calls
        if isinstance(node, ast.Call):
            fname = _dotted(node.func)
            if fname and fname.endswith(('import_module', '__import__')):
                arg = _const_str(node.args[0]) if node.args else None
                import_module_calls.append({
                    'file': str(rel), 'line': node.lineno,
                    'call': fname, 'arg': arg,
                })
            # importlib.metadata.version('x') / distribution_version('x')
            if fname and fname.endswith(('metadata.version', 'distribution_version')):
                arg = _const_str(node.args[0]) if node.args else None
                if arg:
                    metadata_queries.add(arg)

        # __file__ references
        if isinstance(node, ast.Name) and node.id == '__file__':
            file_rel_paths.append(f'{rel}:{node.lineno}')

    # import depth analysis
    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.depth = 0

        def _visit_scope(self, n):
            self.depth += 1
            self.generic_visit(n)
            self.depth -= 1

        visit_FunctionDef = _visit_scope
        visit_AsyncFunctionDef = _visit_scope
        visit_ClassDef = _visit_scope
        visit_Lambda = _visit_scope

        def _record(self, modname, lineno):
            top = modname.split('.')[0]
            if top == PKG or top in STDLIB or not top:
                return
            if self.depth > 0:
                nested_imports.setdefault(top, set()).add(f'{rel}:{lineno}')
            else:
                top_level_3p.add(top)

        def visit_Import(self, n):
            for a in n.names:
                self._record(a.name, n.lineno)

        def visit_ImportFrom(self, n):
            if n.level == 0 and n.module:
                self._record(n.module, n.lineno)

    Visitor().visit(tree)


for f in sorted(ROOT.rglob('*.py')):
    scan(f)

# resolve lazy table module targets
unresolved = []
external_lazy = {}
for table_name, table in lazy_tables.items():
    pkg_dir = table_name.split(':')[0]
    pkg_file = ROOT / pkg_dir
    for key, (mod, attr) in table.items():
        if mod is None:
            unresolved.append((table_name, key, 'non-literal'))
            continue
        if mod.startswith('.'):
            # relative to the package containing the file
            base = pkg_file.parent
            rel_parts = mod.lstrip('.')
            target = base / rel_parts.replace('.', '/')
            if not (target.with_suffix('.py').exists() or (target / '__init__.py').exists()):
                unresolved.append((table_name, key, f'{mod} -> missing file'))
        else:
            external_lazy.setdefault(mod.split('.')[0], set()).add(f'{table_name}:{key}')

non_literal_calls = [c for c in import_module_calls if c['arg'] is None]
literal_calls = {(c['file'], c['line']): c['arg'] for c in import_module_calls if c['arg']}

report = {
    'lazy_tables': {k: len(v) for k, v in lazy_tables.items()},
    'lazy_table_targets_external': {k: sorted(v) for k, v in external_lazy.items()},
    'lazy_table_unresolved': unresolved,
    'import_module_literal_args': sorted(set(literal_calls.values())),
    'import_module_non_literal_calls': non_literal_calls,
    'nested_third_party_imports': {k: sorted(v) for k, v in nested_imports.items()},
    'top_level_third_party': sorted(top_level_3p),
    'metadata_version_queries': sorted(metadata_queries),
    'file_dunder_refs': file_rel_paths,
}
print(json.dumps(report, indent=2))
