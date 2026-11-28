"""The release files' size budget (release-apps.yml): `python3 packaging/check_sizes.py <version>
<file>...` prints each file's size and exits 1 when one is more than 25% over its baseline in
packaging/sizes.json (keyed by the file name with the version as `<v>`). A null baseline is not
set yet and is skipped; a file with no key fails, so a renamed file gets a baseline too."""

import json
import sys
from pathlib import Path

SIZES = Path(__file__).with_name("sizes.json")
LIMIT = 1.25


def check(version: str, paths: list[str], sizes: dict) -> list[str]:
    """One line per file; a line that starts with FAIL fails the job."""
    out = []
    for p in map(Path, paths):
        key, size = p.name.replace(version, "<v>"), p.stat().st_size
        base = sizes.get(key, "missing")
        if base == "missing":
            out.append(f"FAIL  {p.name}: {size} bytes, no {key!r} in {SIZES.name}")
        elif base is None:
            out.append(f"skip  {p.name}: {size} bytes, no baseline yet")
        elif size > base * LIMIT:
            out.append(f"FAIL  {p.name}: {size} bytes, {size / base - 1:+.0%} on {base}")
        else:
            out.append(f"ok    {p.name}: {size} bytes, {size / base - 1:+.0%} on {base}")
    return out


if __name__ == "__main__":
    lines = check(sys.argv[1], sys.argv[2:], json.loads(SIZES.read_text(encoding="utf-8")))
    print("\n".join(lines))
    sys.exit(1 if any(s.startswith("FAIL") for s in lines) else 0)
