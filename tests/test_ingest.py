import hashlib

import pytest

from flashcards.errors import FlashcardsError, IngestError
from flashcards.ingest import chunk_text, digest_of, load_chunks, read_notes
from flashcards.models import Chunk


def squash(text: str) -> str:
    """All whitespace removed, to compare content regardless of how chunks are joined."""
    return "".join(text.split())


def sample_notes() -> str:
    paragraphs = [f"Paragraph {i}. " + " ".join(["word"] * 40) for i in range(30)]
    return "\n\n".join(paragraphs)


class TestChunkText:
    def test_short_text_is_one_chunk(self):
        assert chunk_text("Just one short paragraph.") == ["Just one short paragraph."]

    def test_empty_and_blank_text_give_no_chunks(self):
        assert chunk_text("") == []
        assert chunk_text("  \n\n \t\n") == []

    @pytest.mark.parametrize("max_chars", [0, 1, 199, -5])
    def test_max_chars_below_200_is_rejected(self, max_chars):
        with pytest.raises(ValueError):
            chunk_text("text", max_chars=max_chars)

    def test_max_chars_200_is_accepted(self):
        assert chunk_text("text", max_chars=200) == ["text"]

    def test_paragraphs_that_fit_are_packed_together(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        assert chunk_text(text, max_chars=4000) == [text]

    def test_paragraphs_are_never_split_when_they_fit(self):
        text = sample_notes()
        paragraphs = text.split("\n\n")
        chunks = chunk_text(text, max_chars=1000)
        assert len(chunks) > 1
        for chunk in chunks:
            assert set(chunk.split("\n\n")) <= set(paragraphs)

    def test_chunks_respect_the_limit_and_keep_all_content_in_order(self):
        text = sample_notes()
        for max_chars in (200, 500, 1000, 4000):
            chunks = chunk_text(text, max_chars=max_chars)
            assert all(chunk for chunk in chunks)
            assert all(len(chunk) <= max_chars for chunk in chunks)
            assert squash("".join(chunks)) == squash(text)

    def test_heading_stays_with_the_paragraph_after_it(self):
        filler = "x" * 180
        text = f"{filler}\n\n## Heading\n\nBody under the heading."
        chunks = chunk_text(text, max_chars=200)
        assert chunks == [filler, "## Heading\n\nBody under the heading."]

    def test_consecutive_headings_stay_with_the_next_paragraph(self):
        filler = "x" * 180
        text = f"{filler}\n\n# Title\n\n## Section\n\nBody."
        chunks = chunk_text(text, max_chars=200)
        assert chunks[-1] == "# Title\n\n## Section\n\nBody."

    def test_heading_at_the_end_is_kept(self):
        assert chunk_text("Body.\n\n# Last heading") == ["Body.\n\n# Last heading"]

    def test_hash_without_space_is_not_a_heading(self):
        filler = "x" * 190
        chunks = chunk_text(f"{filler}\n\n#hashtag\n\nBody.", max_chars=200)
        assert chunks == [f"{filler}\n\n#hashtag", "Body."]

    def test_long_paragraph_is_split_at_line_boundaries(self):
        lines = [f"line {i:03d} " + "a" * 41 for i in range(200)]
        text = "\n".join(lines)
        assert len(text) > 10_000
        chunks = chunk_text(text, max_chars=1000)
        assert all(len(chunk) <= 1000 for chunk in chunks)
        for chunk in chunks:
            assert set(chunk.split("\n")) <= set(lines)
        assert squash("".join(chunks)) == squash(text)

    def test_long_single_line_is_split_at_sentence_boundaries(self):
        sentences = [f"Sentence number {i:03d} says something." for i in range(300)]
        text = " ".join(sentences)
        assert len(text) > 10_000
        chunks = chunk_text(text, max_chars=1000)
        assert len(chunks) > 1
        assert all(len(chunk) <= 1000 for chunk in chunks)
        for chunk in chunks:
            assert chunk.endswith("says something.")
            assert chunk.startswith("Sentence number")
        assert squash("".join(chunks)) == squash(text)

    def test_long_word_is_hard_split(self):
        text = "a" * 10_000
        chunks = chunk_text(text, max_chars=1000)
        assert len(chunks) == 10
        assert all(len(chunk) == 1000 for chunk in chunks)
        assert "".join(chunks) == text

    def test_hard_split_after_normal_text_keeps_order(self):
        text = "Intro paragraph.\n\n" + "b" * 450
        chunks = chunk_text(text, max_chars=200)
        assert chunks[0] == "Intro paragraph."
        assert "".join(chunks[1:]) == "b" * 450
        assert all(len(chunk) <= 200 for chunk in chunks)

    def test_crlf_gives_the_same_chunks_as_lf(self):
        text = sample_notes() + "\n\n# End\n\nLast words."
        assert chunk_text(text.replace("\n", "\r\n"), 800) == chunk_text(text, 800)

    def test_blank_lines_with_spaces_still_separate_paragraphs(self):
        filler = "x" * 150
        chunks = chunk_text(f"{filler}\n  \t\n{filler}", max_chars=200)
        assert chunks == [filler, filler]


class TestDigest:
    def test_is_sha256_of_whitespace_collapsed_text(self):
        expected = hashlib.sha256(b"alpha beta gamma").hexdigest()
        assert digest_of("alpha beta gamma") == expected

    def test_ignores_whitespace_only_changes(self):
        assert digest_of("alpha  beta\n\ngamma ") == digest_of(" alpha beta\tgamma")

    def test_changes_when_words_change(self):
        assert digest_of("alpha beta") != digest_of("alpha betA")


class TestReadNotes:
    @pytest.mark.parametrize("name", ["n.txt", "n.md", "n.markdown", "N.MD"])
    def test_supported_extensions(self, tmp_path, name):
        path = tmp_path / name
        path.write_text("hello", encoding="utf-8")
        assert read_notes(path) == "hello"

    def test_accepts_a_string_path(self, tmp_path):
        path = tmp_path / "n.txt"
        path.write_text("hello", encoding="utf-8")
        assert read_notes(str(path)) == "hello"

    def test_crlf_is_normalised_and_bom_removed(self, tmp_path):
        path = tmp_path / "n.md"
        path.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n\r\nthree")
        assert read_notes(path) == "one\ntwo\n\nthree"

    def test_non_ascii_text_is_read(self, tmp_path):
        path = tmp_path / "n.txt"
        path.write_text("Café – naïve", encoding="utf-8")
        assert read_notes(path) == "Café – naïve"

    def test_pdf_is_rejected(self, tmp_path):
        path = tmp_path / "n.pdf"
        path.write_bytes(b"%PDF-1.4")
        with pytest.raises(IngestError, match="unsupported"):
            read_notes(path)

    def test_missing_file_is_rejected(self, tmp_path):
        with pytest.raises(IngestError, match="cannot read"):
            read_notes(tmp_path / "missing.txt")

    def test_directory_is_rejected(self, tmp_path):
        folder = tmp_path / "notes.txt"
        folder.mkdir()
        with pytest.raises(IngestError):
            read_notes(folder)

    @pytest.mark.parametrize("content", [b"", b"  \n\r\n\t "])
    def test_empty_file_is_rejected(self, tmp_path, content):
        path = tmp_path / "n.txt"
        path.write_bytes(content)
        with pytest.raises(IngestError, match="empty"):
            read_notes(path)

    def test_invalid_utf8_is_rejected(self, tmp_path):
        path = tmp_path / "n.txt"
        path.write_bytes(b"ok \xff\xfe broken")
        with pytest.raises(IngestError, match="UTF-8"):
            read_notes(path)

    def test_ingest_error_is_a_flashcards_error(self):
        assert issubclass(IngestError, FlashcardsError)


class TestLoadChunks:
    def test_returns_indexed_chunks_with_digests(self, tmp_path):
        path = tmp_path / "n.md"
        path.write_text(sample_notes(), encoding="utf-8")
        chunks = load_chunks(path, max_chars=1000)
        assert len(chunks) > 1
        assert all(isinstance(chunk, Chunk) for chunk in chunks)
        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
        assert all(chunk.digest == digest_of(chunk.text) for chunk in chunks)
        assert squash("".join(c.text for c in chunks)) == squash(sample_notes())

    def test_crlf_file_equals_lf_file(self, tmp_path):
        lf = tmp_path / "lf.md"
        crlf = tmp_path / "crlf.md"
        lf.write_bytes(sample_notes().encode())
        crlf.write_bytes(sample_notes().replace("\n", "\r\n").encode())
        assert load_chunks(lf, 800) == load_chunks(crlf, 800)

    def test_whitespace_only_edit_keeps_digests(self, tmp_path):
        before = tmp_path / "a.txt"
        after = tmp_path / "b.txt"
        before.write_text("alpha beta gamma", encoding="utf-8")
        after.write_text("alpha   beta\ngamma\n", encoding="utf-8")
        assert load_chunks(before)[0].digest == load_chunks(after)[0].digest

    def test_default_max_chars_is_4000(self, tmp_path):
        path = tmp_path / "n.txt"
        path.write_text("a" * 4001, encoding="utf-8")
        assert [len(chunk.text) for chunk in load_chunks(path)] == [4000, 1]

    def test_errors_from_read_notes_propagate(self, tmp_path):
        with pytest.raises(IngestError):
            load_chunks(tmp_path / "missing.md")
