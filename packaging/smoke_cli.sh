#!/bin/bash
# The built CLI's smoke checks (release-apps.yml), on the build runner and in clean Ubuntu
# containers: `bash packaging/smoke_cli.sh <keepfold CLI> <python3>`. `--version`, `doctor` and the
# MCP handshake (smoke_mcp.py) against a throwaway vault; with SOURCE_SELFTEST set (the source
# tree's `keepfold selftest` line) also `selftest`, whose counts must match it. Run it only where
# HOME is throwaway (a CI runner or container): `init` points ~/.keepfold at the new vault.
set -euo pipefail
cli=$1
py=$2
here=$(cd "$(dirname "$0")" && pwd)
vault=$(mktemp -d)/vault

"$cli" --version
"$cli" init "$vault"

# doctor exits 1 on any FAIL line, and some depend on the machine (a clean container has no git);
# what a freeze breaks shows as a traceback or a run that stops before its last section
out=$("$cli" doctor --vault "$vault" 2>&1) || true
echo "$out"
if grep -q Traceback <<<"$out" || ! grep -q '^Where Keepfold connects:' <<<"$out"; then
    echo "doctor did not run to the end"
    exit 1
fi

"$py" "$here/smoke_mcp.py" "$cli" "$vault"

if [ -n "${SOURCE_SELFTEST:-}" ]; then
    frozen=$("$cli" selftest)
    echo "source: $SOURCE_SELFTEST"
    echo "built:  $frozen"
    # "selftest ok N modules M files"; the build adds pyproject's force-include, the Help tab's
    # help.md and GUIDE.md, which the source tree keeps outside the package
    read -r _ _ src_mods _ src_files _ <<<"$SOURCE_SELFTEST"
    read -r _ _ mods _ files _ <<<"$frozen"
    if [ "$mods" != "$src_mods" ] || [ "$files" != $((src_files + 2)) ]; then
        echo "selftest counts differ: want $src_mods modules $((src_files + 2)) files"
        exit 1
    fi
fi
echo "cli smoke ok"
