"""Security/config tests: secrets must come from the environment."""

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_no_hardcoded_db_credentials():
    text = (ROOT / "base" / "hollow.py").read_text(encoding="utf-8")
    for fragment in ("spaceify", "u56717_T3XxKfsHDF", "Q3YNNCkSI", "s56717_database"):
        assert fragment not in text, "hardcoded database credential still present"


def test_no_hardcoded_token():
    for path in (ROOT / "base" / "config.py", ROOT / "main.py"):
        text = path.read_text(encoding="utf-8")
        assert "MTUxOTY5NTI4MTYyNzU5NDg0Mg" not in text


def test_db_config_requires_env(monkeypatch):
    from base.hollow import Bot

    for key in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.delenv(key, raising=False)
    try:
        Bot._db_config()
    except RuntimeError as exc:
        assert "DB_HOST" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for missing DB config")


def test_db_config_reads_env(monkeypatch):
    from base.hollow import Bot

    monkeypatch.setenv("DB_HOST", "example.invalid")
    monkeypatch.setenv("DB_USER", "hollow")
    monkeypatch.setenv("DB_PASSWORD", "s3cret")
    monkeypatch.setenv("DB_NAME", "hollow")
    monkeypatch.setenv("DB_PORT", "3307")
    config = Bot._db_config()
    assert config["host"] == "example.invalid"
    assert config["port"] == 3307
    assert config["user"] == "hollow"
    assert config["db"] == "hollow"
    assert "password" in config
    # Password must never appear in repr/logs of the config dict keys alone.
    assert "s3cret" not in str(list(config.keys()))


def test_db_config_rejects_bad_numbers(monkeypatch):
    from base.hollow import Bot

    monkeypatch.setenv("DB_HOST", "h")
    monkeypatch.setenv("DB_USER", "u")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("DB_NAME", "d")
    monkeypatch.setenv("DB_PORT", "notaport")
    try:
        Bot._db_config()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError for bad DB_PORT")


def test_env_example_exists_and_has_no_secrets():
    example = ROOT / ".env.example"
    assert example.exists(), ".env.example must exist"
    text = example.read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=" in text
    assert "DB_HOST=" in text
    for fragment in ("spaceify", "u56717_T3XxKfsHDF", "Q3YNNCkSI", "MTUxOTY5NTI4MTYyNzU5NDg0Mg"):
        assert fragment not in text


def test_gitignore_covers_secrets():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in text
    assert "base/config.py" in text


def test_token_from_env(monkeypatch):
    import importlib

    import base.config as config

    monkeypatch.setenv("DISCORD_TOKEN", "tok123")
    importlib.reload(config)
    assert config.CLIENT.TOKEN == "tok123"
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("HOLLOW_TOKEN", "tok456")
    importlib.reload(config)
    assert config.CLIENT.TOKEN == "tok456"
    monkeypatch.delenv("HOLLOW_TOKEN", raising=False)
    monkeypatch.delenv("CLIENT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    importlib.reload(config)
    assert config.CLIENT.TOKEN == ""
    # Restore from real env for other tests.
    for key in ("DISCORD_TOKEN", "HOLLOW_TOKEN", "CLIENT_TOKEN"):
        if os.getenv(key):
            monkeypatch.setenv(key, os.getenv(key))
    importlib.reload(config)


def test_dotenv_loader(tmp_path, monkeypatch):
    from base.env import _load_dotenv

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\n"
        "DOTENV_TEST_KEY=hello\n"
        'DOTENV_QUOTED="quoted value"\n'
        "EMPTY=\n"
        "NOT_A_PAIR\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DOTENV_TEST_KEY", raising=False)
    monkeypatch.delenv("DOTENV_QUOTED", raising=False)
    _load_dotenv(dotenv)
    assert os.getenv("DOTENV_TEST_KEY") == "hello"
    assert os.getenv("DOTENV_QUOTED") == "quoted value"

    # Real environment wins over the file.
    monkeypatch.setenv("DOTENV_TEST_KEY", "real")
    _load_dotenv(dotenv)
    assert os.getenv("DOTENV_TEST_KEY") == "real"


def test_dotenv_missing_file_is_noop(tmp_path):
    from base.env import _load_dotenv

    _load_dotenv(tmp_path / ".env")  # must not raise
