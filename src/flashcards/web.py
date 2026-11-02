"""The Flask web app: home page and statistics pages.

This module must never import `anthropic` (directly or indirectly): the app needs no API key.
"""

from collections.abc import Mapping
from datetime import date
from typing import Any

from flask import Flask, abort, current_app, g, render_template

from flashcards.config import load_settings
from flashcards.models import Stats
from flashcards.storage import CardStore, open_db

MAX_DECK_ID = 2**63 - 1  # sqlite3 binds 64-bit ints only; a bigger id would raise OverflowError
NO_VALUE = "\N{EM DASH}"


def create_app(test_config: Mapping[str, Any] | None = None) -> Flask:
    """Build the app. `test_config` overrides `DATABASE` (path) and `TODAY` (a date or None)."""
    app = Flask(__name__)
    settings = load_settings()
    app.config.from_mapping(DATABASE=str(settings.db_path), TODAY=settings.today)
    if test_config is not None:
        app.config.from_mapping(test_config)

    app.teardown_appcontext(close_store)
    app.register_error_handler(404, page_not_found)
    app.add_url_rule("/", "home", home)
    app.add_url_rule("/stats", "all_stats", all_stats)
    app.add_url_rule(f"/decks/<int(max={MAX_DECK_ID}):deck_id>/stats", "deck_stats", deck_stats)
    app.add_url_rule(f"/decks/<int(max={MAX_DECK_ID}):deck_id>/quiz", "quiz", quiz)
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


def quiz(deck_id: int) -> tuple[str, int]:
    """Placeholder until the quiz flow exists (batch 2.5); keeps the home page links valid."""
    if get_store().get_deck(deck_id) is None:
        abort(404)
    return "The quiz is not available yet.", 501


def page_not_found(_error: Exception) -> tuple[str, int]:
    return render_template("404.html"), 404
