"""REV59-UNITS merge resolver: renumbers my schema contribution on top of
whatever version origin/main just took, splicing my blocks from the
last local merge commit (HEAD, pre-merge) into fresh main checkouts.

Usage: python _rev59_resolve.py <new_version> <prev_migration_from> <prev_migration_to>
e.g.   python _rev59_resolve.py 67 65 66
"""
import io
import subprocess
import sys

NEW_V = int(sys.argv[1])
PREV_FROM = int(sys.argv[2])  # main's last migration from-version (e.g. 65)
PREV_TO = int(sys.argv[3])    # main's last migration to-version (e.g. 66)
SRC = 'HEAD'


def show(rev, path):
    return subprocess.check_output(
        ['git', 'show', f'{rev}:{path}'], text=True,
        encoding='utf-8', errors='replace',
    ).split('\n')


def checkout_main(paths):
    subprocess.check_call(['git', 'checkout', 'origin/main', '--'] + list(paths))


# ---------- extract my blocks from the previous merge commit ----------
M = show(SRC, 'backend/src/htdt/cad_schema_ddl.py')
start = next(i for i, l in enumerate(M)
             if l.strip() == '# REV59-UNITS: #728 typed physical-quantity authority — quantities,')
end = next(i for i, l in enumerate(M) if i > start and l == ')')
ddl_block = '\n'.join(M[start:end])
tstart = next(i for i, l in enumerate(M)
              if l.strip() == '# REV59-UNITS: #728 / #730 / #720 / #719.')
tend = next(i for i, l in enumerate(M) if i > tstart and l.startswith(')'))
tables_block = '\n'.join(M[tstart:tend])
assert ddl_block.count('CREATE TABLE') >= 11 and tables_block.count("'cad_") == 11

MR = show(SRC, 'backend/src/htdt/native_row_integrity.py')
rs = next(i for i, l in enumerate(MR)
          if l.strip() == '# REV59-UNITS: #728 typed physical quantity.')
re_ = next(i for i, l in enumerate(MR) if i > rs and l == '}')
ri_block = '\n'.join(MR[rs:re_])
assert ri_block.count("'cad_") == 11

MA = show(SRC, 'backend/src/htdt/native_authority_audit.py')
fs = next(i for i, l in enumerate(MA) if l.strip() == '# REV59-UNITS authorities.')
fe = next(i for i, l in enumerate(MA) if i > fs and l.strip() == 'raise KeyError(name)')
fac_block = '\n'.join(MA[fs:fe])
ps = next(i for i, l in enumerate(MA) if l.strip() == '# REV59-UNITS: #728 typed physical quantity')
pe = next(i for i, l in enumerate(MA) if i > ps and l == ')')
probe_block = '\n'.join(MA[ps:pe])
assert probe_block.count('_ReplayProbe(') == 11

MP = show(SRC, 'backend/src/htdt/application_pages.py')
ls_ = next(i for i, l in enumerate(MP) if l.strip() == '# REV59-UNITS: #728 型付き物理量')
le_ = next(i for i, l in enumerate(MP) if i > ls_ and l.strip() == '}')
labels_block = '\n'.join(MP[ls_:le_])
assert '"cad_diagnostic_verdicts"' in labels_block

# ---------- reset conflicting files to main, splice ----------
checkout_main([
    'backend/src/htdt/cad_schema.py',
    'backend/src/htdt/cad_schema_ddl.py',
    'backend/src/htdt/native_row_integrity.py',
    'backend/src/htdt/native_authority_audit.py',
    'backend/src/htdt/application_pages.py',
    'backend/tests/test_cad_schema.py',
])

# cad_schema.py
p = 'backend/src/htdt/cad_schema.py'
s = io.open(p, encoding='utf-8').read()
old_v = f'NATIVE_SCHEMA_VERSION = {NEW_V - 1}'
assert old_v in s, old_v
s = s.replace(old_v, f'NATIVE_SCHEMA_VERSION = {NEW_V}', 1)
mig = f'''
def _migrate_{PREV_TO}_to_{NEW_V}(connection: sqlite3.Connection) -> None:
    # Install the REV59-UNITS authorities (#728 typed physical
    # quantity: quantities, operations; #730 engineering-assumption /
    # permissible-use ledger: assumptions, resolutions, assessments;
    # #720 perceptual relevance / audibility: model profiles,
    # assessments; #719 residual diagnostic-hypothesis: cases,
    # hypotheses, tests, verdicts): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


'''
s = s.replace('_MIGRATIONS = {', mig + '_MIGRATIONS = {', 1)
old_dict = f'    {PREV_TO}: _migrate_{PREV_FROM}_to_{PREV_TO},\n}}'
assert old_dict in s, old_dict
s = s.replace(old_dict,
              f'    {PREV_TO}: _migrate_{PREV_FROM}_to_{PREV_TO},\n'
              f'    {NEW_V}: _migrate_{PREV_TO}_to_{NEW_V},\n}}')
io.open(p, 'w', encoding='utf-8').write(s)

# cad_schema_ddl.py
p = 'backend/src/htdt/cad_schema_ddl.py'
s = io.open(p, encoding='utf-8').read()
anchor = s.index('NATIVE_COLUMN_ENSURES: tuple')
close_paren = s.rindex('\n)', 0, anchor)
s = s[:close_paren] + '\n' + ddl_block + s[close_paren:]
ts = s.index('NATIVE_SCHEMA_TABLES')
close2 = s.index('\n)', ts)
s = s[:close2] + '\n' + tables_block + s[close2:]
io.open(p, 'w', encoding='utf-8').write(s)

# native_row_integrity.py — append before the dict's final '}' (last '}' at col 0 before 'def scan_native_row_integrity' or the verify fn)
p = 'backend/src/htdt/native_row_integrity.py'
s = io.open(p, encoding='utf-8').read()
i = s.index('def scan_native_row_integrity')
close = s.rindex('\n}', 0, i)
s = s[:close] + '\n' + ri_block + s[close:]
io.open(p, 'w', encoding='utf-8').write(s)

# native_authority_audit.py
p = 'backend/src/htdt/native_authority_audit.py'
s = io.open(p, encoding='utf-8').read()
key = s.index('        raise KeyError(name)')
s = s[:key] + fac_block + '\n' + s[key:]
# probes: append before the ')' that closes the probe tuple, located right before '# Managed-asset'
ma = s.index('# Managed-asset manifest/evidence')
close = s.rindex('\n)', 0, ma)
s = s[:close] + '\n' + probe_block + s[close:]
io.open(p, 'w', encoding='utf-8').write(s)

# application_pages.py
p = 'backend/src/htdt/application_pages.py'
s = io.open(p, encoding='utf-8').read()
d = s.index('_LIFECYCLE_TABLE_LABELS')
close = s.index('\n}', d)
s = s[:close] + '\n' + labels_block + s[close:]
io.open(p, 'w', encoding='utf-8').write(s)

# test_cad_schema.py — ledger row
p = 'backend/tests/test_cad_schema.py'
s = io.open(p, encoding='utf-8').read()
old_row = f"        ({PREV_TO}, 'migrate native schema to v{PREV_TO}'),\n"
assert old_row in s, old_row
s = s.replace(old_row, old_row + f"        ({NEW_V}, 'migrate native schema to v{NEW_V}'),\n")
io.open(p, 'w', encoding='utf-8').write(s)

# manifest — keep-both
p = 'scripts/issue_verification_manifest.yaml'
s = io.open(p, encoding='utf-8').read()
lines = s.split('\n')
out = []
i = 0
while i < len(lines):
    if lines[i].startswith('<<<<<<< HEAD'):
        head = []
        i += 1
        while not lines[i].startswith('======='):
            head.append(lines[i])
            i += 1
        i += 1
        theirs = []
        while not lines[i].startswith('>>>>>>> origin/main'):
            theirs.append(lines[i])
            i += 1
        i += 1
        out.extend(theirs)
        out.extend(head)
    else:
        out.append(lines[i])
        i += 1
io.open(p, 'w', encoding='utf-8').write('\n'.join(out))

print('resolved to v', NEW_V)
