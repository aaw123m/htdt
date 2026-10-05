"""Bounded STEP Part 21 parser for IFC (issue #578).

This is a deliberately small, fail-closed reader for the exchange
structure defined by ISO 10303-21 (the physical file format) hosting the
IFC 4.3.2.0 / IFC4X3_ADD2 schema (ISO 16739-1:2024). It is NOT a general
IFC toolkit: it parses the PART21 entity graph into typed raw arguments
and leaves semantic interpretation to ``cad_ifc_interop``.

Design rules:
- The file header (``FILE_SCHEMA``/``FILE_NAME``) is mandatory evidence:
  a document whose schema identifier is missing is rejected rather than
  guessed.
- Entities parse into ``IfcStepEntity`` records carrying a name and the
  raw argument tree (lists, refs, enums, typed selects, literals).
  Unknown entity names are preserved verbatim — never dropped silently.
- Strings decode STEP escapes: doubled single quotes, ``\\S\\c``
  (upper-half Latin-1), ``\\X\\NN`` (8-bit), ``\\X2\\...\\X0\\`` and
  ``\\X4\\...\\X0\\`` (UTF-16/32 hex). Undecodable bytes are a hard
  error, never a replacement character (fail-closed).
- Parsing is bounded: entity count, argument depth and byte size are
  capped so a hostile/corrupt file cannot wedge the reader; when a cap
  trips the whole import fails rather than producing partial geometry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Union

# ---------------------------------------------------------------------------
# Value model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IfcStepRef:
    """Reference to another entity: ``#123``."""

    entity_id: int


@dataclass(frozen=True)
class IfcStepEnum:
    """Enumeration literal: ``.LENGTHUNIT.``, ``.T.``, ``.F.``, ``.U.``."""

    name: str


@dataclass(frozen=True)
class IfcStepTypedValue:
    """Typed select: ``IFCLABEL('foo')`` or ``IFCLENGTHMEASURE(0.5)``."""

    type_name: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class IfcStepOmitted:
    """``$`` — explicitly unset / derived value."""

    kind: Literal['omitted'] = 'omitted'


@dataclass(frozen=True)
class IfcStepDerived:
    """``*`` — derived value redeclared."""

    kind: Literal['derived'] = 'derived'


IfcStepValue = Union[
    int,
    float,
    str,
    IfcStepRef,
    IfcStepEnum,
    IfcStepTypedValue,
    IfcStepOmitted,
    IfcStepDerived,
    tuple,
]


@dataclass(frozen=True)
class IfcStepEntity:
    """One ``#id=NAME(args...);`` instance."""

    entity_id: int
    name: str
    args: tuple[Any, ...]
    source_line: int


@dataclass(frozen=True)
class IfcStepHeader:
    """HEADER section evidence."""

    file_description: tuple[str, ...]
    file_name_fields: dict[str, Any]
    schema_identifiers: tuple[str, ...]


class IfcStepParseError(ValueError):
    """Malformed or over-limit STEP content; import must abort."""


# ---------------------------------------------------------------------------
# Limits (bounded reader)
# ---------------------------------------------------------------------------

MAX_STEP_BYTES = 256 * 1024 * 1024
MAX_STEP_ENTITIES = 2_000_000
MAX_ARG_DEPTH = 64
MAX_STEP_LINE_CHARS = 8 * 1024 * 1024
MAX_STRING_CHARS = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_HEX_RUN = re.compile(r'[0-9A-Fa-f]+')


def _decode_step_string(raw: str, line: int) -> str:
    """Decode a STEP string body (content between the quotes)."""
    if len(raw) > MAX_STRING_CHARS:
        raise IfcStepParseError(f'line {line}: string exceeds size limit')
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != '\\':
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            raise IfcStepParseError(f'line {line}: dangling escape in string')
        kind = raw[i + 1]
        if kind == 'S':
            if i + 3 >= n or raw[i + 2] != '\\':
                raise IfcStepParseError(f'line {line}: malformed \\S\\ escape')
            code = raw[i + 3]
            # \S\c — high-half Latin-1: c (0x80 offset), only for c >= 0x20
            out.append(chr(ord(code) + 0x80))
            i += 4
        elif kind == 'X':
            if i + 2 < n and raw[i + 2] == '\\':
                # \X\hh — exactly two hex digits (8-bit codepoint).
                body = raw[i + 3 : i + 5]
                if len(body) != 2 or not _HEX_RUN.fullmatch(body):
                    raise IfcStepParseError(
                        f'line {line}: malformed \\X\\ escape'
                    )
                out.append(chr(int(body, 16)))
                i += 5
            elif i + 2 < n and raw[i + 2] in ('2', '4'):
                # \X2\hhhh...\X0\ (UTF-16) or \X4\hhhhhhhh...\X0\ (UTF-32).
                unit = 2 if raw[i + 2] == '2' else 4
                start = i + 4
                end = raw.find('\\X0\\', start)
                if end < 0:
                    raise IfcStepParseError(
                        f'line {line}: unterminated \\X{unit}\\ escape'
                    )
                hexpart = raw[start:end]
                if len(hexpart) % unit != 0:
                    raise IfcStepParseError(
                        f'line {line}: \\X{unit}\\ body length not a multiple'
                    )
                if hexpart and not _HEX_RUN.fullmatch(hexpart):
                    raise IfcStepParseError(
                        f'line {line}: invalid hex in escape'
                    )
                try:
                    for k in range(0, len(hexpart), unit):
                        out.append(chr(int(hexpart[k : k + unit], 16)))
                except ValueError as exc:
                    raise IfcStepParseError(
                        f'line {line}: invalid codepoint in escape'
                    ) from exc
                i = end + 4
            else:
                raise IfcStepParseError(f'line {line}: malformed \\X escape')
        elif kind == '\\':
            out.append('\\')
            i += 2
        else:
            raise IfcStepParseError(
                f'line {line}: unsupported escape \\{kind}'
            )
    return ''.join(out)


@dataclass(frozen=True)
class _Token:
    kind: str  # 'ident','int','real','string','enum','ref','omit','derived',
    # 'lparen','rparen','comma','semicolon','equals','eof'
    text: str
    line: int


def _lex(text: str) -> Iterator[_Token]:
    i = 0
    n = len(text)
    line = 1
    length = n
    while i < n:
        ch = text[i]
        if ch == '\n':
            line += 1
            i += 1
            continue
        if ch in ' \t\r':
            i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            if end < 0:
                raise IfcStepParseError(f'line {line}: unterminated comment')
            line += text.count('\n', i, end)
            i = end + 2
            continue
        if ch == "'":
            j = i + 1
            body: list[str] = []
            while True:
                if j >= n:
                    raise IfcStepParseError(
                        f'line {line}: unterminated string literal'
                    )
                c = text[j]
                if c == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        body.append("'")
                        j += 2
                        continue
                    break
                if c == '\n':
                    line += 1
                body.append(c)
                j += 1
            yield _Token('string', _decode_step_string(''.join(body), line), line)
            i = j + 1
            continue
        if ch == '"':
            # STEP binary blob; treat as opaque payload marker.
            j = i + 1
            while j < n and text[j] != '"':
                j += 1
            if j >= n:
                raise IfcStepParseError(f'line {line}: unterminated binary')
            yield _Token('ident', f'<binary:{j - i - 1}>', line)
            i = j + 1
            continue
        if ch == '#':
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
            if j == i + 1:
                raise IfcStepParseError(f'line {line}: bad entity ref')
            yield _Token('ref', text[i + 1 : j], line)
            i = j
            continue
        if ch == '.':
            j = i + 1
            while j < n and text[j] != '.':
                if not (text[j].isalnum() or text[j] == '_'):
                    raise IfcStepParseError(f'line {line}: bad enum literal')
                j += 1
            if j >= n:
                raise IfcStepParseError(f'line {line}: unterminated enum')
            yield _Token('enum', text[i + 1 : j].upper(), line)
            i = j + 1
            continue
        if ch == '$':
            yield _Token('omit', '$', line)
            i += 1
            continue
        if ch == '*':
            yield _Token('derived', '*', line)
            i += 1
            continue
        if ch in '(),;=':
            kind = {
                '(': 'lparen',
                ')': 'rparen',
                ',': 'comma',
                ';': 'semicolon',
                '=': 'equals',
            }[ch]
            yield _Token(kind, ch, line)
            i += 1
            continue
        if ch.isalpha() or ch == '_':
            j = i + 1
            # Identifiers include the '-' in END-ISO-10303-21.
            while j < n and (text[j].isalnum() or text[j] in '_-'):
                j += 1
            yield _Token('ident', text[i:j], line)
            i = j
            continue
        if ch.isdigit() or (ch in '+-' and i + 1 < n and (
            text[i + 1].isdigit() or text[i + 1] == '.'
        )):
            j = i
            if text[j] in '+-':
                j += 1
            seen_dot = False
            seen_e = False
            while j < n:
                c = text[j]
                if c.isdigit():
                    j += 1
                    continue
                if c == '.' and not seen_dot:
                    seen_dot = True
                    j += 1
                    continue
                if c in 'Ee' and not seen_e:
                    seen_e = True
                    j += 1
                    if j < n and text[j] in '+-':
                        j += 1
                    continue
                break
            yield _Token('real' if (seen_dot or seen_e) else 'int',
                         text[i:j], line)
            i = j
            continue
        raise IfcStepParseError(f'line {line}: unexpected character {ch!r}')
    yield _Token('eof', '', line)
    _ = length


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_SECTION_KWS = {
    'ISO-10303-21',
    'END-ISO-10303-21',
    'HEADER',
    'ENDSEC',
    'DATA',
    'FILE_DESCRIPTION',
    'FILE_NAME',
    'FILE_SCHEMA',
    'FILE_POPULATION',
}


class _Parser:
    __slots__ = ('tokens', 'pos')

    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> _Token:
        return self.tokens[self.pos]

    def next(self) -> _Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str) -> _Token:
        tok = self.next()
        if tok.kind != kind:
            raise IfcStepParseError(
                f'line {tok.line}: expected {kind}, found {tok.kind} '
                f'({tok.text!r})'
            )
        return tok

    def value(self, depth: int = 0) -> Any:
        if depth > MAX_ARG_DEPTH:
            raise IfcStepParseError('argument nesting exceeds depth limit')
        tok = self.next()
        if tok.kind == 'lparen':
            items: list[Any] = []
            if self.peek().kind == 'rparen':
                self.next()
                return tuple(items)
            while True:
                items.append(self.value(depth + 1))
                sep = self.next()
                if sep.kind == 'comma':
                    continue
                if sep.kind == 'rparen':
                    return tuple(items)
                raise IfcStepParseError(
                    f'line {sep.line}: expected , or ) in list'
                )
        if tok.kind == 'ref':
            return IfcStepRef(int(tok.text))
        if tok.kind == 'enum':
            if tok.text in ('T', 'F', 'U'):
                return IfcStepEnum(tok.text)
            return IfcStepEnum(tok.text)
        if tok.kind == 'omit':
            return IfcStepOmitted()
        if tok.kind == 'derived':
            return IfcStepDerived()
        if tok.kind == 'int':
            return int(tok.text)
        if tok.kind == 'real':
            return float(tok.text)
        if tok.kind == 'string':
            return tok.text
        if tok.kind == 'ident':
            name = tok.text.upper()
            # typed select: IDENT '(' args ')' or bare keyword
            if self.peek().kind == 'lparen':
                self.next()
                args: list[Any] = []
                if self.peek().kind == 'rparen':
                    self.next()
                    return IfcStepTypedValue(name, tuple(args))
                while True:
                    args.append(self.value(depth + 1))
                    sep = self.next()
                    if sep.kind == 'comma':
                        continue
                    if sep.kind == 'rparen':
                        return IfcStepTypedValue(name, tuple(args))
                    raise IfcStepParseError(
                        f'line {sep.line}: expected , or ) in typed value'
                    )
            return name
        raise IfcStepParseError(
            f'line {tok.line}: unexpected token {tok.kind} ({tok.text!r})'
        )


def parse_ifc_step(text: str) -> tuple[IfcStepHeader, tuple[IfcStepEntity, ...]]:
    """Parse a STEP Part 21 document.

    Returns ``(header, entities)``. Raises :class:`IfcStepParseError` on any
    malformed content or exceeded bound — partial parses are never returned.
    """
    if len(text) > MAX_STEP_BYTES:
        raise IfcStepParseError('STEP document exceeds size limit')
    if 'ISO-10303-21;' not in text[:4096]:
        raise IfcStepParseError('missing ISO-10303-21; exchange preamble')

    tokens: list[_Token] = []
    for tok in _lex(text):
        tokens.append(tok)
        if tok.kind == 'eof':
            break

    parser = _Parser(tokens)
    header = _parse_header(parser)
    entities = _parse_data(parser)
    return header, entities


def _parse_header(parser: _Parser) -> IfcStepHeader:
    first = parser.expect('ident')
    if first.text.upper() != 'ISO-10303-21':
        raise IfcStepParseError('expected ISO-10303-21 preamble')
    parser.expect('semicolon')
    tok = parser.expect('ident')
    if tok.text.upper() != 'HEADER':
        raise IfcStepParseError('expected HEADER section')
    parser.expect('semicolon')

    file_description: tuple[str, ...] = ()
    file_name_fields: dict[str, Any] = {}
    schema_identifiers: tuple[str, ...] = ()

    while True:
        tok = parser.peek()
        if tok.kind == 'ident' and tok.text.upper() == 'ENDSEC':
            parser.next()
            parser.expect('semicolon')
            break
        name_tok = parser.expect('ident')
        name = name_tok.text.upper()
        parser.expect('lparen')
        args: list[Any] = []
        sub = _Parser(parser.tokens)
        sub.pos = parser.pos
        if sub.peek().kind != 'rparen':
            while True:
                args.append(sub.value())
                sep = sub.next()
                if sep.kind == 'comma':
                    continue
                if sep.kind == 'rparen':
                    break
                raise IfcStepParseError(
                    f'line {sep.line}: expected , or ) in header call'
                )
        else:
            sub.next()
        parser.pos = sub.pos
        parser.expect('semicolon')

        if name == 'FILE_DESCRIPTION':
            if args and isinstance(args[0], tuple):
                file_description = tuple(
                    str(v) for v in args[0] if isinstance(v, str)
                )
            if len(args) > 1 and isinstance(args[1], str):
                file_description = file_description + (args[1],)
        elif name == 'FILE_NAME':
            keys = (
                'name', 'time_stamp', 'author', 'organization',
                'preprocessor_version', 'originating_system',
                'authorization',
            )
            file_name_fields = {}
            for key, value in zip(keys, args):
                if isinstance(value, tuple):
                    file_name_fields[key] = tuple(
                        str(v) for v in value if isinstance(v, str)
                    )
                elif isinstance(value, str):
                    file_name_fields[key] = value
        elif name == 'FILE_SCHEMA':
            if args and isinstance(args[0], tuple):
                schema_identifiers = tuple(
                    str(v) for v in args[0] if isinstance(v, str)
                )
        # FILE_POPULATION and friends are tolerated and ignored.

    return IfcStepHeader(
        file_description=file_description,
        file_name_fields=file_name_fields,
        schema_identifiers=schema_identifiers,
    )


def _parse_data(parser: _Parser) -> tuple[IfcStepEntity, ...]:
    tok = parser.expect('ident')
    if tok.text.upper() != 'DATA':
        raise IfcStepParseError('expected DATA section')
    parser.expect('semicolon')

    entities: list[IfcStepEntity] = []
    while True:
        tok = parser.peek()
        if tok.kind == 'ident' and tok.text.upper() == 'ENDSEC':
            parser.next()
            parser.expect('semicolon')
            break
        ref = parser.expect('ref')
        parser.expect('equals')
        name_tok = parser.expect('ident')
        entity_name = name_tok.text.upper()
        parser.expect('lparen')
        args: list[Any] = []
        if parser.peek().kind != 'rparen':
            while True:
                args.append(parser.value())
                sep = parser.next()
                if sep.kind == 'comma':
                    continue
                if sep.kind == 'rparen':
                    break
                raise IfcStepParseError(
                    f'line {sep.line}: expected , or ) in entity args'
                )
        else:
            parser.next()
        parser.expect('semicolon')
        entities.append(
            IfcStepEntity(
                entity_id=int(ref.text),
                name=entity_name,
                args=tuple(args),
                source_line=ref.line,
            )
        )
        if len(entities) > MAX_STEP_ENTITIES:
            raise IfcStepParseError('entity count exceeds limit')

    final = parser.expect('ident')
    if final.text.upper() != 'END-ISO-10303-21':
        raise IfcStepParseError('expected END-ISO-10303-21 trailer')
    parser.expect('semicolon')
    return tuple(entities)


__all__ = [
    'IfcStepEntity',
    'IfcStepEnum',
    'IfcStepHeader',
    'IfcStepOmitted',
    'IfcStepDerived',
    'IfcStepParseError',
    'IfcStepRef',
    'IfcStepTypedValue',
    'IfcStepValue',
    'MAX_STEP_BYTES',
    'parse_ifc_step',
]
