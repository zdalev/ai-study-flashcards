"""Error types. The CLI turns any FlashcardsError into one `error: ...` line and exit code 1."""


class FlashcardsError(Exception):
    """Base class for every handled error."""


class IngestError(FlashcardsError):
    """A notes file could not be read."""


class StorageError(FlashcardsError):
    """A database operation failed."""


class GenerationError(FlashcardsError):
    """Card generation with Claude failed."""


class ApiKeyError(GenerationError):
    """The Claude API key is missing or was rejected. Not tied to one chunk, so never wrapped."""


class DemoDataError(FlashcardsError):
    """The demo deck file is missing, is not valid JSON, or has the wrong shape."""


class ConfigError(FlashcardsError):
    """A setting in the environment or `.env` has an invalid value."""
