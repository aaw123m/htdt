from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import unicodedata

from .ingress import IngressTooLargeError
from .limits import MAX_REW_TEXT_BYTES


PARSER_VERSION = 'rew-text-1'
_SPLIT = re.compile(r'[\t ]+')
# Python's float() also accepts Unicode digits ('２０'), underscores and
# 'inf'/'nan' — none of which a REW export emits. Data-row tokens must
# match the ASCII decimal dialect instead of whatever float() can coerce.
_REW_NUMBER = re.compile(
    r'[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?'
)


class RewParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedFrequencyResponse:
    frequency_hz: tuple[float, ...]
    level_db: tuple[float, ...]
    phase_deg: tuple[float, ...] | None
    phase_status: str
    level_reference: str
    header_lines: tuple[str, ...]
    warnings: tuple[str, ...]
    source_sha256: str
    parser_version: str = PARSER_VERSION


def _decode(raw: bytes) -> str:
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise RewParseError('Only UTF-8/ASCII REW text exports are supported in v0.1') from exc


def parse_rew_frequency_response(raw: bytes, *, max_bytes: int = MAX_REW_TEXT_BYTES) -> ParsedFrequencyResponse:
    """Parse a REW text export within the shared ingress byte ceiling.

    ``max_bytes`` mirrors the browser transport's ``MAX_REW_TEXT_BYTES`` so
    every producer of ``raw`` (bounded native file read, decoded Base64 body,
    direct callers) is held to the same resource-safety policy before UTF-8
    decode and line/float expansion.
    """
    if max_bytes < 0:
        raise ValueError('max_bytes must be non-negative')
    if len(raw) > max_bytes:
        raise IngressTooLargeError(
            f'REW text payload is too large: {len(raw)} bytes (limit {max_bytes} bytes)'
        )
    if not raw:
        raise RewParseError('The input file is empty')

    text = _decode(raw)
    frequencies: list[float] = []
    levels: list[float] = []
    phases: list[float] = []
    header_lines: list[str] = []
    warnings: list[str] = []
    row_width: int | None = None

    for line_number, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line:
            continue
        tokens = _SPLIT.split(line)
        starts_numeric = False
        try:
            float(tokens[0])
            starts_numeric = True
        except ValueError:
            pass

        if not starts_numeric:
            if (
                not tokens[0][0].isascii()
                and unicodedata.category(tokens[0][0]) == 'Nd'
            ):
                # A line leading with a non-ASCII digit (e.g. '４０．５')
                # was likely meant as a data row — say so instead of
                # dropping it silently into the header comment block.
                warnings.append(f'non_ascii_numeric_line:{line_number}')
            header_lines.append(original)
            continue

        if not all(_REW_NUMBER.fullmatch(token) for token in tokens):
            raise RewParseError(f'Line {line_number}: non-REW numeric characters')

        if len(tokens) not in (2, 3):
            raise RewParseError(f'Line {line_number}: expected 2 or 3 numeric columns')

        try:
            values = [float(token) for token in tokens]
        except ValueError as exc:
            raise RewParseError(f'Line {line_number}: malformed numeric row') from exc

        if not all(math.isfinite(value) for value in values):
            raise RewParseError(f'Line {line_number}: NaN/Inf is not allowed')

        if row_width is None:
            row_width = len(values)
        elif len(values) != row_width:
            raise RewParseError(f'Line {line_number}: phase column presence changes within the file')

        frequency = values[0]
        if frequency <= 0:
            raise RewParseError(f'Line {line_number}: frequency must be positive')
        if frequencies and frequency <= frequencies[-1]:
            raise RewParseError(f'Line {line_number}: frequencies must be strictly increasing')

        frequencies.append(frequency)
        levels.append(values[1])
        if len(values) == 3:
            phases.append(values[2])

    if len(frequencies) < 2:
        raise RewParseError('At least two frequency-response rows are required')

    phase: tuple[float, ...] | None = tuple(phases) if row_width == 3 else None
    phase_status = 'unknown' if phase is not None else 'absent'
    if phase is not None and all(value == 0 for value in phase):
        warnings.append('phase_all_zero_unverified')

    return ParsedFrequencyResponse(
        frequency_hz=tuple(frequencies),
        level_db=tuple(levels),
        phase_deg=phase,
        phase_status=phase_status,
        level_reference='unknown',
        header_lines=tuple(header_lines),
        warnings=tuple(warnings),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
