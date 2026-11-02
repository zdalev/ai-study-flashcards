"""Dev-only: build examples/demo-cards.json from the sample notes with the real Claude API.

Run it from anywhere: `.venv/bin/python scripts/build_demo.py` (needs ANTHROPIC_API_KEY in .env).
It makes a handful of API calls (well under a cent). Skim the cards before committing the file.
"""

import json
import os
import sys
from functools import partial
from pathlib import Path

from flashcards.config import load_settings
from flashcards.errors import FlashcardsError
from flashcards.generator import generate_cards, make_client
from flashcards.ingest import SUPPORTED_SUFFIXES
from flashcards.pipeline import build_demo_data

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = Path("examples/demo-cards.json")


def main() -> int:
    os.chdir(ROOT)  # so .env is found and the "source" paths in the JSON are examples/<file>
    notes = sorted(p for p in Path("examples").iterdir() if p.suffix in SUPPORTED_SUFFIXES)
    try:
        settings = load_settings()
        client = make_client(settings.api_key)
        source = partial(generate_cards, client=client, model=settings.model)
        data = build_demo_data(notes, source)
    except FlashcardsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    OUTPUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for deck in data["decks"]:
        print(f"{deck['name']}: {len(deck['cards'])} cards")
    print(f"wrote {OUTPUT} - skim the cards before committing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
