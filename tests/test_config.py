import os

import pytest

from saas.core.config import ConfigError, load, validate

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def base(**over):
    cfg = {"audio": {"backend": "dummy"}, "library": {"media_root": "/tmp/m"}}
    for k, v in over.items():
        cfg[k] = v
    return cfg


def test_minimal_ok():
    cfg = validate(base())
    assert cfg["library"]["end_of_folder"] == "continue"
    assert cfg["outputs"]["console"]["enabled"]


def test_dev_config_loads():
    assert load(os.path.join(HERE, "config", "dev.toml"))["inputs"]["keyboard"]["enabled"]


def test_pi_config_requires_device():
    with pytest.raises(ConfigError, match="audio.device"):
        load(os.path.join(HERE, "config", "pi.toml"))


def test_pi_config_ok_once_device_set(tmp_path):
    text = open(os.path.join(HERE, "config", "pi.toml")).read().replace('"CHANGE_ME"', '"alsa/plughw:CARD=USB,DEV=0"')
    path = tmp_path / "pi.toml"
    path.write_text(text)
    cfg = load(str(path))
    b = cfg["inputs"]["gpio"]["buttons"]
    assert b["play_pause"] == {"pin": 17, "hold": "stop", "hold_time": 1.0, "repeat": False}
    assert b["vol_up"]["repeat"] and b["vol_up"]["hold_time"] == 0.4
    assert b["next"]["pin"] == 27


def test_unknown_keys_and_bad_values_all_reported():
    with pytest.raises(ConfigError) as exc:
        validate(base(audio={"backend": "dummy", "volum": 3},
                      library={"media_root": "/m", "end_of_folder": "loop"}))
    msg = str(exc.value)
    assert "audio.volum" in msg and "end_of_folder" in msg


def test_gpio_validation():
    with pytest.raises(ConfigError) as exc:
        validate(base(
            inputs={"gpio": {"enabled": True, "buttons": {
                "play_pause": 17, "next": 17, "jump": 4, "prev": 1,
                "vol_up": {"pin": 25, "repeat": True, "hold": "stop"}}}},
            outputs={"leds": {"enabled": True, "playing": 17}},
        ))
    msg = str(exc.value)
    assert "GPIO 17 is used twice" in msg
    assert "jump" in msg and "buttons.prev" in msg and "can't be combined" in msg


def test_enabled_without_pins():
    with pytest.raises(ConfigError, match="no buttons"):
        validate(base(inputs={"gpio": {"enabled": True}}))
    with pytest.raises(ConfigError, match="no LED pins"):
        validate(base(outputs={"leds": {"enabled": True}}))


def test_gpio_chip_option():
    assert validate(base())["gpio"]["chip"] == "auto"
    assert validate(base(gpio={"chip": "mock"}))["gpio"]["chip"] == "mock"
    with pytest.raises(ConfigError, match="gpio.chip"):
        validate(base(gpio={"chip": 0}))


def test_playlist_names_validated():
    with pytest.raises(ConfigError, match="must differ"):
        validate(base(playlists={"default": "X", "liked": "X"}))
    with pytest.raises(ConfigError, match="playlists.liked"):
        validate(base(playlists={"liked": "a/b"}))


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load("/nonexistent/x.toml")
