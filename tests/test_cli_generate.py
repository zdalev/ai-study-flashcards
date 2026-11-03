import sqlite3
import subprocess
import sys
from datetime import date, datetime

import anthropic
import httpx2
import pytest

from fakes import FakeClient, cards_response
from flashcards import generator
from flashcards.cli import Deps, build_parser, main
from flashcards.errors import ApiKeyError

NOW = datetime(2026, 11, 2, 9, 0, 0)
TODAY = date(2026, 11, 2)
CARDS = [{"question": "What is ATP?", "answer": "The cell's energy currency."}]


@pytest.fixture
def notes(tmp_path):
    path = tmp_path / "biology.md"
    path.write_text("# Cells\n\nThe cell is the basic unit of life.\n", encoding="utf-8")
    return path


def deps_with(client, factory_calls=None):
    def factory(api_key):
        if factory_calls is not None:
            factory_calls.append(api_key)
        return client

    return Deps(today=lambda: TODAY, now=lambda: NOW, client_factory=factory)


def read_db(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_generate_prints_progress_and_summary_and_stores_cards(notes, tmp_path, capsys):
    client = FakeClient(cards_response(CARDS))
    db = tmp_path / "cards.db"

    code = main(["--db", str(db), "generate", str(notes)], deps_with(client))

    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "chunk 1/1: 1 new card",
        "deck 'biology': 1 new card from 1 of 1 chunk",
    ]
    assert read_db(
        db, "SELECT d.name, c.question FROM cards c JOIN decks d ON d.id = c.deck_id"
    ) == [("biology", "What is ATP?")]
    assert read_db(db, "SELECT due_date FROM cards") == [("2026-11-02",)]


def test_generate_sends_the_notes_with_the_configured_model_and_key(notes, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    monkeypatch.setenv("FLASHCARDS_MODEL", "claude-test-model")
    client = FakeClient(cards_response(CARDS))
    key_calls = []

    main(["--db", str(tmp_path / "c.db"), "generate", str(notes)], deps_with(client, key_calls))

    assert key_calls == ["sk-ant-from-env"]
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "claude-test-model"
    assert "The cell is the basic unit of life." in client.calls[0]["messages"][0]["content"]


def test_deck_option_sets_the_deck_name(notes, tmp_path, capsys):
    db = tmp_path / "cards.db"

    code = main(
        ["--db", str(db), "generate", str(notes), "--deck", "Life science"],
        deps_with(FakeClient(cards_response(CARDS))),
    )

    assert code == 0
    assert "deck 'Life science'" in capsys.readouterr().out
    assert read_db(db, "SELECT name FROM decks") == [("Life science",)]


def test_db_option_is_used_and_falls_back_to_settings(notes, tmp_path, monkeypatch):
    monkeypatch.setenv("FLASHCARDS_DB", str(tmp_path / "from-settings.db"))
    explicit = tmp_path / "explicit" / "cards.db"

    main(
        ["--db", str(explicit), "generate", str(notes)],
        deps_with(FakeClient(cards_response(CARDS))),
    )
    assert explicit.exists()
    assert not (tmp_path / "from-settings.db").exists()

    main(["generate", str(notes)], deps_with(FakeClient(cards_response(CARDS))))
    assert (tmp_path / "from-settings.db").exists()


def test_second_run_says_up_to_date_and_needs_no_client(notes, tmp_path, capsys):
    db = tmp_path / "cards.db"
    main(["--db", str(db), "generate", str(notes)], deps_with(FakeClient(cards_response(CARDS))))
    capsys.readouterr()
    factory_calls = []

    code = main(["--db", str(db), "generate", str(notes)], deps_with(FakeClient(), factory_calls))

    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "chunk 1/1: skipped (already done)",
        "deck 'biology' is up to date",
    ]
    assert factory_calls == []


def test_chunk_without_cards_is_reported_and_not_marked_done(notes, tmp_path, capsys):
    db = tmp_path / "cards.db"

    code = main(
        ["--db", str(db), "generate", str(notes)], deps_with(FakeClient(cards_response([])))
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "no cards returned" in out
    assert "1 chunk returned no cards, run again to retry them" in out
    assert read_db(db, "SELECT count(*) FROM chunks") == [(0,)]


def test_authentication_error_gives_exit_1_with_the_key_message(notes, tmp_path, capsys):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    auth_error = anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx2.Response(401, request=request), body=None
    )

    code = main(
        ["--db", str(tmp_path / "c.db"), "generate", str(notes)],
        deps_with(FakeClient(auth_error)),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert err == f"error: {generator.KEY_MESSAGE}\n"  # no "chunk 1/1" or "run again" noise


def test_missing_key_from_the_client_factory_gives_exit_1(notes, tmp_path, capsys):
    def no_key(api_key):
        raise ApiKeyError(generator.KEY_MESSAGE)

    deps = Deps(today=lambda: TODAY, now=lambda: NOW, client_factory=no_key)

    code = main(["--db", str(tmp_path / "c.db"), "generate", str(notes)], deps)

    assert code == 1
    err = capsys.readouterr().err
    assert generator.KEY_MESSAGE in err
    assert "Traceback" not in err
    assert read_db(tmp_path / "c.db", "SELECT count(*) FROM decks") == [(0,)]  # no empty deck


def test_generate_without_a_key_leaves_no_empty_deck(notes, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # The real client factory runs; the SDK client it builds has no credentials.
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: FakeClient(api_key=api_key))
    db = tmp_path / "c.db"

    assert main(["--db", str(db), "generate", str(notes)]) == 1

    assert read_db(db, "SELECT count(*) FROM decks") == [(0,)]


def test_flashcards_today_is_used_for_the_due_date(notes, tmp_path, monkeypatch):
    monkeypatch.setenv("FLASHCARDS_TODAY", "2027-03-04")
    db = tmp_path / "c.db"

    main(["--db", str(db), "generate", str(notes)], deps_with(FakeClient(cards_response(CARDS))))

    assert read_db(db, "SELECT due_date FROM cards") == [("2027-03-04",)]


def test_today_option_beats_flashcards_today(notes, tmp_path, monkeypatch):
    monkeypatch.setenv("FLASHCARDS_TODAY", "2027-03-04")
    db = tmp_path / "c.db"

    main(
        ["--db", str(db), "--today", "2028-01-02", "generate", str(notes)],
        deps_with(FakeClient(cards_response(CARDS))),
    )

    assert read_db(db, "SELECT due_date FROM cards") == [("2028-01-02",)]


def test_global_options_work_after_the_subcommand(notes, tmp_path):
    db = tmp_path / "c.db"

    code = main(
        ["generate", str(notes), "--db", str(db), "--today", "2028-01-02"],
        deps_with(FakeClient(cards_response(CARDS))),
    )

    assert code == 0
    assert read_db(db, "SELECT due_date FROM cards") == [("2028-01-02",)]


def test_missing_file_gives_exit_1_without_traceback(tmp_path, capsys):
    code = main(
        ["--db", str(tmp_path / "c.db"), "generate", str(tmp_path / "nope.md")],
        deps_with(FakeClient()),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: cannot read notes file")
    assert "Traceback" not in captured.err


def test_pdf_file_gives_exit_1_without_traceback(tmp_path, capsys):
    pdf = tmp_path / "notes.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    code = main(["--db", str(tmp_path / "c.db"), "generate", str(pdf)], deps_with(FakeClient()))

    assert code == 1
    captured = capsys.readouterr()
    assert "unsupported file type '.pdf'" in captured.err
    assert "Traceback" not in captured.err


def test_generate_needs_a_notes_argument(capsys):
    assert main(["generate"]) == 2
    assert "NOTES" in capsys.readouterr().err


def test_generate_is_listed_in_the_help():
    assert "generate" in build_parser().format_help()


def test_unknown_command_is_rejected_by_argparse(capsys):
    # argparse rejects it while parsing, so no fallback branch is needed in `_run`.
    assert main(["no-such-command"]) == 2
    assert "invalid choice" in capsys.readouterr().err


def test_default_client_factory_builds_the_real_client_lazily(monkeypatch):
    seen = {}

    def fake_make_client(api_key):
        seen["key"] = api_key
        return "client"

    monkeypatch.setattr(generator, "make_client", fake_make_client)

    assert Deps().client_factory("sk-ant-x") == "client"
    assert seen == {"key": "sk-ant-x"}


def test_importing_cli_does_not_import_anthropic():
    code = "import sys, flashcards.cli; sys.exit(1 if 'anthropic' in sys.modules else 0)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
