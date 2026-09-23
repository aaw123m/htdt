from __future__ import annotations

import argparse
from pathlib import Path

from htdt.cad_repository import SceneRepository
from htdt.equipment_catalog_export import export_equipment_catalog_snapshot


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
