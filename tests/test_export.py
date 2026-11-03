import csv
import io
import subprocess
import sys
from datetime import date, datetime

import pytest

from flashcards.cli import Deps, main
from flashcards.errors import StorageError
from flashcards.export import COLUMNS, export_csv
from flashcards.models import CardDraft, ReviewState
from flashcards.storage import CardStore, open_db

TODAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 9, 0, 0)
HEADER = "deck,question,answer,ease_factor,interval_days,repetitions,due_date,last_reviewed"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "cards.db"


@pytest.fixture
def store(db_path):
    with CardStore(open_db(db_path)) as card_store:
        yield card_store


def add(store, deck_name, *pairs):
    deck = store.get_or_create_deck(deck_name, None, NOW)
    store.add_cards(deck.id, [CardDraft(q, a) for q, a in pairs], TODAY, NOW)
    return deck


def export_to_text(store, deck_id=None):
    out = io.StringIO(newline="")
    count = export_csv(store, out, deck_id)
    return count, out.getvalue()


def read_back(text):
    return list(csv.DictReader(io.StringIO(text, newline="")))


def read_file(path):
    with open(path, encoding="utf-8", newline="") as file:
        return file.read()


# --- export_csv ---


def test_empty_database_gives_only_the_header(store):
    count, text = export_to_text(store)
    assert count == 0
    assert text == HEADER + "\r\n"
    assert read_back(text) == []


def test_header_row_matches_the_documented_columns(store):
    _, text = export_to_text(store)
    assert tuple(text.splitlines()[0].split(",")) == COLUMNS


def test_rows_match_the_database(store):
    add(store, "Biology", ("What is ATP?", "The energy currency."))
    card = store.due_cards(TODAY)[0]
    store.save_review(
        card.id,
        ReviewState(
            due_date=date(2026, 11, 8),
            ease_factor=2.36,
            interval_days=6,
            repetitions=2,
            last_reviewed=TODAY,
        ),
        4,
    )
    add(store, "Biology", ("Never reviewed?", "Yes."))

    count, text = export_to_text(store, None)

    assert count == 2
    reviewed, fresh = read_back(text)
    assert reviewed == {
        "deck": "Biology",
        "question": "What is ATP?",
        "answer": "The energy currency.",
        "ease_factor": "2.36",
        "interval_days": "6",
        "repetitions": "2",
        "due_date": "2026-11-08",
        "last_reviewed": "2026-11-02",
    }
    assert fresh["question"] == "Never reviewed?"
    assert fresh["ease_factor"] == "2.5"
    assert fresh["interval_days"] == "0"
    assert fresh["repetitions"] == "0"
    assert fresh["due_date"] == "2026-11-02"
    assert fresh["last_reviewed"] == ""


def test_commas_quotes_and_newlines_survive(store):
    question = 'Why "quoted", with a comma?'
    answer = 'Line one, "quoted"\nline two\r\nline three,'
    add(store, "Tricky, deck", (question, answer))

    _, text = export_to_text(store)

    (row,) = read_back(text)
    assert row["deck"] == "Tricky, deck"
    assert row["question"] == question
    assert row["answer"] == answer


def test_non_ascii_text_round_trips(store):
    add(store, "Español", ("¿Cómo está? 日本語 \U0001f600", "Bien — très bien"))

    _, text = export_to_text(store)

    (row,) = read_back(text)
    assert row["deck"] == "Español"
    assert row["question"] == "¿Cómo está? 日本語 \U0001f600"
    assert row["answer"] == "Bien — très bien"


def test_ordered_by_deck_name_then_card_id(store):
    add(store, "zoology", ("Z1", "a"))
    add(store, "Biology", ("B2", "a"), ("B1", "a"))
    add(store, "Chemistry", ("C1", "a"))

    _, text = export_to_text(store)

    assert [(r["deck"], r["question"]) for r in read_back(text)] == [
        ("Biology", "B2"),
        ("Biology", "B1"),
        ("Chemistry", "C1"),
        ("zoology", "Z1"),
    ]


def test_deck_id_filters(store):
    biology = add(store, "Biology", ("B1", "a"), ("B2", "a"))
    add(store, "Chemistry", ("C1", "a"))

    count, text = export_to_text(store, biology.id)

    assert count == 2
    assert {r["deck"] for r in read_back(text)} == {"Biology"}


def test_database_error_writes_nothing(store):
    add(store, "Biology", ("B1", "a"))
    store._conn.execute("DROP TABLE cards")
    out = io.StringIO()

    with pytest.raises(StorageError):
        export_csv(store, out)

    assert out.getvalue() == ""


# --- cli export ---


def cli_deps():
    def no_client(api_key):
        raise AssertionError("export must never build an API client")

    return Deps(today=lambda: TODAY, now=lambda: NOW, client_factory=no_client)


def run(db_path, *args):
    return main(["--db", str(db_path), "export", *args], cli_deps())


def test_cli_writes_a_file_and_reports_on_stderr(store, db_path, tmp_path, capsys):
    add(store, "Biology", ("B1", "a"), ("B2", "b"))
    out_file = tmp_path / "cards.csv"

    code = run(db_path, str(out_file))

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"Exported 2 cards to {out_file}\n"
    assert [r["question"] for r in read_back(read_file(out_file))] == ["B1", "B2"]


def test_cli_singular_message(store, db_path, tmp_path, capsys):
    add(store, "Biology", ("B1", "a"))
    run(db_path, str(tmp_path / "cards.csv"))
    assert "Exported 1 card to" in capsys.readouterr().err


def test_cli_overwrites_an_existing_file(store, db_path, tmp_path):
    add(store, "Biology", ("B1", "a"))
    out_file = tmp_path / "cards.csv"
    out_file.write_text("old content that is much longer than the new header row " * 50)

    assert run(db_path, str(out_file)) == 0

    text = read_file(out_file)
    assert text.startswith(HEADER)
    assert "old content" not in text


def test_cli_file_uses_crlf_line_ends_and_utf8_bytes(store, db_path, tmp_path):
    add(store, "Español", ("¿Qué?", "Sí"))
    out_file = tmp_path / "cards.csv"

    run(db_path, str(out_file))

    raw = out_file.read_bytes()
    assert raw.count(b"\r\n") == 2
    assert b"\r\r" not in raw
    assert "¿Qué?".encode() in raw


def test_cli_empty_database_exports_only_the_header(db_path, tmp_path, capsys):
    out_file = tmp_path / "cards.csv"

    assert run(db_path, str(out_file)) == 0

    assert read_file(out_file) == HEADER + "\r\n"
    assert "Exported 0 cards to" in capsys.readouterr().err


def test_cli_dash_writes_csv_to_stdout_and_nothing_to_stderr(store, db_path, capsys):
    add(store, "Biology", ("B1", "a, with comma"))

    code = run(db_path, "-")

    assert code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    (row,) = read_back(captured.out)
    assert row["question"] == "B1"
    assert row["answer"] == "a, with comma"


def test_cli_dash_does_not_create_a_file_named_dash(store, db_path, tmp_path):
    add(store, "Biology", ("B1", "a"))
    run(db_path, "-")
    assert not (tmp_path / "-").exists()


def test_cli_deck_option_filters_case_insensitively(store, db_path, capsys):
    add(store, "Biology", ("B1", "a"))
    add(store, "Chemistry", ("C1", "a"))

    assert run(db_path, "-", "--deck", "chemistry") == 0

    rows = read_back(capsys.readouterr().out)
    assert [(r["deck"], r["question"]) for r in rows] == [("Chemistry", "C1")]


def test_cli_deck_filter_reports_the_filtered_count(store, db_path, tmp_path, capsys):
    add(store, "Biology", ("B1", "a"), ("B2", "a"))
    add(store, "Chemistry", ("C1", "a"))

    run(db_path, str(tmp_path / "bio.csv"), "--deck", "Biology")

    assert "Exported 2 cards to" in capsys.readouterr().err


def test_cli_unknown_deck_is_a_clean_error_and_leaves_the_file_alone(
    store, db_path, tmp_path, capsys
):
    add(store, "Biology", ("B1", "a"))
    out_file = tmp_path / "cards.csv"
    out_file.write_text("precious", encoding="utf-8")

    code = run(db_path, str(out_file), "--deck", "Nope")

    assert code == 1
    captured = capsys.readouterr()
    assert captured.err == "error: deck not found: Nope\n"
    assert captured.out == ""
    assert out_file.read_text(encoding="utf-8") == "precious"


def test_cli_unwritable_output_is_a_clean_error(store, db_path, tmp_path, capsys):
    add(store, "Biology", ("B1", "a"))
    missing_dir_file = tmp_path / "no-such-dir" / "cards.csv"

    code = run(db_path, str(missing_dir_file))

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith(f"error: cannot write {missing_dir_file}: ")
    assert "Traceback" not in err


def test_cli_output_that_is_a_directory_is_a_clean_error(store, db_path, tmp_path, capsys):
    assert run(db_path, str(tmp_path)) == 1
    assert capsys.readouterr().err.startswith(f"error: cannot write {tmp_path}: ")


def test_cli_export_needs_no_api_key_and_never_imports_anthropic(store, db_path, tmp_path):
    add(store, "Biology", ("B1", "a"))
    store.close()
    out_file = tmp_path / "cards.csv"
    code = (
        "import sys\n"
        "from flashcards.cli import main\n"
        f"rc = main(['--db', {str(db_path)!r}, 'export', {str(out_file)!r}])\n"
        "sys.exit(rc or (3 if 'anthropic' in sys.modules else 0))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path
    )

    assert result.returncode == 0, result.stderr
    assert "Exported 1 card to" in result.stderr
    assert "B1" in read_file(out_file)


def test_cli_dash_in_a_real_process_writes_utf8_to_stdout(store, db_path, tmp_path):
    add(store, "Español", ("¿Qué?", "Sí"))
    store.close()

    result = subprocess.run(
        [sys.executable, "-m", "flashcards", "--db", str(db_path), "export", "-"],
        capture_output=True,
        cwd=tmp_path,
        env={"PYTHONIOENCODING": "ascii", "PATH": ""},
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert read_back(result.stdout.decode("utf-8"))[0]["question"] == "¿Qué?"


def test_cli_accepts_db_after_the_subcommand(store, db_path, tmp_path, capsys):
    add(store, "Biology", ("B1", "a"))

    code = main(["export", "-", "--db", str(db_path)], cli_deps())

    assert code == 0
    assert [r["question"] for r in read_back(capsys.readouterr().out)] == ["B1"]
