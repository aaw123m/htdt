from __future__ import annotations

import argparse
from pathlib import Path

from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Export a deterministic HTDT EquipmentDefinition picker snapshot.'
        )
    )
    parser.add_argument('database', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()

    scene_repository = SceneRepository(args.database)
    snapshot = CadEquipmentRepository(
        scene_repository
    ).catalog_snapshot()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(snapshot.canonical_bytes())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
