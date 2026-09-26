"""EDID / DisplayID conformance corpus (#1064).

A versioned conformance harness for display-capability parsing:

- ``EdidCorpusEntry`` pins every fixture to exact source identity —
  repository, path, license, raw sha256, acquisition revision — plus the
  *expected* parser status and warnings and the normalization profile it
  was authored under. Expectations bind to content hash, not filenames.
- ``parse_edid`` is HTDT's own minimal normalizer: it validates base-block
  structure (header, length, checksum, extension-count consistency) and
  extracts only known semantics (manufacturer/product, version, physical
  size, extension inventory). Unknown extension types stay raw-listed and
  never become guessed capability.
- ``run_conformance`` compares a candidate parser result against the
  entry's expectations; an optional oracle callable (e.g. edid-decode or
  libdisplay-info output adapted by the caller) cross-checks — oracle
  disagreement yields AMBIGUOUS, never silently preferred truth (#1064 §7).

Malformed input fails closed: structural errors are detected and reported,
and a malformed EDID is never normalized into clean capability truth.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurements import canonical_json


def _hash(payload: Any) -> str:
    return sha256(canonical_json(payload).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Corpus model (#1064 §1-2)


CorpusClass = Literal[
    'valid_reference',
    'real_device_valid',
    'real_device_quirk',
    'malformed',
    'displayid',
    'cta',
    'repeater_composed',
]

ExpectedStatus = Literal['parse_ok', 'parse_warning', 'parse_error']


class EdidCorpusEntry(BaseModel):
    """One versioned conformance fixture with exact source identity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    entry_id: str = Field(min_length=1)
    corpus_version: Literal['edid-corpus-1'] = 'edid-corpus-1'
    corpus_class: CorpusClass
    # Exact provenance of the bytes — never just a filename.
    source_repository: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    license_name: str = Field(min_length=1)
    acquisition_revision: str = Field(min_length=1)
    acquisition_date: str = Field(min_length=1)
    # Content identity the expectation binds to.
    raw_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_status: ExpectedStatus
    # Warnings the parser MUST report — warnings survive normalization.
    expected_warnings: tuple[str, ...] = ()
    # The semantic profile the expectation was authored under.
    normalization_profile: str = 'cta-861-j'
    parser_version: str = Field(min_length=1)
    entry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_entry(self) -> 'EdidCorpusEntry':
        if self.expected_status == 'parse_error' and not (
            self.expected_warnings
        ):
            raise ValueError(
                'a malformed corpus entry must declare the structural '
                'error the parser must report'
            )
        if self.corpus_class == 'malformed' and (
            self.expected_status == 'parse_ok'
        ):
            raise ValueError(
                'malformed fixtures may not expect a clean parse'
            )
        if self.entry_sha256 != _hash(self.identity_payload()):
            raise ValueError('corpus entry hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'corpus_version': self.corpus_version,
            'entry_id': self.entry_id,
            'corpus_class': self.corpus_class,
            'source_repository': self.source_repository,
            'source_path': self.source_path,
            'license_name': self.license_name,
            'acquisition_revision': self.acquisition_revision,
            'acquisition_date': self.acquisition_date,
            'raw_sha256': self.raw_sha256,
            'expected_status': self.expected_status,
            'expected_warnings': list(self.expected_warnings),
            'normalization_profile': self.normalization_profile,
            'parser_version': self.parser_version,
        }


def build_corpus_entry(
    *,
    raw: bytes,
    corpus_class: CorpusClass,
    source_repository: str,
    source_path: str,
    license_name: str,
    acquisition_revision: str,
    acquisition_date: str,
    expected_status: ExpectedStatus,
    parser_version: str,
    expected_warnings: tuple[str, ...] = (),
    normalization_profile: str = 'cta-861-j',
    entry_id: str | None = None,
) -> EdidCorpusEntry:
    payload: dict[str, Any] = {
        'entry_id': entry_id or str(uuid4()),
        'corpus_class': corpus_class,
        'source_repository': source_repository,
        'source_path': source_path,
        'license_name': license_name,
        'acquisition_revision': acquisition_revision,
        'acquisition_date': acquisition_date,
        'raw_sha256': sha256(raw).hexdigest(),
        'expected_status': expected_status,
        'expected_warnings': tuple(expected_warnings),
        'normalization_profile': normalization_profile,
        'parser_version': parser_version,
    }
    provisional = EdidCorpusEntry.model_construct(
        **payload, entry_sha256='0' * 64
    )
    return EdidCorpusEntry(
        **payload, entry_sha256=_hash(provisional.identity_payload())
    )


# ---------------------------------------------------------------------------
# Minimal parser (#1064 §3-4)


EDID_PARSER_VERSION = 'htdt-edid-parser-1'

EDID_HEADER = b'\x00\xff\xff\xff\xff\xff\xff\x00'
_BLOCK = 128


ParseStatus = Literal['parse_ok', 'parse_warning', 'parse_error']


class EdidExtension(BaseModel):
    """One extension block — type listed, semantics only when known."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    index: int = Field(ge=0)
    tag: int = Field(ge=0, le=255)
    kind: Literal[
        'cta_861', 'displayid', 'vdtb', 'other_known', 'unknown'
    ]
    checksum_ok: bool


class EdidParseResult(BaseModel):
    """Normalized base-block capability + structural findings.

    Only known semantics are normalized. Every detected structural problem
    is a named warning/error — a malformed EDID is never upgraded to a
    clean capability snapshot (#1064 §3-4).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    status: ParseStatus
    parser_version: Literal['htdt-edid-parser-1'] = EDID_PARSER_VERSION  # type: ignore[assignment]
    normalization_profile: str = 'cta-861-j'
    manufacturer_id: str | None = None
    product_code: int | None = None
    serial_number: int | None = None
    manufacture_week: int | None = None
    manufacture_year: int | None = None
    edid_version: tuple[int, int] | None = None
    display_width_cm: int | None = None
    display_height_cm: int | None = None
    declared_extension_count: int | None = None
    actual_extension_count: int | None = None
    extensions: tuple[EdidExtension, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


_EXTENSION_KINDS: dict[int, str] = {
    0x02: 'cta_861',
    0x10: 'vdtb',
    0x40: 'displayid',   # DisplayID 1.x
    0x70: 'displayid',   # DisplayID 2.0
    0x60: 'other_known', # ARVR/left field — recognized but not normalized
    0xFF: 'other_known', # block map
}


def _manufacturer_id(data: bytes) -> str | None:
    if len(data) < 10:
        return None
    packed = (data[8] << 8) | data[9]
    chars = [chr(((packed >> shift) & 0x1F) + ord('A') - 1) for shift in (10, 5, 0)]
    if not all('A' <= c <= 'Z' for c in chars):
        return None
    return ''.join(chars)


def parse_edid(raw: bytes) -> EdidParseResult:
    """Parse a raw EDID blob, fail-closed on structure.

    Errors are unrecoverable structure violations; warnings are anomalies
    where partial safe extraction still happened. Unknown extension types
    are listed raw — never interpreted.
    """

    warnings: list[str] = []
    errors: list[str] = []
    if len(raw) < _BLOCK:
        errors.append(f'truncated_base_block:{len(raw)}')
        return EdidParseResult(
            status='parse_error',
            warnings=tuple(warnings),
            errors=tuple(errors),
        )
    if len(raw) % _BLOCK != 0:
        errors.append(f'invalid_length:{len(raw)}')
        return EdidParseResult(
            status='parse_error',
            warnings=tuple(warnings),
            errors=tuple(errors),
        )
    base = raw[:_BLOCK]
    if base[:8] != EDID_HEADER:
        errors.append('bad_base_header')
    if sum(base) % 256 != 0:
        errors.append('bad_base_checksum')
    declared_ext = base[126]
    actual_ext = len(raw) // _BLOCK - 1
    if declared_ext != actual_ext:
        warnings.append(
            f'extension_count_mismatch:{declared_ext}!={actual_ext}'
        )
    extensions: list[EdidExtension] = []
    for index in range(actual_ext):
        block = raw[(index + 1) * _BLOCK : (index + 2) * _BLOCK]
        tag = block[0]
        extensions.append(
            EdidExtension(
                index=index,
                tag=tag,
                kind=_EXTENSION_KINDS.get(tag, 'unknown'),
                checksum_ok=sum(block) % 256 == 0,
            )
        )
        if tag not in _EXTENSION_KINDS:
            warnings.append(f'unknown_extension_tag:{tag}')
        if not extensions[-1].checksum_ok:
            warnings.append(f'bad_extension_checksum:{index}')
    if any(item.kind == 'other_known' for item in extensions):
        warnings.append('extension_semantics_not_normalized')
    version = (base[18], base[19])
    if not (0 < version[0] <= 1 and 0 <= version[1] <= 5):
        warnings.append(f'unrecognized_edid_version:{version[0]}.{version[1]}')
    status: ParseStatus
    if errors:
        status = 'parse_error'
    elif warnings:
        status = 'parse_warning'
    else:
        status = 'parse_ok'
    return EdidParseResult(
        status=status,
        manufacturer_id=_manufacturer_id(base),
        product_code=int.from_bytes(base[10:12], 'little'),
        serial_number=int.from_bytes(base[12:16], 'little'),
        manufacture_week=base[16],
        manufacture_year=1990 + base[17],
        edid_version=version,
        display_width_cm=base[21] or None,
        display_height_cm=base[22] or None,
        declared_extension_count=declared_ext,
        actual_extension_count=actual_ext,
        extensions=tuple(extensions),
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Conformance runner (#1064 §7-8)


ConformanceOutcome = Literal['match', 'mismatch', 'ambiguous', 'unavailable']


class ConformanceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    entry_id: str
    expected_status: ExpectedStatus
    actual_status: ParseStatus
    outcome: ConformanceOutcome
    # Missing expected warnings and unexpected warnings both recorded —
    # a parser that stays silent where anomalies exist is a mismatch.
    missing_warnings: tuple[str, ...] = ()
    unexpected_warnings: tuple[str, ...] = ()
    oracle_disagreement: str | None = None


def run_conformance(
    entry: EdidCorpusEntry,
    raw: bytes,
    *,
    oracle: Callable[[bytes], ParseStatus | None] | None = None,
) -> ConformanceResult:
    """Evaluate one fixture. ``raw`` must hash to the entry's pinned bytes.

    ``oracle`` is an optional independent parser status extractor; when it
    disagrees with HTDT's parser the field outcome is 'ambiguous' — never
    silently whichever answer was wanted.
    """

    if sha256(raw).hexdigest() != entry.raw_sha256:
        raise ValueError(
            f'corpus entry {entry.entry_id} raw bytes do not match the '
            'pinned sha256'
        )
    result = parse_edid(raw)
    missing = tuple(
        warning
        for warning in entry.expected_warnings
        if warning not in result.warnings
        and warning not in result.errors
    )
    declared = set(entry.expected_warnings)
    unexpected = tuple(
        item
        for item in (*result.warnings, *result.errors)
        if item.split(':')[0] not in {w.split(':')[0] for w in declared}
        and item not in declared
    )
    oracle_disagreement = None
    outcome: ConformanceOutcome
    if result.status != entry.expected_status:
        outcome = 'mismatch'
    elif missing or unexpected:
        outcome = 'mismatch'
    else:
        outcome = 'match'
    if oracle is not None:
        oracle_status = oracle(raw)
        if oracle_status is None:
            outcome = 'unavailable' if outcome == 'match' else outcome
        elif oracle_status != result.status:
            oracle_disagreement = (
                f'oracle={oracle_status} htdt={result.status}'
            )
            outcome = 'ambiguous'
    return ConformanceResult(
        entry_id=entry.entry_id,
        expected_status=entry.expected_status,
        actual_status=result.status,
        outcome=outcome,
        missing_warnings=missing,
        unexpected_warnings=unexpected,
        oracle_disagreement=oracle_disagreement,
    )
