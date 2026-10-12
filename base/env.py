"""Minimal .env loader (no third-party dependency).

Reads key=value pairs from the project `.env` file (if present) and
populates `os.environ` without overwriting real environment variables.
Real env vars always win over `.env` values.

Only `_load_dotenv()` + import side effect is needed; keep it tiny and
safe: ignores blank lines and `#` comments, strips surrounding quotes.
"""

import os
import pathlib


def _load_dotenv(path: pathlib.Path | None = None) -> None:
    if path is None:
        path = pathlib.Path(__file__).resolve().parents[1] / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip("\"'")
        os.environ[key] = value


_load_dotenv()
