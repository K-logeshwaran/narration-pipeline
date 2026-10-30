#!/usr/bin/env bash
# Transparent Bob clips for CapCut.
#   ./render_char.sh projects/<name>                      all scenes, .mov (ProRes 4444 alpha), 540px
#   ./render_char.sh projects/<name> --combined           + one clip for the whole video
#   ./render_char.sh projects/<name> --scenes 1,3-4       only some scenes
#   ./render_char.sh projects/<name> --format webm        VP9 alpha (tiny files)
#   ./render_char.sh projects/<name> --force              re-render even if unchanged
# Anything after the project folder is passed straight to `python -m render.char_only`
# (later flags override the defaults below, e.g. --height 720 or --character atlas).
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -lt 1 ]; then
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi
PROJECT="$1"; shift

if [ -z "${VIRTUAL_ENV:-}" ] && [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

exec python3 -m render.char_only "$PROJECT" \
  --assets assets --character CBbob --height 540 "$@"
