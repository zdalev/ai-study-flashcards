"""Extra Phase 4 tests from the phase-end verification (gaps beyond tests/test_web_add.py).

- concurrent POSTs of the same new card or subject leave one row (checked against a live server by
  hand; this pins it for the test client with real threads);
- a browser sends line breaks as CRLF: they are stored as LF and the answer reveal shows every line;
- unicode text round-trips; a question with a line break counts as a duplicate of the same text on
  one line;
- adding a card leaves every other card and subject untouched;
- a hand-added card is exported like any other card.
"""

import csv
import io
from concurrent.futures import ThreadPoolExecutor

from conftest import WEB_TODAY
from flashcards.cli import main
from flashcards.storage import CardStore, open_db
from flashcards.web import MAX_ANSWER_LEN, MAX_DECK_NAME_LEN, MAX_QUESTION_LEN
from test_web_add import add, card_url, deck_rows, rows, text


def reveal(client, deck_id, card_id=1):
    """The answer step of the quiz for one card: the page that shows the correct answer."""
    data = {"card_id": card_id, "step": "answer", "given_answer": ""}
    return text(client.post(f"/decks/{deck_id}/quiz", data=data))


def post_many(app, path, data, count=12):
    """POST the same form `count` times at once, each from its own client; returns the statuses."""

    def post(_):
        return app.test_client().post(path, data=data).status_code

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(post, range(count)))


def test_parallel_posts_of_the_same_card_save_one_card(app, seed, db_path):
    deck_id = seed("Biology", questions=())
    statuses = post_many(app, card_url(deck_id), {"question": "Same?", "answer": "Yes"})
    assert sorted(set(statuses)) in ([303, 409], [303])
    assert statuses.count(303) == 1
    assert len(rows(db_path)) == 1


def test_parallel_posts_of_the_same_subject_create_one_deck(app, db_path):
    statuses = post_many(app, "/decks/new", {"name": "History"})
    assert set(statuses) == {303}
    assert len(deck_rows(db_path)) == 1


def test_parallel_posts_of_different_cards_all_save(app, seed, db_path):
    deck_id = seed("Biology", questions=())

    def post(i):
        data = {"question": f"Q{i}?", "answer": "A"}
        return app.test_client().post(card_url(deck_id), data=data).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(post, range(20)))
    assert set(statuses) == {303}
    assert len(rows(db_path)) == 20


def test_crlf_line_breaks_in_the_answer_are_stored_as_lf_and_shown(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    add(client, deck_id, "Steps?", "first\r\nsecond\r\n\r\nthird\rfourth")
    assert rows(db_path)[0][3] == "first\nsecond\n\nthird\nfourth"
    body = reveal(client, deck_id)
    assert "first" in body and "second" in body and "third" in body


def test_crlf_counts_as_one_character_for_the_length_limit(client, seed, db_path):
    deck_id = seed("Biology", questions=())
    browser_answer = "a\r\n" * (MAX_ANSWER_LEN // 2)  # the browser counts 1000 chars, posts 3000
    assert add(client, deck_id, "Long?", browser_answer).status_code == 303
    assert rows(db_path)[0][3] == ("a\n" * (MAX_ANSWER_LEN // 2)).strip()
    too_long = "a\r\n" * (MAX_ANSWER_LEN // 2 + 1)
    assert add(client, deck_id, "Longer?", too_long).status_code == 400


def test_crlf_counts_as_one_character_in_a_subject_name(client, db_path):
    name = "a\r\n" * (MAX_DECK_NAME_LEN // 2)  # the browser counts 100 characters
    assert client.post("/decks/new", data={"name": name}).status_code == 303
    assert deck_rows(db_path)[0][1] == " ".join("a" * (MAX_DECK_NAME_LEN // 2))


def test_unicode_question_and_answer_round_trip(client, seed, db_path):
    deck_id = seed("Chemistry", questions=())
    question, answer = "Größe von 日本語 😀?", "H₂O → café"
    assert add(client, deck_id, question, answer).status_code == 303
    assert rows(db_path)[0][2:4] == (question, answer)
    assert question in text(client.get(f"/decks/{deck_id}/quiz"))
    body = reveal(client, deck_id)
    assert question in body and answer in body


def test_question_with_a_line_break_duplicates_the_one_line_form(client, seed, db_path):
    deck_id = seed("Biology", questions=("What is\nDNA?",))
    assert add(client, deck_id, "what is   dna?", "x").status_code == 409
    assert len(rows(db_path)) == 1


def test_adding_a_card_leaves_other_cards_and_subjects_untouched(client, seed, db_path):
    biology = seed("Biology")
    chemistry = seed("Chemistry", questions=("C1?",))
    before = rows(db_path)
    assert add(client, chemistry, "C2?", "x").status_code == 303
    after = rows(db_path)
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1
    home = text(client.get("/"))
    assert "3 cards" in home  # Biology unchanged
    assert "2 cards" in home  # Chemistry grew by one
    with CardStore(open_db(db_path)) as store:
        assert len(store.due_cards(WEB_TODAY, biology)) == 3


def test_a_hand_added_card_is_exported(client, seed, db_path, capsys):
    deck_id = seed("Biology", questions=())
    add(client, deck_id, "Hand made?", "Yes, with\r\ntwo lines")
    code = main(["--db", str(db_path), "export", "-"])
    assert code == 0
    (row,) = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert row["question"] == "Hand made?"
    assert row["answer"].split() == ["Yes,", "with", "two", "lines"]


def test_added_flag_is_ignored_on_a_failed_post(client, seed):
    deck_id = seed("Biology", questions=())
    response = client.post(card_url(deck_id) + "?added=1", data={"question": "", "answer": "x"})
    assert response.status_code == 400
    assert "Card added" not in text(response)


def test_added_notice_links_to_the_quiz(client, seed):
    deck_id = seed("Biology")
    body = text(client.get(card_url(deck_id) + "?added=1"))
    notice = body.split('class="notice"')[1].split("</p>")[0]
    assert f'href="/decks/{deck_id}/quiz"' in notice


def test_hostile_answer_cannot_break_out_of_the_textarea(client, seed):
    deck_id = seed("Biology", questions=())
    evil = "</textarea><script>alert(1)</script>"
    body = text(add(client, deck_id, "", evil))
    assert evil not in body
    assert "&lt;/textarea&gt;&lt;script&gt;" in body


def test_forms_have_labels_and_maxlength_matching_the_server_limits(client, seed):
    deck_id = seed("Biology")
    card = text(client.get(card_url(deck_id)))
    for field in ("question", "answer"):
        assert f'<label for="{field}">' in card
        assert f'<textarea id="{field}" name="{field}"' in card
    assert f'maxlength="{MAX_QUESTION_LEN}"' in card
    assert f'maxlength="{MAX_ANSWER_LEN}"' in card
    subject = text(client.get("/decks/new"))
    assert '<label for="name">' in subject and 'id="name" name="name"' in subject
    assert f'maxlength="{MAX_DECK_NAME_LEN}"' in subject
