"""The Flask web app: home page, statistics pages and the quiz.

This module must never import `anthropic` (directly or indirectly): the app needs no API key.
"""

from collections.abc import Mapping
from datetime import date
from typing import Any

from flask import Flask, abort, current_app, g, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from flashcards import scheduler
from flashcards.config import load_settings
from flashcards.models import Deck, Stats
from flashcards.storage import CardStore, open_db

MAX_ID = 2**63 - 1  # sqlite3 binds 64-bit ints only; a bigger id would raise OverflowError
MAX_GRADE = 5
GRADE_LABELS = (
    (0, "Blackout"),
    (1, "Wrong"),
    (2, "Wrong but familiar"),
    (3, "Hard"),
    (4, "Good"),
    (5, "Perfect"),
)
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
    app.add_url_rule("/", "home", home)
    app.add_url_rule("/stats", "all_stats", all_stats)
    app.add_url_rule(f"/decks/<int(max={MAX_ID}):deck_id>/stats", "deck_stats", deck_stats)
    app.add_url_rule(
        f"/decks/<int(max={MAX_ID}):deck_id>/quiz", "quiz", quiz, methods=["GET", "POST"]
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
        return submit_grade(store, deck_id)
    return show_quiz(store, deck)


def show_quiz(store: CardStore, deck: Deck) -> str:
    """The next due card (most overdue first), or the done page when nothing is due."""
    due = store.due_cards(today(), deck.id)
    previous = previous_card_note(store, deck.id)
    if not due:
        stats = store.stats(today(), deck.id)
        return render_template(
            "done.html",
            deck=deck,
            previous=previous,
            reviewed_today=stats.reviewed_today,
            accuracy_today=percent_text(stats.correct_today, stats.reviews_today),
            next_due=NO_VALUE if stats.next_due is None else stats.next_due.isoformat(),
        )
    return render_template(
        "quiz.html",
        deck=deck,
        card=due[0],
        due_count=len(due),
        previous=previous,
        grades=GRADE_LABELS,
    )


def previous_card_note(store: CardStore, deck_id: int) -> dict[str, Any] | None:
    """Feedback for `?prev=<card_id>`: when that card is next due. None if prev is unusable.

    An invalid, unknown or foreign id, or a card that is still due today (so it was not just
    reviewed), is ignored without an error.
    """
    card_id = request.args.get("prev", type=int)
    if card_id is None or not 0 < card_id <= MAX_ID:
        return None
    card = store.get_card(card_id)
    if card is None or card.deck_id != deck_id:
        return None
    days = (card.state.due_date - today()).days
    if days <= 0:
        return None
    return {"due_date": card.state.due_date.isoformat(), "days": days}


def submit_grade(store: CardStore, deck_id: int) -> Response:
    """Apply one answer, then redirect (POST/redirect/GET) to the next card."""
    card_id = request.form.get("card_id", type=int)
    grade = request.form.get("grade", type=int)
    if card_id is None or not 0 < card_id <= MAX_ID:
        abort(400)
    if grade is None or not 0 <= grade <= MAX_GRADE:
        abort(400)
    card = store.get_card(card_id)
    if card is None or card.deck_id != deck_id:
        abort(404)
    if scheduler.is_due(card.state, today()):  # a repeated submit finds the card not due: no-op
        new_state = scheduler.review(card.state, grade, today())
        store.save_review(card_id, new_state, grade)
    return redirect(url_for("quiz", deck_id=deck_id, prev=card_id), code=303)


def bad_request(_error: Exception) -> tuple[str, int]:
    return render_template("400.html"), 400


def page_not_found(_error: Exception) -> tuple[str, int]:
    return render_template("404.html"), 404
