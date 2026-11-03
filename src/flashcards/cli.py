"""Command line entry point: generation and admin commands (studying happens in the web app)."""

import argparse
import io
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, TextIO

from flashcards import __version__
from flashcards.config import load_settings
from flashcards.errors import DemoDataError, ExportError, FlashcardsError
from flashcards.export import export_csv
from flashcards.models import CardDraft
from flashcards.pipeline import CardSource, GenerateReport, count_noun, generate_deck, load_demo
from flashcards.storage import CardStore, open_db

DEFAULT_DEMO_FILE = Path("examples") / "demo-cards.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
STDOUT_NAME = "-"  # the OUT value that means "write to standard output"


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
    load = commands.add_parser("load-demo", help="load the demo decks (no API key needed)")
    load.add_argument(
        "json_file",
        nargs="?",
        type=Path,
        default=DEFAULT_DEMO_FILE,
        metavar="JSON",
        help=f"demo deck file (default: {DEFAULT_DEMO_FILE.as_posix()}, relative to the cwd)",
    )
    export = commands.add_parser("export", help="write all cards (or one deck) to a CSV file")
    export.add_argument(
        "out",
        metavar="OUT.csv",
        help=f"CSV file to write (overwritten if it exists), or {STDOUT_NAME} for standard output",
    )
    export.add_argument("--deck", help="export only this deck (default: all decks)")
    serve = commands.add_parser("serve", help="start the web app (study in the browser)")
    serve.add_argument(
        "--host", default=DEFAULT_HOST, help=f"address to listen on (default: {DEFAULT_HOST})"
    )
    serve.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help=f"port to listen on (default: {DEFAULT_PORT})",
    )
    return parser


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {text!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 1 and 65535, got {port}")
    return port


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


def _cmd_load_demo(args: argparse.Namespace, deps: Deps) -> int:
    """Load the demo decks. Reads settings only for the database path: no client, no API key."""
    data = _read_demo_json(args.json_file)
    settings = load_settings()
    with CardStore(open_db(args.db or settings.db_path)) as store:
        added = load_demo(store, data, args.today or deps.today(), deps.now())
    decks = len(data["decks"])  # load_demo has checked the shape
    print(f"Loaded {count_noun(added, 'card')} into {count_noun(decks, 'deck')}")
    return 0


def _cmd_export(args: argparse.Namespace, deps: Deps) -> int:
    """Export cards to CSV. Reads settings only for the database path: no client, no API key."""
    settings = load_settings()
    with CardStore(open_db(args.db or settings.db_path)) as store:
        deck_id = None
        if args.deck is not None:
            deck = store.find_deck(args.deck)
            if deck is None:
                raise ExportError(f"deck not found: {args.deck}")
            deck_id = deck.id
        try:
            with _open_csv_output(args.out) as out:
                exported = export_csv(store, out, deck_id)
        except OSError as error:
            raise ExportError(f"cannot write {args.out}: {error.strerror or error}") from error
    if args.out != STDOUT_NAME:  # keep stdout clean when it carries the CSV
        print(f"Exported {count_noun(exported, 'card')} to {args.out}", file=sys.stderr)
    return 0


@contextmanager
def _open_csv_output(target: str) -> Iterator[TextIO]:
    """Open the CSV target as UTF-8 text without newline translation (`-` = standard output)."""
    if target == STDOUT_NAME:
        # Wrap the byte stream so the encoding does not depend on the console's locale.
        sys.stdout.flush()
        out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="")
        try:
            yield out
        finally:
            out.flush()
            out.detach()  # leave sys.stdout's buffer open
        return
    with open(target, "w", encoding="utf-8", newline="") as file:
        yield file


def _cmd_serve(args: argparse.Namespace, deps: Deps) -> int:
    """Run the web app. Needs no API key; `anthropic` is never imported."""
    from flashcards.web import create_app

    config: dict[str, Any] = {}
    if args.db is not None:
        config["DATABASE"] = str(args.db)
    if args.today is not None:
        config["TODAY"] = args.today
    app = create_app(config)
    # debug stays off (no remote debugger); load_dotenv=False keeps .env out of os.environ.
    app.run(host=args.host, port=args.port, debug=False, load_dotenv=False)
    return 0


def _read_demo_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as error:
        raise DemoDataError(f"demo file not found: {path}") from error
    except OSError as error:
        raise DemoDataError(f"cannot read demo file {path}: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise DemoDataError(f"demo file is not valid UTF-8: {path}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise DemoDataError(f"demo file is not valid JSON ({error}): {path}") from error


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
        f"deck '{report.deck_name}': {count_noun(report.cards_added, 'new card')} from "
        f"{report.chunks_generated} of {count_noun(report.chunks_total, 'chunk')}"
    )
    if report.chunks_skipped:
        summary += f" ({report.chunks_skipped} already done)"
    if report.chunks_empty:
        summary += (
            f"; {count_noun(report.chunks_empty, 'chunk')} returned no cards, "
            "run again to retry them"
        )
    return summary


_COMMANDS: dict[str, Callable[[argparse.Namespace, Deps], int]] = {
    "generate": _cmd_generate,
    "load-demo": _cmd_load_demo,
    "export": _cmd_export,
    "serve": _cmd_serve,
}
