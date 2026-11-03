# AI study flashcards

Turn your notes into flashcards with Claude, then study them in the browser with SM-2 spaced
repetition.

- `flashcards generate` reads a `.txt` / `.md` notes file, sends it to the Claude API in chunks
  and stores the question/answer cards it gets back (as structured JSON) in a local SQLite
  database. One notes file becomes one deck (subject).
- `flashcards serve` starts a small Flask web app: pick a subject and study the cards that are due,
  either by typing the answer or by choosing from four options. The SM-2 scheduler decides when
  each card comes back, and the subject's stats page lists your recent answers. You can also add
  your own subjects and cards by hand in the browser.
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

Open <http://127.0.0.1:5000>, choose a subject and click **Study: Write** or **Study: Multiple choice**. Stop the server with Ctrl+C.

`load-demo` can be run again safely: cards that already exist are not added twice.

## Add your own cards

No notes file or API key is needed to write cards yourself. On the home page click **New subject**
and type a name (or click **Add card** next to an existing subject), then fill in the question and
the answer and click **Add card**. The new card is due today, so it shows up in the quiz straight
away, and the form stays open so you can add another. Click **Done** when you are finished to go back
to the home page.

- A question and an answer are both required (at most 500 and 2000 characters). A subject name is at
  most 100 characters.
- A question that is already in the subject (ignoring case and extra spaces) is not added twice; the
  page says so and keeps what you typed.
- A subject name that already exists takes you to that subject's form instead of making a second one.
- There is no way to edit or delete a single card in the app. To remove a whole subject, see below.

## Delete a subject

Open a subject's **Stats** page and click **Delete subject**. A confirmation page shows how many
cards will go; confirming removes the subject with all its cards and their history (reviews and
the record of imported note chunks). This cannot be undone. The home page then shows a short notice.

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
| Home | `/` | All subjects with their card count and how many cards are due today, each with **Study: Write**, **Study: Multiple choice**, **Add card** and **Stats** links, and a **New subject** button. An empty database shows a hint to run `flashcards load-demo` or to create a subject. |
| New subject | `/decks/new` | A form with a subject name. Creates the subject (or reuses one with the same name) and opens its Add card form. |
| Add card | `/decks/<id>/cards/new` | A form with a question and an answer. The card is due today. After saving, the form opens again with a "Card added" note. A **Done** button next to **Add card** goes back to the home page. Linked from the home, stats and done pages. |
| Quiz | `/decks/<id>/quiz?mode=write` | The next due card (most overdue first) and the number of cards due. **Write mode:** type your answer (it may be empty) and click **Save answer**; the page then shows your answer next to the correct one and you grade yourself with **Do not know**, **Wrong**, **Partially correct** or **Correct**. |
| Quiz, multiple choice | `/decks/<id>/quiz?mode=choice` | The question with four options (the correct answer and three different answers from other cards of the same subject, in an order that stays the same when you refresh) and a **Do not know** button. The answer is graded automatically and saved straight away; the result page shows your choice, the correct answer and **Correct**, **Wrong** or **Do not know**, with a **Next card** link. A subject with fewer than four different answers uses write mode instead, with a short note. |
| Done | (same URL) | Shown when nothing is due in the subject: cards reviewed today, accuracy today and the next due date. |
| Stats | `/decks/<id>/stats` | One subject: accuracy today and all time, cards, due today, reviewed today, mature cards (interval of 21 days or more), new cards, average ease, next due date, and a **Recent answers** table with the last 20 answers (date, mode, question, your answer, correct answer, result; older answers saved before this table existed show `-`). |
| Delete subject | `/decks/<id>/delete` | A confirmation page ("Delete <name>? This removes N cards and their history."). **Delete subject** (POST) removes the subject, its cards, chunk records and reviews and returns to the home page with a notice; **Cancel** goes back to the stats page. A GET never deletes. Linked from the subject's stats page only. |
| All stats | `/stats` | The same figures for all subjects together. |

The write-mode buttons are saved as SM-2 grades: **Do not know** = 0, **Wrong** = 1, **Partially
correct** = 2 and **Correct** = 4. A correct answer keeps the ease factor, so the intervals go
1, 6, 15, 38 ... days. A partially correct answer counts as a miss: the card comes back tomorrow
and its ease drops from 2.5 to 2.18. Multiple choice gives three of these grades automatically: the
right option is **Correct**, any other option **Wrong**, and the **Do not know** button **Do not know**. A grade of
3 or more counts as a correct answer for accuracy, so Partially correct counts as a miss. The
**Recent answers** table shows grade 0 as Do not know, 1 as Wrong, 2 as Partially correct and 3-5
as Correct.

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
pytest            # 676 tests, about 2 seconds
ruff check .
```

The tests never call the real Claude API: the LLM is always replaced by a fake client, and a guard
in `tests/conftest.py` fails any test that tries to create a real `anthropic.Anthropic` client. Web
pages are tested with Flask's test client against a temporary database and a fixed date.

## Known limitations

- A local study tool for one person, not a hosted service.
- `flashcards serve` uses Flask's development server and listens on `127.0.0.1` only by default (`--host` can change it).
- No login and no CSRF protection. Cards you add by hand cannot be edited or deleted in the app.
- Duplicate cards are detected by question text within a deck (spaces normalised,
  case-insensitive for ASCII letters only, via SQLite `NOCASE`); reworded questions are kept as
  separate cards.
- One deck per notes file: to add more notes to a subject, run `generate` with the same `--deck`
  name.

## Screenshots

Screenshots will be added.

## License

MIT - see [LICENSE](LICENSE).
