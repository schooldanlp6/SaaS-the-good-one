"""Run main.py for real (dummy backend), then SIGTERM it."""
import os
import signal
import subprocess
import sys
import time

from conftest import make_tree

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_smoke(tmp_path):
    music = tmp_path / "music"
    make_tree(str(music), ["A/1.mp3", "A/2.mp3", "B/1.mp3"])
    state = tmp_path / "state.json"
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"""
[audio]
backend = "dummy"
dummy_track_seconds = 0.5
[library]
media_root = "{music}"
autoplay = true
[state]
file = "{state}"
[sources.usb]
layout = "single"
require_mountpoint = false
poll_interval = 1
[outputs.status]
dir = "{tmp_path}/run"
""")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "main.py"), "-c", str(cfg)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(2.2)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert "[play] F1/2 A  T1/2 1.mp3" in out
    assert "[play] F2/2 B  T1/1 1.mp3" in out   # continued into the next folder
    assert state.exists()


def test_config_error_exit_code(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("[audio]\nbackend = 'mpv'\n[library]\nmedia_root = '/x'\n")
    r = subprocess.run([sys.executable, os.path.join(HERE, "main.py"), "-c", str(cfg)],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "audio.device" in r.stderr


def test_bad_gpio_chip_doesnt_kill_player(tmp_path):
    music = tmp_path / "music"
    make_tree(str(music), ["A/1.mp3"])
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"""
[audio]
backend = "dummy"
[library]
media_root = "{music}"
autoplay = true
[sources.usb]
layout = "single"
require_mountpoint = false
[outputs.status]
dir = "{tmp_path}/run"
[gpio]
chip = "/nonexistent/gpiochip0"
[inputs.gpio]
enabled = true
[inputs.gpio.buttons]
next = 27
[outputs.leds]
enabled = true
playing = 6
""")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "main.py"), "-c", str(cfg)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert "GPIO disabled" in err and "[play]" in out


def test_poweroff_failure_restarts_instead_of_dying(tmp_path):
    """poweroff -> state saved -> command fails -> SaaS comes back (exec) instead of leaving a dead box."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("saas_main", os.path.join(HERE, "main.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.power_off(["true"]) is True
    assert m.power_off(["false"]) is False
    assert m.power_off(["/nonexistent/poweroff"]) is False
