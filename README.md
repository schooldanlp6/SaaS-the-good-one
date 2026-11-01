# SaaS — ScriptAsASystem

Current release: **0.6.5 singing-submarine** (`python3 main.py --version`).

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

Keys: `space` play/pause · `s` stop · `n`/`p` next/prev · `]`/`[` folder up/down · `+`/`-` volume · `a` save/remove song in playlist · `l` next playlist · `c` new playlist · `f` like · `v` folders ⇄ playlists · `*` next medium · `U` update · `z` standby · arrows ←/→ prev/next, ↑/↓ volume · **hold** `n`/`p`/←/→ to scrub · `?` show these keys again · `q` quit.

A terminal only sends key presses (holding a key just auto-repeats it), so SaaS recognises a hold by its rhythm. That's why a single tap of next/prev acts after the auto-repeat delay (~0.3–0.7 s); the delay is measured on your first hold.
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
| **Offline** | `./install.sh --offline` skips apt (deps already installed / no network). |
| **OTA** | see below: `DLP6/Update/UPDATE4SAAS.pub` on a stick, press update twice. |
| **Uninstall** | `saas-uninstall` keeps config and state; `--purge` removes everything incl. the `saas` user. Sticks and their playlists are never touched. |

After a fresh install:
1. `python3 /opt/saas/main.py --list-devices`, then put your DAC's name (the `alsa/plughw:CARD=…` one) into `[audio] device`.
2. `su -s /bin/sh saas -c "python3 /opt/saas/main.py -c /etc/saas/config.toml --check"`, then start the service (the installer prints the exact command).

Works on any Debian-based 32-bit system (stock Debian or Raspberry Pi OS Lite; the Pi 1 is ARMv6, so 64-bit images don't run). Kernel ≥ 5.10 for GPIO. The service never waits for the network.

**GPIO** goes straight through the kernel's `/dev/gpiochip*` interface (`saas/hw/gpiochip.py`, standard library only), so it needs no gpiozero, no `/dev/mem`, and no root. The installer adds a `gpio` group plus a udev rule for it. Check with `ls -l /dev/gpiochip*`, which should show group `gpio`. If the chip can't be opened, SaaS logs `GPIO disabled: …` and keeps playing without buttons/LEDs.

## Network servers (LMS, Jellyfin, Subsonic, DLNA)

Every server in `[[network]]` (examples in `config/pi.toml`) is one more medium for `*`, next to the sticks. Its folders are albums ("Artist - Album") or, for LMS, the folders on the server's disk; its tracks are streamed by mpv. The album list loads in a background thread, so a slow server never freezes the buttons. A server that stops answering drops out like a pulled stick (checked every `poll` seconds, 60 by default) and comes back by itself. Resume works per server.

- **LMS (Lyrion Music Server):** `url` (e.g. `http://192.168.1.10:9000`). `view = "folders"` (default) mirrors the server's music folder like a stick, `start = "Rock/Live"` limits it to a sub-folder; `view = "albums"` lists albums. With password protection on, add `user` and `password`. SaaS is just a client here (no Squeezebox player involved); it streams the original files.
- **Jellyfin:** `url`, `api_key` (Dashboard → API Keys), `user` (needed when there is more than one). Streams the original files, so the Pi decodes them like files from a stick.
- **Subsonic API** (Navidrome, Airsonic, Gonic, …): `url`, `user`, `password`; token auth by default, `auth = "password"` for servers without it.
- **DLNA/UPnP:** `location` = the server's description URL, or leave it empty to find servers on the LAN (`server` picks one by name). Set `start` to one view such as `"Music/Folders"`: DLNA servers list the same songs under several views.
- Playlists, save and like only work on sticks; on a server line 2 says so.
- **Test your servers:** `python3 /opt/saas/main.py -c /etc/saas/config.toml --test-network` connects to each one, lists its folders, reads the first track's tags and fetches the start of its stream, then says OK or what failed.
- The config now can hold passwords, so `install.sh` makes it readable for root and `saas` only. Credentials in stream URLs are blanked in the log.

## Standby and power off

- `standby` (`z`, or a short press on GPIO3) stops playback, releases the audio device and darkens the LEDs; line 1 says `Standby`. **Any** button wakes it, and that press only wakes (no surprise skip). If it was playing, it continues at the same second.
- `poweroff` (hold GPIO3 for 3 s; no default key) saves the state and shuts the system down. After that, GPIO3 to GND boots the Pi again (Pi firmware feature), so one button is on/standby/off. GPIO3 is I2C SDA: keep I2C off while using it.
- Power off needs the right to shut down: as root it runs `poweroff`; as the `saas` user under systemd, `install.sh` adds a polkit rule for exactly that. If it fails, SaaS restarts itself instead of leaving a silent box. `[power] command` overrides the command.

## OTA updates from a stick

1. Put a release package on any stick as `DLP6/Update/UPDATE4SAAS.pub`. It can be a zip or a tar.gz. The old name `DLP6/Update/saas.pub` still works (SaaS before 0.6.5 only knows that one); if both are there, the newer version wins.
2. Press **update** (`U` / GPIO `update`): line 2 says `Update 0.6.2 found - U to install` (or `Up to date`, `No update on the stick`).
3. Press it again within 20 s: `Updating to 0.6.2...`. The package is extracted safely (no paths outside, no links, size limit), its `patches/` are applied, then the **new** code validates your config. Only if all of that passes are the program files swapped (old ones → `/opt/saas/.previous`). SaaS then restarts itself (exec, so it works under any init system or when started by hand), resumes the song and shows `Updated to 0.6.2 - see notes`. Every release has a `NOTES` file with what changed and what's recommended: `python3 /opt/saas/main.py --notes` prints it, and `install.sh` prints it after an upgrade.

OTA updates the program in `/opt/saas`, which belongs to the `saas` user. System parts (udev rules, init scripts, packages) need root. If the package changes those, line 2 says `run install.sh for system parts`, and you run `/opt/saas/package/install.sh --offline` as root. Undo an OTA update with `saas-rollback` (as root), then restart SaaS.

**Building a package yourself:** from the project folder run `./make-pub.sh` (writes `UPDATE4SAAS.pub` next to it), `./make-pub.sh -o /some/file.pub`, or `./make-pub.sh --stick /media/usb/STICK` to put it straight onto a mounted stick as `DLP6/Update/UPDATE4SAAS.pub`. `--old-name` writes `saas.pub` instead, for players before 0.6.5. Bash + tar only. It checks VERSION and the Python syntax first.

`./install.sh --offline` skips apt completely (dependencies already installed, no network) and only checks that python3 and python-mpv are there.

## Media, playlists, status

- **Sticks:** each one is mounted at `/media/usb/<label>` (read-write with `sync`, files owned by `saas`). Every stick is its own medium with its own resume point, and `*` / the `source_next` button cycles through them. If you were playing, it keeps playing on the next stick where that stick left off. Pull the active stick and it falls back to another one, stopped. At boot it prefers the stick that was active last time.
- **Playlists:** stored on the stick as `DLP6/Playlists/<name>.toml` (paths relative to the stick root). `a` / `playlist_add` is one click: it saves the current song to the active playlist, or removes it if it's already in there (line 2 says "Saved to …" / "Removed from …"). `l` / `playlist_next` cycles which playlist that is. `c` / `playlist_new` creates the next numbered playlist ("Playlist 1", "Playlist 2", …, highest number + 1) on the active stick and makes it the save target right away. Names can't be typed with buttons: rename or edit playlists on a PC with pctool. Missing files are skipped. An empty playlist can be a save target but only shows up in the playlists view once it has songs.
- **Folders ⇄ Playlists:** folder up/down only moves within the current view. `v` / `view_toggle` switches between the folders and the playlists (line 2 says "Folders: Rock" / "Playlists: Favorites", line 1 says "Folder 2/20" or "Playlist 1/3"). Each view remembers its place: you come back to the same folder or playlist, song and second, and play/pause stays as it was. When a playlist ends it continues with the next playlist, not with the folders.
- **Liked Songs:** `f` / `like` toggles the song in its own playlist (`[playlists] liked`). It isn't part of the `l` cycle, and the display shows a like icon while a liked song plays.
- **pctool** (on your PC): `python3 pctool.py [stick path]` is a small terminal browser for building playlists on the stick: Tab switches panes, `a` adds a song or a whole folder, `d` removes, `J`/`K` reorders, `[`/`]` switches playlist, `n` creates a new one.
- **Display / dashboard:** `saas-now` (`/usr/local/bin/saas-now`, pure bash, owned by root) prints two lines:
  ```
  Playing: Beta, Band B - Album - (4:56), Track 3/12 Folder 2/20
  Save to: Favorites  <3
  ```
  Line 2 shows the last action for 5 s, otherwise where "save" goes and whether the song is already in there; the like icon sits at the end. `SAAS_WIDTH=20` cuts both lines for a 2×20 VFD (icon stays bottom-right), `SAAS_LIKE_ICON` sets the icon. Source it (`. /usr/local/bin/saas-now; saas_lines`) to get `$SAAS_LINE1`, `$SAAS_LINE2` and every `$SAAS_*` value in your own script. It never `source`s or `eval`s the status file, and numbers are checked to be digits, so a root dashboard can't be tricked into running anything.
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
| update (`U`) | 21 | 40 | button → GND |
| new playlist (`c`) | 18 | 12 | button → GND |
| standby (`z`), hold 3 s = power off | 3 | 5 | button → GND (also wakes the Pi after power off) |
| LED playing | 6 | 31 | 330 Ω–1 kΩ → LED → GND |
| LED media | 13 | 33 | 330 Ω–1 kΩ → LED → GND |

GND is available on pins 6, 9, 14, 20, 25, 30, 34 and 39. Buttons use the Pi's internal pull-ups, so they need no resistors. Logic is **3.3 V only**.
I2C (2/3), SPI (7–11) and UART (14/15) are left free for a display later. Wired it differently? Change the numbers in the config. It refuses reserved or duplicate pins.

## Behaviour

- **Play/pause:** if stopped, it starts (or resumes) the current track.
- **Next / prev:** prev restarts the track if you're more than 3 s in. **Hold** next/prev (button 0.5 s, or the key) to scrub forward/back through the song: 2 s steps, faster the longer you hold (5 s, then 15 s), 4 steps a second; line 2 shows `» 1:23 / 4:56`. Scrubbing stops 1 s before the end and never skips the track. `hold = false` on the button turns it off.
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

Internet radio, audio CD (`cdda://`), an OLED. Planned for 0.6.6 green-grass: custom modules, a tidier package layout, optional auto-standby.
