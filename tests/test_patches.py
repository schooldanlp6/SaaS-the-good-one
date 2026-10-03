"""init/apply-patches.sh: apply / skip-if-contained / abort-if-broken."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(HERE, "init", "apply-patches.sh")

pytestmark = pytest.mark.skipif(not shutil.which("patch"), reason="needs the patch tool")


def write_patch(tmp, src, name, old, new):
    work = tmp / f"mk-{name}"
    (work / "a").mkdir(parents=True)
    (work / "b").mkdir(parents=True)
    if old is not None:
        (work / "a" / "f.txt").write_text(old)
    (work / "b" / "f.txt").write_text(new)
    out = subprocess.run(["diff", "-ruN", "a", "b"], cwd=work, capture_output=True, text=True).stdout
    (src / "patches").mkdir(exist_ok=True)
    (src / "patches" / name).write_text(out)


def run(src, stage):
    return subprocess.run(["sh", SCRIPT, str(src), str(stage)], capture_output=True, text=True)


def test_apply_skip_and_untouched_source(tmp_path):
    src, stage = tmp_path / "src", tmp_path / "stage"
    src.mkdir()
    (src / "f.txt").write_text("one\n")
    write_patch(tmp_path, src, "0001-two.patch", "one\n", "one\ntwo\n")
    r = run(src, stage)
    assert r.returncode == 0, r.stderr
    assert "0001-two.patch: applied" in r.stdout
    assert (stage / "f.txt").read_text() == "one\ntwo\n"
    assert (src / "f.txt").read_text() == "one\n"           # unpacked folder untouched

    # same patch against code that already contains it -> skipped
    (src / "f.txt").write_text("one\ntwo\n")
    shutil.rmtree(stage)
    r = run(src, stage)
    assert r.returncode == 0 and "already contained, skipped" in r.stdout
    assert (stage / "f.txt").read_text() == "one\ntwo\n"


def test_patches_apply_in_order_and_create_files(tmp_path):
    src, stage = tmp_path / "src", tmp_path / "stage"
    src.mkdir()
    (src / "f.txt").write_text("one\n")
    write_patch(tmp_path, src, "0001-a.patch", "one\n", "one\ntwo\n")
    write_patch(tmp_path, src, "0002-b.patch", "one\ntwo\n", "one\ntwo\nthree\n")
    r = run(src, stage)
    assert r.returncode == 0, r.stderr
    assert (stage / "f.txt").read_text() == "one\ntwo\nthree\n"


def test_broken_patch_aborts(tmp_path):
    src, stage = tmp_path / "src", tmp_path / "stage"
    src.mkdir()
    (src / "f.txt").write_text("completely different\n")
    write_patch(tmp_path, src, "0001-x.patch", "one\n", "one\ntwo\n")
    r = run(src, stage)
    assert r.returncode == 1 and "does NOT apply" in r.stderr


def test_no_patches(tmp_path):
    src, stage = tmp_path / "src", tmp_path / "stage"
    src.mkdir()
    (src / "f.txt").write_text("x\n")
    r = run(src, stage)
    assert r.returncode == 0 and "no patches" in r.stdout
