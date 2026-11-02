"""Command line entry point: generation and admin commands (studying happens in the web app)."""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any

from flashcards import __version__
from flashcards.config import load_settings
from flashcards.errors import FlashcardsError
from flashcards.models import CardDraft
from flashcards.pipeline import CardSource, GenerateReport, generate_deck
from flashcards.storage import CardStore, open_db


def _make_claude_client(api_key: str | None) -> Any:
    """Default client factory. Imports `generator` (and so `anthropic`) only when called."""
    from flashcards.generator import make_client

    return make_client(api_key)


@dataclass(frozen=True)
class Deps:
    """Things tests replace: the clock and the Claude client factory."""

    today: Callable[[], date] = date.today
    now: Callable[[], datetime] = datetime.now
    # Takes the API key (or None) and returns a client; the default builds the real Claude client.
    client_factory: Callable[[str | None], Any] = _make_claude_client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flashcards",
        description="Generate flashcards from notes with Claude and study them in the browser.",
    )
    parser.add_argument("--db", type=Path, help="SQLite database file (default: from settings)")
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="pretend today is this date (demos and tests)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    generate = commands.add_parser("generate", help="make flashcards from a notes file with Claude")
    generate.add_argument("notes", type=Path, metavar="NOTES", help="notes file (.txt, .md)")
    generate.add_argument("--deck", help="deck name (default: the file name without extension)")
    return parser


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    """Run the CLI. Returns 0 = ok, 1 = handled error, 2 = usage error."""
    deps = deps or Deps()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:  # argparse exits for --help, --version and usage errors
        return exit_request.code if isinstance(exit_request.code, int) else 1
    try:
        return _run(args, parser, deps)
    except FlashcardsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser, deps: Deps) -> int:
    """Dispatch to the chosen command (argparse has already rejected unknown command names)."""
    if args.command is None:
        parser.print_help()
        return 0
    return _COMMANDS[args.command](args, deps)


def _cmd_generate(args: argparse.Namespace, deps: Deps) -> int:
    settings = load_settings()
    today = args.today or deps.today()

    card_source = _lazy_claude_source(deps.client_factory, settings.api_key, settings.model)
    with CardStore(open_db(args.db or settings.db_path)) as store:
        report = generate_deck(
            args.notes,
            store,
            card_source,
            deck_name=args.deck,
            today=today,
            now=deps.now(),
            on_progress=print,
        )
    print(_summary(report))
    return 0


def _lazy_claude_source(
    client_factory: Callable[[str | None], Any], api_key: str | None, model: str
) -> CardSource:
    """A card source that builds the client on its first call.

    So a bad notes file, or a deck that is already up to date, never needs an API key.
    """
    source: CardSource | None = None

    def card_source(chunk_text: str) -> list[CardDraft]:
        nonlocal source
        if source is None:
            from flashcards.generator import generate_cards

            client = client_factory(api_key)
            source = partial(generate_cards, client=client, model=model)
        return source(chunk_text)

    return card_source


def _summary(report: GenerateReport) -> str:
    if report.up_to_date:
        return f"deck '{report.deck_name}' is up to date"
    summary = (
        f"deck '{report.deck_name}': {report.cards_added} new cards from "
        f"{report.chunks_generated} of {report.chunks_total} chunks"
    )
    if report.chunks_skipped:
        summary += f" ({report.chunks_skipped} already done)"
    if report.chunks_empty:
        summary += f"; {report.chunks_empty} chunks returned no cards, run again to retry them"
    return summary


_COMMANDS: dict[str, Callable[[argparse.Namespace, Deps], int]] = {"generate": _cmd_generate}
