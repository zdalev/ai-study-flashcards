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
    ReviewState,
    Stats,
)
from flashcards.scheduler import PASS_GRADE

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
BEGIN;
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
PRAGMA user_version = 1;
COMMIT;
"""

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
    the v1 schema is applied when the file is new. Opening an existing database is a no-op.
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
            conn.executescript(_SCHEMA_V1)
        except sqlite3.Error:
            # executescript leaves its BEGIN open when a statement fails.
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

    # --- reviews ---

    def save_review(self, card_id: int, state: ReviewState, grade: int) -> bool:
        """Store the card's new state and log the review, in one transaction.

        The review is logged on `state.last_reviewed`, and it only applies while the card is
        still due on that day. Returns False (nothing saved) when it is not: a repeated or
        parallel submit of the same answer therefore logs one review, not several. Raises
        StorageError for an unknown card, a state without `last_reviewed`, or a grade outside
        0..5; nothing is saved then.
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
                "INSERT INTO reviews (card_id, reviewed_on, grade) VALUES (?, ?, ?)",
                (card_id, reviewed_on, grade),
            )
        return True

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
