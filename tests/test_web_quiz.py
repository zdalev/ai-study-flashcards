import re
import sqlite3
from datetime import timedelta

import pytest
from tests.conftest import WEB_TODAY

from flashcards.storage import CardStore, open_db

HOSTILE = "<script>alert(1)</script>"
TOMORROW = WEB_TODAY + timedelta(days=1)


def text(response):
    return response.get_data(as_text=True)


def quiz_url(deck_id):
    return f"/decks/{deck_id}/quiz"


def post(client, deck_id, card_id, grade):
    return client.post(quiz_url(deck_id), data={"card_id": card_id, "grade": grade})


def card_row(db_path, card_id):
    with CardStore(open_db(db_path)) as store:
        return store.get_card(card_id)


def snapshot(db_path):
    """Every card and review row, to prove a request changed nothing."""
    conn = sqlite3.connect(db_path)
    try:
        cards = conn.execute("SELECT * FROM cards ORDER BY id").fetchall()
        reviews = conn.execute("SELECT * FROM reviews ORDER BY rowid").fetchall()
    finally:
        conn.close()
    return cards, reviews


def review_count(db_path):
    return len(snapshot(db_path)[1])


def set_due(db_path, card_id, due_date):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE cards SET due_date = ? WHERE id = ?", (due_date.isoformat(), card_id))
    conn.close()


# --- GET: the question page ---


def test_get_shows_question_answer_in_details_and_six_grade_buttons(client, seed):
    deck_id = seed("Biology", questions=("What is DNA?",))
    response = client.get(quiz_url(deck_id))
    body = text(response)
    assert response.status_code == 200
    assert "What is DNA?" in body
    details = re.search(r"<details>(.*?)</details>", body, re.DOTALL)
    assert details is not None
    assert "Answer to What is DNA?" in details.group(1)
    assert body.index("What is DNA?") < body.index("<details>")  # question visible outside
    assert "Answer to What is DNA?" not in body[: body.index("<details>")]
    values = re.findall(r'<button[^>]*name="grade"[^>]*value="(\d)"', details.group(1))
    assert values == ["0", "1", "2", "3", "4", "5"]
    for label in ("Blackout", "Wrong", "Wrong but familiar", "Hard", "Good", "Perfect"):
        assert label in body
    assert body.count('method="post"') == 1  # all six buttons share one form
    assert re.search(r'name="card_id" value="1"', body)
    assert "<script" not in body


def test_get_shows_the_due_count(client, seed):
    deck_id = seed("Biology", questions=("A?", "B?", "C?"))
    assert "3 cards due" in text(client.get(quiz_url(deck_id)))


def test_get_uses_singular_for_one_due_card(client, seed):
    deck_id = seed("Biology", questions=("A?",))
    body = text(client.get(quiz_url(deck_id)))
    assert "1 card due" in body and "1 cards" not in body


def test_get_shows_the_most_overdue_card_first(client, seed, db_path):
    deck_id = seed("Biology", questions=("Newest?", "Oldest?", "Middle?"))
    set_due(db_path, 1, WEB_TODAY - timedelta(days=1))
    set_due(db_path, 2, WEB_TODAY - timedelta(days=9))
    set_due(db_path, 3, WEB_TODAY - timedelta(days=4))
    body = text(client.get(quiz_url(deck_id)))
    assert "Oldest?" in body
    assert "Newest?" not in body and "Middle?" not in body


def test_get_only_shows_cards_of_that_deck(client, seed, db_path):
    biology = seed("Biology", questions=("Bio?",))
    physics = seed("Physics", questions=("Phys?",))
    set_due(db_path, 1, WEB_TODAY - timedelta(days=30))  # the biology card is the most overdue
    body = text(client.get(quiz_url(physics)))
    assert "Phys?" in body and "Bio?" not in body
    assert "1 card due" in body
    assert "Bio?" in text(client.get(quiz_url(biology)))


def test_get_escapes_hostile_question_and_answer(client, seed):
    deck_id = seed("Biology", questions=(HOSTILE,))
    body = text(client.get(quiz_url(deck_id)))
    assert HOSTILE not in body
    assert body.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 2  # question and answer


def test_get_does_not_change_the_database(client, seed, db_path):
    deck_id = seed("Biology")
    before = snapshot(db_path)
    client.get(quiz_url(deck_id))
    assert snapshot(db_path) == before


# --- POST: grading ---


def test_post_grade_5_redirects_with_303_and_updates_the_card(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?"))
    response = post(client, deck_id, 1, 5)
    assert response.status_code == 303
    assert response.headers["Location"].endswith(f"{quiz_url(deck_id)}?prev=1")
    state = card_row(db_path, 1).state
    assert (state.repetitions, state.interval_days) == (1, 1)
    assert state.due_date == TOMORROW
    assert state.ease_factor == 2.6
    assert state.last_reviewed == WEB_TODAY
    cards, reviews = snapshot(db_path)
    assert len(reviews) == 1
    assert reviews[0] == (1, WEB_TODAY.isoformat(), 5)


def test_post_grade_2_resets_repetitions_and_lowers_ease(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?",))
    assert post(client, deck_id, 1, 2).status_code == 303
    state = card_row(db_path, 1).state
    assert (state.repetitions, state.interval_days) == (0, 1)
    assert state.due_date == TOMORROW
    assert state.ease_factor == 2.18
    assert snapshot(db_path)[1] == [(1, WEB_TODAY.isoformat(), 2)]


@pytest.mark.parametrize("grade", range(6))
def test_every_grade_from_0_to_5_is_accepted(client, seed, grade):
    deck_id = seed("Biology", questions=("A?",))
    assert post(client, deck_id, 1, grade).status_code == 303


def test_following_the_redirect_shows_the_next_card_and_feedback(client, seed):
    deck_id = seed("Biology", questions=("First?", "Second?"))
    response = post(client, deck_id, 1, 5)
    follow = client.get(response.headers["Location"])
    body = text(follow)
    assert follow.status_code == 200
    assert "Second?" in body and "First?" not in body
    assert f"Previous card: next review on {TOMORROW.isoformat()} (in 1 day)" in body
    assert "1 card due" in body


def test_prev_feedback_pluralises_days(client, seed):
    deck_id = seed("Biology", questions=("First?", "Second?"))
    post(client, deck_id, 1, 5)  # first review: due in 1 day
    # review it again after it became due: the second interval is 6 days
    later = WEB_TODAY + timedelta(days=1)
    client.application.config["TODAY"] = later
    post(client, deck_id, 1, 5)
    body = text(client.get(f"{quiz_url(deck_id)}?prev=1"))
    assert f"next review on {(later + timedelta(days=6)).isoformat()} (in 6 days)" in body


def test_double_submit_is_ignored(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?"))
    post(client, deck_id, 1, 5)
    before = snapshot(db_path)
    response = post(client, deck_id, 1, 0)
    assert response.status_code == 303
    assert response.headers["Location"].endswith("?prev=1")
    assert snapshot(db_path) == before
    assert len(before[1]) == 1


def test_a_card_not_due_yet_is_not_reviewed(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?",))
    set_due(db_path, 1, WEB_TODAY + timedelta(days=5))
    before = snapshot(db_path)
    assert post(client, deck_id, 1, 5).status_code == 303
    assert snapshot(db_path) == before


# --- POST: validation ---


@pytest.mark.parametrize(
    "data",
    [
        {"card_id": "1", "grade": "6"},
        {"card_id": "1", "grade": "7"},
        {"card_id": "1", "grade": "-1"},
        {"card_id": "1", "grade": "x"},
        {"card_id": "1", "grade": "5.0"},
        {"card_id": "1", "grade": ""},
        {"card_id": "1"},
        {"card_id": "1", "grade": "99999999999999999999"},
        {"grade": "5"},
        {"card_id": "x", "grade": "5"},
        {"card_id": "1.5", "grade": "5"},
        {"card_id": "", "grade": "5"},
        {"card_id": "0", "grade": "5"},
        {"card_id": "-1", "grade": "5"},
        {"card_id": "99999999999999999999", "grade": "5"},
        {"card_id": "9223372036854775808", "grade": "5"},
        {},
    ],
)
def test_bad_input_gives_400_and_saves_nothing(client, seed, db_path, data):
    deck_id = seed("Biology")
    before = snapshot(db_path)
    response = client.post(quiz_url(deck_id), data=data)
    assert response.status_code == 400
    assert "Bad request" in text(response)
    assert snapshot(db_path) == before


def test_unknown_card_gives_404(client, seed, db_path):
    deck_id = seed("Biology")
    before = snapshot(db_path)
    response = post(client, deck_id, 999, 5)
    assert response.status_code == 404
    assert snapshot(db_path) == before


def test_largest_valid_card_id_gives_404_not_500(client, seed):
    deck_id = seed("Biology")
    assert post(client, deck_id, 2**63 - 1, 5).status_code == 404


def test_card_of_another_deck_gives_404_and_changes_nothing(client, seed, db_path):
    biology = seed("Biology", questions=("Bio?",))
    physics = seed("Physics", questions=("Phys?",))
    before = snapshot(db_path)
    response = post(client, biology, 2, 5)  # card 2 belongs to physics
    assert response.status_code == 404
    assert snapshot(db_path) == before
    assert post(client, physics, 2, 5).status_code == 303


def test_post_to_an_unknown_deck_gives_404(client, seed):
    seed("Biology")
    assert post(client, 999, 1, 5).status_code == 404
    assert post(client, 99999999999999999999, 1, 5).status_code == 404


def test_get_and_post_are_the_only_methods(client, seed):
    deck_id = seed("Biology")
    assert client.put(quiz_url(deck_id)).status_code == 405
    assert client.delete(quiz_url(deck_id)).status_code == 405


# --- ?prev= feedback ---


@pytest.mark.parametrize("prev", ["abc", "0", "-3", "999", "99999999999999999999", "1.5", ""])
def test_invalid_prev_is_ignored_silently(client, seed, prev):
    deck_id = seed("Biology")
    response = client.get(f"{quiz_url(deck_id)}?prev={prev}")
    assert response.status_code == 200
    assert "Previous card" not in text(response)


def test_prev_of_another_deck_is_ignored(client, seed):
    biology = seed("Biology", questions=("Bio?",))
    physics = seed("Physics", questions=("Phys?", "Phys2?"))
    post(client, physics, 2, 5)
    body = text(client.get(f"{quiz_url(biology)}?prev=2"))
    assert "Previous card" not in body


def test_prev_of_a_card_that_is_still_due_is_ignored(client, seed):
    deck_id = seed("Biology", questions=("A?", "B?"))
    assert "Previous card" not in text(client.get(f"{quiz_url(deck_id)}?prev=1"))


# --- done for today ---


def test_last_card_leads_to_the_done_page(client, seed):
    deck_id = seed("Biology", questions=("A?",))
    response = post(client, deck_id, 1, 4)
    body = text(client.get(response.headers["Location"]))
    assert "Done for today" in body
    assert "<details>" not in body
    assert "Reviewed today" in body and "1" in body
    assert "100% (1/1)" in body
    assert TOMORROW.isoformat() in body  # next due date
    assert f"Previous card: next review on {TOMORROW.isoformat()} (in 1 day)" in body
    assert f'href="/decks/{deck_id}/stats"' in body
    assert 'href="/"' in body


def test_done_page_without_prev_shows_next_due_and_accuracy(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?"), today=WEB_TODAY + timedelta(days=3))
    response = client.get(quiz_url(deck_id))
    body = text(response)
    assert response.status_code == 200
    assert "Done for today" in body
    assert (WEB_TODAY + timedelta(days=3)).isoformat() in body
    assert "Previous card" not in body
    assert "\N{EM DASH}" in body  # no reviews today: no accuracy


def test_done_page_of_an_empty_deck(client, seed):
    deck_id = seed("Empty", questions=())
    response = client.get(quiz_url(deck_id))
    assert response.status_code == 200
    assert "Done for today" in text(response)


def test_quiz_pages_for_unknown_decks_give_404(client, seed):
    seed("Biology")
    assert client.get(quiz_url(999)).status_code == 404


# --- end to end ---


def test_grading_three_cards_shows_the_accuracy_on_the_stats_page(client, seed):
    biology = seed("Biology", questions=("A?", "B?", "C?"))
    physics = seed("Physics", questions=("D?",))
    for card_id, grade in ((1, 5), (2, 2), (3, 4)):
        response = post(client, biology, card_id, grade)
        assert response.status_code == 303
        page = client.get(response.headers["Location"])
        assert page.status_code == 200
    assert "Done for today" in text(client.get(quiz_url(biology)))
    stats = text(client.get(f"/decks/{biology}/stats"))
    assert "67% (2/3)" in stats
    other = text(client.get(f"/decks/{physics}/stats"))
    assert "67%" not in other
    assert "Accuracy today</th><td>\N{EM DASH}" in other
    assert "1 card due" in text(client.get(quiz_url(physics)))
