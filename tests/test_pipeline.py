from datetime import date, datetime
from pathlib import Path

import pytest

from flashcards.errors import ApiKeyError, GenerationError, IngestError
from flashcards.ingest import load_chunks
from flashcards.models import CardDraft
from flashcards.pipeline import generate_deck
from flashcards.storage import CardStore, open_db

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
SAMPLE_FILES = ["biology.md", "mathematics.md", "programming.md", "english.txt", "physics.txt"]
TODAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 9, 0, 0)


@pytest.fixture
def store():
    with CardStore(open_db(":memory:")) as card_store:
        yield card_store


def section(number: int, extra: str = "") -> str:
    """About 3300 characters, so two sections never share a chunk (the limit is 4000)."""
    return f"# Section {number}\n\n" + f"Fact {number} is written in the notes. " * 100 + extra


def write_notes(tmp_path, *sections, name="biology.md"):
    path = tmp_path / name
    path.write_text("\n\n".join(sections), encoding="utf-8")
    return path


class FakeSource:
    """A card source that records the chunk texts and answers from a queue or a function."""

    def __init__(self, *outcomes):
        self.calls = []
        self._outcomes = list(outcomes)

    def __call__(self, chunk_text):
        self.calls.append(chunk_text)
        if not self._outcomes:
            number = chunk_text.split("\n")[0].removeprefix("# Section ")
            return [CardDraft(f"Question about {number}?", f"Answer about {number}.")]
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run(store, path, source, **kwargs):
    return generate_deck(path, store, source, today=TODAY, now=NOW, **kwargs)


def questions(store, deck_name):
    deck = store.find_deck(deck_name)
    return [card.question for card in store.due_cards(TODAY, deck.id)]


def test_chunks_become_cards_in_a_deck_named_after_the_file(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2), section(3))
    source = FakeSource()

    report = run(store, path, source)

    assert len(source.calls) == 3
    assert questions(store, "biology") == [
        "Question about 1?",
        "Question about 2?",
        "Question about 3?",
    ]
    assert (report.deck_name, report.chunks_total, report.chunks_generated) == ("biology", 3, 3)
    assert (report.chunks_skipped, report.chunks_empty, report.cards_added) == (0, 0, 3)
    assert not report.up_to_date
    assert store.find_deck("biology").source_path == str(path)


def test_deck_name_option_overrides_the_file_stem(store, tmp_path):
    path = write_notes(tmp_path, section(1))

    report = run(store, path, FakeSource(), deck_name="Life Science")

    assert report.deck_name == "Life Science"
    assert store.find_deck("biology") is None
    assert questions(store, "Life Science") == ["Question about 1?"]


def test_second_run_makes_no_calls(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2))
    run(store, path, FakeSource())
    source = FakeSource()

    report = run(store, path, source)

    assert source.calls == []
    assert report.up_to_date
    assert (report.chunks_skipped, report.chunks_generated, report.cards_added) == (2, 0, 0)
    assert len(questions(store, "biology")) == 2


def test_after_editing_one_section_only_that_chunk_is_sent(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2), section(3))
    run(store, path, FakeSource())
    edited = write_notes(tmp_path, section(1), section(2, "An edited sentence."), section(3))
    source = FakeSource([CardDraft("Question about edited 2?", "Edited answer.")])

    report = run(store, edited, source)

    assert len(source.calls) == 1
    assert "An edited sentence." in source.calls[0]
    assert (report.chunks_skipped, report.chunks_generated, report.cards_added) == (2, 1, 1)
    assert len(questions(store, "biology")) == 4


def test_key_error_is_raised_as_is_without_chunk_context(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2))

    with pytest.raises(ApiKeyError) as excinfo:
        run(store, path, FakeSource(ApiKeyError("key missing")))

    assert str(excinfo.value) == "key missing"
    assert questions(store, "biology") == []


def test_error_keeps_earlier_chunks_and_next_run_resumes_there(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2), section(3))
    failing = FakeSource(
        [CardDraft("Question about 1?", "Answer about 1.")], GenerationError("API is down")
    )

    with pytest.raises(GenerationError) as excinfo:
        run(store, path, failing)

    message = str(excinfo.value)
    assert message.startswith("chunk 2/3: API is down")
    assert "1 chunk saved" in message
    assert "run the same command again to continue" in message
    assert len(failing.calls) == 2  # chunk 3 was never tried
    assert questions(store, "biology") == ["Question about 1?"]

    resumed = FakeSource()
    report = run(store, path, resumed)

    assert len(resumed.calls) == 2
    assert resumed.calls[0].startswith("# Section 2")
    assert (report.chunks_skipped, report.chunks_generated) == (1, 2)
    assert len(questions(store, "biology")) == 3


def test_error_message_counts_several_saved_chunks(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2), section(3))
    source = FakeSource(
        [CardDraft("Q1?", "A1.")], [CardDraft("Q2?", "A2.")], GenerationError("cut off")
    )

    with pytest.raises(GenerationError, match=r"chunk 3/3: cut off - 2 chunks saved"):
        run(store, path, source)


def test_chunk_with_no_cards_is_retried_on_the_next_run(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2))
    first = FakeSource([], [CardDraft("Q2?", "A2.")])

    report = run(store, path, first)

    assert (report.chunks_generated, report.chunks_empty, report.cards_added) == (1, 1, 1)
    assert not report.up_to_date

    second = FakeSource([CardDraft("Q1?", "A1.")])
    report = run(store, path, second)

    assert len(second.calls) == 1  # only the empty chunk is tried again
    assert second.calls[0].startswith("# Section 1")
    assert (report.chunks_skipped, report.chunks_generated, report.chunks_empty) == (1, 1, 0)
    assert report.up_to_date is False
    assert sorted(questions(store, "biology")) == ["Q1?", "Q2?"]


def test_chunk_whose_cards_are_all_duplicates_still_counts_as_done(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2))
    run(store, path, FakeSource([CardDraft("Same?", "Yes.")], [CardDraft("Other?", "Yes.")]))
    other = write_notes(tmp_path, section(3), section(4), name="other.md")
    # A second file into the same deck: its only card repeats an existing question.
    source = FakeSource([CardDraft("same?", "Yes.")], [CardDraft("New?", "Yes.")])

    report = run(store, other, source, deck_name="biology")

    assert report.cards_added == 1
    assert report.chunks_generated == 2
    assert run(store, other, FakeSource(), deck_name="biology").up_to_date


def test_progress_lines_are_reported_per_chunk(store, tmp_path):
    path = write_notes(tmp_path, section(1), section(2), section(3))
    lines = []
    first = FakeSource([CardDraft("Q1?", "A1.")], [], GenerationError("stop"))
    with pytest.raises(GenerationError):
        run(store, path, first, on_progress=lines.append)
    assert lines == [
        "chunk 1/3: 1 new card",
        "chunk 2/3: no cards returned, will be retried next run",
    ]

    lines.clear()
    run(store, path, FakeSource(), on_progress=lines.append)
    assert lines == [
        "chunk 1/3: skipped (already done)",
        "chunk 2/3: 1 new card",
        "chunk 3/3: 1 new card",
    ]


def test_missing_notes_file_raises_ingest_error_before_creating_a_deck(store, tmp_path):
    with pytest.raises(IngestError):
        run(store, tmp_path / "missing.md", FakeSource())

    assert store.deck_summaries(TODAY) == []


@pytest.mark.parametrize("filename", SAMPLE_FILES)
def test_sample_notes_files_read_and_chunk(store, filename):
    chunks = load_chunks(EXAMPLES / filename)

    assert chunks
    assert all(chunk.text.strip() for chunk in chunks)

    report = run(store, EXAMPLES / filename, FakeSource([CardDraft("Q?", "A.")] * len(chunks)))

    assert report.deck_name == Path(filename).stem
    assert report.chunks_total == len(chunks)
