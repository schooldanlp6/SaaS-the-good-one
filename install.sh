#!/bin/sh
# SaaS install AND upgrade. Run as root from the unpacked folder:
#   ./install.sh             normal: installs the apt packages it needs
#   ./install.sh --offline   skip apt entirely (deps already there, e.g. OTA / no network)
# (uninstall: saas-uninstall [--purge], undo an OTA update: saas-rollback)
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

OFFLINE=0
for arg in "$@"; do
	case "$arg" in
		--offline) OFFLINE=1 ;;
		-h|--help) sed -n '2,12p' "$0"; exit 0 ;;
		*) echo "unknown option: $arg (try --help)" >&2; exit 1 ;;
	esac
done

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
if [ "$OFFLINE" = 1 ]; then
	say "offline: skipping apt, only checking what's installed"
elif command -v apt-get >/dev/null 2>&1; then
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
		[ "$OFFLINE" = 0 ] && command -v apt-get >/dev/null 2>&1 && apt-get install -y patch
		command -v patch >/dev/null 2>&1 || die "patches/ has patches but the 'patch' tool is missing"
	fi
	SRC=$(mktemp -d)
	trap 'rm -rf "$SRC"' EXIT
	say "applying patches"
	sh "$HERE/init/apply-patches.sh" "$HERE" "$SRC" || die "patching failed (see above)"
fi

# --- files ----------------------------------------------------------------
say "copying program to $PREFIX"
rm -rf "$PREFIX/saas" "$PREFIX/.previous"
cp -r "$SRC/saas" "$SRC/main.py" "$SRC/pctool.py" "$SRC/VERSION" "$PREFIX/"
# Root-run tools live outside /opt/saas: /opt/saas belongs to the saas user (so
# OTA can update it), and root must never execute files that user can change.
rm -f "$PREFIX/saas-now.sh" "$PREFIX/uninstall.sh"      # pre-0.6.2 locations
mkdir -p /usr/local/bin /usr/local/sbin
install -m 755 "$SRC/saas-now.sh" /usr/local/bin/saas-now
install -m 755 "$SRC/uninstall.sh" /usr/local/sbin/saas-uninstall
install -m 755 "$SRC/init/saas-rollback" /usr/local/sbin/saas-rollback
"$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); from saas.core.updater import system_hash; print(system_hash(sys.argv[1]))" \
	"$SRC" > "$PREFIX/SYSTEM_HASH"
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
chown -R saas:saas "$PREFIX"      # OTA updates write here
# the config can hold server passwords / API keys: root writes, saas reads, nobody else
chown root:saas "$ETC/config.toml" && chmod 640 "$ETC/config.toml"
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

# --- poweroff button ------------------------------------------------------------
# As root SaaS can just run `poweroff`. As the saas user under systemd, a polkit
# rule allows exactly that one action; without systemd it needs root.
if [ "$INIT" = systemd ] && [ -d /etc/polkit-1 ]; then
	say "allowing the saas user to power off (polkit)"
	mkdir -p /etc/polkit-1/rules.d
	install -m 644 "$SRC/init/50-saas-poweroff.rules" /etc/polkit-1/rules.d/50-saas-poweroff.rules
else
	warn "poweroff button: works when SaaS runs as root (no systemd+polkit found for the saas user)"
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
  Display:       saas-now        (two status lines for your dashboard)
  Upgrade:       unpack the new version, run its ./install.sh [--offline]
  OTA:           DLP6/Update/saas.pub on a stick, press update (U) twice
  Undo OTA:      saas-rollback
  Uninstall:     saas-uninstall   (--purge also removes config, state, user)
EOF
