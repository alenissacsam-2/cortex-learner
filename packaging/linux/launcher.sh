#!/bin/bash
# /usr/bin/keepfold and /usr/bin/keepfold-app (the .deb installs this file as both): run the
# program of the same name from the newest Keepfold, the .deb's seed in /opt/keepfold/<version>
# or a per-user copy that Update now unpacked into ~/.local/share/keepfold/app/<version>
# (DESIGN.md 5.2). Versions compare with sort -V, so 0.10.0 beats 0.9.0; on equal versions the
# per-user copy wins. A folder counts only once it holds the program, and dot-folders never
# count (an update unpacks into one, then renames it into place).
set -u
name=$(basename "$0")
opt=${KEEPFOLD_OPT_ROOT:-/opt/keepfold}  # tests point this at a temp folder
user=${HOME:-}/.local/share/keepfold/app
tab=$(printf '\t')

list() {  # <root> <rank>: "<version> TAB <rank> TAB <folder>" per copy that holds the program
    local d
    for d in "$1"/[0-9]*/; do
        if [ -x "$d$name" ]; then
            d=${d%/}
            printf '%s\t%s\t%s\n' "${d##*/}" "$2" "$d"
        fi
    done
}

best=$({ list "$opt" 0; list "$user" 1; } | sort -t "$tab" -k1,1V -k2,2n | tail -n 1 | cut -f3)
if [ -z "$best" ]; then
    echo "$name: Keepfold is not installed (nothing in $opt or $user). Reinstall the keepfold package." >&2
    exit 1
fi
exec "$best/$name" "$@"
