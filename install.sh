#!/bin/sh
# SaaS install AND upgrade. Run as root from the unpacked folder:  ./install.sh
# (uninstall: /opt/saas/uninstall.sh [--purge])
#
# Upgrade = run the new version's install.sh: the service is stopped cleanly
# (that's when it saves its state), files are replaced, your config is kept,
# and the service is started again if it was running.
#
#   - installs python-mpv as a distro package (apt); no pip. GPIO needs nothing
#     extra: it talks to /dev/gpiochip* directly
#   - applies patches/*.patch (if any) to a staging copy
#   - copies the program to /opt/saas, config to /etc/saas/config.toml (kept if present)
#   - creates the "saas" system user (groups: audio, gpio, plugdev)
#   - udev rule so the gpio group may use /dev/gpiochip* (no root needed)
#   - installs the udev auto-mount rule for USB sticks (/media/usb/<label>, rw+sync)
#   - installs + enables the service for whatever init system is running
# A fresh install is not started: set [audio] device in the config first.
set -eu

PREFIX=/opt/saas
ETC=/etc/saas
STATE=/var/lib/saas
HERE=$(cd "$(dirname "$0")" && pwd)

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (su - , then ./install.sh)"

NEW_VERSION=$(cat "$HERE/VERSION" 2>/dev/null || echo "?")
OLD_VERSION=""
if [ -f "$PREFIX/main.py" ]; then
	OLD_VERSION=$(cat "$PREFIX/VERSION" 2>/dev/null || echo "unknown")
	say "upgrading SaaS $OLD_VERSION -> $NEW_VERSION"
else
	say "installing SaaS $NEW_VERSION"
fi

# --- init system helpers ------------------------------------------------------
if [ -d /run/systemd/system ]; then INIT=systemd
elif command -v openrc-run >/dev/null 2>&1; then INIT=openrc
else INIT=sysv; fi

svc() {  # svc start|stop|status
	case "$INIT" in
		systemd) case "$1" in status) systemctl is-active --quiet saas ;; *) systemctl "$1" saas ;; esac ;;
		openrc) rc-service saas "$1" >/dev/null 2>&1 ;;
		sysv) [ -x /etc/init.d/saas ] && /etc/init.d/saas "$1" >/dev/null 2>&1 ;;
	esac
}

WAS_RUNNING=0
if [ -n "$OLD_VERSION" ] && svc status; then
	WAS_RUNNING=1
	say "stopping the running service (it saves its state now)"
	svc stop || warn "could not stop the service"
fi

# --- dependencies (distro packages only, no pip) -----------------------------
if command -v apt-get >/dev/null 2>&1; then
	say "installing packages (apt)"
	apt-get update
	apt-get install -y python3 python3-mpv
else
	warn "no apt: install python3 (>= 3.11) and python-mpv with your distro's package manager"
fi

PY=$(command -v python3) || die "python3 not found"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
	|| die "Python >= 3.11 needed (found $("$PY" -V 2>&1))"
"$PY" -c 'import mpv' 2>/dev/null \
	|| die "python can't import mpv - install your distro's python-mpv package (Debian/Pi OS: python3-mpv)"
mkdir -p "$PREFIX"

# --- patches ----------------------------------------------------------------
# Everything is installed from a staging copy with patches/*.patch applied;
# the unpacked folder itself stays untouched.
SRC=$HERE
if ls "$HERE"/patches/*.patch >/dev/null 2>&1; then
	if ! command -v patch >/dev/null 2>&1; then
		command -v apt-get >/dev/null 2>&1 && apt-get install -y patch
		command -v patch >/dev/null 2>&1 || die "patches/ has patches but the 'patch' tool is missing"
	fi
	SRC=$(mktemp -d)
	trap 'rm -rf "$SRC"' EXIT
	say "applying patches"
	sh "$HERE/init/apply-patches.sh" "$HERE" "$SRC" || die "patching failed (see above)"
fi

# --- files ----------------------------------------------------------------
say "copying program to $PREFIX"
rm -rf "$PREFIX/saas"
cp -r "$SRC/saas" "$SRC/main.py" "$PREFIX/"
cp "$SRC/VERSION" "$PREFIX/VERSION" 2>/dev/null || true
install -m 755 "$SRC/uninstall.sh" "$PREFIX/uninstall.sh"
install -m 755 "$SRC/saas-now.sh" "$PREFIX/saas-now.sh"
find "$PREFIX/saas" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

mkdir -p "$ETC"
if [ -f "$ETC/config.toml" ]; then
	say "keeping existing $ETC/config.toml"
else
	cp "$SRC/config/pi.toml" "$ETC/config.toml"
	say "installed $ETC/config.toml"
fi

# --- user -----------------------------------------------------------------
if ! id saas >/dev/null 2>&1; then
	say "creating user saas"
	if command -v useradd >/dev/null 2>&1; then
		useradd --system --user-group --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin saas
	else
		addgroup -S saas && adduser -S -D -H -h "$STATE" -s /sbin/nologin -G saas saas
	fi
fi
if ! grep -q "^gpio:" /etc/group; then
	say "creating group gpio"
	if command -v groupadd >/dev/null 2>&1; then groupadd --system gpio; else addgroup -S gpio; fi
fi
for g in audio gpio plugdev; do
	if grep -q "^$g:" /etc/group; then
		if command -v usermod >/dev/null 2>&1; then usermod -aG "$g" saas; else addgroup saas "$g"; fi
	fi
done
mkdir -p "$STATE" && chown saas:saas "$STATE"
touch /var/log/saas.log && chown saas:saas /var/log/saas.log

# --- udev auto-mount --------------------------------------------------------
say "installing USB auto-mount (udev)"
command -v udevadm >/dev/null 2>&1 || warn "udev not found - USB auto-mount and GPIO permissions need udev"
mkdir -p /etc/udev/rules.d /usr/local/sbin
install -m 755 "$SRC/init/saas-automount" /usr/local/sbin/saas-automount
install -m 644 "$SRC/init/99-saas-usb.rules" /etc/udev/rules.d/99-saas-usb.rules
mkdir -p /media/usb
# Before 0.4 a single stick was mounted directly on /media/usb. Unmount that,
# the sticks get re-mounted the new way (/media/usb/<label>) below.
if mountpoint -q /media/usb 2>/dev/null; then
	say "unmounting old-style /media/usb mount"
	umount /media/usb 2>/dev/null || umount -l /media/usb
fi

say "installing GPIO access rule (udev)"
install -m 644 "$SRC/init/60-saas-gpio.rules" /etc/udev/rules.d/60-saas-gpio.rules
if udevadm control --reload 2>/dev/null; then
	udevadm trigger --subsystem-match=gpio 2>/dev/null || true
	# mount sticks that are already plugged in
	udevadm trigger --action=add --subsystem-match=block --sysname-match='sd*' 2>/dev/null || true
else
	warn "could not reload udev rules (reboot will do it)"
fi

# --- service --------------------------------------------------------------
subst() { sed -e "s|@PYTHON@|$PY|g" -e "s|@PREFIX@|$PREFIX|g" "$1" > "$2"; }

if [ "$INIT" = systemd ]; then
	say "installing systemd unit"
	subst "$SRC/init/saas.service" /etc/systemd/system/saas.service
	systemctl daemon-reload
	systemctl enable saas
	START="systemctl start saas      (logs: journalctl -u saas -f)"
elif [ "$INIT" = openrc ]; then
	say "installing OpenRC service"
	subst "$SRC/init/saas.openrc" /etc/init.d/saas && chmod 755 /etc/init.d/saas
	rc-update add saas default
	START="rc-service saas start     (logs: tail -f /var/log/saas.log)"
else
	say "installing sysvinit script"
	subst "$SRC/init/saas.sysv" /etc/init.d/saas && chmod 755 /etc/init.d/saas
	if command -v update-rc.d >/dev/null 2>&1; then update-rc.d saas defaults
	elif command -v chkconfig >/dev/null 2>&1; then chkconfig --add saas
	else warn "enable /etc/init.d/saas for your runlevels manually"; fi
	START="/etc/init.d/saas start    (logs: tail -f /var/log/saas.log)"
fi

CHECK="su -s /bin/sh saas -c \"$PY $PREFIX/main.py --config $ETC/config.toml --check\""

if [ -n "$OLD_VERSION" ]; then
	if [ "$WAS_RUNNING" = 1 ]; then
		if su -s /bin/sh saas -c "$PY $PREFIX/main.py --config $ETC/config.toml --check" >/dev/null 2>&1; then
			svc start && say "service restarted"
		else
			warn "config check failed - service NOT restarted. Run: $CHECK"
		fi
	fi
	say "upgrade to $NEW_VERSION done (config kept: $ETC/config.toml)"
	exit 0
fi

cat <<EOF

Done. Next:
  1. $PY $PREFIX/main.py --list-devices
  2. put your Scarlett's device name into $ETC/config.toml ([audio] device)
     and check the GPIO pins match your wiring
  3. as root: $CHECK
  4. $START
  GPIO check: ls -l /dev/gpiochip*   (should be group gpio, crw-rw----)
  Upgrade later: unpack the new version, run its ./install.sh
  Uninstall:     $PREFIX/uninstall.sh   (--purge also removes config, state, user)
EOF
