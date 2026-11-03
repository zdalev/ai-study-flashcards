import re
import sqlite3
import threading
from datetime import timedelta

import pytest

from conftest import WEB_TODAY
from flashcards.storage import CardStore, open_db

HOSTILE = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
TOMORROW = WEB_TODAY + timedelta(days=1)


def text(response):
    return response.get_data(as_text=True)


def quiz_url(deck_id):
    return f"/decks/{deck_id}/quiz"


def post(client, deck_id, card_id, grade, **extra):
    data = {"card_id": card_id, "grade": grade, **extra}
    return client.post(quiz_url(deck_id), data=data)


def answer_step(client, deck_id, card_id, given_answer="", **extra):
    """The first POST of the write flow: send the typed answer, get the reveal page."""
    data = {"card_id": card_id, "step": "answer", "given_answer": given_answer, **extra}
    return client.post(quiz_url(deck_id), data=data)


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


def review_rows(db_path):
    """(card_id, reviewed_on, grade, mode, given_answer) of every review."""
    return snapshot(db_path)[1]


def review_count(db_path):
    return len(snapshot(db_path)[1])


def set_due(db_path, card_id, due_date):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE cards SET due_date = ? WHERE id = ?", (due_date.isoformat(), card_id))
    conn.close()


# --- GET: the question page ---


def test_get_shows_the_question_and_an_answer_box_but_not_the_answer(client, seed):
    deck_id = seed("Biology", questions=("What is DNA?",))
    response = client.get(quiz_url(deck_id))
    body = text(response)
    assert response.status_code == 200
    assert "What is DNA?" in body
    assert "Answer to What is DNA?" not in body
    assert re.search(r'<textarea[^>]*name="given_answer"[^>]*maxlength="2000"', body)
    assert "Save answer" in body
    assert "<details" not in body and 'name="grade"' not in body
    assert body.count('method="post"') == 1
    assert re.search(r'name="card_id" value="1"', body)
    assert re.search(r'name="step" value="answer"', body)
    assert re.search(r'name="mode" value="write"', body)
    assert "<script" not in body


@pytest.mark.parametrize("query", ["", "?mode=write", "?mode=nonsense", "?mode=", "?mode=choice"])
def test_get_defaults_to_write_mode_for_a_missing_or_unknown_mode(client, seed, query):
    deck_id = seed("Biology", questions=("What is DNA?",))
    body = text(client.get(quiz_url(deck_id) + query))
    assert re.search(r'name="mode" value="write"', body)
    assert "Save answer" in body


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


def test_get_escapes_a_hostile_question(client, seed):
    deck_id = seed("Biology", questions=(HOSTILE,))
    body = text(client.get(quiz_url(deck_id)))
    assert HOSTILE not in body
    assert body.count(ESCAPED) == 1


def test_get_does_not_change_the_database(client, seed, db_path):
    deck_id = seed("Biology")
    before = snapshot(db_path)
    client.get(quiz_url(deck_id))
    assert snapshot(db_path) == before


# --- POST: the answer step ---


def test_answer_step_shows_both_answers_and_the_three_grade_buttons(client, seed):
    deck_id = seed("Biology", questions=("What is DNA?",))
    response = answer_step(client, deck_id, 1, "A molecule")
    body = text(response)
    assert response.status_code == 200  # no redirect: nothing is saved yet
    assert "What is DNA?" in body
    assert body.index("Your answer") < body.index("A molecule") < body.index("Correct answer")
    assert body.index("Correct answer") < body.index("Answer to What is DNA?")
    assert "How did you do?" in body
    buttons = re.findall(r'<button[^>]*name="grade"[^>]*value="(\d)"[^>]*>([^<]*)</button>', body)
    assert buttons == [("0", "No idea"), ("1", "Wrong"), ("4", "Correct")]
    for old_label in ("Blackout", "Wrong but familiar", "Hard", "Good", "Perfect"):
        assert old_label not in body
    assert body.count('method="post"') == 1
    assert re.search(r'name="card_id" value="1"', body)
    assert re.search(r'name="given_answer" value="A molecule"', body)
    assert re.search(r'name="mode" value="write"', body)
    assert "<script" not in body


def test_answer_step_saves_nothing_and_a_refresh_is_harmless(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?"))
    before = snapshot(db_path)
    for _ in range(2):
        assert answer_step(client, deck_id, 1, "my answer").status_code == 200
    assert snapshot(db_path) == before


def test_answer_step_with_an_empty_answer_shows_no_answer(client, seed):
    deck_id = seed("Biology", questions=("A?",))
    for given in ("", "  \r\n "):
        body = text(answer_step(client, deck_id, 1, given))
        assert "(no answer)" in body
        assert 'name="given_answer" value=""' in body
    assert "(no answer)" not in text(answer_step(client, deck_id, 1, "something"))


def test_answer_step_without_a_given_answer_field_is_allowed(client, seed):
    deck_id = seed("Biology", questions=("A?",))
    response = client.post(quiz_url(deck_id), data={"card_id": 1, "step": "answer"})
    assert response.status_code == 200
    assert "(no answer)" in text(response)


def test_answer_step_escapes_the_typed_answer_in_text_and_hidden_field(client, seed):
    deck_id = seed("Biology", questions=("A?",))
    given = f'{HOSTILE}" onfocus="x'
    body = text(answer_step(client, deck_id, 1, given))
    assert HOSTILE not in body and 'onfocus="x' not in body
    assert body.count(ESCAPED) == 2  # shown, and carried in the hidden field


def test_answer_step_normalises_crlf_and_trims(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?",))
    body = text(answer_step(client, deck_id, 1, "  line one\r\nline two\rline three \r\n"))
    assert "line one\nline two\nline three" in body
    assert "\r" not in body
    post(client, deck_id, 1, 4, given_answer="x\r\ny\rz")
    assert review_rows(db_path)[0][4] == "x\ny\nz"


def test_crlf_counts_as_one_character_for_the_answer_limit(client, seed):
    deck_id = seed("Biology", questions=("A?",))
    fits = "a\r\n" * 1000  # the browser counts 2000 characters, posts 3000
    assert answer_step(client, deck_id, 1, fits).status_code == 200
    assert answer_step(client, deck_id, 1, "a" * 2000).status_code == 200
    assert answer_step(client, deck_id, 1, "a" * 2001).status_code == 400
    assert answer_step(client, deck_id, 1, "a\r\n" * 1001).status_code == 400


def test_answer_step_keeps_the_mode_from_the_form(client, seed):
    deck_id = seed("Biology", questions=("A?",))
    assert re.search(r'name="mode" value="write"', text(answer_step(client, deck_id, 1, mode="x")))


@pytest.mark.parametrize(
    "data",
    [
        {"step": "answer"},
        {"step": "answer", "card_id": "x"},
        {"step": "answer", "card_id": "0"},
        {"step": "answer", "card_id": "9223372036854775808"},
    ],
)
def test_answer_step_with_a_bad_card_id_gives_400(client, seed, db_path, data):
    deck_id = seed("Biology")
    before = snapshot(db_path)
    assert client.post(quiz_url(deck_id), data=data).status_code == 400
    assert snapshot(db_path) == before


def test_answer_step_for_an_unknown_or_foreign_card_gives_404(client, seed):
    biology = seed("Biology", questions=("Bio?",))
    seed("Physics", questions=("Phys?",))
    assert answer_step(client, biology, 999).status_code == 404
    assert answer_step(client, biology, 2).status_code == 404  # card 2 belongs to physics
    assert answer_step(client, 999, 1).status_code == 404


def test_answer_step_for_a_card_that_is_not_due_goes_back_to_the_quiz(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?",))
    set_due(db_path, 1, WEB_TODAY + timedelta(days=5))
    response = answer_step(client, deck_id, 1, "x")
    assert response.status_code == 303
    assert response.headers["Location"].endswith(f"{quiz_url(deck_id)}?mode=write")


# --- POST: grading ---


def test_post_correct_redirects_with_303_and_updates_the_card(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?"))
    response = post(client, deck_id, 1, 4)
    assert response.status_code == 303
    assert response.headers["Location"].endswith(f"{quiz_url(deck_id)}?mode=write")
    state = card_row(db_path, 1).state
    assert (state.repetitions, state.interval_days) == (1, 1)
    assert state.due_date == TOMORROW
    assert state.ease_factor == 2.5  # grade 4 keeps the ease factor
    assert state.last_reviewed == WEB_TODAY
    assert review_rows(db_path) == [(1, WEB_TODAY.isoformat(), 4, "write", None)]


def test_post_wrong_resets_repetitions_and_lowers_ease(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?",))
    assert post(client, deck_id, 1, 1).status_code == 303
    state = card_row(db_path, 1).state
    assert (state.repetitions, state.interval_days) == (0, 1)
    assert state.due_date == TOMORROW
    assert state.ease_factor == 1.96
    assert review_rows(db_path) == [(1, WEB_TODAY.isoformat(), 1, "write", None)]


def test_post_no_idea_is_grade_0(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?",))
    assert post(client, deck_id, 1, 0).status_code == 303
    assert card_row(db_path, 1).state.ease_factor == 1.7
    assert review_rows(db_path)[0][2] == 0


def test_grading_saves_the_mode_and_the_given_answer(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?"))
    post(client, deck_id, 1, 4, given_answer="My answer", mode="write")
    post(client, deck_id, 2, 0, given_answer="", mode="write")
    assert review_rows(db_path) == [
        (1, WEB_TODAY.isoformat(), 4, "write", "My answer"),
        (2, WEB_TODAY.isoformat(), 0, "write", ""),
    ]


def test_grading_an_unknown_mode_is_saved_as_write(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?",))
    response = post(client, deck_id, 1, 4, mode="bogus", given_answer="x")
    assert response.headers["Location"].endswith("?mode=write")
    assert review_rows(db_path)[0][3] == "write"


def test_grading_with_a_too_long_given_answer_gives_400_and_saves_nothing(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?",))
    before = snapshot(db_path)
    assert post(client, deck_id, 1, 4, given_answer="a" * 2001).status_code == 400
    assert snapshot(db_path) == before


def test_full_write_flow_reveal_then_grade_then_next_card(client, seed, db_path):
    deck_id = seed("Biology", questions=("First?", "Second?"))
    assert "First?" in text(client.get(quiz_url(deck_id)))
    reveal = text(answer_step(client, deck_id, 1, "my guess"))
    assert "my guess" in reveal and "Answer to First?" in reveal
    response = post(client, deck_id, 1, 4, given_answer="my guess", mode="write")
    assert response.status_code == 303
    follow = client.get(response.headers["Location"])
    body = text(follow)
    assert follow.status_code == 200
    assert "Second?" in body and "First?" not in body
    assert "1 card due" in body
    assert "Previous card" not in body
    assert review_rows(db_path) == [(1, WEB_TODAY.isoformat(), 4, "write", "my guess")]


@pytest.mark.parametrize("grade", range(6))
def test_every_grade_from_0_to_5_is_still_accepted(client, seed, grade):
    deck_id = seed("Biology", questions=("A?",))
    assert post(client, deck_id, 1, grade).status_code == 303


def test_no_previous_card_feedback_anywhere(client, seed):
    deck_id = seed("Biology", questions=("First?", "Second?"))
    pages = [
        client.get(quiz_url(deck_id)),
        client.get(f"{quiz_url(deck_id)}?prev=1"),
        answer_step(client, deck_id, 1, "x"),
        client.get(post(client, deck_id, 1, 4).headers["Location"]),
        client.get(post(client, deck_id, 2, 4).headers["Location"]),  # the done page
        client.get(f"{quiz_url(deck_id)}?prev=1"),
    ]
    for page in pages:
        assert "Previous card" not in text(page)
        assert "next review on" not in text(page)


def test_double_submit_is_ignored(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?"))
    post(client, deck_id, 1, 4)
    before = snapshot(db_path)
    response = post(client, deck_id, 1, 0)
    assert response.status_code == 303
    assert response.headers["Location"].endswith("?mode=write")
    assert snapshot(db_path) == before
    assert len(before[1]) == 1


def test_parallel_submits_of_the_same_card_log_one_review(app, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?"))
    threads = 8
    start = threading.Barrier(threads)
    statuses = []

    def submit():
        client = app.test_client()
        start.wait(timeout=10)
        statuses.append(post(client, deck_id, 1, 4, given_answer="x").status_code)

    workers = [threading.Thread(target=submit) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert statuses == [303] * threads
    _cards, reviews = snapshot(db_path)
    assert reviews == [(1, WEB_TODAY.isoformat(), 4, "write", "x")]
    assert card_row(db_path, 1).state.repetitions == 1


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


# --- done for today ---


def test_last_card_leads_to_the_done_page(client, seed):
    deck_id = seed("Biology", questions=("A?",))
    response = post(client, deck_id, 1, 4)
    body = text(client.get(response.headers["Location"]))
    assert "Done for today" in body
    assert "<textarea" not in body
    assert "Reviewed today" in body and "1" in body
    assert "100% (1/1)" in body
    assert TOMORROW.isoformat() in body  # next due date
    assert "Previous card" not in body
    assert f'href="/decks/{deck_id}/stats"' in body
    assert 'href="/"' in body


def test_done_page_without_reviews_shows_next_due_and_no_accuracy(client, seed, db_path):
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
    for card_id, grade in ((1, 4), (2, 1), (3, 4)):
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
