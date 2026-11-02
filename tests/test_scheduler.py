from dataclasses import replace
from datetime import date, timedelta

import pytest

from flashcards.models import ReviewState
from flashcards.scheduler import is_due, new_review_state, review, round_half_up

START = date(2026, 1, 1)


def run_reviews(grades, start=START):
    """Review a new card on each due date. Returns the list of states after each review."""
    state = new_review_state(start)
    today = start
    states = []
    for grade in grades:
        state = review(state, grade, today)
        states.append(state)
        today = state.due_date
    return states


class TestNewReviewState:
    def test_is_due_today_with_defaults(self):
        state = new_review_state(START)
        assert state == ReviewState(due_date=START)
        assert state.ease_factor == 2.5
        assert state.interval_days == 0
        assert state.repetitions == 0
        assert state.last_reviewed is None


class TestSequences:
    @pytest.mark.parametrize(
        ("grades", "intervals", "eases", "due_dates"),
        [
            (
                [5, 5, 5, 5, 5],
                [1, 6, 16, 45, 131],
                [2.6, 2.7, 2.8, 2.9, 3.0],
                [(1, 2), (1, 8), (1, 24), (3, 10), (7, 19)],
            ),
            ([4, 4, 4, 4, 4], [1, 6, 15, 38, 95], [2.5] * 5, None),
            ([3, 3, 3, 3], [1, 6, 13, 27], [2.36, 2.22, 2.08, 1.94], None),
        ],
        ids=["all-5", "all-4", "all-3"],
    )
    def test_documented_sequences(self, grades, intervals, eases, due_dates):
        states = run_reviews(grades)
        assert [s.interval_days for s in states] == intervals
        assert [s.ease_factor for s in states] == eases
        assert [s.repetitions for s in states] == list(range(1, len(grades) + 1))
        if due_dates is not None:
            assert [s.due_date for s in states] == [date(2026, m, d) for m, d in due_dates]

    def test_due_date_is_review_day_plus_interval_and_last_reviewed_is_set(self):
        states = run_reviews([5, 5, 5])
        reviewed_on = [START, states[0].due_date, states[1].due_date]
        for state, day in zip(states, reviewed_on, strict=True):
            assert state.last_reviewed == day
            assert state.due_date == day + timedelta(days=state.interval_days)

    def test_late_review_counts_from_the_review_day(self):
        state = review(new_review_state(START), 5, date(2026, 1, 10))
        assert state.due_date == date(2026, 1, 11)
        assert state.last_reviewed == date(2026, 1, 10)


class TestFailure:
    def test_failure_resets_repetitions_and_interval(self):
        state = ReviewState(
            due_date=START, ease_factor=2.8, interval_days=16, repetitions=3, last_reviewed=START
        )
        result = review(state, 2, START)
        assert result.repetitions == 0
        assert result.interval_days == 1
        assert result.ease_factor == 2.48
        assert result.due_date == START + timedelta(days=1)

    def test_recovery_after_failure_restarts_at_1_then_6(self):
        failed = ReviewState(
            due_date=START, ease_factor=2.8, interval_days=16, repetitions=3, last_reviewed=START
        )
        state = review(failed, 2, START)
        first = review(state, 4, state.due_date)
        second = review(first, 4, first.due_date)
        assert first.interval_days == 1
        assert second.interval_days == 6

    def test_grade_3_is_the_lowest_pass(self):
        state = ReviewState(due_date=START, interval_days=6, repetitions=2)
        assert review(state, 3, START).repetitions == 3
        assert review(state, 2, START).repetitions == 0


class TestEaseFactor:
    @pytest.mark.parametrize(
        ("grade", "expected"),
        [(5, 2.6), (4, 2.5), (3, 2.36), (2, 2.18), (1, 1.96), (0, 1.7)],
    )
    def test_one_grade_from_default_ease(self, grade, expected):
        assert review(new_review_state(START), grade, START).ease_factor == expected

    def test_ease_never_drops_below_1_3(self):
        state = replace(new_review_state(START), ease_factor=1.4)
        assert review(state, 0, START).ease_factor == 1.3

    def test_ease_stays_at_floor_after_repeated_failures(self):
        state = new_review_state(START)
        for _ in range(10):
            state = review(state, 0, START)
        assert state.ease_factor == 1.3

    def test_ease_has_at_most_two_decimals(self):
        for state in run_reviews([3, 5, 4, 3, 2, 5, 1, 4]):
            assert state.ease_factor == round(state.ease_factor, 2)


class TestIntervals:
    def test_uses_ease_from_before_the_review(self):
        # 45 * 2.9 = 130.5: the old EF (2.9) is used, not the new one (3.0)
        state = ReviewState(due_date=START, ease_factor=2.9, interval_days=45, repetitions=4)
        assert review(state, 5, START).interval_days == 131

    def test_half_rounds_up_not_to_even(self):
        # Python's round(130.5) is 130
        assert round(130.5) == 130
        assert round_half_up(130.5) == 131

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0.5, 1), (1.5, 2), (2.5, 3), (2.4, 2), (2.6, 3), (130.5, 131), (16.2, 16), (44.8, 45)],
    )
    def test_round_half_up(self, value, expected):
        assert round_half_up(value) == expected

    def test_interval_grows_with_a_fractional_ease(self):
        state = ReviewState(due_date=START, ease_factor=1.3, interval_days=7, repetitions=2)
        assert review(state, 4, START).interval_days == 9  # 9.1


class TestGradeValidation:
    @pytest.mark.parametrize("grade", [-1, 6, 2.5, 3.0, True, False, "3", None])
    def test_invalid_grades_are_rejected(self, grade):
        with pytest.raises(ValueError, match="grade"):
            review(new_review_state(START), grade, START)

    @pytest.mark.parametrize("grade", range(6))
    def test_valid_grades_are_accepted(self, grade):
        review(new_review_state(START), grade, START)


class TestPurity:
    def test_input_state_is_not_changed(self):
        state = new_review_state(START)
        snapshot = replace(state)
        result = review(state, 5, START)
        assert state == snapshot
        assert result is not state

    def test_same_input_gives_same_output(self):
        state = new_review_state(START)
        assert review(state, 4, START) == review(state, 4, START)


class TestIsDue:
    def test_due_on_and_after_the_due_date(self):
        state = ReviewState(due_date=date(2026, 1, 5))
        assert is_due(state, date(2026, 1, 5))
        assert is_due(state, date(2026, 1, 9))

    def test_not_due_before_the_due_date(self):
        assert not is_due(ReviewState(due_date=date(2026, 1, 5)), date(2026, 1, 4))

    def test_new_card_is_due_today(self):
        assert is_due(new_review_state(START), START)
