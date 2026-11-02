from datetime import date, datetime

import anthropic
import pytest

from flashcards.models import CardDraft
from flashcards.storage import CardStore, open_db
from flashcards.web import create_app

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


WEB_TODAY = date(2026, 11, 2)
WEB_NOW = datetime(2026, 11, 2, 9, 0, 0)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "web-test.db"


@pytest.fixture
def seed(db_path):
    """Add a deck with cards to the test database; returns the deck id.

    Each call opens and closes its own connection, so the app's requests see committed data.
    """

    def add_deck(name, questions=("Q1?", "Q2?", "Q3?"), today=WEB_TODAY):
        with CardStore(open_db(db_path)) as store:
            deck = store.get_or_create_deck(name, None, WEB_NOW)
            drafts = [CardDraft(question, f"Answer to {question}") for question in questions]
            store.add_cards(deck.id, drafts, today, WEB_NOW)
            return deck.id

    return add_deck


@pytest.fixture
def app(db_path):
    return create_app({"TESTING": True, "DATABASE": str(db_path), "TODAY": WEB_TODAY})


@pytest.fixture
def client(app):
    return app.test_client()
