#!/bin/sh
# SaaS uninstaller. Run as root:  saas-uninstall [--purge]
#   default:  removes program, service, udev rules. Keeps config + state.
#   --purge:  also removes /etc/saas, /var/lib/saas, the log and the saas user.
# Your sticks and the playlists on them are never touched.
set -u

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

if [ -d /run/systemd/system ]; then
	say "stopping + removing systemd service"
	systemctl disable --now saas 2>/dev/null
	rm -f /etc/systemd/system/saas.service
	systemctl daemon-reload
elif command -v openrc-run >/dev/null 2>&1; then
	say "stopping + removing OpenRC service"
	rc-service saas stop 2>/dev/null
	rc-update del saas default 2>/dev/null
	rm -f /etc/init.d/saas
else
	say "stopping + removing sysvinit script"
	[ -x /etc/init.d/saas ] && /etc/init.d/saas stop
	if command -v update-rc.d >/dev/null 2>&1; then update-rc.d -f saas remove >/dev/null 2>&1
	elif command -v chkconfig >/dev/null 2>&1; then chkconfig --del saas 2>/dev/null; fi
	rm -f /etc/init.d/saas
fi

say "removing udev rules + auto-mount helper"
rm -f /etc/udev/rules.d/99-saas-usb.rules /etc/udev/rules.d/60-saas-gpio.rules /usr/local/sbin/saas-automount
rm -f /usr/local/bin/saas-now /usr/local/sbin/saas-rollback /usr/local/sbin/saas-uninstall
rm -f /etc/polkit-1/rules.d/50-saas-poweroff.rules
udevadm control --reload 2>/dev/null

say "removing /opt/saas and the status files"
rm -rf /opt/saas /dev/shm/saas /run/saas

if [ "$PURGE" = 1 ]; then
	say "purging config, state, log and user"
	rm -rf /etc/saas /var/lib/saas /var/log/saas.log
	if id saas >/dev/null 2>&1; then
		if command -v userdel >/dev/null 2>&1; then userdel saas; else deluser saas; fi
	fi
	echo "SaaS removed completely (sticks still mounted under /media/usb stay mounted until unplugged)."
else
	echo "SaaS removed. Kept: /etc/saas (config), /var/lib/saas (state). Use --purge to remove those too."
fi
