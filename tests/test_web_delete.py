import sqlite3

import pytest

from conftest import WEB_NOW, WEB_TODAY
from flashcards import scheduler
from flashcards.storage import CardStore, open_db

HOSTILE = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
HUGE_ID = 99999999999999999999


def text(response):
    return response.get_data(as_text=True)


def delete_url(deck_id):
    return f"/decks/{deck_id}/delete"


def count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def review_all(db_path, deck_id):
    """Log one review per card of the deck."""
    with CardStore(open_db(db_path)) as store:
        for _name, card in store.cards_with_deck_names(deck_id):
            state = scheduler.review(card.state, 4, WEB_TODAY)
            store.save_review(card.id, state, 4, mode="write", given_answer="x")


def add_chunk(db_path, deck_id, digest):
    with CardStore(open_db(db_path)) as store:
        store.add_cards(deck_id, [], WEB_TODAY, WEB_NOW, chunk_digest=digest)


# --- Done button on the add-card page ---


def test_add_card_page_has_done_link_to_home(client, seed):
    deck_id = seed("Biology")
    body = text(client.get(f"/decks/{deck_id}/cards/new"))
    assert '<a class="button secondary" href="/">Done</a>' in body
    assert body.index(">Done<") < body.index(">Add card</button>")
    assert "Study this subject" in body and "Back to subjects" in body


def test_done_link_leads_to_home(client, seed):
    deck_id = seed("Biology")
    assert client.get("/").status_code == 200
    assert 'href="/">Done<' in text(client.get(f"/decks/{deck_id}/cards/new?added=1"))


# --- confirm page ---


def test_confirm_page_shows_name_count_and_does_not_delete(client, seed, db_path):
    deck_id = seed("Biology")
    response = client.get(delete_url(deck_id))
    body = text(response)
    assert response.status_code == 200
    assert "Delete Biology?" in body
    assert "This removes 3 cards and their history. This cannot be undone." in body
    assert '<form method="post" action="/decks/1/delete">' in body
    assert "Delete subject</button>" in body
    assert f'href="/decks/{deck_id}/stats">Cancel' in body
    assert count(db_path, "decks") == 1 and count(db_path, "cards") == 3


def test_confirm_page_singular_card(client, seed):
    deck_id = seed("Biology", questions=("Only?",))
    assert "removes 1 card and" in text(client.get(delete_url(deck_id)))


def test_confirm_page_escapes_deck_name(client, seed):
    deck_id = seed(HOSTILE)
    body = text(client.get(delete_url(deck_id)))
    assert HOSTILE not in body
    assert f"Delete {ESCAPED}?" in body


def test_get_never_deletes_even_with_query(client, seed, db_path):
    deck_id = seed("Biology")
    for _ in range(2):
        client.get(delete_url(deck_id) + "?confirm=1&delete=1")
    assert count(db_path, "decks") == 1 and count(db_path, "cards") == 3


# --- POST ---


def test_post_deletes_deck_cards_chunks_and_reviews_only_for_that_deck(client, seed, db_path):
    gone = seed("Biology")
    kept = seed("Chemistry", questions=("C1?", "C2?"))
    for deck_id, digest in ((gone, "g"), (kept, "k")):
        review_all(db_path, deck_id)
        add_chunk(db_path, deck_id, digest)
    assert (count(db_path, "reviews"), count(db_path, "chunks")) == (5, 2)

    response = client.post(delete_url(gone))
    assert response.status_code == 303
    assert response.headers["Location"] == "/?deleted=Biology"
    assert count(db_path, "decks") == 1
    assert count(db_path, "cards") == 2
    assert count(db_path, "reviews") == 2
    assert count(db_path, "chunks") == 1
    assert client.get(f"/decks/{gone}/stats").status_code == 404
    assert client.get(f"/decks/{kept}/stats").status_code == 200


def test_all_stats_counts_drop_after_delete(client, seed):
    gone = seed("Biology")
    seed("Chemistry", questions=("C1?", "C2?"))
    assert "<td>5</td>" in text(client.get("/stats"))
    client.post(delete_url(gone))
    body = text(client.get("/stats"))
    assert "<td>2</td>" in body and "<td>5</td>" not in body


def test_notice_shown_after_redirect(client, seed):
    deck_id = seed("Biology")
    response = client.post(delete_url(deck_id), follow_redirects=True)
    body = text(response)
    assert response.status_code == 200
    assert 'Subject "Biology" deleted.' in body
    assert "Biology</h2>" not in body


def test_notice_only_when_param_present(client, seed):
    seed("Biology")
    assert "deleted." not in text(client.get("/"))
    assert 'Subject "Biology" deleted.' in text(client.get("/?deleted=Biology"))
    assert 'class="notice"' not in text(client.get("/?deleted="))


def test_notice_escapes_param(client):
    body = text(client.get("/", query_string={"deleted": HOSTILE}))
    assert HOSTILE not in body
    assert f'Subject "{ESCAPED}" deleted.' in body


def test_hostile_deck_name_round_trip_is_escaped(client, seed):
    deck_id = seed(HOSTILE)
    response = client.post(delete_url(deck_id), follow_redirects=True)
    body = text(response)
    assert HOSTILE not in body
    assert f'Subject "{ESCAPED}" deleted.' in body


def test_deck_can_be_recreated_after_delete(client, seed, db_path):
    deck_id = seed("Biology")
    client.post(delete_url(deck_id))
    seed("Biology")
    assert count(db_path, "decks") == 1 and count(db_path, "cards") == 3


# --- 404s ---


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("deck_id", [999, 0, HUGE_ID])
def test_unknown_or_huge_id_is_404(client, seed, db_path, method, deck_id):
    seed("Biology")
    response = getattr(client, method)(delete_url(deck_id))
    assert response.status_code == 404
    assert count(db_path, "decks") == 1 and count(db_path, "cards") == 3


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("bad", ["abc", "-1", "1.5"])
def test_non_integer_id_is_404(client, bad, method):
    assert getattr(client, method)(f"/decks/{bad}/delete").status_code == 404


def test_post_for_already_deleted_deck_is_404(client, seed):
    deck_id = seed("Biology")
    assert client.post(delete_url(deck_id)).status_code == 303
    assert client.post(delete_url(deck_id)).status_code == 404
    assert client.get(delete_url(deck_id)).status_code == 404


def test_other_methods_not_allowed(client, seed):
    deck_id = seed("Biology")
    assert client.put(delete_url(deck_id)).status_code == 405
    assert client.delete(delete_url(deck_id)).status_code == 405


# --- delete button placement ---


def test_delete_button_only_on_deck_stats(client, seed):
    deck_id = seed("Biology")
    deck_body = text(client.get(f"/decks/{deck_id}/stats"))
    assert (
        f'<a class="button danger" href="/decks/{deck_id}/delete">Delete subject</a>' in deck_body
    )
    assert "Delete subject" not in text(client.get("/stats"))
    assert "Delete subject" not in text(client.get("/"))
    assert "Delete subject" not in text(client.get(f"/decks/{deck_id}/cards/new"))


def test_delete_button_is_in_the_bottom_actions_row(client, seed):
    deck_id = seed("Biology")
    body = text(client.get(f"/decks/{deck_id}/stats"))
    row = body[body.index('<div class="page-actions">') :]
    assert "Delete subject" in row and "Back to subjects" in row


def test_delete_with_history_keeps_other_stats_page_working(client, seed, db_path):
    gone = seed("Biology")
    kept = seed("Chemistry")
    review_all(db_path, kept)
    client.post(delete_url(gone))
    body = text(client.get(f"/decks/{kept}/stats"))
    assert "Recent answers" in body and "pill" in body
