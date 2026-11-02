"""Read notes files and cut them into chunks for card generation. Pure apart from `read_notes`."""

import hashlib
import re
from pathlib import Path

from flashcards.errors import IngestError
from flashcards.models import Chunk

SUPPORTED_SUFFIXES = (".txt", ".md", ".markdown")
DEFAULT_MAX_CHARS = 4000
MIN_MAX_CHARS = 200

_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_HEADING = re.compile(r"#{1,6}\s+\S")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def read_notes(path: Path | str) -> str:
    """Return the text of a notes file with LF line endings.

    Raises IngestError for a missing file, an unsupported extension, bytes that are not
    UTF-8, or a file with no text in it.
    """
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        supported = ", ".join(SUPPORTED_SUFFIXES)
        raise IngestError(f"unsupported file type '{path.suffix}' (use {supported}): {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IngestError(f"cannot read notes file {path}: {exc.strerror or exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestError(f"notes file is not valid UTF-8: {path}") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise IngestError(f"notes file is empty: {path}")
    return text


def chunk_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Cut text into chunks of at most `max_chars`, without splitting paragraphs if possible.

    Paragraphs (blocks separated by blank lines) are packed greedily. A heading line is kept
    with the block after it. A block that is too long is split by line, then by sentence,
    then cut hard.
    """
    if max_chars < MIN_MAX_CHARS:
        raise ValueError(f"max_chars must be at least {MIN_MAX_CHARS}, got {max_chars}")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[str] = []
    current = ""
    for unit in _glue_headings(_blocks(text)):
        for piece in _split_to_fit(unit, max_chars):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= max_chars:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def load_chunks(path: Path | str, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Read a notes file and return its chunks with index and content digest."""
    pieces = chunk_text(read_notes(path), max_chars)
    return [Chunk(index=i, text=piece, digest=digest_of(piece)) for i, piece in enumerate(pieces)]


def digest_of(text: str) -> str:
    """sha256 of the text with all whitespace runs collapsed to one space."""
    normalised = " ".join(text.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _blocks(text: str) -> list[str]:
    blocks = (block.strip() for block in _BLANK_LINE.split(text))
    return [block for block in blocks if block]


def _is_heading(block: str) -> bool:
    return "\n" not in block and _HEADING.match(block) is not None


def _glue_headings(blocks: list[str]) -> list[str]:
    """Join each heading block to the block after it (a trailing heading stays alone)."""
    units: list[str] = []
    pending: list[str] = []
    for block in blocks:
        pending.append(block)
        if not _is_heading(block):
            units.append("\n\n".join(pending))
            pending = []
    if pending:
        units.append("\n\n".join(pending))
    return units


def _split_to_fit(text: str, max_chars: int, level: int = 0) -> list[str]:
    """Split text into non-empty pieces of at most `max_chars`: by line, sentence, then hard cut."""
    if len(text) <= max_chars:
        return [text]
    if level == 0:
        parts, separator = text.split("\n"), "\n"
    elif level == 1:
        parts, separator = _SENTENCE_END.split(text), " "
    else:
        return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    pieces: list[str] = []
    current = ""
    for part in parts:
        for fragment in _split_to_fit(part, max_chars, level + 1):
            candidate = f"{current}{separator}{fragment}" if current else fragment
            if len(candidate) <= max_chars:
                current = candidate
            else:
                pieces.append(current)
                current = fragment
    pieces.append(current)
    stripped = (piece.strip() for piece in pieces)
    return [piece for piece in stripped if piece]
