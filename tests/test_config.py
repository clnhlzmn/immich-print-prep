"""Loading and validating the server-wide config file."""

from __future__ import annotations

import pytest

from immich_print_prep.config import ConfigError, load_config


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_minimal_config(tmp_path):
    path = write(tmp_path, "immich_url: https://photos.example.com\nusers:\n  - colin\n")
    config = load_config(path, env={"IPP_DATA_DIR": str(tmp_path / "data")})
    assert config.immich_url == "https://photos.example.com"
    assert [user.username for user in config.users] == ["colin"]
    assert config.defaults.width_in == 8.0
    assert config.defaults.dpi == 300


def test_users_may_carry_an_initial_password(tmp_path):
    path = write(
        tmp_path,
        "immich_url: http://immich:2283\n"
        "users:\n"
        "  - Colin\n"
        "  - username: Alice\n"
        "    initial_password: hello\n",
    )
    config = load_config(path, env={"IPP_DATA_DIR": str(tmp_path / "data")})
    assert [user.username for user in config.users] == ["colin", "alice"]
    assert config.user("ALICE").initial_password == "hello"
    assert config.user("nobody") is None


def test_api_suffix_is_trimmed_from_the_url(tmp_path):
    path = write(tmp_path, "immich_url: http://immich:2283/api/\nusers: [colin]\n")
    config = load_config(path, env={"IPP_DATA_DIR": str(tmp_path / "data")})
    assert config.immich_url == "http://immich:2283"


def test_server_defaults_feed_the_pipeline(tmp_path):
    path = write(
        tmp_path,
        "immich_url: http://immich:2283\nusers: [colin]\n"
        "defaults:\n  width_in: 5\n  height_in: 7\n  fit: crop\n  background: '#101010'\n",
    )
    config = load_config(path, env={"IPP_DATA_DIR": str(tmp_path / "data")})
    assert (config.defaults.width_in, config.defaults.height_in) == (5.0, 7.0)
    assert config.defaults.fit == "crop"
    assert config.defaults.background == "#101010"


@pytest.mark.parametrize(
    "text, message",
    [
        ("users: [colin]\n", "immich_url is required"),
        ("immich_url: photos.example.com\nusers: [colin]\n", "must start with http"),
        ("immich_url: http://x\n", "at least one user"),
        ("immich_url: http://x\nusers: [colin, colin]\n", "duplicate username"),
        ("immich_url: http://x\nusers: ['bad name!']\n", "invalid username"),
        ("immich_url: http://x\nusers: [colin]\nsession_days: 9000\n", "between 1 and 365"),
        ("immich_url: http://x\nusers: [colin]\ndefaults:\n  fit: squish\n", "invalid 'defaults'"),
    ],
)
def test_bad_configs_are_rejected(tmp_path, text, message):
    path = write(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        load_config(path, env={"IPP_DATA_DIR": str(tmp_path / "data")})
    assert message in str(exc.value)


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(str(tmp_path / "nope.yaml"), env={})
    assert "config file not found" in str(exc.value)


def test_secret_key_is_generated_once_and_reused(tmp_path):
    path = write(tmp_path, "immich_url: http://immich:2283\nusers: [colin]\n")
    env = {"IPP_DATA_DIR": str(tmp_path / "data")}
    first = load_config(path, env=env)
    second = load_config(path, env=env)
    assert first.secret_key == second.secret_key
    assert (tmp_path / "data" / "secret.key").exists()


def test_environment_overrides_the_file(tmp_path):
    path = write(tmp_path, "immich_url: http://immich:2283\nusers: [colin]\n")
    config = load_config(path, env={
        "IPP_DATA_DIR": str(tmp_path / "data"),
        "IPP_IMMICH_URL": "https://elsewhere.example.com",
        "IPP_SECRET_KEY": "from-the-environment",
    })
    assert config.immich_url == "https://elsewhere.example.com"
    assert config.secret_key == "from-the-environment"
