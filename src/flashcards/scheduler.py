"""SM-2 spaced-repetition scheduler. Pure functions: "today" is always passed in, no I/O."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from flashcards.models import ReviewState

DEFAULT_EASE = 2.5
MIN_EASE = 1.3
PASS_GRADE = 3  # grade >= 3 counts as a correct review
FIRST_INTERVAL_DAYS = 1
SECOND_INTERVAL_DAYS = 6


def new_review_state(today: date) -> ReviewState:
    """State of a card that was never reviewed: due today."""
    return ReviewState(due_date=today)


def is_due(state: ReviewState, today: date) -> bool:
    return state.due_date <= today


def review(state: ReviewState, grade: int, today: date) -> ReviewState:
    """Return the state after answering with `grade` (0..5) on `today`.

    Raises ValueError for anything that is not an int from 0 to 5 (bool is rejected too).
    """
    _check_grade(grade)
    ease = state.ease_factor
    if grade >= PASS_GRADE:
        interval = _next_interval(state)
        repetitions = state.repetitions + 1
    else:
        interval = FIRST_INTERVAL_DAYS
        repetitions = 0
    return replace(
        state,
        due_date=today + timedelta(days=interval),
        ease_factor=_next_ease(ease, grade),
        interval_days=interval,
        repetitions=repetitions,
        last_reviewed=today,
    )


def _check_grade(grade: object) -> None:
    if isinstance(grade, bool) or not isinstance(grade, int) or not 0 <= grade <= 5:
        raise ValueError(f"grade must be an int from 0 to 5, got {grade!r}")


def _next_interval(state: ReviewState) -> int:
    if state.repetitions == 0:
        return FIRST_INTERVAL_DAYS
    if state.repetitions == 1:
        return SECOND_INTERVAL_DAYS
    # Uses the EF from before this review. Decimal keeps the product exact.
    return round_half_up(Decimal(str(state.ease_factor)) * state.interval_days)


def _next_ease(ease: float, grade: int) -> float:
    miss = 5 - grade
    new_ease = round(ease + 0.1 - miss * (0.08 + miss * 0.02), 2)
    return max(MIN_EASE, new_ease)


def round_half_up(value: Decimal | float) -> int:
    """Round to the nearest int, halves upwards (Python's round() rounds halves to even)."""
    exact = value if isinstance(value, Decimal) else Decimal(str(value))
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))
