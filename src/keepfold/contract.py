"""Teaching-contract helpers: scope `## Domain overrides` to the domains in play (wiki spec §2)."""

from __future__ import annotations

import re

MAX_CHARS = 4800  # ≈1,200 tokens; a 20-rule contract fits (tools.md)
_H = re.compile(r"^(#{2,3})\s+(.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def scope_contract(text: str, domains: set[str]) -> str:
    """The contract with only the `### <domain id>` overrides whose id is in `domains`.

    No `## Domain overrides` section: returned unchanged. A section with no match is dropped.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_ov = keep = fenced = False
    pre: list[str] = []  # the section heading and preamble, kept only if a subsection matches
    emitted = False
    want = {d.casefold() for d in domains}
    for line in lines:
        if _FENCE.match(line):
            fenced = not fenced
        m = None if fenced else _H.match(line)
        if m and m.group(1) == "##":
            in_ov = m.group(2).casefold() == "domain overrides"
            keep, emitted = False, False
            if in_ov:
                pre = [line]
                continue
        elif in_ov and m:  # a ### subsection
            keep = m.group(2).casefold() in want
            if keep and not emitted:
                out += pre
                emitted = True
        elif in_ov and not keep and not emitted:
            pre.append(line)
            continue
        if not in_ov or keep:
            out.append(line)
    return "".join(out)
