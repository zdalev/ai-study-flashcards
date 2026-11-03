"""Extra Phase 2 tests from the phase-end verification: gaps the mutation checks found.

Each test pins something that a plausible bug would have left unnoticed:
- the done page counts only the deck's own reviews of today (not all time, not other decks);
- the stats page shows mature and new cards in the right rows (the numbers differ here);
- the plan's end-to-end case: load_demo, grade two biology cards 4 and 1, other decks unchanged;
- load_demo keeps the deck names as written, and `load-demo` accepts a UTF-8 BOM.
"""

import json
import re
from datetime import datetime, timedelta

from conftest import WEB_NOW, WEB_TODAY
from flashcards.cli import Deps, main
from flashcards.models import ReviewState
from flashcards.pipeline import load_demo
from flashcards.storage import CardStore, open_db

DEMO_NAMES = ["biology", "english", "mathematics", "physics", "programming"]


def one_card_demo(name):
    card = {"question": "Q?", "answer": "A."}
    return {"version": 1, "decks": [{"name": name, "cards": [card]}]}


def text(response):
    return response.get_data(as_text=True)


def cells(body):
    return dict(re.findall(r'<th scope="row">(.*?)</th><td>(.*?)</td>', body))


def review(db_path, card_id, grade, reviewed_on=WEB_TODAY, interval=1):
    state = ReviewState(
        due_date=reviewed_on + timedelta(days=interval),
        interval_days=interval,
        repetitions=1,
        last_reviewed=reviewed_on,
    )
    with CardStore(open_db(db_path)) as store:
        with store._conn:
            store._conn.execute(
                "UPDATE cards SET due_date = ? WHERE id = ?", (reviewed_on.isoformat(), card_id)
            )
        assert store.save_review(card_id, state, grade)  # a card can only be reviewed once due


def due_ids(db_path, deck_id):
    with CardStore(open_db(db_path)) as store:
        return [c.id for c in store.due_cards(WEB_TODAY + timedelta(days=400), deck_id)]


def test_done_page_counts_only_this_decks_reviews_of_today(client, seed, db_path):
    biology = seed("Biology", questions=("A?", "B?", "C?"))
    physics = seed("Physics", questions=("D?", "E?"))
    yesterday = WEB_TODAY - timedelta(days=1)
    bio_a, bio_b, bio_c = due_ids(db_path, biology)
    phys_d, phys_e = due_ids(db_path, physics)
    review(db_path, bio_a, 1, reviewed_on=yesterday, interval=0)  # all time only, not today
    review(db_path, bio_a, 5)
    review(db_path, bio_b, 2)
    review(db_path, bio_c, 4)
    review(db_path, phys_d, 5)  # another deck: must not leak into biology
    review(db_path, phys_e, 5)

    response = client.get(f"/decks/{biology}/quiz")
    body = text(response)

    assert "Done for today" in body
    assert cells(body) == {
        "Reviewed today": "3",
        "Accuracy today": "67% (2/3)",  # not 50% (2/4) all time, not 80% (4/5) across decks
        "Next due": (WEB_TODAY + timedelta(days=1)).isoformat(),
    }


def test_stats_page_shows_mature_and_new_cards_in_their_own_rows(client, seed, db_path):
    deck_id = seed("Biology", questions=("A?", "B?", "C?", "D?", "E?"))
    ids = due_ids(db_path, deck_id)
    review(db_path, ids[0], 5, interval=30)  # mature (interval >= 21)
    review(db_path, ids[1], 4, interval=6)  # reviewed, not mature
    # ids[2:] stay new: three new cards against one mature card

    shown = cells(text(client.get(f"/decks/{deck_id}/stats")))

    assert shown["Mature cards"] == "1"
    assert shown["New cards"] == "3"
    assert shown["Cards"] == "5"


def test_end_to_end_after_load_demo_two_biology_grades_give_50_percent(client, db_path):
    data = {
        "version": 1,
        "decks": [
            {"name": name, "source": f"examples/{name}.md", "cards": [
                {"question": f"{name} question {n}?", "answer": f"{name} answer {n}."}
                for n in (1, 2, 3)
            ]}
            for name in DEMO_NAMES
        ],
    }  # fmt: skip
    with CardStore(open_db(db_path)) as store:
        assert load_demo(store, data, WEB_TODAY, WEB_NOW) == 15
        biology = store.find_deck("biology")
        others = [store.find_deck(name) for name in DEMO_NAMES if name != "biology"]
    first, second, _third = due_ids(db_path, biology.id)

    for card_id, grade in ((first, 4), (second, 1)):
        response = client.post(
            f"/decks/{biology.id}/quiz", data={"card_id": card_id, "grade": grade}
        )
        assert response.status_code == 303
        assert client.get(response.headers["Location"]).status_code == 200

    bio_stats = cells(text(client.get(f"/decks/{biology.id}/stats")))
    assert bio_stats["Accuracy today"] == "50% (1/2)"
    assert bio_stats["Reviewed today"] == "2"
    for deck in others:
        other = cells(text(client.get(f"/decks/{deck.id}/stats")))
        assert other["Accuracy today"] == "\N{EM DASH}"
        assert other["Reviewed today"] == "0"
        assert other["Due today"] == "3"
    # the failed card comes back tomorrow, the passed one later: both left today's queue
    assert "1 card due" in text(client.get(f"/decks/{biology.id}/quiz"))


def test_load_demo_keeps_deck_names_as_written():
    with CardStore(open_db(":memory:")) as store:
        load_demo(store, one_card_demo("Biology 101"), WEB_TODAY, WEB_NOW)
        assert [s.deck.name for s in store.deck_summaries(WEB_TODAY)] == ["Biology 101"]


def test_load_demo_command_accepts_a_utf8_bom(tmp_path, capsys):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(one_card_demo("biology")), encoding="utf-8-sig")
    deps = Deps(today=lambda: WEB_TODAY, now=lambda: datetime(2026, 11, 2, 9))

    assert main(["--db", str(tmp_path / "cards.db"), "load-demo", str(path)], deps) == 0

    assert capsys.readouterr().out.strip() == "Loaded 1 card into 1 deck"
