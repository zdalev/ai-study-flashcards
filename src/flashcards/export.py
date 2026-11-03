"""CSV export of all cards (or one deck) with their scheduling state."""

import csv
from typing import TextIO

from flashcards.storage import CardStore

COLUMNS = (
    "deck",
    "question",
    "answer",
    "ease_factor",
    "interval_days",
    "repetitions",
    "due_date",
    "last_reviewed",
)


def export_csv(store: CardStore, out: TextIO, deck_id: int | None = None) -> int:
    """Write a header row and one row per card to `out`; returns the number of cards written.

    Cards are ordered by deck name, then card id. `out` must be a text stream opened with
    `newline=""` (the csv module writes its own `\\r\\n` line ends) and UTF-8 encoding. Dates are
    ISO `YYYY-MM-DD`; `last_reviewed` is empty for a card that was never reviewed.
    """
    rows = store.cards_with_deck_names(deck_id)  # read everything first: no half-written output
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for deck_name, card in rows:
        state = card.state
        writer.writerow(
            [
                deck_name,
                card.question,
                card.answer,
                state.ease_factor,
                state.interval_days,
                state.repetitions,
                state.due_date.isoformat(),
                state.last_reviewed.isoformat() if state.last_reviewed else "",
            ]
        )
    return len(rows)
