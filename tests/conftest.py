import pytest

ENV_VARS = ("ANTHROPIC_API_KEY", "FLASHCARDS_MODEL", "FLASHCARDS_DB", "FLASHCARDS_TODAY")


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Run every test in an empty temp dir with none of the app's env vars set.

    This keeps the developer's real `.env`, database and API key out of the tests.
    """
    monkeypatch.chdir(tmp_path)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
