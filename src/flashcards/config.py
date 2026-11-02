"""Settings from the environment and an optional `.env` file in the working directory."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_DB_PATH = "flashcards.db"


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    db_path: Path = Path(DEFAULT_DB_PATH)
    api_key: str | None = field(default=None, repr=False)  # never print the key


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `./.env` (cwd) under the real environment.

    Real environment variables beat `.env` values. Empty values count as not set.
    `os.environ` is never modified, and the API key is not validated here.
    """
    env = os.environ if environ is None else environ
    # Explicit cwd path: find_dotenv() would search from this module's folder instead.
    file_values = dotenv_values(Path.cwd() / ".env")
    merged = {key: value for key, value in file_values.items() if value}
    merged.update({key: value for key, value in env.items() if value})
    return Settings(
        model=merged.get("FLASHCARDS_MODEL", DEFAULT_MODEL),
        db_path=Path(merged.get("FLASHCARDS_DB", DEFAULT_DB_PATH)),
        api_key=merged.get("ANTHROPIC_API_KEY"),
    )
