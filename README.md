# SaaS — ScriptAsASystem

A tiny headless music player core in Python. You get buttons, LEDs, a USB stick, and **folders = playlists**. Audio goes out through mpv to any ALSA device (here: a Focusrite Scarlett → Behringer mixer).
It's built for a Raspberry Pi 1 B+, but the same code runs on a laptop with only the config swapped.

```
main.py                  wires everything from the config, runs the event loop
saas/core/               events (bus), state, player (controller), library (folder walker), config, resume
saas/backends/           mpv_backend.py (real audio), dummy.py (silent fake, for dev/tests)
saas/inputs/             gpio.py (buttons), keyboard.py (dev)
saas/outputs/            leds.py, console.py
saas/sources/            usb.py (watches the mount points, one medium per stick)
saas/hw/                 gpiochip.py (GPIO via /dev/gpiochip*, stdlib only) + a mock chip
init/                    systemd / OpenRC / sysvinit service, udev USB auto-mount, udev GPIO access
config/                  dev.toml (laptop), pi.toml (Pi)
patches/                 drop *.patch here; install.sh applies them
pctool.py                PC playlist editor for the stick
install.sh / uninstall.sh
tests/                   pytest, no hardware needed
```

Plugins only talk to the core through events: inputs emit events, the player changes state, and outputs get state snapshots. An OLED later means one new file in `outputs/` plus three lines in `main.py`.

## On the laptop

```sh
# distro packages only (Debian/Ubuntu names)
sudo apt install python3-mpv python3-pytest
python3 main.py --config config/dev.toml    # set media_root in it first
```

Keys: `space` play/pause · `s` stop · `n`/`p` next/prev · `]`/`[` folder up/down · `+`/`-` volume · `a` save/remove song in playlist · `l` next playlist · `f` like · `v` folders ⇄ playlists · `*` next medium · `q` quit.
Rename the music folder while it plays to simulate yanking the stick. To test `*`, set `layout = "multi"` and point `media_root` at a folder whose subfolders act as sticks.
No libmpv? Set `backend = "dummy"` (every track "plays" silently for 10 s).

Tests: `python3 -m pytest -q`

## On the Pi: install, upgrade, uninstall

Everything is done as root (`su -`):

| | |
|---|---|
| **Install** | unpack, `./install.sh` |
| **Upgrade** | unpack the new version, run *its* `./install.sh`. It stops the service cleanly (that's when the state is saved), replaces the program, keeps `/etc/saas/config.toml`, and restarts the service if it was running. |
| **Patches** | drop `*.patch` files into `patches/` before running `./install.sh`. They're applied in name order to a staging copy. A patch that's already contained is skipped, one that doesn't apply exactly stops the install. Your unpacked folder is never modified. |
| **Uninstall** | `/opt/saas/uninstall.sh` keeps config and state; `--purge` removes everything incl. the `saas` user. Sticks and their playlists are never touched. |

After a fresh install:
1. `python3 /opt/saas/main.py --list-devices`, then put your DAC's name (the `alsa/plughw:CARD=…` one) into `[audio] device`.
2. `su -s /bin/sh saas -c "python3 /opt/saas/main.py -c /etc/saas/config.toml --check"`, then start the service (the installer prints the exact command).

Works on any Debian-based 32-bit system (stock Debian or Raspberry Pi OS Lite; the Pi 1 is ARMv6, so 64-bit images don't run). Kernel ≥ 5.10 for GPIO. The service never waits for the network.

**GPIO** goes straight through the kernel's `/dev/gpiochip*` interface (`saas/hw/gpiochip.py`, standard library only), so it needs no gpiozero, no `/dev/mem`, and no root. The installer adds a `gpio` group plus a udev rule for it. Check with `ls -l /dev/gpiochip*`, which should show group `gpio`. If the chip can't be opened, SaaS logs `GPIO disabled: …` and keeps playing without buttons/LEDs.

## Media, playlists, status

- **Sticks:** each one is mounted at `/media/usb/<label>` (read-write with `sync`, files owned by `saas`). Every stick is its own medium with its own resume point, and `*` / the `source_next` button cycles through them. If you were playing, it keeps playing on the next stick where that stick left off. Pull the active stick and it falls back to another one, stopped. At boot it prefers the stick that was active last time.
- **Playlists:** stored on the stick as `DLP6/Playlists/<name>.toml` (paths relative to the stick root). `a` / `playlist_add` is one click: it saves the current song to the active playlist, or removes it if it's already in there (line 2 says "Saved to …" / "Removed from …"). `l` / `playlist_next` cycles which playlist that is. Missing files are skipped.
- **Folders ⇄ Playlists:** folder up/down only moves within the current view. `v` / `view_toggle` switches between the folders and the playlists (line 2 says "Folders: Rock" / "Playlists: Favorites", line 1 says "Folder 2/20" or "Playlist 1/3"). Each view remembers its place: you come back to the same folder or playlist, song and second, and play/pause stays as it was. When a playlist ends it continues with the next playlist, not with the folders.
- **Liked Songs:** `f` / `like` toggles the song in its own playlist (`[playlists] liked`). It isn't part of the `l` cycle, and the display shows a like icon while a liked song plays.
- **pctool** (on your PC): `python3 pctool.py [stick path]` is a small terminal browser for building playlists on the stick: Tab switches panes, `a` adds a song or a whole folder, `d` removes, `J`/`K` reorders, `[`/`]` switches playlist, `n` creates a new one.
- **Display / dashboard:** `/opt/saas/saas-now.sh` (pure bash) prints two lines:
  ```
  Playing: Beta, Band B - Album - (4:56), Track 3/12 Folder 2/20
  Save to: Favorites  <3
  ```
  Line 2 shows the last action for 5 s, otherwise where "save" goes and whether the song is already in there; the like icon sits at the end. `SAAS_WIDTH=20` cuts both lines for a 2×20 VFD (icon stays bottom-right), `SAAS_LIKE_ICON` sets the icon. Source it (`. /opt/saas/saas-now.sh; saas_lines`) to get `$SAAS_LINE1`, `$SAAS_LINE2` and every `$SAAS_*` value in your own script. It never `source`s or `eval`s the status file, and numbers are checked to be digits, so a root dashboard can't be tricked into running anything.
- **Status files:** `/dev/shm/saas/now.json`, `now.txt` and `now.vars` (KEY=value for shell scripts). `/dev/shm` is a tmpfs on every Linux, so the SD card is never touched, and SaaS creates the folder itself, whether started by systemd, OpenRC, sysvinit or by hand. It only writes into it if the folder belongs to the user SaaS runs as. They're updated on every change and every second while playing. They hold track/tags/position, folder, medium, volume, the active playlist and the last notice ("added X to Favorites", "medium: B_STICK", …). For example `watch -n1 cat /dev/shm/saas/now.txt`.
- **State on the SD card:** `/var/lib/saas/state.json` holds folder, track, title and seconds per stick. It's written **only on a clean exit** (service stop / shutdown), so a hard power cut loses the position since the last clean stop.

### Wiring (matches `config/pi.toml`; BCM numbers)

| Function | GPIO | Header pin | Wire to |
|---|---|---|---|
| play/pause (hold 1 s = stop) | 17 | 11 | button → GND |
| next | 27 | 13 | button → GND |
| prev | 22 | 15 | button → GND |
| folder up (next folder) | 23 | 16 | button → GND |
| folder down (prev folder) | 24 | 18 | button → GND |
| volume + (hold repeats) | 25 | 22 | button → GND |
| volume − (hold repeats) | 5 | 29 | button → GND |
| add song to playlist | 16 | 36 | button → GND |
| next playlist | 26 | 37 | button → GND |
| next medium (`*`) | 12 | 32 | button → GND |
| like (`f`) | 19 | 35 | button → GND |
| folders ⇄ playlists (`v`) | 20 | 38 | button → GND |
| LED playing | 6 | 31 | 330 Ω–1 kΩ → LED → GND |
| LED media | 13 | 33 | 330 Ω–1 kΩ → LED → GND |

GND is available on pins 6, 9, 14, 20, 25, 30, 34 and 39. Buttons use the Pi's internal pull-ups, so they need no resistors. Logic is **3.3 V only**.
I2C (2/3), SPI (7–11) and UART (14/15) are left free for a display later. Wired it differently? Change the numbers in the config. It refuses reserved or duplicate pins.

## Behaviour

- **Play/pause:** if stopped, it starts (or resumes) the current track.
- **Next / prev:** prev restarts the track if you're more than 3 s in.
- **Folder up/down:** jumps to the next/previous folder (or playlist, in the playlists view), wraps around, starts at track 1, and keeps play/pause as it was.
- **End of a folder:** `continue` (next folder), `repeat`, or `stop`.
- **Resume:** per stick (recognised by its folder structure), see *State on the SD card* above.
- **Errors:** an unplayable file is skipped. A pulled stick stops playback (falling back to another stick if one is present), and the LED shows "no media" when none is left. After 10 broken files in a row it stops.
- **LEDs:** *playing* is on while playing and blinks when paused. *media* is on when a stick is mounted and blinks the folder number on each folder change. With `folder_bits = [..]` set, those LEDs show the folder number in binary instead.

## Pi 1 notes

- All 4 USB ports **and** Ethernet share one USB 2.0 link. Audio plus a stick is fine. If you get dropouts, try `audio_buffer = 0.5` under `[audio.mpv_options]`.
- Bus-powered Scarlett: if it doesn't come up reliably, add `max_usb_current=1` to `config.txt` (`/boot/firmware/config.txt` on Bookworm, `/boot/config.txt` before that), or use a powered hub.
- On mount, the folder list is built by one walk over the stick. Track lists are read lazily, per folder, when you enter it.

## Not yet

Internet radio, audio CD (`cdda://`), and an OLED. Radio and CD would become more media for `*` to cycle through; the OLED is one more output file.
