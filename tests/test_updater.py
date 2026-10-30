"""OTA updates from the stick (saas.core.updater) - no hardware needed."""
import os
import shutil
import tarfile
import time
import zipfile

import pytest

from saas.core.updater import (UpdateError, Updater, extract, parse_version, read_version,
                               system_hash)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests", "*.pyc")


def make_release(tmp_path, version, name="rel", tweak=None):
    """A copy of this repo as a release dir with the given VERSION."""
    rel = tmp_path / name / "saas-player"
    shutil.copytree(HERE, rel, ignore=SKIP)
    (rel / "VERSION").write_text(version + "\n")
    if tweak:
        tweak(rel)
    return rel


def pack(rel, dest, fmt="zip"):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "zip":
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(rel):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, rel.parent))
    else:
        with tarfile.open(dest, "w:gz") as t:
            t.add(rel, arcname=rel.name)
    return dest


@pytest.fixture
def app(tmp_path):
    """An installed app dir at version 0.6.1 + a valid config."""
    rel = make_release(tmp_path, "0.6.1", "installed")
    app = tmp_path / "opt"
    app.mkdir()
    for item in ("saas", "main.py", "pctool.py", "VERSION"):
        src = rel / item
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, app / item)
    (app / "SYSTEM_HASH").write_text(system_hash(str(rel)))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[audio]\nbackend = "dummy"\n[library]\nmedia_root = "/media/usb"\n')
    return app, cfg


def stick_with(tmp_path, rel, fmt="zip", name="STICK"):
    root = tmp_path / name
    pack(rel, root / "DLP6" / "Update" / "saas.pub", fmt)
    return str(root)


def test_parse_and_read_version(tmp_path):
    assert parse_version("0.6.10") > parse_version("0.6.9") and parse_version("x") is None
    rel = make_release(tmp_path, "1.2.3")
    assert read_version(str(pack(rel, tmp_path / "a.pub"))) == "1.2.3"
    assert read_version(str(pack(rel, tmp_path / "b.pub", "tgz"))) == "1.2.3"
    (tmp_path / "junk.pub").write_bytes(b"not a package")
    assert read_version(str(tmp_path / "junk.pub")) is None


def test_messages_before_install(app, tmp_path):
    a, cfg = app
    u = Updater(str(a), str(cfg))
    assert u.press([]) == ("No stick for updates", False)
    (tmp_path / "EMPTY").mkdir()
    assert u.press([str(tmp_path / "EMPTY")]) == ("No update on the stick", False)
    old = stick_with(tmp_path, make_release(tmp_path, "0.6.1", "same"), name="OLD")
    assert u.press([old]) == ("Up to date (0.6.1 janky-jellyfish)", False)


def test_two_presses_install_and_restart(app, tmp_path):
    a, cfg = app
    s1 = stick_with(tmp_path, make_release(tmp_path, "0.6.2", "r1"), name="S1")
    s2 = stick_with(tmp_path, make_release(tmp_path, "0.7.0", "r2"), "tgz", name="S2")
    u = Updater(str(a), str(cfg))
    announced = []
    assert u.press([s1, s2]) == ("Update 0.7.0 janky-jellyfish found - U to install", False)   # newest wins
    msg, restart = u.press([s1, s2], announced.append)
    assert (msg, restart) == ("Updated to 0.7.0 janky-jellyfish, restarting", True)
    assert announced == ["Updating to 0.7.0 janky-jellyfish..."]
    assert (a / "VERSION").read_text().strip() == "0.7.0"
    assert (a / ".previous" / "VERSION").read_text().strip() == "0.6.1"
    assert (a / "package" / "install.sh").exists()
    assert not [p for p in os.listdir(a) if p.startswith(".update-")]       # staging cleaned up
    assert u.consume_marker() == "Updated to 0.7.0" and u.consume_marker() is None


def test_offer_expires(app, tmp_path):
    a, cfg = app
    s = stick_with(tmp_path, make_release(tmp_path, "0.6.2"))
    u = Updater(str(a), str(cfg), confirm_seconds=0.2)
    u.press([s])
    time.sleep(0.3)
    assert u.press([s])[0] == "Update 0.6.2 janky-jellyfish found - U to install"   # just offered again
    assert (a / "VERSION").read_text().strip() == "0.6.1"


def test_new_code_rejecting_config_changes_nothing(app, tmp_path):
    a, cfg = app
    cfg.write_text('[audio]\nbackend = "dummy"\n[library]\nmedia_root = "/m"\nbogus_option = 1\n')
    s = stick_with(tmp_path, make_release(tmp_path, "0.6.2"))
    u = Updater(str(a), str(cfg))
    u.press([s])
    msg, restart = u.press([s])
    assert msg == "Update failed: config rejected (see log)" and not restart
    assert (a / "VERSION").read_text().strip() == "0.6.1" and not (a / ".previous").exists()


def test_broken_new_code_changes_nothing(app, tmp_path):
    a, cfg = app
    rel = make_release(tmp_path, "0.6.2", tweak=lambda r: (r / "saas" / "core" / "player.py").write_text("syntax error("))
    s = stick_with(tmp_path, rel)
    u = Updater(str(a), str(cfg))
    u.press([s])
    assert u.press([s])[0].startswith("Update failed") and (a / "VERSION").read_text().strip() == "0.6.1"


def test_system_parts_changed_is_reported(app, tmp_path):
    a, cfg = app
    rel = make_release(tmp_path, "0.6.2", tweak=lambda r: (r / "init" / "99-saas-usb.rules").write_text("# changed\n"))
    s = stick_with(tmp_path, rel)
    u = Updater(str(a), str(cfg))
    u.press([s]); u.press([s])
    assert u.consume_marker() == "Updated to 0.6.2 - run install.sh for system parts"


@pytest.mark.skipif(not shutil.which("patch"), reason="needs patch")
def test_package_patches_are_applied(app, tmp_path):
    a, cfg = app

    def add_patch(r):
        (r / "patches").mkdir(exist_ok=True)
        (r / "patches" / "0001-x.patch").write_text(
            "--- a/VERSION\n+++ b/VERSION\n@@ -1 +1 @@\n-0.6.2\n+0.6.3\n")
    s = stick_with(tmp_path, make_release(tmp_path, "0.6.2", tweak=add_patch))
    u = Updater(str(a), str(cfg))
    u.press([s]); u.press([s])
    assert (a / "VERSION").read_text().strip() == "0.6.3"


def test_hostile_packages(tmp_path):
    def zip_with(name, entries):
        p = tmp_path / name
        with zipfile.ZipFile(p, "w") as z:
            for n, data in entries:
                z.writestr(n, data)
        return str(p)
    with pytest.raises(UpdateError, match="unsafe path"):
        extract(zip_with("slip.pub", [("saas-player/VERSION", "9"), ("saas-player/../../evil", "x")]), str(tmp_path / "o1"))
    assert not (tmp_path / "evil").exists()
    with pytest.raises(UpdateError, match="not a SaaS package"):
        extract(zip_with("empty.pub", [("x/VERSION", "9")]), str(tmp_path / "o2"))
    t = tmp_path / "link.pub"
    with tarfile.open(t, "w:gz") as tf:
        info = tarfile.TarInfo("saas-player/VERSION")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(UpdateError, match="link"):
        extract(str(t), str(tmp_path / "o3"))


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("tar"), reason="needs bash + tar")
def test_make_pub_builds_an_installable_package(app, tmp_path):
    import subprocess
    proj = make_release(tmp_path, "0.6.9", "proj")
    (proj / "__pycache__").mkdir()
    (proj / "old.pub").write_bytes(b"x")
    r = subprocess.run(["bash", str(proj / "make-pub.sh"), "--stick", str(tmp_path / "STICK")],
                       capture_output=True, text=True)
    (tmp_path / "STICK").mkdir(exist_ok=True)
    assert r.returncode != 0 and "not a directory" in r.stderr          # stick must exist
    r = subprocess.run(["bash", str(proj / "make-pub.sh"), "--stick", str(tmp_path / "STICK")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    pub = tmp_path / "STICK" / "DLP6" / "Update" / "saas.pub"
    assert "saas.pub 0.6.9" in r.stdout and read_version(str(pub)) == "0.6.9"
    with tarfile.open(pub) as t:
        names = t.getnames()
    assert all(n.startswith("saas-player") for n in names)
    assert not any("__pycache__" in n or n.endswith("old.pub") for n in names)
    a, cfg = app
    u = Updater(str(a), str(cfg))
    u.press([str(tmp_path / "STICK")])
    assert u.press([str(tmp_path / "STICK")])[1] is True and (a / "VERSION").read_text().strip() == "0.6.9"


def test_make_pub_refuses_broken_code(tmp_path):
    import subprocess
    proj = make_release(tmp_path, "0.6.9", "proj",
                        tweak=lambda r: (r / "saas" / "core" / "player.py").write_text("def (:\n"))
    r = subprocess.run(["bash", str(proj / "make-pub.sh")], capture_output=True, text=True)
    assert r.returncode != 0 and "syntax error" in r.stderr and not (proj / "saas.pub").exists()
    (proj / "VERSION").write_text("banana\n")
    r = subprocess.run(["bash", str(proj / "make-pub.sh")], capture_output=True, text=True)
    assert "is not like 1.2.3" in r.stderr


def test_codename_read_from_package(tmp_path):
    from saas.core.updater import read_codename
    rel = make_release(tmp_path, "0.6.9")
    assert read_codename(str(pack(rel, tmp_path / "a.pub"))) == "janky-jellyfish"
    (rel / "saas" / "version.py").write_text("x = 1\n")
    assert read_codename(str(pack(rel, tmp_path / "b.pub", "tgz"))) == ""
