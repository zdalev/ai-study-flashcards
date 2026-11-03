import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest

from conftest import WEB_TODAY
from flashcards.storage import CardStore, open_db
from flashcards.web import MAX_ANSWER_LEN, MAX_DECK_NAME_LEN, MAX_QUESTION_LEN

HOSTILE = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
HUGE_ID = 99999999999999999999


def text(response):
    return response.get_data(as_text=True)


def card_url(deck_id):
    return f"/decks/{deck_id}/cards/new"


def add(client, deck_id, question="What is RNA?", answer="A nucleic acid."):
    return client.post(card_url(deck_id), data={"question": question, "answer": answer})


def rows(db_path, sql="SELECT * FROM cards ORDER BY id"):
    open_db(db_path).close()  # a request that failed early may not have created the schema
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def deck_rows(db_path):
    return rows(db_path, "SELECT * FROM decks ORDER BY id")


# --- GET: the add-card form ---


def test_get_shows_form_with_two_textareas_and_deck_name(client, seed):
    deck_id = seed("Biology")
    response = client.get(card_url(deck_id))
    body = text(response)
    assert response.status_code == 200
    assert "Add card: Biology" in body
    assert '<form class="form" method="post"' in body
    assert 'name="question"' in body and 'name="answer"' in body
    assert body.count("<textarea") == 2
    assert "Card added" not in body


def test_get_added_flag_shows_message_and_study_link(client, seed):
    deck_id = seed("Biology")
    body = text(client.get(card_url(deck_id) + "?added=1"))
    assert "Card added" in body
    assert f'href="/decks/{deck_id}/quiz"' in body


def test_get_added_other_value_shows_no_message(client, seed):
    deck_id = seed("Biology")
    assert "Card added" not in text(client.get(card_url(deck_id) + "?added=yes"))


@pytest.mark.parametrize("path", ["/decks/999/cards/new", f"/decks/{HUGE_ID}/cards/new"])
def test_unknown_or_huge_deck_id_is_404(client, path):
    assert client.get(path).status_code == 404
    response = client.post(path, data={"question": "Q?", "answer": "A"})
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/decks/abc/cards/new", "/decks/-1/cards/new"])
def test_non_integer_deck_id_is_404(client, path):
    assert client.get(path).status_code == 404


def test_other_methods_are_405(client, seed):
    deck_id = seed("Biology")
    assert client.put(card_url(deck_id)).status_code == 405
    assert client.delete("/decks/new").status_code == 405


# --- POST: adding a card ---


def test_post_saves_card_and_redirects_303_with_added_flag(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    response = add(client, deck_id)
    assert response.status_code == 303
    assert response.headers["Location"].endswith(f"{card_url(deck_id)}?added=1")
    cards = rows(db_path)
    assert len(cards) == 1
    assert cards[0][1:4] == (deck_id, "What is RNA?", "A nucleic acid.")


def test_following_the_redirect_shows_the_message(client, seed):
    deck_id = seed("Biology")
    response = client.post(
        card_url(deck_id), data={"question": "Q?", "answer": "A"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "Card added" in text(response)


def test_new_card_is_due_today_and_shows_in_quiz_and_counts(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    add(client, deck_id, "Unique question?", "Unique answer")
    with CardStore(open_db(db_path)) as store:
        (card,) = store.due_cards(WEB_TODAY, deck_id)
        assert card.state.due_date == WEB_TODAY
        assert card.state.interval_days == 0
    assert "Unique question?" in text(client.get(f"/decks/{deck_id}/quiz"))
    assert "1 card" in text(client.get("/"))
    stats = text(client.get(f"/decks/{deck_id}/stats"))
    assert '<th scope="row">Cards</th><td>1</td>' in stats
    assert '<th scope="row">Due today</th><td>1</td>' in stats
    assert '<th scope="row">New cards</th><td>1</td>' in stats


def test_card_is_due_on_the_configured_today(db_path, seed):
    from flashcards.web import create_app

    later = WEB_TODAY + timedelta(days=10)
    deck_id = seed("Biology", questions=())
    client = create_app({"DATABASE": str(db_path), "TODAY": later}).test_client()
    add(client, deck_id)
    assert rows(db_path)[0][8] == later.isoformat()


def test_text_is_normalised_by_storage(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    add(client, deck_id, "  What   is\n RNA?  ", "  Line one\nLine two  \n")
    (card,) = rows(db_path)
    assert card[2] == "What is RNA?"
    assert card[3] == "Line one\nLine two"


def test_multiline_answer_is_kept(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    add(client, deck_id, "List?", "- one\n- two")
    assert rows(db_path)[0][3] == "- one\n- two"


@pytest.mark.parametrize(
    ("question", "answer", "message"),
    [
        ("", "An answer", "Please enter a question."),
        ("   \n\t ", "An answer", "Please enter a question."),
        ("A question?", "", "Please enter an answer."),
        ("A question?", " \n ", "Please enter an answer."),
    ],
)
def test_blank_input_is_400_and_saves_nothing(client, seed, db_path, question, answer, message):
    deck_id = seed("Biology", questions=())
    response = add(client, deck_id, question, answer)
    assert response.status_code == 400
    assert message in text(response)
    assert rows(db_path) == []


def test_missing_fields_are_400(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    response = client.post(card_url(deck_id), data={})
    assert response.status_code == 400
    assert rows(db_path) == []


@pytest.mark.parametrize(
    ("question", "answer", "message"),
    [
        ("Q" * (MAX_QUESTION_LEN + 1), "A", "The question is too long"),
        ("Q?", "A" * (MAX_ANSWER_LEN + 1), "The answer is too long"),
    ],
)
def test_too_long_input_is_400_and_saves_nothing(client, seed, db_path, question, answer, message):
    deck_id = seed("Biology", questions=())
    response = add(client, deck_id, question, answer)
    assert response.status_code == 400
    assert message in text(response)
    assert rows(db_path) == []


def test_input_at_the_limits_is_saved(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    response = add(client, deck_id, "Q" * MAX_QUESTION_LEN, "A" * MAX_ANSWER_LEN)
    assert response.status_code == 303
    assert len(rows(db_path)) == 1


def test_400_reshows_the_users_input_escaped(client, seed):
    deck_id = seed("Biology", questions=())
    response = add(client, deck_id, f"{HOSTILE} question", "")
    body = text(response)
    assert response.status_code == 400
    assert f"{ESCAPED} question</textarea>" in body
    assert HOSTILE not in body


def test_400_keeps_both_fields_so_nothing_is_retyped(client, seed):
    deck_id = seed("Biology", questions=())
    body = text(add(client, deck_id, "", "My long answer"))
    assert ">My long answer</textarea>" in body


# --- duplicates ---


def test_duplicate_question_shows_message_and_saves_nothing(client, seed, db_path):
    deck_id = seed("Biology", questions=("What is DNA?",))
    before = rows(db_path)
    response = add(client, deck_id, "What is DNA?", "Something else")
    body = text(response)
    assert response.status_code == 409
    assert "This question already exists in Biology." in body
    assert rows(db_path) == before


def test_duplicate_ignores_case_and_spacing(client, seed, db_path):
    deck_id = seed("Biology", questions=("What is DNA?",))
    response = add(client, deck_id, "  what   IS dna?  ", "x")
    assert response.status_code == 409
    assert len(rows(db_path)) == 1


def test_duplicate_keeps_the_input_in_the_form(client, seed):
    deck_id = seed("Biology", questions=("What is DNA?",))
    body = text(add(client, deck_id, "What is DNA?", "My own answer"))
    assert ">My own answer</textarea>" in body


def test_same_question_in_another_deck_is_allowed(client, seed, db_path):
    seed("Biology", questions=("What is DNA?",))
    other = seed("Chemistry", questions=())
    assert add(client, other, "What is DNA?", "x").status_code == 303
    assert len(rows(db_path)) == 2


def test_adding_the_same_card_twice_gives_one_card(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    assert add(client, deck_id).status_code == 303
    assert add(client, deck_id).status_code == 409
    assert len(rows(db_path)) == 1


# --- escaping ---


def test_hostile_question_and_answer_are_escaped_everywhere(client, seed):
    deck_id = seed("Biology", questions=())
    add(client, deck_id, f"{HOSTILE} Q", f"{HOSTILE} A")
    quiz = text(client.get(f"/decks/{deck_id}/quiz"))
    reveal = text(client.post(f"/decks/{deck_id}/quiz", data={"card_id": 1, "step": "answer"}))
    for page in (quiz, reveal):
        assert HOSTILE not in page
        assert ESCAPED in page
    assert reveal.count(ESCAPED) == 2  # the question and the correct answer


def test_duplicate_message_escapes_a_hostile_deck_name(client, seed):
    deck_id = seed(HOSTILE, questions=("Q?",))
    body = text(add(client, deck_id, "Q?", "x"))
    assert HOSTILE not in body
    assert f"already exists in {ESCAPED}." in body


def test_form_page_escapes_a_hostile_deck_name(client, seed):
    deck_id = seed(HOSTILE)
    body = text(client.get(card_url(deck_id)))
    assert HOSTILE not in body
    assert ESCAPED in body


# --- the new-subject form ---


def test_get_new_subject_form(client):
    response = client.get("/decks/new")
    body = text(response)
    assert response.status_code == 200
    assert 'name="name"' in body
    assert "New flashcards" in body


def test_new_subject_is_created_and_redirects_to_its_card_form(client, db_path):
    response = client.post("/decks/new", data={"name": "History"})
    assert response.status_code == 303
    (deck,) = deck_rows(db_path)
    assert deck[1:3] == ("History", None)
    assert response.headers["Location"].endswith(card_url(deck[0]))


def test_new_subject_appears_on_home_with_zero_cards_and_add_card_link(client, db_path):
    client.post("/decks/new", data={"name": "History"})
    deck_id = deck_rows(db_path)[0][0]
    body = text(client.get("/"))
    assert "History" in body
    assert "0 cards" in body
    assert "0 due" in body
    assert f'href="{card_url(deck_id)}"' in body


def test_new_subject_name_is_normalised(client, db_path):
    client.post("/decks/new", data={"name": "  Ancient   History "})
    assert deck_rows(db_path)[0][1] == "Ancient History"


def test_duplicate_subject_name_reuses_the_deck(client, seed, db_path):
    deck_id = seed("Biology")
    response = client.post("/decks/new", data={"name": " biology "})
    assert response.status_code == 303
    assert response.headers["Location"].endswith(card_url(deck_id))
    assert len(deck_rows(db_path)) == 1
    assert len(rows(db_path)) == 3


@pytest.mark.parametrize("name", ["", "   ", "\n\t"])
def test_blank_subject_name_is_400(client, db_path, name):
    response = client.post("/decks/new", data={"name": name})
    assert response.status_code == 400
    assert "Please enter a name." in text(response)
    assert deck_rows(db_path) == []


def test_missing_subject_name_is_400(client, db_path):
    assert client.post("/decks/new", data={}).status_code == 400
    assert deck_rows(db_path) == []


def test_too_long_subject_name_is_400(client, db_path):
    response = client.post("/decks/new", data={"name": "N" * (MAX_DECK_NAME_LEN + 1)})
    assert response.status_code == 400
    assert "too long" in text(response)
    assert deck_rows(db_path) == []


def test_subject_name_at_the_limit_is_created(client, db_path):
    response = client.post("/decks/new", data={"name": "N" * MAX_DECK_NAME_LEN})
    assert response.status_code == 303
    assert len(deck_rows(db_path)) == 1


def test_hostile_subject_name_is_escaped_on_every_page(client, db_path):
    response = client.post("/decks/new", data={"name": HOSTILE})
    deck_id = deck_rows(db_path)[0][0]
    assert response.status_code == 303
    for url in ("/", card_url(deck_id), f"/decks/{deck_id}/stats", f"/decks/{deck_id}/quiz"):
        body = text(client.get(url))
        assert HOSTILE not in body, url
        assert ESCAPED in body, url


def test_hostile_blank_subject_form_reshows_input_escaped(client):
    body = text(client.post("/decks/new", data={"name": "x" * 101 + HOSTILE}))
    assert HOSTILE not in body


def test_new_subject_form_reshows_the_typed_name_on_400(client):
    body = text(client.post("/decks/new", data={"name": "N" * 101}))
    assert 'value="' + "N" * 101 + '"' in body


# --- links ---


def test_home_links_to_new_subject_and_add_card(client, seed):
    deck_id = seed("Biology")
    body = text(client.get("/"))
    assert 'href="/decks/new"' in body
    assert f'href="{card_url(deck_id)}"' in body


def test_empty_home_links_to_new_subject(client):
    body = text(client.get("/"))
    assert "There are no flashcards yet" in body
    assert 'href="/decks/new"' in body


def test_stats_page_links_to_add_card(client, seed):
    deck_id = seed("Biology")
    assert f'href="{card_url(deck_id)}"' in text(client.get(f"/decks/{deck_id}/stats"))


def test_all_stats_page_has_no_add_card_link(client, seed):
    seed("Biology")
    assert "Add card" not in text(client.get("/stats"))


def test_done_page_links_to_add_card(client, seed):
    deck_id = seed("Biology", questions=())
    assert "No cards are due" in text(client.get(f"/decks/{deck_id}/quiz"))
    assert f'href="{card_url(deck_id)}"' in text(client.get(f"/decks/{deck_id}/quiz"))


def test_adding_a_card_to_a_done_deck_makes_it_studyable_again(client, seed):
    deck_id = seed("Biology", questions=())
    assert "Done for today" in text(client.get(f"/decks/{deck_id}/quiz"))
    add(client, deck_id, "Fresh?", "Yes")
    assert "Fresh?" in text(client.get(f"/decks/{deck_id}/quiz"))


# --- no JS, no sessions, no anthropic ---


def test_pages_have_no_script_tag_and_set_no_cookie(client, seed):
    deck_id = seed("Biology")
    for response in (
        client.get("/decks/new"),
        client.get(card_url(deck_id) + "?added=1"),
        add(client, deck_id, "Another?", "yes"),
    ):
        assert "<script" not in text(response)
        assert "Set-Cookie" not in response.headers


def test_web_module_still_does_not_import_anthropic():
    code = "import sys, flashcards.web; sys.exit(1 if 'anthropic' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
