"""Project field-label authority (#665).

Installs, service calls and BOM receiving involve physical objects that
must be identified in the field without ambiguity. A printed label carries
two artifacts that always travel together:

- a QR encoding of a versioned ``htdt-label-v1`` payload — project
  reference (:class:`~htdt.project_identity.HTDTProjectReference`
  ``project_id``), target kind, the *stable target identifier* (instance /
  rack / cable-run / termination ids — never a display name), a generation
  counter, and a payload checksum;
- a deterministic human-readable short code the installer can type when
  the QR cannot be scanned.

Contract properties:

- the payload never contains filesystem paths, project data, or secrets —
  it is an identity pointer only;
- ``resolve_label_scan`` answers whether a scanned payload belongs to this
  project and which record it names, distinguishing wrong project, stale
  generation, unissued future generation, retired targets, unknown
  targets, bad checksums and malformed payloads;
- replacing equipment creates a new instance **and** a new label minted
  against the *new* target id (generation restarts); label identity is
  never reused across targets, while reissuing a label for the *same*
  target increments its generation (a lost/damaged print, not a
  replacement — see #892);
- :func:`generate_label_sheet` renders printable label sheets as
  deterministic SVG for ordinary office printers — device, cable flag and
  compact-termination layouts;
- labels bound to retired superseded designs remain resolvable as
  historical, never silently valid or silently wrong.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import segno


LABEL_PAYLOAD_VERSION = 'htdt-label-v1'
LABEL_AUTHORITY_VERSION = 'field-label-1'

LabelTargetKind = Literal[
    'installed_equipment',
    'rack',
    'cable_run',
    'termination_point',
    'speaker_instance',
]

LabelScanStatus = Literal[
    'ok',
    'wrong_project',
    'stale_generation',
    'unissued_generation',
    'retired_target',
    'unknown_target',
    'bad_checksum',
    'malformed',
]

LabelSheetKind = Literal['device', 'cable', 'compact_termination']

#: Short-code prefixes per target kind — terse enough to read over the phone.
_SHORT_PREFIX: dict[str, str] = {
    'installed_equipment': 'EQ',
    'rack': 'RK',
    'cable_run': 'CB',
    'termination_point': 'TP',
    'speaker_instance': 'SP',
}

#: Deterministic Base32 alphabet — no ambiguous characters (no 0/O, 1/I).
_B32 = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

#: Physical label footprints (width × height mm) for office printing.
_LABEL_SIZES: dict[str, tuple[float, float]] = {
    'device': (50.0, 25.0),
    'cable': (25.0, 30.0),
    'compact_termination': (30.0, 15.0),
}
_LABEL_COLUMNS = 4
_LABEL_MARGIN_MM = 10.0
_LABEL_GAP_MM = 5.0


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _checksum(project_id: str, target_kind: str, target_id: str,
              generation: int) -> str:
    return _digest({
        'v': LABEL_PAYLOAD_VERSION,
        'p': project_id,
        'k': target_kind,
        't': target_id,
        'g': generation,
    })[:8]


def _short_code(target_kind: str, target_id: str) -> str:
    raw = sha256(target_id.encode('utf-8')).digest()
    value = int.from_bytes(raw[:4], 'big')
    chars = []
    for _ in range(4):
        value, rem = divmod(value, 32)
        chars.append(_B32[rem])
    return f'{_SHORT_PREFIX[target_kind]}-{"".join(chars)}'


class FieldLabelPayload(BaseModel):
    """The ``htdt-label-v1`` machine payload — an identity pointer only."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    v: Literal['htdt-label-v1'] = LABEL_PAYLOAD_VERSION
    p: str = Field(min_length=1)
    k: LabelTargetKind
    t: str = Field(min_length=1)
    g: int = Field(ge=1)
    c: str = Field(min_length=8, max_length=8)

    def checksum_ok(self) -> bool:
        """Structural validity vs. integrity: scans distinguish the two —
        :func:`resolve_label_scan` reports ``bad_checksum`` when this is
        False rather than treating the payload as malformed."""
        return self.c == _checksum(self.p, self.k, self.t, self.g)

    def qr_text(self) -> str:
        return _canonical({
            'v': self.v, 'p': self.p, 'k': self.k,
            't': self.t, 'g': self.g, 'c': self.c,
        })


def parse_label_payload(text: str) -> FieldLabelPayload | None:
    """Decode a scanned payload string; ``None`` when malformed."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        if data.get('v') != LABEL_PAYLOAD_VERSION:
            return None
        return FieldLabelPayload.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        return None


class FieldLabel(BaseModel):
    """One printable label bound to a stable target id and generation."""

    model_config = ConfigDict(frozen=True)

    label_id: str = Field(min_length=1)
    payload: FieldLabelPayload
    short_code: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_label(self) -> 'FieldLabel':
        expected = _short_code(self.payload.k, self.payload.t)
        if self.short_code != expected:
            raise ValueError('field label short code mismatch')
        return self


def mint_label(
    *,
    project_id: str,
    target_kind: LabelTargetKind,
    target_id: str,
    generation: int = 1,
    created_at_utc: str,
) -> FieldLabel:
    payload = FieldLabelPayload(
        p=project_id, k=target_kind, t=target_id, g=generation,
        c=_checksum(project_id, target_kind, target_id, generation),
    )
    return FieldLabel(
        label_id=f'label-{_digest(payload.qr_text())[:16]}',
        payload=payload,
        short_code=_short_code(target_kind, target_id),
        created_at_utc=created_at_utc,
    )


def mint_reissued_label_for_same_target(
    previous: FieldLabel,
    *,
    created_at_utc: str,
) -> FieldLabel:
    """Reissue a label for the SAME target id — generation increments.

    Reissue covers a lost, damaged or re-printed physical label; it is
    NOT equipment replacement. Physical replacement creates a new
    instance with a new target id whose label is minted with
    :func:`mint_label` (generation restarts at 1), linked to the prior
    instance through the #569 lineage edge.
    """
    return mint_label(
        project_id=previous.payload.p,
        target_kind=previous.payload.k,
        target_id=previous.payload.t,
        generation=previous.payload.g + 1,
        created_at_utc=created_at_utc,
    )


class LabelTargetRecord(BaseModel):
    """Current truth for a labelled target — kept in the project registry."""

    model_config = ConfigDict(frozen=True)

    target_id: str = Field(min_length=1)
    target_kind: LabelTargetKind
    latest_generation: int = Field(ge=1)
    retired: bool = False


class LabelRegistry(BaseModel):
    """Project-side record the scanner resolves against."""

    model_config = ConfigDict(frozen=True)

    project_id: str = Field(min_length=1)
    targets: tuple[LabelTargetRecord, ...] = ()

    @model_validator(mode='after')
    def valid_registry(self) -> 'LabelRegistry':
        ids = [item.target_id for item in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError('label registry target ids must be unique')
        return self


class LabelScanResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: LabelScanStatus
    target_id: str | None = None
    target_kind: LabelTargetKind | None = None
    generation: int | None = None
    reason: str | None = None


def resolve_label_scan(
    payload_text: str,
    registry: LabelRegistry,
) -> LabelScanResult:
    """Resolve a scanned QR payload against project truth — never guess."""
    payload = parse_label_payload(payload_text)
    if payload is None:
        return LabelScanResult(status='malformed', reason='unparseable payload')
    if not payload.checksum_ok():
        return LabelScanResult(
            status='bad_checksum', reason='payload checksum mismatch'
        )
    if payload.p != registry.project_id:
        return LabelScanResult(
            status='wrong_project',
            target_id=payload.t,
            target_kind=payload.k,
            generation=payload.g,
            reason='label belongs to a different project',
        )
    record = next(
        (item for item in registry.targets if item.target_id == payload.t),
        None,
    )
    if record is None:
        return LabelScanResult(
            status='unknown_target',
            target_id=payload.t,
            target_kind=payload.k,
            generation=payload.g,
            reason='target id is not in this project registry',
        )
    if record.target_kind != payload.k:
        return LabelScanResult(
            status='unknown_target',
            target_id=payload.t,
            target_kind=payload.k,
            generation=payload.g,
            reason='target kind does not match the registry record',
        )
    if record.retired:
        return LabelScanResult(
            status='retired_target',
            target_id=payload.t,
            target_kind=payload.k,
            generation=payload.g,
            reason='target was retired by a superseding design',
        )
    if payload.g < record.latest_generation:
        return LabelScanResult(
            status='stale_generation',
            target_id=payload.t,
            target_kind=payload.k,
            generation=payload.g,
            reason=(
                f'generation {payload.g} superseded by '
                f'generation {record.latest_generation}'
            ),
        )
    if payload.g > record.latest_generation:
        return LabelScanResult(
            status='unissued_generation',
            target_id=payload.t,
            target_kind=payload.k,
            generation=payload.g,
            reason=(
                f'generation {payload.g} was never issued; latest is '
                f'generation {record.latest_generation}'
            ),
        )
    return LabelScanResult(
        status='ok',
        target_id=payload.t,
        target_kind=payload.k,
        generation=payload.g,
    )


def _qr_modules(text: str) -> list[list[bool]]:
    qr = segno.make(text, error='m', micro=False)
    return [[bool(cell) for cell in row] for row in qr.matrix]


def _qr_svg(text: str, x_mm: float, y_mm: float, size_mm: float) -> str:
    matrix = _qr_modules(text)
    n = len(matrix)
    cell = size_mm / n
    parts = [
        f'<rect x="{x_mm:.3f}" y="{y_mm:.3f}" width="{size_mm:.3f}" '
        f'height="{size_mm:.3f}" fill="#fff"/>'
    ]
    for row_i, row in enumerate(matrix):
        for col_i, dark in enumerate(row):
            if dark:
                parts.append(
                    f'<rect x="{x_mm + col_i * cell:.3f}" '
                    f'y="{y_mm + row_i * cell:.3f}" width="{cell:.3f}" '
                    f'height="{cell:.3f}" fill="#000"/>'
                )
    return ''.join(parts)


def _escape(text: str) -> str:
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


class LabelSheet(BaseModel):
    """A printable sheet of labels — deterministic SVG for office printers."""

    model_config = ConfigDict(frozen=True)

    sheet_id: str = Field(min_length=1)
    label_kind: LabelSheetKind
    page_width_mm: float = Field(gt=0.0)
    page_height_mm: float = Field(gt=0.0)
    labels: tuple[FieldLabel, ...] = ()
    sheet_content_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_sheet(self) -> 'LabelSheet':
        if self.sheet_content_sha256 != _digest(self.content_payload()):
            raise ValueError('LabelSheet content hash mismatch')
        return self

    def content_payload(self) -> dict[str, Any]:
        return {
            'sheet_id': self.sheet_id,
            'label_kind': self.label_kind,
            'page_width_mm': self.page_width_mm,
            'page_height_mm': self.page_height_mm,
            'labels': [item.model_dump(mode='json') for item in self.labels],
        }

    def to_svg(self) -> str:
        w_mm, h_mm = _LABEL_SIZES[self.label_kind]
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{self.page_width_mm:g}mm" height="{self.page_height_mm:g}mm" '
            f'viewBox="0 0 {self.page_width_mm:g} {self.page_height_mm:g}">',
        ]
        for index, label in enumerate(self.labels):
            col = index % _LABEL_COLUMNS
            row = index // _LABEL_COLUMNS
            x = _LABEL_MARGIN_MM + col * (w_mm + _LABEL_GAP_MM)
            y = _LABEL_MARGIN_MM + row * (h_mm + _LABEL_GAP_MM)
            parts.append(
                f'<rect x="{x:.3f}" y="{y:.3f}" width="{w_mm:.3f}" '
                f'height="{h_mm:.3f}" fill="none" stroke="#999" '
                f'stroke-width="0.2" stroke-dasharray="1,1"/>'
            )
            qr_side = min(h_mm - 4.0, w_mm * 0.55)
            parts.append(
                _qr_svg(label.payload.qr_text(), x + 2.0, y + 2.0, qr_side)
            )
            tx = x + 2.0 + qr_side + 2.0
            parts.append(
                f'<text x="{tx:.3f}" y="{y + h_mm / 2 - 1:.3f}" '
                f'font-size="4" font-family="monospace" fill="#000">'
                f'{_escape(label.short_code)}</text>'
            )
            parts.append(
                f'<text x="{tx:.3f}" y="{y + h_mm / 2 + 3.5:.3f}" '
                f'font-size="2.6" font-family="monospace" fill="#333">'
                f'{_escape(label.payload.t)} g{label.payload.g}</text>'
            )
        parts.append('</svg>')
        return ''.join(parts)


def generate_label_sheet(
    labels: Sequence[FieldLabel],
    *,
    label_kind: LabelSheetKind,
    sheet_id: str,
    page: tuple[float, float] = (210.0, 297.0),
) -> LabelSheet:
    w_mm, h_mm = _LABEL_SIZES[label_kind]
    cols = _LABEL_COLUMNS
    rows = int(
        (page[1] - 2 * _LABEL_MARGIN_MM + _LABEL_GAP_MM)
        // (h_mm + _LABEL_GAP_MM)
    )
    if len(labels) > cols * rows:
        raise ValueError(
            f'{len(labels)} labels do not fit on one {label_kind} sheet '
            f'({cols}x{rows} slots); split the batch'
        )
    provisional = LabelSheet.model_construct(
        sheet_id=sheet_id,
        label_kind=label_kind,
        page_width_mm=page[0],
        page_height_mm=page[1],
        labels=tuple(labels),
        sheet_content_sha256='0' * 64,
    )
    return LabelSheet(
        sheet_id=sheet_id,
        label_kind=label_kind,
        page_width_mm=page[0],
        page_height_mm=page[1],
        labels=tuple(labels),
        sheet_content_sha256=_digest(provisional.content_payload()),
    )


__all__ = [
    'LABEL_AUTHORITY_VERSION',
    'LABEL_PAYLOAD_VERSION',
    'FieldLabel',
    'FieldLabelPayload',
    'LabelRegistry',
    'LabelScanResult',
    'LabelScanStatus',
    'LabelSheet',
    'LabelSheetKind',
    'LabelTargetKind',
    'LabelTargetRecord',
    'generate_label_sheet',
    'mint_label',
    'mint_reissued_label_for_same_target',
    'parse_label_payload',
    'resolve_label_scan',
]
