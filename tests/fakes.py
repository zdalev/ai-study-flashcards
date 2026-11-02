"""Fakes for the Claude client: nothing here talks to the network."""

import json
from types import SimpleNamespace


class FakeMessages:
    """Records the kwargs of every `create` call and returns/raises queued items in order."""

    def __init__(self, queue):
        self.calls = []
        self._queue = list(queue)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError("FakeClient: no queued response left")
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    """Stands in for `anthropic.Anthropic`. Queue responses or exceptions, in call order."""

    def __init__(self, *queue, api_key="sk-ant-fake-key"):
        self.api_key = api_key
        self.auth_token = None
        self.credentials = None
        self.messages = FakeMessages(queue)

    @property
    def calls(self):
        return self.messages.calls


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def thinking_block():
    # Like the real ThinkingBlock: no `text` attribute, and empty thinking by default.
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def response(*blocks, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, stop_details=stop_details)


def cards_response(cards, **kwargs):
    """A normal response whose text block holds `{"cards": cards}` as JSON."""
    return response(text_block(json.dumps({"cards": cards})), **kwargs)
