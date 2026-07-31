"""Shared test doubles for driving the agent loop without the Claude API."""

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
