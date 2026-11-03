"""Multiple choice mode of the quiz, and the 'Recent answers' table on the stats page."""

import html
import re
import sqlite3
import threading
from datetime import timedelta

import pytest

from conftest import WEB_NOW, WEB_TODAY
from flashcards import scheduler
from flashcards.models import CardDraft
from flashcards.storage import CardStore, open_db
from flashcards.web import create_app

HOSTILE = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
TOMORROW = WEB_TODAY + timedelta(days=1)
OPTION = re.compile(r'<button[^>]*name="choice"[^>]*value="([^"]*)"[^>]*>([^<]*)</button>')
DONT_KNOW = re.compile(r'<button[^>]*name="dont_know"[^>]*value="([^"]*)"[^>]*>([^<]*)</button>')


def text(response):
    return response.get_data(as_text=True)


def quiz_url(deck_id, mode="choice"):
    return f"/decks/{deck_id}/quiz?mode={mode}"


def options_of(response):
    """[(posted value, option text)] of a choice page; the "Do not know" button is not included."""
    return [
        (html.unescape(value), html.unescape(label))
        for value, label in OPTION.findall(text(response))
    ]


def dont_know_button(response):
    """[(posted value, label)] of the "Do not know" button (one entry on a choice page)."""
    return DONT_KNOW.findall(text(response))


def real_options(response):
    return [label for _value, label in options_of(response)]


def choose(client, deck_id, card_id, choice, **extra):
    data = {"card_id": card_id, "mode": "choice", "choice": choice, **extra}
    return client.post(f"/decks/{deck_id}/quiz", data=data)


def dont_know(client, deck_id, card_id, **extra):
    """Click "Do not know"."""
    data = {"card_id": card_id, "mode": "choice", "dont_know": "1", **extra}
    return client.post(f"/decks/{deck_id}/quiz", data=data)


def value_of(response, answer):
    """The posted value of the option showing `answer` (the option text itself)."""
    return next(value for value, label in options_of(response) if label == answer)


def snapshot(db_path):
    conn = sqlite3.connect(db_path)
    try:
        cards = conn.execute("SELECT * FROM cards ORDER BY id").fetchall()
        reviews = conn.execute("SELECT * FROM reviews ORDER BY rowid").fetchall()
    finally:
        conn.close()
    return cards, reviews


def reviews_of(db_path):
    return snapshot(db_path)[1]


def card_state(db_path, card_id):
    with CardStore(open_db(db_path)) as store:
        return store.get_card(card_id).state


def add_cards(db_path, deck_name, pairs):
    """Add (question, answer) cards to a deck (created if missing), due today; returns its id."""
    with CardStore(open_db(db_path)) as store:
        deck = store.get_or_create_deck(deck_name, None, WEB_NOW)
        drafts = [CardDraft(question, answer) for question, answer in pairs]
        store.add_cards(deck.id, drafts, WEB_TODAY, WEB_NOW)
        return deck.id


def only_due(db_path, card_id, day=WEB_TODAY):
    """Make `card_id` the only card due today; every other card is due far in the future."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "UPDATE cards SET due_date = CASE WHEN id = ? THEN ? ELSE '2099-01-01' END",
            (card_id, day.isoformat()),
        )
    conn.close()


def log_review(db_path, card_id, grade, mode=None, given=None, day=WEB_TODAY):
    """Log a review through the store, as the app does (the card must be due on `day`)."""
    with CardStore(open_db(db_path)) as store:
        card = store.get_card(card_id)
        assert store.save_review(
            card_id, scheduler.review(card.state, grade, day), grade, mode=mode, given_answer=given
        )


def deck_of_five(db_path, name="Biology"):
    """Five cards: Q1..Q5 with answers A1..A5."""
    return add_cards(db_path, name, [(f"Q{n}?", f"A{n}") for n in range(1, 6)])


def history_cells(body):
    """The text cells of every 'Recent answers' body row, unescaped."""
    section = body.split("Recent answers", 1)[1]
    rows = re.findall(r"<tr>\s*(<td>.*?)</tr>", section, flags=re.S)
    return [
        [html.unescape(cell) for cell in re.findall(r"<td>(.*?)</td>", row, re.S)] for row in rows
    ]


# --- the question page ---


def test_choice_page_shows_the_question_four_options_and_do_not_know(client, db_path):
    deck_id = deck_of_five(db_path)
    response = client.get(quiz_url(deck_id))
    body = text(response)
    assert response.status_code == 200
    assert "Q1?" in body
    options = options_of(response)
    assert len(options) == 4 and all(value == label for value, label in options)
    assert dont_know_button(response) == [("1", "Do not know")]
    assert re.search(r'name="card_id" value="1"', body)
    assert re.search(r'name="mode" value="choice"', body)
    assert "<textarea" not in body and "Save answer" not in body
    assert body.count('method="post"') == 1
    assert "<script" not in body


def test_options_hold_the_correct_answer_once_and_three_distinct_wrong_ones(client, db_path):
    deck_id = deck_of_five(db_path)
    labels = real_options(client.get(quiz_url(deck_id)))  # card 1 is the first due card
    assert len(labels) == 4 and len(set(labels)) == 4
    assert labels.count("A1") == 1
    assert set(labels) - {"A1"} <= {"A2", "A3", "A4", "A5"}


def test_wrong_options_come_only_from_the_same_deck(client, db_path):
    biology = add_cards(db_path, "Biology", [(f"B{n}?", f"bio answer {n}") for n in range(1, 5)])
    add_cards(db_path, "Physics", [(f"P{n}?", f"physics answer {n}") for n in range(1, 30)])
    for _ in range(3):  # the options are the same on every load; check the page and later cards
        labels = real_options(client.get(quiz_url(biology)))
        assert sorted(labels) == [f"bio answer {n}" for n in range(1, 5)]


def test_wrong_options_are_distinct_and_never_equal_the_correct_answer(client, db_path):
    # Duplicates and case/spacing variants of the same text must not become extra options.
    answers = ["Paris", "paris", "  PARIS ", "Berlin", "berlin", "Rome", "Madrid", "Madrid"]
    deck_id = add_cards(db_path, "Capitals", [(f"Q{n}?", a) for n, a in enumerate(answers, 1)])
    for card_id in range(1, len(answers) + 1):
        correct = answers[card_id - 1].strip()  # answers are stored trimmed
        only_due(db_path, card_id)
        labels = real_options(client.get(quiz_url(deck_id)))
        keys = [" ".join(label.split()).casefold() for label in labels]
        assert len(labels) == 4 and len(set(keys)) == 4, (card_id, labels)
        assert labels.count(correct) == 1
        assert keys.count(" ".join(correct.split()).casefold()) == 1


def test_option_order_is_stable_on_refresh_and_across_app_instances(client, db_path):
    deck_id = deck_of_five(db_path)
    first = options_of(client.get(quiz_url(deck_id)))
    assert options_of(client.get(quiz_url(deck_id))) == first
    other_app = create_app({"TESTING": True, "DATABASE": str(db_path), "TODAY": WEB_TODAY})
    assert options_of(other_app.test_client().get(quiz_url(deck_id))) == first


def test_options_are_shuffled_not_always_in_deck_order(client, db_path):
    # Over many cards the correct answer must not always sit in the same place.
    deck_id = add_cards(db_path, "Big", [(f"Q{n}?", f"A{n}") for n in range(1, 41)])
    positions = set()
    for card_id in range(1, 41):
        only_due(db_path, card_id)
        positions.add(real_options(client.get(quiz_url(deck_id))).index(f"A{card_id}"))
    assert positions == {0, 1, 2, 3}


def test_choice_page_shows_the_due_count_and_changes_nothing(client, db_path):
    deck_id = deck_of_five(db_path)
    before = snapshot(db_path)
    assert "5 cards due" in text(client.get(quiz_url(deck_id)))
    assert snapshot(db_path) == before


def test_choice_page_escapes_hostile_options_and_question(client, db_path):
    deck_id = add_cards(
        db_path, "Evil", [(f"{HOSTILE} {n}", f'{HOSTILE}" onfocus="x{n}') for n in range(1, 6)]
    )
    body = text(client.get(quiz_url(deck_id)))
    assert HOSTILE not in body and 'onfocus="x' not in body
    assert body.count(ESCAPED) >= 5  # the question and the four options


# --- fewer than four distinct answers ---


def test_a_deck_with_fewer_than_four_distinct_answers_falls_back_to_write_mode(client, db_path):
    deck_id = add_cards(db_path, "Small", [("Q1?", "a"), ("Q2?", "b"), ("Q3?", "c"), ("Q4?", "C")])
    response = client.get(quiz_url(deck_id))
    body = text(response)
    assert response.status_code == 200
    assert "Q1?" in body
    assert re.search(r"<textarea[^>]*name=\"given_answer\"", body)
    assert re.search(r'name="mode" value="write"', body)
    assert 'name="choice"' not in body
    assert "Multiple choice needs at least 4 different answers" in body


def test_exactly_four_distinct_answers_is_enough(client, db_path):
    deck_id = add_cards(db_path, "Four", [(f"Q{n}?", f"a{n}") for n in range(1, 5)])
    body = text(client.get(quiz_url(deck_id)))
    assert sorted(real_options(client.get(quiz_url(deck_id)))) == ["a1", "a2", "a3", "a4"]
    assert "Multiple choice needs" not in body


def test_the_write_page_has_no_fallback_note(client, db_path):
    deck_id = add_cards(db_path, "Small", [("Q1?", "a")])
    assert "Multiple choice needs" not in text(client.get(quiz_url(deck_id, "write")))


def test_a_choice_posted_to_a_deck_that_has_become_small_is_graded_on_the_text_it_showed(
    client, db_path
):
    deck_id = add_cards(db_path, "Small", [("Q1?", "a"), ("Q2?", "b")])
    assert choose(client, deck_id, 1, "b").status_code == 200  # a page shown earlier, when bigger
    assert reviews_of(db_path) == [(1, WEB_TODAY.isoformat(), 1, "choice", "b")]


# --- answering: grades and SM-2 state ---


def test_the_right_option_saves_grade_4_and_the_sm2_state(client, db_path):
    deck_id = deck_of_five(db_path)
    page = client.get(quiz_url(deck_id))
    response = choose(client, deck_id, 1, value_of(page, "A1"))
    assert response.status_code == 200
    state = card_state(db_path, 1)
    assert (state.repetitions, state.interval_days) == (1, 1)
    assert state.due_date == TOMORROW and state.ease_factor == 2.5
    assert state.last_reviewed == WEB_TODAY
    assert reviews_of(db_path) == [(1, WEB_TODAY.isoformat(), 4, "choice", "A1")]


def test_a_wrong_option_saves_grade_1_and_the_chosen_text(client, db_path):
    deck_id = deck_of_five(db_path)
    page = client.get(quiz_url(deck_id))
    wrong_value, wrong_text = next(
        (v, t) for v, t in options_of(page) if t not in ("A1", "Do not know")
    )
    assert choose(client, deck_id, 1, wrong_value).status_code == 200
    state = card_state(db_path, 1)
    assert (state.repetitions, state.interval_days) == (0, 1)
    assert state.due_date == TOMORROW and state.ease_factor == 1.96
    assert reviews_of(db_path) == [(1, WEB_TODAY.isoformat(), 1, "choice", wrong_text)]


def test_do_not_know_saves_grade_0_and_an_empty_answer(client, db_path):
    deck_id = deck_of_five(db_path)
    assert dont_know(client, deck_id, 1).status_code == 200
    state = card_state(db_path, 1)
    assert (state.repetitions, state.interval_days) == (0, 1)
    assert state.due_date == TOMORROW and state.ease_factor == 1.7
    assert reviews_of(db_path) == [(1, WEB_TODAY.isoformat(), 0, "choice", "")]


def test_the_server_ignores_a_given_answer_and_grade_sent_with_a_choice(client, db_path):
    deck_id = deck_of_five(db_path)
    page = client.get(quiz_url(deck_id))
    wrong_value, wrong_text = next(
        (v, t) for v, t in options_of(page) if t not in ("A1", "Do not know")
    )
    choose(client, deck_id, 1, wrong_value, grade="5", given_answer="A1")
    assert reviews_of(db_path) == [(1, WEB_TODAY.isoformat(), 1, "choice", wrong_text)]


def test_right_options_keep_the_ease_and_lengthen_the_interval(client, db_path):
    deck_id = add_cards(db_path, "Biology", [(f"Q{n}?", f"A{n}") for n in range(1, 5)])
    for day, interval in ((WEB_TODAY, 1), (WEB_TODAY + timedelta(days=1), 6)):
        app = create_app({"TESTING": True, "DATABASE": str(db_path), "TODAY": day})
        only_due(db_path, 1, day)
        page = app.test_client().get(quiz_url(deck_id))
        assert choose_on(app, deck_id, 1, value_of(page, "A1")).status_code == 200
        state = card_state(db_path, 1)
        assert state.interval_days == interval and state.ease_factor == 2.5


def choose_on(app, deck_id, card_id, choice):
    return choose(app.test_client(), deck_id, card_id, choice)


# --- the result page ---


def test_the_result_page_shows_choice_correct_answer_result_and_next_link(client, db_path):
    deck_id = deck_of_five(db_path)
    page = client.get(quiz_url(deck_id))
    body = text(choose(client, deck_id, 1, value_of(page, "A1")))
    assert "Q1?" in body
    assert body.index("Your choice") < body.index("A1") < body.index("Correct answer")
    assert re.search(r">\s*Correct\s*</p>", body)
    assert f'href="/decks/{deck_id}/quiz?mode=choice">Next card</a>' in body
    assert 'name="choice"' not in body and "<form" not in body


def test_the_result_page_for_a_wrong_option_and_for_do_not_know(client, db_path):
    deck_id = deck_of_five(db_path)
    page = client.get(quiz_url(deck_id))
    wrong_value, wrong_text = next(
        (v, t) for v, t in options_of(page) if t not in ("A1", "Do not know")
    )
    wrong = text(choose(client, deck_id, 1, wrong_value))
    assert re.search(r">\s*Wrong\s*</p>", wrong)
    assert wrong_text in wrong and "A1" in wrong
    idk = text(dont_know(client, deck_id, 2))
    assert re.search(r">\s*Do not know\s*</p>", idk)
    assert "(no answer)" in idk and "A2" in idk


def test_the_result_page_escapes_everything(client, db_path):
    deck_id = add_cards(
        db_path, "Evil", [(f"{HOSTILE} {n}", f"{HOSTILE} answer {n}") for n in range(1, 6)]
    )
    page = client.get(quiz_url(deck_id))
    correct = f"{HOSTILE} answer 1"
    body = text(choose(client, deck_id, 1, value_of(page, correct)))
    assert HOSTILE not in body
    assert body.count(ESCAPED) == 3  # question, your choice, correct answer


def test_next_card_link_leads_to_the_next_card_and_then_the_done_page(client, db_path):
    deck_id = add_cards(db_path, "Four", [(f"Q{n}?", f"A{n}") for n in range(1, 5)])
    asked = []
    for _ in range(4):
        page = client.get(quiz_url(deck_id))
        question = re.search(r'class="question">([^<]*)<', text(page)).group(1)
        asked.append(question)
        card_id = int(question[1:-1])
        body = text(choose(client, deck_id, card_id, value_of(page, f"A{card_id}")))
        assert f"/decks/{deck_id}/quiz?mode=choice" in body
    assert sorted(asked) == ["Q1?", "Q2?", "Q3?", "Q4?"]
    done = text(client.get(quiz_url(deck_id)))
    assert "Done for today" in done and "100% (4/4)" in done


# --- refresh, double submit, races ---


def test_a_refresh_of_the_result_page_is_ignored(client, db_path):
    deck_id = deck_of_five(db_path)
    page = client.get(quiz_url(deck_id))
    choose(client, deck_id, 1, value_of(page, "A1"))
    before = snapshot(db_path)
    again = dont_know(client, deck_id, 1)
    assert again.status_code == 303
    assert again.headers["Location"].endswith(f"/decks/{deck_id}/quiz?mode=choice")
    assert snapshot(db_path) == before
    assert len(before[1]) == 1


def test_a_card_that_is_not_due_is_not_reviewed(client, db_path):
    deck_id = deck_of_five(db_path)
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE cards SET due_date = '2099-01-01' WHERE id = 1")
    conn.close()
    before = snapshot(db_path)
    assert choose(client, deck_id, 1, "A1").status_code == 303
    assert snapshot(db_path) == before


def test_parallel_submits_of_the_same_choice_log_one_review(app, db_path):
    deck_id = deck_of_five(db_path)
    threads = 8
    start = threading.Barrier(threads)
    statuses = []

    def submit():
        client = app.test_client()
        start.wait(timeout=10)
        statuses.append(dont_know(client, deck_id, 1).status_code)

    workers = [threading.Thread(target=submit) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert sorted(statuses) == [200] + [303] * (threads - 1)
    assert reviews_of(db_path) == [(1, WEB_TODAY.isoformat(), 0, "choice", "")]


# --- tampering and bad input ---


@pytest.mark.parametrize(
    "choice",
    # Index-style values (the old form), text that is not an answer of the subject, and the
    # old "idk" value: only the text of an answer of the same subject is accepted.
    [
        "0",
        "1",
        "3",
        "4",
        "-1",
        "x",
        "1.5",
        "",
        " ",
        "idk",
        "IDK",
        "١",
        "99999999999999999999",
        "A",
        "A11",
        "A1A2",
        "<script>alert(1)</script>",
        "A6",
        "a" * 5000,
    ],  # fmt: skip
)
def test_a_tampered_choice_gives_400_and_saves_nothing(client, db_path, choice):
    deck_id = deck_of_five(db_path)
    before = snapshot(db_path)
    response = choose(client, deck_id, 1, choice)
    assert response.status_code == 400
    assert "Bad request" in text(response)
    assert snapshot(db_path) == before


def test_a_missing_choice_gives_400(client, db_path):
    deck_id = deck_of_five(db_path)
    before = snapshot(db_path)
    response = client.post(f"/decks/{deck_id}/quiz", data={"card_id": 1, "mode": "choice"})
    assert response.status_code == 400
    assert snapshot(db_path) == before


def test_an_answer_of_another_subject_gives_400(client, db_path):
    biology = deck_of_five(db_path)
    add_cards(db_path, "Physics", [("P?", "Only in physics")])
    before = snapshot(db_path)
    assert choose(client, biology, 1, "Only in physics").status_code == 400
    assert snapshot(db_path) == before


def test_answers_that_differ_only_in_case_or_spacing_are_accepted(client, db_path):
    deck_id = deck_of_five(db_path)
    assert choose(client, deck_id, 1, "  a1 ").status_code == 200
    assert choose(client, deck_id, 2, "A  3").status_code == 400  # a different text
    assert choose(client, deck_id, 3, " a 4").status_code == 400
    assert choose(client, deck_id, 4, "a5").status_code == 200
    assert [row[2:] for row in reviews_of(db_path)] == [(4, "choice", "A1"), (1, "choice", "A5")]


def test_a_choice_and_do_not_know_together_count_as_do_not_know(client, db_path):
    deck_id = deck_of_five(db_path)
    choose(client, deck_id, 1, "A1", dont_know="1")
    assert [row[2:] for row in reviews_of(db_path)] == [(0, "choice", "")]


def test_the_answer_step_in_choice_mode_gives_400_not_a_reveal(client, db_path):
    deck_id = deck_of_five(db_path)
    data = {"card_id": 1, "mode": "choice", "step": "answer", "given_answer": "A1"}
    assert client.post(f"/decks/{deck_id}/quiz", data=data).status_code == 400


@pytest.mark.parametrize("card_id", ["", "x", "0", "-1", "9223372036854775808"])
def test_a_bad_card_id_gives_400(client, db_path, card_id):
    deck_id = deck_of_five(db_path)
    before = snapshot(db_path)
    assert choose(client, deck_id, card_id, "A1").status_code == 400
    assert snapshot(db_path) == before


def test_unknown_and_foreign_cards_give_404(client, db_path):
    biology = deck_of_five(db_path)
    physics = add_cards(db_path, "Physics", [("P?", "p")])
    before = snapshot(db_path)
    assert choose(client, biology, 999, "A1").status_code == 404
    assert choose(client, biology, 6, "A1").status_code == 404  # card 6 belongs to physics
    assert choose(client, 999, 1, "A1").status_code == 404
    assert choose(client, physics, 1, "A1").status_code == 404
    assert snapshot(db_path) == before


# --- home and links ---


def test_home_has_both_study_buttons_for_every_subject(client, db_path):
    biology = deck_of_five(db_path)
    physics = add_cards(db_path, "Physics", [("P?", "p")])
    body = text(client.get("/"))
    for deck_id in (biology, physics):
        assert f'href="/decks/{deck_id}/quiz?mode=write">Study: Write</a>' in body
        assert f'href="/decks/{deck_id}/quiz?mode=choice">Study: Multiple choice</a>' in body
        assert f'href="/decks/{deck_id}/cards/new"' in body
        assert f'href="/decks/{deck_id}/stats"' in body
    assert body.count(">Study: Write<") == 2 and ">Study</a>" not in body


def test_the_stats_page_links_to_both_modes(client, db_path):
    deck_id = deck_of_five(db_path)
    body = text(client.get(f"/decks/{deck_id}/stats"))
    assert f'href="/decks/{deck_id}/quiz?mode=write">Study: Write</a>' in body
    assert f'href="/decks/{deck_id}/quiz?mode=choice">Study: Multiple choice</a>' in body


# --- recent answers on the stats page ---


def test_history_shows_both_modes_old_rows_and_every_result(client, db_path):
    deck_id = deck_of_five(db_path)
    log_review(db_path, 1, 4, mode="write", given="my text")
    log_review(db_path, 2, 1, mode="choice", given="A5")
    log_review(db_path, 3, 0, mode="choice", given="")
    log_review(db_path, 4, 3)  # an old row: no mode, no given answer
    log_review(db_path, 5, 2, mode="write", given="")
    body = text(client.get(f"/decks/{deck_id}/stats"))
    assert "Recent answers" in body
    for heading in ("Date", "Mode", "Question", "Your answer", "Correct answer", "Result"):
        assert f"<th>{heading}</th>" in body
    day = WEB_TODAY.isoformat()
    assert history_cells(body) == [  # newest first
        [day, "Write", "Q5?", "(no answer)", "A5", "Wrong"],
        [day, "-", "Q4?", "-", "A4", "Partially correct"],
        [day, "Multiple choice", "Q3?", "(no answer)", "A3", "Do not know"],
        [day, "Multiple choice", "Q2?", "A5", "A2", "Wrong"],
        [day, "Write", "Q1?", "my text", "A1", "Correct"],
    ]


@pytest.mark.parametrize(
    ("grade", "label"),
    [
        (0, "Do not know"),
        (1, "Wrong"),
        (2, "Wrong"),
        (3, "Partially correct"),
        (4, "Correct"),
        (5, "Correct"),
    ],
)
def test_history_maps_every_grade_to_a_result(client, db_path, grade, label):
    deck_id = deck_of_five(db_path)
    log_review(db_path, 1, grade)
    assert history_cells(text(client.get(f"/decks/{deck_id}/stats")))[0][5] == label


def test_history_is_newest_day_first_and_limited_to_20_rows(client, db_path):
    deck_id = add_cards(db_path, "Many", [(f"Q{n}?", f"A{n}") for n in range(1, 26)])
    for card_id in range(1, 26):
        log_review(db_path, card_id, 4, mode="write", given=f"g{card_id}")
    rows = history_cells(text(client.get(f"/decks/{deck_id}/stats")))
    assert len(rows) == 20
    assert [row[2] for row in rows] == [f"Q{n}?" for n in range(25, 5, -1)]


def test_history_orders_by_day_before_save_order(client, db_path):
    deck_id = deck_of_five(db_path)
    # Save the newer day first: day order still wins.
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "UPDATE cards SET due_date = ? WHERE id = 2",
            ((WEB_TODAY - timedelta(days=5)).isoformat(),),
        )
    conn.close()
    with CardStore(open_db(db_path)) as store:
        for card_id, day in ((1, WEB_TODAY), (2, WEB_TODAY - timedelta(days=3))):
            card = store.get_card(card_id)
            state = scheduler.review(card.state, 4, day)
            assert store.save_review(card_id, state, 4, mode="write", given_answer="x")
    rows = history_cells(text(client.get(f"/decks/{deck_id}/stats")))
    assert [row[2] for row in rows] == ["Q1?", "Q2?"]


def test_history_only_lists_the_subjects_own_answers(client, db_path):
    biology = deck_of_five(db_path)
    physics = add_cards(db_path, "Physics", [("Phys?", "p")])
    log_review(db_path, 1, 4, mode="write", given="x")
    log_review(db_path, 6, 4, mode="write", given="y")
    bio_rows = history_cells(text(client.get(f"/decks/{biology}/stats")))
    assert [row[2] for row in bio_rows] == ["Q1?"]
    assert [row[2] for row in history_cells(text(client.get(f"/decks/{physics}/stats")))] == [
        "Phys?"
    ]


def test_history_without_answers_shows_an_empty_state(client, db_path):
    deck_id = deck_of_five(db_path)
    body = text(client.get(f"/decks/{deck_id}/stats"))
    assert "Recent answers" in body and "No answers yet" in body
    assert '<table class="history"' not in body


def test_history_escapes_questions_and_both_answers(client, db_path):
    deck_id = add_cards(db_path, "Evil", [(f"{HOSTILE}?", f"{HOSTILE} right")])
    log_review(db_path, 1, 1, mode="write", given=f'{HOSTILE}" onfocus="x')
    body = text(client.get(f"/decks/{deck_id}/stats"))
    assert HOSTILE not in body and 'onfocus="x' not in body
    assert body.count(ESCAPED) >= 3


def test_all_stats_page_has_no_history_table(client, db_path):
    deck_id = deck_of_five(db_path)
    log_review(db_path, 1, 4, mode="write", given="x")
    assert "Recent answers" not in text(client.get("/stats"))
    assert "Recent answers" in text(client.get(f"/decks/{deck_id}/stats"))


def test_accuracy_counts_choice_reviews_with_grade_4_as_correct(client, db_path):
    deck_id = deck_of_five(db_path)
    page = client.get(quiz_url(deck_id))
    choose(client, deck_id, 1, value_of(page, "A1"))  # correct
    wrong_value = next(
        v for v, t in options_of(client.get(quiz_url(deck_id))) if t not in ("A2", "Do not know")
    )
    choose(client, deck_id, 2, wrong_value)  # wrong
    dont_know(client, deck_id, 3)  # do not know
    log_review(db_path, 4, 4, mode="write", given="ok")  # a write review counts too
    body = text(client.get(f"/decks/{deck_id}/stats"))
    assert "50% (2/4)" in body
    assert [row[1] for row in history_cells(body)] == [
        "Write",
        "Multiple choice",
        "Multiple choice",
        "Multiple choice",
    ]


def test_the_old_labels_appear_on_no_choice_page(client, db_path):
    deck_id = deck_of_five(db_path)
    pages = [
        text(client.get(quiz_url(deck_id))),
        text(dont_know(client, deck_id, 1)),
        text(choose(client, deck_id, 2, "A2")),
        text(client.get(f"/decks/{deck_id}/stats")),
    ]
    for page in pages:
        for old in ("No idea", "I don't know", "I don&#39;t know"):
            assert old not in page


def test_result_styles_give_partially_correct_its_own_class():
    from flashcards.web import result_class

    assert result_class(3) == "result-partial"
    assert {result_class(grade) for grade in (0, 1, 2)} == {"result-miss"}
    assert {result_class(grade) for grade in (4, 5)} == {"result-correct"}
