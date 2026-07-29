"""A parser for the Lua table dialect DCS writes its mission files in.

DCS stores a mission as Lua source: one top-level assignment whose value is a
deeply nested table of strings, numbers, booleans and further tables. It is not
JSON and it is not executable data we want to run, so this reads it as data.

Only the subset DCS actually emits is supported -- table constructors, literals
and comments. There are no function calls, operators or variable references in a
mission file, and anything unrecognised raises rather than being guessed at.

Deliberately not using a real Lua interpreter. The mission file needs no
evaluation, and embedding one would add a compiled dependency for no gain.
(DCS's *weapon* definitions are a different story -- those genuinely are built
by function calls, which is why weapon names come from a curated table instead.)
"""

from __future__ import annotations

import re

BACKSLASH = chr(92)

TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>--\[\[.*?\]\]|--[^\n]*)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<number>-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<punct>[{}\[\]=,;])
    """,
    re.VERBOSE | re.DOTALL,
)

# Lua string escapes. A backslash immediately before a newline means a literal
# newline -- DCS uses that heavily to embed multi-line briefing text.
SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    BACKSLASH: BACKSLASH,
    '"': '"',
    "'": "'",
    "\n": "\n",
}


class LuaParseError(ValueError):
    """The source was not the table dialect this understands."""


def _unescape(raw: str) -> str:
    """Decode a quoted Lua string literal, quotes included."""
    body = raw[1:-1]
    if BACKSLASH not in body:
        return body

    out: list[str] = []
    i = 0
    length = len(body)
    while i < length:
        ch = body[i]
        if ch != BACKSLASH:
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= length:
            break
        nxt = body[i]
        if nxt in SIMPLE_ESCAPES:
            out.append(SIMPLE_ESCAPES[nxt])
            i += 1
        elif nxt.isdigit():
            digits = ""
            while i < length and body[i].isdigit() and len(digits) < 3:
                digits += body[i]
                i += 1
            out.append(chr(int(digits)))
        elif nxt == "x":
            hexits = body[i + 1 : i + 3]
            i += 1 + len(hexits)
            out.append(chr(int(hexits, 16)) if hexits else "x")
        else:
            # Unknown escape: keep the character, which is what Lua does.
            out.append(nxt)
            i += 1
    return "".join(out)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    length = len(text)
    while pos < length:
        match = TOKEN_RE.match(text, pos)
        if not match:
            snippet = text[pos : pos + 40].replace("\n", " ")
            raise LuaParseError(f"unexpected input at offset {pos}: {snippet!r}")
        pos = match.end()
        kind = match.lastgroup
        if kind in ("ws", "comment"):
            continue
        tokens.append((kind, match.group()))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise LuaParseError("unexpected end of input")
        self.i += 1
        return token

    def expect(self, value: str) -> None:
        kind, text = self.next()
        if text != value:
            raise LuaParseError(f"expected {value!r}, found {text!r}")

    def parse_value(self):
        kind, text = self.next()
        if text == "{":
            return self.parse_table()
        if kind == "string":
            return _unescape(text)
        if kind == "number":
            return float(text) if ("." in text or "e" in text.lower()) else int(text)
        if kind == "name":
            if text == "true":
                return True
            if text == "false":
                return False
            if text == "nil":
                return None
            # A bare identifier is not something a mission file contains.
            raise LuaParseError(f"unexpected identifier {text!r}")
        raise LuaParseError(f"unexpected token {text!r}")

    def parse_table(self) -> dict | list:
        """Return a dict, or a list when the table is a clean 1..n sequence."""
        result: dict = {}
        auto_index = 1

        while True:
            token = self.peek()
            if token is None:
                raise LuaParseError("unterminated table")
            if token[1] == "}":
                self.next()
                break

            if token[1] == "[":
                self.next()
                key_kind, key_text = self.next()
                if key_kind == "string":
                    key = _unescape(key_text)
                elif key_kind == "number":
                    key = int(float(key_text))
                else:
                    raise LuaParseError(f"bad table key {key_text!r}")
                self.expect("]")
                self.expect("=")
                result[key] = self.parse_value()
            elif token[0] == "name" and self.i + 1 < len(self.tokens) and self.tokens[self.i + 1][1] == "=":
                self.next()
                self.expect("=")
                result[token[1]] = self.parse_value()
            else:
                result[auto_index] = self.parse_value()
                auto_index += 1

            nxt = self.peek()
            if nxt and nxt[1] in (",", ";"):
                self.next()

        return _maybe_list(result)


def _maybe_list(table: dict):
    """Convert a 1..n integer-keyed table into a list, preserving order."""
    if not table:
        return table
    keys = list(table)
    if all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [table[i] for i in range(1, len(keys) + 1)]
    return table


def loads(text: str):
    """Parse a bare Lua table constructor or literal."""
    parser = _Parser(_tokenize(text))
    value = parser.parse_value()
    return value


def load_assignments(text: str) -> dict:
    """Parse a file of ``name = <value>`` top-level assignments.

    A DCS mission file holds exactly one (``mission = {...}``); the dictionary
    and options files are the same shape. Returns ``{name: value}``.
    """
    tokens = _tokenize(text)
    parser = _Parser(tokens)
    out: dict = {}
    while parser.peek() is not None:
        kind, name = parser.next()
        if kind != "name":
            raise LuaParseError(f"expected an assignment name, found {name!r}")
        parser.expect("=")
        out[name] = parser.parse_value()
        nxt = parser.peek()
        if nxt and nxt[1] in (",", ";"):
            parser.next()
    return out
