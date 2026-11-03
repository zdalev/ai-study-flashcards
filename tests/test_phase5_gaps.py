"""Extra Phase 5 tests from the phase-end verification (gaps beyond the batch tests).

- answers that differ only in inner spacing or in Unicode form count as the same option;
- the option order depends on the day (the seed is the card and the day), not only on the card;
- the result line of the multiple choice result page is styled as a miss for Wrong and No idea;
- a version 1 file that was upgraded has the same `mode` CHECK as a new file;
- a choice page answered after midnight, or after the subject got a new card, is graded on the
  option text it showed (the page posts the text, not an index into rebuilt options).
"""

import re
import sqlite3
from datetime import timedelta

import pytest

from conftest import WEB_TODAY
from flashcards import scheduler
from flashcards.errors import StorageError
from flashcards.storage import CardStore, open_db
from test_storage import make_v1_file
from test_web_choice import (
    add_cards,
    choose,
    deck_of_five,
    dont_know,
    options_of,
    quiz_url,
    real_options,
    reviews_of,
    text,
    value_of,
)

# --- option building ---


def test_answers_that_differ_only_in_inner_spacing_are_one_option(client, db_path):
    # Without the whitespace folding there would be three "other" answers and a choice page.
    deck_id = add_cards(
        db_path,
        "Spacing",
        [("Q1?", "C"), ("Q2?", "alpha beta"), ("Q3?", "alpha  beta"), ("Q4?", "d")],
    )
    body = text(client.get(quiz_url(deck_id)))
    assert "Multiple choice needs at least 4 different answers" in body
    assert 'name="choice"' not in body


def test_answers_that_differ_only_in_unicode_form_are_one_option(client, db_path):
    composed, decomposed = "café", "café"  # the same word, two ways to write the e
    deck_id = add_cards(
        db_path, "Unicode", [("Q1?", "C"), ("Q2?", composed), ("Q3?", decomposed), ("Q4?", "d")]
    )
    # add_cards stores NFC text already, so the duplicate is caught even before the option step.
    body = text(client.get(quiz_url(deck_id)))
    assert "Multiple choice needs at least 4 different answers" in body


def test_the_option_order_changes_from_day_to_day(app, client, db_path):
    deck_id = add_cards(db_path, "Many", [(f"Q{n}?", f"A{n}") for n in range(1, 13)])
    seen = set()
    for offset in range(8):
        app.config["TODAY"] = WEB_TODAY + timedelta(days=offset)
        seen.add(tuple(real_options(client.get(quiz_url(deck_id)))))
    assert len(seen) > 1  # a seed of the card alone would show the same options every day


# --- the result page ---


def test_the_result_line_is_styled_as_a_hit_only_for_correct(client, db_path):
    deck_id = deck_of_five(db_path)
    page = client.get(quiz_url(deck_id))
    right = text(choose(client, deck_id, 1, value_of(page, "A1")))
    wrong_value = next(v for v, t in options_of(page) if t not in ("A2", "I don't know"))
    wrong = text(choose(client, deck_id, 2, wrong_value))
    idk = text(dont_know(client, deck_id, 3))
    assert re.search(r'class="result result-correct">\s*Correct', right)
    assert re.search(r'class="result result-miss">\s*Wrong', wrong)
    assert re.search(r'class="result result-miss">\s*No idea', idk)


# --- schema parity between a new file and an upgraded one ---


@pytest.mark.parametrize("origin", ["new", "upgraded"])
def test_the_mode_column_rejects_unknown_modes_in_new_and_upgraded_files(db_path, origin):
    if origin == "upgraded":
        make_v1_file(db_path)  # has card 1
        open_db(db_path).close()
    else:
        add_cards(db_path, "Biology", [("Q1?", "A1")])  # card 1
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reviews (card_id, reviewed_on, grade, mode)"
                " VALUES (1, '2026-03-11', 4, 'bogus')"
            )
    finally:
        conn.close()


def test_save_review_rejects_an_unknown_mode_on_an_upgraded_file(db_path):
    make_v1_file(db_path)
    with CardStore(open_db(db_path)) as store:
        card = store.get_card(1)
        state = scheduler.review(card.state, 4, card.state.due_date)
        with pytest.raises(StorageError):
            store.save_review(1, state, 4, mode="bogus", given_answer="x")
        assert store.recent_answers(1)[0].mode is None  # nothing was saved
        assert store.get_card(1).state == card.state


# --- options are rebuilt when the answer arrives, but the answer is graded on the shown text ---


def _first_day_where_the_correct_option_moves(app, client, deck_id, answer):
    """(position now, first later day on which the rebuilt options put the answer elsewhere)."""
    first = real_options(client.get(quiz_url(deck_id))).index(answer)
    for offset in range(1, 30):
        app.config["TODAY"] = WEB_TODAY + timedelta(days=offset)
        if real_options(client.get(quiz_url(deck_id))).index(answer) != first:
            return first, WEB_TODAY + timedelta(days=offset)
    raise AssertionError("the options never moved")  # pragma: no cover


def test_a_choice_page_answered_after_midnight_is_graded_on_the_options_it_showed(
    app, client, db_path
):
    deck_id = deck_of_five(db_path)
    app.config["TODAY"] = WEB_TODAY
    shown = client.get(quiz_url(deck_id))
    card_id = int(re.search(r'name="card_id" value="(\d+)"', text(shown)).group(1))
    answer = f"A{card_id}"
    value = value_of(shown, answer)
    _position, later = _first_day_where_the_correct_option_moves(app, client, deck_id, answer)
    app.config["TODAY"] = later  # midnight passes while the page is open
    body = text(choose(client, deck_id, card_id, value))
    assert re.search(r">\s*Correct\s*</p>", body)
    assert reviews_of(db_path)[-1][2:] == (4, "choice", answer)


def test_a_choice_page_answered_after_a_card_was_added_is_graded_on_the_options_it_showed(
    client, db_path
):
    # The new card changes which answers can be drawn, so some pages would move the right option;
    # several subjects make sure the old index-based form would have failed on at least one.
    for trial in range(10):
        name = f"Grow {trial}"
        deck_id = add_cards(db_path, name, [(f"Q{trial}.{n}?", f"A{trial}.{n}") for n in range(8)])
        shown = client.get(quiz_url(deck_id))
        card_id = int(re.search(r'name="card_id" value="(\d+)"', text(shown)).group(1))
        answer = f"A{trial}.{shown_question_number(shown)}"
        value = value_of(shown, answer)
        # Another tab adds a card to the same subject before the answer is posted.
        add_cards(db_path, name, [(f"New{trial}?", f"New answer {trial}")])
        body = text(choose(client, deck_id, card_id, value))
        assert re.search(r">\s*Correct\s*</p>", body), f"trial {trial}: picked the right option"


def shown_question_number(page):
    """The `n` of the card shown ('Q<trial>.<n>?')."""
    return re.search(r'class="question">Q\d+\.(\d+)\?', text(page)).group(1)
