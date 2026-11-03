"""Extra Phase 3 tests from the phase-end verification: gaps the mutation checks found.

- `serve` accepts `--db` and `--today` after the subcommand (only the before-form was tested);
- `generate` cleans the deck name before any Claude call: a blank name is refused without a call,
  and the summary and the stored deck use the same cleaned name.
"""

from datetime import date, datetime

import pytest
from flask import Flask

from flashcards.cli import Deps, main
from flashcards.errors import StorageError
from flashcards.models import CardDraft
from flashcards.pipeline import generate_deck
from flashcards.storage import CardStore, open_db

TODAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 9, 0, 0)


@pytest.fixture
def fake_run(monkeypatch):
    calls = []
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: calls.append((self, kwargs)))
    return calls


@pytest.fixture
def store():
    with CardStore(open_db(":memory:")) as card_store:
        yield card_store


class CountingSource:
    def __init__(self):
        self.calls = 0

    def __call__(self, chunk_text):
        self.calls += 1
        return [CardDraft("Q?", "A.")]


def notes(tmp_path):
    path = tmp_path / "biology.md"
    path.write_text("# Cells\n\nThe cell is the unit of life.\n", encoding="utf-8")
    return path


def test_serve_accepts_db_and_today_after_the_subcommand(fake_run, tmp_path):
    db = tmp_path / "after.db"

    assert main(["serve", "--db", str(db), "--today", "2026-01-05"]) == 0

    app = fake_run[0][0]
    assert app.config["DATABASE"] == str(db)
    assert app.config["TODAY"] == date(2026, 1, 5)


def test_serve_before_form_survives_the_subcommand_defaults(fake_run, tmp_path):
    db = tmp_path / "before.db"

    assert main(["--db", str(db), "--today", "2026-01-05", "serve", "--port", "5055"]) == 0

    app, kwargs = fake_run[0]
    assert (app.config["DATABASE"], app.config["TODAY"]) == (str(db), date(2026, 1, 5))
    assert kwargs["port"] == 5055


def test_blank_deck_name_is_refused_before_any_claude_call(store, tmp_path):
    source = CountingSource()

    with pytest.raises(StorageError, match="deck name must not be empty"):
        generate_deck(notes(tmp_path), store, source, deck_name="   ", today=TODAY, now=NOW)

    assert source.calls == 0
    assert store.find_deck("   ") is None


def test_blank_deck_name_on_the_command_line_needs_no_client(tmp_path, capsys):
    def no_client(api_key):
        raise AssertionError("a blank deck name must be refused before the client is built")

    deps = Deps(today=lambda: TODAY, now=lambda: NOW, client_factory=no_client)

    code = main(
        ["--db", str(tmp_path / "x.db"), "generate", str(notes(tmp_path)), "--deck", " "], deps
    )

    assert code == 1
    assert "deck name must not be empty" in capsys.readouterr().err


def test_deck_name_is_cleaned_in_the_report_and_the_store(store, tmp_path):
    report = generate_deck(
        notes(tmp_path), store, CountingSource(), deck_name="  My   Deck ", today=TODAY, now=NOW
    )

    assert report.deck_name == "My Deck"
    assert store.find_deck("my deck").name == "My Deck"
