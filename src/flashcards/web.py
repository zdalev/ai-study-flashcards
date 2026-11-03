"""The Flask web app: home page, statistics pages and the quiz.

This module must never import `anthropic` (directly or indirectly): the app needs no API key.
"""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from flask import Flask, abort, current_app, g, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from flashcards import scheduler
from flashcards.config import load_settings
from flashcards.errors import StorageError
from flashcards.models import Card, CardDraft, Deck, Stats
from flashcards.storage import CardStore, open_db

MAX_ID = 2**63 - 1  # sqlite3 binds 64-bit ints only; a bigger id would raise OverflowError
MAX_GRADE = 5
MAX_DECK_NAME_LEN = 100
MAX_QUESTION_LEN = 500
MAX_ANSWER_LEN = 2000
# The quiz offers three answers; the server accepts any grade 0-5 (the scheduler is unchanged).
GRADE_BUTTONS = ((0, "No idea"), (1, "Wrong"), (4, "Correct"))
MODE_WRITE = "write"
MODES = (MODE_WRITE,)
NO_VALUE = "\N{EM DASH}"


def create_app(test_config: Mapping[str, Any] | None = None) -> Flask:
    """Build the app. `test_config` overrides `DATABASE` (path) and `TODAY` (a date or None)."""
    app = Flask(__name__)
    settings = load_settings()
    app.config.from_mapping(DATABASE=str(settings.db_path), TODAY=settings.today)
    if test_config is not None:
        app.config.from_mapping(test_config)

    app.teardown_appcontext(close_store)
    app.register_error_handler(400, bad_request)
    app.register_error_handler(404, page_not_found)
    app.register_error_handler(StorageError, storage_failed)
    app.add_url_rule("/", "home", home)
    app.add_url_rule("/stats", "all_stats", all_stats)
    app.add_url_rule("/decks/new", "deck_new", deck_new, methods=["GET", "POST"])
    app.add_url_rule(f"/decks/<int(max={MAX_ID}):deck_id>/stats", "deck_stats", deck_stats)
    app.add_url_rule(
        f"/decks/<int(max={MAX_ID}):deck_id>/quiz", "quiz", quiz, methods=["GET", "POST"]
    )
    app.add_url_rule(
        f"/decks/<int(max={MAX_ID}):deck_id>/cards/new",
        "card_new",
        card_new,
        methods=["GET", "POST"],
    )
    return app


def get_store() -> CardStore:
    """The store for this request. One connection per request, opened on first use."""
    if "store" not in g:
        g.store = CardStore(open_db(current_app.config["DATABASE"]))
    return g.store


def close_store(_exception: BaseException | None) -> None:
    store = g.pop("store", None)
    if store is not None:
        store.close()


def today() -> date:
    """The `TODAY` config (demos and tests) or the real date."""
    return current_app.config["TODAY"] or date.today()


def percent_text(correct: int, total: int) -> str:
    """'67% (2/3)', or an em dash when there is nothing to measure. Rounds half up."""
    if total == 0:
        return NO_VALUE
    percent = (correct * 200 + total) // (2 * total)
    return f"{percent}% ({correct}/{total})"


def stats_figures(stats: Stats) -> dict[str, str]:
    """The numbers of the stats page, already formatted for display."""
    return {
        "accuracy_today": percent_text(stats.correct_today, stats.reviews_today),
        "accuracy_all_time": percent_text(stats.correct_total, stats.reviews_total),
        "total_cards": str(stats.total_cards),
        "due_today": str(stats.due_today),
        "reviewed_today": str(stats.reviewed_today),
        "mature_cards": str(stats.mature_cards),
        "new_cards": str(stats.new_cards),
        "average_ease": NO_VALUE if stats.average_ease is None else f"{stats.average_ease:.2f}",
        "next_due": NO_VALUE if stats.next_due is None else stats.next_due.isoformat(),
    }


def home() -> str:
    return render_template("home.html", summaries=get_store().deck_summaries(today()))


def all_stats() -> str:
    stats = get_store().stats(today())
    return render_template("stats.html", deck=None, figures=stats_figures(stats))


def deck_stats(deck_id: int) -> str:
    store = get_store()
    deck = store.get_deck(deck_id)
    if deck is None:
        abort(404)
    return render_template(
        "stats.html", deck=deck, figures=stats_figures(store.stats(today(), deck_id))
    )


def quiz(deck_id: int) -> str | Response:
    store = get_store()
    deck = store.get_deck(deck_id)
    if deck is None:
        abort(404)
    if request.method == "POST":
        if request.form.get("step") == "answer":
            return show_reveal(store, deck)
        return submit_grade(store, deck_id)
    return show_quiz(store, deck)


def quiz_mode(source: Mapping[str, str]) -> str:
    """The answer mode named in the query string or form; anything unknown means write."""
    mode = source.get("mode")
    return mode if mode in MODES else MODE_WRITE


def show_quiz(store: CardStore, deck: Deck) -> str:
    """The next due card (most overdue first), or the done page when nothing is due."""
    due = store.due_cards(today(), deck.id)
    if not due:
        stats = store.stats(today(), deck.id)
        return render_template(
            "done.html",
            deck=deck,
            reviewed_today=stats.reviewed_today,
            accuracy_today=percent_text(stats.correct_today, stats.reviews_today),
            next_due=NO_VALUE if stats.next_due is None else stats.next_due.isoformat(),
        )
    return render_template(
        "quiz.html",
        deck=deck,
        card=due[0],
        due_count=len(due),
        mode=quiz_mode(request.args),
        max_answer=MAX_ANSWER_LEN,
    )


def form_card(store: CardStore, deck_id: int) -> Card:
    """The posted card: 400 for a bad `card_id`, 404 for an unknown card or one of another deck."""
    card_id = request.form.get("card_id", type=int)
    if card_id is None or not 0 < card_id <= MAX_ID:
        abort(400)
    card = store.get_card(card_id)
    if card is None or card.deck_id != deck_id:
        abort(404)
    return card


def form_given_answer() -> str | None:
    """The typed answer (None when the form has none). 400 when it is too long."""
    if "given_answer" not in request.form:
        return None
    given = form_text("given_answer").strip()
    if len(given) > MAX_ANSWER_LEN:
        abort(400)
    return given


def show_reveal(store: CardStore, deck: Deck) -> str | Response:
    """Step two of the write flow: the typed answer next to the correct one, then the grade buttons.

    Nothing is saved here, so refreshing this page is harmless. A card that is no longer due
    (answered in another tab) goes back to the quiz.
    """
    card = form_card(store, deck.id)
    given = form_given_answer()
    mode = quiz_mode(request.form)
    if not scheduler.is_due(card.state, today()):
        return redirect(url_for("quiz", deck_id=deck.id, mode=mode), code=303)
    return render_template(
        "reveal.html",
        deck=deck,
        card=card,
        given_answer=given or "",
        mode=mode,
        grades=GRADE_BUTTONS,
    )


def submit_grade(store: CardStore, deck_id: int) -> Response:
    """Apply one grade, then redirect (POST/redirect/GET) to the next card in the same mode."""
    grade = request.form.get("grade", type=int)
    if grade is None or not 0 <= grade <= MAX_GRADE:
        abort(400)
    card = form_card(store, deck_id)
    given = form_given_answer()
    mode = quiz_mode(request.form)
    if scheduler.is_due(card.state, today()):  # a repeated submit finds the card not due: no-op
        new_state = scheduler.review(card.state, grade, today())
        # False: a parallel request reviewed the card first, so this one is dropped as well.
        store.save_review(card.id, new_state, grade, mode=mode, given_answer=given)
    return redirect(url_for("quiz", deck_id=deck_id, mode=mode), code=303)


def deck_new() -> str | Response:
    if request.method == "POST":
        return create_deck()
    return render_deck_form(name="", error=None)


def render_deck_form(name: str, error: str | None, status: int = 200) -> tuple[str, int]:
    html = render_template("deck_new.html", name=name, error=error, max_length=MAX_DECK_NAME_LEN)
    return html, status


def form_text(field: str) -> str:
    """A form field with every line break as LF.

    Browsers post textarea line breaks as CRLF but count them as one character for `maxlength`.
    """
    return request.form.get(field, "").replace("\r\n", "\n").replace("\r", "\n")


def create_deck() -> str | tuple[str, int] | Response:
    """Create the subject (or reuse the one with the same name) and go to its add-card form."""
    name = form_text("name")
    if not name.strip():
        return render_deck_form(name, "Please enter a subject name.", 400)
    if len(name.strip()) > MAX_DECK_NAME_LEN:
        return render_deck_form(
            name, f"The subject name is too long (at most {MAX_DECK_NAME_LEN} characters).", 400
        )
    deck = get_store().get_or_create_deck(name, None, datetime.now())
    return redirect(url_for("card_new", deck_id=deck.id), code=303)


def card_new(deck_id: int) -> str | tuple[str, int] | Response:
    store = get_store()
    deck = store.get_deck(deck_id)
    if deck is None:
        abort(404)
    if request.method == "POST":
        return create_card(store, deck)
    return render_card_form(deck, question="", answer="", error=None)


def render_card_form(
    deck: Deck, question: str, answer: str, error: str | None, status: int = 200
) -> tuple[str, int]:
    html = render_template(
        "card_new.html",
        deck=deck,
        question=question,
        answer=answer,
        error=error,
        added=request.method == "GET" and request.args.get("added") == "1",
        max_question=MAX_QUESTION_LEN,
        max_answer=MAX_ANSWER_LEN,
    )
    return html, status


def card_form_error(question: str, answer: str) -> str | None:
    """The message for unusable input, or None when the card can be saved."""
    if not question.strip():
        return "Please enter a question."
    if not answer.strip():
        return "Please enter an answer."
    if len(question.strip()) > MAX_QUESTION_LEN:
        return f"The question is too long (at most {MAX_QUESTION_LEN} characters)."
    if len(answer.strip()) > MAX_ANSWER_LEN:
        return f"The answer is too long (at most {MAX_ANSWER_LEN} characters)."
    return None


def create_card(store: CardStore, deck: Deck) -> tuple[str, int] | Response:
    """Save one card (due today), then redirect (POST/redirect/GET) to a fresh form."""
    question = form_text("question")
    answer = form_text("answer")
    error = card_form_error(question, answer)
    if error:
        return render_card_form(deck, question, answer, error, 400)
    if store.add_cards(deck.id, [CardDraft(question, answer)], today(), datetime.now()) == 0:
        error = f"This question already exists in {deck.name}."
        return render_card_form(deck, question, answer, error, 409)
    return redirect(url_for("card_new", deck_id=deck.id, added=1), code=303)


def bad_request(_error: Exception) -> tuple[str, int]:
    return render_template("400.html"), 400


def page_not_found(_error: Exception) -> tuple[str, int]:
    return render_template("404.html"), 404


def storage_failed(error: Exception) -> tuple[str, int]:
    """A friendly 500 page; the details go to the log, not into the page."""
    current_app.logger.error("database error: %s", error)
    return render_template("500.html"), 500
