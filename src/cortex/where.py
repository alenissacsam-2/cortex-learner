"""FW: cortex_find's `where`, frontmatter filters such as `status=open tags=dsa due<=2026-12-01`.

`parse(text)` turns up to MAX_CLAUSES `field op value` clauses into (field, op, value) tuples
(field casefolded), raising invalid_argument; `match(clauses, fields)` is true when every clause
passes on a page's frontmatter (index `_plain` form). Pure: no vault, no index."""

from __future__ import annotations

import re

from cortex.vault import invalid

MAX_CLAUSES = 6
OPS = ("!=", "<=", ">=", "=", "<", ">", "~")
EXAMPLE = "for example where='status=open tags=dsa due<=2026-12-01 title~\"binary search\"'"
_CLAUSE = re.compile(r'([\w-]+)(!=|<=|>=|=|<|>|~)("[^"]*"|[^\s"]*)')
_LINK = re.compile(r"^\[\[([^\]|#^]+)(?:[|#^][^\]]*)?\]\]$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])")
_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")


def _bad(why: str) -> Exception:
    return invalid(f"where: {why}; clauses are field, operator, value with no spaces "
                   f"(operators {' '.join(OPS)}; quote a value with spaces), {EXAMPLE}.")


def parse(text) -> list[tuple[str, str, str]]:
    if not isinstance(text, str) or not text.strip():
        raise _bad("it is blank")
    out, pos, n = [], 0, len(text)
    while True:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        tok = text[pos:].split(None, 1)[0]
        m = _CLAUSE.match(text, pos)
        if not m:
            raise _bad(f"'{tok}' has no operator" if not re.search(r"[=!<>~]", tok)
                       else f"'{tok}' has an unknown operator or no field")
        field, op, value = m.groups()
        end = m.end()
        if value.startswith('"'):
            value = value[1:-1]
        elif value[:1] in ("=", "!", "<", ">", "~"):  # ==, =>, <>, =~ and the like
            raise _bad(f"'{tok}' has an unknown operator")
        if end < n and not text[end].isspace():
            raise _bad(f"'{tok}' has an unbalanced quote" if '"' in text[pos:end + 1]
                       else f"'{tok}' has an unknown operator")
        out.append((field.casefold(), op, value))
        if len(out) > MAX_CLAUSES:
            raise _bad(f"at most {MAX_CLAUSES} clauses")
        pos = end
    return out


def _norm(v) -> str:
    """Casefolded and stripped; a `[[id]]` or `[[id|label]]` link compares as its bare id."""
    s = str(v).strip()
    if m := _LINK.match(s):
        s = m.group(1).strip()
    return s.casefold()


def _order(a, b) -> tuple | None:
    """Both days (a timestamp by its first 10 characters) or both numbers, else None."""
    if isinstance(a, str) and _DAY.match(a.strip()) and _DAY.match(b.strip()):
        return a.strip()[:10], b.strip()[:10]
    if isinstance(a, bool):
        return None
    if isinstance(a, (int, float)) or isinstance(a, str) and _NUM.match(a.strip()):
        if _NUM.match(b.strip()):
            return float(a), float(b)
    return None


def _one(op: str, field, value: str) -> bool:
    if op in ("=", "!="):
        return (_norm(field) == _norm(value)) == (op == "=")
    if op == "~":
        return _norm(value) in _norm(field)
    pair = _order(field, value)
    if pair is None:
        return False  # no string ordering: "10" < "9" would surprise
    a, b = pair
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def match(clauses, fields: dict) -> bool:
    if not clauses:
        return True
    by = {str(k).casefold(): v for k, v in fields.items()}
    for key, op, value in clauses:
        v = by.get(key)
        if v is None:
            if op != "!=":
                return False
            continue
        items = v if isinstance(v, list) else [v]
        if op == "!=":
            ok = all(_one("!=", x, value) for x in items)  # none equals
        else:
            ok = any(_one(op, x, value) for x in items)
        if not ok:
            return False
    return True
