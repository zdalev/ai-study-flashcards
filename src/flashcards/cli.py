"""Command line entry point: generation and admin commands (studying happens in the web app)."""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flashcards import __version__
from flashcards.errors import FlashcardsError


@dataclass(frozen=True)
class Deps:
    """Things tests replace: the clock and the Claude client factory."""

    today: Callable[[], date] = date.today
    now: Callable[[], datetime] = datetime.now
    # Takes the API key (or None) and returns a client. Wired to generator.make_client in 2.2.
    client_factory: Callable[[str | None], Any] | None = None


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
    parser.add_subparsers(dest="command", metavar="<command>")  # commands arrive in later batches
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
    """Dispatch to the chosen command. No commands exist yet (added in batches 2.2 to 2.4)."""
    if args.command is None:
        parser.print_help()
        return 0
    parser.error(f"unknown command {args.command!r}")  # exits with code 2
    return 2
