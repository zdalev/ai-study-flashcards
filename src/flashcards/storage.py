"""SQLite storage: schema, `open_db()` and `CardStore`.

One connection belongs to one thread (a web request opens its own). Every `sqlite3.Error`
leaves this module as a `StorageError`. Dates are stored as ISO `YYYY-MM-DD` text, never through
the (deprecated) sqlite3 date adapters.
"""

import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import TracebackType

from flashcards.errors import StorageError
from flashcards.models import (
    MATURE_INTERVAL_DAYS,
    Card,
    CardDraft,
    Deck,
    DeckSummary,
    ReviewEntry,
    ReviewState,
    Stats,
)
from flashcards.scheduler import PASS_GRADE

SCHEMA_VERSION = 2

# The schema of a new file (version 2). BEGIN IMMEDIATE takes the write lock first, so parallel
# first opens of a new file queue up (up to the connection's busy timeout); the IF NOT EXISTS
# clauses make the ones that were already past the `user_version == 0` check harmless once the
# winner has committed. Databases created by version 1 are upgraded by `_migrate_v1_to_v2`.
_SCHEMA_V2 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS decks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    source_path TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
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
CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (due_date);
CREATE TABLE IF NOT EXISTS chunks (
    deck_id    INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    digest     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (deck_id, digest)
);
CREATE TABLE IF NOT EXISTS reviews (
    card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    reviewed_on TEXT    NOT NULL,
    grade       INTEGER NOT NULL CHECK (grade BETWEEN 0 AND 5),
    mode        TEXT CHECK (mode IN ('write', 'choice')),
    given_answer TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_day ON reviews (reviewed_on);
PRAGMA user_version = 2;
COMMIT;
"""

# Version 2 added the answer mode and the typed or chosen answer to every review. Old rows keep
# NULL in both columns.
_V2_NEW_COLUMNS = (
    "ALTER TABLE reviews ADD COLUMN mode TEXT CHECK (mode IN ('write', 'choice'))",
    "ALTER TABLE reviews ADD COLUMN given_answer TEXT",
)

_CARD_COLUMNS = (
    "id, deck_id, question, answer, ease_factor, interval_days, repetitions, due_date,"
    " last_reviewed"
)


@contextmanager
def _db_errors(action: str) -> Iterator[None]:
    """Turn any sqlite3.Error raised inside the block into a StorageError."""
    try:
        yield
    except sqlite3.Error as exc:
        raise StorageError(f"database error while {action}: {exc}") from exc


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open (and create if needed) the database at `path` and return a ready connection.

    The parent folder is created, rows come back as `sqlite3.Row`, foreign keys are on, and
    the current schema is applied when the file is new, and a version 1 file is upgraded.
    Opening an up-to-date database is a no-op.
    Raises StorageError for an unusable path or file, or for a database from a newer version.
    """
    target = str(path)
    try:
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
    except (OSError, sqlite3.Error) as exc:
        raise StorageError(f"cannot open database {target}: {exc}") from exc
    try:
        conn.row_factory = sqlite3.Row
        # Must run before any transaction is open, otherwise SQLite silently ignores it.
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_schema(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise StorageError(f"cannot initialise database {target}: {exc}") from exc
    except StorageError:
        conn.close()
        raise
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise StorageError(
            f"database schema version {version} is newer than this program supports "
            f"({SCHEMA_VERSION})"
        )
    if version == 0:
        try:
            conn.executescript(_SCHEMA_V2)
        except sqlite3.Error:
            # executescript leaves its BEGIN open when a statement fails.
            conn.rollback()
            raise
    elif version < SCHEMA_VERSION:
        _migrate_v1_to_v2(conn)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add the review columns of version 2, once, even when several processes open the file.

    The version is read again after the write lock is taken: a process that waited for the
    lock finds the upgrade done and must not repeat it (ALTER TABLE ADD COLUMN fails with
    "duplicate column name" on the second run).
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] == 1:
            for statement in _V2_NEW_COLUMNS:
                conn.execute(statement)
            conn.execute("PRAGMA user_version = 2")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def normalize_text(text: str) -> str:
    """NFC-normalise and collapse every whitespace run to one space."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def _normalize_answer(text: str) -> str:
    """NFC-normalise and trim, keeping line breaks inside the answer."""
    return unicodedata.normalize("NFC", text).strip()


def _iso(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value.isoformat()


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise StorageError(f"invalid date in database: {value!r}") from exc


def _to_deck(row: sqlite3.Row) -> Deck:
    return Deck(id=row["id"], name=row["name"], source_path=row["source_path"])


def _to_card(row: sqlite3.Row) -> Card:
    due = _parse_date(row["due_date"])
    assert due is not None  # the column is NOT NULL
    state = ReviewState(
        due_date=due,
        ease_factor=row["ease_factor"],
        interval_days=row["interval_days"],
        repetitions=row["repetitions"],
        last_reviewed=_parse_date(row["last_reviewed"]),
    )
    return Card(
        id=row["id"],
        deck_id=row["deck_id"],
        question=row["question"],
        answer=row["answer"],
        state=state,
    )


def _to_review_entry(row: sqlite3.Row) -> ReviewEntry:
    reviewed_on = _parse_date(row["reviewed_on"])
    assert reviewed_on is not None  # the column is NOT NULL
    return ReviewEntry(
        card_id=row["card_id"],
        reviewed_on=reviewed_on,
        mode=row["mode"],
        question=row["question"],
        given_answer=row["given_answer"],
        correct_answer=row["answer"],
        grade=row["grade"],
    )


class CardStore:
    """Decks, cards, review state and statistics on top of one open connection.

    Use as a context manager (closes the connection) or call `close()`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> "CardStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with _db_errors("closing the database"):
            self._conn.close()

    # --- decks ---

    def get_or_create_deck(self, name: str, source_path: str | None, now: datetime) -> Deck:
        """Return the deck called `name` (case-insensitive), creating it when missing.

        An existing deck is returned unchanged, `source_path` is only used on creation.
        """
        clean = normalize_text(name)
        if not clean:
            raise StorageError("deck name must not be empty")
        with _db_errors("creating a deck"), self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO decks (name, source_path, created_at) VALUES (?, ?, ?)",
                (clean, source_path, _iso(now)),
            )
            row = self._conn.execute(
                "SELECT id, name, source_path FROM decks WHERE name = ?", (clean,)
            ).fetchone()
        return _to_deck(row)

    def find_deck(self, name: str) -> Deck | None:
        with _db_errors("looking up a deck"):
            row = self._conn.execute(
                "SELECT id, name, source_path FROM decks WHERE name = ?", (normalize_text(name),)
            ).fetchone()
        return None if row is None else _to_deck(row)

    def get_deck(self, deck_id: int) -> Deck | None:
        with _db_errors("looking up a deck"):
            row = self._conn.execute(
                "SELECT id, name, source_path FROM decks WHERE id = ?", (deck_id,)
            ).fetchone()
        return None if row is None else _to_deck(row)

    def delete_deck(self, deck_id: int) -> bool:
        """Delete the deck with its cards, chunk digests and reviews, in one transaction.

        The children are deleted explicitly (the schema also cascades, but only while foreign
        keys are on). Returns False, and changes nothing, for an unknown deck.
        """
        with _db_errors("deleting a deck"), self._conn:
            self._conn.execute(
                "DELETE FROM reviews WHERE card_id IN (SELECT id FROM cards WHERE deck_id = ?)",
                (deck_id,),
            )
            self._conn.execute("DELETE FROM cards WHERE deck_id = ?", (deck_id,))
            self._conn.execute("DELETE FROM chunks WHERE deck_id = ?", (deck_id,))
            cursor = self._conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
        return cursor.rowcount == 1

    def deck_summaries(self, today: date) -> list[DeckSummary]:
        """All decks ordered by name, with their card count and how many are due."""
        with _db_errors("listing decks"):
            rows = self._conn.execute(
                """
                SELECT d.id, d.name, d.source_path,
                       count(c.id) AS cards,
                       coalesce(sum(c.due_date <= ?), 0) AS due
                FROM decks d LEFT JOIN cards c ON c.deck_id = d.id
                GROUP BY d.id
                ORDER BY d.name, d.id
                """,
                (_iso(today),),
            ).fetchall()
        return [DeckSummary(deck=_to_deck(r), cards=r["cards"], due=r["due"]) for r in rows]

    # --- chunks and cards ---

    def has_chunk(self, deck_id: int, digest: str) -> bool:
        with _db_errors("checking a chunk"):
            row = self._conn.execute(
                "SELECT 1 FROM chunks WHERE deck_id = ? AND digest = ?", (deck_id, digest)
            ).fetchone()
        return row is not None

    def add_cards(
        self,
        deck_id: int,
        drafts: Sequence[CardDraft],
        today: date,
        now: datetime,
        chunk_digest: str | None = None,
    ) -> int:
        """Insert new cards (due `today`) in one transaction and return how many were new.

        Question and answer are normalised first. A question already in the deck (ignoring
        case and whitespace) is skipped. When `chunk_digest` is given the chunk is recorded
        too. An empty question or answer, or an unknown deck, raises StorageError and saves
        nothing - not even the chunk.
        """
        cleaned = [self._clean_draft(draft) for draft in drafts]
        with _db_errors("adding cards"), self._conn:
            inserted = 0
            for question, answer in cleaned:
                # OR IGNORE would also swallow CHECK failures, hence the checks above.
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO cards (deck_id, question, answer, created_at, due_date)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (deck_id, question, answer, _iso(now), _iso(today)),
                )
                inserted += cursor.rowcount
            if chunk_digest is not None:
                self._conn.execute(
                    "INSERT OR IGNORE INTO chunks (deck_id, digest, created_at) VALUES (?, ?, ?)",
                    (deck_id, chunk_digest, _iso(now)),
                )
        return inserted

    @staticmethod
    def _clean_draft(draft: CardDraft) -> tuple[str, str]:
        question = normalize_text(draft.question)
        answer = _normalize_answer(draft.answer)
        if not question or not answer:
            raise StorageError("a card needs a non-empty question and answer")
        return question, answer

    def get_card(self, card_id: int) -> Card | None:
        with _db_errors("reading a card"):
            row = self._conn.execute(
                f"SELECT {_CARD_COLUMNS} FROM cards WHERE id = ?", (card_id,)
            ).fetchone()
        return None if row is None else _to_card(row)

    def due_cards(
        self, today: date, deck_id: int | None = None, limit: int | None = None
    ) -> list[Card]:
        """Cards with `due_date <= today`, oldest due date first (then by id)."""
        if limit is not None and limit < 0:
            raise ValueError(f"limit must not be negative, got {limit}")
        sql = f"SELECT {_CARD_COLUMNS} FROM cards WHERE due_date <= ?"
        params: list[object] = [_iso(today)]
        if deck_id is not None:
            sql += " AND deck_id = ?"
            params.append(deck_id)
        sql += " ORDER BY due_date, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with _db_errors("reading due cards"):
            rows = self._conn.execute(sql, params).fetchall()
        return [_to_card(row) for row in rows]

    def cards_with_deck_names(self, deck_id: int | None = None) -> list[tuple[str, Card]]:
        """Every card (of one deck when `deck_id` is given) with its deck's name.

        Ordered by deck name, then card id.
        """
        sql = (
            "SELECT d.name AS deck_name,"
            " c.id, c.deck_id, c.question, c.answer, c.ease_factor, c.interval_days,"
            " c.repetitions, c.due_date, c.last_reviewed"
            " FROM cards c JOIN decks d ON d.id = c.deck_id"
        )
        params: list[object] = []
        if deck_id is not None:
            sql += " WHERE c.deck_id = ?"
            params.append(deck_id)
        sql += " ORDER BY d.name, c.id"
        with _db_errors("reading cards"):
            rows = self._conn.execute(sql, params).fetchall()
        return [(row["deck_name"], _to_card(row)) for row in rows]

    # --- reviews ---

    def save_review(
        self,
        card_id: int,
        state: ReviewState,
        grade: int,
        *,
        mode: str | None = None,
        given_answer: str | None = None,
    ) -> bool:
        """Store the card's new state and log the review, in one transaction.

        `mode` ('write' or 'choice') and `given_answer` (the typed or chosen text, '' for an
        empty answer) are logged with the review; None means "not recorded".

        The review is logged on `state.last_reviewed`, and it only applies while the card is
        still due on that day. Returns False (nothing saved) when it is not: a repeated or
        parallel submit of the same answer therefore logs one review, not several. Raises
        StorageError for an unknown card, a state without `last_reviewed`, a grade outside
        0..5 or an unknown mode; nothing is saved then.
        """
        if state.last_reviewed is None:
            raise StorageError("a reviewed state needs last_reviewed")
        reviewed_on = _iso(state.last_reviewed)
        with _db_errors("saving a review"), self._conn:
            # The write lock is taken before the row is read, so a parallel save waits and then
            # sees the new due date. `due_date <= ?` makes the update (and the log row) happen
            # at most once per due date.
            cursor = self._conn.execute(
                "UPDATE cards SET ease_factor = ?, interval_days = ?, repetitions = ?,"
                " due_date = ?, last_reviewed = ? WHERE id = ? AND due_date <= ?",
                (
                    state.ease_factor,
                    state.interval_days,
                    state.repetitions,
                    _iso(state.due_date),
                    reviewed_on,
                    card_id,
                    reviewed_on,
                ),
            )
            if cursor.rowcount != 1:
                exists = self._conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,))
                if exists.fetchone() is None:
                    raise StorageError(f"no card with id {card_id}")
                return False
            self._conn.execute(
                "INSERT INTO reviews (card_id, reviewed_on, grade, mode, given_answer)"
                " VALUES (?, ?, ?, ?, ?)",
                (card_id, reviewed_on, grade, mode, given_answer),
            )
        return True

    def recent_answers(self, deck_id: int, limit: int = 20) -> list[ReviewEntry]:
        """The deck's latest reviews, newest first (same-day reviews: the last saved first).

        Reviews from before version 2 are included, with `mode` and `given_answer` None.
        """
        if limit < 0:
            raise ValueError(f"limit must not be negative, got {limit}")
        with _db_errors("reading recent answers"):
            rows = self._conn.execute(
                """
                SELECT r.reviewed_on, r.mode, r.given_answer, r.grade,
                       c.id AS card_id, c.question, c.answer
                FROM reviews r JOIN cards c ON c.id = r.card_id
                WHERE c.deck_id = ?
                ORDER BY r.reviewed_on DESC, r.rowid DESC
                LIMIT ?
                """,
                (deck_id, limit),
            ).fetchall()
        return [_to_review_entry(row) for row in rows]

    # --- statistics ---

    def stats(self, today: date, deck_id: int | None = None) -> Stats:
        """Card and review numbers for one deck, or for all decks when `deck_id` is None.

        `next_due` is the earliest due date after `today` (None when there is none).
        """
        day = _iso(today)
        scope = "" if deck_id is None else " AND deck_id = ?"
        scope_params: list[object] = [] if deck_id is None else [deck_id]
        with _db_errors("reading statistics"):
            card_row = self._conn.execute(
                f"""
                SELECT count(*) AS total,
                       coalesce(sum(last_reviewed IS NULL), 0) AS new_cards,
                       coalesce(sum(due_date <= ?), 0) AS due_today,
                       coalesce(sum(last_reviewed = ?), 0) AS reviewed_today,
                       coalesce(sum(interval_days >= ?), 0) AS mature,
                       avg(ease_factor) AS average_ease,
                       min(CASE WHEN due_date > ? THEN due_date END) AS next_due
                FROM cards WHERE 1 = 1{scope}
                """,
                [day, day, MATURE_INTERVAL_DAYS, day, *scope_params],
            ).fetchone()
            review_row = self._conn.execute(
                f"""
                SELECT coalesce(sum(r.reviewed_on = ?), 0) AS reviews_today,
                       coalesce(sum(r.reviewed_on = ? AND r.grade >= ?), 0) AS correct_today,
                       count(r.card_id) AS reviews_total,
                       coalesce(sum(r.grade >= ?), 0) AS correct_total
                FROM reviews r JOIN cards ON cards.id = r.card_id WHERE 1 = 1{scope}
                """,
                [day, day, PASS_GRADE, PASS_GRADE, *scope_params],
            ).fetchone()
        return Stats(
            total_cards=card_row["total"],
            new_cards=card_row["new_cards"],
            due_today=card_row["due_today"],
            reviewed_today=card_row["reviewed_today"],
            mature_cards=card_row["mature"],
            average_ease=card_row["average_ease"],
            next_due=_parse_date(card_row["next_due"]),
            reviews_today=review_row["reviews_today"],
            correct_today=review_row["correct_today"],
            reviews_total=review_row["reviews_total"],
            correct_total=review_row["correct_total"],
        )
