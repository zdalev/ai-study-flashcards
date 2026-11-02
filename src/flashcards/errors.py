"""Error types. The CLI turns any FlashcardsError into one `error: ...` line and exit code 1."""


class FlashcardsError(Exception):
    """Base class for every handled error."""


class IngestError(FlashcardsError):
    """A notes file could not be read."""


class StorageError(FlashcardsError):
    """A database operation failed."""


class GenerationError(FlashcardsError):
    """Card generation with Claude failed."""
