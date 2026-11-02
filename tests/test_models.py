import pytest

from flashcards.models import Stats


def make_stats(**overrides) -> Stats:
    values = dict(
        total_cards=0,
        new_cards=0,
        due_today=0,
        reviewed_today=0,
        mature_cards=0,
        average_ease=None,
        next_due=None,
        reviews_today=0,
        correct_today=0,
        reviews_total=0,
        correct_total=0,
    )
    return Stats(**{**values, **overrides})


def test_accuracy_is_none_without_reviews():
    stats = make_stats()
    assert stats.accuracy_today is None
    assert stats.accuracy_all_time is None


def test_accuracy_with_three_reviews_two_correct():
    stats = make_stats(reviews_today=3, correct_today=2, reviews_total=4, correct_total=2)
    assert stats.accuracy_today == pytest.approx(2 / 3)
    assert stats.accuracy_all_time == pytest.approx(0.5)
