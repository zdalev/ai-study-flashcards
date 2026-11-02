"""Plain data shapes shared by all modules. Pure data, no I/O."""

from dataclasses import dataclass
from datetime import date

MATURE_INTERVAL_DAYS = 21


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    digest: str  # sha256 of whitespace-normalised text


@dataclass(frozen=True)
class CardDraft:
    """A question/answer pair from the LLM or the demo data, before storage."""

    question: str
    answer: str


@dataclass(frozen=True)
class ReviewState:
    due_date: date
    ease_factor: float = 2.5  # EF, >= 1.3, 2 decimals
    interval_days: int = 0  # 0 = never reviewed
    repetitions: int = 0  # correct reviews in a row (grade >= 3)
    last_reviewed: date | None = None


@dataclass(frozen=True)
class Card:
    id: int
    deck_id: int
    question: str
    answer: str
    state: ReviewState


@dataclass(frozen=True)
class Deck:
    id: int
    name: str
    source_path: str | None


@dataclass(frozen=True)
class DeckSummary:
    deck: Deck
    cards: int
    due: int


def _accuracy(correct: int, total: int) -> float | None:
    return correct / total if total else None


@dataclass(frozen=True)
class Stats:
    total_cards: int
    new_cards: int
    due_today: int
    reviewed_today: int
    mature_cards: int  # interval >= MATURE_INTERVAL_DAYS
    average_ease: float | None
    next_due: date | None
    reviews_today: int
    correct_today: int  # correct = grade >= 3
    reviews_total: int
    correct_total: int

    @property
    def accuracy_today(self) -> float | None:
        """Share of correct reviews today, None when there were none."""
        return _accuracy(self.correct_today, self.reviews_today)

    @property
    def accuracy_all_time(self) -> float | None:
        """Share of correct reviews ever, None when there were none."""
        return _accuracy(self.correct_total, self.reviews_total)
