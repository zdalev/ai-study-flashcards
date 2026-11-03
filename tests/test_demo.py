import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from flashcards.cli import Deps, main
from flashcards.config import Settings
from flashcards.errors import DemoDataError, GenerationError, IngestError
from flashcards.models import CardDraft
from flashcards.pipeline import build_demo_data, load_demo
from flashcards.storage import CardStore, open_db

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_FILES = ["biology.md", "english.txt", "mathematics.md", "physics.txt", "programming.md"]
DECK_NAMES = ["biology", "english", "mathematics", "physics", "programming"]
TODAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 9, 0, 0)


def fake_source(chunk_text):
    first_line = chunk_text.splitlines()[0]
    return [CardDraft(f"Q1 {first_line}?", "A1."), CardDraft(f"Q2 {first_line}?", "A2.")]


def sample_paths():
    return [ROOT / "examples" / name for name in SAMPLE_FILES]


def demo_data(*decks):
    return {"version": 1, "decks": list(decks)}


def deck_entry(name="biology", cards=None):
    cards = [{"question": "What is ATP?", "answer": "Energy."}] if cards is None else cards
    return {"name": name, "source": f"examples/{name}.md", "cards": cards}


@pytest.fixture
def store():
    with CardStore(open_db(":memory:")) as card_store:
        yield card_store


@pytest.fixture
def demo_file(tmp_path):
    path = tmp_path / "demo.json"
    data = demo_data(deck_entry("biology"), deck_entry("physics"))
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def failing_factory(_api_key):
    raise AssertionError("load-demo must never build an API client")


DEPS = Deps(today=lambda: TODAY, now=lambda: NOW, client_factory=failing_factory)


def read_db(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# --- build_demo_data ---


def test_build_demo_data_makes_one_deck_per_file_named_after_the_stem():
    data = build_demo_data(sample_paths(), fake_source)

    assert data["version"] == 1
    assert [deck["name"] for deck in data["decks"]] == DECK_NAMES
    biology = data["decks"][0]
    assert biology["source"].endswith("examples/biology.md")
    assert (
        biology["cards"][0] == {"question": "Q1 # Cells?", "answer": "A1."}
        or len(biology["cards"]) >= 2
    )
    assert all(set(card) == {"question", "answer"} for card in biology["cards"])


def test_build_demo_data_keeps_the_path_as_given_in_source(tmp_path):
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "biology.md").write_text("# Cells\n\nText.\n", encoding="utf-8")
    data = build_demo_data([Path("examples") / "biology.md"], lambda _text: [CardDraft("Q?", "A.")])
    assert data["decks"][0]["source"] == "examples/biology.md"


def test_build_demo_data_raises_when_a_file_gives_no_cards():
    with pytest.raises(GenerationError, match="biology.md"):
        build_demo_data([ROOT / "examples" / "biology.md"], lambda _text: [])


def test_build_demo_data_lets_a_bad_file_raise(tmp_path):
    with pytest.raises(IngestError):
        build_demo_data([tmp_path / "missing.md"], fake_source)


def test_build_demo_data_is_json_serialisable_and_round_trips_through_load_demo(store):
    data = build_demo_data(sample_paths(), fake_source)
    data = json.loads(json.dumps(data, ensure_ascii=False))

    added = load_demo(store, data, TODAY, NOW)

    assert added == sum(len(deck["cards"]) for deck in data["decks"])
    summaries = store.deck_summaries(TODAY)
    assert [s.deck.name for s in summaries] == DECK_NAMES
    for summary, deck in zip(summaries, data["decks"], strict=True):
        assert summary.cards == len(deck["cards"])
        assert summary.due == summary.cards  # all due today


# --- load_demo ---


def test_load_demo_creates_decks_and_cards_due_today(store):
    data = demo_data(
        deck_entry("biology", [{"question": "Q1?", "answer": "A1."}]),
        deck_entry("physics", [{"question": "Q2?", "answer": "A2."}]),
    )

    assert load_demo(store, data, TODAY, NOW) == 2

    biology = store.find_deck("biology")
    assert biology.source_path == "examples/biology.md"
    assert [c.question for c in store.due_cards(TODAY, biology.id)] == ["Q1?"]


def test_load_demo_twice_adds_nothing(store):
    data = demo_data(deck_entry("biology"), deck_entry("physics"))
    load_demo(store, data, TODAY, NOW)

    assert load_demo(store, data, TODAY, NOW) == 0
    assert [s.cards for s in store.deck_summaries(TODAY)] == [1, 1]


def test_load_demo_skips_repeated_questions_within_a_deck(store):
    cards = [{"question": "Q?", "answer": "A."}, {"question": " q? ", "answer": "B."}]
    assert load_demo(store, demo_data(deck_entry("biology", cards)), TODAY, NOW) == 1


@pytest.mark.parametrize(
    "data",
    [
        [],
        "text",
        {},
        {"decks": []},
        {"version": 2, "decks": [deck_entry()]},
        {"version": 1},
        {"version": 1, "decks": []},
        {"version": 1, "decks": "biology"},
        {"version": 1, "decks": ["biology"]},
        demo_data({"source": "x", "cards": []}),
        demo_data(deck_entry(" ")),
        demo_data({"name": "biology", "source": 5, "cards": []}),
        demo_data({"name": "biology"}),
        demo_data(deck_entry("biology", "cards")),
        demo_data(deck_entry("biology", ["card"])),
        demo_data(deck_entry("biology", [{"question": "Q?"}])),
        demo_data(deck_entry("biology", [{"question": "Q?", "answer": 5}])),
        demo_data(deck_entry("biology", [{"question": "  ", "answer": "A."}])),
    ],
)
def test_load_demo_rejects_a_wrong_shape(store, data):
    with pytest.raises(DemoDataError):
        load_demo(store, data, TODAY, NOW)
    assert store.deck_summaries(TODAY) == []


def test_load_demo_checks_everything_before_saving_anything(store):
    bad_second_deck = deck_entry("physics", [{"question": "", "answer": "A."}])

    with pytest.raises(DemoDataError, match="deck 2"):
        load_demo(store, demo_data(deck_entry("biology"), bad_second_deck), TODAY, NOW)

    assert store.deck_summaries(TODAY) == []


# --- cli load-demo ---


def test_load_demo_command_loads_decks_without_an_api_key(demo_file, tmp_path, capsys):
    db = tmp_path / "cards.db"

    code = main(["--db", str(db), "load-demo", str(demo_file)], DEPS)

    assert code == 0
    assert capsys.readouterr().out.strip() == "Loaded 2 cards into 2 decks"
    assert read_db(db, "SELECT name FROM decks ORDER BY name") == [("biology",), ("physics",)]
    assert read_db(db, "SELECT DISTINCT due_date FROM cards") == [("2026-11-02",)]


def test_load_demo_command_twice_loads_zero_new_cards(demo_file, tmp_path, capsys):
    db = tmp_path / "cards.db"
    main(["--db", str(db), "load-demo", str(demo_file)], DEPS)
    capsys.readouterr()

    assert main(["--db", str(db), "load-demo", str(demo_file)], DEPS) == 0

    assert capsys.readouterr().out.strip() == "Loaded 0 cards into 2 decks"
    assert read_db(db, "SELECT count(*) FROM cards") == [(2,)]


def test_load_demo_command_uses_the_today_option(demo_file, tmp_path):
    db = tmp_path / "cards.db"
    main(["--db", str(db), "--today", "2027-01-05", "load-demo", str(demo_file)], DEPS)
    assert read_db(db, "SELECT DISTINCT due_date FROM cards") == [("2027-01-05",)]


def test_load_demo_command_default_file_is_relative_to_the_working_directory(tmp_path, capsys):
    (tmp_path / "examples").mkdir()
    data = demo_data(deck_entry("biology"))
    (tmp_path / "examples" / "demo-cards.json").write_text(json.dumps(data), encoding="utf-8")

    code = main(["--db", str(tmp_path / "cards.db"), "load-demo"], DEPS)

    assert code == 0
    assert capsys.readouterr().out.strip() == "Loaded 1 card into 1 deck"


def test_load_demo_command_missing_file_is_a_clean_error(tmp_path, capsys):
    db = tmp_path / "cards.db"

    code = main(["--db", str(db), "load-demo"], DEPS)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("error: demo file not found")
    assert "demo-cards.json" in captured.err
    assert "Traceback" not in captured.err
    assert not db.exists()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"{not json", "not valid JSON"),
        (b"\xff\xfe\x00bad", "not valid UTF-8"),
        (b'{"version": 1, "decks": []}', "non-empty"),
        (b'["a list"]', "version"),
    ],
)
def test_load_demo_command_bad_file_is_a_clean_error(tmp_path, capsys, content, message):
    bad = tmp_path / "bad.json"
    bad.write_bytes(content)

    code = main(["--db", str(tmp_path / "cards.db"), "load-demo", str(bad)], DEPS)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("error: ")
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_load_demo_command_directory_instead_of_file_is_a_clean_error(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "cards.db"), "load-demo", str(tmp_path)], DEPS) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_load_demo_never_imports_the_api_library(demo_file, tmp_path):
    code = (
        "import sys; from flashcards.cli import main; "
        f"rc = main(['--db', {str(tmp_path / 'cards.db')!r}, 'load-demo', {str(demo_file)!r}]); "
        "sys.exit(rc or (3 if 'anthropic' in sys.modules else 0))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- scripts/build_demo.py ---


def load_script(monkeypatch, tmp_path, make_client):
    """The script's namespace, with settings, client and output path replaced (no .env, no API)."""
    spec = importlib.util.spec_from_file_location("build_demo", ROOT / "scripts" / "build_demo.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "load_settings", lambda: Settings(api_key=None))
    monkeypatch.setattr(script, "make_client", make_client)
    monkeypatch.setattr(script, "OUTPUT", tmp_path / "out" / "demo-cards.json")
    (tmp_path / "out").mkdir()
    return script


def test_build_demo_script_writes_the_json_for_the_five_sample_files(monkeypatch, tmp_path, capsys):
    script = load_script(monkeypatch, tmp_path, lambda _key: object())
    monkeypatch.setattr(script, "generate_cards", lambda text, *, client, model: fake_source(text))

    assert script.main() == 0

    out = script.OUTPUT
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [deck["name"] for deck in data["decks"]] == DECK_NAMES
    assert data["decks"][0]["source"] == "examples/biology.md"
    assert out.read_text(encoding="utf-8").endswith("}\n")
    assert "biology: 2 cards" in capsys.readouterr().out
    assert main(["--db", str(tmp_path / "cards.db"), "load-demo", str(out)], DEPS) == 0


def test_build_demo_script_without_a_key_prints_a_clean_error(monkeypatch, tmp_path, capsys):
    def no_key(_api_key):
        raise GenerationError("Claude API key missing or invalid - set ANTHROPIC_API_KEY in .env")

    script = load_script(monkeypatch, tmp_path, no_key)

    assert script.main() == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: Claude API key missing")
    assert captured.out == ""
    assert not script.OUTPUT.exists()


def test_load_demo_command_uses_flashcards_today_and_the_option_beats_it(
    demo_file, tmp_path, monkeypatch
):
    monkeypatch.setenv("FLASHCARDS_TODAY", "2027-03-04")
    from_env, from_option = tmp_path / "env.db", tmp_path / "option.db"

    main(["--db", str(from_env), "load-demo", str(demo_file)], DEPS)
    main(["load-demo", str(demo_file), "--db", str(from_option), "--today", "2028-01-02"], DEPS)

    for db, expected in ((from_env, "2027-03-04"), (from_option, "2028-01-02")):
        conn = sqlite3.connect(db)
        try:
            assert {row[0] for row in conn.execute("SELECT due_date FROM cards")} == {expected}
        finally:
            conn.close()
