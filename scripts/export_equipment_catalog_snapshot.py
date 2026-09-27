from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.equipment_catalog_export import export_equipment_catalog_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Export a deterministic HTDT EquipmentDefinition picker snapshot '
            'for HTDT-Capture.'
        )
    )
    parser.add_argument('database', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()

    result = export_equipment_catalog_snapshot(
        SceneRepository(args.database),
        args.output,
    )
    print(
        f'exported {result.definition_count} definitions '
        f'(authority {result.authority_version}) '
        f'to {result.output_path} '
        f'sha256={result.snapshot_sha256}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
