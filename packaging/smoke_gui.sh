#!/bin/bash
# The built app's GUI smoke check (release-apps.yml): `bash packaging/smoke_gui.sh <app program>
# <screenshot.png>`, from the repo root. It starts the app with --smoke (keepfold.app.desktop
# Desktop.smoke: the window's page loads and the app answers on its own address, then it quits),
# takes a screenshot while the window is up, and fails unless the app exits 0. On Linux run it
# under xvfb-run and dbus-run-session; there the screenshot (Pillow, through `uv run --no-sync`)
# must show a drawn page, not an empty window (the spike's S2.6 check).
set -uo pipefail
app=$1
shot=$2
log=$HOME/.keepfold/logs/app.log  # keepfold.app.launch logs here when no terminal is attached
mkdir -p "$(dirname "$shot")"
if [ "$(uname)" != Darwin ] && [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    XDG_RUNTIME_DIR=$(mktemp -d)  # a desktop session always has one; WebKitGTK needs it
    export XDG_RUNTIME_DIR
fi

"$app" --smoke &
pid=$!
for _ in $(seq 120); do
    grep -qs 'smoke ok' "$log" && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
done
sleep 3  # the page's first paint after its load event

if [ "$(uname)" = Darwin ]; then
    screencapture -x "$shot"
else
    uv run --no-sync python - "$shot" <<'EOF'
import sys

from PIL import ImageGrab

img = ImageGrab.grab().convert("RGB")
img.save(sys.argv[1])
n = len(img.getcolors(1 << 24))
# an undrawn window is one flat colour on Xvfb's black root; a drawn page has text and edges
print(f"screenshot: {img.size[0]}x{img.size[1]}, {n} colours")
sys.exit(0 if n >= 32 else 1)
EOF
fi
shot_rc=$?

wait "$pid"
rc=$?
echo "----- app.log"
cat "$log"
echo "app exit code $rc, screenshot check $shot_rc"
[ "$rc" = 0 ] && [ "$shot_rc" = 0 ]
