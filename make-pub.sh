#!/usr/bin/env bash
# make-pub.sh - build an OTA package (saas.pub) from this project folder.
#
#   ./make-pub.sh                       -> ./saas.pub
#   ./make-pub.sh -o /tmp/x.pub         -> that file
#   ./make-pub.sh --stick /media/usb/S  -> <stick>/DLP6/Update/saas.pub
#
# The package is a tar.gz with one top folder (saas-player/), the same layout
# as the release tarballs; the player accepts zip and tar.gz. Needs only bash
# and tar. If python3 is around, the Python files are syntax-checked first.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT="$ROOT/saas.pub"

die() { echo "make-pub: $*" >&2; exit 1; }

while (($#)); do
	case $1 in
		-o|--output) [[ $# -ge 2 ]] || die "$1 needs a path"; OUT=$2; shift 2 ;;
		--stick)     [[ $# -ge 2 ]] || die "$1 needs a path"
		             [[ -d $2 ]] || die "stick path $2 is not a directory"
		             OUT="$2/DLP6/Update/saas.pub"; shift 2 ;;
		-h|--help)   sed -n '2,11p' "$0"; exit 0 ;;
		*)           die "unknown option $1 (try --help)" ;;
	esac
done

# --- sanity checks --------------------------------------------------------------
[[ -f $ROOT/VERSION ]] || die "no VERSION file in $ROOT"
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
[[ $VERSION =~ ^[0-9]+(\.[0-9]+){0,3}$ ]] || die "VERSION '$VERSION' is not like 1.2.3"
for need in main.py saas/__init__.py saas/core/player.py install.sh; do
	[[ -f $ROOT/$need ]] || die "missing $need - run this from the SaaS project folder"
done
if command -v python3 >/dev/null 2>&1; then
	python3 - "$ROOT" <<'EOF' || die "syntax error (see above) - not packaging"
import os, sys
root, bad = sys.argv[1], 0
for d, dirs, files in os.walk(root):
    dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git", ".pytest_cache")]
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(d, f)
            try:
                compile(open(p, encoding="utf-8").read(), p, "exec")
            except SyntaxError as e:
                print(f"{p}:{e.lineno}: {e.msg}", file=sys.stderr); bad = 1
sys.exit(bad)
EOF
fi

# --- build ------------------------------------------------------------------------
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/saas-player"
tar -C "$ROOT" \
	--exclude='./.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='./.pytest_cache' \
	--exclude='./*.pub' --exclude='./*.tar.gz' --exclude='./.saas-state.json' \
	-cf - . | tar -C "$TMP/saas-player" -xf -

mkdir -p "$(dirname "$OUT")"
tar -C "$TMP" -czf "$TMP/saas.pub" saas-player
mv "$TMP/saas.pub" "$OUT"
sync "$OUT" 2>/dev/null || sync

echo "saas.pub $VERSION -> $OUT ($(du -h "$OUT" | cut -f1))"
