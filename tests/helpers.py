"""Shared test doubles for driving the agent loop without the Claude API."""

import re
from types import SimpleNamespace


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(name, input, id="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def usage(inp=1000, out=500):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )


def response(content, stop_reason="tool_use", usage_=None):
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage_ or usage())


class FakeClient:
    """Plays back scripted responses in place of the Claude API."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        return next(self._responses)


class TicketAwareFakeClient:
    """Replies to whichever ticket the prompt names, so overlapping runs can share one
    client and still issue genuinely concurrent writes."""

    def __init__(self):
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, *, messages, **kw):
        # ticket_prompt renders "New support ticket #<id>" as the first line.
        ticket_id = int(re.search(r"[Tt]icket #?(\d+)", messages[0]["content"]).group(1))
        if len(messages) == 1:
            # 40 chars clears SendReplyInput's min_length=20 on the first turn.
            return response(
                [tool_use_block("send_reply", {"ticket_id": ticket_id, "body": "z" * 40})]
            )
        return response([text_block("done")], stop_reason="end_turn")
