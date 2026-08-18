r"""Jackson-compatible JSON writer for Bitbucket migration export files.

Matches the real exporter byte-for-byte:
- pretty mode: 2-space indent, `"key" : value` (space around colon), arrays of
  objects as `[ { ... }, { ... } ]`, empty array `[ ]`, no trailing newline.
- string escaping: control chars <0x20, `"` `\\`, U+2028/U+2029 backslash-escaped
  (`\uXXXX`); BMP non-ASCII written as raw UTF-8; non-BMP (emoji) written as two
  `\uXXXX` surrogate escapes (lowercase hex).
- keys serialized in the order given (callers pre-sort where FORMAT_SPEC demands
  alphabetical order).
"""
import json


def _esc(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\b':
            out.append('\\b')
        elif ch == '\f':
            out.append('\\f')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif o < 0x20 or o in (0x2028, 0x2029):
            out.append('\\u%04X' % o)
        elif 0x20 <= o < 0x80:
            out.append(ch)
        elif o >= 0x10000:
            v = o - 0x10000
            hi = 0xD800 + (v >> 10)
            lo = 0xDC00 + (v & 0x3FF)
            out.append('\\u%04X\\u%04X' % (hi, lo))
        elif 0xD800 <= o <= 0xDFFF:
            out.append('\\u%04X' % o)
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def _scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return _esc(v)
    raise TypeError(f"not serializable: {v!r}")


def _pretty(v, indent: int) -> str:
    pad = "  " * indent
    if isinstance(v, dict):
        if not v:
            return "{ }"
        parts = []
        for k, val in v.items():
            parts.append(f'{pad}  {_esc(k)} : {_pretty(val, indent + 1)}')
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    if isinstance(v, (list, tuple)):
        if not v:
            return "[ ]"
        pieces = [_pretty(x, indent) for x in v]
        return "[ " + ", ".join(pieces) + " ]"
    return _scalar(v)


def pretty(v) -> str:
    return _pretty(v, 0)


def compact(v, key_order=None) -> str:
    """Compact single-line JSON, alphabetical keys by default, Jackson escaping."""
    if isinstance(v, dict):
        keys = key_order or sorted(v.keys())
        body = ",".join(f"{_esc(k)}:{compact(v[k])}" for k in keys)
        return "{" + body + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(compact(x) for x in v) + "]"
    return _scalar(v)