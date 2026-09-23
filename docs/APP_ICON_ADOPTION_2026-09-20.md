# HTDT app icon adoption — 2026-09-20

## Decision

The user-approved HTDT home-theater / digital-twin artwork is the canonical application icon for the Windows HTDT product.

## Implementation

- Canonical artwork source: `assets/branding/HTDT-AppIcon-source.jpg`.
- The source is a compact derivative of the approved artwork; packaging renders platform-specific assets rather than maintaining duplicated generated binaries.
- `scripts/prepare_branding_assets.py` renders `HTDT.png` for the Qt runtime and `HTDT.ico` for the executable, shortcuts, and installer.
- The Windows render applies a rounded alpha mask so transparency is restricted to the four rounded corners.
- `scripts/build-native.ps1` binds the generated ICO to PyInstaller and bundles the PNG for `QApplication.setWindowIcon`.
- The packaged `HTDT.ico` is copied beside `HTDT.exe`, and Inno Setup uses the same icon for the installer.
- The Windows release workflow is triggered by branding-source changes and fails if the packaged icon is missing.

## Validation

The existing `Windows Release Artifact` workflow remains the authority for packaged executable and installer acceptance. PyInstaller and Inno Setup both consume the generated icon during that workflow, so invalid branding assets fail packaging.

No RDC or local Windows mutation is required for this slice.
