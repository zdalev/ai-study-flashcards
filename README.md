# AI study flashcards

Turn your notes into flashcards with Claude, then study them in the browser with SM-2 spaced
repetition.

- `flashcards generate` reads a `.txt` / `.md` notes file, sends it to the Claude API in chunks
  and stores the question/answer cards it gets back (as structured JSON) in a local SQLite
  database. One notes file becomes one deck (subject).
- `flashcards serve` starts a small Flask web app: pick a subject, answer the cards that are due,
  grade yourself 0-5, and the SM-2 scheduler decides when each card comes back.
- Five sample subjects are included: **biology, english, mathematics, physics, programming**
  (`examples/`), plus a ready-made demo deck so you can try the app **without an API key**.

Requires Python 3.11 or newer.

## Quick demo (no API key needed)

```bash
git clone https://github.com/zdalev/ai-study-flashcards.git
cd ai-study-flashcards
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
flashcards load-demo               # loads examples/demo-cards.json into flashcards.db
flashcards serve
```

Open <http://127.0.0.1:5000>, choose a subject and click **Study**. Stop the server with Ctrl+C.

`load-demo` can be run again safely: cards that already exist are not added twice.

## Generate your own deck (needs a Claude API key)

```bash
cp .env.example .env               # then set ANTHROPIC_API_KEY=... in .env
flashcards generate examples/biology.md
flashcards serve
```

- The deck is named after the file (`biology`); use `--deck NAME` to choose another name.
- The notes are cut into chunks of up to 4000 characters, and Claude makes up to 8 cards per chunk.
- Each chunk is saved as soon as its cards arrive. Running the same command again **skips the
  chunks that are already done**, so it continues after an error and costs nothing when the deck
  is up to date. A chunk that returned no cards is retried on the next run.
- `.env` is git-ignored. Never commit your key.

### Rebuilding the demo data

`examples/demo-cards.json` is generated from the five sample notes files with the real API:

```bash
python scripts/build_demo.py       # needs ANTHROPIC_API_KEY in .env
```

It overwrites the file; skim the cards before committing it.

## Web pages

| Page | URL | What it shows |
|---|---|---|
| Home | `/` | All subjects with their card count and how many cards are due today, each with **Study** and **Stats** links. An empty database shows a hint to run `flashcards load-demo`. |
| Quiz | `/decks/<id>/quiz` | The next due card (most overdue first) and the number of cards due. **Show answer** reveals the answer and the grade buttons **0 Blackout, 1 Wrong, 2 Wrong but familiar, 3 Hard, 4 Good, 5 Perfect**. After grading, the next card appears with a note when the previous one is due again. |
| Done | (same URL) | Shown when nothing is due in the subject: cards reviewed today, accuracy today and the next due date. |
| Stats | `/decks/<id>/stats` | One subject: accuracy today and all time, cards, due today, reviewed today, mature cards (interval of 21 days or more), new cards, average ease, next due date. |
| All stats | `/stats` | The same figures for all subjects together. |

A grade of 3 or more counts as a correct answer for accuracy.

## Commands

```
flashcards [--db DB] [--today YYYY-MM-DD] [--version] <command> ...
```

| Command | Usage | What it does |
|---|---|---|
| `generate` | `flashcards generate [--deck DECK] NOTES` | Make cards from a notes file (`.txt`, `.md`) with Claude. Needs an API key. |
| `load-demo` | `flashcards load-demo [JSON]` | Load the demo decks (default `examples/demo-cards.json`, relative to the current folder). No key needed. |
| `export` | `flashcards export [--deck DECK] OUT.csv` | Write all cards (or one deck) with their scheduling state to a CSV file; `-` writes to standard output. |
| `serve` | `flashcards serve [--host HOST] [--port PORT]` | Start the web app (default `127.0.0.1:5000`, debug off). No key needed. |

- `--db DB` (database file) and `--today YYYY-MM-DD` (pretend today is this date, for demos) work
  before or after the command: `flashcards --db demo.db serve` and `flashcards serve --db demo.db`
  are the same.
- `flashcards --help` and `flashcards <command> --help` show the full usage.
- Exit codes: 0 = ok, 1 = error (one `error: ...` line on stderr), 2 = wrong usage.

## Configuration

Settings come from environment variables or a `.env` file in the current folder (real environment
variables win). See `.env.example`.

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | `generate`, `scripts/build_demo.py` | Your Claude API key. Not needed for the demo or the web app. |
| `FLASHCARDS_MODEL` | `claude-haiku-5-5` | `generate`, `scripts/build_demo.py` | Claude model that writes the cards. |
| `FLASHCARDS_DB` | `flashcards.db` | all commands | SQLite database file (`--db` overrides it). |
| `FLASHCARDS_TODAY` | real date | all commands | Pretend today is this date, `YYYY-MM-DD` (`--today` overrides it). |

## How it works

```
notes file -> ingest -> generator (Claude) -> pipeline -> storage (SQLite) <- web (Flask) -> scheduler (SM-2)
```

| Module (`src/flashcards/`) | Job |
|---|---|
| `ingest.py` | Reads a UTF-8 `.txt` / `.md` file and cuts it into chunks at paragraph boundaries (headings stay with their text). |
| `generator.py` | Sends one chunk to Claude with a JSON schema as the output format and turns the reply into cards. The only module that imports the `anthropic` SDK. |
| `pipeline.py` | Fills a deck from a notes file chunk by chunk, skipping chunks already done; loads the demo JSON. |
| `storage.py` | SQLite: decks, cards with their review state, finished chunks and a review log for accuracy. Each review is saved in one transaction. |
| `scheduler.py` | Pure SM-2 functions (no I/O, "today" is passed in). |
| `web.py` + `templates/` | The Flask app (one database connection per request). It never imports the `anthropic` SDK, so it needs no key; a test checks this. |
| `export.py` | CSV export. |
| `cli.py` | The `flashcards` command (`argparse`). |
| `config.py` | Settings from the environment and `.env`. |

**SM-2 in brief.** Each card has an ease factor (EF, starts at 2.5), an interval and a repetition
count. Grade 3-5 (correct): the next interval is 1 day, then 6 days, then the previous interval
x EF, rounded half up. Grade 0-2 (wrong): the card starts over and is due tomorrow. After every
grade the EF changes (+0.10 for 5, 0 for 4, -0.14 for 3, -0.32 for 2, -0.54 for 1, -0.80 for 0)
and never drops below 1.3.

## Testing

```bash
pytest            # 455 tests, about 2 seconds
ruff check .
```

The tests never call the real Claude API: the LLM is always replaced by a fake client, and a guard
in `tests/conftest.py` fails any test that tries to create a real `anthropic.Anthropic` client. Web
pages are tested with Flask's test client against a temporary database and a fixed date.

## Known limitations

- A local study tool for one person, not a hosted service.
- `flashcards serve` uses Flask's development server and listens on `127.0.0.1` only.
- No login and no CSRF protection.
- Duplicate cards are detected by question text within a deck (spaces normalised,
  case-insensitive for ASCII letters only, via SQLite `NOCASE`); reworded questions are kept as
  separate cards.
- One deck per notes file: to add more notes to a subject, run `generate` with the same `--deck`
  name.

## Screenshots

Screenshots will be added.

## License

MIT - see [LICENSE](LICENSE).
