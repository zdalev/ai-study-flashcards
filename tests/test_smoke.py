import os
from pathlib import Path

import flashcards


def test_package_imports():
    assert flashcards is not None


def test_conftest_isolates_the_environment(tmp_path):
    """Each test runs in an empty temp dir, so no real .env or database is found."""
    assert Path.cwd() == tmp_path
    assert not (tmp_path / ".env").exists()
    for name in ("ANTHROPIC_API_KEY", "FLASHCARDS_MODEL", "FLASHCARDS_DB", "FLASHCARDS_TODAY"):
        assert name not in os.environ
