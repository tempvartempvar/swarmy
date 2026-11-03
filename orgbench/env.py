"""Minimal .env loader (no extra dependency)."""

import os
from pathlib import Path

from .paths import REPO_ROOT


def load_dotenv(path: Path | None = None) -> None:
    for candidate in [path, Path.cwd() / ".env", REPO_ROOT / ".env"]:
        if candidate and candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
            return
