"""OTA updates from a USB stick: <stick>/DLP6/Update/saas.pub

saas.pub is a release package (zip or tar.gz, e.g. saas-player/ with VERSION,
main.py, saas/, init/, ...). Flow, driven by the `update` button / U key:

  1st press  search all sticks, pick the newest version > the installed one
             -> "Update 0.6.2 found - U to install"
  2nd press  (within confirm_seconds) install:
             extract safely -> apply its patches/ -> run the NEW code's
             `--check` against the live config -> swap program files
             (old ones kept in <app>/.previous) -> restart (exec)

Only the program in <app> (/opt/saas) is updated, which the saas user owns.
System parts (udev rules, init scripts, packages) need root: if the package
changes them, the player says so and you run <app>/package/install.sh --offline.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_FILE = "DLP6/Update/saas.pub"
PROGRAM_ITEMS = ("saas", "main.py", "pctool.py", "VERSION")   # what OTA replaces
SYSTEM_FILES = ("install.sh", "uninstall.sh", "saas-now.sh")    # + everything in init/
MAX_MEMBERS = 3000
MAX_BYTES = 50 * 1024 * 1024
MARKER = ".updated"


class UpdateError(Exception):
    pass


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    try:
        parts = tuple(int(p) for p in text.strip().split("."))
    except ValueError:
        return None
    return parts if 1 <= len(parts) <= 4 else None


def system_hash(pkg_dir: str) -> str:
    """Hash of the parts install.sh deploys system-wide (needs root to change)."""
    h = hashlib.sha256()
    files = [os.path.join(pkg_dir, f) for f in SYSTEM_FILES]
    init = os.path.join(pkg_dir, "init")
    if os.path.isdir(init):
        files += [os.path.join(init, f) for f in sorted(os.listdir(init))]
    for path in files:
        if os.path.isfile(path):
            h.update(os.path.relpath(path, pkg_dir).encode() + b"\0")
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


# --- reading packages -------------------------------------------------------------
def _members(path: str) -> List[Tuple[str, Optional[bytes], bool]]:
    """(name, data, is_dir) for every entry; data read lazily is not needed (small)."""
    with open(path, "rb") as f:
        magic = f.read(4)
    out: List[Tuple[str, Optional[bytes], bool]] = []
    total = 0
    if magic.startswith(b"PK"):
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_MEMBERS:
                raise UpdateError("package has too many files")
            for info in infos:
                mode = (info.external_attr >> 16) & 0o170000
                if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
                    raise UpdateError(f"package contains a link/special file: {info.filename}")
                if info.is_dir():
                    out.append((info.filename, None, True))
                    continue
                total += info.file_size
                if total > MAX_BYTES:
                    raise UpdateError("package is too big")
                out.append((info.filename, z.read(info), False))
    elif magic.startswith(b"\x1f\x8b") or magic.startswith(b"BZh") or magic.startswith(b"\xfd7zX"):
        with tarfile.open(path) as t:
            for n, m in enumerate(t):
                if n >= MAX_MEMBERS:
                    raise UpdateError("package has too many files")
                if m.isdir():
                    out.append((m.name, None, True))
                elif m.isreg():
                    total += m.size
                    if total > MAX_BYTES:
                        raise UpdateError("package is too big")
                    out.append((m.name, t.extractfile(m).read(), False))
                else:
                    raise UpdateError(f"package contains a link/special file: {m.name}")
    else:
        raise UpdateError("saas.pub is neither a zip nor a tar.gz")
    return out


def _clean(name: str) -> List[str]:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if name.startswith("/") or ".." in parts:
        raise UpdateError(f"unsafe path in package: {name}")
    return parts


def _strip_top(members):
    """Packages are usually <topdir>/... - strip it if every entry shares one."""
    split = [(_clean(n), d, isdir) for n, d, isdir in members]
    split = [(p, d, isdir) for p, d, isdir in split if p]
    tops = {p[0] for p, _, _ in split}
    if len(tops) == 1 and not any(len(p) == 1 and not isdir for p, _, isdir in split):
        split = [(p[1:], d, isdir) for p, d, isdir in split if p[1:]]
    return split


def read_version(path: str) -> Optional[str]:
    """Version inside a package without extracting it (None if unreadable)."""
    try:
        for parts, data, isdir in _strip_top(_members(path)):
            if parts == ["VERSION"] and data is not None:
                v = data.decode("utf-8", "replace").strip()
                return v if parse_version(v) else None
    except (UpdateError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        log.warning("cannot read update %s: %s", path, exc)
    return None


_CODENAME_RE = re.compile(r'^CODENAME\s*=\s*["\']([a-z0-9-]{1,40})["\']', re.M)


def read_codename(path: str) -> str:
    """Codename inside a package (saas/version.py), "" if it has none."""
    try:
        for parts, data, isdir in _strip_top(_members(path)):
            if parts == ["saas", "version.py"] and data is not None:
                m = _CODENAME_RE.search(data.decode("utf-8", "replace"))
                return m.group(1) if m else ""
    except (UpdateError, OSError, zipfile.BadZipFile, tarfile.TarError):
        pass
    return ""


def extract(path: str, dest: str) -> None:
    for parts, data, isdir in _strip_top(_members(path)):
        target = os.path.join(dest, *parts)
        if isdir:
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
        if parts[-1].endswith(".sh") or parts[0] == "init":
            os.chmod(target, 0o755)
    for need in ("VERSION", "main.py", os.path.join("saas", "__init__.py"),
                 os.path.join("saas", "core", "player.py")):
        if not os.path.isfile(os.path.join(dest, need)):
            raise UpdateError(f"not a SaaS package (missing {need})")


# --- the updater ------------------------------------------------------------------
class Updater:
    def __init__(self, app_dir: str, config_path: str, rel_path: str = DEFAULT_FILE,
                 confirm_seconds: float = 20.0, check_timeout: float = 180.0) -> None:
        self.app_dir = os.path.abspath(app_dir)
        self.config_path = config_path
        self.rel_path = rel_path
        self.confirm_seconds = confirm_seconds
        self.check_timeout = check_timeout
        self._offer: Optional[Tuple[str, str, float]] = None   # (path, "version codename", time)

    @property
    def version(self) -> str:
        try:
            with open(os.path.join(self.app_dir, "VERSION")) as f:
                return f.read().strip()
        except OSError:
            return "0"

    def find(self, roots: List[str]) -> Optional[Tuple[str, str]]:
        """Newest package on any stick: (path, version), or None."""
        best = None
        for root in roots:
            path = os.path.join(root, *self.rel_path.split("/"))
            if not os.path.isfile(path):
                continue
            v = read_version(path)
            if v and (best is None or parse_version(v) > parse_version(best[1])):
                best = (path, v)
        return best

    def press(self, roots: List[str], announce=None) -> Tuple[str, bool]:
        """Handle one press of the update button. Returns (message, restart_now).
        `announce(msg)` is called right before the (slow) install starts."""
        now = time.monotonic()
        offer = self._offer
        if offer and now - offer[2] <= self.confirm_seconds and os.path.isfile(offer[0]):
            self._offer = None
            if announce:
                announce(f"Updating to {offer[1]}...")
            try:
                self.install(offer[0])
            except UpdateError as exc:
                log.error("update failed: %s", exc)
                return f"Update failed: {exc}", False
            except OSError as exc:
                log.exception("update failed")
                return f"Update failed: {exc.strerror or exc}", False
            return f"Updated to {offer[1]}, restarting", True

        self._offer = None
        if not roots:
            return "No stick for updates", False
        found = self.find(roots)
        if found is None:
            return "No update on the stick", False
        path, v = found
        if parse_version(v) <= (parse_version(self.version) or (0,)):
            from ..version import CODENAME
            return f"Up to date ({self.version} {CODENAME})", False
        label = f"{v} {read_codename(path)}".strip()
        self._offer = (path, label, now)
        return f"Update {label} found - U to install", False

    def install(self, package: str) -> None:
        if not os.access(self.app_dir, os.W_OK):
            raise UpdateError(f"{self.app_dir} is not writable (run install.sh once)")
        work = tempfile.mkdtemp(prefix=".update-", dir=self.app_dir)
        try:
            pkg = os.path.join(work, "pkg")
            extract(package, pkg)
            src = pkg
            patches_dir = os.path.join(pkg, "patches")
            has_patches = os.path.isdir(patches_dir) and any(n.endswith(".patch") for n in os.listdir(patches_dir))
            if has_patches:
                if not shutil.which("patch"):
                    raise UpdateError("package has patches but 'patch' is not installed")
                src = os.path.join(work, "patched")
                r = subprocess.run(["sh", os.path.join(pkg, "init", "apply-patches.sh"), pkg, src],
                                   capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    log.error("patching the update failed:\n%s", r.stderr.strip())
                    raise UpdateError("patches don't apply (see log)")

            # Let the NEW code check the live config before anything is replaced.
            r = subprocess.run([sys.executable, os.path.join(src, "main.py"), "--check", "-c", self.config_path],
                               capture_output=True, text=True, timeout=self.check_timeout, cwd=src)
            if r.returncode != 0:
                log.error("new version's --check output:\n%s", (r.stderr or r.stdout).strip())
                raise UpdateError("config rejected (see log)")

            old_version = self.version
            new_version = open(os.path.join(src, "VERSION")).read().strip()
            system_changed = self._system_hash_installed() != system_hash(src)

            # Keep the whole package (install.sh etc.) for root-side steps later.
            package_dir = os.path.join(self.app_dir, "package")
            shutil.rmtree(package_dir, ignore_errors=True)
            shutil.copytree(src, package_dir)

            # Swap the program files; the previous ones go to .previous
            previous = os.path.join(self.app_dir, ".previous")
            shutil.rmtree(previous, ignore_errors=True)
            os.mkdir(previous)
            for item in PROGRAM_ITEMS:
                cur, new = os.path.join(self.app_dir, item), os.path.join(src, item)
                if os.path.lexists(cur):
                    os.rename(cur, os.path.join(previous, item))
                if os.path.exists(new):
                    os.rename(new, cur)
            with open(os.path.join(self.app_dir, MARKER), "w") as f:
                f.write(f"{old_version} {new_version} {'system' if system_changed else 'ok'}\n")
            self._fix_owner()
            log.info("updated %s -> %s (system parts changed: %s)", old_version, new_version, system_changed)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _system_hash_installed(self) -> str:
        try:
            with open(os.path.join(self.app_dir, "SYSTEM_HASH")) as f:
                return f.read().strip()
        except OSError:
            return ""

    def _fix_owner(self) -> None:
        """If root ran the update, hand the files back to the app dir's owner."""
        if os.geteuid() != 0:
            return
        st = os.stat(self.app_dir)
        for dirpath, dirnames, filenames in os.walk(self.app_dir):
            for name in [*dirnames, *filenames]:
                try:
                    os.lchown(os.path.join(dirpath, name), st.st_uid, st.st_gid)
                except OSError:
                    pass

    def consume_marker(self) -> Optional[str]:
        """After a restart: the "Updated to X" message, once."""
        path = os.path.join(self.app_dir, MARKER)
        try:
            with open(path) as f:
                old, new, flag = (f.read().split() + ["", "", ""])[:3]
            os.remove(path)
        except OSError:
            return None
        from ..version import CODENAME, version
        if new == version():
            new = f"{new} {CODENAME}"
        if flag == "system":
            return f"Updated to {new} - run install.sh for system parts"
        return f"Updated to {new}"
