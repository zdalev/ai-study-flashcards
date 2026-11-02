import anthropic
import pytest

ENV_VARS = ("ANTHROPIC_API_KEY", "FLASHCARDS_MODEL", "FLASHCARDS_DB", "FLASHCARDS_TODAY")

# Kept so the one test that checks the SDK's real credential attributes can still build it.
_REAL_ANTHROPIC = anthropic.Anthropic


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Run every test in an empty temp dir with none of the app's env vars set.

    This keeps the developer's real `.env`, database and API key out of the tests.
    """
    monkeypatch.chdir(tmp_path)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class _BlockedAnthropic:
    def __init__(self, *args, **kwargs):
        pytest.fail(
            "a test tried to create a real anthropic.Anthropic client; "
            "use tests.fakes.FakeClient (or patch anthropic.Anthropic) instead"
        )


@pytest.fixture(autouse=True)
def block_real_anthropic_client(monkeypatch):
    """Make creating a real API client fail the test, so no test can spend money or need a key."""
    monkeypatch.setattr(anthropic, "Anthropic", _BlockedAnthropic)


@pytest.fixture
def real_anthropic(tmp_path, monkeypatch):
    """The real `anthropic.Anthropic` class, for constructing it WITHOUT any network call.

    Credential sources are cut off first: no env vars, and an empty home/config folder.
    """
    for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return _REAL_ANTHROPIC
