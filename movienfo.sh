#!/usr/bin/env bash
# Wrapper to launch the movienfo NFO generator from anywhere.
# Any extra arguments are forwarded, e.g.:
#   ./movienfo.sh --dry-run
#   ./movienfo.sh --only "Edgar Wallace"
set -euo pipefail

# Resolve the directory this script lives in (so it works via symlink / any cwd).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "No .env found in $SCRIPT_DIR." >&2
    echo "Copy .env.example to .env and add your TMDB_API_KEY and MOVIE_DIRS." >&2
    exit 1
fi

exec python3 "$SCRIPT_DIR/movienfo.py" "$@"
