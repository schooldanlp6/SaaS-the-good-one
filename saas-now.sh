#!/usr/bin/env bash
# saas-now.sh - two display lines from SaaS (pure bash, no python/jq).
#
#   Playing: Song Name, Artist - Album - (4:56), Track 3/12 Folder 2/20
#   Save to: Favorites                                   <3
#
# Line 2: the last action ("Saved to Favorites", "Liked", "Medium: STICK_B")
# for a few seconds, otherwise where the playlist button saves to (and whether
# the song is already in there). The like icon sits at the end of line 2.
#
# Use:     saas-now.sh               -> prints the 2 lines
#          mapfile -t L < <(saas-now.sh); echo "${L[0]}" "${L[1]}"
#          source it to get the saas_lines function + SAAS_* variables:
#            . /opt/saas/saas-now.sh; saas_lines; echo "$SAAS_LINE1"
# Env:     SAAS_VARS            (default /dev/shm/saas/now.vars)
#          SAAS_NOTICE_SECONDS  how long an action stays on line 2 (default 5)
#          SAAS_WIDTH           cut both lines to N chars (e.g. 20 for a VFD; 0 = no cut)
#          SAAS_LIKE_ICON       default "<3" (use e.g. "♥" if your display has it)

# Only these keys are taken from the file. Numbers must be plain digits: bash
# arithmetic would otherwise *execute* things like a[$(cmd)] hidden in a value.
SAAS_NUM_KEYS=" MEDIA VOLUME MEDIUM_INDEX MEDIUM_COUNT FOLDER_INDEX FOLDER_COUNT FOLDER_IS_PLAYLIST
 TRACK_INDEX TRACK_COUNT DURATION POSITION POSITION_AT IN_PLAYLIST LIKED NOTICE_TIME "
SAAS_TEXT_KEYS=" STATUS VIEW MEDIUM FOLDER FILE TITLE ARTIST ALBUM PLAYLIST NOTICE "

saas_read() {
	local f=${SAAS_VARS:-/dev/shm/saas/now.vars} k v key
	SAAS_RUNNING=0
	[[ -r $f ]] || return 1
	# No `source`/eval: the file is written by the saas user, the dashboard may run as root.
	for key in $SAAS_NUM_KEYS; do printf -v "SAAS_$key" '%s' 0; done
	for key in $SAAS_TEXT_KEYS; do printf -v "SAAS_$key" '%s' ""; done
	while IFS='=' read -r k v; do
		key=${k#SAAS_}
		[[ $k == SAAS_* && $key =~ ^[A-Z_]+$ ]] || continue
		if [[ $SAAS_NUM_KEYS == *" $key "* ]]; then
			if [[ $v =~ ^[0-9]{1,15}$ ]]; then v=$((10#$v)); else v=0; fi
		elif [[ $SAAS_TEXT_KEYS != *" $key "* ]]; then
			continue
		fi
		printf -v "$k" '%s' "$v"
	done < "$f"
	SAAS_RUNNING=1
}

saas_mmss() { local s=${1:-0}; printf '%d:%02d' $((s / 60)) $((s % 60)); }

saas_lines() {
	local now=${EPOCHSECONDS:-$(date +%s)} width=${SAAS_WIDTH:-0} keep=${SAAS_NOTICE_SECONDS:-5} who status title
	[[ $width =~ ^[0-9]+$ ]] || width=0
	[[ $keep =~ ^[0-9]+$ ]] || keep=5
	if ! saas_read; then
		SAAS_LINE1="SaaS: not running"; SAAS_LINE2=""
	elif [[ $SAAS_MEDIA != 1 ]]; then
		SAAS_LINE1="No media"; SAAS_LINE2=""
	else
		case $SAAS_STATUS in
			playing) status="Playing" ;; paused) status="Paused" ;; *) status="Stop" ;;
		esac
		title=${SAAS_TITLE:-${SAAS_FILE%.*}}
		who=$SAAS_ARTIST
		[[ -n $SAAS_ALBUM ]] && who+="${who:+ - }$SAAS_ALBUM"
		SAAS_LINE1="$status: $title${who:+, $who}"
		((SAAS_DURATION > 0)) && SAAS_LINE1+=" - ($(saas_mmss "$SAAS_DURATION"))"
		local where="Folder"
		[[ $SAAS_VIEW == playlists ]] && where="Playlist"
		SAAS_LINE1+=", Track $SAAS_TRACK_INDEX/$SAAS_TRACK_COUNT $where $SAAS_FOLDER_INDEX/$SAAS_FOLDER_COUNT"
		((SAAS_MEDIUM_COUNT > 1)) && SAAS_LINE1+=" [$SAAS_MEDIUM]"

		if [[ -n $SAAS_NOTICE ]] && ((now - SAAS_NOTICE_TIME < keep)); then
			SAAS_LINE2=$SAAS_NOTICE
		elif [[ -n $SAAS_PLAYLIST ]]; then
			if [[ $SAAS_IN_PLAYLIST == 1 ]]; then SAAS_LINE2="Saved in: $SAAS_PLAYLIST"
			else SAAS_LINE2="Save to: $SAAS_PLAYLIST"; fi
		else
			SAAS_LINE2=""
		fi
	fi
	local icon=""
	[[ $SAAS_RUNNING == 1 && $SAAS_MEDIA == 1 && $SAAS_LIKED == 1 ]] && icon=${SAAS_LIKE_ICON:-<3}
	if ((width > 0)); then
		SAAS_LINE1=${SAAS_LINE1:0:width}
		if [[ -n $icon ]]; then  # icon always in the bottom-right corner
			local room=$((width - ${#icon} - 1))
			((room < 0)) && room=0
			printf -v SAAS_LINE2 '%-*s %s' "$room" "${SAAS_LINE2:0:room}" "$icon"
		else
			SAAS_LINE2=${SAAS_LINE2:0:width}
		fi
	elif [[ -n $icon ]]; then
		SAAS_LINE2+="${SAAS_LINE2:+  }$icon"
	fi
}

# executed (not sourced): print the two lines
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
	saas_lines
	printf '%s\n%s\n' "$SAAS_LINE1" "$SAAS_LINE2"
fi
