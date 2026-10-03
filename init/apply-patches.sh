#!/bin/sh
# apply-patches.sh SRC STAGE
# Copies SRC to STAGE and applies SRC/patches/*.patch (name order) to STAGE.
# A patch that is already contained in the code is skipped, so re-running an
# install or shipping a tarball that already includes a patch is harmless.
set -eu

SRC=$1
STAGE=$2

mkdir -p "$STAGE"
cp -R "$SRC/." "$STAGE/"

found=0
for p in "$SRC"/patches/*.patch; do
	[ -e "$p" ] || continue
	found=1
	name=$(basename "$p")
	if patch -d "$STAGE" -p1 -F0 -R -s -f --dry-run < "$p" >/dev/null 2>&1; then
		echo "patch $name: already contained, skipped"
	elif patch -d "$STAGE" -p1 -F0 -s -f --dry-run < "$p" >/dev/null 2>&1; then
		patch -d "$STAGE" -p1 -F0 -s -f --no-backup-if-mismatch < "$p"
		echo "patch $name: applied"
	else
		echo "patch $name: does NOT apply to this version - aborting" >&2
		patch -d "$STAGE" -p1 -F0 -f --dry-run < "$p" >&2 || true
		exit 1
	fi
done
[ "$found" = 1 ] || echo "no patches"
exit 0
