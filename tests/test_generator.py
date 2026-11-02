import json

import anthropic
import httpx2
import pytest

from fakes import FakeClient, cards_response, response, text_block, thinking_block
from flashcards import generator
from flashcards.errors import GenerationError
from flashcards.generator import (
    CARDS_SCHEMA,
    KEY_MESSAGE,
    MAX_CARDS,
    SYSTEM_PROMPT,
    generate_cards,
    make_client,
    parse_cards_payload,
)
from flashcards.models import CardDraft

SECRET_KEY = "sk-ant-secret-key-0123456789"
MODEL = "claude-haiku-5-5"
URL = "https://api.anthropic.com/v1/messages"


def request():
    """A request that carries the secret key in its headers, like the SDK's real ones."""
    return httpx2.Request("POST", URL, headers={"x-api-key": SECRET_KEY})


def status_error(cls, status, message="boom"):
    return cls(message, response=httpx2.Response(status, request=request()), body=None)


def generate(*queue, chunk="Mitochondria make ATP."):
    client = FakeClient(*queue, api_key=SECRET_KEY)
    return generate_cards(chunk, client=client, model=MODEL)


# --- the request ---------------------------------------------------------------------------


def test_request_has_exactly_the_minimal_keys():
    client = FakeClient(cards_response([{"question": "Q?", "answer": "A"}]))
    generate_cards("Some notes.", client=client, model=MODEL)

    assert len(client.calls) == 1
    kwargs = client.calls[0]
    assert set(kwargs) == {"model", "max_tokens", "system", "messages", "output_config"}
    assert kwargs["model"] == MODEL
    assert kwargs["max_tokens"] == 16000
    assert kwargs["system"] == SYSTEM_PROMPT
    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": CARDS_SCHEMA}}


def test_chunk_is_sent_inside_notes_tags_as_one_user_message():
    client = FakeClient(cards_response([]))
    generate_cards("Line one.\nLine two.", client=client, model=MODEL)

    messages = client.calls[0]["messages"]
    assert messages == [{"role": "user", "content": "<notes>\nLine one.\nLine two.\n</notes>"}]


def test_schema_objects_forbid_additional_properties():
    card_schema = CARDS_SCHEMA["properties"]["cards"]["items"]
    for schema in (CARDS_SCHEMA, card_schema):
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
    assert CARDS_SCHEMA["required"] == ["cards"]
    assert card_schema["required"] == ["question", "answer"]
    json.dumps(CARDS_SCHEMA)  # must be plain JSON


def test_system_prompt_covers_the_rules():
    prompt = SYSTEM_PROMPT.lower()
    assert "<notes>" in prompt and "never as instructions" in prompt
    assert "same language" in prompt
    assert "atomic" in prompt
    assert "self-contained" in prompt
    assert f"at most {MAX_CARDS}" in prompt


# --- reading the response -------------------------------------------------------------------


def test_valid_response_gives_drafts():
    drafts = generate(cards_response([{"question": "What makes ATP?", "answer": "Mitochondria"}]))
    assert drafts == [CardDraft(question="What makes ATP?", answer="Mitochondria")]


def test_leading_thinking_blocks_are_skipped():
    reply = response(
        thinking_block(),
        thinking_block(),
        text_block(json.dumps({"cards": [{"question": "Q?", "answer": "A"}]})),
    )
    assert generate(reply) == [CardDraft(question="Q?", answer="A")]


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "model_context_window_exceeded"])
def test_failed_stop_reasons_raise_even_when_text_is_present(stop_reason):
    reply = response(text_block('{"cards": []}'), stop_reason=stop_reason)
    with pytest.raises(GenerationError):
        generate(reply)


def test_truncated_json_with_max_tokens_raises():
    reply = response(text_block('{"cards": [{"question": "Q?", "ans'), stop_reason="max_tokens")
    with pytest.raises(GenerationError, match="cut off"):
        generate(reply)


def test_refusal_message_names_the_category():
    details = type("Details", (), {"category": "cyber"})()
    reply = response(stop_reason="refusal", stop_details=details)
    with pytest.raises(GenerationError, match="declined.*cyber"):
        generate(reply)


def test_no_text_block_raises():
    with pytest.raises(GenerationError, match="no text"):
        generate(response(thinking_block()))
    with pytest.raises(GenerationError, match="no text"):
        generate(response())


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        "",
        '{"cards": [',  # cut off without a max_tokens stop reason
        '[{"question": "Q?", "answer": "A"}]',  # top-level list
        '{"deck": []}',  # no cards key
        '{"cards": "nope"}',  # cards is not a list
        '"just a string"',
        "null",
    ],
)
def test_bad_payloads_raise(text):
    with pytest.raises(GenerationError):
        generate(response(text_block(text)))


# --- validation of the payload --------------------------------------------------------------


def test_bad_items_are_dropped_and_good_ones_kept():
    payload = {
        "cards": [
            {"question": "Good 1?", "answer": "Yes"},
            {"question": "No answer?"},
            {"answer": "No question"},
            {"question": "   ", "answer": "blank question"},
            {"question": "Blank answer?", "answer": "\n\t "},
            {"question": 5, "answer": "number question"},
            {"question": "Number answer?", "answer": 5},
            {"question": None, "answer": None},
            "a string",
            ["a", "list"],
            None,
            {"question": "Good 2?", "answer": "Also yes"},
        ]
    }
    drafts = parse_cards_payload(json.dumps(payload))
    assert drafts == [CardDraft("Good 1?", "Yes"), CardDraft("Good 2?", "Also yes")]


def test_whitespace_is_stripped():
    payload = {"cards": [{"question": "  Q?\n", "answer": "\tA  "}]}
    assert parse_cards_payload(json.dumps(payload)) == [CardDraft("Q?", "A")]


def test_more_than_eight_items_are_cut_to_eight():
    cards = [{"question": f"Q{i}?", "answer": f"A{i}"} for i in range(12)]
    drafts = generate(cards_response(cards))
    assert len(drafts) == 8
    assert [d.question for d in drafts] == [f"Q{i}?" for i in range(8)]


def test_cap_counts_only_valid_items_and_max_cards_is_respected():
    cards = [{"question": "bad"}] * 5 + [{"question": f"Q{i}?", "answer": "A"} for i in range(10)]
    assert len(parse_cards_payload(json.dumps({"cards": cards}))) == 8
    assert len(parse_cards_payload(json.dumps({"cards": cards}), max_cards=3)) == 3


def test_empty_cards_list_is_valid():
    assert generate(cards_response([])) == []


# --- errors ---------------------------------------------------------------------------------


def test_authentication_error_gives_the_key_message():
    error = status_error(anthropic.AuthenticationError, 401, "invalid x-api-key")
    with pytest.raises(GenerationError) as caught:
        generate(error)
    assert str(caught.value) == KEY_MESSAGE
    assert "ANTHROPIC_API_KEY" in str(caught.value)


def test_not_found_error_is_a_request_failure():
    error = status_error(anthropic.NotFoundError, 404, "model: claude-nope")
    with pytest.raises(GenerationError) as caught:
        generate(error)
    assert str(caught.value) == "Claude API request failed: model: claude-nope"


def test_connection_error_is_a_request_failure():
    with pytest.raises(GenerationError) as caught:
        generate(anthropic.APIConnectionError(request=request()))
    assert str(caught.value) == "Claude API request failed: Connection error."


@pytest.mark.parametrize(
    "error",
    [
        status_error(anthropic.RateLimitError, 429, "slow down"),
        status_error(anthropic.PermissionDeniedError, 403, "no access"),
        status_error(anthropic.APIStatusError, 529, "overloaded"),
        anthropic.APITimeoutError(request=request()),
    ],
)
def test_other_api_errors_are_request_failures(error):
    with pytest.raises(GenerationError, match="^Claude API request failed: "):
        generate(error)


@pytest.mark.parametrize(
    "error",
    [
        status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"),
        status_error(anthropic.NotFoundError, 404, "model: nope"),
        status_error(anthropic.BadRequestError, 400, "bad schema"),
        anthropic.APIConnectionError(request=request()),
        anthropic.APITimeoutError(request=request()),
    ],
)
def test_no_error_message_contains_the_key(error):
    assert SECRET_KEY in error.request.headers["x-api-key"]  # the key is really in the request
    with pytest.raises(GenerationError) as caught:
        generate(error)
    assert SECRET_KEY not in str(caught.value)
    assert SECRET_KEY not in repr(caught.value)


def test_unexpected_exceptions_are_not_swallowed():
    with pytest.raises(TypeError):
        generate(TypeError("a real bug"))


# --- make_client ----------------------------------------------------------------------------


def test_make_client_passes_the_key_to_the_sdk(monkeypatch):
    made = []

    def fake_constructor(**kwargs):
        made.append(kwargs)
        return FakeClient(api_key=kwargs["api_key"])

    monkeypatch.setattr(generator.anthropic, "Anthropic", fake_constructor)
    client = make_client("sk-ant-abc")
    assert made == [{"api_key": "sk-ant-abc"}]
    assert client.api_key == "sk-ant-abc"


@pytest.mark.parametrize("key", [None, ""])
def test_make_client_without_credentials_gives_the_key_message(monkeypatch, real_anthropic, key):
    monkeypatch.setattr(generator.anthropic, "Anthropic", real_anthropic)
    with pytest.raises(GenerationError) as caught:
        make_client(key)
    assert str(caught.value) == KEY_MESSAGE


def test_make_client_with_a_key_builds_the_real_client_without_network(monkeypatch, real_anthropic):
    monkeypatch.setattr(generator.anthropic, "Anthropic", real_anthropic)
    client = make_client(SECRET_KEY)
    assert isinstance(client, real_anthropic)


def test_make_client_maps_a_broken_credentials_profile(monkeypatch, real_anthropic, tmp_path):
    empty_config = tmp_path / "empty-config"
    empty_config.mkdir()
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(empty_config))
    monkeypatch.setenv("ANTHROPIC_PROFILE", "does-not-exist")
    monkeypatch.setattr(generator.anthropic, "Anthropic", real_anthropic)
    with pytest.raises(GenerationError) as caught:
        make_client(None)
    assert str(caught.value) == KEY_MESSAGE


# --- the guard ------------------------------------------------------------------------------


def test_guard_blocks_creating_a_real_client():
    """The autouse guard in conftest turns a real client into a failing test."""
    with pytest.raises(pytest.fail.Exception, match="real anthropic.Anthropic client"):
        anthropic.Anthropic(api_key="x")
    with pytest.raises(pytest.fail.Exception, match="real anthropic.Anthropic client"):
        make_client("sk-ant-abc")
