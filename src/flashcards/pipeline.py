"""Fill the database from a notes file. The card source is injected, so this never needs the API."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from flashcards.errors import ApiKeyError, DemoDataError, GenerationError, StorageError
from flashcards.ingest import load_chunks
from flashcards.models import CardDraft
from flashcards.storage import CardStore, normalize_text

DEMO_VERSION = 1

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
    the chunk position (an ApiKeyError is raised as is); chunks saved before it stay saved, and
    the same command continues there. The deck is created only when its first cards are saved,
    so a run that fails before that (no API key, API error) leaves no empty deck behind.
    """
    notes_path = Path(notes_path)
    chunks = load_chunks(notes_path)
    name = normalize_text(deck_name or notes_path.stem)
    if not name:
        raise StorageError("deck name must not be empty")
    deck = store.find_deck(name)
    notify = on_progress or (lambda _message: None)

    skipped = generated = empty = added = 0
    total = len(chunks)
    for number, chunk in enumerate(chunks, start=1):
        label = f"chunk {number}/{total}"
        if deck is not None and store.has_chunk(deck.id, chunk.digest):
            skipped += 1
            notify(f"{label}: skipped (already done)")
            continue
        try:
            drafts = card_source(chunk.text)
        except ApiKeyError:
            raise  # a key problem has nothing to do with this chunk: keep the one-line message
        except GenerationError as error:
            saved = generated
            raise GenerationError(
                f"{label}: {error} - {count_noun(saved, 'chunk')} saved, "
                "run the same command again to continue"
            ) from error
        if not drafts:
            # No digest: add_cards would record the chunk as done and it would never be retried.
            empty += 1
            notify(f"{label}: no cards returned, will be retried next run")
            continue
        if deck is None:
            deck = store.get_or_create_deck(name, str(notes_path), now)
        new_cards = store.add_cards(deck.id, drafts, today, now, chunk_digest=chunk.digest)
        generated += 1
        added += new_cards
        notify(f"{label}: {count_noun(new_cards, 'new card')}")
    return GenerateReport(name, total, skipped, generated, empty, added)


def count_noun(number: int, noun: str) -> str:
    """'1 card' / '2 cards'."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def build_demo_data(paths: Sequence[Path | str], card_source: CardSource) -> dict:
    """Run the card source over every chunk of each notes file and return the demo-deck dict.

    One deck per file, named after the file stem; `source` is the path as given. A file that
    yields no cards at all raises GenerationError (a demo deck must not be empty). Nothing is
    deduplicated here - `load_demo` skips repeated questions.
    """
    decks = []
    for path in map(Path, paths):
        cards = [
            {"question": draft.question, "answer": draft.answer}
            for chunk in load_chunks(path)
            for draft in card_source(chunk.text)
        ]
        if not cards:
            raise GenerationError(f"no cards were generated from {path}")
        decks.append({"name": path.stem, "source": path.as_posix(), "cards": cards})
    return {"version": DEMO_VERSION, "decks": decks}


def load_demo(store: CardStore, data: object, today: date, now: datetime) -> int:
    """Add the demo decks to the store (all cards due `today`) and return how many cards are new.

    The whole shape is checked first, so a bad file changes nothing. Decks that exist are reused
    and questions already in a deck are skipped, so a second run returns 0.
    """
    decks = _validated_demo_decks(data)
    added = 0
    for name, source, drafts in decks:
        deck = store.get_or_create_deck(name, source, now)
        added += store.add_cards(deck.id, drafts, today, now)
    return added


def _validated_demo_decks(data: object) -> list[tuple[str, str | None, list[CardDraft]]]:
    if not isinstance(data, dict) or data.get("version") != DEMO_VERSION:
        raise DemoDataError(f'demo data must be an object with "version": {DEMO_VERSION}')
    entries = data.get("decks")
    if not isinstance(entries, list) or not entries:
        raise DemoDataError('demo data needs a non-empty "decks" list')
    decks = []
    for position, entry in enumerate(entries, start=1):
        where = f"deck {position}"
        if not isinstance(entry, dict):
            raise DemoDataError(f"{where} must be an object")
        name, source, cards = entry.get("name"), entry.get("source"), entry.get("cards")
        if not isinstance(name, str) or not name.strip():
            raise DemoDataError(f'{where} needs a non-empty "name"')
        if source is not None and not isinstance(source, str):
            raise DemoDataError(f'{where} ("{name}"): "source" must be text')
        if not isinstance(cards, list):
            raise DemoDataError(f'{where} ("{name}") needs a "cards" list')
        drafts = [
            _validated_demo_card(card, f'{where} ("{name}")', n) for n, card in enumerate(cards, 1)
        ]
        decks.append((name, source, drafts))
    return decks


def _validated_demo_card(card: object, where: str, number: int) -> CardDraft:
    if not isinstance(card, dict):
        raise DemoDataError(f"{where}, card {number} must be an object")
    question, answer = card.get("question"), card.get("answer")
    if not all(isinstance(text, str) and text.strip() for text in (question, answer)):
        raise DemoDataError(f"{where}, card {number} needs a non-empty question and answer")
    return CardDraft(question=question, answer=answer)
