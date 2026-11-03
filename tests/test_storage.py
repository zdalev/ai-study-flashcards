import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest

from flashcards.errors import StorageError
from flashcards.models import CardDraft, ReviewState
from flashcards.scheduler import review
from flashcards.storage import CardStore, open_db

TODAY = date(2026, 3, 10)
YESTERDAY = TODAY - timedelta(days=1)
NOW = datetime(2026, 3, 10, 9, 30, 0)


def draft(question="What is ATP?", answer="The energy currency of the cell."):
    return CardDraft(question=question, answer=answer)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "flashcards.db"


@pytest.fixture
def store(db_path):
    with CardStore(open_db(db_path)) as card_store:
        yield card_store


@pytest.fixture
def deck(store):
    return store.get_or_create_deck("Biology", "notes/biology.md", NOW)


def add(store, deck, *questions, today=TODAY):
    return store.add_cards(deck.id, [draft(q, f"answer to {q}") for q in questions], today, NOW)


def review_card(store, card_id, grade, day):
    """Review a card like the quiz does and save it."""
    card = store.get_card(card_id)
    store.save_review(card_id, review(card.state, grade, day), grade)


# The schema of version 1, frozen here: files from before the answer modes look like this.
SCHEMA_V1 = """
CREATE TABLE decks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    source_path TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE cards (
    id            INTEGER PRIMARY KEY,
    deck_id       INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    question      TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(question)) > 0),
    answer        TEXT NOT NULL CHECK (length(trim(answer)) > 0),
    created_at    TEXT NOT NULL,
    ease_factor   REAL    NOT NULL DEFAULT 2.5 CHECK (ease_factor >= 1.3),
    interval_days INTEGER NOT NULL DEFAULT 0   CHECK (interval_days >= 0),
    repetitions   INTEGER NOT NULL DEFAULT 0   CHECK (repetitions >= 0),
    due_date      TEXT    NOT NULL,
    last_reviewed TEXT,
    UNIQUE (deck_id, question)
);
CREATE INDEX idx_cards_due ON cards (due_date);
CREATE TABLE chunks (
    deck_id    INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    digest     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (deck_id, digest)
);
CREATE TABLE reviews (
    card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    reviewed_on TEXT    NOT NULL,
    grade       INTEGER NOT NULL CHECK (grade BETWEEN 0 AND 5)
);
CREATE INDEX idx_reviews_day ON reviews (reviewed_on);
INSERT INTO decks (id, name, created_at) VALUES (1, 'Biology', '2026-03-01T09:00:00');
INSERT INTO cards (id, deck_id, question, answer, created_at, due_date, last_reviewed)
    VALUES (1, 1, 'What is ATP?', 'Energy.', '2026-03-01T09:00:00', '2026-03-12', '2026-03-10');
INSERT INTO reviews (card_id, reviewed_on, grade) VALUES (1, '2026-03-09', 1), (1, '2026-03-10', 4);
PRAGMA user_version = 1;
"""


def make_v1_file(path):
    """Create a database file the way version 1 of the program did, with two reviews."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V1)
    conn.close()


def review_columns(path):
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(reviews)")]
    finally:
        conn.close()


class TestOpenDb:
    def test_first_open_creates_parent_folder_and_schema(self, db_path):
        conn = open_db(db_path)
        try:
            tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {"decks", "cards", "chunks", "reviews"} <= tables
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
            assert conn.in_transaction is False
        finally:
            conn.close()

    def test_reopening_keeps_the_data(self, db_path):
        with CardStore(open_db(db_path)) as first:
            deck = first.get_or_create_deck("Biology", None, NOW)
            add(first, deck, "What is ATP?")
        with CardStore(open_db(db_path)) as second:
            assert second.find_deck("Biology") == deck
            assert [c.question for c in second.due_cards(TODAY)] == ["What is ATP?"]

    def test_reopening_does_not_reapply_the_schema(self, db_path):
        open_db(db_path).close()
        open_db(db_path).close()  # would fail with "table decks already exists"

    def test_parallel_first_opens_of_a_new_file_all_succeed(self, db_path):
        count = 16
        barrier = threading.Barrier(count)

        def first_open(_):
            barrier.wait()  # every thread sees user_version 0 and tries to create the schema
            open_db(db_path).close()

        for _ in range(5):  # a fresh file each round makes the race likely
            db_path.unlink(missing_ok=True)
            with ThreadPoolExecutor(max_workers=count) as pool:
                list(pool.map(first_open, range(count)))  # re-raises any StorageError
            conn = sqlite3.connect(db_path)
            try:
                tables = [
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'decks'"
                    )
                ]
                assert tables == ["decks"]
                assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
            finally:
                conn.close()

    def test_a_new_file_has_the_v2_review_columns(self, db_path):
        open_db(db_path).close()
        assert review_columns(db_path) == [
            "card_id",
            "reviewed_on",
            "grade",
            "mode",
            "given_answer",
        ]

    def test_v1_file_is_upgraded_and_keeps_its_data(self, db_path):
        make_v1_file(db_path)
        with CardStore(open_db(db_path)) as store:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
            assert store._conn.in_transaction is False
            rows = store._conn.execute(
                "SELECT card_id, reviewed_on, grade, mode, given_answer FROM reviews ORDER BY rowid"
            ).fetchall()
            assert [tuple(r) for r in rows] == [
                (1, "2026-03-09", 1, None, None),
                (1, "2026-03-10", 4, None, None),
            ]
            card = store.get_card(1)
            assert (card.question, card.state.due_date) == ("What is ATP?", date(2026, 3, 12))
            assert store.stats(TODAY).reviews_total == 2
        assert review_columns(db_path)[-2:] == ["mode", "given_answer"]

    def test_upgraded_file_accepts_new_reviews_and_reopening_is_a_no_op(self, db_path):
        make_v1_file(db_path)
        open_db(db_path).close()
        with CardStore(open_db(db_path)) as store:  # the second open must not ALTER again
            card = store.get_card(1)
            state = review(card.state, 4, date(2026, 3, 12))
            assert store.save_review(1, state, 4, mode="write", given_answer="Energy") is True
            newest = store.recent_answers(1)[0]
            assert (newest.mode, newest.given_answer) == ("write", "Energy")

    def test_parallel_first_opens_of_a_v1_file_all_succeed(self, db_path):
        count = 16
        barrier = threading.Barrier(count)

        def first_open(_):
            barrier.wait()  # every thread sees user_version 1 and tries to upgrade
            open_db(db_path).close()

        for _ in range(5):
            db_path.unlink(missing_ok=True)
            make_v1_file(db_path)
            with ThreadPoolExecutor(max_workers=count) as pool:
                list(pool.map(first_open, range(count)))  # re-raises any StorageError
            assert review_columns(db_path) == [
                "card_id",
                "reviewed_on",
                "grade",
                "mode",
                "given_answer",
            ]
            conn = sqlite3.connect(db_path)
            try:
                assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
                assert conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 2
            finally:
                conn.close()

    def test_rows_come_back_by_name(self, db_path):
        conn = open_db(db_path)
        try:
            assert conn.row_factory is sqlite3.Row
        finally:
            conn.close()

    def test_foreign_keys_are_enforced_on_every_connection(self, db_path):
        for _ in range(2):
            conn = open_db(db_path)
            try:
                assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO cards (deck_id, question, answer, created_at, due_date)"
                        " VALUES (999, 'q', 'a', '2026-01-01', '2026-01-01')"
                    )
            finally:
                conn.close()

    def test_deleting_a_deck_cascades_to_its_rows(self, db_path):
        conn = open_db(db_path)
        store = CardStore(conn)
        try:
            deck = store.get_or_create_deck("Biology", None, NOW)
            add(store, deck, "What is ATP?")
            store.add_cards(deck.id, [], TODAY, NOW, chunk_digest="abc")
            review_card(store, store.due_cards(TODAY)[0].id, 5, TODAY)
            with conn:
                conn.execute("DELETE FROM decks WHERE id = ?", (deck.id,))
            for table in ("cards", "chunks", "reviews"):
                assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        finally:
            store.close()

    @pytest.mark.parametrize("version", [3, 99])
    def test_database_from_a_newer_version_is_refused(self, db_path, version):
        conn = open_db(db_path)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.close()
        with pytest.raises(StorageError, match="newer"):
            open_db(db_path)

    def test_a_file_that_is_not_a_database_raises_storage_error(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"this is definitely not an sqlite database" * 50)
        with pytest.raises(StorageError):
            open_db(bad)

    def test_a_directory_as_path_raises_storage_error(self, tmp_path):
        with pytest.raises(StorageError):
            open_db(tmp_path)

    def test_parent_that_is_a_file_raises_storage_error(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        with pytest.raises(StorageError):
            open_db(blocker / "flashcards.db")


class TestDecks:
    def test_get_or_create_is_idempotent_and_case_insensitive(self, store):
        first = store.get_or_create_deck("Biology", "a.md", NOW)
        again = store.get_or_create_deck("  biology ", "b.md", NOW)
        assert again == first
        assert first.source_path == "a.md"
        assert len(store.deck_summaries(TODAY)) == 1

    def test_name_whitespace_is_collapsed(self, store):
        deck = store.get_or_create_deck("  World   War  II ", None, NOW)
        assert deck.name == "World War II"
        assert store.find_deck("world war ii") == deck

    def test_find_deck_ignores_case_and_extra_whitespace(self, store):
        deck = store.get_or_create_deck("World War II", None, NOW)
        assert store.find_deck("  world   war ii ") == deck

    def test_empty_name_is_rejected(self, store):
        with pytest.raises(StorageError):
            store.get_or_create_deck("   ", None, NOW)

    def test_find_and_get_return_none_when_missing(self, store, deck):
        assert store.find_deck("Chemistry") is None
        assert store.get_deck(deck.id + 100) is None
        assert store.get_deck(deck.id) == deck


class TestAddCards:
    def test_returns_number_inserted_and_new_cards_are_due_today(self, store, deck):
        assert add(store, deck, "Q1", "Q2", "Q3") == 3
        cards = store.due_cards(TODAY)
        assert len(cards) == 3
        for card in cards:
            assert card.state == ReviewState(
                due_date=TODAY, ease_factor=2.5, interval_days=0, repetitions=0, last_reviewed=None
            )

    def test_same_question_with_other_case_or_whitespace_is_ignored(self, store, deck):
        assert add(store, deck, "What is ATP?") == 1
        assert add(store, deck, "what is atp?", "  What   is\tATP?  ") == 0
        assert len(store.due_cards(TODAY)) == 1

    def test_duplicate_inside_one_call_is_counted_once(self, store, deck):
        assert add(store, deck, "Q1", "q1") == 1

    def test_same_question_is_allowed_in_another_deck(self, store, deck):
        other = store.get_or_create_deck("Chemistry", None, NOW)
        assert add(store, deck, "What is ATP?") == 1
        assert add(store, other, "What is ATP?") == 1

    def test_text_is_normalised_before_insert(self, store, deck):
        store.add_cards(
            deck.id,
            [CardDraft(question="  What   is\n ATP? ", answer="  Line one\nline two \n")],
            TODAY,
            NOW,
        )
        card = store.due_cards(TODAY)[0]
        assert card.question == "What is ATP?"
        assert card.answer == "Line one\nline two"

    def test_unicode_is_normalised_to_nfc(self, store, deck):
        decomposed = "Café?"
        precomposed = "Café?"
        assert add(store, deck, decomposed) == 1
        assert add(store, deck, precomposed) == 0
        assert store.due_cards(TODAY)[0].question == precomposed

    def test_answer_is_normalised_to_nfc(self, store, deck):
        store.add_cards(deck.id, [draft("Q", "Cafe\u0301")], TODAY, NOW)
        assert store.due_cards(TODAY)[0].answer == "Caf\u00e9"

    def test_created_at_is_the_given_time_not_the_due_day(self, store, deck):
        add(store, deck, "Q1", today=YESTERDAY)
        row = store._conn.execute("SELECT created_at FROM cards").fetchone()
        assert row["created_at"] == "2026-03-10T09:30:00"

    def test_digest_is_recorded_per_deck(self, store, deck):
        other = store.get_or_create_deck("Chemistry", None, NOW)
        assert store.has_chunk(deck.id, "abc") is False
        store.add_cards(deck.id, [draft()], TODAY, NOW, chunk_digest="abc")
        assert store.has_chunk(deck.id, "abc") is True
        assert store.has_chunk(deck.id, "other") is False
        assert store.has_chunk(other.id, "abc") is False

    def test_chunk_is_still_recorded_when_all_cards_are_duplicates(self, store, deck):
        add(store, deck, "What is ATP?")
        assert store.add_cards(deck.id, [draft()], TODAY, NOW, chunk_digest="abc") == 0
        assert store.has_chunk(deck.id, "abc") is True

    def test_recording_the_same_chunk_twice_is_fine(self, store, deck):
        store.add_cards(deck.id, [draft("Q1")], TODAY, NOW, chunk_digest="abc")
        assert store.add_cards(deck.id, [draft("Q2")], TODAY, NOW, chunk_digest="abc") == 1

    def test_no_digest_records_no_chunk(self, store, deck):
        add(store, deck, "Q1")
        assert store.has_chunk(deck.id, "") is False

    @pytest.mark.parametrize(
        "bad",
        [
            CardDraft(question="Bad?", answer=""),
            CardDraft(question="Bad?", answer="  \n\t "),
            CardDraft(question="   ", answer="Fine."),
        ],
    )
    def test_empty_text_saves_nothing_not_even_the_chunk(self, store, deck, bad):
        with pytest.raises(StorageError):
            store.add_cards(deck.id, [draft("Good?"), bad], TODAY, NOW, chunk_digest="abc")
        assert store.due_cards(TODAY) == []
        assert store.has_chunk(deck.id, "abc") is False

    def test_unknown_deck_raises_storage_error(self, store):
        with pytest.raises(StorageError):
            store.add_cards(999, [draft()], TODAY, NOW, chunk_digest="abc")


class TestGetCard:
    def test_returns_the_card_or_none(self, store, deck):
        add(store, deck, "What is ATP?")
        card_id = store.due_cards(TODAY)[0].id
        card = store.get_card(card_id)
        assert card.id == card_id
        assert card.deck_id == deck.id
        assert card.question == "What is ATP?"
        assert card.answer == "answer to What is ATP?"
        assert store.get_card(card_id + 100) is None


class TestDueCards:
    def seed(self, store, deck):
        """Q-late (due yesterday), Q-today, Q-future (due tomorrow); ids in that order."""
        add(store, deck, "Q-late", today=YESTERDAY)
        add(store, deck, "Q-today")
        add(store, deck, "Q-future", today=TODAY + timedelta(days=1))

    def test_only_cards_due_on_or_before_today(self, store, deck):
        self.seed(store, deck)
        assert [c.question for c in store.due_cards(TODAY)] == ["Q-late", "Q-today"]

    def test_a_card_due_exactly_today_is_included_and_tomorrow_is_not(self, store, deck):
        self.seed(store, deck)
        assert [c.question for c in store.due_cards(YESTERDAY)] == ["Q-late"]
        assert len(store.due_cards(TODAY + timedelta(days=1))) == 3

    def test_ordered_by_due_date_then_id(self, store, deck):
        add(store, deck, "B-today")
        add(store, deck, "A-today")
        add(store, deck, "C-late", today=YESTERDAY)
        assert [c.question for c in store.due_cards(TODAY)] == ["C-late", "B-today", "A-today"]

    def test_limit(self, store, deck):
        self.seed(store, deck)
        assert [c.question for c in store.due_cards(TODAY, limit=1)] == ["Q-late"]
        assert store.due_cards(TODAY, limit=0) == []
        assert len(store.due_cards(TODAY, limit=10)) == 2

    def test_negative_limit_is_rejected(self, store, deck):
        with pytest.raises(ValueError):
            store.due_cards(TODAY, limit=-1)

    def test_deck_filter(self, store, deck):
        other = store.get_or_create_deck("Chemistry", None, NOW)
        add(store, deck, "Bio question")
        add(store, other, "Chem question")
        assert [c.question for c in store.due_cards(TODAY, deck_id=other.id)] == ["Chem question"]
        assert len(store.due_cards(TODAY)) == 2
        assert store.due_cards(TODAY, deck_id=other.id + 100) == []

    def test_reviewed_card_leaves_the_queue(self, store, deck):
        add(store, deck, "Q1")
        review_card(store, store.due_cards(TODAY)[0].id, 5, TODAY)
        assert store.due_cards(TODAY) == []


class TestSaveReview:
    def test_round_trips_the_state_exactly(self, store, deck):
        add(store, deck, "Q1")
        card_id = store.due_cards(TODAY)[0].id
        state = ReviewState(
            due_date=date(2026, 4, 2),
            ease_factor=1.86,
            interval_days=23,
            repetitions=4,
            last_reviewed=TODAY,
        )
        store.save_review(card_id, state, 4)
        assert store.get_card(card_id).state == state

    def test_returns_true_when_the_review_was_saved(self, store, deck):
        add(store, deck, "Q1")
        card = store.due_cards(TODAY)[0]
        assert store.save_review(card.id, review(card.state, 5, TODAY), 5) is True

    def test_second_save_from_the_same_due_state_is_not_applied(self, store, deck):
        add(store, deck, "Q1")
        card = store.due_cards(TODAY)[0]
        first = review(card.state, 5, TODAY)
        assert store.save_review(card.id, first, 5) is True

        # a parallel request that read the same due state arrives second
        assert store.save_review(card.id, review(card.state, 0, TODAY), 0) is False

        assert store.get_card(card.id).state == first
        rows = store._conn.execute("SELECT grade FROM reviews").fetchall()
        assert [tuple(r) for r in rows] == [(5,)]

    def test_card_that_is_not_due_yet_is_not_reviewed(self, store, deck):
        add(store, deck, "Q1", today=TODAY + timedelta(days=3))
        card = store.get_card(1)
        assert store.save_review(card.id, review(card.state, 5, TODAY), 5) is False
        assert store.get_card(card.id) == card
        assert store._conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0

    def test_appends_exactly_one_review_row_per_call(self, store, deck):
        add(store, deck, "Q1")
        card_id = store.due_cards(TODAY)[0].id
        review_card(store, card_id, 5, TODAY)
        review_card(store, card_id, 3, TODAY + timedelta(days=1))
        rows = store._conn.execute(
            "SELECT card_id, reviewed_on, grade FROM reviews ORDER BY rowid"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            (card_id, "2026-03-10", 5),
            (card_id, "2026-03-11", 3),
        ]

    def test_unknown_card_raises_and_writes_no_review(self, store, deck):
        add(store, deck, "Q1")
        state = review(store.due_cards(TODAY)[0].state, 5, TODAY)
        with pytest.raises(StorageError, match="no card"):
            store.save_review(999, state, 5)
        assert store._conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0

    def test_invalid_grade_rolls_back_the_card_update(self, store, deck):
        add(store, deck, "Q1")
        card = store.due_cards(TODAY)[0]
        with pytest.raises(StorageError):
            store.save_review(card.id, review(card.state, 5, TODAY), 9)
        assert store.get_card(card.id) == card
        assert store._conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0

    def test_state_without_last_reviewed_is_rejected(self, store, deck):
        add(store, deck, "Q1")
        card = store.due_cards(TODAY)[0]
        with pytest.raises(StorageError):
            store.save_review(card.id, card.state, 5)
        assert store.get_card(card.id) == card

    def test_mode_and_given_answer_are_logged_with_the_review(self, store, deck):
        add(store, deck, "Q1", "Q2", "Q3")
        first, second, third = store.due_cards(TODAY)
        for card, mode, given in ((first, "write", "my text"), (second, "choice", "")):
            assert store.save_review(
                card.id, review(card.state, 4, TODAY), 4, mode=mode, given_answer=given
            )
        assert store.save_review(third.id, review(third.state, 4, TODAY), 4)
        rows = store._conn.execute("SELECT mode, given_answer FROM reviews ORDER BY rowid")
        assert [tuple(r) for r in rows] == [("write", "my text"), ("choice", ""), (None, None)]

    def test_unknown_mode_is_rejected_and_rolls_back(self, store, deck):
        add(store, deck, "Q1")
        card = store.due_cards(TODAY)[0]
        with pytest.raises(StorageError):
            store.save_review(card.id, review(card.state, 4, TODAY), 4, mode="typo")
        assert store.get_card(card.id) == card
        assert store._conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0

    def test_a_save_that_is_not_applied_logs_no_answer(self, store, deck):
        add(store, deck, "Q1")
        card = store.due_cards(TODAY)[0]
        store.save_review(card.id, review(card.state, 4, TODAY), 4, mode="write", given_answer="a")
        again = store.save_review(
            card.id, review(card.state, 0, TODAY), 0, mode="write", given_answer="b"
        )
        assert again is False
        rows = store._conn.execute("SELECT given_answer FROM reviews").fetchall()
        assert [tuple(r) for r in rows] == [("a",)]

    def test_review_survives_reopening(self, db_path):
        with CardStore(open_db(db_path)) as first:
            deck = first.get_or_create_deck("Biology", None, NOW)
            add(first, deck, "Q1")
            card_id = first.due_cards(TODAY)[0].id
            review_card(first, card_id, 4, TODAY)
        with CardStore(open_db(db_path)) as second:
            assert second.get_card(card_id).state.last_reviewed == TODAY
            assert second.stats(TODAY).reviews_total == 1


class TestRecentAnswers:
    def seed(self, store, deck):
        yesterday = TODAY - timedelta(days=1)
        add(store, deck, "Q1", "Q2", today=yesterday)  # due yesterday, so reviewable then
        first, second = store.due_cards(TODAY)
        store.save_review(
            first.id, review(first.state, 1, yesterday), 1, mode="write", given_answer="old try"
        )
        store.save_review(
            second.id, review(second.state, 4, TODAY), 4, mode="write", given_answer="right"
        )
        card = store.get_card(first.id)
        store.save_review(
            card.id, review(card.state, 4, TODAY), 4, mode="choice", given_answer="Answer to Q1"
        )
        return first, second

    def test_returns_the_reviews_newest_first_with_both_texts(self, store, deck):
        first, second = self.seed(store, deck)
        entries = store.recent_answers(deck.id)
        assert [(e.card_id, e.reviewed_on, e.mode, e.given_answer, e.grade) for e in entries] == [
            (first.id, TODAY, "choice", "Answer to Q1", 4),
            (second.id, TODAY, "write", "right", 4),
            (first.id, YESTERDAY, "write", "old try", 1),
        ]
        assert entries[0].question == "Q1"
        assert entries[0].correct_answer == "answer to Q1"

    def test_limit_keeps_the_newest(self, store, deck):
        self.seed(store, deck)
        assert [e.given_answer for e in store.recent_answers(deck.id, limit=2)] == [
            "Answer to Q1",
            "right",
        ]
        assert store.recent_answers(deck.id, limit=0) == []

    def test_default_limit_is_20(self, store, deck):
        questions = [f"Q{n}" for n in range(25)]
        add(store, deck, *questions)
        for card in store.due_cards(TODAY):
            store.save_review(card.id, review(card.state, 4, TODAY), 4, mode="write")
        assert len(store.recent_answers(deck.id)) == 20
        assert len(store.recent_answers(deck.id, limit=100)) == 25

    def test_negative_limit_is_rejected(self, store, deck):
        with pytest.raises(ValueError, match="limit"):
            store.recent_answers(deck.id, limit=-1)

    def test_only_the_decks_own_reviews(self, store, deck):
        self.seed(store, deck)
        other = store.get_or_create_deck("Physics", None, NOW)
        add(store, other, "Phys?")
        card = store.due_cards(TODAY, other.id)[0]
        store.save_review(card.id, review(card.state, 4, TODAY), 4, mode="write", given_answer="p")
        assert [e.given_answer for e in store.recent_answers(other.id)] == ["p"]
        assert len(store.recent_answers(deck.id)) == 3
        assert store.recent_answers(999) == []

    def test_old_reviews_without_an_answer_come_back_as_none(self, db_path):
        make_v1_file(db_path)
        with CardStore(open_db(db_path)) as store:
            entries = store.recent_answers(1)
        assert [(e.reviewed_on, e.mode, e.given_answer) for e in entries] == [
            (date(2026, 3, 10), None, None),
            (date(2026, 3, 9), None, None),
        ]

    def test_empty_answer_is_kept_apart_from_no_answer(self, store, deck):
        add(store, deck, "Q1")
        card = store.due_cards(TODAY)[0]
        store.save_review(card.id, review(card.state, 0, TODAY), 0, mode="write", given_answer="")
        assert store.recent_answers(deck.id)[0].given_answer == ""


class TestStats:
    def seed_reviews(self, store, deck):
        """Today: grades 5, 2, 4 (2 correct). Yesterday: grade 1. 4 reviews in all, 2 correct."""
        add(store, deck, "Q1", "Q2", "Q3", "Q4", today=YESTERDAY)
        ids = [c.id for c in store.due_cards(TODAY)]
        review_card(store, ids[0], 1, YESTERDAY)
        review_card(store, ids[0], 5, TODAY)
        review_card(store, ids[1], 2, TODAY)
        review_card(store, ids[2], 4, TODAY)
        return ids

    def test_empty_database_gives_zeros_and_no_accuracy(self, store):
        stats = store.stats(TODAY)
        assert (stats.total_cards, stats.new_cards, stats.due_today) == (0, 0, 0)
        assert (stats.reviewed_today, stats.mature_cards) == (0, 0)
        assert stats.average_ease is None
        assert stats.next_due is None
        assert (stats.reviews_today, stats.reviews_total) == (0, 0)
        assert stats.accuracy_today is None
        assert stats.accuracy_all_time is None

    def test_accuracy_today_and_all_time(self, store, deck):
        self.seed_reviews(store, deck)
        stats = store.stats(TODAY)
        assert (stats.reviews_today, stats.correct_today) == (3, 2)
        assert (stats.reviews_total, stats.correct_total) == (4, 2)
        assert stats.accuracy_today == pytest.approx(2 / 3)
        assert stats.accuracy_all_time == pytest.approx(2 / 4)

    def test_accuracy_with_deck_filter(self, store, deck):
        other = store.get_or_create_deck("Chemistry", None, NOW)
        self.seed_reviews(store, deck)
        add(store, other, "Chem Q1")
        review_card(store, store.due_cards(TODAY, deck_id=other.id)[0].id, 0, TODAY)

        bio = store.stats(TODAY, deck_id=deck.id)
        assert (bio.reviews_today, bio.correct_today) == (3, 2)
        assert (bio.reviews_total, bio.correct_total) == (4, 2)
        chem = store.stats(TODAY, deck_id=other.id)
        assert (chem.reviews_total, chem.correct_total) == (1, 0)
        assert chem.accuracy_all_time == 0.0
        everything = store.stats(TODAY)
        assert (everything.reviews_total, everything.correct_total) == (5, 2)

    def test_grade_three_counts_as_correct_and_two_does_not(self, store, deck):
        add(store, deck, "Q1", "Q2")
        first, second = (c.id for c in store.due_cards(TODAY))
        review_card(store, first, 3, TODAY)
        review_card(store, second, 2, TODAY)
        stats = store.stats(TODAY)
        assert (stats.reviews_total, stats.correct_total) == (2, 1)

    def test_grade_three_today_counts_as_correct_today(self, store, deck):
        add(store, deck, "Q1")
        review_card(store, store.due_cards(TODAY)[0].id, 3, TODAY)
        stats = store.stats(TODAY)
        assert (stats.reviews_today, stats.correct_today) == (1, 1)
        assert stats.accuracy_today == 1.0

    def test_card_due_today_is_due_but_next_due_is_after_today(self, store, deck):
        add(store, deck, "Q1")  # due today
        add(store, deck, "Q2", today=TODAY + timedelta(days=2))
        add(store, deck, "Q3", today=TODAY + timedelta(days=5))
        stats = store.stats(TODAY)
        assert stats.due_today == 1
        assert stats.next_due == TODAY + timedelta(days=2)

    def test_stats_for_a_deck_without_cards_are_empty(self, store, deck):
        stats = store.stats(TODAY, deck_id=deck.id)
        assert stats.total_cards == 0
        assert stats.accuracy_all_time is None

    def test_card_counts(self, store, deck):
        ids = self.seed_reviews(store, deck)
        stats = store.stats(TODAY)
        assert stats.total_cards == 4
        assert stats.new_cards == 1  # ids[3] was never reviewed
        assert stats.reviewed_today == 3
        # every reviewed card is due tomorrow (first correct answer or a fail gives 1 day)
        assert stats.due_today == 1  # only the untouched one
        assert stats.next_due == TODAY + timedelta(days=1)
        assert stats.mature_cards == 0
        assert store.get_card(ids[3]).state.last_reviewed is None

    def test_mature_cards_and_average_ease(self, store, deck):
        add(store, deck, "Q1", "Q2")
        first, second = (c.id for c in store.due_cards(TODAY))
        store.save_review(
            first, ReviewState(date(2026, 5, 1), 2.0, 21, 4, TODAY), 5
        )  # interval 21 = mature
        store.save_review(second, ReviewState(date(2026, 3, 20), 1.5, 20, 3, TODAY), 4)
        stats = store.stats(TODAY)
        assert stats.mature_cards == 1
        assert stats.average_ease == pytest.approx(1.75)
        assert stats.next_due == date(2026, 3, 20)

    def test_new_cards_with_deck_filter(self, store, deck):
        other = store.get_or_create_deck("Chemistry", None, NOW)
        add(store, deck, "Q1", "Q2")
        add(store, other, "Chem Q1")
        assert store.stats(TODAY, deck_id=deck.id).new_cards == 2
        assert store.stats(TODAY, deck_id=other.id).total_cards == 1
        assert store.stats(TODAY).total_cards == 3


class TestDeckSummaries:
    def test_empty(self, store):
        assert store.deck_summaries(TODAY) == []

    def test_counts_cards_and_due_cards_ordered_by_name(self, store):
        zoology = store.get_or_create_deck("zoology", None, NOW)
        biology = store.get_or_create_deck("Biology", None, NOW)
        empty = store.get_or_create_deck("Art", None, NOW)
        add(store, biology, "Q1", "Q2")
        add(store, biology, "Q3", today=TODAY + timedelta(days=3))
        add(store, zoology, "Z1")

        summaries = store.deck_summaries(TODAY)
        assert [s.deck for s in summaries] == [empty, biology, zoology]
        assert [(s.cards, s.due) for s in summaries] == [(0, 0), (3, 2), (1, 1)]


class TestCardsWithDeckNames:
    def test_empty(self, store):
        assert store.cards_with_deck_names() == []

    def test_ordered_by_deck_name_then_card_id(self, store):
        zoology = store.get_or_create_deck("zoology", None, NOW)
        biology = store.get_or_create_deck("Biology", None, NOW)
        add(store, zoology, "Z1")
        add(store, biology, "B2", "B1")
        add(store, zoology, "Z2")

        rows = store.cards_with_deck_names()
        assert [(name, card.question) for name, card in rows] == [
            ("Biology", "B2"),
            ("Biology", "B1"),
            ("zoology", "Z1"),
            ("zoology", "Z2"),
        ]

    def test_deck_filter_and_full_card_state(self, store, deck):
        other = store.get_or_create_deck("Chemistry", None, NOW)
        add(store, deck, "B1")
        add(store, other, "C1")

        rows = store.cards_with_deck_names(deck.id)
        assert [(name, card.question) for name, card in rows] == [("Biology", "B1")]
        assert rows[0][1] == store.due_cards(TODAY, deck_id=deck.id)[0]

    def test_unknown_deck_gives_nothing(self, store, deck):
        add(store, deck, "B1")
        assert store.cards_with_deck_names(deck.id + 99) == []


class TestStoreLifecycle:
    def test_context_manager_closes_the_connection(self, db_path):
        with CardStore(open_db(db_path)) as card_store:
            pass
        with pytest.raises(StorageError):
            card_store.find_deck("Biology")

    def test_sqlite_errors_are_wrapped(self, store):
        store._conn.execute("DROP TABLE cards")
        with pytest.raises(StorageError, match="database error"):
            store.get_card(1)


class TestDeleteDeck:
    def seeded(self, store, deck):
        other = store.get_or_create_deck("Chemistry", None, NOW)
        for target, digest in ((deck, "d1"), (other, "d2")):
            store.add_cards(
                target.id, [draft("A?", "a"), draft("B?", "b")], TODAY, NOW, chunk_digest=digest
            )
            for card in store.cards_with_deck_names(target.id):
                review_card(store, card[1].id, 4, TODAY)
        return other

    def count(self, store, table):
        return store._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_removes_deck_cards_chunks_and_reviews(self, store, deck):
        other = self.seeded(store, deck)
        assert store.delete_deck(deck.id) is True
        assert store.get_deck(deck.id) is None
        assert store.cards_with_deck_names(deck.id) == []
        assert store.has_chunk(deck.id, "d1") is False
        assert store.recent_answers(deck.id) == []
        assert (self.count(store, "cards"), self.count(store, "chunks")) == (2, 1)
        assert self.count(store, "reviews") == 2
        assert store.get_deck(other.id) == other
        assert store.has_chunk(other.id, "d2") is True

    def test_deletes_children_even_without_foreign_keys(self, store, deck):
        self.seeded(store, deck)
        store._conn.execute("PRAGMA foreign_keys = OFF")
        assert store.delete_deck(deck.id) is True
        assert self.count(store, "cards") == 2
        assert self.count(store, "reviews") == 2
        assert self.count(store, "chunks") == 1

    def test_unknown_or_already_deleted_deck_returns_false(self, store, deck):
        self.seeded(store, deck)
        assert store.delete_deck(deck.id + 99) is False
        assert self.count(store, "cards") == 4
        assert store.delete_deck(deck.id) is True
        assert store.delete_deck(deck.id) is False

    def test_name_can_be_reused_after_delete(self, store, deck):
        add(store, deck, "A?")
        store.delete_deck(deck.id)
        again = store.get_or_create_deck("Biology", None, NOW)
        assert add(store, again, "A?") == 1

    def test_storage_error_leaves_everything_in_place(self, store, deck):
        self.seeded(store, deck)
        store._conn.execute("DROP TABLE chunks")
        with pytest.raises(StorageError, match="deleting a deck"):
            store.delete_deck(deck.id)
        assert store.get_deck(deck.id) is not None
        assert self.count(store, "cards") == 4
        assert self.count(store, "reviews") == 4
