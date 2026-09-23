from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFile


ImageFile.LOAD_TRUNCATED_IMAGES = True


ICON_SIZE = 1024
CORNER_RADIUS = 192
MASK_SCALE = 4
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_branding_assets(source: Path, output_dir: Path) -> tuple[Path, Path]:
    """Render Windows runtime and executable icons from the approved HTDT artwork."""

    if not source.is_file():
        raise FileNotFoundError(f"HTDT branding source not found: {source}")

    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = output_dir / "HTDT.png"
    ico_path = output_dir / "HTDT.ico"

    with Image.open(source) as opened:
        rgb = opened.convert("RGB").resize(
            (ICON_SIZE, ICON_SIZE),
            Image.Resampling.LANCZOS,
        )

    rgba = rgb.convert("RGBA")
    scaled_size = ICON_SIZE * MASK_SCALE
    mask = Image.new("L", (scaled_size, scaled_size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, scaled_size - 1, scaled_size - 1),
        radius=CORNER_RADIUS * MASK_SCALE,
        fill=255,
    )
    mask = mask.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
    rgba.putalpha(mask)

    rgba.resize((512, 512), Image.Resampling.LANCZOS).save(
        runtime_path,
        format="PNG",
        optimize=True,
    )
    rgba.save(
        ico_path,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
    )
    return runtime_path, ico_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render HTDT Windows branding assets from canonical artwork."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    runtime_path, ico_path = render_branding_assets(args.source, args.output_dir)
    print(f"Rendered runtime icon: {runtime_path}")
    print(f"Rendered executable icon: {ico_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
