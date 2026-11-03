import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

from conftest import WEB_TODAY
from flashcards import web
from flashcards.cli import main
from flashcards.models import ReviewState
from flashcards.storage import CardStore, open_db
from flashcards.web import create_app, percent_text

HOSTILE = "<script>alert(1)</script>"


def text(response):
    return response.get_data(as_text=True)


def review(db_path, card_id, grade, reviewed_on=WEB_TODAY, interval=1, ease=2.5):
    """Log a review for a card through the storage API."""
    state = ReviewState(
        due_date=reviewed_on + timedelta(days=interval),
        ease_factor=ease,
        interval_days=interval,
        repetitions=1,
        last_reviewed=reviewed_on,
    )
    with CardStore(open_db(db_path)) as store:
        with store._conn:
            store._conn.execute(
                "UPDATE cards SET due_date = ? WHERE id = ?", (reviewed_on.isoformat(), card_id)
            )
        assert store.save_review(card_id, state, grade)  # a card can only be reviewed once due


def stats_of(db_path, deck_id=None, today=WEB_TODAY):
    with CardStore(open_db(db_path)) as store:
        return store.stats(today, deck_id)


# --- home ---


def test_home_lists_decks_with_counts_and_links(client, seed):
    biology = seed("Biology", questions=("A?", "B?", "C?"))
    physics = seed("Physics", questions=("D?",))
    seed("Chemistry", questions=("E?", "F?"), today=WEB_TODAY + timedelta(days=3))
    response = client.get("/")
    body = text(response)
    assert response.status_code == 200
    for name in ("Biology", "Physics", "Chemistry"):
        assert name in body
    assert "3 cards" in body
    assert ">Study</a>" not in body  # replaced by the two mode buttons
    assert "3 due" in body  # Biology: all three are due today
    assert "0 due" in body  # Chemistry: not due for three days
    for deck_id in (biology, physics):
        assert f'href="/decks/{deck_id}/quiz?mode=write">Study: Write</a>' in body
        assert f'href="/decks/{deck_id}/quiz?mode=choice">Study: Multiple choice</a>' in body
        assert f'href="/decks/{deck_id}/stats"' in body
    assert 'href="/stats"' in body
    assert body.index("Biology") < body.index("Chemistry") < body.index("Physics")  # by name


def test_home_uses_singular_card(client, seed):
    seed("Physics", questions=("D?",))
    assert "1 card" in text(client.get("/")) and "1 cards" not in text(client.get("/"))


def test_home_empty_state_shows_the_load_demo_hint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "flashcards load-demo" in text(response)
    assert "/quiz" not in text(response)


def test_home_escapes_deck_names(client, seed):
    seed(HOSTILE)
    body = text(client.get("/"))
    assert HOSTILE not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_today_config_changes_the_due_counts(db_path, seed):
    seed("Biology", questions=("A?", "B?"), today=WEB_TODAY + timedelta(days=5))
    early = create_app({"DATABASE": str(db_path), "TODAY": WEB_TODAY}).test_client()
    late = create_app({"DATABASE": str(db_path), "TODAY": WEB_TODAY + timedelta(days=5)})
    assert "0 due" in text(early.get("/"))
    assert "2 due" in text(late.test_client().get("/"))


def test_today_defaults_to_the_real_date(db_path):
    app = create_app({"DATABASE": str(db_path)})
    with app.app_context():
        assert web.today() == date.today()


def test_defaults_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASHCARDS_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("FLASHCARDS_TODAY", "2026-01-05")
    app = create_app()
    assert app.config["DATABASE"] == str(tmp_path / "env.db")
    assert app.config["TODAY"] == date(2026, 1, 5)


def test_test_config_overrides_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASHCARDS_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("FLASHCARDS_TODAY", "2026-01-05")
    app = create_app({"DATABASE": "other.db", "TODAY": date(2030, 1, 1)})
    assert app.config["DATABASE"] == "other.db"
    assert app.config["TODAY"] == date(2030, 1, 1)


def test_unusable_database_gives_a_friendly_500_page(client, db_path):
    db_path.write_bytes(b"this is not a sqlite database " * 20)

    response = client.get("/")

    body = text(response)
    assert response.status_code == 500
    assert "The database could not be opened - check FLASHCARDS_DB / --db" in body
    assert "site-header" in body  # extends the base layout
    assert "Traceback" not in body and "sqlite3" not in body
    assert str(db_path) not in body


def test_pages_have_a_stylesheet_that_is_served(client):
    body = text(client.get("/"))
    assert 'href="/static/style.css"' in body
    with client.get("/static/style.css") as css:  # closes the file behind the response
        assert css.status_code == 200
        assert css.mimetype == "text/css"


# --- stats ---


def card_ids(db_path, deck_id):
    with CardStore(open_db(db_path)) as store:
        return [card.id for card in store.due_cards(WEB_TODAY + timedelta(days=400), deck_id)]


def test_stats_without_reviews_show_dashes(client, seed):
    seed("Biology", questions=("A?", "B?"))
    body = text(client.get("/stats"))
    assert "Accuracy today</th><td>\N{EM DASH}<" in body
    assert "Accuracy all time</th><td>\N{EM DASH}<" in body
    assert "Next due</th><td>\N{EM DASH}<" in body


def test_stats_match_the_store_numbers(client, db_path, seed):
    deck_id = seed("Biology", questions=("A?", "B?", "C?", "D?"))
    first, second, third, _fourth = card_ids(db_path, deck_id)
    review(db_path, first, 5)
    review(db_path, second, 3, interval=30, ease=2.2)
    review(db_path, third, 1, reviewed_on=WEB_TODAY - timedelta(days=1), interval=0)
    stats = stats_of(db_path, deck_id)
    body = text(client.get(f"/decks/{deck_id}/stats"))
    assert response_cells(body) == {
        "Accuracy today": "100% (2/2)",
        "Accuracy all time": "67% (2/3)",
        "Cards": str(stats.total_cards),
        "Due today": str(stats.due_today),
        "Reviewed today": str(stats.reviewed_today),
        "Mature cards": str(stats.mature_cards),
        "New cards": str(stats.new_cards),
        "Average ease": f"{stats.average_ease:.2f}",
        "Next due": stats.next_due.isoformat(),
    }
    assert stats.reviewed_today == 2 and stats.mature_cards == 1 and stats.new_cards == 1
    assert stats.next_due == WEB_TODAY + timedelta(days=1)


def response_cells(body):
    """Read the label -> value pairs of the stats table."""
    pairs = re.findall(r'<th scope="row">(.*?)</th><td>(.*?)</td>', body)
    return dict(pairs)


def test_all_stats_cover_every_deck_and_a_deck_page_only_its_own(client, db_path, seed):
    biology = seed("Biology", questions=("A?", "B?"))
    physics = seed("Physics", questions=("C?",))
    review(db_path, card_ids(db_path, physics)[0], 0)
    everything = response_cells(text(client.get("/stats")))
    only_biology = response_cells(text(client.get(f"/decks/{biology}/stats")))
    only_physics = response_cells(text(client.get(f"/decks/{physics}/stats")))
    assert everything["Cards"] == "3"
    assert only_biology["Cards"] == "2" and only_biology["Accuracy all time"] == "\N{EM DASH}"
    assert only_physics["Cards"] == "1" and only_physics["Accuracy all time"] == "0% (0/1)"
    assert everything["Accuracy all time"] == "0% (0/1)"


def test_stats_titles_name_the_subject(client, seed):
    deck_id = seed("Biology")
    assert "Stats: Biology" in text(client.get(f"/decks/{deck_id}/stats"))
    assert "Stats: All flashcards" in text(client.get("/stats"))


def test_stats_pages_escape_the_deck_name(client, seed):
    deck_id = seed(HOSTILE)
    body = text(client.get(f"/decks/{deck_id}/stats"))
    assert HOSTILE not in body
    assert "&lt;script&gt;" in body


def test_stats_on_an_empty_database_work(client):
    response = client.get("/stats")
    assert response.status_code == 200
    assert response_cells(text(response))["Cards"] == "0"


@pytest.mark.parametrize(
    ("correct", "total", "expected"),
    [
        (2, 3, "67% (2/3)"),
        (1, 3, "33% (1/3)"),
        (0, 1, "0% (0/1)"),
        (5, 5, "100% (5/5)"),
        (1, 8, "13% (1/8)"),  # 12.5 rounds half up
        (0, 0, "\N{EM DASH}"),
    ],
)
def test_percent_text(correct, total, expected):
    assert percent_text(correct, total) == expected


# --- 404 and bad ids ---


@pytest.mark.parametrize(
    "path",
    [
        "/decks/999/stats",
        "/decks/0/stats",
        "/decks/abc/stats",
        "/decks/-1/stats",
        "/decks/1.5/stats",
        "/decks/1/stats/",
        "/decks/99999999999999999999/stats",
        "/decks/9223372036854775808/stats",
        "/decks/999/quiz",
        "/decks/99999999999999999999/quiz",
        "/nothing-here",
    ],
)
def test_unknown_pages_and_ids_give_the_404_page(client, seed, path):
    seed("Biology")
    response = client.get(path)
    assert response.status_code == 404
    assert "Page not found" in text(response)
    assert 'href="/stats"' in text(response)  # the page still extends the base template


def test_the_largest_valid_id_is_handled_without_error(client):
    assert client.get("/decks/9223372036854775807/stats").status_code == 404


def test_post_to_a_page_is_not_allowed(client):
    assert client.post("/stats").status_code == 405


# --- connection per request ---


def test_connection_is_closed_after_each_request(client, seed, monkeypatch):
    seed("Biology")
    opened = []
    real_open_db = web.open_db

    def recording_open_db(path):
        conn = real_open_db(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(web, "open_db", recording_open_db)
    assert client.get("/").status_code == 200
    assert client.get("/stats").status_code == 200
    assert client.get("/decks/999/stats").status_code == 404
    assert len(opened) == 3  # one per request
    for conn in opened:
        with pytest.raises(Exception, match="closed"):
            conn.execute("SELECT 1")


def test_connection_is_closed_when_the_view_fails(app, monkeypatch):
    opened = []
    real_open_db = web.open_db

    def recording_open_db(path):
        opened.append(real_open_db(path))
        return opened[-1]

    def broken_stats(self, today, deck_id=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "open_db", recording_open_db)
    monkeypatch.setattr(CardStore, "stats", broken_stats)
    with pytest.raises(RuntimeError, match="boom"):
        app.test_client().get("/stats")
    with pytest.raises(Exception, match="closed"):
        opened[0].execute("SELECT 1")


def test_no_connection_is_opened_for_a_404_route(client, monkeypatch):
    def fail(path):
        raise AssertionError("the database must not be opened")

    monkeypatch.setattr(web, "open_db", fail)
    assert client.get("/nothing-here").status_code == 404


def test_pages_work_without_an_api_key(client, seed, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seed("Biology")
    assert client.get("/").status_code == 200
    assert client.get("/stats").status_code == 200


def test_importing_web_does_not_import_anthropic():
    code = "import sys, flashcards.web; sys.exit(1 if 'anthropic' in sys.modules else 0)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_serving_the_app_does_not_import_anthropic(tmp_path):
    code = (
        "import sys\n"
        "from datetime import date\n"
        "from flashcards.web import create_app\n"
        f"db = {str(tmp_path / 'x.db')!r}\n"
        "app = create_app({'DATABASE': db, 'TODAY': date(2026, 11, 2)})\n"
        "c = app.test_client()\n"
        "assert c.get('/').status_code == 200 and c.get('/stats').status_code == 200\n"
        "sys.exit(1 if 'anthropic' in sys.modules else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- serve ---


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def run(self, **kwargs):
        calls.append((self, kwargs))

    monkeypatch.setattr(Flask, "run", run)
    return calls


def test_serve_runs_the_app_on_localhost_with_debug_off(fake_run):
    assert main(["serve"]) == 0
    ((app, kwargs),) = fake_run
    assert isinstance(app, Flask)
    assert kwargs == {"host": "127.0.0.1", "port": 5000, "debug": False, "load_dotenv": False}


def test_serve_accepts_host_and_port(fake_run):
    assert main(["serve", "--host", "0.0.0.0", "--port", "8080"]) == 0
    assert fake_run[0][1]["host"] == "0.0.0.0"
    assert fake_run[0][1]["port"] == 8080
    assert fake_run[0][1]["debug"] is False


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", ""])
def test_serve_rejects_a_bad_port(fake_run, port, capsys):
    assert main(["serve", "--port", port]) == 2
    assert "--port" in capsys.readouterr().err
    assert fake_run == []


def test_serve_passes_db_and_today_to_the_app(fake_run, tmp_path):
    db = tmp_path / "chosen.db"
    assert main(["--db", str(db), "--today", "2026-01-05", "serve"]) == 0
    app = fake_run[0][0]
    assert app.config["DATABASE"] == str(db)
    assert app.config["TODAY"] == date(2026, 1, 5)


def test_serve_falls_back_to_settings(fake_run, tmp_path, monkeypatch):
    monkeypatch.setenv("FLASHCARDS_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("FLASHCARDS_TODAY", "2026-02-03")
    assert main(["serve"]) == 0
    app = fake_run[0][0]
    assert app.config["DATABASE"] == str(tmp_path / "env.db")
    assert app.config["TODAY"] == date(2026, 2, 3)


def test_serve_reports_a_bad_flashcards_today(fake_run, monkeypatch, capsys):
    monkeypatch.setenv("FLASHCARDS_TODAY", "soon")
    assert main(["serve"]) == 1
    assert "FLASHCARDS_TODAY" in capsys.readouterr().err
    assert fake_run == []


def test_serve_does_not_load_dotenv_into_the_environment(fake_run, tmp_path, monkeypatch):
    # The real Flask.run would load .env; here we check that serve asks it not to.
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    main(["serve"])
    assert fake_run[0][1]["load_dotenv"] is False
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_no_page_calls_a_deck_a_subject(client, seed):
    """The app says "flashcards", never "subject", on any page (including error pages)."""
    biology = seed("Biology", questions=("Q1?", "Q2?", "Q3?", "Q4?", "Q5?"))
    short = seed("Chemistry")  # 3 cards: multiple choice falls back to write mode with a note
    finished = seed("History", today=WEB_TODAY + timedelta(days=1))  # nothing due: done page
    pages = {
        "home": client.get("/"),
        "home deleted notice": client.get("/?deleted=Biology"),
        "new": client.get("/decks/new"),
        "new blank name": client.post("/decks/new", data={"name": " "}),
        "new too long name": client.post("/decks/new", data={"name": "N" * 500}),
        "add card": client.get(f"/decks/{biology}/cards/new"),
        "add card duplicate": client.post(
            f"/decks/{biology}/cards/new", data={"question": "Q1?", "answer": "A"}
        ),
        "quiz write": client.get(f"/decks/{biology}/quiz?mode=write"),
        "quiz choice": client.get(f"/decks/{biology}/quiz?mode=choice"),
        "quiz choice fallback": client.get(f"/decks/{short}/quiz?mode=choice"),
        "reveal": client.post(
            f"/decks/{biology}/quiz",
            data={"card_id": 1, "step": "answer", "given_answer": "guess"},
        ),
        "choice result": client.post(
            f"/decks/{biology}/quiz",
            data={"card_id": 1, "mode": "choice", "dont_know": "1"},
            follow_redirects=True,
        ),
        "done": client.get(f"/decks/{finished}/quiz"),
        "stats deck": client.get(f"/decks/{biology}/stats"),
        "stats all": client.get("/stats"),
        "delete confirm": client.get(f"/decks/{biology}/delete"),
        "404": client.get("/decks/999/stats"),
        "400": client.post(f"/decks/{biology}/quiz", data={"card_id": "x"}),
    }
    for name, response in pages.items():
        assert response.status_code in (200, 400, 404, 409), name
        assert "subject" not in text(response).lower(), name
