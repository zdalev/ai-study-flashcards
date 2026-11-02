"""Fill the database from a notes file. The card source is injected, so this never needs the API."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from flashcards.errors import GenerationError
from flashcards.ingest import load_chunks
from flashcards.models import CardDraft
from flashcards.storage import CardStore

# Takes the text of one chunk and returns card drafts (generator.generate_cards, or a fake).
CardSource = Callable[[str], list[CardDraft]]


@dataclass(frozen=True)
class GenerateReport:
    deck_name: str
    chunks_total: int
    chunks_skipped: int  # already done in an earlier run
    chunks_generated: int  # sent to the card source and saved
    chunks_empty: int  # sent, but no cards came back: not recorded, retried next run
    cards_added: int  # new cards saved (duplicates of existing questions are not counted)

    @property
    def up_to_date(self) -> bool:
        return self.chunks_skipped == self.chunks_total


def generate_deck(
    notes_path: Path | str,
    store: CardStore,
    card_source: CardSource,
    *,
    deck_name: str | None = None,
    today: date,
    now: datetime,
    on_progress: Callable[[str], None] | None = None,
) -> GenerateReport:
    """Turn a notes file into cards in a deck (named after the file unless `deck_name` is given).

    Chunks already stored for the deck are skipped, so a second run makes no calls. Each new
    chunk is saved on its own. The first GenerationError stops the run and is raised again with
    the chunk position; chunks saved before it stay saved, and the same command continues there.
    """
    notes_path = Path(notes_path)
    chunks = load_chunks(notes_path)
    deck = store.get_or_create_deck(deck_name or notes_path.stem, str(notes_path), now)
    notify = on_progress or (lambda _message: None)

    skipped = generated = empty = added = 0
    total = len(chunks)
    for number, chunk in enumerate(chunks, start=1):
        label = f"chunk {number}/{total}"
        if store.has_chunk(deck.id, chunk.digest):
            skipped += 1
            notify(f"{label}: skipped (already done)")
            continue
        try:
            drafts = card_source(chunk.text)
        except GenerationError as error:
            saved = generated
            noun = "chunk" if saved == 1 else "chunks"
            raise GenerationError(
                f"{label}: {error} - {saved} {noun} saved, run the same command again to continue"
            ) from error
        if not drafts:
            # No digest: add_cards would record the chunk as done and it would never be retried.
            empty += 1
            notify(f"{label}: no cards returned, will be retried next run")
            continue
        new_cards = store.add_cards(deck.id, drafts, today, now, chunk_digest=chunk.digest)
        generated += 1
        added += new_cards
        notify(f"{label}: {new_cards} new cards")
    return GenerateReport(deck.name, total, skipped, generated, empty, added)
